// Monthly digest page: load monthly_index.json, render tab bar; on tab click,
// load digest/monthly/<ym>.json and render headline + summary as HTML.

import { Theme } from './storage.js?v=6be6c568';
import {
  escapeHTML,
  attachSearchRedirect,
  fetchJSON,
} from './utils.js?v=6be6c568';

const STATE = {
  digests: [],     // index entries
  current: null,
};

async function loadIndex() {
  return fetchJSON('data/digest/monthly_index.json')
    .then((r) => r.json())
    .catch(() => ({ digests: [] }));
}

async function loadOne(ym) {
  return fetchJSON(`data/digest/monthly/${ym}.json`).then((r) => r.json());
}

// 极简 markdown → HTML：只处理 ##/### 标题、列表、加粗、段落。
// 月度综述结构固定（我们的 system prompt 强制），不需要完整 md 库。
function mdToHTML(md) {
  if (!md) return '';
  const lines = md.replace(/\r/g, '').split('\n');
  const out = [];
  let inList = false;
  const flushList = () => {
    if (inList) {
      out.push('</ul>');
      inList = false;
    }
  };
  for (const raw of lines) {
    const line = raw;
    const trimmed = line.trim();
    if (!trimmed) {
      flushList();
      continue;
    }
    let m;
    if ((m = /^###\s+(.+)$/.exec(trimmed))) {
      flushList();
      out.push(`<h4>${inline(m[1])}</h4>`);
      continue;
    }
    if ((m = /^##\s+(.+)$/.exec(trimmed))) {
      flushList();
      out.push(`<h3>${inline(m[1])}</h3>`);
      continue;
    }
    if ((m = /^#\s+(.+)$/.exec(trimmed))) {
      flushList();
      out.push(`<h2>${inline(m[1])}</h2>`);
      continue;
    }
    if ((m = /^[-*]\s+(.+)$/.exec(trimmed))) {
      if (!inList) {
        out.push('<ul>');
        inList = true;
      }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    flushList();
    out.push(`<p>${inline(trimmed)}</p>`);
  }
  flushList();
  return out.join('\n');
}

function inline(s) {
  // escape first, then unescape allowed inline markup
  let safe = escapeHTML(s);
  safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  safe = safe.replace(/`([^`]+)`/g, '<code>$1</code>');
  return safe;
}

function renderTabs() {
  const wrap = document.getElementById('month-tabs');
  wrap.innerHTML = STATE.digests
    .map(
      (d) =>
        `<button class="rp-tab ${d.year_month === STATE.current ? 'is-active' : ''}" data-ym="${escapeHTML(d.year_month)}">${escapeHTML(d.year_month)}</button>`,
    )
    .join('');
  wrap.querySelectorAll('button').forEach((b) => {
    b.addEventListener('click', () => {
      const ym = b.getAttribute('data-ym');
      selectMonth(ym);
    });
  });
}

function renderEmpty() {
  document.getElementById('monthly-body').innerHTML = `
    <p class="rp-status">还没有月度综述。运行 <code>python scripts/run_monthly_digest.py --all</code> 即可生成。</p>
  `;
}

async function selectMonth(ym) {
  STATE.current = ym;
  renderTabs();
  const body = document.getElementById('monthly-body');
  body.innerHTML = '<p class="rp-status">加载中…</p>';
  try {
    const d = await loadOne(ym);
    const themes = (d.themes || [])
      .map((t) => `<span class="rp-chip rp-chip--method">${escapeHTML(t)}</span>`)
      .join('');
    body.innerHTML = `
      <header class="rp-monthly-head">
        <div class="rp-monthly-meta">
          <span>📅 ${escapeHTML(d.year_month)}</span>
          <span>📚 ${d.paper_count} 篇</span>
          <span>🤖 ${escapeHTML(d.model || 'LLM')}</span>
          <span>⏱ ${escapeHTML(d.generated_at || '')}</span>
        </div>
        <h3 class="rp-monthly-headline">${escapeHTML(d.headline || '')}</h3>
        ${themes ? `<div class="rp-monthly-themes">${themes}</div>` : ''}
      </header>
      <div class="rp-monthly-content">${mdToHTML(d.summary_md || '')}</div>
    `;
  } catch (e) {
    body.innerHTML = `<p class="rp-status">加载失败：${escapeHTML(String(e))}</p>`;
  }
}

async function main() {
  Theme.init();
  document.querySelector('#theme-toggle')?.addEventListener('click', () => {
    Theme.cycle();
  });
  attachSearchRedirect();
  const idx = await loadIndex();
  STATE.digests = idx.digests || [];
  if (!STATE.digests.length) {
    renderEmpty();
    return;
  }
  const initial = STATE.digests[0].year_month;
  await selectMonth(initial);
}

main();
