// Painel lateral: mostra a transcrição ao vivo, envia as legendas do Meet e finaliza.

const falas = document.getElementById("falas");
const parciais = document.getElementById("parciais");
const estado = document.getElementById("estado");
const erro = document.getElementById("erro");
const botaoParar = document.getElementById("parar");

const parcialPorCanal = {};
let legendasPendentes = [];
let captura = null;

async function carregarCaptura() {
  ({ captura } = await chrome.storage.session.get("captura"));
  botaoParar.hidden = !captura;
  if (captura) estado.textContent = `Transcrevendo: ${captura.titulo || "reunião"}`;
}
carregarCaptura();
chrome.storage.session.onChanged.addListener(carregarCaptura);

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.alvo !== "painel") return;
  if (msg.tipo === "trecho") mostrarTrecho(msg);
  if (msg.tipo === "legenda" && captura) {
    legendasPendentes.push({
      falante: msg.falante,
      texto: msg.texto,
      inicio_ms: Math.max(0, msg.momento - captura.inicio),
    });
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

botaoParar.addEventListener("click", async () => {
  if (!captura) return;
  botaoParar.disabled = true;
  estado.textContent = "Encerrando e salvando...";
  chrome.runtime.sendMessage({ alvo: "offscreen", tipo: "parar" });
  // O serviço ainda devolve os últimos segundos depois que o áudio para.
  await new Promise((r) => setTimeout(r, 4000));
  await enviarLegendas();
  try {
    const titulo = (captura.titulo || "").replace(/\s*-\s*Google Meet$/, "") || null;
    await backend(`/transcricoes/${captura.sessao}/finalizar`, {
      titulo,
      url_reuniao: captura.url,
    });
    const { backendUrl } = await chrome.storage.sync.get("backendUrl");
    estado.innerHTML = "";
    const link = document.createElement("a");
    link.href = `${backendUrl.replace(/\/$/, "")}/transcricoes/${captura.sessao}`;
    link.target = "_blank";
    link.textContent = "Salvo. Ver comparação com as legendas do Meet";
    estado.append(link);
  } catch (e) {
    estado.textContent = `Não foi possível salvar: ${e.message}. Os trechos já estão no servidor.`;
  }
  await chrome.offscreen.closeDocument().catch(() => {});
  await chrome.storage.session.remove("captura");
  botaoParar.disabled = false;
});
