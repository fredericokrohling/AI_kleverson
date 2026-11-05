"""Testes de "Pôr na agenda": eventos criados com a marca invisível, e como tirar.

O que estes testes garantem (as quatro regras de app/calendar_events.py):
- o evento é de dia inteiro, livre, privado, sem convidados e criado sem aviso a ninguém;
- só fatos com prazo, conferidos, valendo e abertos vão para a agenda; um fato, um evento;
- o Google fora do ar não deixa registro pela metade;
- apagar confere a marca antes: evento sem a marca nunca é apagado;
- "Conferir" acerta o registro com o que mudou no Google, e "Apagar todos" só tira o
  que tem a marca;
- as páginas mostram o botão e a página Na agenda, e o comando de terminal funciona.
"""

# Data, data e hora e fuso UTC.
from datetime import UTC, date, datetime, timedelta

# Biblioteca de testes.
import pytest

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import select

# O que é testado.
from app import calendar_events
from app.calendar_events import (
    MARK_KEY,
    EventError,
    active_by_fact,
    create_for_fact,
    event_body,
    remove,
    sync_company,
)

# Comando de terminal.
from app.cli import main as cli_main

# Criação de Empresa.
from app.companies import create_company

# Configuração.
from app.config import get_settings

# "Extrair de novo" (apaga os fatos de um item).
from app.facts import redo_item

# O app.
from app.main import app

# Tabelas.
from app.models import (
    AuditLog,
    BronzeItem,
    ClassificationMethod,
    CreatedEvent,
    Fact,
    FactKind,
    ItemClassification,
    SourceKind,
)

# Cadastro de projetos e pessoas.
from app.projects import create_project, save_person

# Empresa de todo teste, a agenda falsa e o gerador de erros do Google.
from tests.fakes import EMPRESA, FakeCalendar, http_error

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient.
SAME = {**OWNER, "Origin": "http://testserver"}
# Um cliente para o arquivo inteiro.
client = TestClient(app)
# Dia do prazo usado nos testes.
DUE = date(2026, 11, 10)


# Um evento seu, que não foi criado pelo assistente (não pode ser tocado).
def _her_event() -> dict:
    # Evento comum, sem a marca.
    return {
        "id": "dela1",
        "summary": "Dentista",
        "start": {"date": "2026-11-10"},
        "end": {"date": "2026-11-11"},
    }


# Um fato com prazo num e-mail classificado no projeto Atlas.
def _fact(session, company_id: int = EMPRESA, **extra) -> Fact:
    # Projeto (o nome muda por Empresa para não esbarrar no "nome único").
    project = create_project(session, company_id, f"Atlas {company_id}", f"AT{company_id}")
    # Responsável.
    owner = save_person(
        session, None, name="Márcia", email=f"marcia{company_id}@cli.com", company_id=company_id
    )
    # E-mail no bronze.
    item = BronzeItem(
        company_id=company_id,
        source=SourceKind.GMAIL,
        external_id=f"e-{company_id}-{extra.get('title', 'x')}",
        subject="Atlas",
        body_text="texto",
        occurred_at=datetime(2026, 11, 3, tzinfo=UTC),
        content_sha256=f"{company_id}{extra.get('title', 'x')}".ljust(64, "0")[:64],
    )
    # Prepara.
    session.add(item)
    # Gera o id.
    session.flush()
    # Classificado no projeto.
    session.add(
        ItemClassification(
            bronze_item_id=item.id, project_id=project.id, method=ClassificationMethod.MANUAL
        )
    )
    # Fato.
    fact = Fact(
        project_id=project.id,
        kind=extra.pop("kind", FactKind.TASK),
        title=extra.pop("title", "Enviar cronograma"),
        source_item_id=item.id,
        source_quote=extra.pop("quote", "enviar o cronograma até segunda"),
        quote_verified=extra.pop("verified", True),
        owner_id=owner.id,
        status=extra.pop("status", "aberto"),
        due_date=extra.pop("due_date", DUE),
        **extra,
    )
    # Prepara.
    session.add(fact)
    # Gera o id.
    session.flush()
    # Devolve.
    return fact


# O corpo do evento segue as regras: dia inteiro, livre, privado, sem convidados, marca.
def test_event_body_rules(sessions):
    # Uma sessão.
    with sessions() as session:
        # Fato com trecho longo.
        fact = _fact(session, kind=FactKind.DEADLINE, quote="x" * 900)
        # Corpo.
        body = event_body(fact, project="Atlas", owner="Márcia", settings=get_settings(), row_id=7)
    # Título com o tipo na frente.
    assert body["summary"] == "Prazo: Enviar cronograma"
    # Dia inteiro: começa no prazo e termina no dia seguinte (fim exclusivo).
    assert body["start"] == {"date": "2026-11-10"} and body["end"] == {"date": "2026-11-11"}
    # Livre e privado.
    assert body["transparency"] == "transparent" and body["visibility"] == "private"
    # Sem convidados nem videoconferência: ninguém recebe nada.
    assert "attendees" not in body and "conferenceData" not in body
    # A marca e os ids.
    private = body["extendedProperties"]["private"]
    # Marca, registro e fato.
    assert private == {MARK_KEY: "1", "kleverson_registro": "7", "kleverson_fato": str(fact.id)}
    # Trecho cortado em 500 caracteres, com reticências.
    assert "x" * 500 + "…" in body["description"] and "x" * 501 not in body["description"]
    # Link para a origem no console.
    assert f"/conteudo/{fact.source_item_id}" in body["description"]


# Cria uma vez; o segundo clique não cria outro.
def test_create_once(sessions):
    # Agenda falsa com um evento seu.
    calendar = FakeCalendar([_her_event()])
    # Uma sessão.
    with sessions() as session:
        # Fato.
        fact = _fact(session)
        # Cria.
        row = create_for_fact(
            session, get_settings(), fact.id, "dona", calendar_for=lambda c: calendar
        )
        # Grava.
        session.commit()
        # O registro tem o id do Google, o dia e quem pediu.
        assert row.google_event_id == "criado1" and row.day == DUE and row.created_by == "dona"
        # Um pedido ao Google, sem aviso a ninguém.
        assert len(calendar.inserted) == 1 and calendar.inserted[0][1] == "none"
        # O id do registro foi na marca.
        sent = calendar.inserted[0][0]["extendedProperties"]["private"]
        # Confere.
        assert sent["kleverson_registro"] == str(row.id)
        # O botão vira "na agenda".
        assert active_by_fact(session, {fact.id}) == {fact.id: row}
        # Diário.
        assert (
            session.scalars(select(AuditLog).where(AuditLog.action == "agenda.evento_criado"))
            .one()
            .company_id
            == EMPRESA
        )
        # Segundo clique: recusado, sem ir ao Google.
        with pytest.raises(EventError, match="já está na agenda"):
            create_for_fact(
                session, get_settings(), fact.id, "dona", calendar_for=lambda c: calendar
            )
        # Continua um pedido só.
        assert len(calendar.inserted) == 1


# O índice único barra dois eventos ativos para o mesmo fato (dois cliques ao mesmo tempo).
def test_unique_index(sessions):
    # Uma sessão.
    with sessions() as session:
        # Fato.
        fact = _fact(session)
        # Dois registros ativos do mesmo fato, direto no banco.
        for _ in range(2):
            session.add(
                CreatedEvent(
                    company_id=EMPRESA, fact_id=fact.id, title="x", day=DUE, created_by="t"
                )
            )
        # O banco recusa.
        with pytest.raises(Exception, match="uq_created_events_fact_ativo"):
            session.flush()


# Fatos que não podem ir para a agenda, e o Google fora do ar: nada fica gravado.
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"verified": False}, "suspeito"),
        ({"due_date": None}, "não tem prazo"),
        ({"status": "concluido"}, "concluído"),
    ],
)
def test_not_eligible(sessions, extra, message):
    # Agenda falsa.
    calendar = FakeCalendar([])
    # Uma sessão.
    with sessions() as session:
        # Fato fora da régua.
        fact = _fact(session, **extra)
        # Recusado com o motivo.
        with pytest.raises(EventError, match=message):
            create_for_fact(
                session, get_settings(), fact.id, "dona", calendar_for=lambda c: calendar
            )
    # Nada foi ao Google.
    assert calendar.inserted == []


# Fato inexistente.
def test_missing_fact(sessions):
    # Uma sessão.
    with sessions() as session:
        # Recusado.
        with pytest.raises(EventError, match="não encontrado"):
            create_for_fact(session, get_settings(), 999, "dona", calendar_for=lambda c: None)


# Google fora do ar ou conta sem permissão: aviso claro e nenhum registro.
def test_google_failure_leaves_nothing(sessions):
    # Agenda que recusa a criação.
    calendar = FakeCalendar([])
    # 403, como quando a permissão de agenda foi tirada.
    calendar.fail_insert = http_error(403)
    # Uma sessão.
    with sessions() as session:
        # Fato.
        fact = _fact(session)
        # Recusado com a frase do 403.
        with pytest.raises(EventError, match="HTTP 403"):
            create_for_fact(
                session, get_settings(), fact.id, "dona", calendar_for=lambda c: calendar
            )
        # Grava o resto (o registro foi desfeito pelo ponto de salvamento).
        session.commit()
        # Nenhum registro.
        assert session.scalars(select(CreatedEvent)).all() == []

    # Sem conta Google conectada (load_credentials levanta RuntimeError com o motivo).
    def no_account(company_id):
        raise RuntimeError("Nenhuma conta Google conectada nesta Empresa.")

    # Uma sessão.
    with sessions() as session:
        # Fato de antes.
        fact_id = session.scalar(select(Fact.id))
        # A frase do motivo chega ao console.
        with pytest.raises(EventError, match="Nenhuma conta Google"):
            create_for_fact(session, get_settings(), fact_id, "dona", calendar_for=no_account)


# A Empresa do fato decide a agenda: o de outra Empresa vai para a conta dela.
def test_company_of_the_fact(sessions):
    # Agenda falsa.
    calendar = FakeCalendar([])
    # Empresas pedidas.
    asked: list[int] = []

    # Fábrica que anota a Empresa.
    def factory(company_id):
        # Anota.
        asked.append(company_id)
        # Devolve a agenda.
        return calendar

    # Uma sessão.
    with sessions() as session:
        # Segunda Empresa.
        other = create_company(session, "Cliente B")
        # Fato dela.
        fact = _fact(session, company_id=other.id)
        # Cria.
        row = create_for_fact(session, get_settings(), fact.id, "dona", calendar_for=factory)
    # A conta da Empresa B foi a usada, e o registro é dela.
    assert asked == [other.id] and row.company_id == other.id


# Apagar: confere a marca, apaga sem aviso e acompanha o que você já apagou no Google.
def test_remove(sessions):
    # Agenda falsa com um evento seu.
    calendar = FakeCalendar([_her_event()])
    # Uma sessão.
    with sessions() as session:
        # Dois fatos, dois eventos.
        first = create_for_fact(
            session,
            get_settings(),
            _fact(session, title="Um").id,
            "dona",
            calendar_for=lambda c: calendar,
        )
        # Segundo.
        second = create_for_fact(
            session,
            get_settings(),
            _fact(session, company_id=create_company(session, "Cliente B").id, title="Dois").id,
            "dona",
            calendar_for=lambda c: calendar,
        )
        # Alguém tirou a marca do segundo (ou o id aponta para outro evento).
        for event in calendar.items:
            # O segundo.
            if event["id"] == second.google_event_id:
                event.pop("extendedProperties")
        # Recusado: sem a marca, nunca apaga.
        with pytest.raises(EventError, match="não tem a marca"):
            remove(session, get_settings(), second, "dona", calendar_for=lambda c: calendar)
        # Nada apagado.
        assert calendar.deleted == []
        # O primeiro sai, sem aviso a ninguém.
        message = remove(session, get_settings(), first, "dona", calendar_for=lambda c: calendar)
        # Frase, pedido e registro.
        assert "saiu da agenda" in message and calendar.deleted == [("criado1", "none")]
        # Registro apagado.
        assert first.removed_at is not None and first.removed_by == "dona"
        # De novo: recusado.
        with pytest.raises(EventError, match="já foi apagado"):
            remove(session, get_settings(), first, "dona", calendar_for=lambda c: calendar)
        # Você apagou o segundo pelo Google.
        calendar.items = [e for e in calendar.items if e["id"] != second.google_event_id]
        # O console só acerta o registro.
        message = remove(session, get_settings(), second, "dona", calendar_for=lambda c: calendar)
        # Frase e nota.
        assert (
            "já não estava" in message and second.removed_note == "já tinha sido apagado na agenda"
        )
        # O seu evento continua lá.
        assert [e["id"] for e in calendar.items] == ["dela1"]
        # Depois de apagar, o mesmo fato pode ir de novo (o índice só vale para os ativos).
        again = create_for_fact(
            session, get_settings(), first.fact_id, "dona", calendar_for=lambda c: calendar
        )
        # Novo registro.
        assert again.id != first.id


# Conferir com a agenda e Apagar todos.
def test_sync_and_remove_all(sessions):
    # Agenda falsa com um evento seu.
    calendar = FakeCalendar([_her_event()])
    # Uma sessão.
    with sessions() as session:
        # O primeiro fato (cria o projeto, a Pessoa e o item).
        first = _fact(session, title="A")
        # Mais dois fatos no mesmo item (o _fact criaria o projeto de novo, e o nome do
        # projeto não pode repetir na Empresa).
        others = []
        # B e C.
        for title in ("B", "C"):
            # Fato.
            fact = Fact(
                project_id=first.project_id,
                kind=FactKind.TASK,
                title=title,
                source_item_id=first.source_item_id,
                source_quote="trecho",
                quote_verified=True,
                status="aberto",
                due_date=DUE,
            )
            # Prepara.
            session.add(fact)
            # Gera o id.
            session.flush()
            # Guarda.
            others.append(fact)
        # Cria os três eventos.
        rows = [
            create_for_fact(session, get_settings(), f.id, "dona", calendar_for=lambda c: calendar)
            for f in [first, *others]
        ]
        # Você apagou o A pelo Google.
        calendar.items = [e for e in calendar.items if e["id"] != rows[0].google_event_id]
        # E arrastou o B para o dia 12.
        for event in calendar.items:
            # O B.
            if event["id"] == rows[1].google_event_id:
                event["start"] = {"date": "2026-11-12"}
        # Um evento com a marca sem registro (ex.: banco restaurado de um backup).
        calendar.items.append(
            {
                "id": "orfao",
                "summary": "Prazo: antigo",
                "start": {"date": "2026-11-09"},
                "extendedProperties": {"private": {MARK_KEY: "1"}},
            }
        )
        # Conferir.
        summary = sync_company(
            session, get_settings(), EMPRESA, "dona", calendar_for=lambda c: calendar
        )
        # Um sumiu, três continuam (B, C e o órfão), um sem registro.
        assert (summary.gone, summary.present, summary.orphans, summary.removed) == (1, 3, 1, 0)
        # O A ficou como apagado; o B acompanhou o novo dia.
        assert rows[0].removed_at is not None and rows[1].day == date(2026, 11, 12)
        # Nada foi apagado no Google.
        assert calendar.deleted == []
        # Apagar todos.
        summary = sync_company(
            session,
            get_settings(),
            EMPRESA,
            "dona",
            remove_all=True,
            calendar_for=lambda c: calendar,
        )
        # B, C e o órfão saíram.
        assert summary.removed == 3 and "3 evento(s) apagado(s)" in summary.describe()
        # Só o seu evento ficou.
        assert [e["id"] for e in calendar.items] == ["dela1"]
        # Todos os registros ficaram como apagados.
        assert all(r.removed_at is not None for r in rows)
        # Diário das duas rodadas.
        actions = set(session.scalars(select(AuditLog.action)))
        # As duas.
        assert {"agenda.conferir", "agenda.apagar_todos"} <= actions


# "Extrair de novo" apaga o fato, mas o registro (e o evento) ficam, para poder apagar.
def test_redo_keeps_record(sessions):
    # Agenda falsa.
    calendar = FakeCalendar([])
    # Uma sessão.
    with sessions() as session:
        # Fato.
        fact = _fact(session)
        # Evento.
        row = create_for_fact(
            session, get_settings(), fact.id, "dona", calendar_for=lambda c: calendar
        )
        # Grava.
        session.commit()
        # Extrair de novo.
        redo_item(session, session.get(BronzeItem, fact.source_item_id), "dona")
        # Grava.
        session.commit()
        # Relê.
        session.refresh(row)
        # Sem vínculo, ainda ativo.
        assert row.fact_id is None and row.removed_at is None


# Páginas, botões e o comando de terminal.
def test_pages_buttons_and_command(sessions, monkeypatch, capsys):
    # Agenda falsa com um evento seu.
    calendar = FakeCalendar([_her_event()])
    # A agenda falsa no lugar da do Google.
    monkeypatch.setattr(calendar_events, "calendar_client", lambda db, settings, cid: calendar)
    # Uma sessão.
    with sessions() as session:
        # Fato.
        fact_id = _fact(session).id
        # Grava.
        session.commit()
    # Fatos: o botão aparece.
    page = client.get("/fatos", headers=OWNER).text
    # Botão.
    assert "Pôr na agenda" in page and 'action="/eventos/criar"' in page
    # Menu com a página nova.
    assert 'href="/eventos"' in page
    # POST de outro site: recusado.
    assert (
        client.post(
            "/eventos/criar",
            data={"fato_id": fact_id},
            headers={**OWNER, "Origin": "http://mal"},
        ).status_code
        == 403
    )
    # Pôr na agenda, voltando para Fatos com o filtro.
    done = client.post(
        "/eventos/criar",
        data={"fato_id": fact_id, "volta": "/fatos?tipo=tarefa"},
        headers=SAME,
        follow_redirects=False,
    )
    # Volta para onde estava.
    assert done.status_code == 303 and done.headers["location"] == "/fatos?tipo=tarefa"
    # O aviso e o "na agenda".
    page = client.get("/fatos", headers=OWNER).text
    # Aviso e selo.
    assert "foi para a agenda em 10/11/2026" in page and "na agenda ✓" in page
    # Segundo clique: aviso vermelho, sem outro evento; volta forjada vai para /eventos.
    again = client.post(
        "/eventos/criar",
        data={"fato_id": fact_id, "volta": "//mal.com/x"},
        headers=SAME,
        follow_redirects=False,
    )
    # Endereço seguro.
    assert again.headers["location"] == "/eventos" and len(calendar.inserted) == 1
    # A página Na agenda mostra o evento e o aviso.
    page = client.get("/eventos", headers=OWNER).text
    # Evento, aviso e botões.
    assert "Tarefa: Enviar cronograma" in page and "já está na agenda" in page
    # Botões por Empresa.
    assert "Conferir com a agenda" in page and "Apagar todos" in page
    # Alertas: a página abre com o mapa de eventos (sem alertas, sem botão; só não quebra).
    assert client.get("/alertas", headers=OWNER).status_code == 200
    # Conferir (o cliente segue o redirecionamento: a resposta já é a página com o aviso).
    page = client.post("/eventos/conferir", data={"empresa": EMPRESA}, headers=SAME).text
    # Aviso.
    assert "1 continua(m) na agenda" in page
    # Conferir sem Empresa: erro de pedido.
    assert client.post("/eventos/conferir", data={}, headers=SAME).status_code == 400
    # Apagar um.
    with sessions() as session:
        # O registro.
        row_id = session.scalar(select(CreatedEvent.id))
    # Pedido; a resposta é a página Na agenda, com o aviso.
    page = client.post(f"/eventos/{row_id}/apagar", data={"volta": "/eventos"}, headers=SAME).text
    # Aviso e lista vazia.
    assert "saiu da agenda" in page and "Nenhum evento na agenda" in page
    # Com os apagados, ele aparece.
    assert "Tarefa: Enviar cronograma" in client.get("/eventos?apagados=1", headers=OWNER).text
    # Registro inexistente.
    assert client.post("/eventos/999/apagar", headers=SAME).status_code == 404
    # Põe de novo e apaga pelo terminal.
    client.post("/eventos/criar", data={"fato_id": fact_id}, headers=SAME)
    # Comando.
    cli_main(["apagar-eventos-criados", "--empresa", str(EMPRESA)])
    # Saída.
    assert "Empresa Teste: 1 evento(s) apagado(s) da agenda." in capsys.readouterr().out
    # Só o seu evento ficou.
    assert [e["id"] for e in calendar.items] == ["dela1"]
    # Empresa inexistente.
    cli_main(["apagar-eventos-criados", "--empresa", "99"])
    # Saída.
    assert "Nenhuma Empresa" in capsys.readouterr().out


# O briefing mostra o botão nos prazos.
def test_briefing_button(sessions, monkeypatch):
    # Briefing e agenda falsa.
    from app import briefing

    # Agenda vazia.
    calendar = FakeCalendar([])
    # As duas trocas.
    monkeypatch.setattr(briefing, "calendar_client", lambda db, settings, cid: calendar)
    monkeypatch.setattr(calendar_events, "calendar_client", lambda db, settings, cid: calendar)
    # Hoje de verdade (a página usa o relógio real).
    today = briefing.today_local(get_settings())
    # Uma sessão.
    with sessions() as session:
        # Prazo para amanhã.
        _fact(session, due_date=today + timedelta(days=1))
        # Grava.
        session.commit()
    # Monta o briefing.
    client.post("/briefing/gerar", headers=SAME)
    # O prazo tem o botão.
    page = client.get("/briefing", headers=OWNER).text
    # Botão, voltando para o briefing.
    assert "Pôr na agenda" in page and 'name="volta" value="/briefing"' in page
