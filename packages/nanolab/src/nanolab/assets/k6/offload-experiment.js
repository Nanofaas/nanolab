import http from 'k6/http';
import execution from 'k6/execution';
import { Counter } from 'k6/metrics';

const INPUTS = JSON.parse(open(__ENV.B4_INPUTS));
const EDGE = __ENV.B4_EDGE;
const CLOUD = __ENV.B4_CLOUD;
const FN = __ENV.NANOFAAS_FUNCTION;
const TARGET = __ENV.B4_MODE === 'cloud' ? 'cloud' : 'edge';
const URLS = {edge: EDGE, cloud: CLOUD};
const warmup = Number(__ENV.B4_WARMUP_SECONDS);
const vus = Number(__ENV.B4_VUS);
const invalid = new Counter('b4_invalid_output');

function equal(actual, expected) {
    if (actual === expected) return true;
    if (actual === null || expected === null || typeof actual !== 'object' || typeof expected !== 'object') return false;
    if (Array.isArray(actual) !== Array.isArray(expected)) return false;
    const keys = Object.keys(expected);
    return Object.keys(actual).length === keys.length && keys.every(key =>
        Object.prototype.hasOwnProperty.call(actual, key) && equal(actual[key], expected[key]));
}

export const options = {
    scenarios: {
        warmup: {executor: 'constant-arrival-rate', rate: 20, timeUnit: '1s',
            duration: `${warmup}s`, preAllocatedVUs: vus, maxVUs: vus,
            exec: 'invoke', gracefulStop: '40s'},
        measurement: {executor: 'constant-arrival-rate', rate: Number(__ENV.B4_RATE), timeUnit: '1s',
            startTime: `${warmup + 40}s`, duration: `${__ENV.B4_DURATION_SECONDS}s`,
            preAllocatedVUs: vus, maxVUs: vus, exec: 'invoke', gracefulStop: '40s'},
    },
    maxRedirects: 0,
    // Errors/refusals and drops are measured outcomes. Invalid bodies fail verification.
    thresholds: {b4_invalid_output: ['count==0'],
        'dropped_iterations{scenario:measurement}': ['count>=0'],
        'dropped_iterations{scenario:warmup}': ['count>=0']},
};

function request(target, fixture, phase, iteration) {
    const started = Date.now();
    const response = http.post(`${URLS[target]}/v1/functions/${encodeURIComponent(FN)}:invoke`,
        JSON.stringify({input: fixture.input}), {timeout: '30s', redirects: 0,
            headers: {'Content-Type': 'application/json'}, tags: {phase, target, function: FN}});
    const ended = Date.now();
    let valid = false;
    if (response.status === 200) {
        try {
            const body = JSON.parse(response.body);
            valid = body.status === 'success' && equal(body.output, fixture.expected);
        } catch (_) { valid = false; }
        invalid.add(valid ? 0 : 1);
    }
    const offloaded = response.headers['X-Nanofaas-Offloaded'] || response.headers['X-NanoFaaS-Offloaded'] || null;
    console.log(JSON.stringify({t: 'b4', phase, target, iteration, case_name: fixture.name,
        status: response.status, body: response.body || '', offloaded, duration_ms: ended - started,
        http_duration_ms: response.timings.duration, ts: ended, error_code: response.error_code || 0}));
    return valid;
}

export function setup() {
    invalid.add(0);
    for (const target of ['edge', 'cloud'])
        for (let i = 0; i < INPUTS.length; i++)
            if (!request(target, INPUTS[i], 'qualification', i))
                throw new Error(`B4 qualification failed for ${target}/${INPUTS[i].name}`);
}

export function invoke() {
    const iteration = execution.scenario.iterationInTest;
    request(TARGET, INPUTS[iteration % INPUTS.length], execution.scenario.name, iteration);
}
