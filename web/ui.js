'use strict';
const $ = selector => document.querySelector(selector);
const node = (tag, text, className) => { const element = document.createElement(tag); if (text != null) element.textContent = String(text); if (className) element.className = className; return element; };
const state = {hosts: [], agents: [], outbox: []};
const activity = new Map();
let selectedHost = null, selectedAgent = null, createTarget = null, discoveryHost = null;
let candidates = [], cursor = 0, historyGeneration = 0, toastTimer, refreshing = false, refreshTask = null;
let terminalSession = null;
let outputView = null, outputGeneration = 0, bindOutputTarget = null;
let directoryTarget = null, directoryGeneration = 0, closeTarget = null, clipboardTarget = null, contextTerminal = null;
const rendered = {hosts: '', agents: '', outbox: '', detail: ''};
const workbenchStorageKey = 'pron-hub.worker-workbench.v1';
let workbenchSaved = {};
try { workbenchSaved = JSON.parse(window.localStorage?.getItem(workbenchStorageKey) || '{}') || {}; } catch (_) {}
// Old active-only preferences must not hide connected work after the automatic
// worker-access migration. Registration and lifecycle are the single model.
const agentFilters = {host: typeof workbenchSaved.host === 'string' ? workbenchSaved.host : '', query: typeof workbenchSaved.query === 'string' ? workbenchSaved.query : '', status: ['working', 'all', 'connected', 'attention', 'closed'].includes(workbenchSaved.status) ? workbenchSaved.status : 'working', active: false};
if (workbenchSaved.active) { workbenchSaved.active = false; try { window.localStorage?.setItem(workbenchStorageKey, JSON.stringify(workbenchSaved)); } catch (_) {} }
let restoreAgentKey = agentKeyFromHash() || (typeof workbenchSaved.agent === 'string' ? workbenchSaved.agent : null), renameTarget = null;
const renameDrafts = new Map();
const reconnectOperations = new Map(Array.isArray(workbenchSaved.reconnect) ? workbenchSaved.reconnect.filter(entry => Array.isArray(entry) && typeof entry[0] === 'string' && typeof entry[1]?.requestKey === 'string') : []);
const autoReconnectChanges = new Map();
const terminalResizeFlights = new Map();
const agentRecoveryNotices = new Map();
const pendingCreateStorageKey = 'pron-hub.pending-terminal-create.v1';
let pendingTerminalCreate = null, pendingCreateStorageError = false, submittingTerminalCreate = false;
try {
  const stored = terminalCreateStorage()?.getItem(pendingCreateStorageKey);
  if (stored) {
    const value = JSON.parse(stored);
    if (value?.version !== 1 || !validTerminalCreateBody(value.body)) throw new Error('Invalid pending terminal request');
    pendingTerminalCreate = value;
  }
} catch (_) { pendingCreateStorageError = true; }
let displayServiceStatus = null, displayServiceCheck = null, displayServiceCheckedAt = 0, displayServiceApiMissing = false, displayRecoveryTarget = null;
let reconnectReconcileTarget = null;
const reopenStorageKey = 'agent-captain.pending-terminal-reopen.v1';
const reopenOperations = new Map(), reopenDrafts = new Map();
let reopenTarget = null, reopenStorageError = false;
try {
  const saved = terminalCreateStorage()?.getItem(reopenStorageKey);
  if (saved) {
    const entries = JSON.parse(saved);
    if (!Array.isArray(entries) || entries.length > 100 || entries.some(entry => !validReopenOperation(entry))) throw new Error('Invalid reopen record');
    for (const entry of entries) reopenOperations.set(`${entry.body.host}:${entry.body.thread_id}`, entry);
  }
} catch (_) { reopenStorageError = true; }
const displayServiceHelp = '本机显示服务离线不等于远端任务退出。恢复只启动显示服务，不重启 Codex、不发送输入，也不重建已经结束的本机终端。';
function needsDisplayServiceRecovery(recovery) { return recovery?.code === 'local_display_unavailable' || recovery?.next_action === 'recover_display_service'; }
function needsReconnectReconciliation(recovery) { return ['reconcile', 'reconcile_reconnect', 'reconcile_original_worker'].includes(recovery?.next_action); }
function displayServiceUnavailable() { return displayServiceStatus?.running === false || displayServiceStatus?.available === false || displayServiceStatus?.online === false || displayServiceStatus?.healthy === false || ['offline', 'unavailable', 'stopped'].includes(displayServiceStatus?.state || displayServiceStatus?.status) || displayServiceStatus?.running !== true && needsDisplayServiceRecovery(currentAgent()?.recovery); }
const deliveryLabels = {queued: '已排队', waiting_for_agent: '等待在终端中启动工作 agent', human_busy: '等待你完成当前终端输入', blocked: '等待处理终端中的登录或审批', not_sent: '未发送', submitting: '输入提交中', submitted: '已写入终端', delivered: '已写入终端', completed: '该轮已结束', failed: '送达失败', interrupted: '已中断', scope_revoked: '已取消 · 原工作会话不再可用', unknown: '送达结果不确定 · 请核对终端', awaiting_connection: '等待终端连接'};
const statusLabels = {shell: 'Shell 已就绪', running: '终端运行中', connected: '终端已连接', disconnected: '显示连接已断开', not_connected: '尚未连接终端', exited: '显示连接已结束', closed: '终端已关闭', closing: '正在关闭终端', close_unknown: '关闭结果未确认', error: '连接错误', unknown: '终端状态未知'};
const disconnectedLabel = '显示连接已断开 · 工作进程未确认';
const agentKey = agent => `${agent.host}:${agent.thread_id}`;
const currentAgent = () => state.agents.find(agent => agentKey(agent) === selectedAgent);
const currentHost = () => state.hosts.find(host => host.id === selectedHost);
const isTerminal = agent => agent?.mode === 'terminal' || !!agent?.terminal_id;
const isWorkerRecord = agent => agent?.mode !== 'controller' && agent?.kind !== 'controller';
const isClosedAgent = agent => !!agent && (agent.closed || agent.lifecycle === 'closed' || agent.status === 'closed' || agent.state === 'closed');
const isClosingAgent = agent => !!agent && (['closing', 'unknown'].includes(agent.close_state) || ['closing', 'close_unknown'].includes(agent.status) || ['closing', 'close_unknown'].includes(agent.state));
const canReopenAgent = agent => isWorkerRecord(agent) && isTerminal(agent) && !!agent.terminal_id && !isClosedAgent(agent) && !isClosingAgent(agent);
const pendingReopen = agent => agent && ['unknown', 'submitting'].includes(reopenOperations.get(agentKey(agent))?.state);
function canInspectPendingReopen(agent, body = null) {
  if (!agent || !isWorkerRecord(agent) || !isTerminal(agent) || !registeredAgent(agentKey(agent))) return false;
  const saved = reopenOperations.get(agentKey(agent));
  return saved?.state === 'unknown' && validReopenOperation(saved) && saved.body.host === agent.host && saved.body.thread_id === agent.thread_id
    && (!body || Object.keys(saved.body).every(key => saved.body[key] === body[key]) && Object.keys(body).length === Object.keys(saved.body).length);
}
const sessionConnected = session => typeof session?.displayConnected === 'boolean' ? session.displayConnected : session?.agent?.connected !== false;
function displayDisconnected(agent) {
  const session = terminalSession?.key === agentKey(agent) && !terminalSession.disposed ? terminalSession : null;
  return session ? !sessionConnected(session) : agent.connected === false || ['exited', 'disconnected', 'not_connected'].includes(agent.status || agent.state);
}
function hostDisplayStatus(host) {
  if (host.transport !== 'ssh') return {connected: !!host.connected, label: host.connected ? '本机已连接' : '本机未连接'};
  const workers = state.agents.filter(agent => agent.host === host.id && isTerminal(agent) && !isClosedAgent(agent) && !isClosingAgent(agent));
  const count = workers.filter(agent => !displayDisconnected(agent)).length;
  return {connected: count > 0, label: workers.length ? `显示连接 ${count} / ${workers.length}` : host.connected ? '上次 SSH 检查成功' : 'SSH 尚未验证'};
}
const shortId = id => id ? String(id).slice(0, 8) + '…' + String(id).slice(-6) : '';
const workerMachineName = agent => agent.host_label || state.hosts.find(host => host.id === agent.host)?.label || agent.host;
function defaultWorkerName(host, cwd) { const machine = state.hosts.find(item => item.id === host)?.label || host; const leaf = String(cwd || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop(); return `${machine} · ${leaf || '工作终端'}`; }
const workerDisplayName = agent => agent.alias || agent.title || defaultWorkerName(agent.host, agent.cwd);
const resumeCommand = id => `codex resume ${id}`;
function agentKeyFromHash() { try { return new URLSearchParams(String(window.location?.hash || '').replace(/^#/, '')).get('agent'); } catch (_) { return null; } }
function persistWorkbench() {
  try { window.localStorage?.setItem(workbenchStorageKey, JSON.stringify({...agentFilters, agent: selectedAgent || restoreAgentKey, selectedHost, reconnect: [...reconnectOperations]})); } catch (_) {}
}
function updateAgentLocation() {
  if (!window.history?.replaceState || !window.location) return;
  const params = new URLSearchParams(String(window.location.hash || '').replace(/^#/, ''));
  if (selectedAgent) params.set('agent', selectedAgent); else params.delete('agent');
  window.history.replaceState(null, '', (window.location.pathname || '/') + (window.location.search || '') + (params.toString() ? '#' + params : ''));
}
function registeredAgent(key) { return state.agents.find(agent => agentKey(agent) === key || agent.agent_id === key || `${agent.host}::${agent.thread_id}` === key); }
function canAutoReconnect(agent) {
  return isWorkerRecord(agent) && agent?.mode === 'terminal' && !!agent.terminal_id && !isClosedAgent(agent) && !isClosingAgent(agent)
    && state.hosts.some(host => host.id === agent.host && host.transport === 'ssh');
}
function autoReconnectView(agent) {
  const policy = agent?.auto_reconnect;
  if (policy?.enabled !== true) return {text: '已关闭 · 断线后可手动重连', tone: 'off'};
  if (policy.status === 'reconnecting') return {text: '后台正在重连原终端…', tone: 'reconnecting'};
  if (policy.status === 'needs_attention') return {text: `自动重连需处理${policy.last_result?.message ? ' · ' + policy.last_result.message : ' · 请检查原会话或登录状态'}`, tone: 'attention'};
  if (policy.status === 'connected') return {text: '自动重连已开启 · 显示已连接', tone: 'connected'};
  const next = timestamp(policy.next_attempt_at);
  return {text: next ? `下次尝试 ${next.text}（本地时间）` : '自动重连已开启 · 断线后每 10 分钟尝试', tone: 'waiting'};
}
function autoReconnectCheckbox(agent) {
  const label = node('label', null, 'auto-reconnect-option'), input = node('input', null, 'auto-reconnect-checkbox');
  const terminalId = agent.terminal_id;
  input.type = 'checkbox'; input.checked = agent.auto_reconnect?.enabled === true;
  input.disabled = autoReconnectChanges.has(agentKey(agent)) || !!pendingReopen(agent);
  input.setAttribute('aria-label', `自动重连 ${workerDisplayName(agent)}`);
  input.onchange = () => action(input, () => setAgentAutoReconnect(agent, input.checked, terminalId));
  label.append(input, node('span', '自动重连（每 10 分钟）'));
  return label;
}
function renderAutoReconnectDetail(agent) {
  const panel = $('#agent-auto-reconnect'), eligible = canAutoReconnect(agent);
  panel.hidden = !eligible;
  if (!eligible) return;
  const input = $('#agent-auto-reconnect-enabled'), view = autoReconnectView(agent);
  input.checked = agent.auto_reconnect?.enabled === true;
  input.disabled = autoReconnectChanges.has(agentKey(agent)) || !!pendingReopen(agent);
  // Bind the selected generation rather than resolving a different terminal
  // when an old checkbox's event completes after a reopen or selection change.
  const terminalId = agent.terminal_id;
  input.onchange = () => action(input, () => setAgentAutoReconnect(agent, input.checked, terminalId));
  $('#agent-auto-reconnect-status').textContent = view.text;
  $('#agent-auto-reconnect-status').className = `auto-reconnect-status ${view.tone}`;
}
async function setAgentAutoReconnect(agent, enabled, expectedTerminalId = agent.terminal_id) {
  const key = agentKey(agent), selected = registeredAgent(key);
  if (!canAutoReconnect(selected) || selected.terminal_id !== expectedTerminalId) {
    renderAgents(true); renderDetail();
    throw new Error('终端已切换或关闭，请核对当前终端后再设置自动重连。');
  }
  if (autoReconnectChanges.has(key)) return;
  const operation = {terminalId: expectedTerminalId, requestKey: crypto.randomUUID()};
  autoReconnectChanges.set(key, operation); renderAgents(true); renderDetail();
  try {
    const result = await request('/api/agents/auto-reconnect', {host: selected.host, thread_id: selected.thread_id,
      enabled, expected_terminal_id: expectedTerminalId, request_key: operation.requestKey});
    const updated = result.agent || result, current = registeredAgent(key);
    if (!current || current.terminal_id !== expectedTerminalId || updated.host !== selected.host
        || updated.thread_id !== selected.thread_id || updated.terminal_id !== expectedTerminalId) {
      throw new Error('终端在保存期间已切换，请核对新终端的自动重连设置。');
    }
    if (typeof updated.auto_reconnect?.enabled !== 'boolean') throw new Error('后台未返回自动重连设置，请刷新页面核对。');
    // Only apply this setting. A configuration response cannot replace a live
    // display generation, input state or manual reconnect receipt.
    current.auto_reconnect = updated.auto_reconnect;
    await refresh(true);
    toast(enabled ? '自动重连已开启：后台每 10 分钟接回断线的原终端，关闭网页后仍生效。' : '自动重连已关闭。');
  } finally {
    if (autoReconnectChanges.get(key) === operation) autoReconnectChanges.delete(key);
    renderAgents(true); renderDetail();
  }
}
function agentNeedsAttention(agent) { return !isClosedAgent(agent) && (!!agent.management?.needs_attention || isClosingAgent(agent) || displayDisconnected(agent) || !!agent.blocked_reason || !!agent.error || ['setup_menu', 'approval', 'login', 'error', 'unknown'].includes(agent.state || agent.status) || !isTerminal(agent)); }
function filteredAgents() {
  const query = agentFilters.query.trim().toLocaleLowerCase();
  return state.agents.filter(agent => isWorkerRecord(agent) && (!agentFilters.host || agent.host === agentFilters.host)
    && (!query || [agent.alias, agent.title, agent.cwd, agent.host_label, state.hosts.find(host => host.id === agent.host)?.label, agent.host].some(value => String(value || '').toLocaleLowerCase().includes(query)))
    && (agentFilters.status === 'closed' ? isClosedAgent(agent) : !isClosedAgent(agent))
    && (agentFilters.status !== 'working' || isTerminal(agent))
    && (agentFilters.status !== 'connected' || isTerminal(agent) && !isClosingAgent(agent) && !displayDisconnected(agent))
    && (agentFilters.status !== 'attention' || agentNeedsAttention(agent)))
    .sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || workerDisplayName(a).localeCompare(workerDisplayName(b), 'zh-CN') || agentKey(a).localeCompare(agentKey(b)));
}
function renderWorkbenchFilters() {
  const select = $('#agent-host-filter'); select.replaceChildren();
  const all = node('option', '全部机器'); all.value = ''; select.append(all);
  for (const host of state.hosts) { const option = node('option', host.label || host.id); option.value = host.id; select.append(option); }
  select.value = agentFilters.host; $('#agent-status-filter').value = agentFilters.status;
  $('#agent-search').value = agentFilters.query;
  $('#all-machines').classList.toggle('selected', !agentFilters.host);
}
function dateText(value) { if (!value) return ''; const date = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN'); }
function timestamp(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  const date = new Date(value * 1000); if (Number.isNaN(date.getTime())) return null;
  const pad = part => String(part).padStart(2, '0');
  return {iso: date.toISOString(), text: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`};
}
function responseDuration(value) {
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
}
function hostLatencyView(host, now = Date.now()) {
  const explanation = '已有 SSH 查询的端到端响应，包含建连、远端执行和传输，不是网络 ping；不会额外发起探测。';
  if (host.transport === 'local') return {state: 'local', text: '延时 — · 本机', title: '本机不经过 SSH，不伪造网络延时。'};
  const sample = host.latency || {}, measured = typeof sample.last_ms === 'number' && Number.isFinite(sample.last_ms) && sample.last_ms >= 0;
  const observed = timestamp(sample.last_success_at), last = sample.metric === 'ssh_response' && measured && observed ? responseDuration(sample.last_ms) : null;
  if (sample.state === 'failed') return {state: 'failed', text: last ? `SSH 查询失败 · 上次 ${last}` : 'SSH 响应暂不可用', title: `${explanation} 最近一次查询未成功。${last ? `上次成功 ${last}，测于 ${observed.text}（本地时间）。` : ''}`};
  if (sample.metric !== 'ssh_response' || sample.state !== 'measured' || !last) return {state: 'unknown', text: 'SSH 响应 —', title: `${explanation} 尚无成功测量。`};
  const age = now - sample.last_success_at * 1000, stale = age < 0 || age > 120000;
  return {state: stale ? 'stale' : 'measured', text: `${stale ? '上次 SSH' : 'SSH 响应'} ${last}`, title: `${explanation} 测于 ${observed.text}（本地时间）。${stale ? '这是历史样本，不代表当前响应。' : ''}`};
}
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 7500); }
// These exact GET routes use the same deliberately opaque authentication
// response in app.py. Do not reinterpret an arbitrary missing resource as auth.
const protectedUiGetPaths = new Set(['/api/state', '/api/terminal-service/status', '/api/terminals/output', '/api/terminals/history', '/api/controllers', '/api/controllers/get', '/api/controllers/output']);
function uiSessionError(path, body, status, data) {
  const expired = status === 403 && data?.error === 'Invalid session or origin'
    || body === undefined && status === 404 && protectedUiGetPaths.has(String(path).split('?')[0]) && data?.error === 'Not found or unauthorized';
  return expired ? Object.assign(new Error('页面认证已失效，可能是 AgentCaptain 后台已重启。请刷新页面重新连接；已保存的原终端打开请求会保留，不会自动创建或重发输入。'), {code: 'ui_session_expired', status}) : null;
}
async function request(path, body) {
  let response;
  try { response = await fetch(path, body === undefined ? {credentials: 'same-origin', cache: 'no-store'} : {method: 'POST', credentials: 'same-origin', keepalive: path === '/api/terminals/input', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); }
  catch (_) { throw Object.assign(new Error('无法连接本机 AgentCaptain 后台，可能正在停止或重启。请先恢复后台；这不证明远端 SSH 或工作进程已经退出。未收到确认的请求不会自动重发。'), {code: 'hub_unreachable'}); }
  let data;
  try { data = await response.json(); }
  catch (_) {
    if (response.ok) throw Object.assign(new Error('AgentCaptain 后台响应格式异常，尚不能确认请求结果。请恢复后台后核对原请求，不要重新创建终端或重发输入。'), {code: 'hub_response_invalid', status: response.status});
    data = {};
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw Object.assign(new Error('AgentCaptain 后台响应格式异常，尚不能确认请求结果。请核对原请求，不会自动重试。'), {code: 'hub_response_invalid', status: response.status});
  if (!response.ok) {
    const sessionError = uiSessionError(path, body, response.status, data);
    if (sessionError) throw sessionError;
    const error = new Error(typeof data.error === 'string' ? data.error : data.error?.message || `请求失败 (${response.status})`); error.status = response.status; error.code = data.code || data.error?.code; error.recovery = data.recovery; throw error;
  }
  return data;
}
async function action(element, task) { element.disabled = true; try { await task(); } catch (error) { toast(error.message); } finally { element.disabled = false; if (['terminal-connect', 'terminal-copy', 'terminal-paste'].includes(element.id)) renderDetail(); } }
function button(text, className, onClick) { const element = node('button', text, className); element.type = 'button'; element.onclick = () => action(element, onClick); return element; }
function empty(parent, text) { parent.append(node('p', text, 'empty-copy')); }
function inline(parent, text) { for (const part of String(text).split(/(`[^`]+`|\*\*[^*]+\*\*)/g)) { if (part.startsWith('`') && part.endsWith('`')) parent.append(node('code', part.slice(1, -1))); else if (part.startsWith('**') && part.endsWith('**')) parent.append(node('strong', part.slice(2, -2))); else parent.append(document.createTextNode(part)); } }
function formatted(parent, text) { let code = null; for (const line of String(text).split('\n')) { if (line.trim().startsWith('```')) { if (code) { parent.append(code); code = null; } else code = node('pre', '', 'code-block'); } else if (code) code.textContent += line + '\n'; else { const paragraph = node('div', null, 'prose-line'); inline(paragraph, line); parent.append(paragraph); } } if (code) parent.append(code); }
function renderHosts() {
  const list = $('#hosts'); list.replaceChildren(); if (!state.hosts.length) empty(list, '尚未配置机器。点击 + 添加本机或 SSH 服务器。');
  for (const host of state.hosts) {
    const display = hostDisplayStatus(host);
    const card = node('div', null, 'host-card' + (host.id === agentFilters.host ? ' selected' : '')); const select = button('', 'host-select', async () => selectHost(host.id));
    const name = node('span', null, 'host-name'); name.append(node('span', '', 'connection-dot' + (display.connected ? ' connected' : '')), node('strong', host.label || host.id));
    select.append(name, node('span', host.transport === 'local' ? '本机' : (host.target || host.targetAlias || 'SSH'), 'host-target')); card.append(select);
    const latency = hostLatencyView(host), latencyLabel = node('span', latency.text, `host-latency ${latency.state}`);
    latencyLabel.title = latency.title; select.append(latencyLabel);
    const controls = node('div', null, 'host-controls'); controls.append(node('span', display.label, 'muted'), button('连接', 'text-button', async () => {
      const result = await request('/api/hosts/connect', {host: host.id}); await refresh(true);
      if (result.connected === false) throw new Error(result.error || '机器连接失败。请检查 SSH 配置。');
      toast(`${host.label || host.id} 已连接，可以打开工作终端。`);
    }), button('配置', 'text-button', async () => openMachine(host)));
    card.append(controls); if (host.error) card.append(node('p', host.error, 'host-error')); list.append(card);
  }
  $('#new-agent').disabled = !selectedHost; $('#discover-button').disabled = !selectedHost;
  $('#machine-caption').textContent = agentFilters.host ? `仅看：${state.hosts.find(host => host.id === agentFilters.host)?.label || agentFilters.host}` : '跨机器查看 · 新终端使用当前选定机器';
  renderWorkbenchFilters();
}
function selectHost(hostId) {
  selectedHost = hostId; agentFilters.host = hostId;
  candidates = []; discoveryHost = null; $('#candidates-section').hidden = true;
  persistWorkbench(); renderHosts(); renderAgents(true); renderDetail();
}
async function selectAgent(agent, options = {}) {
  if (selectedAgent === agentKey(agent)) { if (options.focus && terminalSession) terminalSession.term.focus(); return; }
  selectedAgent = agentKey(agent); selectedHost = agentFilters.host || agent.host; restoreAgentKey = null; historyGeneration++; resetConversationOutput(); disposeTerminal(); persistWorkbench(); updateAgentLocation(); renderHosts(); renderAgents(true); renderDetail();
  if (isTerminal(agent)) await openTerminal(agent, {focus: !!options.focus}); else if (!options.restore) await readHistory();
}
function renderAgents(force = false) {
  // A live registry update should not destroy a name being edited or steal its
  // focus. Explicit filter/selection actions may redraw from the retained draft.
  if (renameTarget?.rendered && renameTarget.mode !== 'dialog' && !force) return;
  const list = $('#agents'); list.replaceChildren(); const matches = filteredAgents();
  if (renameTarget) renameTarget.rendered = false;
  $('#registry-count').textContent = String(matches.length);
  const closedCount = state.agents.filter(isClosedAgent).length;
  $('#agent-count-caption').textContent = `显示 ${matches.length} / ${state.agents.length} 个登记${agentFilters.status !== 'closed' && closedCount ? ` · ${closedCount} 个已关闭记录默认收起` : ''} · 连接状态不是任务完成状态`;
  if (!matches.length) {
    const copy = !state.agents.length ? '还没有登记工作会话。选择机器打开新终端，再按平常方式启动 Codex。' : agentFilters.status === 'closed' ? '没有符合筛选的已关闭记录。' : '没有符合筛选的工作会话；调整机器、名称或状态，已关闭记录可单独查看。';
    empty(list, copy);
    if (state.agents.length) list.append(button('清除筛选，查看工作会话', 'subtle agent-clear-filter', async () => clearAgentFilters()));
  }
  for (const agent of matches) {
    const terminal = isTerminal(agent), card = node('div', null, 'agent-card workbench-agent-card' + (agentKey(agent) === selectedAgent ? ' selected' : '') + (agent.pinned ? ' pinned' : ''));
    const heading = node('div', null, 'agent-card-topline'); heading.append(node('span', workerMachineName(agent), 'agent-host-label'));
    const pin = button(agent.pinned ? '★' : '☆', 'agent-pin-button' + (agent.pinned ? ' pinned' : ''), async () => updateAgent(agent, {pinned: !agent.pinned})); pin.setAttribute('aria-label', `${agent.pinned ? '取消置顶' : '置顶'} ${workerDisplayName(agent)}`); pin.setAttribute('aria-pressed', String(!!agent.pinned)); heading.append(pin); card.append(heading);
    const open = button('', 'agent-select', async () => selectAgent(agent));
    const status = terminal ? isClosedAgent(agent) || isClosingAgent(agent) ? statusLabels[agent.status || agent.state] || '终端已暂停' : displayDisconnected(agent) ? disconnectedLabel : agent.command ? `显示连接已连接 · ${agent.command}` : '显示连接已连接' : '尚未连接终端';
    open.append(node('strong', workerDisplayName(agent)), node('span', status, 'agent-meta'), node('span', agent.cwd || '目录未记录', 'agent-directory'), node('span', shortId(agent.thread_id), 'agent-id')); card.append(open);
    if (agent.management?.label) card.append(node('span', agent.management.label, 'agent-management-label' + (agentNeedsAttention(agent) ? ' attention' : '')));
    if (canAutoReconnect(agent) && agent.auto_reconnect?.enabled === true) {
      const view = autoReconnectView(agent); card.append(node('span', view.text, `agent-auto-reconnect-status ${view.tone}`));
    }
    const quick = node('div', null, 'agent-quick-actions');
    quick.append(button(terminal ? '打开终端' : '查看记录', 'subtle', async () => selectAgent(agent)));
    const rename = button(agent.alias ? '重命名' : '命名', 'subtle agent-rename-button', async () => openAgentRename(agent)); rename.setAttribute('aria-label', `${agent.alias ? '重命名' : '命名'} ${workerDisplayName(agent)}`); quick.append(rename);
    if (terminal) quick.append(button('会话输出', 'subtle', async () => { await selectAgent(agent); if (selectedAgent === agentKey(agent)) await readConversationOutput(); }));
    if (terminal && !isClosedAgent(agent) && !isClosingAgent(agent) && displayDisconnected(agent) && agent.management?.primary_action !== 'none') quick.append(button('重连原终端', 'agent-reconnect-button', async () => { await selectAgent(agent); if (selectedAgent === agentKey(agent)) await reconnectTerminal(); }));
    card.append(quick);
    const more = node('details', null, 'agent-more'), summary = node('summary', '更多'); more.append(summary);
    const moreActions = node('div', null, 'agent-more-actions'); moreActions.append(button('复制直达链接', 'text-button', async () => copyAgentLink(agent)));
    if (canAutoReconnect(agent)) moreActions.append(autoReconnectCheckbox(agent));
    if (canReopenAgent(agent) || canInspectPendingReopen(agent)) moreActions.append(button(canInspectPendingReopen(agent) ? '核对原重开结果…' : '重开终端…', 'text-button agent-reopen-link', async () => openReopenTerminal(agent)));
    if (terminal && !isClosedAgent(agent) && !isClosingAgent(agent)) moreActions.append(button('关闭终端…', 'text-button agent-close-link', async () => { await selectAgent(agent); if (selectedAgent === agentKey(agent)) openCloseTerminal(); })); more.append(moreActions); card.append(more);
    if (renameTarget?.key === agentKey(agent) && renameTarget.mode !== 'dialog') {
      const form = node('form', null, 'agent-inline-rename'), input = node('input'); input.value = renameTarget.value; input.maxLength = 200; input.required = true; input.setAttribute('aria-label', '工作会话新名称'); input.disabled = renameTarget.saving;
      input.oninput = () => { renameTarget.value = input.value; };
      const save = node('button', '保存'); save.type = 'submit'; save.disabled = renameTarget.saving;
      form.onsubmit = event => { event.preventDefault(); void action(save, () => saveAgentRename(agent)); };
      form.append(input, save, button('取消', 'text-button', async () => { renameDrafts.delete(agentKey(agent)); renameTarget = null; renderAgents(true); })); card.append(form); renameTarget.rendered = true;
      if (renameTarget.focus) { renameTarget.focus = false; requestAnimationFrame(() => input.focus()); }
    }
    if (!terminal) card.append(button('在终端中继续', 'text-button history-open', async () => openCreate(agent, agent.host)));
    list.append(card);
  }
}
function clearAgentFilters() { agentFilters.host = ''; agentFilters.query = ''; agentFilters.status = 'working'; agentFilters.active = false; persistWorkbench(); renderHosts(); renderAgents(true); }
async function updateAgent(agent, fields) { const result = await request('/api/agents/update', {host: agent.host, thread_id: agent.thread_id, ...fields}); const updated = result.agent || result; if (updated.host === agent.host && updated.thread_id === agent.thread_id) Object.assign(agent, updated); await refresh(true); renderAgents(true); renderDetail(); }
function openAgentRename(agent, options = {}) {
  if (renameTarget?.saving) return;
  if (renameTarget) renameDrafts.set(renameTarget.key, renameTarget.value);
  const key = agentKey(agent);
  renameTarget = {key, value: renameDrafts.has(key) ? renameDrafts.get(key) : workerDisplayName(agent), mode: options.dialog ? 'dialog' : 'inline', focus: !options.dialog, saving: false, rendered: false};
  if (options.dialog) {
    $('#rename-target').textContent = `${workerDisplayName(agent)} · ${workerMachineName(agent)} · ${agent.cwd || '目录未记录'}`;
    $('#rename-name').value = renameTarget.value; $('#rename-name').disabled = false; $('#rename-save').disabled = false;
    $('#rename-dialog').showModal();
  }
  renderAgents(true);
}
async function saveAgentRename(agent) {
  const target = renameTarget; if (!target || target.key !== agentKey(agent) || target.saving) return;
  const alias = target.value.trim(); if (!alias || alias.length > 200) throw new Error('名称不能为空，最多 200 个字符。');
  target.saving = true;
  if (target.mode === 'dialog') $('#rename-name').disabled = true; else renderAgents(true);
  try { await updateAgent(agent, {alias}); renameDrafts.delete(target.key); if (renameTarget === target) { renameTarget = null; if (target.mode === 'dialog') $('#rename-dialog').close(); renderAgents(true); } }
  finally { target.saving = false; if (renameTarget === target) { if (target.mode === 'dialog') $('#rename-name').disabled = false; else renderAgents(true); } }
}
async function copyAgentLink(agent) {
  if (!window.location?.origin) throw new Error('当前浏览器地址不可用。');
  const url = window.location.origin + (window.location.pathname || '/') + '#agent=' + encodeURIComponent(agentKey(agent));
  if (!navigator.clipboard?.writeText) throw new Error('浏览器未允许复制链接，请使用地址栏中的直达地址。');
  await navigator.clipboard.writeText(url); toast('工作会话直达链接已复制；打开链接只显示原对象，不会启动或接管。');
}
function renderCandidates() {
  $('#candidates-section').hidden = false; const list = $('#candidates'); list.replaceChildren();
  if (!candidates.length) empty(list, '没有找到匹配的对话。试试更短的标题或项目目录。');
  for (const thread of candidates) {
    const id = thread.id || thread.thread_id, card = node('div', null, 'candidate-card');
    card.append(node('strong', thread.title || thread.name || shortId(id)), node('span', thread.cwd || '项目目录未记录', 'candidate-path'), node('span', `${shortId(id)}${thread.updated_at ? ' · ' + dateText(thread.updated_at) : ''}`, 'agent-id'));
    const existing = state.agents.find(agent => agent.host === discoveryHost && agent.thread_id === id);
    card.append(button(isTerminal(existing) ? '打开终端' : '在终端中继续', 'candidate-action', async () => { if (isTerminal(existing)) await selectAgent(existing); else openCreate(thread, discoveryHost); })); list.append(card);
  }
}
function renderDetail() {
  const agent = currentAgent(); renderAgentRecoveryNotice(agent); $('#detail-empty').hidden = !!agent; $('#agent-detail').hidden = !agent; if (!agent) return;
  renderAutoReconnectDetail(agent);
  if (outputView?.key === selectedAgent && outputView.terminalId !== agent.terminal_id) resetConversationOutput();
  const terminal = isTerminal(agent), host = state.hosts.find(item => item.id === agent.host);
  $('#detail-host').textContent = host?.label || agent.host; $('#detail-name').textContent = workerDisplayName(agent);
  $('#detail-rename').textContent = agent.alias ? '重命名' : '命名';
  const meta = $('#detail-meta'); meta.replaceChildren();
  meta.append(node('span', terminal ? '共享终端' : '对话历史', 'badge'));
  if (agent.cwd) meta.append(node('span', agent.cwd, 'muted')); meta.append(node('code', shortId(agent.terminal_id || agent.thread_id), 'thread-id'));
  $('#terminal-panel').hidden = !terminal; $('#legacy-panel').hidden = terminal; $('#refresh-history').hidden = terminal; $('#terminal-focus').hidden = !terminal; $('#terminal-connect').hidden = !terminal;
  const closed = isClosedAgent(agent), closing = isClosingAgent(agent) || !!(terminalSession?.key === selectedAgent && terminalSession.closing), disconnected = terminal && displayDisconnected(agent);
  const reopening = pendingReopen(agent);
  $('#terminal-reopen').hidden = !canReopenAgent(agent) && !canInspectPendingReopen(agent); $('#terminal-reopen').disabled = !!reopenTarget?.submitting;
  $('#terminal-reopen').textContent = canInspectPendingReopen(agent) ? '核对原重开结果…' : '重开终端…';
  for (const id of ['terminal-focus', 'terminal-connect', 'terminal-copy', 'terminal-paste', 'terminal-close']) $(`#${id}`).disabled = closed || closing || reopening;
  if (disconnected) { $('#terminal-focus').disabled = true; $('#terminal-paste').disabled = true; }
  if (terminalSession?.key === selectedAgent) { terminalSession.closed = closed; if (closed || closing) { terminalSession.closing = true; cancelPendingResize(terminalSession); terminalSession.term.blur(); } }
  $('#terminal-help').textContent = '滚轮、PageUp / PageDown 只滚动翻页，不输入到 agent。Shift+Home 查看最早保留历史，Shift+End 回到最新。' + (closed ? '终端已关闭，保留的输出仍可查看。需要继续工作时请新建终端；这个已关闭的进程不会重新启动。' : closing ? '终端正在关闭或关闭结果尚未确认，输入与重连已暂停。保留的输出仍可查看，请先核对进程状态。' : disconnected ? '显示连接已断开，输入已暂停。保留画面仍可查看和复制；这不能证明远端工作进程退出。可以点击「重新连接」接回原终端，会话和进程是否仍在需重新核对。' : '选中文字后 Ctrl+C 复制，无选中时 Ctrl+C 中断。Ctrl+V 粘贴，也可用 Ctrl+Shift+C/V 或右键菜单。关闭页面会保留任务；「关闭终端」会结束其中的工作进程。');
  $('#terminal-connect').textContent = disconnected ? '重新连接' : '刷新画面';
  $('#terminal-reconcile').hidden = !terminal || !canReconcileReconnect(agent);
  $('#terminal-reconcile').textContent = agent.recovery?.reconcile_kind === 'live_display' ? '核对原连接' : '核验原重连…';
  $('#terminal-reconcile').disabled = closed || closing;
  const sourceId = agent.resume_thread_id || agent.source_thread_id || (terminal && /^[0-9a-f]{8}-[0-9a-f-]+$/i.test(agent.thread_id) ? agent.thread_id : null);
  const runningAgent = agent.agent_ready === true || agent.state === 'agent' || agent.status === 'agent';
  $('#resume-guide').hidden = !sourceId || !terminal || disconnected || runningAgent;
  if (sourceId) $('#resume-command').textContent = resumeCommand(sourceId);
  if (reopening) $('#terminal-status').textContent = '重开结果待核对 · 旧输入入口已暂停';
  else if (terminal && closed) $('#terminal-status').textContent = '终端已关闭';
  else if (terminal && disconnected && !closing) $('#terminal-status').textContent = disconnectedLabel;
  else if (terminal && (!terminalSession || terminalSession.key !== selectedAgent)) $('#terminal-status').textContent = agent.connected ? '正在读取终端画面……' : '终端尚未连接，点击重新连接';
  $('#terminal-output').disabled = !terminal;
  $('#terminal-bind-output').disabled = !terminal;
  if (outputView?.key === selectedAgent && outputView.open) renderOutputBinding(agent);
}
function renderAgentRecoveryNotice(agent = currentAgent()) {
  const localNotice = agent ? agentRecoveryNotices.get(agentKey(agent)) : '';
  let message = agent ? localNotice || agent.recovery?.message || '' : '';
  const obsoleteWait = !localNotice || localNotice === agent?.recovery?.message
    || localNotice === '原终端显示已附着且身份一致；前台尚未就绪，请进入终端核对。';
  if (agent?.recovery?.code === 'awaiting_foreground' && agent.connected === true && agent.agent_ready === true
      && !agent.human_busy && !agent.blocked_reason && obsoleteWait) message = '原终端已连接，前台已核验可交互。';
  $('#agent-recovery-notice').textContent = message; $('#agent-recovery-notice').hidden = !message;
  renderDisplayServiceNotice();
}
function renderDisplayServiceNotice() {
  const unavailable = displayServiceUnavailable();
  $('#terminal-service-notice').hidden = !unavailable;
  $('#terminal-service-heading').textContent = ['unknown', 'blocked'].includes(displayServiceStatus?.state) ? '本机终端显示服务状态待核验' : '本机终端显示服务未连接';
  $('#terminal-service-message').textContent = displayServiceStatus?.message || displayServiceHelp;
  $('#terminal-service-recover').disabled = !!displayRecoveryTarget?.submitting || !displayServiceApiMissing && displayServiceStatus?.can_recover === false;
  $('#terminal-service-recover').textContent = displayServiceApiMissing ? '查看恢复方法' : '恢复本机显示服务…';
}
async function checkDisplayServiceStatus(force = false) {
  if (displayServiceCheck) return displayServiceCheck;
  if (!force && displayServiceCheckedAt && Date.now() - displayServiceCheckedAt < 15000) return displayServiceStatus;
  displayServiceCheckedAt = Date.now();
  displayServiceCheck = (async () => {
    try { displayServiceStatus = await request('/api/terminal-service/status'); displayServiceApiMissing = false; }
    catch (error) { if (error.status === 404) displayServiceApiMissing = true; /* A failed check is not proof that remote agents exited. */ }
    finally { displayServiceCheck = null; renderDisplayServiceNotice(); }
    return displayServiceStatus;
  })();
  return displayServiceCheck;
}
async function openDisplayServiceRecovery() {
  if (displayRecoveryTarget?.submitting) return;
  await checkDisplayServiceStatus(true);
  const agent = currentAgent(), host = state.hosts.find(item => item.id === agent?.host);
  const remote = host?.transport === 'ssh' && isTerminal(agent) && !isClosedAgent(agent) && !isClosingAgent(agent);
  displayRecoveryTarget = {key: remote ? agentKey(agent) : null, name: remote ? agent.alias || agent.title || shortId(agent.thread_id) : '', submitting: false};
  $('#terminal-service-recovery-target').textContent = remote ? `恢复后仅核验并接回当前工作会话：${displayRecoveryTarget.name}（${host.label || host.id}）。旧的未确认请求会先核验原请求键；不会创建替代会话或重发输入。` : '只恢复显示服务，不自动连接任何工作会话。';
  $('#terminal-service-recovery-error').hidden = true;
  $('#terminal-service-recovery-error').textContent = '';
  $('#terminal-service-recovery-submit').hidden = displayServiceApiMissing;
  $('#terminal-service-recovery-submit').disabled = !displayServiceApiMissing && displayServiceStatus?.can_recover === false;
  $('#terminal-service-recovery-submit').textContent = remote ? '恢复显示服务并接回所选会话' : '仅恢复本机显示服务';
  $('#terminal-service-recovery-legacy').hidden = !displayServiceApiMissing;
  $('#terminal-service-recovery-dialog').showModal();
}
async function recoverDisplayService() {
  const target = displayRecoveryTarget; if (!target || target.submitting || displayServiceApiMissing) return;
  target.submitting = true; $('#terminal-service-recovery-submit').disabled = true; renderDisplayServiceNotice();
  $('#terminal-service-recovery-error').hidden = true;
  let completionMessage = '本机显示服务已恢复；未重启工作 agent、未发送任务。';
  try {
    const result = await request('/api/terminal-service/recover', {confirm: true});
    await checkDisplayServiceStatus(true);
    if (result.running !== true || displayServiceStatus?.running === false) throw new Error(result.message || result.error || '本机显示服务尚未恢复，请核对服务状态后再试。');
    await refresh(true);
    // The service recovery endpoint has no terminal identity and cannot start
    // Codex. The human's confirmation covers only this captured, still selected
    // remote view; never batch reconnect other workers or the management session.
    if (target.key && selectedAgent === target.key) {
      const agent = currentAgent();
      if (isTerminal(agent) && !isClosedAgent(agent) && !isClosingAgent(agent) && displayDisconnected(agent)) {
        if (terminalSession?.key === target.key && terminalSession.input) completionMessage = '显示服务已恢复。当前终端有未发送输入，已保留；请先核对草稿，再点击重新连接。';
        else {
          const operation = reconnectOperations.get(target.key);
          if (agent.recovery?.request_origin === 'plugin' || operation?.requestOrigin === 'plugin') {
            completionMessage = '显示服务已恢复。原重连属于 Captain 插件，请核验原请求，再让 Captain 沿用同一插件请求键接回；网页不会以 UI 来源重试。';
            agentRecoveryNotices.set(target.key, completionMessage); renderAgentRecoveryNotice(agent);
          } else {
            if (operation && !operation.acknowledged && (needsReconnectReconciliation(agent.recovery) || needsReconnectReconciliation({next_action: operation.nextAction}))) {
              if (result.owner_recovered !== true || !result.recovery_id) throw new Error('显示服务已在线，但没有本次恢复的核验证据。旧请求结果仍未确认，已保留原请求键；不会新建请求或重发输入。');
              const reconciled = await request('/api/agents/reconcile-reconnect', {host: agent.host, thread_id: agent.thread_id, request_key: operation.requestKey, confirm: true});
              const recovery = reconciled.recovery || reconciled.agent?.recovery || {};
              if (!((recovery.retryable === true || reconciled.retryable === true) && (recovery.operation_state || reconciled.operation_state) === 'blocked')) throw new Error(recovery.message || '原请求结果仍未确认，已保留原请求键；不会自动重发。');
              operation.nextAction = recovery.next_action; operation.recoveryCode = recovery.code; operation.operationState = recovery.operation_state; persistWorkbench();
              if (reconciled.agent?.host === agent.host && reconciled.agent?.thread_id === agent.thread_id) Object.assign(agent, reconciled.agent);
              agent.recovery = recovery;
            }
            await reconnectTerminal({afterServiceRecovery: true});
          }
        }
      }
    }
    if (displayRecoveryTarget === target) $('#terminal-service-recovery-dialog').close();
    toast(completionMessage);
  } catch (error) {
    if (displayRecoveryTarget === target) { $('#terminal-service-recovery-error').textContent = error.message; $('#terminal-service-recovery-error').hidden = false; }
  } finally { target.submitting = false; $('#terminal-service-recovery-submit').disabled = displayServiceStatus?.can_recover === false; renderDisplayServiceNotice(); }
}
function reconnectReconcileKeys(agent) { return agent?.recovery?.reconcile_available === true && Array.isArray(agent.recovery.reconcile_request_keys) ? [...new Set(agent.recovery.reconcile_request_keys.filter(key => typeof key === 'string' && key.length > 0))] : []; }
function canReconcileReconnect(agent) { return isTerminal(agent) && !isClosedAgent(agent) && !isClosingAgent(agent) && state.hosts.some(host => host.id === agent.host && host.transport === 'ssh') && reconnectReconcileKeys(agent).length > 0; }
function openReconnectReconciliation() {
  const agent = currentAgent(); if (!canReconcileReconnect(agent) || reconnectReconcileTarget?.submitting) return;
  const keys = reconnectReconcileKeys(agent);
  const liveDisplay = agent.recovery?.reconcile_kind === 'live_display';
  const pluginRequest = agent.recovery?.next_action === 'reconcile_original_worker';
  reconnectReconcileTarget = {key: agentKey(agent), host: agent.host, thread_id: agent.thread_id, keys, liveDisplay,
    requestOrigin: pluginRequest ? 'plugin' : 'ui', endpoint: pluginRequest ? '/api/agents/reconcile-original-worker' : '/api/agents/reconcile-reconnect', submitting: false};
  const select = $('#terminal-reconcile-key'); select.replaceChildren();
  const placeholder = node('option', '请选择要核验的原重连请求'); placeholder.value = ''; select.append(placeholder);
  for (const key of keys) { const option = node('option', key); option.value = key; select.append(option); }
  // Even a unique candidate still needs human confirmation; for multiple
  // candidates never silently pick the first or a new browser-generated key.
  const held = reconnectOperations.get(agentKey(agent)); select.value = keys.length === 1 ? keys[0] : keys.includes(held?.requestKey) ? held.requestKey : '';
  $('#terminal-reconcile-dialog h2').textContent = liveDisplay ? '核对原连接？' : pluginRequest ? '核验 Captain 原重连请求？' : '核验原重连结果并接回终端？';
  $('#terminal-reconcile-dialog .field-help').textContent = liveDisplay
    ? '后台会核验当前显示服务、原显示进程和远端原 tmux/pane 身份，确认后核销这条原重连请求。不重新附着、不新建终端、不发送输入；无法确认时保留未知状态。'
    : pluginRequest ? '后台只核验这条 Captain 插件的原请求及原工作进程证据。核验成功后仍需 Captain 沿用同一插件请求键接回；网页不会以 UI 来源重试，不发送输入。'
    : '只使用该工作会话已有的原请求键。后台会核验显示服务身份，以及原显示已结束或经确认恢复的服务中不存在；原显示已结束时，还需核对远端原 tmux/pane 身份未变，才允许同键接回。无法确认时保持未知。不会新建 Codex、执行 resume、发送或重发输入，也不连接其他工作会话。';
  $('#terminal-reconcile-confirm').textContent = liveDisplay ? '核对原连接' : pluginRequest ? '核验原请求' : '核验并重连所选原终端';
  $('#terminal-reconcile-target').textContent = `${liveDisplay ? '仅核验当前原连接' : '仅核验并接回'}：${agent.alias || agent.title || shortId(agent.thread_id)}（${state.hosts.find(host => host.id === agent.host)?.label || agent.host}）。`;
  $('#terminal-reconcile-error').hidden = true; $('#terminal-reconcile-confirm').disabled = !select.value;
  $('#terminal-reconcile-dialog').showModal();
}
async function reconcileSelectedReconnect() {
  const target = reconnectReconcileTarget, requestKey = $('#terminal-reconcile-key').value;
  if (!target || target.submitting || !target.keys.includes(requestKey)) return;
  if (selectedAgent !== target.key) throw new Error('当前工作会话已改变，请重新选择并核验；不会连接其他 agent。');
  if (terminalSession?.key === target.key && terminalSession.input) throw new Error('当前终端有未发送输入，已保留；请先核对草稿。');
  target.submitting = true; $('#terminal-reconcile-confirm').disabled = true; $('#terminal-reconcile-error').hidden = true;
  try {
    const result = await request(target.endpoint, {host: target.host, thread_id: target.thread_id, request_key: requestKey, confirm: true});
    const recovery = result.recovery || result.agent?.recovery || {};
    if (result.operation_state === 'done' && recovery.reconciled === true && recovery.reconciliation_kind === 'live_display' && recovery.code === 'reconnected') {
      reconnectOperations.set(target.key, {requestKey, requestOrigin: target.requestOrigin, acknowledged: true, acknowledgedBy: 'verified_live_display', recoveryCode: recovery.code, nextAction: recovery.next_action, operationState: 'done'}); persistWorkbench();
      const agent = registeredAgent(target.key), updated = result.agent || result;
      if (agent) { if (updated.host === target.host && updated.thread_id === target.thread_id) Object.assign(agent, updated); agent.recovery = recovery; }
      agentRecoveryNotices.set(target.key, recovery.message || '已核对原显示连接，未重新附着或发送输入。');
      if (terminalSession?.key === target.key) terminalSession.forceSnapshot = true;
      await refresh(true);
      if (reconnectReconcileTarget === target) $('#terminal-reconcile-dialog').close();
      return;
    }
    if (!((recovery.retryable === true || result.retryable === true) && (recovery.operation_state || result.operation_state) === 'blocked')) throw new Error(recovery.message || '原重连结果仍未确认；不会重新附着或新建请求。');
    reconnectOperations.set(target.key, {requestKey, requestOrigin: target.requestOrigin, acknowledged: false, recoveryCode: recovery.code, nextAction: recovery.next_action, operationState: 'blocked'}); persistWorkbench();
    const agent = registeredAgent(target.key); if (agent) agent.recovery = recovery;
    if (target.requestOrigin === 'plugin') {
      agentRecoveryNotices.set(target.key, '原 Captain 插件请求已核验。请让 Captain 沿用同一插件请求键接回；网页不会以 UI 来源重试，也不会发送输入。');
      if (selectedAgent === target.key) renderAgentRecoveryNotice(agent);
      if (reconnectReconcileTarget === target) $('#terminal-reconcile-dialog').close();
      return;
    }
    if (selectedAgent === target.key) { await reconnectTerminal(); if (reconnectReconcileTarget === target) $('#terminal-reconcile-dialog').close(); }
    else toast('原请求核验结果已保留在对应工作会话；选择已改变，未执行重连。');
  } catch (error) { if (reconnectReconcileTarget === target) { $('#terminal-reconcile-error').textContent = error.message; $('#terminal-reconcile-error').hidden = false; } }
  finally { target.submitting = false; $('#terminal-reconcile-confirm').disabled = !$('#terminal-reconcile-key').value; }
}
function showTerminalError(text) { $('#terminal-error').textContent = text; $('#terminal-error').hidden = !text; }
function renderTerminalErrors(session) {
  if (session.kind === 'controller') { window.PronController?.renderTerminalErrors(session); return; }
  if (session.disposed || terminalSession !== session) return;
  showTerminalError(session.inputError || session.resizeError || session.pollError || '');
}
function disposeTerminal() {
  if (!terminalSession) return;
  if (terminalSession.input && !terminalSession.reopening) flushTerminalInput(terminalSession);
  if (terminalSession.input && terminalSession.reopening) reopenDrafts.set(terminalSession.key, terminalSession.input);
  terminalSession.disposed = true; clearTimeout(terminalSession.timer); clearTimeout(terminalSession.inputTimer); clearTimeout(terminalSession.resizeTimer);
  disposeTerminalScroll(terminalSession);
  disposeTerminalInput(terminalSession);
  terminalSession.observer.disconnect(); terminalSession.term.dispose(); terminalSession = null; $('#terminal').replaceChildren(); showTerminalError('');
  hideTerminalContext();
}
function isCurrentTerminal(session) {
  if (session?.kind === 'controller') return !!window.PronController?.isTerminalCurrent(session) && session.initialized && !session.closing && !session.closed;
  return !!session && terminalSession === session && selectedAgent === session.key && !session.disposed && !session.closing && !session.closed && session.initialized;
}
function isTerminalDisplayCurrent(session) { return session?.kind === 'controller' ? !!window.PronController?.isTerminalCurrent(session) : terminalSession === session; }
function canWriteTerminal(session) { return isCurrentTerminal(session) && !session.reopening && isTerminalGenerationCurrent(session) && sessionConnected(session) && !window.PronTerminalHistory?.isActive?.(session) && (session.kind !== 'controller' || window.PronController?.canWriteTerminal(session)); }
function terminalApiPath(session, operation) { return `/api/${session.kind === 'controller' ? 'controllers' : 'terminals'}/${operation}`; }
function terminalApiIdentity(session) { return session.kind === 'controller' ? {id: session.controllerId} : {host: session.agent.host, thread_id: session.agent.thread_id}; }
function terminalMutationIdentity(session) { return {...terminalApiIdentity(session), ...(session.kind === 'controller' ? {} : {expected_terminal_id: session.terminalId || session.agent.terminal_id})}; }
function isTerminalGenerationCurrent(session) { return session.kind === 'controller' || !session.terminalId || registeredAgent(session.key)?.terminal_id === session.terminalId; }
function terminalStatusElement(session) { return $(session.kind === 'controller' ? '#controller-terminal-status' : '#terminal-status'); }
function updateDisplayConnection(session, connected) {
  if (session.kind === 'controller') { window.PronController?.updateDisplayConnection(session, connected); return; }
  // Canonical output can reconcile a lost successful reconnect response for
  // this exact registered display. Never use a cached machine check for this.
  const recoveryOperation = reconnectOperations.get(session.key);
  if (connected && recoveryOperation && !recoveryOperation.acknowledged) { recoveryOperation.acknowledged = true; recoveryOperation.acknowledgedBy = 'connected_output'; agentRecoveryNotices.set(session.key, '已从原终端的实际输出核验显示连接；未重发任务。'); persistWorkbench(); }
  const changed = session.displayConnected !== connected;
  session.displayConnected = connected;
  if (!connected) {
    cancelPendingResize(session); session.resizeError = '';
    clearTimeout(session.inputTimer); session.inputTimer = null; session.input = '';
    session.term.blur();
  }
  if (session.term.options) session.term.options.disableStdin = !connected || session.closing || session.closed || !!window.PronTerminalHistory?.isActive?.(session);
  if (changed && !session.disposed && terminalSession === session) { renderDetail(); renderAgents(); renderHosts(); }
}
function isDisconnectedError(error) { return /(?:Terminal (?:is )?disconnected|SSH transport disconnected)/i.test(error.message || ''); }
function classifyTerminalKey(event, hasSelection) {
  if (event.type !== 'keydown' || event.altKey || event.metaKey) return null;
  const key = String(event.key || '').toLowerCase();
  if (!event.ctrlKey && event.shiftKey && key === 'insert') return 'native-paste';
  if (!event.ctrlKey) return null;
  if (key === 'v') return event.shiftKey ? 'paste' : 'native-paste';
  if (key === 'c') return event.shiftKey ? 'copy' : hasSelection ? 'native-copy' : null;
  return null;
}
function handleTerminalKey(session, event) {
  const pageIntent = classifyTerminalScrollKey(event);
  if (pageIntent) {
    // Navigation is a display action even while disconnected. Do not encode it
    // as an agent key, focus the textarea, fit the terminal, or flush a draft.
    event.preventDefault(); event.stopPropagation();
    scrollTerminalView(session, pageIntent);
    return false;
  }
  const intent = classifyTerminalKey(event, session.term.hasSelection());
  if (!intent) return true;
  // xterm must not turn Ctrl+V into ^V or selected Ctrl+C into ^C.
  // Leaving the browser default intact triggers xterm's native clipboard handler once.
  if (intent.startsWith('native-')) return false;
  event.preventDefault(); event.stopPropagation();
  if (intent === 'copy') void copyTerminal(session); else void pasteTerminal(session);
  return false;
}
function classifyTerminalScrollKey(event) {
  if (event.type !== 'keydown' || event.altKey || event.metaKey || event.ctrlKey) return null;
  if (event.key === 'PageUp') return 'page-up';
  if (event.key === 'PageDown') return 'page-down';
  if (event.shiftKey && event.key === 'Home') return 'top';
  if (event.shiftKey && event.key === 'End') return 'latest';
  return null;
}
function terminalScrollPosition(session) {
  const buffer = session?.term?.buffer?.active;
  if (!buffer) return null;
  const bottom = Math.max(0, Number(buffer.baseY) || 0);
  const position = Math.max(0, Math.min(bottom, Number(buffer.viewportY) || 0));
  return {buffer, position, bottom, following: position >= bottom, alternate: buffer.type === 'alternate'};
}
function terminalScrollControls(session) {
  const prefix = session?.kind === 'controller' ? 'controller' : 'terminal';
  return {up: $(`#${prefix}-page-up`), down: $(`#${prefix}-page-down`), latest: $(`#${prefix}-latest`), status: $(`#${prefix}-scroll-status`)};
}
function renderTerminalScroll(session) {
  if (!session || session.disposed || !isTerminalDisplayCurrent(session)) return;
  const view = terminalScrollPosition(session), controls = terminalScrollControls(session);
  const unavailable = !view;
  if (controls.up) controls.up.disabled = unavailable || view.position <= 0;
  if (controls.down) controls.down.disabled = unavailable || view.following;
  if (controls.latest) controls.latest.disabled = unavailable || view.following;
  if (!controls.status) return;
  const text = !view ? '本地翻页尚未就绪' : view.bottom > 0
    ? view.following ? `正在跟随最新画面 · 本地保留 ${view.bottom} 行历史` : `正在查看历史 · 距最新 ${view.bottom - view.position} 行；新输出不会跳回底部`
    : view.alternate ? session.kind === 'controller'
      ? '当前全屏画面没有本地历史；终端画面不是完整会话记录。'
      : '当前全屏画面没有本地历史；较早的对话请用「会话输出」。'
    : '当前已在最新画面 · 暂无本地历史';
  if (controls.status.textContent !== text) controls.status.textContent = text;
  controls.latest?.setAttribute('aria-pressed', String(!!view?.following));
  window.PronTerminalHistory?.render?.(session);
}
function disposeTerminalScroll(session) {
  window.PronTerminalHistory?.dispose?.(session);
  session.scrollView?.marker?.dispose?.(); session.scrollView = null;
  session.scrollSubscription?.dispose?.(); session.scrollSubscription = null;
  for (const [element, type, handler] of session.scrollGestureListeners || []) element.removeEventListener?.(type, handler, true);
  session.scrollGestureListeners = []; session.scrollGesture = null;
}
function captureTerminalScroll(session) {
  const view = terminalScrollPosition(session); if (!view) return null;
  const anchor = [];
  // Real buffer lines, not a retained stream of TUI redraws. Markers keep a
  // viewport anchored when xterm trims normal scrollback during new output.
  if (!view.following && view.buffer.getLine) for (let line = 0; line < 3; line++) {
    const value = view.buffer.getLine(view.position + line)?.translateToString?.(true);
    if (typeof value === 'string') anchor.push(value);
  }
  const marker = !view.following && !view.alternate && session.term.registerMarker
    ? session.term.registerMarker(view.position - (view.bottom + (Number(view.buffer.cursorY) || 0))) : null;
  return {...view, anchor, marker, distance: view.bottom - view.position};
}
function rememberTerminalScroll(session) {
  const next = captureTerminalScroll(session);
  session.scrollView?.marker?.dispose?.(); session.scrollView = next;
  renderTerminalScroll(session);
}
function initializeTerminalScroll(session) {
  session.scrollIntentVersion = 0;
  const container = session.term.element || $(session.kind === 'controller' ? '#controller-terminal' : '#terminal');
  const markGestureEvent = gesture => {
    gesture.eventActive = true;
    const revision = gesture.eventRevision = (gesture.eventRevision || 0) + 1;
    // xterm's scrollbar reacts synchronously to this captured DOM event.
    // A later parser task is not human input, even if the button is held down
    // and full scrollback trims viewportY without changing capped baseY.
    Promise.resolve().then(() => { if (session.scrollGesture === gesture && gesture.eventRevision === revision) gesture.eventActive = false; });
  };
  const beginGesture = event => {
    if (session.disposed || !isTerminalDisplayCurrent(session) || event.button != null && event.button !== 0) return;
    const scrollbar = event.target?.closest?.('.xterm-scrollable-element .scrollbar, .xterm-viewport');
    if (!scrollbar || container?.contains && !container.contains(scrollbar)) return;
    const view = terminalScrollPosition(session); if (!view) return;
    session.scrollGesture = {buffer: view.buffer, bottom: view.bottom, position: view.position, pointerId: event.pointerId};
    markGestureEvent(session.scrollGesture);
  };
  const moveGesture = event => {
    const gesture = session.scrollGesture;
    if (!gesture || event.pointerId != null && gesture.pointerId != null && event.pointerId !== gesture.pointerId) return;
    markGestureEvent(gesture);
  };
  const endGesture = event => {
    const gesture = session.scrollGesture;
    if (!gesture || event.pointerId != null && gesture.pointerId != null && event.pointerId !== gesture.pointerId) return;
    // A scrollbar's last movement can run later in this same pointer-up event.
    Promise.resolve().then(() => { if (session.scrollGesture === gesture) session.scrollGesture = null; });
  };
  session.scrollGestureListeners = [[container, 'pointerdown', beginGesture], [container, 'mousedown', beginGesture],
    [document, 'pointermove', moveGesture], [document, 'mousemove', moveGesture],
    [document, 'pointerup', endGesture], [document, 'pointercancel', endGesture], [document, 'mouseup', endGesture]].filter(([element]) => element?.addEventListener);
  for (const [element, type, handler] of session.scrollGestureListeners) element.addEventListener(type, handler, true);
  session.scrollSubscription = session.term.onScroll?.(() => {
    const view = terminalScrollPosition(session), gesture = session.scrollGesture;
    // Parser auto-scroll changes baseY (or activates a different buffer). A
    // captured scrollbar drag changes viewportY at unchanged geometry. Only
    // that genuine human gesture can override a pending frame checkpoint.
    const humanGesture = !!gesture?.eventActive && !!view && gesture.buffer === view.buffer && gesture.bottom === view.bottom && gesture.position !== view.position;
    if (gesture && view) { gesture.buffer = view.buffer; gesture.bottom = view.bottom; gesture.position = view.position; }
    if (session.disposed || session.scrollApplying || session.writingFrame && !humanGesture) return;
    session.scrollIntentVersion++; rememberTerminalScroll(session);
  });
  rememberTerminalScroll(session);
}
function restoreTerminalScroll(session, saved, reset = false) {
  const view = terminalScrollPosition(session);
  if (!saved || !view || session.disposed || !isTerminalDisplayCurrent(session)) { saved?.marker?.dispose?.(); renderTerminalScroll(session); return; }
  // Alternate screens are not the normal scrollback history. Do not search or
  // apply a normal-buffer anchor to a newly activated full-screen TUI (or vice
  // versa); retain the newly active buffer's own actual viewport instead.
  if (saved.alternate !== view.alternate) { saved.marker?.dispose?.(); rememberTerminalScroll(session); return; }
  let target = view.bottom;
  if (!saved.following) {
    if (!reset && saved.buffer === view.buffer) target = saved.marker && !saved.marker.isDisposed ? saved.marker.line : saved.marker?.isDisposed ? 0 : saved.position;
    else {
      target = Math.max(0, view.bottom - saved.distance);
      // A canonical snapshot/reset invalidates markers. Recover the retained
      // top lines if they still exist; otherwise retain the distance to latest.
      if (saved.anchor?.some(line => line.trim()) && view.buffer.getLine) {
        let closest = null;
        for (let position = 0; position <= view.bottom; position++) {
          if (saved.anchor.every((line, offset) => view.buffer.getLine(position + offset)?.translateToString?.(true) === line)
            && (closest === null || Math.abs(position - target) < Math.abs(closest - target))) closest = position;
        }
        if (closest !== null) target = closest;
      }
    }
  }
  target = Math.max(0, Math.min(view.bottom, Number(target) || 0));
  session.scrollApplying = true;
  try { if (target !== view.position) session.term.scrollToLine?.(target); }
  finally { session.scrollApplying = false; saved.marker?.dispose?.(); }
  rememberTerminalScroll(session);
}
function scrollTerminalView(session, intent) {
  if (!session || session.disposed || !isTerminalDisplayCurrent(session)) return;
  if (window.PronTerminalHistory?.page?.(session, intent)) return;
  const view = terminalScrollPosition(session); if (!view) { renderTerminalScroll(session); return; }
  session.scrollIntentVersion = (session.scrollIntentVersion || 0) + 1;
  session.wheelRemainder = 0;
  const page = Math.max(1, (Number(session.term.rows) || 1) - 1);
  const target = intent === 'latest' ? view.bottom : intent === 'top' ? 0 : view.position + (intent === 'page-up' ? -page : page);
  const amount = Math.max(0, Math.min(view.bottom, target)) - view.position;
  if (amount) session.term.scrollLines?.(amount);
  rememberTerminalScroll(session);
}
function terminalWheelLineHeight(term) {
  const screen = term.element?.querySelector('.xterm-screen');
  const measured = Number(screen?.getBoundingClientRect?.().height) / Math.max(1, Number(term.rows) || 1);
  if (Number.isFinite(measured) && measured > 0) return measured;
  return Math.max(1, (Number(term.options?.fontSize) || 13) * (Number(term.options?.lineHeight) || 1.15));
}
function handleTerminalWheel(session, event) {
  // This capture handler runs before xterm's viewport and mouse listeners.
  // xterm otherwise turns wheels in alternate buffers into cursor-key input,
  // or into mouse/SGR reports. Wheels here only change the local display.
  event.stopImmediatePropagation();
  if (!session || session.disposed || !isTerminalDisplayCurrent(session) || event.ctrlKey) return;
  if (window.PronTerminalHistory?.wheel?.(session, event)) return;
  const delta = Number(event.deltaY); if (!Number.isFinite(delta) || delta === 0) return;
  const buffer = session.term.buffer?.active; if (!buffer) return;
  if (session.wheelBuffer !== buffer) { session.wheelBuffer = buffer; session.wheelRemainder = 0; }
  const position = Math.max(0, Number(buffer.viewportY) || 0), bottom = Math.max(0, Number(buffer.baseY) || 0);
  if ((delta < 0 && position <= 0) || (delta > 0 && position >= bottom)) {
    // Do not trap the page at the terminal's scrollback boundary. Propagation
    // remains stopped so xterm cannot send input, but browser scrolling is free.
    session.wheelRemainder = 0; return;
  }
  const units = event.deltaMode === 1 ? 1 : event.deltaMode === 2 ? Math.max(1, Number(session.term.rows) || 1) : 1 / terminalWheelLineHeight(session.term);
  session.wheelRemainder = (session.wheelRemainder || 0) + delta * units;
  const wholeLines = Math.trunc(session.wheelRemainder);
  event.preventDefault();
  if (!wholeLines) return;
  session.wheelRemainder -= wholeLines;
  const amount = Math.max(-position, Math.min(wholeLines, bottom - position));
  if (amount) {
    session.scrollIntentVersion = (session.scrollIntentVersion || 0) + 1;
    session.term.scrollLines(amount); rememberTerminalScroll(session);
  }
}
function openClipboardFallback(session, mode, text = '') {
  if (!isCurrentTerminal(session) || mode === 'paste' && !canWriteTerminal(session)) { toast('终端已切换、关闭或断开，本次剪贴板操作已取消。'); return; }
  clipboardTarget = {session, mode};
  $('#clipboard-heading').textContent = mode === 'copy' ? '手动复制选中文字' : '手动粘贴到这个终端';
  $('#clipboard-note').textContent = mode === 'copy' ? '浏览器未允许写入剪贴板。下面已选中终端文本，请按 Ctrl+C 复制。' : '浏览器未允许直接读取剪贴板。请在下面输入框按 Ctrl+V，然后点击粘贴；目标仍是刚才选中的工作终端。';
  $('#clipboard-text').value = text; $('#clipboard-text').readOnly = mode === 'copy';
  $('#clipboard-confirm').textContent = mode === 'copy' ? '完成' : '粘贴到这个终端';
  $('#clipboard-dialog').showModal(); $('#clipboard-text').focus(); if (mode === 'copy') $('#clipboard-text').select();
}
async function copyTerminal(session = terminalSession) {
  if (!isCurrentTerminal(session)) { toast('请先连接一个正在运行的终端。'); return; }
  const text = window.PronTerminalHistory?.isActive?.(session)
    ? window.PronTerminalHistory?.selection?.(session) || '' : session.term.getSelection();
  if (!text) { toast('请先用鼠标选中要复制的终端文字。'); return; }
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
    await navigator.clipboard.writeText(text); toast('终端选中文字已复制。');
  } catch (error) { openClipboardFallback(session, 'copy', text); }
}
async function pasteTerminal(session = terminalSession) {
  if (!canWriteTerminal(session)) { toast('请先重新连接这个终端，再粘贴输入。'); return; }
  if (session.pasteReading) return;
  session.pasteReading = true;
  try {
    if (!navigator.clipboard?.readText) throw new Error('Clipboard unavailable');
    const text = await navigator.clipboard.readText();
    if (!canWriteTerminal(session)) { toast('终端已切换、关闭或断开，本次粘贴已取消。'); return; }
    if (!text) { toast('剪贴板没有文本。'); return; }
    session.term.paste(text); session.term.focus();
  } catch (error) { openClipboardFallback(session, 'paste'); }
  finally { session.pasteReading = false; }
}
function hideTerminalContext() { $('#terminal-context-menu').hidden = true; contextTerminal = null; }
function showTerminalContext(event) {
  if (event.shiftKey) return;
  event.preventDefault();
  const session = terminalSession; if (!isCurrentTerminal(session)) { hideTerminalContext(); return; }
  contextTerminal = session;
  $('#context-copy').disabled = !session.term.hasSelection();
  const menu = $('#terminal-context-menu'); menu.hidden = false;
  menu.style.left = `${Math.max(6, Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 6))}px`;
}
function safeFit(session) {
  if (session.disposed || !isTerminalDisplayCurrent(session) || !sessionConnected(session)) return;
  if (window.PronTerminalHistory?.isActive?.(session)) return;
  if (session.kind === 'controller' && !window.PronController?.isDisplayVisible(session)) return;
  if (session.writingFrame) { session.fitPending = true; return; }
  session.fitPending = false;
  const view = captureTerminalScroll(session);
  session.fit.fit();
  restoreTerminalScroll(session, view);
}
async function writeTerminalFrame(session, data, reset, cols, rows) {
  const view = captureTerminalScroll(session), scrollVersion = session.scrollIntentVersion || 0;
  session.writingFrame = true;
  session.restoringFrame = !!reset;
  try {
    if (reset) {
      session.term.reset();
      if (cols >= 2 && rows >= 2) session.term.resize(cols, rows);
    }
    await new Promise(resolve => { if (data) session.term.write(data, resolve); else resolve(); });
  } finally {
    session.restoringFrame = false;
    session.writingFrame = false;
    if (scrollVersion === (session.scrollIntentVersion || 0)) restoreTerminalScroll(session, view, reset);
    else { view?.marker?.dispose?.(); rememberTerminalScroll(session); }
  }
}
function installTerminalProtocolGuards(term) {
  // Only the canonical broker terminal answers remote queries. In particular,
  // DECRQM ($p) otherwise generates a browser onData mode report with no key.
  for (const identifier of [{final: 'n'}, {prefix: '?', final: 'n'}, {final: 'c'}, {prefix: '>', final: 'c'}, {final: 't'},
    {intermediates: '$', final: 'p'}, {prefix: '?', intermediates: '$', final: 'p'}]) term.parser.registerCsiHandler(identifier, () => true);
  for (const identifier of [{intermediates: '$', final: 'q'}, {intermediates: '+', final: 'q'}]) term.parser.registerDcsHandler(identifier, () => true);
  for (const identifier of [4, 10, 11, 12]) term.parser.registerOscHandler(identifier, data => data.split(';').includes('?'));
}
function initializeTerminalInput(session) {
  // xterm emits focus reports synchronously from its textarea DOM listeners.
  // Do not classify every identical byte string as protocol: a real paste can
  // contain those exact bytes. The captured event scopes this narrow filter.
  const container = session.term.element, textarea = session.term.textarea;
  const markFocus = event => {
    if (session.disposed || !textarea || event.target !== textarea) return;
    const marker = {data: event.type === 'focus' ? '\x1b[I' : '\x1b[O'};
    session.focusProtocolEvent = marker;
    Promise.resolve().then(() => { if (session.focusProtocolEvent === marker) session.focusProtocolEvent = null; });
  };
  if (container?.addEventListener && textarea) {
    container.addEventListener('focus', markFocus, true); container.addEventListener('blur', markFocus, true);
    session.inputFocusListeners = {container, markFocus};
  }
  session.term.onData(data => {
    if (session.focusProtocolEvent?.data === data) { session.focusProtocolEvent = null; return; }
    if (!canWriteTerminal(session)) return;
    session.input += data;
    if (!session.inputTimer) session.inputTimer = setTimeout(() => flushTerminalInput(session), 12);
  });
}
function disposeTerminalInput(session) {
  const listeners = session.inputFocusListeners;
  if (listeners) {
    listeners.container.removeEventListener('focus', listeners.markFocus, true);
    listeners.container.removeEventListener('blur', listeners.markFocus, true);
  }
  session.inputFocusListeners = null; session.focusProtocolEvent = null;
}
async function openTerminal(agent, options = {}) {
  if (terminalSession?.key === agentKey(agent)) return;
  disposeTerminal();
  if (!window.Terminal || !window.FitAddon?.FitAddon) throw new Error('终端组件未加载。请刷新页面；若仍失败，请检查后台的本地 xterm 资源。');
  const term = new window.Terminal({fontFamily: 'Cascadia Mono, Consolas, monospace', fontSize: 13, lineHeight: 1.15, cursorBlink: true, scrollback: 5000, convertEol: false, allowProposedApi: true, theme: {background: '#101517', foreground: '#dde7e0', cursor: '#b9e9c7', selectionBackground: '#45634f99'}});
  installTerminalProtocolGuards(term);
  const fit = new window.FitAddon.FitAddon(); term.loadAddon(fit); term.open($('#terminal'));
  const session = {key: agentKey(agent), agent, term, fit, seq: 0, initialized: false, disposed: false, displayConnected: agent.connected !== false, closing: false, closed: isClosedAgent(agent), pasteReading: false, wheelRemainder: 0, wheelBuffer: null, frameGeneration: 0, forceSnapshot: true, writingFrame: false, restoringFrame: false, fitPending: false, resizeGeneration: 0, resizePending: false, geometryUnknown: false, resizeRecoveryAttempts: 0, resizeRecoveryRetryAt: 0, targetSize: '', timer: null, inputTimer: null, resizeTimer: null, input: '', inputChain: Promise.resolve(), size: '', observer: null, inputError: '', pollError: '', resizeError: ''};
  session.terminalId = agent.terminal_id;
  session.reopening = !!pendingReopen(agent);
  if (term.options) term.options.disableStdin = session.reopening;
  terminalSession = session;
  initializeTerminalScroll(session);
  term.attachCustomKeyEventHandler(event => handleTerminalKey(session, event));
  term.attachCustomWheelEventHandler?.(() => false);
  initializeTerminalInput(session);
  term.onResize(({cols, rows}) => scheduleResize(session, cols, rows));
  session.observer = new ResizeObserver(() => { if (!session.disposed && $('#terminal').clientWidth > 0) safeFit(session); }); session.observer.observe($('#terminal'));
  requestAnimationFrame(() => safeFit(session));
  await pollTerminal(session);
  if (options.focus && !session.disposed && !session.closed && sessionConnected(session)) term.focus();
}
function flushTerminalInput(session) {
  clearTimeout(session.inputTimer); session.inputTimer = null;
  if (session.reopening) return;
  const data = session.input; session.input = ''; if (!data || session.disposed || session.closing || session.closed || !sessionConnected(session)) return;
  const body = {...terminalMutationIdentity(session), data, request_key: crypto.randomUUID()};
  session.inputChain = session.inputChain.then(() => {
    if (session.reopening || !isTerminalGenerationCurrent(session)) {
      session.reopenQueuedDraft = (session.reopenQueuedDraft || '') + data;
      reopenDrafts.set(session.key, session.reopenQueuedDraft + (session.reopenBufferedDraft || '')); return;
    }
    return session.closing || session.closed || !sessionConnected(session) || session.kind === 'controller' && !canWriteTerminal(session) ? undefined : request(terminalApiPath(session, 'input'), body);
  }).then(() => { session.inputError = ''; renderTerminalErrors(session); }).catch(error => {
    if (isDisconnectedError(error)) updateDisplayConnection(session, false);
    session.inputError = `输入未确认送达：${error.message}。请核对终端后再操作，避免重复输入。`;
    renderTerminalErrors(session);
  });
}
function cancelPendingResize(session) {
  if (!session.resizePending) return;
  clearTimeout(session.resizeTimer); session.resizeTimer = null; session.resizePending = false; session.targetSize = '';
  session.resizeDesired = null;
  session.resizeGeneration = (session.resizeGeneration || 0) + 1;
  session.frameGeneration = (session.frameGeneration || 0) + 1;
  session.forceSnapshot = true;
}
function scheduleResize(session, cols, rows) {
  if (session.disposed || session.reopening || !isTerminalGenerationCurrent(session) || session.closing || session.closed || session.restoringFrame || !session.initialized || !sessionConnected(session) || cols < 2 || rows < 2) return;
  if (window.PronTerminalHistory?.isActive?.(session)) return;
  if (session.kind === 'controller' && !window.PronController?.isDisplayVisible(session)) return;
  const size = `${cols}:${rows}`;
  const recoveringGeometry = session.geometryUnknown === true;
  // A canonical snapshot only establishes how to parse the frame, not that the
  // remote PTY accepted its size. An uncertain resize needs a verified resize
  // ACK even when that snapshot reports the same dimensions as the browser.
  if ((session.resizePending && session.targetSize === size) || (!session.resizePending && session.size === size && !recoveringGeometry)) return;
  if (recoveringGeometry && (session.resizeRecoveryAttempts || 0) >= 3) {
    session.resizeError = '终端尺寸仍未同步，自动重试已暂停。请检查机器连接后点「刷新画面」重试；不会重启工作会话。';
    renderTerminalErrors(session); return;
  }
  if (recoveringGeometry && Date.now() < (session.resizeRecoveryRetryAt || 0)) return;
  session.targetSize = size;
  const generation = session.resizeGeneration = (session.resizeGeneration || 0) + 1;
  session.frameGeneration = (session.frameGeneration || 0) + 1;
  session.resizePending = true;
  session.resizeDesired = {cols, rows, size, generation};
  if (!session.resizeInFlight && !session.resizeWaitingForFlight) queueTerminalResize(session);
}
function queueTerminalResize(session) {
  clearTimeout(session.resizeTimer); session.resizeTimer = setTimeout(async () => {
    session.resizeTimer = null;
    if (session.disposed || session.reopening || !isTerminalGenerationCurrent(session) || !isTerminalDisplayCurrent(session) || !session.resizeDesired) return;
    if (window.PronTerminalHistory?.isActive?.(session)) { cancelPendingResize(session); return; }
    if (session.closing || session.closed || !sessionConnected(session)) { cancelPendingResize(session); return; }
    // A disposed/reopened display can still have a request in flight for the
    // same registered terminal. Wait for it too; local generations cannot
    // cancel a resize already queued in the broker.
    const flightKey = `${terminalApiPath(session, 'resize')}:${JSON.stringify(terminalApiIdentity(session))}`;
    const existing = terminalResizeFlights.get(flightKey);
    if (existing) {
      session.resizeWaitingForFlight = existing;
      existing.then(() => {
        if (session.resizeWaitingForFlight !== existing) return;
        session.resizeWaitingForFlight = null;
        if (!session.disposed && isTerminalDisplayCurrent(session) && session.resizeDesired && !session.closing && !session.closed && sessionConnected(session)) queueTerminalResize(session);
      });
      return;
    }
    const {cols, rows, size, generation} = session.resizeDesired;
    const recoveringGeometry = session.geometryUnknown === true;
    let finishFlight;
    const flight = new Promise(resolve => { finishFlight = resolve; });
    terminalResizeFlights.set(flightKey, flight); session.resizeInFlight = true;
    if (recoveringGeometry) session.resizeRecoveryAttempts = (session.resizeRecoveryAttempts || 0) + 1;
    try {
      await request(terminalApiPath(session, 'resize'), {...terminalMutationIdentity(session), cols, rows});
      if (session.disposed || session.reopening || !isTerminalGenerationCurrent(session) || !isTerminalDisplayCurrent(session) || generation !== session.resizeGeneration) return;
      session.size = size; session.resizePending = false; session.resizeDesired = null; session.resizeError = '';
      session.geometryUnknown = false; session.resizeRecoveryAttempts = 0; session.resizeRecoveryRetryAt = 0;
      session.frameGeneration = (session.frameGeneration || 0) + 1;
      session.forceSnapshot = true; renderTerminalErrors(session);
    } catch (error) {
      if (session.disposed || session.reopening || !isTerminalGenerationCurrent(session) || !isTerminalDisplayCurrent(session) || generation !== session.resizeGeneration) return;
      session.resizePending = false; session.resizeDesired = null; session.targetSize = ''; session.forceSnapshot = true;
      if (recoveringGeometry) session.resizeRecoveryRetryAt = Date.now() + Math.min(30000, 2000 * 2 ** (session.resizeRecoveryAttempts - 1));
      if (isDisconnectedError(error)) { updateDisplayConnection(session, false); renderTerminalErrors(session); return; }
      session.resizeError = `调整终端尺寸失败：${error.message}`; renderTerminalErrors(session);
    } finally {
      session.resizeInFlight = false;
      if (terminalResizeFlights.get(flightKey) === flight) terminalResizeFlights.delete(flightKey);
      finishFlight();
      // While this request ran, retain only the final requested dimensions.
      // Obsolete ACKs cannot clear that pending generation's geometry fence.
      if (!session.disposed && isTerminalDisplayCurrent(session) && session.resizeDesired && !session.closing && !session.closed && sessionConnected(session)) queueTerminalResize(session);
    }
  }, 140);
}
async function pollTerminal(session) {
  if (session.disposed || session.reopening || !isTerminalGenerationCurrent(session)) return;
  if (session.kind === 'controller' && !window.PronController?.canReadDisplay(session)) return;
  if (session.resizePending) { session.timer = setTimeout(() => pollTerminal(session), 200); return; }
  let delay = 350;
  const generation = session.frameGeneration || 0, snapshot = !!session.forceSnapshot || !session.initialized;
  try {
    const query = new URLSearchParams({...terminalApiIdentity(session), after: String(snapshot ? 0 : session.seq)});
    const result = await request(terminalApiPath(session, 'output') + '?' + query);
    if (session.disposed || session.reopening || !isTerminalGenerationCurrent(session) || !isTerminalDisplayCurrent(session) || generation !== (session.frameGeneration || 0) || session.resizePending) return;
    if (session.kind !== 'controller' && result.terminal_id && result.terminal_id !== session.terminalId) { session.pollError = '终端代次已变化，旧画面不再更新。请核对原重开请求。'; return; }
    const reset = result.reset || snapshot;
    if (!reset && result.cols >= 2 && result.rows >= 2 && session.size && session.size !== `${result.cols}:${result.rows}`) {
      session.frameGeneration = (session.frameGeneration || 0) + 1; session.forceSnapshot = true;
      return;
    }
    const data = result.reset && result.ansi != null ? result.ansi : result.data ?? result.ansi ?? '';
    await writeTerminalFrame(session, data, reset, result.cols, result.rows);
    if (session.disposed || session.reopening || !isTerminalGenerationCurrent(session) || !isTerminalDisplayCurrent(session) || generation !== (session.frameGeneration || 0) || session.resizePending) return;
    session.seq = Number(result.seq ?? result.current_seq ?? session.seq);
    session.forceSnapshot = false;
    if (reset && result.cols >= 2 && result.rows >= 2) session.size = `${result.cols}:${result.rows}`;
    // Older snapshot adapters expose the same fence as blocked_reason instead
    // of geometry_unknown. Neither pixels nor matching dimensions clear it.
    session.geometryUnknown = result.geometry_unknown === true || result.blocked_reason === 'geometry_unknown';
    if (!session.geometryUnknown) { session.resizeRecoveryAttempts = 0; session.resizeRecoveryRetryAt = 0; }
    session.initialized = true;
    const outputState = result.status || result.state;
    if (typeof result.connected === 'boolean') updateDisplayConnection(session, result.connected);
    if (['exited', 'disconnected', 'not_connected'].includes(outputState)) updateDisplayConnection(session, false);
    if (['closed', 'closing', 'close_unknown'].includes(outputState) || ['closed', 'closing', 'unknown'].includes(result.close_state)) {
      session.closed = outputState === 'closed' || result.close_state === 'closed'; session.closing = true; session.term.blur(); if (session.kind === 'controller') window.PronController?.render(); else renderDetail();
    }
    if (result.connected === false || ['exited', 'closed', 'disconnected', 'closing', 'close_unknown'].includes(outputState)) {
      terminalStatusElement(session).textContent = ['closed', 'closing', 'close_unknown'].includes(outputState) ? statusLabels[outputState] : disconnectedLabel; delay = 1500;
    } else terminalStatusElement(session).textContent = result.command ? `终端已连接 · ${result.command}` : '终端已连接';
    session.pollError = result.error || ''; renderTerminalErrors(session);
    if (sessionConnected(session)) { safeFit(session); scheduleResize(session, session.term.cols, session.term.rows); }
  } catch (error) {
    if (session.disposed || session.reopening || !isTerminalGenerationCurrent(session) || !isTerminalDisplayCurrent(session)) return;
    terminalStatusElement(session).textContent = '终端连接暂时不可用';
    session.pollError = ['hub_unreachable', 'ui_session_expired', 'hub_response_invalid'].includes(error.code) ? error.message : `${error.message}。可尝试「重新连接」；重新连接不会自动创建第二个终端。`;
    renderTerminalErrors(session); delay = 2000;
  } finally {
    if (!session.disposed && !session.reopening && isTerminalGenerationCurrent(session) && isTerminalDisplayCurrent(session) && (session.kind !== 'controller' || window.PronController?.canReadDisplay(session))) session.timer = setTimeout(() => pollTerminal(session), delay);
  }
}
async function reconnectTerminal(options = {}) {
  const agent = currentAgent(); if (!isTerminal(agent)) return;
  if (pendingReopen(agent)) { openReopenTerminal(agent); return; }
  if (agent.recovery?.code === 'reconnect_unknown' && canReconcileReconnect(agent)) { openReconnectReconciliation(); return; }
  if (!displayDisconnected(agent)) { disposeTerminal(); await openTerminal(agent); return; }
  const key = agentKey(agent), previous = reconnectOperations.get(key);
  if ((agent.recovery?.request_origin === 'plugin' && agent.recovery?.retryable === true) || (previous?.requestOrigin === 'plugin' && !previous.acknowledged)) {
    agentRecoveryNotices.set(key, '原重连属于 Captain 插件，请让 Captain 沿用同一插件请求键继续接回；网页不会以 UI 来源重试。'); renderAgentRecoveryNotice(agent); return;
  }
  const unknown = ['unknown', 'reconnect_unknown', 'delivery_unknown'].includes(agent.recovery?.code);
  if (unknown && canReconcileReconnect(agent)) { openReconnectReconciliation(); return; }
  if (unknown && (!previous || previous.acknowledged && previous.acknowledgedBy !== 'connected_output')) {
    agentRecoveryNotices.set(key, '原重连结果未确认且本浏览器没有可核验的原请求键。请核对原终端；不会创建新键绕过未知结果，也不会重发输入。'); renderAgentRecoveryNotice(agent); return;
  }
  // This recovery belongs to the exact current registry object. Only a known
  // pre-attach UI failure can repopulate lost browser state; plugin/controller
  // keys have a different fingerprint and are never adopted as a UI request.
  const restoredUiKey = needsDisplayServiceRecovery(agent.recovery) && agent.recovery?.retryable === true && agent.recovery.request_origin === 'ui' && typeof agent.recovery.request_key === 'string' && agent.recovery.request_key ? agent.recovery.request_key : null;
  const operation = previous && !previous.acknowledged ? previous : {requestKey: restoredUiKey || crypto.randomUUID(), acknowledged: false}; reconnectOperations.set(key, operation); persistWorkbench();
  let result;
  try { result = await request('/api/agents/reconnect', {host: agent.host, thread_id: agent.thread_id, request_key: operation.requestKey}); }
  catch (error) {
    const recovery = error.recovery;
    const message = recovery?.message || `重连结果未确认：${error.message}。保留原请求键，请核对这个终端；不会新建、resume 或重发任务。`;
    agentRecoveryNotices.set(key, message); agent.recovery = recovery || {code: 'unknown', message};
    operation.recoveryCode = agent.recovery.code; operation.nextAction = agent.recovery.next_action; operation.operationState = agent.recovery.operation_state; persistWorkbench();
    if (selectedAgent === key) renderAgentRecoveryNotice(agent);
    if (selectedAgent === key && needsDisplayServiceRecovery(recovery)) { if (!options.afterServiceRecovery) await openDisplayServiceRecovery(); return {recovery}; }
    throw error;
  }
  const updated = result.agent || result;
  if (updated.host === agent.host && updated.thread_id === agent.thread_id) Object.assign(agent, updated);
  const recovery = result.recovery || {};
  agent.recovery = recovery; operation.recoveryCode = recovery.code; operation.nextAction = recovery.next_action; operation.operationState = recovery.operation_state;
  agentRecoveryNotices.set(key, recovery.message || '已核对原终端的重连请求。重新连接不代表 Codex 已恢复或任务已完成。');
  if (selectedAgent === key) renderAgentRecoveryNotice(agent);
  if (!['unknown', 'reconnect_unknown', 'delivery_unknown'].includes(recovery.code) && !needsReconnectReconciliation(recovery) && !(recovery.retryable === true && recovery.operation_state === 'blocked') && !needsDisplayServiceRecovery(recovery)) operation.acknowledged = true;
  persistWorkbench();
  await refresh(true);
  if (selectedAgent !== key) return result;
  const current = currentAgent() || agent;
  const connected = result.connected === true || updated.connected === true || ['connected', 'already_connected', 'reattached', 'reconnected'].includes(recovery.code);
  if (connected) { disposeTerminal(); await openTerminal(current); }
  else { if (terminalSession?.key === key) updateDisplayConnection(terminalSession, false); renderDetail(); if (needsDisplayServiceRecovery(recovery) && !options.afterServiceRecovery) await openDisplayServiceRecovery(); }
  return result;
}
function messageText(message) { if (message.text != null) return String(message.text); if (typeof message.content === 'string') return message.content; return (message.content || []).map(item => item.text || '').filter(Boolean).join('\n'); }
function resetConversationOutput() {
  outputGeneration++; outputView = null;
  $('#conversation-output-panel').hidden = true;
  $('#terminal-output').setAttribute('aria-expanded', 'false');
  $('#output-messages').replaceChildren(); $('#output-signals').replaceChildren();
  $('#output-source').textContent = ''; $('#output-note').textContent = '';
  $('#output-more').hidden = true; $('#output-pagination-note').textContent = '';
  $('#output-refresh').disabled = false; $('#output-more').disabled = false;
  $('#output-include-tools').checked = false; $('#output-include-tools').disabled = false;
}
function renderOutputBinding(agent) {
  if (outputView?.key !== agentKey(agent) || outputView.result) return;
  const binding = agent.output_binding || {};
  $('#output-source').textContent = binding.session_id ? `已记录关联 · ${binding.session_id}（尚未验证当前运行会话）` : '实际会话输出尚未读取';
  $('#output-note').textContent = binding.reason || '点击刷新，只读取这个终端对应的会话记录；不会输入命令或扫描其他会话。';
}
function outputChunkKey(item) {
  if (!item.message_id) return null;
  return `${item.message_id}:${item.byte_offset ?? ''}:${item.chunk_offset ?? 0}`;
}
function appendOutputMessages(view, messages) {
  const container = $('#output-messages');
  if (!view.messageCount && messages.length) container.replaceChildren();
  for (const message of messages) {
    const key = outputChunkKey(message); if (key && view.messageKeys.has(key)) continue;
    if (key) view.messageKeys.add(key);
    const article = node('article', null, 'message ' + (message.role === 'user' ? 'user' : message.role === 'tool' ? 'tool' : 'assistant'));
    article.dataset.messageId = message.message_id || ''; article.dataset.chunkOffset = String(message.chunk_offset ?? 0);
    const label = node('div', null, 'message-label');
    label.append(node('strong', message.role === 'user' ? '用户' : message.role === 'assistant' ? '工作 agent' : message.role === 'tool' ? '工具记录' : message.role || '记录'), node('span', dateText(message.time || message.timestamp)));
    if ((message.chunk_offset || 0) > 0 || message.content_complete === false) {
      const continuation = message.has_more_content === true ? ' · 后文待继续读取' : message.has_more_content === false ? ' · 已读到该消息末尾' : ' · 请核对分页状态';
      label.append(node('span', `消息片段 · 字符 ${message.chunk_offset || 0}${message.total_chars != null ? ' / ' + message.total_chars : ''}${continuation}`, 'output-chunk-label'));
    }
    const body = node('div', null, 'message-body'); formatted(body, messageText(message));
    article.append(label, body); container.append(article); view.messageCount++;
  }
  if (!view.messageCount) { container.replaceChildren(); empty(container, '当前读取范围没有对话消息。空结果不代表工作完成。'); }
}
function appendOutputSignals(view, signals) {
  const container = $('#output-signals');
  if (!view.signalCount && signals.length) container.replaceChildren();
  for (const signal of signals) {
    const key = outputChunkKey(signal); if (key && view.signalKeys.has(key)) continue;
    if (key) view.signalKeys.add(key);
    const box = node('article', null, 'signal');
    box.dataset.chunkOffset = String(signal.chunk_offset ?? 0);
    box.append(node('div', `${signal.type || '输出'} · ${dateText(signal.time || signal.timestamp)}${signal.chunk_offset ? ' · 续片 ' + signal.chunk_offset : ''}`, 'message-label'), node('pre', signal.text || JSON.stringify(signal, null, 2)));
    container.append(box); view.signalCount++;
  }
  if (!view.signalCount) { container.replaceChildren(); empty(container, '当前读取范围暂无记录的工具输出。'); }
}
function renderConversationOutput(view, result) {
  const data = result.thread || result, binding = result.output_binding || data.output_binding || {}, actual = result.source === 'codex_rollout' || data.source === 'codex_rollout';
  view.sessionId = actual ? binding.session_id || data.session_id || null : null;
  view.result = result; view.nextCursor = result.next_cursor ?? data.next_cursor ?? null;
  view.afterCursor = result.after_cursor ?? data.after_cursor ?? null;
  const hasMore = result.has_more ?? data.has_more ?? false;
  const verifiedLive = binding.live_session_verified === true;
  $('#output-source').textContent = actual ? `${verifiedLive ? '真实会话记录' : '已关联的会话记录'} · ${binding.session_id || data.session_id || '已关联会话'}` : '未读到实际会话输出 · 当前仅可获取终端画面';
  if (!actual) {
    view.nextCursor = null; view.messageCount = 0; view.signalCount = 0;
    $('#output-messages').replaceChildren(); $('#output-signals').replaceChildren();
    empty($('#output-messages'), '这里不会把终端当前画面当成完整回复。请使用「关联会话」提供这个终端中 Codex 的精确会话 ID，再读取实际记录。');
    $('#output-note').textContent = [binding.reason, data.note || result.note, '没有读到完整记录，不能据此认定报告完整或任务完成。'].filter(Boolean).join(' ');
  } else {
    appendOutputMessages(view, data.messages || result.messages || []);
    appendOutputSignals(view, data.recent_signals || result.recent_signals || []);
    const incomplete = data.content_complete === false || result.content_complete === false;
    const truncated = data.truncated || result.truncated;
    $('#output-note').textContent = [data.note || result.note, !verifiedLive ? '当前进程未暴露会话 ID；若在 Codex 内切换会话，请重新关联，不将此记录认作新会话结果。' : '', incomplete ? '本页含分段内容，请按片段与分页标识核对。' : '', hasMore ? '还有记录可继续读取。' : truncated ? '返回范围有限，不是完整历史。' : '已读到本次范围的末尾；会话记录不证明任务成功，也不证明终端当前在线。'].filter(Boolean).join(' ');
  }
  $('#output-more').hidden = !actual || !hasMore || !view.nextCursor;
  $('#output-pagination-note').textContent = actual && hasMore ? view.nextCursor ? '继续读取会保留当前消息与片段顺序。' : '还有记录，但后台未返回继续读取标识，请刷新后重试。' : actual ? '刷新最新输出会重新读取最新范围。' : '请先关联当前工作终端的会话。';
}
async function readConversationOutput(append = false) {
  const agent = currentAgent(); if (!isTerminal(agent)) return;
  const key = agentKey(agent);
  if (!outputView || outputView.key !== key || outputView.terminalId !== agent.terminal_id) outputView = {key, terminalId: agent.terminal_id, open: true, busy: false, messageKeys: new Set(), signalKeys: new Set(), messageCount: 0, signalCount: 0};
  const view = outputView; if (view.busy || append && !view.nextCursor) return;
  const previousData = view.result?.thread || view.result;
  const hasSuccessfulPage = view.result?.source === 'codex_rollout' || previousData?.source === 'codex_rollout';
  const requestedTools = append ? !!view.includeTools : !!$('#output-include-tools').checked;
  if (!append && !hasSuccessfulPage) {
    $('#output-messages').replaceChildren(node('p', '正在读取这个终端的实际会话记录……', 'empty-copy')); $('#output-signals').replaceChildren();
    $('#output-source').textContent = '正在确认并读取实际会话输出……'; $('#output-note').textContent = '';
    $('#output-more').hidden = true; $('#output-pagination-note').textContent = '';
  } else if (!append) {
    $('#output-note').textContent = '正在刷新最新输出；读取成功前保留上次成功读取的页面。';
  }
  const generation = ++outputGeneration, body = {host: agent.host, thread_id: agent.thread_id, limit: 30};
  body.include_tools = requestedTools;
  if (append) body.cursor = view.nextCursor;
  view.open = true; view.busy = true;
  $('#conversation-output-panel').hidden = false; $('#terminal-output').setAttribute('aria-expanded', 'true');
  $('#output-refresh').disabled = true; $('#output-more').disabled = true; $('#output-include-tools').disabled = true;
  try {
    const result = await request('/api/agents/read', body);
    if (selectedAgent !== key || currentAgent()?.terminal_id !== view.terminalId || outputView !== view || generation !== outputGeneration) return;
    const returnedSession = result.output_binding?.session_id || result.thread?.output_binding?.session_id || result.thread?.session_id || result.session_id;
    if (append && view.sessionId && returnedSession && returnedSession !== view.sessionId) throw new Error('会话关联已变化，请刷新最新输出后再继续读取，不会混合两个会话的内容');
    const returnedData = result.thread || result;
    const actual = result.source === 'codex_rollout' || returnedData.source === 'codex_rollout';
    if (hasSuccessfulPage && !actual) {
      const binding = result.output_binding || returnedData.output_binding || {};
      throw Object.assign(new Error(binding.reason || returnedData.note || result.note || '本次未读取到真实会话输出'), {code: binding.reason_code});
    }
    // Commit a new range only after a successful response. Failed refreshes
    // keep the previous cursor, deduplication keys and its exact tool mode.
    if (!append) {
      view.result = null; view.nextCursor = null; view.sessionId = null; view.messageKeys.clear(); view.signalKeys.clear(); view.messageCount = 0; view.signalCount = 0;
      $('#output-messages').replaceChildren(); $('#output-signals').replaceChildren();
    }
    view.includeTools = requestedTools; $('#output-include-tools').checked = requestedTools;
    renderConversationOutput(view, result);
  } catch (error) {
    if (selectedAgent !== key || currentAgent()?.terminal_id !== view.terminalId || outputView !== view || generation !== outputGeneration) return;
    const readBusy = error.code === 'ssh_read_in_progress';
    if (hasSuccessfulPage) {
      $('#output-include-tools').checked = !!view.includeTools;
      $('#output-note').textContent = `${readBusy ? '同一路由已有会话输出读取，请等前一次结束后手动重试。' : `本次会话输出读取失败：${error.message}。`}已保留上次成功读取的正文、分页及工具显示模式；保留的内容不代表本次已刷新。`;
    } else if (readBusy) {
      $('#output-source').textContent = '会话输出读取暂忙';
      $('#output-note').textContent = '同一路由已有会话输出读取，请等前一次结束后手动重试。本次未发起远端查询。';
      $('#output-messages').replaceChildren(); empty($('#output-messages'), '暂未读取到会话记录，请稍后手动点击「刷新最新输出」。');
      $('#output-pagination-note').textContent = '等待前一次读取结束后可手动重试。';
    } else {
      $('#output-note').textContent = `会话输出读取失败：${error.message}。不能把现有终端画面当成完整报告。`;
      if (!append) { $('#output-messages').replaceChildren(); empty($('#output-messages'), '本次未读到实际会话记录，请稍后手动重试。'); }
    }
    throw error;
  } finally {
    view.busy = false;
    if (selectedAgent === key && currentAgent()?.terminal_id === view.terminalId && outputView === view && generation === outputGeneration) { $('#output-refresh').disabled = false; $('#output-more').disabled = false; $('#output-include-tools').disabled = false; }
  }
}
function openBindOutput() {
  const agent = currentAgent(); if (!isTerminal(agent)) return;
  bindOutputTarget = {key: agentKey(agent), host: agent.host, thread_id: agent.thread_id, terminal_id: agent.terminal_id};
  $('#bind-output-form').reset(); $('#bind-output-session').value = agent.output_binding?.session_id || '';
  $('#bind-output-target').textContent = `${agent.alias || agent.title || shortId(agent.thread_id)} · ${agent.host}`;
  $('#bind-output-error').hidden = true; $('#bind-output-confirm').disabled = false;
  $('#bind-output-dialog').showModal();
}
async function bindConversationOutput(target = bindOutputTarget) {
  if (!target || selectedAgent !== target.key || currentAgent()?.terminal_id !== target.terminal_id) throw new Error('所选终端已变化，请重新确认要关联的终端。');
  const sessionId = $('#bind-output-session').value.trim();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sessionId)) throw new Error('请填写 /status 中完整的 Codex 会话 ID（UUID）。');
  const result = await request('/api/agents/bind-output', {host: target.host, thread_id: target.thread_id, session_id: sessionId});
  if (selectedAgent !== target.key || currentAgent()?.terminal_id !== target.terminal_id || bindOutputTarget !== target) { toast('此前终端的会话关联请求已处理；不会读取新选中的终端。'); return; }
  const agent = currentAgent(), binding = result.output_binding || result.agent?.output_binding;
  if (agent && binding) agent.output_binding = binding;
  $('#bind-output-dialog').close(); resetConversationOutput();
  await readConversationOutput();
}
function renderHistory(result) {
  const messages = $('#messages'); messages.replaceChildren(); const data = result.thread || result, history = data.messages || result.messages || [];
  if (!history.length) empty(messages, '这个对话还没有可显示的历史消息。');
  for (const message of history) {
    const article = node('article', null, 'message ' + (message.role === 'user' ? 'user' : 'assistant')), label = node('div', null, 'message-label');
    label.append(node('strong', message.role === 'user' ? '用户' : message.role === 'assistant' ? '工作 agent' : message.role || '记录'), node('span', dateText(message.time || message.timestamp)));
    const body = node('div', null, 'message-body'); formatted(body, messageText(message)); article.append(label, body); messages.append(article);
  }
  const signals = $('#signals'); signals.replaceChildren(); for (const signal of data.recent_signals || result.recent_signals || []) { const box = node('article', null, 'signal'); box.append(node('div', `${signal.type || '输出'} · ${dateText(signal.time || signal.timestamp)}`, 'message-label'), node('pre', signal.text || JSON.stringify(signal, null, 2))); signals.append(box); }
  if (!signals.childElementCount) empty(signals, '暂无记录的工具输出。');
  $('#history-note').textContent = [data.note || result.note, data.truncated ? '只显示最近一部分记录。' : '', '历史记录不代表原终端当前在线。'].filter(Boolean).join(' ');
}
async function readHistory() {
  const agent = currentAgent(); if (!agent || isTerminal(agent)) return; const generation = ++historyGeneration, key = agentKey(agent);
  $('#refresh-history').disabled = true; $('#messages').replaceChildren(node('p', '正在读取所选对话的记录……', 'empty-copy'));
  try { const data = await request('/api/agents/read', {host: agent.host, thread_id: agent.thread_id, limit: 30}); if (selectedAgent === key && generation === historyGeneration) renderHistory(data); }
  catch (error) { if (selectedAgent === key && generation === historyGeneration) $('#messages').replaceChildren(node('p', error.message, 'empty-copy')); throw error; }
  finally { if (generation === historyGeneration) $('#refresh-history').disabled = false; }
}
function renderOutbox() {
  const box = $('#outbox'); box.replaceChildren(); const records = state.outbox.slice().sort((a, b) => (b.time || 0) - (a.time || 0)).slice(0, 8); if (!records.length) empty(box, '暂无 Captain 发送记录。你在终端直接输入时不会生成任务卡。');
  for (const message of records) {
    const card = node('article', null, 'feedback-card'), agent = state.agents.find(item => item.host === message.host && item.thread_id === message.thread_id);
    card.append(node('strong', deliveryLabels[message.state] || message.state || '已记录'), node('div', `${agent?.alias || shortId(message.thread_id)} · ${message.host}`, 'muted'));
    const times = node('div', null, 'delivery-times'), created = timestamp(message.time), submitted = timestamp(message.submitted_at);
    for (const [label, value, help] of [
      ['创建', created, '消息记录的创建时间；不表示已写入终端。'],
      ['确认写入', submitted, '首次得到终端写入确认的时间；不表示模型已接收或任务已完成。']
    ]) {
      if (!value) continue;
      const time = node('time', `${label} ${value.text}`); time.setAttribute('datetime', value.iso); time.title = `${help} 按浏览器本地时间显示；UTC ${value.iso}`; times.append(time);
    }
    if (!created) times.append(node('span', '创建时间未记录'));
    if (!submitted && ['submitted', 'delivered'].includes(message.state)) times.append(node('span', '确认写入时间未记录 · 历史记录'));
    card.append(times, node('p', message.text || ''));
    if (message.error) card.append(node('p', message.error, 'error-text'));
    box.append(card);
  }
}
function renderEvents(events) {
  for (const item of events || []) { const id = item.id ?? `${item.kind}:${JSON.stringify(item.data)}`; activity.set(id, item); if (typeof item.id === 'number') cursor = Math.max(cursor, item.id); }
  while (activity.size > 60) activity.delete(activity.keys().next().value); const list = $('#activity'); list.replaceChildren();
  const kinds = {terminal_created: '打开工作终端', terminal_connected: '终端重新连接', terminal_exited: '显示连接已结束', terminal_closed: '关闭工作终端', terminal_state: '终端状态更新', terminal_output: '终端输出更新', terminal_input_submitted: 'Captain 终端输入送达', message_submitted: 'Captain 消息提交', agent_created: '创建工作终端', agent_attached: '连接工作终端', agent_updated: '工作名称或置顶已更新', agent_reconnected: '已附着原工作终端', agent_activated: '纳入 Captain 管理', agent_deactivated: '移出 Captain 管理', host_connected: '机器连接', host_unavailable: '机器连接不可用', host_configured: '机器配置更新', error: '后台错误'};
  for (const item of Array.from(activity.values()).reverse().slice(0, 12)) { const data = item.data || {}, row = node('article', null, 'event-row'); row.append(node('strong', kinds[item.kind] || item.kind || '后台事件'), node('span', [data.host, shortId(data.thread_id), dateText(item.time || item.created_at || item.timestamp)].filter(Boolean).join(' · '), 'muted')); if (data.text || data.reason) row.append(node('p', String(data.text || data.reason).slice(0, 280))); list.append(row); }
  if (!list.childElementCount) empty(list, '暂无连接事件。');
}
async function refresh(force = false) {
  if (refreshing) { const previous = await refreshTask; if (!force) return previous; }
  refreshing = true;
  refreshTask = (async () => {
  try {
    const next = await request('/api/state?after=' + cursor);
    for (const field of ['hosts', 'agents', 'outbox']) state[field] = Array.isArray(next[field]) ? next[field] : [];
    if (!selectedHost || !state.hosts.some(host => host.id === selectedHost)) selectedHost = state.hosts.find(host => host.id === workbenchSaved.selectedHost)?.id || state.hosts[0]?.id || null;
    if (agentFilters.host && !state.hosts.some(host => host.id === agentFilters.host)) agentFilters.host = '';
    if (restoreAgentKey && !selectedAgent) { const restored = registeredAgent(restoreAgentKey); if (restored) await selectAgent(restored, {restore: true}); }
    if (selectedAgent && !currentAgent()) { selectedAgent = null; historyGeneration++; resetConversationOutput(); disposeTerminal(); }
    // Registry updates never replace the terminal DOM or focus it: typing, selection and dimensions stay intact.
    // Host cards depend on live display counts, not just the saved machine
    // configuration. Keep output sequence/text out of this cache signature.
    const hostDisplays = state.hosts.map(host => [host.id, hostDisplayStatus(host)]);
    const hostLatencies = state.hosts.map(host => [host.id, hostLatencyView(host)]);
    const parts = {hosts: JSON.stringify([state.hosts, selectedHost, hostDisplays, agentFilters.host, hostLatencies]), agents: JSON.stringify([state.agents, selectedAgent, agentFilters]), outbox: JSON.stringify(state.outbox), detail: JSON.stringify(currentAgent() || null)};
    for (const [key, render] of Object.entries({hosts: renderHosts, agents: renderAgents, outbox: renderOutbox, detail: renderDetail})) if (rendered[key] !== parts[key]) { rendered[key] = parts[key]; render(); }
    renderEvents(next.events); $('#status').textContent = `后台服务在线${next.version ? ' · ' + next.version : ''}`; $('#status').classList.add('online');
    // Independent diagnostics must not delay the selected terminal's frame
    // pipeline or a reconnect ACK. This still performs a throttled GET only.
    void checkDisplayServiceStatus();
    return true;
  } catch (error) { $('#status').textContent = error.code === 'ui_session_expired' ? '页面认证已失效 · 请刷新页面' : error.code === 'hub_response_invalid' ? '后台响应异常 · 原工作会话未确认退出' : '后台服务未连接 · 请恢复 AgentCaptain 后台'; $('#status').classList.remove('online'); return false; }
  finally { refreshing = false; renderPendingCreateNotice(); }
  })();
  return await refreshTask;
}
function openMachine(host) { $('#machine-form').reset(); $('#machine-id').value = host?.id || ''; $('#machine-id').readOnly = !!host; $('#machine-label').value = host?.label || ''; $('#machine-transport').value = host?.transport || 'ssh'; $('#machine-target').value = host?.target || host?.targetAlias || ''; updateMachineTransport(); $('#machine-dialog').showModal(); }
function updateMachineTransport() { const local = $('#machine-transport').value === 'local'; $('#machine-target').disabled = local; $('#machine-target').required = !local; }
function terminalCreateStorage() { return window.sessionStorage || window.localStorage; }
function validTerminalCreateBody(body) {
  const boundedText = (value, max, empty = false) => typeof value === 'string' && (empty || !!value) && value.length <= max && !/[\x00-\x1f\x7f-\x9f]/.test(value);
  return !!body && typeof body === 'object' && !Array.isArray(body)
    && Object.keys(body).every(key => ['host', 'cwd', 'title', 'cols', 'rows', 'request_key', 'thread_id'].includes(key))
    && boundedText(body.host, 200) && boundedText(body.cwd, 4096, true) && boundedText(body.title, 200)
    && typeof body.request_key === 'string' && /^[A-Za-z0-9_.:-]{1,200}$/.test(body.request_key)
    && Number.isInteger(body.cols) && body.cols >= 20 && body.cols <= 500 && Number.isInteger(body.rows) && body.rows >= 5 && body.rows <= 200
    && (!Object.hasOwn(body, 'thread_id') || boundedText(body.thread_id, 200));
}
function storePendingTerminalCreate(body) {
  const value = {version: 1, state: 'unknown', body: {...body}}, serialized = JSON.stringify(value);
  try {
    const storage = terminalCreateStorage();
    if (!storage?.setItem || !storage?.getItem) throw new Error('Storage unavailable');
    storage.setItem(pendingCreateStorageKey, serialized);
    if (storage.getItem(pendingCreateStorageKey) !== serialized) throw new Error('Pending request was not retained');
    pendingTerminalCreate = value;
  } catch (_) {
    throw Object.assign(new Error('浏览器无法可靠保存原终端请求，尚未发送创建。请允许本站会话存储后重试；不会在丢失请求键的情况下创建终端。'), {code: 'create_storage_unavailable'});
  }
  renderPendingCreateNotice();
}
function clearPendingTerminalCreate() {
  try {
    const storage = terminalCreateStorage();
    if (storage?.removeItem) storage.removeItem(pendingCreateStorageKey); else storage?.setItem(pendingCreateStorageKey, '');
    if (storage?.getItem(pendingCreateStorageKey)) throw new Error('Pending request still present');
    pendingTerminalCreate = null;
  } catch (_) { /* Retain a safe same-key receipt if completed-request cleanup fails. */ }
  renderPendingCreateNotice();
}
function renderPendingCreateNotice() {
  const pending = pendingTerminalCreate?.body;
  $('#pending-create-notice').hidden = !pending && !pendingCreateStorageError;
  $('#pending-create-open').disabled = !pending;
  $('#pending-create-message').textContent = pendingCreateStorageError
    ? '浏览器中有无法读取的终端打开记录；请先核对已有登记，不能用新请求绕过未确认结果。'
    : pending ? `终端打开请求待核对：${state.hosts.find(host => host.id === pending.host)?.label || pending.host} · ${pending.title}。关闭此对话框或刷新当前页面会保留同一请求；不会自动创建终端。` : '';
}
function renderCreateRequestState(target = createTarget) {
  if (!target || target !== createTarget) return;
  const locked = !!target.fingerprint;
  $('#create-title').disabled = locked; $('#create-cwd').disabled = locked; $('#browse-directory').disabled = locked;
  $('#create-save').disabled = submittingTerminalCreate;
  $('#create-save').textContent = locked ? '核对原打开请求' : '打开终端';
  $('#create-status').hidden = !locked;
  $('#create-status').textContent = locked ? '此请求已提交但结果仍需核对。机器、目录、名称和请求键已固定；点击仅以原请求核对，不自动启动第二个终端。' : '';
}
function openCreate(thread = null, hostId = selectedHost) {
  if (pendingCreateStorageError) { toast('原终端请求记录无法读取，请先核对已有登记；不会生成新请求键绕过未知结果。'); return; }
  const pending = pendingTerminalCreate?.body;
  if (pending) {
    hostId = pending.host; thread = pending.thread_id ? {thread_id: pending.thread_id, title: pending.title, cwd: pending.cwd} : null;
    createTarget = {host: hostId, thread_id: pending.thread_id || null, requestKey: pending.request_key, fingerprint: JSON.stringify(pending), body: {...pending}};
  } else createTarget = {host: hostId, thread_id: thread?.thread_id || thread?.id || null, requestKey: crypto.randomUUID(), fingerprint: null};
  const host = state.hosts.find(item => item.id === hostId);
  $('#create-form').reset(); $('#create-heading').textContent = thread ? '在终端中继续对话' : '打开工作终端'; $('#create-host').textContent = `运行位置：${host?.label || hostId}`;
  $('#create-title').value = pending?.title || thread?.alias || thread?.title || thread?.name || ''; $('#create-cwd').value = pending?.cwd ?? thread?.cwd ?? '';
  $('#create-title').placeholder = `留空使用：${defaultWorkerName(hostId, thread?.cwd)}`;
  $('#create-resume-guide').hidden = !thread; if (thread) $('#create-resume-command').textContent = resumeCommand(createTarget.thread_id);
  renderCreateRequestState();
  $('#create-dialog').showModal();
}
async function submitCreateTerminal() {
  if (!createTarget || submittingTerminalCreate) return;
  const target = createTarget, cwd = $('#create-cwd').value.trim(), body = {host: target.host, cwd, title: $('#create-title').value.trim() || defaultWorkerName(target.host, cwd), cols: 120, rows: 32, request_key: target.requestKey};
  if (target.thread_id) body.thread_id = target.thread_id;
  const fingerprint = JSON.stringify(body);
  if (target.fingerprint && target.fingerprint !== fingerprint || pendingTerminalCreate && JSON.stringify(pendingTerminalCreate.body) !== fingerprint) throw new Error('原打开请求结果尚未核对，不能修改配置或换请求键创建另一个终端。请先核对原请求。');
  if (!validTerminalCreateBody(body)) throw new Error('终端设置无效，请核对机器、名称和目录；本次尚未发送。');
  const host = state.hosts.find(item => item.id === body.host);
  if (cwd && !cwd.startsWith('/') && !(host?.transport === 'local' && /^[A-Za-z]:[\\/]/.test(cwd))) throw new Error('项目目录必须是这台机器上的绝对路径，或留空使用主目录；本次尚未发送。');
  storePendingTerminalCreate(body);
  target.fingerprint = fingerprint; target.body = {...body}; submittingTerminalCreate = true; renderCreateRequestState(target);
  try {
    const result = await request('/api/terminals/create', body), agent = result.agent || result, id = agent.thread_id || result.thread_id;
    if (typeof result.created !== 'boolean') throw new Error('后台尚未明确确认终端打开结果，原请求仍待核对；不会换键重复创建。');
    if (typeof id !== 'string' || !id) throw new Error('后台没有返回终端标识，原打开请求仍待核对；不会换键重复创建。');
    if (agent.host !== body.host || body.thread_id && id !== body.thread_id) throw new Error('后台返回的终端身份不匹配，原请求仍待核对，不会重复创建。');
    if (result.created === false) {
      const fresh = await refresh(true), registered = state.agents.find(item => item.host === body.host && item.thread_id === id);
      if (!fresh || !registered) throw new Error('原终端打开结果尚未确认，登记表也暂不能重新核验。已保留原请求，请恢复后台后继续核对。');
      if (typeof agent.terminal_id !== 'string' || !agent.terminal_id || registered.terminal_id !== agent.terminal_id || registered.connected !== true || !isTerminal(registered) || isClosedAgent(registered) || isClosingAgent(registered)) throw new Error('原终端已登记，但显示连接尚未确认。原请求仍保留；可收起此窗口核对该原终端，不会另行创建或重发输入。');
    }
    clearPendingTerminalCreate();
    if (createTarget !== target) { await refresh(true); toast('此前的终端打开请求已处理，结果保留在对应机器的登记表中。'); return; }
    selectedHost = body.host; selectedAgent = null; $('#create-dialog').close(); await refresh(true);
    const registered = state.agents.find(item => item.host === body.host && item.thread_id === id);
    if (registered) await selectAgent(registered); else toast('终端已登记，等待列表刷新；不会重复创建。');
    if (result.created === false) toast('已重新核验同一登记的原终端显示连接；没有另行创建或重发输入。');
    return result;
  } catch (error) {
    if (createTarget === target) { $('#create-status').hidden = false; $('#create-status').textContent = `${error.message} 原请求和配置已保留；恢复后台后可核对原打开请求。`; }
    throw error;
  } finally { submittingTerminalCreate = false; renderCreateRequestState(); renderPendingCreateNotice(); }
}
function openDirectories() {
  if (!createTarget || createTarget.fingerprint || !$('#create-dialog').open) return;
  directoryTarget = {create: createTarget, host: createTarget.host, view: null};
  const host = state.hosts.find(item => item.id === directoryTarget.host);
  $('#directory-host').textContent = `浏览位置：${host?.label || directoryTarget.host}`;
  $('#directory-dialog').showModal();
  void loadDirectory($('#create-cwd').value.trim());
}
function isCurrentDirectory(target, generation) {
  return directoryTarget === target && createTarget === target.create && directoryGeneration === generation && $('#directory-dialog').open;
}
async function loadDirectory(path = '') {
  const target = directoryTarget; if (!target) return; const generation = ++directoryGeneration;
  target.view = null; $('#directory-select').disabled = true; $('#directory-parent').disabled = true;
  $('#directory-loading').textContent = '读取目录中…'; $('#directory-error').hidden = true;
  $('#directory-path').value = path; $('#directory-list').replaceChildren(); $('#directory-roots').replaceChildren(); $('#directory-note').textContent = '';
  try {
    const view = await request('/api/directories/list', {host: target.host, path});
    if (!isCurrentDirectory(target, generation)) return;
    target.view = view; $('#directory-path').value = view.path; $('#directory-loading').textContent = '';
    $('#directory-parent').disabled = !view.parent; $('#directory-select').disabled = !view.path;
    for (const root of view.roots || []) $('#directory-roots').append(button(root.name || root.path, 'subtle', async () => loadDirectory(root.path)));
    const list = $('#directory-list');
    for (const directory of view.directories || []) list.append(button(directory.name, 'directory-row', async () => loadDirectory(directory.path)));
    if (!list.childElementCount) empty(list, '这个目录没有可浏览的子目录。可以选择当前目录。');
    $('#directory-note').textContent = view.truncated ? '子目录较多，只显示部分结果。也可以直接在上方输入完整路径。' : '点击子目录进入，再选择当前目录。目录浏览不会启动工作 agent。';
  } catch (error) {
    if (!isCurrentDirectory(target, generation)) return;
    $('#directory-loading').textContent = ''; $('#directory-error').hidden = false;
    const hostName = state.hosts.find(item => item.id === target.host)?.label || target.host;
    $('#directory-error').textContent = `${hostName} 目录读取失败：${error.message}`;
    $('#directory-note').textContent = '请先按上方提示检查机器连接、路径或权限，再重新读取目录。创建窗口的设置会保留；不会自动创建终端。';
  }
}
function selectDirectory(path) {
  const target = directoryTarget;
  if (!target || createTarget !== target.create || createTarget.fingerprint || !$('#create-dialog').open) { $('#directory-dialog').close(); return; }
  $('#create-cwd').value = path; $('#create-title').placeholder = `留空使用：${defaultWorkerName(createTarget.host, path)}`; $('#directory-dialog').close(); $('#create-cwd').focus();
}
function validReopenOperation(value) {
  const body = value?.body, text = item => typeof item === 'string' && item.length > 0 && item.length <= 200 && !/[\x00-\x1f\x7f-\x9f]/.test(item);
  return value?.version === 1 && ['unknown', 'blocked', 'done'].includes(value.state) && !!body && typeof body === 'object' && !Array.isArray(body)
    && Object.keys(body).length === 5 && Object.keys(body).every(key => ['host', 'thread_id', 'expected_terminal_id', 'confirmed', 'request_key'].includes(key))
    && text(body.host) && text(body.thread_id) && text(body.expected_terminal_id) && body.confirmed === true && /^[A-Za-z0-9_.:-]{1,200}$/.test(body.request_key || '');
}
function persistReopenOperation(key, operation) {
  const entries = [...reopenOperations].filter(([entryKey]) => entryKey !== key).map(([, entry]) => entry);
  entries.push(operation);
  if (entries.length > 100) throw new Error('保留的重开记录过多，请先核对已有请求；没有发送新的重开操作。');
  const serialized = JSON.stringify(entries);
  try {
    const storage = terminalCreateStorage();
    if (!storage?.setItem || !storage?.getItem) throw new Error('Storage unavailable');
    storage.setItem(reopenStorageKey, serialized);
    if (storage.getItem(reopenStorageKey) !== serialized) throw new Error('Request not retained');
    reopenOperations.set(key, operation);
  } catch (_) { throw new Error('浏览器无法可靠保存原重开请求，尚未发送新的操作。请允许会话存储后重试；不会丢失请求键后重新结束终端。'); }
}
function renderReopenDialog() {
  const target = reopenTarget; if (!target) return;
  const held = target.operation.state === 'unknown', blocked = target.operation.state === 'blocked';
  $('#reopen-name').textContent = target.name; $('#reopen-host').textContent = target.machine;
  $('#reopen-cwd').textContent = target.cwd || '目录未记录'; $('#reopen-terminal-id').textContent = target.operation.body.expected_terminal_id;
  $('#reopen-approval').disabled = target.submitting || held || blocked;
  $('#reopen-confirm').disabled = target.submitting || blocked || !held && !$('#reopen-approval').checked;
  $('#reopen-confirm').textContent = target.submitting ? '正在核对原请求…' : held ? '核对原重开结果' : blocked ? '已阻止 · 关闭后重新确认' : '结束原终端并重开 Shell';
  $('#reopen-error').textContent = target.error || target.operation.note || (held ? '原重开结果尚未确认。只沿用同一个请求键核对，不自动重试、新建或重发输入。' : '');
  $('#reopen-error').hidden = !$('#reopen-error').textContent;
  const draft = terminalSession?.key === target.key ? terminalSession.input || reopenDrafts.get(target.key) || '' : reopenDrafts.get(target.key) || '';
  $('#reopen-draft').value = draft; $('#reopen-draft-panel').hidden = !draft;
  $('#reopen-human-warning').hidden = !target.humanBusy;
  document.querySelectorAll('#reopen-dialog [data-close]').forEach(element => { element.disabled = target.submitting; });
}
function openReopenTerminal(agent = currentAgent()) {
  if (reopenTarget?.submitting || !canReopenAgent(agent) && !canInspectPendingReopen(agent)) return;
  if (reopenStorageError) { toast('原重开记录无法读取，请先核对登记与原请求；不会生成新键绕过未知结果。'); return; }
  const key = agentKey(agent), saved = reopenOperations.get(key), session = terminalSession?.key === key ? terminalSession : null;
  const operation = saved?.state === 'unknown' ? saved : {version: 1, state: 'prepared', body: {
    host: agent.host, thread_id: agent.thread_id, expected_terminal_id: agent.terminal_id, confirmed: true, request_key: crypto.randomUUID()}};
  reopenTarget = {key, operation, name: workerDisplayName(agent), machine: `${workerMachineName(agent)} (${agent.host})`, cwd: agent.cwd,
    humanBusy: !!agent.human_busy || !!session?.input, submitting: false, error: ''};
  $('#reopen-approval').checked = false;
  renderReopenDialog(); $('#reopen-dialog').showModal();
}
function pauseTerminalForReopen(session) {
  if (!session || session.disposed) return;
  session.reopening = true; session.frameGeneration = (session.frameGeneration || 0) + 1;
  clearTimeout(session.inputTimer); session.inputTimer = null; clearTimeout(session.timer); session.timer = null;
  cancelPendingResize(session); session.term.options.disableStdin = true; session.term.blur();
  if (session.input) {
    session.reopenBufferedDraft = session.input;
    reopenDrafts.set(session.key, (session.reopenQueuedDraft || '') + session.input); session.input = '';
  }
}
async function submitReopenTerminal() {
  const target = reopenTarget; if (!target || target.submitting || target.operation.state === 'blocked') return;
  const operation = target.operation, body = operation.body, agent = registeredAgent(target.key);
  if (operation.state === 'unknown' ? !canInspectPendingReopen(agent, body) : !canReopenAgent(agent)) throw new Error('这个工作终端或原重开请求身份不可用，没有发送操作。');
  if (operation.state !== 'unknown' && (!$('#reopen-approval').checked || agent.terminal_id !== body.expected_terminal_id)) throw new Error('请重新核对目标并明确确认；终端身份已变化时不会操作。');
  target.submitting = true; target.error = ''; renderReopenDialog();
  const session = terminalSession?.key === target.key ? terminalSession : null;
  try {
    persistReopenOperation(target.key, {...operation, state: 'unknown'});
    operation.state = 'unknown';
    pauseTerminalForReopen(session); renderDetail(); renderReopenDialog();
    if (session) {
      await session.inputChain;
      const flight = terminalResizeFlights.get(`${terminalApiPath(session, 'resize')}:${JSON.stringify(terminalApiIdentity(session))}`);
      if (flight) await flight;
    }
    const result = await request('/api/terminals/reopen', {...body});
    const validReceipt = result.operation === 'reopen_terminal' && result.operation_id === body.request_key
      && result.previous_terminal_id === body.expected_terminal_id && result.host === body.host && result.thread_id === body.thread_id;
    if (!validReceipt || !['done', 'blocked', 'unknown'].includes(result.operation_state)) throw new Error('重开回执身份未通过核对；保留原请求键，不自动操作。');
    operation.note = result.recovery?.message || '已核对原重开请求。';
    if (result.operation_state === 'done' && result.reopened === true && typeof result.terminal_id === 'string' && result.terminal_id !== body.expected_terminal_id) {
      operation.state = 'done'; persistReopenOperation(target.key, operation);
      const current = registeredAgent(target.key);
      if (current && ![body.expected_terminal_id, result.terminal_id].includes(current.terminal_id)) {
        agentRecoveryNotices.set(target.key, '原重开已完成，但当前登记已进入另一代终端。不会用迟到回执替换新代次，请刷新并核对当前终端。');
        if (reopenTarget === target) $('#reopen-dialog').close();
        renderDetail(); renderAgents(true); return result;
      }
      if (current) Object.assign(current, result);
      reconnectOperations.delete(target.key); persistWorkbench();
      agentRecoveryNotices.set(target.key, '已重开为新的 Shell。没有启动或恢复模型，没有重发原任务；请按需要在终端自行启动。');
      if (selectedAgent === target.key) {
        if (terminalSession?.key === target.key) { pauseTerminalForReopen(terminalSession); disposeTerminal(); }
        resetConversationOutput(); renderDetail();
        if (current) {
          try { await openTerminal(current, {focus: false}); }
          catch (error) { agentRecoveryNotices.set(target.key, `新 Shell 已确认建立，但网页画面尚未打开：${error.message}。请刷新画面；不要再发新的重开请求。`); renderAgentRecoveryNotice(current); }
        }
      }
      renderAgents(true); renderHosts();
      if (reopenTarget === target) $('#reopen-dialog').close();
      toast('已重开 Shell；名称、置顶和目录保留。未启动 Codex，也未重发任务。');
      return result;
    }
    operation.state = result.operation_state === 'blocked' ? 'blocked' : 'unknown';
    if (result.operation_state === 'done') operation.note = '后台回执未证明新终端身份，结果仍待核对。';
    persistReopenOperation(target.key, operation);
    if (operation.state === 'blocked' && session && !session.disposed && registeredAgent(target.key)?.terminal_id === session.terminalId) {
      session.reopening = false; session.term.options.disableStdin = !canWriteTerminal(session); void pollTerminal(session);
    }
    agentRecoveryNotices.set(target.key, operation.note); renderDetail(); return result;
  } catch (error) {
    target.error = `重开结果待核对：${error.message}。不会自动换键、重试或重发输入。`;
    agentRecoveryNotices.set(target.key, target.error);
    if (reopenOperations.has(target.key)) { operation.state = 'unknown'; operation.note = target.error; try { persistReopenOperation(target.key, operation); } catch (_) {} }
    if (selectedAgent === target.key) renderAgentRecoveryNotice(agent);
    throw error;
  } finally { target.submitting = false; if (reopenTarget === target) renderReopenDialog(); renderDetail(); }
}
function openCloseTerminal() {
  const agent = currentAgent(); if (!isTerminal(agent) || isClosedAgent(agent) || isClosingAgent(agent)) return;
  if (pendingReopen(agent)) { openReopenTerminal(agent); return; }
  const host = state.hosts.find(item => item.id === agent.host);
  closeTarget = {host: agent.host, thread_id: agent.thread_id, terminal_id: agent.terminal_id, key: agentKey(agent), alias: agent.alias || agent.title || shortId(agent.thread_id), command: agent.command && agent.command !== 'xterm-256color' ? agent.command : '当前进程名称未确认（可能仍在运行）'};
  $('#close-name').textContent = closeTarget.alias; $('#close-host').textContent = host?.label || agent.host; $('#close-command').textContent = closeTarget.command;
  setCloseControls(false);
  $('#close-dialog').showModal();
}
function setCloseControls(busy) {
  $('#close-confirm').disabled = busy;
  document.querySelectorAll('#close-dialog [data-close]').forEach(element => { element.disabled = busy; });
}
async function closeTerminal(target = closeTarget) {
  if (!target || selectedAgent !== target.key || currentAgent()?.terminal_id !== target.terminal_id) throw new Error('所选终端已变化，请重新确认要关闭的终端。');
  if (target.submitting) return;
  target.submitting = true; setCloseControls(true);
  const session = terminalSession?.key === target.key ? terminalSession : null;
  try {
  if (session) {
    session.closing = true; cancelPendingResize(session); clearTimeout(session.inputTimer); session.inputTimer = null; session.input = '';
    // Finish any already submitted keyboard writes before the close request.
    // No buffered draft or asynchronous clipboard read can reach the closed process.
    await session.inputChain;
  }
    if (selectedAgent !== target.key || (session && terminalSession !== session)) throw new Error('终端已切换，本次关闭请求已取消，请重新确认。');
    const result = await request('/api/terminals/close', {host: target.host, thread_id: target.thread_id, expected_terminal_id: target.terminal_id, confirm: true});
    if (session && !session.disposed && terminalSession === session) { session.closed = !!result.closed; session.term.blur(); }
    await refresh(true);
    if (!result.closed) throw new Error('关闭结果尚未确认，输入与重连已暂停。请核对这个终端的进程状态。' + (result.error ? ' ' + result.error : ''));
    if (closeTarget === target) $('#close-dialog').close();
    toast(`已关闭 ${target.alias}，进程已结束，登记与保留输出仍可查看。`);
    return result;
  } catch (error) {
    // A failed response cannot prove that the remote close did not execute.
    // Keep input paused until the user verifies the same terminal's state.
    if (session && terminalSession === session) { session.term.blur(); renderDetail(); }
    throw error;
  } finally {
    target.submitting = false;
    if (closeTarget === target) setCloseControls(false);
  }
}
$('#machine-settings').onclick = () => openMachine(currentHost()); $('#add-machine').onclick = () => openMachine(); $('#machine-transport').onchange = updateMachineTransport;
document.querySelectorAll('[data-close]').forEach(element => { element.onclick = () => { if (element.dataset.close === 'terminal-service-recovery-dialog' && displayRecoveryTarget?.submitting || element.dataset.close === 'terminal-reconcile-dialog' && reconnectReconcileTarget?.submitting || element.dataset.close === 'reopen-dialog' && reopenTarget?.submitting) return; document.getElementById(element.dataset.close).close(); }; });
document.querySelectorAll('dialog').forEach(dialog => { dialog.addEventListener('click', event => { if (event.target === dialog && !(dialog.id === 'close-dialog' && closeTarget?.submitting) && !(dialog.id === 'terminal-service-recovery-dialog' && displayRecoveryTarget?.submitting) && !(dialog.id === 'terminal-reconcile-dialog' && reconnectReconcileTarget?.submitting) && !(dialog.id === 'reopen-dialog' && reopenTarget?.submitting)) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } }); });
$('#machine-form').onsubmit = event => { event.preventDefault(); action($('#machine-save'), async () => {
  const id = $('#machine-id').value.trim(); await request('/api/hosts/upsert', {id, label: $('#machine-label').value.trim(), transport: $('#machine-transport').value, target: $('#machine-transport').value === 'ssh' ? $('#machine-target').value.trim() : '', live: true}); selectHost(id); $('#machine-dialog').close(); await refresh(true); toast('机器配置已保存，可以打开工作终端。');
}); };
$('#discover-form').onsubmit = event => { event.preventDefault(); action($('#discover-button'), async () => {
  if (!selectedHost) return; const host = selectedHost; $('#candidates-section').hidden = false; $('#candidates').replaceChildren(node('p', '正在读取当前机器的候选记录……', 'empty-copy'));
  const result = await request('/api/agents/discover', {host, query: $('#discovery-query').value.trim(), cwd: $('#discovery-cwd').value.trim(), limit: 20});
  if (selectedHost !== host) return; discoveryHost = host; candidates = result.threads || []; $('#candidate-note').textContent = '这里只查找历史记录；连接会打开 shell，由你选择模型和运行 resume。'; renderCandidates();
}); };
$('#clear-candidates').onclick = () => { $('#candidates-section').hidden = true; candidates = []; };
$('#detail-rename').onclick = () => { const agent = currentAgent(); if (agent) openAgentRename(agent, {dialog: true}); };
$('#rename-name').oninput = () => { if (renameTarget?.mode === 'dialog') renameTarget.value = $('#rename-name').value; };
$('#rename-form').onsubmit = event => { event.preventDefault(); const target = renameTarget, agent = target && registeredAgent(target.key); if (target?.mode === 'dialog' && agent) void action($('#rename-save'), () => saveAgentRename(agent)); };
$('#rename-dialog').addEventListener('close', () => { if (renameTarget?.mode === 'dialog' && !renameTarget.saving) { renameDrafts.delete(renameTarget.key); renameTarget = null; renderAgents(true); } });
$('#agent-search').oninput = () => { agentFilters.query = $('#agent-search').value; persistWorkbench(); renderAgents(true); };
$('#agent-host-filter').onchange = () => { agentFilters.host = $('#agent-host-filter').value; if (agentFilters.host) selectedHost = agentFilters.host; persistWorkbench(); renderHosts(); renderAgents(true); };
$('#agent-status-filter').onchange = () => { agentFilters.status = $('#agent-status-filter').value; persistWorkbench(); renderAgents(true); };
$('#all-machines').onclick = () => { agentFilters.host = ''; persistWorkbench(); renderHosts(); renderAgents(true); };
$('#new-agent').onclick = () => { if (currentHost()) openCreate(null, agentFilters.host || selectedHost); };
$('#pending-create-open').onclick = () => openCreate();
$('#continue-terminal').onclick = () => { const agent = currentAgent(); if (agent) openCreate(agent, agent.host); };
$('#browse-directory').onclick = openDirectories;
$('#create-cwd').oninput = () => { if (createTarget) $('#create-title').placeholder = `留空使用：${defaultWorkerName(createTarget.host, $('#create-cwd').value.trim())}`; };
$('#directory-path-form').onsubmit = event => { event.preventDefault(); void loadDirectory($('#directory-path').value.trim()); };
$('#directory-home').onclick = () => void loadDirectory('');
$('#directory-parent').onclick = () => { if (directoryTarget?.view?.parent) void loadDirectory(directoryTarget.view.parent); };
$('#directory-select').onclick = () => { if (directoryTarget?.view?.path) selectDirectory(directoryTarget.view.path); };
$('#directory-use-home').onclick = () => selectDirectory('');
$('#directory-dialog').addEventListener('close', () => { directoryTarget = null; directoryGeneration++; });
$('#create-dialog').addEventListener('close', () => { createTarget = null; if ($('#directory-dialog').open) $('#directory-dialog').close(); });
$('#create-form').onsubmit = event => { event.preventDefault(); void action($('#create-save'), submitCreateTerminal); };
$('#refresh-history').onclick = () => action($('#refresh-history'), readHistory);
$('#terminal-connect').onclick = () => action($('#terminal-connect'), reconnectTerminal);
$('#terminal-service-recover').onclick = () => action($('#terminal-service-recover'), openDisplayServiceRecovery);
$('#terminal-service-recovery-form').onsubmit = event => { event.preventDefault(); void recoverDisplayService(); };
$('#terminal-service-recovery-dialog').addEventListener('cancel', event => { if (displayRecoveryTarget?.submitting) event.preventDefault(); });
$('#terminal-service-recovery-dialog').addEventListener('close', () => { if (!displayRecoveryTarget?.submitting) displayRecoveryTarget = null; });
$('#terminal-reconcile').onclick = openReconnectReconciliation;
$('#terminal-reconcile-key').onchange = () => { $('#terminal-reconcile-confirm').disabled = !$('#terminal-reconcile-key').value || !!reconnectReconcileTarget?.submitting; };
$('#terminal-reconcile-form').onsubmit = event => { event.preventDefault(); void action($('#terminal-reconcile-confirm'), reconcileSelectedReconnect); };
$('#terminal-reconcile-dialog').addEventListener('cancel', event => { if (reconnectReconcileTarget?.submitting) event.preventDefault(); });
$('#terminal-reconcile-dialog').addEventListener('close', () => { if (!reconnectReconcileTarget?.submitting) reconnectReconcileTarget = null; });
$('#terminal-focus').onclick = () => terminalSession?.term.focus();
$('#terminal-copy').onclick = () => action($('#terminal-copy'), () => copyTerminal(terminalSession));
$('#terminal-paste').onclick = () => action($('#terminal-paste'), () => pasteTerminal(terminalSession));
$('#terminal-page-up').onclick = () => scrollTerminalView(terminalSession, 'page-up');
$('#terminal-page-down').onclick = () => scrollTerminalView(terminalSession, 'page-down');
$('#terminal-latest').onclick = () => scrollTerminalView(terminalSession, 'latest');
$('#terminal-close').onclick = openCloseTerminal;
$('#terminal-reopen').onclick = () => openReopenTerminal();
$('#reopen-approval').onchange = renderReopenDialog;
$('#reopen-form').onsubmit = event => { event.preventDefault(); void action($('#reopen-confirm'), submitReopenTerminal); };
$('#reopen-dialog').addEventListener('cancel', event => { if (reopenTarget?.submitting) event.preventDefault(); });
$('#reopen-dialog').addEventListener('close', () => { if (!reopenTarget?.submitting) reopenTarget = null; });
$('#terminal-output').onclick = () => action($('#terminal-output'), () => readConversationOutput());
$('#terminal-bind-output').onclick = openBindOutput;
$('#output-refresh').onclick = () => action($('#output-refresh'), () => readConversationOutput());
$('#output-more').onclick = () => action($('#output-more'), () => readConversationOutput(true));
$('#output-include-tools').onchange = () => { if (outputView?.open && !$('#conversation-output-panel').hidden) void action($('#output-refresh'), () => readConversationOutput()); };
$('#output-hide').onclick = () => { if (outputView) outputView.open = false; $('#conversation-output-panel').hidden = true; $('#terminal-output').setAttribute('aria-expanded', 'false'); };
$('#bind-output-form').onsubmit = event => { event.preventDefault(); action($('#bind-output-confirm'), async () => {
  try { await bindConversationOutput(bindOutputTarget); } catch (error) { $('#bind-output-error').textContent = error.message; $('#bind-output-error').hidden = false; throw error; }
}); };
$('#bind-output-dialog').addEventListener('close', () => { bindOutputTarget = null; });
$('#close-form').onsubmit = event => { event.preventDefault(); action($('#close-confirm'), () => closeTerminal(closeTarget)); };
$('#close-dialog').addEventListener('close', () => { closeTarget = null; });
$('#close-dialog').addEventListener('cancel', event => { if (closeTarget?.submitting) event.preventDefault(); });
$('#terminal').addEventListener('contextmenu', showTerminalContext);
$('#terminal').addEventListener('wheel', event => handleTerminalWheel(terminalSession, event), {capture: true, passive: false});
$('#context-copy').onclick = () => { const session = contextTerminal; hideTerminalContext(); void copyTerminal(session); };
$('#context-paste').onclick = () => { const session = contextTerminal; hideTerminalContext(); void pasteTerminal(session); };
document.addEventListener('click', event => { if (!$('#terminal-context-menu').contains(event.target)) hideTerminalContext(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTerminalContext(); });
$('#clipboard-form').onsubmit = event => { event.preventDefault(); action($('#clipboard-confirm'), async () => {
  const target = clipboardTarget; if (!target) return;
  if (target.mode === 'copy') { $('#clipboard-dialog').close(); return; }
  if (!canWriteTerminal(target.session)) throw new Error('显示连接已断开，请重新连接后再粘贴。');
  if (!isCurrentTerminal(target.session)) throw new Error('终端已切换或关闭，没有发送这段文本。');
  const text = $('#clipboard-text').value; if (!text) throw new Error('请输入要粘贴的文本。');
  target.session.term.paste(text); $('#clipboard-dialog').close(); target.session.term.focus();
}); };
$('#clipboard-dialog').addEventListener('close', () => { clipboardTarget = null; $('#clipboard-text').value = ''; $('#clipboard-text').readOnly = false; });
$('#copy-resume').onclick = () => action($('#copy-resume'), async () => { await navigator.clipboard.writeText($('#resume-command').textContent); toast('命令已复制。请在终端中自行粘贴并执行。'); });
window.addEventListener('beforeunload', event => {
  if (terminalSession?.input) flushTerminalInput(terminalSession);
  if ([...reopenDrafts.values()].some(Boolean)) { event?.preventDefault?.(); if (event) event.returnValue = ''; }
});
window.addEventListener('hashchange', () => { const key = agentKeyFromHash(), agent = key ? registeredAgent(key) : null; if (agent && agentKey(agent) !== selectedAgent) void selectAgent(agent, {restore: true}).catch(error => toast(error.message)); });
refresh(); setInterval(refresh, 3000);
