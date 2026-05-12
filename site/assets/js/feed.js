// Homepage feed: load papers, bucket by 7-day window, render Xiaohongshu-style
// masonry one "week" at a time, and append older weeks via IntersectionObserver
// before the user hits the bottom.

import { Favorites, Reads, Theme } from './storage.js?v=a7301820';
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
} from './utils.js?v=a7301820';

const STATE = {
  channels: [],
  papers: [],
  palettes: [],
  activeChannel: 'all',
  searchQuery: '',
  // Filled per render: Map<weekIdx, Paper[]> + sorted list of week indices.
  buckets: new Map(),
  weekOrder: [],
  renderedWeeks: new Set(),
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

  return `
    <a class="rp-card ${read ? 'is-read' : ''}" href="${paperUrl(p.id)}" data-id="${p.id}">
      <div class="rp-cover ${cover.cls}"${paletteStyle ? ` style="${paletteStyle}"` : ''}>
        <span class="rp-cover__source">${escapeHTML(source)}</span>
        <p class="rp-cover__headline">${escapeHTML(headline)}</p>
      </div>
      <div class="rp-card__body">
        <h4 class="rp-card__title">${escapeHTML(titleZh)}</h4>
        ${p.tldr_zh ? `<p class="rp-card__tldr">${escapeHTML(p.tldr_zh)}</p>` : ''}
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

// ----- Week bucketing ---------------------------------------------------
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

function weekIndexOf(p, anchor) {
  const d = paperDate(p);
  if (!d) return 0; // 没日期的（手动收录）丢到本周
  const days = Math.max(0, Math.floor((anchor - d) / DAY_MS));
  return Math.floor(days / 7);
}

function weekDateRange(idx, anchor) {
  // 第 idx 周 = 距今 idx*7 天 到 (idx+1)*7 - 1 天。
  const end = new Date(anchor);
  end.setDate(end.getDate() - idx * 7);
  const start = new Date(end);
  start.setDate(start.getDate() - 6);
  const fmt = (d) => `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${fmt(start)} – ${fmt(end)}`;
}

function weekLabel(idx) {
  if (idx === 0) return '本周精选';
  if (idx === 1) return '上周精选';
  if (idx === 2) return '两周前';
  return `${idx} 周前`;
}

function weekEmoji(idx) {
  if (idx === 0) return '🌟';
  if (idx === 1) return '📅';
  if (idx === 2) return '📚';
  return '🗂';
}

function bucketByWeek(papers) {
  const anchor = todayMidnight();
  const buckets = new Map();
  for (const p of papers) {
    const w = weekIndexOf(p, anchor);
    if (!buckets.has(w)) buckets.set(w, []);
    buckets.get(w).push(p);
  }
  for (const arr of buckets.values()) {
    arr.sort((a, b) => {
      const sa = a.score || 0;
      const sb = b.score || 0;
      if (sb !== sa) return sb - sa;
      return (b.published || '').localeCompare(a.published || '');
    });
  }
  return { buckets, anchor };
}

// ----- Rendering --------------------------------------------------------
function appendWeek(feed, weekIdx) {
  if (STATE.renderedWeeks.has(weekIdx)) return;
  const papers = STATE.buckets.get(weekIdx) || [];
  if (!papers.length) {
    STATE.renderedWeeks.add(weekIdx);
    return;
  }
  STATE.renderedWeeks.add(weekIdx);

  const section = document.createElement('section');
  section.className = 'rp-week';
  section.dataset.week = String(weekIdx);

  // 周间分隔条：跨周时（除第一个周块以外）打一条带药丸标签的虚线。
  // 第一周也加一个 chip，但不带虚线 — 通过 :first-of-type 隐藏 ::before/::after。
  const divider = document.createElement('div');
  divider.className = 'rp-week__divider';
  divider.innerHTML = `
    <span class="rp-week__chip">
      <span class="rp-week__chip-emoji">${weekEmoji(weekIdx)}</span>
      <span>${weekLabel(weekIdx)}</span>
    </span>`;
  section.appendChild(divider);

  const title = document.createElement('h2');
  title.className = 'rp-week__title';
  title.innerHTML =
    `<span class="rp-week__date">${weekDateRange(weekIdx, STATE.anchor)}</span>` +
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
      const next = STATE.weekOrder.find((w) => !STATE.renderedWeeks.has(w));
      if (next === undefined) {
        teardownObserver();
        const end = document.createElement('div');
        end.id = 'rp-feed-end';
        end.className = 'rp-status rp-status--end';
        end.textContent = '已经到底啦';
        feed.appendChild(end);
        return;
      }
      appendWeek(feed, next);
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

  const { buckets, anchor } = bucketByWeek(list);
  STATE.buckets = buckets;
  STATE.anchor = anchor;
  STATE.weekOrder = [...buckets.keys()].sort((a, b) => a - b);
  STATE.renderedWeeks = new Set();

  // 渲染第一个非空周，然后挂 sentinel；如果本周空就直接 fallback 到下一周。
  const first = STATE.weekOrder.find((w) => (buckets.get(w) || []).length > 0);
  if (first === undefined) return;
  appendWeek(feed, first);
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
