// 数据看板：读 data/stats.json，用 ECharts 画 8 类图，滚动到哪张图触发哪张
// 图的入场动画。ECharts 通过 insights.html 的 CDN <script> 提供全局 echarts。
import { Theme } from './storage.js?v=486100e7';
import { escapeHTML, attachSearchRedirect, fetchJSON } from './utils.js?v=486100e7';

// 站点暖色调色板（跟首页红主题呼应）
const PALETTE = [
  '#FF2442', '#FF8A5B', '#FFC24B', '#5AC8B0', '#5AA9E6',
  '#9B6BDF', '#FF6B8A', '#7ED957', '#E8A317', '#8AB4F8',
];

const STATE = {
  data: null,
  charts: new Map(),
  // 方向筛选：参与「各方向」图的频道 id 集合（默认全选）
  selectedChannels: null,
  // 频道 → 固定颜色（按全量顺序定，筛选时颜色不跳）
  channelColor: new Map(),
};

// 当前被勾选、参与可视化的频道（按全量顺序保持稳定）
function selChannels(d) {
  if (!STATE.selectedChannels) return d.channels;
  return d.channels.filter((c) => STATE.selectedChannels.has(c.id));
}
function colorOf(id, fallbackIdx) {
  return STATE.channelColor.get(id) || PALETTE[(fallbackIdx || 0) % PALETTE.length];
}
function selIds(d) {
  return STATE.selectedChannels ? [...STATE.selectedChannels] : d.channels.map((c) => c.id);
}

// 按勾选方向求和重排「排行类」分布（{cid:{name:count}} → [{name,count}] top N）
function sumRankByCh(byCh, topN) {
  if (!byCh) return null;
  const agg = {};
  for (const cid of selIds(STATE.data)) {
    const m = byCh[cid];
    if (!m) continue;
    for (const [k, v] of Object.entries(m)) agg[k] = (agg[k] || 0) + v;
  }
  return Object.entries(agg)
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, topN);
}

// 按勾选方向求和「时间序列类」（{cid:{label:{month:n}}} → {label:{month:sum}}）
function sumTimeByCh(byCh, months) {
  if (!byCh) return null;
  const agg = {};
  for (const cid of selIds(STATE.data)) {
    const m = byCh[cid];
    if (!m) continue;
    for (const [label, series] of Object.entries(m)) {
      const a = agg[label] || (agg[label] = {});
      for (const mm of months) a[mm] = (a[mm] || 0) + (series[mm] || 0);
    }
  }
  return agg;
}

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
  const chMeta = selChannels(d);
  const series = chMeta.map((c) => ({
    name: `${c.emoji || ''} ${c.name}`.trim(),
    type: 'line',
    stack: 'total',
    smooth: true,
    showSymbol: false,
    areaStyle: { opacity: 0.7 },
    emphasis: { focus: 'series' },
    lineStyle: { width: 0 },
    color: colorOf(c.id),
    data: d.months.map((m) => (d.cat_time[c.id] || {})[m] || 0),
  }));
  return {
    color: chMeta.map((c) => colorOf(c.id)),
    tooltip: baseTooltip({ trigger: 'axis' }),
    legend: { type: 'scroll', top: 0, textStyle: { color: ink(true), fontSize: 11 } },
    grid: Object.assign({}, BASE_GRID, { top: 56 }),
    xAxis: { type: 'category', boundaryGap: false, data: d.months, axisLabel: { color: ink(true), fontSize: 11 }, axisLine: { lineStyle: { color: ink(true) } } },
    yAxis: { type: 'value', axisLabel: { color: ink(true) }, splitLine: splitLine() },
    series,
    ...ANIM,
  };
}

// 独立折线（不堆叠）：每个方向一条线，高低直接对应当月数量，一眼看出谁多。
function optCatLine(d) {
  const chMeta = selChannels(d);
  const series = chMeta.map((c) => ({
    name: `${c.emoji || ''} ${c.name}`.trim(),
    type: 'line', smooth: true, showSymbol: false,
    lineStyle: { width: 2.5 },
    emphasis: { focus: 'series' },
    color: colorOf(c.id),
    data: d.months.map((m) => (d.cat_time[c.id] || {})[m] || 0),
  }));
  return {
    color: chMeta.map((c) => colorOf(c.id)),
    tooltip: baseTooltip({ trigger: 'axis' }),
    legend: { type: 'scroll', top: 0, textStyle: { color: ink(true), fontSize: 11 } },
    grid: Object.assign({}, BASE_GRID, { top: 56 }),
    xAxis: { type: 'category', boundaryGap: false, data: d.months, axisLabel: { color: ink(true), fontSize: 11 }, axisLine: { lineStyle: { color: ink(true) } } },
    yAxis: { type: 'value', axisLabel: { color: ink(true) }, splitLine: splitLine() },
    series, ...ANIM,
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

// 各方向占比环图：受方向筛选影响，颜色与上面两张图保持一致。
function optCatCount(d) {
  const items = selChannels(d).map((c) => ({
    name: `${c.emoji || ''} ${c.name}`.trim(),
    value: d.cat_count[c.id] || 0,
    itemStyle: { color: colorOf(c.id) },
  }));
  return {
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
      data: items,
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

// 各会议 × 方向 论文数（堆叠柱状）：x=会议，每个频道一段，颜色与「各方向」图一致。
function optVenueChannel(d) {
  const vc = d.venue_channel || {};
  const venues = vc.venues || [];
  const chName = new Map((d.channels || []).map((c) => [c.id, `${c.emoji || ''} ${c.name}`.trim()]));
  const series = (vc.series || [])
    .filter((s) => (s.data || []).some((n) => n > 0))
    .map((s) => ({
      name: chName.get(s.channel) || s.channel,
      type: 'bar',
      stack: 'total',
      emphasis: { focus: 'series' },
      color: colorOf(s.channel),
      data: s.data,
    }));
  return {
    tooltip: baseTooltip({ trigger: 'axis', axisPointer: { type: 'shadow' } }),
    legend: { type: 'scroll', top: 0, textStyle: { color: ink(true), fontSize: 11 } },
    grid: Object.assign({}, BASE_GRID, { top: 56 }),
    xAxis: { type: 'category', data: venues, axisLabel: { color: ink(true), fontSize: 11 }, axisLine: { lineStyle: { color: ink(true) } } },
    yAxis: { type: 'value', axisLabel: { color: ink(true) }, splitLine: splitLine() },
    series,
    ...ANIM,
  };
}

function optMethodTime(d) {
  // 受方向筛选影响：从 method_time_by_ch 按勾选方向求和，再取前 6 条线。
  let timeMap = d.method_time;
  if (d.method_time_by_ch) {
    const agg = sumTimeByCh(d.method_time_by_ch, d.months);
    const totals = Object.entries(agg)
      .map(([k, s]) => [k, Object.values(s).reduce((a, b) => a + b, 0)])
      .sort((a, b) => b[1] - a[1]);
    timeMap = {};
    for (const [k] of totals.slice(0, 6)) timeMap[k] = agg[k];
  }
  const tags = Object.keys(timeMap);
  const series = tags.map((t, i) => ({
    name: t, type: 'line', smooth: true, showSymbol: false,
    color: PALETTE[i % PALETTE.length], lineStyle: { width: 2.5 },
    emphasis: { focus: 'series' },
    data: d.months.map((m) => timeMap[t][m] || 0),
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
  const hotMap = d.hot_by_ch ? sumTimeByCh(d.hot_by_ch, d.months) : d.hot_keywords;
  const labels = Object.keys(hotMap);
  const series = labels.map((t, i) => ({
    name: t, type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
    color: PALETTE[i % PALETTE.length], lineStyle: { width: 2.5 },
    emphasis: { focus: 'series' },
    data: d.months.map((m) => hotMap[t][m] || 0),
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

// 受方向筛选影响的排行类图（按勾选方向求和重排），老 stats.json 没 *_by_ch 时回退全站
const _GRAD_METHOD = () => new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: '#5AA9E6' }, { offset: 1, color: '#9B6BDF' }]);
const _GRAD_PLATFORM = () => new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: '#5AC8B0' }, { offset: 1, color: '#5AA9E6' }]);
function optInst(d) { return optHBar(sumRankByCh(d.inst_by_ch, 15) || d.institutions); }
function optMethodTop(d) { return optHBar(sumRankByCh(d.method_by_ch, 12) || d.method_top, _GRAD_METHOD()); }
function optPlatform(d) { return optHBar(sumRankByCh(d.platform_by_ch, 12) || d.platform, _GRAD_PLATFORM()); }

// 所有受方向筛选影响的图：勾选变化时统一重渲。
const FILTER_CHARTS = {
  'chart-cat-time': optCatTime,
  'chart-cat-line': optCatLine,
  'chart-cat-count': optCatCount,
  'chart-inst': optInst,
  'chart-method-top': optMethodTop,
  'chart-method-time': optMethodTime,
  'chart-hot': optHot,
  'chart-platform': optPlatform,
};

function rerenderCategoryCharts() {
  for (const [id, fn] of Object.entries(FILTER_CHARTS)) {
    const chart = STATE.charts.get(id);
    // notMerge=true：方向数变化时彻底替换 series，避免残留旧曲线
    if (chart) chart.setOption(fn(STATE.data), true);
  }
}

function syncAllBtn() {
  if (!STATE.data) return;
  const total = STATE.data.channels.length;
  const sel = STATE.selectedChannels ? STATE.selectedChannels.size : total;
  const btn = document.getElementById('cat-filter-all');
  if (btn) btn.textContent = sel >= total ? '全不选' : '全选';
  const cnt = document.getElementById('cat-filter-count');
  if (cnt) cnt.textContent = `${sel}/${total}`;
}

// 构建方向筛选 chips（默认全选）
function buildCatFilter(d) {
  const wrap = document.getElementById('cat-filter');
  const chips = document.getElementById('cat-filter-chips');
  if (!wrap || !chips) return;
  STATE.selectedChannels = new Set(d.channels.map((c) => c.id));
  chips.innerHTML = d.channels.map((c) => `
    <label class="rp-filter-chip">
      <input type="checkbox" value="${escapeHTML(c.id)}" checked />
      <span>${escapeHTML(`${c.emoji || ''} ${c.name}`.trim())}</span>
    </label>`).join('');
  chips.querySelectorAll('input').forEach((inp) => {
    inp.addEventListener('change', () => {
      if (inp.checked) STATE.selectedChannels.add(inp.value);
      else STATE.selectedChannels.delete(inp.value);
      syncAllBtn();
      rerenderCategoryCharts();
    });
  });
  document.getElementById('cat-filter-all')?.addEventListener('click', () => {
    const total = d.channels.length;
    const allOn = STATE.selectedChannels.size >= total;
    STATE.selectedChannels = new Set(allOn ? [] : d.channels.map((c) => c.id));
    chips.querySelectorAll('input').forEach((i) => { i.checked = STATE.selectedChannels.has(i.value); });
    syncAllBtn();
    rerenderCategoryCharts();
  });
  // 展开 / 收起（吸顶时省空间）
  document.getElementById('cat-filter-toggle')?.addEventListener('click', () => {
    const collapsed = wrap.classList.toggle('is-collapsed');
    document.getElementById('cat-filter-toggle')?.setAttribute('aria-expanded', String(!collapsed));
  });
  wrap.hidden = false;
  syncAllBtn();
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

// 重渲所有已挂载的图表（主题切换后用：option 里的文字/网格/tooltip 颜色是 mount 时
// 按当时主题定的，切暗色/亮色后要重算一次，否则颜色停在旧主题）。
function rerenderAllCharts() {
  if (!STATE.data || !STATE.chartDefs) return;
  for (const [id, fn] of Object.entries(STATE.chartDefs)) {
    const chart = STATE.charts.get(id);
    if (chart) {
      try { chart.setOption(fn(STATE.data), true); } catch (_) { /* noop */ }
    }
  }
}

async function main() {
  Theme.init();
  document.querySelector('#theme-toggle')?.addEventListener('click', () => {
    Theme.cycle();
    rerenderAllCharts();
  });
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

  // 频道固定配色（按全量顺序）+ 方向筛选（默认全选）
  STATE.channelColor = new Map(d.channels.map((c, i) => [c.id, PALETTE[i % PALETTE.length]]));
  buildCatFilter(d);

  // 所有图表的 id → option 构造器，统一注册（lazyMount 用 + 主题切换重渲用）。
  const defs = {
    'chart-cat-time': optCatTime,
    'chart-cat-line': optCatLine,
    'chart-cat-count': optCatCount,
    'chart-source': (x) => optDonut(x.source, 'name', 'value'),
    'chart-intake': optIntake,
    'chart-inst': optInst,
    'chart-method-top': optMethodTop,
    'chart-method-time': optMethodTime,
    'chart-hot': optHot,
    'chart-platform': optPlatform,
  };
  if ((d.venues || []).length) {
    defs['chart-venue'] = (x) => optHBar(x.venues, new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: '#9B6BDF' }, { offset: 1, color: '#FF6B8A' }]));
  } else {
    const card = document.getElementById('chart-venue');
    if (card && card.parentElement) card.parentElement.style.display = 'none';
  }
  if (((d.venue_channel || {}).venues || []).length) {
    defs['chart-venue-channel'] = optVenueChannel;
  } else {
    const card = document.getElementById('chart-venue-channel');
    if (card && card.parentElement) card.parentElement.style.display = 'none';
  }
  STATE.chartDefs = defs;
  for (const [id, fn] of Object.entries(defs)) lazyMount(id, fn);

  window.addEventListener('resize', () => {
    for (const c of STATE.charts.values()) c.resize();
  });
}

main();
