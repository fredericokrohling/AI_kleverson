// Clique no ícone da extensão numa aba do Meet: abre o painel e começa a captura daquela aba.
// A captura da aba precisa partir deste clique (é a permissão do usuário).
//
// Várias reuniões ao mesmo tempo: cada aba do Meet capturada tem sua própria captura, com
// um id próprio ("sessao"). O servidor e o Início do console separam tudo por esse id.
// As capturas em andamento ficam em chrome.storage.session.capturas, por id da aba.

// Lê as capturas em andamento ({ idDaAba: captura }).
async function lerCapturas() {
  const { capturas } = await chrome.storage.session.get("capturas");
  return capturas || {};
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.url || !tab.url.startsWith("https://meet.google.com/")) {
    chrome.runtime.openOptionsPage();
    return;
  }
  // Painel desta aba; o endereço leva o id da aba, para o painel saber qual reunião mostrar.
  // As duas chamadas saem já, sem esperar: o Chrome só abre o painel dentro do clique.
  chrome.sidePanel.setOptions({ tabId: tab.id, path: `painel.html?aba=${tab.id}`, enabled: true });
  chrome.sidePanel.open({ tabId: tab.id });

  const capturas = await lerCapturas();
  if (capturas[tab.id]) return; // esta aba já está sendo transcrita; só reabre o painel

  const { backendUrl } = await chrome.storage.sync.get("backendUrl");
  if (!backendUrl) {
    chrome.runtime.openOptionsPage();
    return;
  }

  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  await garantirOffscreen();
  const sessao = crypto.randomUUID();
  capturas[tab.id] = {
    sessao,
    tabId: tab.id,
    url: tab.url,
    titulo: tab.title,
    inicio: Date.now(),
    microfone: true, // você pode desligar no painel; mudo no Meet também não envia
  };
  await chrome.storage.session.set({ capturas });
  chrome.runtime.sendMessage({ alvo: "offscreen", tipo: "iniciar", streamId, sessao, backendUrl, tabId: tab.id });
  // Avisa o console: o Início abre a área desta reunião, com o título da aba.
  avisarServidor(backendUrl, `/transcricoes/${sessao}/iniciar`, { titulo: tab.title, url_reuniao: tab.url });
});

// Aba do Meet fechada sem "Parar e salvar": para a captura e salva o que já foi dito.
chrome.tabs.onRemoved.addListener(async (tabId) => {
  const capturas = await lerCapturas();
  const captura = capturas[tabId];
  if (!captura) return;
  delete capturas[tabId];
  await chrome.storage.session.set({ capturas });
  chrome.runtime.sendMessage({ alvo: "offscreen", tipo: "parar", sessao: captura.sessao }).catch(() => {});
  const { backendUrl } = await chrome.storage.sync.get("backendUrl");
  // O serviço ainda devolve os últimos segundos depois que o áudio para.
  setTimeout(() => {
    const titulo = (captura.titulo || "").replace(/\s*-\s*Google Meet$/, "") || null;
    avisarServidor(backendUrl, `/transcricoes/${captura.sessao}/finalizar`, { titulo, url_reuniao: captura.url });
    if (Object.keys(capturas).length === 0) chrome.offscreen.closeDocument().catch(() => {});
  }, 4000);
});

// POST em JSON para o console; falha é ignorada (o Início se acerta na próxima mudança).
function avisarServidor(backendUrl, caminho, corpo) {
  if (!backendUrl) return;
  fetch(`${backendUrl.replace(/\/$/, "")}${caminho}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corpo),
  }).catch(() => {});
}

async function garantirOffscreen() {
  const existentes = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (existentes.length > 0) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Capturar o áudio da reunião e do microfone para transcrição.",
  });
}
