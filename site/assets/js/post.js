// Detail page: load /data/papers/{id}.json, render hero + bilingual content.

import { Favorites, Likes, Reads, Theme } from './storage.js';
import {
  escapeHTML,
  formatAuthors,
  paperUrl,
  showToast,
  HEART_SVG_OUTLINE,
  HEART_SVG_FILL,
} from './utils.js';

function getId() {
  return new URLSearchParams(window.location.search).get('id');
}

async function loadPaper(id) {
  return fetch(`data/papers/${encodeURIComponent(id)}.json`).then((r) => {
    if (!r.ok) throw new Error('not found');
    return r.json();
  });
}

async function loadIndex() {
  return fetch('data/index.json').then((r) => r.json()).catch(() => ({ papers: [] }));
}

function badgeHTML(b) {
  return `<span class="rp-badge ${b.kind === 'hot' ? 'rp-badge--hot' : b.kind === 'fresh' ? 'rp-badge--fresh' : b.kind === 'lab' ? 'rp-badge--lab' : ''}">${escapeHTML(b.label)}</span>`;
}

function bibtex(p) {
  if (!p.arxiv_id) return '';
  const cite = `arxiv_${p.arxiv_id.replace(/\W/g, '_')}`;
  const authors = (p.authors || []).map((a) => a.name).join(' and ');
  return `@misc{${cite},
  title  = {${p.title}},
  author = {${authors}},
  year   = {${(p.published || '').slice(0, 4)}},
  eprint = {${p.arxiv_id}},
  archivePrefix = {arXiv}
}`;
}

function relatedPapersHTML(current, allPapers) {
  const tags = new Set(current.channels || []);
  const candidates = allPapers
    .filter(
      (p) =>
        p.id !== current.id &&
        (p.channels || []).some((c) => tags.has(c)),
    )
    .slice(0, 4);
  if (!candidates.length) return '';
  return `
    <section class="rp-section">
      <h3 class="rp-section__title">相关论文</h3>
      <div class="rp-post__related">
        ${candidates
          .map(
            (p) => `
              <a class="rp-mini-card" href="${paperUrl(p.id)}">
                <div class="rp-mini-card__title">${escapeHTML(p.title_zh || p.title)}</div>
              </a>`,
          )
          .join('')}
      </div>
    </section>`;
}

function relatedLinksHTML(p) {
  const links = p.related_links || [];
  if (!links.length) return '';
  return `
    <section class="rp-section">
      <h3 class="rp-section__title">相关讨论 / 报道</h3>
      <div class="rp-post__related">
        ${links
          .map(
            (l) => `
              <a class="rp-mini-card" href="${escapeHTML(l.url)}" target="_blank" rel="noopener">
                <div style="font-size: 11px; color: var(--rp-text-dim); margin-bottom: 4px;">📰 ${escapeHTML(l.source_name || l.source || '')}</div>
                <div class="rp-mini-card__title">${escapeHTML(l.title || l.url)}</div>
              </a>`,
          )
          .join('')}
      </div>
    </section>`;
}

function renderPaper(p, all) {
  Reads.mark(p.id);

  const titleZh = p.title_zh || p.title;
  const heart = Favorites.has(p.id) ? HEART_SVG_FILL : HEART_SVG_OUTLINE;
  const authorsText = (p.authors || []).map((a) => a.name).join('、');
  const badges = (p.badges || []).map(badgeHTML).join('');

  document.title = `${titleZh} · redpaper`;

  const root = document.querySelector('#post-root');
  root.innerHTML = `
    ${
      p.cover_image
        ? `<div class="rp-post__hero"><img src="${escapeHTML(p.cover_image)}" alt="论文首页"/></div>`
        : ''
    }
    <h1 class="rp-post__title-zh">${escapeHTML(titleZh)}</h1>
    <p class="rp-post__title-en">${escapeHTML(p.title || '')}</p>
    <div class="rp-post__meta">
      ${authorsText ? `<span>${escapeHTML(authorsText)}</span>` : ''}
      ${p.published ? `<span>📅 ${escapeHTML(p.published)}</span>` : ''}
      ${p.primary_category ? `<span>🏷 ${escapeHTML(p.primary_category)}</span>` : ''}
      ${badges ? `<span>${badges}</span>` : ''}
    </div>

    <div class="rp-post__actions">
      ${p.abs_url ? `<a class="rp-btn rp-btn--primary" href="${escapeHTML(p.abs_url)}" target="_blank" rel="noopener">打开 arXiv</a>` : ''}
      ${p.pdf_url ? `<a class="rp-btn" href="${escapeHTML(p.pdf_url)}" target="_blank" rel="noopener">下载 PDF</a>` : ''}
      <button class="rp-btn" id="bibtex-btn">复制 BibTeX</button>
      <button class="rp-btn" id="share-btn">复制链接</button>
      <button class="rp-btn ${Favorites.has(p.id) ? 'rp-btn--primary' : ''}" id="fav-btn">${heart} 收藏</button>
    </div>

    ${
      p.tldr_zh
        ? `<section class="rp-section">
            <h3 class="rp-section__title">TL;DR</h3>
            <p class="rp-section__zh">${escapeHTML(p.tldr_zh)}</p>
          </section>`
        : ''
    }

    <section class="rp-section">
      <h3 class="rp-section__title">中文摘要</h3>
      <p class="rp-section__zh">${escapeHTML(p.abstract_zh || p.abstract || '')}</p>
      <details>
        <summary>查看英文原文</summary>
        <p class="rp-section__en">${escapeHTML(p.abstract || '')}</p>
      </details>
    </section>

    ${relatedLinksHTML(p)}
    ${relatedPapersHTML(p, all)}
  `;

  // Wire up actions
  const favBtn = document.querySelector('#fav-btn');
  favBtn?.addEventListener('click', () => {
    const fav = Favorites.toggle(p.id);
    favBtn.classList.toggle('rp-btn--primary', fav);
    favBtn.innerHTML = `${fav ? HEART_SVG_FILL : HEART_SVG_OUTLINE} 收藏`;
    showToast(fav ? '已收藏' : '取消收藏');
  });

  document.querySelector('#share-btn')?.addEventListener('click', async () => {
    const url = window.location.href;
    try {
      await navigator.clipboard.writeText(url);
      showToast('链接已复制');
    } catch {
      showToast('复制失败');
    }
  });

  // Trigger KaTeX after dynamic insert (the inline onload only handles the
  // initial body, which is empty when modules execute).
  if (typeof window.renderMathInElement === 'function') {
    try {
      window.renderMathInElement(root, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '$', right: '$', display: false },
          { left: '\\(', right: '\\)', display: false },
          { left: '\\[', right: '\\]', display: true },
        ],
        throwOnError: false,
      });
    } catch (e) {
      console.warn('KaTeX render failed', e);
    }
  }

  document.querySelector('#bibtex-btn')?.addEventListener('click', async () => {
    const txt = bibtex(p);
    if (!txt) {
      showToast('暂无 arXiv ID');
      return;
    }
    try {
      await navigator.clipboard.writeText(txt);
      showToast('BibTeX 已复制');
    } catch {
      showToast('复制失败');
    }
  });
}

function renderNotFound() {
  document.querySelector('#post-root').innerHTML = `
    <div class="rp-status">
      <p class="rp-status__title">论文不存在或还没拉取</p>
      <p><a class="rp-btn" href="index.html">回到首页</a></p>
    </div>`;
}

async function main() {
  Theme.init();
  const themeBtn = document.querySelector('#theme-toggle');
  themeBtn?.addEventListener('click', () => {
    const mode = Theme.cycle();
    showToast(mode === 'auto' ? '跟随系统' : mode === 'dark' ? '暗色模式' : '亮色模式');
  });

  const id = getId();
  if (!id) return renderNotFound();
  try {
    const [paper, index] = await Promise.all([loadPaper(id), loadIndex()]);
    renderPaper(paper, index.papers || []);
  } catch (e) {
    console.error(e);
    renderNotFound();
  }
}

document.addEventListener('DOMContentLoaded', main);
