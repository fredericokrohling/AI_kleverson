# 15. Reuniões: a reunião como uma coisa só, acima das transcrições

Pergunta da dona (03/11/2026): "a transcrição está ligada a uma reunião no banco? Estamos
guardando a data, o título, os participantes e a Empresa?" A resposta honesta era "mais
ou menos": cada transcrição guardava a sua cópia da reunião, de um jeito diferente
conforme a origem. Este guia explica o que estava errado, o que mudou e por quê.

## O problema

Antes desta mudança:

| Origem | Onde a reunião ficava | O que faltava |
| --- | --- | --- |
| Importada de um evento | no "extra" (JSON) da transcrição | participantes eram texto, não Pessoas |
| Importada fora da agenda | só o título e o dia | o horário (ficava meia-noite) |
| Extensão | na captura ao vivo (`live_meetings`) | o evento da agenda (achado no começo e jogado fora) |

Consequências:

1. **Uma reunião sem transcrição não existia**, e duas transcrições da mesma reunião (a
   importada do Meet e a da extensão) eram dois itens soltos, sem nada que as juntasse.
2. **Os convidados não eram Pessoas.** Para saber "com quem a dona se reuniu nesta
   semana" seria preciso abrir o JSON de cada transcrição e comparar nomes.
3. Cada parte do código (tags, classificação, reconhecimento de quem falou) lia os
   convidados de um lugar diferente.

## A mudança

Duas tabelas novas:

- **`meetings`**: uma linha por reunião, com Empresa (obrigatória), id do evento da
  agenda, título, início, fim, organizador, endereço e plataforma;
- **`meeting_attendees`**: uma linha por participante, com nome, e-mail e a Pessoa
  (ligada pelo e-mail, dentro da Empresa).

E uma coluna `meeting_id` na transcrição (`bronze_items`) e na captura ao vivo
(`live_meetings`).

> **Decisão:** Avaliamos três caminhos: continuar no JSON e criar uma "view" por cima;
> ligar as transcrições entre si (uma aponta para a outra); e uma tabela de reuniões.
> Optamos pela **tabela**, porque a reunião é o que a ata e o relatório contam, e porque
> participantes precisam ser linhas ligadas às Pessoas (uma view sobre JSON não liga
> nada e não deixa o banco conferir a Empresa).

### Uma reunião da agenda é uma linha **por Empresa**

O mesmo evento pode estar na agenda das contas de duas Empresas (as duas foram
convidadas). Ele vira **duas** reuniões, uma em cada Empresa, com a regra "não repete"
`(Empresa, id do evento)`. A alternativa (uma reunião compartilhada) quebraria a
premissa 1: os participantes de uma Empresa apareceriam na outra.

### O "extra" da transcrição continua como estava

A transcrição é bronze: o que chegou, como chegou. O evento guardado no "extra" fica lá,
como registro da origem. A **tabela** passa a ser a fonte que o código lê (tags,
classificação, reconhecimento de quem falou).

### Como a transcrição da extensão encontra a importada

Pelo **evento da agenda**. Quando a extensão começa, o console procura o endereço do Meet
nos eventos de hoje da agenda de cada Empresa (isso já existia para descobrir a Empresa).
Agora, além da Empresa, ele **guarda o evento**: a captura já nasce ligada à reunião do
evento. Quando você importa depois a transcrição do Meet do mesmo evento, a importação
acha a reunião pelo id do evento e as duas ficam juntas. As duas também recebem o mesmo
`thread_id` (`agenda:<id do evento>`), e a classificação por projeto trata as duas como a
mesma conversa.

Fora da agenda, não há como saber que duas transcrições são da mesma reunião: cada uma
vira a sua.

### A Empresa de uma reunião não muda

Como os cadastros, a reunião tem a trava `empresa_fixa()` da migração 0008. Corrigir a
Empresa de uma transcrição não muda a reunião: liga a transcrição à reunião do mesmo
evento na Empresa nova (ou a uma cópia), com os participantes procurados de novo entre as
Pessoas de lá. A reunião antiga é apagada se ficar vazia. E o gatilho `empresa_coerente()`
passou a conferir também que a transcrição, a captura e os participantes são da mesma
Empresa da reunião.

### A migração preenche o que já existe

A migração 0011 cria as reuniões das transcrições que já estavam no banco. A parte mais
delicada é juntar uma transcrição antiga da extensão (que não guardava o evento) com uma
importada: ela compara o **código do Meet** e o **horário**, com uma hora de folga. A
folga existe porque a captura começa quando você clica, não na hora marcada. A migração
tem uma cópia própria das funções de nome e de código do Meet, em vez de importar do app:
uma migração precisa rodar igual daqui a um ano, mesmo que o código do app mude.

## O que ainda falta (e é dito na cara)

- **Reunião da agenda sem transcrição não é gravada.** A reunião nasce da transcrição ou
  da captura. Para a ata semanal ("teve reunião e ninguém transcreveu"), será preciso
  copiar a agenda inteira para a tabela, de tempos em tempos.
- **Não há botão para juntar duas reuniões** (por exemplo, duas importações fora da
  agenda da mesma reunião).
- **Quem participou de verdade ≠ quem foi convidado.** A tabela junta os convidados da
  agenda e quem você cadastrou ao vivo; não sabe quem faltou. O Meet sabe, mas só pela
  API do Meet, que ainda não usamos.

## Onde está no código

- `migrations/versions/0011_reunioes.py`: as tabelas, as travas e o preenchimento.
- `app/meetings.py`: achar ou criar a reunião de um evento, criar fora da agenda, ligar a
  captura ao vivo, participantes, e a correção de Empresa.
- `app/transcription/importer.py`, `app/transcription/store.py`,
  `app/transcription/routes.py` e `app/transcription/routes_live.py`: onde a reunião é
  criada ou ligada.
- `app/transcription/agenda.py`: o calendário da página Reuniões acha a transcrição pela
  reunião (agora também a da extensão).
- `tests/test_meetings.py`: os testes.
