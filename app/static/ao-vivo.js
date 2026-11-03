// Início ao vivo: recebe os trechos das reuniões e põe cada um na área da reunião dele.
//
// Um canal só (EventSource em /ao-vivo/eventos) traz os trechos de todas as reuniões;
// cada trecho tem o id da captura ("sessao"), e é por ele que duas reuniões ao mesmo
// tempo não se misturam. O navegador reconecta sozinho se o canal cair.
//
// Avisos que chegam:
// - "trecho": um rascunho (substitui o anterior do mesmo canal) ou uma fala definitiva;
// - "falantes": os nomes mudaram (você disse quem é "Falante 2"): troca em todas as falas;
// - "reunioes": uma reunião começou ou foi salva: pede as áreas de novo ao servidor.
(function () {
  "use strict";

  // Sem a seção (outra página), nada a fazer.
  if (!document.getElementById("ao-vivo")) return;

  // Área de uma reunião pelo id da captura.
  function area(sessao) {
    return document.querySelector('.reuniao-viva[data-sessao="' + CSS.escape(sessao) + '"]');
  }

  // Nomes conhecidos de uma área: {"canal|rótulo": "Nome"}.
  function nomes(el) {
    try { return JSON.parse(el.dataset.nomes || "{}"); } catch (e) { return {}; }
  }

  // "mm:ss" a partir de milissegundos.
  function tempo(ms) {
    var s = Math.floor((ms || 0) / 1000);
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  // Monta (ou preenche) uma linha de fala com o nome, ou o rótulo quando ainda não há nome.
  function preencher(linha, chave, rotulo, texto, ms, nome) {
    linha.dataset.chave = chave;
    linha.dataset.rotulo = rotulo;
    linha.classList.toggle("sem-nome", !nome);
    var quando = document.createElement("span");
    quando.className = "quando";
    quando.textContent = tempo(ms);
    var quem = document.createElement("b");
    quem.className = "quem";
    quem.textContent = nome || rotulo;
    var dito = document.createElement("span");
    dito.textContent = texto;
    linha.replaceChildren(quando, quem, dito);
  }

  // Está perto do fim? (Só rola sozinho se você não subiu para ler algo antigo.)
  function pertoDoFim(caixa) {
    return caixa.scrollHeight - caixa.scrollTop - caixa.clientHeight < 80;
  }

  // Um trecho chegou.
  function trecho(t) {
    var el = area(t.sessao);
    // Reunião sem área ainda (o aviso "reunioes" pode chegar depois): redesenha.
    if (!el) { recarregar(); return; }
    var caixa = el.querySelector("[data-falas]");
    var rolar = pertoDoFim(caixa);
    var chave = t.canal + "|" + (t.rotulo || "");
    var nome = nomes(el)[chave];
    var parcial = caixa.querySelector('[data-parcial="' + t.canal + '"]');
    // Primeira fala: tira o "Aguardando a primeira fala".
    var espera = caixa.querySelector("[data-sem-falas]");
    if (espera) espera.remove();
    if (t.final) {
      // Fala definitiva: entra antes dos rascunhos, e o rascunho do canal some.
      var linha = document.createElement("div");
      linha.className = "fala";
      preencher(linha, chave, t.rotulo || "", t.texto, t.inicio_ms, nome);
      caixa.insertBefore(linha, caixa.querySelector("[data-parcial]"));
      if (parcial) parcial.hidden = true;
      // Falante que o bloco de participantes ainda não conhece: pede o bloco de novo.
      conferirFalante(el, chave);
    } else if (parcial) {
      // Rascunho: substitui o anterior do mesmo canal.
      preencher(parcial, chave, t.rotulo || "", t.texto, t.inicio_ms, nome);
      parcial.classList.add("parcial");
      parcial.hidden = false;
    }
    // Chegou trecho: a reunião está no ar.
    var estado = el.querySelector("[data-estado]");
    if (estado && !estado.classList.contains("salva")) {
      estado.className = "estado-vivo ao_vivo";
      estado.textContent = "ao vivo";
    }
    if (rolar) caixa.scrollTop = caixa.scrollHeight;
  }

  // Pede de novo o bloco de participantes se apareceu um falante novo.
  function conferirFalante(el, chave) {
    var bloco = el.querySelector("[data-participantes]");
    // Bloco ainda carregando: ele já virá com o falante.
    if (!bloco) return;
    var conhecidas = [];
    try { conhecidas = JSON.parse(bloco.dataset.chaves || "[]"); } catch (e) {}
    if (conhecidas.indexOf(chave) !== -1) return;
    // Você está escrevendo no bloco: não apaga o que você digitou.
    if (bloco.contains(document.activeElement)) return;
    htmx.ajax("GET", "/ao-vivo/" + encodeURIComponent(el.dataset.sessao) + "/participantes",
              { target: bloco, swap: "outerHTML" });
  }

  // Nomes novos: troca em todas as falas da reunião.
  function falantes(ev) {
    var el = area(ev.sessao);
    if (!el) return;
    el.dataset.nomes = JSON.stringify(ev.nomes || {});
    el.querySelectorAll(".fala[data-chave]").forEach(function (linha) {
      var nome = (ev.nomes || {})[linha.dataset.chave];
      var quem = linha.querySelector(".quem");
      if (quem) quem.textContent = nome || linha.dataset.rotulo;
      linha.classList.toggle("sem-nome", !nome);
    });
  }

  // Pede as áreas de novo (reunião nova ou salva). Espera um pouco para juntar avisos,
  // e não redesenha enquanto você escreve num campo da seção.
  var espera = null;
  function recarregar() {
    clearTimeout(espera);
    espera = setTimeout(function () {
      var secao = document.getElementById("ao-vivo");
      if (secao && secao.contains(document.activeElement) && document.activeElement.matches("input, select")) {
        recarregar();
        return;
      }
      // Lembra quais caixas de participantes estavam abertas.
      var abertas = [];
      document.querySelectorAll(".reuniao-viva[data-sessao] details[open]").forEach(function (d) {
        abertas.push(d.closest(".reuniao-viva").dataset.sessao);
      });
      htmx.ajax("GET", "/ao-vivo", { target: "#ao-vivo", swap: "outerHTML" }).then(function () {
        abertas.forEach(function (s) {
          var el = area(s);
          if (el) el.querySelector("details").open = true;
        });
        estado(conectado ? "recebendo ao vivo" : "reconectando…");
      });
    }, 400);
  }

  // Texto de situação da conexão.
  var conectado = false;
  function estado(texto) {
    var el = document.getElementById("ao-vivo-conexao");
    if (el) el.textContent = texto;
  }

  // Abre o canal.
  var fonte = new EventSource("/ao-vivo/eventos");
  fonte.onopen = function () {
    // Na primeira conexão, as áreas já vieram com a página; numa reconexão, podem ter
    // faltado trechos: pede as áreas de novo.
    if (conectado === null) recarregar();
    conectado = true;
    estado("recebendo ao vivo");
  };
  fonte.onerror = function () {
    // O navegador tenta de novo sozinho (a cada 3 s, como o servidor pediu).
    if (conectado) conectado = null;
    estado("reconectando…");
  };
  fonte.addEventListener("trecho", function (e) { trecho(JSON.parse(e.data)); });
  fonte.addEventListener("falantes", function (e) { falantes(JSON.parse(e.data)); });
  fonte.addEventListener("reunioes", recarregar);
})();
