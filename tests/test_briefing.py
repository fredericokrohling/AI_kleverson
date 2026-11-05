"""Testes do briefing diário (app/briefing.py e a página Briefing).

O que provam:
- a agenda vem da conta da Empresa, com as lideranças presentes e o que está aberto com
  quem vai estar (sem contar a própria dona);
- sem agenda (conta desconectada), o briefing sai mesmo assim, com o motivo;
- alertas novos aparecem um por fato, marcando os que chegaram depois do último briefing;
- prazos: vencidos, de hoje e próximos; concluídos, suspeitos e distantes ficam de fora;
  "meu" marca os da dona;
- "o que mudou" conta por projeto só o que entrou na janela, com os destaques;
- a janela começa no último briefing aberto; sem nenhum aberto, os dias se somam;
- o coletor só monta depois da hora, uma vez por Empresa por dia;
- a Empresa B não aparece no briefing da Empresa A;
- a página, o "Gerar agora", o quadro do Início, o ponto do menu e o comando funcionam.
"""

# Data e hora (o "agora" fixo dos testes, prazos e a data dos itens).
from datetime import UTC, date, datetime, timedelta

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import select

# Regras de alerta (para criar alertas como o coletor criaria).
from app.alerts import generate as generate_alerts
from app.alerts import save_rule

# O que é testado.
from app.briefing import due_companies, generate, run_due, unread_today, window_start

# Comando de terminal (gerar-briefing).
from app.cli import main as cli_main

# Criação de Empresa.
from app.companies import create_company

# Configuração.
from app.config import get_settings

# O app.
from app.main import app

# Tabelas.
from app.models import (
    Alert,
    AlertRule,
    Briefing,
    BronzeItem,
    ClassificationMethod,
    Fact,
    FactKind,
    ItemClassification,
    SourceKind,
)

# Cadastro de projetos e pessoas.
from app.projects import create_project, save_person, set_member

# Empresa de todo teste e a agenda falsa.
from tests.fakes import EMPRESA, FakeCalendar

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient.
SAME = {**OWNER, "Origin": "http://testserver"}
# Um cliente para o arquivo inteiro.
client = TestClient(app)
# "Agora" fixo: 04/11/2026, 10h em UTC (7h em Brasília, a hora do briefing).
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
# Hoje em Brasília.
TODAY = date(2026, 11, 4)


# Configuração de teste: briefing às 7h, prazos até 7 dias, janela de 30 dias.
def _settings():
    # Copia a configuração de teste.
    return get_settings().model_copy(
        update={"briefing_hour": 7, "briefing_due_days": 7, "alerts_lookback_days": 30}
    )


# Uma reunião da agenda no formato da API do Google, hoje em Brasília.
def _event(
    event_id: str, title: str, start: str, end: str, emails: list[str], day: str = "2026-11-04"
) -> dict:
    # Evento (no dia pedido; o padrão é o dia fixo dos testes que passam a data).
    return {
        "id": event_id,
        "summary": title,
        "start": {"dateTime": f"{day}T{start}:00-03:00"},
        "end": {"dateTime": f"{day}T{end}:00-03:00"},
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "attendees": [{"email": e} for e in emails],
    }


# Agenda do dia: uma reunião com Márcia (liderança) e a dona; outra só com o Beto.
CALENDAR = FakeCalendar(
    [
        _event("ev1", "Status Atlas", "09:00", "10:00", ["marcia@cli.com", "dona@exemplo.com"]),
        _event("ev2", "Alinhamento técnico", "14:00", "14:30", ["beto@cli.com"]),
        # Dia inteiro: fica de fora.
        {"id": "ev3", "summary": "Feriado", "start": {"date": "2026-11-04"}, "end": {}},
    ]
)


# Cadastros: projeto Atlas; Márcia (liderança na empresa), Beto (sem liderança) e a dona.
def _setup(session, company_id: int = EMPRESA) -> dict:
    # Projeto.
    atlas = create_project(session, company_id, "Projeto Atlas", "ATL")
    # Márcia: diretora.
    marcia = save_person(
        session, None, name="Márcia", email="marcia@cli.com", company_id=company_id, is_leader=True
    )
    # Beto: analista.
    beto = save_person(session, None, name="Beto", email="beto@cli.com", company_id=company_id)
    # A dona, com o login do Tailscale.
    dona = save_person(
        session, None, name="Natália", email="dona@exemplo.com", company_id=company_id
    )
    # Equipe.
    set_member(session, atlas.id, marcia.id, "Patrocinadora", False)
    set_member(session, atlas.id, beto.id, "Analista", False)
    # Envia.
    session.flush()
    # Devolve.
    return {"atlas": atlas, "marcia": marcia, "beto": beto, "dona": dona}


# Um e-mail no bronze, gravado no momento dado, já classificado no projeto (ou sem).
def _item(session, external_id: str, project=None, when: datetime = NOW, company_id=EMPRESA):
    # Item.
    item = BronzeItem(
        company_id=company_id,
        source=SourceKind.GMAIL,
        external_id=external_id,
        subject="Atlas",
        body_text="texto",
        occurred_at=when,
        ingested_at=when,
        content_sha256=external_id.ljust(64, "0")[:64],
    )
    # Prepara.
    session.add(item)
    # Gera o id.
    session.flush()
    # Classificação no projeto, quando dado.
    if project is not None:
        session.add(
            ItemClassification(
                bronze_item_id=item.id, project_id=project.id, method=ClassificationMethod.MANUAL
            )
        )
    # Devolve.
    return item


# Um fato já extraído, criado no momento dado.
def _fact(session, item, project, said_by, kind=FactKind.TASK, when=NOW, **extra) -> Fact:
    # Fato.
    fact = Fact(
        project_id=project.id if project else None,
        kind=kind,
        title=extra.pop("title", f"{kind.value} de {said_by.name if said_by else 'ninguém'}"),
        source_item_id=item.id,
        source_quote="trecho do texto original",
        quote_verified=extra.pop("verified", True),
        said_by_id=said_by.id if said_by else None,
        status=extra.pop("status", "aberto"),
        created_at=when,
        **extra,
    )
    # Prepara.
    session.add(fact)
    # Gera o id.
    session.flush()
    # Devolve.
    return fact


# Só a regra "Pedido de liderança" (as padrão da migração saem, para o teste ser exato).
def _only_task_rule(session) -> AlertRule:
    # Apaga as regras existentes.
    for rule in session.scalars(select(AlertRule)):
        session.delete(rule)
    # Envia.
    session.flush()
    # Regra.
    return save_rule(
        session,
        None,
        name="Pedido de liderança",
        company_id=None,
        kinds=["tarefa"],
        leadership="qualquer",
        owner_rule="qualquer",
        due_within_days=None,
        severity="alta",
        active=True,
        actor="teste",
    )


# Agenda: lideranças presentes e o que está aberto com quem vai estar.
def test_agenda_section(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Regra.
        _only_task_rule(session)
        # Um pedido da Márcia (vira alerta aberto).
        _fact(session, _item(session, "e1", p["atlas"]), p["atlas"], p["marcia"])
        # Uma tarefa da dona, pedida pelo Beto (sem liderança: não vira alerta).
        _fact(session, _item(session, "e2", p["atlas"]), p["atlas"], p["beto"])
        # Alertas.
        generate_alerts(session, _settings(), NOW)
        # Monta.
        b = generate(session, _settings(), EMPRESA, now=NOW, calendar_for=lambda cid: CALENDAR)
        # Agenda.
        agenda = b.content["agenda"]
        # Duas reuniões com horário (o feriado de dia inteiro saiu), em ordem.
        assert [r["titulo"] for r in agenda] == ["Status Atlas", "Alinhamento técnico"]
        # Horário no fuso de Brasília.
        assert (agenda[0]["inicio"], agenda[0]["fim"]) == ("09:00", "10:00")
        # Márcia é liderança presente; a dona não conta.
        assert agenda[0]["liderancas"] == ["Márcia"]
        # Um aberto com quem vai estar (o pedido da Márcia).
        assert agenda[0]["alertas_abertos"] == 1
        # Na reunião do Beto, nenhuma liderança e nenhum alerta (o fato dele não é alerta).
        assert agenda[1]["liderancas"] == [] and agenda[1]["alertas_abertos"] == 0
        # Meet.
        assert agenda[0]["plataforma"] == "meet"
        # Sem erro.
        assert b.content["agenda_erro"] is None


# Sem conta Google na Empresa, o briefing sai mesmo assim, com o motivo.
def test_agenda_error_does_not_block(sessions):
    # Agenda que falha como load_credentials sem conta.
    def no_account(cid):
        raise RuntimeError("Nenhuma conta Google conectada para a Empresa Empresa Teste.")

    # Uma sessão.
    with sessions() as session:
        # Monta.
        b = generate(session, _settings(), EMPRESA, now=NOW, calendar_for=no_account)
        # Grava.
        session.commit()
        # Sem reuniões, com o motivo.
        assert b.content["agenda"] == [] and "Nenhuma conta" in b.content["agenda_erro"]
        # As outras seções existem.
        assert set(b.content["prazos"]) == {"vencidos", "hoje", "proximos"}


# Alertas novos: um por fato; "chegou" para os que nasceram depois do último briefing.
def test_alerts_section(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Regra.
        _only_task_rule(session)
        # Pedido antigo (alerta criado há 2 dias).
        old = _fact(session, _item(session, "e1", p["atlas"]), p["atlas"], p["marcia"])
        # Alertas agora; depois o alerta antigo ganha a data de 2 dias atrás.
        generate_alerts(session, _settings(), NOW)
        # Recua o alerta do fato antigo.
        session.scalars(select(Alert)).one().created_at = NOW - timedelta(days=2)
        # Briefing de ontem, aberto (a janela começa nele).
        session.add(
            Briefing(
                company_id=EMPRESA,
                day=TODAY - timedelta(days=1),
                generated_at=NOW - timedelta(days=1),
                since=NOW - timedelta(days=2),
                viewed_at=NOW - timedelta(days=1),
            )
        )
        # Pedido novo.
        new = _fact(session, _item(session, "e2", p["atlas"]), p["atlas"], p["marcia"])
        # Alerta agora.
        generate_alerts(session, _settings(), NOW)
        # Monta.
        b = generate(session, _settings(), EMPRESA, now=NOW, calendar_for=lambda cid: CALENDAR)
        # Alertas por fato.
        by_fact = {a["fato_id"]: a for a in b.content["alertas"]}
        # Os dois aparecem.
        assert set(by_fact) == {old.id, new.id} and b.content["alertas_total"] == 2
        # Só o novo "chegou agora".
        assert by_fact[new.id]["chegou"] and not by_fact[old.id]["chegou"]
        # Contagem dos que chegaram.
        assert b.content["alertas_chegaram"] == 1
        # Quem disse, com a liderança.
        assert by_fact[new.id]["quem"] == "Márcia (liderança na empresa)"


# Prazos: grupos, filtros e "meu".
def test_deadlines_section(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Item.
        item = _item(session, "e1", p["atlas"])
        # Atalho.
        f = lambda title, due, **kw: _fact(  # noqa: E731 (atalho só deste teste)
            session, item, p["atlas"], p["marcia"], title=title, due_date=due, **kw
        )
        # Vencido há 2 dias, da dona.
        late = f("Enviar ata", TODAY - timedelta(days=2), owner_id=p["dona"].id)
        # Vence hoje.
        today = f("Aprovar escopo", TODAY)
        # Vence em 5 dias.
        soon = f("Homologar", TODAY + timedelta(days=5), owner_text="o Beto")
        # Vence em 20 dias: fora.
        f("Go-live", TODAY + timedelta(days=20))
        # Concluído: fora.
        f("Já feito", TODAY, status="concluido")
        # Suspeito: fora.
        f("Inventado", TODAY, verified=False)
        # Vencido há 40 dias (mais que a janela): fora.
        f("Muito antigo", TODAY - timedelta(days=40))
        # Monta.
        b = generate(session, _settings(), EMPRESA, now=NOW, calendar_for=lambda cid: CALENDAR)
        # Prazos.
        pz = b.content["prazos"]
        # Um em cada grupo.
        assert [x["fato_id"] for x in pz["vencidos"]] == [late.id]
        assert [x["fato_id"] for x in pz["hoje"]] == [today.id]
        assert [x["fato_id"] for x in pz["proximos"]] == [soon.id]
        # Atrasado há 2 dias, e é meu.
        assert pz["vencidos"][0]["dias"] == -2 and pz["vencidos"][0]["meu"] is True
        # O de hoje não é meu.
        assert pz["hoje"][0]["meu"] is False
        # Responsável como escrito no texto.
        assert pz["proximos"][0]["responsavel"] == "o Beto"


# O que mudou: só a janela, por projeto, com os destaques.
def test_changes_section(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Segundo projeto.
        beta = create_project(session, EMPRESA, "Projeto Beta", "BET")
        # Dois e-mails do Atlas na janela (últimas 24 h, primeiro briefing).
        a1 = _item(session, "e1", p["atlas"], when=NOW - timedelta(hours=3))
        _item(session, "e2", p["atlas"], when=NOW - timedelta(hours=1))
        # Um e-mail ainda sem projeto.
        _item(session, "e3", None, when=NOW - timedelta(hours=2))
        # Um e-mail do Beta antigo (fora da janela).
        b_old = _item(session, "e4", beta, when=NOW - timedelta(days=3))
        # Decisão nova no Atlas (destaque).
        _fact(session, a1, p["atlas"], p["marcia"], FactKind.DECISION, title="Adiar go-live")
        # Tarefa nova no Atlas (conta, mas não é destaque).
        _fact(session, a1, p["atlas"], p["marcia"], FactKind.TASK)
        # Risco suspeito: não conta.
        _fact(session, a1, p["atlas"], p["marcia"], FactKind.RISK, verified=False)
        # Risco antigo no Beta: fora da janela.
        _fact(session, b_old, beta, p["marcia"], FactKind.RISK, when=NOW - timedelta(days=3))
        # Monta.
        b = generate(session, _settings(), EMPRESA, now=NOW, calendar_for=lambda cid: CALENDAR)
        # Mudanças.
        changes = b.content["mudancas"]
        # Atlas e "sem projeto" (Beta não teve nada na janela).
        assert [c["projeto"] for c in changes] == ["Projeto Atlas", None]
        # Atlas: dois e-mails, uma decisão e uma tarefa.
        atlas = changes[0]
        # Itens.
        assert atlas["itens"] == {"e-mail(s)": 2}
        # Fatos, na ordem dos tipos.
        assert atlas["fatos"] == {"Tarefa": 1, "Decisão": 1}
        # Destaque: só a decisão.
        assert [d["titulo"] for d in atlas["destaques"]] == ["Adiar go-live"]
        # Sem projeto: um e-mail.
        assert changes[1]["itens"] == {"e-mail(s)": 1}


# A janela: último briefing aberto; sem nenhum aberto, os dias se somam; com limite.
def test_window_start(sessions):
    # Configuração.
    settings = _settings()
    # Uma sessão.
    with sessions() as session:
        # Sem briefing anterior: 24 horas.
        assert window_start(session, settings, EMPRESA, TODAY, NOW) == NOW - timedelta(hours=24)
        # Briefing de anteontem, não aberto, com janela começando 3 dias atrás.
        session.add(
            Briefing(
                company_id=EMPRESA,
                day=TODAY - timedelta(days=2),
                generated_at=NOW - timedelta(days=2),
                since=NOW - timedelta(days=3),
            )
        )
        # Envia.
        session.flush()
        # Nenhum aberto: a janela do primeiro (os dias se somam).
        assert window_start(session, settings, EMPRESA, TODAY, NOW) == NOW - timedelta(days=3)
        # Briefing de ontem, aberto.
        session.add(
            Briefing(
                company_id=EMPRESA,
                day=TODAY - timedelta(days=1),
                generated_at=NOW - timedelta(days=1),
                since=NOW - timedelta(days=2),
                viewed_at=NOW - timedelta(hours=20),
            )
        )
        # Envia.
        session.flush()
        # Começa quando o de ontem foi montado.
        assert window_start(session, settings, EMPRESA, TODAY, NOW) == NOW - timedelta(days=1)
        # Com janela de 0 dias, o limite manda (nunca antes de agora - janela).
        tight = settings.model_copy(update={"alerts_lookback_days": 0})
        # Limite.
        assert window_start(session, tight, EMPRESA, TODAY, NOW) == NOW


# Coletor: só depois da hora, uma vez por Empresa por dia; "Gerar de novo" substitui.
def test_schedule_and_regenerate(sessions):
    # Configuração.
    settings = _settings()
    # Uma sessão.
    with sessions() as session:
        # Segunda Empresa.
        create_company(session, "Cliente B")
        # Às 6h59 de Brasília: nada.
        early = datetime(2026, 11, 4, 9, 59, tzinfo=UTC)
        # Ainda não deu a hora.
        assert due_companies(session, settings, early) == []
        # Rodada às 7h: monta as duas.
        summary = run_due(session, settings, NOW, calendar_for=lambda cid: CALENDAR)
        # As duas, em ordem alfabética.
        assert summary.generated == ["Cliente B", "Empresa Teste"]
        # Grava.
        session.commit()
        # Outra rodada no mesmo dia: nada.
        later = NOW + timedelta(hours=3)
        # Nenhuma.
        assert run_due(session, settings, later, calendar_for=lambda cid: CALENDAR).generated == []
        # Ainda não lido: o menu mostra o ponto.
        assert unread_today(session, settings, NOW)
        # Gerar de novo: substitui o do dia (continua um só).
        b = generate(
            session,
            settings,
            EMPRESA,
            now=later,
            calendar_for=lambda cid: CALENDAR,
            trigger="manual",
        )
        # Grava.
        session.commit()
        # Um briefing por Empresa no dia.
        assert session.scalar(select(Briefing).where(Briefing.company_id == EMPRESA)).id == b.id
        # Com o momento novo e quem pediu.
        assert b.generated_at == later and b.trigger == "manual"
        # No dia seguinte, às 7h, monta de novo.
        assert len(due_companies(session, settings, NOW + timedelta(days=1))) == 2


# A Empresa B não aparece no briefing da Empresa A.
def test_company_isolation(sessions):
    # Uma sessão.
    with sessions() as session:
        # Empresa B com os mesmos cadastros.
        other = create_company(session, "Cliente B")
        # Cadastros em A e em B.
        pa, pb = _setup(session), _setup(session, other.id)
        # Regra.
        _only_task_rule(session)
        # Pedido e prazo em B.
        item_b = _item(session, "b1", pb["atlas"], company_id=other.id)
        _fact(session, item_b, pb["atlas"], pb["marcia"], due_date=TODAY)
        # Alertas.
        generate_alerts(session, _settings(), NOW)
        # Briefing de A.
        b = generate(session, _settings(), EMPRESA, now=NOW, calendar_for=lambda cid: CALENDAR)
        # Nada de B.
        assert b.content["alertas"] == [] and b.content["prazos"]["hoje"] == []
        # Nem nas mudanças.
        assert b.content["mudancas"] == []
        # Mesmo a reunião com a Márcia de B: em A, quem conta é a Márcia de A, sem alerta.
        assert b.content["agenda"][0]["alertas_abertos"] == 0
        # E o projeto de A existe (só para usar pa).
        assert pa["atlas"].company_id == EMPRESA


# Página, "Gerar agora", leitura, Início, menu e comando.
def test_page_index_menu_and_command(sessions, monkeypatch, capsys):
    # A agenda falsa no lugar da do Google.
    from app import briefing

    # Cadastros e um prazo para hoje (pela data real, porque a página usa o relógio real).
    real_today = briefing.today_local(get_settings())
    # A reunião também precisa ser de hoje: com o dia fixo, o teste quebrava no dia seguinte
    # ao que foi escrito.
    today_calendar = FakeCalendar(
        [
            _event(
                "ev1",
                "Status Atlas",
                "09:00",
                "10:00",
                ["marcia@cli.com", "dona@exemplo.com"],
                day=real_today.isoformat(),
            )
        ]
    )
    # Troca.
    monkeypatch.setattr(briefing, "calendar_client", lambda db, settings, cid: today_calendar)
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Prazo.
        _fact(
            session,
            _item(session, "e1", p["atlas"]),
            p["atlas"],
            p["marcia"],
            title="Aprovar escopo",
            due_date=real_today,
        )
        # Grava.
        session.commit()
    # Antes de montar: aviso e botão.
    page = client.get("/briefing", headers=OWNER)
    # Abre com o aviso.
    assert page.status_code == 200 and "Gerar agora" in page.text
    # No Início também.
    assert "O briefing de hoje sai a partir das" in client.get("/", headers=OWNER).text
    # POST de outro site: recusado.
    assert (
        client.post("/briefing/gerar", headers={**OWNER, "Origin": "http://mal"}).status_code == 403
    )
    # Gerar agora.
    done = client.post("/briefing/gerar", headers=SAME, follow_redirects=False)
    # Volta para a página.
    assert done.status_code == 303 and done.headers["location"] == "/briefing"
    # O Início mostra o quadro e o menu, o ponto (ainda não aberto).
    home = client.get("/", headers=OWNER).text
    # Quadro.
    assert "1 vencem em breve" in home and "ainda não aberto" in home and 'class="ponto"' in home
    # A página mostra a agenda, o prazo e marca como lido.
    page = client.get("/briefing", headers=OWNER).text
    # Agenda e prazo.
    assert "Status Atlas" in page and "Aprovar escopo" in page and "lideranças: Márcia" in page
    # Lido.
    with sessions() as session:
        # Um briefing, lido.
        assert session.scalars(select(Briefing)).one().viewed_at is not None
    # O ponto sumiu.
    assert 'class="ponto"' not in client.get("/", headers=OWNER).text
    # Dia inválido vira hoje; dia futuro também.
    assert client.get("/briefing?data=xx", headers=OWNER).status_code == 200
    assert client.get("/briefing?data=2999-01-01", headers=OWNER).status_code == 200
    # Comando sem --refazer: nada (já tem o de hoje).
    cli_main(["gerar-briefing"])
    # Saída.
    assert "nenhum novo" in capsys.readouterr().out
    # Com --refazer: monta de novo.
    cli_main(["gerar-briefing", "--refazer"])
    # Saída.
    assert "Empresa Teste" in capsys.readouterr().out
