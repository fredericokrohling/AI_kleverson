"""Testes do bloco Servidor de Configurações: medição, veredito, gráficos e disco.

A medição lê arquivos do Linux (/proc/stat, /proc/meminfo, /proc/loadavg). Os testes
escrevem versões falsas desses arquivos numa pasta temporária, com números escolhidos,
para conferir as contas sem depender da máquina onde o teste roda.
"""

# threading.Event: o sinal de parar a thread de medição.
import threading

# Data e hora com fuso, e intervalos.
from datetime import UTC, datetime, timedelta

# Path manipula caminhos de arquivos.
from pathlib import Path

# Simula o navegador.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import func, select

# Módulos testados.
from app import disk_usage, server_metrics

# Geometria dos gráficos.
from app.charts import bar_chart, donut_arcs

# Configuração.
from app.config import Settings

# O app.
from app.main import app

# Tabelas.
from app.models import Attachment, BronzeItem, ServerMetric, SourceKind, TaggedItem

# Cadastro de projetos (cria a tag junto).
from app.projects import create_project

# Empresa que todo teste tem.
from tests.fakes import EMPRESA

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Momento fixo: 03/11/2026 15:00 UTC (12:00 em Brasília).
NOW = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
# Um cliente para o arquivo inteiro.
client = TestClient(app)


# Escreve os três arquivos falsos do /proc numa pasta.
def _proc(folder: Path, cpu: str, mem_available_kb: int = 1_000_000, swap: str = "") -> Path:
    # /proc/stat: só a primeira linha importa.
    (folder / "stat").write_text(f"cpu  {cpu}\ncpu0 1 2 3 4\n")
    # /proc/meminfo: 4 GB de memória, com o disponível escolhido.
    (folder / "meminfo").write_text(
        f"MemTotal:        4000000 kB\nMemFree:          100000 kB\n"
        f"MemAvailable:    {mem_available_kb} kB\n{swap}"
    )
    # /proc/loadavg.
    (folder / "loadavg").write_text("1.50 0.80 0.40 2/300 12345\n")
    # Devolve a pasta.
    return folder


# Uma medição pronta para gravar, com os valores pedidos.
def _metric(minutes_ago: int, cpu=10.0, iowait=0.0, steal=0.0, mem=50.0, swap=None) -> ServerMetric:
    # Linha.
    return ServerMetric(
        # Momento.
        sampled_at=NOW - timedelta(minutes=minutes_ago),
        # Processador trabalhando.
        cpu_pct=cpu,
        # Espera de disco.
        iowait_pct=iowait,
        # Roubado.
        steal_pct=steal,
        # Memória.
        mem_pct=mem,
        # Swap.
        swap_pct=swap,
        # Carga.
        load1=0.5,
    )


# --- Leitura do Linux ----------------------------------------------------------------


# O % do processador vem da diferença entre duas leituras.
def test_cpu_percent_from_two_readings(tmp_path):
    # Primeira leitura: user nice system idle iowait irq softirq steal.
    _proc(tmp_path, "100 0 100 700 50 0 0 50")
    # Medidor apontando para a pasta falsa.
    sampler = server_metrics.Sampler(tmp_path)
    # A primeira medição só guarda a leitura inicial.
    assert sampler.measure(NOW) is None
    # Segunda leitura: +1000 no total (300 trabalhando, 500 ocioso, 100 disco, 100 roubado).
    _proc(tmp_path, "300 0 200 1200 150 0 0 150", mem_available_kb=1_000_000)
    # Mede.
    row = sampler.measure(NOW)
    # 300 de 1000 trabalhando.
    assert row.cpu_pct == 30.0
    # 100 de 1000 esperando o disco.
    assert row.iowait_pct == 10.0
    # 100 de 1000 roubados.
    assert row.steal_pct == 10.0
    # Memória: 3 de 4 GB em uso (o disponível conta como livre, não o MemFree).
    assert row.mem_pct == 75.0
    # Sem swap no arquivo: vazio.
    assert row.swap_pct is None
    # Carga do primeiro número de loadavg.
    assert row.load1 == 1.5


# Swap e contador sem avanço.
def test_memory_swap_and_zero_interval(tmp_path):
    # Arquivos com swap de 1 GB, metade em uso.
    _proc(tmp_path, "1 0 1 1 0 0 0 0", swap="SwapTotal: 1000000 kB\nSwapFree: 500000 kB\n")
    # Lê a memória.
    mem, swap = server_metrics.read_memory(tmp_path / "meminfo")
    # Swap pela metade.
    assert swap == 50.0
    # Mesma leitura duas vezes: nenhum tempo passou, nada de divisão por zero.
    times = server_metrics.read_cpu_times(tmp_path / "stat")
    # Tudo zero.
    assert server_metrics.cpu_percent(times, times) == (0.0, 0.0, 0.0)


# Gravar uma medição apaga as de mais de 8 dias.
def test_record_saves_and_purges(tmp_path, sessions):
    # Uma medição antiga já gravada.
    with sessions() as s:
        # 9 dias atrás.
        s.add(_metric(minutes_ago=9 * 24 * 60))
        # Confirma.
        s.commit()
    # Arquivos falsos.
    _proc(tmp_path, "100 0 100 800 0 0 0 0")
    # Medidor com a leitura inicial já feita.
    sampler = server_metrics.Sampler(tmp_path)
    # Leitura inicial.
    sampler.measure(NOW)
    # Avança os contadores.
    _proc(tmp_path, "200 0 100 900 0 0 0 0")
    # Grava.
    server_metrics.record(sessions, sampler, NOW)
    # Confere.
    with sessions() as s:
        # Sobrou só a nova.
        assert s.scalar(select(func.count()).select_from(ServerMetric)) == 1


# A thread de medição para quando o sinal é acionado.
def test_run_forever_stops(tmp_path, sessions):
    # Arquivos falsos.
    _proc(tmp_path, "100 0 100 800 0 0 0 0")
    # Sinal de parar.
    stop = threading.Event()
    # Thread com intervalo curtinho.
    thread = threading.Thread(
        target=server_metrics.run_forever,
        args=(sessions, stop, server_metrics.Sampler(tmp_path), 0.01),
    )
    # Liga.
    thread.start()
    # Pede para parar.
    stop.set()
    # Espera terminar (no máximo 2 s).
    thread.join(2)
    # Parou.
    assert not thread.is_alive()


# --- Barras e veredito ---------------------------------------------------------------


# Medições viram barras de 2 minutos; buraco fica vazio.
def test_buckets_two_hours():
    # Período de 2 horas.
    period = server_metrics.PERIODS["2h"]
    # Duas medições no mesmo pedaço de 2 minutos, há 1 e 2 minutos (14:58 e 14:59 UTC).
    rows = [_metric(2, cpu=20), _metric(1, cpu=40, iowait=10)]
    # Agrupa no fuso de Brasília.
    result = server_metrics.buckets(rows, period, NOW, "America/Sao_Paulo")
    # 60 barras.
    assert len(result) == 60
    # A barra das 11:58 (hora de Brasília) tem as duas medições.
    bucket = next(b for b in result if b.start.strftime("%H:%M") == "11:58")
    # Duas.
    assert bucket.samples == 2
    # Média do processador.
    assert bucket.cpu == 30
    # Pico: o minuto com 40 trabalhando + 10 de disco.
    assert bucket.cpu_peak == 50
    # As outras estão vazias.
    assert sum(1 for b in result if b.samples) == 1


# Folga: veredito "ok".
def test_summary_ok():
    # 120 minutos tranquilos.
    rows = [_metric(i, cpu=15, mem=40) for i in range(120)]
    # Resume.
    summary = server_metrics.summarize(rows, server_metrics.PERIODS["2h"])
    # Só a frase de folga.
    assert [level for level, _ in summary.verdicts] == ["ok"]


# Gargalo de processador, roubo da AWS e memória no limite.
def test_summary_bottlenecks():
    # 120 minutos: 20 no limite, roubo alto e memória a 95%.
    rows = [_metric(i, cpu=95 if i < 20 else 30, steal=8, mem=95) for i in range(120)]
    # Resume.
    summary = server_metrics.summarize(rows, server_metrics.PERIODS["2h"])
    # Textos juntos.
    texts = " ".join(text for _, text in summary.verdicts)
    # Processador no limite em 20 minutos.
    assert "Processador no limite em 20 minutos" in texts
    # Roubo.
    assert "créditos de pico" in texts
    # Memória.
    assert "Memória no limite" in texts
    # Nenhuma frase de folga.
    assert "ok" not in [level for level, _ in summary.verdicts]


# Poucas medições: avisa a cobertura; nenhuma: avisa que está vazio.
def test_summary_coverage_and_empty():
    # Só 10 minutos medidos em 2 horas.
    summary = server_metrics.summarize(
        [_metric(i) for i in range(10)], server_metrics.PERIODS["2h"]
    )
    # Aviso de cobertura.
    assert any("cobrem" in text for _, text in summary.verdicts)
    # Sem medição.
    empty = server_metrics.summarize([], server_metrics.PERIODS["7d"])
    # Uma frase só, informativa.
    assert [level for level, _ in empty.verdicts] == ["info"]


# Geometria: barra vazia, empilhamento e marca do pico.
def test_bar_chart_geometry():
    # Barras de 2 horas.
    period = server_metrics.PERIODS["2h"]
    # Uma medição com 40 + 10 + 0; outra com pico maior no mesmo pedaço.
    rows = [_metric(2, cpu=20), _metric(1, cpu=60, iowait=20)]
    # Agrupa.
    groups = server_metrics.buckets(rows, period, NOW, "UTC")
    # Monta.
    chart = bar_chart(groups, ["cpu", "iowait", "steal"], "cpu_peak", lambda b: "x", False, 90)
    # A barra com dados.
    bar = next(b for b in chart.bars if not b.empty)
    # Dois pedaços visíveis (o roubado é zero e não desenha).
    assert [s.series for s in bar.segments] == ["cpu", "iowait"]
    # Altura do azul: 40% da área.
    assert abs(bar.segments[0].height - chart.plot_height * 0.4) < 0.1
    # Pico (80%) acima do topo da barra (50%): tem marca.
    assert bar.peak_y is not None
    # As outras são buracos com dica própria.
    assert "sem medição" in chart.bars[0].tip
    # Rótulos a cada meia hora.
    assert all(label.endswith((":00", ":30")) for _, label in chart.ticks)


# Arcos da rosca: começam onde o anterior terminou.
def test_donut_arcs():
    # Duas fatias de 25% e 75%.
    arcs = donut_arcs([25, 75], radius=70, spacing=0)
    # A segunda começa a 1/4 do círculo.
    assert abs(-arcs[1].offset - arcs[0].dash) < 0.01
    # Fatia de 0%: não desenha.
    assert donut_arcs([0], radius=70)[0].dash == 0


# --- Disco ---------------------------------------------------------------------------


# Arquivos somados por projeto; arquivo de dois projetos é dividido; o resto é sistema.
def test_disk_breakdown(tmp_path, sessions):
    # Pasta de arquivos falsa.
    originals = tmp_path / "originais" / "ab" / "cd"
    # Cria as pastas.
    originals.mkdir(parents=True)
    # Três arquivos: um e-mail, um anexo e um arquivo sem item (solto).
    for name, size in (("email.eml", 40_000), ("anexo.pdf", 80_000), ("solto.bin", 8_000)):
        # Escreve o tamanho pedido.
        (originals / name).write_bytes(b"x" * size)
    # Tamanhos como o disco conta (blocos).
    sizes = disk_usage.file_sizes(tmp_path)
    # Grava os dados.
    with sessions() as s:
        # Dois projetos.
        alfa = create_project(s, EMPRESA, "Alfa")
        beta = create_project(s, EMPRESA, "Beta")
        # E-mail com o .eml e o anexo.
        mail = BronzeItem(
            # Empresa.
            company_id=EMPRESA,
            # Fonte.
            source=SourceKind.GMAIL,
            # Id no Gmail.
            external_id="m1",
            # Impressão digital qualquer.
            content_sha256="0" * 64,
            # Caminho do .eml, como o coletor grava.
            extra={"eml": "ab/cd/email.eml"},
        )
        # Grava e gera o id.
        s.add(mail)
        s.flush()
        # O anexo do e-mail.
        s.add(Attachment(bronze_item_id=mail.id, filename="a.pdf", object_key="ab/cd/anexo.pdf"))
        # O e-mail é dos dois projetos.
        s.add(TaggedItem(bronze_item_id=mail.id, tag_id=alfa.tag_id, rule="manual"))
        s.add(TaggedItem(bronze_item_id=mail.id, tag_id=beta.tag_id, rule="manual"))
        # Confirma.
        s.commit()
        # Disco falso de 10 GB com 6 GB livres.
        result = disk_usage.breakdown(
            s, Settings(data_dir=str(tmp_path)), disk=(10 * 1024**3, 6 * 1024**3), now=NOW
        )
    # Fatias por nome.
    by_label = {item.label: item for item in result.slices}
    # Cada projeto fica com metade do e-mail e do anexo.
    half = (sizes["originais/ab/cd/email.eml"] + sizes["originais/ab/cd/anexo.pdf"]) / 2
    # Alfa.
    assert abs(by_label["Alfa"].size - half) <= 1
    # Beta.
    assert abs(by_label["Beta"].size - half) <= 1
    # O arquivo solto fica sem projeto.
    assert by_label["Sem projeto"].size == sizes["originais/ab/cd/solto.bin"]
    # As fatias somam o disco inteiro.
    assert abs(sum(item.size for item in result.slices) - result.total) <= 2
    # 40% ocupado.
    assert round(result.used_percent) == 40
    # Cores diferentes para os dois projetos.
    assert by_label["Alfa"].color != by_label["Beta"].color


# Formato brasileiro dos tamanhos.
def test_human_sizes():
    # Kilobytes inteiros.
    assert disk_usage.human(2048) == "2 KB"
    # Gigabytes com vírgula.
    assert disk_usage.human(1.5 * 1024**3) == "1,5 GB"
    # Porcentagem pequena não vira "0,0%".
    assert disk_usage.percent_text(0.01) == "<0,1%"
    # Porcentagem comum, com vírgula.
    assert disk_usage.percent_text(24.36) == "24,4%"


# --- Páginas -------------------------------------------------------------------------


# Configurações mostra a rosca e os gráficos; o pedaço do HTMX troca o período.
def test_config_page_shows_server_section(sessions):
    # Algumas medições recentes (relativas a agora de verdade, que é o que a página usa).
    with sessions() as s:
        # Agora.
        now = datetime.now(UTC)
        # Cinco minutos medidos.
        for i in range(5):
            # Uma medição por minuto.
            s.add(
                ServerMetric(
                    # i minutos atrás.
                    sampled_at=now - timedelta(minutes=i),
                    # Valores tranquilos.
                    cpu_pct=20,
                    iowait_pct=1,
                    steal_pct=0,
                    mem_pct=45,
                    load1=0.3,
                )
            )
        # Confirma.
        s.commit()
    # Página inteira.
    page = client.get("/config", headers=OWNER).text
    # Seção do servidor.
    assert 'id="servidor"' in page
    # Rosca e legenda do disco.
    assert "Sistema" in page and "Livre" in page
    # Barras e botões de período.
    assert 'class="barras"' in page and "Últimos 7 dias" in page
    # Medições contadas.
    assert "5 de 120 minutos medidos" in page
    # Pedaço de 7 dias.
    part = client.get("/config/servidor?periodo=7d", headers=OWNER).text
    # Conta 7 dias de minutos e não se atualiza sozinho.
    assert "de 10080 minutos medidos" in part and "every 60s" not in part
    # Período inválido volta para 2 horas.
    assert "every 60s" in client.get("/config/servidor?periodo=xyz", headers=OWNER).text
