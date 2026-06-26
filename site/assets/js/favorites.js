// Favorites page: filter the global index to favorited papers, grouped by
// user-defined categories. All state is local to this browser.

import { Favorites, Curated, Theme } from './storage.js?v=7adda7f1';
import {
  pickCover,
  loadPalettes,
  coverStyleAttr,
  escapeHTML,
  formatAuthors,
  paperUrl,
  attachSearchRedirect,
  HEART_SVG_OUTLINE,
  HEART_SVG_FILL,
  showToast,
  fetchJSON,
} from './utils.js?v=7adda7f1';
import { chipRowsHTML, videoBadgeHTML, githubCardHTML, externalCardHTML } from './feed.js?v=7adda7f1';

const STATE = {
  papers: [],          // master list from index.json
  palettes: [],
  activeCategory: '',  // '' means "全部"
};

async function loadIndex() {
  return fetchJSON('data/index.json').then((r) => r.json()).catch(() => ({ papers: [] }));
}

function cardHTML(p) {
  // 开源仓复用 feed 的 GitHub 卡片（显示 star/语言、直链仓库），保持一致。
  if ((p.source || '') === 'github') return githubCardHTML(p);
  if ((p.source || '') === 'external_link') return externalCardHTML(p);
  const cover = pickCover(p.id);
  const titleZh = p.title_zh || p.title;
  const headline = p.cover_zh || p.tldr_zh || titleZh;
  const fav = Favorites.has(p.id);
  const source = (p.source || '').toUpperCase();
  const cats = Favorites.categoriesOf(p.id);

  const paletteStyle = coverStyleAttr(p.id, STATE.palettes, cover.style);

  return `
    <a class="rp-card" href="${paperUrl(p.id)}" data-id="${p.id}">
      <div class="rp-cover ${cover.cls}"${paletteStyle ? ` style="${paletteStyle}"` : ''}>
        <span class="rp-cover__source">${escapeHTML(source)}</span>
        ${videoBadgeHTML(p)}
        <p class="rp-cover__headline">${escapeHTML(headline)}</p>
      </div>
      <div class="rp-card__body">
        <h4 class="rp-card__title">${escapeHTML(titleZh)}</h4>
        ${p.tldr_zh ? `<p class="rp-card__tldr">${escapeHTML(p.tldr_zh)}</p>` : ''}
        ${chipRowsHTML(p)}
        ${
          cats.length
            ? `<div class="rp-card__badges">${cats
                .map(
                  (c) =>
                    `<span class="rp-badge">${escapeHTML(c)}</span>`,
                )
                .join('')}</div>`
            : ''
        }
        <div class="rp-card__meta">
          <span class="rp-card__authors">${escapeHTML(formatAuthors(p.authors || []))}</span>
          <button class="rp-card__like ${fav ? 'is-liked' : ''}" data-fav="${p.id}" title="从收藏移除">
            ${fav ? HEART_SVG_FILL : HEART_SVG_OUTLINE}
          </button>
        </div>
      </div>
    </a>`;
}

function renderCategoryTabs() {
  const wrap = document.querySelector('#category-tabs');
  if (!wrap) return;
  const cats = Favorites.categories();
  const counts = {};
  counts[''] = Favorites.ids().length;
  for (const c of cats) counts[c] = Favorites.ids(c).length;

  const tabs = [{ id: '', label: '全部' }, ...cats.map((c) => ({ id: c, label: c }))];
  wrap.innerHTML = `
    ${tabs
      .map((t) => {
        const active = t.id === STATE.activeCategory;
        const removable = t.id && t.id !== Favorites.DEFAULT_CATEGORY;
        return `
          <span class="rp-cat-tab ${active ? 'is-active' : ''}" data-cat="${escapeHTML(t.id)}">
            <span class="rp-cat-tab__label" data-cat-label="${escapeHTML(t.id)}">${escapeHTML(t.label)}</span>
            <span class="rp-cat-tab__count">${counts[t.id] || 0}</span>
            ${removable ? `<button class="rp-cat-tab__remove" data-remove="${escapeHTML(t.id)}" title="删除分类">×</button>` : ''}
          </span>`;
      })
      .join('')}
    <button class="rp-cat-tab rp-cat-tab--add" id="cat-add">+ 新建分类</button>
  `;

  wrap.querySelectorAll('.rp-cat-tab[data-cat]').forEach((el) => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('[data-remove]')) return;
      STATE.activeCategory = el.dataset.cat;
      renderAll();
    });
  });
  wrap.querySelectorAll('[data-remove]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const name = btn.dataset.remove;
      if (!confirm(`删除分类「${name}」？已收藏的论文若只属于该分类，会从收藏夹移除。`)) return;
      if (Favorites.removeCategory(name)) {
        if (STATE.activeCategory === name) STATE.activeCategory = '';
        renderAll();
        showToast('分类已删除');
      }
    });
  });

  // Double-click on the label of a non-default category renames it.
  wrap.querySelectorAll('[data-cat-label]').forEach((el) => {
    if (!el.dataset.catLabel || el.dataset.catLabel === Favorites.DEFAULT_CATEGORY) return;
    el.title = '双击重命名';
    el.addEventListener('dblclick', (e) => {
      e.stopPropagation();
      const old = el.dataset.catLabel;
      const next = prompt('重命名分类：', old);
      if (next && next.trim() && next.trim() !== old) {
        if (Favorites.renameCategory(old, next.trim())) {
          if (STATE.activeCategory === old) STATE.activeCategory = next.trim();
          renderAll();
        } else {
          showToast('名字已存在或无效');
        }
      }
    });
  });

  document.querySelector('#cat-add')?.addEventListener('click', () => {
    const name = prompt('新建分类名（最多 20 字）：');
    if (!name) return;
    if (Favorites.addCategory(name.trim())) {
      STATE.activeCategory = name.trim();
      renderAll();
    } else {
      showToast('分类已存在');
    }
  });
}

/** Placeholder card for a favorited id that's no longer in the master index
 *  (paper got pruned by retag_and_prune, or was withdrawn from arXiv). The
 *  user can still see SOMETHING and click "从收藏夹移除" to clean it up. */
function missingCardHTML(id) {
  return `
    <div class="rp-card rp-card--missing" data-id="${id}">
      <div class="rp-cover rp-cover--missing">
        <span class="rp-cover__source">已下架</span>
        <p class="rp-cover__headline">📄 论文已下架</p>
      </div>
      <div class="rp-card__body">
        <h4 class="rp-card__title">${escapeHTML(id)}</h4>
        <p class="rp-card__tldr">站长把这篇从网站上撤掉了（可能是不符合机器人方向的过滤），但你之前点的收藏还留在浏览器里。</p>
        <div class="rp-card__meta">
          <span class="rp-card__authors">—</span>
          <button class="rp-card__like is-liked" data-fav="${id}" title="从收藏移除" aria-label="从收藏移除">
            ${HEART_SVG_FILL}
          </button>
        </div>
      </div>
    </div>`;
}

function renderFeed() {
  const feed = document.querySelector('#feed');
  const count = document.querySelector('#count');
  const favIds = Favorites.ids(STATE.activeCategory || undefined);
  if (count) count.textContent = favIds.length;

  if (!favIds.length) {
    feed.innerHTML = `
      <div class="rp-status">
        <p class="rp-status__title">${STATE.activeCategory ? `「${escapeHTML(STATE.activeCategory)}」还没有收藏` : '收藏夹空空如也'}</p>
        <p>在首页或详情页点 ❤ 就会出现在这里。所有收藏只存在你这台浏览器，不会同步到服务器。</p>
      </div>`;
    return;
  }

  // Index existing papers by id so we render real cards when we have data,
  // and a "已下架" placeholder otherwise. Earlier this page would silently
  // show nothing when the favorite count was >0 but no paper id matched.
  const byId = new Map(STATE.papers.map((p) => [p.id, p]));
  const html = favIds.map((id) => {
    const p = byId.get(id);
    return p ? cardHTML(p) : missingCardHTML(id);
  });
  feed.innerHTML = html.join('');

  feed.querySelectorAll('[data-fav]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const id = btn.dataset.fav;
      Favorites.toggle(id);
      renderAll();
      showToast('已从收藏移除');
    });
  });
  // 复用的 GitHub / 外链卡里也有 💎 按钮（data-gem）。收藏页若不拦截，点 💎 会触发
  // 外层 <a> 跳转而不是切换站长甄选 —— 这里单独绑定。
  feed.querySelectorAll('[data-gem]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const id = btn.dataset.gem;
      const on = Curated.toggle(id);
      btn.classList.toggle('is-on', on);
      showToast(on ? '已标记为高质量 💎' : '已取消高质量标记');
    });
  });
}

function renderAll() {
  renderCategoryTabs();
  renderFeed();
}

async function main() {
  Theme.init();
  attachSearchRedirect();
  document.querySelector('#theme-toggle')?.addEventListener('click', () => {
    const mode = Theme.cycle();
    showToast(mode === 'auto' ? '跟随系统' : mode === 'dark' ? '暗色模式' : '亮色模式');
  });

  const [index, palettes] = await Promise.all([loadIndex(), loadPalettes()]);
  STATE.papers = index.papers || [];
  STATE.palettes = palettes || [];
  renderAll();
  injectCuratedBar();
}

// 「💎 高质量」导出条：浏览时在卡片上点 💎 标记的论文，这里一键导出 JSON，
// 再用 scripts/import_curated.py 合并进 config/curated.yaml（提交进仓库）。
function injectCuratedBar() {
  const host = document.querySelector('#category-tabs') || document.querySelector('#feed');
  if (!host || document.querySelector('#curated-bar')) return;
  const bar = document.createElement('div');
  bar.id = 'curated-bar';
  bar.className = 'rp-curated-bar';
  const n = Curated.count();
  bar.innerHTML = `
    <span>💎 已标记高质量 <b>${n}</b> 篇</span>
    <button class="rp-btn" id="curated-export">导出清单</button>
    <span class="rp-curated-bar__hint">导出后跑 <code>python scripts/import_curated.py 下载的.json</code> 合并入库</span>`;
  host.parentNode.insertBefore(bar, host);
  document.querySelector('#curated-export')?.addEventListener('click', () => {
    const ids = Curated.ids();
    if (!ids.length) { showToast('还没标记任何高质量论文'); return; }
    const byId = new Map(STATE.papers.map((p) => [p.id, p]));
    const papers = ids.map((id) => {
      const p = byId.get(id);
      return { id, title: p ? (p.title || '') : '', title_zh: p ? (p.title_zh || '') : '' };
    });
    const blob = new Blob(
      [JSON.stringify({ exported_at: new Date().toISOString(), curated: ids, papers }, null, 2)],
      { type: 'application/json' },
    );
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'curated-export.json';
    a.click();
    URL.revokeObjectURL(a.href);
    showToast(`已导出 ${ids.length} 篇`);
  });
}

document.addEventListener('DOMContentLoaded', main);
