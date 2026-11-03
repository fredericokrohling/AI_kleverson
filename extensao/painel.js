// Painel lateral de UMA aba do Meet: mostra a transcrição dela ao vivo, envia as legendas
// do Meet daquela aba, liga e desliga o seu microfone nesta reunião e finaliza.
//
// Com duas reuniões ao mesmo tempo, cada aba tem o seu painel; o id da aba vem no endereço
// do painel (painel.html?aba=123), e tudo que chega é filtrado pela captura desta aba.

const falas = document.getElementById("falas");
const parciais = document.getElementById("parciais");
const estado = document.getElementById("estado");
const erro = document.getElementById("erro");
const botaoParar = document.getElementById("parar");
const botaoMic = document.getElementById("mic");
const estadoMic = document.getElementById("estado-mic");

const parcialPorCanal = {};
let legendasPendentes = [];
let captura = null;
let aba = Number(new URLSearchParams(location.search).get("aba")) || null;

async function carregarCaptura() {
  // Painel aberto pelo menu do Chrome (sem o id no endereço): usa a aba ativa.
  if (!aba) {
    const [ativa] = await chrome.tabs.query({ active: true, currentWindow: true });
    aba = ativa ? ativa.id : null;
  }
  const { capturas } = await chrome.storage.session.get("capturas");
  captura = (capturas || {})[aba] || null;
  botaoParar.hidden = !captura;
  botaoMic.hidden = !captura;
  if (captura) {
    estado.textContent = `Transcrevendo: ${captura.titulo || "reunião"}`
      + (captura.empresa ? ` · Empresa ${captura.empresa}` : " · Empresa não identificada: escolha no Início do console");
    botaoMic.textContent = captura.microfone ? "Desligar meu microfone aqui" : "Ligar meu microfone aqui";
    // Pede ao documento de áudio a situação do microfone.
    chrome.runtime.sendMessage({ alvo: "offscreen", tipo: "estado" }).catch(() => {});
  }
}
carregarCaptura();
chrome.storage.session.onChanged.addListener(carregarCaptura);

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.alvo !== "painel") return;
  // Legenda: só as lidas na aba deste painel.
  if (msg.tipo === "legenda") {
    if (captura && sender.tab && sender.tab.id === aba) {
      legendasPendentes.push({
        falante: msg.falante,
        texto: msg.texto,
        inicio_ms: Math.max(0, msg.momento - captura.inicio),
      });
    }
    return;
  }
  // O resto vem do documento de áudio, com o id da captura: só os desta reunião.
  if (!captura || msg.sessao !== captura.sessao) return;
  if (msg.tipo === "trecho") mostrarTrecho(msg);
  if (msg.tipo === "microfone") {
    estadoMic.textContent = msg.vai
      ? "Seu microfone está indo para esta reunião."
      : msg.desligado
        ? "Seu microfone está desligado aqui."
        : "Mudo no Meet: seu microfone não vai para esta reunião.";
  }
  if (msg.tipo === "erro") {
    erro.hidden = false;
    erro.textContent = msg.mensagem;
  }
});

function mostrarTrecho({ canal, falante, texto, final }) {
  if (final) {
    const div = document.createElement("div");
    div.className = "fala";
    const nome = document.createElement("b");
    nome.textContent = falante;
    div.append(nome, texto);
    falas.append(div);
    delete parcialPorCanal[canal];
  } else {
    parcialPorCanal[canal] = `${falante}: ${texto}`;
  }
  parciais.textContent = Object.values(parcialPorCanal).join("\n");
  window.scrollTo(0, document.body.scrollHeight);
}

async function backend(caminho, corpo) {
  const { backendUrl } = await chrome.storage.sync.get("backendUrl");
  const resposta = await fetch(`${backendUrl.replace(/\/$/, "")}${caminho}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corpo),
  });
  if (!resposta.ok) throw new Error(`Servidor respondeu ${resposta.status}`);
  return resposta.json();
}

async function enviarLegendas() {
  if (!captura || legendasPendentes.length === 0) return;
  const lote = legendasPendentes;
  legendasPendentes = [];
  try {
    await backend(`/transcricoes/${captura.sessao}/legendas`, { trechos: lote });
  } catch (e) {
    legendasPendentes = lote.concat(legendasPendentes); // tenta de novo no próximo ciclo
  }
}
setInterval(enviarLegendas, 10000);

// Liga ou desliga o seu microfone só nesta reunião (a outra continua como está).
botaoMic.addEventListener("click", async () => {
  if (!captura) return;
  const { capturas } = await chrome.storage.session.get("capturas");
  const atual = (capturas || {})[aba];
  if (!atual) return;
  atual.microfone = !atual.microfone;
  await chrome.storage.session.set({ capturas });
  chrome.runtime.sendMessage({ alvo: "offscreen", tipo: "microfone", sessao: atual.sessao, ligado: atual.microfone });
});

botaoParar.addEventListener("click", async () => {
  if (!captura) return;
  const esta = captura;
  botaoParar.disabled = true;
  estado.textContent = "Encerrando e salvando...";
  chrome.runtime.sendMessage({ alvo: "offscreen", tipo: "parar", sessao: esta.sessao });
  // O serviço ainda devolve os últimos segundos depois que o áudio para.
  await new Promise((r) => setTimeout(r, 4000));
  await enviarLegendas();
  try {
    const titulo = (esta.titulo || "").replace(/\s*-\s*Google Meet$/, "") || null;
    const resposta = await backend(`/transcricoes/${esta.sessao}/finalizar`, { titulo, url_reuniao: esta.url });
    const { backendUrl } = await chrome.storage.sync.get("backendUrl");
    const base = backendUrl.replace(/\/$/, "");
    estado.innerHTML = "";
    const link = document.createElement("a");
    link.target = "_blank";
    // Sem Empresa, o console guarda a reunião mas só salva no banco depois que você escolhe.
    if (resposta && resposta.pendente === "empresa") {
      link.href = `${base}/`;
      link.textContent = "Encerrada. Escolha a Empresa no Início do console para salvar";
    } else {
      link.href = `${base}/transcricoes/${esta.sessao}`;
      link.textContent = "Salvo. Ver a transcrição e acertar os participantes";
    }
    estado.append(link);
  } catch (e) {
    estado.textContent = `Não foi possível salvar: ${e.message}. Os trechos já estão no servidor.`;
  }
  // Tira esta captura da lista; fecha o documento de áudio só se não sobrar nenhuma.
  const { capturas } = await chrome.storage.session.get("capturas");
  const restantes = capturas || {};
  delete restantes[aba];
  await chrome.storage.session.set({ capturas: restantes });
  if (Object.keys(restantes).length === 0) await chrome.offscreen.closeDocument().catch(() => {});
  botaoParar.disabled = false;
});
