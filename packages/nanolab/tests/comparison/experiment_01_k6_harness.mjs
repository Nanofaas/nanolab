import crypto from 'node:crypto';
import fs from 'node:fs';
import vm from 'node:vm';

// Load the shipped k6 script with fake k6 modules, make one invocation, and
// print the events it emitted, the request it sent and the options it built.
//   argv[2]  the script
//   argv[3]  {"env", "status", "body" | "raw" (base64), "headers", "invalidJson"}
const sample = JSON.parse(process.argv[3]);
const events = [];
let request = null;
const body = sample.raw !== undefined
  ? Uint8Array.from(Buffer.from(sample.raw, 'base64')).buffer
  : JSON.stringify(sample.body);
const context = vm.createContext({
  __ENV: sample.env ?? {E01_PROTOCOL: JSON.stringify({phases: [], size: 1024}), E01_PAYLOAD: '{}'},
  Date: {now: () => 100000},
  console: {log: line => events.push(JSON.parse(line))},
});
const modules = {
  'k6/http': {default: {post: (url, payload, params) => {
    request = {url, payload, params};
    return {
      status: sample.status ?? 200,
      error_code: 0,
      timings: {duration: 100},
      headers: sample.headers ?? {},
      body,
      json: () => {
        if (sample.invalidJson) throw new SyntaxError('invalid JSON');
        return JSON.parse(body);
      },
    };
  }}},
  'k6/execution': {default: {scenario: {name: 'warmup', iterationInTest: 0}}},
  'k6/encoding': {default: {b64decode: text => {
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(text)) throw new Error('invalid base64');
    return Uint8Array.from(Buffer.from(text, 'base64')).buffer;
  }}},
  'k6/crypto': {default: {sha256: (data, encoding) =>
    crypto.createHash('sha256').update(Buffer.from(data)).digest(encoding)}},
  'k6': {sleep: () => {}},
};
const script = new vm.SourceTextModule(fs.readFileSync(process.argv[2], 'utf8'), {context});
await script.link(name => {
  const exports = modules[name];
  return new vm.SyntheticModule(Object.keys(exports), function() {
    for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
  }, {context});
});
await script.evaluate();
let error;
try { script.namespace.invoke(); } catch (failure) { error = String(failure); }
process.stdout.write(JSON.stringify({events, request, options: script.namespace.options, error}));
