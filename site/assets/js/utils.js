// Shared helpers: deterministic hashing, escaping, formatting.

export function hashStr(s) {
  let h = 5381;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  }
  return Math.abs(h);
}

export function pickPalette(id, n = 8) {
  return hashStr(id) % n;
}

/** Pick one of the two cover styles + one of 6 color variants.
 *  Deterministic on paper id so the same paper always renders the same way. */
export function pickCover(id) {
  const styles = ['washi', 'magazine'];
  const h = hashStr(id);
  const style = styles[h % styles.length];
  const color = Math.floor(h / 13) % 6;
  return { style, color, cls: `rp-cover--${style} c${color}` };
}

// ---- Cover palette ---------------------------------------------------------
//
// 之前我们做过一版「从 colorhunt.co 抓 80 个 palette 随机分配」的方案，
// 站长反馈太丑后撤回。现在恢复到 CSS 里 hardcode 的 c0..c5 六个手调变体
// （由 pickCover() 的 .cls 决定）。
//
// loadPalettes / coverStyleAttr 两个 export 保留是为了向后兼容（feed.js /
// post.js / archive.js / favorites.js 还在调用），它们现在永远返回空，
// 让 c0..c5 这套 CSS 兜底接管。
export function loadPalettes() {
  return Promise.resolve([]);
}

export function coverStyleAttr(_id, _palettes, _style) {
  return '';
}

export function escapeHTML(s) {
  if (s == null) return '';
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

export function formatAuthors(authors) {
  if (!authors || !authors.length) return '';
  const head = authors.slice(0, 2).join('、');
  return authors.length > 2 ? `${head} 等` : head;
}

export function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  if (sameDay) return '今天';
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return '昨天';
  return iso;
}

export function showToast(msg, ms = 1500) {
  let el = document.querySelector('.rp-toast');
  if (!el) {
    el = document.createElement('div');
    el.className = 'rp-toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('is-visible');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('is-visible'), ms);
}

// Truncate Chinese-or-mixed text by character count, adding ellipsis.
export function clip(text, n) {
  if (!text) return '';
  const t = text.trim();
  return t.length > n ? t.slice(0, n) + '…' : t;
}

export function paperUrl(id) {
  return `post.html?id=${encodeURIComponent(id)}`;
}

/** Wire up a search input that submits by jumping back to the home page with
 *  `?q=` in the URL. The home page feed.js reads that on load. */
export function attachSearchRedirect(selector = '#search-input') {
  const input = document.querySelector(selector);
  if (!input) return;
  function go() {
    const q = input.value.trim();
    if (!q) return;
    window.location.href = `index.html?q=${encodeURIComponent(q)}`;
  }
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') go();
  });
  const btn = document.querySelector(`${selector} + .rp-search__go, .rp-search__go`);
  btn?.addEventListener('click', go);
}

// Heart icon (svg, color follows currentColor)
export const HEART_SVG_OUTLINE =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';

export const HEART_SVG_FILL =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';
