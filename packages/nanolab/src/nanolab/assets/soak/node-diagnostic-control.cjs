'use strict';

// Load with --require in the owned Node application. No inspector listener is
// opened: Session connects in-process; the only transport is a mode-0600 Unix
// socket in the target's private /tmp, reached from the same-UID helper.
const fs = require('node:fs');
const net = require('node:net');
const inspector = require('node:inspector');
const { performance, PerformanceObserver, constants } = require('node:perf_hooks');

const socketPath = process.env.NANOLAB_DIAGNOSTIC_SOCKET || '/tmp/nanolab-diagnostic.sock';
if (!/^\/tmp\/[A-Za-z0-9_.-]+$/.test(socketPath) || inspector.url()) {
  throw new Error('Private diagnostic socket and no inspector listener required');
}
let busy = false;
let majorCount = 0;
let observed = [];
const observer = new PerformanceObserver(list => {
  for (const event of list.getEntries()) {
    if (event.detail.kind === constants.NODE_PERFORMANCE_GC_MAJOR) {
      majorCount += 1;
      observed.push({ kind: event.detail.kind, flags: event.detail.flags,
        startTime: event.startTime, duration: event.duration });
      if (observed.length > 128) observed.shift();
    }
  }
});
observer.observe({ entryTypes: ['gc'] });

function destination(request) {
  const match = /^\/proc\/([0-9]+)\/root\/out\/[a-f0-9]{32}\/(probe\.bin|capture\.heapsnapshot)$/.exec(request.path || '');
  if (!match || !Number.isSafeInteger(request.max_bytes) || request.max_bytes <= 0) {
    throw new Error('Expected bounded helper proc-root destination');
  }
  const status = fs.readFileSync(`/proc/${match[1]}/status`, 'utf8');
  const uid = Number(/^Uid:\s+\d+\s+(\d+)/m.exec(status)?.[1]);
  if (uid !== process.geteuid() || fs.readlinkSync(`/proc/${match[1]}/ns/pid`) !== fs.readlinkSync('/proc/self/ns/pid')) {
    throw new Error('Helper UID/PID namespace differs from target');
  }
  const parent = request.path.slice(0, request.path.lastIndexOf('/'));
  const stat = fs.statfsSync(parent);
  const capacity = stat.blocks * stat.bsize;
  if (stat.type !== 0x01021994 || capacity <= 0 || capacity > request.max_bytes) {
    throw new Error('Target-visible tmpfs exceeds reservation');
  }
  return { type: 'tmpfs', capacity_bytes: capacity };
}

function post(session, method) {
  return new Promise((resolve, reject) => session.post(method, {}, (error, value) => error ? reject(error) : resolve(value)));
}

async function perform(request) {
  if (inspector.url()) throw new Error('Inspector listener unexpectedly opened');
  const session = new inspector.Session();
  session.connect();
  try {
    const base = { ok: true, pid: process.pid, version: process.versions.node,
      inspector_url: inspector.url() || null };
    if (request.operation === 'probe') {
      const quota = destination(request);
      // A target-side create/write plus statfs is the quota/access evidence.
      fs.writeFileSync(request.path, Buffer.from('nanolab-private-control\n'), { flag: 'wx', mode: 0o600 });
      return { ...base, quota, attach_output: 'in-process inspector.Session connected' };
    }
    if (request.operation === 'gc') {
      await new Promise(resolve => setImmediate(resolve));
      const before = majorCount;
      const started = performance.now();
      await post(session, 'HeapProfiler.collectGarbage');
      // PerformanceObserver delivery is asynchronous. Only actual major events
      // whose complete duration lies in this request window can prove success.
      await new Promise(resolve => setTimeout(resolve, 50));
      const ended = performance.now();
      const events = observed.filter(event => event.startTime >= started && event.startTime + event.duration <= ended);
      if (!events.length || majorCount <= before) throw new Error('No observed major-GC completion event');
      return { ...base, before_count: before, after_count: majorCount, started_ms: started,
        ended_ms: ended, major_kind: constants.NODE_PERFORMANCE_GC_MAJOR, events };
    }
    if (request.operation === 'heap_dump') {
      destination(request);
      const fd = fs.openSync(request.path, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
      let written = 0;
      let failure;
      session.on('HeapProfiler.addHeapSnapshotChunk', message => {
        if (failure) return;
        try {
          const chunk = Buffer.from(message.params.chunk);
          if (written + chunk.length > request.max_bytes) throw new Error('Snapshot reservation exhausted');
          let offset = 0;
          while (offset < chunk.length) {
            const count = fs.writeSync(fd, chunk, offset, chunk.length - offset);
            if (!count) throw new Error('Snapshot writer made no progress');
            offset += count;
          }
          written += chunk.length;
        } catch (error) { failure = error; }
      });
      try {
        await post(session, 'HeapProfiler.takeHeapSnapshot');
      } finally {
        fs.closeSync(fd);
      }
      if (failure) throw failure;
      if (!written) throw new Error('Empty heap snapshot');
      return { ...base, bytes_written: written };
    }
    throw new Error('Unsupported private diagnostic method');
  } finally {
    session.disconnect();
  }
}

const server = net.createServer(connection => {
  let pending = Buffer.alloc(0);
  let received = false;
  connection.setTimeout(5000, () => { if (!received) connection.destroy(); });
  connection.on('error', () => {});
  connection.on('data', async part => {
    if (received) return connection.destroy();
    pending = Buffer.concat([pending, part]);
    if (pending.length > 65536) return connection.destroy();
    if (!pending.includes(10)) return;
    received = true;
    if (busy) return connection.end(JSON.stringify({ ok: false, error: 'Diagnostic already active' }) + '\n');
    busy = true;
    try {
      const result = await perform(JSON.parse(pending.toString('utf8')));
      connection.end(JSON.stringify(result) + '\n');
    } catch (error) {
      connection.end(JSON.stringify({ ok: false, pid: process.pid, error: String(error) }) + '\n');
    } finally { busy = false; }
  });
});
// Existing socket is an ownership error; never unlink an unknown endpoint.
const previousMask = process.umask(0o077);
server.listen(socketPath, () => { fs.chmodSync(socketPath, 0o600); });
process.umask(previousMask);
server.unref();
