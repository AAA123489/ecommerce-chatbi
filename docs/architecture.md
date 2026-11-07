# 代码结构

> 这 6 个文件是一条流水线，前一个的产物交给后一个。

---

## 流水线

```
1. schema.sql      规定有哪些表、每张表哪些列
        ↓
2. generate.py     照着表造 10 万条假数据，存进 ecommerce.duckdb
        ↓
3. verify.py       检查造出来的数据对不对（29 项）
        ↓
4. metrics.yaml    手写：GMV 怎么算、退款率怎么算
   dimensions.yaml 手写：能按哪几个维度分组
        ↓
5. loader.py       把 yaml 读成程序能用的东西，顺便检查写没写错
        ↓
6. guards.py       检查模型写的 SQL，有没有按 yaml 的规矩算
```

前面 3 个是**数据**，中间 2 个是**规矩**，最后 1 个是**检查**。

---

## 谁用谁

```
generate.py    用  schema.sql
verify.py      用  generate.py 造出来的数据库
loader.py      用  两个 yaml
guards.py      用  loader.py 读出来的结果
```

就这么四条，没有别的依赖。

---

## 从一句问题到一个页面

```
用户："上个月各省销售额"
        ↓
  loader 拿到 GMV 的定义（公式、必带的过滤、可用的维度）
        ↓
  模型写 SQL
        ↓
  guards 检查 → 不过就带着报错让模型改，最多改 2 次
        ↓
  查库（只读连接）→ 报错也回修复；0 行给一次改写机会
        ↓
  结论用模板拼，图表按结果形状选
        ↓
  每一步都通过 SSE 推给前端，边跑边显示
```

中间那两环（守卫 → 修复 → 守卫）是这条链路的核心：模型第一次写不对很正常，
关键是能把「哪里不对」原样喂回去让它改。

---

## 每个文件一句话

| 文件 | 干什么的 | 状态 |
|---|---|---|
| `data/schema.sql` | 6 张表的建表语句 | ✅ |
| `data/generate.py` | 按固定种子造数，3.7 秒跑完 | ✅ |
| `data/verify.py` | 验收工具，29 项检查 | ✅ |
| `backend/app/semantic/metrics.yaml` | 8 个指标怎么算 | ✅ |
| `backend/app/semantic/dimensions.yaml` | 6 个可分组维度 | ✅ |
| `backend/app/semantic/loader.py` | 读 yaml + 检查写没写错 | ✅ |
| `backend/app/graph/guards.py` | 检查模型写的 SQL | ✅ |
| `backend/app/graph/prompts.py` | 把指标口径摊开成提示词 | ✅ |
| `backend/app/graph/llm.py` | 调模型的唯一入口 | ✅ |
| `backend/app/graph/executor.py` | 只读查库，顺手取各维度的真实取值 | ✅ |
| `backend/app/graph/pipeline.py` | LangGraph 编排，含守卫→修复的环 | ✅ |
| `backend/app/graph/chart.py` | 按结果形状选图表，不走模型 | ✅ |
| `backend/app/api.py` | FastAPI，三个接口 + SSE | ✅ |
| `frontend/` | 一个页面，原生 JS + 本地 ECharts | ✅ |
| `eval/seed_queries.sql` | 手写 8 条标准 SQL，用来对答案 | ✅ |
| `eval/questions.yaml` | 30 道评测题 + 标准 SQL | ✅ |
| `eval/run.py` | 评测脚本，判分只看结果数字 | ✅ |

---

## 额外的：怎么确认 yaml 写对了

`metrics.yaml` 里的公式是手写的，可能写错。所以另写了一份 `seed_queries.sql`，
里面是 8 条**人工写好的标准 SQL**，直接拿去查库，得到基准数字。

然后让 `guards.py` 放行按 `metrics.yaml` 拼出来的 SQL，也查一遍。

```
seed_queries.sql  →  查库  →  基准数字
                                    ≠  比对
metrics.yaml      →  查库  →  另一组数字
```

两边一致，说明 yaml 写对了。目前 8 个指标全部一致。

---

## 额外的：模型怎么知道列里有哪些值

省份存的是「上海市」不是「上海」，支付方式存的是 `wechat` 不是「微信支付」。
模型猜不出来，猜错就是 0 行。

所以 `dimensions.yaml` 里每个分类维度都写了 `source`，启动时查一次
`SELECT DISTINCT`，把真实取值拼进提示词：

```
- payment_method: o.payment_method（取值只能是：alipay、bank_card、cash、wechat）
```

超过 40 个不同值的列自动跳过 —— 那种列塞进去只会把提示词撑爆。

---

## 额外的：接口为什么用 SSE

`POST /api/ask` 不等结果攒齐再返回，而是一步一条地往下推：

```
event: step
data: {"node": "guard", "message": "未通过：missing_filter"}   ← 守卫拦下来了
event: step
data: {"node": "repair", "message": "第 2 次生成 SQL（修复）"}
event: step
data: {"node": "guard", "message": "通过"}
...
event: result
data: {"answer": "...", "sql": "...", "chart": {...}}
```

攒成一个 JSON 返回，这些过程就全看不见了 —— 而这恰恰是这个项目最值得看的部分。

---

## 怎么跑

```bash
# 1. 造数据
python data/generate.py --out data/ecommerce.duckdb

# 2. 验收
python data/verify.py data/ecommerce.duckdb

# 3. 检查 yaml 写没写错
python backend/app/semantic/loader.py

# 4. 跑守卫的演示
cd backend && python -m app.graph.guards

# 5. 评测（参考 SQL 模式，不花 token）
python eval/run.py

# 6. 评测（真实模型，需要 key）
set LLM_API_KEY=sk-xxx
set LLM_MODEL=deepseek-v4-flash
set LLM_BASE_URL=https://api.deepseek.com/v1
python eval/run.py --model

# 7. 起页面
cd backend && python -m app.api
# 浏览器打开 http://127.0.0.1:8000
# 或者双击 frontend/index.html（后端起着就行，页面会连过来）
```

key 只从环境变量读，不写进任何文件。
