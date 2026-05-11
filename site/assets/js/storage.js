// localStorage helpers for favorites / likes / read / theme.
// All state lives client-side; no login needed.

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

export const Likes = {
  has: (id) => readSet('likes').has(id),
  toggle(id) {
    const s = readSet('likes');
    if (s.has(id)) s.delete(id); else s.add(id);
    writeSet('likes', s);
    return s.has(id);
  },
  all: () => [...readSet('likes')],
};

export const Favorites = {
  has: (id) => readSet('favorites').has(id),
  toggle(id) {
    const s = readSet('favorites');
    if (s.has(id)) s.delete(id); else s.add(id);
    writeSet('favorites', s);
    return s.has(id);
  },
  all: () => [...readSet('favorites')],
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
