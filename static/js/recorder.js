/* Microphone capture, encoded the one way PyWhispr can read it.
 *
 * PyWhispr accepts wav and headerless PCM only — its own wav.py says browser
 * MediaRecorder output "cannot be decoded without ffmpeg; web clients should
 * send raw float32 PCM from an AudioContext instead". So we take the Web Audio
 * route: collect Float32 samples, resample to 16 kHz, and send signed 16-bit
 * little-endian PCM. s16le at 16 kHz is ~32 KB/s, about a twelfth of raw 48 kHz
 * float32, which matters a lot over a phone connection.
 */

const TARGET_SAMPLE_RATE = 16000;

window.Recorder = (function () {
    'use strict';

    let stream = null;
    let context = null;
    let source = null;
    let node = null;
    let mute = null;
    let chunks = [];
    let frames = 0;
    let recording = false;
    let onLevel = null;
    let workletLoaded = false;

    /** getUserMedia only exists in a secure context: HTTPS, or localhost. */
    function isSupported() {
        return Boolean(window.isSecureContext && navigator.mediaDevices &&
            navigator.mediaDevices.getUserMedia && (window.AudioContext || window.webkitAudioContext));
    }

    function secondsRecorded() {
        return context && frames ? frames / context.sampleRate : 0;
    }

    function handleChunk(event) {
        if (!recording) { return; }
        chunks.push(event.data.samples);
        frames += event.data.samples.length;
        if (onLevel) { onLevel(event.data.rms); }
    }

    /* AudioWorklet runs the capture off the main thread and is the modern path.
     * ScriptProcessorNode is deprecated but is the only fallback on older
     * WebViews, and a deprecated recorder beats no recorder. */
    async function createCaptureNode() {
        if (context.audioWorklet) {
            try {
                // Once per context: the context now outlives a single clip, and
                // re-registering the same processor name would throw.
                if (!workletLoaded) {
                    await context.audioWorklet.addModule('/static/js/capture-worklet.js');
                    workletLoaded = true;
                }
                const worklet = new AudioWorkletNode(context, 'capture-processor');
                worklet.port.onmessage = handleChunk;
                return worklet;
            } catch (err) {
                console.warn('AudioWorklet unavailable, falling back to ScriptProcessor', err);
            }
        }

        const processor = context.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = function (event) {
            const input = event.inputBuffer.getChannelData(0);
            let sumSquares = 0;
            for (let i = 0; i < input.length; i++) { sumSquares += input[i] * input[i]; }
            handleChunk({data: {samples: new Float32Array(input), rms: Math.sqrt(sumSquares / input.length)}});
        };
        return processor;
    }

    function streamIsLive() {
        return Boolean(stream) && stream.getTracks().some(function (t) { return t.readyState === 'live'; });
    }

    /* Ask for the microphone once per page load and hold onto it. Stopping the
     * tracks between clips makes the browser treat the next getUserMedia as a
     * fresh request, which is a permission prompt every single time on a phone.
     * We mute the tracks instead while idle, and only really let go on unload. */
    async function acquireStream() {
        if (streamIsLive()) {
            stream.getTracks().forEach(function (t) { t.enabled = true; });
            return;
        }
        stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            },
        });
    }

    async function acquireContext() {
        // Don't request a sampleRate here: iOS ignores it and reports whatever
        // the hardware gives, so we resample at the end instead.
        if (!context || context.state === 'closed') {
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            context = new AudioContextClass();
            workletLoaded = false;
        }
        // Safari starts contexts suspended until a user gesture unlocks them,
        // and we suspend it ourselves between clips.
        if (context.state === 'suspended') { await context.resume(); }
    }

    async function start(levelCallback) {
        if (recording) { return; }
        onLevel = levelCallback || null;
        chunks = [];
        frames = 0;

        await acquireStream();
        await acquireContext();

        source = context.createMediaStreamSource(stream);
        node = await createCaptureNode();
        source.connect(node);

        // ScriptProcessorNode only fires while connected to a destination. A
        // zero gain keeps it pumping without echoing the mic to the speakers.
        mute = context.createGain();
        mute.gain.value = 0;
        node.connect(mute);
        mute.connect(context.destination);

        recording = true;
    }

    /* Tear the graph down and mute the mic, but keep the stream and the context
     * so the next clip needs no permission prompt and no worklet reload. */
    function releaseHardware() {
        if (node) { try { node.disconnect(); } catch (e) { /* already gone */ } }
        if (source) { try { source.disconnect(); } catch (e) { /* already gone */ } }
        if (mute) { try { mute.disconnect(); } catch (e) { /* already gone */ } }
        if (stream) { stream.getTracks().forEach(function (t) { t.enabled = false; }); }
        if (context && context.state === 'running') { context.suspend(); }
        node = source = mute = null;
    }

    /** Hand the microphone back for good — for page unload. */
    function release() {
        releaseHardware();
        if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); }
        if (context && context.state !== 'closed') { context.close(); }
        stream = context = null;
        workletLoaded = false;
    }

    function concatenate(buffers, total) {
        const merged = new Float32Array(total);
        let offset = 0;
        for (let i = 0; i < buffers.length; i++) {
            merged.set(buffers[i], offset);
            offset += buffers[i].length;
        }
        return merged;
    }

    /* OfflineAudioContext resamples properly (band-limited); the linear
     * interpolation fallback aliases a little but keeps us working on engines
     * that refuse a 16 kHz context. */
    async function resample(samples, fromRate) {
        if (fromRate === TARGET_SAMPLE_RATE) { return samples; }
        const length = Math.max(1, Math.round(samples.length * TARGET_SAMPLE_RATE / fromRate));

        const OfflineContextClass = window.OfflineAudioContext || window.webkitOfflineAudioContext;
        if (OfflineContextClass) {
            try {
                const offline = new OfflineContextClass(1, length, TARGET_SAMPLE_RATE);
                const buffer = offline.createBuffer(1, samples.length, fromRate);
                buffer.copyToChannel ? buffer.copyToChannel(samples, 0)
                                     : buffer.getChannelData(0).set(samples);
                const bufferSource = offline.createBufferSource();
                bufferSource.buffer = buffer;
                bufferSource.connect(offline.destination);
                bufferSource.start();
                const rendered = await offline.startRendering();
                return rendered.getChannelData(0);
            } catch (err) {
                console.warn('OfflineAudioContext resample failed, interpolating instead', err);
            }
        }

        const out = new Float32Array(length);
        const ratio = (samples.length - 1) / Math.max(1, length - 1);
        for (let i = 0; i < length; i++) {
            const position = i * ratio;
            const index = Math.floor(position);
            const next = Math.min(index + 1, samples.length - 1);
            const fraction = position - index;
            out[i] = samples[index] * (1 - fraction) + samples[next] * fraction;
        }
        return out;
    }

    function toInt16(samples) {
        const out = new Int16Array(samples.length);
        for (let i = 0; i < samples.length; i++) {
            // Clamp before scaling, so a hot mic clips rather than wrapping
            // round to the opposite sign.
            const clamped = Math.max(-1, Math.min(1, samples[i]));
            out[i] = Math.round(clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff);
        }
        return out;
    }

    /** Stop, and return the recording as 16 kHz mono s16le, or null if silent. */
    async function stop() {
        if (!recording) { return null; }
        recording = false;
        onLevel = null;

        const sourceRate = context.sampleRate;
        const collected = concatenate(chunks, frames);
        chunks = [];
        frames = 0;
        releaseHardware();

        if (collected.length === 0) { return null; }

        const resampled = await resample(collected, sourceRate);
        return {
            pcm: toInt16(resampled),
            sampleRate: TARGET_SAMPLE_RATE,
            seconds: collected.length / sourceRate,
        };
    }

    /** Abandon a recording without producing audio. */
    function cancel() {
        if (!recording) { return; }
        recording = false;
        onLevel = null;
        chunks = [];
        frames = 0;
        releaseHardware();
    }

    return {
        isSupported: isSupported,
        isRecording: function () { return recording; },
        secondsRecorded: secondsRecorded,
        start: start,
        stop: stop,
        cancel: cancel,
        release: release,
        targetSampleRate: TARGET_SAMPLE_RATE,
    };
})();
