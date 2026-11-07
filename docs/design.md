# 电商经营分析 Agent（ChatBI）设计稿 v1.0 · 作品集版

> 状态：方向已定，待实现
> 技术约束：LangGraph · FastAPI · DuckDB · DeepSeek · Vue3 + ECharts
> v1.0：由 v0.9 产品规格裁剪为**单人 2~3 周可完成**的项目。砍掉审批流、多层 CTE 血缘、转化率/复购指标；指标 5 个、评测 30 题、路线图 3 阶段。v0.9 原文见 git 历史。

## 1. 一句话定位

用自然语言问数据：生成 SQL → 校验口径 → 执行 → 自纠错 → 出图 → 给结论。

**模型负责理解和表达，代码负责口径和安全。**

## 2. 要解决的问题

BI 自助取数的真实痛点是**口径不一致**：同一个「销售额」，市场部含退款、财务部不含，两个人问同一个问题得到两个数。直接让大模型写 SQL 只会把混乱放大——它可能漏掉「只算已支付订单」这种过滤，而且**不会告诉你它漏了**。

这个项目把三件事放在模型前面：

1. **指标语义层**：指标公式、必需过滤、时间字段、维度白名单写在 YAML 里。
2. **口径签名校验**：AST 检查聚合、引用列、必需过滤、时间字段、维度；缺过滤即拒，不静默放行。
3. **歧义澄清**：「最近卖得怎么样」不猜，先问清时间窗和指标。

## 3. 与已有项目的边界

| 维度 | RAG 知识库（已有） | 本项目 |
|---|---|---|
| 数据类型 | 非结构化文档 | 结构化表 |
| 核心动作 | 检索 + 生成 | 理解 → 生成 SQL → 执行 → 解释 |
| 失败形态 | 编造不存在的答案 | SQL 写错、口径用错、答非所问 |
| 正确性判据 | 来源是否支持答案 | **结果集是否与标准答案一致** |
| 核心工程 | 召回、拒答、切分 | SQL 守卫、口径契约、状态机 |

## 4. 演示数据

时间范围 2025-01-01 ~ 2026-09-30，固定种子生成，`DATA_VERSION=ec_v1`。

```sql
customers(customer_id, name, gender, age, city, province, register_date, channel)
products(product_id, name, category, sub_category, price, cost)
orders(order_id, customer_id, order_date, paid_at, status, channel,
       province, payment_method, shipping_fee, discount_amount)
order_items(order_id, product_id, quantity, unit_price)
refunds(refund_id, order_id, refund_date, amount, reason)
```

规模约：2 万客户 / 2000 商品 / 10 万订单 / 25 万明细 / 1.5 万退款。

刻意埋入：618 与双 11 峰值、渠道差异、高退款品类。

**退款与订单状态（口径前提，必须写死）**

- 退款**不修改** `orders.status`；生成器不得产出 `status = 'refunded'`。
- 退款信息只通过 `refunds` 表关联 `orders.order_id`。
- 因此 `keep_original` 指标（`gmv`、`order_count`、`product_cost`）天然包含已退款订单；`refunded_orders` 也仍然落在「有效订单」集合内。

**有效订单定义**：`status IN ('paid','shipped','completed') AND paid_at IS NOT NULL`。

## 5. 指标语义层

### 5.1 指标字段规范

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` / `synonyms` | 是 | 展示名与同义词，用于槽位识别 |
| `kind` | 是 | `additive` / `ratio` / `derived` |
| `source` | 是 | FROM/JOIN 片段 |
| `expression` | 是 | 指标表达式（可解析为 SQL） |
| `required_filters` | 是* | 口径必需的过滤条件；*additive 必填，ratio / derived 由组件继承，可省略 |
| `required_joins` | 否 | 口径必需的 JOIN 条件（仅 `refunded_orders` 使用） |
| `time_field` | 是 | 时间切片字段 |
| `refund_treatment` | 见说明 | 退款处理方式；**additive 必填，ratio / derived 不得声明** |
| `dimensions` | 是 | 允许的切片维度；空列表表示不可切片 |
| `default_window` | 是 | 默认时间窗，结论必须注明 |
| `unit` / `description` | 是 | 单位与一句话口径（含已知局限） |
| `verification` | 是 | `strict` / `component` |
| `visible` | 是 | `true` 表示用户可问；`false` 为内部组件 |
| `mixed_treatment_ok` | 否 | ratio 允许分子分母退款口径不同；默认 false |

`refund_treatment` 两值：`none`（不为退款调整，含统计退款本身的指标）、`keep_original`（退款不回冲）。

**ratio / derived 不得声明 `refund_treatment`**：loader 从 `numerator` / `denominator` / `components` 继承并记录；组件口径不一致且未声明 `mixed_treatment_ok: true`，启动失败。

完整 ratio 示例：

```yaml
refund_rate:
  name: 退款率
  synonyms: [退款订单占比, 退货率]
  kind: ratio
  numerator: refunded_orders
  denominator: order_count
  time_field: o.paid_at
  mixed_treatment_ok: true        # refunded_orders=none，order_count=keep_original
  dimensions: [date, province, category, sub_category, channel, payment_method]
  default_window: last_30d
  verification: component
  visible: true
  description: 有效订单中发生过退款的订单占比；部分退款按整单计一次。
```

### 5.2 维度定义

```yaml
dimensions:
  date:           { type: time, expr: o.paid_at, granularities: [day, week, month, quarter] }
  province:       { type: category, expr: o.province }
  category:       { type: category, expr: p.category }
  sub_category:   { type: category, expr: p.sub_category }
  channel:        { type: category, expr: o.channel }
  payment_method: { type: category, expr: o.payment_method }
```

`GROUP BY` 必须映射到这些表达式；`DATE_TRUNC('month', o.paid_at)` 映射到 `date` + `month`。

### 5.3 指标清单（5 个可问 + 3 个组件）

| key | kind | 口径 | time_field | refund_treatment | 可见 |
|---|---|---|---|---|---|
| `gmv` | additive | 有效订单商品成交价合计；**不含运费、不扣优惠、退款不回冲** | `o.paid_at` | keep_original | ✅ |
| `order_count` | additive | 有效订单去重数 | `o.paid_at` | keep_original | ✅ |
| `aov` | derived | `gmv / NULLIF(order_count, 0)` | `o.paid_at` | 继承 | ✅ |
| `refund_rate` | ratio | `refunded_orders / order_count` | `o.paid_at` | 混合（已声明） | ✅ |
| `gross_margin` | ratio | `gross_profit / gmv`；**未扣优惠与运费** | `o.paid_at` | 继承 | ✅ |
| `product_cost` | additive | 有效订单商品成本合计 | `o.paid_at` | keep_original | 组件 |
| `refunded_orders` | additive | 有效订单中发生过退款的订单数 | `o.paid_at` | none | 组件 |
| `gross_profit` | derived | `gmv - product_cost` | `o.paid_at` | 继承 | 组件 |

`refunded_orders` 必须声明：

```yaml
refunded_orders:
  source: orders o LEFT JOIN refunds r ON o.order_id = r.order_id
  expression: COUNT(DISTINCT CASE WHEN r.refund_id IS NOT NULL THEN o.order_id END)
  required_filters:
    - o.status IN ('paid','shipped','completed')
    - o.paid_at IS NOT NULL
  required_joins:
    - o.order_id = r.order_id
```

必须写进 description 的已知局限：

- GMV 不扣折扣、不含运费；毛利因此偏高。
- 退款率按订单数计，不区分部分退款；退款金额需另建指标。
- 退款不改变订单状态，`keep_original` 指标**包含已退款订单**。

### 5.4 生成策略：混合方案

```
语义层（口径契约） → LLM 生成完整 SQL → 守卫校验口径签名 → 执行
                         ↑                    │
                         └──── 不符则带差异修复 ┘
```

模型必须：用指标 key 作别名；指标表达式与 YAML 等价；只能用声明的维度分组；时间范围过滤作用在 `time_field` 上。

同比、环比、排名、窗口函数由模型自由组织，语义层不限制 SQL 结构。

### 5.5 口径签名校验

对生成 SQL 做 AST 分析，逐个别名校验：

| 检查 | 规则 | 失败码 |
|---|---|---|
| 聚合函数 | 与 YAML 一致（含 DISTINCT 标记） | `agg_mismatch` / `distinct_mismatch` |
| 引用列 | 叶子列集合与 YAML 一致（表别名不参与） | `column_mismatch` |
| 必需过滤 | `required_filters` 在允许作用域内命中 | `missing_filter` |
| 必需 JOIN | `required_joins` 命中（JOIN ON 或 WHERE 等价等值谓词均可） | `missing_join` |
| 时间字段 | **若存在**时间比较谓词，必须落在 `time_field`（含 `=` / `IN` / `BETWEEN` / 范围 / `DATE_TRUNC` 派生等值；仅限与非列表达式比较） | `wrong_time_field` |
| 维度 | `GROUP BY` 可映射到声明维度 | `invalid_dimension` |
| 派生/比率 | 组件别名存在且各自通过校验 | `component_invalid` |

**作用域（简化版）**：

1. 别名 A 定义在 SELECT 块 B。`required_filters` / `required_joins` 允许出现在 B 及其**上一层外层块**，以及 B 通过 FROM/JOIN 直接引用的**单层 CTE**。
2. 时间比较谓词只与**非列表达式**（常量、参数）比较；列-列等值（join 谓词）不算时间过滤。
3. **不支持多层 CTE**：出现两层及以上嵌套 CTE、或多层 `SELECT *` / 列重命名导致血缘无法解析 → 直接判 `unresolved_lineage`，拒绝并修复。
4. `required_joins` 归一化只统一语法糖：去表别名、`CAST(x AS DATE)` 与 `x::date` 视为同一写法、忽略括号差异；**不做类型推导**。

失败即拒，修复提示要具体：

> `gmv` 缺少必需过滤：`o.status IN ('paid','shipped','completed')`

v1 全部指标都能自动校验，没有 `manual` 指标。

### 5.6 Loader 校验（启动即失败）

- Pydantic 校验字段完整性；
- `numerator` / `denominator` / `components` 引用的指标必须存在；
- additive 必须声明 `refund_treatment`；ratio / derived 声明了则报错；
- 混合退款口径的 ratio 必须 `mixed_treatment_ok: true`；
- 依赖图无环；
- `expression` / `required_filters` / `required_joins` 可被 sqlglot 以 DuckDB 方言解析；
- `dimensions` 在维度表定义，且指标 `source` 能提供对应表；
- description 必含口径与局限；
- 任一失败，服务拒绝启动并打印 `metric_key + 字段 + 原因`。

### 5.7 验收闸门

| 闸门 | 要求 |
|---|---|
| G1 数据 | 8 个指标的标准 SQL 全部可执行，结果人工抽查合理 |
| G2 语义层 | loader 加载 8 个指标；缺字段 / 环依赖 / 坏 SQL / 混合口径未声明均能报错 |
| G3 误报组 | 8 条**正确口径**复杂 SQL（含 2 条同比/环比、2 条单层 CTE、1 条 JOIN 写在 WHERE）；**≥7 条通过** |
| G4 漏报组 | 6 条**故意写错**的 SQL，**6/6 被拒且失败码命中预期** |
| G5 声明 | 8 个指标全部声明 `verification` |

漏报组用例：

| # | 错误写法 | 期望失败码 |
|---|---|---|
| 1 | `gmv` 漏 `status` 过滤 | `missing_filter` |
| 2 | `order_count` 写成 `COUNT(*)` | `distinct_mismatch` |
| 3 | 时间过滤用 `order_date` 而非 `paid_at` | `wrong_time_field` |
| 4 | `aov` 直接 `SUM/COUNT`，不引用组件别名 | `component_invalid` |
| 5 | 按 `customer_id` 分组（不在维度白名单） | `invalid_dimension` |
| 6 | `refunded_orders` 写成 `COUNT(DISTINCT r.refund_id)`（口径换掉） | `agg_mismatch` |

G3 或 G4 任一不过，停在该阶段，细化规则后再往下走。

## 6. LangGraph 设计

### 6.1 状态

```python
class AnalysisState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    rewritten: str
    intent: Literal["analysis", "chat", "out_of_scope"]
    missing: list[str]
    metric_keys: list[str]
    schema_context: str
    sql: str | None
    attempts: list[Attempt]        # kind: guard_fail | exec_error | empty_result
    guard: GuardResult | None
    clarify_attempts: int
    columns: list[str]
    rows: list[dict]
    row_count: int
    chart: dict | None
    answer: str
    usage: Usage
    error: str | None
    human_required: bool
```

```python
class GuardResult(TypedDict):
    passed: bool
    failures: list[dict]   # code / metric / detail
    flags: dict            # had_limit, had_aggregation, table_count
```

### 6.2 节点与边

```
START
  ↓
rewrite_question
  ↓
classify ──chat / out_of_scope──> respond_directly → END
  ↓
load_context
  ↓
check_slots ──缺失──> clarify（interrupt）→ check_slots
  ↓
generate_sql
  ↓
guard_sql ──不通过──> repair_sql ──> guard_sql
  ↓（通过）
execute ──报错/空结果──> repair_sql ──> guard_sql ──> execute
  ↓
build_chart
  ↓
interpret
  ↓
END
```

**修复后的 SQL 必须重新过守卫**，不能绕过 guard 直接执行。执行失败与守卫失败共用 3 次预算。

**澄清上限**：`clarify` 最多 2 次。超限按槽位兜底：时间窗 → `default_window` 并在结论注明；指标 → 同义词匹配度最高者取之并在结论注明「按 X 理解」，并列则落 `human_required`；维度 → 问题已明确维度时视为已填、不追问，缺失则不拆。用户明确说「不限时间」时，时间槽位视为已填。

### 6.3 修复与降级

- `kind` 枚举：`guard_fail` / `exec_error` / `empty_result`。
- 每次把结构化差异写进修复提示。
- 3 次用尽：`error = "repair_exhausted"`、`human_required = True`，返回最后一次 SQL、错误摘要与尝试列表；前端展示「需要人工介入」。
- 修复成功率按 kind 分桶统计。

### 6.4 LangGraph 特性对应

| 能力 | 用法 |
|---|---|
| `StateGraph` + 条件边 | 澄清 / 修复两个环 |
| `interrupt()` | 槽位追问 |
| `checkpointer` | `thread_id = session_id`，SQLite 持久化 |
| `astream_events` | 节点级事件 + token 流 |

## 7. SQL 安全

| 规则 | 处理 |
|---|---|
| 单语句 | 多语句直接拒 |
| 只允许 SELECT / WITH ... SELECT | DDL/DML/ATTACH/COPY/PRAGMA/LOAD 全拒 |
| 表白名单 | 只允许 `metrics.yaml` / `dimensions.yaml` 的 `source` 里登记过的表（目前 `orders` / `order_items` / `products` / `refunds`）；系统表、CTE 之外的表全拒 |
| 禁止文件函数 | `read_csv` / `read_parquet` / `glob` / `install` / `load` |
| 强制 LIMIT | 无 LIMIT 追加 1000；已有不超过 5000 |
| 执行隔离 | DuckDB `read_only=True`；独立线程 + 超时中断；行数上限 |

**顺序约定**：守卫先在原始 AST 上记录 `had_limit` / `had_aggregation`，再追加 LIMIT。

## 8. 图表与结论

模型只输出 chart spec：

```json
{"type": "bar", "x": "category", "y": "gmv", "series": null,
 "title": "各品类成交总额", "reason": "分类对比用柱状图"}
```

校验：`x` / `y` 必须存在于结果列；`type` 限定 line / bar / pie / scatter / table；数据点 > 50 或校验失败 → 降级表格。

**结论必须包含**：指标名、时间范围、默认窗口说明、口径局限。

**数字可追溯（新增）**：结论中出现的每个数值，必须能在 `rows`（或由 `rows` 直接计算出的比值）里找到。校验方式：抽取结论中的数字，逐个与结果集列值比对（容差同 §11.2）；匹配失败 → 重新生成一次，仍失败则降级为「只返回表和图，不输出结论」。**占比、环比等派生数字必须先写进 SQL 作为输出列**，不许模型心算。

## 9. API 与事件

### 9.1 交互契约

- `POST /api/sessions/{id}/ask` → `202 {run_id}`，只负责启动。
- `POST /api/sessions/{id}/runs/{run_id}/resume` → `202`，提交澄清结果。
- 所有输出统一走 `GET /api/sessions/{id}/runs/{run_id}/stream`。
- 前端只维护 `idle | running | interrupted | done | failed`，由 SSE 驱动。
- 中断事件：`{"type":"interrupt","interrupt_id":"...","kind":"clarify","payload":{...}}`。

### 9.2 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions` | 创建会话 |
| POST | `/api/sessions/{id}/ask` | 启动一轮提问 |
| POST | `/api/sessions/{id}/runs/{run_id}/resume` | 澄清后继续 |
| GET | `/api/sessions/{id}/runs/{run_id}/stream` | SSE |
| GET | `/api/sessions/{id}` | 会话与历史 |
| GET | `/api/metrics` | 指标说明 |

事件：`node_start` / `sql_generated` / `guard_failed` / `clarify_required` / `query_result` / `chart` / `answer_delta` / `done` / `error`。

### 9.3 业务表

```sql
sessions(id, title, created_at, updated_at)
runs(id, session_id, question, status, interrupted_kind,
     final_answer, cost_usd, tokens_in, tokens_out, created_at)
qa_records(id, run_id, question, sql, columns_json, rows_json, verified, created_at)
run_events(id, run_id, seq, type, payload_json, created_at)
```

LangGraph checkpoint 存独立库，与业务表解耦。

## 10. 目录结构

```
.
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/            sessions.py · runs.py · stream.py
│   │   ├── core/           config.py · logging.py
│   │   ├── graph/
│   │   │   ├── state.py
│   │   │   ├── build.py
│   │   │   ├── nodes/
│   │   │   └── guards.py           # AST 守卫 + 口径签名
│   │   ├── db/             duckdb_engine.py · repo.py
│   │   ├── semantic/       metrics.yaml · dimensions.yaml · loader.py
│   │   └── llm/            provider.py · prompts/*.md
│   ├── tests/              fakes.py · test_loader.py · test_signature.py · test_gate.py
│   └── pyproject.toml
├── data/
│   ├── schema.sql
│   ├── generate.py
│   ├── verify.py
│   └── ecommerce.duckdb    # 不入库
├── eval/
│   ├── questions.jsonl
│   ├── run_eval.py
│   └── results/
└── frontend/
```

## 11. 评测

### 11.1 题目（30 道）

- 12 道分析题（单指标 4 / 多指标 4 / 同比环比 4）
- 5 道分组排序题
- 3 道歧义题（测澄清）
- 10 道补充题（相对时间 2 / 时间分组 2 / 空结果 1 / 口语 1 /
  不可问指标 1 / 多维交叉 1 / Top-N 2）

每题带人工编写的 `reference_sql`，评测时实时执行；`reference_sql` 的列别名必须等于指标 key。

```json
{
  "id": "q_07",
  "question": "2025 年 6 月各品类 GMV 和订单数",
  "reference_sql": "SELECT p.category AS category, SUM(oi.quantity * oi.unit_price) AS gmv, COUNT(DISTINCT o.order_id) AS order_count ...",
  "expected_slots": {
    "metrics": ["gmv", "order_count"],
    "dimensions": ["category"],
    "time_window": {"start": "2025-06-01", "end": "2025-06-30"}
  }
}
```

`time_window: null` 表示不限时间。

### 11.2 结果集比较

1. **列对齐**：按列名（小写、去空白）对齐，换列序不影响；列名集合不同 → `column_mismatch`。
2. **别名契约**：模型必须用指标 key 作别名。
3. **行比较**：tuple 后按 multiset（`Counter`）比较，保留重复行。
4. **数值**：统一 `Decimal`，且**从字符串构造**（`Decimal(str(x))`）。相对容差 `abs(a-b) <= 1e-6 * max(1, |a|, |b|)`。
5. **NULL**：`None` 只等于 `None`。
6. **字符串**：去首尾空白后精确比较。
7. **判定**：完全一致才算正确。

### 11.3 指标定义

- `execution_accuracy`：严格按「完全一致」判定，别名不符即错；分母 20。
- `alias_mismatch`：仅「其余列与值全对、唯一差异是别名」时计入 / 20。
- `clarification_hit`：触发 clarify 且槽位集合与标注一致 / 3。
- `guard_false_positive`：误报组被拒条数 / 8。
- `guard_false_negative`：故意错误 SQL 被放行条数 / 6。
- `p50/p95 latency`、`cost_usd` 按 run 统计。

### 11.4 口径可信度

报告同时给出「执行准确率」「别名诊断」「自动校验覆盖率」「守卫误报率」「守卫漏报率」。**覆盖率只说明声明了多少 strict，不说明校验准不准**，不能只报一个数。

## 12. 技术栈

| 层 | 选择 |
|---|---|
| 编排 | LangGraph + langgraph-checkpoint-sqlite |
| API | FastAPI + SSE |
| 分析库 | DuckDB（只读） |
| SQL 校验 | sqlglot |
| LLM | DeepSeek（OpenAI 兼容抽象） |
| 图表 | ECharts（spec 驱动） |
| 前端 | Vue3 + Vite + Pinia |
| 测试 | pytest + FakeLLM + 内存 DuckDB |

## 13. 路线图

| 阶段 | 内容 | 工期 | 验收 |
|---|---|---|---|
| S1 数据与口径 | 5 表生成 + DuckDB + 8 指标 YAML + loader + 守卫原型 | 1 周 | G1 / G2 通过 |
| S2 端到端 | LangGraph 主干 + 执行 + 修复环 + 澄清 | 1 周 | 简单问题跑通；G3 / G4 通过 |
| S3 呈现与评测 | 图表 + 结论 + 前端单页 + 30 题评测 | 1 周 | 三个数字可复现，能录屏 |

## 14. 风险与对策

| 风险 | 对策 |
|---|---|
| 签名校验误杀正确 SQL | 误报组 ≤1/8；作用域含单层 CTE |
| 签名校验放行错误 SQL | 漏报组 6/6 |
| Text2SQL 准确率低 | 语义层收敛；修复环；先测基线再优化 |
| 图表好看但错 | 列强校验 + 降级表格 |
| 结论数字编造 | §8 数字可追溯校验 + 降级不给结论 |
| 澄清过多 | 只对三个槽位追问；有默认值时用默认并注明 |
| 做不完 | 严格按 S1→S2→S3；任一阶段超期则砍 S3 的前端美化 |

## 15. 相比 v0.9 砍掉了什么

| 项目 | v0.9 | v1.0 | 理由 |
|---|---|---|---|
| 指标数 | 14 | 8（5 可问 + 3 组件） | 覆盖加总/派生/比率三类即可 |
| 数据表 | 6 | 5 | 去掉 visits，转化率指标一并移除 |
| 评测题 | 100 | 30 | 20 题覆盖不到相对时间、时间分组、空结果这些高频失败模式 |
| 审批流 | interrupt 审批 + 3 次修改 | 删除 | 纯工程细节，不产生技术亮点 |
| CTE 血缘 | 多层链逐层可追溯判定 | 单层，多层直接拒 | 规则复杂度与收益不成正比 |
| 模板指标 | `repurchase_rate_monthly` | 删除 | 需要 `manual` 特殊处理 |
| 任务生命周期 | asyncio + 重启恢复 | 删除 | 演示环境不需要 |
| 误报/漏报组 | 12 / 6 | 8 / 6 | 同比例缩小 |
| 路线图 | P0~P5 | S1~S3 | 3 周可走完 |

## 16. 变更记录

### v1.0（2026-10-29）

- 由 v0.9 裁剪为作品集版，见 §15。
- 指标 8 条（5 可问 + 3 组件），移除 visits 相关指标。
- 删除审批流、任务生命周期、多层 CTE 血缘、模板指标。
- §8 新增「结论数字可追溯」校验与降级策略。
- 路线图压缩为 S1~S3。
