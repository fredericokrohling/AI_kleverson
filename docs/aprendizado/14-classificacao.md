# 14. Classificação por projeto: regras primeiro, Claude só no que sobrar

Este guia explica o primeiro pedaço do **bloco 3 (silver)**: decidir a qual projeto cada
item coletado pertence. O código está em `app/classification.py`; a tela, em
**Classificação** (menu Trabalho).

## 14.1 Por que este é o próximo passo do roadmap

Tudo o que vem depois depende disto:

- **Extração de fatos** (tarefas, decisões, riscos, prazos): um fato precisa de projeto.
  Sem saber o projeto do e-mail, uma tarefa extraída fica solta.
- **Alertas de liderança**: "o patrocinador do Portal pediu X" só faz sentido se a fala
  ou o e-mail estiver ligado ao Portal.
- **Relatórios por projeto, MCP e o Markdown de auditoria**: todos filtram por projeto.

O backlog tinha outros itens (o aviso de microfone aberto nas duas reuniões, por
exemplo), mas eles não destravam nada. A classificação é o **caminho crítico**: enquanto
ela não existe, os blocos 4 a 8 não andam.

## 14.2 Tag e projeto não são a mesma coisa

Desde o PR #10, um e-mail recebe **várias tags** de projeto (uma por regra que achar
algo). Isso é bom para buscar, mas não serve para contar: se o mesmo e-mail estiver em
dois projetos, um relatório de "e-mails por projeto" conta duas vezes, e uma tarefa
extraída dele teria dois donos.

Por isso a classificação escolhe **um projeto principal** (ou "nenhum"). As tags
continuam como estão; a classificação **lê** as tags como sinais.

> **Decisão: ler as tags em vez de reler o texto.** Avaliamos **A, refazer as regras
> lendo o texto de novo** e **B, usar as tags que as regras já gravaram**, e optamos por
> **B**: as tags guardam qual regra achou o quê (pasta, equipe, nome no texto, à mão) e
> respeitam as tags que você tirou. Uma fonte só da verdade: se a regra de tag melhorar,
> a classificação melhora junto, sem duplicar código.

## 14.3 A cascata

Do sinal mais barato e confiável ao mais caro:

| Passo | Sinal | Método gravado | Confiança |
| --- | --- | --- | --- |
| 1 | Você escolheu no console (fica **travada**) | `manual` | 100% |
| 2 | Você pôs à mão a tag de **um só** projeto | `manual` | 100% |
| 3 | Pasta do Gmail ou do Drive ligada a **um só** projeto | `pasta` | 95% |
| 4 | Outro item da **mesma conversa** já é de um projeto | `mesma_conversa` | 90% |
| 5 | Equipe e nome no texto apontam **um só** projeto | `stakeholder` / `palavra_chave` | 85% / 80% |
| 6 | Sinais de projetos diferentes, ou nenhum sinal | `llm` (Claude) | a que ele der |
| 7 | Claude com confiança abaixo de 70% | fila **A revisar** | você decide |

As confianças dos passos 1 a 5 **não são estatística**: são a ordem de força dos sinais,
para a tela mostrar e para você comparar com a nota do Claude.

**Por que a pasta vem antes da conversa:** a pasta é a sua organização explícita. Se
você moveu o e-mail para "Clientes/Portal", isso vale mais que a mensagem anterior da
conversa.

**Conflito entre equipe e texto:** a Arquitetura dizia que "stakeholder exclusivo decide
sozinho". Na prática, a Márcia (equipe só do Portal) pode escrever sobre o ERP. Quando a
equipe aponta um projeto e o texto aponta outro, a cascata **não escolhe às cegas**: manda
os dois ao Claude, que lê o texto. É um ajuste da regra original, registrado na
Arquitetura.

**Sem sinal nenhum:** o Claude recebe todos os projetos ativos da Empresa e pode
responder `null` ("não é de projeto nenhum": newsletter, assunto pessoal). Isso evita que
uma propaganda entre num projeto só porque precisava ir para algum.

## 14.4 Por que o Haiku, e não o modelo do console

O console usa o Opus por padrão. Para classificar, o padrão é o **Haiku**
(`CLASSIFY_MODEL=claude-haiku-5-5`), com esforço baixo.

> **Decisão: modelo da classificação.** Avaliamos **A, o modelo do console (Opus)** e
> **B, Haiku fixo para classificar**, e optamos por **B**:
> - classificar é escolher um item numa lista curta, com o texto na mão; não exige o
>   raciocínio do Opus;
> - o Haiku custa cerca de **40 vezes menos**. Conta aproximada por item: 1.500 tokens de
>   entrada e 300 de saída dão **US$ 0,0003** no Haiku e cerca de **US$ 0,012** no Opus.
>   Com 100 itens por dia indo ao Claude, são **US$ 0,90 por mês** contra **US$ 36 por
>   mês**, o que estouraria o teto de US$ 10;
> - se a qualidade não bastar, troque `CLASSIFY_MODEL` no `.env` para
>   `claude-sonnet-5-5` (metade do preço do Opus) e compare pela fila "A revisar".

Medida de verdade: a página Classificação mostra **quanto do total foi decidido sem
custo** e o **gasto do mês só com classificação**. A meta da Arquitetura é **90% de
acerto**; para medir, use a fila e corrija o que estiver errado.

## 14.5 O que impede o Claude de inventar

- **A resposta é conferida por código.** O Claude devolve JSON
  (`{"projeto": id, "confianca": 0..1, "motivo": "..."}`). Se o id não estiver na lista
  enviada, se o JSON vier quebrado ou se ele recusar, o item vai para **A revisar** com
  o motivo, em vez de ser gravado errado.
- **Nada é pago duas vezes à toa.** Resposta ilegível vira linha na fila (e não volta ao
  Claude na rodada seguinte). Se os sinais mudarem e os candidatos forem os mesmos de
  antes, a decisão do Claude é mantida sem nova chamada.
- **Separação por Empresa (premissa 6).** O pedido ao Claude leva só o item e os
  projetos da Empresa dele. O banco também recusa gravar projeto de outra Empresa (trava
  da migração 0008), e a escolha no console confere a Empresa antes.
- **Texto cortado avisando.** Só os primeiros 4.000 caracteres do texto vão ao Claude
  (`CLASSIFY_MAX_CHARS`); o pedido diz que houve corte. No e-mail, vai o texto próprio da
  mensagem, sem as respostas citadas e sem a assinatura, mais os nomes dos anexos.

## 14.6 Quando a decisão é refeita

- **Tag colocada ou tirada depois da decisão** (por você, por uma pasta do Gmail ou por
  um projeto novo): o item volta à fila do coletor e é decidido de novo. É assim que a
  classificação acompanha um cadastro novo sem reprocessar tudo.
- **Escolha sua no console:** fica travada (`locked`). Nem tag nova muda. Para soltar,
  abra o item e clique em **Voltar à classificação automática**.
- **Comando `classificar --refazer`:** apaga as decisões automáticas e refaz tudo (as
  suas ficam). Use depois de mudar muito os cadastros, por exemplo ao ligar pastas do
  Drive a projetos (a herança de tags do Drive não tem data, então não reabre sozinha).

## 14.7 Limitações conhecidas

- **Um projeto principal por item.** Um e-mail que trata de dois projetos fica num só;
  as tags continuam mostrando os dois. A extração de fatos (próximo PR) pode ligar cada
  fato ao projeto certo, mesmo quando o item é de outro.
- **Pasta tirada no Gmail:** a coleta apaga a tag da pasta (não marca como tirada), e
  isso não deixa rastro de data. O item mantém a classificação "pasta" até um
  `classificar --refazer`.
- **Projeto encerrado** (inativo) não recebe classificação nova, mas as antigas ficam.
- **Agenda e Google Chat** ainda não são classificados (não são coletados como itens).
