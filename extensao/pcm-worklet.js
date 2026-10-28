// Converte o áudio (float) para PCM 16 bits e envia em blocos de 100 ms.
class Pcm16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.bloco = new Int16Array(sampleRate / 10);
    this.n = 0;
  }

  process(entradas) {
    const canal = entradas[0][0];
    if (canal) {
      for (let i = 0; i < canal.length; i++) {
        const s = Math.max(-1, Math.min(1, canal[i]));
        this.bloco[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
        if (this.n === this.bloco.length) {
          this.port.postMessage(this.bloco.buffer.slice(0));
          this.n = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm16", Pcm16);
