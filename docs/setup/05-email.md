# 5. Coleta de e-mail (bloco 2)

Resultado: a cada 5 minutos o servidor lê os e-mails novos do seu Gmail e grava cada um no
banco (camada bronze), com os anexos (PDF, Word, Excel, PowerPoint) já convertidos em
texto. A coleta **começa no momento da instalação**: o histórico anterior não é importado.
Links do Google Drive citados nos e-mails ficam só registrados; o conteúdo do Drive entra
pelas pastas que você cadastrar no console (guia [06-console.md](06-console.md)).

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
   coleta a cada 5 minutos (e também varre as pastas do Drive e roda os testes de saúde,
   guia 06). A primeira rodada marca o "marco zero": dali para trás, nada
   entra.
4. Nasce um volume novo do Docker, `arquivos`, onde ficam os e-mails originais (`.eml`) e
   os anexos. Ele sobrevive a atualizações e entra no snapshot automático do Lightsail.

Confira se os quatro serviços estão de pé (`migrate` aparece como terminado, é normal):

```bash
docker compose ps -a
```

## 5.2 Pastas do Drive

O Drive não é configurado no `.env`: as pastas monitoradas são cadastradas pelo console, em
**Configurações → Pastas do Drive** (guia [06-console.md](06-console.md)). Links do Drive
que aparecem nos e-mails ficam registrados (qual e-mail cita qual arquivo), sem baixar nada.

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

**Se aparecer "Nenhuma conta Google conectada":** conecte a conta pelo console
(**Configurações → Contas conectadas**); a próxima rodada, em até 5 minutos, já funciona.

## 5.4 Conferir pelo console

Abra o console. Na página inicial (ícone da casa), a seção **Coleta de e-mail** mostra quantos e-mails e arquivos estão no
bronze e as últimas rodadas. Cada rodada tem uma situação:

| Situação | Significado |
| --- | --- |
| `em_andamento` | Rodando agora. Se ficar assim por horas, o processo morreu no meio: veja os logs |
| `ok` | Terminou sem erro |
| `com_erros` | Alguma mensagem falhou (rede, API). Passe o mouse sobre o número de erros para ver o motivo. A próxima rodada tenta de novo sozinha |

"Sem texto" **não é erro de coleta**: o arquivo foi guardado (ou o link registrado), mas o
texto não pôde ser lido (PDF escaneado, imagem, Word antigo). O motivo fica registrado arquivo por arquivo.

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
| `GMAIL_QUERY` | `-category:promotions -category:social -in:drafts` | Filtro na mesma sintaxe da busca do Gmail. Para trazer tudo, deixe só `-in:drafts` |
| `GMAIL_POLL_MINUTES` | `5` | Intervalo entre coletas |
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
- **Links do Drive nos e-mails:** só registrados. Para o conteúdo entrar no banco, o
  arquivo precisa estar numa pasta monitorada (guia 06).
- **Horários no console estão em UTC** (3 horas à frente de Brasília), como no restante
  do console hoje.
