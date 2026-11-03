// Documento invisível que captura o áudio e envia ao backend em PCM 16 kHz.
// Canal "outros": áudio da aba do Meet. Canal "voce": seu microfone.
//
// Várias capturas ao mesmo tempo (uma por aba do Meet), cada uma com seu id ("sessao") e
// suas duas conexões. O microfone é um só: ele só vai para uma reunião quando você está
// com o microfone aberto no Meet daquela aba e não o desligou no painel. Enquanto não vai,
// a conexão do microfone recebe "manter" a cada 5 s em vez de áudio (não custa nada no
// serviço de transcrição e a conexão não cai).

const MANTER_MS = 5000;

let contexto = null; // AudioContext de 16 kHz que converte para o serviço
let reproducao = null; // AudioContext que devolve o som da aba para você ouvir
const capturas = new Map(); // sessao -> captura

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.alvo === "offscreen") {
    if (msg.tipo === "iniciar") iniciar(msg).catch((e) => avisar(msg.sessao, `Falha ao iniciar: ${e.message}`));
    if (msg.tipo === "parar") parar(msg.sessao);
    if (msg.tipo === "microfone") ligarMicrofone(msg.sessao, msg.ligado);
    if (msg.tipo === "estado") for (const cap of capturas.values()) informarMicrofone(cap);
  }
  // O Meet de uma aba avisou que o microfone foi aberto ou fechado (legendas.js).
  if (msg.alvo === "extensao" && msg.tipo === "mudo" && sender.tab) {
    for (const cap of capturas.values()) {
      if (cap.tabId === sender.tab.id && cap.mudo !== msg.mudo) {
        cap.mudo = msg.mudo;
        informarMicrofone(cap);
      }
    }
  }
});

async function iniciar({ streamId, sessao, backendUrl, tabId }) {
  const cap = { sessao, tabId, partes: [], desligado: false, mudo: false, timer: null };
  capturas.set(sessao, cap);

  const aba = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
  });
  // Capturar a aba silencia o som dela; devolvemos o áudio para você continuar ouvindo.
  reproducao = reproducao || new AudioContext();
  const saida = reproducao.createMediaStreamSource(aba);
  saida.connect(reproducao.destination);
  cap.partes.push({ fonte: saida, stream: aba });

  // O navegador converte para 16 kHz, a taxa que o serviço de transcrição espera.
  if (!contexto) {
    contexto = new AudioContext({ sampleRate: 16000 });
    await contexto.audioWorklet.addModule("pcm-worklet.js");
  }
  conectar(aba, "outros", cap, backendUrl, () => true);

  try {
    const microfone = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    const ws = conectar(microfone, "voce", cap, backendUrl, () => microfoneVai(cap));
    // Sem áudio indo, a conexão recebe "manter" para não cair.
    cap.timer = setInterval(() => {
      if (!microfoneVai(cap) && ws.readyState === WebSocket.OPEN) ws.send("manter");
    }, MANTER_MS);
  } catch (e) {
    avisar(sessao, "Microfone sem permissão: abra as opções da extensão e clique em Autorizar microfone.");
  }
  informarMicrofone(cap);
}

// O microfone vai para esta reunião?
function microfoneVai(cap) {
  return !cap.desligado && !cap.mudo;
}

function conectar(stream, canal, cap, backendUrl, enviando) {
  const base = backendUrl.replace(/^http/, "ws").replace(/\/$/, "");
  const ws = new WebSocket(`${base}/ws/transcricao?sessao=${cap.sessao}&canal=${canal}`);
  ws.binaryType = "arraybuffer";

  const fonte = contexto.createMediaStreamSource(stream);
  const conversor = new AudioWorkletNode(contexto, "pcm16");
  conversor.port.onmessage = (e) => {
    if (ws.readyState === WebSocket.OPEN && enviando()) ws.send(e.data);
  };
  fonte.connect(conversor);
  conversor.connect(contexto.destination); // saída é silêncio; mantém o conversor ativo

  ws.onmessage = (e) =>
    chrome.runtime.sendMessage({ alvo: "painel", sessao: cap.sessao, ...JSON.parse(e.data) }).catch(() => {});
  ws.onerror = () => avisar(cap.sessao, `Conexão do canal "${canal}" com o servidor falhou.`);
  cap.partes.push({ ws, stream, fonte, conversor });
  return ws;
}

function ligarMicrofone(sessao, ligado) {
  const cap = capturas.get(sessao);
  if (!cap) return;
  cap.desligado = !ligado;
  informarMicrofone(cap);
}

// Conta ao painel da aba se o seu microfone está indo para esta reunião.
function informarMicrofone(cap) {
  chrome.runtime
    .sendMessage({ alvo: "painel", tipo: "microfone", sessao: cap.sessao, vai: microfoneVai(cap), mudo: cap.mudo, desligado: cap.desligado })
    .catch(() => {});
}

function parar(sessao) {
  const cap = capturas.get(sessao);
  if (!cap) return;
  clearInterval(cap.timer);
  for (const { ws, stream, fonte, conversor } of cap.partes) {
    fonte?.disconnect();
    conversor?.disconnect();
    stream?.getTracks().forEach((t) => t.stop());
    ws?.close();
  }
  capturas.delete(sessao);
  // Última captura: libera o áudio.
  if (capturas.size === 0) {
    contexto?.close();
    reproducao?.close();
    contexto = null;
    reproducao = null;
  }
}

function avisar(sessao, mensagem) {
  chrome.runtime.sendMessage({ alvo: "painel", tipo: "erro", sessao, mensagem }).catch(() => {});
}
