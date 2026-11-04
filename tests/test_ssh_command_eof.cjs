'use strict';
// Only local Node fixtures and fake SSH/PTY sinks. No network, real SSH,
// production terminal, model process, or worker input is used by these tests.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const childProcess = require('node:child_process');
const { promisify } = require('node:util');

const brokerPath = path.resolve(__dirname, '..', 'terminal', 'terminal_broker.cjs');
const source = fs.readFileSync(brokerPath, 'utf8');
const actualExecFileAsync = promisify(childProcess.execFile);
const foreground = { command: 'node', state: 'agent', reason: 'verified_foreground_codex',
  identity_verified: true, agent_kind: 'codex', agent_pid: 201, agent_start_ticks: 10201, foreground_pgid: 200 };

function loadBroker(overrides = {}, platform = process.platform) {
  const module = { exports: {} };
  const fakeProcess = Object.create(process);
  Object.defineProperty(fakeProcess, 'platform', { value: platform });
  vm.runInNewContext(source + '\nmodule.exports.testCommandResult = commandResult;', {
    module, exports: module.exports, __dirname: path.dirname(brokerPath), __filename: brokerPath,
    require: name => name === 'node:child_process' ? { ...childProcess, ...overrides } : require(name),
    process: fakeProcess, Buffer, URL, console, setTimeout, clearTimeout,
  }, { filename: brokerPath });
  return module.exports;
}

function fakeTransport(respond) {
  const commands = [], spawns = [];
  function execFile() { throw new Error('Expected the promisified execFile contract'); }
  execFile[promisify.custom] = (file, args, options) => {
    const response = respond(file, args);
    // A real local process deliberately prints once, then waits for stdin EOF
    // before returning the helper payload. The old broker hangs here.
    const script = `process.stdout.write(${JSON.stringify(response.preamble ?? 'fixture started\n')});process.stdin.resume();process.stdin.on('end',()=>{process.stdout.write(${JSON.stringify(response.stdout || '')});process.stderr.write(${JSON.stringify(response.stderr || '')});process.exitCode=${response.code || 0};});`;
    const result = actualExecFileAsync(process.execPath, ['-e', script], options);
    commands.push({ file, args, options, child: result.child });
    return result;
  };
  function spawn(file, args, options) {
    const child = new EventEmitter();
    Object.assign(child, { pid: 123, stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(), killed: false, input: [] });
    child.stdin.on('data', data => child.input.push(Buffer.from(data)));
    child.kill = () => { child.killed = true; child.emit('close', 0); return true; };
    spawns.push({ file, args, options, child });
    queueMicrotask(() => child.emit('spawn'));
    return child;
  }
  return { execFile, spawn, commands, spawns };
}

async function remoteFixture(t, transport) {
  const { TerminalBroker } = loadBroker(transport);
  const broker = new TerminalBroker();
  const created = await broker.create({ transport: 'ssh', target: 'fake-test-host', host: 'fake-test-host', cwd: '/tmp', cols: 80, rows: 30 });
  const session = broker.get(created.id);
  t.after(() => { session.pty.kill(); session.terminal.dispose(); });
  return { broker, session, id: created.id };
}

test('no-input command closes stdin immediately and drains both first and post-EOF output', async () => {
  const { testCommandResult } = loadBroker();
  const result = await testCommandResult(process.execPath, ['-e', "process.stdout.write('first\\n');process.stdin.resume();process.stdin.on('end',()=>{process.stdout.write('after EOF\\n');process.stderr.write('fixture warning\\n');});"], 3000);
  assert.equal(result.stdout, 'first\nafter EOF\n');
  assert.equal(result.stderr, 'fixture warning\n');
});

test('EOF preserves nonzero exit metadata and a missing executable still rejects', async () => {
  const { testCommandResult } = loadBroker();
  await assert.rejects(testCommandResult(process.execPath, ['-e', "process.stdout.write('first\\n');process.stdin.resume();process.stdin.on('end',()=>{process.stderr.write('failed after EOF');process.exitCode=7;});"], 3000), error => {
    assert.equal(error.code, 7);
    assert.equal(error.stdout, 'first\n');
    assert.equal(error.stderr, 'failed after EOF');
    return true;
  });
  await assert.rejects(testCommandResult(path.join(__dirname, 'nonexistent-eof-test-executable'), [], 3000), error => error.code === 'ENOENT');
});

test('a command ignoring EOF remains time-bounded and retains captured output', async () => {
  const { testCommandResult } = loadBroker();
  await assert.rejects(testCommandResult(process.execPath, ['-e', "process.stdout.write('still running');process.stderr.write('fixture warning');process.stdin.resume();setInterval(()=>{},1000);"], 1000), error => {
    assert.equal(error.killed, true);
    assert.equal(error.stdout, 'still running');
    assert.equal(error.stderr, 'fixture warning');
    return true;
  });
});

test('SSH probe, resize, and close end only auxiliary stdin, keeping interactive stdin writable', async t => {
  const transport = fakeTransport((file, args) => {
    const command = args.at(-1);
    if (command.includes('ceo-hub-foreground-probe')) return { stdout: `CEO_HUB_FOREGROUND_RESULT=${JSON.stringify(foreground)}\n` };
    if (command.includes("'resize'")) return { stdout: 'CEO_HUB_PTY_RESULT={"ok":true,"cols":91,"rows":28,"client_cols":91,"client_rows":28,"window_cols":91,"window_rows":28}\n' };
    assert.match(command, /^tmux kill-session -t 'ceohub-[a-f0-9]+'/);
    return { stdout: '' };
  });
  const f = await remoteFixture(t, transport);
  const interactive = transport.spawns[0];
  assert.deepEqual(Array.from(interactive.options.stdio), ['pipe', 'pipe', 'pipe']);
  assert.equal(interactive.args[0], '-tt');
  assert.equal(interactive.args.includes('-n'), false);
  assert.equal(interactive.child.stdin.writableEnded, false);
  const result = await f.broker.probeForeground(f.session);
  assert.equal(result.state, 'agent');
  assert.equal(result.command, 'node');
  await f.broker.resize(f.id, { cols: 91, rows: 28 });
  assert.equal(f.session.geometryUnknown, false);
  assert.equal(f.session.cols, 91);
  assert.equal(f.session.rows, 28);
  // This is a fake byte sink, not input into a real terminal or coding agent.
  f.session.pty.write('\x1b[Aordinary fake input\r');
  assert.equal(Buffer.concat(interactive.child.input).toString(), '\x1b[Aordinary fake input\r');
  assert.equal(interactive.child.stdin.writableEnded, false);
  assert.equal(interactive.child.killed, false);
  const closed = await f.broker.close(f.id);
  assert.equal(closed.closed, true);
  assert.equal(f.session.connected, false);
  assert.equal(interactive.child.killed, true);
  assert.equal(transport.commands.length, 3);
  for (const command of transport.commands) {
    assert.equal(command.child.stdin.writableEnded, true);
    const isForeground = command.args.at(-1).includes('ceo-hub-foreground-probe');
    const isResize = command.args.at(-1).includes("'resize'");
    assert.ok(command.args.includes(isForeground || isResize ? 'ConnectTimeout=60' : 'ConnectTimeout=8'));
    if (isForeground || isResize) {
      assert.ok(command.args.includes('ControlMaster=no'));
      assert.ok(command.args.includes('ControlPersist=no'));
      assert.ok(command.args.includes('ControlPath=none'), 'An existing configured master cannot bypass the independent check');
      assert.equal(command.options.timeout, 150000, 'Authenticated probe/resize queries share a bounded slow-relay budget');
    } else {
      assert.ok(command.options.timeout <= 18000, 'Other auxiliary operations retain their existing budget');
    }
    assert.ok(command.args.includes('BatchMode=yes'));
    assert.equal(command.options.windowsHide, true);
    assert.equal(command.options.maxBuffer, 2 * 1024 * 1024);
    assert.equal(command.options.encoding, 'utf8');
  }
});

test('failed auxiliary SSH close cannot mark the interactive terminal closed or kill its transport', async t => {
  const transport = fakeTransport(() => ({ stdout: 'fixture close failed\n', stderr: 'fixture failure\n', code: 255 }));
  const f = await remoteFixture(t, transport);
  await assert.rejects(f.broker.close(f.id), error => error.code === 255);
  assert.equal(transport.commands[0].child.stdin.writableEnded, true);
  assert.equal(f.session.connected, true);
  assert.equal(transport.spawns[0].child.killed, false);
  assert.equal(transport.spawns[0].child.stdin.writableEnded, false);
});

test('Windows PowerShell and POSIX process probes are also explicitly no-input commands', async () => {
  for (const platform of ['win32', 'linux']) {
    const transport = fakeTransport(file => ({ preamble: '', stdout: file === 'ps' ? '123 1 123 123 bash\n' : '[]\n' }));
    const { TerminalBroker } = loadBroker(transport, platform);
    const broker = new TerminalBroker();
    const result = await broker.probeForeground({ transport: 'local', pty: { pid: 123, process: platform === 'win32' ? 'powershell.exe' : 'bash' } });
    assert.equal(result.state, 'shell');
    assert.equal(transport.commands.length, 1);
    assert.equal(transport.commands[0].child.stdin.writableEnded, true);
    assert.equal(transport.commands[0].file, platform === 'win32' ? 'powershell.exe' : 'ps');
  }
});
