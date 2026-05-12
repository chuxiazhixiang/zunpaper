// Detail page: load /data/papers/{id}.json, render hero + bilingual content.

import { Favorites, Reads, Theme } from './storage.js';
import {
  escapeHTML,
  formatAuthors,
  paperUrl,
  showToast,
  attachSearchRedirect,
  pickCover,
  loadPalettes,
  coverStyleAttr,
  HEART_SVG_OUTLINE,
  HEART_SVG_FILL,
  fetchJSON,
} from './utils.js';

let _palettes = [];

function getId() {
  return new URLSearchParams(window.location.search).get('id');
}

async function loadPaper(id) {
  return fetchJSON(`data/papers/${encodeURIComponent(id)}.json`).then((r) => {
    if (!r.ok) throw new Error('not found');
    return r.json();
  });
}

async function loadIndex() {
  return fetchJSON('data/index.json').then((r) => r.json()).catch(() => ({ papers: [] }));
}

function badgeHTML(b) {
  const cls =
    b.kind === 'hot'
      ? 'rp-badge--hot'
      : b.kind === 'fresh'
      ? 'rp-badge--fresh'
      : b.kind === 'lab'
      ? 'rp-badge--lab'
      : b.kind === 'pin'
      ? 'rp-badge--pin'
      : '';
  return `<span class="rp-badge ${cls}">${escapeHTML(b.label)}</span>`;
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

/** Render the per-paper "为啥今天选了你" score breakdown.
 *  The pipeline writes `p.score` and `p.score_breakdown` (array of items
 *  `{ label, points, hint? }`). If neither is present we hide the section. */
function judgeBlockHTML(p) {
  const j = p.judge || {};
  if (!j.model) return '';
  const valueLabel = { high: '高', medium: '中', low: '低' }[j.research_value] || j.research_value || '';
  const stamp = j.relevant ? '✅ 通过' : '❌ 被砍';
  const channelLine = j.primary_channel && j.primary_channel !== 'none'
    ? `<span class="rp-judge__chip">主方向：${escapeHTML(j.primary_channel)}</span>`
    : '';
  return `
    <section class="rp-section rp-judge">
      <h3 class="rp-section__title">LLM 把关 (${escapeHTML(j.model)})</h3>
      <div class="rp-judge__meta">
        <span class="rp-judge__chip rp-judge__chip--${j.relevant ? 'pass' : 'fail'}">${stamp}</span>
        <span class="rp-judge__chip">科研价值：${escapeHTML(valueLabel)}</span>
        ${channelLine}
      </div>
      ${j.reason ? `<p class="rp-judge__reason">${escapeHTML(j.reason)}</p>` : ''}
    </section>
  `;
}


function scoreBreakdownHTML(p) {
  const items = p.score_breakdown || [];
  if (!items.length && p.score == null) return '';
  const total = p.score != null ? p.score : items.reduce((s, x) => s + (x.points || 0), 0);
  return `
    <section class="rp-section rp-score">
      <h3 class="rp-section__title">
        为啥今天选了它
        <span class="rp-score__total">总分 ${total}</span>
      </h3>
      ${items.length
        ? `<ul class="rp-score__list">
            ${items
              .map(
                (x) => `
                  <li class="rp-score__row">
                    <span class="rp-score__points ${x.points > 0 ? 'is-plus' : x.points < 0 ? 'is-minus' : ''}">${x.points > 0 ? '+' : ''}${x.points}</span>
                    <div class="rp-score__body">
                      <div class="rp-score__label">${escapeHTML(x.label || '')}</div>
                      ${x.hint ? `<div class="rp-score__hint">${escapeHTML(x.hint)}</div>` : ''}
                    </div>
                  </li>`,
              )
              .join('')}
          </ul>
          <p class="rp-score__note">分数 = 命中条目相加（满分 100）。规则在 config/scoring.yaml 里改，自己想看哪类多就把对应权重调大。</p>`
        : '<p class="rp-score__note">这篇论文还没跑过打分。下一次 daily build 会算上。</p>'}
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
  const paletteStyle = coverStyleAttr(p.id, _palettes, cover.style);

  document.title = `${titleZh} · redpaper`;

  // The post layout = visual 2-slide carousel (cover + PDF preview) + a normal
  // page-flow body section below. The body is ALWAYS in the document; the
  // carousel is just decoration on top. This is the bug-fix for the earlier
  // "below the post is gone" report — content is no longer hidden inside a
  // nested scroll container.
  // Build the slide list: cover + (page 1 image if any) + preview pages.
  // For high-quality papers preview_pages usually contains pages 2/3/4 which
  // hold the architecture / pipeline figure ("流程图").
  const pdfSlides = [];
  if (p.cover_image) {
    pdfSlides.push({ src: p.cover_image, label: '首页' });
  }
  for (let i = 0; i < (p.preview_pages || []).length; i++) {
    pdfSlides.push({ src: p.preview_pages[i], label: `第 ${i + 2} 页` });
  }

  const slidesHTML = [
    `<article class="rp-post-slide rp-post-slide--cover" data-idx="0" style="transform: translateX(0%);">
      <div class="rp-cover ${cover.cls}"${paletteStyle ? ` style="${paletteStyle}"` : ''}>
        <span class="rp-cover__source">${escapeHTML(source)}</span>
        <p class="rp-cover__headline">${escapeHTML(headline)}</p>
      </div>
      <div class="rp-post-deck__peek" aria-hidden="true"></div>
      <div class="rp-post-deck__hint">向右滑动看论文内页 →</div>
    </article>`,
  ];
  if (pdfSlides.length) {
    pdfSlides.forEach((s, i) => {
      const idx = i + 1;
      const isLast = i === pdfSlides.length - 1;
      slidesHTML.push(`
        <article class="rp-post-slide rp-post-slide--preview" data-idx="${idx}"
                 style="transform: translateX(${idx * 100}%);">
          <img class="rp-post-slide__hero" src="${escapeHTML(s.src)}" alt="论文${escapeHTML(s.label)}" loading="lazy"/>
          <span class="rp-post-deck__page-label">${escapeHTML(s.label)}</span>
          ${
            isLast
              ? `<button class="rp-post-deck__scroll-cta" id="deck-scroll-cta">↓ 滚下来看正文</button>`
              : ''
          }
        </article>`);
    });
  } else {
    slidesHTML.push(`
      <article class="rp-post-slide rp-post-slide--preview" data-idx="1" style="transform: translateX(100%);">
        <div class="rp-post-slide__noimg">
          <p>${escapeHTML(p.tldr_zh || p.abstract_zh || '').slice(0, 200)}</p>
        </div>
        <button class="rp-post-deck__scroll-cta" id="deck-scroll-cta">↓ 滚下来看正文</button>
      </article>`);
  }

  const dotCount = slidesHTML.length;
  const dotsHTML = Array.from({ length: dotCount })
    .map(
      (_, i) =>
        `<button class="rp-post-deck__dot${i === 0 ? ' is-active' : ''}" data-slide="${i}" aria-label="${
          i === 0 ? '封面' : `第 ${i} 页`
        }"></button>`,
    )
    .join('');

  const root = document.querySelector('#post-root');
  root.innerHTML = `
    <div class="rp-post-deck" data-slide-count="${dotCount}">
      <button class="rp-post-deck__nav rp-post-deck__nav--prev" id="deck-prev" aria-label="上一页">‹</button>
      <button class="rp-post-deck__nav rp-post-deck__nav--next" id="deck-next" aria-label="下一页">›</button>

      <div class="rp-post-deck__viewport" id="post-viewport">
        ${slidesHTML.join('\n')}
      </div>

      <div class="rp-post-deck__dots" id="deck-dots">${dotsHTML}</div>
    </div>

    <article class="rp-post__body" id="post-body">
      <h1 class="rp-post__title-zh">${escapeHTML(titleZh)}</h1>
      <p class="rp-post__title-en">${escapeHTML(p.title || '')}</p>
      <div class="rp-post__meta">
        ${authorsText ? `<span>${escapeHTML(authorsText)}</span>` : ''}
        ${p.published ? `<span>📅 ${escapeHTML(p.published)}</span>` : ''}
        ${p.primary_category ? `<span>🏷 ${escapeHTML(p.primary_category)}</span>` : ''}
        ${badges ? `<span>${badges}</span>` : ''}
      </div>

      <div class="rp-post__actions">
        ${p.abs_url ? `<a class="rp-btn rp-btn--primary" href="${escapeHTML(p.abs_url)}" target="_blank" rel="noopener">${
          (p.source || '').toLowerCase() === 'arxiv' ? '打开 arXiv'
            : (p.source || '').toLowerCase() === 'qbitai' ? '阅读 · 量子位'
            : (p.source || '').toLowerCase() === 'jiqizhixin' ? '阅读 · 机器之心'
            : (p.source || '').toLowerCase() === 'synced_review' ? '阅读 · 新智元'
            : '阅读原文'
        }</a>` : ''}
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

      ${judgeBlockHTML(p)}

      ${scoreBreakdownHTML(p)}

      ${relatedLinksHTML(p)}
      ${relatedPapersHTML(p, all)}
    </article>
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
  const slides = Array.from(deck.querySelectorAll('.rp-post-slide'));
  const dots = Array.from(deck.querySelectorAll('.rp-post-deck__dot'));
  const hint = deck.querySelector('.rp-post-deck__hint');
  const peek = deck.querySelector('.rp-post-deck__peek');
  const total = slides.length;
  let current = 0;

  function setSlide(idx) {
    idx = Math.max(0, Math.min(total - 1, idx));
    current = idx;
    slides.forEach((sl, i) => {
      const delta = i - idx;
      sl.style.transform = `translateX(${delta * 100}%)`;
      // Slight blur + dim on the previous slide for the "deck under cover" look
      // that the original 2-slide design had.
      if (i === idx - 1) {
        sl.style.filter = 'blur(2px) brightness(0.92)';
      } else {
        sl.style.filter = '';
      }
    });
    dots.forEach((d, i) => d.classList.toggle('is-active', i === idx));
    if (hint) hint.style.opacity = idx === 0 ? '1' : '0';
    if (peek) peek.style.opacity = idx === 0 ? '1' : '0';
  }

  dots.forEach((dot, i) => {
    dot.addEventListener('click', () => setSlide(i));
  });

  deck.querySelector('#deck-prev')?.addEventListener('click', () => setSlide(current - 1));
  deck.querySelector('#deck-next')?.addEventListener('click', () => setSlide(current + 1));

  deck.querySelector('#deck-scroll-cta')?.addEventListener('click', () => {
    const body = document.querySelector('#post-body');
    body?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // Touch swipe — left/right only.
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
    setSlide(current + (dx < 0 ? 1 : -1));
  }
  deck.addEventListener('touchstart', onStart, { passive: true });
  deck.addEventListener('touchend', onEnd, { passive: true });

  // Keyboard arrows
  if (_deckKeyHandler) {
    document.removeEventListener('keydown', _deckKeyHandler);
  }
  _deckKeyHandler = (e) => {
    if (e.target.matches('input,textarea')) return;
    if (e.key === 'ArrowRight') setSlide(current + 1);
    if (e.key === 'ArrowLeft') setSlide(current - 1);
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
  // 99% 的 not-found 都是「这个 paper 被 judge 砍了 / 数据已更新」的旧浏
  // 览器缓存遗留；剩下 1% 是用户手贴了一个完全无效的 id。两种情况都给一
  // 个会话级 sessionStorage 清理按钮 —— 点了之后下次加载会拿到最新 index。
  document.querySelector('#post-root').innerHTML = `
    <div class="rp-status">
      <p class="rp-status__title">这篇论文不在站上了</p>
      <p class="rp-status__hint">可能是被 LLM 把关砍掉了 / 站长清理了，也可能是浏览器缓存还没刷新。</p>
      <p style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:14px;">
        <a class="rp-btn" href="index.html">回到首页</a>
        <button class="rp-btn rp-btn--ghost" id="post-bust-cache">强制刷新缓存重试</button>
      </p>
    </div>`;
  document.querySelector('#post-bust-cache')?.addEventListener('click', () => {
    try { sessionStorage.removeItem('rp_data_v'); } catch (_) {}
    location.reload();
  });
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
    const [paper, index, palettes] = await Promise.all([
      loadPaper(id),
      loadIndex(),
      loadPalettes(),
    ]);
    _palettes = palettes || [];
    renderPaper(paper, index.papers || []);
  } catch (e) {
    console.error(e);
    renderNotFound();
  }
}

document.addEventListener('DOMContentLoaded', main);
