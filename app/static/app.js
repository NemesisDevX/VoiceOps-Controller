export class PCM16Encoder {
    constructor(inputRate, onFrame) {
        if (!Number.isFinite(inputRate) || inputRate < 16000 || inputRate > 192000) {
            throw new RangeError("Input rate must be between 16,000 and 192,000 Hz.");
        }
        this.ratio = inputRate / 16000;
        this.half = 48;
        this.taps = this.half * 2;
        this.phases = 512;
        this.onFrame = onFrame;
        this.kernel = new Float64Array(this.phases * this.taps);
        const cutoff = Math.min(7000 / inputRate, 0.45);
        for (let phase = 0; phase < this.phases; phase++) {
            let sum = 0;
            for (let tap = 0; tap < this.taps; tap++) {
                const distance = tap - this.half + 1 - phase / this.phases;
                const sinc = Math.abs(distance) < 1e-10 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * distance) / (Math.PI * distance);
                const window = 0.42 + 0.5 * Math.cos(Math.PI * distance / this.half) + 0.08 * Math.cos(2 * Math.PI * distance / this.half);
                const weight = sinc * window;
                this.kernel[phase * this.taps + tap] = weight;
                sum += weight;
            }
            for (let tap = 0; tap < this.taps; tap++) this.kernel[phase * this.taps + tap] /= sum;
        }
        this.reset();
    }

    reset() {
        this.ring = new Float32Array(256);
        this.inputCount = 0;
        this.outputCount = 0;
        this.frameOffset = 0;
        this.frame = new ArrayBuffer(3200);
        this.view = new DataView(this.frame);
    }

    push(channels) {
        if (!channels.length || !channels[0].length) return;
        for (let i = 0; i < channels[0].length; i++) {
            let mono = 0;
            for (const channel of channels) mono += Number.isFinite(channel[i]) ? channel[i] : 0;
            this.ring[this.inputCount % this.ring.length] = mono / channels.length;
            this.inputCount++;
            let position = this.outputCount * this.ratio;
            while (Math.floor(position) + this.half < this.inputCount) {
                const center = Math.floor(position);
                const phase = Math.min(this.phases - 1, Math.floor((position - center) * this.phases));
                let value = 0;
                for (let tap = 0; tap < this.taps; tap++) {
                    const index = center - this.half + 1 + tap;
                    if (index >= 0) value += this.ring[index % this.ring.length] * this.kernel[phase * this.taps + tap];
                }
                value = Math.max(-1, Math.min(1, value));
                this.view.setInt16(this.frameOffset * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
                this.frameOffset++;
                this.outputCount++;
                if (this.frameOffset === 1600) {
                    this.onFrame(this.frame);
                    this.frame = new ArrayBuffer(3200);
                    this.view = new DataView(this.frame);
                    this.frameOffset = 0;
                }
                position = this.outputCount * this.ratio;
            }
        }
    }
}

export function reconnectDelay(attempt, random = Math.random) {
    return Math.min(30000, 500 * 2 ** Math.min(attempt, 6) * (1 + random() * 0.25));
}

if (typeof AudioWorkletProcessor !== "undefined") {
    class PCM16CaptureProcessor extends AudioWorkletProcessor {
        constructor() {
            super();
            this.enabled = false;
            this.epoch = 0;
            this.encoder = new PCM16Encoder(sampleRate, frame => this.port.postMessage({ frame, epoch: this.epoch, capturedAt: currentTime }, [frame]));
            this.port.onmessage = event => {
                this.enabled = event.data.enabled === true;
                this.epoch = event.data.epoch;
                this.encoder.reset();
            };
        }

        process(inputs) {
            if (this.enabled && inputs[0]?.length) this.encoder.push(inputs[0]);
            return true;
        }
    }
    registerProcessor("voiceops-pcm16", PCM16CaptureProcessor);
}

export class ReconnectingSocket {
    constructor(path, { onMessage, onState, onClose = () => {}, maxAttempts = Infinity }) {
        this.path = path;
        this.onMessage = onMessage;
        this.onState = onState;
        this.onClose = onClose;
        this.maxAttempts = maxAttempts;
        this.attempt = 0;
        this.active = false;
        this.socket = null;
        this.timer = null;
        this.watchdog = null;
    }

    start() {
        if (this.active) return;
        this.active = true;
        this.connect();
    }

    connect() {
        if (!this.active) return;
        this.onState("connecting", this.attempt);
        const url = new URL(this.path, location.href);
        url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
        const socket = new WebSocket(url);
        socket.binaryType = "arraybuffer";
        this.socket = socket;
        this.armWatchdog(15000);
        socket.onmessage = event => {
            if (socket !== this.socket || !this.active) return;
            try {
                const payload = JSON.parse(event.data);
                this.onMessage(payload);
            } catch {
                this.onState("invalid");
                socket.close(4003, "Invalid server payload");
            }
        };
        socket.onerror = () => socket.close();
        socket.onclose = () => {
            if (socket !== this.socket) return;
            clearTimeout(this.watchdog);
            this.socket = null;
            this.onClose();
            if (!this.active) return;
            if (this.attempt >= this.maxAttempts) {
                this.active = false;
                this.onState("exhausted");
                return;
            }
            const delay = reconnectDelay(this.attempt++);
            this.onState("retrying", delay);
            this.timer = setTimeout(() => this.connect(), delay);
        };
    }

    markHealthy(timeout = 0) {
        this.attempt = 0;
        clearTimeout(this.watchdog);
        if (timeout) this.armWatchdog(timeout);
    }

    armWatchdog(timeout) {
        clearTimeout(this.watchdog);
        this.watchdog = setTimeout(() => this.socket?.close(4000, "Upstream idle"), timeout);
    }

    send(frame) {
        if (this.socket?.readyState !== WebSocket.OPEN) return false;
        if (this.socket.bufferedAmount > 32000) {
            this.socket.close(4001, "Audio backpressure");
            return false;
        }
        this.socket.send(frame);
        return true;
    }

    stop() {
        this.active = false;
        clearTimeout(this.timer);
        clearTimeout(this.watchdog);
        const socket = this.socket;
        this.socket = null;
        socket?.close(1000, "Operator stopped");
    }
}

class MicrophoneUplink {
    constructor({ onMode, onEvent, onDisconnect, onFormat }) {
        this.onMode = onMode;
        this.onEvent = onEvent;
        this.onDisconnect = onDisconnect;
        this.onFormat = onFormat;
        this.wanted = false;
        this.generation = 0;
        this.ready = false;
        this.analyser = null;
    }

    async start() {
        if (this.wanted) return;
        this.wanted = true;
        const generation = ++this.generation;
        this.onMode("connecting", "REQUESTING MICROPHONE");
        try {
            if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia || !globalThis.AudioWorkletNode) {
                throw new Error("Microphone streaming requires a modern browser on HTTPS or localhost with AudioWorklet support.");
            }
            const context = new AudioContext({ latencyHint: "interactive" });
            this.context = context;
            await context.resume();
            if (generation !== this.generation) return;
            const media = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: { ideal: 1 }, echoCancellation: true, noiseSuppression: true, autoGainControl: false }, video: false });
            if (generation !== this.generation) {
                media.getTracks().forEach(track => track.stop());
                return;
            }
            this.media = media;
            await context.audioWorklet.addModule(new URL("./app.js", import.meta.url));
            if (generation !== this.generation) return;
            const node = new AudioWorkletNode(context, "voiceops-pcm16", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCountMode: "max" });
            this.node = node;
            this.source = context.createMediaStreamSource(media);
            this.analyser = context.createAnalyser();
            this.analyser.fftSize = 2048;
            this.mute = context.createGain();
            this.mute.gain.value = 0;
            this.source.connect(this.analyser);
            this.source.connect(node);
            node.connect(this.mute).connect(context.destination);
            this.onFormat(`${context.sampleRate.toLocaleString()} HZ NATIVE → 16,000 HZ / MONO / PCM16LE`);
            node.onprocessorerror = () => this.fail("Audio processor failed. Stop and re-enable the microphone.");
            let captureEpoch = 0;
            node.port.onmessage = event => {
                const { frame, epoch, capturedAt } = event.data;
                if (generation === this.generation && epoch === captureEpoch && this.ready && this.wanted && context.state === "running" && context.currentTime - capturedAt < 0.25) this.transport.send(frame);
            };
            media.getAudioTracks()[0].onended = () => this.fail("Microphone disconnected or permission revoked.");
            context.onstatechange = () => {
                if (this.wanted && context.state === "suspended") this.fail("Audio capture was suspended by the browser. Re-enable the microphone to resume.");
            };
            this.transport = new ReconnectingSocket("/ws/voice-stream", {
                maxAttempts: 5,
                onMessage: message => {
                    if (message.type === "ready") {
                        this.ready = true;
                        this.transport.markHealthy();
                        node.port.postMessage({ enabled: true, epoch: ++captureEpoch });
                        this.onMode("live", "UPLINK ACTIVE");
                    } else if (message.type === "error") {
                        this.onEvent(message);
                        if (message.retryable === false) this.stop();
                        else this.transport.socket?.close(4002, "Provider unavailable");
                    } else {
                        this.onEvent(message);
                    }
                },
                onState: (state, detail) => {
                    if (state === "exhausted") this.fail("Voice uplink unavailable after repeated attempts. Re-enable the microphone to retry.");
                    else this.onMode("connecting", state === "retrying" ? `RECONNECT IN ${Math.ceil(detail / 1000)}s` : "CONNECTING TO STT");
                },
                onClose: () => {
                    this.ready = false;
                    node.port.postMessage({ enabled: false });
                    this.onDisconnect();
                },
            });
            this.transport.start();
        } catch (error) {
            if (generation === this.generation) {
                const message = error.name === "NotAllowedError" ? "Microphone permission denied. Allow access in browser site settings, then retry." : error.name === "NotFoundError" ? "No microphone found. Connect an input device and retry." : error.message;
                this.fail(message);
            }
        }
    }

    fail(message) {
        this.stop();
        this.onEvent({ type: "error", message });
        this.onMode("error", "UPLINK ERROR");
    }

    stop() {
        this.wanted = false;
        this.generation++;
        this.ready = false;
        this.transport?.stop();
        this.node?.port.postMessage({ enabled: false });
        this.node?.disconnect();
        this.source?.disconnect();
        this.mute?.disconnect();
        this.media?.getTracks().forEach(track => { track.onended = null; track.stop(); });
        if (this.context) {
            this.context.onstatechange = null;
            if (this.context.state !== "closed") void this.context.close().catch(() => {});
        }
        this.node = this.source = this.mute = this.media = this.context = this.analyser = null;
        this.onMode("offline", "OFFLINE");
        this.onDisconnect();
    }
}

function bootDashboard() {
    const $ = id => document.getElementById(id);
    const histories = { cpu: [], memory: [], disk: [] };
    let telemetry = null;
    let lastSample = 0;
    let packetCount = 0;
    let ranking = "memory";
    let pending = null;
    let busy = false;
    let consumed = false;
    let invalidated = false;
    const dialog = $("confirmation-dialog");
    const feed = $("terminal-feed");
    const formatTime = () => new Date().toISOString().slice(11, 19);

    function log(kind, label, text) {
        const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
        const entry = document.createElement("div");
        entry.className = "feed-entry";
        entry.dataset.kind = kind;
        for (const [className, content] of [["feed-time", formatTime()], ["feed-tag", label], ["feed-text", String(text).slice(0, 4000)]]) {
            const span = document.createElement("span");
            span.className = className;
            span.textContent = content;
            entry.append(span);
        }
        feed.append(entry);
        while (feed.childElementCount > 200) feed.firstElementChild.remove();
        if (atBottom) feed.scrollTop = feed.scrollHeight;
    }

    function renderProcesses() {
        const rows = telemetry?.[ranking === "cpu" ? "top_cpu_processes" : "top_memory_processes"];
        if (!Array.isArray(rows)) return;
        const body = $("process-rows");
        body.replaceChildren();
        for (const process of rows.slice(0, 50)) {
            const row = document.createElement("tr");
            const values = [process.pid, process.name, Number(process.cpu_percent).toFixed(1), Number(process.memory_percent).toFixed(1), Number(process.memory_rss_mb).toLocaleString(undefined, { maximumFractionDigits: 1 })];
            values.forEach((value, index) => {
                const cell = document.createElement("td");
                cell.textContent = value;
                if (index > 1) cell.className = "numeric";
                if (index === (ranking === "cpu" ? 2 : 3)) cell.classList.add("ranked");
                if (index === 1) cell.title = String(value);
                row.append(cell);
            });
            body.append(row);
        }
        if (!rows.length) {
            const cell = document.createElement("td");
            cell.colSpan = 5;
            cell.className = "empty-state";
            cell.textContent = "No visible processes in this sample.";
            const row = document.createElement("tr");
            row.append(cell);
            body.append(row);
        }
        $("process-count").textContent = `${Math.min(rows.length, 50)} PROCESSES`;
    }

    function updateTelemetry(data) {
        if (![data.cpu_percent, data.memory_percent, data.disk_percent, data.active_sockets].every(Number.isFinite)) throw new Error("Invalid telemetry");
        telemetry = data;
        lastSample = Date.now();
        packetCount++;
        for (const metric of ["cpu", "memory", "disk"]) {
            const value = Math.max(0, Math.min(100, data[`${metric}_percent`]));
            $(`${metric}-value`).textContent = value.toFixed(1);
            $(`${metric}-gauge`).value = value;
            $(`${metric}-card`).dataset.severity = value >= 90 ? "critical" : value >= 75 ? "warning" : "normal";
            const history = histories[metric];
            history.push(value);
            if (history.length > 30) history.shift();
            $(`${metric}-spark`).setAttribute("points", history.map((point, index) => `${index * 120 / 29},${38 - point * 0.35}`).join(" "));
        }
        $("sockets-value").textContent = data.active_sockets.toLocaleString();
        $("packet-count").textContent = `${packetCount.toLocaleString()} PACKETS RECEIVED`;
        $("connection-notice").hidden = true;
        $("telemetry-status").dataset.state = "live";
        $("telemetry-status").lastChild.textContent = "SYSTEM ONLINE";
        telemetrySocket.markHealthy(6000);
        renderProcesses();
    }

    const telemetrySocket = new ReconnectingSocket("/ws/telemetry", {
        onMessage: updateTelemetry,
        onState: (state, detail) => {
            $("telemetry-status").dataset.state = "connecting";
            $("telemetry-status").lastChild.textContent = state === "retrying" ? "RECONNECTING" : "CONNECTING";
            if (state === "retrying") {
                $("connection-notice").hidden = false;
                log("system", "LINK", `Telemetry disconnected. Retry in ${(detail / 1000).toFixed(1)}s; displayed readings may be stale.`);
            }
        },
    });

    function syncConfirmation() {
        const seconds = pending ? Math.max(0, Math.ceil((Date.parse(pending.expires_at) - Date.now()) / 1000)) : 0;
        $("confirmation-countdown").textContent = consumed ? "TOKEN CONSUMED" : invalidated ? "REQUEST UNAVAILABLE" : `${seconds}s REMAINING`;
        $("execute-confirmation").disabled = !pending || busy || consumed || invalidated || seconds <= 0;
        $("preview-confirmation").disabled = !pending || busy || consumed || invalidated || seconds <= 0;
        $("cancel-confirmation").disabled = busy;
        if (pending && !consumed && seconds <= 0 && !busy) $("confirmation-feedback").textContent = "This request expired. Issue a new voice command to obtain a fresh preview.";
    }

    function dismissConfirmation() {
        if (busy) return;
        if (pending && !consumed) log("gate", "GATE", "Request dismissed locally. No execution submitted; the server token will expire automatically.");
        pending = null;
        dialog.close();
    }

    function showConfirmation(message) {
        if (!message.token || !Number.isFinite(Date.parse(message.expires_at)) || message.preview?.success !== true || !["PROCESS_KILL", "NETWORK_ISOLATE"].includes(message.intent)) {
            log("error", "GATE", "Rejected an invalid confirmation payload.");
            return;
        }
        if (dialog.open) {
            log("gate", "GATE", "Another request arrived while this review was open. Finish the current review, then repeat the new command.");
            return;
        }
        pending = message;
        consumed = false;
        invalidated = false;
        $("confirmation-intent").textContent = message.intent;
        $("confirmation-target").textContent = `${message.preview.affected_process_name ?? "process"} / PID ${message.preview.affected_pid ?? "unknown"}`;
        $("confirmation-summary").textContent = message.preview.message;
        $("confirmation-warning").textContent = message.intent === "NETWORK_ISOLATE" ? "Scope warning: existing isolation blocks remote IPs host-wide, affecting other applications too. It is not a per-process sandbox. Firewall changes may need administrator privileges and manual removal." : "This terminates the target process and may discard unsaved work. The server rechecks its protected-process policy before execution.";
        $("confirmation-feedback").textContent = "Review the exact target. Voice keywords do not confirm execution.";
        syncConfirmation();
        dialog.showModal();
        $("cancel-confirmation").focus();
        log("gate", "GATE", `${message.intent} queued for operator review. No action executed.`);
    }

    async function confirm(dryRun) {
        if (!pending || busy || consumed || invalidated || Date.parse(pending.expires_at) <= Date.now()) return;
        const request = pending;
        busy = true;
        if (!dryRun) consumed = true;
        syncConfirmation();
        $("confirmation-feedback").textContent = dryRun ? "Evaluating without changing system state…" : "Execution submitted. Waiting for the system result…";
        try {
            const response = await fetch("/api/v1/commands/confirm", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token: request.token, dry_run: dryRun }),
                signal: AbortSignal.timeout(15000),
                credentials: "same-origin",
            });
            const result = await response.json();
            const message = response.ok ? result.message : typeof result.detail === "string" ? result.detail : `Request rejected (HTTP ${response.status}).`;
            $("confirmation-feedback").textContent = message ?? "The server returned an invalid result. Verify host state before issuing another command.";
            log(response.ok && result.success ? "result" : "error", dryRun ? "DRY RUN" : "RESULT", $("confirmation-feedback").textContent);
            if (dryRun && response.ok && result.success) $("confirmation-summary").textContent = result.message;
            if (dryRun && (!response.ok || result.success !== true)) invalidated = true;
        } catch {
            const message = dryRun ? "Dry-run request could not be completed. No execution was requested." : "Execution outcome unknown: the request or response was interrupted. Verify the host before issuing another command. This action will not be retried automatically.";
            $("confirmation-feedback").textContent = message;
            log("error", "RESULT", message);
        } finally {
            busy = false;
            syncConfirmation();
        }
    }

    const uplink = new MicrophoneUplink({
        onMode: (mode, label) => {
            $("voice-status").textContent = label;
            $("voice-status").dataset.state = mode;
            $("mic-toggle").setAttribute("aria-pressed", String(uplink.wanted));
            $("mic-label").textContent = uplink.wanted ? "STOP MICROPHONE" : "ENABLE MICROPHONE";
            $("audio-help").textContent = mode === "live" ? "Streaming live audio to AssemblyAI. Stop the microphone to end transmission." : uplink.wanted ? "Waiting for the transcription service. Audio is not buffered or replayed during reconnects." : "Microphone access is opt-in. Audio streams to AssemblyAI only while the uplink is active.";
        },
        onFormat: text => { $("audio-format").textContent = text; },
        onDisconnect: () => {
            $("partial-transcript").textContent = "Awaiting voice input";
            if (!busy && pending && !consumed) {
                dismissConfirmation();
                log("gate", "GATE", "Voice session ended. Repeat the inspection in a new session before using a follow-up target.");
            }
        },
        onEvent: message => {
            if (message.type === "transcript") {
                if (message.end_of_turn) {
                    log("voice", "VOICE", message.text);
                    $("partial-transcript").textContent = "Awaiting voice input";
                } else $("partial-transcript").textContent = String(message.text).slice(0, 2000);
            } else if (message.type === "confirmation_required") showConfirmation(message);
            else if (message.type === "command_result") {
                log(message.success ? "result" : "error", "INTENT", `${message.intent} / ${message.message}`);
                if (Array.isArray(message.processes)) {
                    for (const process of message.processes.slice(0, 50)) log("result", "PROCESS", `PID ${process.pid} / ${process.name} / CPU ${Number(process.cpu_percent).toFixed(1)}% / RAM ${Number(process.memory_percent).toFixed(1)}%`);
                }
            } else if (message.type === "error") log("error", "ERROR", message.message ?? "Voice service unavailable.");
        },
    });

    $("mic-toggle").addEventListener("click", () => uplink.wanted ? uplink.stop() : void uplink.start());
    $("clear-feed").addEventListener("click", () => { feed.replaceChildren(); log("system", "SYS", "Local display cleared. Server-side state is unchanged."); });
    for (const metric of ["memory", "cpu"]) $("rank-" + metric).addEventListener("click", () => {
        ranking = metric;
        for (const name of ["memory", "cpu"]) $("rank-" + name).setAttribute("aria-pressed", String(name === metric));
        renderProcesses();
    });
    $("cancel-confirmation").addEventListener("click", dismissConfirmation);
    $("preview-confirmation").addEventListener("click", () => void confirm(true));
    $("execute-confirmation").addEventListener("click", () => void confirm(false));
    dialog.addEventListener("cancel", event => { event.preventDefault(); dismissConfirmation(); });

    const canvas = $("waveform");
    const painter = canvas.getContext("2d");
    const samples = new Float32Array(2048);
    const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
    let animation = 0;
    let previousDraw = 0;
    function draw(time) {
        animation = requestAnimationFrame(draw);
        if (document.hidden || time - previousDraw < (reducedMotion.matches ? 200 : 33)) return;
        previousDraw = time;
        const width = canvas.clientWidth;
        const height = canvas.clientHeight;
        const scale = Math.min(devicePixelRatio || 1, 2);
        if (canvas.width !== Math.round(width * scale) || canvas.height !== Math.round(height * scale)) {
            canvas.width = Math.round(width * scale);
            canvas.height = Math.round(height * scale);
        }
        if (!painter) return;
        painter.setTransform(scale, 0, 0, scale, 0, 0);
        painter.clearRect(0, 0, width, height);
        samples.fill(0);
        uplink.analyser?.getFloatTimeDomainData(samples);
        let energy = 0;
        painter.beginPath();
        for (let i = 0; i < samples.length; i += 4) {
            const x = i / (samples.length - 1) * width;
            const y = height / 2 - Math.max(-1, Math.min(1, samples[i] * 2)) * height * 0.44;
            energy += samples[i] ** 2;
            if (i === 0) painter.moveTo(x, y);
            else painter.lineTo(x, y);
        }
        painter.strokeStyle = uplink.analyser ? "#55e5cf" : "#3b6468";
        painter.lineWidth = 1.5;
        painter.stroke();
        $("audio-level").textContent = uplink.analyser ? `${Math.max(-90, 20 * Math.log10(Math.sqrt(energy / 512) || 0.00001)).toFixed(0)} dBFS` : "STANDBY";
    }
    animation = requestAnimationFrame(draw);
    const clock = setInterval(() => {
        $("utc-clock").textContent = `${formatTime()} UTC`;
        if (lastSample) {
            const age = Math.floor((Date.now() - lastSample) / 1000);
            $("sample-age").textContent = age <= 2 ? "LIVE / 1s INTERVAL" : `STALE / ${age}s AGO`;
            if (age > 5) $("connection-notice").hidden = false;
        }
        syncConfirmation();
    }, 500);
    addEventListener("pagehide", () => {
        uplink.stop();
        telemetrySocket.stop();
        cancelAnimationFrame(animation);
        clearInterval(clock);
    });
    addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
    log("system", "SYS", "VoiceOps incident console initialized. Waiting for host telemetry.");
    log("gate", "POLICY", "Protected-process policy active. All mutations require explicit operator confirmation.");
    log("system", "AUDIO", "Microphone is off. Enable the uplink to begin a private-to-this-session conversation.");
    telemetrySocket.start();
}

if (typeof window !== "undefined" && typeof document !== "undefined") bootDashboard();
