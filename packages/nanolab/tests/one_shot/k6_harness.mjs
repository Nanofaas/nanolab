import fs from 'node:fs';
import vm from 'node:vm';
const row = JSON.parse(process.argv[3]);
const outputs = [];
let headers;
const context = vm.createContext({
  __ENV: {ONE_SHOT_VUS:'1', ONE_SHOT_DURATION:'10s'}, open:() => JSON.stringify([row]),
  Date:{now:() => 100000}, console:{log:line => outputs.push(JSON.parse(line))},
});
const modules = {
  'k6/http': {default:{post:(_url, _body, options) => {
    headers=options.headers;
    return {status:200, body:row.sdk ? '10654779122033555500' : '{"executionId":"api-id","status":"success","output":10654779122033555500}',
      json:() => row.sdk ? 10654779122033555500 : {executionId:'api-id',status:'success'}, headers:{},timings:{duration:100}};
  }}},
  'k6': {sleep:() => {}},
  'k6/execution': {default:{scenario:{iterationInTest:0}}},
  'k6/data': {SharedArray:class {constructor(_name, read) {return read();}}},
};
const script = new vm.SourceTextModule(fs.readFileSync(process.argv[2],'utf8'),{context});
await script.link(name => {
  const exports = modules[name];
  return new vm.SyntheticModule(Object.keys(exports),function(){
    for(const [name,value] of Object.entries(exports))this.setExport(name,value);
  },{context});
});
await script.evaluate();
script.namespace.default();
process.stdout.write(JSON.stringify({headers,result:outputs.at(-1)}));
