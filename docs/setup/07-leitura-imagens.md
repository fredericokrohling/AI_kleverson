# 7. Leitura de imagens e PDFs digitalizados pelo Claude

**O que muda:** imagens (PNG, JPG, GIF, WebP, BMP, TIFF) e PDFs digitalizados, que antes
ficavam na lista **Arquivos sem texto** com `IMAGEM_SEM_OCR` ou `PDF_SEM_TEXTO`, passam a
ser lidos pelo Claude. Vale para anexos de e-mail, arquivos das pastas monitoradas do Drive
e arquivos do Drive citados por link. O texto entra no banco como qualquer outro, com um
aviso no começo dizendo que foi lido pelo Claude.

O porquê de cada escolha está em
[docs/aprendizado/08-leitura-pelo-claude.md](../aprendizado/08-leitura-pelo-claude.md).

**Pré-requisito:** a chave da Anthropic (`ANTHROPIC_API_KEY`) já configurada no guia
[03-anthropic.md](03-anthropic.md). Sem ela, nada é lido e as imagens continuam na lista.

## 7.1 Instalar

No servidor:

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

O `--build` é necessário: a imagem Docker ganha a biblioteca Pillow (imagens). Não há
migração de banco nesta versão.

Se você já usa o botão **Atualizar** do console (6.1), ele faz o mesmo.

## 7.2 Como funciona no dia a dia

1. A coleta do Gmail e a do Drive guardam a imagem como sempre. Ela entra na lista
   **Arquivos sem texto** com o código de espera (`IMAGEM_SEM_OCR` ou `PDF_SEM_TEXTO`),
   agora com a explicação "aguardando leitura pelo Claude".
2. A cada 5 minutos, o serviço `coletor` manda até 10 arquivos da fila ao Claude. Lidos,
   eles saem da lista.
3. **Tentar de novo** (em uma linha ou em todas) lê na hora, sem esperar os 5 minutos.
4. O texto aparece em **Ver o conteúdo coletado** (6.6.1), começando com
   `[Lido pelo Claude (modelo) a partir de imagem; confira números e nomes importantes no original.]`.
5. Cada rodada fica em **Processamento** como `claude.leitura_visual`, com quantos foram
   lidos e quantos deram erro. O custo de cada leitura entra no teto mensal do Claude.

Não vão para o Claude, para não gastar à toa:

- imagens embutidas no corpo do e-mail (logo da assinatura); isso já era assim;
- imagens com o lado maior abaixo de 200 pixels (ícones): ficam como `IMAGEM_PEQUENA`;
- avisos que você **dispensou**;
- o mesmo arquivo anexado em vários e-mails: é lido uma vez e o texto vale para todos.

## 7.3 Testar

1. Mande para o seu Gmail um e-mail com um diagrama em PNG anexado (por exemplo, um
   fluxograma exportado do Lucid) e um PDF digitalizado.
2. Espere a coleta (até 5 minutos). Os dois aparecem em **Arquivos sem texto** com
   "aguardando leitura pelo Claude".
3. Clique **Tentar de novo** no PNG. Deve aparecer **Funcionou** e a linha some.
4. Clique no nome do e-mail em **Ver o conteúdo coletado** e abra o anexo: confira se o
   texto e a descrição do diagrama batem com a imagem.
5. Espere mais 5 minutos sem clicar no PDF: ele deve sair da lista sozinho.
6. Em **Configurações → Claude**, confira o gasto do mês.

Para forçar uma rodada pelo terminal, vendo o resultado:

```bash
docker compose run --rm coletor python -m app.cli ler-imagens
```

## 7.4 Quanto custa

Estimativa por arquivo, com os preços de outubro de 2026 (confira em
https://www.anthropic.com/pricing):

| Modelo | Imagem | PDF digitalizado de 10 páginas |
| --- | --- | --- |
| Opus 5.5 (padrão do console) | cerca de US$ 0,03 | cerca de US$ 0,25 |
| Sonnet 5.5 | cerca de US$ 0,015 | cerca de US$ 0,12 |
| Haiku 5.5 | menos de US$ 0,001 | cerca de US$ 0,01 |

Com o teto de US$ 10 e o Opus, cabem umas 300 imagens por mês, dividindo o teto com o
resto do sistema. Quando o teto acaba, nada é perdido: os arquivos esperam na fila e são
lidos no mês seguinte (ou quando você aumentar o teto).

Para gastar menos, defina um modelo só para a leitura (7.5). Recomendação: comece com o
padrão, compare algumas leituras com o original e só então experimente o Haiku.

## 7.5 Ajustes (no `.env`)

Depois de mudar o `.env`, rode `docker compose up -d`.

| Variável | Padrão | Para quê |
| --- | --- | --- |
| `VISION_MODEL` | vazio | Modelo só para a leitura (ex.: `claude-haiku-5-5`). Vazio = o escolhido no console |
| `VISION_MAX_PDF_PAGES` | `30` | PDF digitalizado com mais páginas não é enviado (custo) |
| `VISION_FILES_PER_RUN` | `10` | Arquivos por rodada de 5 minutos |

## 7.6 Códigos novos na lista

| Código | O que significa | O que fazer |
| --- | --- | --- |
| `IMAGEM_SEM_OCR` | Imagem aguardando o Claude | Esperar a rodada ou Tentar de novo |
| `PDF_SEM_TEXTO` | PDF digitalizado aguardando o Claude | Esperar a rodada ou Tentar de novo |
| `IMAGEM_PEQUENA` | Ícone ou logo (lado maior abaixo de 200 pixels) | Dispensar |
| `IMAGEM_FORMATO` | Formato que o sistema não abre (ex.: HEIC do iPhone) ou arquivo corrompido | Salvar como PNG ou JPG na pasta monitorada |
| `PDF_LONGO` | PDF digitalizado acima de `VISION_MAX_PDF_PAGES` | Aumentar o limite (7.5) e Tentar de novo, ou dividir o PDF |
| `CLAUDE_ARQUIVO_GRANDE` | PDF digitalizado acima de 20 MB (limite da API) | Dividir o PDF |
| `CLAUDE_FALHOU` | A API do Claude falhou nesse arquivo | Tentar de novo; a rodada automática não insiste, para não gastar em loop |
| `CLAUDE_RECUSOU` | O Claude recusou o conteúdo (política de segurança) | Nada; fica registrado |
| `CLAUDE_SEM_CHAVE` | Aviso do Tentar de novo: falta `ANTHROPIC_API_KEY` | Configurar a chave (guia 3) |
| `CLAUDE_TETO_MENSAL` | Aviso do Tentar de novo: teto do mês atingido | Esperar o mês virar ou aumentar o teto |

Os dois últimos só aparecem no aviso do topo da página: o arquivo continua na fila com o
código de espera.

## Limitações conhecidas

- **PDF com parte das páginas digitalizada:** se o PDF tem texto em algumas páginas, só o
  texto é lido; as páginas que são só imagem ficam de fora, sem aviso.
- **Imagens dentro de Word, PowerPoint e Excel** não são lidas; só o texto desses arquivos.
- **GIF animado e TIFF de várias páginas:** só o primeiro quadro.
- **Imagem muito comprida** (print de uma página inteira): é reduzida para 2576 pixels no
  lado maior, e letras pequenas podem ficar ilegíveis. Corte em partes antes de salvar.
- **O Claude pode errar uma leitura**, principalmente números em imagem ruim. Por isso o
  aviso no começo do texto; confira no original o que for importante.
