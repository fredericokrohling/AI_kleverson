"""Testes do console: configurações do console, proteção de POST e página Saúde.

Nenhum teste chama serviços externos: os testes de saúde de verdade são trocados por
versões falsas, e a consulta ao GitHub também.
"""

# Data e hora com fuso, e intervalos.
from datetime import UTC, datetime, timedelta

# Biblioteca de testes.
import pytest

# Simula o navegador.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import select

# Módulo de saúde (para trocar partes por falsos).
from app import health

# Seção Processamento.
from app.activity import PAGE_SIZE, activity_page, parse_day

# Configuração.
from app.config import Settings, get_settings

# O app.
from app.main import app

# Tabelas.
from app.models import AppSetting, Attachment, AuditLog, BronzeItem, HealthCheck, SourceKind

# Configurações do console.
from app.runtime_settings import current, get_value, set_value

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient.
SAME = {**OWNER, "Origin": "http://testserver"}
# Momento fixo.
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
# Um cliente para o arquivo inteiro.
client = TestClient(app)


# Sem nada salvo, vale o .env; depois de salvo, vale o banco.
def test_runtime_settings(sessions):
    # Configuração com padrões conhecidos.
    settings = Settings(llm_model="claude-opus-5-5", transcription_language="pt-BR")
    # Uma sessão.
    with sessions() as s:
        # Padrão do .env.
        assert current(s, settings).transcription_language == "pt-BR"
        # Troca para automático.
        set_value(s, settings, "transcription_language", "multi")
        # Grava.
        s.commit()
        # Agora vale o banco.
        assert get_value(s, settings, "transcription_language") == "multi"
        # Valor fora da lista é recusado.
        with pytest.raises(ValueError):
            # Modelo que não está na lista.
            set_value(s, settings, "llm_model", "gpt-qualquer")
        # A troca ficou na auditoria, com o antes e o depois.
        log = s.scalar(select(AuditLog).where(AuditLog.action == "config.alterar"))
        # Detalhes.
        assert log.details == {"de": "pt-BR", "para": "multi"}


# Formulários da página Configurações.
def test_config_forms(sessions):
    # Página abre com as seções.
    page = client.get("/config", headers=OWNER).text
    # Seções.
    for title in ("Contas conectadas", "Claude", "Transcrição", "Pastas do Drive", "Tags"):
        # Cada uma.
        assert title in page
    # Salva modelo e esforço.
    response = client.post(
        "/config/claude",
        data={"modelo": "claude-haiku-5-5", "esforco": "xhigh"},
        headers=SAME,
        follow_redirects=False,
    )
    # Redireciona para a seção.
    assert response.status_code == 303
    # Gravado.
    with sessions() as s:
        # Modelo.
        assert s.get(AppSetting, "llm_model").value == "claude-haiku-5-5"
    # A página mostra a escolha e o aviso.
    page = client.get("/config", headers=OWNER).text
    # Escolha e aviso.
    assert "Haiku 5.5" in page and "Extra alto" in page and "salva" in page
    # Idioma inválido: aviso de erro, nada gravado.
    client.post(
        "/config/transcricao", data={"idioma": "klingon"}, headers=SAME, follow_redirects=False
    )
    # Aviso.
    assert "não permitido" in client.get("/config", headers=OWNER).text
    # Tags pelo formulário: cria e apaga.
    client.post("/config/tags", data={"nome": "Arquitetura"}, headers=SAME)
    # Aparece.
    assert "Arquitetura" in client.get("/config", headers=OWNER).text


# POST vindo de outro site é recusado, mesmo com o cabeçalho do Tailscale.
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        # Sem Origin nem Referer.
        (OWNER, 403),
        # De outro site.
        ({**OWNER, "Origin": "https://malicioso.exemplo"}, 403),
        # Referer de outro site.
        ({**OWNER, "Referer": "https://malicioso.exemplo/pagina"}, 403),
        # Referer do próprio console.
        ({**OWNER, "Referer": "http://testserver/config"}, 303),
        # Origin do próprio console.
        (SAME, 303),
    ],
)
def test_post_requires_same_origin(headers, status):
    # Tenta criar uma tag.
    response = client.post(
        "/config/tags", data={"nome": "Teste"}, headers=headers, follow_redirects=False
    )
    # Confere.
    assert response.status_code == status


# Testes de saúde: cada um isolado, erros viram "problema", histórico antigo é apagado.
def test_run_checks(sessions):
    # Um resultado antigo, de 8 dias atrás.
    with sessions() as s:
        # Linha velha.
        s.add(HealthCheck(checked_at=NOW - timedelta(days=8), service="banco", ok=True))
        # Grava.
        s.commit()

    # Teste que levanta erro.
    def broken(session, settings, ctx):
        raise RuntimeError("fora do ar")

    # Testes falsos: banco de verdade, um quebrado e um "não sei".
    checks = {
        "banco": health.check_database,
        "claude": broken,
        "status:github": lambda *a: health.Result(None, "inacessível"),
    }
    # Roda.
    rows = health.run_checks(sessions, get_settings(), checks=checks, now=NOW)
    # Resultado por serviço.
    by = {r.service: r for r in rows}
    # Banco ok, Claude com o erro, GitHub desconhecido.
    assert by["banco"].ok is True and by["claude"].ok is False and by["status:github"].ok is None
    # O motivo do erro.
    assert "fora do ar" in by["claude"].detail
    # A linha antiga foi apagada.
    with sessions() as s:
        # Só as 3 novas.
        assert len(s.scalars(select(HealthCheck)).all()) == 3
        # Último resultado por serviço, na ordem da página.
        latest = {item["service"]: item for item in health.latest(s)}
        # Banco com histórico de 1.
        assert latest["banco"]["last"].ok is True and latest["banco"]["history"] == [True]


# Página de status pública: incidente grave = problema; leve = funcionando.
@pytest.mark.parametrize(
    ("indicator", "ok"), [("none", True), ("minor", True), ("major", False), ("critical", False)]
)
def test_status_page(monkeypatch, indicator, ok):
    # Resposta falsa no formato do Statuspage.
    monkeypatch.setattr(
        health, "_get_json", lambda url, headers=None: {"status": {"indicator": indicator}}
    )
    # Roda o teste de uma página.
    result = health.check_status_page("https://x/api/v2/status.json")(None, None, {})
    # Confere.
    assert result.ok is ok


# Coletor parado: o teste acusa quando a última coleta é antiga.
def test_collector_check(sessions):
    # Banco vazio.
    with sessions() as s:
        # Nenhuma coleta.
        result = health.check_collector(s, get_settings(), {"now": NOW})
    # Problema.
    assert result.ok is False and "Nenhuma coleta" in result.detail


# Versão instalada lida do .git, inclusive de packed-refs.
def test_installed_commit(tmp_path):
    # Sem .git.
    assert health.installed_commit(str(tmp_path / "nada")) is None
    # HEAD apontando para a main, que está só no packed-refs.
    (tmp_path / "HEAD").write_text("ref: refs/heads/main\n")
    # Arquivo compactado de referências.
    (tmp_path / "packed-refs").write_text("# pack-refs\nabc123 refs/heads/main\n")
    # Lê.
    assert health.installed_commit(str(tmp_path)) == "abc123"
    # Com o arquivo próprio da referência, ele vale.
    (tmp_path / "refs" / "heads").mkdir(parents=True)
    # Commit mais novo.
    (tmp_path / "refs" / "heads" / "main").write_text("def456\n")
    # Lê.
    assert health.installed_commit(str(tmp_path)) == "def456"


# Comparação com o GitHub: quantos commits faltam e qual o último PR.
def test_version_info(tmp_path, monkeypatch):
    # .git falso com o commit instalado.
    (tmp_path / "HEAD").write_text("aaa111\n")
    # Respostas falsas da API do GitHub, por endereço.
    answers = {
        "commits/main": {"sha": "bbb222"},
        "commits/bbb222/pulls": [
            {"number": 7, "title": "Saúde", "html_url": "u7", "merged_at": "x"}
        ],
        "commits/aaa111/pulls": [
            {"number": 5, "title": "Bloco 2", "html_url": "u5", "merged_at": "x"}
        ],
        "compare/aaa111...main": {"ahead_by": 3},
    }

    # Devolve a resposta cujo final do endereço combina.
    def fake_get(url, headers=None):
        # Procura.
        return next(v for k, v in answers.items() if url.endswith(k))

    # Troca a consulta.
    monkeypatch.setattr(health, "_get_json", fake_get)
    # Configuração apontando para o .git falso.
    settings = Settings(git_dir=str(tmp_path))
    # Consulta (forçando, para não usar cache de outro teste).
    info = health.version_info(settings, force=True)
    # Instalado: PR 5; último: PR 7; faltam 3 commits.
    assert (info["installed_pr"]["number"], info["latest_pr"]["number"], info["behind"]) == (
        5,
        7,
        3,
    )


# Página Saúde: tabela, aviso de testes parados, botões e "Testar agora".
def test_health_page(sessions, monkeypatch):
    # GitHub fora do ar (não deve quebrar a página).
    monkeypatch.setattr(health, "_get_json", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    # Testes falsos e rápidos.
    monkeypatch.setattr(health, "CHECKS", {"banco": health.check_database})
    # Sem nenhum teste ainda: avisa que os automáticos não rodaram.
    page = client.get("/monitoramento", headers=OWNER).text
    # Aviso, erro do GitHub e botões de terminal.
    assert "Nenhum teste automático" in page and "Não foi possível consultar o GitHub" in page
    # Botões kleverson://.
    assert 'href="kleverson://reiniciar"' in page and 'href="kleverson://atualizar"' in page
    # Testar agora (HTMX): devolve a tabela com o banco OK.
    response = client.post("/monitoramento/testar", headers=SAME)
    # Tabela nova.
    assert response.status_code == 200 and "Tudo funcionando" in response.text
    # Sem origem: recusado.
    assert client.post("/monitoramento/testar", headers=OWNER).status_code == 403


# O idioma escolhido no console chega ao serviço de transcrição.
def test_transcription_uses_console_language(sessions, monkeypatch):
    # Rotas da transcrição.
    from app.transcription import routes

    # Grava "multi" como o console faria.
    with sessions() as s:
        # Automático.
        set_value(s, get_settings(), "transcription_language", "multi")
        # Grava.
        s.commit()
    # Idiomas recebidos pela fábrica.
    seen = []

    # Fábrica falsa que só registra o idioma e falha (o teste não precisa do áudio).
    def factory(settings):
        # Registra.
        seen.append(settings.transcription_language)
        # Falha de propósito.
        raise RuntimeError("parou aqui")

    # Troca a fábrica.
    monkeypatch.setattr(routes, "create_provider", factory)
    # Conecta como a extensão faria.
    with client.websocket_connect(
        "/ws/transcricao?sessao=abc12345&canal=outros", headers=OWNER
    ) as ws:
        # Recebe o aviso de erro.
        assert ws.receive_json()["tipo"] == "erro"
    # O idioma do console chegou.
    assert seen == ["multi"]


# --- Coração do menu ------------------------------------------------------------------


# Vermelho quando o último teste de algum serviço falhou ou os testes pararam.
def test_heart_turns_red(sessions):
    # Configuração com intervalo de 10 min.
    settings = Settings(health_poll_minutes=10)
    # Sem testes ainda: sem alarme.
    with sessions() as s:
        # Instalação nova.
        assert not health.has_problem(s, settings, NOW)
        # Banco falhou antes e voltou; Claude ok; Deepgram "não sei".
        s.add_all(
            [
                HealthCheck(checked_at=NOW - timedelta(minutes=10), service="banco", ok=False),
                HealthCheck(checked_at=NOW, service="banco", ok=True),
                HealthCheck(checked_at=NOW, service="claude", ok=True),
                HealthCheck(checked_at=NOW, service="deepgram", ok=None),
            ]
        )
        # Grava.
        s.commit()
        # Tudo normal agora: a falha antiga não conta.
        assert not health.has_problem(s, settings, NOW)
        # O Claude falha no teste seguinte.
        s.add(HealthCheck(checked_at=NOW + timedelta(minutes=1), service="claude", ok=False))
        # Grava.
        s.commit()
        # Problema.
        assert health.has_problem(s, settings, NOW + timedelta(minutes=1))
        # Volta.
        s.add(HealthCheck(checked_at=NOW + timedelta(minutes=2), service="claude", ok=True))
        # Grava.
        s.commit()
        # Normal de novo.
        assert not health.has_problem(s, settings, NOW + timedelta(minutes=2))
        # Testes parados há mais de 3 intervalos (coletor fora do ar): problema.
        assert health.has_problem(s, settings, NOW + timedelta(minutes=40))


# A página mostra o coração com a classe "alerta" só quando há problema.
def test_heart_in_menu(sessions):
    # Um teste recente com falha.
    with sessions() as s:
        # Agora mesmo.
        s.add(HealthCheck(checked_at=datetime.now(UTC), service="gmail", ok=False))
        # Grava.
        s.commit()
    # Qualquer página tem o menu.
    page = client.get("/config", headers=OWNER).text
    # Coração vermelho, com o título explicando.
    assert "alerta" in page and "há serviço com problema" in page
    # O Gmail volta.
    with sessions() as s:
        # Novo resultado.
        s.add(
            HealthCheck(
                checked_at=datetime.now(UTC) + timedelta(seconds=1), service="gmail", ok=True
            )
        )
        # Grava.
        s.commit()
    # Coração normal.
    assert "há serviço com problema" not in client.get("/config", headers=OWNER).text


# --- Processamento --------------------------------------------------------------------


# Grava n eventos a partir de um instante, um por minuto.
def _events(sessions, start: datetime, n: int, prefix: str) -> None:
    # Uma sessão.
    with sessions() as s:
        # Cada evento.
        for i in range(n):
            # Ação numerada.
            s.add(
                AuditLog(
                    actor="sistema",
                    action=f"{prefix}.{i:02d}",
                    created_at=start + timedelta(minutes=i),
                )
            )
        # Grava.
        s.commit()


# Hoje: última página (mais recentes). Outro dia: primeira página. Dia pelo fuso de Brasília.
def test_activity_page(sessions):
    # "Agora": 31/10 às 01:00 UTC = 30/10 às 22:00 em Brasília.
    now = datetime(2026, 10, 31, 1, 0, tzinfo=UTC)
    # 35 eventos "hoje" (30/10 em Brasília), começando às 20:00 de Brasília (23:00 UTC).
    _events(sessions, datetime(2026, 10, 30, 23, 0, tzinfo=UTC), 35, "hoje")
    # 3 eventos no dia 29/10 em Brasília.
    _events(sessions, datetime(2026, 10, 29, 12, 0, tzinfo=UTC), 3, "ontem")
    # Consulta.
    with sessions() as s:
        # Padrão: hoje, na última página.
        p = activity_page(s, "America/Sao_Paulo", now=now)
        # 30/10 (em UTC já seria 31/10), 35 eventos, página 2 de 2, rolagem no fim.
        assert (p.day.isoformat(), p.total, p.page, p.pages) == ("2026-10-30", 35, 2, 2)
        # Os 5 últimos, em ordem cronológica, e a rolagem abre no fim.
        assert [r.action for r in p.rows] == [
            f"hoje.{i}" for i in range(30, 35)
        ] and p.scroll_to_end
        # Página 1: os 30 primeiros do dia, o primeiro em cima.
        first = activity_page(s, "America/Sao_Paulo", page=1, now=now)
        # Tamanho e ordem.
        assert len(first.rows) == PAGE_SIZE and first.rows[0].action == "hoje.00"
        # A página 1 de hoje não rola para o fim (ela não tem os mais recentes).
        assert not first.scroll_to_end
        # Outro dia: primeira página, primeiro evento em cima, sem rolar para o fim.
        other = activity_page(s, "America/Sao_Paulo", parse_day("2026-10-29"), now=now)
        # Três eventos, do primeiro ao último.
        assert [r.action for r in other.rows] == ["ontem.00", "ontem.01", "ontem.02"]
        # Abre no começo.
        assert not other.scroll_to_end and not other.is_today
        # Data inválida ou no futuro: volta para hoje.
        assert activity_page(s, "America/Sao_Paulo", parse_day("abc"), now=now).is_today
        # Futuro.
        assert activity_page(s, "America/Sao_Paulo", parse_day("2030-01-01"), now=now).is_today
        # Página fora do limite: fica na última.
        assert activity_page(s, "America/Sao_Paulo", page=99, now=now).page == 2


# A seção aparece na página inicial e sozinha, pelo HTMX.
def test_activity_section(sessions):
    # Um evento agora, com detalhes.
    with sessions() as s:
        # Como a renomeação de tag registra.
        s.add(
            AuditLog(
                actor="dona", action="tag.renomear", target="1", details={"de": "A", "para": "B"}
            )
        )
        # Grava.
        s.commit()
    # Página inicial.
    page = client.get("/", headers=OWNER).text
    # Título novo, evento, detalhes e rolagem no fim.
    assert "Processamento" in page and "tag.renomear" in page and "de=A, para=B" in page
    # Rolagem no fim (hoje, última página).
    assert "data-rolar-fim" in page
    # Só a seção, num dia sem eventos.
    part = client.get("/processamento?data=2026-01-02", headers=OWNER).text
    # Sem o resto da página, com aviso e link de volta para hoje.
    assert (
        "<html" not in part and "Nenhum evento em 02/01/2026" in part and "Voltar para hoje" in part
    )


# --- Conteúdo coletado ----------------------------------------------------------------


# Lista com busca e página de um e-mail com o texto do anexo.
def test_content_pages(sessions):
    # Um e-mail com anexo lido e outro sem relação.
    with sessions() as s:
        # E-mail.
        email = BronzeItem(
            source=SourceKind.GMAIL,
            external_id="m1",
            occurred_at=NOW,
            author="Ana <ana@cliente.com>",
            subject="Cronograma Atlas",
            body_text="Segue o cronograma.",
            content_sha256="a" * 64,
        )
        # Outro.
        other = BronzeItem(
            source=SourceKind.GMAIL,
            external_id="m2",
            occurred_at=NOW,
            subject="Almoço",
            body_text="Vamos?",
            content_sha256="b" * 64,
        )
        # Grava os dois.
        s.add_all([email, other])
        # Gera os ids.
        s.flush()
        # Anexo com texto.
        s.add(
            Attachment(
                bronze_item_id=email.id,
                filename="plano.xlsx",
                extracted_text="### Planilha: Cronograma\nHomologação | 2026-11-20",
            )
        )
        # Grava.
        s.commit()
        # Id do e-mail.
        email_id = email.id
    # Busca por palavra do texto.
    page = client.get("/conteudo?q=cronograma", headers=OWNER).text
    # Acha o e-mail, não o outro.
    assert "Cronograma Atlas" in page and "Almoço" not in page
    # Hora no fuso de Brasília (12:00 UTC = 09:00).
    assert "30/10/2026 09:00" in page
    # Página do e-mail.
    page = client.get(f"/conteudo/{email_id}", headers=OWNER).text
    # Corpo e texto do anexo.
    assert "Segue o cronograma." in page and "Homologação | 2026-11-20" in page
    # Inexistente: 404.
    assert client.get("/conteudo/999999", headers=OWNER).status_code == 404
