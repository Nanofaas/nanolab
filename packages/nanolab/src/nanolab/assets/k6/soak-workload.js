import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import execution from 'k6/execution';

// Frozen input/expected fixtures, using the existing invocation generator's
// input envelope and explicit HTTP + success-body semantics. No implicit data.
const configuration = JSON.parse(open(__ENV.NANOLAB_SOAK_CONFIG));
const counters = {};
for (const name of ['offered', 'success', 'error', 'retry', 'replay']) {
    counters[name] = new Counter(`soak_${name}`);
}
const scenarios = {};
const thresholds = {soak_error: ['count==0'], dropped_iterations: ['count==0']};
for (const fn of configuration.functions) {
    scenarios[fn.scenario] = fn.options;
    // Explicit submetrics retain per-function evidence in k6's summary.
    for (const name of Object.keys(counters)) {
        thresholds[`soak_${name}{scenario:${fn.scenario}}`] = ['count>=0'];
    }
    thresholds[`dropped_iterations{scenario:${fn.scenario}}`] = ['count==0'];
}
export const options = {scenarios, thresholds, maxRedirects: 0};

function equal(actual, expected) {
    if (actual === expected) return true;
    if (actual === null || expected === null || typeof actual !== 'object' || typeof expected !== 'object') return false;
    if (Array.isArray(actual) !== Array.isArray(expected)) return false;
    const keys = Object.keys(expected);
    return Object.keys(actual).length === keys.length && keys.every(key =>
        Object.prototype.hasOwnProperty.call(actual, key) && equal(actual[key], expected[key]));
}

export function invoke() {
    const fn = configuration.functions.find(item => item.scenario === execution.scenario.name);
    if (!fn || !fn.payloads.length) throw new Error('missing frozen function payload');
    const fixture = fn.payloads[__ITER % fn.payloads.length];
    const tags = {fn: fn.name, scenario: fn.scenario, path: 'sync'};
    counters.offered.add(1, tags);
    // These describe the client's deliberate zero-retry, zero-replay policy.
    counters.retry.add(0, tags);
    counters.replay.add(0, tags);
    let valid = false;
    try {
        const response = http.post(
            `${configuration.base_url}/v1/functions/${encodeURIComponent(fn.name)}:invoke`,
            JSON.stringify({input: fixture.input}),
            {headers: {'Content-Type': 'application/json'},
             timeout: `${configuration.request_timeout_s}s`, redirects: 0, tags},
        );
        valid = check(response, {
            'status is 200': r => r.status === 200,
            'has expected success response': r => {
                if (r.status !== 200) return false;
                try {
                    const body = JSON.parse(r.body);
                    return body !== null && body.status === 'success' && equal(body.output, fixture.expected);
                } catch (_) { return false; }
            },
        }, tags);
    } catch (_) {
        valid = false;
    }
    counters.success.add(valid ? 1 : 0, tags);
    counters.error.add(valid ? 0 : 1, tags);
}

export function handleSummary(data) {
    // The owned runner separates these frames from logs while enforcing one
    // shared output budget. k6 never opens an unrestricted summary file.
    const marker = __ENV.NANOLAB_SOAK_SUMMARY_MARKER;
    return {stdout: `\n${marker}:START\n${JSON.stringify(data)}\n${marker}:END\n`};
}
