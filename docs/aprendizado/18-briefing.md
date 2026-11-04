# 18. Briefing diário: uma foto da manhã, montada por SQL

Este guia explica a primeira peça do **bloco 5 (ações)**: o briefing diário. O código está
em `app/briefing.py`; a tela, em **Briefing** (menu Trabalho, logo abaixo de Início), e
o resumo em números aparece no **Início**.

## 18.1 Por que este é o próximo passo do roadmap

Com o PR #20, o bloco 3 (silver) ficou completo: cada item tem projeto, cada fato tem o
trecho conferido e quem disse, e as lideranças geram alertas. A Arquitetura (seção 17)
diz que, depois do bloco 3, os blocos 4, 5, 7 e 8 podem seguir em qualquer ordem. O
briefing ganha porque:

- é o **primeiro uso diário** de tudo o que foi construído: até aqui, saber "o que eu
  preciso fazer hoje" exigia abrir Reuniões, Alertas, Fatos e Classificação e cruzar de
  cabeça;
- está na lista de funcionalidades desde a primeira versão da Arquitetura (seção 11:
  "agenda, prazos, pendências e o que mudou por projeto") e o guia 17 já o apontava
  como o próximo ("agenda + alertas novos + prazos");
- **não pede permissão nova ao Google** nem custo de IA. As outras duas peças do bloco 5
  (criar eventos na agenda e rascunhos de resposta) escrevem em algum lugar ou chamam o
  Claude a cada e-mail; elas merecem um PR próprio, com decisão própria.

Crítica franca: o requisito original dizia só "briefing pela manhã". Eu decidi o
conteúdo (as cinco seções abaixo), o horário (7h) e onde ele aparece (no console).
Se o seu briefing ideal tem outra cara, por exemplo "só os alertas de peso alto" ou
"uma Empresa por vez", é ajuste de tela; a estrutura aguenta.

## 18.2 As cinco perguntas

Cada Empresa tem o seu briefing, com cinco seções:

| Seção | Pergunta | De onde vem |
| --- | --- | --- |
| **Agenda** | Que reuniões tenho hoje, com que lideranças, e o que está aberto com quem vai estar? | Google Agenda da conta da Empresa + Pessoas + alertas abertos |
| **Alertas novos** | O que as lideranças pediram, decidiram ou apontaram e eu ainda não vi? | tabela `alerts` (situação "novo") |
| **Prazos** | O que venceu e o que vence nos próximos 7 dias? Qual é meu? | `facts` com prazo, conferidos e abertos |
| **O que mudou por projeto** | Quantos e-mails, transcrições e arquivos chegaram em cada projeto, e que decisões, riscos e problemas apareceram? | `bronze_items` + classificação + `facts` novos |
| **Pendências** | O que espera uma decisão minha para o sistema continuar certo? | fila A revisar, fatos suspeitos, arquivos sem texto |

O detalhe mais útil é o **"aberto com quem vai estar"** na agenda: para cada reunião, o
código procura os convidados cadastrados como Pessoa e conta os fatos com alerta aberto
ditos por eles ou com eles como responsáveis. É o "não esqueça de levar o cronograma
para a Márcia" antes da reunião com a Márcia. Você (seu e-mail do Tailscale ou da conta
Google da Empresa) não conta, senão toda reunião mostraria todas as suas tarefas.

## 18.3 A decisão central: foto gravada ou página montada na hora

> **Decisão: como o briefing existe.** Avaliamos **A, montar a página na hora em que você
> abre** e **B, montar de manhã e gravar uma foto (JSON) por Empresa por dia**, e optamos
> por **B**:
> - **A janela de "o que mudou" precisa ficar fixa.** Montado na hora, o "desde o último
>   briefing" mudaria a cada vez que você abrisse a página.
> - **A agenda vem do Google**, que pode estar lento ou fora do ar às 9h. A foto das 7h
>   já está pronta; se a agenda falhar, só aquela seção mostra o motivo.
> - **Histórico:** dá para ver o briefing de terça-feira passada como ele estava, com as
>   setas da página ou o calendário.
> - **Custo:** o briefing de duas Empresas são algumas dezenas de consultas, uma vez por
>   dia, em vez de a cada vez que a página abre.
>
> O preço: a foto envelhece durante o dia. Por isso há o botão **Gerar de novo**, e cada
> linha tem o link para a origem, que mostra o estado de agora.

A foto fica na tabela `briefings` (migração 0014): uma linha por (Empresa, dia), com o
conteúdo em `content` (JSONB), o momento em que foi montado (`generated_at`), o começo da
janela (`since`) e quando você abriu (`viewed_at`). Por que JSON e não tabelas por seção:
a foto nunca é editada nem consultada por partes, só mostrada inteira. JSONB guarda
exatamente o que a página desenha, e a propriedade `Briefing.sections` completa as
seções que faltarem, para um briefing antigo não quebrar quando uma seção nova for criada.

## 18.4 "Desde o último briefing que você abriu"

> **Decisão: onde começa o "o que mudou".** Avaliamos **A, as últimas 24 horas** e **B,
> desde o último briefing que você abriu**, e optamos por **B**:
> - Na segunda-feira, "últimas 24 horas" esconde o que chegou no sábado.
> - Depois de dois dias sem abrir o console, "últimas 24 horas" esconde o primeiro dia.
> - Com B, a janela começa onde você parou de ler. Se você não abriu nenhum, os dias se
>   somam (vale o começo da janela do primeiro briefing não lido).

Regras que o código (`window_start`) segue:

1. só contam briefings de **dias anteriores**: "Gerar de novo" à tarde não encolhe a
   janela só porque você abriu o de hoje de manhã;
2. abrir a página com o briefing de **hoje** marca como lido; abrir um dia passado não
   muda nada;
3. a janela nunca passa de `ALERTS_LOOKBACK_DAYS` (30 dias), para a primeira manhã
   depois das férias não virar um livro;
4. o primeiro briefing de uma Empresa olha as últimas 24 horas.

O mesmo "desde" marca os alertas com o selo **chegou agora**: nasceram depois do último
briefing que você abriu.

## 18.5 Por que sem IA

> **Decisão: resumo escrito pelo Claude.** Avaliamos **A, um parágrafo do Claude no topo
> ("hoje o mais importante é...")** e **B, só listas e contagens do banco**, e optamos por
> **B**:
> - Tudo o que o briefing mostra já foi lido pelo Claude uma vez (classificação e
>   extração) e conferido pelo código. Um resumo seria a terceira leitura do mesmo texto.
> - Um resumo pode afirmar o que o banco não diz ("o projeto está atrasado"), e é
>   exatamente o tipo de frase que soa certa e ninguém confere. O princípio da
>   Arquitetura é "números vêm de código, não da IA".
> - Custo: pouco por dia, mas todo dia, para cada Empresa, dentro do teto de US$ 10.
>
> **Revisitar** quando o MCP (bloco 7) estiver pronto: perguntar ao Claude "o que eu
> preciso saber hoje?" com o briefing como contexto dá o resumo sob demanda, sem custo
> fixo e com a fonte de cada frase.

## 18.6 Onde o briefing aparece

> **Decisão: canal do briefing.** Avaliamos **A, e-mail para você mesma**, **B, um
> evento "Briefing" na agenda** e **C, página no console com o resumo no Início**, e
> optamos por **C**:
> - **A** exigiria `gmail.send`, a permissão que o projeto decidiu nunca pedir (seção 5
>   da Arquitetura: o sistema não envia e-mail).
> - **B** é possível com a permissão de agenda que já existe, mas poluiria a agenda com
>   um evento por dia por Empresa, e o texto do evento ficaria nos servidores do Google
>   fora do alcance do console.
> - **C** não muda nada em segurança: continua tudo dentro da rede Tailscale.
>
> Fora do console, o caminho é o bot do Google Chat (fase 2). O menu ganha um **ponto
> azul** ao lado de "Briefing" quando o de hoje foi montado e você ainda não abriu.

## 18.7 Quando ele é montado

- O coletor tenta a cada 5 minutos, depois dos alertas (o das 7h já sai com os alertas
  da madrugada). A partir de `BRIEFING_HOUR` (7, no fuso do console), monta o de cada
  Empresa que ainda não tem o de hoje. Se o coletor estava parado às 7h, monta quando
  voltar, no mesmo dia.
- **Gerar agora / Gerar de novo** (página e Início) e o comando `gerar-briefing` montam
  na hora, sem esperar a hora do coletor. `--refazer` substitui o de hoje.
- Fins de semana: monta todo dia. Como a janela é "desde o último que você abriu", não
  abrir no sábado e no domingo não perde nada na segunda.
- Cada montagem vai para o diário (Processamento) como `briefing.gerado`, com os números.

## 18.8 O que fica de fora (por enquanto)

- **Prazos suspeitos** (trecho não conferido) não entram, pelo mesmo motivo dos alertas:
  uma data inventada com cara de certeza é pior que nenhuma. Eles aparecem como número em
  **Pendências**, com o link para a página Fatos.
- **Reuniões de dia inteiro** (feriado, férias) ficam de fora da agenda.
- **Criar eventos e rascunhos de resposta**, as outras peças do bloco 5, continuam
  planejadas.
- **Notificação no celular**: só com o bot do Google Chat (fase 2).

## 18.9 Para estudar no código

- `window_start`: um bom exemplo de regra de negócio escrita como três consultas
  pequenas, cada uma com o seu porquê, em vez de uma consulta gigante.
- `agenda_section`: o `try/except` em volta da leitura da agenda transforma qualquer
  falha do Google numa mensagem; o resto do briefing sai mesmo assim. É o padrão "uma
  parte que falha não derruba o todo", o mesmo do agendador (`safe` em `app/cli.py`).
- `changes_section`: `outerjoin` com a classificação para contar também os itens ainda
  sem projeto, e uma função interna (`entry`) que cria a entrada do projeto na primeira
  vez que ele aparece.
- `tests/test_briefing.py`: o "agora" é fixo (`NOW`) e a agenda é falsa
  (`FakeCalendar`), para o teste dar o mesmo resultado em qualquer dia e hora.
