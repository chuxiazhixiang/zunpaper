// Homepage feed: load papers, bucket by period (day/week/year depending on
// how far back), render Xiaohongshu-style masonry one bucket at a time, and
// append older buckets via IntersectionObserver as user scrolls.
// 站点切日更后，近 7 天按日分组（今天/昨天/前天/X月X日），让用户清晰看到
// 每日产出节奏；7-90 天按周分组保留一定密度；>90 天按年分组防止上古论文
// 把页面塞爆。
// before the user hits the bottom.

import { Favorites, Curated, Reads, Theme } from './storage.js?v=537b1498';
import {
  pickCover,
  loadPalettes,
  coverStyleAttr,
  escapeHTML,
  formatAuthors,
  paperUrl,
  HEART_SVG_OUTLINE,
  HEART_SVG_FILL,
  showToast,
  fetchJSON,
} from './utils.js?v=537b1498';

const STATE = {
  channels: [],
  papers: [],
  palettes: [],
  mode: 'papers',          // 'papers' | 'repos' —— 顶层切换；方向标签对两者通用
  repoSort: 'stars',       // 'stars' | 'updated' —— 开源项目排序方式
  activeChannel: 'all',
  activeVenue: '',         // 会议/期刊筛选（去年份的 base 名，如 "ICRA"）；'' = 全部
  searchQuery: '',
  // Filled per render: bucket maps + ordered list of bucket keys.
  // 桶 key 形如 "day:0"/"day:1"/"week:1"/"year:2025"，meta 含 emoji/label
  buckets: new Map(),
  metaMap: new Map(),
  periodOrder: [],
  renderedKeys: new Set(),
  observer: null,
};

const DAY_MS = 86400000;

async function loadData() {
  const [index, channelsResp, siteResp, palettes] = await Promise.all([
    fetchJSON('data/index.json').then((r) => r.json()).catch(() => ({ papers: [] })),
    fetchJSON('data/channels.json').then((r) => r.json()).catch(() => ({ channels: [] })),
    fetchJSON('data/site.json').then((r) => r.json()).catch(() => ({})),
    loadPalettes(),
  ]);
  STATE.papers = index.papers || [];
  STATE.channels = channelsResp.channels || [];
  STATE.site = siteResp || {};
  STATE.palettes = palettes || [];
}

function renderCrawlBanner() {
  const el = document.querySelector('#crawl-banner');
  if (!el) return;
  const days = STATE.site?.crawl_lookback_days;
  const generated = STATE.site?.crawl_generated_at;
  if (!days) { el.hidden = true; return; }
  let when = '';
  if (generated) {
    try {
      const d = new Date(generated);
      const today = new Date();
      const sameDay =
        d.getFullYear() === today.getFullYear() &&
        d.getMonth() === today.getMonth() &&
        d.getDate() === today.getDate();
      when = sameDay
        ? `今天 ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')} 更新`
        : `${d.getMonth() + 1}月${d.getDate()}日更新`;
    } catch (_) {}
  }
  el.innerHTML =
    `<span class="rp-crawl-banner__dot"></span>` +
    `<span>arXiv 抓取近 ${days} 天${when ? ` · ${when}` : ''}</span>`;
  el.hidden = false;
}

function buildChannelTabs() {
  const wrap = document.querySelector('#channel-tabs');
  if (!wrap) return;
  const all = [{ id: 'all', name: '全部', emoji: '✨' }, ...STATE.channels];
  wrap.innerHTML = all
    .map(
      (c) =>
        `<button class="rp-tab ${c.id === STATE.activeChannel ? 'is-active' : ''}" data-channel="${
          c.id
        }">${c.emoji || ''} ${escapeHTML(c.name)}</button>`,
    )
    .join('') +
    // 「加分类」放在分类这一行末尾（链接到表单页），不再占顶部导航。
    '<a class="rp-tab rp-tab--add" href="add-category.html" title="添加论文分类">➕ 加分类</a>';
  wrap.querySelectorAll('.rp-tab[data-channel]').forEach((el) => {
    el.addEventListener('click', () => {
      STATE.activeChannel = el.dataset.channel;
      buildChannelTabs();
      renderFeed();
    });
  });
}

// 搜索匹配（分两路，避免"散落命中"误报）：
//   1) 正文路：在 标题/摘要/机构/方法/平台（不含作者）里做「整串子串」或「多词全命中」。
//      处理 "zhejiang"、"diffusion policy"、"humanoid teleoperation" 这类查询。
//   2) 作者路：把查询当人名，要求命中**同一个作者**（正序或姓名调转后的拼接里
//      包含去空格的查询）。这样 "wangyue / yuewang / wang yue / yue wang" 都能精确
//      定位到 "Yue Wang"，而 "Yuran Wang"（含 wang 但不含 yue）不会被误命中。
function _textHay(p) {
  return [
    p.title, p.title_zh, p.tldr_zh, p.abstract_zh,
    p.venue,                                    // 搜会议名（rss / icra / corl…）能命中
    (p.institutions || []).join(' '),
    (p.method_tags || []).join(' '),
    (p.platform || []).join(' '),
  ].filter(Boolean).join(' ').toLowerCase();
}

function _authorMatches(name, qCompact) {
  const lower = (name || '').toLowerCase();
  const compact = lower.replace(/[^a-z0-9\u4e00-\u9fff]/g, '');
  if (!compact) return false;
  if (compact.includes(qCompact)) return true;
  // 姓/名调转：把 token 反序再拼（"Yue Wang" → "wangyue"），覆盖 family-first 写法
  const toks = lower.split(/[\s,]+/).filter(Boolean);
  if (toks.length >= 2) {
    const rev = toks.slice().reverse().join('').replace(/[^a-z0-9\u4e00-\u9fff]/g, '');
    if (rev.includes(qCompact)) return true;
  }
  return false;
}

function matchesQuery(p, q) {
  const text = _textHay(p);
  if (text.includes(q)) return true; // 整串命中（zhejiang / diffusion policy ...）
  const qCompact = q.replace(/\s+/g, '');
  const authors = p.authors_all || p.authors || [];
  if (qCompact.length >= 3 && authors.some((n) => _authorMatches(n, qCompact))) return true;
  // 多词查询：正文里每个词都出现（顺序无关），不含作者避免散落误报
  const tokens = q.split(/\s+/).filter(Boolean);
  if (tokens.length > 1 && tokens.every((t) => text.includes(t))) return true;
  return false;
}

// 会议/期刊 base 名（"ICRA 2026" → "ICRA"，"CoRL 2024 Poster" → "CoRL"），
// 去年份 + 去 track 后缀，用于下拉筛选/跳转归并同会议不同年/不同 track。
function venueBase(v) {
  return (v || '')
    .replace(/\b20\d{2}\b/g, ' ')
    .replace(/\b(poster|oral|spotlight|workshop|findings|demo|track|conference|main|datasets?(\s+and\s+benchmarks)?)\b/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function visiblePapers() {
  const q = STATE.searchQuery.trim().toLowerCase();
  const wantRepos = STATE.mode === 'repos';
  return STATE.papers.filter((p) => {
    const isGithub = (p.source || '') === 'github';
    // 顶层模式过滤：论文模式只看非 github，开源模式只看 github。
    if (wantRepos !== isGithub) return false;
    // 二级方向标签：对论文和开源项目通用（开源项目的 channel 由 AI 判定）。
    if (STATE.activeChannel !== 'all' && !(p.channels || []).includes(STATE.activeChannel)) {
      return false;
    }
    // 会议/期刊筛选只作用于论文模式；开源项目（repo 没有 venue）不套用，否则会被全过滤光。
    if (!wantRepos && STATE.activeVenue && venueBase(p.venue) !== STATE.activeVenue) {
      return false;
    }
    if (!q) return true;
    return matchesQuery(p, q);
  });
}

// 会议筛选提示条：仅当从倒计时点会议名跳来（STATE.activeVenue 有值）时显示一条
// 「🎓 正在看 <会议> 的论文 · ✕ 清除」，点 ✕ 回到全部。会议入口统一收敛到上方
// 倒计时面板，不再单独放下拉，保持 tab 行清爽。
function renderVenueBar() {
  const bar = document.querySelector('#venue-bar');
  if (!bar) return;
  // 开源项目模式不按 venue 过滤，提示条也别显示（否则文案误导）。
  if (!STATE.activeVenue || STATE.mode === 'repos') { bar.hidden = true; bar.innerHTML = ''; return; }
  const n = STATE.papers.filter((p) => venueBase(p.venue) === STATE.activeVenue).length;
  bar.innerHTML = `<span class="rp-venuebar__chip">🎓 正在看 <b>${escapeHTML(STATE.activeVenue)}</b> 收录的论文（${n}）
    <a class="rp-venuebar__clear" href="index.html" title="清除筛选">✕</a></span>`;
  bar.hidden = false;
}

function badgeHTML(badge) {
  const cls =
    badge.kind === 'hot'
      ? 'rp-badge rp-badge--hot'
      : badge.kind === 'fresh'
      ? 'rp-badge rp-badge--fresh'
      : badge.kind === 'lab'
      ? 'rp-badge rp-badge--lab'
      : badge.kind === 'pin'
      ? 'rp-badge rp-badge--pin'
      : badge.kind === 'venue'
      ? 'rp-badge rp-badge--venue'
      : 'rp-badge';
  return `<span class="${cls}">${escapeHTML(badge.label)}</span>`;
}

function fmtStars(n) {
  n = n || 0;
  if (n >= 10000) return Math.round(n / 1000) + 'k';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

// 开源项目卡：封面右上角显示 ⭐star，正文显示语言 + AI 判出的子方向，
// 点击直接跳 GitHub（新标签）。收藏页等也复用这个渲染。
export function githubCardHTML(p) {
  const cover = pickCover(p.id);
  const g = p.github || {};
  const headline = p.cover_zh || p.tldr_zh || (p.abstract_zh || '').slice(0, 60) || p.title;
  const fav = Favorites.has(p.id);
  const heart = fav ? HEART_SVG_FILL : HEART_SVG_OUTLINE;
  const stars = fmtStars(g.stars);
  const lang = g.language || '';
  const isCompanion = (p.source_tags || []).includes('paper_companion');
  const dir = (p.method_tags || [])[0] || '';
  const url = p.abs_url || `https://github.com/${p.title}`;
  const archived = g.archived ? '<span class="rp-chip rp-chip--inst">已归档</span>' : '';
  return `
    <a class="rp-card rp-card--repo" href="${url}" target="_blank" rel="noopener" data-id="${p.id}">
      <div class="rp-cover ${cover.cls}">
        <span class="rp-cover__source">GITHUB</span>
        <span class="rp-cover__stars">⭐ ${stars}</span>
        <p class="rp-cover__headline">${escapeHTML(headline)}</p>
      </div>
      <div class="rp-card__body">
        <h4 class="rp-card__title">${escapeHTML(p.title)}</h4>
        ${p.tldr_zh ? `<p class="rp-card__tldr">${escapeHTML(p.tldr_zh)}</p>` : ''}
        <div class="rp-card__chips">
          ${isCompanion ? '<span class="rp-chip rp-chip--plat">📄 论文配套</span>' : ''}
          ${lang ? `<span class="rp-chip rp-chip--sim">${escapeHTML(lang)}</span>` : ''}
          ${dir ? `<span class="rp-chip rp-chip--method">${escapeHTML(dir)}</span>` : ''}
          ${archived}
        </div>
        <div class="rp-card__meta">
          <span class="rp-card__authors">${escapeHTML(g.owner || '')}${g.pushed_at ? ` · 🕒 ${escapeHTML(g.pushed_at)}` : ''}</span>
          <button class="rp-card__gem ${Curated.has(p.id) ? 'is-on' : ''}" data-gem="${p.id}" title="标记为高质量（站长甄选）" aria-label="标记高质量">💎</button>
          <button class="rp-card__like ${fav ? 'is-liked' : ''}" data-fav="${p.id}" title="${fav ? '取消收藏' : '收藏'}" aria-label="收藏">
            ${heart}
          </button>
        </div>
      </div>
    </a>`;
}

function _domainLabel(url) {
  try {
    const h = new URL(url).hostname.replace(/^www\./, '');
    if (h.includes('science.org')) return 'SCIENCE';
    if (h.includes('nature.com')) return 'NATURE';
    if (h.includes('ieee')) return 'IEEE';
    return (h.split('.')[0] || 'LINK').toUpperCase();
  } catch (_) {
    return 'LINK';
  }
}

// 外链 pin 卡（source=external_link，如 Nature/Science 等本站抓不到 PDF 的论文）：
// 点进站内详情页（展示作者 / 摘要 / 会议 + 「阅读原文」外链），封面标 🔗 来源域名。
export function externalCardHTML(p) {
  const cover = pickCover(p.id);
  const titleZh = p.title_zh || p.title;
  const headline = p.cover_zh || p.tldr_zh || titleZh;
  const fav = Favorites.has(p.id);
  const heart = fav ? HEART_SVG_FILL : HEART_SVG_OUTLINE;
  const badges = (p.badges || []).map(badgeHTML).join('');
  const chips = chipRowsHTML(p);
  const authors = formatAuthors(p.authors || []);
  return `
    <a class="rp-card" href="${paperUrl(p.id)}" data-id="${p.id}">
      <div class="rp-cover ${cover.cls}">
        <span class="rp-cover__source">🔗 ${escapeHTML(_domainLabel(p.abs_url))}</span>
        <p class="rp-cover__headline">${escapeHTML(headline)}</p>
      </div>
      <div class="rp-card__body">
        <h4 class="rp-card__title">${escapeHTML(titleZh)}</h4>
        ${p.tldr_zh ? `<p class="rp-card__tldr">${escapeHTML(p.tldr_zh)}</p>` : ''}
        ${chips}
        ${badges ? `<div class="rp-card__badges">${badges}</div>` : ''}
        <div class="rp-card__meta">
          <span class="rp-card__authors">${escapeHTML(authors || '阅读原文')}</span>
          <button class="rp-card__gem ${Curated.has(p.id) ? 'is-on' : ''}" data-gem="${p.id}" title="标记为高质量（站长甄选）" aria-label="标记高质量">💎</button>
          <button class="rp-card__like ${fav ? 'is-liked' : ''}" data-fav="${p.id}" title="${fav ? '取消收藏' : '收藏'}" aria-label="收藏">
            ${heart}
          </button>
        </div>
      </div>
    </a>`;
}

function cardHTML(p) {
  if ((p.source || '') === 'github') return githubCardHTML(p);
  if ((p.source || '') === 'external_link') return externalCardHTML(p);
  const cover = pickCover(p.id);
  const titleZh = p.title_zh || p.title;
  const headline = p.cover_zh || p.tldr_zh || titleZh;
  const fav = Favorites.has(p.id);
  const read = Reads.has(p.id);
  const heart = fav ? HEART_SVG_FILL : HEART_SVG_OUTLINE;
  const badges = (p.badges || []).map(badgeHTML).join('');
  const source = (p.source || '').toUpperCase();
  const authors = formatAuthors(p.authors || []);

  const paletteStyle = coverStyleAttr(p.id, STATE.palettes, cover.style);

  const chips = chipRowsHTML(p);
  const videoFlag = videoBadgeHTML(p);

  return `
    <a class="rp-card ${read ? 'is-read' : ''}" href="${paperUrl(p.id)}" data-id="${p.id}">
      <div class="rp-cover ${cover.cls}"${paletteStyle ? ` style="${paletteStyle}"` : ''}>
        <span class="rp-cover__source">${escapeHTML(source)}</span>
        ${videoFlag}
        <p class="rp-cover__headline">${escapeHTML(headline)}</p>
      </div>
      <div class="rp-card__body">
        <h4 class="rp-card__title">${escapeHTML(titleZh)}</h4>
        ${p.tldr_zh ? `<p class="rp-card__tldr">${escapeHTML(p.tldr_zh)}</p>` : ''}
        ${chips}
        ${badges ? `<div class="rp-card__badges">${badges}</div>` : ''}
        <div class="rp-card__meta">
          <span class="rp-card__authors">${escapeHTML(authors)}</span>
          <button class="rp-card__gem ${Curated.has(p.id) ? 'is-on' : ''}" data-gem="${p.id}" title="标记为高质量（站长甄选）" aria-label="标记高质量">💎</button>
          <button class="rp-card__like ${fav ? 'is-liked' : ''}" data-fav="${p.id}" title="${fav ? '取消收藏' : '收藏'}" aria-label="收藏">
            ${heart}
          </button>
        </div>
      </div>
    </a>`;
}

// 二级 chip 行：四类，颜色区分。
//   inst（🏛 浅蓝边框）= 机构（公司或大学）
//   plat（🤖 紫底白字）= 机器人平台（Unitree G1 / Atlas / Figure ...）
//   sim  （🎮 青底白字）= 仿真栈（Isaac Lab / MuJoCo / Genesis ...）
//   method（红底白字） = 方法 / 问题 tag
// 数据全部来自 enrich.py 的单次 DeepSeek 抽取，前端无业务判断，缺啥不显啥。
export function chipRowsHTML(p) {
  const insts = (p.institutions || []).slice(0, 3);
  const plats = (p.platform || []).slice(0, 3);
  const sims = (p.sim_stack || []).slice(0, 2);
  const methods = (p.method_tags || []).slice(0, 3);
  if (!insts.length && !plats.length && !sims.length && !methods.length) return '';
  const instHTML = insts.map((t) => `<span class="rp-chip rp-chip--inst">🏛 ${escapeHTML(t)}</span>`).join('');
  const platHTML = plats.map((t) => `<span class="rp-chip rp-chip--plat">🤖 ${escapeHTML(t)}</span>`).join('');
  const simHTML = sims.map((t) => `<span class="rp-chip rp-chip--sim">🎮 ${escapeHTML(t)}</span>`).join('');
  const methodHTML = methods.map((t) => `<span class="rp-chip rp-chip--method">${escapeHTML(t)}</span>`).join('');
  return `<div class="rp-card__chips">${platHTML}${simHTML}${instHTML}${methodHTML}</div>`;
}

// demo 视频角标：卡片右上角，告诉用户"这篇有 demo 视频可看"。
export function videoBadgeHTML(p) {
  const vids = p.demo_videos || [];
  if (!vids.length) return '';
  return `<span class="rp-card__videoflag" title="有 demo 视频">🎬</span>`;
}

// ----- Period bucketing -------------------------------------------------
// 三段分组策略（站点已切日更）：
//   • 近 7 天          → 按「日」分组（今天 / 昨天 / 前天 / X月X日）
//   • 7–90 天          → 按「周」分组（上周 / 两周前 / N 周前）
//   • 90 天以上        → 按「年」分组（2026 年早期 / 2025 年 / ...）
// 这样高频更新阶段每日见到清晰的"今天 vs 昨天"切片，老论文不至于把
// 周块塞爆，更老的年代直接整年汇总。
function todayMidnight() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

function paperDate(p) {
  // 取「发布日」与「被收录公告日」里更晚的那个：一篇 2 月挂 arXiv、6 月被 RSS
  // 收录的论文，会以"收录日"重新冒泡到 feed 顶部（带 🎉 最新收录 徽章）。
  const cands = [p.published, p.venue_announced].filter(Boolean);
  if (!cands.length) return null;
  const iso = cands.sort().slice(-1)[0]; // 字典序最大 = 最新
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  d.setHours(0, 0, 0, 0);
  return d;
}

function _fmtMD(d) {
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

// 给一篇 paper 计算 bucket key + meta。返回 { key, meta }。
//   meta = { kind, label, emoji, dateRange, sortKey }
//   sortKey 越小越靠近今天，用于最终排序。
function periodOf(p, anchor) {
  const d = paperDate(p);
  // 无日期：手动收录类，丢到「今天」桶。
  if (!d) {
    return periodMeta('day', 0, anchor);
  }
  const days = Math.max(0, Math.floor((anchor - d) / DAY_MS));
  if (days < 7) {
    return periodMeta('day', days, anchor, d);
  }
  if (days < 90) {
    const weekIdx = Math.floor(days / 7);
    return periodMeta('week', weekIdx, anchor, d);
  }
  return periodMeta('year', d.getFullYear(), anchor, d);
}

function periodMeta(kind, n, anchor, sourceDate) {
  if (kind === 'day') {
    const day = new Date(anchor);
    day.setDate(day.getDate() - n);
    const dayLabels = ['今天', '昨天', '前天'];
    const dayEmojis = ['🔥', '✨', '🌟'];
    const label = n < 3 ? dayLabels[n] : _fmtMD(day);
    const emoji = n < 3 ? dayEmojis[n] : '📅';
    return {
      key: `day:${n}`,
      kind: 'day',
      sortKey: n,                 // 0..6
      label,
      emoji,
      dateRange: _fmtMD(day),
    };
  }
  if (kind === 'week') {
    const end = new Date(anchor);
    end.setDate(end.getDate() - n * 7);
    const start = new Date(end);
    start.setDate(start.getDate() - 6);
    const labels = { 1: '上周精选', 2: '两周前' };
    return {
      key: `week:${n}`,
      kind: 'week',
      sortKey: 100 + n,           // 101..112 — 永远排在 day 之后
      label: labels[n] || `${n} 周前`,
      emoji: n <= 2 ? '📚' : '🗂',
      dateRange: `${_fmtMD(start)} – ${_fmtMD(end)}`,
    };
  }
  // year
  const year = n;
  return {
    key: `year:${year}`,
    kind: 'year',
    sortKey: 10000 - year,        // 越早的年份 sortKey 越大 → 排在最末
    label: `${year} 年`,
    emoji: '📦',
    dateRange: `${year}-01-01 – ${year}-12-31`,
  };
}

function bucketByPeriod(papers) {
  const anchor = todayMidnight();
  const buckets = new Map();    // key → Paper[]
  const metaMap = new Map();    // key → meta
  for (const p of papers) {
    const meta = periodOf(p, anchor);
    if (!buckets.has(meta.key)) {
      buckets.set(meta.key, []);
      metaMap.set(meta.key, meta);
    }
    buckets.get(meta.key).push(p);
  }
  for (const arr of buckets.values()) {
    arr.sort((a, b) => {
      const sa = a.score || 0;
      const sb = b.score || 0;
      if (sb !== sa) return sb - sa;
      return (b.published || '').localeCompare(a.published || '');
    });
  }
  // 「今日新到」聚合 —— arxiv announce 节奏导致今天早上抓到的 papers
  // published 字段往往是昨天（cs.RO 是 EST 20:00 announce = 北京 08-09:00），
  // 严格按 published 分桶会把"今早上的 17 篇新论文"塞到「昨天」。当
  // day:0 < 5 篇时把 day:0 + day:1 合并成一个「今日新到」桶顶在最前面，
  // 用户一进主页就能看到完整的当日批次。
  const day0 = buckets.get('day:0') || [];
  const day1 = buckets.get('day:1') || [];
  if (day0.length < 5 && day1.length >= 5) {
    const merged = [...day0, ...day1];
    merged.sort((a, b) => {
      const sa = a.score || 0;
      const sb = b.score || 0;
      if (sb !== sa) return sb - sa;
      return (b.published || '').localeCompare(a.published || '');
    });
    buckets.set('day:0', merged);
    const today = new Date(anchor);
    const yesterday = new Date(anchor);
    yesterday.setDate(yesterday.getDate() - 1);
    metaMap.set('day:0', {
      key: 'day:0',
      kind: 'day',
      sortKey: 0,
      label: '今日新到',
      emoji: '🔥',
      dateRange: `${_fmtMD(yesterday)} – ${_fmtMD(today)}`,
    });
    buckets.delete('day:1');
    metaMap.delete('day:1');
  }
  // 排序：今天最前，年最后
  const order = [...buckets.keys()].sort(
    (a, b) => metaMap.get(a).sortKey - metaMap.get(b).sortKey,
  );
  return { buckets, metaMap, order, anchor };
}

// ----- Rendering --------------------------------------------------------
// 渲染一个 period bucket（可以是 day/week/year）。CSS class 仍叫 rp-week*
// 是因为只有 emoji + label 文本变了，视觉布局完全复用之前的「周精选」样式。
function appendPeriod(feed, key) {
  if (STATE.renderedKeys.has(key)) return;
  const papers = STATE.buckets.get(key) || [];
  const meta = STATE.metaMap.get(key);
  if (!papers.length || !meta) {
    STATE.renderedKeys.add(key);
    return;
  }
  STATE.renderedKeys.add(key);

  const section = document.createElement('section');
  section.className = `rp-week rp-week--${meta.kind}`;
  section.dataset.bucket = key;

  // 分隔条 chip：「🔥 今天」、「📅 5月10日」、「📚 上周精选」、「📦 2025 年」
  const divider = document.createElement('div');
  divider.className = 'rp-week__divider';
  divider.innerHTML = `
    <span class="rp-week__chip">
      <span class="rp-week__chip-emoji">${meta.emoji}</span>
      <span>${meta.label}</span>
    </span>`;
  section.appendChild(divider);

  const title = document.createElement('h2');
  title.className = 'rp-week__title';
  title.innerHTML =
    `<span class="rp-week__date">${meta.dateRange}</span>` +
    `<em>${papers.length} 篇</em>`;
  section.appendChild(title);

  const grid = document.createElement('div');
  grid.className = 'rp-feed';
  grid.innerHTML = papers.map(cardHTML).join('');
  section.appendChild(grid);

  feed.appendChild(section);
}

// 开源项目排序：⭐ star 最多 / 🕒 最近更新（pushed_at）。单个 section 一次铺满。
function sortRepos(list) {
  const arr = [...list];
  if (STATE.repoSort === 'updated') {
    arr.sort((a, b) =>
      ((b.github && b.github.pushed_at) || '').localeCompare((a.github && a.github.pushed_at) || ''),
    );
  } else {
    arr.sort((a, b) => ((b.github && b.github.stars) || 0) - ((a.github && a.github.stars) || 0));
  }
  return arr;
}

function renderOpenSource(feed, list) {
  const sorted = sortRepos(list);
  const section = document.createElement('section');
  section.className = 'rp-week rp-week--day';
  const divider = document.createElement('div');
  divider.className = 'rp-week__divider';
  divider.innerHTML =
    '<span class="rp-week__chip"><span class="rp-week__chip-emoji">🐙</span><span>优质开源项目</span></span>';
  section.appendChild(divider);
  const title = document.createElement('h2');
  title.className = 'rp-week__title';
  const starActive = STATE.repoSort !== 'updated';
  title.innerHTML =
    `<span class="rp-reposort">
       <button class="rp-reposort__btn ${starActive ? 'is-active' : ''}" data-sort="stars">⭐ Star 最多</button>
       <button class="rp-reposort__btn ${!starActive ? 'is-active' : ''}" data-sort="updated">🕒 最近更新</button>
     </span>` +
    `<em>${sorted.length} 个</em>`;
  section.appendChild(title);
  const grid = document.createElement('div');
  grid.className = 'rp-feed';
  grid.innerHTML = sorted.map(cardHTML).join('');
  section.appendChild(grid);
  feed.appendChild(section);

  section.querySelectorAll('.rp-reposort__btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const next = btn.dataset.sort;
      if (STATE.repoSort === next) return;
      STATE.repoSort = next;
      renderFeed();
    });
  });
}

function teardownObserver() {
  if (STATE.observer) {
    STATE.observer.disconnect();
    STATE.observer = null;
  }
  const old = document.querySelector('#rp-feed-sentinel');
  if (old) old.remove();
  const end = document.querySelector('#rp-feed-end');
  if (end) end.remove();
}

// 连续补分组，直到 sentinel 被推出视口下方 600px（首屏填满）或没有更多分组。
// 不依赖「sentinel 留在视口里会自动再触发」——IntersectionObserver 在元素持续
// 可见时不会重复回调，稀疏频道（首组只有 1-2 篇）会卡在 2 篇加载不出后续。
function fillViewport(feed, sentinel) {
  let guard = 0;
  while (guard++ < 80) {
    const next = STATE.periodOrder.find((k) => !STATE.renderedKeys.has(k));
    if (next === undefined) {
      teardownObserver();
      if (!document.querySelector('#rp-feed-end')) {
        const end = document.createElement('div');
        end.id = 'rp-feed-end';
        end.className = 'rp-status rp-status--end';
        end.textContent = '已经到底啦';
        feed.appendChild(end);
      }
      return;
    }
    appendPeriod(feed, next);
    feed.appendChild(sentinel); // 保持 sentinel 在最底部
    const rect = sentinel.getBoundingClientRect();
    if (rect.top > window.innerHeight + 600) return; // 视口已填满，等用户滚动
  }
}

function attachSentinel(feed) {
  teardownObserver();
  const sentinel = document.createElement('div');
  sentinel.id = 'rp-feed-sentinel';
  feed.appendChild(sentinel);

  STATE.observer = new IntersectionObserver(
    (entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      fillViewport(feed, sentinel);
    },
    { rootMargin: '600px 0px' }, // 提前 600px 触发，体感是“自动刷出”
  );
  STATE.observer.observe(sentinel);

  // 初始主动填满首屏（不等首次 intersection 回调，修稀疏频道只剩 2 篇的问题）。
  fillViewport(feed, sentinel);
}

function renderFeed() {
  const feed = document.querySelector('#feed');
  if (!feed) return;

  renderVenueBar();

  teardownObserver();
  feed.innerHTML = '';

  const list = visiblePapers();
  if (!list.length) {
    feed.innerHTML = `
      <div class="rp-status">
        <p class="rp-status__title">还没有内容</p>
        <p>等下次定时任务跑过就有啦，或者本地手动 <code>python scripts/build.py</code>。</p>
      </div>`;
    return;
  }

  // 开源项目模式：不按日期分组，整体按 GitHub Star 倒序一次性渲染。
  if (STATE.mode === 'repos') {
    renderOpenSource(feed, list);
    return;
  }

  const { buckets, metaMap, order, anchor } = bucketByPeriod(list);
  STATE.buckets = buckets;
  STATE.metaMap = metaMap;
  STATE.anchor = anchor;
  STATE.periodOrder = order;
  STATE.renderedKeys = new Set();

  // 挂 sentinel 并主动把首屏填满（见 attachSentinel/fillViewport）。
  attachSentinel(feed);
}

// ----- Wiring -----------------------------------------------------------
function wireModeSwitch() {
  const wrap = document.querySelector('#mode-switch');
  if (!wrap) return;
  wrap.querySelectorAll('.rp-modeswitch__btn').forEach((el) => {
    el.addEventListener('click', () => {
      if (STATE.mode === el.dataset.mode) return;
      STATE.mode = el.dataset.mode;
      wrap
        .querySelectorAll('.rp-modeswitch__btn')
        .forEach((b) => b.classList.toggle('is-active', b === el));
      STATE.activeChannel = 'all'; // 切模式时方向回到「全部」
      buildChannelTabs();
      renderFeed();
    });
  });
}

function wireFavDelegation() {
  const feed = document.querySelector('#feed');
  if (!feed) return;
  feed.addEventListener('click', (e) => {
    const gem = e.target.closest('[data-gem]');
    if (gem && feed.contains(gem)) {
      e.preventDefault();
      e.stopPropagation();
      const on = Curated.toggle(gem.dataset.gem);
      gem.classList.toggle('is-on', on);
      showToast(on ? `已标记高质量（共 ${Curated.count()} 篇，记得导出）` : '已取消高质量标记');
      return;
    }
    const btn = e.target.closest('[data-fav]');
    if (!btn || !feed.contains(btn)) return;
    e.preventDefault();
    e.stopPropagation();
    const id = btn.dataset.fav;
    const fav = Favorites.toggle(id);
    btn.classList.toggle('is-liked', fav);
    btn.innerHTML = fav ? HEART_SVG_FILL : HEART_SVG_OUTLINE;
    btn.title = fav ? '取消收藏' : '收藏';
    showToast(fav ? '已加入收藏夹' : '已取消收藏');
  });
}

function wireUpChrome() {
  const search = document.querySelector('#search-input');
  if (search) {
    search.addEventListener('input', (e) => {
      STATE.searchQuery = e.target.value;
      renderFeed();
    });
  }
  const themeBtn = document.querySelector('#theme-toggle');
  if (themeBtn) {
    themeBtn.addEventListener('click', () => {
      const mode = Theme.cycle();
      showToast(
        mode === 'auto' ? '跟随系统' : mode === 'dark' ? '暗色模式' : '亮色模式',
      );
    });
  }
}

async function main() {
  Theme.init();
  wireUpChrome();
  wireModeSwitch();
  wireFavDelegation();
  const params = new URLSearchParams(window.location.search);
  const initialQ = params.get('q');
  if (initialQ) {
    STATE.searchQuery = initialQ;
    const el = document.querySelector('#search-input');
    if (el) el.value = initialQ;
  }
  // 会议倒计时点会议名跳来的 ?venue=ICRA —— 进站直接按该会议筛选，然后立刻把 venue
  // 参数从地址栏抹掉：这样「刷新」会回到全部论文，不会一直卡在某个会议（用 ✕ 也能清）。
  const initialVenue = params.get('venue');
  if (initialVenue) {
    STATE.activeVenue = initialVenue;
    try {
      const u = new URL(window.location.href);
      u.searchParams.delete('venue');
      history.replaceState({}, '', u.pathname + u.search + u.hash);
    } catch (_) { /* noop */ }
  }
  await loadData();
  renderCrawlBanner();
  buildChannelTabs();
  renderFeed();
}

// feed.js 既是首页入口，又被 favorites.js / archive.js 当共享模块导入
// （借用 chipRowsHTML / githubCardHTML 等）。只有首页才有 #mode-switch，
// 据此判断；否则不跑 main()，避免在收藏/归档页覆盖它们的 #feed、重复绑定事件。
if (document.querySelector('#mode-switch')) {
  document.addEventListener('DOMContentLoaded', main);
}
