# 电商经营分析 Agent（ChatBI）

用自然语言问数据：生成 SQL → 口径校验 → 执行 → 自纠错 → 出图 → 给结论。
**指标口径统一、过程可控、结果可审计。**

![status](https://img.shields.io/badge/status-demo%20ready-green)
![Python](https://img.shields.io/badge/Python-3.11%2B-3772AA?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-stateful%20graph-1C3C3C)
![DuckDB](https://img.shields.io/badge/DuckDB-analytics-FFF000)
![FastAPI](https://img.shields.io/badge/FastAPI-SSE-009688?logo=fastapi&logoColor=white)

> 设计文档：[docs/design.md](docs/design.md) ｜ 代码结构：[docs/architecture.md](docs/architecture.md)

## 为什么做这个

BI 自助取数的真实痛点是**口径不一致**：同一个「销售额」，市场部含退款、财务部不含，两个人问同一个问题得到两个数。直接让大模型写 SQL 只会把这个混乱放大。

所以这个项目把两件事放在模型前面：

1. **指标在 YAML 里定义**——公式、时间字段、必带的过滤和 JOIN、可分组维度、退货怎么算，全是手写的。模型可以自由组织 SQL，但必须用指标别名，并满足口径签名。
2. **口径签名校验**——AST 检查聚合表达式、必带过滤、必带 JOIN、分组维度，缺一条就拒，把原因原样回填给模型让它改。

模型负责理解和表达，代码负责口径和安全。

## 一条问题怎么走完

```
"2026年6月各省的销售额，从高到低"
        ↓
understand   在指标白名单里按别名做最长匹配 → GMV
        ↓        匹配不到就追问「目前支持哪 5 个指标」
generate     把展开后的口径、必带过滤、维度真实取值拼进提示词 → 模型写 SQL
        ↓
guard        过六关；不过就把 code + hint 回填
        ↓ 不过
repair       ——> 回到 guard（最多 2 次）
        ↓ 通过
execute      只读 DuckDB，外面再套一层 LIMIT 兜底
        ↓ 报错 → 回 repair；0 行 → 给一次改写机会
summarize    结论用模板拼（不经过模型），图表按结果形状选
```

每一步都通过 SSE 推给前端，边跑边显示。守卫拦下来、模型改一版才对，这些过程在页面上看得见——攒成一个 JSON 返回就全没了。

## 现在做到了什么

| 能力 | 状态 |
|---|---|
| 指标语义层（YAML 定义 8 个指标，5 个对用户可见 / 3 个内部组件） | ✅ |
| 指标白名单匹配，匹配不到就追问，不让模型瞎猜 | ✅ |
| 口径校验：单条 SELECT、表名白名单、表达式等价、必带过滤、必带 JOIN、分组维度白名单、敏感列不外泄 | ✅ |
| 自纠错环（最多 3 次生成）：口径不过、执行报错回到修复；返回 0 行给一次改写机会 | ✅ |
| 只读 DuckDB + 行数上限 | ✅ |
| 结论模板拼装，数字必须来自结果集 | ✅ |
| 图表按结果形状选（一个数 / 时间序列 / 单维度 / 表格） | ✅ |
| FastAPI + SSE + 单页前端 | ✅ |
| 30 题评测，两种模式 | ✅ |
| 多轮会话、checkpointer、高风险 SQL 审批、空结果重试 | ❌ 未做 |

## 核心设计

### 1. 指标语义层

`metrics.yaml` 里每个指标写清楚：公式、数据来源、必带过滤、必带 JOIN、时间字段、退货怎么算、能按哪些维度分组。

```yaml
gmv:
  kind: additive
  expression: SUM(oi.quantity * oi.unit_price)
  source: >
    orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    JOIN products p ON p.product_id = oi.product_id
  required_filters:
    - o.status IN ('paid', 'shipped', 'completed')
    - o.paid_at IS NOT NULL
  dimensions: [date, province, category, sub_category, channel, payment_method]
```

比率型指标（退款率、毛利率）引用的组件指标标 `visible: false`——用户在页面上问不到「商品成本」，但它要参与计算。

### 2. 守卫过六关

模型写的 SQL 不直接查库：

1. 只允许单条 SELECT——防 DROP / DELETE / 多语句
2. 只能查语义层里登记过的表——表白名单不是另写一份，而是从 `metrics.yaml` / `dimensions.yaml` 的 `source` 里推出来的，YAML 没提的表一律拒
3. 表达式等价于指标标准算法——防模型自己改口径
4. 必带的过滤都在——防漏掉 `status` / `paid_at` 把未付款订单算进来
5. 必带的 JOIN 都在——防写成没连上的笛卡尔积
6. GROUP BY 只能分组白名单维度——防按 `customer_id` 分组泄露用户数据

任何一条不过都抛 `GuardError`，带机器可读的 `code` 和给人看的 `hint`，`hint` 直接回填给模型。

### 3. 提示词把口径摊开

只给模型「退款率 = 退款订单数 / 订单数」是不够的——它会去猜 `o.is_refunded` 这种根本不存在的列。所以提示词里给的是**完全展开后的表达式**，加上每个叶子组件的来源、必带过滤和必带 JOIN。

同理，模型不知道库里支付方式存的是 `wechat` 而不是「微信支付」，省份存的是「上海市」而不是「上海」。启动时查一次 `SELECT DISTINCT`，把真实取值拼进提示词；超过 40 个不同值的列自动跳过。

相对时间也写死一条规则：以数据末尾为基准日，「最近 N 天」= 基准日往前 N-1 天到基准日（含当天）。不给这条规则，同一个问题会跑出两个答案。

### 4. 结论不走模型

设计文档里写的是模型输出 chart spec、模型写结论文案。实现时改成了模板拼装：

- **结论**：数值、最高/最低、分组数都从结果集里取，模型没有机会编数字。
- **图表**：类型由结果形状决定（一行一列 → 大数字；时间列 → 折线；单维度 → 横向条形；其余 → 表格）。

理由是可复现：同一份数据两次跑出两种图、两句结论，就没法验。

## 演示数据

固定随机种子生成，时间范围 **2025-01-01 ~ 2026-09-30**：

| 表 | 行数 |
|---|---|
| `customers` | 20,000 |
| `products` | 2,000 |
| `orders` | 100,000（有效订单 84,760） |
| `order_items` | 247,174 |
| `refunds` | 15,000 |

生成器刻意埋了大促峰值、渠道差异、区域偏好和高退款品类——为了让「各省销售额」「各品类退款率」这类问题有真实的形状可看，不是均匀随机数。

## 快速开始

```bash
git clone <repo> && cd ecommerce-chatbi

python -m venv .venv
.venv\Scripts\activate          # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt

# 1. 造数据（约 5 秒）
python data/generate.py --out data/ecommerce.duckdb

# 2. 验收数据（29 项）
python data/verify.py data/ecommerce.duckdb

# 3. 起服务
cd backend
python -m app.api
```

打开页面有两种方式，效果一样：

- 浏览器访问 <http://127.0.0.1:8000>（推荐）
- 或者 **直接双击 `frontend/index.html`** —— 页面会连 `http://127.0.0.1:8000`，后端开了 CORS，跨源也能通。前提是后端已经起来；没起来的话页面会提示「连不上后端」。

模型配置两种方式，选一种：

```bash
# 方式一：写进 .env，配一次就够了（.env 已在 .gitignore 里）
cp .env.example .env      # 然后填 LLM_API_KEY
```

```powershell
# 方式二：当前终端里设，同一个终端里启动
$env:LLM_API_KEY="sk-..."
$env:LLM_MODEL="deepseek-v4-flash"
$env:LLM_BASE_URL="https://api.deepseek.com/v1"
```

终端里已经设的变量优先级更高，所以临时换 key 不用改 `.env`。key 不会出现在任何被提交的文件里。

不配 key 也能打开页面，提问时会返回一条「没有配置 key」的提示——不会假装答了。

## 评测

30 道题，覆盖三类：单值回答 15 题、分组结果 11 题、该追问的 4 题。

```bash
python eval/run.py              # 参考 SQL 模式：不经过模型，不花 token
python eval/run.py --model      # 真实模型模式：需要 LLM_API_KEY
```

**参考 SQL 模式**把每题的参考答案直接喂给守卫和执行器，验证的是「守卫不误拦合法查询」「执行结果与 golden 一致」「结论取对了列」，测的不是模型。

**模型模式**跑完整链路，判分只看结果数字，不看 SQL 长什么样。

当前结果：

| | 参考 SQL 模式 | 模型模式（`deepseek-v4-flash`） |
|---|---|---|
| 通过 | 30/30 | 30/30（连续两次） |
| 修复 | — | 1 题触发（2024 年那题返回 0 行，改写一次后仍是 0 行，照实返回「没查到」） |
| 耗时 | P50 0.02s / P95 0.02s | P50 1.4s / P95 3.6s |
| 调用 | 0 | 27 次（3 道澄清题不调模型），平均每题 1,010 tokens |

这 30 题能说明的是：在题目覆盖到的失败模式上，链路没出问题。它**不能**说明模型在任意问题上的准确率——题目是我自己出的、和提示词在同一轮迭代里改的，存在过拟合风险；30 题全对，按三倍法则置信下界也只在 90% 左右。评测集的构造、覆盖了哪些失败模式、还有哪些测不了，写在 [docs/design.md](docs/design.md) 的评测一节。

评测集在迭代中改过三次题（`a10` 双十一口径歧义、`a12` 想问合计却写成四个地方、补 10 题），改的是题目，不是 golden 数字。

## 目录结构

```
data/              造数脚本、建表语句、验收脚本
backend/app/
  semantic/        指标与维度的 YAML，加一个 loader 做检查
  graph/           prompts / llm / executor / guards / chart / pipeline（LangGraph）
  api.py           FastAPI：/api/health /api/metrics /api/ask(SSE)
frontend/          单页，原生 JS + 本地 ECharts，浅色 / 深色两套
eval/              30 题、判分脚本、答案文件
docs/              design.md 设计文档，architecture.md 代码结构
```

## 已知局限

- **多轮**：每次提问都是独立的，不支持「按省份拆一下」这类追问。
- **澄清只有一种**：匹配不到指标才追问。时间窗没给就用默认窗口，不反问。
- **空结果只重试一次**：`2024年各品类的销售额` 会让模型改一版，改完还是 0 行就照实说「没查到」——因为分不清「写错了」和「这段区间本来就没数据」。
- **没有审批流**：设计文档里的高风险 SQL 人工审批、checkpointer 续跑都还没做。
- **前端是演示级的**：没有登录、没有历史记录、没有导出。

## License

MIT
