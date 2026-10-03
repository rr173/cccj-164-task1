"""租约控制台页面（单页，原生 JS，直接访问 JSON API）。"""

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>配额租约控制台</title>
<style>
  :root { --bg:#0f1420; --card:#1a2233; --line:#2a3550; --fg:#dbe4f5;
          --dim:#8b98b8; --ok:#3ecf8e; --warn:#f5b942; --bad:#f0616d; --acc:#5b9dff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  header { padding:14px 24px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:16px; }
  header h1 { font-size:18px; margin:0; }
  header nav a { color:var(--dim); text-decoration:none; margin-right:12px; }
  header nav a:hover { color:var(--fg); }
  main { padding:20px 24px; max-width:1280px; margin:0 auto; }
  section { background:var(--card); border:1px solid var(--line);
            border-radius:10px; padding:16px 18px; margin-bottom:18px; }
  h2 { font-size:15px; margin:0 0 12px; color:var(--dim);
       text-transform:uppercase; letter-spacing:.06em; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line);
          white-space:nowrap; }
  th { color:var(--dim); font-weight:500; font-size:12px; }
  tr:last-child td { border-bottom:none; }
  .cards { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:14px 16px; }
  .card .label { color:var(--dim); font-size:12px; }
  .card .value { font-size:24px; font-weight:600; margin-top:4px; }
  .bar { height:6px; background:var(--line); border-radius:3px; margin-top:10px;
         overflow:hidden; display:flex; }
  .bar i { display:block; height:100%; }
  .num { font-variant-numeric:tabular-nums; }
  .tag { display:inline-block; padding:1px 8px; border-radius:10px;
         font-size:12px; }
  .tag.active { background:rgba(62,207,142,.15); color:var(--ok); }
  .tag.closed { background:rgba(139,152,184,.15); color:var(--dim); }
  .tag.expired,.tag.revoked,.tag.cascade { background:rgba(240,97,109,.15); color:var(--bad); }
  .tag.returned { background:rgba(245,185,66,.15); color:var(--warn); }
  button { background:var(--acc); color:#fff; border:0; border-radius:6px;
           padding:5px 12px; cursor:pointer; font-size:13px; }
  button.ghost { background:transparent; border:1px solid var(--line);
                 color:var(--dim); padding:2px 8px; font-size:12px; }
  button.ghost:hover { color:var(--fg); }
  button.danger { background:transparent; border:1px solid var(--bad);
                  color:var(--bad); padding:2px 8px; font-size:12px; }
  input,select { background:var(--bg); border:1px solid var(--line);
                 color:var(--fg); border-radius:6px; padding:5px 8px; font-size:13px; }
  form { display:flex; gap:8px; align-items:center; flex-wrap:wrap;
         margin-bottom:10px; }
  form .title { width:100%; color:var(--dim); font-size:12px; }
  .msg { font-size:12px; min-height:16px; }
  .msg.err { color:var(--bad); }
  .msg.ok { color:var(--ok); }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
  .dim { color:var(--dim); }
  .check-ok { color:var(--ok); } .check-bad { color:var(--bad); }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
  @media (max-width:900px){ .grid2,.cards{ grid-template-columns:1fr 1fr; } }
</style>
</head>
<body>
<header>
  <h1>多租户配额租约服务</h1>
  <nav><a href="/">总览</a><a href="/leases">租约</a><a href="/api/audit" target="_blank">审计 JSON</a></nav>
  <span class="dim" id="clock"></span>
</header>
<main>
  <div class="cards" id="rootCards"></div>

  <section>
    <h2>层级分配（平台 → 组织 → 项目 → 客户端）</h2>
    <table id="nodes">
      <thead><tr><th>节点</th><th>类型</th><th>可用余额</th><th>已授予下级</th>
      <th>本级已消费</th><th>子树累计消费</th></tr></thead>
      <tbody></tbody>
    </table>
  </section>

  <section id="leases">
    <h2>租约
      <select id="leaseFilter" onchange="render()">
        <option value="">全部</option><option value="active">活跃</option>
        <option value="closed">已关闭</option>
      </select>
    </h2>
    <table id="leaseTable">
      <thead><tr><th>ID</th><th>授予方 → 持有方</th><th>面额</th><th>余额</th>
      <th>已消费</th><th>已转授</th><th>剩余有效期</th><th>状态</th><th>操作</th></tr></thead>
      <tbody></tbody>
    </table>
  </section>

  <div class="grid2">
    <section>
      <h2>分配操作</h2>
      <form id="fRoot"><span class="title">配置全局容量</span>
        <input id="rootCap" type="number" min="0" placeholder="capacity" required>
        <button>设置</button></form>
      <form id="fNode"><span class="title">创建节点（组织/项目/客户端）</span>
        <select id="nodeParent"></select>
        <input id="nodeName" placeholder="名称" required>
        <button>创建</button></form>
      <form id="fLease"><span class="title">申请租约（子级 ← 父级可用额度）</span>
        <select id="leaseHolder"></select>
        <input id="leaseAmount" type="number" min="1" placeholder="金额" required>
        <input id="leaseTtl" type="number" min="1" placeholder="TTL(秒)" required>
        <input id="leaseIdem" placeholder="幂等键(可选)">
        <button>申请</button></form>
      <form id="fUsage"><span class="title">上报用量（幂等事件扣减）</span>
        <select id="usageLease"></select>
        <input id="usageEvent" placeholder="event_id" required>
        <input id="usageAmount" type="number" min="1" placeholder="次数" required>
        <button>扣减</button>
        <button type="button" class="ghost" onclick="dupUsage()">重复回执测试</button></form>
      <div class="msg" id="opMsg"></div>
    </section>
    <section>
      <h2>一致性审计</h2>
      <p class="dim">从事件账本独立重放并校验：全局守恒、无超卖、幂等、TTL 嵌套、资金完整性。</p>
      <button onclick="runAudit()">运行审计</button>
      <div id="auditResult" style="margin-top:10px"></div>
    </section>
  </div>

  <section>
    <h2>事件账本（最近 50 条）</h2>
    <table id="events">
      <thead><tr><th>#</th><th>时间</th><th>类型</th><th>内容</th></tr></thead>
      <tbody></tbody>
    </table>
  </section>
</main>
<script>
let STATE = null;
const $ = id => document.getElementById(id);
const fmt = n => Number(n).toLocaleString('en-US');
const short = id => id ? id.slice(0, 12) + '…' : '';

async function api(path, method = 'GET', body) {
  const r = await fetch(path, { method,
    headers: {'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && j.error.message) || ('HTTP ' + r.status));
  return j;
}
function showMsg(text, ok) {
  const el = $('opMsg');
  el.textContent = text; el.className = 'msg ' + (ok ? 'ok' : 'err');
  setTimeout(() => el.textContent = '', 6000);
}
async function act(fn) {
  try { const r = await fn(); showMsg(typeof r === 'string' ? r : '成功', true); }
  catch (e) { showMsg(e.message, false); }
  await refresh();
}

function renderRoot() {
  const r = STATE.root;
  const pct = v => r.capacity ? (100 * v / r.capacity) : 0;
  $('rootCards').innerHTML = `
    <div class="card"><div class="label">全局容量</div>
      <div class="value num">${fmt(r.capacity)}</div>
      <div class="bar"><i style="width:${pct(r.issued)}%;background:var(--acc)"></i></div></div>
    <div class="card"><div class="label">未发行（可分配）</div>
      <div class="value num">${fmt(r.unissued)}</div></div>
    <div class="card"><div class="label">已发行（租约在外）</div>
      <div class="value num">${fmt(r.issued)}</div></div>
    <div class="card"><div class="label">已消费（不可恢复）</div>
      <div class="value num" style="color:var(--warn)">${fmt(r.burned)}</div>
      <div class="bar"><i style="width:${pct(r.burned)}%;background:var(--warn)"></i></div></div>`;
}

function renderNodes() {
  const rows = STATE.nodes.map(n => `
    <tr><td>${'&nbsp;'.repeat(n.depth * 4)}${n.name}</td>
      <td class="dim">${n.kind}</td>
      <td class="num">${fmt(n.available)}</td>
      <td class="num">${fmt(n.granted_out)}</td>
      <td class="num">${fmt(n.consumed_own)}</td>
      <td class="num">${fmt(n.burned_subtree)}</td></tr>`);
  $('nodes').querySelector('tbody').innerHTML = rows.join('');
}

function ttlText(l) {
  if (l.status !== 'active') return '—';
  const s = Math.max(0, Math.round(l.ttl_remaining));
  return s >= 60 ? `${Math.floor(s / 60)}m${s % 60}s` : `${s}s`;
}
function renderLeases() {
  const f = $('leaseFilter').value;
  const list = STATE.leases.filter(l => !f || l.status === f);
  const rows = list.map(l => {
    const st = l.status === 'active'
      ? '<span class="tag active">活跃</span>'
      : `<span class="tag ${l.close_reason}">已关闭·${l.close_reason}</span>`;
    const ops = l.status === 'active' ? `
      <button class="ghost" onclick="closeLease('${l.id}','returned')">归还</button>
      <button class="danger" onclick="closeLease('${l.id}','revoked')">撤销</button>
      <button class="ghost" onclick="reduceLease('${l.id}',${l.amount})">缩容</button>` : '';
    return `<tr><td class="mono">${short(l.id)}</td>
      <td>${l.granter_name} → ${l.holder_name}</td>
      <td class="num">${fmt(l.amount)}</td><td class="num">${fmt(l.remaining)}</td>
      <td class="num">${fmt(l.consumed)}</td><td class="num">${fmt(l.granted_out)}</td>
      <td>${ttlText(l)}</td><td>${st}</td><td>${ops}</td></tr>`;
  });
  $('leaseTable').querySelector('tbody').innerHTML =
    rows.join('') || '<tr><td colspan="9" class="dim">暂无租约</td></tr>';
}

function renderForms() {
  const nodes = STATE.nodes;
  const parents = nodes.filter(n => n.kind !== 'client');
  $('nodeParent').innerHTML = parents.map(n =>
    `<option value="${n.id}">${n.name}（${n.kind}）</option>`).join('');
  const holders = nodes.filter(n => n.kind !== 'root');
  $('leaseHolder').innerHTML = holders.map(n =>
    `<option value="${n.id}">${n.name}（${n.kind}）</option>`).join('');
  const active = STATE.leases.filter(l => l.status === 'active');
  $('usageLease').innerHTML = active.map(l =>
    `<option value="${l.id}">${l.holder_name} · ${short(l.id)} · 余${fmt(l.remaining)}</option>`).join('');
}

function render() { renderRoot(); renderNodes(); renderLeases(); renderForms(); }

async function refresh() {
  try { STATE = await api('/api/state'); render();
    $('clock').textContent = new Date(STATE.now * 1000).toLocaleTimeString();
  } catch (e) { /* 服务未就绪 */ }
}

async function loadEvents() {
  try {
    const evs = await api('/api/events?limit=50');
    $('events').querySelector('tbody').innerHTML = evs.map(e => `
      <tr><td class="num">${e.seq}</td>
        <td class="dim">${new Date(e.ts * 1000).toLocaleTimeString()}</td>
        <td class="mono">${e.type}</td>
        <td class="mono dim">${esc(JSON.stringify(e.payload)).slice(0, 140)}</td></tr>`).join('');
  } catch (e) {}
}
const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

// ---- 操作 ----
$('fRoot').onsubmit = e => { e.preventDefault();
  act(() => api('/api/root/capacity', 'POST', {capacity: +$('rootCap').value})); };
$('fNode').onsubmit = e => { e.preventDefault();
  const p = STATE.nodes.find(n => n.id === $('nodeParent').value);
  const kind = {root:'org', org:'project', project:'client'}[p.kind];
  act(() => api('/api/nodes', 'POST',
    {parent_id: p.id, name: $('nodeName').value, kind})); };
$('fLease').onsubmit = e => { e.preventDefault();
  act(() => api('/api/leases', 'POST', {
    holder_id: $('leaseHolder').value, amount: +$('leaseAmount').value,
    ttl_seconds: +$('leaseTtl').value,
    idempotency_key: $('leaseIdem').value || undefined})); };
$('fUsage').onsubmit = e => { e.preventDefault();
  act(async () => {
    const r = await api('/api/usage', 'POST', {
      event_id: $('usageEvent').value, lease_id: $('usageLease').value,
      amount: +$('usageAmount').value});
    return r.dedup ? '重复回执：已忽略（幂等）' : `已扣减，租约余额 ${r.remaining}`;
  }); };
function dupUsage() {  // 用同一 event_id 再发一次，验证幂等
  act(async () => {
    const r = await api('/api/usage', 'POST', {
      event_id: $('usageEvent').value, lease_id: $('usageLease').value,
      amount: +$('usageAmount').value});
    return r.dedup ? '重复回执：已忽略（幂等）' : `已扣减，租约余额 ${r.remaining}`;
  }); }
function closeLease(id, reason) {
  act(() => api(`/api/leases/${id}/${reason === 'returned' ? 'return' : 'revoke'}`, 'POST', {})); }
function reduceLease(id, cur) {
  const v = prompt(`缩容：输入新面额（当前 ${cur}）`);
  if (v !== null) act(() => api(`/api/leases/${id}/reduce`, 'POST', {new_amount: +v})); }
async function runAudit() {
  const a = await api('/api/audit');
  $('auditResult').innerHTML =
    `<p class="${a.ok ? 'check-ok' : 'check-bad'}">${a.ok ? '✓ 全部通过' : '✗ 存在违例'}
     （${a.event_count} 条事件）</p>` +
    a.checks.map(c => `<div class="${c.ok ? 'check-ok' : 'check-bad'}">
      ${c.ok ? '✓' : '✗'} ${c.name} <span class="dim">— ${esc(c.detail)}</span></div>`).join('');
}

$('usageEvent').value = 'evt-' + Math.random().toString(36).slice(2, 10);
refresh(); loadEvents();
setInterval(refresh, 2000); setInterval(loadEvents, 4000);
</script>
</body>
</html>
"""
