// 首页会议投稿倒计时组件。读 data/conferences.json，前端实时算倒计时：
//   - 收起态：只显示最近 2-3 个临近截止的小药丸（不抢下方帖子版面）。
//   - 「全部会议 ▾」展开一个面板，列出所有会议：截止倒计时 + 开会时间地点 + 主页。
//   - 点会议名 → 跳到该会议的论文（index.html?venue=<基名>，feed.js 会按 venue 筛选）。
import { escapeHTML, fetchJSON } from './utils.js?v=484e3a77';

const DAY = 86400000;

function todayMidnight() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

// 计算下一个未过的截止日。deadline_exact（YYYY-MM-DD）在未来则用它（已确认）；
// 否则用 deadline（MM-DD）滚到下一个未过的年份（预计）。返回 {date, predicted} 或 null。
function nextDeadline(conf) {
  const now = todayMidnight();
  if (conf.deadline_exact) {
    const d = new Date(`${conf.deadline_exact}T00:00:00`);
    if (!Number.isNaN(d.getTime()) && d >= now) return { date: d, predicted: false };
  }
  if (conf.deadline && /^\d{2}-\d{2}$/.test(conf.deadline)) {
    const [mm, dd] = conf.deadline.split('-').map(Number);
    let y = now.getFullYear();
    let d = new Date(y, mm - 1, dd);
    if (d < now) d = new Date(y + 1, mm - 1, dd);
    return { date: d, predicted: true };
  }
  // 只有已过的 exact（双年会过期）：返回该日期但标记已过
  if (conf.deadline_exact) {
    const d = new Date(`${conf.deadline_exact}T00:00:00`);
    if (!Number.isNaN(d.getTime())) return { date: d, predicted: false };
  }
  return null;
}

function daysLeft(date) {
  return Math.ceil((date - todayMidnight()) / DAY);
}

function _pad2(n) { return String(n).padStart(2, '0'); }

// 实时倒计时文本，按「天 小时 分 秒」展示。
function fmtCountdown(date) {
  let ms = date.getTime() - Date.now();
  if (ms <= 0) return '已截止';
  const d = Math.floor(ms / 86400000); ms -= d * 86400000;
  const h = Math.floor(ms / 3600000); ms -= h * 3600000;
  const m = Math.floor(ms / 60000); ms -= m * 60000;
  const s = Math.floor(ms / 1000);
  if (d > 0) return `${d}天 ${h}小时 ${_pad2(m)}:${_pad2(s)}`;
  if (h > 0) return `${h}小时 ${_pad2(m)}:${_pad2(s)}`;
  return `${_pad2(m)}:${_pad2(s)}`;
}

function urgencyClass(days) {
  if (days < 0) return 'is-passed';
  if (days <= 7) return 'is-hot';
  if (days <= 30) return 'is-soon';
  return '';
}

function fmtDate(d) {
  return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}`;
}

function venueHref(venue) {
  return `index.html?venue=${encodeURIComponent(venue || '')}`;
}

function pillHTML(c) {
  const cls = urgencyClass(c._days);
  return `<a class="rp-conf__pill ${cls}" href="${venueHref(c.venue || c.name)}" data-ts="${c._date.getTime()}" title="${escapeHTML(c.full || c.name)} · 截止 ${fmtDate(c._date)}${c._predicted ? '（预计）' : ''}">
    <b>${escapeHTML(c.name)}</b><span class="rp-conf__cd">${fmtCountdown(c._date)}</span></a>`;
}

function rowHTML(c) {
  const cls = c._has ? urgencyClass(c._days) : '';
  const ddl = c._has
    ? `<span class="rp-conf__ddl ${cls}" data-ts="${c._date.getTime()}"><span class="rp-conf__cd">${fmtCountdown(c._date)}</span> · 截止 ${fmtDate(c._date)}${c._predicted ? ' 预计' : ''}</span>`
    : '<span class="rp-conf__ddl">—</span>';
  return `<div class="rp-conf__row">
    <a class="rp-conf__name" href="${venueHref(c.venue || c.name)}" title="查看 ${escapeHTML(c.name)} 收录的论文">${escapeHTML(c.name)}</a>
    ${ddl}
    <span class="rp-conf__when">${escapeHTML(c.conf || '')}</span>
    ${c.homepage ? `<a class="rp-conf__home" href="${escapeHTML(c.homepage)}" target="_blank" rel="noopener">主页 ↗</a>` : ''}
  </div>`;
}

// 每秒刷新所有带 data-ts 的倒计时文本（药丸 + 面板行），让用户看到时间流逝。
function startTicking() {
  const tick = () => {
    document.querySelectorAll('#conf-countdown [data-ts]').forEach((el) => {
      const cd = el.querySelector('.rp-conf__cd');
      if (cd) cd.textContent = fmtCountdown(new Date(Number(el.getAttribute('data-ts'))));
    });
  };
  tick();
  setInterval(tick, 1000);
}

async function main() {
  const root = document.getElementById('conf-countdown');
  if (!root) return;
  let data = null;
  try {
    data = await fetchJSON('data/conferences.json').then((r) => r.json());
  } catch (_) {
    return;
  }
  const confs = (data && data.conferences) || [];
  if (!confs.length) return;

  // 算每个会议的下一个截止
  for (const c of confs) {
    const nd = nextDeadline(c);
    c._has = !!nd;
    if (nd) {
      c._date = nd.date;
      c._predicted = nd.predicted;
      c._days = daysLeft(nd.date);
    }
  }
  // 收起药丸：取「未过 + 最近」的前 3
  const upcoming = confs.filter((c) => c._has && c._days >= 0).sort((a, b) => a._days - b._days);
  const pills = upcoming.slice(0, 3);
  document.getElementById('conf-pills').innerHTML = pills.map(pillHTML).join('') ||
    '<span class="rp-conf__empty">暂无临近截止</span>';

  // 全部面板：未过的按倒计时升序，再接已过/无日期的
  const ordered = [
    ...upcoming,
    ...confs.filter((c) => !(c._has && c._days >= 0)),
  ];
  document.getElementById('conf-panel').innerHTML = ordered.map(rowHTML).join('');

  const moreBtn = document.getElementById('conf-more');
  const panel = document.getElementById('conf-panel');
  moreBtn?.addEventListener('click', () => {
    const open = panel.hasAttribute('hidden');
    if (open) panel.removeAttribute('hidden');
    else panel.setAttribute('hidden', '');
    moreBtn.setAttribute('aria-expanded', String(open));
    moreBtn.textContent = open ? '收起 ▴' : '全部会议 ▾';
  });

  root.hidden = false;
  startTicking();
}

main();
