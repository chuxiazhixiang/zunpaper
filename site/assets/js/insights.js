// 数据看板：读 data/stats.json，用 ECharts 画 8 类图，滚动到哪张图触发哪张
// 图的入场动画。ECharts 通过 insights.html 的 CDN <script> 提供全局 echarts。
import { Theme } from './storage.js?v=33cdb9ca';
import { escapeHTML, attachSearchRedirect, fetchJSON } from './utils.js?v=33cdb9ca';

// 站点暖色调色板（跟首页红主题呼应）
const PALETTE = [
  '#FF2442', '#FF8A5B', '#FFC24B', '#5AC8B0', '#5AA9E6',
  '#9B6BDF', '#FF6B8A', '#7ED957', '#E8A317', '#8AB4F8',
];

const STATE = { data: null, charts: new Map() };

function isDark() {
  return document.documentElement.classList.contains('rp-dark') ||
    document.body.classList.contains('rp-dark') ||
    document.documentElement.getAttribute('data-theme') === 'dark';
}

// 通用文字 / 网格颜色（兼顾明暗）
function ink(soft) {
  return isDark() ? (soft ? '#9aa0ac' : '#e6e8ec') : (soft ? '#888' : '#333');
}
function splitLine() {
  return { lineStyle: { color: isDark() ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)' } };
}

const BASE_GRID = { left: 8, right: 18, top: 30, bottom: 8, containLabel: true };

function baseTooltip(extra) {
  return Object.assign({
    backgroundColor: isDark() ? 'rgba(30,30,36,0.95)' : 'rgba(255,255,255,0.97)',
    borderColor: 'rgba(0,0,0,0.08)',
    textStyle: { color: ink(false), fontSize: 12 },
    extraCssText: 'box-shadow:0 6px 24px rgba(0,0,0,0.18);border-radius:10px;',
  }, extra || {});
}

const ANIM = { animationDuration: 900, animationEasing: 'cubicOut', animationDelay: (i) => i * 30 };

// ---- 各图 option 构造 --------------------------------------------------

function optCatTime(d) {
  const chMeta = d.channels;
  const series = chMeta.map((c, i) => ({
    name: `${c.emoji || ''} ${c.name}`.trim(),
    type: 'line',
    stack: 'total',
    smooth: true,
    showSymbol: false,
    areaStyle: { opacity: 0.7 },
    emphasis: { focus: 'series' },
    lineStyle: { width: 0 },
    color: PALETTE[i % PALETTE.length],
    data: d.months.map((m) => d.cat_time[c.id][m] || 0),
  }));
  return {
    color: PALETTE,
    tooltip: baseTooltip({ trigger: 'axis' }),
    legend: { type: 'scroll', top: 0, textStyle: { color: ink(true), fontSize: 11 } },
    grid: Object.assign({}, BASE_GRID, { top: 56 }),
    xAxis: { type: 'category', boundaryGap: false, data: d.months, axisLabel: { color: ink(true), fontSize: 11 }, axisLine: { lineStyle: { color: ink(true) } } },
    yAxis: { type: 'value', axisLabel: { color: ink(true) }, splitLine: splitLine() },
    series,
    ...ANIM,
  };
}

function optDonut(items, nameKey, valKey) {
  return {
    color: PALETTE,
    tooltip: baseTooltip({ trigger: 'item', formatter: '{b}: {c} ({d}%)' }),
    legend: { type: 'scroll', bottom: 0, textStyle: { color: ink(true), fontSize: 11 } },
    series: [{
      type: 'pie',
      radius: ['42%', '70%'],
      center: ['50%', '46%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: isDark() ? '#1e1e24' : '#fff', borderWidth: 2, borderRadius: 6 },
      label: { color: ink(true), fontSize: 11, formatter: '{b}\n{d}%' },
      labelLine: { length: 8, length2: 8 },
      data: items.map((it) => ({ name: it[nameKey], value: it[valKey] })),
    }],
    animationDuration: 900,
    animationEasing: 'cubicOut',
  };
}

function optHBar(items, color) {
  const sorted = [...items].sort((a, b) => a.count - b.count); // 从下到上递增
  return {
    tooltip: baseTooltip({ trigger: 'axis', axisPointer: { type: 'shadow' } }),
    grid: Object.assign({}, BASE_GRID, { left: 8, right: 40 }),
    xAxis: { type: 'value', axisLabel: { color: ink(true) }, splitLine: splitLine() },
    yAxis: { type: 'category', data: sorted.map((i) => i.name), axisLabel: { color: ink(true), fontSize: 11 }, axisLine: { lineStyle: { color: ink(true) } } },
    series: [{
      type: 'bar',
      data: sorted.map((i) => i.count),
      itemStyle: {
        borderRadius: [0, 6, 6, 0],
        color: color || new echarts.graphic.LinearGradient(0, 0, 1, 0, [
          { offset: 0, color: '#FF6B8A' }, { offset: 1, color: '#FF2442' },
        ]),
      },
      label: { show: true, position: 'right', color: ink(true), fontSize: 11 },
      barMaxWidth: 22,
    }],
    animationDuration: 1000,
    animationEasing: 'elasticOut',
    animationDelay: (i) => i * 40,
  };
}

function optMethodTime(d) {
  const tags = Object.keys(d.method_time);
  const series = tags.map((t, i) => ({
    name: t, type: 'line', smooth: true, showSymbol: false,
    color: PALETTE[i % PALETTE.length], lineStyle: { width: 2.5 },
    emphasis: { focus: 'series' },
    data: d.months.map((m) => d.method_time[t][m] || 0),
  }));
  return {
    color: PALETTE,
    tooltip: baseTooltip({ trigger: 'axis' }),
    legend: { type: 'scroll', top: 0, textStyle: { color: ink(true), fontSize: 11 } },
    grid: Object.assign({}, BASE_GRID, { top: 56 }),
    xAxis: { type: 'category', boundaryGap: false, data: d.months, axisLabel: { color: ink(true), fontSize: 11 }, axisLine: { lineStyle: { color: ink(true) } } },
    yAxis: { type: 'value', axisLabel: { color: ink(true) }, splitLine: splitLine() },
    series, ...ANIM,
  };
}

function optHot(d) {
  const labels = Object.keys(d.hot_keywords);
  const series = labels.map((t, i) => ({
    name: t, type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
    color: PALETTE[i % PALETTE.length], lineStyle: { width: 2.5 },
    emphasis: { focus: 'series' },
    data: d.months.map((m) => d.hot_keywords[t][m] || 0),
  }));
  return {
    color: PALETTE,
    tooltip: baseTooltip({ trigger: 'axis' }),
    legend: { type: 'scroll', top: 0, textStyle: { color: ink(true), fontSize: 11 } },
    grid: Object.assign({}, BASE_GRID, { top: 56 }),
    xAxis: { type: 'category', boundaryGap: false, data: d.months, axisLabel: { color: ink(true), fontSize: 11 }, axisLine: { lineStyle: { color: ink(true) } } },
    yAxis: { type: 'value', axisLabel: { color: ink(true) }, splitLine: splitLine() },
    series, ...ANIM,
  };
}

function optIntake(d) {
  const data = d.intake_daily; // [[date,count],...]
  if (!data.length) return { series: [] };
  const start = data[0][0];
  const end = data[data.length - 1][0];
  const maxV = Math.max(...data.map((x) => x[1]), 1);
  return {
    tooltip: baseTooltip({ formatter: (p) => `${p.value[0]}：${p.value[1]} 篇` }),
    visualMap: {
      min: 0, max: maxV, calculable: false, orient: 'horizontal',
      left: 'center', bottom: 0, itemWidth: 12, itemHeight: 120,
      inRange: { color: isDark()
        ? ['#2a2a33', '#5a2030', '#a82f4a', '#FF2442']
        : ['#f3f3f5', '#ffd0d9', '#ff8aa0', '#FF2442'] },
      textStyle: { color: ink(true), fontSize: 11 },
    },
    calendar: {
      top: 28, left: 30, right: 16, cellSize: ['auto', 16], range: [start, end],
      itemStyle: { color: 'transparent', borderColor: isDark() ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.05)', borderWidth: 1 },
      splitLine: { show: false },
      dayLabel: { color: ink(true), fontSize: 10, nameMap: ['日', '一', '二', '三', '四', '五', '六'] },
      monthLabel: { color: ink(true), fontSize: 11, nameMap: 'cn' },
      yearLabel: { show: false },
    },
    series: [{
      type: 'heatmap', coordinateSystem: 'calendar', data,
      itemStyle: { borderRadius: 3 },
    }],
    animationDuration: 1200,
  };
}

// ---- 渲染调度 -----------------------------------------------------------

function mount(id, optFn) {
  const el = document.getElementById(id);
  if (!el || STATE.charts.has(id)) return;
  let chart;
  try {
    chart = echarts.init(el, null, { renderer: 'canvas' });
    const opt = optFn(STATE.data);
    chart.setOption(opt);
  } catch (e) {
    console.warn('[insights] chart init failed', id, e);
    return;
  }
  STATE.charts.set(id, chart);
}

function renderStatCards(d) {
  const el = document.getElementById('stat-cards');
  if (!el) return;
  const cards = [
    ['📄', d.totals.papers, '篇论文 / 资讯'],
    ['🐙', d.totals.repos, '个开源项目'],
    ['🏛', d.institutions.length ? d.institutions[0].name : '—', '产出最多机构', true],
    ['🤖', d.platform.length ? d.platform[0].name : '—', '最热机器人本体', true],
  ];
  el.innerHTML = cards.map(([emoji, val, label, small]) => `
    <div class="rp-stat-card">
      <div class="rp-stat-card__emoji">${emoji}</div>
      <div class="rp-stat-card__val ${small ? 'is-small' : ''}">${escapeHTML(String(val))}</div>
      <div class="rp-stat-card__label">${escapeHTML(label)}</div>
    </div>`).join('');
}

// 滚动到视口才 init（动画在此时播放）
function lazyMount(id, optFn) {
  const el = document.getElementById(id);
  if (!el) return;
  const io = new IntersectionObserver((entries, obs) => {
    if (entries.some((e) => e.isIntersecting)) {
      mount(id, optFn);
      obs.disconnect();
    }
  }, { rootMargin: '120px 0px' });
  io.observe(el);
}

async function main() {
  Theme.init();
  document.querySelector('#theme-toggle')?.addEventListener('click', () => { Theme.cycle(); });
  attachSearchRedirect();

  const d = await fetchJSON('data/stats.json').then((r) => r.json()).catch(() => null);
  if (!d) {
    document.querySelector('#insights-sub').textContent = '数据还没生成，等下次构建跑完再来～';
    return;
  }
  STATE.data = d;
  document.querySelector('#insights-sub').textContent =
    `每天随论文更新 · 当前 ${d.totals.papers} 篇论文、${d.totals.repos} 个开源项目`;
  const pn = document.getElementById('platform-note');
  if (pn) pn.textContent = `（基于披露型号的 ${d.platform_disclosed} 篇）`;

  renderStatCards(d);

  lazyMount('chart-cat-time', optCatTime);
  lazyMount('chart-cat-count', (x) => optDonut(x.channels.map((c) => ({ name: `${c.emoji || ''} ${c.name}`.trim(), value: x.cat_count[c.id] || 0 })), 'name', 'value'));
  lazyMount('chart-source', (x) => optDonut(x.source, 'name', 'value'));
  lazyMount('chart-intake', optIntake);
  lazyMount('chart-inst', (x) => optHBar(x.institutions));
  lazyMount('chart-method-top', (x) => optHBar(x.method_top, new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: '#5AA9E6' }, { offset: 1, color: '#9B6BDF' }])));
  lazyMount('chart-method-time', optMethodTime);
  lazyMount('chart-hot', optHot);
  lazyMount('chart-platform', (x) => optHBar(x.platform, new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: '#5AC8B0' }, { offset: 1, color: '#5AA9E6' }])));

  window.addEventListener('resize', () => {
    for (const c of STATE.charts.values()) c.resize();
  });
}

main();
