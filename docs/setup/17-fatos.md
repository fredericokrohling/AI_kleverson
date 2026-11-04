# 17. Extração de fatos com trecho conferido

**O que muda (bloco 3, segunda parte):**

- **Coletor:** a cada 5 minutos, depois da classificação, lê os e-mails, transcrições e
  arquivos do Drive que **já têm projeto** (e estão fora da fila A revisar) e extrai
  tarefas, decisões, riscos, problemas, premissas, dependências, prazos e definições.
- **Conferência do trecho:** cada fato vem com uma frase copiada do texto. O código
  procura essa frase no original: achou, o fato fica **conferido**; não achou, fica
  **suspeito**.
- **Página Fatos** (menu Trabalho, abaixo de Classificação): números, filtros por
  Empresa, projeto, tipo e "só suspeitos", e a lista com responsável, prazo, quem disse
  e o item de origem.
- **Página de cada item:** seção **Fatos** com o trecho de cada um e o botão
  **Extrair de novo**.
- **Comando novo:** `extrair-fatos [--refazer]`.
- **Banco:** migração 0012 (colunas novas em `facts` e a tabela `fact_extractions`).

O porquê de cada escolha está em
[docs/aprendizado/16-fatos.md](../aprendizado/16-fatos.md).

## 17.1 Antes de atualizar: cadastros

A extração liga o **responsável** ("o Beto vai mandar") e **quem disse** (remetente do
e-mail, quem falou na reunião) às Pessoas cadastradas da Empresa do item. Por isso:

1. em **Cadastros → Pessoas**, preencha os **outros nomes** de quem é chamado pelo
   apelido ou só pelo primeiro nome ("Beto", "Má");
2. confira a **equipe** de cada projeto: o Claude recebe a lista e usa o nome dela.

Pessoa não cadastrada não se perde: o nome fica gravado como apareceu no texto e a tela
mostra "(não cadastrada)".

## 17.2 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

A migração 0012 roda sozinha. Opcional no `.env` (os padrões já servem):

```bash
# Modelo da extração (padrão: Haiku, o único que cabe no teto de US$ 10).
FACTS_MODEL=claude-haiku-5-5
# Máximo de itens lidos pelo Claude por rodada de 5 minutos.
FACTS_LLM_PER_RUN=20
# Máximo de caracteres lidos de um item (60 mil = uma reunião de 1h30).
FACTS_MAX_CHARS=60000
```

## 17.3 Extrair o que já foi coletado

O coletor faz isso sozinho, 20 itens por rodada. Para não esperar:

```bash
docker compose exec app python -m app.cli extrair-fatos
```

Ele mostra no fim algo como
`Fatos: 180 item(ns) lido(s), 402 fato(s), 377 com trecho conferido, 0 esperando.`

Custo esperado com o Haiku: cerca de US$ 0,0004 por e-mail e US$ 0,003 por reunião de
1 hora. Com 50 e-mails e 4 reuniões por dia, perto de US$ 1 por mês.

## 17.4 Roteiro de testes

1. **Página:** abra **Fatos**. "Fatos" mostra o total e o % com trecho conferido.
2. **E-mail:** abra um e-mail de projeto com um pedido claro ("pode me mandar o
   cronograma até sexta?"). A seção **Fatos** mostra uma tarefa com o responsável, o
   prazo em data (a sexta daquela semana) e "trecho conferido".
3. **Conversa citada:** abra a resposta a esse e-mail. Os fatos dela não repetem a
   tarefa do e-mail anterior (o texto citado não é lido de novo).
4. **Reunião:** abra uma transcrição com uma decisão. **Quem disse** mostra a pessoa que
   falou, com a marca "liderança" se ela for liderança na organização.
5. **Suspeito:** marque **só suspeitos**. Abra um deles e procure o trecho no texto
   original: normalmente o Claude trocou uma palavra. Se for um fato inventado, anote
   (é o número que diz se o Haiku basta).
6. **Projeto corrigido:** na página de um item com fatos, escolha outro projeto e
   **Gravar projeto**. Em até 5 minutos os fatos aparecem com o projeto novo, sem nova
   chamada ao Claude.
7. **Extrair de novo:** cadastre uma pessoa que aparecia como "(não cadastrada)" e
   clique em **Extrair de novo** no item. Em até 5 minutos o responsável aparece ligado.
8. **Duas Empresas:** filtre pelo combo de Empresa. Os projetos do combo e os fatos
   são só os daquela Empresa.
9. **Custo:** "Custo no mês" da página Fatos sobe centavos.

## 17.5 Se algo der errado

- **Nada é lido ("Esperando" alto):** os itens não têm projeto, estão na fila A revisar,
  têm anexo esperando a leitura pelo Claude, falta a chave da Anthropic ou o teto do mês
  foi atingido. Veja `docker compose logs --tail 50 coletor` (linha "Fatos: ...").
- **Muitos suspeitos:** abra alguns. Se o Claude só reescreve o trecho, o fato está certo
  e a conferência é que está rígida; se ele inventa, troque para
  `FACTS_MODEL=claude-sonnet-5-5` e rode `extrair-fatos --refazer` só depois de olhar o
  custo (o Sonnet custa cerca de 20 vezes mais).
- **"recusa" ou "resposta ilegível" na página do item:** o Claude recusou ou a resposta
  veio cortada. Clique em **Extrair de novo** no item.
- **Fatos repetidos entre a transcrição importada e a da extensão:** não acontece quando
  as duas estão ligadas à mesma reunião (a segunda é pulada). Se acontecer, as duas não
  foram ligadas ao mesmo evento da agenda.
- **Quero refazer tudo:** `docker compose exec app python -m app.cli extrair-fatos --refazer`
  (custa de novo: cada item volta ao Claude).
