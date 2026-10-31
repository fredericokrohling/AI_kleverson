# 6. Console: pastas do Drive, tags, configurações e saúde

Resultado: o console ganha um menu lateral com três atalhos:

| Ícone | Página | O que tem |
| --- | --- | --- |
| Casa | Início | Coleta de e-mail, Drive (totais), arquivos sem texto e Processamento |
| Engrenagem | Configurações | Contas conectadas, Claude (modelo, esforço, gasto, teste), idioma da transcrição, pastas do Drive monitoradas e tags |
| Coração | Saúde | Situação de cada serviço, versão instalada x versão nova, botões de terminal. Fica **vermelho** quando algum serviço falha |

Pré-requisitos: guias 1 a 5 concluídos. **Nada muda no Google:** as permissões continuam
só de leitura.

O porquê de cada escolha está em
[docs/aprendizado/07-console-drive-saude.md](../aprendizado/07-console-drive-saude.md).

## 6.1 Atualizar o servidor

**Onde:** no **servidor** (`ssh ubuntu@assistente`).

```bash
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

**O que acontece:**

1. O serviço `migrate` aplica as migrações `0004` e `0005`: tabelas de configurações,
   tags, pastas e itens do Drive, testes de saúde, duas views que calculam a herança das
   tags, e os campos para arquivos citados por link e avisos dispensados. Nada existente é
   apagado.
2. O serviço `coletor` passa a rodar o **agendador**: Gmail a cada 5 min, pastas do Drive a
   cada 30 min e testes de saúde a cada 10 min.
3. O console passa a ler a pasta `.git` do projeto (só leitura) para mostrar qual versão
   está instalada.

**Se você tinha colocado `DRIVE_FOLDER_ID` no `.env`:** pode apagar a linha. Ela não é mais
usada; a pasta agora é cadastrada pelo console (6.2).

Confira os serviços:

```bash
docker compose ps -a
```

## 6.2 Cadastrar uma pasta do Drive

**Onde:** no **navegador**, no console → **Configurações → Pastas do Drive monitoradas**.

1. No Google Drive, crie a pasta (ou use uma existente) e abra-a. Copie o endereço da barra
   do navegador (algo como `https://drive.google.com/drive/folders/1AbC...`).
2. Cole no campo e clique **Monitorar pasta**.
3. Em até 1 minuto começa a primeira varredura. A coluna "Última varredura" mostra a hora
   quando terminar.

Tudo dentro da pasta, em qualquer nível de subpasta, passa a ser lido a cada 30 minutos.
Cada arquivo alterado vira uma **versão nova** no banco (a anterior continua guardada), mas
só quando o texto muda de verdade. Arquivos que saem da pasta ficam marcados como "saiu da
pasta"; o histórico não é apagado.

**Parar** desliga o monitoramento sem apagar nada; cadastrar a mesma pasta de novo retoma.
**Varrer agora** pede uma varredura imediata (em até 1 minuto).

## 6.3 Tags

**Criar, renomear e apagar:** em **Configurações → Tags**.

**Colocar numa pasta ou arquivo:** clique no nome da pasta (em Pastas do Drive). A página
mostra o conteúdo da pasta, com as tags de cada item. Digite o nome da tag no campo e
clique **+ tag**; se a tag não existir, ela é criada.

- Tag de **borda cheia**: colocada naquele item. O **×** retira.
- Tag **tracejada**: herdada de uma pasta acima. Para retirá-la, vá à pasta onde ela foi
  colocada.
- Arquivos que chegarem depois na pasta **já nascem com as tags** dela.
- Renomear ou apagar uma tag vale na hora para todos os itens.

## 6.4 Claude e transcrição

**Onde:** **Configurações → Claude** e **Configurações → Transcrição de reuniões**.

- **Modelo e esforço:** valem a partir da próxima chamada. O `.env` (`LLM_MODEL`,
  `LLM_EFFORT`) passa a ser só o padrão inicial. **Testar Claude** faz uma chamada mínima
  e mostra o custo.
- **Idioma:** vale a partir da próxima transcrição iniciada na extensão. Para reuniões em
  inglês, escolha **Inglês**. Se a reunião mistura português e inglês, escolha
  **Automático**.

## 6.5 Página Saúde

**Onde:** no console, ícone de coração.

- **Coração vermelho** no menu: algum serviço falhou no último teste, ou os testes
  automáticos pararam há mais de 30 minutos (o `coletor` caiu). Volta à cor normal sozinho
  quando o teste seguinte passar. "Não deu para saber" (por exemplo, página de status de um
  fornecedor fora do ar) não deixa o coração vermelho.
- A tabela mostra o último teste de cada serviço e o histórico curto (bolinhas).
  **Testar agora** roda todos na hora.
- Se aparecer "Os testes automáticos não rodam há X min", o serviço `coletor` está parado.
  Use **Ver serviços** (6.7) ou, no servidor, `docker compose ps`.
- **Versão instalada:** mostra o PR em uso e avisa se a `main` tem novidade. **Verificar
  atualização** consulta o GitHub na hora.

## 6.6 Arquivos sem texto (página inicial)

**Onde:** no console, página inicial, seção **Arquivos sem texto**.

A lista mostra tudo que chegou mas não virou texto: anexos de e-mail, arquivos do Drive
(das pastas ou citados por link num e-mail), arquivos acima do limite de tamanho e links
do Lucid e do Canva. Em cada linha aparecem o nome, a origem (assunto do e-mail ou caminho
no Drive), o **remetente** e a **data e hora** do e-mail, o **código do erro** e a
explicação. Enquanto um arquivo não vira texto, a IA não sabe o que há nele.

- **Tentar de novo** (em cada linha): lê o arquivo outra vez. Se funcionar, aparece
  "Funcionou" e a linha some. Se não, aparece "Ainda com erro", com o código. Não aparece
  nos links do Lucid e do Canva, em que tentar de novo não adianta.
- **Tentar todos de novo**: faz o mesmo para a lista inteira e mostra quantos foram
  resolvidos.
- **Dispensar**: tira a linha da lista sem apagar nada. Use depois de resolver à mão (por
  exemplo, depois de exportar o diagrama do Lucid como PDF ou PNG para a pasta monitorada).
  "Tentar de novo" num item dispensado o devolve à lista se ainda falhar.
- Cada tentativa e cada aviso dispensado ficam registrados em **Processamento** (6.6.2).

**Arquivos citados por link:** um link do Drive num e-mail é lido em até 1 minuto, mesmo
que o arquivo não esteja numa pasta sua, desde que a sua conta tenha acesso. Depois, ele é
conferido a cada 30 minutos, e cada mudança vira uma versão nova.

Quando vale a pena tentar de novo: depois que o dono liberar o download ou compartilhar o
arquivo com você, ou depois de uma atualização do sistema que passe a ler um formato novo.
Para anexos e arquivos já baixados, a tentativa relê o original guardado, sem ir ao Google.

| Código | O que significa | O que fazer |
| --- | --- | --- |
| `PDF_SEM_TEXTO` | PDF digitalizado aguardando a leitura pelo Claude | Esperar até 5 minutos ou Tentar de novo (guia 7) |
| `IMAGEM_SEM_OCR` | Imagem (PNG, JPG...) aguardando a leitura pelo Claude | Esperar até 5 minutos ou Tentar de novo (guia 7) |
| `OFFICE_ANTIGO` | `.doc`, `.xls` ou `.ppt` antigos | Salvar como `.docx`, `.xlsx` ou `.pptx` e reenviar |
| `TIPO_NAO_SUPORTADO` | Formato que o sistema não lê | Nada; fica registrado |
| `FALHA_LEITURA` | Arquivo corrompido ou protegido por senha | Pedir uma cópia sem senha |
| `DRIVE_DOWNLOAD_BLOQUEADO` | O dono bloqueou "baixar, imprimir e copiar" para quem só lê | Pedir ao dono que libere, ou que dê acesso de comentarista ou editor; depois, Tentar de novo |
| `DRIVE_SEM_ACESSO` | Arquivo apagado ou não compartilhado com a conta conectada | Pedir acesso; depois, Tentar de novo |
| `DRIVE_POLITICA_EMPRESA` | A empresa dona do arquivo bloqueia aplicativos externos | Só o administrador da empresa resolve |
| `DRIVE_EXPORTACAO_GRANDE` | Docs, Sheets ou Slides acima de 10 MB (limite do Google) | Dividir o arquivo |
| `DRIVE_ARQUIVO_GRANDE` | Arquivo acima de `DRIVE_MAX_FILE_MB` | Aumentar o limite no `.env` (6.8) e Tentar de novo |
| `DRIVE_TIPO_NAO_EXPORTAVEL` | Formulário, Desenho e outros tipos do Google sem exportação útil | Nada; fica registrado |
| `DRIVE_LINK_PASTA` | O link no e-mail aponta para uma pasta, que não é varrida | Cadastrar a pasta em Configurações (6.2), se quiser o conteúdo |
| `LINK_EXTERNO_NAO_LIDO` | Diagrama do Lucid ou do Canva: o conteúdo fica fora do Google | Exportar como PDF ou PNG para uma pasta monitorada e Dispensar |
| `DRIVE_HTTP_<n>` | Outro erro do Google (ex.: `DRIVE_HTTP_500`) | Tentar de novo mais tarde |
| `GOOGLE_DESCONECTADO` | A conta Google não está conectada | Conectar em Configurações → Contas conectadas |
| `SEM_ORIGINAL` | Registro antigo sem arquivo guardado | Nada a reprocessar |

Os códigos da leitura pelo Claude (`IMAGEM_PEQUENA`, `CLAUDE_FALHOU` e outros) estão no
[guia 7](07-leitura-imagens.md#76-códigos-novos-na-lista).

Mensagens sem código são de registros gravados antes dos códigos existirem.

### 6.6.1 Ver o conteúdo coletado

**Onde:** página inicial → **Coleta de e-mail** → **Ver o conteúdo coletado**, ou clique no
nome de um arquivo na lista acima.

Mostra o texto exatamente como a IA vai ler: o corpo de cada e-mail, o texto extraído de
cada anexo (clique no nome do anexo para abrir), e cada versão dos arquivos do Drive. A
busca procura uma palavra no assunto e no texto; sem busca, aparecem os 50 mais recentes.

### 6.6.2 Processamento

**Onde:** página inicial, última seção.

É o diário do que o sistema e você fizeram (coletas, varreduras, tags, tentativas).

- **Sem escolher data:** mostra o dia de hoje, já rolado até os **6 eventos mais
  recentes**. Role para cima para ver os anteriores (até 30 na mesma página).
- **Escolhendo uma data no calendário:** mostra aquele dia a partir do **primeiro
  evento**.
- Mais de 30 eventos no dia: as setas **◀ ▶** trocam de página.
- A coluna **Detalhes** mostra os dados de cada ação (por exemplo, `de=A, para=B` ao
  renomear uma tag).
- Os horários do console seguem o fuso `CONSOLE_TIMEZONE` (padrão: Brasília). O banco
  guarda tudo em UTC; a conversão é só na tela.

## 6.7 Botões de terminal (uma vez, no seu computador)

Os botões **Abrir terminal no servidor**, **Ver serviços**, **Ver logs**, **Reiniciar
serviços** e **Aplicar atualização** abrem um terminal **no seu computador**, que conecta ao
servidor por SSH e roda o comando. Reiniciar e Aplicar atualização mostram o que vai
acontecer e pedem confirmação (`s` + Enter) antes de rodar.

Para os botões funcionarem, instale o atalho `kleverson://` uma vez.

**Onde:** no **seu computador** (Linux Mint), num terminal. Você precisa de uma cópia do
repositório no computador; se ainda não tiver:

```bash
git clone https://github.com/fredericokrohling/AI_kleverson.git ~/AI_kleverson
```

Instale:

```bash
cd ~/AI_kleverson && git pull
bash scripts/terminal-local/instalar.sh
```

Teste:

```bash
xdg-open kleverson://status
```

Deve abrir um terminal com a lista dos contêineres do servidor. Na primeira vez que você
clicar num desses botões no Chrome, ele pergunta se pode abrir o aplicativo: marque
**Sempre permitir** para o endereço do console e confirme.

**Se não abrir nada:** confira que `ssh ubuntu@assistente` funciona no seu terminal. Se o seu
servidor tiver outro nome, reinstale informando-o: `bash scripts/terminal-local/instalar.sh
usuario@nome`.

Sem o atalho, a mesma página tem os comandos para copiar e colar.

## 6.8 Ajustes opcionais (no `.env`)

Depois de mudar o `.env`, rode `docker compose up -d`.

| Variável | Padrão | Para quê |
| --- | --- | --- |
| `DRIVE_POLL_MINUTES` | `30` | Intervalo entre varreduras das pastas do Drive |
| `DRIVE_MAX_FILE_MB` | `25` | Maior arquivo comum baixado do Drive (Docs, Sheets e Slides têm limite de 10 MB do Google) |
| `HEALTH_POLL_MINUTES` | `10` | Intervalo entre testes de saúde automáticos |
| `LLM_EFFORT` | `medium` | Esforço padrão do Claude, até alguém mudar pelo console |
| `CONSOLE_TIMEZONE` | `America/Sao_Paulo` | Fuso dos horários do console e do "dia atual" do Processamento |

## 6.9 Comandos úteis

```bash
# Varrer as pastas do Drive agora, vendo o resultado no terminal
docker compose run --rm coletor python -m app.cli coletar-drive

# Ler agora, com o Claude, as imagens e PDFs digitalizados da fila
docker compose run --rm coletor python -m app.cli ler-imagens

# Rodar os testes de saúde agora, no terminal
docker compose run --rm coletor python -m app.cli verificar-saude

# Tags de cada versão de arquivo do Drive no banco
docker compose exec db psql -U assistente -d assistente -c \
  "SELECT b.subject, t.tag, t.herdada FROM bronze_item_tags t JOIN bronze_items b ON b.id = t.bronze_item_id LIMIT 20;"
```

## Limitações conhecidas

- **Atalhos do Drive** (o arquivo "atalho" que aponta para outro lugar) não são seguidos.
- **Formulários e Desenhos do Google** não têm exportação útil; ficam listados como "não lido".
- **Reiniciar a máquina** (não só os serviços) continua sendo pelo terminal:
  `sudo reboot`.
- **Google e AWS** não têm status público consultável por programa; para eles valem os
  testes funcionais (Gmail, Drive) e o fato de o console abrir.
