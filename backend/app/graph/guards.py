"""SQL 守卫：在 SQL 送去执行之前，对照指标口径做静态检查。

模型的活儿是「理解用户想算什么」，代码的活儿是「确认它没算错、没越界」。
这里就是后半句 —— SQL 生成出来不直接查库，先过这六关::

    1. 只允许单条 SELECT            —— 防 DROP / DELETE / 多语句注入
    2. 只能查语义层登记过的表          —— 防绕开口径去碰没定义过的数据
    3. 表达式等价于指标标准算法       —— 防模型自己改口径
    4. 必带的过滤条件都在             —— 防漏掉 status / paid_at 把未付款订单算进来
    5. 必带的 JOIN 条件都在           —— 防 LEFT JOIN 写成了没连上的笛卡尔积
    6. GROUP BY 只能分组白名单维度     —— 防按 customer_id 分组泄露用户数据

再加一道最终投影的检查：客户标识列不许出现在结果里。

全部通过才返回解析后的 AST；任何一条不过都抛 :class:`GuardError`，
异常里带 ``code``（给程序分流）和 ``hint``（回填给模型让它改）。

用法::

    from app.graph.guards import check, GuardError

    try:
        check(sql, registry.get("gmv"), registry)
    except GuardError as exc:
        print(exc.code, exc.hint)

自带演示，看实际效果::

    cd backend && python -m app.graph.guards
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.semantic.loader import DIALECT, GRANULARITIES, MetricRegistry, MetricSpec

__all__ = ["GuardError", "check"]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


#: 出现这些节点就说明不是只读查询。用 getattr 取，免得撞上 sqlglot 版本差异。
_FORBIDDEN_NODES = tuple(
    node
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Drop",
        "Create",
        "Alter",
        "Merge",
        "TruncateTable",
        "Command",
        "Copy",
    )
    if (node := getattr(exp, name, None)) is not None
)

#: 出现在最终投影里就算泄露的列。province / city 是允许的统计维度，不在此列。
_SENSITIVE_COLUMNS = frozenset(
    {"customer_id", "customer_name", "phone", "email", "address"}
)

#: 这些 Cast 不改变数值语义，比对前剥掉（DuckDB 的 ``/`` 本来就返回 DOUBLE）。
_NUMERIC_TYPES = frozenset({"DOUBLE", "REAL", "FLOAT", "DECIMAL", "NUMERIC"})


class GuardError(Exception):
    """SQL 没通过口径检查。"""

    def __init__(self, code: str, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint or message

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def check(sql: str, spec: MetricSpec, registry: MetricRegistry) -> exp.Expression:
    """检查一条 SQL 是否按 ``spec`` 的口径写。

    通过则返回解析后的 AST（执行层可以直接复用），不通过抛 :class:`GuardError`。
    """
    node = _parse(sql)

    _check_select_only(node)
    _check_tables(node, registry)
    _check_expression(node, spec, registry)
    _check_filters(node, _all_required_filters(spec, registry))
    _check_joins(node, _all_required_joins(spec, registry))
    _check_dimensions(node, spec, registry)
    _check_no_leak(node)

    return node


# ---------------------------------------------------------------------------
# 派生指标：条件要沿依赖链合并
# ---------------------------------------------------------------------------


def _dependency_chain(spec: MetricSpec, registry: MetricRegistry) -> list[MetricSpec]:
    """spec 自己 + 递归依赖，去重且保持顺序。

    ratio / derived 在 YAML 里不写 required_filters，口径全在组件身上
    （比如退款率的 ``JOIN refunds`` 和有效订单过滤都在 refunded_orders 里）。
    只看顶层 spec 会漏检查，所以先摊平成一条链。
    """
    chain: list[MetricSpec] = []
    seen: set[str] = set()

    def visit(current: MetricSpec) -> None:
        if current.key in seen:
            return
        seen.add(current.key)
        chain.append(current)
        for dep in current.dependencies:
            visit(registry.get(dep))

    visit(spec)
    return chain


def _all_required_filters(spec: MetricSpec, registry: MetricRegistry) -> tuple[str, ...]:
    out: dict[str, None] = {}
    for current in _dependency_chain(spec, registry):
        for condition in current.required_filters:
            out[condition] = None
    return tuple(out)


def _all_required_joins(spec: MetricSpec, registry: MetricRegistry) -> tuple[str, ...]:
    out: dict[str, None] = {}
    for current in _dependency_chain(spec, registry):
        for condition in current.required_joins:
            out[condition] = None
    return tuple(out)


def _allowed_dimensions(spec: MetricSpec, registry: MetricRegistry) -> tuple[str, ...]:
    """取交集：每个组件都支持的维度才能分组。"""
    allowed = list(spec.dimensions)
    for current in _dependency_chain(spec, registry)[1:]:
        supported = set(current.dimensions)
        allowed = [d for d in allowed if d in supported]
    return tuple(allowed)


# ---------------------------------------------------------------------------
# 第 1 关：只允许单条 SELECT
# ---------------------------------------------------------------------------


def _parse(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except Exception as exc:  # sqlglot 的异常类型跨版本不统一
        raise GuardError(
            "bad_sql",
            f"SQL 解析失败：{exc}",
            hint="只输出一条合法的 DuckDB SQL，不要带 markdown 代码块标记",
        ) from exc

    if not statements:
        raise GuardError("bad_sql", "没有解析出任何 SQL")
    if len(statements) > 1:
        raise GuardError(
            "multiple_statements",
            f"一次只能执行一条 SQL，收到 {len(statements)} 条",
            hint="不要用分号拼接多条语句",
        )
    return statements[0]


def _check_select_only(node: exp.Expression) -> None:
    if not isinstance(node, exp.Select):
        raise GuardError(
            "not_select",
            f"只允许 SELECT 查询，收到的是 {type(node).__name__.upper()}",
            hint="只能生成只读的 SELECT 语句",
        )

    for sub in node.walk():
        if isinstance(sub, _FORBIDDEN_NODES):
            raise GuardError(
                "not_select",
                f"查询里包含禁止的语句：{type(sub).__name__.upper()}",
                hint="只能生成只读的 SELECT 语句",
            )


# ---------------------------------------------------------------------------
# 第 2 关：只许查语义层里登记过的表
# ---------------------------------------------------------------------------


def _check_tables(node: exp.Expression, registry: MetricRegistry) -> None:
    """表名必须来自 YAML。

    没这一关，模型可以 `JOIN customers c ON c.customer_id = o.customer_id`
    然后按别的东西分组 —— 敏感列在投影里会被 :func:`_check_no_leak` 拦住，
    但「能碰到哪张表」这件事本身就该是白名单说了算，不该事后补网。

    白名单不写在代码里，而是从 metrics.yaml 的 ``source`` 和 dimensions.yaml
    的 ``source`` 里抠出来。加一张表要动 YAML，不动作业代码，两边不会漂。
    """
    allowed = _allowed_tables(registry)
    ctes = {(cte.alias_or_name or "").lower() for cte in node.find_all(exp.CTE)}

    for table in node.find_all(exp.Table):
        name = (table.name or "").lower()
        if name in ctes or name in allowed:
            continue
        raise GuardError(
            "unknown_table",
            f"SQL 引用了语义层之外的表 `{name}`",
            hint=(
                f"只能用这些表：{'、'.join(sorted(allowed))}。"
                f"要用别的表，先在 metrics.yaml / dimensions.yaml 里登记。"
            ),
        )


def _allowed_tables(registry: MetricRegistry) -> frozenset[str]:
    tables: set[str] = set()
    for spec in registry.metrics.values():
        tables |= _tables_in(spec.source or "")
    for dimension in registry.dimensions.values():
        tables |= _tables_in(dimension.source or "")
    return frozenset(tables)


def _tables_in(fragment: str) -> set[str]:
    """从 ``orders o JOIN order_items oi ON ...`` 这种片段里抠出表名。"""
    fragment = fragment.strip()
    if not fragment:
        return set()
    try:
        parsed = sqlglot.parse_one(f"SELECT * FROM {fragment}", dialect=DIALECT)
    except Exception:  # 片段写得不像 SQL（比如派生指标只写了句说明）
        return set()
    if parsed is None:
        return set()
    return {(table.name or "").lower() for table in parsed.find_all(exp.Table)}


# ---------------------------------------------------------------------------
# 第 3 关：表达式等价于指标标准算法
# ---------------------------------------------------------------------------


def _check_expression(
    node: exp.Expression,
    spec: MetricSpec,
    registry: MetricRegistry,
) -> None:
    expected = _canonical(_expand(spec, registry))

    # 任意一层 SELECT 的投影里出现标准算法就算通过 —— 允许模型先算在 CTE 里
    for select in node.find_all(exp.Select):
        for projection in select.expressions:
            if _canonical(_unwrap_alias(projection)) == expected:
                return

    actual = "、".join(
        _canonical(_unwrap_alias(p)) for p in _outer_select(node).expressions
    )
    raise GuardError(
        "expression_mismatch",
        f"SQL 里没有 `{spec.key}` 的标准算法。\n"
        f"  应有：{expected}\n"
        f"  实际：{actual}",
        hint=f"{spec.key} 的表达式必须等价于：{expected}",
    )


def _expand(spec: MetricSpec, registry: MetricRegistry) -> exp.Expression:
    """把派生指标展开成原子指标的 SQL。

    aov 在 YAML 里写的是 ``gmv / NULLIF(order_count, 0)``，gmv / order_count
    是指标名不是 SQL。这里按依赖递归替换，直到全是真实表达式。
    """
    node = sqlglot.parse_one(spec.expression, dialect=DIALECT)

    if not spec.is_derived:
        return node

    for dep in spec.dependencies:
        replacement = _expand(registry.get(dep), registry)
        node = node.transform(
            lambda current, name=dep, repl=replacement: _substitute(
                current, name, repl
            ),
            copy=False,
        )

    return node


def expanded_sql(spec: MetricSpec, registry: MetricRegistry) -> str:
    """给提示词用：返回完全展开后的表达式。

    ``refund_rate`` 在 YAML 里写的是 ``refunded_orders / NULLIF(order_count, 0)``，
    光看这一行模型不知道 refunded_orders 对应什么 SQL，只能瞎猜列名。
    把展开结果一起给它，它照抄就行。
    """
    return _expand(spec, registry).sql(dialect=DIALECT)


def _substitute(
    current: exp.Expression,
    name: str,
    replacement: exp.Expression,
) -> exp.Expression:
    """把指标引用换成它的展开式。

    展开式是 ``A - B`` 这种二元运算时，必须补括号 —— 否则塞进除法左边会变成
    ``A - B / C``，优先级就错了。sqlglot 不会自动帮你加。
    """
    if not _is_metric_ref(current, name):
        return current

    piece = replacement.copy()
    return exp.Paren(this=piece) if isinstance(piece, exp.Binary) else piece


def _is_metric_ref(node: exp.Expression, name: str) -> bool:
    """``gmv`` 这种不带表名的裸标识符，才当作指标引用。"""
    return isinstance(node, exp.Column) and not node.table and node.name == name


# ---------------------------------------------------------------------------
# 第 4、5 关：必带的过滤条件 / JOIN 条件
# ---------------------------------------------------------------------------


def _check_filters(node: exp.Expression, required: tuple[str, ...]) -> None:
    present = {_predicate_key(c) for c in _conditions(node)}
    missing = [f for f in required if _predicate_key(_parse_one(f)) not in present]

    if missing:
        raise GuardError(
            "missing_filter",
            f"缺少口径必需的过滤条件：{missing}",
            hint=f"WHERE 里必须包含：{' AND '.join(missing)}",
        )


def _check_joins(node: exp.Expression, required: tuple[str, ...]) -> None:
    if not required:
        return

    present = {_predicate_key(c) for c in _join_conditions(node)}
    missing = [j for j in required if _predicate_key(_parse_one(j)) not in present]

    if missing:
        raise GuardError(
            "missing_join",
            f"缺少必需的 JOIN 条件：{missing}",
            hint=f"JOIN 的 ON 里必须包含：{' AND '.join(missing)}",
        )


def _conditions(node: exp.Expression) -> list[exp.Expression]:
    """所有 WHERE 条件，按 AND 拆开。"""
    out: list[exp.Expression] = []
    for where in node.find_all(exp.Where):
        out.extend(_conjuncts(where.this))
    return out


def _join_conditions(node: exp.Expression) -> list[exp.Expression]:
    out: list[exp.Expression] = []
    for join in node.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            out.extend(_conjuncts(on))
    return out


def _conjuncts(expr: exp.Expression) -> list[exp.Expression]:
    if isinstance(expr, exp.And):
        return _conjuncts(expr.this) + _conjuncts(expr.expression)
    if isinstance(expr, exp.Paren):
        return _conjuncts(expr.this)
    return [expr]


def _predicate_key(expr: exp.Expression) -> str:
    """比较条件的指纹。等号的左右两边不分先后。"""
    if isinstance(expr, exp.EQ):
        left = _canonical(expr.this)
        right = _canonical(expr.expression)
        return f"{min(left, right)} = {max(left, right)}"
    return _canonical(expr)


# ---------------------------------------------------------------------------
# 第 6 关：GROUP BY 白名单
# ---------------------------------------------------------------------------


def _check_dimensions(
    node: exp.Expression,
    spec: MetricSpec,
    registry: MetricRegistry,
) -> None:
    select = _outer_select(node)
    group = select.args.get("group")
    if group is None:
        return  # 不带 GROUP BY 就是查总数，合法

    allowed = _allowed_dimensions(spec, registry)
    for raw in group.expressions:
        expr = _resolve_group_expr(raw, select)
        if not _is_allowed_dimension(expr, allowed, registry):
            raise GuardError(
                "bad_dimension",
                f"不能按 `{_canonical(expr)}` 分组",
                hint=f"`{spec.key}` 只允许按这些维度分组：{allowed}",
            )


def _resolve_group_expr(expr: exp.Expression, select: exp.Select) -> exp.Expression:
    """解析 ``GROUP BY 1`` 和 ``GROUP BY 别名`` 这两种写法。"""
    if isinstance(expr, exp.Literal) and expr.is_int:
        index = int(expr.this) - 1
        if 0 <= index < len(select.expressions):
            return _unwrap_alias(select.expressions[index])
        return expr

    if isinstance(expr, exp.Column) and not expr.table:
        for projection in select.expressions:
            if isinstance(projection, exp.Alias) and projection.alias == expr.name:
                return _unwrap_alias(projection)

    return expr


def _is_allowed_dimension(
    expr: exp.Expression,
    allowed: tuple[str, ...],
    registry: MetricRegistry,
) -> bool:
    core = expr
    granularity: str | None = None

    # DATE_TRUNC('month', o.paid_at)
    if isinstance(core, (exp.TimestampTrunc, exp.DateTrunc)):
        granularity = _trunc_unit(core)
        core = core.this
    # o.paid_at::DATE
    elif isinstance(core, exp.Cast) and _type_name(core.to) == "DATE":
        granularity = "day"
        core = core.this

    target = _canonical(core)

    for name in allowed:
        dimension = registry.dimensions[name]
        if _canonical(sqlglot.parse_one(dimension.expr, dialect=DIALECT)) != target:
            continue
        if granularity is None:
            return True
        if dimension.is_time and granularity in GRANULARITIES:
            return True

    return False


def _trunc_unit(node: exp.Expression) -> str:
    unit = node.args.get("unit")
    if unit is None:
        return ""
    return str(unit.name or unit.this).lower()


# ---------------------------------------------------------------------------
# 附加关：不要把用户明细暴露出去
# ---------------------------------------------------------------------------


def _check_no_leak(node: exp.Expression) -> None:
    for projection in _outer_select(node).expressions:
        for column in _unwrap_alias(projection).find_all(exp.Column):
            if column.name.lower() in _SENSITIVE_COLUMNS:
                raise GuardError(
                    "sensitive_column",
                    f"结果里不能包含用户字段 `{column.sql(dialect=DIALECT)}`",
                    hint="只能按语义层白名单里的维度聚合，不要输出客户明细字段",
                )


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _outer_select(node: exp.Expression) -> exp.Select:
    return node if isinstance(node, exp.Select) else node.find(exp.Select)


def _unwrap_alias(projection: exp.Expression) -> exp.Expression:
    return projection.this if isinstance(projection, exp.Alias) else projection


def _type_name(datatype: exp.Expression | None) -> str:
    if datatype is None:
        return ""
    return datatype.sql(dialect=DIALECT).split("(")[0].strip().upper()


def _canonical(expr: exp.Expression) -> str:
    """归一化后的字符串。两边都比这个，才不会因为写法差异误判。"""
    return _strip_cosmetics(expr).sql(dialect=DIALECT)


def _strip_cosmetics(expr: exp.Expression) -> exp.Expression:
    """剥掉 ROUND(...) 和 ::DOUBLE 这类不影响口径的外壳。"""

    def strip(current: exp.Expression) -> exp.Expression:
        if isinstance(current, exp.Round):
            return current.this
        if isinstance(current, exp.Cast) and _type_name(current.to) in _NUMERIC_TYPES:
            return current.this
        return current

    return expr.transform(strip, copy=True)


def _parse_one(sql: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(sql, dialect=DIALECT)
    except Exception as exc:
        raise GuardError("bad_sql", f"条件解析失败：{sql}\n{exc}") from exc


# ---------------------------------------------------------------------------
# 演示：cd backend && python -m app.graph.guards
# ---------------------------------------------------------------------------


def _demo() -> int:
    import sys

    from app.semantic import load_registry

    try:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    registry = load_registry("app/semantic")

    base_filter = (
        "o.status IN ('paid', 'shipped', 'completed') AND o.paid_at IS NOT NULL"
    )
    joins = (
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN products p ON p.product_id = oi.product_id"
    )

    refund_join = "LEFT JOIN refunds r ON r.order_id = o.order_id"
    refund_numerator = (
        "COUNT(DISTINCT CASE WHEN r.refund_id IS NOT NULL THEN o.order_id END)"
    )

    cases: list[tuple[str, str, str, bool]] = [
        (
            "按月看 GMV",
            "gmv",
            f"SELECT DATE_TRUNC('month', o.paid_at) AS m, "
            f"ROUND(SUM(oi.quantity * oi.unit_price), 2) AS gmv "
            f"FROM orders o {joins} WHERE {base_filter} GROUP BY 1 ORDER BY 1",
            True,
        ),
        (
            "客单价（派生指标按组件展开后比对）",
            "aov",
            f"SELECT SUM(oi.quantity * oi.unit_price) "
            f"/ NULLIF(COUNT(DISTINCT o.order_id), 0) AS aov "
            f"FROM orders o {joins} WHERE {base_filter}",
            True,
        ),
        (
            "退款率（ratio + required_joins）",
            "refund_rate",
            f"SELECT {refund_numerator} / NULLIF(COUNT(DISTINCT o.order_id), 0) AS refund_rate "
            f"FROM orders o {joins} {refund_join} WHERE {base_filter}",
            True,
        ),
        (
            "GMV 写在 CTE 里",
            "gmv",
            f"WITH monthly AS ("
            f"  SELECT DATE_TRUNC('month', o.paid_at) AS m, "
            f"         SUM(oi.quantity * oi.unit_price) AS gmv "
            f"  FROM orders o {joins} WHERE {base_filter} GROUP BY 1) "
            f"SELECT m, gmv FROM monthly ORDER BY m",
            True,
        ),
        (
            "按年分组（粒度不在白名单）",
            "gmv",
            f"SELECT DATE_TRUNC('year', o.paid_at) AS y, "
            f"SUM(oi.quantity * oi.unit_price) AS gmv "
            f"FROM orders o {joins} WHERE {base_filter} GROUP BY 1",
            False,
        ),
        (
            "换算成万元（改了单位）",
            "gmv",
            f"SELECT SUM(oi.quantity * oi.unit_price) / 10000 AS gmv_wan "
            f"FROM orders o {joins} WHERE {base_filter}",
            False,
        ),
        (
            "漏了 paid_at 过滤",
            "gmv",
            f"SELECT SUM(oi.quantity * oi.unit_price) AS gmv "
            f"FROM orders o {joins} "
            f"WHERE o.status IN ('paid', 'shipped', 'completed')",
            False,
        ),
        (
            "退款率漏了 JOIN refunds",
            "refund_rate",
            f"SELECT {refund_numerator} / NULLIF(COUNT(DISTINCT o.order_id), 0) AS refund_rate "
            f"FROM orders o {joins} WHERE {base_filter}",
            False,
        ),
        (
            "自己改了口径：只算单价不算数量",
            "gmv",
            f"SELECT SUM(oi.unit_price) AS gmv "
            f"FROM orders o {joins} WHERE {base_filter}",
            False,
        ),
        (
            "按用户分组泄露明细",
            "gmv",
            f"SELECT o.customer_id, SUM(oi.quantity * oi.unit_price) AS gmv "
            f"FROM orders o {joins} WHERE {base_filter} GROUP BY 1",
            False,
        ),
        (
            "多 JOIN 一张语义层里没有的表",
            "gmv",
            f"SELECT SUM(oi.quantity * oi.unit_price) AS gmv "
            f"FROM orders o {joins} "
            f"JOIN customers c ON c.customer_id = o.customer_id "
            f"WHERE {base_filter}",
            False,
        ),
        (
            "DROP TABLE",
            "gmv",
            "DROP TABLE orders",
            False,
        ),
        (
            "两条语句拼接",
            "gmv",
            "SELECT 1; SELECT 2",
            False,
        ),
    ]

    passed = failed = 0
    for label, key, sql, should_pass in cases:
        spec = registry.get(key)
        try:
            check(sql, spec, registry)
            ok, detail = True, ""
        except GuardError as exc:
            ok, detail = False, f"[{exc.code}] {str(exc).splitlines()[0]}"

        mark = "PASS" if ok else "FAIL"
        agree = "✓" if ok == should_pass else "✗ 预期不符"
        print(f"{agree} [{mark}] {label}")
        if detail:
            print(f"        {detail}")

        if ok == should_pass:
            passed += 1
        else:
            failed += 1

    print(f"\n{passed} 条符合预期，{failed} 条不符预期")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_demo())
