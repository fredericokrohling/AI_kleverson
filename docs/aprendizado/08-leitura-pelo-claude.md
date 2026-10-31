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
- **Ícone não é conteúdo.** Imagem com o lado maior abaixo de 200 pixels vira
  `IMAGEM_PEQUENA` e não é enviada. O logo da assinatura, embutido no corpo do e-mail, já
  era ignorado desde o bloco 2.
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

## Onde o texto vai parar

No mesmo lugar do texto extraído por biblioteca:

- anexo de e-mail: `attachments.extracted_text`;
- arquivo do Drive: o `body_text` da versão atual em `bronze_items`. A impressão digital da
  versão passa a ser a do texto, como numa leitura normal, para a próxima varredura do
  Drive comparar certo e não criar uma versão nova sem mudança.

O original continua guardado e intocado: se um modelo melhor aparecer, dá para ler de novo.
