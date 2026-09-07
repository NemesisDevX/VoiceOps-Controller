import test from "node:test";
import assert from "node:assert/strict";
import { PCM16Encoder, reconnectDelay } from "../app/static/app.js";

function encode(rate, length, signal, blockSize = 128) {
    const frames = [];
    const encoder = new PCM16Encoder(rate, frame => frames.push(frame));
    for (let offset = 0; offset < length; offset += blockSize) {
        const size = Math.min(blockSize, length - offset);
        encoder.push([Float32Array.from({ length: size }, (_, i) => signal(offset + i))]);
    }
    return { frames, encoder };
}

for (const rate of [16000, 44100, 48000, 96000]) {
    test(`${rate} Hz produces continuous 100 ms PCM16 frames without sample drift`, () => {
        const length = rate * 10;
        const { frames, encoder } = encode(rate, length, () => 0);
        const expected = Math.ceil((length - encoder.half) / (rate / 16000));
        assert.equal(encoder.outputCount, expected);
        assert.equal(frames.length, Math.floor(expected / 1600));
        assert.ok(frames.every(frame => frame.byteLength === 3200));
        assert.ok(frames.every(frame => new Uint8Array(frame).every(value => value === 0)));
    });
}

test("resampling is independent of input block boundaries", () => {
    const signal = n => Math.sin(2 * Math.PI * 1000 * n / 44100);
    const a = encode(44100, 44100, signal, 128);
    const b = encode(44100, 44100, signal, 317);
    assert.deepEqual(a.frames.map(f => new Uint8Array(f)), b.frames.map(f => new Uint8Array(f)));
});

test("stereo channels are mixed to mono, not interleaved", () => {
    const frames = [];
    const encoder = new PCM16Encoder(48000, frame => frames.push(frame));
    for (let i = 0; i < 400; i++) {
        encoder.push([new Float32Array(128).fill(0.8), new Float32Array(128).fill(-0.8)]);
    }
    assert.ok(frames.length > 0);
    assert.ok(frames.every(frame => new Uint8Array(frame).every(value => value === 0)));
});

test("signed PCM clips safely and serializes little-endian", () => {
    for (const [amplitude, expected] of [[2, 32767], [-2, -32768]]) {
        const { frames } = encode(48000, 9600, () => amplitude);
        const frame = new DataView(frames[0]);
        assert.equal(frame.getInt16(2000, true), expected);
        assert.deepEqual([...new Uint8Array(frames[0]).slice(2000, 2002)], amplitude > 0 ? [255, 127] : [0, 128]);
    }
});

test("low-pass filter suppresses out-of-band audio before downsampling", () => {
    const rms = hz => {
        const { frames } = encode(48000, 48000, n => Math.sin(2 * Math.PI * hz * n / 48000));
        const samples = new DataView(frames[4]);
        let energy = 0;
        for (let i = 0; i < 1600; i++) energy += (samples.getInt16(i * 2, true) / 32768) ** 2;
        return Math.sqrt(energy / 1600);
    };
    assert.ok(rms(1000) > 0.65);
    assert.ok(rms(12000) < 0.02);
});

test("reset discards partial frames so disconnected audio is never replayed", () => {
    const frames = [];
    const encoder = new PCM16Encoder(48000, frame => frames.push(frame));
    encoder.push([new Float32Array(3000).fill(1)]);
    encoder.reset();
    encoder.push([new Float32Array(6000)]);
    assert.equal(frames.length, 1);
    assert.ok(new Uint8Array(frames[0]).every(value => value === 0));
});

test("invalid sample rates are rejected", () => {
    for (const rate of [0, NaN, 8000, 1000000]) assert.throws(() => new PCM16Encoder(rate, () => {}));
});

test("reconnect uses bounded exponential backoff with jitter", () => {
    assert.equal(reconnectDelay(0, () => 0), 500);
    assert.equal(reconnectDelay(1, () => 0), 1000);
    assert.equal(reconnectDelay(5, () => 0), 16000);
    assert.equal(reconnectDelay(40, () => 1), 30000);
    assert.ok(reconnectDelay(2, () => 0.5) > reconnectDelay(2, () => 0));
});
