"""拼给模型的提示词。

把语义层里的口径契约原样交给模型 —— 模型不需要「理解」口径，
只需要照着抄。抄错了守卫会拦下来。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.semantic.loader import MetricRegistry, MetricSpec

from .guards import expanded_sql

_SYSTEM_TEMPLATE = """\
你是一个电商数据分析助手，负责把中文问题翻译成 DuckDB SQL。

输出要求：
1. 只输出一条 SELECT 语句。不要 markdown 代码块标记，不要解释，不要分号。
2. 指标的计算表达式必须和下面给出的完全一致，不要改写、不要四舍五入、不要换算单位。
3. WHERE 里必须包含「必带过滤」列出的每一条条件。
4. 需要的表都写在「数据来源」里；如果「必带 JOIN」非空，ON 条件也必须写进去。
5. 只能按「可用维度」里的字段 GROUP BY。时间维度用 DATE_TRUNC('粒度', 字段)。
6. 相对时间一律以数据末尾 {data_end} 为基准日，并且包含基准日当天。
   「最近 N 天」= 基准日往前 N-1 天到基准日，写成
   >= DATE '起点' AND < DATE '基准日次日'。
   例：最近 7 天 → o.paid_at >= DATE '{last7_start}' AND o.paid_at < DATE '{data_end_next}'
7. 问题里没给时间范围时，用「默认时间窗口」，按第 6 条同样的规则换算。
8. 输出列名用英文小写别名。
9. 时间范围只写进 WHERE。除非用户明说「按天/按周/按月/趋势/每天」，否则不要
   GROUP BY 时间维度，也不要把时间列放进 SELECT。
10. 除非用户明说「分别/各/每个/排名/从高到低/对比」，否则不要 GROUP BY 任何维度，
    直接返回一个总数。
11. 维度取值必须用「可用维度」里列出来的原值，不要翻译、不要补全、不要改写。

数据覆盖范围：{data_start} 到 {data_end}（历史快照，不是真实今天）。

【本次要算的指标】
key: {key}
名称: {name}
单位: {unit}
计算表达式: {expression}
展开后的完整表达式（直接照抄这一段）: {expanded}
数据来源: {source}
必带过滤: {filters}
必带 JOIN: {joins}
默认时间窗口: {default_window}
时间字段: {time_field}
口径说明: {description}
{components}
【可用维度】
{dimensions}
"""

_REPAIR_TEMPLATE = """\
上一条 SQL 出了问题（口径校验 / 执行 / 返回结果，卡在哪一步下面会写）：

{sql}

具体问题：
{error}

请修正后重新输出一条完整的 SQL。
"""


def build_messages(
    question: str,
    spec: MetricSpec,
    registry: MetricRegistry,
    *,
    bounds: tuple[date, date],
    previous_sql: str | None = None,
    error: str | None = None,
    value_hints: dict[str, list[str]] | None = None,
) -> list[dict[str, str]]:
    """返回 OpenAI 兼容格式的 messages。"""
    data_end = bounds[1]
    system = _SYSTEM_TEMPLATE.format(
        data_start=bounds[0].isoformat(),
        data_end=data_end.isoformat(),
        data_end_next=(data_end + timedelta(days=1)).isoformat(),
        last7_start=(data_end - timedelta(days=6)).isoformat(),
        key=spec.key,
        name=spec.name,
        unit=spec.unit,
        expression=spec.expression,
        expanded=expanded_sql(spec, registry),
        source=(spec.source or "").strip() or "（由组件指标提供，见下面的组件口径）",
        filters="\n  - ".join([""] + list(spec.required_filters))
        if spec.required_filters
        else "（无，由组件指标提供）",
        joins="\n  - ".join([""] + list(spec.required_joins))
        if spec.required_joins
        else "（无）",
        default_window=spec.default_window,
        time_field=spec.time_field,
        description=spec.description,
        components=_component_block(spec, registry),
        dimensions=_dimension_block(spec, registry, value_hints),
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    if previous_sql:
        messages.append(
            {
                "role": "user",
                "content": _REPAIR_TEMPLATE.format(
                    sql=previous_sql,
                    error=error or "（没有详细信息）",
                ),
            }
        )

    messages.append({"role": "user", "content": f"问题：{question}\n\n请给出 SQL。"})
    return messages


def _component_block(spec: MetricSpec, registry: MetricRegistry) -> str:
    """派生 / 比率指标要额外说明每个组件怎么算。

    否则模型只看到 ``refunded_orders / NULLIF(order_count, 0)``，
    既不知道 refunded_orders 该写成什么，也不知道要 JOIN refunds。
    """
    if not spec.is_derived:
        return ""

    lines = ["\n【组件口径】（上面的表达式由这些基础指标组成）"]
    for metric in _walk_dependencies(spec, registry):
        if metric.is_derived:
            continue
        lines.append(f"- {metric.key} = {metric.expression}")
        lines.append(f"    来源: {' '.join((metric.source or '').split())}")
        if metric.required_filters:
            lines.append(f"    必带过滤: {'; '.join(metric.required_filters)}")
        if metric.required_joins:
            lines.append(f"    必带 JOIN: {'; '.join(metric.required_joins)}")
    return "\n".join(lines) + "\n"


def _walk_dependencies(spec: MetricSpec, registry: MetricRegistry) -> list[MetricSpec]:
    seen: set[str] = set()
    ordered: list[MetricSpec] = []

    def walk(metric: MetricSpec) -> None:
        for key in metric.dependencies:
            if key in seen:
                continue
            seen.add(key)
            dependency = registry.get(key)
            ordered.append(dependency)
            walk(dependency)

    walk(spec)
    return ordered


def _dimension_block(
    spec: MetricSpec,
    registry: MetricRegistry,
    value_hints: dict[str, list[str]] | None = None,
) -> str:
    hints = value_hints or {}
    lines: list[str] = []
    for name in spec.dimensions:
        dimension = registry.dimensions[name]
        if dimension.is_time:
            granularities = "/".join(dimension.granularities)
            lines.append(
                f"- {name}: {dimension.expr}"
                f"（时间维度，粒度 {granularities}，用 DATE_TRUNC('粒度', {dimension.expr})）"
            )
            continue

        line = f"- {name}: {dimension.expr}"
        values = hints.get(name)
        if values:
            shown = "、".join(values[:30])
            if len(values) > 30:
                shown += " 等"
            line += f"（取值只能是：{shown}）"
        lines.append(line)
    return "\n".join(lines) if lines else "（无）"


def describe_available(registry: MetricRegistry) -> list[dict[str, Any]]:
    """给澄清节点用：列出用户能问哪些指标。"""
    return [
        {"key": metric.key, "name": metric.name, "unit": metric.unit}
        for metric in registry.visible()
    ]
