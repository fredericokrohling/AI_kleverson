"""Geometria dos gráficos de Configurações (rosca do disco e barras do servidor).

Por que desenhar no servidor, em SVG, em vez de usar uma biblioteca de gráficos em
JavaScript (Chart.js, ECharts): o console não carrega nada de fora da rede privada (nem
fontes do Google), e uma biblioteca de 200 KB para dois gráficos seria peso e dependência
a mais. SVG é desenho vetorial que o navegador entende nativamente: o Python calcula as
posições, o modelo HTML escreve os retângulos e os arcos. A dica ao passar o mouse vem do
<title> de cada barra, que todo navegador mostra sem JavaScript.

Este arquivo só faz contas (posições em pixels); as cores e os textos ficam no modelo
HTML (app/templates/_servidor_uso.html e config.html).
"""

# math.pi para o comprimento do círculo da rosca.
import math

# dataclass cria classes simples de dados.
from dataclasses import dataclass, field

# Data e hora.
from datetime import datetime

# Medições agrupadas em barras.
from app.server_metrics import Bucket

# Dias da semana abreviados, em português (segunda = 0, como no Python).
_WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


# --- Rosca --------------------------------------------------------------------------


# Um arco da rosca, desenhado como um traço de círculo com "tracejado" de um traço só.
@dataclass(frozen=True)
class Arc:
    # Comprimento do traço visível.
    dash: float
    # Comprimento do resto do círculo (o vão até o traço repetir).
    gap: float
    # Onde o traço começa, a partir do topo, no sentido horário (negativo, regra do SVG).
    offset: float


# Arcos da rosca para uma lista de porcentagens (somando até 100).
def donut_arcs(percents: list[float], radius: float, spacing: float = 2.0) -> list[Arc]:
    """O truque do traço: um círculo com stroke-dasharray "traço vão" desenha um arco.

    `spacing` é o espaço vazio entre fatias, em pixels, para uma não encostar na outra.
    """
    # Comprimento do círculo (2πr).
    circumference = 2 * math.pi * radius
    # Arcos.
    arcs: list[Arc] = []
    # Quanto do círculo já foi usado.
    start = 0.0
    # Cada fatia.
    for percent in percents:
        # Comprimento da fatia.
        length = circumference * percent / 100
        # Traço visível: tira o espaçamento; fatias minúsculas ficam com meio pixel para
        # não sumirem do gráfico (a tabela ao lado mostra o valor exato).
        dash = max(length - spacing, 0.5) if length > 0 else 0.0
        # Guarda.
        arcs.append(Arc(dash=round(dash, 2), gap=round(circumference, 2), offset=round(-start, 2)))
        # Anda.
        start += length
    # Devolve.
    return arcs


# --- Barras -------------------------------------------------------------------------


# Um pedaço empilhado de uma barra.
@dataclass(frozen=True)
class Segment:
    # Topo do retângulo (y cresce para baixo no SVG).
    y: float
    # Altura.
    height: float
    # Qual série (o modelo HTML escolhe a cor por ela).
    series: str


# Uma barra: posição, pedaços e a marca do pico.
@dataclass
class Bar:
    # Esquerda.
    x: float
    # Largura.
    width: float
    # Pedaços empilhados, de baixo para cima.
    segments: list[Segment] = field(default_factory=list)
    # Altura (y) da marca do pico; None = sem marca.
    peak_y: float | None = None
    # Texto da dica ao passar o mouse.
    tip: str = ""
    # Sem medição neste pedaço.
    empty: bool = False


# Um gráfico de barras pronto para o modelo HTML.
@dataclass
class BarChart:
    # Largura total do desenho.
    width: int
    # Altura total do desenho.
    height: int
    # Onde a área das barras começa, à esquerda.
    left: int
    # Onde a área das barras começa, no alto.
    top: int
    # Largura da área das barras.
    plot_width: int
    # Altura da área das barras.
    plot_height: int
    # Barras.
    bars: list[Bar]
    # Linhas de grade: (y, rótulo ou "").
    grid: list[tuple[float, str]]
    # Rótulos do eixo do tempo: (x, texto).
    ticks: list[tuple[float, str]]
    # Altura (y) da linha de atenção.
    limit_y: float
    # Valor da linha de atenção (%).
    limit: int


# Converte um valor em % (0 a 100) na altura y do desenho.
def _y(value: float, top: int, plot_height: int) -> float:
    # Limita a 0..100 (arredondamentos podem dar 100,1).
    value = min(max(value, 0.0), 100.0)
    # 100% fica no topo; 0% no pé.
    return round(top + plot_height * (1 - value / 100), 2)


# Texto do horário de uma barra para a dica.
def _when(bucket: Bucket, long: bool) -> str:
    # Período de 7 dias: dia da semana, data e horas.
    if long:
        # Ex.: "ter 03/11, 14:00–15:00".
        return (
            f"{_WEEKDAYS[bucket.start.weekday()]} {bucket.start:%d/%m}, "
            f"{bucket.start:%H:%M}–{bucket.end:%H:%M}"
        )
    # Período de 2 horas: só as horas.
    return f"{bucket.start:%H:%M}–{bucket.end:%H:%M}"


# Rótulos do eixo do tempo.
def _ticks(buckets: list[Bucket], slot: float, left: int, long: bool) -> list[tuple[float, str]]:
    # Resultado.
    ticks: list[tuple[float, str]] = []
    # Cada barra.
    for i, bucket in enumerate(buckets):
        # Início desta barra.
        start: datetime = bucket.start
        # 7 dias: um rótulo por dia, à meia-noite.
        if long and start.hour == 0 and start.minute == 0:
            # Ex.: "ter 03/11".
            ticks.append((round(left + i * slot, 2), f"{_WEEKDAYS[start.weekday()]} {start:%d/%m}"))
        # 2 horas: um rótulo a cada meia hora.
        elif not long and start.minute % 30 == 0:
            # Ex.: "14:30".
            ticks.append((round(left + i * slot, 2), f"{start:%H:%M}"))
    # Devolve.
    return ticks


# Monta um gráfico de barras empilhadas.
def bar_chart(
    buckets: list[Bucket],
    # Séries empilhadas, de baixo para cima: nomes de atributos do Bucket.
    series: list[str],
    # Atributo com o pico do pedaço (a marca fina acima da barra).
    peak: str,
    # Monta a dica de cada barra.
    describe,
    # Período longo (7 dias) ou curto (2 horas).
    long: bool,
    # Linha de atenção, em %.
    limit: int,
) -> BarChart:
    """Calcula posição e altura de cada pedaço; o modelo HTML só desenha."""
    # Tamanho do desenho (o SVG estica para a largura da tela mantendo a proporção).
    width, height = 720, 170
    # Margens: à esquerda, os rótulos de %; embaixo, os horários.
    left, top, right, bottom = 34, 8, 6, 22
    # Área das barras.
    plot_width, plot_height = width - left - right, height - top - bottom
    # Espaço de cada barra.
    slot = plot_width / max(len(buckets), 1)
    # Vão entre barras: 2 px quando há espaço, 1 px quando as barras são finas.
    gap = 2 if slot >= 6 else 1
    # Barras.
    bars: list[Bar] = []
    # Cada pedaço do tempo.
    for i, bucket in enumerate(buckets):
        # Barra nesta posição.
        bar = Bar(
            # Esquerda (meio vão de cada lado).
            x=round(left + i * slot + gap / 2, 2),
            # Largura.
            width=round(max(slot - gap, 1), 2),
            # Dica.
            tip=f"{_when(bucket, long)} · {describe(bucket)}",
        )
        # Sem medição: barra vazia (buraco).
        if bucket.samples == 0:
            # Marca.
            bar.empty = True
            # Dica própria.
            bar.tip = f"{_when(bucket, long)} · sem medição (coletor parado)"
            # Guarda e segue.
            bars.append(bar)
            continue
        # Base do empilhamento (0%).
        base = 0.0
        # Cada série, de baixo para cima.
        for name in series:
            # Valor da série.
            value = getattr(bucket, name)
            # Topo e pé do pedaço.
            y_top, y_bottom = _y(base + value, top, plot_height), _y(base, top, plot_height)
            # Só desenha pedaço com altura visível.
            if y_bottom - y_top >= 0.5:
                # Guarda.
                bar.segments.append(
                    Segment(y=y_top, height=round(y_bottom - y_top, 2), series=name)
                )
            # Sobe a base.
            base += value
        # Marca do pico, só quando fica acima do topo da barra (pico = média: sem marca).
        peak_value = getattr(bucket, peak)
        # Pico pelo menos 1 ponto acima do topo da barra.
        if peak_value - base >= 1:
            # Posição.
            bar.peak_y = _y(peak_value, top, plot_height)
        # Guarda.
        bars.append(bar)
    # Linhas de grade a cada 25%, com rótulo em 0, 50 e 100.
    grid = [(_y(v, top, plot_height), f"{v}%" if v % 50 == 0 else "") for v in (0, 25, 50, 75, 100)]
    # Pronto.
    return BarChart(
        # Tamanho e área (ver a classe BarChart).
        width=width,
        height=height,
        left=left,
        top=top,
        plot_width=plot_width,
        plot_height=plot_height,
        # Barras.
        bars=bars,
        # Grade.
        grid=grid,
        # Horários.
        ticks=_ticks(buckets, slot, left, long),
        # Linha de atenção.
        limit_y=_y(limit, top, plot_height),
        # Valor dela.
        limit=limit,
    )
