# 16. Extração de fatos: o Claude lê, o código confere

Este guia explica o segundo pedaço do **bloco 3 (silver)**: tirar de cada item as
tarefas, decisões, riscos e prazos, com a prova no texto. O código está em
`app/facts.py`; a tela, em **Fatos** (menu Trabalho).

## 16.1 Por que este é o próximo passo do roadmap

A classificação (guia 14) diz **de qual projeto** é cada item. Ela sozinha não responde
nenhuma pergunta de gestão. A extração é o que transforma "200 e-mails do Atlas" em
"3 decisões, 7 tarefas abertas, 2 riscos". Tudo o que vem depois lê esta tabela:

- **Alertas de liderança** ("o que a diretora pediu nesta semana?"): precisam saber o
  que foi pedido **e quem pediu**. Por isso a extração já grava quem disse.
- **Briefing diário, relatórios, MCP e Markdown de auditoria** (blocos 5, 7 e 8): todos
  contam e listam fatos.
- **RAID** (fase 2): riscos, premissas, problemas e dependências são quatro dos oito
  tipos.

Os outros itens abertos (agenda inteira em `meetings`, aviso de microfone) não
destravam nada. A Arquitetura (seção 17) marca a extração como o próximo do bloco 3.

## 16.2 O problema: o modelo inventa

Um modelo de linguagem, quando lê um e-mail e "resume as tarefas", às vezes escreve uma
tarefa que ninguém pediu, ou junta duas frases e muda o sentido. Num relatório de
projeto, uma tarefa inventada é pior que uma tarefa esquecida: ela parece verdadeira.

A Arquitetura (seção 8, decisão "controle de alucinação") escolheu: **todo fato traz o
trecho literal que o sustenta, e o código confere se o trecho existe no original.**

> **Decisão: quem confere o trecho.** Avaliamos **A, pedir ao próprio Claude que
> confira** e **B, o código procurar o trecho no texto**, e optamos por **B**: A usa o
> mesmo modelo que pode ter errado para julgar a si mesmo e custa outra chamada. B é uma
> busca de texto dentro de texto: exata, instantânea, de custo zero e que não alucina.

### Como a conferência funciona (`quote_found`)

1. Os dois textos vão para uma **forma comparável** (`normalize`): aspas curvas viram
   retas, travessões viram hífen, maiúsculas viram minúsculas e qualquer sequência de
   espaços e quebras de linha vira um espaço só. Assim, "Ficou “decidido”" e
   `ficou "decidido"` são iguais, mas uma palavra trocada não é.
2. Trecho com menos de 12 caracteres **não prova nada** ("sim", "ok" existem em quase
   todo texto) e fica suspeito.
3. Se o trecho inteiro está no texto: **conferido**.
4. Se o Claude usou reticências ("A ... B"), cada parte precisa estar no texto, **na
   ordem** e a no máximo 200 caracteres da anterior. Partes longe uma da outra podem
   juntar frases que, separadas, dizem outra coisa.

Fato com trecho não achado **não é apagado**: fica com `quote_verified = false` e a tela
marca **suspeito**. Muitas vezes o fato está certo e o Claude só trocou uma palavra ao
copiar; a decisão de descartar é sua. O número de suspeitos é também o termômetro do
modelo (16.5).

## 16.3 O que o Claude lê

| Fonte | Texto lido |
| --- | --- |
| E-mail | Assunto + **texto próprio** (sem a conversa citada abaixo de "escreveu:" e sem assinatura) + o texto dos anexos, cada um com o nome |
| Transcrição | Título + a transcrição inteira, no formato `[mm:ss] Nome: fala` |
| Drive | Nome do arquivo + o texto extraído |

**Por que tirar a conversa citada:** numa troca de 10 respostas, a primeira mensagem
aparece citada nas outras 9. Lendo tudo, a mesma tarefa sairia 10 vezes, e você pagaria
10 vezes. Cada mensagem é lida pelo que ela acrescenta. É a mesma função (`own_text`) que
as regras de tag usam.

Junto do texto vão: a **data do item com o dia da semana** (para o Claude transformar
"até sexta" na data certa), o **projeto** e a **equipe do projeto** (para ele escrever o
responsável com o nome cadastrado). Nada de outra Empresa entra no pedido.

**Quando um item espera:**

- sem projeto, ou na fila **A revisar**: o projeto do fato ainda pode mudar;
- "não é de projeto nenhum" (newsletter, pessoal): nunca é lido, e não gasta nada;
- com anexo esperando a leitura pelo Claude (PDF escaneado, imagem): lê depois, com o
  texto do anexo junto, para não perder a ata que veio em PDF.

## 16.4 Formato fixo da resposta (saída estruturada)

A classificação pede JSON "no texto" e o código procura o bloco `{...}` na resposta. Para
os fatos, a resposta é maior (até 40 fatos) e um JSON quebrado perderia todos. Aqui o
pedido usa a **saída estruturada** da API: o esquema (`SCHEMA` em `app/facts.py`) vai em
`output_config.format`, e a própria API garante que a resposta é um JSON válido naquele
formato (tipos fechados, todos os campos presentes, nada a mais).

> **Decisão: JSON no texto ou saída estruturada.** Avaliamos **A, pedir JSON na
> instrução e procurar o bloco**, como na classificação, e **B, saída estruturada com
> esquema**, e optamos por **B**: a resposta é uma lista longa, e com A um caractere
> errado descartaria a lista inteira; com B o formato é garantido pela API. O código
> ainda confere cada campo (uma data como "2026-02-30" é recusada) e trata a resposta
> cortada no limite de tamanho como "ilegível". A camada `app/llm.py` ganhou o parâmetro
> `json_schema` para isso; a classificação pode passar a usá-lo depois.

## 16.5 Qual modelo

| Modelo | Por e-mail | Por reunião de 1h | Mês (50 e-mails + 4 reuniões por dia) |
| --- | --- | --- | --- |
| Haiku 5.5 | ~US$ 0,0004 | ~US$ 0,003 | **~US$ 1** |
| Sonnet 5.5 | ~US$ 0,008 | ~US$ 0,05 | ~US$ 18 |
| Opus 5.5 | ~US$ 0,016 | ~US$ 0,10 | ~US$ 36 |

> **Decisão: modelo da extração.** Avaliamos **A, o modelo do console (Opus)**,
> **B, Sonnet** e **C, Haiku fixo (`FACTS_MODEL`)**, e optamos por **C**: com o teto de
> US$ 10 por mês para tudo, só o Haiku cabe. O risco de qualidade fica medido: a
> conferência do trecho pega o fato inventado, e a contagem de suspeitos na página Fatos
> mostra se ele está inventando. Se os suspeitos forem fatos errados (e não só trechos
> reescritos), a troca é uma linha no `.env`, e o teto precisa subir junto.

Esforço baixo (`effort: low`): copiar trechos e classificar em oito tipos não pede
raciocínio longo, e esforço maior gasta mais tokens de saída, que são os caros.

## 16.6 Pessoas: responsável e quem disse

- **Responsável:** o Claude escreve o nome como está no texto ("Beto"). O código liga à
  Pessoa com a mesma regra das falas (`PeopleIndex.match`: e-mail, nome ou outro nome,
  sem acento, e o convite da reunião), **só entre as Pessoas da Empresa do item**. Não
  achou: o nome fica em `owner_text`.
- **Quem disse:** no e-mail, o remetente (pelo e-mail dele). Na transcrição, o código
  procura a fala que contém o trecho e usa a pessoa daquela fala (que você pode ter
  ligado ao vivo). Fica em `said_by_id`, com o nome como apareceu em `said_by_text`.

> **Decisão: gravar quem disse já na extração.** Avaliamos **A, descobrir quem disse só
> quando as regras de alerta existirem** e **B, gravar agora**, e optamos por **B**: o
> custo é zero (o remetente e as falas já estão no banco) e, com a coluna, "o que
> lideranças decidiram" vira uma consulta SQL (`facts` com `stakeholders.is_leader`), sem
> ler de novo nem pagar o Claude de novo. O banco confere a Empresa também nesta coluna
> (gatilho da 0008, estendido na migração 0012).

## 16.7 Sem repetir e sem pagar duas vezes

- **`fact_extractions`:** um registro por item lido, com ou sem fatos. Sem ele, um
  "obrigada!" (zero fatos) voltaria ao Claude a cada 5 minutos. O registro guarda também
  o modelo, quantos fatos vieram, quantos foram conferidos e se o texto foi cortado.
- **Duas transcrições da mesma reunião** (a da extensão e a importada): a segunda é
  registrada como "duplicada" sem chamar o Claude. A ligação vem da tabela `meetings`
  (guia 15).
- **Projeto corrigido:** os fatos seguem o projeto do item numa consulta só
  (`realign_projects`), sem ler de novo.
- **Recusa e resposta ilegível:** ficam registradas e não voltam sozinhas (pagariam de
  novo e provavelmente falhariam de novo). O botão **Extrair de novo** do item tenta outra
  vez quando você quiser.

## 16.8 Limites conhecidos

- **Texto longo é cortado** em 60 mil caracteres (uma reunião de 1h30). Acima disso, o
  fim não é lido, e o registro marca "texto cortado". Ler em partes fica para quando
  aparecer o caso real.
- **O mesmo fato em itens diferentes** (a tarefa pedida no e-mail e repetida na
  reunião) vira dois fatos. Juntar fatos entre itens é tarefa do auditor da fase 2.
- **Situação do fato:** tarefas, riscos, problemas, dependências e prazos nascem
  "aberto"; fechar ainda não tem botão. Vem com a fila de fatos do console (bloco 4).
- **Prazo relativo** ("semana que vem") depende de o Claude contar o dia certo a partir da
  data do item. O código só confere se é uma data válida.

## 16.9 Onde está no código

| Arquivo | O que faz |
| --- | --- |
| `app/facts.py` | Fila, texto lido, pedido, esquema, conferência do trecho, pessoas e números da página |
| `app/llm.py` | Parâmetro `json_schema` (saída estruturada) |
| `app/routes_facts.py` e `app/templates/fatos.html` | Página Fatos e "Extrair de novo" |
| `app/templates/conteudo_item.html` | Seção Fatos do item |
| `app/cli.py` | Comando `extrair-fatos` e a rodada de 5 minutos no coletor |
| `migrations/versions/0012_extracao_de_fatos.py` | Colunas novas em `facts`, `fact_extractions` e a trava de Empresa |
| `tests/test_facts.py` | O que a extração precisa garantir |
