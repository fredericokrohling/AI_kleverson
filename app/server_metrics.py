"""Uso do servidor: medição a cada minuto e os dados dos gráficos de Configurações.

O que medimos e por quê (o objetivo é saber se o hardware dá conta e onde aperta):
- processador trabalhando (cpu_pct): o uso "normal" dos programas;
- espera de disco (iowait_pct): o processador parado esperando leitura ou gravação. Alta
  = o gargalo é o disco, não o processador;
- tempo roubado (steal_pct): o tempo em que a AWS não entregou o processador à máquina.
  O Lightsail é "com pico" (burstable): a máquina junta créditos parada e gasta quando
  trabalha; sem crédito, a AWS segura o processador e isso aparece aqui. É o indicador
  mais importante neste servidor, porque o painel do Linux mostraria "CPU 30%" enquanto
  a máquina está, na prática, estrangulada;
- memória em uso (mem_pct) e swap (swap_pct): memória acabando deixa tudo lento (o Linux
  passa a usar o disco como memória) e, no limite, o Linux mata um processo;
- carga de 1 minuto (load1): quantos processos queriam o processador ao mesmo tempo.
  Acima do número de núcleos, alguém ficou na fila.

De onde vêm os números: arquivos especiais do Linux em /proc. Dentro de um contêiner do
Docker eles mostram a máquina inteira (o Docker não isola /proc/stat nem /proc/meminfo),
então o coletor enxerga o servidor todo, banco e console incluídos. É o que queremos.

Como o % do processador é calculado: /proc/stat traz contadores que só crescem (tempo
gasto em cada estado desde que o servidor ligou). Uma leitura sozinha não diz nada; a
diferença entre duas leituras com 1 minuto de distância diz quanto do minuto foi gasto em
cada estado. Por isso a medição é a média do minuto, não uma foto de um instante.

Quem mede: uma "thread" (linha de execução paralela) dentro do serviço coletor, a cada
60 segundos. Thread, e não o laço principal do coletor, porque uma coleta do Gmail pode
levar minutos e o laço ficaria esse tempo sem medir, justo no momento de mais uso.
"""

# threading: a linha de execução paralela que mede a cada minuto.
import threading

# time.monotonic: relógio que só anda para frente (não pula com ajuste de horário).
import time

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Data e hora com fuso, e intervalos.
from datetime import UTC, datetime, timedelta

# Path manipula caminhos de arquivos.
from pathlib import Path

# Fusos horários pelo nome (o gráfico mostra a hora de Brasília).
from zoneinfo import ZoneInfo

# Consultas.
from sqlalchemy import delete, select

# Session e a fábrica de sessões.
from sqlalchemy.orm import Session, sessionmaker

# Tabela das medições.
from app.models import ServerMetric

# Intervalo entre medições, em segundos.
INTERVAL_SECONDS = 60

# Quantos dias guardar (um a mais que o gráfico, para o primeiro dia não sair cortado).
KEEP_DAYS = 8

# Limites usados no veredito e na linha de atenção dos gráficos (explicados em
# docs/aprendizado/13-uso-do-servidor.md).
# Processador (trabalhando + roubado) a partir deste %: minuto "no limite".
CPU_LIMIT = 90
# Roubo médio a partir deste %: a AWS está segurando o processador.
STEAL_AVG_LIMIT = 5
# Roubo num minuto a partir deste %: pelo menos um aperto forte.
STEAL_PEAK_LIMIT = 25
# Memória a partir deste %: apertada.
MEM_WARN = 80
# Memória a partir deste %: no limite.
MEM_LIMIT = 90
# Swap a partir deste %: a máquina já está usando o disco como memória.
SWAP_LIMIT = 20
# Espera de disco média a partir deste %: disco lento.
IOWAIT_AVG_LIMIT = 10
# Espera de disco num minuto a partir deste %.
IOWAIT_PEAK_LIMIT = 40
# Fatia do tempo no limite que já conta como gargalo (5% = 6 min em 2 horas).
BUSY_SHARE_LIMIT = 0.05


# --- Leitura do Linux ----------------------------------------------------------------


# Contadores do processador numa leitura de /proc/stat (em "jiffies", centésimos de s).
@dataclass(frozen=True)
class CpuTimes:
    # Trabalhando: programas (user, nice), sistema (system) e interrupções (irq, softirq).
    busy: int
    # Parado esperando o disco.
    iowait: int
    # Roubado pelo hipervisor (a AWS).
    steal: int
    # Total de todos os estados (inclui o ocioso).
    total: int


# Lê a primeira linha de /proc/stat ("cpu  user nice system idle iowait irq softirq steal").
def read_cpu_times(path: Path) -> CpuTimes:
    # Primeira linha do arquivo: a soma de todos os núcleos.
    first = path.read_text().splitlines()[0]
    # Separa os números; [1:] pula a palavra "cpu".
    values = [int(v) for v in first.split()[1:]]
    # Completa com zeros se o kernel for antigo e trouxer menos colunas.
    values += [0] * (8 - len(values))
    # Dá nome às 8 primeiras colunas (as seguintes, guest, já estão dentro de user).
    user, nice, system, idle, iowait, irq, softirq, steal = values[:8]
    # Junta.
    return CpuTimes(
        # Trabalho útil.
        busy=user + nice + system + irq + softirq,
        # Espera de disco.
        iowait=iowait,
        # Roubado.
        steal=steal,
        # Tudo.
        total=user + nice + system + idle + iowait + irq + softirq + steal,
    )


# Percentuais do intervalo entre duas leituras: (trabalhando, espera de disco, roubado).
def cpu_percent(before: CpuTimes, after: CpuTimes) -> tuple[float, float, float]:
    # Quanto tempo passou no total.
    total = after.total - before.total
    # Sem tempo (leituras iguais ou contador reiniciado): zero, para não dividir por zero.
    if total <= 0:
        return 0.0, 0.0, 0.0
    # Fração de cada estado, em %.
    return (
        # Trabalhando.
        (after.busy - before.busy) / total * 100,
        # Espera de disco.
        (after.iowait - before.iowait) / total * 100,
        # Roubado.
        (after.steal - before.steal) / total * 100,
    )


# Memória e swap em uso, em %, lidos de /proc/meminfo. Swap vazio = sem swap.
def read_memory(path: Path) -> tuple[float, float | None]:
    # Cada linha é "Nome:   valor kB"; monta um dicionário nome → valor.
    info: dict[str, int] = {}
    # Cada linha.
    for line in path.read_text().splitlines():
        # Separa nome e resto.
        name, _, rest = line.partition(":")
        # O primeiro pedaço do resto é o número.
        parts = rest.split()
        # Linha com número: guarda.
        if parts and parts[0].isdigit():
            info[name] = int(parts[0])
    # Memória total.
    total = info.get("MemTotal", 0)
    # MemAvailable = o que os programas ainda podem usar (livre + cache que o Linux solta).
    # Usar "MemFree" daria um número assustador e errado: o Linux enche a memória de cache
    # de propósito, e devolve na hora quando um programa pede.
    available = info.get("MemAvailable", info.get("MemFree", 0))
    # % em uso (0 se o arquivo vier vazio).
    mem = (total - available) / total * 100 if total else 0.0
    # Swap total.
    swap_total = info.get("SwapTotal", 0)
    # % do swap em uso; None quando não há swap.
    swap = (swap_total - info.get("SwapFree", 0)) / swap_total * 100 if swap_total else None
    # Devolve os dois.
    return mem, swap


# Carga média de 1 minuto (primeiro número de /proc/loadavg).
def read_load(path: Path) -> float:
    # "0.42 0.30 0.25 1/123 4567": o primeiro é a média de 1 minuto.
    return float(path.read_text().split()[0])


# --- Medição periódica ---------------------------------------------------------------


# Guarda a leitura anterior do processador e produz uma medição por chamada.
class Sampler:
    """Mede o servidor. A primeira chamada só guarda a leitura inicial."""

    # Recebe a pasta /proc (os testes passam uma pasta com arquivos falsos).
    def __init__(self, proc: Path = Path("/proc")) -> None:
        # Pasta.
        self.proc = proc
        # Leitura anterior do processador; None até a primeira.
        self.previous: CpuTimes | None = None

    # Lê tudo e devolve a linha a gravar, ou None na primeira leitura.
    def measure(self, now: datetime) -> ServerMetric | None:
        # Contadores agora.
        current = read_cpu_times(self.proc / "stat")
        # Leitura anterior.
        before = self.previous
        # Guarda para a próxima.
        self.previous = current
        # Primeira vez: ainda não há intervalo para comparar.
        if before is None:
            return None
        # % do minuto em cada estado.
        cpu, iowait, steal = cpu_percent(before, current)
        # Memória e swap.
        mem, swap = read_memory(self.proc / "meminfo")
        # Linha nova, com 1 casa decimal (precisão mais fina não muda nenhuma decisão).
        return ServerMetric(
            # Momento.
            sampled_at=now,
            # Processador.
            cpu_pct=round(cpu, 1),
            # Disco.
            iowait_pct=round(iowait, 1),
            # Roubado.
            steal_pct=round(steal, 1),
            # Memória.
            mem_pct=round(mem, 1),
            # Swap (ou vazio).
            swap_pct=round(swap, 1) if swap is not None else None,
            # Carga.
            load1=round(read_load(self.proc / "loadavg"), 2),
        )


# Uma volta: mede, grava e apaga o que passou de KEEP_DAYS. Devolve a linha gravada.
def record(sessions: sessionmaker[Session], sampler: Sampler, now: datetime) -> ServerMetric | None:
    # Mede.
    row = sampler.measure(now)
    # Primeira leitura: nada a gravar.
    if row is None:
        return None
    # Sessão própria (a thread não divide sessão com ninguém).
    with sessions() as session:
        # Grava.
        session.add(row)
        # Apaga o antigo; o índice em sampled_at deixa isso instantâneo.
        session.execute(
            delete(ServerMetric).where(ServerMetric.sampled_at < now - timedelta(days=KEEP_DAYS))
        )
        # Confirma.
        session.commit()
    # Devolve.
    return row


# Laço da thread: mede a cada INTERVAL_SECONDS até `stop` ser acionado.
def run_forever(
    sessions: sessionmaker[Session],
    stop: threading.Event,
    sampler: Sampler | None = None,
    interval: float = INTERVAL_SECONDS,
) -> None:
    """Mede para sempre. Um erro numa volta não para as seguintes."""
    # logging: os erros aparecem em `docker compose logs coletor`.
    import logging

    # Medidor (os testes passam um com arquivos falsos).
    sampler = sampler or Sampler()
    # Próximo horário de medir, no relógio que só anda para frente.
    next_at = time.monotonic()
    # Até pedirem para parar.
    while not stop.is_set():
        # Tenta medir e gravar.
        try:
            # Uma volta.
            record(sessions, sampler, datetime.now(UTC))
        # Banco fora do ar, arquivo ilegível: registra e segue.
        except Exception:
            # Rastro no log.
            logging.exception("Medição do servidor falhou")
        # Marca o próximo horário a partir do anterior (não do fim desta volta), para
        # o intervalo não ir escorregando alguns milissegundos a cada minuto.
        next_at += interval
        # Espera até lá; wait devolve antes se alguém acionar `stop`.
        stop.wait(max(next_at - time.monotonic(), 0))


# Liga a thread de medição (chamado pelo agendador do coletor).
def start_thread(sessions: sessionmaker[Session]) -> threading.Thread:
    # daemon=True: a thread não impede o processo de terminar quando o coletor para.
    thread = threading.Thread(
        target=run_forever, args=(sessions, threading.Event()), daemon=True, name="metricas"
    )
    # Começa.
    thread.start()
    # Devolve (os testes conferem que está viva).
    return thread


# --- Dados dos gráficos --------------------------------------------------------------


# Um período do gráfico: rótulo, duração e tamanho de cada barra.
@dataclass(frozen=True)
class Period:
    # Texto do botão.
    label: str
    # O período numa frase ("nas últimas 2 horas").
    phrase: str
    # Duração total.
    span: timedelta
    # Tempo de cada barra.
    step: timedelta


# Os dois períodos pedidos. 2 horas em barras de 2 min (60 barras): fino o bastante para
# ver um pico de coleta. 7 dias em barras de 1 hora (168 barras): mostra o padrão do dia.
PERIODS: dict[str, Period] = {
    # Últimas 2 horas.
    "2h": Period(
        "Últimas 2 horas", "nas últimas 2 horas", timedelta(hours=2), timedelta(minutes=2)
    ),
    # Últimos 7 dias.
    "7d": Period("Últimos 7 dias", "nos últimos 7 dias", timedelta(days=7), timedelta(hours=1)),
}


# Uma barra: o resumo das medições de um pedaço do período.
@dataclass
class Bucket:
    # Começo (hora local).
    start: datetime
    # Fim (hora local).
    end: datetime
    # Quantas medições caíram aqui (0 = buraco: o coletor estava parado).
    samples: int = 0
    # Média do processador trabalhando.
    cpu: float = 0.0
    # Média da espera de disco.
    iowait: float = 0.0
    # Média do tempo roubado.
    steal: float = 0.0
    # Média da memória.
    mem: float = 0.0
    # Pico do processador indisponível (trabalhando + espera + roubado) num minuto.
    cpu_peak: float = 0.0
    # Pico da memória.
    mem_peak: float = 0.0
    # Pico do swap (vazio sem swap).
    swap_peak: float | None = None
    # Pico da carga.
    load_peak: float = 0.0


# Resumo do período inteiro, com o veredito.
@dataclass
class Summary:
    # Medições no período.
    samples: int
    # Medições esperadas (uma por minuto).
    expected: int
    # Processador trabalhando: média e pico.
    cpu_avg: float = 0.0
    cpu_peak: float = 0.0
    # Tempo roubado: média e pico.
    steal_avg: float = 0.0
    steal_peak: float = 0.0
    # Espera de disco: média e pico.
    iowait_avg: float = 0.0
    iowait_peak: float = 0.0
    # Memória: média e pico.
    mem_avg: float = 0.0
    mem_peak: float = 0.0
    # Pico do swap (vazio sem swap).
    swap_peak: float | None = None
    # Pico da carga.
    load_peak: float = 0.0
    # Minutos com processador no limite.
    busy_minutes: int = 0
    # Frases do veredito: (nível "ok"/"aviso"/"erro"/"info", texto).
    verdicts: list[tuple[str, str]] | None = None


# Arredonda um momento para baixo, para o múltiplo de `step` (ex.: 14:37 → 14:36).
def _floor(moment: datetime, step: timedelta) -> datetime:
    # Segundos desde 1970.
    seconds = moment.timestamp()
    # Tamanho do passo em segundos.
    size = step.total_seconds()
    # Corta o resto e volta a ser data, em UTC.
    return datetime.fromtimestamp(seconds - seconds % size, UTC)


# Medições do período, em ordem de tempo.
def load_rows(session: Session, period: Period, now: datetime) -> list[ServerMetric]:
    # Fim: o fim do pedaço atual (inclui o pedaço em andamento).
    end = _floor(now, period.step) + period.step
    # Começo.
    start = end - period.span
    # Consulta.
    return list(
        session.scalars(
            select(ServerMetric)
            .where(ServerMetric.sampled_at >= start, ServerMetric.sampled_at < end)
            .order_by(ServerMetric.sampled_at)
        )
    )


# Agrupa as medições em barras de `period.step`.
def buckets(rows: list[ServerMetric], period: Period, now: datetime, tz: str) -> list[Bucket]:
    # Fuso de exibição.
    zone = ZoneInfo(tz)
    # Fim do pedaço atual.
    end = _floor(now, period.step) + period.step
    # Começo do período.
    start = end - period.span
    # Quantas barras.
    count = int(period.span / period.step)
    # Barras vazias, cada uma com o seu horário local.
    result = [
        Bucket(
            start=(start + i * period.step).astimezone(zone),
            end=(start + (i + 1) * period.step).astimezone(zone),
        )
        for i in range(count)
    ]
    # Medições de cada barra.
    groups: list[list[ServerMetric]] = [[] for _ in range(count)]
    # Cada medição vai para a barra do seu horário.
    for row in rows:
        # Posição: quantos passos depois do começo.
        index = int((row.sampled_at - start) / period.step)
        # Dentro do período: guarda.
        if 0 <= index < count:
            groups[index].append(row)
    # Calcula médias e picos de cada barra.
    for bucket, group in zip(result, groups, strict=True):
        # Barra sem medição: fica vazia (vira buraco no gráfico).
        if not group:
            continue
        # Quantas.
        n = len(group)
        # Contagem.
        bucket.samples = n
        # Média do processador trabalhando.
        bucket.cpu = sum(r.cpu_pct for r in group) / n
        # Média da espera de disco.
        bucket.iowait = sum(r.iowait_pct for r in group) / n
        # Média do roubado.
        bucket.steal = sum(r.steal_pct for r in group) / n
        # Média da memória.
        bucket.mem = sum(r.mem_pct for r in group) / n
        # Pico do processador: o minuto em que menos sobrou (trabalhando + espera + roubado).
        bucket.cpu_peak = max(r.cpu_pct + r.iowait_pct + r.steal_pct for r in group)
        # Pico da memória.
        bucket.mem_peak = max(r.mem_pct for r in group)
        # Swap: só se o servidor tiver.
        swaps = [r.swap_pct for r in group if r.swap_pct is not None]
        # Pico do swap.
        bucket.swap_peak = max(swaps) if swaps else None
        # Pico da carga.
        bucket.load_peak = max(r.load1 for r in group)
    # Devolve.
    return result


# Resume o período e escreve o veredito.
def summarize(rows: list[ServerMetric], period: Period) -> Summary:
    # Uma medição por minuto.
    expected = int(period.span.total_seconds() // INTERVAL_SECONDS)
    # Resumo com os totais.
    summary = Summary(samples=len(rows), expected=expected, verdicts=[])
    # Sem medição: só o aviso.
    if not rows:
        summary.verdicts.append(
            (
                "info",
                "Ainda sem medições neste período. O serviço coletor grava uma por minuto; "
                "se continuar vazio, veja se ele está rodando na página Saúde.",
            )
        )
        return summary
    # Quantas.
    n = len(rows)
    # Média de cada indicador: soma dividida pela quantidade.
    summary.cpu_avg = sum(r.cpu_pct for r in rows) / n
    # Roubado.
    summary.steal_avg = sum(r.steal_pct for r in rows) / n
    # Espera de disco.
    summary.iowait_avg = sum(r.iowait_pct for r in rows) / n
    # Memória.
    summary.mem_avg = sum(r.mem_pct for r in rows) / n
    # Pico de cada indicador: o maior minuto.
    summary.cpu_peak = max(r.cpu_pct for r in rows)
    # Roubado.
    summary.steal_peak = max(r.steal_pct for r in rows)
    # Espera de disco.
    summary.iowait_peak = max(r.iowait_pct for r in rows)
    # Memória.
    summary.mem_peak = max(r.mem_pct for r in rows)
    # Carga.
    summary.load_peak = max(r.load1 for r in rows)
    # Swap, se houver.
    swaps = [r.swap_pct for r in rows if r.swap_pct is not None]
    # Pico do swap.
    summary.swap_peak = max(swaps) if swaps else None
    # Minutos sem processador sobrando (trabalhando + roubado no limite).
    summary.busy_minutes = sum(1 for r in rows if r.cpu_pct + r.steal_pct >= CPU_LIMIT)
    # Atalho para acrescentar frases.
    add = summary.verdicts.append
    # Processador no limite em boa parte do tempo: gargalo.
    if summary.busy_minutes / n > BUSY_SHARE_LIMIT:
        add(
            (
                "erro",
                f"Processador no limite em {summary.busy_minutes} minutos "
                f"({summary.busy_minutes / n:.0%} do tempo medido). Tarefas estão esperando "
                "na fila: é gargalo de processador.",
            )
        )
    # Só alguns minutos no limite: picos pontuais (uma coleta grande, por exemplo).
    elif summary.busy_minutes:
        add(
            (
                "aviso",
                f"Processador no limite em {summary.busy_minutes} minuto(s), de forma pontual. "
                "Aceitável se coincidir com coletas grandes; preocupa se virar rotina.",
            )
        )
    # A AWS segurando o processador.
    if summary.steal_avg >= STEAL_AVG_LIMIT or summary.steal_peak >= STEAL_PEAK_LIMIT:
        add(
            (
                "erro",
                f"Tempo roubado pela AWS: média {summary.steal_avg:.0f}%, pico "
                f"{summary.steal_peak:.0f}%. Sinal de que os créditos de pico do Lightsail "
                "acabaram: o servidor fica lento mesmo com o processador aparentemente livre. "
                "Plano maior resolve.",
            )
        )
    # Memória no limite ou já usando swap.
    if summary.mem_peak >= MEM_LIMIT or (summary.swap_peak or 0) >= SWAP_LIMIT:
        add(
            (
                "erro",
                f"Memória no limite: pico de {summary.mem_peak:.0f}%"
                + (f", swap em {summary.swap_peak:.0f}%" if summary.swap_peak else "")
                + ". Perto de 100%, o Linux encerra um programa (normalmente o banco ou o "
                "coletor). Plano com mais memória resolve.",
            )
        )
    # Memória apertada, mas ainda com folga.
    elif summary.mem_peak >= MEM_WARN:
        add(
            (
                "aviso",
                f"Memória apertada: pico de {summary.mem_peak:.0f}%. Ainda funciona, mas sobra "
                "pouco para crescer.",
            )
        )
    # Disco lento.
    if summary.iowait_avg >= IOWAIT_AVG_LIMIT or summary.iowait_peak >= IOWAIT_PEAK_LIMIT:
        add(
            (
                "aviso",
                f"Espera de disco alta: média {summary.iowait_avg:.0f}%, pico "
                f"{summary.iowait_peak:.0f}%. O processador ficou parado esperando o disco.",
            )
        )
    # Nenhum problema: folga.
    if not summary.verdicts:
        add(
            (
                "ok",
                f"Sobra recurso: nenhum gargalo {period.phrase}. Processador em média "
                f"{summary.cpu_avg:.0f}% (pico {summary.cpu_peak:.0f}%), memória em média "
                f"{summary.mem_avg:.0f}% (pico {summary.mem_peak:.0f}%).",
            )
        )
    # Faltam medições: avisa, porque o veredito vale só para o que foi medido.
    if n < expected * 0.8:
        add(
            (
                "info",
                f"As medições cobrem {n / expected:.0%} do período ({n} de {expected} minutos): "
                "o coletor estava parado ou a medição foi instalada há pouco. O veredito vale "
                "para o que foi medido.",
            )
        )
    # Devolve.
    return summary
