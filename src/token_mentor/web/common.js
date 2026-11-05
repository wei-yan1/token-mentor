const paths = {
  brand: '<path d="m12 2 9 5v10l-9 5-9-5V7Z"/><path d="m3 7 9 5 9-5M12 12v10M8 4.5v7l8 5"/>',
  'arrow-up-right': '<path d="M7 17 17 7M7 7h10v10"/>',
  'arrow-right': '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  'arrow-left': '<path d="M19 12H5m6-6-6 6 6 6"/>',
  scan: '<path d="M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M3 16v3a2 2 0 0 0 2 2h3m13-5v3a2 2 0 0 1-2 2h-3M7 9h10M7 13h10M7 17h6"/>',
  sparkles: '<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6ZM20 2v4m-2-2h4"/>',
  sliders: '<path d="M4 7h5m4 0h7M4 17h9m4 0h3"/><circle cx="11" cy="7" r="2"/><circle cx="15" cy="17" r="2"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8Zm-9 9 9 5 9-5m-18 5 9 5 9-5"/>',
  wallet: '<path d="M20 8V6a2 2 0 0 0-2-2H5a3 3 0 0 0 0 6h15v10H5a3 3 0 0 1-3-3V7m18 6h-5v4h5"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Zm-4 9 3 3 5-6"/>',
  play: '<path d="m9 5 10 7-10 7Z"/>',
  eye: '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  key: '<circle cx="8" cy="9" r="5"/><path d="m12 13 9 9m-4-4 3-3m-6 0 3-3"/>',
  'log-out': '<path d="M9 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h4m5-14 5 5-5 5m-7-5h12"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  x: '<path d="m6 6 12 12M6 18 18 6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5m-4-4a8 8 0 0 1 13-3l2 2M4 17l2 2a8 8 0 0 0 13-3"/>',
  'credit-card': '<rect x="2" y="4" width="20" height="16" rx="3"/><path d="M2 9h20M6 15h4"/>',
};

export function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.sparkles}</svg>`;
}
export function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });
}
export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
export async function api(path, options = {}) {
  const headers = {'X-Requested-With': 'TokenMentor', ...(options.body ? {'Content-Type': 'application/json'} : {}), ...options.headers};
  let response;
  try { response = await fetch(path, {...options, headers, credentials: 'same-origin'}); }
  catch { const error = new Error('网络连接中断。请检查连接后重试，同一请求不会重复扣次。'); error.status = 0; throw error; }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? '请检查输入格式、长度与必填项。' : data.detail;
    const error = new Error(detail || `请求暂未完成（${response.status}），请稍后重试。`);
    error.status = response.status;
    throw error;
  }
  return data;
}
export function message(id, text = '', success = false) {
  const node = document.getElementById(id);
  node.textContent = text;
  node.hidden = !text;
  node.classList.toggle('success', success);
}
let toastTimer;
export function toast(text) {
  const node = document.getElementById('toast');
  if (!node) return;
  node.textContent = text; node.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => {node.hidden = true;}, 4200);
}
export async function busy(button, label, action) {
  const original = button.innerHTML;
  button.disabled = true; button.textContent = label; button.setAttribute('aria-busy', 'true');
  try { return await action(); }
  finally { button.disabled = false; button.innerHTML = original; button.removeAttribute('aria-busy'); }
}
export function formatDate(value) {
  return value ? new Date(value).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}) : '—';
}
export function money(fen) { return `¥${(fen / 100).toFixed(2).replace(/\.00$/, '')}`; }
export function table(headers, rows) {
  const wrapper = el('div', 'table-wrap');
  const table = el('table'); const head = el('thead'); const tr = el('tr');
  headers.forEach(label => { const th = el('th', '', label); th.scope = 'col'; tr.append(th); });
  head.append(tr); table.append(head); const body = el('tbody');
  rows.forEach(cells => { const row = el('tr'); cells.forEach(value => { const cell = el('td'); cell.append(value instanceof Node ? value : document.createTextNode(String(value))); row.append(cell); }); body.append(row); });
  table.append(body); wrapper.append(table); return wrapper;
}
export function empty(title, description, link, label) {
  const node = el('div', 'empty-state'); const glyph = el('div'); glyph.innerHTML = icon('layers');
  node.append(glyph, el('h3', '', title), el('p', '', description));
  if (link) { const a = el('a', 'button small', label); a.href = link; node.append(a); }
  return node;
}
