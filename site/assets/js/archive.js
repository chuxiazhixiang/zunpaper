// Archive page: pick a day, list papers from that day.

import { Theme } from './storage.js';
import {
  pickPalette,
  escapeHTML,
  formatAuthors,
  clip,
  paperUrl,
  showToast,
} from './utils.js';

async function loadDays() {
  return fetch('data/days.json').then((r) => r.json()).catch(() => ({ days: [] }));
}

async function loadDay(d) {
  return fetch(`data/daily/${d}.json`).then((r) => r.json());
}

function cardHTML(p) {
  const palette = pickPalette(p.id);
  const titleZh = p.title_zh || p.title;
  const coverText = clip(p.abstract_zh || p.abstract || '', 90);
  const source = (p.source || '').toUpperCase();
  return `
    <a class="rp-card" href="${paperUrl(p.id)}">
      <div class="rp-cover p${palette}">
        <span class="rp-cover__source">${escapeHTML(source)}</span>
        <h3 class="rp-cover__title">${escapeHTML(titleZh)}</h3>
        <p class="rp-cover__body">${escapeHTML(coverText)}</p>
      </div>
      <div class="rp-card__body">
        <h4 class="rp-card__title">${escapeHTML(titleZh)}</h4>
        ${p.tldr_zh ? `<p class="rp-card__tldr">${escapeHTML(p.tldr_zh)}</p>` : ''}
        <div class="rp-card__meta">
          <span class="rp-card__authors">${escapeHTML(formatAuthors(p.authors || []))}</span>
        </div>
      </div>
    </a>`;
}

function renderDayList(days, activeDay) {
  const wrap = document.querySelector('#day-list');
  if (!wrap) return;
  if (!days.length) {
    wrap.innerHTML = '<span class="rp-tab">还没有归档</span>';
    return;
  }
  wrap.innerHTML = days
    .map(
      (d) =>
        `<a class="rp-tab ${d === activeDay ? 'is-active' : ''}" href="archive.html?date=${encodeURIComponent(
          d,
        )}">${escapeHTML(d)}</a>`,
    )
    .join('');
}

async function main() {
  Theme.init();
  document.querySelector('#theme-toggle')?.addEventListener('click', () => {
    const mode = Theme.cycle();
    showToast(mode === 'auto' ? '跟随系统' : mode === 'dark' ? '暗色模式' : '亮色模式');
  });

  const params = new URLSearchParams(window.location.search);
  const { days } = await loadDays();
  const requested = params.get('date');
  const activeDay = requested || days[0];
  renderDayList(days, activeDay);

  const feed = document.querySelector('#feed');
  if (!activeDay) {
    feed.innerHTML = `<div class="rp-status">还没有任何归档。</div>`;
    return;
  }

  try {
    const data = await loadDay(activeDay);
    document.querySelector('#day-title').textContent = `📅 ${activeDay}  ·  ${data.count} 篇`;
    if (!data.papers?.length) {
      feed.innerHTML = `<div class="rp-status">这天没有内容。</div>`;
      return;
    }
    feed.innerHTML = data.papers.map(cardHTML).join('');
  } catch {
    feed.innerHTML = `<div class="rp-status">这天的数据加载失败。</div>`;
  }
}

document.addEventListener('DOMContentLoaded', main);
