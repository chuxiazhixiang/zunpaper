// 「加分类」表单：把站长填的内容生成一个 channels.d/<id>.yaml 配置文件，
// 供下载 / 复制。纯前端，不依赖后端。生成的 YAML 与 config/channels.d 加载器
// （config.load_channels）和 B 方案独立判定（judge.judge_paper_for_channel）对齐。
import { Theme } from './storage.js?v=5d96b194';
import { attachSearchRedirect } from './utils.js?v=5d96b194';

Theme.init();
attachSearchRedirect();

const $ = (id) => document.getElementById(id);

// ---- slug 生成 -------------------------------------------------------------
function slugify(s) {
  return (s || '')
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fff]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .replace(/-{2,}/g, '-')
    .replace(/[\u4e00-\u9fff]/g, ''); // CJK 不进 slug，提示用户改
}

// ---- 列表解析（换行 / 逗号分隔，去空去重）---------------------------------
function parseList(raw) {
  const out = [];
  const seen = new Set();
  for (const part of (raw || '').split(/[\n,]/)) {
    const v = part.trim();
    if (v && !seen.has(v)) {
      seen.add(v);
      out.push(v);
    }
  }
  return out;
}

// ---- YAML 序列化（针对我们这个固定结构，安全引号）-------------------------
function q(s) {
  // 双引号字符串，转义反斜杠和双引号。
  return '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
}

function yamlList(key, items, indent = '') {
  if (!items.length) return `${indent}${key}: []\n`;
  let s = `${indent}${key}:\n`;
  for (const it of items) s += `${indent}  - ${q(it)}\n`;
  return s;
}

function yamlBlock(key, text, indent = '') {
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  let s = `${indent}${key}: |-\n`;
  for (const ln of lines) s += `${indent}  ${ln}\n`;
  return s;
}

// ---- 收集表单 --------------------------------------------------------------
function collect() {
  const name = $('f-name').value.trim();
  let id = $('f-id').value.trim();
  if (!id) id = slugify(name);
  return {
    name,
    id,
    emoji: $('f-emoji').value.trim(),
    desc: $('f-desc').value.trim(),
    judge_prompt: $('f-judge').value.trim(),
    keywords: parseList($('f-keywords').value),
    arxiv_categories: parseList($('f-cats').value),
    exclude: parseList($('f-exclude').value),
    venues: parseList($('f-venues').value),
    examples: collectExamples(),
    backfill_days: parseInt($('f-backfill').value || '30', 10) || 0,
  };
}

function collectExamples() {
  const out = [];
  document.querySelectorAll('#examples-list .rp-example-row').forEach((row) => {
    const title = row.querySelector('.ex-title').value.trim();
    const url = row.querySelector('.ex-url').value.trim();
    if (url) out.push({ title, url });
  });
  return out;
}

// ---- 生成 YAML 文本 --------------------------------------------------------
function buildYAML(d) {
  let s = '';
  s += '# redpaper 自定义分类 —— 由网页「加分类」表单生成\n';
  s += '# 用法：把本文件放进仓库 config/channels.d/ 文件夹，提交后重新构建即可。\n';
  s += `id: ${q(d.id || 'your-id')}\n`;
  s += `name: ${q(d.name || '未命名分类')}\n`;
  if (d.emoji) s += `emoji: ${q(d.emoji)}\n`;
  s += yamlList('arxiv_categories', d.arxiv_categories.length ? d.arxiv_categories : ['cs.RO']);
  s += yamlList('keywords', d.keywords);
  s += yamlList('exclude', d.exclude);
  if (d.desc) s += `desc: ${q(d.desc)}\n`;
  if (d.judge_prompt) s += yamlBlock('judge_prompt', d.judge_prompt);
  s += yamlList('venues', d.venues);
  if (d.examples.length) {
    s += 'examples:\n';
    for (const ex of d.examples) {
      const t = ex.title ? `title: ${q(ex.title)}, ` : '';
      s += `  - { ${t}url: ${q(ex.url)} }\n`;
    }
  } else {
    s += 'examples: []\n';
  }
  s += `backfill_days: ${d.backfill_days}\n`;
  s += 'max_per_day: 20\n';
  return s;
}

// ---- 校验 ------------------------------------------------------------------
function validate(d) {
  const errs = [];
  if (!d.name) errs.push('分类名称');
  if (!d.id) errs.push('分类 ID（中文名请手动填一个英文 ID）');
  if (!/^[a-z0-9][a-z0-9-]*$/.test(d.id || '')) errs.push('分类 ID 只能是英文小写 + 连字符');
  if (!d.desc) errs.push('一句话方向定义');
  if (!d.judge_prompt) errs.push('筛选标准');
  if (!d.keywords.length) errs.push('至少 1 个关键词');
  return errs;
}

// ---- 示例论文行 ------------------------------------------------------------
function addExampleRow(title = '', url = '') {
  const list = $('examples-list');
  if (list.querySelectorAll('.rp-example-row').length >= 6) return;
  const row = document.createElement('div');
  row.className = 'rp-example-row';
  row.innerHTML = `
    <input class="ex-title" type="text" placeholder="论文标题（可留空）" />
    <input class="ex-url" type="text" placeholder="https://arxiv.org/abs/..." />
    <button type="button" class="rp-example-row__del" title="删除">✕</button>`;
  row.querySelector('.ex-title').value = title;
  row.querySelector('.ex-url').value = url;
  row.querySelector('.rp-example-row__del').addEventListener('click', () => {
    row.remove();
    refresh();
  });
  row.querySelectorAll('input').forEach((i) => i.addEventListener('input', refresh));
  list.appendChild(row);
}

// ---- 实时刷新预览 ----------------------------------------------------------
function refresh() {
  const d = collect();
  $('yaml-preview').textContent = buildYAML(d);
  $('preview-filename').textContent = `${d.id || 'your-id'}.yaml`;
}

let msgTimer = null;
function flash(text, ok = true) {
  const el = $('form-msg');
  el.textContent = text;
  el.classList.toggle('is-ok', ok);
  el.classList.toggle('is-err', !ok);
  if (msgTimer) clearTimeout(msgTimer);
  msgTimer = setTimeout(() => {
    el.textContent = '';
    el.classList.remove('is-ok', 'is-err');
  }, 4000);
}

function download() {
  const d = collect();
  const errs = validate(d);
  if (errs.length) {
    flash('还差：' + errs.join('、'), false);
    return;
  }
  const blob = new Blob([buildYAML(d)], { type: 'text/yaml;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${d.id}.yaml`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 0);
  flash(`已下载 ${d.id}.yaml ✓ 接下来按右侧 3 步上传到仓库`, true);
}

async function copy() {
  const d = collect();
  const errs = validate(d);
  if (errs.length) {
    flash('还差：' + errs.join('、'), false);
    return;
  }
  try {
    await navigator.clipboard.writeText(buildYAML(d));
    flash('配置内容已复制到剪贴板 ✓', true);
  } catch {
    flash('复制失败，请手动从右侧预览框选中复制', false);
  }
}

// ---- 初始化 ----------------------------------------------------------------
function main() {
  // 名称 → 自动生成 ID（用户未手动改过 ID 时）
  let idTouched = false;
  $('f-id').addEventListener('input', () => {
    idTouched = true;
    refresh();
  });
  $('f-name').addEventListener('input', () => {
    if (!idTouched) {
      const slug = slugify($('f-name').value);
      if (slug) $('f-id').value = slug;
    }
    refresh();
  });
  ['f-emoji', 'f-desc', 'f-judge', 'f-keywords', 'f-cats', 'f-exclude', 'f-venues', 'f-backfill'].forEach(
    (id) => $(id).addEventListener('input', refresh)
  );
  $('add-example').addEventListener('click', () => {
    addExampleRow();
    refresh();
  });
  $('btn-download').addEventListener('click', download);
  $('btn-copy').addEventListener('click', copy);
  $('theme-toggle')?.addEventListener('click', () => Theme.cycle());

  addExampleRow();
  refresh();
}

main();
