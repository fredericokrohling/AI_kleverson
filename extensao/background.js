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

  const sessao = crypto.randomUUID();
  // Avisa o console ANTES de abrir o áudio: ele cria a reunião e procura a Empresa na
  // agenda de cada conta. O idioma da transcrição é o da Empresa, e é escolhido quando o
  // áudio conecta; por isso o áudio só começa depois desta resposta (no máximo 6 s).
  const inicio = await avisarServidor(
    backendUrl, `/transcricoes/${sessao}/iniciar`, { titulo: tab.title, url_reuniao: tab.url }, 6000,
  );
  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  await garantirOffscreen();
  capturas[tab.id] = {
    sessao,
    tabId: tab.id,
    url: tab.url,
    titulo: tab.title,
    inicio: Date.now(),
    microfone: true, // você pode desligar no painel; mudo no Meet também não envia
    empresa: inicio && inicio.empresa, // null: escolha no Início do console
  };
  await chrome.storage.session.set({ capturas });
  chrome.runtime.sendMessage({ alvo: "offscreen", tipo: "iniciar", streamId, sessao, backendUrl, tabId: tab.id });
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

// POST em JSON para o console. Devolve a resposta lida, ou null se falhar ou passar do
// tempo (falha é ignorada: o Início se acerta na próxima mudança).
async function avisarServidor(backendUrl, caminho, corpo, limiteMs = 15000) {
  if (!backendUrl) return null;
  try {
    const resposta = await fetch(`${backendUrl.replace(/\/$/, "")}${caminho}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corpo),
      signal: AbortSignal.timeout(limiteMs),
    });
    return resposta.ok ? await resposta.json() : null;
  } catch (e) {
    return null;
  }
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
