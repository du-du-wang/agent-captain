'use strict';

// This process owns the PTYs, not the web UI or the Python Hub. A browser or Hub
// restart therefore does not send EOF to the user's shell or coding agent.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const net = require('node:net');
const zlib = require('node:zlib');
const { execFile, execFileSync, spawn: spawnChild } = require('node:child_process');
const { promisify } = require('node:util');
const pty = require('node-pty');
const { Terminal } = require('@xterm/headless');
const { SerializeAddon } = require('@xterm/addon-serialize');
const { updateHumanInput, reserveHumanInput, humanInputState } = require(path.join(__dirname, 'input_activity.cjs'));
const execFileAsync = promisify(execFile);
const ROOT = path.resolve(__dirname, '..');
const PYTHON_SOURCE = path.join(ROOT, 'src', 'agent_captain');
// Existing descriptors name the original launcher. Accept only these two exact
// checkout-owned paths so upgrades preserve leases without trusting basenames.
const BROKER_SCRIPTS = [path.resolve(__filename), path.join(ROOT, 'terminal_broker.cjs')];
const normalizedPath = value => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value);
const brokerScript = value => typeof value === 'string' && path.isAbsolute(value)
  && BROKER_SCRIPTS.some(script => normalizedPath(script) === normalizedPath(value));
const ENTRY_SCRIPT = process.argv[1] && brokerScript(path.resolve(process.argv[1]))
  ? path.resolve(process.argv[1]) : path.resolve(__filename);

const VERSION = '0.3.0';
const SESSION_ID = /^ceohub-[a-f0-9]{16,48}$/;
const SHELLS = new Set(['bash', 'sh', 'zsh', 'fish', 'dash', 'pwsh', 'powershell', 'cmd']);
const AGENTS = /(?:^|[\\/\s])(?:codex|claude|aider|opencode|gemini)(?:\.(?:exe|cmd|ps1|cjs|mjs|js))?(?:$|[\s\\/])/i;
const MAX_OUTPUT = 2 * 1024 * 1024;
// Authentication on relayed routes can exceed a minute. This is a complete
// control-command deadline, not permission to reuse identity or resize proofs.
const SSH_CONTROL_BUDGET = Object.freeze({ connectSeconds: 60, commandMs: 150000 });

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

function shellQuote(value) { return "'" + String(value).replace(/'/g, "'\\''") + "'"; }
function boundedInteger(value, fallback, minimum, maximum) {
  const n = value == null ? fallback : Number(value);
  if (!Number.isInteger(n) || n < minimum || n > maximum) {
    throw new ApiError(400, 'invalid_size', `Expected an integer between ${minimum} and ${maximum}`);
  }
  return n;
}
function classifyCommand(command) {
  const name = path.basename(String(command || '').trim()).replace(/\.exe$/i, '').toLowerCase();
  if (AGENTS.test(String(command || ''))) return 'agent';
  if (SHELLS.has(name)) return 'shell';
  return 'unknown';
}

function descriptorPath() {
  if (process.env.CEO_HUB_TERMINAL_FILE) return path.resolve(process.env.CEO_HUB_TERMINAL_FILE);
  const base = process.platform === 'win32'
    ? path.join(process.env.LOCALAPPDATA || os.homedir(), 'CEOHub')
    : path.join(os.homedir(), '.local', 'state', 'ceo-hub');
  return path.join(base, 'terminal-broker.json');
}

function sshExecutable() {
  return process.env.CEO_HUB_SSH || (process.platform === 'win32' ? path.join(process.env.WINDIR || 'C:\\Windows', 'System32', 'OpenSSH', 'ssh.exe') : 'ssh');
}

function blockedScreen(session) {
  const buffer = session.terminal.buffer.active;
  const bottom = Array.from({ length: session.rows }, (_, i) => buffer.getLine(buffer.baseY + i)?.translateToString(true) || '').join('\n');
  if (/(?:enter|paste|provide)\s+(?:your\s+)?(?:api[ -]?key|password|verification code)|sign in (?:with|to)|press enter to (?:open|continue.*sign)|authentication required|log in to/i.test(bottom)) return 'authentication';
  if (/would you like to (?:run|allow|approve)|approve this (?:command|action)|allow (?:once|for this session)|requires (?:your )?approval|trust this (?:folder|directory)|do you trust|yes, proceed|1\.\s+(?:yes|allow|approve)/i.test(bottom)) return 'approval';
  // A foreground Codex process can still be choosing its startup mode. Require
  // the compatibility heading and a numbered action, not daemon prose alone.
  if (/background\s+server\s+has\s+incompatible\s+feature\s+settings/i.test(bottom)
      && /(?:^|\n)\s*(?:[›❯»>]\s*)?(?:1\.\s*run\s+without\s+(?:the\s+)?daemon\s+this\s+time|2\.\s*restart\s+with\s+these\s+settings)/i.test(bottom)) return 'setup_menu';
  // An upgrade choice is also startup UI, never an ordinary task prompt. The
  // title and both kinds of numbered choices avoid matching update prose.
  if (/update\s+available\b/i.test(bottom)
      && /(?:^|\n)\s*(?:[›❯»>]\s*)?1\.\s*update\s+now\b/i.test(bottom)
      && /(?:^|\n)\s*(?:[›❯»>]\s*)?[23]\.\s*skip\b/i.test(bottom)) return 'setup_menu';
  if (/choose (?:a |your )?(?:model|provider|account)|select (?:a |your )?(?:model|provider|account)|how would you like to sign in/i.test(bottom)) return 'setup_menu';
  if (/this conversation is open in another app/i.test(bottom)
      && /(?:press\s+r\b|close it there|retry|fork)/i.test(bottom)) return 'session_locked';
  if (/(?:resume (?:a |an |previous )?(?:session|conversation)|select (?:a |an )?(?:session|conversation))/i.test(bottom)
      && /(?:search|enter|esc|↑|↓|up.*down)/i.test(bottom)) return 'setup_menu';
  return null;
}

async function commandResult(file, args, timeout = 12000) {
  const result = execFileAsync(file, args, { windowsHide: true, timeout, maxBuffer: 2 * 1024 * 1024, encoding: 'utf8' });
  // Every caller here is a no-input command. Windows OpenSSH can otherwise
  // wait after its first output for this pipe to reach EOF. This must not be
  // shared with spawnSshPipe: interactive terminals keep stdin open for input.
  if (result.child.stdin) {
    result.child.stdin.on('error', () => {}); // The process result still reports spawn/exit failures.
    result.child.stdin.end();
  }
  return result;
}

function remoteBridgeCommand(mode, { sessionId, bridgeId, cols, rows, cwd = '' }) {
  if (!['attach', 'resize'].includes(mode) || !SESSION_ID.test(sessionId) || !/^[a-f0-9]{24,48}$/.test(bridgeId)) {
    throw new ApiError(400, 'invalid_bridge', 'Invalid owned remote display identity');
  }
  cols = boundedInteger(cols, 100, 20, 500); rows = boundedInteger(rows, 30, 5, 200);
  if (typeof cwd !== 'string' || cwd.length > 4096 || /[\x00-\x1f\x7f-\x9f]/.test(cwd)) throw new ApiError(400, 'invalid_directory', 'Invalid terminal directory');
  // Inline the local standalone helper; no remote installation/configuration or
  // auth files. Compression keeps even quoted long paths below Windows argv limits.
  const body = zlib.deflateSync(fs.readFileSync(path.join(PYTHON_SOURCE, 'transport', 'remote_pty_bridge.py'))).toString('base64');
  const bootstrap = `import base64,zlib;exec(compile(zlib.decompress(base64.b64decode('${body}')),'ceo-hub-pty-bridge','exec'))`;
  const args = ['python3', '-c', bootstrap, mode, '--session', sessionId, '--bridge-id', bridgeId,
    '--cols', String(cols), '--rows', String(rows)];
  if (mode === 'attach') args.push('--cwd', cwd);
  return args.map(shellQuote).join(' ');
}

function remoteForegroundCommand(sessionId) {
  if (!SESSION_ID.test(sessionId)) throw new ApiError(400, 'invalid_session_id', 'Invalid owned terminal session ID');
  const body = zlib.deflateSync(fs.readFileSync(path.join(PYTHON_SOURCE, 'transport', 'remote_foreground_probe.py'))).toString('base64');
  const bootstrap = `import base64,zlib;exec(compile(zlib.decompress(base64.b64decode('${body}')),'ceo-hub-foreground-probe','exec'))`;
  return ['python3', '-c', bootstrap, '--session', sessionId].map(shellQuote).join(' ');
}

function validRemoteCloseIdentity(value, sessionId) {
  return value && Object.keys(value).sort().join(',') === 'boot_id,created,name,pane,pane_pid,pane_start_ticks,server_pid,session'
    && value.name === sessionId && typeof value.session === 'string' && /^\$\d+$/.test(value.session)
    && typeof value.pane === 'string' && /^%\d+$/.test(value.pane)
    && typeof value.boot_id === 'string' && /^[a-f0-9-]{36}$/.test(value.boot_id)
    && ['created', 'pane_pid', 'pane_start_ticks', 'server_pid'].every(key => Number.isSafeInteger(value[key]) && value[key] > 0);
}

function remoteCloseCommand(sessionId, expected) {
  if (!SESSION_ID.test(sessionId) || !expected || typeof expected.exists !== 'boolean'
      || Object.keys(expected).sort().join(',') !== (expected.exists ? 'exists,identity' : 'exists')
      || expected.exists && !validRemoteCloseIdentity(expected.identity, sessionId)) {
    throw new ApiError(400, 'invalid_close_identity', 'Exact terminal close identity is required');
  }
  const body = zlib.deflateSync(fs.readFileSync(path.join(PYTHON_SOURCE, 'transport', 'remote_terminal_close.py'))).toString('base64');
  const bootstrap = `import base64,zlib;exec(compile(zlib.decompress(base64.b64decode('${body}')),'ceo-hub-terminal-close','exec'))`;
  const args = ['python3', '-c', bootstrap, 'close', '--session', sessionId];
  if (expected.exists) args.push('--expected', Buffer.from(JSON.stringify(expected.identity)).toString('base64url'));
  else args.push('--expected-absent');
  return args.map(shellQuote).join(' ');
}

function verifiedIdentity(result) {
  if (result.identity_verified !== true || result.state !== 'agent'
      || !['codex', 'claude', 'aider', 'opencode', 'gemini'].includes(result.agent_kind)
      || ![result.agent_pid, result.agent_start_ticks, result.foreground_pgid].every(value => Number.isSafeInteger(value) && value > 0)) return null;
  return { kind: result.agent_kind, pid: result.agent_pid, startTicks: result.agent_start_ticks, pgid: result.foreground_pgid };
}

function sameIdentity(one, two) {
  return !!one && !!two && one.kind === two.kind && one.pid === two.pid && one.startTicks === two.startTicks && one.pgid === two.pgid;
}

function validDisplayIdentity(identity, platform) {
  const paths = platform === 'win32' ? path.win32 : path.posix;
  return identity && Object.keys(identity).sort().join(',') === 'birth,executable'
    && typeof identity.birth === 'string' && /^(win-filetime:[0-9]{1,24}|linux-start:[a-f0-9-]{36}:[0-9]{1,24})$/.test(identity.birth)
    && typeof identity.executable === 'string' && identity.executable.length > 0 && identity.executable.length <= 4096
    && !/[\x00-\x1f]/.test(identity.executable) && paths.isAbsolute(identity.executable)
    && ['ssh', 'ssh.exe'].includes(paths.basename(identity.executable).toLowerCase());
}

function nativeRootKind(program, platform) {
  if (typeof program !== 'string') return null;
  const name = (platform === 'win32' ? path.win32 : path.posix).basename(program);
  const match = (platform === 'win32' ? /^(codex|claude|aider|opencode|gemini)\.exe$/i : /^(codex|claude|aider|opencode|gemini)$/).exec(name);
  return match ? match[1].toLowerCase() : null;
}

function directRootIdentity(session, metadata, platform) {
  // Exact kernel process identity, not argv text, a matching filename in a process
  // tree, or a UI title. Only an owned directly launched native agent qualifies.
  const kind = nativeRootKind(session.program, platform);
  if (!kind || session.transport !== 'local' || !metadata || metadata.pid !== session.pty?.pid
      || !Number.isSafeInteger(metadata.pid) || metadata.pid <= 0
      || !Number.isSafeInteger(metadata.startTicks) || metadata.startTicks <= 0
      || typeof metadata.executable !== 'string' || typeof metadata.expectedExecutable !== 'string'
      || metadata.executable.length > 4096 || metadata.expectedExecutable.length > 4096
      || metadata.executable.includes('\0') || metadata.expectedExecutable.includes('\0')) return null;
  const normalize = value => platform === 'win32' ? path.win32.normalize(value).toLowerCase() : path.posix.normalize(value);
  if (!metadata.executable || normalize(metadata.executable) !== normalize(metadata.expectedExecutable)
      || nativeRootKind(metadata.expectedExecutable, platform) !== kind) return null;
  if (platform === 'win32' && nativeRootKind(metadata.name || '', platform) !== kind) return null;
  const pgid = platform === 'win32' ? metadata.pid : metadata.pgid;
  if (!Number.isSafeInteger(pgid) || pgid <= 0 || platform !== 'win32' && metadata.tpgid !== pgid) return null;
  const proof = { kind, pid: metadata.pid, startTicks: metadata.startTicks, pgid };
  if (session.directRootProof && !sameIdentity(session.directRootProof, proof)) return null;
  return proof;
}

function linuxRootMetadata(pid, program) {
  const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
  if (stat.length > 16384 || !stat.startsWith(`${pid} (`)) throw new Error('Invalid owned process identity');
  const fields = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
  return { pid, executable: fs.readlinkSync(`/proc/${pid}/exe`), expectedExecutable: fs.realpathSync(program),
    startTicks: Number(fields[19]), pgid: Number(fields[2]), tpgid: Number(fields[5]) };
}

function identityResult(command, proof) {
  return { command, state: 'agent', reason: 'verified_direct_agent_root', identity_verified: true,
    agent_kind: proof.kind, agent_pid: proof.pid, agent_start_ticks: proof.startTicks, foreground_pgid: proof.pgid };
}

function spawnSshPipe(file, args, options) {
  // With a second ConPTY around Windows OpenSSH, startup/cooked-mode DA replies
  // were observed as stray readline text during tmux reattach. A direct raw-TTY
  // test preserves these bytes, so this is not a general byte-corruption claim.
  // SSH -tt already allocates a genuine remote PTY; stdio pipes remove the extra
  // terminal layer and its startup-query traffic while preserving raw replies.
  const child = spawnChild(file, args, { cwd: options.cwd, env: options.env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  let dataListener, exitListener, exited = false, exitCode;
  const pending = [];
  const deliver = data => dataListener ? dataListener(data) : pending.push(data);
  for (const stream of [child.stdout, child.stderr]) {
    stream.setEncoding('utf8');
    stream.on('data', deliver);
  }
  child.stdin.on('error', () => {});
  const ready = new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
  child.on('error', error => { deliver(`\r\nSSH could not start: ${error.message}\r\n`); });
  child.once('close', code => { exited = true; exitCode = code == null ? 255 : code; if (exitListener) exitListener({ exitCode }); });
  return {
    pid: child.pid, process: 'ssh', ready,
    write(data) { if (exited || child.stdin.destroyed) throw new ApiError(409, 'terminal_exited', 'SSH transport disconnected'); child.stdin.write(data, 'utf8'); },
    async resize(cols, rows) {
      let result;
      try {
        // Resizing uses its own authenticated SSH connection, just like the
        // foreground probe. Do not time it out before a slow relay can even
        // finish authentication; a missing/mismatched ACK still fences input.
        result = await commandResult(file, ['-T', '-o', 'BatchMode=yes', '-o', `ConnectTimeout=${SSH_CONTROL_BUDGET.connectSeconds}`,
          '-o', 'ControlMaster=no', '-o', 'ControlPersist=no', '-o', 'ControlPath=none', options.target,
          remoteBridgeCommand('resize', { sessionId: options.sessionId, bridgeId: options.bridgeId, cols, rows })], SSH_CONTROL_BUDGET.commandMs);
      } catch (error) { result = { stdout: typeof error.stdout === 'string' ? error.stdout : '' }; }
      const marker = 'CEO_HUB_PTY_RESULT=';
      const line = String(result.stdout || '').split(/\r?\n/).reverse().find(line => line.startsWith(marker));
      let reply;
      try { reply = line ? JSON.parse(line.slice(marker.length)) : null; } catch {}
      if (!reply?.ok || reply.cols !== cols || reply.rows !== rows || reply.client_cols !== cols || reply.client_rows !== rows || reply.window_cols !== cols || reply.window_rows !== rows) {
        const error = new ApiError(409, 'remote_resize_failed', 'Remote display resize was not confirmed; retry the resize before entering input');
        error.geometryConfirmed = reply?.geometry_confirmed === true;
        error.actualCols = reply?.cols; error.actualRows = reply?.rows;
        throw error;
      }
      return reply;
    },
    kill() { if (!exited) child.kill(); },
    onData(callback) { dataListener = callback; for (const data of pending.splice(0)) callback(data); return { dispose() { dataListener = null; } }; },
    onExit(callback) { exitListener = callback; if (exited) callback({ exitCode }); return { dispose() { exitListener = null; } }; },
  };
}

class TerminalBroker {
  constructor(options = {}) {
    this.sessions = new Map();
    this.spawn = options.spawn || pty.spawn;
    this.spawnRemote = options.spawnRemote || options.spawn || spawnSshPipe;
    this.probe = options.probe || this.probeForeground.bind(this);
    this.platform = options.platform || process.platform;
    this.runCommand = options.runCommand || commandResult;
    this.resolveProgramPath = options.resolveProgramPath || fs.realpathSync;
    this.readRootMetadata = options.readRootMetadata || linuxRootMetadata;
    this.queryDisplayIdentity = options.queryDisplayIdentity || (pid => identityQuery(['observe', String(pid)]));
    this.maxOutput = options.maxOutput || MAX_OUTPUT;
    this.closeExitTimeout = options.closeExitTimeout ?? 5000;
    this.verifiedCloseAttempts = new Map();
  }

  describe(session) {
    const blocked = session.closeFenced ? 'terminal_close_unknown' : session.geometryUnknown ? 'geometry_unknown' : session.connected && session.state === 'agent' ? blockedScreen(session) : null;
    return {
      id: session.id, host: session.host, transport: session.transport,
      target: session.target, title: session.title, cwd: session.cwd,
      connected: session.connected, pid: session.pty?.pid || null,
      ...(session.transport === 'ssh' ? { display_identity: session.displayIdentityPid === session.pty?.pid
        && Number.isSafeInteger(session.displayIdentityPid) && session.displayIdentityPid > 0
        && validDisplayIdentity(session.displayIdentity, this.platform) ? { ...session.displayIdentity } : null } : {}),
      cols: session.cols, rows: session.rows, state: session.connected ? session.geometryUnknown ? 'unknown' : session.state : 'exited',
      command: session.command, current_seq: session.seq, created_at: session.createdAt,
      agent_ready: session.connected && session.state === 'agent' && !blocked,
      blocked_reason: blocked,
      human_busy: !!session.humanLineLength || Date.now() < session.humanLeaseUntil,
      human_input: humanInputState(session),
      geometry_unknown: !!session.geometryUnknown,
      ...(session.exitCode == null ? {} : { exit_code: session.exitCode }),
    };
  }

  async create(body) {
    const transport = body.transport || 'local';
    if (!['local', 'ssh'].includes(transport)) throw new ApiError(400, 'invalid_transport', 'Use local or ssh');
    const direct = Object.hasOwn(body, 'program') || Object.hasOwn(body, 'args');
    if (direct && (transport !== 'local' || typeof body.program !== 'string' || !path.isAbsolute(body.program)
        || !body.program || body.program.length > 4096 || /[\x00-\x1f\x7f-\x9f]/.test(body.program)
        || !Array.isArray(body.args) || body.args.length > 200
        || body.args.some(arg => typeof arg !== 'string' || arg.includes('\0') || arg.length > 32768)
        || body.args.reduce((total, arg) => total + Buffer.byteLength(arg), 0) > 128 * 1024)) {
      throw new ApiError(400, 'invalid_program', 'A direct local program requires an absolute executable and bounded string arguments');
    }
    const id = body.session_id || `ceohub-${crypto.randomBytes(12).toString('hex')}`;
    if (!SESSION_ID.test(id)) throw new ApiError(400, 'invalid_session_id', 'Invalid owned terminal session ID');
    const target = transport === 'ssh' ? String(body.target || '') : '';
    if (transport === 'ssh' && (!target || target.startsWith('-') || /[\s\x00-\x1f]/.test(target))) {
      throw new ApiError(400, 'invalid_target', 'Use an SSH host alias or user@host');
    }
    const host = String(body.host || (transport === 'local' ? 'local' : target));
    const cwd = String(body.cwd || (transport === 'local' ? os.homedir() : '~'));
    if (cwd.includes('\0')) throw new ApiError(400, 'invalid_directory', 'Invalid terminal directory');
    let session = this.sessions.get(id);
    if (this.verifiedCloseAttempts.has(id)) throw new ApiError(409, 'terminal_closed', 'Original terminal is fenced by an explicit replacement operation');
    if (session && (session.host !== host || session.target !== target || session.transport !== transport
        || session.program !== (direct ? body.program : null)
        || JSON.stringify(session.programArgs) !== JSON.stringify(direct ? body.args : []))) {
      throw new ApiError(409, 'session_conflict', 'Session belongs to another machine');
    }
    if (session?.connected) return this.describe(session);
    if (session?.attaching) throw new ApiError(409, 'session_attaching', 'The original terminal attachment is still starting');
    if (session && transport === 'local') throw new ApiError(409, 'session_exited', 'Local terminal has exited; create a new terminal');
    const cols = boundedInteger(body.cols, session?.cols || 100, 20, 500);
    const rows = boundedInteger(body.rows, session?.rows || 30, 5, 200);
    if (!session) {
      const terminal = new Terminal({ cols, rows, scrollback: 5000, allowProposedApi: true });
      const serializer = new SerializeAddon();
      terminal.loadAddon(serializer);
      session = {
        id, host, transport, target, cwd, title: String(body.title || 'Terminal').slice(0, 200),
        program: direct ? body.program : null, programArgs: direct ? [...body.args] : [],
        cols, rows, terminal, serializer, connected: false, state: 'unknown', command: '',
        seq: 0, ring: [], ringSize: 0, rendered: Promise.resolve(), writeQueue: Promise.resolve(),
        humanLeaseUntil: 0, humanLineLength: 0, humanPaste: false, inputEscape: '', requests: new Map(), lastProbe: 0,
        createdAt: new Date().toISOString(),
      };
      // Codex and tmux query cursor position/device attributes during startup.
      // The broker answers even when no browser is open. These are terminal
      // protocol replies, not human keystrokes and must not take a typing lease.
      terminal.onData(data => {
        const child = session.pty, generation = session.attachmentGeneration;
        if (session.connected) this.serializeWrite(session, () => {
          if (session.connected && session.pty === child && session.attachmentGeneration === generation) child.write(data);
        }).catch(() => {});
      });
      this.sessions.set(id, session);
    }
    // A registry row may outlive its SSH attachment. Invalidate all observations
    // and callbacks from the old child before waiting for a replacement child.
    const attachmentGeneration = session.attachmentGeneration = (session.attachmentGeneration || 0) + 1;
    session.cwd = cwd;
    session.state = 'unknown'; session.command = ''; session.agentIdentity = null;
    session.lastProbe = 0; session.probing = null; session.directRootProof = null;
    session.displayIdentity = null; session.displayIdentityPid = null;
    let file, args, localCwd, bridgeId;
    if (transport === 'local') {
      if (!fs.statSync(cwd, { throwIfNoEntry: false })?.isDirectory()) {
        throw new ApiError(400, 'invalid_directory', 'Local directory does not exist');
      }
      file = direct ? body.program : process.env.CEO_HUB_SHELL || (process.platform === 'win32' ? 'powershell.exe' : process.env.SHELL || '/bin/bash');
      args = direct ? [...body.args] : process.platform === 'win32' && /(?:powershell|pwsh)(?:\.exe)?$/i.test(file) ? ['-NoLogo'] : ['-l'];
      localCwd = cwd;
      session.state = direct ? classifyCommand(file) : 'shell';
      session.command = path.basename(file);
    } else {
      file = sshExecutable();
      // tmux starts the user's normal login shell and keeps it alive when the
      // SSH transport disconnects. No model config, credentials or task text is
      // rewritten, and the tmux namespace is restricted to our generated IDs.
      bridgeId = crypto.randomBytes(12).toString('hex');
      const remote = remoteBridgeCommand('attach', { sessionId: id, bridgeId, cols, rows, cwd: cwd === '~' ? '' : cwd });
      args = ['-tt', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', target, remote];
      localCwd = os.homedir();
    }
    session.attaching = true;
    try {
      // Drain already accepted frames while the previous child is disconnected.
      // Late DA/DSR parsing must never reply into the replacement attachment.
      await this.flushScreen(session);
      const spawn = transport === 'ssh' ? this.spawnRemote : this.spawn;
      const child = spawn(file, args, {
        name: 'xterm-256color', cols, rows, cwd: localCwd, env: { ...process.env, TERM: 'xterm-256color' },
        useConpty: true, ...(process.platform === 'win32' ? { useConptyDll: true } : { encoding: 'utf8' }),
        target, sessionId: id, bridgeId,
      });
      if (child.ready) await child.ready;
      if (transport === 'ssh' && Number.isSafeInteger(child.pid) && child.pid > 0 && child.pid <= 2147483647) {
        try {
          const observed = this.queryDisplayIdentity(child.pid);
          const identity = { birth: observed?.birth, executable: observed?.executable };
          if (observed?.state === 'alive' && validDisplayIdentity(identity, this.platform)) {
            session.displayIdentity = Object.freeze(identity);
            session.displayIdentityPid = child.pid;
          }
        } catch { /* The display remains usable without a strict birth proof. */ }
      }
      session.pty = child;
      let resolveExit;
      const exitObservation = { child, observed: false, promise: new Promise(resolve => { resolveExit = resolve; }) };
      session.exitObservation = exitObservation;
      session.connected = true;
      session.geometryUnknown = false;
      session.exitCode = null;
      session.cols = cols; session.rows = rows;
      const isCurrentChild = () => session.attachmentGeneration === attachmentGeneration && session.pty === child;
      child.onData(data => { if (isCurrentChild() && session.connected) this.acceptOutput(session, data); });
      child.onExit(({ exitCode }) => {
        exitObservation.observed = true;
        resolveExit();
        if (isCurrentChild()) { session.connected = false; session.exitCode = exitCode; session.state = 'exited'; }
      });
    } catch (error) {
      session.connected = false;
      throw new ApiError(500, 'spawn_failed', `Could not start ${transport} terminal: ${error.message}`);
    } finally { session.attaching = false; }
    return this.describe(session);
  }

  acceptOutput(session, data) {
    const seq = ++session.seq;
    session.ring.push({ seq, data });
    session.ringSize += Buffer.byteLength(data);
    while (session.ringSize > this.maxOutput && session.ring.length > 1) {
      session.ringSize -= Buffer.byteLength(session.ring.shift().data);
    }
    session.rendered = session.rendered.then(() => new Promise(resolve => session.terminal.write(data, resolve)));
  }

  get(id) {
    const session = this.sessions.get(id);
    if (!session) throw new ApiError(404, 'terminal_not_found', 'Terminal not found');
    return session;
  }

  async flushScreen(session) {
    // Output may arrive while xterm parses the pending frame. Snapshot sequence
    // must correspond to the rendered screen, never to an unrendered byte tail.
    let pending;
    do { pending = session.rendered; await pending; } while (pending !== session.rendered);
  }

  async snapshot(id, lines = 120) {
    const session = this.get(id);
    lines = boundedInteger(lines, 120, 1, 5000);
    await this.refreshForeground(session);
    await this.flushScreen(session);
    const buffer = session.terminal.buffer.active;
    const screen = [];
    for (let index = Math.max(0, buffer.length - lines); index < buffer.length; index++) {
      screen.push(buffer.getLine(index)?.translateToString(true) || '');
    }
    return { ...this.describe(session), screen: screen.join('\n').trimEnd(), ansi: session.serializer.serialize({ scrollback: lines }) };
  }

  async output(id, after = 0) {
    const session = this.get(id);
    after = boundedInteger(after, 0, 0, Number.MAX_SAFE_INTEGER);
    // Only a terminal being polled is sampled. The cached/coalesced probe keeps
    // frontend delta polling from making a new SSH/process query every frame.
    await this.refreshForeground(session);
    const metadata = () => {
      const view = this.describe(session);
      return {
        connected: view.connected, state: view.state, status: view.state,
        command: view.command, agent_ready: view.agent_ready,
        blocked_reason: view.blocked_reason, human_busy: view.human_busy, human_input: view.human_input, geometry_unknown: view.geometry_unknown, cols: view.cols, rows: view.rows,
        ...(view.exit_code == null ? {} : { exit_code: view.exit_code }),
      };
    };
    const earliest = session.ring[0]?.seq || session.seq + 1;
    if (after < earliest - 1 || after > session.seq) {
      await this.flushScreen(session);
      return { ...metadata(), data: '', seq: session.seq, reset: true, ansi: session.serializer.serialize({ scrollback: 5000 }) };
    }
    return { ...metadata(), data: session.ring.filter(chunk => chunk.seq > after).map(chunk => chunk.data).join(''), seq: session.seq, reset: false };
  }

  serializeWrite(session, action) {
    const result = session.writeQueue.then(action);
    session.writeQueue = result.catch(() => {});
    return result;
  }

  async input(id, body) {
    const session = this.get(id);
    if (session.closeFenced) throw new ApiError(409, 'terminal_closing', 'Terminal is fenced for replacement');
    if (typeof body.data !== 'string' || Buffer.byteLength(body.data) > 256 * 1024) throw new ApiError(400, 'invalid_input', 'Expected terminal data up to 256 KiB');
    const origin = body.origin || 'ui';
    if (!['ui', 'plugin'].includes(origin)) throw new ApiError(400, 'invalid_origin', 'Invalid input origin');
    if (origin === 'plugin' && (typeof body.request_key !== 'string' || !body.request_key || body.request_key.length > 200)) {
      throw new ApiError(400, 'missing_request_key', 'Plugin input requires a request_key for safe delivery');
    }
    const digest = crypto.createHash('sha256').update(`input:${body.data}`).digest('hex');
    const existing = body.request_key && (session.requests.get(body.request_key) || session.pendingHumanInputs?.get(body.request_key));
    if (existing) {
      if (existing.digest !== digest) throw new ApiError(409, 'request_conflict', 'request_key was already used with different input');
      return existing.promise ? { ...await existing.promise, deduplicated: true } : { ...existing.result, deduplicated: true };
    }
    if (!session.connected) throw new ApiError(409, 'terminal_exited', 'Terminal is disconnected');
    if (session.geometryUnknown) throw new ApiError(409, 'geometry_unknown', 'Display geometry is not synchronized; retry resizing before entering input');
    const settleHuman = origin === 'ui' ? reserveHumanInput(session, body.data) : null;
    const result = this.serializeWrite(session, () => {
      let attemptedWrite = false;
      try {
      if (body.request_key) {
        const prior = session.requests.get(body.request_key);
        if (prior) {
          if (prior.digest !== digest) throw new ApiError(409, 'request_conflict', 'request_key was already used with different input');
          if (settleHuman) settleHuman(false);
          return { ...prior.result, deduplicated: true };
        }
      }
      if (!session.connected) throw new ApiError(409, 'terminal_exited', 'Terminal is disconnected');
      if (session.geometryUnknown) throw new ApiError(409, 'geometry_unknown', 'Display geometry is not synchronized; retry resizing before entering input');
      if (origin === 'plugin' && (session.humanLineLength || Date.now() < session.humanLeaseUntil)) {
        throw new ApiError(409, 'human_busy', 'Terminal has pending human input; retry after the user submits or cancels it');
      }
      attemptedWrite = true;
      session.pty.write(body.data);
      if (settleHuman) settleHuman(true);
      const result = { id, accepted: true, state: 'submitted', origin, ...(body.request_key ? { request_key: body.request_key } : {}) };
      if (body.request_key) {
        session.requests.set(body.request_key, { digest, result });
        if (session.requests.size > 500) session.requests.delete(session.requests.keys().next().value);
      }
      return result;
      } catch (error) {
        if (settleHuman) settleHuman(false, attemptedWrite);
        throw error;
      }
    });
    if (origin === 'ui') {
      if (!session.pendingHumanInputs) session.pendingHumanInputs = new Map();
      if (body.request_key) session.pendingHumanInputs.set(body.request_key, { digest, promise: result });
      result.then(() => { if (body.request_key) session.pendingHumanInputs.delete(body.request_key); }, () => {
        if (body.request_key) session.pendingHumanInputs.delete(body.request_key);
      });
    }
    return result;
  }

  async prompt(id, body) {
    const session = this.get(id);
    if (session.closeFenced) throw new ApiError(409, 'terminal_closing', 'Terminal is fenced for replacement');
    if (typeof body.text !== 'string' || !body.text.trim() || Buffer.byteLength(body.text) > 256 * 1024 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/.test(body.text)) {
      throw new ApiError(400, 'invalid_prompt', 'Expected nonempty task text, without terminal escape sequences, up to 256 KiB');
    }
    if (typeof body.request_key !== 'string' || !body.request_key || body.request_key.length > 200) throw new ApiError(400, 'missing_request_key', 'A request_key is required for safe delivery');
    const origin = body.origin || 'plugin';
    if (!['ui', 'plugin'].includes(origin)) throw new ApiError(400, 'invalid_origin', 'Invalid prompt origin');
    return this.serializeWrite(session, async () => {
      const digest = crypto.createHash('sha256').update(`prompt:${body.text}`).digest('hex');
      const prior = session.requests.get(body.request_key);
      if (prior) {
        if (prior.digest !== digest) throw new ApiError(409, 'request_conflict', 'request_key was already used with different text');
        return { ...prior.result, deduplicated: true };
      }
      if (!session.connected) throw new ApiError(409, 'terminal_exited', 'Terminal is disconnected');
      if (session.geometryUnknown) throw new ApiError(409, 'geometry_unknown', 'Display geometry is not synchronized; retry resizing before sending a task');
      if (session.humanLineLength || Date.now() < session.humanLeaseUntil) {
        throw new ApiError(409, 'human_busy', 'Terminal has pending human input; retry after the user submits or cancels it');
      }
      await this.refreshForeground(session, true);
      if (session.state !== 'agent') throw new ApiError(409, session.state === 'shell' ? 'agent_not_started' : 'agent_not_confirmed', 'Start Codex or another coding agent in this terminal first');
      const expectedIdentity = session.agentIdentity;
      if (session.transport === 'ssh' && !expectedIdentity) throw new ApiError(409, 'agent_not_confirmed', 'Foreground agent identity was not confirmed; task was not sent');
      await this.flushScreen(session);
      const blocked = blockedScreen(session);
      if (blocked) throw new ApiError(409, 'waiting_for_agent', `The terminal is showing an ${blocked} prompt; the user must handle it in the terminal first`);
      if (session.humanLineLength || Date.now() < session.humanLeaseUntil) throw new ApiError(409, 'human_busy', 'User started typing; task was not sent');
      if (expectedIdentity) {
        // A launcher can exit, replace its child, or return to a shell while
        // screen parsing/readiness checks are running. Reconfirm the exact
        // native identity close to the write, rather than trusting an old name.
        await this.refreshForeground(session, true);
        if (session.state !== 'agent' || !sameIdentity(expectedIdentity, session.agentIdentity)) {
          throw new ApiError(409, 'agent_not_confirmed', 'Foreground agent identity changed; task was not sent');
        }
        await this.flushScreen(session);
        if (!session.connected) throw new ApiError(409, 'terminal_exited', 'Terminal disconnected; task was not sent');
        if (session.geometryUnknown) throw new ApiError(409, 'geometry_unknown', 'Display geometry is not synchronized; task was not sent');
        const latestBlocked = blockedScreen(session);
        if (latestBlocked) throw new ApiError(409, 'waiting_for_agent', `The terminal is showing an ${latestBlocked} prompt; task was not sent`);
        if (session.humanLineLength || Date.now() < session.humanLeaseUntil) throw new ApiError(409, 'human_busy', 'User started typing; task was not sent');
      }
      if (session.state !== 'agent' || expectedIdentity && !sameIdentity(expectedIdentity, session.agentIdentity)) {
        throw new ApiError(409, 'agent_not_confirmed', 'Foreground agent identity changed; task was not sent');
      }
      // One PTY write prevents user input interleaving between multiline paste
      // and Enter. Codex's bracketed paste handles multiline task text safely.
      const normalized = body.text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      session.pty.write(`\x1b[200~${normalized}\x1b[201~\r`);
      const result = { id, accepted: true, state: 'submitted', request_key: body.request_key, origin, delivered_at: new Date().toISOString() };
      session.requests.set(body.request_key, { digest, result });
      if (session.requests.size > 500) session.requests.delete(session.requests.keys().next().value);
      return result;
    });
  }

  async refreshForeground(session, force = false) {
    if (!session.connected || (!force && Date.now() - session.lastProbe < 1500)) return;
    const child = session.pty, generation = session.attachmentGeneration;
    const isCurrentChild = () => session.connected && session.pty === child && session.attachmentGeneration === generation;
    if (session.probing) { await session.probing; if (!force || !isCurrentChild()) return; }
    const probing = (async () => {
      try {
        const result = await this.probe(session);
        if (isCurrentChild()) {
          session.command = result.command || ''; session.state = result.state || classifyCommand(session.command);
          session.agentIdentity = verifiedIdentity(result);
        }
      } catch { if (isCurrentChild()) { session.state = 'unknown'; session.agentIdentity = null; } }
      finally { if (isCurrentChild()) session.lastProbe = Date.now(); }
    })();
    session.probing = probing;
    try { await probing; } finally { if (session.probing === probing) session.probing = null; }
  }

  async probeForeground(session) {
    if (session.transport === 'ssh') {
      // Relayed SSH authentication can exceed a minute even when the remote probe
      // itself is immediate. Bound the complete independent query generously;
      // its failure still clears identity rather than reusing an old proof.
      const result = await commandResult(sshExecutable(), ['-T', '-o', 'BatchMode=yes', '-o', `ConnectTimeout=${SSH_CONTROL_BUDGET.connectSeconds}`,
        '-o', 'ControlMaster=no', '-o', 'ControlPersist=no', '-o', 'ControlPath=none', session.target,
        remoteForegroundCommand(session.id)], SSH_CONTROL_BUDGET.commandMs);
      const marker = 'CEO_HUB_FOREGROUND_RESULT=';
      const line = String(result.stdout || '').split(/\r?\n/).reverse().find(line => line.startsWith(marker));
      let identity;
      try { identity = line ? JSON.parse(line.slice(marker.length)) : null; } catch {}
      const reasons = ['agent_identity_unresolved', 'foreground_shell', 'process_changed', 'verified_foreground_codex', 'verified_foreground_agent'];
      if (!identity || !['shell', 'agent', 'unknown'].includes(identity.state) || typeof identity.command !== 'string'
          || !/^[A-Za-z0-9_.+-]{0,64}$/.test(identity.command) || !reasons.includes(identity.reason)
          || (identity.state === 'agent' && !verifiedIdentity(identity))) {
        return { command: '', state: 'unknown' };
      }
      // Only whitelisted identity metadata crosses the API. No executable paths,
      // command-line arguments, environment, credentials or startup output.
      return { command: identity.command, state: identity.state, reason: identity.reason,
        ...(identity.state === 'agent' ? { identity_verified: true, agent_kind: identity.agent_kind,
          agent_pid: identity.agent_pid, agent_start_ticks: identity.agent_start_ticks, foreground_pgid: identity.foreground_pgid } : {}) };
    }
    if (this.platform === 'win32') {
      const rootKind = nativeRootKind(session.program, 'win32');
      let rootProof = null;
      if (rootKind) {
        const pid = session.pty.pid;
        if (!Number.isSafeInteger(pid) || pid <= 0) return { command: '', state: 'unknown' };
        // This is a bounded exact-PID query. Get-Process reads local process
        // metadata directly instead of paying CIM provider startup latency.
        // Never return command lines, environment, or unrelated process paths.
        const script = `$r=Get-Process -Id ${pid} -ErrorAction Stop; [pscustomobject]@{pid=[int]$r.Id;name=$r.ProcessName+'.exe';executable=$r.Path;startTicks=[long](($r.StartTime.ToUniversalTime()-([datetime]'1970-01-01T00:00:00Z').ToUniversalTime()).Ticks/10)} | ConvertTo-Json -Compress`;
        try {
          const result = await this.runCommand('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], 8000);
          const metadata = result.stdout.trim() ? JSON.parse(result.stdout) : null;
          rootProof = directRootIdentity(session, { ...metadata, expectedExecutable: this.resolveProgramPath(session.program) }, 'win32');
        } catch {}
        if (!rootProof) return { command: path.win32.basename(session.program), state: 'unknown' };
        session.directRootProof ||= rootProof;
      }
      // Only return names, never command lines (which may contain credentials).
      let names = [];
      if (!rootProof) {
        const script = `$all=Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name; $ids=@(${Number(session.pty.pid)}); $names=@(); for($i=0;$i -lt 12;$i++){ $children=@($all | Where-Object {$ids -contains $_.ParentProcessId}); if($children.Count -eq 0){break}; $names+=@($children | ForEach-Object {$_.Name}); $ids+=@($children | ForEach-Object {$_.ProcessId}) }; $names | ConvertTo-Json -Compress`;
        const result = await this.runCommand('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], 8000);
        const parsed = result.stdout.trim() ? JSON.parse(result.stdout) : [];
        names = Array.isArray(parsed) ? parsed : [parsed];
      }
      const agent = rootProof ? path.win32.basename(session.program) : names.find(name => classifyCommand(name) === 'agent');
      if (agent) {
        await this.flushScreen(session);
        const buffer = session.terminal.buffer.active;
        const cursor = buffer.getLine(buffer.baseY + buffer.cursorY)?.translateToString(true) || '';
        const visible = Array.from({ length: session.rows }, (_, i) => buffer.getLine(buffer.baseY + i)?.translateToString(true) || '').join('\n');
        // Windows has no POSIX foreground process group. A background Codex
        // descendant is not sufficient: require its visible input UI, and refuse
        // when the actual cursor has returned to a shell prompt.
        if (/^\s*(?:PS\s+.+>|[A-Za-z]:\\[^>]*>|[$#])\s*/.test(cursor)) return { command: 'powershell.exe', state: 'shell' };
        const inputUi = /^\s*[›❯»]\s/m.test(visible) || (session.terminal.buffer.active.type === 'alternate' && /codex|claude|aider|opencode|gemini/i.test(visible));
        if (!(inputUi || blockedScreen(session))) return { command: agent, state: 'unknown' };
        return rootProof ? identityResult(agent, rootProof) : { command: agent, state: 'agent' };
      }
      if (names.some(name => !['conhost.exe', 'openconsole.exe'].includes(String(name).toLowerCase()))) return { command: names[names.length - 1], state: 'unknown' };
      return { command: session.pty.process || 'powershell.exe', state: 'shell' };
    }
    const result = await this.runCommand('ps', ['-eo', 'pid=,ppid=,pgid=,tpgid=,comm=']);
    const rows = result.stdout.split('\n').map(line => /^\s*(\d+)\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)$/.exec(line)).filter(Boolean);
    const ids = new Set([session.pty.pid]);
    const names = [];
    const rootRow = rows.find(row => Number(row[1]) === session.pty.pid);
    const foreground = Number(rootRow?.[4]);
    if (nativeRootKind(session.program, this.platform)) {
      if (this.platform !== 'linux' || !rootRow || Number(rootRow[3]) !== foreground || foreground <= 0) return { command: '', state: 'unknown' };
      let rootProof;
      try {
        rootProof = directRootIdentity(session, this.readRootMetadata(session.pty.pid, session.program), this.platform);
      } catch {}
      if (!rootProof || rootProof.pgid !== foreground) return { command: '', state: 'unknown' };
      session.directRootProof ||= rootProof;
      return identityResult(path.posix.basename(session.program), rootProof);
    }
    for (let i = 0; i < 12; i++) for (const row of rows) if (ids.has(Number(row[2])) && !ids.has(Number(row[1]))) {
      ids.add(Number(row[1]));
      if (Number(row[3]) === foreground) names.push(row[5]);
    }
    const agent = names.find(name => classifyCommand(name) === 'agent');
    return { command: agent || names.at(-1) || session.pty.process, state: agent ? 'agent' : classifyCommand(names.at(-1) || session.pty.process) };
  }

  async resize(id, body) {
    const session = this.get(id);
    if (session.closeFenced) throw new ApiError(409, 'terminal_closing', 'Terminal is fenced for replacement');
    const cols = boundedInteger(body.cols, session.cols, 20, 500);
    const rows = boundedInteger(body.rows, session.rows, 5, 200);
    return this.serializeWrite(session, async () => {
      if (session.closeFenced) throw new ApiError(409, 'terminal_closing', 'Terminal is fenced for replacement');
      if (!session.connected) throw new ApiError(409, 'terminal_exited', 'Terminal is disconnected');
      await this.flushScreen(session);
      const previous = { cols: session.cols, rows: session.rows };
      // A remote resize emits its newly wrapped/reflowed frame while SSH is
      // still in flight. Parse that frame at the new geometry, not the old one.
      session.terminal.resize(cols, rows);
      session.cols = cols; session.rows = rows;
      try { await session.pty.resize(cols, rows); }
      catch (error) {
        await this.flushScreen(session);
        if (session.transport === 'ssh') {
          // An operation can fail after changing the original PTY. Only restore
          // a confirmed geometry; never pretend that an uncertain old size won.
          if (error.geometryConfirmed && Number.isInteger(error.actualCols) && Number.isInteger(error.actualRows)
              && error.actualCols >= 20 && error.actualCols <= 500 && error.actualRows >= 5 && error.actualRows <= 200) {
            session.terminal.resize(error.actualCols, error.actualRows);
            session.cols = error.actualCols; session.rows = error.actualRows;
          }
          // Even confirmed rollback may have delivered frames at two sizes.
          // A subsequent successful resize/full redraw is needed before input.
          session.geometryUnknown = true;
        } else {
          session.terminal.resize(previous.cols, previous.rows);
          session.cols = previous.cols; session.rows = previous.rows;
        }
        throw error;
      }
      session.geometryUnknown = false;
      return this.describe(session);
    });
  }

  async close(id, body = {}) {
    if (body.require_verified === true) return this.verifiedClose(id, body);
    const session = this.get(id);
    return this.serializeWrite(session, async () => {
      if (session.closeFenced) throw new ApiError(409, 'terminal_close_unknown', 'Original replacement close remains fenced');
      if (body.require_idle === true) {
        if (session.transport !== 'local') throw new ApiError(400, 'invalid_close', 'Idle handoff is only supported for local management terminals');
        await this.refreshForeground(session, true);
        await this.flushScreen(session);
        const view = this.describe(session);
        if (view.human_busy) throw new ApiError(409, 'human_busy', 'Human input changed before handoff; terminal was not closed');
        if (!view.connected || !view.agent_ready) throw new ApiError(409, 'waiting_for_agent', 'Management terminal is not ready for handoff; terminal was not closed');
      }
      if (session.transport === 'ssh') {
        await commandResult(sshExecutable(), ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', session.target,
          `tmux kill-session -t ${shellQuote(session.id)} 2>/dev/null || ! tmux has-session -t ${shellQuote(session.id)} 2>/dev/null`]);
      }
      if (session.connected) session.pty.kill();
      session.connected = false; session.state = 'exited';
      return { id, closed: true };
    });
  }

  async verifiedClose(id, body) {
    if (!SESSION_ID.test(id) || !['local', 'ssh'].includes(body.expected_transport)
        || typeof body.expected_host !== 'string' || !body.expected_host || body.expected_host.length > 200
        || typeof body.expected_target !== 'string' || body.expected_target.length > 4096
        || (body.expected_transport === 'local' ? body.expected_target !== ''
          : !body.expected_target || body.expected_target.startsWith('-') || /[\s\x00-\x1f]/.test(body.expected_target))) {
      throw new ApiError(400, 'invalid_close_identity', 'Exact registered terminal route is required');
    }
    // Validate before publishing the fence. Never accept an arbitrary remote
    // command or a missing identity as permission to kill a matching name.
    const remote = body.expected_transport === 'ssh' ? remoteCloseCommand(id, body.expected_remote) : null;
    const digest = crypto.createHash('sha256').update(JSON.stringify([body.expected_host, body.expected_transport,
      body.expected_target, body.expected_remote ?? null])).digest('hex');
    const previous = this.verifiedCloseAttempts.get(id);
    if (previous) {
      if (previous.digest !== digest) throw new ApiError(409, 'terminal_close_unknown', 'Original terminal close is already fenced with another identity');
      return previous.promise;
    }
    const session = this.sessions.get(id);
    if (!session && body.expected_transport === 'local') throw new ApiError(409, 'terminal_close_unknown', 'Original local terminal exit cannot be established');
    if (session && (session.host !== body.expected_host || session.transport !== body.expected_transport || session.target !== body.expected_target)) {
      throw new ApiError(409, 'session_conflict', 'Terminal close route changed');
    }
    if (session?.attaching) throw new ApiError(409, 'session_attaching', 'Original terminal attachment is still starting');
    if (session) session.closeFenced = true;
    const execute = async () => {
      if (remote) {
        let response;
        try {
          const result = await this.runCommand(sshExecutable(), ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
            '-o', 'ControlMaster=no', '-o', 'ControlPersist=no', '-o', 'ControlPath=none', body.expected_target, remote], 45000);
          const prefix = 'CEO_HUB_TERMINAL_CLOSE_RESULT=';
          const lines = String(result.stdout || '').split(/\r?\n/).filter(line => line.startsWith(prefix));
          response = lines.length === 1 ? JSON.parse(lines[0].slice(prefix.length)) : null;
        } catch { throw new ApiError(409, 'terminal_close_unknown', 'Remote terminal close was not confirmed'); }
        if (!response || Object.keys(response).sort().join(',') !== 'closed_verified,ok,terminal_id'
            || response.ok !== true || response.closed_verified !== true || response.terminal_id !== id) {
          throw new ApiError(409, 'terminal_close_unknown', 'Remote terminal close was not confirmed');
        }
      }
      if (session) {
        const observation = session.exitObservation;
        if (!observation || observation.child !== session.pty) throw new ApiError(409, 'terminal_close_unknown', 'Original display exit cannot be established');
        if (!observation.observed) {
          try { session.pty.kill(); }
          catch { throw new ApiError(409, 'terminal_close_unknown', 'Original display exit was not confirmed'); }
          let timer;
          try {
            await Promise.race([observation.promise, new Promise((resolve, reject) => {
              timer = setTimeout(() => reject(new ApiError(409, 'terminal_close_unknown', 'Original display exit was not confirmed')), this.closeExitTimeout);
            })]);
          } finally { clearTimeout(timer); }
        }
        if (!observation.observed || this.sessions.get(id) !== session || session.pty !== observation.child) {
          throw new ApiError(409, 'terminal_close_unknown', 'Original display identity changed during close');
        }
        session.connected = false; session.state = 'exited';
        this.sessions.delete(id);
        session.terminal.dispose();
      }
      return { id, closed: true, closed_verified: true, record_removed: true,
        ...(remote ? { remote_closed_verified: true } : {}),
        local_display_exit_verified: !!session, missing_record: !session };
    };
    const promise = session ? this.serializeWrite(session, execute) : Promise.resolve().then(execute);
    this.verifiedCloseAttempts.set(id, { digest, promise });
    return promise;
  }
}

async function readBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new ApiError(413, 'body_too_large', 'Request body too large');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'); }
  catch { throw new ApiError(400, 'invalid_json', 'Expected a JSON object'); }
}

function createServer(broker, token, recoveryId = null) {
  const ownerMatches = owner => owner && Object.keys(owner).sort().join(',') === 'pid,recovery_id'
    && owner.pid === process.pid && owner.recovery_id === recoveryId;
  return http.createServer(async (request, response) => {
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    try {
      // This endpoint is backend-to-backend only. Browser requests must pass
      // through the Hub's existing origin/CSRF boundary, never this bearer token.
      if (request.headers.origin) throw new ApiError(403, 'browser_origin_denied', 'Use the Hub terminal API');
      const auth = String(request.headers.authorization || '');
      const expected = `Bearer ${token}`;
      if (auth.length !== expected.length || !crypto.timingSafeEqual(Buffer.from(auth), Buffer.from(expected))) throw new ApiError(401, 'unauthorized', 'Authentication required');
      const url = new URL(request.url, 'http://127.0.0.1');
      let result;
      if (request.method === 'GET' && url.pathname === '/health') result = { ok: true, version: VERSION, pid: process.pid, recovery_id: recoveryId, features: ['direct_program', 'input_busy', 'idle_close', 'verified_close'] };
      else if (request.method === 'GET' && url.pathname === '/sessions') result = { sessions: [...broker.sessions.values()].map(session => broker.describe(session)) };
      else if (request.method === 'POST' && url.pathname === '/sessions/create') {
        const body = await readBody(request);
        if ((Object.hasOwn(body, 'expected_owner') || body.require_verified === true) && !ownerMatches(body.expected_owner)) {
          throw new ApiError(409, 'terminal_owner_changed', 'Original terminal service generation changed');
        }
        result = await broker.create(body);
      }
      else {
        const match = /^\/sessions\/(ceohub-[a-f0-9]{16,48})\/(snapshot|output|input|prompt|resize|close)$/.exec(url.pathname);
        if (!match) throw new ApiError(404, 'not_found', 'Endpoint not found');
        const [, id, action] = match;
        if (request.method === 'GET' && action === 'snapshot') result = await broker.snapshot(id, url.searchParams.get('lines') || 120);
        else if (request.method === 'GET' && action === 'output') result = await broker.output(id, url.searchParams.get('after') || 0);
        else if (request.method === 'POST' && ['input', 'prompt', 'resize', 'close'].includes(action)) {
          const body = await readBody(request);
          if (action === 'close' && body.require_verified === true
              && !ownerMatches(body.expected_owner)) {
            throw new ApiError(409, 'terminal_owner_changed', 'Original terminal service generation changed');
          }
          result = await broker[action](id, body);
        } else throw new ApiError(405, 'method_not_allowed', 'Invalid request method');
      }
      response.end(JSON.stringify(result));
    } catch (error) {
      response.statusCode = error.status || 500;
      response.end(JSON.stringify({ error: error.code || 'terminal_error', message: error.status ? error.message : 'Terminal request failed' }));
    }
  });
}

function recoverySnapshot(filename, maximum = 65536) {
  if (fs.realpathSync(filename) !== path.resolve(filename) || fs.lstatSync(filename).isSymbolicLink()) throw new Error('Recovery file is linked');
  const before = fs.statSync(filename, { bigint: true });
  if (!before.isFile() || before.size > BigInt(maximum)) throw new Error('Recovery file is invalid');
  const raw = fs.readFileSync(filename);
  const after = fs.statSync(filename, { bigint: true });
  if (raw.length > maximum || before.ino !== after.ino || before.dev !== after.dev || before.mtimeNs !== after.mtimeNs || before.ctimeNs !== after.ctimeNs || before.size !== after.size) throw new Error('Recovery file changed');
  return { raw, hash: crypto.createHash('sha256').update(raw).digest('hex'),
    identity: { ino: String(after.ino), size: String(after.size), mtime_ns: String(after.mtimeNs) } };
}

function recoveryPidState(pid) {
  try { process.kill(pid, 0); return 'alive'; }
  catch (error) { return error.code === 'ESRCH' ? 'dead' : 'unknown'; }
}

function identityQuery(args) {
  try {
    const python = process.env.CEO_HUB_IDENTITY_PYTHON || process.env.CEO_HUB_PYTHON || 'python';
    const result = execFileSync(python, ['-I', path.join(PYTHON_SOURCE, 'infrastructure', 'process_identity.py'), ...args],
      { encoding: 'utf8', windowsHide: true, timeout: 5000, maxBuffer: 32768 });
    return JSON.parse(result);
  } catch { return { state: 'unknown' }; }
}

function validOwnerIdentity(identity) {
  return identity && Object.keys(identity).sort().join(',') === 'birth,executable,script'
    && typeof identity.birth === 'string' && /^(win-filetime:[0-9]{1,24}|linux-start:[a-f0-9-]{36}:[0-9]{1,24})$/.test(identity.birth)
    && ['executable', 'script'].every(key => typeof identity[key] === 'string' && identity[key].length > 0
      && identity[key].length <= 4096 && !/[\x00-\x1f]/.test(identity[key]) && path.isAbsolute(identity[key]))
    && ['node', 'node.exe'].includes(path.basename(identity.executable).toLowerCase())
    && brokerScript(identity.script);
}

function ownBrokerIdentity() {
  const current = identityQuery(['observe', String(process.pid)]);
  const identity = { birth: current.birth, executable: current.executable, script: ENTRY_SCRIPT };
  if (current.state !== 'alive' || !validOwnerIdentity(identity)) throw new Error('Cannot establish terminal broker process birth identity');
  return identity;
}

function recoveryOwnerState(owner, { pidState = recoveryPidState, query = identityQuery } = {}) {
  if (!owner || !Number.isSafeInteger(owner.pid) || owner.pid <= 0 || owner.pid > 2147483647
      || (owner.owner_identity !== undefined && !validOwnerIdentity(owner.owner_identity))) return 'unknown';
  const state = pidState(owner.pid);
  if (state !== 'alive') return state;
  const observation = query(['owner', String(owner.pid), JSON.stringify(owner.owner_identity ?? null), owner.owner_identity?.script || ENTRY_SCRIPT]);
  return ['alive', 'dead'].includes(observation.state) ? observation.state : 'unknown';
}

function parseOwnerLock(raw) {
  const text = raw.toString('utf8');
  if (/^[1-9][0-9]{0,9}$/.test(text) && Number(text) <= 2147483647) return { pid: Number(text) };
  const owner = JSON.parse(text);
  if (!owner || Object.keys(owner).sort().join(',') !== 'owner_identity,pid'
      || !Number.isSafeInteger(owner.pid) || owner.pid <= 0 || owner.pid > 2147483647
      || !validOwnerIdentity(owner.owner_identity)) throw new Error('Broker lease owner is unknown');
  return owner;
}

function recoveryGuard(filename, raw = process.env.CEO_HUB_RECOVERY_GUARD, referenceFile = path.join(ROOT, '.runtime', 'worker-terminal-reference.json')) {
  if (!raw) return null;
  try {
    if (raw.length > 8192) throw new Error();
    const guard = JSON.parse(raw);
    const validIdentity = value => value && Object.keys(value).sort().join(',') === 'ino,mtime_ns,size'
      && Object.values(value).every(part => typeof part === 'string' && /^[0-9]{1,32}$/.test(part));
    if (!guard || Object.keys(guard).sort().join(',') !== 'descriptor_file,descriptor_identity,descriptor_sha256,lock_identity,lock_sha256,maintenance_identity,maintenance_sha256,pid,recovery_id,reference_file,reference_identity,reference_sha256'
        || guard.descriptor_file !== path.resolve(filename) || guard.reference_file !== referenceFile
        || !Number.isSafeInteger(guard.pid) || guard.pid <= 0 || guard.pid > 2147483647
        || !/^[a-f0-9]{64}$/.test(guard.descriptor_sha256) || !/^[a-f0-9]{64}$/.test(guard.maintenance_sha256)
        || (guard.lock_sha256 !== null && !/^[a-f0-9]{64}$/.test(guard.lock_sha256))
        || !/^[a-f0-9]{32}$/.test(guard.recovery_id) || !/^[a-f0-9]{64}$/.test(guard.reference_sha256)
        || !validIdentity(guard.descriptor_identity) || !validIdentity(guard.maintenance_identity)
        || !validIdentity(guard.reference_identity) || (guard.lock_sha256 === null ? guard.lock_identity !== null : !validIdentity(guard.lock_identity))) throw new Error();
    return guard;
  } catch { throw new Error('Recovery authorization is invalid'); }
}

function verifyRecoveryInputs(filename, guard, { ownedLock = false, pidState, ownerState, currentOwnerIdentity } = {}) {
  try {
    const same = (snapshot, hash, identity) => snapshot.hash === hash
      && snapshot.identity.ino === identity.ino && snapshot.identity.size === identity.size && snapshot.identity.mtime_ns === identity.mtime_ns;
    if (!same(recoverySnapshot(`${filename}.recovery.lock`, 256), guard.maintenance_sha256, guard.maintenance_identity)) throw new Error();
    const reference = recoverySnapshot(guard.reference_file, 8192);
    if (!same(reference, guard.reference_sha256, guard.reference_identity)
        || JSON.parse(reference.raw).descriptor_file !== filename) throw new Error();
    const snapshot = recoverySnapshot(filename);
    if (!same(snapshot, guard.descriptor_sha256, guard.descriptor_identity)) throw new Error();
    const descriptor = JSON.parse(snapshot.raw);
    const endpoint = new URL(descriptor.url);
    // Legacy pidState is retained as a private unit-test hook. Production always
    // checks the original process birth/image through the shared OS helper.
    const stateOf = ownerState || (pidState ? owner => pidState(owner.pid) : recoveryOwnerState);
    if (descriptor.pid !== guard.pid || descriptor.version !== VERSION || endpoint.protocol !== 'http:'
        || endpoint.hostname !== '127.0.0.1' || !endpoint.port || endpoint.username || endpoint.password
        || !['', '/'].includes(endpoint.pathname) || endpoint.search || endpoint.hash
        || (descriptor.owner_identity !== undefined && !validOwnerIdentity(descriptor.owner_identity))
        || stateOf(descriptor) !== 'dead') throw new Error();
    const lockfile = `${filename}.lock`;
    if (ownedLock) {
      const owner = parseOwnerLock(recoverySnapshot(lockfile, 16384).raw);
      if (owner.pid !== process.pid) throw new Error();
      if (owner.owner_identity) {
        if (JSON.stringify(owner.owner_identity) !== JSON.stringify(currentOwnerIdentity || ownBrokerIdentity())) throw new Error();
      } else if (!pidState) throw new Error(); // Only old fixture hooks use PID-only child locks.
    } else if (guard.lock_sha256 === null) {
      if (fs.existsSync(lockfile)) throw new Error();
    } else {
      const lock = recoverySnapshot(lockfile, 16384);
      const owner = parseOwnerLock(lock.raw);
      if (!same(lock, guard.lock_sha256, guard.lock_identity) || owner.pid !== guard.pid
          || JSON.stringify(owner.owner_identity) !== JSON.stringify(descriptor.owner_identity)
          || stateOf(owner) !== 'dead') throw new Error();
    }
    return descriptor;
  } catch { throw new Error('Bound terminal owner changed or cannot be proved dead; recovery refused'); }
}

async function requireRefusedRecoveryPort(url) {
  const endpoint = new URL(url);
  await new Promise((resolve, reject) => {
    const probe = net.createConnection({ host: '127.0.0.1', port: Number(endpoint.port) });
    const fail = () => { probe.destroy(); reject(new Error('Bound terminal endpoint is not affirmatively offline; recovery refused')); };
    // Windows can report a refused loopback connection after about two seconds.
    // Extend only observation time: timeout/open/other errors still fail closed.
    probe.setTimeout(4000, fail);
    probe.once('connect', fail);
    probe.once('error', error => { probe.destroy(); if (error.code === 'ECONNREFUSED') resolve(); else fail(); });
  });
}

function releaseOwnedStartupFile(filename, owned) {
  if (!owned) return false;
  try {
    const current = recoverySnapshot(filename);
    if (current.hash !== owned.hash || current.identity.ino !== owned.identity.ino
        || current.identity.size !== owned.identity.size || current.identity.mtime_ns !== owned.identity.mtime_ns) return false;
    fs.unlinkSync(filename);
    return true;
  } catch { return false; }
}

async function initializeOwnedDisplay(filename, lockfile, guard, options = {}) {
  // Everything after lease acquisition belongs to one failure-cleanup scope.
  // In particular constructor/listen failures must not strand a new-child
  // lease next to the unchanged old descriptor, fencing all future recovery.
  const ownedLease = options.ownedLease || recoverySnapshot(lockfile, 16384);
  const verify = options.verify || verifyRecoveryInputs;
  const portProof = options.portProof || requireRefusedRecoveryPort;
  let server = null, temp = null, ownedTemp = null;
  try {
    if (guard) {
      const old = verify(filename, guard, { ownedLock: true });
      await portProof(old.url);
      verify(filename, guard, { ownedLock: true });
    }
    const token = (options.tokenFactory || (() => crypto.randomBytes(32).toString('hex')))();
    const broker = (options.brokerFactory || (() => new TerminalBroker()))();
    server = (options.serverFactory || createServer)(broker, token, guard?.recovery_id || null);
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    const descriptor = { url: `http://127.0.0.1:${server.address().port}`, token, pid: process.pid, version: VERSION,
      owner_identity: options.ownerIdentity || ownBrokerIdentity() };
    if (guard) descriptor.recovery_id = guard.recovery_id;
    temp = `${filename}.${process.pid}.tmp`;
    if (guard) verify(filename, guard, { ownedLock: true });
    fs.writeFileSync(temp, JSON.stringify(descriptor), { mode: 0o600, flag: 'wx' });
    ownedTemp = recoverySnapshot(temp);
    if (guard) verify(filename, guard, { ownedLock: true });
    fs.renameSync(temp, filename);
    return { broker, server, descriptor };
  } catch (error) {
    // There are no initialized work sessions until publication. Close only
    // this unpublished empty server, never a remote tmux/model or other owner.
    if (server) await new Promise(resolve => {
      try { server.close(() => resolve()); } catch { resolve(); }
    });
    if (temp) releaseOwnedStartupFile(temp, ownedTemp);
    if (parseOwnerLock(ownedLease.raw).pid === process.pid) releaseOwnedStartupFile(lockfile, ownedLease);
    throw error;
  }
}

async function main() {
  const filename = descriptorPath();
  const guard = recoveryGuard(filename);
  if (!guard && fs.existsSync(`${filename}.recovery.lock`)) throw new Error('Bound terminal maintenance is in progress');
  if (guard) {
    const old = verifyRecoveryInputs(filename, guard);
    await requireRefusedRecoveryPort(old.url);
    verifyRecoveryInputs(filename, guard);
  }
  fs.mkdirSync(path.dirname(filename), { recursive: true, mode: 0o700 });
  if (!guard) try {
    const existing = JSON.parse(fs.readFileSync(filename, 'utf8'));
    if (/^http:\/\/127\.0\.0\.1:\d+$/.test(existing.url)) {
      const result = await fetch(`${existing.url}/health`, { headers: { Authorization: `Bearer ${existing.token}` }, signal: AbortSignal.timeout(1000) });
      if (result.ok) { process.stdout.write('AgentCaptain terminal broker is already running.\n'); return; }
    }
  } catch {}
  const lockfile = `${filename}.lock`;
  const ownerIdentity = ownBrokerIdentity();
  let lock;
  try { lock = fs.openSync(lockfile, 'wx', 0o600); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    if (guard) verifyRecoveryInputs(filename, guard);
    const previous = recoverySnapshot(lockfile, 16384);
    const oldOwner = parseOwnerLock(previous.raw);
    if (recoveryOwnerState(oldOwner) !== 'dead') throw new Error('Existing terminal broker lease is alive or unknown; not replacing it');
    if (guard) verifyRecoveryInputs(filename, guard);
    if (!releaseOwnedStartupFile(lockfile, previous)) throw new Error('Existing terminal broker lease changed; not replacing it');
    lock = fs.openSync(lockfile, 'wx', 0o600);
  }
  fs.writeFileSync(lock, JSON.stringify({ pid: process.pid, owner_identity: ownerIdentity }));
  fs.closeSync(lock);
  const publishedLease = recoverySnapshot(lockfile, 16384);
  const { broker, server, descriptor } = await initializeOwnedDisplay(filename, lockfile, guard, { ownerIdentity });
  process.stdout.write(`AgentCaptain terminal broker listening on ${descriptor.url}\n`);
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => {
    for (const session of broker.sessions.values()) if (session.connected) try { session.pty.kill(); } catch {}
    try {
      const current = JSON.parse(fs.readFileSync(filename, 'utf8'));
      if (current.pid === process.pid && JSON.stringify(current.owner_identity) === JSON.stringify(ownerIdentity)) fs.unlinkSync(filename);
    } catch {}
    releaseOwnedStartupFile(lockfile, publishedLease);
    server.close(() => process.exit(0));
  });
}

module.exports = { main, TerminalBroker, ApiError, createServer, descriptorPath, shellQuote, classifyCommand, updateHumanInput, blockedScreen, spawnSshPipe, remoteBridgeCommand, remoteCloseCommand, validRemoteCloseIdentity, directRootIdentity, nativeRootKind, recoveryGuard, verifyRecoveryInputs, requireRefusedRecoveryPort, initializeOwnedDisplay, recoveryOwnerState, validOwnerIdentity, ownBrokerIdentity, parseOwnerLock, SSH_CONTROL_BUDGET };
if (require.main === module) main().catch(error => { process.stderr.write(`Terminal broker failed: ${error.message}\n`); process.exitCode = 1; });
