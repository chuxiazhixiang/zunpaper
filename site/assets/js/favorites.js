// Favorites page: filter the global index to favorited papers.

import { Favorites, Likes, Reads, Theme } from './storage.js';
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

async function loadIndex() {
  return fetch('data/index.json').then((r) => r.json()).catch(() => ({ papers: [] }));
}

function cardHTML(p) {
  const palette = pickPalette(p.id);
  const titleZh = p.title_zh || p.title;
  const coverText = clip(p.abstract_zh || p.abstract || '', 90);
  const liked = Likes.has(p.id);
  const source = (p.source || '').toUpperCase();
  return `
    <a class="rp-card" href="${paperUrl(p.id)}" data-id="${p.id}">
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
          <button class="rp-card__like ${liked ? 'is-liked' : ''}" data-like="${p.id}">
            ${liked ? HEART_SVG_FILL : HEART_SVG_OUTLINE}
          </button>
        </div>
      </div>
    </a>`;
}

async function main() {
  Theme.init();
  document.querySelector('#theme-toggle')?.addEventListener('click', () => {
    const mode = Theme.cycle();
    showToast(mode === 'auto' ? '跟随系统' : mode === 'dark' ? '暗色模式' : '亮色模式');
  });

  const index = await loadIndex();
  const favs = new Set(Favorites.all());
  const list = (index.papers || []).filter((p) => favs.has(p.id));

  const feed = document.querySelector('#feed');
  document.querySelector('#count').textContent = list.length;
  if (!list.length) {
    feed.innerHTML = `
      <div class="rp-status">
        <p class="rp-status__title">收藏夹空空如也</p>
        <p>在论文详情页点「收藏」就会出现在这里。</p>
      </div>`;
    return;
  }
  feed.innerHTML = list.map(cardHTML).join('');
  feed.querySelectorAll('[data-like]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const id = btn.dataset.like;
      const liked = Likes.toggle(id);
      btn.classList.toggle('is-liked', liked);
      btn.innerHTML = liked ? HEART_SVG_FILL : HEART_SVG_OUTLINE;
    });
  });
}

document.addEventListener('DOMContentLoaded', main);
