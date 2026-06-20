// Detail page: load /data/papers/{id}.json, render hero + bilingual content.

import { Favorites, Reads, Theme } from './storage.js?v=fd711e11';
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
} from './utils.js?v=fd711e11';

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
        (p.source || '') !== 'github' && // 开源仓不进「相关论文」（链接走 post 而非仓库）
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
function postChipsHTML(p) {
  // Detail 页的 chip 行：与 feed card 同款样式，4 类 chip 都吃：
  //   plat（🤖）/ sim（🎮）/ inst（🏛）/ method
  const insts = (p.institutions || []).slice(0, 3);
  const plats = (p.platform || []).slice(0, 3);
  const sims = (p.sim_stack || []).slice(0, 2);
  const methods = (p.method_tags || []).slice(0, 3);
  if (!insts.length && !plats.length && !sims.length && !methods.length) return '';
  const platHTML = plats.map((t) => `<span class="rp-chip rp-chip--plat">🤖 ${escapeHTML(t)}</span>`).join('');
  const simHTML = sims.map((t) => `<span class="rp-chip rp-chip--sim">🎮 ${escapeHTML(t)}</span>`).join('');
  const instHTML = insts.map((t) => `<span class="rp-chip rp-chip--inst">🏛 ${escapeHTML(t)}</span>`).join('');
  const methodHTML = methods.map((t) => `<span class="rp-chip rp-chip--method">${escapeHTML(t)}</span>`).join('');
  return `<div class="rp-post__chips">${platHTML}${simHTML}${instHTML}${methodHTML}</div>`;
}

// P1: 结构化"事实卡片"——把方法家族 / 是否真机 / 训练规模做成一行三栏 KV
// 表格，让用户在最显眼的位置就能 1 秒判断这篇是不是要细读。
function postStructuredHTML(p) {
  const items = [];
  // method_family / training_summary 已弃用（常含糊无用）：不再展示。
  if (p.real_robot === 'yes') items.push(['🤝 真机实验', '是']);
  else if (p.real_robot === 'no') items.push(['🤝 真机实验', '否 (sim only)']);
  if (!items.length) return '';
  const cells = items.map(([k, v]) =>
    `<div class="rp-factbox__cell"><div class="rp-factbox__k">${k}</div><div class="rp-factbox__v">${v}</div></div>`
  ).join('');
  return `<div class="rp-factbox">${cells}</div>`;
}

// P0: demo 视频嵌入区。
// YouTube 直接 iframe 嵌入会触发两个常见拦截：
//   - 错误 153：iframe 没带 Referer / 视频只允许 nocookie 嵌入
//   - 「请登录，以便我们确认你不是机器人」：iframe 一加载就被 YT 反 bot 命中
// 解法：**懒加载 facade**——平时只渲染 YouTube 官方缩略图 + 播放按钮，等
// 用户真的点了再 swap 成 iframe（autoplay=1）。YT 把"用户主动点击"判定为
// 真人，几乎不会再弹 challenge。Bilibili / mp4 没有这个问题，照旧嵌入。
function ytVideoId(v) {
  return (
    /youtube\.com\/embed\/([A-Za-z0-9_-]{11})/.exec(v.embed_url || '')?.[1] ||
    /[?&]v=([A-Za-z0-9_-]{11})/.exec(v.url || '')?.[1] ||
    /youtu\.be\/([A-Za-z0-9_-]{11})/.exec(v.url || '')?.[1] ||
    /shorts\/([A-Za-z0-9_-]{11})/.exec(v.url || '')?.[1] ||
    ''
  );
}

// YouTube 反 bot 是按 IP / 浏览器指纹判定的：哪怕用户手动点了"播放"，只要
// YT 把当前网络判定为高风险，iframe 一加载就会弹「请登录确认不是机器人」。
// 客户端没办法绕过这个判断。最稳的 UX 是直接把 facade 做成 <a>，点了就在
// 新标签页跳 YouTube。本机能正常访问 YT 的用户体验损失很小；网络受限的
// 用户也不再被卡在永远转圈的嵌入框里。
function ytFacadeHTML(v) {
  const id = ytVideoId(v);
  const title = escapeHTML(v.title || 'Demo 视频');
  if (!id) {
    return `<p><a href="${escapeHTML(v.url)}" target="_blank" rel="noopener">${title} ↗</a></p>`;
  }
  // hqdefault 几乎所有 YT 视频都有；maxres 有时 404 → CSS 多层 background 顺序回退。
  const thumb = `https://i.ytimg.com/vi/${id}/hqdefault.jpg`;
  const thumbHD = `https://i.ytimg.com/vi/${id}/maxresdefault.jpg`;
  const watchURL = `https://www.youtube.com/watch?v=${id}`;
  return `<figure class="rp-video">
    <a class="rp-video__facade" href="${watchURL}" target="_blank" rel="noopener"
       style="background-image:url('${thumbHD}'),url('${thumb}')"
       aria-label="在 YouTube 上播放：${title}">
      <span class="rp-video__play" aria-hidden="true">▶</span>
      <span class="rp-video__brand" aria-hidden="true">YouTube</span>
    </a>
    <figcaption>${title} · <a class="rp-video__cap-link" href="${watchURL}" target="_blank" rel="noopener">在 YouTube 打开 ↗</a></figcaption>
  </figure>`;
}

function videoBlockHTML(v) {
  const title = escapeHTML(v.title || 'Demo 视频');
  if (v.kind === 'youtube') {
    return ytFacadeHTML(v);
  }
  if (v.kind === 'bilibili') {
    return `<figure class="rp-video">
      <iframe src="${escapeHTML(v.embed_url || v.url)}"
              scrolling="no"
              border="0"
              frameborder="no"
              framespacing="0"
              allowfullscreen
              loading="lazy"></iframe>
      <figcaption>${title} · <a href="${escapeHTML(v.url)}" target="_blank" rel="noopener">在 B 站打开 ↗</a></figcaption>
    </figure>`;
  }
  if (v.kind === 'mp4') {
    return `<figure class="rp-video">
      <video controls preload="metadata" src="${escapeHTML(v.url)}"></video>
      <figcaption>${title}</figcaption>
    </figure>`;
  }
  return `<p><a href="${escapeHTML(v.url)}" target="_blank" rel="noopener">${title} ↗</a></p>`;
}

function postVideosHTML(p) {
  const vids = p.demo_videos || [];
  if (!vids.length) return '';
  const blocks = vids.slice(0, 3).map(videoBlockHTML).join('');
  return `<section class="rp-post__videos"><h3>🎬 Demo 视频</h3>${blocks}</section>`;
}


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

      ${postChipsHTML(p)}
      ${postStructuredHTML(p)}
      ${postVideosHTML(p)}

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
  setupLightbox();

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

// P0+: 详情页图片放大预览（轻量 lightbox），支持缩放和平移。
//   - 点 PDF 预览页大图 → 弹出全屏蒙层 + 原图最大 95vw/95vh
//   - 滚轮：以鼠标位置为锚点缩放（0.8x ~ 6x）
//   - 双击：在 1x / 2.5x 间切换，锚点是鼠标位置
//   - 缩放 > 1 时鼠标拖拽 / 触屏单指拖动 → 平移
//   - 触屏双指捏合 → 缩放
//   - +/-/0 键盘缩放；ESC 关；←/→ 翻页
//   - 切换图片时缩放/位移自动重置
//   - 工具栏右下：放大 / 缩小 / 重置
//   - 不引入任何依赖。
function setupLightbox() {
  const targets = Array.from(document.querySelectorAll('.rp-post-slide__hero'));
  if (!targets.length) return;
  const srcs = targets.map((img) => img.getAttribute('src')).filter(Boolean);
  if (!srcs.length) return;

  // 单例蒙层
  let lb = document.querySelector('.rp-lightbox');
  if (!lb) {
    lb = document.createElement('div');
    lb.className = 'rp-lightbox';
    lb.innerHTML = `
      <button class="rp-lightbox__close" aria-label="关闭">×</button>
      <button class="rp-lightbox__nav rp-lightbox__nav--prev" aria-label="上一张">‹</button>
      <button class="rp-lightbox__nav rp-lightbox__nav--next" aria-label="下一张">›</button>
      <figure class="rp-lightbox__stage">
        <img class="rp-lightbox__img" alt="" draggable="false" />
        <figcaption class="rp-lightbox__caption"></figcaption>
      </figure>
      <div class="rp-lightbox__tools" aria-label="缩放工具">
        <button class="rp-lightbox__tool" data-act="zoom-out" aria-label="缩小">−</button>
        <button class="rp-lightbox__tool" data-act="reset" aria-label="重置">⤾</button>
        <button class="rp-lightbox__tool" data-act="zoom-in" aria-label="放大">+</button>
        <span class="rp-lightbox__zoom">100%</span>
      </div>
      <div class="rp-lightbox__dots"></div>
    `;
    document.body.appendChild(lb);
  }
  const imgEl = lb.querySelector('.rp-lightbox__img');
  const capEl = lb.querySelector('.rp-lightbox__caption');
  const dotsEl = lb.querySelector('.rp-lightbox__dots');
  const zoomLabel = lb.querySelector('.rp-lightbox__zoom');
  let idx = 0;

  // 变换状态。scale 是 CSS transform 的 scale，tx/ty 是平移（像素）。
  const MIN_SCALE = 0.8;
  const MAX_SCALE = 6;
  let scale = 1;
  let tx = 0;
  let ty = 0;
  function applyTransform() {
    imgEl.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    zoomLabel.textContent = `${Math.round(scale * 100)}%`;
    lb.classList.toggle('is-zoomed', scale > 1.01);
  }
  function resetTransform() {
    scale = 1; tx = 0; ty = 0;
    applyTransform();
  }
  // 以容器内某一点为锚点缩放：保持该点在屏幕坐标系下不变。
  // (cx, cy) 是蒙层（视口）坐标，imgRect 是 img 当前 boundingRect。
  function zoomAt(targetScale, cx, cy) {
    targetScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, targetScale));
    if (Math.abs(targetScale - scale) < 1e-3) return;
    const rect = imgEl.getBoundingClientRect();
    // 图像中心
    const ix = rect.left + rect.width / 2;
    const iy = rect.top + rect.height / 2;
    // 锚点相对图像中心的偏移（屏幕像素，已经包含当前 scale）
    const dx = cx - ix;
    const dy = cy - iy;
    // 缩放因子
    const k = targetScale / scale;
    // 新平移：旧 tx + 偏移 - 偏移*k = 旧 tx + dx*(1-k)
    tx += dx * (1 - k);
    ty += dy * (1 - k);
    scale = targetScale;
    applyTransform();
  }

  function go(i) {
    idx = (i + srcs.length) % srcs.length;
    imgEl.src = srcs[idx];
    capEl.textContent = targets[idx]?.getAttribute('alt') || `第 ${idx + 1} 张`;
    dotsEl.querySelectorAll('span').forEach((d, k) => {
      d.classList.toggle('is-active', k === idx);
    });
    lb.classList.toggle('is-single', srcs.length <= 1);
    resetTransform();
  }
  function open(i) {
    go(i);
    lb.classList.add('is-open');
    document.body.style.overflow = 'hidden';
  }
  function close() {
    lb.classList.remove('is-open');
    document.body.style.overflow = '';
  }

  dotsEl.innerHTML = srcs.map(() => '<span></span>').join('');

  targets.forEach((img, i) => {
    img.style.cursor = 'zoom-in';
    img.addEventListener('click', (e) => {
      e.preventDefault();
      open(i);
    });
  });

  // 顶层控件
  lb.querySelector('.rp-lightbox__close').addEventListener('click', close);
  lb.querySelector('.rp-lightbox__nav--prev').addEventListener('click', (e) => {
    e.stopPropagation();
    go(idx - 1);
  });
  lb.querySelector('.rp-lightbox__nav--next').addEventListener('click', (e) => {
    e.stopPropagation();
    go(idx + 1);
  });
  // 工具栏：放大/缩小/重置
  lb.querySelector('.rp-lightbox__tools').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    e.stopPropagation();
    const act = btn.getAttribute('data-act');
    const r = lb.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    if (act === 'zoom-in') zoomAt(scale * 1.4, cx, cy);
    else if (act === 'zoom-out') zoomAt(scale / 1.4, cx, cy);
    else if (act === 'reset') resetTransform();
  });

  // 滚轮缩放（以鼠标位置为锚点）
  lb.addEventListener(
    'wheel',
    (e) => {
      if (!lb.classList.contains('is-open')) return;
      e.preventDefault();
      // deltaY 越大缩越快；trackpad pinch 在多数浏览器里也走 wheel + ctrlKey
      const intensity = e.ctrlKey ? 0.02 : 0.0015;
      const factor = Math.exp(-e.deltaY * intensity);
      zoomAt(scale * factor, e.clientX, e.clientY);
    },
    { passive: false },
  );

  // 双击切 1x / 2.5x
  imgEl.addEventListener('dblclick', (e) => {
    e.stopPropagation();
    if (scale > 1.01) resetTransform();
    else zoomAt(2.5, e.clientX, e.clientY);
  });

  // 鼠标拖拽平移
  let dragging = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let dragStartTx = 0;
  let dragStartTy = 0;
  imgEl.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    dragging = true;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    dragStartTx = tx;
    dragStartTy = ty;
    lb.classList.add('is-dragging');
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    tx = dragStartTx + (e.clientX - dragStartX);
    ty = dragStartTy + (e.clientY - dragStartY);
    applyTransform();
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    lb.classList.remove('is-dragging');
  });

  // 触屏：单指拖拽 + 双指捏合缩放。
  // 用 pointermove 也行，但 touch 事件下能直接拿到 touches 列表，捏合实现最简单。
  let touchMode = null; // 'pan' | 'pinch' | null
  let pinchStartDist = 0;
  let pinchStartScale = 1;
  let pinchCenter = { x: 0, y: 0 };
  let panStartX = 0;
  let panStartY = 0;
  let panStartTx = 0;
  let panStartTy = 0;
  imgEl.addEventListener(
    'touchstart',
    (e) => {
      if (e.touches.length === 1) {
        touchMode = 'pan';
        panStartX = e.touches[0].clientX;
        panStartY = e.touches[0].clientY;
        panStartTx = tx;
        panStartTy = ty;
      } else if (e.touches.length === 2) {
        touchMode = 'pinch';
        const [a, b] = e.touches;
        pinchStartDist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
        pinchStartScale = scale;
        pinchCenter = { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 };
      }
    },
    { passive: true },
  );
  imgEl.addEventListener(
    'touchmove',
    (e) => {
      if (touchMode === 'pan' && e.touches.length === 1) {
        e.preventDefault();
        tx = panStartTx + (e.touches[0].clientX - panStartX);
        ty = panStartTy + (e.touches[0].clientY - panStartY);
        applyTransform();
      } else if (touchMode === 'pinch' && e.touches.length === 2) {
        e.preventDefault();
        const [a, b] = e.touches;
        const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
        if (pinchStartDist > 0) {
          zoomAt(pinchStartScale * (dist / pinchStartDist), pinchCenter.x, pinchCenter.y);
        }
      }
    },
    { passive: false },
  );
  imgEl.addEventListener('touchend', (e) => {
    if (e.touches.length === 0) touchMode = null;
    else if (e.touches.length === 1) {
      // pinch 结束后过渡到 pan
      touchMode = 'pan';
      panStartX = e.touches[0].clientX;
      panStartY = e.touches[0].clientY;
      panStartTx = tx;
      panStartTy = ty;
    }
  });

  // 点蒙层空白 / stage 留白处关闭。但放大后不要误触关闭，所以仅在 scale==1 时生效。
  lb.addEventListener('click', (e) => {
    if (scale > 1.01) return;
    if (e.target === lb || e.target.classList.contains('rp-lightbox__stage')) {
      close();
    }
  });

  // 键盘
  document.addEventListener('keydown', (e) => {
    if (!lb.classList.contains('is-open')) return;
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowLeft') go(idx - 1);
    else if (e.key === 'ArrowRight') go(idx + 1);
    else if (e.key === '+' || e.key === '=') {
      const r = lb.getBoundingClientRect();
      zoomAt(scale * 1.4, r.left + r.width / 2, r.top + r.height / 2);
    } else if (e.key === '-' || e.key === '_') {
      const r = lb.getBoundingClientRect();
      zoomAt(scale / 1.4, r.left + r.width / 2, r.top + r.height / 2);
    } else if (e.key === '0') {
      resetTransform();
    }
  });
}


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
    // 图片 lightbox 打开时，方向键归 lightbox 翻图，别让背后的 deck 也翻页。
    if (document.querySelector('.rp-lightbox.is-open')) return;
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
