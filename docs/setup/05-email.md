# 5. Coleta de e-mail (bloco 2)

Resultado: a cada 5 minutos o servidor lê os e-mails novos do seu Gmail e grava cada um no
banco (camada bronze), com os anexos (PDF, Word, Excel, PowerPoint) já convertidos em
texto e com as planilhas e documentos do Google Drive citados por link. Na primeira vez,
importa os últimos 30 dias sozinho.

Pré-requisitos: instalação 1 a 4 concluída e a conta Google conectada pelo console
(guia 02). **Nada muda no Google:** a coleta usa as permissões que já foram concedidas
(`gmail.readonly` e `drive.readonly`, as duas só de leitura) e a Drive API já está ligada.

O porquê de cada escolha está em
[docs/aprendizado/06-ingestao-email.md](../aprendizado/06-ingestao-email.md).

## 5.1 Atualizar o servidor

**Onde:** no **servidor** (`ssh ubuntu@assistente`).

```bash
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

**O que acontece:**

1. A imagem é reconstruída com as bibliotecas novas (leitura de PDF, Word, Excel e
   PowerPoint).
2. O serviço `migrate` aplica a migração `0003`, que cria a tabela das rodadas de coleta
   (`ingestion_runs`) e acrescenta colunas à tabela de anexos. Nada existente é apagado.
3. Sobe um serviço novo, `coletor`, separado do console. Ele roda sem parar e repete a
   coleta a cada 5 minutos.
4. Nasce um volume novo do Docker, `arquivos`, onde ficam os e-mails originais (`.eml`) e
   os anexos. Ele sobrevive a atualizações e entra no snapshot automático do Lightsail.

Confira se os quatro serviços estão de pé (`migrate` aparece como terminado, é normal):

```bash
docker compose ps -a
```

**Não precisa mexer no `.env`.** Os valores padrão já servem. As opções existem no
`.env.example`, se um dia quiser ajustar (ver 5.5).

## 5.2 Acompanhar a primeira coleta

A primeira rodada importa 30 dias e pode levar de alguns minutos a meia hora, conforme o
volume de e-mails (cada mensagem é uma chamada ao Gmail).

```bash
docker compose logs -f coletor
```

Você deve ver, nesta ordem:

```
... INFO Coleta 1: buscando '-category:promotions -category:social -in:drafts after:1759...'
... INFO Coleta 1: 100 novas até agora
... INFO Coleta 1 (ok): 812 na janela, 790 novas, 143 arquivos, 9 sem texto, 0 mensagens com erro.
```

`Ctrl+C` sai do acompanhamento (o coletor continua rodando).

**Se aparecer "Nenhuma conta Google conectada":** conecte a conta pelo console (botão
"Conectar conta Google"); a próxima rodada, em até 5 minutos, já funciona.

## 5.3 Conferir pelo console

Abra o console. A seção **Coleta de e-mail** mostra quantos e-mails e arquivos estão no
bronze e as últimas rodadas. Cada rodada tem uma situação:

| Situação | Significado |
| --- | --- |
| `em_andamento` | Rodando agora. Se ficar assim por horas, o processo morreu no meio: veja os logs |
| `ok` | Terminou sem erro |
| `com_erros` | Alguma mensagem falhou (rede, API). Passe o mouse sobre o número de erros para ver o motivo. A próxima rodada tenta de novo sozinha |

"Sem texto" **não é erro de coleta**: o arquivo foi guardado, mas o texto não pôde ser
lido (PDF escaneado, imagem, Word antigo). O motivo fica registrado arquivo por arquivo.

## 5.4 Critério de pronto do bloco 2: um mês importado sem erro

1. A primeira rodada (a de 30 dias) terminou com situação `ok`.
2. Compare a contagem com o Gmail. No Gmail, pesquise
   `-category:promotions -category:social -in:drafts newer_than:30d`; o número de
   **conversas** que o Gmail mostra é menor que o de mensagens, então use o terminal:

   ```bash
   docker compose exec db psql -U assistente -d assistente -c \
     "SELECT count(*) AS emails, min(occurred_at), max(occurred_at) FROM bronze_items WHERE source = 'gmail';"
   ```

3. Veja os arquivos que ficaram sem texto e por quê:

   ```bash
   docker compose exec db psql -U assistente -d assistente -c \
     "SELECT extraction_error, count(*) FROM attachments WHERE extraction_error IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;"
   ```

   Anote quantos são "Formato antigo do Office" e "PDF sem texto". Esses números decidem se
   vale incluir a conversão de formatos antigos e a leitura de imagem (OCR) depois.

4. Espaço em disco usado pelos originais:

   ```bash
   docker compose exec coletor du -sh /dados
   ```

## 5.5 Ajustes opcionais (no `.env`)

Depois de mudar o `.env`, rode `docker compose up -d` para o coletor ler os valores novos.

| Variável | Padrão | Para quê |
| --- | --- | --- |
| `GMAIL_QUERY` | `-category:promotions -category:social -in:drafts` | Filtro na mesma sintaxe da busca do Gmail. Para trazer tudo, deixe só `-in:drafts` |
| `GMAIL_INITIAL_DAYS` | `30` | Dias importados na primeira coleta |
| `GMAIL_POLL_MINUTES` | `5` | Intervalo entre coletas |
| `GMAIL_FOLLOW_DRIVE_LINKS` | `true` | Baixar Docs, Sheets, Slides e PDFs do Drive citados nos e-mails |
| `DRIVE_MAX_FILE_MB` | `25` | Maior arquivo baixado do Drive |

## 5.6 Comandos úteis

**Importar um período mais antigo** (ex.: desde 1º de janeiro). Pare o coletor antes,
porque só uma coleta roda por vez; a outra desiste com a mensagem "Outra coleta já está
em andamento":

```bash
docker compose stop coletor
docker compose run --rm coletor python -m app.cli coletar-gmail --desde 2026-01-01
docker compose start coletor
```

**Extrair de novo o texto dos arquivos que falharam** (útil depois de uma atualização
que melhore a leitura de algum formato; usa os originais guardados, sem baixar nada):

```bash
docker compose run --rm coletor python -m app.cli reextrair-anexos
```

**Pausar a coleta** (por exemplo, para economizar enquanto testa outra coisa):

```bash
docker compose stop coletor    # pausa
docker compose start coletor   # retoma de onde parou
```

## Limitações conhecidas

- **Word, Excel e PowerPoint antigos** (`.doc`, `.xls`, `.ppt`, anteriores a 2007): o
  original fica guardado, mas o texto ainda não é extraído.
- **PDF escaneado e imagens:** sem leitura de texto em imagem (OCR).
- **Arquivo do Drive sem compartilhamento com você:** o link fica registrado com o
  motivo "Sem acesso ao arquivo no Drive".
- **Docs, Sheets e Slides acima de 10 MB:** o Google recusa exportar; o link fica
  registrado com o motivo.
- **Arquivo do Drive é uma foto do momento:** o Drive muda; o bronze guarda a versão do
  dia em que o e-mail foi coletado. Um e-mail novo com o mesmo link baixa a versão nova.
- **Horários no console estão em UTC** (3 horas à frente de Brasília), como no restante
  do console hoje.
