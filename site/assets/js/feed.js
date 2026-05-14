// Homepage feed: load papers, bucket by period (day/week/year depending on
// how far back), render Xiaohongshu-style masonry one bucket at a time, and
// append older buckets via IntersectionObserver as user scrolls.
// 站点切日更后，近 7 天按日分组（今天/昨天/前天/X月X日），让用户清晰看到
// 每日产出节奏；7-90 天按周分组保留一定密度；>90 天按年分组防止上古论文
// 把页面塞爆。
// before the user hits the bottom.

import { Favorites, Reads, Theme } from './storage.js?v=b09ab243';
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
} from './utils.js?v=b09ab243';

const STATE = {
  channels: [],
  papers: [],
  palettes: [],
  activeChannel: 'all',
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
    .join('');
  wrap.querySelectorAll('.rp-tab').forEach((el) => {
    el.addEventListener('click', () => {
      STATE.activeChannel = el.dataset.channel;
      buildChannelTabs();
      renderFeed();
    });
  });
}

function visiblePapers() {
  const q = STATE.searchQuery.trim().toLowerCase();
  return STATE.papers.filter((p) => {
    if (STATE.activeChannel !== 'all' && !(p.channels || []).includes(STATE.activeChannel)) {
      return false;
    }
    if (!q) return true;
    const hay = [
      p.title,
      p.title_zh,
      p.tldr_zh,
      p.abstract_zh,
      (p.authors || []).join(' '),
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return hay.includes(q);
  });
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
      : 'rp-badge';
  return `<span class="${cls}">${escapeHTML(badge.label)}</span>`;
}

function cardHTML(p) {
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
  if (!p.published) return null;
  // Most papers carry YYYY-MM-DD; Date.parse handles that as UTC midnight,
  // which is fine for day-bucket math.
  const d = new Date(p.published);
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

function attachSentinel(feed) {
  teardownObserver();
  const sentinel = document.createElement('div');
  sentinel.id = 'rp-feed-sentinel';
  feed.appendChild(sentinel);

  STATE.observer = new IntersectionObserver(
    (entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      const next = STATE.periodOrder.find((k) => !STATE.renderedKeys.has(k));
      if (next === undefined) {
        teardownObserver();
        const end = document.createElement('div');
        end.id = 'rp-feed-end';
        end.className = 'rp-status rp-status--end';
        end.textContent = '已经到底啦';
        feed.appendChild(end);
        return;
      }
      appendPeriod(feed, next);
      // Move the sentinel back to the bottom so it can fire again.
      feed.appendChild(sentinel);
    },
    { rootMargin: '600px 0px' }, // 提前 600px 触发，体感是“自动刷出”
  );
  STATE.observer.observe(sentinel);
}

function renderFeed() {
  const feed = document.querySelector('#feed');
  if (!feed) return;

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

  const { buckets, metaMap, order, anchor } = bucketByPeriod(list);
  STATE.buckets = buckets;
  STATE.metaMap = metaMap;
  STATE.anchor = anchor;
  STATE.periodOrder = order;
  STATE.renderedKeys = new Set();

  // 渲染第一个非空 bucket（通常是「今天」），然后挂 sentinel 让用户向下滚动
  // 时自动加载更老的桶。
  const first = STATE.periodOrder.find((k) => (buckets.get(k) || []).length > 0);
  if (first === undefined) return;
  appendPeriod(feed, first);
  attachSentinel(feed);
}

// ----- Wiring -----------------------------------------------------------
function wireFavDelegation() {
  const feed = document.querySelector('#feed');
  if (!feed) return;
  feed.addEventListener('click', (e) => {
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
  wireFavDelegation();
  const params = new URLSearchParams(window.location.search);
  const initialQ = params.get('q');
  if (initialQ) {
    STATE.searchQuery = initialQ;
    const el = document.querySelector('#search-input');
    if (el) el.value = initialQ;
  }
  await loadData();
  renderCrawlBanner();
  buildChannelTabs();
  renderFeed();
}

document.addEventListener('DOMContentLoaded', main);
