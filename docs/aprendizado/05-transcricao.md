# 5. Transcrição em tempo real: como funciona e por quê

## O requisito

Transcrever reuniões do Meet **ao vivo**, **sem bot** entrando na reunião, e funcionar
também em reuniões de fornecedores (em que você não é organizadora e não controla a
gravação nem a transcrição oficial do Google).

Isso descarta dois caminhos comuns:

- **Transcrição oficial do Meet/Workspace:** só sai depois da reunião, depende do plano do
  Workspace e de quem organiza. Em reunião de fornecedor, o arquivo fica com ele.
- **Bots (Recall.ai, Fireflies, Otter):** entram na reunião como participante. Você pediu
  explicitamente que não houvesse bot.

Sobra capturar o áudio **no seu próprio navegador**. É o que a extensão faz.

## O caminho do áudio

```
Aba do Meet (áudio dos outros) ─┐
                                ├─ extensão do Chrome ── WebSocket ── servidor ── WebSocket ── Deepgram
Seu microfone (sua voz) ────────┘        ▲                              │
                                         └──────── texto ao vivo ───────┘
```

1. A extensão captura **dois canais separados**: o áudio da aba (os outros) e o seu
   microfone (você).
2. Converte cada um em PCM 16 bits, 16 kHz, mono, e envia em pedaços de ~100 ms.
3. O servidor repassa ao Deepgram e devolve o texto para o painel lateral.
4. Cada trecho definitivo é gravado no banco na hora.
5. Em "Parar e salvar", os trechos viram uma transcrição completa no bronze.

## Por que cada peça

### Por que dois canais (você e os outros)
A separação de vozes do serviço (diarização) erra mais quando as vozes se misturam.
Separando seu microfone, **tudo que você fala é identificado como "Você" com certeza**, e
a diarização só precisa separar os outros. Por isso o fone de ouvido é obrigatório: sem
ele, o microfone também capta a voz dos outros pelo alto-falante.

### Por que uma extensão do Chrome
É o único lugar, sem bot, que tem acesso ao áudio da reunião. A API `tabCapture` do
Chrome entrega o áudio da aba do Meet. Funciona igual em qualquer reunião aberta no
navegador, inclusive de fornecedores.

### Por que o "offscreen document"
Nas extensões modernas do Chrome (Manifest V3), o código de fundo (service worker) não
pode processar áudio. O Chrome oferece uma página invisível ("offscreen") só para isso.
É lá que o áudio é capturado e convertido.

### Por que PCM 16 bits, 16 kHz, mono
- **PCM** é áudio "cru", sem compressão. O serviço não precisa decodificar nada e o
  atraso fica mínimo.
- **16 kHz** é o padrão para voz: capta toda a faixa da fala e usa um terço dos dados do
  áudio de música (48 kHz). Menos dados, menos atraso.
- **Mono**, porque cada canal (você, os outros) já vai numa conexão separada.
- A conversão é feita por um **AudioWorklet** (`extensao/pcm-worklet.js`), que roda numa
  linha de execução própria do navegador e não engasga a página.

### Por que WebSocket
Uma requisição HTTP comum é pergunta e resposta, e fecha. O WebSocket fica aberto nos
dois sentidos durante a reunião inteira: o áudio sobe sem parar e o texto desce assim que
fica pronto. É o protocolo padrão para streaming de voz.

### Por que o servidor no meio (e não a extensão direto no Deepgram)
A chave do Deepgram fica **só no servidor**. Se a extensão falasse direto com o Deepgram,
a chave teria que estar no navegador, onde qualquer extensão maliciosa poderia lê-la. O
servidor também grava os trechos no banco, e a extensão não precisa saber nada de banco.

### Por que o Deepgram (hipótese a validar, não decisão final)

| Serviço | Ponto a favor | Ponto contra para este caso |
| --- | --- | --- |
| **Deepgram** (escolhido para o teste) | Streaming por WebSocket simples; texto parcial enquanto a pessoa fala; separação de falantes já no streaming; português; cobra por minuto e dá crédito inicial para testar | Serviço externo: o áudio sai da sua máquina |
| Google Speech-to-Text | Mesmo ecossistema do Meet | Usa gRPC (mais complexo) e, até onde sei, limita cada conexão de streaming a alguns minutos, exigindo reconectar no meio da reunião |
| Azure Speech | Boa qualidade em português | Exige conta e SDK da Microsoft |
| AssemblyAI, OpenAI | Bons em inglês | Não tenho certeza da qualidade de streaming em português com separação de falantes |
| Whisper rodando no seu servidor | Áudio não sai da sua máquina; sem custo por minuto | Não é streaming de verdade; exige GPU para ficar rápido, o que custa mais que o Deepgram no seu volume |

O código isola o serviço num único arquivo (`app/transcription/providers.py`). Trocar de
serviço é escrever outra classe com os mesmos métodos (`send_audio`, `finish`,
`results`). O teste do bloco 1 serve justamente para validar a escolha com reuniões reais.

### Por que diarização e legendas do Meet ao mesmo tempo
- A **diarização** do Deepgram separa vozes, mas só sabe dizer "Falante 1, 2, 3".
- As **legendas do Meet** sabem o **nome** de quem fala, mas a qualidade do texto e o
  acesso a elas dependem da página do Meet, que o Google muda sem aviso.

O teste grava as duas para comparar. O cenário provável é combinar: texto do Deepgram,
nomes das legendas.

### Por que gravar trecho a trecho
Se a internet cair no minuto 40, os 40 minutos já transcritos estão salvos. A
finalização pode ser chamada de novo sem duplicar (a regra única do bronze garante).

## Onde isso está no código

| Arquivo | Papel |
| --- | --- |
| `extensao/manifest.json` | Declara as permissões da extensão |
| `extensao/background.js` | Abre o painel e a página offscreen ao clicar no ícone |
| `extensao/offscreen.js` | Captura os dois canais e manda o áudio por WebSocket |
| `extensao/pcm-worklet.js` | Converte o áudio para PCM 16 kHz |
| `extensao/legendas.js` | Lê as legendas da página do Meet |
| `extensao/painel.js` | Mostra o texto ao vivo e o botão de salvar |
| `app/transcription/routes.py` | WebSocket e rotas de legendas e finalização |
| `app/transcription/providers.py` | Conversa com o Deepgram |
| `app/transcription/store.py` | Grava trechos e monta a transcrição no bronze |
