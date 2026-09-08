import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";

const browserPath = process.env.VOICEOPS_BROWSER ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const baseURL = process.env.VOICEOPS_BASE_URL ?? "http://127.0.0.1:8000";
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

const fixture = `
    globalThis.__voiceFrames = [];
    globalThis.__requests = [];
    globalThis.__tracks = [];
    const originalMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async constraints => {
        if (globalThis.__denyMic) throw new DOMException('denied', 'NotAllowedError');
        const media = await originalMedia(constraints);
        globalThis.__tracks.push(...media.getTracks());
        return media;
    };
    const NativeSocket = globalThis.WebSocket;
    globalThis.WebSocket = class extends NativeSocket {
        constructor(url) {
            if (String(url).includes('/ws/voice-stream')) {
                const socket = {
                    readyState: 1, bufferedAmount: 0,
                    send(data) { if (data instanceof ArrayBuffer) globalThis.__voiceFrames.push(Array.from(new Uint8Array(data))); },
                    close() { this.readyState = 3; setTimeout(() => this.onclose?.({ code: 1000 }), 0); },
                };
                globalThis.__voiceSocket = socket;
                setTimeout(() => socket.onmessage?.({ data: JSON.stringify({ type: 'ready', sample_rate: 16000 }) }), 30);
                return socket;
            }
            const socket = super(url);
            globalThis.__telemetrySocket = socket;
            return socket;
        }
    };
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (url, options) => {
        if (String(url).includes('/api/v1/commands/confirm')) {
            const body = JSON.parse(options.body);
            globalThis.__requests.push(body);
            await new Promise(resolve => setTimeout(resolve, 100));
            if (globalThis.__requestFailure) throw new TypeError('simulated network loss');
            return new Response(JSON.stringify({ success: !globalThis.__denyPreview, dry_run: body.dry_run, intent: 'PROCESS_KILL', message: globalThis.__denyPreview ? 'Mock evaluation denied.' : body.dry_run ? 'Mock dry-run: no mutation.' : 'Mock execution complete.' }), { headers: { 'Content-Type': 'application/json' } });
        }
        return originalFetch(url, options);
    };
    globalThis.__emitVoice = message => globalThis.__voiceSocket.onmessage({ data: JSON.stringify(message) });
    globalThis.__showGate = (expires = 60000) => globalThis.__emitVoice({ type: 'confirmation_required', token: 'browser-test-token', expires_at: new Date(Date.now() + expires).toISOString(), intent: 'PROCESS_KILL', preview: { success: true, affected_pid: 4242, affected_process_name: '<img src=x onerror=alert(1)>', message: 'Would terminate sandbox worker.' } });
`;

test("native HUD browser integration (local server required; all mutations and STT mocked)", { timeout: 90000 }, async t => {
    const profile = await mkdtemp(join(tmpdir(), "voiceops-browser-"));
    const child = spawn(browserPath, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "about:blank"], { stdio: ["ignore", "pipe", "pipe"] });
    let ws;
    const calls = new Map();
    let nextId = 1;
    let sessionId;
    const exceptions = [];
    const endpoint = new Promise((resolve, reject) => {
        let output = "";
        child.stderr.on("data", chunk => {
            output += chunk.toString();
            const match = output.match(/DevTools listening on (ws:\/\/[^\s]+)/);
            if (match) resolve(match[1]);
        });
        child.once("error", reject);
        child.once("exit", code => reject(new Error(`Browser exited (${code}) before debugger connected.`)));
    });
    function send(method, params = {}, session = sessionId) {
        const id = nextId++;
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => { calls.delete(id); reject(new Error(`CDP timed out: ${method}`)); }, 10000);
            calls.set(id, { resolve, reject, timer });
            ws.send(JSON.stringify({ id, method, params, ...(session ? { sessionId: session } : {}) }));
        });
    }
    async function evaluate(expression) {
        const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true, userGesture: true });
        if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? "Browser evaluation failed");
        return result.result.value;
    }
    async function until(expression, timeout = 10000) {
        const deadline = Date.now() + timeout;
        while (Date.now() < deadline) {
            if (await evaluate(expression)) return;
            await pause(100);
        }
        const diagnostics = await evaluate("document.getElementById('terminal-feed')?.textContent + ' / VOICE: ' + document.getElementById('voice-status')?.textContent");
        throw new Error(`Browser condition failed: ${expression}\n${diagnostics}`);
    }
    try {
        ws = new WebSocket(await Promise.race([endpoint, pause(15000).then(() => { throw new Error("Browser startup timed out"); })]));
        await once(ws, "open");
        ws.onmessage = event => {
            const message = JSON.parse(event.data);
            if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
            const call = calls.get(message.id);
            if (!call) return;
            clearTimeout(call.timer);
            calls.delete(message.id);
            if (message.error) call.reject(new Error(message.error.message));
            else call.resolve(message.result);
        };
        const { targetId } = await send("Target.createTarget", { url: "about:blank" });
        ({ sessionId } = await send("Target.attachToTarget", { targetId, flatten: true }));
        await send("Runtime.enable");
        await send("Page.enable");
        await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
        await send("Page.addScriptToEvaluateOnNewDocument", { source: fixture });
        await send("Page.navigate", { url: baseURL });
        await until("document.getElementById('telemetry-status')?.dataset.state === 'live'");

        await t.test("renders live telemetry and switches process rankings", async () => {
            assert.notEqual(await evaluate("document.getElementById('cpu-value').textContent"), "--");
            assert.ok(await evaluate("document.querySelectorAll('#process-rows tr').length > 0"));
            await evaluate("document.getElementById('rank-cpu').click()");
            assert.equal(await evaluate("document.getElementById('rank-cpu').getAttribute('aria-pressed')"), "true");
            assert.equal(await evaluate("document.querySelector('#process-rows td.ranked').cellIndex"), 2);
            assert.ok(await evaluate("document.documentElement.scrollWidth <= innerWidth"));
        });
        await t.test("telemetry reconnects after a dropped WebSocket", async () => {
            await evaluate("globalThis.__oldTelemetry = globalThis.__telemetrySocket; globalThis.__telemetrySocket.close()");
            await until("globalThis.__telemetrySocket !== globalThis.__oldTelemetry && document.getElementById('telemetry-status').dataset.state === 'live'");
        });
        await t.test("actual AudioWorklet captures a fake microphone into PCM frames", async () => {
            await evaluate("document.getElementById('mic-toggle').click()");
            await until("globalThis.__voiceFrames.length >= 3 && globalThis.__voiceFrames.some(frame => frame.some(value => value !== 0))");
            assert.equal(await evaluate("document.getElementById('voice-status').textContent"), "UPLINK ACTIVE");
            assert.ok(await evaluate("globalThis.__voiceFrames.every(frame => frame.length === 3200)"));
            assert.ok(await evaluate("globalThis.__voiceFrames.some(frame => frame.some(value => value !== 0))"));
        });
        await t.test("transcripts are text-safe and confirmation never auto-executes", async () => {
            await evaluate("__emitVoice({type:'transcript', text:'<img src=x onerror=alert(1)>', end_of_turn:false})");
            assert.equal(await evaluate("document.getElementById('partial-transcript').textContent"), "<img src=x onerror=alert(1)>");
            await evaluate("__showGate()");
            assert.equal(await evaluate("document.getElementById('confirmation-dialog').open"), true);
            assert.equal(await evaluate("document.activeElement.id"), "cancel-confirmation");
            assert.equal(await evaluate("document.querySelectorAll('#confirmation-dialog img').length"), 0);
            await evaluate("__emitVoice({type:'transcript', text:'confirm', end_of_turn:true})");
            assert.equal(await evaluate("__requests.length"), 0);
        });
        await t.test("dry-run is explicit, execution is single-flight and not replayed", async () => {
            await evaluate("document.getElementById('preview-confirmation').click()");
            await until("document.getElementById('confirmation-feedback').textContent === 'Mock dry-run: no mutation.'");
            assert.equal(await evaluate("__requests[0].dry_run"), true);
            await evaluate("document.getElementById('execute-confirmation').click(); document.getElementById('execute-confirmation').click()");
            await until("document.getElementById('confirmation-feedback').textContent === 'Mock execution complete.'");
            assert.equal(await evaluate("__requests.filter(item => !item.dry_run).length"), 1);
            assert.equal(await evaluate("document.getElementById('execute-confirmation').disabled"), true);
            await evaluate("document.getElementById('cancel-confirmation').click()");
        });
        await t.test("expired and dismissed requests cannot execute", async () => {
            await evaluate("__showGate(-1000)");
            assert.equal(await evaluate("document.getElementById('execute-confirmation').disabled"), true);
            await evaluate("document.getElementById('cancel-confirmation').click(); __showGate(); document.getElementById('cancel-confirmation').click()");
            assert.equal(await evaluate("__requests.length"), 2);
        });
        await t.test("ambiguous execution failures never retry a mutation", async () => {
            await evaluate("__requestFailure = true; __showGate(); document.getElementById('execute-confirmation').click()");
            await until("document.getElementById('confirmation-feedback').textContent.includes('outcome unknown')");
            assert.equal(await evaluate("document.getElementById('execute-confirmation').disabled"), true);
            assert.equal(await evaluate("__requests.length"), 3);
            await evaluate("__requestFailure = false; document.getElementById('cancel-confirmation').click()");
        });
        await t.test("a failed dry-run recheck disables execution without consuming the token", async () => {
            await evaluate("__denyPreview = true; __showGate(); document.getElementById('preview-confirmation').click()");
            await until("document.getElementById('confirmation-feedback').textContent === 'Mock evaluation denied.'");
            assert.equal(await evaluate("document.getElementById('execute-confirmation').disabled"), true);
            assert.equal(await evaluate("document.getElementById('confirmation-countdown').textContent"), "REQUEST UNAVAILABLE");
            await evaluate("__denyPreview = false; document.getElementById('cancel-confirmation').click()");
        });
        await t.test("voice reconnect discards review and stop releases microphone tracks", async () => {
            await evaluate("__showGate(); globalThis.__oldVoice = __voiceSocket; __voiceSocket.close()");
            await until("__voiceSocket !== __oldVoice && document.getElementById('voice-status').textContent === 'UPLINK ACTIVE'");
            assert.equal(await evaluate("document.getElementById('confirmation-dialog').open"), false);
            await evaluate("document.getElementById('mic-toggle').click()");
            await until("__tracks.every(track => track.readyState === 'ended')");
            assert.equal(await evaluate("document.getElementById('mic-toggle').getAttribute('aria-pressed')"), "false");
        });
        await t.test("permission denial is recoverable and mobile layout stays within viewport", async () => {
            await evaluate("__denyMic = true; document.getElementById('mic-toggle').click()");
            await until("document.getElementById('terminal-feed').textContent.includes('permission denied')");
            assert.equal(await evaluate("document.getElementById('mic-toggle').getAttribute('aria-pressed')"), "false");
            await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
            await pause(200);
            assert.ok(await evaluate("document.documentElement.scrollWidth <= innerWidth"));
            await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
        });
        await t.test("K8S_CLUSTER mode toggle swaps HUD chrome and renders simulated pods", async () => {
            await evaluate("document.getElementById('mode-cluster').click()");
            await until("document.getElementById('mode-cluster').getAttribute('aria-pressed') === 'true'");
            await until("document.getElementById('processes-title-text').textContent === 'Pod monitor'");
            await until("document.querySelectorAll('#process-rows tr').length === 3");
            assert.equal(await evaluate("document.getElementById('col-1').textContent"), "NAME");
            assert.ok(await evaluate("[...document.querySelectorAll('#process-rows td')].some(cell => cell.textContent === 'payment-gateway-pod')"));
            assert.equal(await evaluate("document.getElementById('rank-toggle').hidden"), true);
            await evaluate("document.getElementById('mode-host').click()");
            await until("document.getElementById('processes-title-text').textContent === 'Process monitor'");
        });
        await t.test("export post-mortem button exists and reports a clear error with no resolved incident", async () => {
            await evaluate("document.getElementById('export-post-mortem').click()");
            await until("document.getElementById('terminal-feed').textContent.includes('No resolved incident')");
        });
        assert.deepEqual(exceptions, []);
        if (process.env.VOICEOPS_SCREENSHOT) {
            const { data } = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
            await writeFile(process.env.VOICEOPS_SCREENSHOT, Buffer.from(data, "base64"));
        }
    } finally {
        if (ws?.readyState === WebSocket.OPEN) {
            await send("Browser.close", {}, undefined).catch(() => {});
            ws.close();
        }
        if (child.exitCode === null) {
            await Promise.race([once(child, "exit"), pause(3000)]);
            if (child.exitCode === null) child.kill();
        }
        for (const call of calls.values()) clearTimeout(call.timer);
        await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 }).catch(() => {});
    }
});
