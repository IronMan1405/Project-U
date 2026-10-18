// AudioWorklet for wake-word standby: forwards the microphone's samples to the page in
// batches (about 40 ms at 48 kHz) so it can keep a short ring buffer and measure the level.
// The samples stay in the page; only short clips the detector selects are ever uploaded,
// and only to the local K.R.I.S. server.
class WakeCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.batch = new Float32Array(2048);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) {
      for (let index = 0; index < channel.length; index += 1) {
        this.batch[this.filled++] = channel[index];
        if (this.filled === this.batch.length) {
          this.port.postMessage(this.batch.slice());
          this.filled = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("wake-capture", WakeCaptureProcessor);
