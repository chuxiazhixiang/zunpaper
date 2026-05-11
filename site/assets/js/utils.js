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

// ---- Color Hunt palette integration ---------------------------------------
//
// We ship ~80 hand-curated light palettes scraped from https://colorhunt.co/
// in site/assets/data/palettes.json. Each palette = [c1,c2,c3,c4] hex (no #).
// pickCoverStyleAttr below picks one deterministically per paper id and
// outputs an inline `style="..."` snippet that overrides the cover's CSS
// custom properties — so the legacy c0..c5 hardcoded variants still work as
// a fallback when the JSON hasn't loaded yet.

let _palettesCache = null;
let _palettesPromise = null;
export function loadPalettes() {
  if (_palettesCache) return Promise.resolve(_palettesCache);
  if (_palettesPromise) return _palettesPromise;
  _palettesPromise = fetch('assets/data/palettes.json')
    .then((r) => (r.ok ? r.json() : { palettes: [] }))
    .then((d) => {
      _palettesCache = Array.isArray(d?.palettes) ? d.palettes : [];
      return _palettesCache;
    })
    .catch(() => {
      _palettesCache = [];
      return _palettesCache;
    });
  return _palettesPromise;
}

function _luminance(hex) {
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return 0.299 * r + 0.587 * g + 0.114 * b;
}

function _sortLightToDark(colors) {
  return [...colors].sort((a, b) => _luminance(b) - _luminance(a));
}

/** Mix a CSS rgba() from #hex + alpha (0..1). */
function _alpha(hex, a) {
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

/** Build an inline style="..." override for `.rp-cover--{style}` given a paper id
 *  and the loaded palettes list. Returns '' if palettes are missing — the
 *  legacy `c0..c5` class on the cover then takes over.
 *
 *  The 4 colors are sorted lightest -> darkest before assignment so each
 *  palette consistently maps the lightest tone to the background and darkest
 *  to the accent / text. This keeps text readable across the 80 palettes
 *  without per-palette manual tuning. */
export function coverStyleAttr(id, palettes, style) {
  if (!palettes || !palettes.length) return '';
  const h = hashStr(id + ':palette-v1');
  const p = palettes[h % palettes.length];
  if (!p?.colors || p.colors.length < 4) return '';
  const [c1, c2, c3, c4] = _sortLightToDark(p.colors); // lightest -> darkest
  const bg = `#${c1}`;
  const tint = `#${c2}`;
  const accent = `#${c3}`;
  const text = `#${c4}`;
  if (style === 'washi') {
    return [
      `--rp-washi-bg:${bg}`,
      `--rp-washi-tape:${accent}`,
      `--rp-washi-text:${text}`,
      `--rp-washi-grid:${_alpha(c4, 0.10)}`,
    ].join(';');
  }
  // magazine (the only other style currently)
  return [
    `--rp-mag-bg:${bg}`,
    `--rp-mag-accent:${accent}`,
    `--rp-mag-text:${text}`,
    `--rp-mag-soft:${tint}`,
  ].join(';');
}

/** Sticker placement (anime emoji). Picks 0-2 stickers and randomized corners
 *  per paper id, again deterministic. Returns array of { src, corner, rotate }.
 *
 *  `available` is the parsed manifest.json: an array of { src, name }. We avoid
 *  the top-right corner because that's where the source badge (ARXIV / HF…)
 *  lives on every cover. */
export function pickStickers(id, available, max = 2) {
  if (!available || !available.length) return [];
  const h = hashStr(id + ':sticker-v2');
  // 30% of cards stay clean; rest get 1 (60% of remaining) or 2 (40%) stickers.
  const roll = h % 10;
  let count;
  if (roll < 3) count = 0;
  else if (roll < 8) count = 1;
  else count = 2;
  if (!count) return [];
  // Top-right reserved for the source badge — only use TL / BL / BR.
  const corners = ['bl', 'br', 'tl', 'bl', 'br'];
  const out = [];
  const used = new Set();
  for (let i = 0; i < Math.min(count, max); i++) {
    const entry = available[(h + i * 17) % available.length];
    const src = typeof entry === 'string' ? entry : entry.src;
    let corner = corners[(h + i * 31) % corners.length];
    if (used.has(corner)) {
      corner = corners.find((c) => !used.has(c)) || corner;
    }
    used.add(corner);
    const rotate = ((h + i * 41) % 31) - 15;
    out.push({ src, corner, rotate });
  }
  return out;
}

/** Render the HTML for a list of stickers, ready to drop inside `.rp-cover`.
 *  URL-encodes the path so Chinese filenames survive the trip through the
 *  static server / CDN. */
export function stickersHTML(stickers) {
  if (!stickers || !stickers.length) return '';
  function encodePath(p) {
    // Split on slashes so we don't encode them. Each segment is encoded.
    return p
      .split('/')
      .map((seg) => encodeURIComponent(seg))
      .join('/');
  }
  return stickers
    .map(
      (s) => `<img class="rp-cover__sticker rp-cover__sticker--${s.corner}"
              src="${encodePath(s.src)}" alt="" loading="lazy"
              style="transform: rotate(${s.rotate}deg);" />`,
    )
    .join('');
}

let _stickerManifestCache = null;
let _stickerManifestPromise = null;
/** Load and cache the sticker manifest.json. Returns [] on failure. */
export function loadStickerManifest() {
  if (_stickerManifestCache) return Promise.resolve(_stickerManifestCache);
  if (_stickerManifestPromise) return _stickerManifestPromise;
  _stickerManifestPromise = fetch('assets/img/stickers/manifest.json')
    .then((r) => (r.ok ? r.json() : []))
    .then((d) => {
      _stickerManifestCache = Array.isArray(d) ? d : [];
      return _stickerManifestCache;
    })
    .catch(() => {
      _stickerManifestCache = [];
      return _stickerManifestCache;
    });
  return _stickerManifestPromise;
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
