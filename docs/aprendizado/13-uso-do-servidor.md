# 13. Uso do servidor: disco por projeto e gargalos de processador e memória

Pedido da dona em 03/11/2026: ver em Configurações quanto do disco cada projeto e o
sistema ocupam, e o uso do servidor nas últimas 2 horas ou nos últimos 7 dias, para saber
se o hardware dá conta e se há momentos de gargalo.

Este guia explica **o que** medimos, **por que** essas medidas e não outras, e as
escolhas de implementação. O passo a passo de instalação está em
[docs/setup/14-uso-do-servidor.md](../setup/14-uso-do-servidor.md).

## 13.1 A pergunta certa: "o servidor aguenta?"

"CPU em 30%" não responde se o servidor aguenta. Três motivos:

1. **Gargalo é padrão no tempo.** Um número de agora não mostra que todo dia às 9h, quando
   o Gmail traz a leva da manhã, o processador fica no limite por 10 minutos. Por isso
   gravamos uma medição por minuto e mostramos dois períodos: 2 horas (para ver o pico de
   uma coleta) e 7 dias (para ver o padrão dos dias e horários).
2. **Média esconde pico.** Uma hora com média de 20% pode ter tido 5 minutos a 100%.
   Cada barra mostra a média do pedaço e, num traço fino acima, o minuto mais cheio.
3. **O Lightsail engana o Linux.** As máquinas do Lightsail são "com pico" (*burstable*):
   juntam créditos quando estão paradas e gastam quando trabalham. Sem créditos, a AWS
   limita o processador da máquina. Lá dentro, o Linux enxerga isso como **tempo
   roubado** (*steal*): o processador parece livre, mas a máquina está estrangulada. Este
   é o indicador mais importante neste servidor, e o primeiro que um painel comum esquece.

> **Decisão:** avaliamos mostrar só "CPU %" e "memória %" (o comum) e mostrar processador
> separado em trabalhando, espera de disco e roubado, mais memória e swap. Optamos pela
> segunda: são as três causas diferentes de lentidão, e cada uma tem um remédio diferente
> (plano com mais processador, disco mais rápido, plano maior por causa dos créditos).

## 13.2 O que cada número significa

| Indicador | O que é | Quando preocupa | O que resolve |
| --- | --- | --- | --- |
| **Trabalhando** (azul) | % do minuto em que o processador executou programas e o sistema | trabalhando + roubado ≥ 90% em mais de 5% dos minutos | plano com mais processador, ou espalhar as tarefas no tempo |
| **Espera de disco** (laranja) | % do minuto com o processador parado esperando leitura ou gravação | média ≥ 10% ou pico ≥ 40% | disco mais rápido, menos gravação simultânea |
| **Roubado pela AWS** (violeta) | % do minuto em que a AWS não entregou o processador | média ≥ 5% ou pico ≥ 25% | plano maior do Lightsail (mais créditos de pico) |
| **Memória** | % da RAM em uso pelos programas (o cache do Linux não conta) | pico ≥ 90% (atenção a partir de 80%) | plano com mais memória |
| **Swap** | % da memória de troca em uso (só se o servidor tiver swap) | ≥ 20% | plano com mais memória |
| **Carga** | quantos processos queriam o processador no mesmo minuto | acima do número de núcleos, alguém ficou na fila | mesmo remédio do processador |

Os limites ficam no começo de `app/server_metrics.py` (`CPU_LIMIT`, `MEM_LIMIT`...). São
pontos de partida conservadores, não leis: se depois de algumas semanas eles avisarem
demais ou de menos, ajuste ali.

**Por que memória "em uso" e não "livre":** o Linux enche a memória livre com cache de
disco de propósito (deixa leituras mais rápidas) e devolve na hora quando um programa
pede. Medir pela memória "livre" (`MemFree`) daria 95% de uso numa máquina folgada.
Usamos `MemAvailable`, a estimativa do próprio Linux de quanto ainda pode ser usado.

**Por que não usamos PSI** (*pressure stall information*, `/proc/pressure`): é o
indicador mais direto de "tarefas esperando recurso", mas é mais difícil de explicar e de
ajustar. Os seis números acima cobrem as mesmas causas. Fica como evolução possível.

## 13.3 Como a medição funciona

- **De onde vêm os números:** arquivos especiais do Linux: `/proc/stat` (tempo do
  processador em cada estado), `/proc/meminfo` (memória) e `/proc/loadavg` (carga).
  Dentro de um contêiner Docker esses arquivos mostram **a máquina inteira**, porque o
  Docker não isola o `/proc`. Então o coletor enxerga o servidor todo: banco, console e
  ele mesmo.
- **Como sai o %:** `/proc/stat` traz contadores que só crescem desde que o servidor
  ligou. A diferença entre duas leituras com 1 minuto de distância diz quanto do minuto
  foi gasto em cada estado. Por isso cada medição é a **média do minuto**, e não uma foto
  de um instante (uma foto pode cair justo num momento parado ou num pico).
- **Quem mede:** uma *thread* (linha de execução paralela) dentro do serviço **coletor**.
  > **Decisão:** avaliamos (a) medir no laço principal do coletor, (b) criar um serviço
  > novo no Docker Compose só para medir e (c) uma thread no coletor. Optamos por (c).
  > (a) falha justo quando mais importa: uma coleta do Gmail ocupa o laço por minutos, e
  > ninguém mediria durante a coleta. (b) funciona, mas é mais um contêiner com ~40 MB de
  > memória para fazer uma leitura por minuto. A thread custa quase nada e roda no seu
  > próprio ritmo. Se o coletor cair, a medição para junto, e isso aparece como buraco no
  > gráfico ("sem medição"), o que também é informação.
- **Onde fica:** tabela `server_metrics`, uma linha por minuto, guardada por 8 dias
  (~11,5 mil linhas, menos de 2 MB). A própria medição apaga o que passou de 8 dias.

## 13.4 Disco: o que é "de um projeto"

A rosca mostra o disco inteiro, fatiado em:

- **cada projeto:** os arquivos originais guardados (e-mails `.eml`, anexos, arquivos do
  Drive, imagens tiradas de PowerPoint e Word), somados pelo projeto do item. "Projeto do
  item" segue a regra de todo o console: o item tem a tag do projeto (regra automática,
  pasta do Gmail, à mão ou, no Drive, a tag herdada da pasta). Até 5 projetos aparecem com
  fatia própria; com mais, os menores viram "Outros projetos";
- **sem projeto:** arquivos de itens sem tag de projeto;
- **banco de dados:** o tamanho do Postgres;
- **sistema:** todo o resto do que está ocupado: Linux, Docker e as imagens dos
  contêineres, logs e os ~5% que o Linux reserva para si;
- **livre.**

Escolhas e o porquê:

- **Arquivo usado por dois projetos é dividido ao meio.** Os arquivos são guardados uma
  vez só (o nome é o SHA-256 do conteúdo). Se contássemos inteiro em cada projeto, as
  fatias somariam mais que o disco. Dividir mantém a soma honesta.
- **O banco não é dividido por projeto.** O Postgres não informa o espaço ocupado por
  linha; qualquer divisão seria estimativa inventada. Na prática o banco é pequeno perto
  dos arquivos.
- **Tamanho no disco, não tamanho do conteúdo.** Um arquivo de 300 bytes ocupa um bloco
  de 4 KB. Usamos o espaço em blocos (o mesmo número do comando `du`), porque é isso que
  enche o disco.
- **Medido a cada 10 minutos, não a cada abertura da página.** Medir passa por todos os
  arquivos; com dezenas de milhares, leva alguns segundos. O disco não muda de forma
  relevante em 10 minutos.
- **"Ocupado" inclui a reserva do Linux.** O teste de disco da página Saúde passou a usar
  a mesma conta (tudo menos o livre), para os dois números baterem. Antes ele não contava
  a reserva de ~5%, por isso o número de lá pode subir alguns pontos depois desta versão.

**Crítica franca sobre a rosca:** hoje os projetos devem aparecer como fatias finíssimas
(menos de 1% do disco), porque o sistema e o Docker ocupam muito mais que os seus dados.
É a resposta verdadeira, e é boa notícia: o disco não está enchendo por causa dos
projetos. Para comparar projetos entre si, use a tabela ao lado e a frase abaixo dela
("o projeto que mais ocupa é X, com Y% desses dados"), não o tamanho das fatias.

## 13.5 Por que os gráficos são desenhados no servidor, em SVG

> **Decisão:** avaliamos uma biblioteca de gráficos em JavaScript (Chart.js, ECharts) e
> desenho em SVG feito pelo Python. Optamos pelo SVG. O console não carrega nada de fora
> da rede privada (nem fontes do Google), e uma biblioteca de 200 KB para dois gráficos
> seria peso e dependência a mais. SVG é desenho vetorial que todo navegador entende: o
> Python (`app/charts.py`) calcula a posição de cada barra e de cada arco, e o modelo HTML
> só desenha. A dica ao passar o mouse vem do `<title>` de cada barra, sem JavaScript.

- **A rosca** é um círculo com traço grosso. O atributo `stroke-dasharray` ("traço vão")
  faz o traço aparecer só num pedaço do círculo; `stroke-dashoffset` diz onde ele começa.
  Uma fatia por círculo, cada uma começando onde a anterior terminou.
- **As cores** vêm de uma paleta testada para daltonismo sobre o fundo escuro dos cartões
  (validador de paleta, distância de cor mínima entre vizinhas). Cada projeto tem uma cor
  fixa pela ordem de cadastro, e não pelo tamanho: assim um projeto não troca de cor
  quando outro passa à frente. Banco e sistema ficam em cinza, porque a cor é reservada
  para o que é seu.
- **Um gráfico para processador e outro para memória**, cada um com a sua escala de 0 a
  100%. Juntar os dois num gráfico só, com dois eixos, faria parecer que um acompanha o
  outro quando a escala é que foi ajustada.
- **Atualização:** no período de 2 horas, o bloco se atualiza sozinho a cada minuto
  (HTMX `hx-trigger="every 60s"`), junto com a medição nova. O de 7 dias não, porque uma
  barra de 1 hora não muda de um minuto para o outro.
