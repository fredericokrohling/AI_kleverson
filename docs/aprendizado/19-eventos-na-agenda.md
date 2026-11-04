# 19. Pôr prazos na agenda: a primeira vez que o assistente escreve no Google

Este guia explica a segunda peça do **bloco 5 (ações)**: o botão **Pôr na agenda** e a
página **Na agenda**. O código está em `app/calendar_events.py` (as regras) e
`app/routes_events.py` (a página e os botões).

## 19.1 Por que este é o próximo passo do roadmap

A seção 11 da Arquitetura lista três peças no bloco 5: briefing, eventos na agenda e
rascunhos de resposta. O briefing saiu no PR #21. Entre as duas que sobraram, eventos
vieram antes porque:

- **não pedem permissão nova.** A permissão `calendar.events` (ler e criar eventos) está
  pedida desde o bloco 0; você já a concedeu em cada conta conectada;
- **não custam IA.** O evento é montado do fato que já existe, por código. Rascunhos de
  resposta chamam o Claude a cada e-mail e precisam de uma decisão de custo e de modelo;
- **fecham o ciclo do briefing.** O briefing mostra os prazos de manhã; agora dá para,
  com um clique, levar o prazo para onde você já olha o dia inteiro: a agenda.

Crítica franca: o requisito original dizia "leitura e criação de eventos na agenda", sem
dizer *quais* eventos. Eu escolhi **prazos de fatos**, porque é o único dado do sistema
que tem dia certo e prova no texto. Criar reuniões (com convidados) ficou de fora de
propósito, como explica a seção 19.3.

## 19.2 O que acontece quando você clica

O botão aparece ao lado de cada prazo nas páginas **Fatos**, **Briefing** (seção Prazos) e
**Alertas**, mas só nos fatos que passam na mesma régua dos alertas:

| Condição | Por quê |
| --- | --- |
| tem prazo | sem dia, não há onde pôr o evento |
| trecho **conferido** | um evento errado na sua agenda é pior que nenhum; o suspeito você confere antes, na origem |
| fato valendo (não substituído) | o mais novo é o que vale |
| não **concluído** | prazo fechado não precisa de lembrete |

O código confere tudo de novo no servidor (`why_not`): o formulário manda só o id do
fato, e título, dia e descrição vêm do banco. Um formulário forjado não consegue pôr
outro texto na sua agenda.

O evento criado é assim:

- **título** com o tipo na frente: `Tarefa: Enviar cronograma revisado`;
- **dia inteiro**, no dia do prazo;
- **livre** (`transparency: transparent`): não bloqueia horário, ninguém deixa de marcar
  reunião com você por causa dele;
- **privado** (`visibility: private`): quem vê a sua agenda (colegas na conta da
  empresa) vê só "ocupado", sem o texto do cliente;
- **descrição** com o projeto, o responsável, o trecho citado e o link para a origem no
  console (que só abre na sua rede Tailscale);
- **lembretes**: os seus lembretes padrão de evento de dia inteiro;
- **na agenda da conta Google da Empresa do fato**: um prazo da Acme vai para a conta da
  Acme, nunca para a de outro cliente.

Eventos de dia inteiro não entram na seção Agenda do briefing, nem viram reunião em
Reuniões: essas telas só leem eventos com horário. Então o prazo na agenda não "volta"
para o sistema como se fosse uma reunião.

## 19.3 As quatro regras de quem escreve na sua agenda

Até aqui o sistema só lia. A partir deste PR ele escreve, e por isso o módulo segue
quatro regras, cada uma com um teste em `tests/test_calendar_events.py`.

> **Decisão: quando criar eventos.** Avaliamos **A, o coletor criar sozinho um evento
> para cada prazo novo** e **B, só quando você clica em "Pôr na agenda"**, e optamos por
> **B**:
> - A extração erra. Um prazo inventado, criado sozinho, aparece na sua agenda como se
>   fosse verdade, e você só descobre na data.
> - Num dia com 20 e-mails, A encheria a agenda de "Prazo: ..." que você não pediu.
> - Com B, o clique é a sua conferência. O custo é um clique por prazo.
> - **Revisitar** quando a extração tiver uma taxa de acerto medida (meta do guia 16).

> **Decisão: convidados.** Avaliamos **A, criar o evento com os responsáveis como
> convidados** e **B, nunca pôr convidados, e criar com `sendUpdates="none"`**, e
> optamos por **B**:
> - Um convite é uma mensagem que sai em seu nome para outra pessoa. O projeto decidiu
>   que o assistente não manda mensagem a ninguém: é a mesma regra que tirou o
>   `gmail.send` e o `gmail.compose` (seção 5 da Arquitetura).
> - Um bug que pusesse o cliente errado como convidado seria um vazamento de dados para
>   fora, visível e sem volta.
> - O teste `test_event_body_rules` falha se o corpo do evento tiver `attendees` ou
>   videoconferência; os testes de criar e apagar conferem `sendUpdates="none"`.

> **Decisão: como saber que um evento é do assistente.** Avaliamos **A, guardar só o id
> do evento no banco**, **B, um calendário separado "Assistente"** e **C, uma marca
> invisível no próprio evento (propriedade privada `kleverson=1`) mais o registro no
> banco**, e optamos por **C**:
> - **A** se perde se o banco for restaurado de um backup antigo: os eventos continuam
>   na agenda e ninguém sabe quais são do assistente.
> - **B** foi descartado na decisão "onde criar eventos" (seção 11): você quer os
>   prazos onde já trabalha.
> - **C**: a marca viaja com o evento. A API do Google busca por ela
>   (`privateExtendedProperty`), então "Apagar todos" acha até os eventos sem registro.
>   Propriedades privadas só aparecem para a dona da agenda, não no evento nem para
>   quem você convidar depois.

> **Decisão: o que o assistente pode apagar.** Avaliamos **A, apagar pelo id guardado no
> banco** e **B, ler o evento antes e só apagar se ele tiver a marca**, e optamos por
> **B**:
> - Um id errado no banco (bug, restauração, edição à mão) com A apagaria um evento seu,
>   por exemplo uma reunião com o cliente.
> - Com B, o pior caso é o console dizer "o evento não tem a marca; nada foi apagado".
> - Custa uma leitura a mais por apagamento, nada perto do risco.

## 19.4 Por que uma tabela, se a marca já está no evento

A tabela `created_events` (migração 0015) é o **índice do console**, não a prova:

- a página **Na agenda** lista os eventos sem perguntar ao Google, então abre mesmo com
  a conta desconectada;
- o **índice único parcial** `uq_created_events_fact_ativo` impede dois eventos ativos
  para o mesmo fato. "Parcial" quer dizer que a regra só vale para as linhas com
  `removed_at` vazio: depois de apagar, dá para pôr de novo;
- guarda **quem pediu e quem apagou**, e o diário (`audit_log`) registra cada ação:
  `agenda.evento_criado`, `agenda.evento_apagado`, `agenda.evento_sumiu`,
  `agenda.conferir` e `agenda.apagar_todos`.

A ordem dentro de `create_for_fact` importa. O registro é gravado **antes** de chamar o
Google, dentro de um *ponto de salvamento* (`session.begin_nested()`):

1. grava o registro (aqui o índice único barra o segundo de dois cliques seguidos, antes
   de o segundo evento existir no Google);
2. chama o Google, já com o id do registro na marca;
3. se o Google falhar (sem conta, 403, rede), desfaz só o ponto de salvamento: não fica
   registro pela metade;
4. se der certo, guarda o id do evento e fecha.

O único buraco que sobra: o Google criar o evento e o banco cair antes do commit. O
evento fica na agenda com a marca e sem registro. **Conferir com a agenda** mostra
"N com a marca, mas sem registro", e **Apagar todos** tira esses também.

## 19.5 Quando você mexe direto no Google

Você pode apagar ou arrastar o evento pelo próprio Google Agenda, e o console não fica
sabendo na hora (não há notificação push; ver a decisão "como receber novidades" na
seção 5). Por isso há o botão **Conferir com a agenda**, por Empresa:

- evento **apagado** no Google: o registro passa a "apagado", com a nota "já tinha sido
  apagado na agenda";
- evento **arrastado** para outro dia: o registro acompanha o dia novo;
- evento com a marca **sem registro**: aparece na contagem, para você decidir.

"Apagar da agenda", de um evento só, também percebe sozinho que você já apagou (o Google
responde 404 ou 410) e só acerta o registro.

**"Extrair de novo"** apaga os fatos de um item. O registro do evento fica (a chave
estrangeira é `ondelete="SET NULL"`): o evento continua na agenda e precisa continuar
apagável. A página mostra "o fato foi extraído de novo; o evento continua na agenda".

## 19.6 Desfazer em lote

Três caminhos, do mais fino ao mais grosso:

1. **Apagar da agenda**, na linha do evento;
2. **Apagar todos**, por Empresa, na página Na agenda (pede confirmação);
3. o comando `python -m app.cli apagar-eventos-criados [--empresa ID]`, que faz o mesmo
   que o 2 pelo terminal, para todas as Empresas ou uma. Serve se o console estiver fora
   do ar.

Os dois últimos buscam no Google pela marca, não pelo banco. Nenhum deles toca num
evento sem a marca.

## 19.7 O que ficou de fora (e quando voltar)

- **Mover o evento quando o prazo muda:** um fato não muda de prazo; um prazo novo vira
  um fato novo. Se isso acontecer, apague o evento antigo e ponha o novo. A página mostra
  "prazo no fato: dd/mm" quando o dia do evento e o do fato não batem (por exemplo,
  depois de você arrastar o evento).
- **Reuniões com convidados** (ex.: "marcar a reunião de follow-up"): ficam para quando
  houver um jeito seguro de você revisar o convite antes. Hoje a regra é: ninguém
  recebe nada do assistente.
- **Rascunhos de resposta:** a próxima peça do bloco 5, com Claude, e por isso com uma
  decisão própria de custo.
