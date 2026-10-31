# 6. Ingestão de e-mail: como funciona e por quê

## O requisito

Levar para o bronze **todo e-mail relevante** com **anexos legíveis** (PDF, Word, Excel,
PowerPoint), sem duplicar, sem perder nada quando algo falha e sem nenhuma permissão de
escrita no Gmail. O bronze é a matéria-prima do bloco 3 (classificação e extração de
fatos): o que não entrar aqui, a IA nunca vai ver.

## O caminho de um e-mail

```
Gmail ──(lista de ids na janela)──► coletor ──► já existe no banco? ── sim ──► ignora
                                                  │ não
                                                  ▼
                               baixa o e-mail bruto (.eml) ──► guarda no disco
                                                  │
                                                  ▼
                       desmonta: cabeçalhos, corpo, anexos, links do Drive
                                                  │
                       ┌──────────────────────────┼──────────────────────────┐
                       ▼                          ▼                          ▼
              bronze_items (1 linha)   anexos: guarda + extrai texto   Drive: baixa/exporta + extrai
                                                  │
                                                  ▼
                                attachments (1 linha por arquivo)
```

Tudo de uma mensagem é gravado numa **transação só**: ou o e-mail entra com todos os
arquivos, ou nada entra e ele é tentado de novo na próxima rodada.

## Por que cada peça

### Por que consultar de tempos em tempos, e não receber aviso (push)
O Gmail pode avisar quando chega e-mail, mas o aviso precisa de um endereço público na
internet, o que quebraria a regra de nenhuma porta aberta. Alguns minutos de atraso são
aceitáveis para e-mail. (Decisão já registrada no bloco 0.)

### Por que janela por data, e não o "histórico" do Gmail
O Gmail oferece duas formas de saber o que é novo:

| Caminho | Como funciona | Problema |
| --- | --- | --- |
| **Histórico** (`history.list`) | Guarda um marcador; pergunta "o que mudou desde o marcador?" | O marcador expira em cerca de uma semana. A primeira carga precisa de outro caminho. Duas lógicas para manter |
| **Janela por data + id** (escolhido) | Pergunta "quais e-mails desde tal hora?"; pula os ids que já estão no banco | Pede de novo a lista da folga a cada rodada (barato: 500 ids por chamada) |

A janela serve igual para a primeira rodada e para as seguintes (desde a última rodada
boa, menos 60 minutos de folga). A folga cobre e-mails que o Gmail indexa com atraso.
Repetir uma janela é inofensivo, porque o banco tem a regra "fonte + id não repete".

### Por que começar "a partir de agora"
Você pediu para não carregar o histórico. Por isso `GMAIL_INITIAL_DAYS` vale 0: a primeira
rodada marca o **marco zero**, e a folga de 60 minutos nunca volta antes dele (senão a
segunda rodada traria e-mails da hora anterior à instalação). Se um dia quiser o
histórico, o comando `coletar-gmail --desde AAAA-MM-DD` importa um período específico.

### Por que pedir o e-mail bruto (format="raw")
A API do Gmail entrega a mensagem de dois jeitos:

- **JSON já desmontado** (`format="full"`): cada anexo exige **outra chamada** à API.
- **Bruto** (`format="raw"`, escolhido): o arquivo `.eml` completo, numa chamada só,
  com todos os anexos dentro.

O bruto também é o **original de verdade**. Guardado no disco, ele permite reprocessar
qualquer coisa no futuro (um cabeçalho que hoje não usamos) mesmo que o e-mail seja
apagado do Gmail. Quem desmonta o `.eml` é o módulo `email` do próprio Python, que existe
há mais de 20 anos.

### Por que guardar os arquivos no disco e não no banco nem num bucket
A documentação previa um bucket privado do Lightsail (armazenamento de objetos, como o
S3). Comparando:

| Opção | Prós | Contras |
| --- | --- | --- |
| Dentro do Postgres | Uma peça só | Banco e backups ficam pesados sem ganho |
| Bucket do Lightsail | Durável, separado do servidor | Mais uma credencial (chave de acesso) no `.env`, mais uma biblioteca, custo à parte |
| **Volume do Docker no disco do servidor** (escolhido) | Zero credencial nova; entra no snapshot diário do Lightsail | Divide o disco com o resto |

Para uma pessoa, o volume de anexos cabe folgado no disco, e o snapshot automático já
protege. O código acessa os arquivos por uma única classe (`FileStore`); trocar por um
bucket depois é mudar essa classe, sem mexer no coletor.

### Por que o nome do arquivo é a impressão digital (SHA-256)
O mesmo PDF encaminhado em dez e-mails vira **um** arquivo no disco e **uma** extração
de texto (as outras nove linhas reaproveitam o resultado). E gravar de novo o mesmo
arquivo não faz nada, o que deixa a coleta segura para repetir.

### Por que uma trava no Postgres
O coletor contínuo e um comando manual (`--desde`) poderiam rodar juntos e tentar gravar a
mesma mensagem. Em vez de um arquivo de trava (que fica "preso" se o processo morrer),
usamos a **trava consultiva** do Postgres (`pg_try_advisory_lock`): ela pertence à
conexão e some sozinha se o processo cair. Quem chega depois desiste sem fazer nada.

### Por que um serviço separado no Docker
Opções avaliadas:

| Opção | Problema |
| --- | --- |
| Agendador dentro do console (FastAPI) | Uma coleta longa ou um erro nela afeta o console; reiniciar o console interrompe a coleta |
| `cron` do servidor | Configuração fora do repositório, que você teria de lembrar de refazer numa máquina nova |
| **Serviço `coletor` no Compose** (escolhido) | Nenhum: mesma imagem, sobe e reinicia com o resto, logs separados |

### Como os erros são tratados
- **Falha ao ler um anexo** (PDF escaneado, arquivo corrompido): o e-mail entra; o motivo
  fica em `attachments.extraction_error`. O original fica guardado para tentar de novo
  (`reextrair-anexos`).
- **Falha numa mensagem** (rede, API): a mensagem é desfeita, as outras seguem. A rodada
  termina `com_erros` e **não avança a janela**: a próxima rodada recomeça do fim da
  última rodada `ok` e tenta só o que falta.
- **Rascunhos** são pulados: não são fatos, são textos que ainda podem mudar.

### Por que filtrar promoções e redes sociais
Esses e-mails não têm conteúdo de projeto e, no bloco 3, cada e-mail passa pela
classificação, que pode custar chamadas ao Claude (teto de US$ 10 por mês). O filtro é a
própria sintaxe de busca do Gmail, então você muda no `.env` sem mexer no código.

## Os extratores de texto

| Formato | Biblioteca | Por que esta |
| --- | --- | --- |
| PDF | **pypdf** | Python puro, licença livre (BSD). PyMuPDF lê melhor tabelas, mas a licença (AGPL) obriga a abrir o código de quem a usa num serviço; pdfplumber é mais lento e só ganha em tabelas |
| Word (.docx) | **python-docx** | A biblioteca padrão; lê parágrafos e tabelas na ordem do documento |
| Excel (.xlsx) | **openpyxl** | A biblioteca padrão; lê todas as abas e os **valores calculados** das fórmulas |
| PowerPoint (.pptx) | **python-pptx** | A biblioteca padrão; lê caixas de texto, tabelas e anotações |
| HTML | `html.parser` (do Python) | Basta tirar as tags; uma biblioteca a mais não compensa |

Cada extrator marca a origem do trecho (`### Página 3`, `### Planilha: Cronograma`,
`### Slide 2`). No bloco 3, isso deixa a citação literal de cada fato mais fácil de
conferir.

**Planos de projeto (PLAN):** chegam como Excel ou como Google Sheets. As datas saem no
formato `2026-11-20`, que a IA interpreta sem ambiguidade (ao contrário de `11/12/2026`).

**Formatos antigos (.doc, .xls, .ppt):** convertê-los exige o LibreOffice dentro da imagem
Docker, o que acrescenta centenas de MB e um programa grande para manter atualizado. A
decisão foi **medir antes**: o original fica guardado, o motivo fica registrado, e a
contagem dos primeiros dias de coleta (guia 05, passo 5.5) diz se vale a pena.

## Os links do Google Drive

Em empresas que usam Google Workspace, o cronograma raramente vem anexado: vem um link.
O coletor procura links do Drive no texto e no HTML do e-mail (o Gmail coloca o link no
`href`, invisível no texto) e **registra** cada um: uma linha em `attachments` com origem
`link_drive` e o id do arquivo.

### Quem lê o conteúdo do link
O coletor de e-mail não baixa o arquivo: quem lê é o monitor do Drive, na volta seguinte do
agendador (em até 1 minuto). Ele lê cada arquivo citado a que a sua conta tem acesso, mesmo
fora das pastas monitoradas: um cronograma de outra pessoa, um documento num drive
corporativo, um arquivo compartilhado só para leitura. Cada mudança no arquivo vira uma
versão nova no bronze, como nas pastas.

Por que separar: a coleta de e-mail continua rápida. Um arquivo grande ou um Drive lento
não atrasa a gravação dos e-mails, e um erro no Drive não marca a rodada do Gmail como
"com erros".

Se o arquivo também estiver numa pasta monitorada, a pasta manda: é ela que dá as tags
herdadas. Arquivos acima de `DRIVE_MAX_FILE_MB`, sem acesso ou com download bloqueado pelo
dono aparecem em **Arquivos sem texto**, com o remetente e a data do e-mail.

> **Decisão: ler os arquivos citados por link (31/10/2026, revisa a decisão anterior)**
> Avaliamos **A — só registrar o link; o conteúdo entra pelas pastas monitoradas** e
> **B — ler todo arquivo citado por link a que a conta tem acesso, até 25 MB**, e
> optamos por **B**:
> - Arquivos de outras pessoas e de drives corporativos chegam por link; você não consegue
>   colocá-los na sua pasta, e sem eles a IA perde contexto.
> - O disco continua sob controle: o limite de tamanho vale também para os links, e o que
>   passa dele aparece na lista de arquivos sem texto.

### Links do Lucid e do Canva
Um diagrama do Lucid ou do Canva fica nos servidores desses serviços, fora do Google (no
Drive, um arquivo do Lucid é só um atalho). Ler direto exigiria conectar sua conta neles.
Por isso o coletor só registra o link (origem `link_externo`) com o código
`LINK_EXTERNO_NAO_LIDO`, e ele aparece na página inicial com o remetente e a data do
e-mail. Você exporta o diagrama como PDF ou PNG para uma pasta monitorada e dispensa o
aviso.

**Importante:** qualquer limite de leitura do Drive é do **código**, não da **permissão**. A
`drive.readonly` lê o Drive inteiro. O Google não tem uma permissão "ler só esta pasta" (a
`drive.file`, a mais restrita, só enxerga arquivos criados pelo próprio app ou escolhidos
num seletor na tela, o que não serve para um coletor automático). É diferente do envio de
e-mail, que é impedido pela própria permissão.

## Onde isso está no código

Comece por `app/ingestion/__init__.py`, que indica a ordem de leitura:

- `app/ingestion/storage.py`: guarda dos originais.
- `app/ingestion/extractors.py`: arquivo vira texto.
- `app/ingestion/email_parser.py`: `.eml` vira cabeçalhos, corpo, anexos e links.
- `app/ingestion/drive.py`: download e exportação de arquivos do Drive (usado pelo monitor de pastas, guia 07).
- `app/ingestion/gmail.py`: o coletor.
- `migrations/versions/0003_coleta_de_e_mail.py`: as mudanças no banco.
- `tests/test_ingestion.py` e `tests/fakes.py`: os testes, com Gmail e Drive falsos.
