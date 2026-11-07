/* 前端只有一件事：把 SSE 推过来的东西摆出来。
   图表类型、配色、标签位置都由后端给的 spec 和这里的固定规则决定。 */

const EXAMPLES = [
  "2026年6月各省份的销售额，从高到低",
  "最近30天的销售额",
  "2026年6月的退款率是多少",
  "各品类的退款率从高到低",
  "最近生意怎么样",
];

const NODE_LABELS = {
  understand: "理解问题",
  generate: "生成",
  guard: "口径校验",
  repair: "修复",
  execute: "查询数据",
  summarize: "生成结论",
  clarify: "追问",
  give_up: "放弃",
};

const el = (id) => document.getElementById(id);
let chart = null;
let lastResult = null;

/* 双击打开 index.html 时页面是 file://，同源的 /api 并不存在，
   所以显式指到本机后端；从 http://127.0.0.1:8000 打开时用同源地址就够。 */
const API_BASE = location.protocol === "file:" ? "http://127.0.0.1:8000" : "";

/* ── 主题 ───────────────────────────────────────────── */

function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;

  el("theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    const now = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = now;
    localStorage.setItem("theme", now);
    if (lastResult) renderFigure(lastResult);
  });
}

function token(name) {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}

function palette() {
  return {
    surface: token("--surface"),
    text: token("--text-primary"),
    secondary: token("--text-secondary"),
    muted: token("--text-muted"),
    grid: token("--grid"),
    axis: token("--axis"),
    border: token("--border"),
    series1: token("--series-1"),
    sans: token("--sans") || "system-ui, sans-serif",
  };
}

/* ── 数字格式 ───────────────────────────────────────── */

function formatNumber(value, unit) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  if (unit === "%") return `${(value * 100).toFixed(2)}%`;

  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  if (Number.isInteger(value)) return value.toLocaleString("zh-CN");
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

/* ── 提问 ───────────────────────────────────────────── */

async function ask(question) {
  const submit = el("submit");
  submit.disabled = true;
  submit.textContent = "查询中…";

  el("empty").hidden = true;
  el("card").hidden = true;
  el("trace").innerHTML = "";
  el("trace-empty").hidden = true;
  lastResult = null;

  try {
    const response = await fetch(`${API_BASE}/api/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });

    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `请求失败（${response.status}）`);
    }

    await readStream(response.body);
  } catch (error) {
    el("empty").hidden = false;
    // fetch 连不上时抛的是 TypeError，原文是 "Failed to fetch"，看不懂
    showError(
      error instanceof TypeError
        ? `连不上后端。先在终端里启动：python backend/app/api.py，再刷新页面。`
        : error.message
    );
  } finally {
    submit.disabled = false;
    submit.textContent = "提问";
  }
}

async function readStream(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const parsed = parseEvent(chunk);
      if (parsed) handleEvent(parsed.event, parsed.data);
    }
  }
}

function parseEvent(chunk) {
  let event = "message";
  const dataLines = [];

  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }

  if (!dataLines.length) return null;

  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}

function handleEvent(event, data) {
  if (event === "step") addStep(data);
  else if (event === "result") showResult(data);
  else if (event === "error") showError(data.message);
}

/* ── 执行轨迹 ───────────────────────────────────────── */

function addStep(step) {
  const item = document.createElement("li");
  const label = NODE_LABELS[step.node] || step.node;

  if (step.node === "give_up") item.className = "fail";
  else if (/未通过|报错|失败|没能|0 行/.test(step.message)) item.className = "warn";
  else item.className = "ok";

  const name = document.createElement("span");
  name.className = "node";
  name.textContent = label;

  const message = document.createElement("span");
  message.className = "message";
  message.textContent = step.message;

  item.append(name, message);
  el("trace").append(item);
}

/* ── 结果 ───────────────────────────────────────────── */

function showResult(result) {
  lastResult = result;
  el("card").hidden = false;

  renderFigure(result);
  renderAnswer(result);
  renderTable(result);
  renderSql(result);

  if (!el("trace").children.length) {
    el("trace-empty").hidden = false;
  }
}

function renderAnswer(result) {
  const node = el("answer");
  node.className = "answer";

  if (result.status === "clarify") node.classList.add("clarify");
  if (result.status === "failed") node.classList.add("error");

  // 一个大数字已经说完了，下面再复述一遍「退款率为 18.41%」是废话
  node.hidden = result.status === "ok" && result.chart?.type === "stat";
  node.textContent = result.answer || "（没有结论）";
}

function renderSql(result) {
  const sql = (result.sql || "").trim();
  el("sql").textContent = sql;
  el("sql-details").hidden = !sql;
}

function renderFigure(result) {
  const figure = el("figure");
  figure.innerHTML = "";
  if (chart) {
    chart.dispose();
    chart = null;
  }

  const spec = result.chart;
  if (!spec) {
    el("data-details").hidden = !(result.columns && result.columns.length);
    return;
  }

  if (spec.type === "stat") {
    figure.append(buildStat(spec));
  } else if (spec.type === "bar" || spec.type === "line") {
    if (!window.echarts) {
      const note = document.createElement("p");
      note.className = "muted small";
      note.textContent = "图表库没加载出来，看下面的表格。";
      figure.append(note);
    } else {
      figure.append(buildChart(spec, result));
    }
  }

  if (spec.type === "table" && spec.reason) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = spec.reason;
    figure.append(note);
  }
}

function buildStat(spec) {
  const box = document.createElement("div");
  box.className = "stat";

  const label = document.createElement("p");
  label.className = "stat-label";
  label.textContent = spec.label;

  const value = document.createElement("p");
  value.className = "stat-value";

  if (spec.unit === "%") {
    value.textContent = formatNumber(spec.value, "%");
  } else {
    value.textContent = formatNumber(spec.value);
    const unit = document.createElement("span");
    unit.className = "stat-unit";
    unit.textContent = spec.unit || "";
    value.append(unit);
  }

  box.append(label, value);
  return box;
}

function buildChart(spec, result) {
  const wrap = document.createElement("div");
  wrap.className = "chart";

  const title = document.createElement("p");
  title.className = "chart-title";
  title.textContent = spec.title;

  const canvas = document.createElement("div");
  canvas.className = "chart-canvas";

  const rows = result.rows || [];
  // 条形图一根一行、随行数长高；折线的高度和点数无关
  canvas.style.height =
    spec.type === "bar" ? `${Math.max(180, rows.length * 32 + 48)}px` : "320px";

  wrap.append(title, canvas);
  requestAnimationFrame(() => {
    chart = echarts.init(canvas);
    chart.setOption(
      spec.type === "bar" ? barOption(spec, rows) : lineOption(spec, rows)
    );
  });
  return wrap;
}

function barOption(spec, rows) {
  const c = palette();
  const data = rows
    .map((row) => ({ name: String(row[spec.x]), value: row[spec.y] }))
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));

  return {
    animationDuration: 260,
    grid: { left: 4, right: 76, top: 8, bottom: 4, containLabel: true },
    tooltip: tooltipStyle(c, (p) => `${p.name}<br><b>${formatNumber(p.value, spec.unit)}</b>`),
    xAxis: {
      type: "value",
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: c.muted,
        fontFamily: c.sans,
        formatter: (v) => formatTick(v, spec.unit),
      },
      splitLine: { lineStyle: { color: c.grid, width: 1 } },
    },
    yAxis: {
      type: "category",
      data: data.map((d) => d.name),
      inverse: true,
      axisLine: { lineStyle: { color: c.axis } },
      axisTick: { show: false },
      axisLabel: { color: c.secondary, fontFamily: c.sans },
    },
    series: [
      {
        type: "bar",
        data: data.map((d) => d.value),
        barMaxWidth: 24,
        itemStyle: { color: c.series1, borderRadius: [0, 4, 4, 0] },
        label: {
          show: data.length <= 16,
          position: "right",
          color: c.secondary,
          fontFamily: c.sans,
          fontSize: 12,
          formatter: (p) => formatNumber(p.value, spec.unit),
        },
      },
    ],
  };
}

function lineOption(spec, rows) {
  const c = palette();
  const raw = rows.map((row) => String(row[spec.x]));
  const labels = shortenDates(raw);
  const values = rows.map((row) => row[spec.y]);

  return {
    animationDuration: 260,
    grid: { left: 4, right: 52, top: 16, bottom: 4, containLabel: true },
    tooltip: tooltipStyle(
      c,
      (p) => {
        const point = p[0];
        return `${point.axisValue}<br><b>${formatNumber(point.value, spec.unit)}</b>`;
      },
      "axis"
    ),
    xAxis: {
      type: "category",
      data: labels,
      boundaryGap: false,
      axisLine: { lineStyle: { color: c.axis } },
      axisTick: { show: false },
      axisLabel: { color: c.muted, fontFamily: c.sans },
    },
    yAxis: {
      type: "value",
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: c.muted,
        fontFamily: c.sans,
        formatter: (v) => formatTick(v, spec.unit),
      },
      splitLine: { lineStyle: { color: c.grid, width: 1 } },
    },
    series: [
      {
        type: "line",
        data: values,
        // 点太多就别每个点都画圆，线本身就是数据；悬浮时仍会出点
        showSymbol: values.length <= 40,
        symbolSize: 8,
        lineStyle: { width: 2, color: c.series1, cap: "round", join: "round" },
        itemStyle: { color: c.series1, borderColor: c.surface, borderWidth: 2 },
        areaStyle: { color: c.series1, opacity: 0.1 },
        // 折线只在末端标一个数，不是每个点都标
        endLabel: {
          show: true,
          color: c.secondary,
          fontFamily: c.sans,
          fontSize: 12,
          formatter: (p) => formatNumber(p.value, spec.unit),
        },
      },
    ],
  };
}

function tooltipStyle(c, formatter, trigger = "item") {
  return {
    trigger,
    backgroundColor: c.surface,
    borderColor: c.border,
    borderWidth: 1,
    padding: [8, 12],
    textStyle: { color: c.text, fontFamily: c.sans, fontSize: 13 },
    formatter,
  };
}

/* 坐标轴刻度要干净：50万，不是 50.00万 */
function formatTick(value, unit) {
  if (unit === "%") return `${(value * 100).toFixed(0)}%`;
  if (typeof value !== "number") return String(value);

  const abs = Math.abs(value);
  if (abs >= 1e8) return `${+(value / 1e8).toFixed(1)}亿`;
  if (abs >= 1e4) return `${+(value / 1e4).toFixed(1)}万`;
  if (abs >= 1) return `${+value.toFixed(1)}`;
  return `${+value.toFixed(2)}`;
}

/* 时间轴标签：月粒度留「2026-06」，日粒度在同一年时省掉年份。
   省年份是为了让 90 个点排得下；跨年时留着，不省。 */
function shortenDates(texts) {
  if (!texts.every((t) => /^\d{4}-\d{2}-\d{2}$/.test(t))) return texts;

  // 全是月初的，就是月粒度
  if (texts.every((t) => t.endsWith("-01"))) return texts.map((t) => t.slice(0, 7));
  if (texts.length <= 12) return texts;

  const years = new Set(texts.map((t) => t.slice(0, 4)));
  return years.size === 1 ? texts.map((t) => t.slice(5)) : texts;
}

/* ── 数据表 ─────────────────────────────────────────── */

function renderTable(result) {
  const columns = result.columns || [];
  const rows = result.rows || [];
  const wrap = el("table-wrap");
  wrap.innerHTML = "";

  // 只有一个数时，那个数就在上面，表格没什么可看的
  const singleValue = result.chart?.type === "stat";

  if (!columns.length || singleValue) {
    el("data-details").hidden = true;
    return;
  }

  el("data-details").hidden = false;

  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");

  for (const column of columns) {
    const th = document.createElement("th");
    th.textContent = column;
    headRow.append(th);
  }
  thead.append(headRow);

  const tbody = document.createElement("tbody");
  const numeric = new Set(
    columns.filter((column) =>
      rows.every((row) => typeof row[column] === "number")
    )
  );

  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const column of columns) {
      const td = document.createElement("td");
      const value = row[column];
      if (numeric.has(column)) {
        td.className = "num";
        td.textContent = formatNumber(value);
      } else {
        td.textContent = value === null || value === undefined ? "—" : String(value);
      }
      tr.append(td);
    }
    tbody.append(tr);
  }

  table.append(thead, tbody);
  wrap.append(table);
}

/* ── 报错 ───────────────────────────────────────────── */

function showError(message) {
  el("card").hidden = false;
  el("figure").innerHTML = "";
  el("data-details").hidden = true;
  el("sql-details").hidden = true;

  const node = el("answer");
  node.className = "answer error";
  node.textContent = message;
}

/* ── 启动 ───────────────────────────────────────────── */

function init() {
  initTheme();

  const examples = el("examples");
  for (const text of EXAMPLES) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.addEventListener("click", () => {
      el("question").value = text;
      ask(text);
    });
    examples.append(button);
  }

  el("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = el("question").value.trim();
    if (question) ask(question);
  });

  window.addEventListener("resize", () => chart && chart.resize());

  // ?q=... 直接把问题带进来，方便分享链接和截图
  const initial = new URLSearchParams(location.search).get("q");
  if (initial) {
    el("question").value = initial;
    ask(initial);
  }
}

init();
