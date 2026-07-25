/* Pulls raw microphone samples off the audio thread.
 *
 * PyWhispr has no codec support, so MediaRecorder (WebM/Opus) is unusable and
 * we have to collect PCM ourselves. This posts each render quantum's samples to
 * the main thread along with its RMS, which drives the level indicator. */

class CaptureProcessor extends AudioWorkletProcessor {
    process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (!channel || channel.length === 0) {
            // No input connected yet; keep the processor alive.
            return true;
        }

        let sumSquares = 0;
        for (let i = 0; i < channel.length; i++) {
            sumSquares += channel[i] * channel[i];
        }

        // The buffer is recycled by the audio engine, so post a copy.
        const samples = new Float32Array(channel);
        this.port.postMessage(
            {samples: samples, rms: Math.sqrt(sumSquares / channel.length)},
            [samples.buffer],
        );
        return true;
    }
}

registerProcessor('capture-processor', CaptureProcessor);
