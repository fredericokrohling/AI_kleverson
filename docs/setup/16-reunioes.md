# 16. Reuniões: uma linha por reunião, com data, participantes e as transcrições dela

**O que muda (pedido da dona em 03/11/2026):**

- **Banco:** tabela nova **meetings** (a reunião: Empresa, evento da agenda, título,
  início, fim, organizador, endereço e plataforma) e **meeting_attendees** (os
  participantes, cada um ligado à Pessoa pelo e-mail). Toda transcrição aponta para a
  reunião dela.
- **Mesma reunião, transcrições juntas:** a transcrição importada do Teams ou do Meet e a
  feita pela extensão ficam na mesma reunião quando são do mesmo evento da agenda.
- **Extensão:** quando o endereço do Meet está num evento da agenda, a captura já nasce
  ligada à reunião do evento (data, horário, organizador e convidados vêm da agenda).
  Isso vale também quando você escolhe a Empresa no Início ou clica em **Trazer
  convidados da agenda**. A extensão em si **não muda**: não precisa recarregar.
- **Importar fora da agenda:** campo novo **Início** (opcional) na página Reuniões. Sem
  ele, a reunião fica só com o dia e a tela diz "horário não informado".
- **Página de uma transcrição:** bloco **Reunião** com data e horário, se é da agenda,
  organizador, participantes (com link para a Pessoa) e as **outras transcrições desta
  reunião**.
- **Migração 0011:** cria as reuniões das transcrições que já existem (ver 16.2).

O porquê de cada escolha está em
[docs/aprendizado/15-reunioes.md](../aprendizado/15-reunioes.md).

## 16.1 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

Depois recarregue o console com **Ctrl+Shift+R**. Nos computadores com a extensão, só o
`git pull` (para ficar igual ao servidor); a extensão continua na 0.3.0.

## 16.2 O que a migração faz com o que já existe

1. **Transcrição importada de um evento da agenda:** uma reunião por evento em cada
   Empresa, com os convidados do evento. Duas importações do mesmo evento ficam na mesma.
2. **Transcrição feita pela extensão:** se o endereço do Meet e o horário (com 1 hora de
   folga) batem com uma reunião do passo 1 da mesma Empresa, é a mesma reunião. Senão,
   uma reunião nova com o título da aba, a hora de início e a de fim da captura, e os
   participantes que você cadastrou nela.
3. **Importada fora da agenda:** uma reunião por transcrição, só com o dia.

Em todas, o participante cujo e-mail é de uma Pessoa da Empresa fica ligado a ela.

## 16.3 Roteiro de testes

1. **Reuniões antigas:** abra uma transcrição importada de um evento (Início →
   Transcrições recentes). O bloco **Reunião** mostra o título do evento, a data com início
   e fim, "da agenda" e os convidados. Quem está em Pessoas aparece com link.
2. **Transcrição da extensão e importada juntas:** se você tem uma reunião transcrita
   pela extensão e importada do Meet, as duas páginas mostram **Outras transcrições desta
   reunião** com o link da outra.
3. **Reunião nova pela extensão:** comece a transcrever uma reunião que está na agenda.
   Encerre. Na página da transcrição, a reunião tem o título e o horário **da agenda** (não
   o título da aba), e os participantes são os convidados mais os que você incluiu.
4. **Depois, importe a transcrição do Meet da mesma reunião** (página Reuniões, clique
   nela no calendário). O calendário já mostrava a reunião como **transcrição ligada** (a
   da extensão); a importada cai na mesma reunião.
5. **Fora da agenda com horário:** importe uma transcrição com **Fora da agenda**, título
   e **Início** 10:00. A página mostra 10:00. Sem o Início, mostra "horário não informado".
6. **Pessoa cadastrada depois:** cadastre em Pessoas alguém que era só convidado (mesmo
   e-mail). Na página da transcrição, o nome vira link.
7. **Corrigir Empresa:** corrija a Empresa de uma transcrição importada. Na Empresa nova,
   ela fica numa reunião da Empresa nova (a do mesmo evento, se já existir lá), com os
   participantes ligados às Pessoas de lá.

## 16.4 Se algo der errado

- **A transcrição da extensão não ficou junto da importada:** a ligação é pelo evento da
  agenda. Ela não acontece se a reunião não tinha o endereço do Meet no convite, se a
  Empresa foi escolhida e a agenda dessa Empresa não tinha o evento, ou se a conta Google
  estava fora do ar naquela hora. Hoje não há botão para juntar duas reuniões à mão.
- **Reunião fora da agenda aparece duas vezes:** cada importação fora da agenda vira uma
  reunião própria (o console não tem como saber que são a mesma).
- **Reunião da agenda que não teve transcrição não aparece no banco:** por enquanto, a
  reunião só é gravada quando há uma transcrição ou captura dela. Gravar a agenda inteira
  fica para quando a ata e os relatórios precisarem.
