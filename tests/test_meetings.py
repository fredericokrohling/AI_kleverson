"""Testes da tabela de reuniões (uma linha por reunião, acima das transcrições).

O que se prova aqui:
- a importação de um evento da agenda cria a reunião com data, horário, organizador e
  convidados ligados às Pessoas; outra transcrição do mesmo evento cai na mesma reunião;
- fora da agenda, cada importação é uma reunião, com o horário digitado (ou só o dia);
- a extensão liga a captura ao evento quando acha o endereço do Meet na agenda, e a
  transcrição dela fica junto da importada do mesmo evento;
- corrigir a Empresa leva a transcrição para uma reunião da Empresa nova;
- o banco recusa ligar uma transcrição a uma reunião de outra Empresa;
- a migração 0011 cria as reuniões das transcrições que já existiam.
"""

# JSON do "extra" no formato antigo.
import json

# Datas de exemplo.
from datetime import UTC, date, datetime, time, timedelta

# Biblioteca de testes.
import pytest

# Migrações (para o teste da carga inicial): os comandos e a configuração.
from alembic import command

# Configuração do Alembic (lê o alembic.ini).
from alembic.config import Config

# Simula o navegador e a extensão.
from fastapi.testclient import TestClient

# Consultas e SQL direto.
from sqlalchemy import select, text

# Erro do banco: regra violada.
from sqlalchemy.exc import DBAPIError

# Empresas: criar e corrigir a Empresa de uma transcrição.
from app.companies import create_company, move_item

# Conexão com o banco.
from app.db import get_engine

# Guarda dos originais.
from app.ingestion.storage import FileStore

# O app.
from app.main import app

# Tabelas.
from app.models import (
    BronzeItem,
    Company,
    LiveMeeting,
    Meeting,
    MeetingAttendee,
    OAuthToken,
)

# Cadastro de pessoas.
from app.projects import save_person

# Módulos trocados nos testes.
from app.transcription import calendar as calendar_module
from app.transcription import live, routes, routes_live

# Evento da agenda (o formato que a importação recebe).
from app.transcription.calendar import CalendarEvent

# Importação de transcrição.
from app.transcription.importer import import_transcript

# Agenda falsa e a Empresa que todo teste tem.
from tests.fakes import EMPRESA, FakeCalendar

# Serviço de transcrição falso e o envio de "áudio" pela extensão.
from tests.test_live import EchoStream, _falar

# Navegador simulado.
client = TestClient(app)

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}

# POST vindo do próprio console.
SAME = {**OWNER, "Origin": "http://testserver"}

# Captura da extensão.
SESSAO = "cccccccc-0000-4000-8000-000000000003"

# Legenda do Teams (uma fala).
VTT = """WEBVTT

00:00:03.000 --> 00:00:05.000
<v Ana Souza>Vamos fechar o cronograma do Atlas.</v>
"""

# Outra legenda (outra transcrição da mesma reunião).
VTT_2 = VTT.replace("fechar", "revisar")

# Início do evento de exemplo.
INICIO = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)

# Evento da agenda de exemplo.
EVENTO = CalendarEvent(
    id="ev-atlas",
    title="Atlas semanal",
    start=INICIO,
    end=INICIO + timedelta(hours=1),
    organizer="dona@empresa.com",
    attendees=["Ana Souza", "joao@cliente.com"],
    attendee_emails={"Ana Souza": "ana@cliente.com", "joao@cliente.com": "joao@cliente.com"},
    platform="meet",
    meeting_url="https://meet.google.com/abc-defg-hij",
)


# Uma segunda Empresa (a primeira, EMPRESA, vem do conftest).
@pytest.fixture
def outra(sessions) -> int:
    # Cria.
    with sessions() as s:
        # Empresa nova.
        company = create_company(s, "Cliente B", None)
        # Grava.
        s.commit()
        # Id.
        return company.id


# Troca o serviço de transcrição pelo falso e ignora os avisos ao Início.
@pytest.fixture
def ao_vivo(monkeypatch):
    # Cada conexão ganha o seu serviço falso.
    monkeypatch.setattr(routes, "create_provider", lambda settings: EchoStream())
    # Avisos ao Início: descartados.
    monkeypatch.setattr(live.hub, "publish", lambda event: None)


# Importa uma legenda; devolve o id do item.
def _importar(session, tmp_path, *, event=None, data=VTT, title="Atlas", start_time=None):
    # Importa.
    result = import_transcript(
        session,
        FileStore(str(tmp_path)),
        actor="dona",
        data=data.encode(),
        filename="ata.vtt",
        event=event,
        title=title,
        day=date(2026, 11, 3),
        timezone="America/Sao_Paulo",
        company_id=EMPRESA,
        start_time=start_time,
    )
    # Grava.
    session.commit()
    # Id.
    return result.item_id


# --- Importação ----------------------------------------------------------------------


# Evento da agenda: reunião com os dados do evento e convidados ligados às Pessoas.
def test_import_from_event_creates_meeting(sessions, tmp_path):
    # Ana está cadastrada (o e-mail dela está no convite).
    with sessions() as s:
        # Pessoa.
        ana = save_person(s, None, name="Ana Souza", email="ana@cliente.com", company_id=EMPRESA)
        # Grava.
        s.commit()
        # Importa duas transcrições do mesmo evento.
        first = _importar(s, tmp_path, event=EVENTO)
        # A segunda (outro arquivo).
        second = _importar(s, tmp_path, event=EVENTO, data=VTT_2)
        # Uma reunião só.
        meeting = s.scalars(select(Meeting)).one()
        # Com os dados do evento.
        assert (meeting.calendar_event_id, meeting.title, meeting.starts_at) == (
            "ev-atlas",
            "Atlas semanal",
            INICIO,
        )
        # Fim, organizador, plataforma e horário conhecido.
        assert meeting.ends_at == INICIO + timedelta(hours=1)
        # Organizador.
        assert meeting.organizer == "dona@empresa.com" and meeting.platform == "meet"
        # As duas transcrições apontam para ela.
        assert {s.get(BronzeItem, i).meeting_id for i in (first, second)} == {meeting.id}
        # Convidados: Ana ligada à Pessoa, João sem cadastro.
        rows = {
            a.email: a.stakeholder_id
            for a in s.scalars(
                select(MeetingAttendee).where(MeetingAttendee.meeting_id == meeting.id)
            )
        }
        # Confere.
        assert rows == {"ana@cliente.com": ana.id, "joao@cliente.com": None}
    # A página da transcrição mostra a reunião, os participantes e a outra transcrição.
    page = client.get(f"/conteudo/{first}", headers=OWNER).text
    # Reunião.
    assert "<strong>Reunião:</strong> Atlas semanal" in page and "da agenda" in page
    # Ana com link para a Pessoa.
    assert f'<a href="/cadastros/pessoas/{ana.id}">Ana Souza</a>' in page
    # A outra transcrição.
    assert f'<a href="/conteudo/{second}">importada ({second})</a>' in page


# Fora da agenda: uma reunião por importação, com o horário digitado ou só o dia.
def test_import_outside_calendar(sessions, tmp_path):
    # Com horário.
    with sessions() as s:
        # 10h no fuso do console (13h em UTC).
        with_time = _importar(s, tmp_path, title="Conversa", start_time=time(10, 0))
        # Sem horário (outro arquivo).
        no_time = _importar(s, tmp_path, title="Conversa", data=VTT_2)
        # Duas reuniões (fora da agenda não há como saber que são a mesma).
        assert len(s.scalars(select(Meeting)).all()) == 2
        # A com horário.
        m1 = s.get(Meeting, s.get(BronzeItem, with_time).meeting_id)
        # Horário certo e conhecido.
        assert m1.starts_at == INICIO and m1.time_known and m1.calendar_event_id is None
        # A sem horário: só o dia.
        m2 = s.get(Meeting, s.get(BronzeItem, no_time).meeting_id)
        # Meia-noite, marcada como sem horário.
        assert not m2.time_known
    # A página diz que o horário não foi informado.
    assert "(horário não informado)" in client.get(f"/conteudo/{no_time}", headers=OWNER).text


# Horário digitado errado no formulário: recusado com motivo, nada gravado.
def test_import_form_rejects_bad_time(sessions):
    # Envia (o TestClient segue o redirecionamento e abre a página com o aviso).
    page = client.post(
        "/transcricoes/importar",
        data={
            "data": "2026-11-03",
            "evento": "__fora__",
            "titulo": "X",
            "hora": "25h",
            "empresa": str(EMPRESA),
        },
        files={"arquivo": ("ata.vtt", VTT.encode())},
        headers=SAME,
    ).text
    # O aviso.
    assert "Horário inválido; use HH:MM." in page
    # Nada gravado.
    with sessions() as s:
        # Nenhuma transcrição.
        assert s.scalars(select(BronzeItem)).first() is None


# --- Extensão ao vivo ------------------------------------------------------------------


# Agenda falsa com o evento de hoje, e conta Google na Empresa.
def _agenda_de_hoje(monkeypatch, sessions) -> datetime:
    # A reunião começa agora.
    start = datetime.now(UTC).replace(microsecond=0)
    # Agenda.
    calendar = FakeCalendar(
        [
            {
                "id": "ev-hoje",
                "summary": "Status semanal",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
                "hangoutLink": "https://meet.google.com/abc-defg-hij",
                "organizer": {"email": "dona@empresa.com"},
                "attendees": [{"email": "ana@cliente.com", "displayName": "Ana Souza"}],
            }
        ]
    )
    # Descoberta da Empresa (no /iniciar) e rotas do Início usam a agenda falsa.
    monkeypatch.setattr(calendar_module, "calendar_client", lambda db, settings, *_a: calendar)
    # Rotas do Início.
    monkeypatch.setattr(routes_live, "calendar_client", lambda db, settings, *_a: calendar)
    # Conta Google da Empresa.
    with sessions() as s:
        # Conta.
        s.add(
            OAuthToken(
                company_id=EMPRESA,
                provider="google",
                account_email="dona@empresa.com",
                scopes=[],
                refresh_token_enc=b"x",
            )
        )
        # Grava.
        s.commit()
    # Devolve o início.
    return start


# A extensão acha o evento: a captura e a transcrição salva ficam na reunião do evento.
def test_live_meeting_linked_to_calendar_event(ao_vivo, sessions, monkeypatch, tmp_path):
    # Agenda com o evento.
    start = _agenda_de_hoje(monkeypatch, sessions)
    # Começa com o endereço do Meet.
    client.post(
        f"/transcricoes/{SESSAO}/iniciar",
        json={"titulo": "Meet", "url_reuniao": "https://meet.google.com/abc-defg-hij"},
        headers=OWNER,
    )
    # Inclui um participante durante a reunião.
    client.post(
        f"/ao-vivo/{SESSAO}/participantes",
        data={"nome": "Bruno Reis", "email": "bruno@cliente.com"},
        headers=SAME,
    )
    # Fala e encerra.
    _falar(SESSAO, "outros", "o marco atrasou")
    # Encerra.
    bronze = client.post(f"/transcricoes/{SESSAO}/finalizar", json={}, headers=OWNER).json()
    # Confere.
    with sessions() as s:
        # A reunião do evento.
        meeting = s.scalars(select(Meeting)).one()
        # Dados da agenda.
        assert (meeting.calendar_event_id, meeting.title, meeting.starts_at) == (
            "ev-hoje",
            "Status semanal",
            start,
        )
        # A captura aponta para ela.
        assert s.scalars(select(LiveMeeting)).one().meeting_id == meeting.id
        # A transcrição também, com o thread_id do evento.
        item = s.get(BronzeItem, bronze["bronze_id"])
        # Confere.
        assert item.meeting_id == meeting.id and item.thread_id == "agenda:ev-hoje"
        # Participantes: o convidado da agenda e o incluído ao vivo.
        assert sorted(
            a.email
            for a in s.scalars(
                select(MeetingAttendee).where(MeetingAttendee.meeting_id == meeting.id)
            )
        ) == ["ana@cliente.com", "bruno@cliente.com"]
        # Importa a transcrição do Meet do mesmo evento: cai na mesma reunião.
        event = calendar_module.parse_event(
            {
                "id": "ev-hoje",
                "summary": "Status semanal",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
            }
        )
        # Importa.
        imported = _importar(s, tmp_path, event=event)
        # Mesma reunião.
        assert s.get(BronzeItem, imported).meeting_id == meeting.id
    # A página da importada mostra a da extensão como outra transcrição da reunião.
    page = client.get(f"/conteudo/{imported}", headers=OWNER).text
    # Link.
    assert f'<a href="/conteudo/{item.id}">da extensão ({item.id})</a>' in page


# Sem o evento na agenda: a reunião nasce ao salvar, com o título da aba e a hora do fim.
def test_live_meeting_without_calendar(ao_vivo, sessions):
    # Começa sem endereço.
    client.post(f"/transcricoes/{SESSAO}/iniciar", json={"titulo": "Conversa"}, headers=OWNER)
    # Escolhe a Empresa no Início (sem conta Google, a agenda não é lida).
    client.post(f"/ao-vivo/{SESSAO}/empresa", data={"empresa": EMPRESA}, headers=SAME)
    # Fala.
    _falar(SESSAO, "voce", "bom dia")
    # Antes de salvar, ainda não há reunião.
    with sessions() as s:
        # Nenhuma.
        assert s.scalars(select(Meeting)).first() is None
    # Encerra.
    bronze = client.post(f"/transcricoes/{SESSAO}/finalizar", json={}, headers=OWNER).json()
    # Confere.
    with sessions() as s:
        # A reunião.
        meeting = s.scalars(select(Meeting)).one()
        # A captura.
        capture = s.scalars(select(LiveMeeting)).one()
        # Título da aba, início e fim da captura, fora da agenda.
        assert (meeting.title, meeting.calendar_event_id) == ("Conversa", None)
        # Horários.
        assert meeting.starts_at == capture.started_at and meeting.ends_at == capture.finished_at
        # A transcrição aponta para ela.
        assert s.get(BronzeItem, bronze["bronze_id"]).meeting_id == meeting.id


# --- Empresa -------------------------------------------------------------------------


# Corrigir a Empresa leva a transcrição para uma reunião da Empresa nova.
def test_move_item_moves_meeting(sessions, tmp_path, outra):
    # Ana existe nas duas Empresas (Pessoas não são compartilhadas).
    with sessions() as s:
        # Na primeira.
        save_person(s, None, name="Ana Souza", email="ana@cliente.com", company_id=EMPRESA)
        # Na outra.
        ana_b = save_person(s, None, name="Ana Souza", email="ana@cliente.com", company_id=outra)
        # Grava.
        s.commit()
        # Importa na primeira.
        item_id = _importar(s, tmp_path, event=EVENTO)
        # Reunião antiga.
        old = s.get(BronzeItem, item_id).meeting_id
        # Corrige.
        move_item(s, s.get(BronzeItem, item_id), s.get(Company, outra), "dona")
        # Grava (as travas do banco conferem aqui).
        s.commit()
        # A transcrição.
        item = s.get(BronzeItem, item_id)
        # A reunião nova é da outra Empresa, do mesmo evento.
        meeting = s.get(Meeting, item.meeting_id)
        # Confere.
        assert meeting.company_id == outra and meeting.calendar_event_id == "ev-atlas"
        # A antiga ficou vazia e foi apagada.
        assert s.get(Meeting, old) is None
        # Ana ligada à Pessoa da outra Empresa.
        attendee = s.scalars(
            select(MeetingAttendee).where(
                MeetingAttendee.meeting_id == meeting.id, MeetingAttendee.email == "ana@cliente.com"
            )
        ).one()
        # Confere.
        assert attendee.stakeholder_id == ana_b.id


# O banco recusa uma transcrição apontando para a reunião de outra Empresa.
def test_database_rejects_meeting_of_other_company(sessions, tmp_path, outra):
    # Importa na primeira Empresa.
    with sessions() as s:
        # Item.
        item_id = _importar(s, tmp_path, event=EVENTO)
        # Reunião na outra Empresa.
        other = Meeting(company_id=outra, title="Outra", starts_at=INICIO)
        # Prepara.
        s.add(other)
        # Gera o id.
        s.flush()
        # Aponta a transcrição para ela.
        s.get(BronzeItem, item_id).meeting_id = other.id
        # O commit é recusado.
        with pytest.raises(DBAPIError, match="Empresas diferentes"):
            s.commit()


# A Empresa de uma reunião não muda.
def test_meeting_company_is_fixed(sessions, tmp_path, outra):
    # Importa.
    with sessions() as s:
        # Item.
        item_id = _importar(s, tmp_path, event=EVENTO)
        # A reunião.
        meeting = s.get(Meeting, s.get(BronzeItem, item_id).meeting_id)
        # Tenta mudar a Empresa.
        meeting.company_id = outra
        # Recusado na hora.
        with pytest.raises(DBAPIError, match="não muda de Empresa"):
            s.flush()


# Pessoa cadastrada depois: o convidado de uma reunião antiga passa a ser ela.
def test_attendee_linked_when_person_is_created(sessions, tmp_path):
    # Importa sem ninguém cadastrado.
    with sessions() as s:
        # Item.
        _importar(s, tmp_path, event=EVENTO)
        # Cadastra o João depois.
        joao = save_person(s, None, name="João", email="joao@cliente.com", company_id=EMPRESA)
        # Grava.
        s.commit()
        # O convidado.
        attendee = s.scalars(
            select(MeetingAttendee).where(MeetingAttendee.email == "joao@cliente.com")
        ).one()
        # Ligado.
        assert attendee.stakeholder_id == joao.id


# --- Migração ------------------------------------------------------------------------


# A migração 0011 cria as reuniões das transcrições que já existiam.
def test_migration_backfills_meetings():
    # Configuração do Alembic.
    config = Config("alembic.ini")
    # Volta o banco para antes da tabela de reuniões.
    command.downgrade(config, "0010")
    # Tenta (o banco volta para a versão atual mesmo se algo falhar).
    try:
        # Dados no formato antigo.
        with get_engine().begin() as conn:
            # Pessoa com o e-mail da Ana.
            conn.execute(
                text(
                    "INSERT INTO stakeholders (id, company_id, name, email, is_leader, aliases) "
                    "VALUES (50, :c, 'Ana Souza', 'ana@cliente.com', false, '{}')"
                ),
                {"c": EMPRESA},
            )
            # Importada de um evento, com o evento no "extra".
            conn.execute(
                text(
                    "INSERT INTO bronze_items (id, company_id, source, external_id, subject, "
                    "occurred_at, content_sha256, extra) VALUES (100, :c, 'transcricao_reuniao', "
                    "'importada:1', 'Atlas', '2026-11-03T13:00:00+00:00', 'x', CAST(:e AS jsonb))"
                ),
                {
                    "c": EMPRESA,
                    "e": json.dumps(
                        {
                            "origem": "importada",
                            "plataforma": "meet",
                            "evento_agenda": EVENTO.to_extra(),
                        }
                    ),
                },
            )
            # Da extensão, do mesmo endereço e 10 minutos depois do início.
            conn.execute(
                text(
                    "INSERT INTO bronze_items (id, company_id, source, external_id, subject, "
                    "occurred_at, content_sha256, extra) VALUES (101, :c, 'transcricao_reuniao', "
                    "'sessao-1', 'Meet', '2026-11-03T13:10:00+00:00', 'y', '{}')"
                ),
                {"c": EMPRESA},
            )
            # A captura dela.
            conn.execute(
                text(
                    "INSERT INTO live_meetings (id, session_id, title, meeting_url, company_id, "
                    "started_at, last_activity_at, finished_at, bronze_item_id) VALUES (7, "
                    "'sessao-1', 'Meet', 'https://meet.google.com/abc-defg-hij', :c, "
                    "'2026-11-03T13:10:00+00:00', now(), '2026-11-03T13:50:00+00:00', 101)"
                ),
                {"c": EMPRESA},
            )
            # Participante cadastrado ao vivo.
            conn.execute(
                text(
                    "INSERT INTO meeting_participants (meeting_id, name, name_key, email) "
                    "VALUES (7, 'Bruno', 'bruno', 'bruno@cliente.com')"
                )
            )
            # Importada fora da agenda.
            conn.execute(
                text(
                    "INSERT INTO bronze_items (id, company_id, source, external_id, subject, "
                    "occurred_at, content_sha256, extra) VALUES (102, :c, 'transcricao_reuniao', "
                    "'importada:2', 'Conversa', '2026-11-03T03:00:00+00:00', 'z', "
                    "CAST(:e AS jsonb))"
                ),
                {"c": EMPRESA, "e": '{"origem": "importada", "evento_agenda": null}'},
            )
    # Sobe de novo, sempre.
    finally:
        command.upgrade(config, "head")
    # Confere.
    with get_engine().begin() as conn:
        # Reunião de cada transcrição.
        rows = dict(conn.execute(text("SELECT id, meeting_id FROM bronze_items")).all())
        # A importada do evento e a da extensão ficam na mesma reunião; a fora da agenda, sozinha.
        assert rows[100] == rows[101] and rows[102] not in (None, rows[100])
        # A reunião do evento.
        event = conn.execute(
            text("SELECT calendar_event_id, title, time_known FROM meetings WHERE id = :i"),
            {"i": rows[100]},
        ).one()
        # Dados do evento.
        assert tuple(event) == ("ev-atlas", "Atlas semanal", True)
        # A fora da agenda: só o dia.
        assert (
            conn.execute(
                text("SELECT time_known FROM meetings WHERE id = :i"), {"i": rows[102]}
            ).scalar()
            is False
        )
        # Participantes: Ana (ligada à Pessoa) e Bruno (da captura).
        attendees = dict(
            conn.execute(
                text("SELECT email, stakeholder_id FROM meeting_attendees WHERE meeting_id = :i"),
                {"i": rows[100]},
            ).all()
        )
        # Confere.
        assert attendees == {
            "ana@cliente.com": 50,
            "joao@cliente.com": None,
            "bruno@cliente.com": None,
        }
        # A captura aponta para a reunião; a transcrição da extensão ganhou o thread_id.
        assert conn.execute(text("SELECT meeting_id FROM live_meetings")).scalar() == rows[100]
        # thread_id.
        assert (
            conn.execute(text("SELECT thread_id FROM bronze_items WHERE id = 101")).scalar()
            == "agenda:ev-atlas"
        )
