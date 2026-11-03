# 8. Leitura de imagens e PDFs digitalizados pelo Claude

Este guia explica o porquê da leitura de imagens. O passo a passo está em
[docs/setup/07-leitura-imagens.md](../setup/07-leitura-imagens.md).

Código: `app/ingestion/vision.py` (o leitor) e `app/ingestion/vision_queue.py` (a fila).

## O problema

Uma imagem não tem texto para uma biblioteca tirar. O que o sistema faz com PDF, Word e
Excel (abrir o arquivo e copiar o texto) não funciona com um PNG ou com um PDF que é só a
foto de páginas. Até aqui, esses arquivos ficavam guardados, mas a IA não sabia o que havia
neles. Nos seus projetos, boa parte disso é diagrama exportado do Lucid ou do Canva, print
de tela e documento assinado e digitalizado.

## Claude ou OCR

Avaliamos duas formas de ler imagem:

| | OCR (Tesseract) | Claude (visão) |
| --- | --- | --- |
| Custo | Zero por arquivo | Por arquivo (centavos), dentro do teto mensal |
| Texto corrido | Bom em documento limpo, fraco em foto torta | Bom nos dois |
| Diagrama | Devolve as palavras soltas, sem dizer quem liga em quem | Transcreve e descreve a estrutura (A → B, responsáveis, datas) |
| Tabela | Perde as colunas com frequência | Mantém linha e coluna |
| Servidor | Mais um programa grande na imagem Docker, mais memória | Nada novo além da biblioteca Pillow |
| Risco | Erro de leitura óbvio (letras trocadas) | Erro que parece certo (um número lido errado, com boa redação) |

**Optamos pelo Claude**, por sua escolha: o que mais importa aqui é diagrama, e o OCR não
entende desenho. O risco do erro "que parece certo" é tratado de dois jeitos: o texto
começa com um aviso (`[Lido pelo Claude (modelo) a partir de imagem; confira números e
nomes importantes no original.]`), que a IA do bloco 3 também lê; e a instrução manda
escrever `[ilegível]` em vez de completar.

## Na hora da coleta ou numa fila

Avaliamos ler a imagem durante a coleta e ler numa etapa separada.

- **Durante a coleta:** mais simples, o texto já nasce pronto. Mas a coleta do Gmail
  passaria a depender da API da Anthropic. Se a API cair, ou o teto do mês acabar, o
  e-mail falha junto, e a coleta é o que não pode parar.
- **Numa fila (escolhido):** a coleta grava a imagem com um código de espera
  (`IMAGEM_SEM_OCR` ou `PDF_SEM_TEXTO`) e segue. A cada 5 minutos o `coletor` pega até 10
  arquivos da fila e manda ao Claude. Custa até 5 minutos de atraso, e o "Tentar de novo"
  do console fura a fila quando você tem pressa.

A fila não é uma tabela nova: é "todo arquivo cujo erro começa com um código de espera".
Os códigos já existiam, então os arquivos que chegaram antes desta versão entram na fila
sozinhos, sem migração.

## Regras para não gastar à toa

Cada leitura custa dinheiro, então a fila tem regras que um OCR gratuito não precisaria:

- **O mesmo arquivo é lido uma vez.** Um diagrama encaminhado em cinco e-mails tem a mesma
  impressão digital (SHA-256). A fila lê o primeiro e copia o texto para os outros.
- **Só a versão atual de um arquivo do Drive.** Versões antigas não são lidas.
- **Imagem de e-mail com menos de 70 KB é ignorada** (desde 03/11/2026), colada no corpo
  ou anexada pelo clipe: nem vira linha, continua só no `.eml` guardado. Veja "O limite de
  70 KB" abaixo.
- **Ícone não é conteúdo.** Imagem anexada com o lado maior abaixo de 200 pixels vira
  `IMAGEM_PEQUENA` e não é enviada. Imagem pequena **colada no corpo** do e-mail (logo e
  ícones da assinatura) nem vira linha: continua só no `.eml` guardado.
- **Aviso dispensado não é lido.** Se você dispensou, é porque não interessa.
- **Falha da API não é repetida sozinha.** O arquivo sai da fila com `CLAUDE_FALHOU` e só
  volta pelo "Tentar de novo". Sem isso, um arquivo que sempre falha seria cobrado (ou
  tentado) a cada 5 minutos, para sempre. O SDK da Anthropic já tenta de novo erros
  passageiros (rede, sobrecarga) antes de desistir.
- **Teto atingido para a rodada antes de gastar.** O `LLMClient` confere o gasto do mês
  antes de cada chamada. Na fila, o teto encerra a rodada e os arquivos esperam; no
  "Tentar de novo", aparece o aviso `CLAUDE_TETO_MENSAL`, e o arquivo continua esperando.

## Esforço baixo e modelo configurável

Transcrever não exige raciocínio longo. Por isso a leitura pede `effort: low`, o que corta
os tokens de "pensamento", que são cobrados como saída.

O modelo é o mesmo escolhido no console, a não ser que `VISION_MODEL` diga outro. Avaliamos
fixar um modelo barato (Haiku) para a leitura e usar o do console. Optamos pelo do console
como padrão: a primeira impressão de qualidade decide se você confia no recurso, e o Haiku
pode ser testado depois, com uma linha no `.env`, comparando leituras reais.

## Preparar a imagem antes de enviar

A API tem limites: lado maior de até 2576 pixels, até 5 MB por imagem, e o pedido inteiro
até 32 MB. O `prepare_image`:

1. gira a foto conforme a orientação gravada pela câmera do celular (senão o Claude recebe
   a foto deitada);
2. reduz para caber em 2576 pixels e 3,75 megapixels (mandar maior só gasta banda, a API
   reduziria do mesmo jeito);
3. grava em PNG, que não perde nitidez nas letras de um diagrama;
4. se o PNG passar de 3,7 MB (vira cerca de 4,9 MB em base64), troca para JPEG, que
   comprime foto muito melhor.

PDF vai inteiro, como "documento": a API transforma cada página em imagem do lado de lá.
O limite de 20 MB e de `VISION_MAX_PDF_PAGES` páginas existe porque o custo cresce por
página.

## A instrução

O texto de sistema (`_SYSTEM` em `vision.py`) diz para quem é o resultado (outra IA, que
responde sobre projetos) e dá regras objetivas: transcrever na ordem de leitura sem
traduzir; tabelas com `|`; diagramas com uma seção "Estrutura:" de ligações; foto sem texto
com uma ou duas frases; `[ilegível]` em vez de inventar; `### Página N` em PDF (o mesmo
marcador que o extrator de PDF já usa, para a IA citar a página).

## Imagens dentro de PowerPoint e Word

Avaliamos dois jeitos de ler a imagem colada num slide:

- **Juntar o texto da imagem ao texto do arquivo:** um texto só por arquivo. Mas o texto
  do arquivo é pronto na hora, e o da imagem depende do Claude: a versão ficaria
  "incompleta" até a fila andar, e uma falha numa imagem se misturaria ao arquivo inteiro.
- **Uma linha por imagem, ligada ao mesmo item (escolhido):** cada imagem vira um
  `attachment` com origem `imagem_embutida`, ligado ao e-mail ou à versão do Drive. Ela
  entra na mesma fila, com as mesmas regras de custo, aparece na lista de arquivos sem
  texto se falhar, e pode ser tentada de novo sozinha.

O nome da linha guarda a posição (`Status.pptx › slide 3 › image2.png`): a IA do bloco 3
sabe que aquela descrição é do slide 3. O campo `external_ref` guarda a impressão digital
do arquivo de onde a imagem saiu; é assim que o comando `extrair-imagens-embutidas` sabe
que um arquivo já foi tratado e não duplica.

Filtros antes da fila: imagens repetidas no mesmo arquivo viram uma; ícones abaixo de 200
pixels e formatos que não abrem são ignorados sem aviso (num deck corporativo seriam
dezenas de linhas inúteis na lista); no Word, só o corpo do documento (cabeçalho e rodapé
guardam o logo); e no máximo 20 imagens por arquivo, para um deck de prints não consumir o
teto do mês de uma vez.

## O limite de 70 KB nas imagens de e-mail (03/11/2026)

O filtro por pixels (200 no lado maior) não bastava: o logo de uma assinatura costuma ter
de 300 a 600 pixels de largura, passava no teste e ia para a fila do Claude. Você pediu um
limite pelo **tamanho do arquivo**: toda imagem de e-mail com menos de 70 KB é ignorada na
coleta, seja colada no corpo, seja anexada pelo clipe.

Por que o tamanho do arquivo funciona: logo e ícone são imagens simples (poucas cores,
fundo liso), que o PNG e o JPEG comprimem muito; quase nunca passam de 30 KB. Um print de
tela ou uma foto de documento tem muito mais detalhe e passa de 70 KB com folga.

Como fica a regra completa, na ordem:

1. É imagem (pelo tipo ou pela extensão, porque há programas que mandam PNG como
   `application/octet-stream`) e tem menos de 70 KB (70 x 1024 bytes)? Ignorada.
2. Anexada pelo clipe? Entra (se tiver menos de 200 pixels no lado maior, entra como
   `IMAGEM_PEQUENA`, sem ir ao Claude).
3. Colada no corpo? Entra só se tiver 200 pixels ou mais no lado maior.

**O preço, com franqueza:** um print pequeno de verdade (três linhas de uma tabela, um
trecho de cronograma) pode ter menos de 70 KB e será ignorado sem aviso. Se isso
acontecer com algo importante, o original continua no `.eml`; mande o arquivo para uma
pasta do Drive monitorada (lá não há limite de tamanho). Se acontecer com frequência, o
número fica em `MIN_EMAIL_IMAGE_BYTES`, em `app/ingestion/gmail.py`.

O que o limite **não** pega: imagens das pastas do Drive e imagens de dentro de PowerPoint
e Word anexados (essas têm a regra própria: a repetida vira uma, ícones e cabeçalho do
Word ficam de fora). E não mexe no que já foi coletado: as imagens pequenas já gravadas
continuam onde estão.

## O erro do filtro de assinatura (corrigido em 02/11/2026)

No bloco 2, o leitor de e-mail descartava toda imagem que tivesse o cabeçalho
`Content-ID`, achando que era o logo da assinatura. A premissa estava errada: o Gmail põe
`Content-ID` também nas imagens anexadas pelo clipe. Resultado: nenhuma imagem anexada a
e-mail entrava no sistema, e o teste real da dona (uma certidão em PNG) mostrou isso.

A correção troca o critério. Toda imagem é lida do e-mail. A que foi anexada vira linha
sempre. A colada no corpo só vira linha se for grande o bastante para ter conteúdo (200
pixels no lado maior); o logo e os ícones da assinatura ficam de fora pelo tamanho, não
pelo cabeçalho. O logo maior que isso entra, mas a regra "o mesmo arquivo é lido uma vez"
faz ele custar uma leitura só, não uma por e-mail.

Os e-mails coletados antes da correção não precisam ser baixados de novo: o `.eml`
original está guardado, e o comando `recuperar-imagens-emails` relê cada um e cria as
linhas que faltam.

A lição: um teste com dado real pegou o que os testes automáticos não pegaram, porque o
e-mail de teste foi montado com a mesma premissa errada do código.

## Onde o texto vai parar

No mesmo lugar do texto extraído por biblioteca:

- anexo de e-mail: `attachments.extracted_text`;
- arquivo do Drive: o `body_text` da versão atual em `bronze_items`. A impressão digital da
  versão passa a ser a do texto, como numa leitura normal, para a próxima varredura do
  Drive comparar certo e não criar uma versão nova sem mudança.

O original continua guardado e intocado: se um modelo melhor aparecer, dá para ler de novo.
