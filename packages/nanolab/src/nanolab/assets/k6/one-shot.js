import http from 'k6/http';
import {sleep} from 'k6';
import exec from 'k6/execution';
import {SharedArray} from 'k6/data';
const rows = new SharedArray('originals', () => JSON.parse(open(__ENV.ONE_SHOT_SCHEDULE)));
export const options = {
  scenarios: {originals: {executor: 'shared-iterations', vus: Number(__ENV.ONE_SHOT_VUS), iterations: rows.length, maxDuration: __ENV.ONE_SHOT_DURATION}},
  maxRedirects: 0,
};
function header(result, name) {
  const key = Object.keys(result.headers).find(key => key.toLowerCase() === name.toLowerCase());
  return key ? result.headers[key] : null;
}
export default function () {
  const row = rows[exec.scenario.iterationInTest];
  const delay = row.scheduledAt - Date.now()/1000;
  if (delay > 0) sleep(delay);
  const emittedAt = Date.now()/1000;
  console.log(JSON.stringify({...row, event:'emitted', emittedAt}));
  const result = http.post(row.url, JSON.stringify({input: row.input}), {
    headers: {'Content-Type':'application/json', 'Idempotency-Key':row.originalId, 'X-Trace-Id':row.originalId},
    timeout:'30s', redirects:0,
  });
  let body = {};
  try {body = result.json();} catch (_) {}
  const output = /"output"\s*:\s*([0-9]+)/.exec(result.body || '');
  console.log(JSON.stringify({...row, event:'result', emittedAt, finishedAt:Date.now()/1000,
    latencySeconds:result.timings.duration/1000, statusCode:result.status, responseBody:result.body,
    executionId:body.executionId || header(result, 'X-Execution-Id') || null,
    terminalExecutionId:header(result, 'X-NanoFaaS-Terminal-Execution-Id') || body.executionId || null,
    responseStatus:body.status || null, outputJson:output ? output[1] : null,
    executionNode:header(result, 'X-NanoFaaS-Execution-Node') || null,
    offloadedTarget:header(result, 'X-NanoFaaS-Offloaded') || null}));
}
