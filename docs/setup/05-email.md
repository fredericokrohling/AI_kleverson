# 5. Coleta de e-mail (bloco 2)

Resultado: a cada 5 minutos o servidor lê os e-mails novos do seu Gmail e grava cada um no
banco (camada bronze), com os anexos (PDF, Word, Excel, PowerPoint) já convertidos em
texto. A coleta **começa no momento da instalação**: o histórico anterior não é importado.
Links do Google Drive citados nos e-mails só são baixados se o arquivo estiver dentro da
pasta que você autorizar (passo 5.2).

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
   coleta a cada 5 minutos. A primeira rodada marca o "marco zero": dali para trás, nada
   entra.
4. Nasce um volume novo do Docker, `arquivos`, onde ficam os e-mails originais (`.eml`) e
   os anexos. Ele sobrevive a atualizações e entra no snapshot automático do Lightsail.

Confira se os quatro serviços estão de pé (`migrate` aparece como terminado, é normal):

```bash
docker compose ps -a
```

## 5.2 Autorizar uma pasta do Drive (quando quiser)

**Por quê:** muitos e-mails trazem o link de uma planilha ou documento em vez do anexo. O
coletor pode baixar esses arquivos, mas só os que estiverem dentro de uma pasta escolhida
por você (e das subpastas dela). Assim o disco não enche com arquivos que não interessam.
Sem pasta configurada, nada é baixado do Drive.

**Atenção:** o limite é do código, não da permissão. A `drive.readonly` dá leitura do
Drive inteiro; o Google não oferece uma permissão de leitura restrita a uma pasta.

1. **No navegador:** abra a pasta no Google Drive. O endereço fica assim:
   `https://drive.google.com/drive/folders/1AbC...xyz`. O id é o trecho depois de
   `/folders/` (sem `?usp=...`, se houver).
2. **No servidor:** abra o `.env` (`nano ~/AI_kleverson/.env`) e acrescente no fim:

   ```
   DRIVE_FOLDER_ID=1AbC...xyz
   ```

   Salve com `Ctrl+O`, `Enter`, e saia com `Ctrl+X`.
3. **No servidor:** `cd ~/AI_kleverson && docker compose up -d` (o coletor reinicia e lê o
   valor novo).

Links para arquivos fora dessa pasta ficam registrados no banco com o motivo "Fora da pasta
autorizada do Drive", sem baixar nada.

## 5.3 Acompanhar a coleta

```bash
docker compose logs -f coletor
```

Logo depois de subir, a primeira rodada aparece sem nenhum e-mail (é o marco zero):

```
... INFO Coleta 1: buscando '-category:promotions -category:social -in:drafts after:1793...'
... INFO Coleta 1 (ok): 0 na janela, 0 novas, 0 arquivos, 0 sem texto, 0 mensagens com erro.
```

Mande um e-mail de teste para você mesma, de preferência com um PDF anexado. Em até 5
minutos deve aparecer uma rodada com `1 novas`. `Ctrl+C` sai do acompanhamento (o coletor
continua rodando).

**Se aparecer "Nenhuma conta Google conectada":** conecte a conta pelo console (botão
"Conectar conta Google"); a próxima rodada, em até 5 minutos, já funciona.

## 5.4 Conferir pelo console

Abra o console. A seção **Coleta de e-mail** mostra quantos e-mails e arquivos estão no
bronze e as últimas rodadas. Cada rodada tem uma situação:

| Situação | Significado |
| --- | --- |
| `em_andamento` | Rodando agora. Se ficar assim por horas, o processo morreu no meio: veja os logs |
| `ok` | Terminou sem erro |
| `com_erros` | Alguma mensagem falhou (rede, API). Passe o mouse sobre o número de erros para ver o motivo. A próxima rodada tenta de novo sozinha |

"Sem texto" **não é erro de coleta**: o arquivo foi guardado (ou o link registrado), mas o
texto não pôde ser lido (PDF escaneado, imagem, Word antigo, arquivo fora da pasta do
Drive). O motivo fica registrado arquivo por arquivo.

## 5.5 Critério de pronto do bloco 2

Como a coleta começa do zero, o critério passa a ser: **um mês de coleta contínua sem
rodada com erro persistente**. Para conferir a qualquer momento:

1. Quantos e-mails entraram e o período coberto:

   ```bash
   docker compose exec db psql -U assistente -d assistente -c \
     "SELECT count(*) AS emails, min(occurred_at), max(occurred_at) FROM bronze_items WHERE source = 'gmail';"
   ```

2. Rodadas com erro (o ideal é nenhuma, ou só erros que sumiram na rodada seguinte):

   ```bash
   docker compose exec db psql -U assistente -d assistente -c \
     "SELECT started_at, message_errors, error_detail FROM ingestion_runs WHERE status <> 'ok' ORDER BY started_at DESC LIMIT 10;"
   ```

3. Arquivos que ficaram sem texto, e por quê:

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

## 5.6 Ajustes opcionais (no `.env`)

Depois de mudar o `.env`, rode `docker compose up -d` para o coletor ler os valores novos.

| Variável | Padrão | Para quê |
| --- | --- | --- |
| `DRIVE_FOLDER_ID` | vazio | Pasta do Drive de onde os links podem ser baixados (passo 5.2). Vazio = nada do Drive |
| `GMAIL_QUERY` | `-category:promotions -category:social -in:drafts` | Filtro na mesma sintaxe da busca do Gmail. Para trazer tudo, deixe só `-in:drafts` |
| `GMAIL_POLL_MINUTES` | `5` | Intervalo entre coletas |
| `DRIVE_MAX_FILE_MB` | `25` | Maior arquivo baixado do Drive |
| `GMAIL_INITIAL_DAYS` | `0` | Dias para trás na primeira coleta. Só vale antes da primeira rodada; depois, use o comando `--desde` (5.7) |

## 5.7 Comandos úteis

**Importar um período anterior** (só se um dia quiser o histórico; ex.: desde 1º de
outubro). Pare o coletor antes, porque só uma coleta roda por vez:

```bash
docker compose stop coletor
docker compose run --rm coletor python -m app.cli coletar-gmail --desde 2026-10-01
docker compose start coletor
```

**Extrair de novo o texto dos arquivos que falharam** (útil depois de uma atualização
que melhore a leitura de algum formato; usa os originais guardados, sem baixar nada):

```bash
docker compose run --rm coletor python -m app.cli reextrair-anexos
```

**Pausar a coleta**:

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
