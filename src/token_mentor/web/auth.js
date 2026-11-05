import {api, busy, hydrateIcons, message} from './common.js';
hydrateIcons();
const form = document.getElementById('auth-form');
let mode = 'login';
const $ = id => document.getElementById(id);
function setMode(next) {
  mode = next;
  const register = mode === 'register', email = mode === 'email';
  $('auth-title').textContent = register ? '创建你的工作台' : email ? 'QQ 邮箱认证' : '欢迎回来';
  $('auth-intro').textContent = register ? '绑定 QQ 邮箱，创建你的账号。' : email ? '使用已绑定的 QQ 邮箱验证码登录。' : '使用账号和密码，继续你的项目选型。';
  $('name-field').hidden = !register; $('name').required = register;
  $('username-field').hidden = !register; $('username').required = register; $('username').disabled = !register;
  $('account-field').hidden = register || email; $('account').required = !register && !email; $('account').disabled = register || email;
  $('email-field').hidden = !register && !email; $('email').required = register || email; $('email').disabled = !register && !email;
  $('password-field').hidden = email; $('password').required = !email; $('password').disabled = email;
  $('password').minLength = register ? 10 : 1;
  $('password').autocomplete = register ? 'new-password' : 'current-password';
  $('password-hint').hidden = !register;
  $('code-field').hidden = !register && !email; $('email-code').required = register || email; $('email-code').disabled = !register && !email;
  $('auth-submit').textContent = register ? '认证邮箱并创建账户' : '登录工作台';
  $('auth-hint').textContent = register ? '新账户从零次数开始；登录后可创建调用服务的 API Key。' : 'API Key 仅用于调用服务，不用于网页登录。';
  $('email-mode').textContent = email ? '使用账号密码登录' : '使用 QQ 邮箱验证码登录';
  const selected = register ? 'register' : 'login';
  document.querySelectorAll('[data-mode]').forEach(button => { button.setAttribute('aria-selected', String(button.dataset.mode === selected)); button.tabIndex = button.dataset.mode === selected ? 0 : -1; });
  message('auth-error');
}
document.querySelectorAll('[data-mode]').forEach(button => {
  button.addEventListener('click', () => setMode(button.dataset.mode));
  button.addEventListener('keydown', event => {
    if (['ArrowLeft','ArrowRight'].includes(event.key)) {
      event.preventDefault(); const next = mode === 'register' ? 'login' : 'register';
      setMode(next); document.querySelector(`[data-mode="${next}"]`).focus();
    }
  });
});
$('email-mode').addEventListener('click', () => setMode(mode === 'email' ? 'login' : 'email'));
let countdown;
$('send-code').addEventListener('click', async () => {
  message('auth-error');
  if (!$('email').reportValidity()) return;
  try {
    await busy($('send-code'), '发送中…', () => api('/v1/auth/email-code', {method:'POST',body:JSON.stringify({email:$('email').value.trim(),purpose:mode==='register'?'register':'login'})}));
    $('code-status').textContent='验证码已发送至 QQ 邮箱，10 分钟内有效。';
    let seconds=60; clearInterval(countdown); $('send-code').disabled=true; $('send-code').textContent=`${seconds} 秒后重发`;
    countdown=setInterval(()=>{seconds--;$('send-code').textContent=`${seconds} 秒后重发`;if(seconds<=0){clearInterval(countdown);$('send-code').disabled=false;$('send-code').textContent='发送验证码';}},1000);
  } catch(error) { message('auth-error',error.message); }
});
$('password-toggle').addEventListener('click', () => {
  const show = $('password').type === 'password'; $('password').type = show ? 'text' : 'password';
  $('password-toggle').setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
  $('password-toggle').setAttribute('aria-pressed', String(show));
});
form.addEventListener('submit', async event => {
  event.preventDefault(); message('auth-error');
  const data = mode === 'login' ? {account:$('account').value.trim(),password:$('password').value} : {email:$('email').value.trim(),code:$('email-code').value.trim()};
  if (mode === 'register') Object.assign(data,{name:$('name').value.trim(),username:$('username').value.trim(),password:$('password').value});
  try {
    await busy($('auth-submit'), mode === 'register' ? '正在创建账户…' : '正在登录…', () => api(`/v1/auth/${mode === 'email' ? 'email-login' : mode}`, {method: 'POST', body: JSON.stringify(data)}));
    form.reset(); location.replace('/app');
  } catch (error) { message('auth-error', error.message); }
});
setMode(new URLSearchParams(location.search).get('mode') === 'register' ? 'register' : 'login');
api('/v1/auth/me').then(() => location.replace('/app')).catch(() => {});
