"""Run the Node preload in a mocked VM: no sockets, GC, snapshots or procfs."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
let observer, connectionHandler, bytes = 0, connected = 0, posts = [];
let clock = 10;
const mockFs = {
  readFileSync: () => 'Uid:\t1000\t1000\t1000\t1000\n',
  readlinkSync: () => 'pid:[same]',
  statfsSync: () => ({type: 0x01021994, blocks: 2, bsize: 4096}),
  chmodSync: () => {}, writeFileSync: () => {}, openSync: () => 3,
  closeSync: () => {}, constants: {O_WRONLY: 1, O_CREAT: 64, O_EXCL: 128},
  writeSync: (fd, chunk, offset, length) => {
    if (scenario === 'write-failure') throw Error('ENOSPC');
    const count = Math.min(2, length); bytes += count; return count;
  }
};
class Session {
  connect() { connected += 1; }
  disconnect() {}
  on(name, callback) { this.chunk = callback; }
  post(method, params, callback) {
    posts.push(method);
    if (method === 'HeapProfiler.collectGarbage' && scenario !== 'missing-event') {
      observer({getEntries: () => [{detail: {kind: 4, flags: 4}, startTime: 11, duration: 1}]});
    }
    if (method === 'HeapProfiler.takeHeapSnapshot') {
      this.chunk({params: {chunk: '{"snapshot":'}});
      this.chunk({params: {chunk: '{}}'}});
    }
    callback(null, {});
  }
}
const context = {
  Buffer, setImmediate, setTimeout,
  process: {env: {}, pid: 1, versions: {node: '22.23.2'}, geteuid: () => 1000, umask: () => 0o022},
  require: name => {
    if (name === 'node:fs') return mockFs;
    if (name === 'node:net') return {createServer: handler => {
      connectionHandler = handler;
      return {listen: (path, callback) => callback(), unref: () => {}};
    }};
    if (name === 'node:inspector') return {Session, url: () => scenario === 'public-inspector' ? 'ws://bad' : undefined};
    if (name === 'node:perf_hooks') return {
      performance: {now: () => { const result = clock; clock += 10; return result; }},
      constants: {NODE_PERFORMANCE_GC_MAJOR: 4, NODE_PERFORMANCE_GC_FLAGS_FORCED: 4},
      PerformanceObserver: class {constructor(callback) { observer = callback; } observe() {}}
    };
    throw Error('unexpected real dependency: ' + name);
  }
};
try { vm.runInNewContext(source, context); }
catch (error) { console.log(JSON.stringify({startup_error: String(error)})); process.exit(0); }
const handlers = {};
const connection = {
  setTimeout: () => {}, on: (name, callback) => { handlers[name] = callback; },
  destroy: () => { throw Error('unexpected destroy'); },
  end: raw => console.log(JSON.stringify({reply: JSON.parse(raw), bytes, connected, posts}))
};
connectionHandler(connection);
const request = scenario === 'gc' || scenario === 'missing-event' ? {operation: 'gc'} : {
  operation: 'heap_dump', max_bytes: scenario === 'quota-mismatch' ? 4096 : 8192,
  path: '/proc/8/root/out/' + 'a'.repeat(32) + '/capture.heapsnapshot'
};
handlers.data(Buffer.from(JSON.stringify(request) + '\n'));
"""


@pytest.mark.parametrize(
    ("scenario", "ok"),
    [
        ("gc", True),
        ("missing-event", False),
        ("snapshot", True),
        ("quota-mismatch", False),
        ("write-failure", False),
    ],
)
def test_private_node_control_enforces_runtime_events_and_target_writer_bounds(
    scenario, ok
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node interpreter required for synthetic VM harness")
    source = Path(__file__).parents[2] / "assets/soak/node-diagnostic-control.cjs"
    result = subprocess.run(
        [node, "-e", HARNESS, str(source), scenario],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["reply"]["ok"] is ok
    if scenario == "gc":
        assert observed["reply"]["events"] == [
            {"kind": 4, "flags": 4, "startTime": 11, "duration": 1}
        ]
        assert observed["posts"] == ["HeapProfiler.collectGarbage"]
    if scenario == "snapshot":
        assert observed["bytes"] == 15
        assert observed["posts"] == ["HeapProfiler.takeHeapSnapshot"]
    if scenario == "quota-mismatch":
        assert observed["bytes"] == 0 and observed["posts"] == []


def test_private_node_control_refuses_existing_inspector_listener():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node interpreter required for synthetic VM harness")
    source = Path(__file__).parents[2] / "assets/soak/node-diagnostic-control.cjs"
    result = subprocess.run(
        [node, "-e", HARNESS, str(source), "public-inspector"],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    assert "startup_error" in json.loads(result.stdout)
