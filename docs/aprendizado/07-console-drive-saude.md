# 7. Console: pastas do Drive, tags herdadas, configurações e saúde

Este guia explica o porquê das peças que entraram com o menu lateral. O passo a passo de
instalação está em [docs/setup/06-console.md](../setup/06-console.md).

## Pastas monitoradas do Drive

### Espelho da árvore
O Drive é uma árvore: cada arquivo está dentro de uma pasta, que está dentro de outra. A
tabela `drive_items` guarda uma cópia dessa árvore (nome, caminho, pasta-mãe) para cada
pasta que você cadastra. A varredura desce nível por nível com `files.list` ("o que há
dentro desta pasta?"), como quem abre pasta por pasta no navegador.

Por que guardar a árvore no banco em vez de perguntar ao Drive toda vez: as tags precisam
saber "quem está dentro de quem" a qualquer momento, inclusive quando o Drive está fora do
ar, e a página de navegação abre na hora, sem esperar o Google.

### Uma versão por mudança de texto
O bronze é imutável: nada é editado. Por isso, cada mudança num arquivo vira uma **linha
nova** (`bronze_items` com fonte `drive` e id `<id do arquivo>:<versão>`). A versão antiga
continua lá. É assim que, no bloco 3, o sistema vai perceber que a data de homologação no
cronograma mudou de 20/11 para 04/12.

O Drive informa um número de versão que muda a cada alteração, mas ele também muda quando
alguém só renomeia o arquivo ou mexe no compartilhamento. Exportar o mesmo Google Docs duas
vezes, além disso, gera bytes diferentes (o arquivo `.docx` leva a hora da exportação).
Então a regra é: versão nova → baixa e extrai o texto → **só grava se o texto mudou**
(compara a impressão digital SHA-256 do texto). Assim não pagamos a IA, depois, para ler
a mesma coisa duas vezes.

## Tags e herança

### O que é
Uma tag diz a que um conteúdo se refere. Colocada numa pasta, vale para tudo dentro dela,
em qualquer nível.

### Como a herança é calculada
A tabela `drive_item_tags` guarda **só as tags colocadas diretamente** num item. A herança
é calculada por uma **view** (uma consulta salva no banco, que se usa como se fosse uma
tabela), `drive_item_effective_tags`. Ela usa uma "consulta recursiva" (`WITH RECURSIVE`):
para cada item, sobe pela pasta-mãe, depois pela avó, e assim por diante, juntando as tags
encontradas no caminho.

Uma segunda view, `bronze_item_tags`, liga isso ao bronze: cada versão de arquivo do Drive
aparece com as tags que o arquivo tem **agora**.

> **Decisão: copiar as tags ou calcular na consulta**
> Avaliamos **A — copiar as tags da pasta para cada item dentro dela** e **B — guardar só
> as tags colocadas diretamente e calcular a herança numa view**, e optamos por **B**:
> - Renomear, apagar ou retirar uma tag de uma pasta é uma mudança numa linha só. Com
>   cópia, seriam milhares de linhas, e qualquer falha no meio deixaria itens com tags
>   erradas.
> - Um arquivo novo na pasta já nasce com as tags, sem código extra lembrando de copiar.
> - O custo é uma consulta um pouco mais pesada, irrelevante no volume de uma pessoa.
>
> **Revisitar** se a árvore passar de centenas de milhares de itens.

## Arquivos sem texto e "Tentar de novo"

Um anexo ou arquivo que não vira texto é invisível para a IA. A coleta não para por causa
dele (o e-mail entra do mesmo jeito), então a falha precisa aparecer em algum lugar: a
página inicial lista esses arquivos, com um **código de erro** e um botão para tentar de
novo.

### Código no começo da mensagem
Cada erro é gravado como `[CODIGO] explicação` (ex.: `[PDF_SEM_TEXTO] PDF sem texto...`).
O código é estável e serve para procurar na tabela do guia de instalação; a explicação é
para leitura. Quando o erro vem do Google, o sistema lê o **motivo** que o Google informa
(`cannotDownloadFile`, `domainPolicy`...) e traduz: um 403 pode ser "o dono bloqueou o
download", "a empresa bloqueia aplicativos" ou "não foi compartilhado com você", e cada um
pede uma ação diferente.

> **Decisão: onde guardar o código do erro**
> Avaliamos **A — uma coluna nova `codigo_erro` nas tabelas** e **B — o código no começo
> da própria mensagem**, e optamos por **B**:
> - Erros ficam em três lugares (anexos, itens do Drive e o JSON das versões). Com A,
>   seriam três colunas novas e uma migração.
> - A mensagem já é exibida inteira em vários pontos; com o código nela, todos mostram o
>   código sem mudança.
> - Uma função (`split_error`) separa as duas partes para a tabela da página inicial.
>
> **Revisitar** se precisarmos filtrar ou contar erros por código em consultas pesadas.

### Remetente, data e "Dispensar"
Para o que veio de e-mail (anexos, links do Drive, links do Lucid e do Canva), a linha
mostra o remetente e a data e hora do e-mail, para você achar a mensagem. "Dispensar"
grava a data em `dismissed_at` e a linha sai da lista; o registro e o motivo continuam no
banco. É o caminho para os avisos que você resolve à mão, como exportar um diagrama.

### O que "Tentar de novo" faz
- **Já baixado, só a leitura falhou** (PDF escaneado, imagem): relê o original guardado
  no disco. Não chama o Google. Serve para quando o sistema ganhar um leitor novo.
- **Download falhou** (bloqueado, sem acesso): pergunta ao Drive de novo e baixa. Serve
  para quando o dono liberar o arquivo.

O texto extraído de uma versão pode ser atualizado: ele é derivado do original, que nunca
muda. O original e a versão continuam os mesmos; o bronze segue sendo o registro fiel do
que chegou.

## Configurações pelo console

Modelo do Claude, esforço e idioma da transcrição ficam na tabela `app_settings`. O `.env`
continua valendo como padrão inicial.

> **Decisão: onde guardar configurações que mudam pelo console**
> Avaliamos **A — continuar só no `.env`** e **B — banco de dados, com o `.env` como
> padrão**, e optamos por **B**:
> - O `.env` só é lido quando o contêiner sobe: trocar o modelo exigiria entrar no
>   servidor, editar e reiniciar.
> - No banco, a troca vale na próxima chamada e fica registrada na auditoria (quem mudou,
>   de quê para quê).
> - Senhas e chaves continuam só no `.env`: o banco guarda apenas escolhas sem segredo.

### Esforço do Claude
O "esforço" (`effort` na API) diz quanto o modelo pensa antes de responder: `low`, `medium`,
`high`, `xhigh` e `max`. O padrão do Opus 5.5 é `medium`. Mais esforço significa respostas
mais cuidadosas, mas mais tokens (mais caro) e mais demora. O console oferece os cinco
níveis; uma rotina do sistema que precise de um nível específico pode pedir o seu, e o do
console vale para as demais.

### Idioma da transcrição
O Deepgram precisa saber o idioma falado. Com `pt-BR`, frases em inglês saem erradas ou
somem. A opção **Automático** (`multi` no Deepgram) reconhece vários idiomas e aceita troca
no meio da frase; num idioma só, escolher o idioma certo costuma acertar um pouco mais.

## Proteção contra pedidos forjados (CSRF)

O console reconhece você pelo cabeçalho que o Tailscale acrescenta a cada pedido que sai do
seu computador. Um site malicioso aberto no seu navegador poderia ter um formulário
escondido que envia um POST para o endereço do console (por exemplo, para apagar uma tag), e
o Tailscale acrescentaria o cabeçalho do mesmo jeito.

O navegador sempre informa de qual site o pedido saiu (cabeçalho `Origin`, ou `Referer`).
Por isso todo POST do console passa por `same_origin` (`app/web.py`), que só aceita pedidos
saídos do próprio console.

## Saúde

### Dois tipos de teste
- **Funcionais:** o sistema usa o serviço do jeito que usa no dia a dia (consulta o banco,
  pergunta ao Gmail qual conta está conectada, valida a chave do Claude sem gerar texto).
  Se passar, funciona para nós.
- **Páginas de status públicas** (Anthropic, GitHub, Deepgram, Tailscale): mostram se o
  fornecedor declarou um incidente. Explicam uma falha ("o Claude está com incidente, não é
  problema nosso"). Google e AWS não publicam status num formato simples de ler por
  programa; para eles valem os testes funcionais e o fato de o console abrir.

Os testes rodam no serviço `coletor`. Se ele cair, os testes param, e a página avisa
"os testes automáticos não rodam há X min". É assim que o próprio coletor é vigiado.

### Coração vermelho
Toda página do console mostra o menu, então o estado do coração não pode depender de
cada rota lembrar de calculá-lo. O `Jinja2Templates` aceita **context processors**:
funções que rodam a cada página e acrescentam dados a ela. `app/web.py` registra uma que
consulta o último teste de cada serviço (`has_problem` em `app/health.py`).

Regras: vermelho se algum serviço falhou no **último** teste (uma falha antiga, já
recuperada, não conta) ou se os testes pararam há mais de 3 intervalos. "Não deu para
saber" não conta, para uma página de status fora do ar não acender alarme falso. Se o
próprio banco não responder, o coração fica vermelho.

### Versão instalada
O contêiner lê a pasta `.git` do projeto (montada só para leitura) para saber qual commit
está rodando. A API pública do GitHub diz a que PR esse commit pertence e quantos commits a
`main` tem a mais. O resultado fica guardado por 10 minutos, porque a API pública aceita só
60 consultas por hora.

### Reiniciar e atualizar: pelo seu computador, não pelo console

> **Decisão: como reiniciar e atualizar pelo console**
> Avaliamos **A — o console executa os comandos no servidor** e **B — o console abre um
> terminal no seu computador, que conecta por SSH e roda o comando**, e optamos por **B**:
> - Para reiniciar contêineres, o console precisaria do "socket" do Docker, que dá poder de
>   administrador sobre o servidor inteiro. Um bug no console, ou alguém que o invadisse,
>   teria o servidor.
> - Em B, quem roda o comando é o seu computador, com a sua chave e o seu login do
>   Tailscale. O console continua sem poder nenhum sobre o servidor.
> - O navegador não deixa uma página abrir programas sozinha. O caminho permitido é um
>   **tipo de link próprio** (`kleverson://reiniciar`), registrado no seu computador por um
>   arquivo `.desktop`; o Chrome pergunta antes de abrir.

O script (`scripts/terminal-local/kleverson-terminal.sh`) só aceita uma lista fixa de ações.
O texto do link nunca vira comando: ele só escolhe uma linha da lista. As ações que mudam o
servidor mostram o que vai acontecer e pedem confirmação no terminal.

## Processamento: o diário por dia

A seção lê a tabela `audit_log`, um dia por vez, em ordem cronológica, 30 eventos por
página. A caixa mostra 6 linhas (altura fixa em CSS: cada linha tem 25 px) e rola até 30.
No dia de hoje, a página abre na última página e um script curto rola a caixa até o fim,
para os 6 eventos mais recentes aparecerem primeiro. Num dia escolhido no calendário,
abre no começo. Trocar de dia ou de página usa o HTMX: só a seção é recarregada.

> **Decisão: em que fuso mostrar os horários**
> Avaliamos **A — mostrar em UTC, como o banco guarda** e **B — guardar em UTC e
> converter na tela para o fuso do console (`CONSOLE_TIMEZONE`)**, e optamos por **B**:
> - Em UTC, o "dia atual" viraria às 21h de Brasília, e os eventos da noite apareceriam
>   no dia seguinte.
> - Guardar em UTC continua certo: não depende de horário de verão nem de onde o servidor
>   está. Só a exibição muda.

## Conteúdo coletado

A página `/conteudo` mostra o texto como a IA vai ler. É uma versão simples da busca
do bloco 4: procura com `ILIKE` (contém, sem diferenciar maiúsculas) no assunto e no
texto. Serve bem para o volume de uma pessoa; a busca por significado (pgvector) vem
depois.

## Atualização automática da página inicial

Avaliamos três jeitos de a página mostrar o que é novo:

- **Recarregar a página inteira a cada N segundos:** o mais simples, mas perde a rolagem,
  interrompe um clique em "Tentar de novo" e consulta tudo de novo mesmo sem mudança.
- **Conexão aberta com o servidor (WebSocket ou SSE),** que empurra cada novidade: é
  instantâneo, mas o coletor é outro processo e precisaria avisar o console, uma peça a
  mais para algo que pode chegar com 30 segundos de atraso.
- **Marca + evento (escolhido):** a página guarda uma "marca" do estado do sistema (o
  último id do Processamento, do bronze e dos anexos, quantos arquivos estão sem texto, a
  última rodada de coleta e a cor do coração). A cada 30 segundos, um elemento invisível
  pergunta `/novidades?marca=...`. Se a marca mudou, o servidor responde com o cabeçalho
  `HX-Trigger: novidades`, e o HTMX dispara esse evento na página. Cada seção que o escuta
  (`hx-trigger="novidades from:body"`) se busca de novo e troca só a si mesma.

A pergunta custa umas poucas consultas de máximo e contagem, sem montar a página. O
Processamento só escuta o evento quando você está na última página de hoje: se escolheu
outro dia, a seção não muda debaixo dos seus olhos.

## O visual do console

Pedido da dona em 02/11/2026: manter o fundo escuro com branco e azul, melhorar as fontes,
trocar as explicações escritas por dicas, ícones menores e um jeito bonito de ver as
reuniões do dia.

### Cores em variáveis, num lugar só

Todas as cores ficam no começo do `<style>` de `app/templates/base.html`, como variáveis CSS
(`--menu`, `--fundo`, `--cartao`, `--accent`...). O resto do CSS só usa os nomes. Por quê:
trocar um tom muda o console inteiro, e fica fácil ver a paleta de uma vez.

Três tons de preto separam as camadas: o **menu** é o mais escuro (`#07080b`), o **fundo**
vem depois (`#0c0e13`) e os **cartões** são um pouco mais claros (`#12151b`). A diferença é
pequena de propósito: o olho percebe as camadas sem que a tela fique "listrada". O azul só
aparece no que é clicável ou no item ativo do menu.

O console agora é **só escuro**. Antes ele seguia o sistema operacional (claro ou escuro).
Um tema só é metade do CSS para manter, e foi o que a dona pediu.

### A fonte vem do próprio console

A fonte é a **Inter**, desenhada para telas (números com a mesma largura, que alinham nas
tabelas). O arquivo fica em `app/static/fonts/inter.woff2` e é servido pelo app, como o
htmx. Por que não o Google Fonts: cada página aberta avisaria o Google, e o console é
privado. O arquivo tem 48 KB porque traz só os caracteres latinos (com todos os acentos do
português). É um arquivo "variável": um só desenha todos os pesos, do fino ao negrito. A
licença (SIL Open Font License) está ao lado, em `OFL-Inter.txt`.

### Dicas no lugar do texto fixo

O ícone ⓘ ao lado do título mostra a explicação ao passar o mouse. É uma macro do Jinja2
(`app/templates/_macros.html`): `{{ m.dica("texto") }}`, ou `{% call m.dica() %}...{% endcall %}`
quando a explicação tem link. É só CSS: o balão fica escondido (`display: none`) e aparece
com `:hover` (mouse) ou `:focus-within` (teclado, com Tab). Por que `display: none` e não só
invisível: um balão invisível ainda ocupa largura e fazia a página rolar para o lado no
celular.

Avisos, erros e listas vazias continuam escritos, porque pedem ação.

### Menu em grupos

O menu tem nomes ao lado dos ícones e três grupos (Trabalho, Organização, Sistema). Uma
fileira de ícones sem nome funciona com cinco páginas; com dez, ninguém lembra qual é qual.
O botão no topo recolhe o menu só nos ícones, para sobrar espaço; a escolha fica guardada no
navegador (`localStorage`). Em tela estreita (celular), o menu já abre recolhido.

### A página inicial em duas colunas

Dados à esquerda, agenda à direita. O bloco que se atualiza sozinho (`#inicio-dados`) usa
`display: contents`: para o desenho ele "some" e os cartões dele viram itens da grade da
página, mas para o HTMX ele continua sendo um bloco só, trocado de uma vez. Assim a agenda
fica fora da atualização automática sem quebrar a grade.

## Agenda em formato de dia do calendário

As reuniões do dia aparecem numa coluna de calendário, como no Google Agenda: cada uma no
seu horário, com altura proporcional à duração. A cor diz a plataforma (Meet, Teams ou
outra); a bolinha diz se a transcrição já está no sistema.

### As contas ficam em Python

`app/transcription/agenda.py` calcula, para cada reunião, a distância do topo e a altura (em
minutos) e a coluna. O HTML (`_agenda_dia.html`) só converte minutos em pixels com uma
variável CSS (`--px`). Separar assim deixa as contas testáveis sem navegador.

Reuniões no mesmo horário ficam lado a lado. O algoritmo é o clássico dos calendários:

1. Ordena pelo início.
2. Junta em **grupos** as reuniões que se encostam (cada uma começa antes de a anterior do
   grupo terminar).
3. Dentro do grupo, cada reunião vai para a primeira **coluna** que já está livre naquele
   horário; se nenhuma está, abre uma coluna nova.
4. Todas do grupo dividem a largura pelo número de colunas do grupo.

Reunião muito curta (5 minutos) ganha uma altura mínima de 25 minutos, senão o título não
cabe. A grade vai das 8h às 19h e cresce se houver reunião fora disso.

### Como saber se a reunião já tem transcrição

A importação já grava `thread_id = "agenda:<id do evento>"` em cada transcrição (guia 9).
O calendário procura esses `thread_id` para as reuniões do dia, numa consulta só, sem
coluna nova nem migração.

### Quando a agenda é lida

A agenda vem da API do Google, que leva até um segundo para responder. Na página inicial, o
cartão chega logo depois do resto (`hx-trigger="load"`), e fica fora da atualização
automática de 30 segundos: a agenda não muda com a coleta, e perguntar ao Google duas vezes
por minuto gastaria cota à toa. As setas trocam o dia pedindo só o cartão de novo.

Na página Reuniões, trocar a data devolve a lista de reuniões do formulário **e** o
calendário do novo dia numa resposta só. O calendário vem marcado com `hx-swap-oob="true"`
("fora da faixa"): o HTMX o põe no lugar dele, mesmo não sendo o alvo do pedido.

## Um agendador só

O serviço `coletor` rodava só o Gmail. Agora roda o `agendador` (`python -m app.cli
agendador`): uma volta por minuto, e cada tarefa no seu intervalo. Uma tarefa que falha não
derruba as outras. Uma pasta do Drive recém-cadastrada é varrida na volta seguinte, sem
esperar os 30 minutos.

## Onde isso está no código

- `app/ingestion/drive_sync.py`: cadastro de pastas, varredura e nova tentativa no Drive.
- `app/activity.py`: seção Processamento.
- `app/updates.py` e `app/routes_updates.py`: a marca e a pergunta da atualização automática.
- `app/routes_content.py`: conteúdo coletado.
- `app/ingestion/failures.py` e `app/routes_failures.py`: lista de arquivos sem texto e
  botões "Tentar de novo".
- `app/tags.py`: tags e leitura da herança.
- `app/runtime_settings.py`: configurações do console.
- `app/health.py`: testes de saúde e versão instalada.
- `app/web.py`: peças comuns das páginas e a proteção de POST.
- `app/routes_config.py` e `app/routes_health.py`: as páginas.
- `app/templates/base.html`: as cores, a fonte, o menu e o CSS de todas as páginas.
- `app/templates/_macros.html`: a dica que aparece ao passar o mouse.
- `app/transcription/agenda.py`, `_agenda_dia.html` e `_agenda_cartao.html`: o dia de calendário.
- `app/static/`: logo claro (`logo-claro.png`), o olho do menu recolhido (`marca.png`) e a fonte.
- `migrations/versions/0004_console_configuracoes_drive_saude.py`: tabelas e as duas views.
- `scripts/terminal-local/`: o atendente dos links `kleverson://`.
