// Homepage feed: load papers, render Xiaohongshu-style masonry.

import { Likes, Favorites, Reads, Theme } from './storage.js';
import {
  pickPalette,
  escapeHTML,
  formatAuthors,
  clip,
  paperUrl,
  HEART_SVG_OUTLINE,
  HEART_SVG_FILL,
  showToast,
} from './utils.js';

const STATE = {
  channels: [],
  papers: [],
  activeChannel: 'all',
  searchQuery: '',
};

async function loadData() {
  const [index, channelsResp] = await Promise.all([
    fetch('data/index.json').then((r) => r.json()).catch(() => ({ papers: [] })),
    fetch('data/channels.json').then((r) => r.json()).catch(() => ({ channels: [] })),
  ]);
  STATE.papers = index.papers || [];
  STATE.channels = channelsResp.channels || [];
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
      : 'rp-badge';
  return `<span class="${cls}">${escapeHTML(badge.label)}</span>`;
}

function cardHTML(p) {
  const palette = pickPalette(p.id);
  const titleZh = p.title_zh || p.title;
  const coverText = clip(p.abstract_zh || p.abstract || '', 90);
  const liked = Likes.has(p.id);
  const read = Reads.has(p.id);
  const heart = liked ? HEART_SVG_FILL : HEART_SVG_OUTLINE;
  const badges = (p.badges || []).map(badgeHTML).join('');
  const source = (p.source || '').toUpperCase();
  const authors = formatAuthors(p.authors || []);

  return `
    <a class="rp-card ${read ? 'is-read' : ''}" href="${paperUrl(p.id)}" data-id="${p.id}">
      <div class="rp-cover p${palette}">
        <span class="rp-cover__source">${escapeHTML(source)}</span>
        <h3 class="rp-cover__title">${escapeHTML(titleZh)}</h3>
        <p class="rp-cover__body">${escapeHTML(coverText)}</p>
      </div>
      <div class="rp-card__body">
        <h4 class="rp-card__title">${escapeHTML(titleZh)}</h4>
        ${p.tldr_zh ? `<p class="rp-card__tldr">${escapeHTML(p.tldr_zh)}</p>` : ''}
        ${badges ? `<div class="rp-card__badges">${badges}</div>` : ''}
        <div class="rp-card__meta">
          <span class="rp-card__authors">${escapeHTML(authors)}</span>
          <button class="rp-card__like ${liked ? 'is-liked' : ''}" data-like="${p.id}" aria-label="点赞">
            ${heart}
          </button>
        </div>
      </div>
    </a>`;
}

function renderFeed() {
  const feed = document.querySelector('#feed');
  if (!feed) return;
  const list = visiblePapers();
  if (!list.length) {
    feed.innerHTML = `
      <div class="rp-status">
        <p class="rp-status__title">还没有内容</p>
        <p>等明早的定时任务跑过就有啦，或者本地手动 <code>python scripts/build.py</code>。</p>
      </div>`;
    return;
  }
  feed.innerHTML = list.map(cardHTML).join('');
  // Wire up like buttons (inside the card link, so we need to stopPropagation)
  feed.querySelectorAll('[data-like]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const id = btn.dataset.like;
      const liked = Likes.toggle(id);
      btn.classList.toggle('is-liked', liked);
      btn.innerHTML = liked ? HEART_SVG_FILL : HEART_SVG_OUTLINE;
      showToast(liked ? '已点赞' : '取消点赞');
    });
  });
}

function wireUpChrome() {
  // Search
  const search = document.querySelector('#search-input');
  if (search) {
    search.addEventListener('input', (e) => {
      STATE.searchQuery = e.target.value;
      renderFeed();
    });
  }
  // Theme toggle
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
  await loadData();
  buildChannelTabs();
  renderFeed();
}

document.addEventListener('DOMContentLoaded', main);
