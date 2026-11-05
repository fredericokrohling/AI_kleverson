# 21. Rascunhos de resposta

**O que muda (bloco 5, terceira parte):**

- **Página Rascunhos** (menu Trabalho, abaixo de Na agenda):
  - a lista **Para responder**, com os e-mails dos últimos 7 dias em que a conversa espera
    você. Ela é montada pelo banco, **sem custo**;
  - os **rascunhos recentes**, com o custo de cada um.
- **Página de cada e-mail** (`/rascunhos/email/<id>`):
  - a conversa, só com o texto próprio de cada mensagem;
  - o campo **O que responder** e o botão **Rascunhar resposta**, que é o único ponto
    pago;
  - os rascunhos, com **Copiar**, **Abrir conversa no Gmail**, **Falta completar** e
    **Confira**.
- **Link "Rascunhar resposta"** na página de cada e-mail em Processamento e na coluna
  Origem dos alertas que vieram de e-mail.
- **Banco:** migração 0016, com as tabelas `email_drafts` e `reply_dismissals`.
- **Custo:** cerca de US$ 0,02 por rascunho com o Sonnet. Há um teto próprio de US$ 3 por
  mês, dentro do teto geral.
- **Nenhuma permissão nova no Google.** O assistente continua sem poder gravar ou enviar
  no Gmail: você copia e responde pelo Gmail.

O porquê de cada escolha (modelo, custo, por que só a pedido e as defesas contra e-mail
malicioso) está em [docs/aprendizado/20-rascunhos.md](../aprendizado/20-rascunhos.md).

## 21.1 Antes de atualizar

- **Cadastre-se como Pessoa** em cada Empresa, com o e-mail da conta Google dela, se
  ainda não fez isso para os alertas. Assim o rascunho termina com o seu nome e as suas
  mensagens aparecem como "você".
- As Pessoas cadastradas são o que mais enche a lista **Para responder**. O e-mail de
  quem não é Pessoa só entra se tiver projeto.

## 21.2 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

A migração 0016 roda sozinha. As variáveis novas têm valor padrão e só precisam ir para o
`.env` se você quiser mudar algo:

```bash
# Modelo: claude-sonnet-5-5 (padrão), claude-haiku-5-5 (quase de graça) ou claude-opus-5-5.
DRAFTS_MODEL=claude-sonnet-5-5
# Esforço: low (padrão), medium ou high.
DRAFTS_EFFORT=low
# Teto dos rascunhos no mês, em dólares (com o Opus, use 6).
DRAFTS_MONTHLY_BUDGET_USD=3
# Janela da lista "Para responder", em dias.
DRAFTS_LOOKBACK_DAYS=7
# Máximo de caracteres da conversa enviados ao Claude.
DRAFTS_MAX_CHARS=20000
```

Depois de mudar o `.env`, rode `docker compose up -d` para o app ler os valores novos.

## 21.3 Roteiro de testes

1. **Lista:** abra **Rascunhos**. Os e-mails recentes que esperam você aparecem com quem
   escreveu (a marca "liderança" quando for o caso), o projeto e a Empresa. E-mails de
   no-reply, faturas e newsletters não aparecem.
2. **Rascunho sem pedido:** abra um e-mail e clique em **Rascunhar resposta** sem
   escrever nada. Depois de 5 a 20 segundos, a página volta com o aviso "Rascunho pronto
   (US$ 0,0xx)". Onde a decisão é sua, o texto traz marcadores como
   `[aceitar ou recusar?]`, listados em **Falta completar**.
3. **Rascunho com pedido:** escreva "aceitar, mas propor quinta às 10h" e peça de novo.
   O rascunho novo aparece em cima, e o anterior continua embaixo.
4. **Confira:** se o rascunho tiver uma data ou um valor que não está na conversa, ele
   aparece em **Confira**.
5. **Tom:** compare a saudação e a despedida com as suas. O Claude usa até três e-mails
   seus da mesma Empresa como exemplo. Se não houver nenhum e-mail seu coletado, o tom é
   o profissional padrão.
6. **Copiar e responder:** clique em **Copiar** e depois em **Abrir conversa no Gmail**.
   A conversa abre na conta da Empresa certa. Responda colando o texto.
7. **Saída automática da lista:** até 5 minutos depois de responder pelo Gmail, a conversa
   some de **Para responder**, porque a sua resposta foi coletada e virou a última
   mensagem.
8. **Dispensar:** num e-mail que não precisa de resposta, clique em **Dispensar**. Ele
   sai da lista. Na página do e-mail, **Voltar para a lista** desfaz.
9. **Outros pontos de entrada:** em Processamento, abra um e-mail e veja o botão
   **Rascunhar resposta**. Em Alertas, o alerta que veio de e-mail tem o link
   **Rascunhar resposta**.
10. **Custo:** cada rascunho mostra o próprio custo, e o quadro **Gasto no mês** de
    Rascunhos mostra a soma e o teto. A soma vem da tabela `llm_calls`, com o motivo
    `rascunho_resposta`.

## 21.4 Se algo der errado

- **"O teto dos rascunhos deste mês foi atingido":** o gasto chegou a
  `DRAFTS_MONTHLY_BUDGET_USD`. Suba o valor no `.env`, ou espere o mês virar. A
  classificação e os fatos continuam funcionando.
- **"Teto mensal de US$ 10 atingido":** foi o teto geral, que vale para tudo.
- **"O Claude não respondeu (APIConnectionError)":** foi a rede ou a Anthropic fora do
  ar. Tente de novo. A falha fica no registro de custo, com o erro e sem cobrança.
- **"A resposta veio cortada ou fora do formato":** é raro. Peça de novo.
- **Um e-mail que espera você não aparece na lista:** confira se você está no **Para** e
  não só em cópia. Confira também se quem escreveu é Pessoa cadastrada ou se o e-mail tem
  projeto. Para responder a qualquer outro e-mail, abra-o em Processamento e use
  **Rascunhar resposta**.
- **O rascunho não termina com o seu nome:** cadastre-se como Pessoa nesta Empresa, com o
  e-mail da conta Google dela.
