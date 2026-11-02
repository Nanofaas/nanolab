import http from 'k6/http';
import execution from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter } from 'k6/metrics';
import { equal, loadInputs } from './word-stats-oracle.js';

const INPUTS = new SharedArray('module-cost-inputs', () => loadInputs(
    JSON.parse(open(__ENV.NANOFAAS_PAYLOAD)), JSON.parse(open(__ENV.NANOFAAS_CONTRACT))));
const invalid = new Counter('mc_invalid_output');
const phase = __ENV.MC_PHASE;
export const options = {
    scenarios: {load: {executor: 'constant-arrival-rate', rate: 40, timeUnit: '1s',
        duration: `${__ENV.MC_SECONDS}s`, preAllocatedVUs: 128, maxVUs: 128,
        gracefulStop: '40s', exec: 'invoke'}},
    maxRedirects: 0, thresholds: {mc_invalid_output: ['count==0']},
};
function request(fixture, kind, iteration) {
    const id = `${phase}:${kind}:${iteration}`;
    const started = Date.now();
    console.log(JSON.stringify({t: 'send', id, phase: kind, iteration,
        case_name: fixture.name, ts: started}));
    const response = http.post(`${__ENV.NANOFAAS_URL}/v1/functions/word-stats-java:invoke`,
        JSON.stringify({input: fixture.input}),
        {headers: {'Content-Type': 'application/json'}, timeout: '30s', redirects: 0});
    const ended = Date.now();
    let valid = false;
    if (response.status === 200) {
        try { const body = JSON.parse(response.body);
            valid = body.status === 'success' && equal(body.output, fixture.expected);
        } catch (_) { valid = false; }
        invalid.add(valid ? 0 : 1);
    }
    console.log(JSON.stringify({t: 'terminal', id, phase: kind, iteration,
        case_name: fixture.name, ts: ended, started_at: started,
        duration_ms: ended - started, http_duration_ms: response.timings.duration,
        status: response.status, error_code: response.error_code || 0,
        body: response.body || '', valid}));
    return valid;
}
export function setup() {
    invalid.add(0);
    for (let i = 0; i < INPUTS.length; i++)
        if (!request(INPUTS[i], 'qualification', i)) throw new Error('module-cost qualification failed');
}
export function invoke() {
    const i = execution.scenario.iterationInTest;
    request(INPUTS[i % INPUTS.length], phase, i);
}
