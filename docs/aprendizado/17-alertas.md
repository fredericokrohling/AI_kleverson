# 17. Alertas de liderança: regras são filtros, não perguntas à IA

Este guia explica o terceiro pedaço do **bloco 3 (silver)**: avisar o que as lideranças
pediram, decidiram ou apontaram. O código está em `app/alerts.py`; a tela, em
**Alertas** (menu Trabalho).

## 17.1 Por que este é o próximo passo do roadmap

A Arquitetura (seção 17) deixou um único item aberto no bloco 3: "desenhar as regras de
alerta sobre pedidos e decisões de lideranças". Ele é **requisito seu desde
02/11/2026** (alertas por liderança na empresa **e** no projeto), e tudo o que ele
precisa já existe:

- a **liderança** está nos cadastros (PR #10): `stakeholders.is_leader` (na empresa) e
  `project_stakeholders.is_leader` (no projeto);
- os **fatos** (PR #19) sabem o tipo, o responsável, o prazo e **quem disse**
  (`facts.said_by_id`).

Os outros candidatos esperam por ele ou por outra coisa:

- **Briefing diário** (bloco 5) é a lista de "o que precisa de você hoje": sem alertas,
  ele seria só a agenda. Com alertas, o briefing é agenda + alertas novos + prazos.
- **Relatórios e MCP** (blocos 7 e 8) leem os fatos; ganham pouco com alertas, mas
  também não perdem nada esperando.

Crítica franca: o requisito dizia "regras a desenhar com a dona". Eu não esperei o
desenho: as regras ficaram **editáveis por você no console**, com quatro prontas para
começar. Se o desenho que você tem em mente não cabe num filtro (por exemplo "avise se
a diretora pediu a mesma coisa duas vezes"), é sinal de que precisamos da parte 2 (seção
17.6), e não de mexer nesta.

## 17.2 A decisão central: filtro ou pergunta ao Claude

> **Decisão: o que é uma regra.** Avaliamos **A, regra em texto livre que o Claude
> julga** ("me avise quando a diretoria pedir algo urgente") e **B, regra como filtro
> sobre os fatos** (tipos, liderança, responsável, prazo), e optamos por **B**:
> - **Explicável:** cada alerta diz por que disparou ("Márcia, liderança no projeto,
>   pediu uma tarefa"). Com A, o motivo é "o Claude achou".
> - **Repetível:** o mesmo fato dá o mesmo resultado sempre. Com A, o mesmo fato pode
>   disparar hoje e não amanhã.
> - **Custo zero:** é uma consulta SQL. Com A, cada regra × cada fato novo seria uma
>   chamada ao Claude a cada 5 minutos.
> - **O Claude já trabalhou:** a parte difícil (ler o texto, achar o pedido, o prazo e
>   quem disse) foi feita na extração. Pedir de novo seria pagar duas vezes.

## 17.3 O que um fato precisa para virar alerta

A consulta está em `matching()`. Um fato só entra se:

1. **o trecho foi conferido** pelo código (`quote_verified = true`);
2. **quem disse foi reconhecido** como Pessoa da mesma Empresa (`said_by_id`);
3. está **valendo** (não substituído), **com projeto** e **não concluído**;
4. passa nos filtros da regra (tipos, liderança, responsável, prazo, Empresa);
5. o item é dos **últimos 30 dias** (`ALERTS_LOOKBACK_DAYS`), ou, em regra de prazo, o
   prazo está entre hoje − 30 dias e hoje + N dias;
6. ainda não há alerta **desta regra** para **este fato**.

> **Decisão: suspeitos e não reconhecidos ficam de fora.** Avaliamos **A, alertar tudo
> e marcar os duvidosos** e **B, só alertar o que tem prova e autor**, e optamos por
> **B**: "a diretora pediu X" com a pessoa errada ou com um pedido inventado leva a uma
> ação errada com cara de certeza. É a mesma regra do PR #10 para as falas: nenhum
> alerta é melhor que um alerta falso. Os duvidosos continuam à vista na página Fatos.

> **Decisão: janela de 30 dias.** Avaliamos **A, aplicar a regra a todo o histórico** e
> **B, só aos itens recentes**, e optamos por **B**: uma regra criada daqui a seis meses
> dispararia centenas de alertas de assuntos já resolvidos, e a lista deixaria de ser
> lida. Para o histórico existe a página Fatos.

### Liderança na empresa e no projeto

São duas marcas diferentes, e a regra escolhe qual conta:

| Opção | Conta quem | Exemplo |
| --- | --- | --- |
| Liderança na empresa ou no projeto | qualquer das duas | a diretora **ou** a PO |
| Só liderança na empresa | marca na Pessoa | a diretora de TI, em qualquer projeto |
| Só liderança no projeto | marca na equipe do projeto **do fato** | a PO do Atlas, só nos fatos do Atlas |

A liderança no projeto é conferida no projeto **do fato**: a PO do Atlas falando sobre o
Boreal não é liderança ali. O alerta grava as duas marcas no momento em que nasce
(`by_company_leader`, `by_project_leader`), para a tela dizer por que disparou mesmo se
o cadastro mudar depois.

### "O responsável sou eu"

O sistema não tem uma "Pessoa dona" separada. Você é reconhecida pela Pessoa cujo e-mail
é o **login do Tailscale** (o único que abre o console) ou o da **conta Google
conectada** naquela Empresa. Por isso é preciso cadastrar-se como Pessoa em cada
Empresa. Alternativa descartada: um campo "sou eu" na Pessoa; seria mais um dado para
manter em dia, e o e-mail já existe.

## 17.4 Um alerta por (regra, fato), uma linha por fato

O banco tem a regra "**o par (regra, fato) é único**" (`UniqueConstraint`). Por isso o
coletor pode rodar a cada 5 minutos sem repetir nada: o que já disparou não dispara de
novo, nem depois de você marcar como visto.

Mas duas regras diferentes podem disparar para o mesmo fato: um pedido da diretora com
prazo amanhã passa em "Pedido de liderança" e em "Prazo em até 3 dias". Guardar os dois
alertas é útil (a regra de prazo pode disparar **dias depois** do pedido, como lembrete).
Mostrar os dois como linhas separadas seria ruído. Então:

- a página mostra **uma linha por fato**: o alerta mais forte, com "também: outra
  regra" embaixo (`group_by_fact`);
- o número do menu e os números da página contam **fatos**, não alertas;
- **Visto**, **Resolvido** e **Descartar** valem para todos os alertas abertos do fato
  (`set_status`). **Reabrir** vale só para o clicado.

## 17.5 Resolver fecha o fato

Os fatos de tarefa, risco, problema, dependência e prazo nascem "aberto" (PR #19), e
até aqui não havia como fechar. **Resolvido** muda o fato para "concluido". Efeitos:

- as outras regras param de avisar sobre ele (a consulta exclui fatos concluídos);
- **Reabrir** um alerta resolvido devolve o fato para "aberto".

**Descartar** não mexe no fato: serve para o alerta que não procede (pessoa reconhecida
errada, por exemplo). O fato continua aberto e visível na página Fatos.

## 17.6 Limites conhecidos (crítica franca)

- **O alerta é tão bom quanto a extração.** Se o Haiku não reconheceu um pedido como
  tarefa, não há alerta. A contagem de suspeitos (guia 16) continua sendo a medida.
- **O mesmo pedido no e-mail e na reunião** são dois fatos, logo dois alertas. Juntar
  fatos parecidos é o trabalho do auditor da fase 2.
- **Só filtros por fato.** Regras de padrão ("pediu a mesma coisa três vezes", "decisão
  que contradiz outra", "ninguém respondeu em 2 dias") exigem olhar vários fatos juntos;
  ficam para uma parte 2, quando você tiver usado estas por algumas semanas e souber
  quais perguntas faltam.
- **Sem aviso fora do console.** O alerta aparece no menu e na página; não vai por
  e-mail nem por Chat. O briefing diário (bloco 5) é o lugar natural para isso.
- **"Extrair de novo" apaga os alertas** do item junto com os fatos (inclusive os
  resolvidos), porque os fatos novos são outros registros.

## 17.7 Onde está cada coisa no código

| O quê | Onde |
| --- | --- |
| Tabelas `alert_rules` e `alerts` | `app/models.py` (AlertRule, Alert) e `migrations/versions/0013_alertas_de_lideranca.py` |
| Regras-padrão | `DEFAULT_RULES` na migração 0013 |
| Conferir e gravar uma regra | `save_rule` em `app/alerts.py` |
| A consulta de cada regra | `matching` em `app/alerts.py` |
| A rodada (coletor e comando) | `generate` e `run_from_settings` em `app/alerts.py`; `cmd_gerar_alertas` e o laço em `app/cli.py` |
| Visto, resolvido, descartado | `set_status` em `app/alerts.py` |
| Página e formulário | `app/routes_alerts.py`, `app/templates/alertas.html` e `alertas_regra.html` |
| Número no menu | `count_new` em `app/alerts.py`, lido por `app/web.py` |
| Testes | `tests/test_alerts.py` |
