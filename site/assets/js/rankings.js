// Rankings page: 总榜 / 周榜 / 月榜 — pure score-desc, no week bucketing.
// Each ranking row is a slim list item (medal + score + title + meta)
// so the user can scan top-N quickly.

import { Theme } from './storage.js?v=086fce10';
import {
  escapeHTML,
  formatAuthors,
  paperUrl,
  attachSearchRedirect,
  showToast,
  fetchJSON,
} from './utils.js?v=086fce10';

const DAY_MS = 86400000;

const STATE = {
  papers: [],
  window: 'all', // 'all' | 'week' | 'month'
};

async function loadData() {
  const r = await fetchJSON('data/index.json').then((r) => r.json()).catch(() => ({ papers: [] }));
  STATE.papers = r.papers || [];
}

function withinWindow(p, days) {
  if (!days) return true;
  if (!p.published) return false;
  const d = new Date(p.published);
  if (Number.isNaN(d.getTime())) return false;
  const now = Date.now();
  return now - d.getTime() <= days * DAY_MS + 12 * 3600 * 1000;
}

function papersForWindow(win) {
  let pool;
  if (win === 'week') pool = STATE.papers.filter((p) => withinWindow(p, 7));
  else if (win === 'month') pool = STATE.papers.filter((p) => withinWindow(p, 30));
  else pool = STATE.papers.slice();
  pool.sort((a, b) => {
    const sa = a.score || 0;
    const sb = b.score || 0;
    if (sb !== sa) return sb - sa;
    return (b.published || '').localeCompare(a.published || '');
  });
  return pool;
}

function medal(rank) {
  if (rank === 1) return '<span class="rp-rank__medal rp-rank__medal--gold">🥇</span>';
  if (rank === 2) return '<span class="rp-rank__medal rp-rank__medal--silver">🥈</span>';
  if (rank === 3) return '<span class="rp-rank__medal rp-rank__medal--bronze">🥉</span>';
  return `<span class="rp-rank__medal">${rank}</span>`;
}

function badgePill(b) {
  return `<span class="rp-rank__pill">${escapeHTML(b.label)}</span>`;
}

function rowHTML(p, rank) {
  const title = p.title_zh || p.title;
  const authors = formatAuthors(p.authors || []);
  const score = p.score || 0;
  // Show only the most expressive 2 badges to keep the row compact.
  const badges = (p.badges || []).slice(0, 2).map(badgePill).join('');
  const channels = (p.channels || []).slice(0, 3).map((c) => `<span class="rp-rank__chan">#${escapeHTML(c)}</span>`).join('');
  return `
    <li class="rp-rank__row">
      <a class="rp-rank__link" href="${paperUrl(p.id)}">
        <div class="rp-rank__rank">${medal(rank)}</div>
        <div class="rp-rank__score" title="评分">${score}</div>
        <div class="rp-rank__body">
          <div class="rp-rank__title">${escapeHTML(title)}</div>
          <div class="rp-rank__meta">
            <span>${escapeHTML(authors)}</span>
            <span>·</span>
            <span>${escapeHTML(p.published || '')}</span>
            ${channels}
          </div>
          ${badges ? `<div class="rp-rank__badges">${badges}</div>` : ''}
        </div>
      </a>
    </li>`;
}

function render() {
  const list = document.querySelector('#rank-list');
  if (!list) return;
  const pool = papersForWindow(STATE.window);
  if (!pool.length) {
    list.innerHTML = `<li class="rp-status">这个时段还没有论文，主页换个频道或等下次更新～</li>`;
    return;
  }
  // Cap the leaderboard at top 200 to keep DOM cheap.
  const top = pool.slice(0, 200);
  list.innerHTML = top.map((p, i) => rowHTML(p, i + 1)).join('');
}

function wireTabs() {
  const tabs = document.querySelectorAll('.rp-rank-tabs .rp-tab');
  tabs.forEach((el) => {
    el.addEventListener('click', () => {
      tabs.forEach((t) => t.classList.toggle('is-active', t === el));
      STATE.window = el.dataset.window;
      render();
    });
  });
}

function wireChrome() {
  attachSearchRedirect();
  const themeBtn = document.querySelector('#theme-toggle');
  themeBtn?.addEventListener('click', () => {
    const mode = Theme.cycle();
    showToast(
      mode === 'auto' ? '跟随系统' : mode === 'dark' ? '暗色模式' : '亮色模式',
    );
  });
}

async function main() {
  Theme.init();
  wireChrome();
  wireTabs();
  await loadData();
  render();
}

document.addEventListener('DOMContentLoaded', main);
