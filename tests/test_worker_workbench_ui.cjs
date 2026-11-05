'use strict';
// Inert UI contracts for the shipped workbench: no browser, SSH, PTY or Codex.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function element(tag = 'div') {
  return {tagName: tag, value: '', textContent: '', checked: false, hidden: false, disabled: false, clientWidth: 900, children: [], childElementCount: 0, style: {}, dataset: {}, listeners: {}, attributes: {}, classList: {add() {}, remove() {}, toggle() {}},
    append(...items) { this.children.push(...items); this.childElementCount = this.children.length; }, replaceChildren(...items) { this.children = items; this.childElementCount = items.length; }, setAttribute(name, value) { this.attributes[name] = String(value); }, getAttribute(name) { return this.attributes[name]; }, addEventListener(type, handler) { this.listeners[type] = handler; }, removeEventListener() {}, contains() { return false; }, focus() { this.focusCount = (this.focusCount || 0) + 1; }, select() {}, reset() {}, showModal() { this.open = true; }, close() { this.open = false; }, getBoundingClientRect: () => ({left: 0, top: 0, right: 900, bottom: 600})};
}
const worker = (host, id, alias, extra = {}) => ({host, thread_id: id, terminal_id: `terminal-${id}`, mode: 'terminal', alias, cwd: host === 'local' ? 'C:/work/project' : '/home/example/workspace/project', connected: true, active: true, command: 'codex', state: 'agent', agent_ready: true, management: {status: 'ready', label: 'Codex 输入就绪', primary_action: 'open_terminal', needs_attention: false}, ...extra});
function descendants(root) { return [root, ...(root.children || []).flatMap(descendants)]; }
const cardButton = (card, label) => descendants(card).find(item => item.tagName === 'button' && item.textContent === label);
const cardTitles = get => get('#agents').children.filter(c => /agent-card/.test(c.className || '')).map(c => descendants(c).find(item => item.tagName === 'strong')?.textContent);
async function settle() { for (let i = 0; i < 15; i++) await Promise.resolve(); }
async function harness(options = {}) {
  const elements = new Map(), calls = [], storage = options.storage || new Map(), windowListeners = {}, timers = new Map(), terminals = [], clipboard = []; let uuid = 0, timerId = 0;
  let agents = options.agents || [worker('local', 'one', 'Alpha'), worker('remote', 'two', 'Beta'), worker('remote', 'closed', 'Archived', {closed: true, status: 'closed', state: 'closed', connected: false, active: false})];
  const hosts = [{id: 'local', label: 'My computer', transport: 'local', connected: true}, {id: 'remote', label: 'Remote machine', transport: 'ssh', connected: true}];
  const get = selector => { if (!elements.has(selector)) { const item = element(); item.id = selector.slice(1); elements.set(selector, item); } return elements.get(selector); };
  class FakeTerminal {
    constructor() { this.cols = 100; this.rows = 30; this.options = {}; this.focusCount = 0; this.pastes = []; this.parser = {registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {}}; terminals.push(this); }
    open() {} loadAddon() {} focus() { this.focusCount++; } blur() {} reset() {} dispose() { this.disposed = true; } attachCustomKeyEventHandler(handler) { this.keyHandler = handler; } attachCustomWheelEventHandler() {} onData(handler) { this.dataHandler = handler; } onResize(handler) { this.resizeHandler = handler; } resize(cols, rows) { this.cols = cols; this.rows = rows; this.resizeHandler?.({cols, rows}); } write(data, callback) { callback?.(); } hasSelection() { return true; } getSelection() { return 'worker output'; } paste(text) { this.pastes.push(text); }
  }
  const location = {origin: 'http://fixture', pathname: '/', search: '', hash: options.hash || ''};
  const context = vm.createContext({console, URLSearchParams, crypto: {randomUUID: () => `workbench-operation-${++uuid}`}, navigator: {clipboard: {writeText: async text => clipboard.push(text), readText: async () => 'clipboard text'}},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element, createTextNode: text => ({textContent: text}), addEventListener() {}},
    window: {Terminal: FakeTerminal, FitAddon: {FitAddon: class {fit() {}}}, addEventListener(type, handler) { windowListeners[type] = handler; }, location, history: {replaceState(_state, _title, url) { location.hash = url.includes('#') ? '#' + url.split('#')[1] : ''; }}, localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}, PronController: options.controller},
    ResizeObserver: class {observe() {} disconnect() {}}, requestAnimationFrame: callback => callback(), setInterval() {}, setTimeout: callback => { timers.set(++timerId, callback); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, opts = {}) => {
      const body = opts.body ? JSON.parse(opts.body) : undefined; const call = {url: String(url), options: opts, body}; calls.push(call);
      const reply = value => ({ok: true, status: 200, json: async () => value});
      if (options.handle) { const result = await options.handle(call, {agents, hosts, reply}); if (result) return result; }
      if (url.startsWith('/api/state')) return reply({hosts, agents, outbox: options.outbox || [], events: []});
      if (url.startsWith('/api/terminals/output')) { const query = new URLSearchParams(url.split('?')[1]); const agent = agents.find(a => a.host === query.get('host') && a.thread_id === query.get('thread_id')); return reply({data: '', seq: 1, reset: true, cols: 100, rows: 30, connected: agent?.connected !== false}); }
      if (url === '/api/agents/update') { const agent = agents.find(a => a.host === body.host && a.thread_id === body.thread_id); Object.assign(agent, body); return reply({...agent}); }
      if (url === '/api/agents/auto-reconnect') { const agent = agents.find(a => a.host === body.host && a.thread_id === body.thread_id); agent.auto_reconnect = {enabled: body.enabled, interval_seconds: 600, status: body.enabled ? agent.connected ? 'connected' : 'waiting' : 'off', next_attempt_at: body.enabled && !agent.connected ? 1792304700 : null}; return reply({...agent}); }
      if (url === '/api/agents/reconnect') { const agent = agents.find(a => a.host === body.host && a.thread_id === body.thread_id); agent.connected = true; return reply({...agent, recovery: {code: 'connected', message: '已重新连接原 tmux 显示，未运行 resume 或任务。'}}); }
      if (url === '/api/agents/read') return reply({messages: [], signals: [], content_complete: true, has_more: false});
      return reply({accepted: true});
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context); await context.refresh(); await settle();
  return {context, get, calls, agents, hosts, storage, terminals, windowListeners, location, clipboard};
}
function setFilter(h, field, value) { h.get('#agent-' + (field === 'query' ? 'search' : field + '-filter')).value = value; h.get('#agent-' + (field === 'query' ? 'search' : field + '-filter'))[field === 'query' ? 'oninput' : 'onchange'](); }

test('default workbench is cross-machine and closed records are hidden with honest counts', async () => {
  const h = await harness(); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']); assert.equal(h.get('#registry-count').textContent, '2');
  assert.match(h.get('#agent-count-caption').textContent, /2 \/ 3.*1.*已关闭.*不是任务完成/); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

test('delivery cards distinguish creation from confirmed terminal write with exact datetime values', async () => {
  const created = 1792304100.125, submitted = created + 62.5;
  const h = await harness({outbox: [{id: 'delivery-fixture', host: 'remote', thread_id: 'two', text: 'Message fixture', state: 'submitted', time: created, submitted_at: submitted}]});
  const card = h.get('#outbox').children[0], times = descendants(card).filter(item => item.tagName === 'time');
  assert.equal(times.length, 2);
  assert.match(times[0].textContent, /^创建 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  assert.match(times[1].textContent, /^确认写入 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  assert.equal(times[0].getAttribute('datetime'), new Date(created * 1000).toISOString());
  assert.equal(times[1].getAttribute('datetime'), new Date(submitted * 1000).toISOString());
  assert.notEqual(times[0].getAttribute('datetime'), times[1].getAttribute('datetime'));
  assert.match(times[0].title, /创建时间.*不表示已写入终端/);
  assert.match(times[1].title, /首次得到终端写入确认.*不表示模型已接收或任务已完成/);
  assert.match(times[1].title, /本地时间.*UTC/);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
});

test('legacy acknowledged deliveries show unknown confirmation time instead of using creation or current time', async () => {
  const h = await harness({outbox: [
    {id: 'legacy-null', host: 'remote', thread_id: 'two', text: 'Null historical timestamp', state: 'submitted', time: 1792304100, submitted_at: null},
    {id: 'legacy-missing', host: 'remote', thread_id: 'two', text: 'Missing historical timestamps', state: 'delivered'}
  ]});
  const cards = h.get('#outbox').children;
  const knownCreation = cards.find(card => descendants(card).some(item => item.textContent === 'Null historical timestamp'));
  const noTimes = cards.find(card => descendants(card).some(item => item.textContent === 'Missing historical timestamps'));
  assert.equal(descendants(knownCreation).filter(item => item.tagName === 'time').length, 1);
  assert.equal(descendants(noTimes).filter(item => item.tagName === 'time').length, 0);
  for (const card of cards) assert.ok(descendants(card).some(item => item.textContent === '确认写入时间未记录 · 历史记录'));
  assert.ok(descendants(noTimes).some(item => item.textContent === '创建时间未记录'));
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
});

test('queued submitting and uncertain records never acquire an acknowledgement time from their creation date', async () => {
  const h = await harness({outbox: ['queued', 'submitting', 'unknown'].map((state, index) => ({id: 'pending-' + state, host: 'remote', thread_id: 'two', text: state, state, time: 1792304100 + index, submitted_at: null}))});
  for (const card of h.get('#outbox').children) {
    const times = descendants(card).filter(item => item.tagName === 'time');
    assert.equal(times.length, 1); assert.match(times[0].textContent, /^创建 /);
    assert.equal(descendants(card).some(item => /^确认写入/.test(item.textContent)), false);
  }
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
});

test('invalid delivery timestamp values render unknown without fabricated datetime attributes', async () => {
  for (const value of [null, undefined, 0, -1, NaN, Infinity, '1792304100', 1e20]) {
    const h = await harness({outbox: [{id: 'invalid-time', host: 'remote', thread_id: 'two', state: 'submitted', time: value, submitted_at: value}]});
    const nodes = descendants(h.get('#outbox').children[0]);
    assert.equal(nodes.filter(item => item.tagName === 'time').length, 0, `invalid timestamp ${String(value)}`);
    assert.ok(nodes.some(item => item.textContent === '创建时间未记录'));
    assert.ok(nodes.some(item => item.textContent === '确认写入时间未记录 · 历史记录'));
  }
});

test('default working includes every real open terminal even if old activation is false or display disconnected', async () => {
  const h = await harness({agents: [worker('local', 'one', 'Alpha', {active: false}), worker('remote', 'two', 'Beta'), worker('remote', 'old', 'Old offline', {active: false, connected: false}), {host: 'local', thread_id: 'history', alias: 'Old history', mode: 'observe', active: false}]});
  assert.equal(h.get('#agent-status-filter').value, 'working'); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta', 'Old offline']);
  setFilter(h, 'status', 'all'); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta', 'Old history', 'Old offline']);
  h.agents[2].pinned = true; h.context.clearAgentFilters(); assert.deepEqual(cardTitles(h.get), ['Old offline', 'Alpha', 'Beta']);
  h.agents[1].connected = false; await h.context.refresh(true); assert.ok(cardTitles(h.get).includes('Beta'), 'Every registered disconnected worker remains visible');
});

test('previous explicit all preference stays all, rather than silently changing existing user choice', async () => {
  const storage = new Map([['pron-hub.worker-workbench.v1', JSON.stringify({status: 'all'})]]);
  const h = await harness({storage, agents: [worker('remote', 'old', 'Old offline', {active: false, connected: false})]});
  assert.equal(h.get('#agent-status-filter').value, 'all'); assert.deepEqual(cardTitles(h.get), ['Old offline']);
});

test('machine, name/directory, connection and attention filters compose; closed requires explicit filter', async () => {
  const h = await harness({agents: [worker('local', 'one', 'Alpha'), worker('remote', 'two', 'Beta', {connected: false, management: {needs_attention: true, label: '断开', primary_action: 'reconnect'}}), worker('remote', 'closed', 'Archived', {closed: true, connected: false})]});
  setFilter(h, 'host', 'remote'); assert.deepEqual(cardTitles(h.get), ['Beta']); setFilter(h, 'query', 'workspace'); assert.deepEqual(cardTitles(h.get), ['Beta']);
  setFilter(h, 'status', 'connected'); assert.deepEqual(cardTitles(h.get), []); setFilter(h, 'status', 'attention'); assert.deepEqual(cardTitles(h.get), ['Beta']);
  setFilter(h, 'query', ''); setFilter(h, 'status', 'closed'); assert.deepEqual(cardTitles(h.get), ['Archived']);
  h.context.clearAgentFilters(); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']);
});

test('pinned ordering is stable across registry order changes; pin mutation updates metadata only', async () => {
  const h = await harness({agents: [worker('remote', 'b', 'Beta'), worker('local', 'a', 'Alpha', {pinned: true})]});
  assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']); h.agents.reverse(); await h.context.refresh(true); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']);
  const beta = h.get('#agents').children[1]; await cardButton(beta, '☆').onclick();
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/update').body, {host: 'remote', thread_id: 'b', pinned: true});
  assert.equal(h.calls.some(c => /activate|input|\/create|\/connect/.test(c.url)), false);
});

test('inline rename keeps draft during polling and preserves opened worker terminal', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); const session = vm.runInContext('terminalSession', h.context); session.input = 'worker draft';
  h.context.openAgentRename(h.agents[0]); const target = vm.runInContext('renameTarget', h.context); target.value = 'New display name'; const before = h.get('#agents').children[0];
  await h.context.refresh(true); assert.equal(h.get('#agents').children[0], before); await h.context.saveAgentRename(h.agents[0]);
  assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.input, 'worker draft'); assert.equal(session.term.focusCount, 0);
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/update').body, {host: 'local', thread_id: 'one', alias: 'New display name'});
});

test('filtering and machine navigation preserve selection, terminal DOM and human draft without focus', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); const session = vm.runInContext('terminalSession', h.context); session.input = 'human draft';
  h.context.selectHost('remote'); setFilter(h, 'query', 'Beta'); await h.context.refresh(true);
  assert.equal(vm.runInContext('selectedAgent', h.context), 'local:one'); assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.input, 'human draft'); assert.equal(session.term.focusCount, 0);
});

test('browser preferences and encoded hash restore exact registered view with GET only, never attach/start/focus', async () => {
  const storage = new Map([['pron-hub.worker-workbench.v1', JSON.stringify({host: 'remote', query: 'Beta', status: 'attention', agent: 'local:one', selectedHost: 'remote'})]]);
  const h = await harness({storage, hash: '#agent=' + encodeURIComponent('remote::two')});
  assert.equal(vm.runInContext('selectedAgent', h.context), 'remote:two'); assert.equal(h.get('#agent-search').value, 'Beta'); assert.equal(h.get('#agent-status-filter').value, 'attention');
  assert.equal(h.terminals[0].focusCount, 0); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  assert.equal(h.calls.some(c => /resume|discover|\/create|\/reconnect|\/connect/.test(c.url)), false);
});

test('unknown deep link never discovers history or guesses another session', async () => {
  const h = await harness({hash: '#agent=' + encodeURIComponent('remote:missing')}); assert.equal(vm.runInContext('selectedAgent', h.context), null);
  assert.equal(h.calls.some(c => /history|discover|output|\/create|\/connect/.test(c.url)), false);
});

test('quick output opens exact existing record without sending input or reconnecting disconnected agent', async () => {
  const h = await harness({agents: [worker('remote', 'offline', 'Disconnected', {connected: false})]});
  await cardButton(h.get('#agents').children[0], '会话输出').onclick();
  const read = h.calls.find(c => c.url === '/api/agents/read'); assert.equal(read.body.host, 'remote'); assert.equal(read.body.thread_id, 'offline');
  assert.equal(h.calls.some(c => /\/input|\/create|\/reconnect|\/connect/.test(c.url)), false); assert.equal(h.terminals[0].focusCount, 0);
});

test('quick close selects and captures intended worker then waits for original explicit confirm dialog', async () => {
  const h = await harness(); await cardButton(h.get('#agents').children[1], '关闭终端…').onclick();
  assert.equal(h.get('#close-dialog').open, true); assert.equal(vm.runInContext('closeTarget.host', h.context), 'remote'); assert.equal(vm.runInContext('closeTarget.thread_id', h.context), 'two');
  assert.equal(h.calls.some(c => c.url.endsWith('/close')), false);
});

test('reconnect is shown only for disconnected open terminals and calls only safe exact-agent recovery', async () => {
  const h = await harness({agents: [worker('local', 'one', 'Online'), worker('remote', 'two', 'Offline', {connected: false})]});
  const offline = h.get('#agents').children.find(card => cardButton(card, '重连原终端'));
  assert.ok(offline); assert.equal(h.get('#agents').children.filter(card => cardButton(card, '重连原终端')).length, 1);
  await cardButton(offline, '重连原终端').onclick(); const reconnect = h.calls.find(c => c.url === '/api/agents/reconnect');
  assert.equal(reconnect.body.host, 'remote'); assert.equal(reconnect.body.thread_id, 'two'); assert.ok(reconnect.body.request_key);
  assert.equal(h.calls.some(c => /\/create|\/connect|\/input|resume/.test(c.url)), false); assert.match(h.get('#agent-recovery-notice').textContent, /未运行 resume/);
});

test('uncertain reconnect retains same request key, never silently retries or creates a terminal', async () => {
  let lost = true;
  const h = await harness({agents: [worker('remote', 'two', 'Offline', {connected: false})], handle: async call => { if (call.url === '/api/agents/reconnect' && lost) { lost = false; throw new Error('lost connection'); } }});
  await h.context.selectAgent(h.agents[0]); await assert.rejects(h.context.reconnectTerminal(), error => error.code === 'hub_unreachable');
  const first = h.calls.find(c => c.url === '/api/agents/reconnect').body.request_key; await h.context.refresh(true); assert.equal(h.calls.filter(c => c.url === '/api/agents/reconnect').length, 1);
  await h.context.reconnectTerminal(); assert.equal(h.calls.filter(c => c.url === '/api/agents/reconnect')[1].body.request_key, first);
  assert.equal(h.calls.some(c => /\/create|\/connect|\/input|resume/.test(c.url)), false);
});

test('known blocked reconnect keeps useful classification but never creates shell or resumes stopped Codex', async () => {
  const h = await harness({agents: [worker('remote', 'two', 'Offline', {connected: false})], handle: async (call, backend) => call.url === '/api/agents/reconnect' ? backend.reply({...backend.agents[0], recovery: {code: 'tmux_missing', message: '原持久终端不存在，请由用户选择新建终端。', next_action: 'create_new_terminal'}}) : null});
  await h.context.selectAgent(h.agents[0]); await h.context.reconnectTerminal(); assert.match(h.get('#agent-recovery-notice').textContent, /原持久终端不存在/);
  assert.equal(h.calls.some(c => /\/create|\/connect|\/input|resume/.test(c.url)), false); assert.equal(vm.runInContext('terminalSession.displayConnected', h.context), false);
});

test('registered workers expose naming directly with no activation or per-worker management grants', async () => {
  let opened = 0;
  const h = await harness({controller: {model: {controller: {scope: ['local::one']}}, openSettings(scopeOnly) { assert.equal(scopeOnly, true); opened++; }}});
  const cards = h.get('#agents').children;
  assert.ok(cards.every(card => cardButton(card, '重命名')));
  assert.equal(cards.some(card => descendants(card).some(item => /当前主控|管理范围|未加入管理名单/.test(item.textContent))), false);
  assert.ok(cards.flatMap(descendants).filter(item => item.type === 'checkbox').every(item => item.className === 'auto-reconnect-checkbox'), 'Only the optional SSH reconnect preference may use a checkbox, never a management grant');
  assert.equal(opened, 0); assert.equal(h.calls.some(c => /activate|scope/.test(c.url)), false);
});

test('auto reconnect defaults off and appears only for open SSH workers', async () => {
  const h = await harness({agents: [worker('local', 'local', 'Local'), worker('remote', 'open', 'Remote'),
    worker('remote', 'closed', 'Closed', {closed: true, mode: 'closed', state: 'closed'}),
    worker('remote', 'closing', 'Closing', {mode: 'close_unknown', state: 'close_unknown'}),
    worker('remote', 'manager', 'Manager', {kind: 'controller'})]});
  const checks = h.get('#agents').children.flatMap(descendants).filter(item => item.type === 'checkbox');
  assert.equal(checks.length, 1); assert.equal(checks[0].checked, false); assert.equal(checks[0].disabled, false);
  await h.context.selectAgent(h.agents[0]); assert.equal(h.get('#agent-auto-reconnect').hidden, true);
  await h.context.selectAgent(h.agents[1]); assert.equal(h.get('#agent-auto-reconnect').hidden, false);
  assert.equal(h.get('#agent-auto-reconnect-enabled').checked, false);
  assert.equal(h.calls.some(call => call.options.method === 'POST'), false, 'Showing the setting never enables or reconnects anything');
});

test('card opt-in persists the frozen original terminal preference without opening or reconnecting it', async () => {
  const h = await harness({agents: [worker('remote', 'one', 'Remote', {connected: false})]});
  const input = descendants(h.get('#agents').children[0]).find(item => item.type === 'checkbox');
  input.checked = true; await input.onchange();
  const writes = h.calls.filter(call => call.options.method === 'POST');
  assert.equal(writes.length, 1); assert.equal(writes[0].url, '/api/agents/auto-reconnect');
  assert.deepEqual(writes[0].body, {host: 'remote', thread_id: 'one', enabled: true,
    expected_terminal_id: h.agents[0].terminal_id, request_key: 'workbench-operation-1'});
  assert.equal(h.agents[0].auto_reconnect.enabled, true);
  assert.ok(descendants(h.get('#agents').children[0]).some(item => /下次尝试.*本地时间/.test(item.textContent)));
  assert.equal(vm.runInContext('selectedAgent', h.context), null); assert.equal(h.terminals.length, 0);
});

test('detail opt-out updates the same preference and preserves the worker terminal and human draft', async () => {
  const h = await harness({agents: [worker('remote', 'one', 'Remote', {auto_reconnect: {enabled: true, interval_seconds: 600, status: 'connected'}})]});
  await h.context.selectAgent(h.agents[0]);
  const session = vm.runInContext('terminalSession', h.context); session.input = 'human draft';
  const input = h.get('#agent-auto-reconnect-enabled'); assert.equal(input.checked, true);
  input.checked = false; await input.onchange();
  assert.equal(h.agents[0].auto_reconnect.enabled, false); assert.equal(h.get('#agent-auto-reconnect-enabled').checked, false);
  assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.input, 'human draft'); assert.equal(session.term.focusCount, 0);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  assert.ok(h.calls.every(call => !/\/input|\/prompt|\/reconnect$|\/create|\/reopen|\/connect$/.test(call.url)));
});

test('failed auto reconnect save restores the authoritative value without retrying or touching a manual recovery key', async () => {
  const h = await harness({agents: [worker('remote', 'one', 'Remote')], handle: async call => {
    if (call.url === '/api/agents/auto-reconnect') return {ok: false, status: 409, json: async () => ({error: '终端已切换，请刷新'})};
  }});
  await h.context.selectAgent(h.agents[0]);
  vm.runInContext("reconnectOperations.set('remote:one',{requestKey:'manual-key',acknowledged:false})", h.context);
  const input = h.get('#agent-auto-reconnect-enabled'); input.checked = true; await input.onchange();
  assert.equal(h.get('#agent-auto-reconnect-enabled').checked, false); assert.equal(h.get('#agent-auto-reconnect-enabled').disabled, false);
  assert.match(h.get('#toast').textContent, /终端已切换/);
  await h.context.refresh(true); await h.context.refresh(true);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  assert.equal(vm.runInContext("reconnectOperations.get('remote:one').requestKey", h.context), 'manual-key');
});

test('in-flight preference save disables both controls and ignores a stale terminal-generation response', async () => {
  let release;
  const h = await harness({agents: [worker('remote', 'one', 'Remote')], handle: async (call, backend) => {
    if (call.url !== '/api/agents/auto-reconnect') return null;
    return await new Promise(resolve => { release = () => resolve(backend.reply({...backend.agents[0], terminal_id: call.body.expected_terminal_id,
      auto_reconnect: {enabled: true, interval_seconds: 600, status: 'waiting'}})); });
  }});
  await h.context.selectAgent(h.agents[0]);
  const input = h.get('#agent-auto-reconnect-enabled'); input.checked = true;
  const saving = input.onchange(); await settle();
  assert.equal(h.get('#agent-auto-reconnect-enabled').disabled, true);
  assert.equal(descendants(h.get('#agents').children[0]).find(item => item.type === 'checkbox').disabled, true);
  h.agents[0].terminal_id = 'ceohub-new-generation'; await h.context.refresh(true);
  release(); await saving;
  assert.equal(h.get('#agent-auto-reconnect-enabled').checked, false);
  assert.match(h.get('#toast').textContent, /保存期间已切换/);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
});

test('an old checkbox cannot enable automatic reconnect for a replacement terminal', async () => {
  const h = await harness({agents: [worker('remote', 'one', 'Remote')]});
  const input = descendants(h.get('#agents').children[0]).find(item => item.type === 'checkbox');
  h.agents[0].terminal_id = 'ceohub-replacement'; input.checked = true; await input.onchange();
  assert.equal(h.calls.some(call => call.options.method === 'POST'), false);
  assert.match(h.get('#toast').textContent, /终端已切换或关闭/);
});

test('a lost preference response rolls back the checkbox and polling never resubmits it', async () => {
  const h = await harness({agents: [worker('remote', 'one', 'Remote')], handle: async call => {
    if (call.url === '/api/agents/auto-reconnect') throw new Error('connection lost');
  }});
  const input = descendants(h.get('#agents').children[0]).find(item => item.type === 'checkbox');
  input.checked = true; await input.onchange();
  assert.equal(descendants(h.get('#agents').children[0]).find(item => item.type === 'checkbox').checked, false);
  assert.match(h.get('#toast').textContent, /无法连接本机/);
  await h.context.refresh(true); await h.context.refresh(true);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
});

test('late preference confirmation stays on the original worker after selection changes', async () => {
  let release;
  const h = await harness({agents: [worker('remote', 'one', 'Alpha'), worker('remote', 'two', 'Beta')], handle: async (call, backend) => {
    if (call.url !== '/api/agents/auto-reconnect') return null;
    return await new Promise(resolve => { release = () => {
      backend.agents[0].auto_reconnect = {enabled: true, interval_seconds: 600, status: 'connected'};
      resolve(backend.reply({...backend.agents[0]}));
    }; });
  }});
  const input = descendants(h.get('#agents').children[0]).find(item => item.type === 'checkbox');
  input.checked = true; const saving = input.onchange(); await settle();
  await h.context.selectAgent(h.agents[1]);
  const session = vm.runInContext('terminalSession', h.context); session.input = 'Beta draft';
  release(); await saving;
  assert.equal(h.agents[0].auto_reconnect.enabled, true);
  assert.equal(h.get('#agent-auto-reconnect-enabled').checked, false);
  assert.equal(vm.runInContext('selectedAgent', h.context), 'remote:two');
  assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.input, 'Beta draft');
  assert.equal(session.term.focusCount, 0); assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
});

test('backend automatic reconnect progress is rendered by polling without retrying a manual uncertain action or stealing focus', async () => {
  const h = await harness({agents: [worker('remote', 'one', 'Remote', {connected: false,
    auto_reconnect: {enabled: true, interval_seconds: 600, status: 'waiting', next_attempt_at: 1792304700}})]});
  await h.context.selectAgent(h.agents[0]); const session = vm.runInContext('terminalSession', h.context); session.input = 'keep draft';
  vm.runInContext("reconnectOperations.set('remote:one',{requestKey:'manual-unknown',acknowledged:false})", h.context);
  h.agents[0].auto_reconnect.status = 'reconnecting'; await h.context.refresh(true);
  assert.match(h.get('#agent-auto-reconnect-status').textContent, /后台正在重连原终端/);
  h.agents[0].auto_reconnect.status = 'needs_attention'; h.agents[0].auto_reconnect.last_result = {code: 'original_session_missing', message: '原终端不存在'};
  await h.context.refresh(true); assert.match(h.get('#agent-auto-reconnect-status').textContent, /原终端不存在/);
  assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.input, 'keep draft'); assert.equal(session.term.focusCount, 0);
  assert.equal(vm.runInContext("reconnectOperations.get('remote:one').requestKey", h.context), 'manual-unknown');
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
});

test('auto reconnect time comes only from a valid backend schedule and text is rendered safely', async () => {
  const h = await harness();
  for (const value of [null, undefined, NaN, Infinity, -1, 0, '1792304700']) {
    const view = h.context.autoReconnectView({auto_reconnect: {enabled: true, status: 'waiting', next_attempt_at: value}});
    assert.match(view.text, /断线后每 10 分钟尝试/); assert.doesNotMatch(view.text, /下次尝试/);
  }
  const text = '<img src=x onerror=alert(1)>';
  h.agents[1].auto_reconnect = {enabled: true, status: 'needs_attention', last_result: {message: text}};
  await h.context.selectAgent(h.agents[1]);
  assert.ok(h.get('#agent-auto-reconnect-status').textContent.includes(text));
  assert.equal(h.get('#agent-auto-reconnect-status').children.length, 0);
});

test('old active-only preference is normalized away without hiding false-active registered workers', async () => {
  const storage = new Map([['pron-hub.worker-workbench.v1', JSON.stringify({active: true, status: 'working'})]]);
  const h = await harness({storage, agents: [worker('remote', 'one', 'Old inactive', {active: false, connected: false})]});
  assert.deepEqual(cardTitles(h.get), ['Old inactive']); assert.equal(vm.runInContext('agentFilters.active', h.context), false);
  assert.equal(JSON.parse(storage.get('pron-hub.worker-workbench.v1')).active, false);
});

test('unnamed duplicate projects retain visible machine directory and stable ID context', async () => {
  const h = await harness({agents: [worker('remote', 'one', '', {title: ''}), worker('local', 'two', '', {title: ''})]});
  assert.deepEqual(cardTitles(h.get), ['My computer · project', 'Remote machine · project']);
  for (const card of h.get('#agents').children) {
    assert.ok(cardButton(card, '命名'));
    assert.ok(descendants(card).some(item => /agent-directory/.test(item.className || '') && item.textContent));
    assert.ok(descendants(card).some(item => /agent-id/.test(item.className || '') && item.textContent));
  }
});

test('detail rename edits the exact disconnected worker and naming drafts survive target switches', async () => {
  const h = await harness({agents: [worker('local', 'one', 'Alpha'), worker('remote', 'two', 'Beta', {connected: false})]});
  await h.context.selectAgent(h.agents[1]); h.get('#detail-rename').onclick();
  assert.equal(vm.runInContext('renameTarget.key', h.context), 'remote:two');
  vm.runInContext('renameTarget.value = "Beta draft"', h.context);
  await h.context.selectAgent(h.agents[0]); h.context.openAgentRename(h.agents[0]);
  vm.runInContext('renameTarget.value = "Alpha draft"', h.context); h.context.openAgentRename(h.agents[1]);
  assert.equal(vm.runInContext('renameTarget.value', h.context), 'Beta draft');
  const current = vm.runInContext('renameTarget', h.context); await h.context.refresh(true); assert.equal(vm.runInContext('renameTarget', h.context), current);
  await h.context.saveAgentRename(h.agents[1]);
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/update').body, {host: 'remote', thread_id: 'two', alias: 'Beta draft'});
  assert.equal(h.calls.some(c => /\/input|\/create|\/reconnect|\/connect|activate/.test(c.url)), false);
});

test('detail naming stays visible for a worker filtered out of cards and submits captured target only', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); h.context.selectHost('remote'); h.get('#detail-rename').onclick();
  assert.equal(h.get('#rename-dialog').open, true); assert.equal(h.get('#rename-name').value, 'Alpha');
  h.get('#rename-name').value = 'Selected but filtered'; h.get('#rename-name').oninput();
  await h.context.selectAgent(h.agents[1]); await h.context.refresh(true);
  assert.equal(h.get('#rename-name').value, 'Selected but filtered'); assert.equal(vm.runInContext('renameTarget.key', h.context), 'local:one');
  h.get('#rename-form').onsubmit({preventDefault() {}}); await settle();
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/update').body, {host: 'local', thread_id: 'one', alias: 'Selected but filtered'});
  assert.equal(h.agents[1].alias, 'Beta'); assert.equal(h.calls.some(c => /\/input|\/create|\/connect|activate/.test(c.url)), false);
});

test('manual naming cancel discards only that rename draft and leaves terminal input untouched', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); const session = vm.runInContext('terminalSession', h.context); session.input = 'Terminal human draft';
  h.get('#detail-rename').onclick(); h.get('#rename-name').value = 'Cancelled draft'; h.get('#rename-name').oninput();
  h.get('#rename-dialog').listeners.close(); h.get('#detail-rename').onclick();
  assert.equal(h.get('#rename-name').value, 'Alpha'); assert.equal(session.input, 'Terminal human draft');
  assert.equal(h.calls.some(c => /\/input|\/update|\/create|\/connect/.test(c.url)), false);
});

test('manager records never become worker cards even when an old registry contains their terminal ID', async () => {
  const h = await harness({agents: [worker('local', 'one', 'Alpha'), worker('local', 'manager', 'Manager', {mode: 'controller', kind: 'controller'})]});
  setFilter(h, 'status', 'all'); assert.deepEqual(cardTitles(h.get), ['Alpha']);
});

test('create name is optional with machine and selected project default, not an activation control', async () => {
  const h = await harness({handle: async (call, backend) => call.url === '/api/terminals/create' ? backend.reply({created: true, host: 'remote', thread_id: 'created'}) : null});
  h.context.openCreate(null, 'remote'); assert.equal(h.get('#create-title').value, ''); h.get('#create-cwd').value = '/workspace/sample';
  h.get('#create-form').onsubmit({preventDefault() {}}); await settle();
  const body = h.calls.find(c => c.url === '/api/terminals/create').body;
  assert.equal(body.title, 'Remote machine · sample'); assert.equal(Object.hasOwn(body, 'active'), false);
});

test('shipped markup has naming in detail and no separate worker activation or grant UI', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /id="detail-rename"/); assert.match(html, /id="rename-dialog"/); assert.match(html, /id="create-title" maxlength="200"/);
  assert.doesNotMatch(html, /id="(?:active-only|create-active|controller-workers|controller-legacy-warning)"/);
  assert.doesNotMatch(html, /当前主控(?:未|已)授权|只看允许管理|加入 (?:CEO|Captain) 的激活范围/);
});

test('AgentCaptain branding explains one lead across machines without the previous joke identity', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const css = fs.readFileSync(path.join(__dirname, '../web/style.css'), 'utf8');
  assert.match(html, /<title>AgentCaptain<\/title>/);
  assert.doesNotMatch(html, /\u961f\u957f/);
  assert.match(html, /One captain for agents across your machines\./);
  assert.match(html, /一个主控，统筹本机与远程 Agent。/);
  assert.match(html, /派发、跟进、协调和交付审查/);
  assert.match(html, /多机器统一管理.*集中汇总.*真实终端直接对话/);
  assert.match(html, /你与 Captain 共用这个终端/);
  assert.doesNotMatch(html, /PRON|Personal Remote Ops|CEO|run_pron|pron-hub-logo/i);
  assert.doesNotMatch(css, /#f59b23|\.wordmark span\{[^}]*background:/i);
  assert.match(css, /@media\(max-width:700px\)\{\.topbar\{[^}]*flex-wrap:wrap/);
  assert.match(css, /@media\(max-width:420px\)/);
});

test('brand mark and favicon use the same canonical generated compass PNG asset', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  const mark = fs.readFileSync(path.join(__dirname, '../assets/brand/agentcaptain-mark.png'));
  assert.match(html, /rel="icon" href="\/agent-captain-mark\.png" type="image\/png"/);
  assert.match(html, /class="brand-mark" src="\/agent-captain-mark\.png"/);
  assert.deepEqual([...mark.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
  assert.ok(mark.length < 2 * 1024 * 1024, 'keep the original artwork bounded without stripping provenance');
  assert.ok(fs.existsSync(path.join(__dirname, '../assets/brand/generation.md')));
  assert.ok(!fs.existsSync(path.join(__dirname, '../assets/brand/agentcaptain-mark.svg')));
  assert.ok(!fs.existsSync(path.join(__dirname, '../assets/agent-captain-logo.svg')));
  assert.ok(!fs.existsSync(path.join(__dirname, '../web/agent-captain-mark.svg')));
});

test('empty registry and empty search have different helpful, non-scanning states', async () => {
  const empty = await harness({agents: []}); assert.match(empty.get('#agents').children[0].textContent, /还没有登记/); assert.equal(empty.calls.some(c => /discover/.test(c.url)), false);
  const h = await harness(); setFilter(h, 'query', 'missing project'); assert.match(h.get('#agents').children[0].textContent, /没有符合筛选/); assert.ok(cardButton(h.get('#agents'), '清除筛选，查看工作会话'));
});

test('copied direct link names exact registry key and text metadata is never interpreted as HTML', async () => {
  const h = await harness({agents: [worker('remote', 'two', '<img onerror=alert(1)>')]}); await h.context.copyAgentLink(h.agents[0]);
  assert.equal(h.clipboard[0], 'http://fixture/#agent=remote%3Atwo'); assert.equal(cardTitles(h.get)[0], '<img onerror=alert(1)>');
});

test('new terminal respects explicit machine filter even when an older selected detail is on another machine', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); h.context.selectHost('remote'); h.get('#new-agent').onclick();
  assert.equal(vm.runInContext('selectedAgent', h.context), 'local:one'); assert.equal(vm.runInContext('createTarget.host', h.context), 'remote');
  assert.equal(h.calls.some(call => call.url === '/api/terminals/create'), false);
});

test('a later confirmed-disconnect recovery uses a new key, while uncertain response survives browser reload', async () => {
  const h = await harness({agents: [worker('remote', 'two', 'Offline', {connected: false})]}); await h.context.selectAgent(h.agents[0]); await h.context.reconnectTerminal();
  const first = h.calls.find(call => call.url === '/api/agents/reconnect').body.request_key;
  h.agents[0].connected = false; h.context.updateDisplayConnection(vm.runInContext('terminalSession', h.context), false); await h.context.reconnectTerminal();
  assert.notEqual(h.calls.filter(call => call.url === '/api/agents/reconnect')[1].body.request_key, first);
  const stored = new Map([['pron-hub.worker-workbench.v1', JSON.stringify({agent: 'remote:two', reconnect: [['remote:two', {requestKey: 'existing-uncertain-key', acknowledged: false}]]})]]);
  const reload = await harness({storage: stored, agents: [worker('remote', 'two', 'Offline', {connected: false})]});
  assert.equal(reload.calls.some(call => call.url === '/api/agents/reconnect'), false); await reload.context.reconnectTerminal();
  assert.equal(reload.calls.find(call => call.url === '/api/agents/reconnect').body.request_key, 'existing-uncertain-key');
});

test('canonical connected output reconciles lost successful reconnect before a later disconnect cycle', async () => {
  let lost = true;
  const h = await harness({agents: [worker('remote', 'two', 'Offline', {connected: false})], handle: async (call, backend) => {
    if (call.url === '/api/agents/reconnect' && lost) { lost = false; backend.agents[0].connected = true; throw new Error('response lost after attach'); }
  }});
  await h.context.selectAgent(h.agents[0]); await assert.rejects(h.context.reconnectTerminal(), error => error.code === 'hub_unreachable'); const first = h.calls.find(call => call.url === '/api/agents/reconnect').body.request_key;
  const session = vm.runInContext('terminalSession', h.context); await h.context.pollTerminal(session);
  h.agents[0].connected = false; h.context.updateDisplayConnection(session, false); await h.context.reconnectTerminal();
  assert.notEqual(h.calls.filter(call => call.url === '/api/agents/reconnect')[1].body.request_key, first);
});

for (const outcome of ['classified', 'lost']) test(`late ${outcome} recovery for A is cached on A and cannot overwrite selected B notice`, async () => {
  let release, reject;
  const h = await harness({agents: [worker('remote', 'a', 'A offline', {connected: false}), worker('local', 'b', 'B online')], handle: async call => {
    if (call.url === '/api/agents/reconnect') return new Promise((resolve, fail) => { release = resolve; reject = fail; });
  }});
  await h.context.selectAgent(h.agents[0]); const recovering = h.context.reconnectTerminal().catch(error => error); await settle(); await h.context.selectAgent(h.agents[1]);
  if (outcome === 'classified') release({ok: true, status: 200, json: async () => ({...h.agents[0], recovery: {code: 'tmux_missing', message: 'A original tmux missing', next_action: 'create_new_terminal'}})});
  else reject(new Error('A transport outcome unknown'));
  await recovering;
  assert.equal(vm.runInContext('selectedAgent', h.context), 'local:b'); assert.equal(h.get('#agent-recovery-notice').hidden, true); assert.equal(h.get('#agent-recovery-notice').textContent, '');
  await h.context.selectAgent(h.agents[0]); assert.equal(h.get('#agent-recovery-notice').hidden, false); assert.match(h.get('#agent-recovery-notice').textContent, outcome === 'classified' ? /A original tmux/ : /无法连接本机 AgentCaptain 后台/);
});

test('inline rename uses backend-compatible 200-character limit without accepting oversized names', async () => {
  const h = await harness(); h.context.openAgentRename(h.agents[0]);
  const input = descendants(h.get('#agents')).find(item => item.tagName === 'input' && item.maxLength); assert.equal(input.maxLength, 200);
  vm.runInContext('renameTarget.value = "x".repeat(200)', h.context); await h.context.saveAgentRename(h.agents[0]); assert.equal(h.calls.find(call => call.url === '/api/agents/update').body.alias.length, 200);
  h.context.openAgentRename(h.agents[0]); vm.runInContext('renameTarget.value = "x".repeat(201)', h.context); await assert.rejects(h.context.saveAgentRename(h.agents[0]), /200/);
  assert.equal(h.calls.filter(call => call.url === '/api/agents/update').length, 1);
});

const displayOffline = {state: 'offline', running: false, can_recover: true, recovery_available: true, code: 'bound_owner_offline', message: '本机显示服务已离线；远端任务未确认。'};
const displayReady = {state: 'ready', running: true, can_recover: false, owner_recovered: false};
const localDisplayBlocked = {code: 'local_display_unavailable', message: '本机显示服务离线，原连接尚未附着。', next_action: 'recover_display_service', operation_state: 'blocked', retryable: true};

test('offline display status is read-only on load and refresh, with no automatic recover or reconnect', async () => {
  const h = await harness({agents: [worker('remote', 'two', '316 original', {connected: false})], handle: async (call, backend) => call.url === '/api/terminal-service/status' ? backend.reply(displayOffline) : null});
  assert.equal(h.get('#terminal-service-notice').hidden, false); assert.match(h.get('#terminal-service-message').textContent, /远端任务未确认/);
  await h.context.selectAgent(h.agents[0]); await h.context.refresh(true); await h.context.checkDisplayServiceStatus(true);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  assert.equal(h.calls.some(c => c.url === '/api/agents/reconcile-reconnect'), false);
});

test('readonly service polling preserves selected terminal, rename draft and focus', async () => {
  const h = await harness({handle: async (call, backend) => call.url === '/api/terminal-service/status' ? backend.reply(displayOffline) : null});
  await h.context.selectAgent(h.agents[0]); h.context.openAgentRename(h.agents[0]); const session = vm.runInContext('terminalSession', h.context); session.input = 'pending human text';
  vm.runInContext('renameTarget.value="My retained rename"', h.context); await h.context.checkDisplayServiceStatus(true); await h.context.refresh(true);
  assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.input, 'pending human text'); assert.equal(session.term.focusCount, 0);
  assert.equal(vm.runInContext('renameTarget.value', h.context), 'My retained rename'); assert.equal(vm.runInContext('selectedAgent', h.context), 'local:one');
  assert.equal(h.calls.some(c => /recover|reconcile|\/input/.test(c.url)), false);
});

test('known local display block has clear recovery action and keeps stable reconnect key until human confirmation', async () => {
  let running = false, attempts = 0;
  const h = await harness({agents: [worker('remote', 'two', '316 original', {connected: false}), worker('remote', 'spark', 'Spark original', {connected: false})], handle: async (call, backend) => {
    if (call.url === '/api/terminal-service/status') return backend.reply(running ? displayReady : displayOffline);
    if (call.url === '/api/terminal-service/recover') { assert.deepEqual(call.body, {confirm: true}); running = true; return backend.reply({...displayReady, owner_recovered: true, recovery_id: 'actual-service-proof'}); }
    if (call.url === '/api/agents/reconnect' && attempts++ === 0) return backend.reply({...backend.agents[0], operation_state: 'blocked', recovery: localDisplayBlocked});
  }});
  await h.context.selectAgent(h.agents[0]); await h.context.reconnectTerminal();
  assert.equal(h.get('#terminal-service-recovery-dialog').open, true); assert.match(h.get('#agent-recovery-notice').textContent, /本机显示服务离线/); assert.doesNotMatch(h.get('#agent-recovery-notice').textContent, /结果未确认/);
  assert.equal(h.calls.filter(c => c.url === '/api/terminal-service/recover').length, 0); assert.equal(vm.runInContext('reconnectOperations.get("remote:two").acknowledged', h.context), false);
  assert.match(h.get('#terminal-service-recovery-target').textContent, /仅核验.*316 original/);
  await h.context.recoverDisplayService();
  const reconnect = h.calls.filter(c => c.url === '/api/agents/reconnect'); assert.equal(reconnect.length, 2); assert.equal(reconnect[0].body.request_key, reconnect[1].body.request_key);
  assert.equal(h.agents[1].connected, false); assert.equal(reconnect.every(c => c.body.thread_id === 'two'), true);
  assert.equal(h.calls.some(c => /\/input|\/create|resume|controllers/.test(c.url)), false);
});

test('explicit service recovery does not reconnect a selection changed while recovery is pending', async () => {
  let finish; let running = false;
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false}), worker('remote', 'spark', 'Spark', {connected: false})], handle: async (call, backend) => {
    if (call.url === '/api/terminal-service/status') return backend.reply(running ? displayReady : displayOffline);
    if (call.url === '/api/terminal-service/recover') return new Promise(resolve => { finish = () => { running = true; resolve(backend.reply({...displayReady, owner_recovered: true, recovery_id: 'proof'})); }; });
  }});
  await h.context.selectAgent(h.agents[0]); await h.context.openDisplayServiceRecovery(); const restoring = h.context.recoverDisplayService(); await settle();
  await h.context.selectAgent(h.agents[1]); finish(); await restoring;
  assert.equal(h.calls.some(c => c.url === '/api/agents/reconnect'), false); assert.equal(vm.runInContext('selectedAgent', h.context), 'remote:spark');
});

test('service recovery with retained unsent input does not send it or reconnect the terminal', async () => {
  let running = false;
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false})], handle: async (call, backend) => {
    if (call.url === '/api/terminal-service/status') return backend.reply(running ? displayReady : displayOffline);
    if (call.url === '/api/terminal-service/recover') { running = true; return backend.reply({...displayReady, owner_recovered: true, recovery_id: 'proof'}); }
  }});
  await h.context.selectAgent(h.agents[0]); const session = vm.runInContext('terminalSession', h.context); session.input = 'unsent'; await h.context.openDisplayServiceRecovery(); await h.context.recoverDisplayService();
  assert.equal(session.input, 'unsent'); assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.term.focusCount, 0);
  assert.equal(h.calls.some(c => /\/input|\/reconnect|\/create|controllers/.test(c.url)), false);
});

test('legacy backend 404 offers manual explicit helper and never POSTs a missing recovery API', async () => {
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false, recovery: localDisplayBlocked})], handle: async call => call.url === '/api/terminal-service/status' ? {ok: false, status: 404, json: async () => ({error: 'missing endpoint'})} : null});
  await h.context.selectAgent(h.agents[0]); await h.context.openDisplayServiceRecovery(); assert.equal(h.get('#terminal-service-recovery-legacy').hidden, false); assert.equal(h.get('#terminal-service-recovery-submit').hidden, true);
  await h.context.recoverDisplayService(); assert.equal(h.calls.some(c => c.options.method === 'POST'), false);
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8'); assert.match(html, /python scripts\/captain\.py terminal recover --confirm/); assert.match(html, /本机 PTY 不会重建/);
});

for (const freshProof of [true, false]) test(`legacy unknown recovery ${freshProof ? 'uses real proof and same key after explicit reconcile' : 'keeps unknown without fabricating proof on already-running service'}`, async () => {
  let running = false;
  const storage = new Map([['pron-hub.worker-workbench.v1', JSON.stringify({agent: 'remote:two', reconnect: [['remote:two', {requestKey: 'original-unknown-key', acknowledged: false, nextAction: 'reconcile'}]]})]]);
  const unknown = {code: 'reconnect_unknown', message: '原请求结果未确认', next_action: 'reconcile'};
  const h = await harness({storage, agents: [worker('remote', 'two', '316', {connected: false, recovery: unknown})], handle: async (call, backend) => {
    if (call.url === '/api/terminal-service/status') return backend.reply(running ? displayReady : displayOffline);
    if (call.url === '/api/terminal-service/recover') { running = true; return backend.reply(freshProof ? {...displayReady, owner_recovered: true, recovery_id: 'real-proof'} : displayReady); }
    if (call.url === '/api/agents/reconcile-reconnect') { assert.deepEqual(call.body, {host: 'remote', thread_id: 'two', request_key: 'original-unknown-key', confirm: true}); return backend.reply({...backend.agents[0], operation_state: 'blocked', recovery: {...localDisplayBlocked, next_action: 'reconnect'}}); }
  }});
  assert.equal(h.calls.some(c => /reconcile|recover|reconnect/.test(c.url)), false);
  await h.context.openDisplayServiceRecovery(); await h.context.recoverDisplayService();
  if (freshProof) { assert.equal(h.calls.filter(c => c.url === '/api/agents/reconcile-reconnect').length, 1); assert.equal(h.calls.find(c => c.url === '/api/agents/reconnect').body.request_key, 'original-unknown-key'); }
  else { assert.equal(h.calls.some(c => /reconcile-reconnect|\/agents\/reconnect/.test(c.url)), false); assert.match(h.get('#terminal-service-recovery-error').textContent, /没有本次恢复的核验证据/); assert.equal(vm.runInContext('reconnectOperations.get("remote:two").acknowledged', h.context), false); }
  assert.equal(h.calls.some(c => /\/input|\/create|resume/.test(c.url)), false);
});

test('unknown or blocked owner status stays fenced and cannot be presented as a recoverable dead process', async () => {
  const h = await harness({handle: async (call, backend) => call.url === '/api/terminal-service/status' ? backend.reply({state: 'unknown', running: false, can_recover: false, message: '原 owner 身份待核验'}) : null});
  assert.equal(h.get('#terminal-service-notice').hidden, false); assert.match(h.get('#terminal-service-heading').textContent, /状态待核验/); assert.equal(h.get('#terminal-service-recover').disabled, true); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

test('lost browser storage plus unknown backend result never creates a new reconnect key', async () => {
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false, recovery: {code: 'reconnect_unknown', next_action: 'reconcile', message: '旧重连未知'}})]});
  await h.context.selectAgent(h.agents[0]); await h.context.reconnectTerminal();
  assert.equal(h.calls.some(c => /\/agents\/reconnect|reconcile-reconnect/.test(c.url)), false); assert.equal(vm.runInContext('reconnectOperations.size', h.context), 0);
  assert.match(h.get('#agent-recovery-notice').textContent, /不会创建新键绕过未知结果/);
});

test('server-proven original key offers manual reconciliation after storage loss, never acts before confirmation', async () => {
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false, recovery: {code: 'reconnect_unknown', next_action: 'reconcile_reconnect', reconcile_available: true, reconcile_request_keys: ['original-backend-key']}})], handle: async (call, backend) => {
    if (call.url === '/api/agents/reconcile-reconnect') return backend.reply({...backend.agents[0], operation_state: 'blocked', recovery: {...localDisplayBlocked, next_action: 'reconnect'}});
  }});
  await h.context.selectAgent(h.agents[0]); assert.equal(h.get('#terminal-reconcile').hidden, false); await h.context.reconnectTerminal();
  assert.equal(h.get('#terminal-reconcile-dialog').open, true); assert.equal(h.get('#terminal-reconcile-key').value, 'original-backend-key');
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  await h.context.reconcileSelectedReconnect();
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/reconcile-reconnect').body, {host: 'remote', thread_id: 'two', request_key: 'original-backend-key', confirm: true});
  assert.equal(h.calls.find(c => c.url === '/api/agents/reconnect').body.request_key, 'original-backend-key');
  assert.equal(h.calls.some(c => /terminal-service\/recover|\/input|\/create|controllers/.test(c.url)), false);
});

test('connected unknown display offers exact-key verification and accepts done without reattaching', async () => {
  const h = await harness({agents: [worker('remote', 'two', 'Spark', {recovery: {code: 'reconnect_unknown', next_action: 'reconcile_reconnect', reconcile_kind: 'live_display', reconcile_available: true, reconcile_request_keys: ['original-live-key']}})], handle: async (call, backend) => {
    if (call.url === '/api/agents/reconcile-reconnect') return backend.reply({...backend.agents[0], operation_state: 'done', recovery: {code: 'reconnected', reconciliation_kind: 'live_display', reconciled: true, retryable: false, next_action: 'open_terminal'}});
  }});
  await h.context.selectAgent(h.agents[0]);
  const session = vm.runInContext('terminalSession', h.context);
  assert.equal(h.get('#terminal-reconcile').hidden, false); assert.equal(h.get('#terminal-reconcile').textContent, '核对原连接');
  await h.context.reconnectTerminal();
  assert.equal(h.get('#terminal-reconcile-dialog').open, true);
  assert.match(h.get('#terminal-reconcile-dialog .field-help').textContent, /不重新附着.*不发送输入/);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  await h.context.reconcileSelectedReconnect();
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/reconcile-reconnect').body, {host: 'remote', thread_id: 'two', request_key: 'original-live-key', confirm: true});
  assert.equal(h.calls.some(c => /\/agents\/reconnect|\/input|\/create|\/close|terminal-service\/recover/.test(c.url)), false);
  assert.equal(vm.runInContext('terminalSession', h.context), session, 'Verification preserves the existing browser terminal');
  assert.equal(vm.runInContext('reconnectOperations.get("remote:two").acknowledged', h.context), true);
  assert.equal(h.get('#terminal-reconcile-dialog').open, false);
});

test('fresh readiness replaces only an obsolete awaiting-foreground notice without rewriting receipts', async () => {
  const wait = '原终端显示已附着且身份一致；前台尚未就绪，请进入终端核对。';
  const h = await harness({agents: [worker('remote', 'two', 'Spark', {recovery: {code: 'awaiting_foreground', message: wait}})]});
  await h.context.selectAgent(h.agents[0]);
  assert.equal(h.get('#agent-recovery-notice').textContent, '原终端已连接，前台已核验可交互。');
  h.context.waitNotice = wait; vm.runInContext('agentRecoveryNotices.set("remote:two", waitNotice);', h.context);
  h.context.renderAgentRecoveryNotice(h.agents[0]);
  assert.equal(h.get('#agent-recovery-notice').textContent, '原终端已连接，前台已核验可交互。');
  assert.equal(h.agents[0].recovery.message, wait); assert.equal(vm.runInContext('agentRecoveryNotices.get("remote:two")', h.context), wait);
  for (const change of [{connected: false}, {agent_ready: false}, {human_busy: true}, {blocked_reason: 'authentication'}, {recovery: {code: 'reconnect_unknown', message: wait}}]) {
    h.context.renderAgentRecoveryNotice({...h.agents[0], ...change});
    assert.equal(h.get('#agent-recovery-notice').textContent, wait);
  }
  vm.runInContext('agentRecoveryNotices.set("remote:two", "当前登录需要处理");', h.context);
  h.context.renderAgentRecoveryNotice(h.agents[0]);
  assert.equal(h.get('#agent-recovery-notice').textContent, '当前登录需要处理');
});

test('connected Captain request captures its plugin endpoint and key without impersonating UI origin', async () => {
  const h = await harness({agents: [worker('remote', 'two', 'Spark', {recovery: {code: 'reconnect_unknown', request_origin: 'plugin', next_action: 'reconcile_original_worker', reconcile_kind: 'live_display', reconcile_available: true, reconcile_request_keys: ['original-plugin-key']}})], handle: async (call, backend) => {
    if (call.url === '/api/agents/reconcile-original-worker') return backend.reply({...backend.agents[0], operation: 'reconcile_original_worker', operation_state: 'done', recovery: {code: 'reconnected', request_origin: 'plugin', reconciliation_kind: 'live_display', reconciled: true, retryable: false, next_action: 'open_terminal'}});
  }});
  await h.context.selectAgent(h.agents[0]); const session = vm.runInContext('terminalSession', h.context);
  await h.context.reconnectTerminal();
  // The request's protocol belongs to the captured confirmation target, not a
  // subsequent metadata refresh which might carry a different next_action.
  h.agents[0].recovery.next_action = 'reconcile_reconnect';
  await h.context.reconcileSelectedReconnect();
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/reconcile-original-worker').body, {host: 'remote', thread_id: 'two', request_key: 'original-plugin-key', confirm: true});
  assert.equal(h.calls.some(c => /\/agents\/(?:reconcile-reconnect|reconnect)|\/input|\/create|\/close/.test(c.url)), false);
  assert.equal(vm.runInContext('reconnectOperations.get("remote:two").requestOrigin', h.context), 'plugin');
  assert.equal(vm.runInContext('reconnectOperations.get("remote:two").requestKey', h.context), 'original-plugin-key');
  assert.equal(vm.runInContext('terminalSession', h.context), session);
});

test('reconciled disconnected plugin request directs Captain to same key without a UI reconnect', async () => {
  const h = await harness({agents: [worker('remote', 'two', 'Spark', {connected: false, recovery: {code: 'reconnect_unknown', request_origin: 'plugin', next_action: 'reconcile_original_worker', reconcile_available: true, reconcile_request_keys: ['owner-proof-plugin-key']}})], handle: async (call, backend) => {
    if (call.url === '/api/agents/reconcile-original-worker') return backend.reply({...backend.agents[0], operation_state: 'blocked', recovery: {code: 'local_display_unavailable', request_origin: 'plugin', request_key: 'owner-proof-plugin-key', reconciled: true, retryable: true, next_action: 'reconnect'}});
  }});
  await h.context.selectAgent(h.agents[0]); await h.context.reconnectTerminal(); await h.context.reconcileSelectedReconnect();
  await h.context.reconnectTerminal();
  assert.equal(h.calls.filter(c => c.url === '/api/agents/reconcile-original-worker').length, 1);
  assert.equal(h.calls.some(c => /\/agents\/(?:reconcile-reconnect|reconnect)|\/input|\/create|\/close/.test(c.url)), false);
  assert.equal(vm.runInContext('reconnectOperations.get("remote:two").requestOrigin', h.context), 'plugin');
  assert.equal(vm.runInContext('reconnectOperations.get("remote:two").requestKey', h.context), 'owner-proof-plugin-key');
  assert.match(h.get('#agent-recovery-notice').textContent, /Captain.*同一插件请求键.*不会以 UI 来源重试/);
});

test('multiple server-known keys require human selection, not first-key guessing; invalid key is inert', async () => {
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false, recovery: {code: 'reconnect_unknown', reconcile_available: true, reconcile_request_keys: ['first-old-key', 'second-old-key']}})], handle: async (call, backend) => call.url === '/api/agents/reconcile-reconnect' ? backend.reply({...backend.agents[0], operation_state: 'blocked', recovery: {...localDisplayBlocked, next_action: 'reconnect'}}) : null});
  await h.context.selectAgent(h.agents[0]); h.context.openReconnectReconciliation(); assert.equal(h.get('#terminal-reconcile-key').value, ''); assert.equal(h.get('#terminal-reconcile-confirm').disabled, true);
  await h.context.reconcileSelectedReconnect(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.get('#terminal-reconcile-key').value = 'invented-key'; await h.context.reconcileSelectedReconnect(); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  h.get('#terminal-reconcile-key').value = 'second-old-key'; h.get('#terminal-reconcile-key').onchange(); await h.context.reconcileSelectedReconnect();
  assert.equal(h.calls.find(c => c.url === '/api/agents/reconcile-reconnect').body.request_key, 'second-old-key'); assert.equal(h.calls.find(c => c.url === '/api/agents/reconnect').body.request_key, 'second-old-key');
});

test('manual reconciliation refusal or switched target never attaches another worker', async () => {
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false, recovery: {code: 'reconnect_unknown', reconcile_available: true, reconcile_request_keys: ['original-key']}}), worker('remote', 'spark', 'Spark', {connected: false})], handle: async call => call.url === '/api/agents/reconcile-reconnect' ? {ok: false, status: 409, json: async () => ({error: '没有原 owner 的足够证据'})} : null});
  await h.context.selectAgent(h.agents[0]); h.context.openReconnectReconciliation(); await h.context.reconcileSelectedReconnect();
  assert.match(h.get('#terminal-reconcile-error').textContent, /足够证据/); assert.equal(h.calls.some(c => c.url === '/api/agents/reconnect'), false);
  await h.context.selectAgent(h.agents[1]); await assert.rejects(h.context.reconcileSelectedReconnect(), /当前工作会话已改变/);
  assert.equal(h.calls.filter(c => c.url === '/api/agents/reconcile-reconnect').length, 1); assert.equal(h.agents[1].connected, false);
});

test('selection changed during per-agent reconciliation keeps key on original worker without reattaching either', async () => {
  let finish;
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false, recovery: {code: 'reconnect_unknown', reconcile_available: true, reconcile_request_keys: ['original-key']}}), worker('remote', 'spark', 'Spark', {connected: false})], handle: async (call, backend) => call.url === '/api/agents/reconcile-reconnect' ? new Promise(resolve => { finish = () => resolve(backend.reply({...backend.agents[0], operation_state: 'blocked', recovery: {...localDisplayBlocked, next_action: 'reconnect'}})); }) : null});
  await h.context.selectAgent(h.agents[0]); h.context.openReconnectReconciliation(); const pending = h.context.reconcileSelectedReconnect(); await settle(); await h.context.selectAgent(h.agents[1]); finish(); await pending;
  assert.equal(h.calls.some(c => c.url === '/api/agents/reconnect'), false); assert.equal(vm.runInContext('reconnectOperations.get("remote:two").requestKey', h.context), 'original-key'); assert.equal(vm.runInContext('reconnectOperations.has("remote:spark")', h.context), false);
});

for (const origin of ['ui', 'plugin', undefined]) test(`known blocked ${origin || 'unspecified'} recovery preserves request origin after storage loss`, async () => {
  const h = await harness({agents: [worker('remote', 'two', '316', {connected: false, recovery: {...localDisplayBlocked, request_origin: origin, request_key: 'exact-old-key'}}), worker('remote', 'spark', 'Spark', {connected: false, recovery: {...localDisplayBlocked, request_origin: 'ui', request_key: 'different-agent-key'}})]});
  await h.context.selectAgent(h.agents[0]); await h.context.reconnectTerminal(); const operation = h.calls.find(c => c.url === '/api/agents/reconnect');
  if (origin === 'plugin') {
    assert.equal(operation, undefined, 'A plugin retry cannot be converted into a new UI-origin attempt');
    assert.match(h.get('#agent-recovery-notice').textContent, /Captain.*同一插件请求键.*不会以 UI 来源重试/);
    assert.equal(h.calls.some(c => /\/input|\/create|controllers/.test(c.url)), false);
    return;
  }
  assert.equal(operation.body.host, 'remote'); assert.equal(operation.body.thread_id, 'two');
  if (origin === 'ui') assert.equal(operation.body.request_key, 'exact-old-key'); else assert.notEqual(operation.body.request_key, 'exact-old-key');
  assert.notEqual(operation.body.request_key, 'different-agent-key'); assert.equal(h.calls.some(c => /\/input|\/create|controllers/.test(c.url)), false);
});
