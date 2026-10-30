# 6. Console: pastas do Drive, tags, configurações e saúde

Resultado: o console ganha um menu lateral com três atalhos:

| Ícone | Página | O que tem |
| --- | --- | --- |
| Casa | Início | Coleta de e-mail, pastas do Drive (totais) e últimas ações |
| Engrenagem | Configurações | Contas conectadas, Claude (modelo, esforço, gasto, teste), idioma da transcrição, pastas do Drive monitoradas e tags |
| Coração | Saúde | Situação de cada serviço, versão instalada x versão nova, botões de terminal |

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

1. O serviço `migrate` aplica a migração `0004`: tabelas de configurações, tags, pastas e
   itens do Drive, testes de saúde, e duas views que calculam a herança das tags. Nada
   existente é apagado.
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

- A tabela mostra o último teste de cada serviço e o histórico curto (bolinhas).
  **Testar agora** roda todos na hora.
- Se aparecer "Os testes automáticos não rodam há X min", o serviço `coletor` está parado.
  Use **Ver serviços** (6.6) ou, no servidor, `docker compose ps`.
- **Versão instalada:** mostra o PR em uso e avisa se a `main` tem novidade. **Verificar
  atualização** consulta o GitHub na hora.

## 6.6 Botões de terminal (uma vez, no seu computador)

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

## 6.7 Ajustes opcionais (no `.env`)

Depois de mudar o `.env`, rode `docker compose up -d`.

| Variável | Padrão | Para quê |
| --- | --- | --- |
| `DRIVE_POLL_MINUTES` | `30` | Intervalo entre varreduras das pastas do Drive |
| `DRIVE_MAX_FILE_MB` | `25` | Maior arquivo comum baixado do Drive (Docs, Sheets e Slides têm limite de 10 MB do Google) |
| `HEALTH_POLL_MINUTES` | `10` | Intervalo entre testes de saúde automáticos |
| `LLM_EFFORT` | `medium` | Esforço padrão do Claude, até alguém mudar pelo console |

## 6.8 Comandos úteis

```bash
# Varrer as pastas do Drive agora, vendo o resultado no terminal
docker compose run --rm coletor python -m app.cli coletar-drive

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
