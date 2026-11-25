/* 前端交互：API 调用、表单校验、Toast、权重重算刷新 */

const toastHost = () => {
  let h = document.getElementById('toast-host');
  if (!h) { h = document.createElement('div'); h.id = 'toast-host'; document.body.appendChild(h); }
  return h;
};

function toast(msg, type = 'ok') {
  const el = document.createElement('div');
  el.className = 'mvs-toast' + (type === 'error' ? ' error' : '');
  el.innerHTML = '<span class="dot"></span><span></span>';
  el.querySelector('span:last-child').textContent = msg;
  toastHost().appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 200ms'; setTimeout(() => el.remove(), 220); }, 2500);
}

async function api(path, options = {}) {
  const cfg = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (cfg.body && typeof cfg.body !== 'string') cfg.body = JSON.stringify(cfg.body);
  const resp = await fetch(path, cfg);
  let data;
  try { data = await resp.json(); } catch { data = { ok: false, error: '服务器返回格式错误' }; }
  if (!resp.ok || data.ok === false) throw new Error(data.error || `请求失败 ${resp.status}`);
  return data;
}

function btnLoading(btn, loading) {
  if (!btn) return;
  if (loading) { btn.dataset.text = btn.textContent; btn.textContent = '处理中…'; btn.disabled = true; }
  else { if (btn.dataset.text) btn.textContent = btn.dataset.text; btn.disabled = false; }
}

/* ---------- 作品录入 ---------- */

function clearErrors(form) {
  form.querySelectorAll('.mvs-input-group.invalid').forEach(g => g.classList.remove('invalid'));
}

function validateWork(form) {
  clearErrors(form);
  const required = ['script', 'views', 'comments', 'shares', 'favs', 'finish', 'jump2'];
  let first = null;
  required.forEach(name => {
    const input = form.querySelector(`[name="${name}"]`);
    if (!input || !String(input.value).trim()) {
      input?.closest('.mvs-input-group')?.classList.add('invalid');
      if (!first) first = input;
    }
  });
  if (first) { first.focus(); toast('请填写所有必填项', 'error'); return false; }
  return true;
}

async function submitWork(form, btn) {
  if (!validateWork(form)) return;
  const fd = new FormData(form);
  const payload = Object.fromEntries(fd.entries());
  btnLoading(btn, true);
  try {
    const res = await api('/api/works', { method: 'POST', body: payload });
    toast(`已录入，权重已自动重算（${res.sample_size} 条样本）`);
    setTimeout(() => location.reload(), 700);
  } catch (e) { toast(e.message, 'error'); btnLoading(btn, false); }
}

async function deleteWork(id, btn) {
  if (!confirm('确认删除这条作品？删除后权重会自动重算。')) return;
  btnLoading(btn, true);
  try { await api(`/api/works/${id}`, { method: 'DELETE' }); toast('已删除，权重已重算'); setTimeout(() => location.reload(), 600); }
  catch (e) { toast(e.message, 'error'); btnLoading(btn, false); }
}

/* ---------- 候选操作 ---------- */

async function setCandidateStatus(id, status, btn) {
  btnLoading(btn, true);
  try { await api(`/api/candidates/${id}/status`, { method: 'POST', body: { status } }); toast('状态已更新'); setTimeout(() => location.reload(), 500); }
  catch (e) { toast(e.message, 'error'); btnLoading(btn, false); }
}

/* ---------- 手动操作 ---------- */

async function trigger(path, btn, successMsg) {
  btnLoading(btn, true);
  try {
    const res = await api(path, { method: 'POST', body: {} });
    if (res.error) { toast(res.error, 'error'); btnLoading(btn, false); return res; }
    toast(successMsg || '完成');
    btnLoading(btn, false);
    return res;
  } catch (e) { toast(e.message, 'error'); btnLoading(btn, false); return null; }
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-action]').forEach(el => {
    el.addEventListener('click', () => {
      const a = el.dataset.action;
      if (a === 'generate') trigger('/api/generate', el, '候选已生成').then(r => { if (r?.candidates) setTimeout(() => location.reload(), 800); });
      else if (a === 'regenerate') {
        if (!confirm('清掉今日草稿候选并重新生成？已采用或弃用的会保留。')) return;
        btnLoading(el, true);
        api('/api/generate', { method: 'POST', body: { force: true } })
          .then(() => { toast('已重新生成'); setTimeout(() => location.reload(), 800); })
          .catch(e => { toast(e.message, 'error'); btnLoading(el, false); });
      }
      else if (a === 'crawl') trigger('/api/crawl', el, '热点抓取完成').then(r => { if (r) setTimeout(() => location.reload(), 800); });
      else if (a === 'recompute') trigger('/api/recompute', el, '权重已重算').then(() => setTimeout(() => location.reload(), 600));
      else if (a === 'send') trigger('/api/send', el);
      else if (a === 'daily') trigger('/api/daily', el, '每日流程已执行').then(() => setTimeout(() => location.reload(), 800));
    });
  });
});
