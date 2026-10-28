// Clique no ícone da extensão numa aba do Meet: abre o painel e começa a captura.
// A captura da aba precisa partir deste clique (é a permissão do usuário).

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.url || !tab.url.startsWith("https://meet.google.com/")) {
    chrome.runtime.openOptionsPage();
    return;
  }
  chrome.sidePanel.open({ tabId: tab.id });

  const { captura } = await chrome.storage.session.get("captura");
  if (captura) return; // já está transcrevendo; só reabre o painel

  const { backendUrl } = await chrome.storage.sync.get("backendUrl");
  if (!backendUrl) {
    chrome.runtime.openOptionsPage();
    return;
  }

  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
  await garantirOffscreen();
  const sessao = crypto.randomUUID();
  await chrome.storage.session.set({
    captura: { sessao, tabId: tab.id, url: tab.url, titulo: tab.title, inicio: Date.now() },
  });
  chrome.runtime.sendMessage({ alvo: "offscreen", tipo: "iniciar", streamId, sessao, backendUrl });
});

async function garantirOffscreen() {
  const existentes = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (existentes.length > 0) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Capturar o áudio da reunião e do microfone para transcrição.",
  });
}
