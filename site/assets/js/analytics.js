// 访问统计（GoatCounter，免费/隐私友好/无 cookie）。只有 config/site.yaml 里填了
// goatcounter code 才启用：注入官方 count.js 上报本次访问，并在页脚显示累计访问数。
// 每日明细在 GoatCounter 自己的面板看（https://<code>.goatcounter.com）。
import { fetchJSON } from './utils.js?v=2fa2b768';

async function main() {
  let code = '';
  try {
    const site = await fetchJSON('data/site.json').then((r) => r.json());
    code = (site && site.goatcounter || '').trim();
  } catch (_) { /* no site.json / not configured */ }
  if (!code) return;

  const endpoint = `https://${code}.goatcounter.com/count`;

  // 1) 上报本次访问（官方脚本，按当前路径自动计 pageview）
  const s = document.createElement('script');
  s.async = true;
  s.src = '//gc.zgo.at/count.js';
  s.setAttribute('data-goatcounter', endpoint);
  document.body.appendChild(s);

  // 2) 页脚显示累计访问数（公开 counter 接口，无需登录）
  try {
    const r = await fetch(`https://${code}.goatcounter.com/counter/TOTAL.json`, { mode: 'cors' });
    if (r.ok) {
      const j = await r.json();
      const n = (j && (j.count_unique || j.count)) || '';
      const footer = document.querySelector('.rp-footer p');
      if (footer && n !== '') {
        const span = document.createElement('span');
        span.className = 'rp-visits';
        span.textContent = ` · 👀 访问 ${n}`;
        footer.appendChild(span);
      }
    }
  } catch (_) { /* counter not public / offline — 忽略，面板仍可看 */ }
}

main();
