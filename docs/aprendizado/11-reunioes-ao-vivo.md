# 11. Reuniões ao vivo no Início: duas reuniões, participantes e quem falou

Pedido da dona (03/11/2026): o Início é o painel de controle dela. Uma das funções
principais é ver a transcrição das reuniões **em tempo real**, inclusive de **duas
reuniões ao mesmo tempo**, e dizer **quem é cada participante**, para o banco guardar
quem disse o quê no lugar de "Falante 1", "Falante 2".

Este guia explica como a transcrição chega à tela, por que as reuniões não se misturam,
como os falantes viram pessoas e onde cada coisa está no código.

## O caminho de uma fala

```
aba do Meet ──áudio──► extensão ──WebSocket──► servidor ──► Deepgram
                                                  │  ◄──texto──┘
                                                  ├──► banco (transcript_segments)
                                                  ├──► painel da extensão
                                                  └──► Início do console (SSE)
```

1. A extensão captura o áudio da aba (os outros) e o seu microfone (você), em duas
   conexões WebSocket (ver [guia 05](05-transcricao.md)).
2. O servidor repassa o áudio ao Deepgram e recebe o texto.
3. Cada trecho vai para três lugares: o banco (só o definitivo), o painel da extensão e,
   desde esta versão, o **Início do console**.

## Como o texto chega ao Início: Server-Sent Events (SSE)

O Início precisa receber os trechos sem pedir. Havia três caminhos:

- **Perguntar ao servidor a cada segundo** ("polling"): simples, mas um segundo de atraso
  no mínimo, muitos pedidos vazios e nenhum rascunho (o rascunho não vai para o banco).
- **WebSocket**: conversa nos dois sentidos. O Início, porém, só recebe; quem manda é a
  extensão.
- **SSE** (Server-Sent Events): uma resposta HTTP comum que não termina. O servidor
  escreve um aviso por vez (`event: trecho` + `data: {...}`), e o navegador tem o
  `EventSource`, que lê esses avisos e **reconecta sozinho** se cair.

> **Decisão:** Avaliamos polling, WebSocket e SSE. Optamos pelo **SSE**, porque o Início só
> recebe, o navegador já reconecta sozinho e ele passa pelo Tailscale como qualquer página.
> O WebSocket continua só entre a extensão e o servidor, onde há áudio indo e texto
> voltando.

No código: a rota `GET /ao-vivo/eventos` em `app/transcription/routes_live.py` e o script
`app/static/ao-vivo.js`. A cada 15 segundos sem aviso, o servidor manda um comentário
(`: ping`), para ninguém no caminho achar que a conexão morreu.

### Quem distribui os avisos: o `LiveHub`

Cada aba do console aberta é um "ouvinte" com uma fila (`asyncio.Queue`). Quando chega um
trecho, o `LiveHub` (em `app/transcription/live.py`) põe o aviso na fila de cada ouvinte.

Isso fica **na memória do processo**, e funciona porque o servidor roda **um processo só**
(o `uvicorn` do Dockerfile, sem `--workers`). Com dois processos, a extensão poderia estar
ligada a um e o Início ao outro, e os trechos não chegariam. Nesse dia, a troca é usar o
`LISTEN/NOTIFY` do Postgres como ponte. Os trechos definitivos vão para o banco de
qualquer jeito: a memória só leva o aviso até a tela.

> **Decisão:** Avaliamos distribuir pelo Postgres (`LISTEN/NOTIFY`) e pela memória do
> processo. Optamos pela **memória**, porque o console tem um processo só e o banco
> continua sendo a fonte da verdade. Se o console passar a ter mais de um processo, essa
> decisão precisa ser revista.

Um detalhe: os avisos nascem em dois lugares. O WebSocket da extensão roda no laço
assíncrono do servidor; cadastrar um participante roda numa rota comum, que o FastAPI
executa em outra *thread*. Uma fila assíncrona não aceita escrita de outra thread, então o
`LiveHub` usa `loop.call_soon_threadsafe`, que pede ao laço dono da fila para fazer a
escrita.

## Por que duas reuniões não se misturam

Cada clique no ícone da extensão gera um **id de captura** (`sessao`, um UUID). Tudo fica
preso a ele:

- o áudio vai pelo endereço `/ws/transcricao?sessao=<id>&canal=...`;
- cada trecho no banco tem o `session_id`;
- cada aviso para o Início leva o `sessao`, e o navegador põe o trecho na área com o
  mesmo `data-sessao`;
- participantes e falantes ficam na reunião (`live_meetings`) daquele id.

Por isso o servidor não precisa saber de janelas ou abas: duas capturas são só dois ids.

### Na extensão

A extensão 0.1 tinha **uma** captura de cada vez. A 0.2 guarda as capturas por aba
(`chrome.storage.session.capturas`) e o documento de áudio (`offscreen.js`) mantém um
mapa id → captura. Cada aba tem o seu painel lateral (`painel.html?aba=<id da aba>`), que
só mostra o que é da captura dela.

### O seu microfone, com duas reuniões

O microfone é um só. Se ele fosse para as duas reuniões, a sua fala apareceria nas duas.
A regra: **o microfone vai para a reunião em que ele está aberto no Meet**. O
`legendas.js` (que já lia as legendas do Meet) agora também olha o botão de microfone do
Meet, que tem o atributo `data-is-muted`, e avisa a extensão. Além disso, cada painel tem
o botão **Desligar meu microfone aqui**, para quando o Meet mudar a página e a leitura do
botão falhar.

Quando o microfone não vai para uma reunião, a extensão não manda áudio nenhum: manda
`manter` a cada 5 segundos, e o servidor repassa um `KeepAlive` ao Deepgram. Sem isso, o
Deepgram fecha a conexão depois de uns 10 segundos sem áudio. Mandar silêncio também
manteria a conexão, mas silêncio é áudio cobrado.

> **Decisão:** Avaliamos mandar o microfone para todas as reuniões, escolher à mão em qual
> ele vai, e seguir o estado do microfone no Meet. Optamos por **seguir o Meet, com o
> botão do painel para desligar**, porque é o que você já faz ao trocar de reunião (abrir o
> microfone numa e fechar na outra). A leitura do botão do Meet é frágil (o Meet muda a
> página sem aviso), por isso o botão manual existe.

### Janelas anônimas

O pedido falava em duas janelas anônimas. Três cuidados:

1. A extensão só roda em janela anônima com **Permitir no modo anônimo** ligado em
   `chrome://extensions`.
2. Todas as janelas anônimas abertas ao mesmo tempo **dividem o mesmo login**. Duas
   janelas anônimas não servem para entrar com duas contas Google diferentes.
3. Não deu para testar a captura de áudio de aba anônima aqui.

Para duas reuniões com a mesma conta, duas abas ou janelas comuns bastam. Para duas contas,
o caminho seguro são dois **perfis** do Chrome, cada um com a extensão instalada.

## Quem é cada voz

O Deepgram separa as vozes pelo som e numera ("Falante 1", "Falante 2"), mas não sabe
nomes. Quem sabe é você. Três tabelas novas (`app/models.py`):

| Tabela | O que guarda |
|---|---|
| `live_meetings` | uma linha por captura: título, endereço do Meet, começo, último trecho, quando foi salva |
| `meeting_participants` | quem participa de cada reunião: nome, e-mail (opcional) e a Pessoa, quando houver |
| `meeting_speakers` | qual participante é cada rótulo de cada canal ("outros" + "Falante 2" → Márcia) |

E a fala (`utterances`) ganhou `participant_id`.

**Por que ligar o rótulo, e não cada fala:** você diz uma vez que "Falante 2" é a Márcia,
e isso vale para as falas que já passaram e para as próximas. No navegador, só os nomes
mudam (o script troca o texto de todas as falas com aquele rótulo). No banco, a troca
acontece ao salvar a reunião.

**Depois de salva:** mudar quem é uma voz refaz a transcrição salva (texto e falas). Isso
usa a mesma função de salvar (`store.finalize`), que pode ser chamada de novo sem duplicar.

**Nome único na reunião:** o nome é comparado na forma "sem acento e minúsculo" (a mesma
`normalize` do reconhecimento de quem falou), então "João Lima" e "joao lima" contam como o
mesmo nome. A regra está no banco também (`UNIQUE (meeting_id, name_key)`), e não só no
código, para nenhuma rota esquecida conseguir furar.

## O e-mail como chave da Pessoa

A dona pediu que o e-mail seja a chave da Pessoa. Ele passou a ser **obrigatório, único e
guardado em minúsculas**. O número (`id`) continua como chave interna das ligações
(projetos, falas, participantes).

> **Decisão:** Avaliamos trocar a chave primária da tabela de pessoas pelo e-mail e manter
> o número com o e-mail obrigatório e único. Optamos pelo **número como chave interna e o
> e-mail como chave de negócio**, porque e-mails mudam (troca de empresa, de domínio) e,
> com o e-mail como chave primária, cada mudança teria de ser repetida em todas as tabelas
> ligadas. Para quem usa o sistema, o efeito é o pedido: duas pessoas nunca têm o mesmo
> e-mail, e é pelo e-mail que o sistema acha a pessoa.

**O que isso permite já agora:** um participante escrito à mão com o e-mail de uma Pessoa
é ligado a ela na hora. E, quando você cadastra uma Pessoa nova, os participantes de
reuniões antigas com o mesmo e-mail são ligados a ela, com todas as falas
(`link_participants_by_email` em `app/people.py`). É a base do requisito futuro ("criar a
pessoa e vincular todo o conteúdo dela").

**A migração** (`0007`) confere antes de mudar: se houver pessoa sem e-mail ou dois e-mails
iguais com maiúsculas diferentes, ela para com a lista de nomes, sem mudar nada.

## Trazer convidados da agenda

A extensão manda o endereço do Meet (`https://meet.google.com/abc-defg-hij`). O evento da
agenda também tem esse endereço (`hangoutLink`). O botão compara o código da reunião
(`abc-defg-hij`) com os eventos do dia e inclui os convidados com o nome da agenda e o
e-mail. Salas de reunião ficam de fora, e quem já está na reunião não é incluído de novo.

## As áreas do Início

- Sempre há pelo menos **duas** áreas. Uma reunião ocupa a da esquerda; a outra fica livre.
- Uma reunião aparece desde o clique no ícone (a extensão avisa `POST
  /transcricoes/<id>/iniciar`) e fica até ser salva. Salva, ela fica mais **30 minutos**,
  para você ainda acertar os nomes. Sem nenhum trecho por **3 horas** (aba fechada sem
  salvar, por exemplo), ela some do Início; os trechos continuam no banco.
- Fechar a aba do Meet agora salva a reunião sozinho.
- A situação: **ao vivo** (trecho nos últimos 2 minutos), **sem áudio agora** ou **salva**.

## Pastas do Drive em Cadastros

O cadastro das pastas monitoradas saiu de Configurações e foi para Cadastros, junto com
projetos, pessoas e tags. O motivo é a separação: em **Cadastros** fica o que o sistema lê
e como isso é organizado; em **Configurações**, como o sistema funciona (contas, modelo do
Claude, idioma). As rotas foram para `app/routes_drive.py`, e os endereços passaram de
`/config/drive/...` para `/cadastros/drive/...`.

## Limites conhecidos (sem rodeio)

- **A separação de vozes erra.** Duas pessoas de voz parecida podem virar o mesmo
  "Falante", e o número pode mudar no meio da reunião. O ajuste manual corrige o rótulo
  inteiro, não fala a fala.
- **A leitura do microfone do Meet é frágil.** Se o Meet mudar o botão, use o botão do
  painel.
- **Um processo só** no servidor (ver o `LiveHub` acima).
- **Sem reconexão do áudio.** Se a internet cair, a captura daquela reunião para; o que já
  foi dito está salvo.

## Onde está no código

| Arquivo | O que faz |
|---|---|
| `app/transcription/live.py` | `LiveHub`, reuniões, participantes, falantes, convidados |
| `app/transcription/routes_live.py` | canal de eventos e rotas dos participantes |
| `app/transcription/routes.py` | WebSocket do áudio: cria a reunião e publica cada trecho |
| `app/transcription/store.py` | salvar a reunião com os nomes |
| `app/templates/_ao_vivo.html`, `_participantes.html` | as áreas e o bloco de participantes |
| `app/static/ao-vivo.js` | recebe os avisos e põe cada trecho na área certa |
| `extensao/` | várias capturas, microfone por reunião, aba fechada salva |
| `migrations/versions/0007_...` | tabelas novas e e-mail obrigatório |
| `tests/test_live.py` | duas reuniões sem misturar, nomes, salvar, convidados, microfone mudo |
