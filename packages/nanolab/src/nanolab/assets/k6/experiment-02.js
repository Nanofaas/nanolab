import http from 'k6/http';
import exec from 'k6/execution';
import encoding from 'k6/encoding';
import crypto from 'k6/crypto';
import { sleep } from 'k6';

// Experiment 02: experiment 01's calendar and ledger, sent to any platform.
//
// Idle, warmup, constant-rate plateaux and a bounded drain, each load phase
// its own `constant-arrival-rate` scenario with an explicit `startTime` and
// `gracefulStop`, exactly as in experiment-01.js. A `send` event is written
// immediately before the request and one terminal event after it, so a
// response landing after a boundary stays in the cohort that sent it.
//
// What differs per platform comes in E02_TARGET: the URL, the headers, and
// where the PNG is in the answer. The check is the same for every platform:
// a PNG signature and an IHDR at least as large as the requested size, and,
// once the protocol is frozen, the sha256 of the whole PNG. An HTTP 200 whose
// output is wrong is an incorrect response, never a success.
//
//   E02_PROTOCOL  {"phases": [...], "httpTimeoutSeconds", "gracefulStopSeconds",
//                  "preAllocatedVUs", "maxVUs"}
//   E02_PAYLOAD   the exact body, sent byte for byte on every request.
//   E02_TARGET    {"url", "headers", "extractor", "field", "pngSha256", "size"}
const PROTOCOL = JSON.parse(__ENV.E02_PROTOCOL);
const PAYLOAD = __ENV.E02_PAYLOAD;
const TARGET = JSON.parse(__ENV.E02_TARGET);
const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

function loadScenario(phase) {
    return {
        executor: 'constant-arrival-rate',
        rate: phase.rate,
        timeUnit: '1s',
        duration: `${phase.durationSeconds}s`,
        startTime: `${phase.startSeconds}s`,
        preAllocatedVUs: PROTOCOL.preAllocatedVUs,
        maxVUs: PROTOCOL.maxVUs,
        gracefulStop: `${PROTOCOL.gracefulStopSeconds}s`,
        exec: 'invoke',
        tags: { phase: phase.name },
    };
}

const scenarios = {};
const thresholds = {};
for (const phase of PROTOCOL.phases) {
    if (phase.rate > 0) {
        scenarios[phase.name] = loadScenario(phase);
        // Always-passing thresholds, declared so the summary carries
        // per-phase submetrics instead of one total.
        thresholds[`dropped_iterations{scenario:${phase.name}}`] = ['count>=0'];
        thresholds[`http_reqs{scenario:${phase.name}}`] = ['count>=0'];
    } else if (phase.name === 'drain') {
        scenarios.drain = {
            executor: 'constant-vus',
            vus: 1,
            duration: `${phase.durationSeconds}s`,
            startTime: `${phase.startSeconds}s`,
            gracefulStop: '0s',
            exec: 'wait',
            tags: { phase: 'drain' },
        };
    }
}

export const options = {
    scenarios,
    thresholds,
    // No retries: nothing here may mask a failure the report must count.
    discardResponseBodies: false,
};

function emit(event) {
    console.log(JSON.stringify(event));
}

export function setup() {
    // The calendar origin: scenario startTimes count from the end of setup.
    emit({ t: 'origin', ts: Date.now() });
}

function validPng(bytes) {
    if (bytes.length < 24 || PNG_SIGNATURE.some((value, index) => bytes[index] !== value)) {
        return false;
    }
    const width = ((bytes[16] << 24) | (bytes[17] << 16) | (bytes[18] << 8) | bytes[19]) >>> 0;
    const height = ((bytes[20] << 24) | (bytes[21] << 16) | (bytes[22] << 8) | bytes[23]) >>> 0;
    if (width !== height || width < TARGET.size) {
        return false;
    }
    return !TARGET.pngSha256 || crypto.sha256(bytes.buffer, 'hex') === TARGET.pngSha256;
}

// Only the first 32 characters are decoded unless the hash is checked: the
// header is all the shape check needs, and decoding every PNG in full would
// spend the generator's CPU on bytes nobody reads.
function base64Png(text) {
    if (typeof text !== 'string' || !text.startsWith('iVBORw0KGgo')) {
        return false;
    }
    const encoded = TARGET.pngSha256 ? text : text.slice(0, 32);
    return validPng(new Uint8Array(encoding.b64decode(encoded, 'std')));
}

function mediaType(headers) {
    return Object.keys(headers || {})
        .filter((key) => key.toLowerCase() === 'content-type')
        .map((key) => String(headers[key]).split(';')[0].trim().toLowerCase())[0];
}

export function correct(response) {
    if (response.status !== 200) {
        return false;
    }
    try {
        if (TARGET.extractor === 'raw-png') {
            return mediaType(response.headers) === 'image/png'
                && validPng(new Uint8Array(response.body));
        }
        const body = response.json();
        if (TARGET.extractor === 'json-base64' && !TARGET.field) {
            return base64Png(body);
        }
        if (body === null || typeof body !== 'object' || Array.isArray(body)) {
            return false;
        }
        if (TARGET.extractor === 'nanofaas-envelope') {
            return body.status === 'success'
                && body.statusCode === 200
                && body.encoding === 'base64'
                && mediaType(body.headers) === 'image/png'
                && base64Png(body.output);
        }
        if (TARGET.extractor === 'json-base64') {
            return (body.error === undefined || body.error === null)
                && base64Png(body[TARGET.field]);
        }
        return false;
    } catch (error) {
        return false;
    }
}

export function invoke() {
    const phase = exec.scenario.name;
    const id = `${phase}-${exec.scenario.iterationInTest}`;
    const sent = Date.now();
    emit({ t: 'send', id, phase, ts: sent });
    const response = http.post(TARGET.url, PAYLOAD, {
        headers: TARGET.headers,
        timeout: `${PROTOCOL.httpTimeoutSeconds}s`,
        tags: { phase },
        responseType: TARGET.extractor === 'raw-png' ? 'binary' : 'text',
    });
    const ts = Date.now();
    // 1050 is k6's request-timeout error code: the client gave up, which says
    // nothing about whether the server stopped working on the request.
    if (response.error_code === 1050) {
        emit({ t: 'timeout', id, phase, ts, dur: ts - sent });
    } else if (response.status === 0) {
        emit({ t: 'error', id, phase, ts, dur: ts - sent, error: response.error });
    } else {
        emit({
            t: 'done',
            id,
            phase,
            ts,
            dur: response.timings.duration,
            status: response.status,
            ok: correct(response),
        });
    }
}

export function wait() {
    sleep(1);
}
