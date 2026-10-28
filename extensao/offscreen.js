// Documento invisível que captura o áudio e envia ao backend em PCM 16 kHz.
// Canal "outros": áudio da aba do Meet. Canal "voce": seu microfone.

let contexto = null;
let reproducao = null;
const capturas = [];

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.alvo !== "offscreen") return;
  if (msg.tipo === "iniciar") iniciar(msg).catch((e) => avisar(`Falha ao iniciar: ${e.message}`));
  if (msg.tipo === "parar") parar();
});

async function iniciar({ streamId, sessao, backendUrl }) {
  const aba = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
  });
  // Capturar a aba silencia o som dela; devolvemos o áudio para você continuar ouvindo.
  reproducao = new AudioContext();
  reproducao.createMediaStreamSource(aba).connect(reproducao.destination);

  // O navegador converte para 16 kHz, a taxa que o serviço de transcrição espera.
  contexto = new AudioContext({ sampleRate: 16000 });
  await contexto.audioWorklet.addModule("pcm-worklet.js");
  conectar(aba, "outros", sessao, backendUrl);

  try {
    const microfone = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    conectar(microfone, "voce", sessao, backendUrl);
  } catch (e) {
    avisar("Microfone sem permissão: abra as opções da extensão e clique em Autorizar microfone.");
  }
}

function conectar(stream, canal, sessao, backendUrl) {
  const base = backendUrl.replace(/^http/, "ws").replace(/\/$/, "");
  const ws = new WebSocket(`${base}/ws/transcricao?sessao=${sessao}&canal=${canal}`);
  ws.binaryType = "arraybuffer";

  const fonte = contexto.createMediaStreamSource(stream);
  const conversor = new AudioWorkletNode(contexto, "pcm16");
  conversor.port.onmessage = (e) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(e.data);
  };
  fonte.connect(conversor);
  conversor.connect(contexto.destination); // saída é silêncio; mantém o conversor ativo

  ws.onmessage = (e) =>
    chrome.runtime.sendMessage({ alvo: "painel", ...JSON.parse(e.data) }).catch(() => {});
  ws.onerror = () => avisar(`Conexão do canal "${canal}" com o servidor falhou.`);
  capturas.push({ ws, stream, fonte, conversor });
}

function parar() {
  for (const { ws, stream, fonte, conversor } of capturas) {
    fonte.disconnect();
    conversor.disconnect();
    stream.getTracks().forEach((t) => t.stop());
    ws.close();
  }
  capturas.length = 0;
  contexto?.close();
  reproducao?.close();
}

function avisar(mensagem) {
  chrome.runtime.sendMessage({ alvo: "painel", tipo: "erro", mensagem }).catch(() => {});
}
