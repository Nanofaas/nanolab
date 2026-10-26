import http from 'k6/http';
import exec from 'k6/execution';
import encoding from 'k6/encoding';
import { sleep } from 'k6';

// Experiment 01: the control plane's cost in front of one function, on a
// calendar of constant-rate plateaux.
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
//   E01_PROTOCOL  {"phases": [...], "httpTimeoutSeconds", "gracefulStopSeconds",
//                  "preAllocatedVUs", "maxVUs", "size"}
//   E01_PAYLOAD   the exact JSON body, sent byte for byte on every request.
const BASE_URL = __ENV.NANOFAAS_URL || 'http://localhost:30080';
const FUNCTION = __ENV.NANOFAAS_FUNCTION || 'qr-code-rust';
const PROTOCOL = JSON.parse(__ENV.E01_PROTOCOL);
const PAYLOAD = __ENV.E01_PAYLOAD;
const URL = `${BASE_URL}/v1/functions/${FUNCTION}:invoke`;

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

// Light per-request check: the nanoFaaS envelope, success, media type and
// encoding, the PNG signature and its IHDR dimensions. It does not decode the
// QR symbol; full QR decoding remains outside this measurement.
export function correct(response) {
    if (response.status !== 200) {
        return false;
    }
    try {
        const body = response.json();
        if (body === null || typeof body !== 'object' || Array.isArray(body)) {
            return false;
        }
        const headers = body.headers || {};
        const type = Object.keys(headers)
            .filter((key) => key.toLowerCase() === 'content-type')
            .map((key) => String(headers[key]).split(';')[0].trim().toLowerCase())[0];
        if (
            body.status !== 'success'
            || body.statusCode !== 200
            || body.encoding !== 'base64'
            || type !== 'image/png'
            || typeof body.output !== 'string'
            || !body.output.startsWith('iVBORw0KGgo')
        ) {
            return false;
        }
        const head = new Uint8Array(encoding.b64decode(body.output.slice(0, 32), 'std'));
        const width = ((head[16] << 24) | (head[17] << 16) | (head[18] << 8) | head[19]) >>> 0;
        const height = ((head[20] << 24) | (head[21] << 16) | (head[22] << 8) | head[23]) >>> 0;
        return width === height && width >= PROTOCOL.size;
    } catch (error) {
        return false;
    }
}

export function invoke() {
    const phase = exec.scenario.name;
    const id = `${phase}-${exec.scenario.iterationInTest}`;
    const sent = Date.now();
    emit({ t: 'send', id, phase, ts: sent });
    const response = http.post(URL, PAYLOAD, {
        headers: { 'Content-Type': 'application/json' },
        timeout: `${PROTOCOL.httpTimeoutSeconds}s`,
        tags: { phase },
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
