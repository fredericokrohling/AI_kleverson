# 19. Briefing diário

**O que muda (bloco 5, primeira parte):**

- **Página Briefing** (menu Trabalho, logo abaixo de Início): por Empresa, a **agenda**
  do dia (com as lideranças presentes e o que está aberto com quem vai estar), os
  **alertas novos**, os **prazos** (vencidos, de hoje e dos próximos 7 dias, marcando os
  seus), **o que mudou por projeto** desde o último briefing que você abriu e as
  **pendências** do sistema. Combo de Empresa, calendário e setas para os dias anteriores.
- **Início:** quadro **Briefing de hoje** com os números de cada Empresa.
- **Menu:** ponto azul ao lado de "Briefing" quando o de hoje ainda não foi aberto.
- **Coletor:** a partir das 7h (no fuso do console), monta o briefing de cada Empresa,
  uma vez por dia. **Sem custo de IA.**
- **Comando novo:** `gerar-briefing [--refazer]`.
- **Banco:** migração 0014 (tabela `briefings`).

O porquê de cada escolha está em
[docs/aprendizado/18-briefing.md](../aprendizado/18-briefing.md).

## 19.1 Antes de atualizar

Nada obrigatório. O briefing usa o que já existe; quanto mais completo, melhor ele fica:

- **conta Google conectada em cada Empresa** (Configurações → Contas conectadas): sem
  ela, a seção Agenda mostra "Nenhuma conta Google conectada" e o resto sai normalmente;
- **lideranças marcadas e você cadastrada como Pessoa** (guia 18, seção 18.1): é o que
  preenche "lideranças" na agenda e o selo **meu** nos prazos;
- **convidados das reuniões cadastrados como Pessoa** com o mesmo e-mail da agenda: é o
  que liga a reunião aos alertas abertos.

## 19.2 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

A migração 0014 roda sozinha. Opcional no `.env` (os padrões já servem):

```bash
# Hora (no fuso do console) a partir da qual o coletor monta o briefing (padrão: 7).
BRIEFING_HOUR=7
# Quantos dias à frente a seção Prazos olha (padrão: 7).
BRIEFING_DUE_DAYS=7
```

Mudou o `.env`? Rode `docker compose up -d` de novo para o coletor ler o valor novo.

## 19.3 O primeiro briefing

Não precisa esperar as 7h de amanhã:

```bash
docker compose exec app python -m app.cli gerar-briefing
```

Ele mostra algo como `Briefing: montado para Cliente Alfa, Cliente Beta.` Ou, no
console, **Início → Gerar agora**.

O primeiro briefing de cada Empresa olha as **últimas 24 horas** em "O que mudou". Os
seguintes olham desde o último que você abriu.

## 19.4 Roteiro de testes

1. **Início:** o quadro **Briefing de hoje** mostra uma linha por Empresa (reuniões,
   alertas novos, prazos vencidos, vencem em breve) e "ainda não aberto". O menu tem o
   ponto azul ao lado de **Briefing**.
2. **Abrir:** clique em **Abrir o briefing**. O ponto do menu some; no Início, a linha
   passa a "aberto às HH:MM".
3. **Agenda:** confira as reuniões de hoje contra o Google Agenda daquela conta (eventos
   de dia inteiro ficam de fora). Numa reunião com uma liderança cadastrada, aparece
   "lideranças: Nome"; se essa pessoa tem alerta aberto, aparece "N aberto(s) com quem
   vai estar", com o link para Alertas.
4. **Alertas:** os mesmos "Novos" da página Alertas (até 15), com o selo **chegou
   agora** nos que nasceram depois do último briefing que você abriu. Marque um como
   **Visto** em Alertas e clique em **Gerar de novo**: ele sai da lista.
5. **Prazos:** um prazo vencido aparece em **Vencidos** com "há N dia(s)"; um prazo seu
   (responsável = você) tem o selo **meu**. Marque o alerta dele como **Resolvido** e
   gere de novo: o prazo some (o fato foi concluído).
6. **O que mudou:** um cartão por projeto com e-mails, transcrições e arquivos que
   chegaram, os fatos novos e os destaques (decisões, riscos, problemas). Clique num
   destaque: abre o item de origem.
7. **Pendências:** os três números levam às páginas Classificação (fila A revisar),
   Fatos (só suspeitos) e Início (arquivos sem texto).
8. **Histórico:** amanhã, use a seta **← dia** ou o calendário para ver o briefing de
   hoje como ele estava.
9. **Duas Empresas:** com "Todas as Empresas", cada uma aparece separada, com o nome em
   cima. Nada de uma Empresa aparece no briefing da outra.

## 19.5 Se algo der errado

- **Nenhum briefing às 7h:** veja `docker compose logs coletor | grep -i briefing`. A
  linha esperada é `Briefing: montado para ...`. Se o coletor estava parado, ele monta
  quando voltar. Confira `CONSOLE_TIMEZONE` e `BRIEFING_HOUR` no `.env`.
- **Agenda vazia com aviso:** a mensagem diz o motivo (conta não conectada, acesso
  revogado, Google fora do ar). Corrija em Configurações e clique em **Gerar de novo**.
- **"O que mudou" vazio:** a janela começa no último briefing que você abriu; se você o
  abriu há poucos minutos, quase nada chegou depois. É o comportamento esperado.
- **Prazo que você sabe que existe não aparece:** o fato pode estar **suspeito** (veja
  em Fatos, filtro "só suspeitos"), concluído, ou com prazo depois dos próximos
  `BRIEFING_DUE_DAYS` dias.
