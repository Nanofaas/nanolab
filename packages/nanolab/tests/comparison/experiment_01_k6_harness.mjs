import fs from 'node:fs';
import vm from 'node:vm';

const sample = JSON.parse(process.argv[3]);
const events = [];
const context = vm.createContext({
  __ENV: {E01_PROTOCOL: JSON.stringify({phases: [], size: 1024}), E01_PAYLOAD: '{}'},
  Date: {now: () => 100000},
  console: {log: line => events.push(JSON.parse(line))},
});
const modules = {
  'k6/http': {default: {post: () => ({
    status: sample.status ?? 200,
    error_code: 0,
    timings: {duration: 100},
    json: () => {
      if (sample.invalidJson) throw new SyntaxError('invalid JSON');
      return sample.body;
    },
  })}},
  'k6/execution': {default: {scenario: {name: 'warmup', iterationInTest: 0}}},
  'k6/encoding': {default: {b64decode: text => {
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(text)) throw new Error('invalid base64');
    return Uint8Array.from(Buffer.from(text, 'base64')).buffer;
  }}},
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
process.stdout.write(JSON.stringify({events, error}));
