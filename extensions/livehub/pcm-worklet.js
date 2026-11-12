// 把输入拆成 16k 单声道 PCM16，按 4096 采样（256ms）一块发出，附 RMS。
// 与网页版 app/web/index.html 里的 worklet 保持同样的块长，服务端只认这一种。
class PCM16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Int16Array(4096);
    this.n = 0;
    this.acc = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      this.acc += s * s;
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n >= this.buf.length) {
        const rms = Math.sqrt(this.acc / this.buf.length);
        this.acc = 0;
        this.port.postMessage({ pcm: this.buf.buffer, rms }, [this.buf.buffer]);
        this.buf = new Int16Array(4096);
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm16", PCM16);
