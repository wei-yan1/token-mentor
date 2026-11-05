import {api, busy, el, empty, formatDate, hydrateIcons, icon, message, money, table, toast} from './common.js';

hydrateIcons();
const $ = id => document.getElementById(id);
const state = {me: null, projects: [], balance: 0, pack: null, usage: [], keys: [], orders: [], project: '', analysis: 'profile', report: null, order: null};
const tasks = {general:'通用问答', coding:'编码与代码分析', reasoning:'规划与推理', summarization:'摘要与报告', translation:'翻译与多语言', rewrite:'改写与文案', embeddings:'文本向量化', reranking:'检索重排', speech_recognition:'语音识别', speech_generation:'语音合成', image_generation:'图像生成', multimodal:'多模态理解', video_generation:'视频生成', video_understanding:'视频理解', '3d_generation':'3D 内容生成', self_hosted:'本地私有化部署'};
const views = {overview:['你的项目工作台','把每个任务，交给合适的模型。'], recommend:['为你的项目，选对模型','从真实任务出发，权衡能力与调用成本。'], projects:['你的项目，各自清晰','一个账户，管理不同项目的用途与接入。'], keys:['让项目，连接起来','独立签发、按项目授权，随时管理密钥。'], billing:['每次使用，都有记录','查看调用状态，按需补充推荐次数。']};
const statusNames = {completed:'已完成',processing:'执行中',failed:'已退回',pending:'待支付',paid:'已到账'};
const operationNames = {profile:'任务画像推荐',recommend:'云端用途推荐',comparison:'模型对比'};
let paymentTimer;

function errorToUser(error) {
  if (error.status === 401) { location.replace('/login'); return; }
  message('global-error', error.message);
}
function projectHeaders() { return {'X-Project-ID': state.project}; }
function currentProject() { return state.projects.find(p => p.project_id === state.project); }
function saveProject() { sessionStorage.setItem(`tm_project_${state.me.account_id}`, state.project); }
function chooseProject(project) {
  state.project = project; saveProject(); renderProjectSelector(); renderProjects();
  $('report-panel').hidden = true; state.report = null;
}
function renderProjectSelector() {
  const select = $('active-project'); select.replaceChildren();
  if (!state.projects.length) { const option = el('option','','还没有项目，请先创建'); option.value = ''; select.append(option); }
  state.projects.forEach(project => { const option = el('option','',project.name); option.value = project.project_id; select.append(option); });
  select.value = state.project;
  $('recommend-submit').disabled = !state.project;
  $('recommend-note').textContent = state.project ? `将为「${currentProject()?.name || '当前项目'}」生成推荐，可用 ${state.balance} 次。` : '请先在项目管理中创建一个项目。';
}
function badge(value) { return el('span', `badge ${['completed','paid'].includes(value) ? 'success' : ''}`, statusNames[value] || value); }
function renderUsage(target, rows, recent = false) {
  const root = $(target); root.replaceChildren();
  if (!rows.length) { root.append(empty('从第一份推荐开始','生成一份推荐后，你可以在这里查看项目归属与执行状态。','#recommend','开始模型推荐')); return; }
  root.append(table(['调用任务','所属项目','状态','时间',...(recent ? [] : ['操作'])],rows.map(item => {
    const project = state.projects.find(p => p.project_id === item.project_id);
    const cells = [operationNames[item.operation] || item.operation, project?.name || item.project_id.slice(0,8), badge(item.status), formatDate(item.created_at)];
    if (!recent) {
      const action = el('span','muted','—');
      if (item.status === 'processing') {
        const button = el('button','button small quiet','释放中断预扣'); button.type = 'button';
        button.addEventListener('click', async () => {
          try { await busy(button,'正在处理…',() => api(`/v1/billing/requests/${item.request_id}/cancel`,{method:'POST'})); await refreshData(); }
          catch(error) { toast(error.message); }
        }); cells.push(button);
      } else cells.push(action);
    }
    return cells;
  })));
}
function renderStats() {
  $('credit-count').textContent = state.balance;
  $('project-count').textContent = state.projects.length;
  $('billing-balance').textContent = `可用 ${state.balance} 次`;
  if (state.pack) {
    $('pack-price').textContent = money(state.pack.amount_fen);
    $('pack-unit').textContent = `/ ${state.pack.credits} 次`;
    $('offer-title').textContent = `${state.pack.credits} 次推荐包`;
    $('offer-price').replaceChildren(document.createTextNode(money(state.pack.amount_fen)),el('small','',`/ ${state.pack.credits} 次`));
    $('offer-description').textContent = '支付核验成功后到账，所有项目共享。';
  }
}
function renderProjects() {
  const root = $('projects-grid'); root.replaceChildren();
  if (!state.projects.length) { root.append(empty('你的第一个项目，等你命名','建立项目后，就能生成推荐和签发项目密钥。')); return; }
  state.projects.forEach(project => {
    const card = el('article',`glass project-tile ${project.project_id === state.project ? 'selected' : ''}`);
    const glyph = el('span','feature-icon'); glyph.innerHTML = icon('layers');
    card.append(glyph,el('h3','',project.name),el('p','',`项目 ID：${project.project_id}`),el('p','',`创建于 ${formatDate(project.created_at)}`));
    const button = el('button',`button small ${project.project_id === state.project ? '' : 'primary'}`,project.project_id === state.project ? '当前项目' : '切换到此项目'); button.type = 'button';
    button.disabled = project.project_id === state.project;
    button.addEventListener('click', () => { chooseProject(project.project_id); location.hash = 'recommend'; });
    card.append(button); root.append(card);
  });
}
function renderKeys() {
  const root = $('keys-content'); root.replaceChildren();
  if (!state.me.can_manage) { root.append(empty('当前以项目 Key 登录','账户级 Key 或邮箱登录可管理密钥；当前 Key 仍可生成绑定项目的推荐。')); return; }
  if (!state.keys.length) { root.append(empty('还没有 API Key','在这里为项目创建密钥，把模型选型接入你的工作流。')); return; }
  root.append(table(['Key ID','归属','状态','创建时间','操作'],state.keys.map(key => {
    const project = state.projects.find(p => p.project_id === key.project_id);
    const revoke = el('button','button small quiet',key.revoked ? '已撤销' : '撤销'); revoke.type = 'button'; revoke.disabled = Boolean(key.revoked);
    revoke.addEventListener('click', async () => {
      try { await busy(revoke,'正在撤销…',() => api(`/v1/keys/${key.key_id}`,{method:'DELETE'})); state.keys = await api('/v1/keys'); renderKeys(); }
      catch(error) { errorToUser(error); }
    });
    return [el('code','',`${key.key_id.slice(0,12)}…`), project?.name || (key.project_id ? '项目密钥' : '账户密钥'),el('span','badge',key.revoked ? '已撤销' : '使用中'),formatDate(key.created_at),revoke];
  })));
}
function renderOrders() {
  const root = $('orders-content'); root.replaceChildren();
  if (!state.me.can_manage) { root.append(empty('请使用账户登录充值','项目 Key 可查看余额与用量，支付订单由所属账户管理。')); return; }
  if (!state.orders.length) { root.append(empty('还没有充值订单','按需购买次数包，订单与到账状态会保存在这里。')); return; }
  root.append(table(['订单','次数包','金额','状态','操作'],state.orders.map(order => {
    const button = el('button','button small quiet',order.status === 'paid' ? '已到账' : '继续支付'); button.type = 'button'; button.disabled = order.status === 'paid';
    button.addEventListener('click', () => startCheckout(order));
    return [el('code','',order.order_id.slice(0,12)),`${order.credits} 次`,money(order.amount_fen),badge(order.status),button];
  })));
}
async function refreshData() {
  const [projects,balance,pack,usage,keys,orders] = await Promise.all([
    api('/v1/projects'),api('/v1/billing/balance'),api('/v1/billing/pack'),api('/v1/billing/usage'),
    state.me.can_manage ? api('/v1/keys') : Promise.resolve([]),state.me.can_manage ? api('/v1/billing/orders') : Promise.resolve([])
  ]);
  Object.assign(state,{projects,balance:balance.credits,pack,usage,keys,orders});
  if (!projects.some(project => project.project_id === state.project)) state.project = projects[0]?.project_id || '';
  saveProject(); renderStats(); renderProjectSelector(); renderProjects(); renderKeys(); renderOrders();
  renderUsage('recent-usage',usage.slice(0,5),true); renderUsage('usage-content',usage);
}
function route() {
  const view = Object.hasOwn(views,location.hash.slice(1)) ? location.hash.slice(1) : 'overview';
  Object.keys(views).forEach(name => { $(`view-${name}`).hidden = name !== view; });
  document.querySelectorAll('[data-view]').forEach(link => { if(link.dataset.view === view) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current'); });
  $('view-title').textContent = views[view][0]; $('view-description').textContent = views[view][1];
  document.title = `${views[view][0]} · Token Mentor`;
  message('global-error');
}
function showSecret(secret) { $('new-secret').textContent = secret; $('secret-dialog').showModal(); }
function sessionId() {
  const key = `tm_conversation_${state.me.account_id}_${state.project}`;
  let id = sessionStorage.getItem(key); if (!id) {id = crypto.randomUUID(); sessionStorage.setItem(key,id);} return id;
}
async function bill(path,payload) {
  const material = JSON.stringify({path,payload,project:state.project});
  const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(material)))].map(n=>n.toString(16).padStart(2,'0')).join('');
  const slot = `tm_pending_${state.me.account_id}_${state.project}`;
  let pending;
  try { pending = JSON.parse(sessionStorage.getItem(slot)); } catch { pending = null; }
  if (!pending || pending.hash !== hash) { pending = {hash,key:crypto.randomUUID()}; sessionStorage.setItem(slot,JSON.stringify(pending)); }
  const result = await api(path,{method:'POST',headers:{...projectHeaders(),'Idempotency-Key':pending.key},body:JSON.stringify(payload)});
  sessionStorage.removeItem(slot); return result;
}
function inlineText(node,text) {
  text.split(/(\*\*[^*]+\*\*)/g).forEach(part => node.append(part.startsWith('**') && part.endsWith('**') ? el('strong','',part.slice(2,-2)) : document.createTextNode(part)));
}
function renderReport(markdown) {
  const root = $('report-content'); root.replaceChildren();
  const lines = markdown.split('\n');
  for(let index=0; index<lines.length; index++) {
    const line = lines[index].trim(); if(!line) continue;
    if(line === '## 反馈与测评') break;
    if(line.startsWith('|') && lines[index+1]?.match(/^\|[\s:|\-]+\|$/)) {
      const split = row => row.split('|').slice(1,-1).map(cell=>cell.trim());
      const headers = split(line); index += 2; const rows = [];
      while(index<lines.length && lines[index].trim().startsWith('|')) {rows.push(split(lines[index])); index++;}
      index--; root.append(table(headers,rows.map(row=>row.map(text=>{const span=el('span');inlineText(span,text);return span;})))); continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.*)/);
    const node = el(heading ? `h${heading[1].length}` : 'p');
    inlineText(node,heading ? heading[2] : line.replace(/^>\s*/,'')); root.append(node);
  }
}
async function startCheckout(order) {
  state.order = order; $('payment-qr').hidden = true; $('payment-link').hidden = true;
  $('payment-description').textContent = `正在准备 ${money(order.amount_fen)} / ${order.credits} 次的支付二维码…`;
  message('payment-error'); $('check-payment').disabled = false;
  if (!$('payment-dialog').open) $('payment-dialog').showModal();
  try {
    const result = await api(`/v1/billing/orders/${order.order_id}/checkout`,{method:'POST'});
    if (result.status === 'paid') { await paymentComplete(); return; }
    const img = new Image(); img.alt = `支付宝支付二维码，金额 ${money(result.amount_fen)}`;
    img.src = `/v1/billing/orders/${order.order_id}/qr.svg`; $('payment-qr').replaceChildren(img); $('payment-qr').hidden = false;
    $('payment-description').textContent = `使用支付宝扫码支付 ${money(result.amount_fen)}，到账 ${result.credits} 次。`;
    try {const url=new URL(result.qr_code); if(url.protocol==='https:' && (url.hostname==='alipay.com'||url.hostname.endsWith('.alipay.com'))) {$('payment-link').href=url.href;$('payment-link').hidden=false;}} catch { /* QR remains available for supported payment links. */ }
    clearInterval(paymentTimer); paymentTimer=setInterval(async()=>{
      if(!$('payment-dialog').open) {clearInterval(paymentTimer);return;}
      try {const status=await api(`/v1/billing/orders/${order.order_id}`);if(status.status==='paid') await paymentComplete();} catch {clearInterval(paymentTimer);}
    },5000);
  } catch(error) { $('payment-description').textContent = '订单已保存，可在充值订单中继续支付。'; message('payment-error',error.message); }
  await refreshData().catch(errorToUser);
}
async function paymentComplete() {
  clearInterval(paymentTimer); $('payment-qr').hidden = true; $('payment-link').hidden = true;
  $('payment-description').textContent = '支付已核验，推荐次数已到账。'; $('check-payment').disabled = true;
  message('payment-error'); await refreshData();
}

document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',() => $(button.dataset.close).close()));
$('secret-dialog').addEventListener('close',()=>{$('new-secret').textContent='';});
$('payment-dialog').addEventListener('close',()=>{clearInterval(paymentTimer);});
$('active-project').addEventListener('change',event=>chooseProject(event.target.value));
window.addEventListener('hashchange',route);
$('logout').addEventListener('click',async()=>{try {await api('/v1/auth/logout',{method:'POST'});sessionStorage.clear();location.replace('/login');}catch(error){errorToUser(error);}});
$('create-project').addEventListener('click',()=>{message('project-error');$('project-form').reset();$('project-dialog').showModal();});
$('project-form').addEventListener('submit',async event=>{
  event.preventDefault(); message('project-error');
  try {const result=await busy($('project-submit'),'正在创建…',()=>api('/v1/projects',{method:'POST',body:JSON.stringify({name:$('project-name').value.trim()})}));state.project=result.project_id;$('project-dialog').close();await refreshData();showSecret(result.api_key);}catch(error){message('project-error',error.message);}
});
$('issue-key').addEventListener('click',()=>{
  message('key-error');const select=$('key-project');select.replaceChildren();const account=el('option','','账户级 Key');account.value='';select.append(account);
  state.projects.forEach(project=>{const option=el('option','',project.name);option.value=project.project_id;select.append(option);});select.value=state.project;$('key-dialog').showModal();
});
$('key-form').addEventListener('submit',async event=>{
  event.preventDefault();message('key-error');
  try {const result=await busy($('key-submit'),'正在创建…',()=>api('/v1/keys',{method:'POST',body:JSON.stringify({project_id:$('key-project').value||null})}));$('key-dialog').close();showSecret(result.api_key);await refreshData();}catch(error){message('key-error',error.message);}
});
$('copy-secret').addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('new-secret').textContent);toast('API Key 已复制。');}catch{toast('无法自动复制，请选中密钥后手动复制。');}});
Object.entries(tasks).forEach(([value,title])=>{const label=el('label','task-option');const input=el('input');input.type='checkbox';input.name='task';input.value=value;input.checked=value==='general';label.append(input,document.createTextNode(title));$('task-options').append(label);});
document.querySelectorAll('[data-analysis]').forEach(button=>button.addEventListener('click',()=>{
  state.analysis=button.dataset.analysis;document.querySelectorAll('[data-analysis]').forEach(tab=>tab.setAttribute('aria-selected',String(tab===button)));
  $('profile-fields').hidden=state.analysis!=='profile';$('cloud-fields').hidden=state.analysis!=='cloud';$('intent').required=state.analysis==='cloud';$('content-consent').required=state.analysis==='cloud';message('recommend-error');
}));
$('profile-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  try{
    if(file.size>32000)throw new Error('画像文件应小于 32 KB，只包含任务类别和技术栈。');
    const profile=JSON.parse(await file.text());
    if(!Array.isArray(profile.tasks)||!profile.tasks.length||profile.tasks.some(task=>!Object.hasOwn(tasks,task)))throw new Error('画像中需要有效的 tasks 任务列表。');
    if(Object.keys(profile).some(key=>!['tasks','technologies','input_tokens','output_tokens','weights'].includes(key)))throw new Error('请仅导入本地 profile 命令生成的任务画像，不要包含源码或业务描述。');
    if(profile.technologies && (!Array.isArray(profile.technologies)||profile.technologies.some(item=>typeof item!=='string')))throw new Error('technologies 应为技术名称列表。');
    document.querySelectorAll('[name="task"]').forEach(input=>{input.checked=profile.tasks.includes(input.value);});$('technologies').value=(profile.technologies||[]).join(', ');
    if(profile.input_tokens!==undefined)$('input-tokens').value=profile.input_tokens;
    if(profile.output_tokens!==undefined)$('output-tokens').value=profile.output_tokens;
    if(profile.weights)Object.keys(profile.weights).forEach(key=>{const input=$(`weight-${key}`);if(input)input.value=profile.weights[key];});
    message('recommend-error','本地任务画像已填入。文件本身不会上传。',true);
  }catch(error){message('recommend-error',error.message);}
  event.target.value='';
});
$('recommend-form').addEventListener('submit',async event=>{
  event.preventDefault();message('recommend-error');if(!state.project){message('recommend-error','请先创建或选择项目。');return;}
  const payload={technologies:$('technologies').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean),input_tokens:Number($('input-tokens').value),output_tokens:Number($('output-tokens').value),weights:Object.fromEntries(['cost','quality','stability','speed'].map(key=>[key,Number($(`weight-${key}`).value)]))};
  let path='/v1/recommend/profile';
  if(state.analysis==='cloud'){
    if(!$('content-consent').checked){message('recommend-error','请先同意本次云端分析，或切换到本地任务画像。');return;}
    path='/v1/recommend';Object.assign(payload,{intent:$('intent').value.trim(),allow_project_content:true});
    if($('use-memory').checked){payload.user_id='web';payload.session_id=sessionId();}
  }else{payload.tasks=[...document.querySelectorAll('[name="task"]:checked')].map(input=>input.value);if(!payload.tasks.length||payload.tasks.length>12){message('recommend-error','请选择 1 到 12 种任务。');return;}}
  try{
    const result=await busy($('recommend-submit'),'正在分析并生成推荐…',()=>bill(path,payload));
    state.report=result;renderReport(result.markdown);message('feedback-message');$('feedback-form').reset();$('report-meta').textContent=`报告 ${result.report_id} · 本次成功调用计 1 次`;$('report-panel').hidden=false;
    await refreshData();$('report-panel').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
  }catch(error){if(error.status===401)errorToUser(error);else message('recommend-error',`${error.message}${error.status===402?' 可在“用量与充值”购买次数包。':''}`);await refreshData().catch(()=>{});}
});
$('clear-memory').addEventListener('click',async()=>{if(!state.project){toast('请先选择项目。');return;}try{await api(`/v1/conversations/web/${sessionId()}`,{method:'DELETE',headers:projectHeaders()});sessionStorage.removeItem(`tm_conversation_${state.me.account_id}_${state.project}`);toast('本项目短期记忆已清除。');}catch(error){toast(error.message);}});
$('download-report').addEventListener('click',()=>{if(!state.report)return;const url=URL.createObjectURL(new Blob([state.report.markdown],{type:'text/markdown;charset=utf-8'}));const a=el('a');a.href=url;a.download=`token-mentor-${state.report.report_id}.md`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
$('feedback-form').addEventListener('submit',async event=>{event.preventDefault();if(!state.report)return;message('feedback-message');try{await busy($('feedback-submit'),'正在记录…',()=>api('/v1/feedback',{method:'POST',headers:projectHeaders(),body:JSON.stringify({report_id:state.report.report_id,rating:Number($('feedback-rating').value),note:$('feedback-note').value.trim()})}));message('feedback-message','评价已记录，将用于改进推荐。',true);}catch(error){message('feedback-message',error.message);}});
$('buy-pack').addEventListener('click',async()=>{try{const order=await busy($('buy-pack'),'正在创建订单…',()=>api('/v1/billing/orders',{method:'POST'}));await startCheckout(order);}catch(error){errorToUser(error);}});
$('check-payment').addEventListener('click',async()=>{if(!state.order)return;message('payment-error');try{const result=await busy($('check-payment'),'正在核对…',()=>api(`/v1/billing/orders/${state.order.order_id}/reconcile`,{method:'POST'}));if(result.status==='paid')await paymentComplete();else message('payment-error','暂未查到支付成功，请完成付款后再核对。');}catch(error){message('payment-error',error.message);}});
$('refresh-orders').addEventListener('click',()=>busy($('refresh-orders'),'正在刷新…',refreshData).catch(errorToUser));
$('refresh-usage').addEventListener('click',()=>busy($('refresh-usage'),'正在刷新…',refreshData).catch(errorToUser));

async function init(){
  try{
    state.me=await api('/v1/auth/me');$('account-name').textContent=state.me.name;$('avatar').textContent=state.me.name.slice(0,1).toUpperCase();
    state.project=sessionStorage.getItem(`tm_project_${state.me.account_id}`)||state.me.project_id||'';
    ['create-project','issue-key','buy-pack'].forEach(id=>{$(id).disabled=!state.me.can_manage;});
    await refreshData();$('main').setAttribute('aria-busy','false');route();
  }catch(error){errorToUser(error);$('main').setAttribute('aria-busy','false');}
}
route();init();
