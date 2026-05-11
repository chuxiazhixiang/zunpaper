// Detail page: load /data/papers/{id}.json, render hero + bilingual content.

import { Favorites, Reads, Theme } from './storage.js';
import {
  escapeHTML,
  formatAuthors,
  paperUrl,
  showToast,
  attachSearchRedirect,
  pickCover,
  pickStickers,
  stickersHTML,
  loadStickerManifest,
  HEART_SVG_OUTLINE,
  HEART_SVG_FILL,
} from './utils.js';

let _stickers = [];

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
  const cover = pickCover(p.id);
  const headline = p.cover_zh || p.tldr_zh || titleZh;
  const source = (p.source || '').toUpperCase();

  document.title = `${titleZh} · redpaper`;

  const root = document.querySelector('#post-root');
  root.innerHTML = `
    <div class="rp-post-deck">
      <button class="rp-post-deck__nav rp-post-deck__nav--prev" id="deck-prev" aria-label="上一页">‹</button>
      <button class="rp-post-deck__nav rp-post-deck__nav--next" id="deck-next" aria-label="下一页">›</button>

      <div class="rp-post-deck__viewport" id="post-viewport">
        <article class="rp-post-slide rp-post-slide--cover">
          <div class="rp-cover ${cover.cls}">
            <span class="rp-cover__source">${escapeHTML(source)}</span>
            <p class="rp-cover__headline">${escapeHTML(headline)}</p>
            ${stickersHTML(pickStickers(p.id, _stickers, 2))}
          </div>
          <div class="rp-post-deck__peek" aria-hidden="true"></div>
          <div class="rp-post-deck__hint">向右滑动看正文 →</div>
        </article>

        <article class="rp-post-slide rp-post-slide--body">
          <div class="rp-post-slide__inner">
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
              <button class="rp-btn ${Favorites.has(p.id) ? 'rp-btn--primary' : ''}" id="fav-btn">${heart} <span id="fav-label">${Favorites.has(p.id) ? '已收藏' : '收藏'}</span></button>
              <button class="rp-btn" id="fav-cat-btn" title="分类管理">📁 分类</button>
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
          </div>
        </article>
      </div>

      <div class="rp-post-deck__dots" id="deck-dots">
        <button class="rp-post-deck__dot is-active" data-slide="0" aria-label="封面"></button>
        <button class="rp-post-deck__dot" data-slide="1" aria-label="正文"></button>
      </div>
    </div>
  `;

  setupDeck();

  // Wire up actions
  const favBtn = document.querySelector('#fav-btn');
  function refreshFavBtn() {
    const on = Favorites.has(p.id);
    favBtn.classList.toggle('rp-btn--primary', on);
    favBtn.innerHTML = `${on ? HEART_SVG_FILL : HEART_SVG_OUTLINE} <span id="fav-label">${on ? '已收藏' : '收藏'}</span>`;
  }
  favBtn?.addEventListener('click', () => {
    const fav = Favorites.toggle(p.id);
    refreshFavBtn();
    showToast(fav ? '已加入「默认」' : '已取消收藏');
  });

  document.querySelector('#fav-cat-btn')?.addEventListener('click', () => {
    openCategoryPicker(p.id, refreshFavBtn);
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

let _deckKeyHandler = null;

function setupDeck() {
  const deck = document.querySelector('.rp-post-deck');
  if (!deck) return;
  const dots = deck.querySelectorAll('.rp-post-deck__dot');
  const hint = deck.querySelector('.rp-post-deck__hint');
  const peek = deck.querySelector('.rp-post-deck__peek');

  function setSlide(idx) {
    const onBody = idx === 1;
    deck.classList.toggle('is-on-body', onBody);
    dots.forEach((d, i) => d.classList.toggle('is-active', i === idx));
    if (hint) hint.style.opacity = onBody ? '0' : '1';
    if (peek) peek.style.opacity = onBody ? '0' : '1';
    // When entering the body slide for the first time, scroll it to top so
    // the user lands on the title/PDF hero instead of wherever they left off.
    if (onBody) {
      const body = deck.querySelector('.rp-post-slide--body');
      if (body && !body.dataset.touched) {
        body.scrollTop = 0;
        body.dataset.touched = '1';
      }
    }
  }

  dots.forEach((dot, i) => {
    dot.addEventListener('click', () => setSlide(i));
  });

  deck.querySelector('#deck-prev')?.addEventListener('click', () => setSlide(0));
  deck.querySelector('#deck-next')?.addEventListener('click', () => setSlide(1));

  // Pointer / touch swipe (horizontal). Only triggers on the cover slide
  // so we don't fight body's vertical scroll.
  let startX = 0;
  let startY = 0;
  let tracking = false;
  function onStart(e) {
    const t = e.touches ? e.touches[0] : e;
    startX = t.clientX;
    startY = t.clientY;
    tracking = true;
  }
  function onEnd(e) {
    if (!tracking) return;
    const t = e.changedTouches ? e.changedTouches[0] : e;
    const dx = t.clientX - startX;
    const dy = t.clientY - startY;
    tracking = false;
    if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy)) return;
    if (dx < 0) setSlide(1);
    else setSlide(0);
  }
  deck.addEventListener('touchstart', onStart, { passive: true });
  deck.addEventListener('touchend', onEnd, { passive: true });

  // Keyboard arrows
  if (_deckKeyHandler) {
    document.removeEventListener('keydown', _deckKeyHandler);
  }
  _deckKeyHandler = (e) => {
    if (e.target.matches('input,textarea')) return;
    if (e.key === 'ArrowRight') setSlide(1);
    if (e.key === 'ArrowLeft') setSlide(0);
  };
  document.addEventListener('keydown', _deckKeyHandler);
}

function openCategoryPicker(paperId, onChange) {
  const existing = document.querySelector('.rp-modal');
  existing?.remove();

  const cats = Favorites.categories();
  const checked = new Set(Favorites.categoriesOf(paperId));

  const wrap = document.createElement('div');
  wrap.className = 'rp-modal';
  wrap.innerHTML = `
    <div class="rp-modal__sheet">
      <div class="rp-modal__header">
        <h3>分类管理</h3>
        <button class="rp-icon-btn" data-close>×</button>
      </div>
      <p class="rp-modal__hint">勾选这篇论文要进入哪些分类。分类都存在你的浏览器里。</p>
      <div class="rp-modal__list">
        ${cats
          .map(
            (c) => `
              <label class="rp-modal__row">
                <input type="checkbox" data-cat="${escapeHTML(c)}" ${checked.has(c) ? 'checked' : ''}/>
                <span>${escapeHTML(c)}</span>
              </label>`,
          )
          .join('')}
      </div>
      <div class="rp-modal__create">
        <input id="new-cat" type="text" placeholder="新建分类，例如「必读」" maxlength="20" />
        <button class="rp-btn" id="new-cat-btn">添加</button>
      </div>
    </div>
  `;
  document.body.appendChild(wrap);

  function close() {
    wrap.remove();
  }
  wrap.addEventListener('click', (e) => {
    if (e.target === wrap || e.target.matches('[data-close]')) close();
  });

  function commit() {
    const picked = [...wrap.querySelectorAll('input[type="checkbox"][data-cat]')]
      .filter((el) => el.checked)
      .map((el) => el.dataset.cat);
    Favorites.setCategoriesOf(paperId, picked);
    onChange?.();
  }
  wrap.querySelectorAll('input[type="checkbox"][data-cat]').forEach((el) => {
    el.addEventListener('change', commit);
  });

  wrap.querySelector('#new-cat-btn').addEventListener('click', () => {
    const input = wrap.querySelector('#new-cat');
    const name = input.value.trim();
    if (!name) return;
    if (Favorites.addCategory(name)) {
      Favorites.setCategoriesOf(paperId, [...Favorites.categoriesOf(paperId), name]);
      onChange?.();
      // Re-render the modal so the new category shows up and is checked.
      close();
      openCategoryPicker(paperId, onChange);
    } else {
      showToast('分类已存在或名字为空');
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
  attachSearchRedirect();
  const themeBtn = document.querySelector('#theme-toggle');
  themeBtn?.addEventListener('click', () => {
    const mode = Theme.cycle();
    showToast(mode === 'auto' ? '跟随系统' : mode === 'dark' ? '暗色模式' : '亮色模式');
  });

  const id = getId();
  if (!id) return renderNotFound();
  try {
    const [paper, index, stickers] = await Promise.all([
      loadPaper(id),
      loadIndex(),
      loadStickerManifest(),
    ]);
    _stickers = stickers || [];
    renderPaper(paper, index.papers || []);
  } catch (e) {
    console.error(e);
    renderNotFound();
  }
}

document.addEventListener('DOMContentLoaded', main);
