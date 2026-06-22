// localStorage helpers for favorites / reads / theme.
// All state lives client-side; no login needed. When the site is public,
// every visitor's data is private to their browser — nothing flows back to
// the static repo.

const NS = 'redpaper:';

function readSet(key) {
  try {
    const raw = localStorage.getItem(NS + key);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function writeSet(key, set) {
  localStorage.setItem(NS + key, JSON.stringify([...set]));
}

// -------- Favorites with custom categories --------
//
// State shape (v2):
// {
//   categories: ["默认", "必读", ...],   // user-defined, order preserved
//   items: { "<paper_id>": { categories: ["默认"], addedAt: ISOString } }
// }
//
// Categories are visitor-local; each browser has its own. Old "likes" + flat
// "favorites" Sets from earlier versions are migrated on first read.

const FAV_KEY = NS + 'favorites:v2';
const DEFAULT_CATEGORY = '默认';

function readFav() {
  try {
    const raw = localStorage.getItem(FAV_KEY);
    if (raw) {
      const obj = JSON.parse(raw);
      if (obj && Array.isArray(obj.categories) && obj.items) {
        if (!obj.categories.includes(DEFAULT_CATEGORY)) {
          obj.categories.unshift(DEFAULT_CATEGORY);
        }
        return obj;
      }
    }
  } catch {
    /* fall through to migration */
  }
  return migrateLegacy();
}

function migrateLegacy() {
  const legacyFav = readSet('favorites');
  const legacyLike = readSet('likes');
  const state = { categories: [DEFAULT_CATEGORY], items: {} };
  const now = new Date().toISOString();
  for (const id of new Set([...legacyFav, ...legacyLike])) {
    state.items[id] = { categories: [DEFAULT_CATEGORY], addedAt: now };
  }
  writeFav(state);
  return state;
}

function writeFav(state) {
  localStorage.setItem(FAV_KEY, JSON.stringify(state));
}

export const Favorites = {
  // — Membership ————————————————————————————————————————————
  has(id) {
    const s = readFav();
    return !!s.items[id];
  },

  /** Toggle membership in `category`. If the paper is in any category and no
   *  specific category is given, remove it from all. */
  toggle(id, category) {
    const s = readFav();
    if (category) {
      const item = s.items[id] || { categories: [], addedAt: new Date().toISOString() };
      const cats = new Set(item.categories);
      if (cats.has(category)) {
        cats.delete(category);
      } else {
        cats.add(category);
      }
      if (cats.size === 0) {
        delete s.items[id];
      } else {
        item.categories = [...cats];
        s.items[id] = item;
      }
      writeFav(s);
      return cats.size > 0;
    }
    if (s.items[id]) {
      delete s.items[id];
      writeFav(s);
      return false;
    }
    s.items[id] = { categories: [DEFAULT_CATEGORY], addedAt: new Date().toISOString() };
    writeFav(s);
    return true;
  },

  /** Replace the set of categories a paper belongs to. Empty list removes the
   *  paper from favorites entirely. */
  setCategoriesOf(id, categories) {
    const s = readFav();
    const clean = [...new Set(categories.filter(Boolean))];
    if (clean.length === 0) {
      delete s.items[id];
    } else {
      const item = s.items[id] || { addedAt: new Date().toISOString() };
      item.categories = clean;
      s.items[id] = item;
      // Ensure category list contains all referenced names.
      for (const c of clean) {
        if (!s.categories.includes(c)) s.categories.push(c);
      }
    }
    writeFav(s);
  },

  categoriesOf(id) {
    const s = readFav();
    return s.items[id]?.categories || [];
  },

  /** All favorite paper ids, optionally filtered by category. */
  ids(category) {
    const s = readFav();
    const all = Object.entries(s.items);
    if (!category) return all.map(([id]) => id);
    return all.filter(([, it]) => it.categories.includes(category)).map(([id]) => id);
  },

  // — Category management ——————————————————————————————————
  categories() {
    return readFav().categories.slice();
  },

  addCategory(name) {
    name = (name || '').trim();
    if (!name) return false;
    const s = readFav();
    if (s.categories.includes(name)) return false;
    s.categories.push(name);
    writeFav(s);
    return true;
  },

  renameCategory(oldName, newName) {
    oldName = (oldName || '').trim();
    newName = (newName || '').trim();
    if (!oldName || !newName || oldName === newName) return false;
    if (oldName === DEFAULT_CATEGORY) return false; // 默认 不可改名
    const s = readFav();
    const idx = s.categories.indexOf(oldName);
    if (idx < 0) return false;
    if (s.categories.includes(newName)) return false;
    s.categories[idx] = newName;
    for (const it of Object.values(s.items)) {
      it.categories = it.categories.map((c) => (c === oldName ? newName : c));
    }
    writeFav(s);
    return true;
  },

  /** Remove a category. Papers in this category lose this tag; if a paper
   *  has no remaining categories, it's removed from favorites entirely. */
  removeCategory(name) {
    if (name === DEFAULT_CATEGORY) return false;
    const s = readFav();
    const idx = s.categories.indexOf(name);
    if (idx < 0) return false;
    s.categories.splice(idx, 1);
    for (const [id, it] of Object.entries(s.items)) {
      it.categories = it.categories.filter((c) => c !== name);
      if (it.categories.length === 0) delete s.items[id];
    }
    writeFav(s);
    return true;
  },

  DEFAULT_CATEGORY,
};

// -------- 站长「💎 高质量」标记（独立于收藏） --------
// 浏览时一键标记你认可的高质量论文，存本浏览器。用「导出高质量清单」导出后，
// 跑 scripts/import_curated.py 合并进 config/curated.yaml（提交进仓库 = 金标准
// 数据集），下次 build 就会打 💎 徽章 + 评分加成。
export const Curated = {
  has: (id) => readSet('curated').has(id),
  toggle(id) {
    const s = readSet('curated');
    let on;
    if (s.has(id)) { s.delete(id); on = false; } else { s.add(id); on = true; }
    writeSet('curated', s);
    return on;
  },
  ids: () => [...readSet('curated')],
  count: () => readSet('curated').size,
};

export const Reads = {
  has: (id) => readSet('reads').has(id),
  mark(id) {
    const s = readSet('reads');
    if (!s.has(id)) {
      s.add(id);
      writeSet('reads', s);
    }
  },
  all: () => [...readSet('reads')],
};

const THEME_KEY = NS + 'theme';

export const Theme = {
  get() {
    return localStorage.getItem(THEME_KEY) || 'auto';
  },
  apply(mode) {
    const root = document.documentElement;
    if (mode === 'auto') {
      root.removeAttribute('data-theme');
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      root.setAttribute('data-theme', mq.matches ? 'dark' : 'light');
    } else {
      root.setAttribute('data-theme', mode);
    }
  },
  set(mode) {
    localStorage.setItem(THEME_KEY, mode);
    this.apply(mode);
  },
  cycle() {
    const order = ['auto', 'light', 'dark'];
    const cur = this.get();
    const next = order[(order.indexOf(cur) + 1) % order.length];
    this.set(next);
    return next;
  },
  init() {
    this.apply(this.get());
    // Re-apply when system theme changes (only matters in 'auto').
    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (this.get() === 'auto') this.apply('auto');
      });
    }
  },
};
