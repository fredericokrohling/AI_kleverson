# 20. Pôr prazos na agenda

**O que muda (bloco 5, segunda parte):**

- **Botão "Pôr na agenda"** ao lado de cada prazo nas páginas **Fatos**, **Briefing**
  (seção Prazos) e **Alertas**. Cria um evento de dia inteiro no dia do prazo, na agenda
  da conta Google da Empresa do fato: sem convidados (ninguém recebe aviso), livre (não
  bloqueia horário), privado e com uma marca invisível do assistente. Depois do clique, o
  botão vira **na agenda ✓**.
- **Página Na agenda** (menu Trabalho, abaixo de Alertas): o que o assistente pôs na sua
  agenda, com **Apagar da agenda** em cada um e, por Empresa, **Conferir com a agenda** e
  **Apagar todos**.
- **Comando novo:** `apagar-eventos-criados [--empresa ID]`.
- **Banco:** migração 0015 (tabela `created_events`).
- **Sem custo de IA. Sem permissão nova no Google:** a `calendar.events` já foi concedida
  quando você conectou cada conta.

O porquê de cada escolha está em
[docs/aprendizado/19-eventos-na-agenda.md](../aprendizado/19-eventos-na-agenda.md).

## 20.1 Antes de atualizar

Nada obrigatório. Só aparece botão em prazo **conferido** (não suspeito) e **aberto**.
Se uma Empresa não tem conta Google conectada, o clique mostra o aviso "Nenhuma conta
Google conectada" e nada é criado.

## 20.2 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

A migração 0015 roda sozinha. Não há variável nova no `.env`.

## 20.3 Roteiro de testes

1. **Fatos:** escolha um prazo conferido e clique em **Pôr na agenda**. A página volta
   com o aviso "“Tarefa: ...” foi para a agenda em dd/mm/aaaa" e o botão vira
   **na agenda ✓**.
2. **Google Agenda** da conta daquela Empresa: no dia do prazo há um evento de dia
   inteiro com o título "Tarefa: ..." (ou "Prazo:", "Dependência:"). Abra: a descrição
   traz o projeto, o responsável, o trecho e o link da origem. O evento está como
   **Livre** e **Particular**, sem convidados.
3. **Clique duplo:** clique de novo no mesmo fato por outra aba (antes de recarregar).
   Aviso vermelho "Este fato já está na agenda" e nenhum segundo evento no Google.
4. **Na agenda:** o evento aparece com o dia, "em N dia(s)", o projeto, a Empresa e quem
   criou.
5. **Apagar da agenda:** clique. O evento some do Google Agenda e a linha sai da lista
   (marque **mostrar apagados** para vê-la de novo).
6. **Apagado pelo Google:** ponha outro prazo na agenda, apague o evento direto no
   Google Agenda e clique em **Conferir com a agenda** daquela Empresa. Aviso "1 já
   tinha(m) sido apagado(s) pelo Google"; a linha vai para os apagados.
7. **Arrastado pelo Google:** ponha um prazo, arraste o evento para outro dia no Google,
   **Conferir**: o dia na lista acompanha; aparece "prazo no fato: dd/mm".
8. **Seus eventos ficam:** com dois ou três eventos do assistente na agenda, clique em
   **Apagar todos** (confirma). Só os do assistente somem; as suas reuniões continuam.
9. **Briefing e Alertas:** o botão aparece nos prazos do briefing de hoje e na coluna
   Prazo dos alertas.
10. **Duas Empresas:** um prazo da Empresa B vai para a agenda da conta da Empresa B.
11. **Terminal:**

    ```bash
    docker compose exec app python -m app.cli apagar-eventos-criados --empresa 1
    ```

    Mostra, por Empresa, quantos eventos saíram da agenda.

## 20.4 Se algo der errado

- **"A agenda recusou o pedido (HTTP 403)":** a conta pode ter tirado o acesso à agenda
  em `myaccount.google.com/permissions`, ou a Google Calendar API foi desligada no
  projeto do Google Cloud. Conecte a conta de novo em Configurações.
- **"Fato suspeito":** o trecho não foi achado no texto original. Abra a origem e
  confira; se o fato estiver certo, "Extrair de novo" no item costuma resolver.
- **"O evento na agenda não tem a marca do assistente; nada foi apagado":** o id
  guardado aponta para um evento que não foi criado pelo assistente. É a proteção
  funcionando. Use **Conferir com a agenda** e, se preciso, apague o evento à mão no
  Google.
- **Evento sem registro ("com a marca, mas sem registro no console"):** acontece depois
  de restaurar o banco de um backup. **Apagar todos** tira esses também.
