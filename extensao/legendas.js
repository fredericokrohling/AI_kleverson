// Lê as legendas do próprio Meet (precisam estar ligadas, botão CC) para comparar
// com o serviço de transcrição. O Meet muda a página sem aviso, então isto é frágil
// de propósito: serve ao teste, não à produção.

const SELETOR_REGIAO = [
  'div[role="region"][aria-label="Captions"]',
  'div[role="region"][aria-label="Legendas"]',
  'div[aria-label="Captions"]',
  'div[aria-label="Legendas"]',
].join(",");
const ESPERA_FINAL_MS = 2500;

// Para cada bloco de legenda: texto já enviado e temporizador de "fala terminou".
const blocos = new WeakMap();

function enviar(falante, texto) {
  if (!texto.trim()) return;
  chrome.runtime
    .sendMessage({ alvo: "painel", tipo: "legenda", falante, texto, momento: Date.now() })
    .catch(() => {}); // painel fechado: nada a fazer
}

function lerBloco(bloco) {
  const linhas = bloco.innerText.split("\n").map((l) => l.trim()).filter(Boolean);
  if (linhas.length < 2) return null;
  return { falante: linhas[0], texto: linhas.slice(1).join(" ") };
}

function concluir(bloco) {
  const estado = blocos.get(bloco);
  const atual = lerBloco(bloco);
  if (!estado || !atual) return;
  const novo = atual.texto.slice(estado.enviado);
  enviar(atual.falante, novo);
  estado.enviado = atual.texto.length;
}

function observarRegiao(regiao) {
  const verificar = () => {
    for (const bloco of regiao.children) {
      if (!blocos.has(bloco)) blocos.set(bloco, { enviado: 0, timer: null });
      const estado = blocos.get(bloco);
      clearTimeout(estado.timer);
      estado.timer = setTimeout(() => concluir(bloco), ESPERA_FINAL_MS);
    }
  };
  new MutationObserver((mudancas) => {
    for (const m of mudancas) {
      for (const removido of m.removedNodes) {
        if (blocos.has(removido)) concluir(removido);
      }
    }
    verificar();
  }).observe(regiao, { childList: true, subtree: true, characterData: true });
}

let regiaoAtual = null;
new MutationObserver(() => {
  const regiao = document.querySelector(SELETOR_REGIAO);
  if (regiao && regiao !== regiaoAtual) {
    regiaoAtual = regiao;
    observarRegiao(regiao);
  }
}).observe(document.body, { childList: true, subtree: true });

// Microfone aberto ou fechado no Meet desta aba. O botão do microfone do Meet tem o
// atributo data-is-muted ("true" quando está mudo). Com duas reuniões ao mesmo tempo,
// isso decide para qual delas o seu microfone vai. Se o Meet mudar a página e o botão não
// for achado, nada é avisado e vale o botão do painel da extensão.
// Repete o aviso a cada 5 s mesmo sem mudança: uma captura que começou depois do último
// aviso também fica sabendo.
let mudoAtual = null;
let voltas = 0;
setInterval(() => {
  const botao = [...document.querySelectorAll("[data-is-muted]")].find((b) =>
    /microfone|microphone/i.test(b.getAttribute("aria-label") || ""),
  );
  if (!botao) return;
  const mudo = botao.getAttribute("data-is-muted") === "true";
  voltas = (voltas + 1) % 5;
  if (mudo === mudoAtual && voltas !== 0) return;
  mudoAtual = mudo;
  chrome.runtime.sendMessage({ alvo: "extensao", tipo: "mudo", mudo }).catch(() => {});
}, 1000);
