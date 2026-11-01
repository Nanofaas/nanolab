import { equal, loadInputs } from './word-stats-oracle.js';
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import execution from 'k6/execution';

const BASE_URL = __ENV.NANOFAAS_URL || 'http://localhost:30080';
const FN = __ENV.NANOFAAS_FUNCTION;
if (!FN) throw new Error('B5 requires one explicit function');
const INPUTS = new SharedArray('b5-qualified-corpus', () => loadInputs(
    JSON.parse(open(__ENV.NANOFAAS_PAYLOAD)), JSON.parse(open(__ENV.NANOFAAS_CONTRACT))));
const counters = {};
for (const name of ['offered', 'valid_completed', 'refused', 'invalid_output', 'http_error'])
    counters[name] = new Counter(`b5_${name}`);
const validDuration = new Trend('b5_valid_duration', true);
const scale = Number(__ENV.K6_RATE_SCALE || 1);
const vus = Number(__ENV.K6_MAX_VUS || 5000);
const shape = [['30s',40],['60s',200],['60s',200],['10s',600],['30s',600],
    ['10s',200],['60s',200],['60s',350],['45s',350],['10s',900],['30s',900],['45s',40]];
export const options = {
    scenarios: {function_runtime: {
        executor: 'ramping-arrival-rate', startRate: Math.max(1, Math.round(40 * scale)),
        timeUnit: '1s', preAllocatedVUs: vus, maxVUs: vus,
        stages: shape.map(([duration,target]) => ({duration,target: Math.max(1, Math.round(target * scale))})),
        exec: 'invoke', gracefulStop: '40s',
    }},
    maxRedirects: 0, thresholds: {},
};
function invokeCase(fixture, phase, iteration = null) {
    const tags = {fn: FN, path: 'sync', phase};
    const response = http.post(`${BASE_URL}/v1/functions/${encodeURIComponent(FN)}:invoke`,
        JSON.stringify({input: fixture.input}),
        {headers: {'Content-Type': 'application/json'}, timeout: '30s', redirects: 0, tags});
    let valid = false;
    if (response.status === 200) {
        try {
            const body = JSON.parse(response.body);
            valid = body.status === 'success' && equal(body.output, fixture.expected);
        } catch (_) { valid = false; }
    }
    const outcome = valid ? 'valid_completed' : response.status === 200 ? 'invalid_output'
        : [429,503].includes(response.status) ? 'refused' : 'http_error';
    console.log(JSON.stringify({t: 'http', fn: FN, path: 'sync', idem: 'no', part: 'single',
        phase, case_name: fixture.name, iteration, outcome,
        status: response.status, error_code: response.error_code || 0,
        ts: Date.now(), duration_ms: response.timings.duration}));
    if (phase === 'measurement') {
        counters.offered.add(1, tags);
        counters[outcome].add(1, tags);
        if (valid) validDuration.add(response.timings.duration, tags);
        check(response, {'valid output or overload refusal': () => valid || outcome === 'refused'}, tags);
    }
    return valid;
}
export function setup() {
    for (const counter of Object.values(counters)) counter.add(0);
    for (const fixture of INPUTS)
        if (!invokeCase(fixture, 'qualification')) throw new Error(`B5 qualification failed: ${fixture.name}`);
}
export function invoke() {
    const iteration = execution.scenario.iterationInTest;
    invokeCase(INPUTS[iteration % INPUTS.length], 'measurement', iteration);
}
