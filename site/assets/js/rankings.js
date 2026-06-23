// Rankings page: 日榜 / 周榜 / 月榜 / 总榜 — pure score-desc, no week bucketing.
// Each ranking row is a slim list item (medal + score + title + meta)
// so the user can scan top-N quickly.

import { Theme } from './storage.js?v=6c6ddfea';
import {
  escapeHTML,
  formatAuthors,
  paperUrl,
  attachSearchRedirect,
  showToast,
  fetchJSON,
} from './utils.js?v=6c6ddfea';

const DAY_MS = 86400000;

const STATE = {
  papers: [],
  repos: [],
  window: 'day', // 'day' | 'week' | 'month' | 'all' | 'repos'
};

const _GH_DIR_LABEL = {
  'loco-manip-wbc': '全身控制',
  manipulation: '操作',
  teleop: '遥操作',
  locomotion: '运动控制',
  'world-model': '世界模型',
  sim2real: 'Sim2Real',
};

function fmtStars(n) {
  n = n || 0;
  if (n >= 10000) return Math.round(n / 1000) + 'k';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

async function loadData() {
  const r = await fetchJSON('data/index.json').then((r) => r.json()).catch(() => ({ papers: [] }));
  // 论文榜（日/周/月/总）排除开源仓；开源仓单独走「🐙 开源 Star 榜」。
  STATE.papers = (r.papers || []).filter((p) => (p.source || '') !== 'github');
  STATE.repos = (r.papers || []).filter((p) => (p.source || '') === 'github');
}

function withinWindow(p, days) {
  if (!days) return true;
  if (!p.published) return false;
  const d = new Date(p.published);
  if (Number.isNaN(d.getTime())) return false;
  const now = Date.now();
  return now - d.getTime() <= days * DAY_MS + 12 * 3600 * 1000;
}

function reposByStars() {
  return [...STATE.repos].sort(
    (a, b) => ((b.github && b.github.stars) || 0) - ((a.github && a.github.stars) || 0),
  );
}

function papersForWindow(win) {
  if (win === 'repos') return reposByStars();
  let pool;
  // 日榜：最新有内容那一天为锚，paper 数 < 8 时往前合并直到凑够（最多合
  // 并到 3 天）。原因：arxiv 偶尔 HTTP 429 把当天 cron 几乎打空（只剩个
  // 位数 paper），用户进来日榜空得离谱；这种 sparse 兜底让用户至少能看
  // 到最近 24-72h 的内容。
  if (win === 'day') {
    const dates = STATE.papers
      .map((p) => (p.published || '').slice(0, 10))
      .filter(Boolean);
    if (!dates.length) return [];
    const uniqDates = [...new Set(dates)].sort().reverse();  // 新→旧
    const includeDates = new Set();
    let pickedCount = 0;
    for (const d of uniqDates.slice(0, 3)) {
      includeDates.add(d);
      pickedCount += dates.filter((x) => x === d).length;
      if (pickedCount >= 8) break;
    }
    pool = STATE.papers.filter((p) =>
      includeDates.has((p.published || '').slice(0, 10)),
    );
  } else if (win === 'week') {
    pool = STATE.papers.filter((p) => withinWindow(p, 7));
  } else if (win === 'month') {
    pool = STATE.papers.filter((p) => withinWindow(p, 30));
  } else {
    pool = STATE.papers.slice();
  }
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

function repoRowHTML(p, rank) {
  const g = p.github || {};
  const stars = fmtStars(g.stars);
  const lang = g.language || '';
  const dir = (p.channels || [])[0];
  const dirLabel = dir ? _GH_DIR_LABEL[dir] || dir : '';
  const tldr = p.tldr_zh || '';
  const url = p.abs_url || `https://github.com/${p.title}`;
  return `
    <li class="rp-rank__row">
      <a class="rp-rank__link" href="${url}" target="_blank" rel="noopener">
        <div class="rp-rank__rank">${medal(rank)}</div>
        <div class="rp-rank__score rp-rank__score--star" title="GitHub Star">⭐${stars}</div>
        <div class="rp-rank__body">
          <div class="rp-rank__title">${escapeHTML(p.title)}</div>
          <div class="rp-rank__meta">
            <span>${escapeHTML(g.owner || '')}</span>
            ${lang ? `<span>·</span><span>${escapeHTML(lang)}</span>` : ''}
            ${dirLabel ? `<span class="rp-rank__chan">#${escapeHTML(dirLabel)}</span>` : ''}
          </div>
          ${tldr ? `<div class="rp-rank__tldr">${escapeHTML(tldr)}</div>` : ''}
        </div>
      </a>
    </li>`;
}

function rowHTML(p, rank) {
  if ((p.source || '') === 'github') return repoRowHTML(p, rank);
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
  const note = document.querySelector('#rank-note');
  if (note) {
    if (STATE.window === 'repos') {
      note.textContent = `按 GitHub Star 倒序 · 共 ${pool.length} 个开源项目（已由 AI 筛除课程/复现/无关项目，点击直达仓库）`;
      note.style.display = '';
    } else if (STATE.window === 'day' && pool.length) {
      const dates = [
        ...new Set(pool.map((p) => (p.published || '').slice(0, 10)).filter(Boolean)),
      ].sort().reverse();
      if (dates.length === 1) {
        note.textContent = `当日榜基于最新 published 日期：${dates[0]}（arXiv 每天 UTC 20:00 公布新批次，早上看到的可能仍是昨日批次）`;
      } else {
        note.textContent = `当日榜：${dates[dates.length - 1]} → ${dates[0]}（最新一天 paper 太少，自动合并最近 ${dates.length} 天兜底）`;
      }
      note.style.display = '';
    } else {
      note.style.display = 'none';
    }
  }
  if (!pool.length) {
    const msg = STATE.window === 'repos'
      ? '还没有收录开源项目，等下次定时任务跑过就有啦～'
      : '这个时段还没有论文，主页换个频道或等下次更新～';
    list.innerHTML = `<li class="rp-status">${msg}</li>`;
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
