import http from 'k6/http';
import exec from 'k6/execution';
import encoding from 'k6/encoding';
import crypto from 'k6/crypto';
import { sleep } from 'k6';

// Experiments 01 and 02: one calendar of constant-rate plateaux, one ledger.
//
// Experiment 01 measures the control plane's cost in front of one function;
// experiment 02 sends the same calendar to any platform. Both run this script,
// so a step means the same load in both.
//
// Idle, warmup, then steps with sharp changes, then a bounded drain. Each load
// phase is its own `constant-arrival-rate` scenario with an explicit
// `startTime`, so the calendar is the script's and not a ramp's. Every phase
// sets `gracefulStop` explicitly: at the end of its duration it stops STARTING
// requests, while the ones already sent may finish during the next phase. k6's
// default of 30s would cancel exactly the slow responses this run exists to see.
//
// The ledger is the event stream, not the summary. One `send` event is written
// immediately before `http.post`, carrying the phase captured at that moment;
// the terminal event (`done`, `timeout` or `error`) names the same id and phase,
// so a response landing after a boundary stays in the cohort that sent it. A
// request with a send and no terminal when the generator stops is pending, and
// the report keeps it in its cohort's denominator.
//
// Everything comes in through the environment, from one serialized protocol:
//   E01_PROTOCOL       {"phases": [...], "httpTimeoutSeconds", "gracefulStopSeconds",
//                       "preAllocatedVUs", "maxVUs", "size"}
//   E01_PAYLOAD        the exact body, sent byte for byte on every request.
//   EXPERIMENT_TARGET  experiment 02 only: {"url", "headers", "extractor",
//                       "field", "idHeader", "pngSha256"}.
// Without a target, requests go to nanoFaaS's invocation API at NANOFAAS_URL
// for NANOFAAS_FUNCTION and the answer is its envelope: experiment 01.
const PROTOCOL = JSON.parse(__ENV.E01_PROTOCOL);
const PAYLOAD = __ENV.E01_PAYLOAD;
const TARGET = __ENV.EXPERIMENT_TARGET
    ? JSON.parse(__ENV.EXPERIMENT_TARGET)
    : {
        url: `${__ENV.NANOFAAS_URL || 'http://localhost:30080'}/v1/functions/`
            + `${__ENV.NANOFAAS_FUNCTION || 'qr-code-rust'}:invoke`,
        headers: { 'Content-Type': 'application/json' },
        extractor: 'nanofaas-envelope',
        field: null,
        idHeader: null,
        pngSha256: null,
    };
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
        // Thresholds that always pass, declared only so the summary carries
        // per-phase submetrics: without them dropped iterations are one total.
        thresholds[`dropped_iterations{scenario:${phase.name}}`] = ['count>=0'];
        thresholds[`http_reqs{scenario:${phase.name}}`] = ['count>=0'];
    } else if (phase.name === 'drain') {
        // Keeps the test alive for the whole drain, sending nothing, so the
        // residual iterations of the last step can finish and be observed.
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
    // No retries anywhere: k6 retries nothing by default, and nothing here
    // should mask a failure the report must count.
    discardResponseBodies: false,
};

function emit(event) {
    console.log(JSON.stringify(event));
}

export function setup() {
    // The calendar origin. Scenario startTimes count from the end of setup,
    // so this is the nearest observable instant to the origin; the report
    // measures every delay from it, never from when the generator was launched.
    emit({ t: 'origin', ts: Date.now() });
}

// The check is the same for every target: a PNG signature and a square IHDR
// at least as large as the requested size and, once experiment 02's protocol
// is frozen, the sha256 of the whole PNG. It does not decode the QR symbol;
// full QR decoding remains outside this measurement. An HTTP 200 whose output
// is wrong is an incorrect response, never a success.
function validPng(bytes) {
    if (bytes.length < 24 || PNG_SIGNATURE.some((value, index) => bytes[index] !== value)) {
        return false;
    }
    const width = ((bytes[16] << 24) | (bytes[17] << 16) | (bytes[18] << 8) | bytes[19]) >>> 0;
    const height = ((bytes[20] << 24) | (bytes[21] << 16) | (bytes[22] << 8) | bytes[23]) >>> 0;
    if (width !== height || width < PROTOCOL.size) {
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

// `nanofaas-envelope` is the control plane's invocation envelope; `json-base64`
// a base64 PNG at `field` of a JSON object, or the whole JSON body when `field`
// is null; `raw-png` the PNG as the response body.
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
    // A target that admits one active execution per id gets each request's
    // own ledger id: a shared id would turn every overlap into a rejection.
    const headers = TARGET.idHeader
        ? Object.assign({}, TARGET.headers, { [TARGET.idHeader]: id })
        : TARGET.headers;
    const sent = Date.now();
    emit({ t: 'send', id, phase, ts: sent });
    const response = http.post(TARGET.url, PAYLOAD, {
        headers,
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
