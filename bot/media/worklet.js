// AudioWorklet: turns s16le interleaved stereo PCM chunks (posted from the main
// thread) into the audio graph's output. Ring-buffered; underruns play silence.
class PcmFeeder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = 48000 * 4; // 4s of frames
    this.left = new Float32Array(this.capacity);
    this.right = new Float32Array(this.capacity);
    this.readPos = 0;
    this.writePos = 0;
    this.available = 0;
    this.consumedFrames = 0;
    this.port.onmessage = (e) => {
      const int16 = new Int16Array(e.data);
      const frames = int16.length / 2;
      for (let i = 0; i < frames; i++) {
        if (this.available >= this.capacity) break; // overflow: drop
        this.left[this.writePos] = int16[i * 2] / 32768;
        this.right[this.writePos] = int16[i * 2 + 1] / 32768;
        this.writePos = (this.writePos + 1) % this.capacity;
        this.available++;
      }
    };
    this._tick = 0;
  }

  process(_inputs, outputs) {
    const out = outputs[0];
    const n = out[0].length;
    for (let i = 0; i < n; i++) {
      if (this.available > 0) {
        out[0][i] = this.left[this.readPos];
        out[1][i] = this.right[this.readPos];
        this.readPos = (this.readPos + 1) % this.capacity;
        this.available--;
      } else {
        out[0][i] = 0;
        out[1][i] = 0;
      }
    }
    this.consumedFrames += n;
    if (++this._tick % 40 === 0) { // ~every 100ms at 128-frame quanta
      this.port.postMessage({ consumed: this.consumedFrames, buffered: this.available });
    }
    return true;
  }
}
registerProcessor("pcm-feeder", PcmFeeder);
