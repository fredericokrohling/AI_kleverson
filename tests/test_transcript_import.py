"""Testes da importação de transcrições do Teams e do Meet (agenda, leitura e página)."""

# Data, e data e hora.
from datetime import date, datetime

# Biblioteca de testes.
import pytest

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import func, select

# Configuração (para trocar a pasta dos originais por uma temporária).
from app.config import get_settings

# O app.
from app.main import app

# Tabelas.
from app.models import AuditLog, BronzeItem, SourceKind

# Rotas da importação (para trocar a agenda e o Drive pelos falsos).
from app.transcription import routes_import

# Contas do dia de calendário.
from app.transcription.agenda import build_day

# Agenda.
from app.transcription.calendar import events_on, parse_event

# Leitura dos arquivos.
from app.transcription.importer import ImportFailure, parse_captions, read_transcript

# Agenda e Drive falsos, e geradores de arquivos.
from tests.fakes import FakeCalendar, FakeDrive, make_docx, make_pdf

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient.
SAME = {**OWNER, "Origin": "http://testserver"}
# Um cliente para o arquivo inteiro.
client = TestClient(app)

# Legenda no formato do Teams: a fala da Ana vem cortada em dois blocos.
TEAMS_VTT = """WEBVTT

1a2b3c/12-0
00:00:03.120 --> 00:00:05.440
<v Ana Souza>Bom dia a todos.</v>

1a2b3c/13-0
00:00:05.500 --> 00:00:08.000
<v Ana Souza>Vamos ao status &amp; riscos.</v>

1a2b3c/14-0
00:01:10.000 --> 00:01:12.000
<v João Lima>O marco 2 atrasou uma semana.</v>
"""

# Legenda .srt, sem nome de quem fala, passando de uma hora.
SRT = """1
00:00:01,000 --> 00:00:02,000
Primeira frase.

2
01:02:03,500 --> 01:02:05,000
<i>Última frase.</i>
"""


# Um evento da agenda no formato da API.
def make_event(event_id: str, title: str, start: str, end: str, **extra) -> dict:
    # Campos mínimos e os extras do teste.
    return {
        # Id do evento.
        "id": event_id,
        # Título.
        "summary": title,
        # Início com hora.
        "start": {"dateTime": start},
        # Fim com hora.
        "end": {"dateTime": end},
        # O que o teste acrescentar.
        **extra,
    }


# Agenda de 02/11/2026: uma reunião do Meet com transcrição anexada, uma do Teams, um
# feriado (dia inteiro) e uma reunião de outro dia.
def sample_calendar() -> FakeCalendar:
    return FakeCalendar(
        [
            make_event(
                "meet1",
                "Status semanal",
                "2026-11-02T14:00:00-03:00",
                "2026-11-02T15:00:00-03:00",
                hangoutLink="https://meet.google.com/abc-defg-hij",
                organizer={"email": "dona@exemplo.com"},
                attendees=[
                    {"email": "ana@exemplo.com", "displayName": "Ana Souza"},
                    {"email": "joao@exemplo.com"},
                    {"email": "sala@resource.calendar.google.com", "resource": True},
                ],
                attachments=[
                    {
                        "fileId": "doc-transcricao",
                        "title": "Status semanal (2026-11-02 14:00 GMT-3) - Transcrição",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                    {
                        "fileId": "doc-gemini",
                        "title": "Anotações do Gemini",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                ],
            ),
            make_event(
                "teams1",
                "Comitê do cliente",
                "2026-11-02T09:00:00-03:00",
                "2026-11-02T10:00:00-03:00",
                description="Join: https://teams.microsoft.com/l/meetup-join/xyz",
            ),
            {
                "id": "feriado",
                "summary": "Finados",
                "start": {"date": "2026-11-02"},
                "end": {"date": "2026-11-03"},
            },
            make_event(
                "outro-dia", "Amanhã", "2026-11-03T09:00:00-03:00", "2026-11-03T10:00:00-03:00"
            ),
        ]
    )


# Troca a agenda, o Drive e a pasta dos originais nas rotas.
@pytest.fixture
def google(monkeypatch, tmp_path):
    # Agenda falsa.
    calendar = sample_calendar()
    # Drive falso com a transcrição do Meet (um Google Docs, exportado como Word).
    drive = FakeDrive(
        {
            "doc-transcricao": {
                "name": "Transcrição",
                "mimeType": "application/vnd.google-apps.document",
                "data": make_docx(),
            }
        }
    )
    # As rotas usam os falsos.
    monkeypatch.setattr(routes_import, "calendar_client", lambda db, settings: calendar)
    # Drive.
    monkeypatch.setattr(routes_import, "drive_client", lambda db, settings: drive)
    # Originais numa pasta temporária.
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    # Entrega os dois ao teste.
    return calendar, drive


# Legenda do Teams: falas cortadas são juntadas e o texto sai no formato da extensão.
def test_parse_teams_vtt():
    # Lê a legenda.
    turns = parse_captions(TEAMS_VTT)
    # Os dois blocos da Ana viraram uma fala; o código &amp; virou "&".
    assert [(t.speaker, t.text) for t in turns] == [
        ("Ana Souza", "Bom dia a todos. Vamos ao status & riscos."),
        ("João Lima", "O marco 2 atrasou uma semana."),
    ]
    # Texto final no formato da transcrição da extensão.
    transcript = read_transcript(TEAMS_VTT.encode(), "reuniao.vtt")
    # Uma linha por fala, com o tempo.
    assert transcript.text.splitlines() == [
        "[00:03] Ana Souza: Bom dia a todos. Vamos ao status & riscos.",
        "[01:10] João Lima: O marco 2 atrasou uma semana.",
    ]
    # Quem falou e quantas falas.
    assert (transcript.speakers, transcript.turns) == (["Ana Souza", "João Lima"], 2)


# .srt sem nomes, .txt que é .vtt, .txt comum e Word.
def test_parse_srt_and_txt_with_vtt_inside():
    # .srt sem nomes, com uma fala depois de uma hora (marcação <i> removida).
    assert read_transcript(SRT.encode(), "legenda.srt").text.splitlines() == [
        "[00:01] Primeira frase.",
        "[1:02:03] Última frase.",
    ]
    # Um .txt que na verdade é .vtt é lido como legenda.
    assert read_transcript(TEAMS_VTT.encode(), "transcricao.txt").kind == "legenda"
    # Um .txt no formato "Nome: texto" (o do Meet) vira uma fala por linha.
    named = read_transcript("Ana: bom dia\nJoão: oi".encode(), "notas.txt")
    # Mesmo formato das legendas, com quem falou.
    assert (named.text, named.speakers) == (
        "[00:00] Ana: bom dia\n[00:00] João: oi",
        ["Ana", "João"],
    )
    # Um .txt comum é lido como documento, sem falas.
    plain = read_transcript("Pauta da reunião\nRevisar o escopo".encode(), "notas.txt")
    # Texto como veio.
    assert (plain.kind, plain.turns, plain.items) == ("documento", None, [])
    # Word.
    assert "Projeto Atlas" in read_transcript(make_docx(), "Transcrição.docx").text


# Arquivos que não servem: formato, vazio, legenda sem falas e PDF digitalizado.
@pytest.mark.parametrize(
    ("data", "name", "message"),
    [
        (b"x", "gravacao.mp4", "não aceito"),
        (b"", "vazio.vtt", "vazio"),
        (b"WEBVTT\n\nNOTE sem falas\n", "nada.vtt", "Nenhuma fala"),
        (make_pdf(""), "digitalizado.pdf", "Não consegui ler"),
    ],
)
def test_read_transcript_errors(data, name, message):
    # Cada arquivo inválido levanta o erro com a explicação.
    with pytest.raises(ImportFailure, match=message):
        read_transcript(data, name)


# Reuniões do dia: fuso, ordem, plataforma, participantes e transcrição anexada.
def test_calendar_events():
    # Agenda falsa.
    calendar = sample_calendar()
    # Reuniões de 02/11 no fuso de Brasília.
    events = events_on(calendar, date(2026, 11, 2), "America/Sao_Paulo")
    # Só as duas com horário, em ordem; o feriado e a do dia seguinte ficam fora.
    assert [e.id for e in events] == ["teams1", "meet1"]
    # O dia foi pedido de meia-noite a meia-noite, no fuso de Brasília.
    assert calendar.list_calls == [("2026-11-02T00:00:00-03:00", "2026-11-03T00:00:00-03:00")]
    # Plataformas reconhecidas.
    assert [e.platform for e in events] == ["teams", "meet"]
    # Reunião do Meet.
    meet = events[1]
    # A sala de reunião não é participante; sem nome, vale o e-mail.
    assert meet.attendees == ["Ana Souza", "joao@exemplo.com"]
    # Só a transcrição conta; as anotações do Gemini não.
    assert [t.file_id for t in meet.transcripts] == ["doc-transcricao"]
    # Evento de dia inteiro não vira reunião.
    assert parse_event(calendar.items[2]) is None


# A página lista as reuniões do dia e troca a lista quando a data muda.
def test_import_page_lists_meetings(google):
    # Abre a página no dia das reuniões.
    page = client.get("/transcricoes/importar?data=2026-11-02", headers=OWNER).text
    # As duas reuniões aparecem com horário e plataforma.
    assert "09:00–10:00 · Comitê do cliente · Teams" in page
    # A do Meet avisa que tem transcrição anexada.
    assert "Status semanal · Meet · transcrição do Meet anexada" in page
    # O feriado não aparece.
    assert "Finados" not in page
    # A opção de reunião fora da agenda está sempre lá.
    assert "Não está na agenda" in page
    # Trocar a data devolve só a lista do novo dia.
    part = client.get("/transcricoes/importar/reunioes?data=2026-11-03", headers=OWNER).text
    # Reunião do dia 3.
    assert "Amanhã" in part and "Status semanal" not in part


# Sem a agenda, a página abre e deixa importar "fora da agenda".
def test_import_page_without_google(monkeypatch):
    # Sem conta Google: a agenda falha.
    def no_account(db, settings):
        raise RuntimeError("Nenhuma conta Google conectada.")

    # As rotas usam a agenda que falha.
    monkeypatch.setattr(routes_import, "calendar_client", no_account)
    # A página abre mesmo assim.
    page = client.get("/transcricoes/importar", headers=OWNER).text
    # Com o motivo e a saída "fora da agenda".
    assert "Nenhuma conta Google conectada." in page and "Não está na agenda" in page


# Importação de .vtt ligada à reunião do Teams, sem duplicar ao repetir.
def test_import_vtt_linked_to_event(google, sessions):
    # Envia a legenda do Teams ligada à reunião do Teams.
    response = client.post(
        "/transcricoes/importar",
        data={"data": "2026-11-02", "evento": "teams1"},
        files={"arquivo": ("Comite.vtt", TEAMS_VTT.encode(), "text/vtt")},
        headers=SAME,
        follow_redirects=False,
    )
    # Volta para a página, no mesmo dia.
    assert response.headers["location"] == "/transcricoes/importar?data=2026-11-02"
    # Aviso de sucesso com o resumo.
    page = client.get(response.headers["location"], headers=OWNER).text
    # Texto do aviso.
    assert "Transcrição carregada com sucesso: “Comitê do cliente”, 2 falas, 2 pessoas." in page
    # Lê o que foi gravado.
    with sessions() as session:
        # O item.
        item = session.scalars(select(BronzeItem)).one()
        # Fonte, ligação ao evento e título vindos da agenda.
        assert (item.source, item.thread_id, item.subject) == (
            SourceKind.MEET_TRANSCRIPT,
            "agenda:teams1",
            "Comitê do cliente",
        )
        # Horário do evento.
        assert item.occurred_at.isoformat() == "2026-11-02T12:00:00+00:00"
        # Detalhes.
        assert item.extra["plataforma"] == "teams"
        # Quem falou.
        assert item.extra["falantes"] == ["Ana Souza", "João Lima"]
        # Original guardado.
        assert item.extra["arquivo"] and item.extra["nome_arquivo"] == "Comite.vtt"
        # Registrado no diário.
        assert session.scalar(select(AuditLog.action)) == "transcricao_importada"
    # A página da reunião mostra os detalhes.
    detail = client.get(f"/conteudo/{item.id}", headers=OWNER).text
    # Plataforma e falantes.
    assert "Transcrição importada do Teams" in detail and "falaram: Ana Souza, João Lima" in detail
    # Importar o mesmo arquivo de novo não duplica.
    again = client.post(
        "/transcricoes/importar",
        data={"data": "2026-11-02", "evento": "teams1"},
        files={"arquivo": ("copia.vtt", TEAMS_VTT.encode(), "text/vtt")},
        headers=SAME,
    ).text
    # Aviso de que já existia.
    assert "já tinha sido importada" in again
    # Continua um item só.
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(BronzeItem)) == 1
    # A página inicial lista a transcrição.
    home = client.get("/", headers=OWNER).text
    # Botão e reunião.
    assert "Importar transcrição" in home and "Comitê do cliente" in home


# Reunião do Meet sem arquivo: a transcrição anexada é baixada do Drive.
def test_import_meet_transcript_from_drive(google, sessions):
    # Agenda e Drive falsos.
    _, drive = google
    # Reunião do Meet, sem arquivo: baixa a transcrição anexada.
    page = client.post(
        "/transcricoes/importar",
        data={"data": "2026-11-02", "evento": "meet1"},
        headers=SAME,
    ).text
    # Sucesso (documento: conta caracteres).
    assert "Transcrição carregada com sucesso: “Status semanal”" in page
    # Só a transcrição foi baixada (não as anotações do Gemini).
    assert drive.downloads == ["doc-transcricao"]
    # O item guarda a plataforma e os participantes da agenda.
    with sessions() as session:
        # O item.
        item = session.scalars(select(BronzeItem)).one()
        # Veio do Meet.
        assert item.extra["plataforma"] == "meet"
        # Participantes da agenda.
        assert item.extra["evento_agenda"]["participantes"] == ["Ana Souza", "joao@exemplo.com"]
        # Texto do documento.
        assert "Projeto Atlas" in item.body_text


# Reunião fora da agenda: exige o título e usa o dia escolhido.
def test_import_outside_calendar(google, sessions):
    # Fora da agenda e sem título: recusa.
    page = client.post(
        "/transcricoes/importar",
        data={"data": "2026-10-30", "evento": routes_import.OUTSIDE_CALENDAR},
        files={"arquivo": ("notas.txt", b"Ana: bom dia", "text/plain")},
        headers=SAME,
    ).text
    # Pede o título.
    assert "digite o título" in page
    # Com título: grava no dia escolhido.
    page = client.post(
        "/transcricoes/importar",
        data={
            "data": "2026-10-30",
            "evento": routes_import.OUTSIDE_CALENDAR,
            "titulo": "Conversa com fornecedor",
        },
        files={"arquivo": ("notas.txt", b"Ana: bom dia", "text/plain")},
        headers=SAME,
    ).text
    # Sucesso.
    assert "“Conversa com fornecedor”, 12 caracteres" in page
    # Sem evento: sem ligação, meia-noite do dia em Brasília (03:00 UTC).
    with sessions() as session:
        # O item.
        item = session.scalars(select(BronzeItem)).one()
        # Sem evento.
        assert item.thread_id is None and item.extra["evento_agenda"] is None
        # Dia escolhido.
        assert item.occurred_at.isoformat() == "2026-10-30T03:00:00+00:00"


# Erros do formulário: nada escolhido, sem arquivo e evento apagado.
@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"evento": ""}, "Escolha a reunião na lista."),
        ({"evento": "teams1"}, "Escolha o arquivo da transcrição."),
        ({"evento": "apagado"}, "não está mais na agenda"),
    ],
)
def test_import_form_errors(google, form, message):
    # Envia sem arquivo.
    page = client.post(
        "/transcricoes/importar", data={"data": "2026-11-02", **form}, headers=SAME
    ).text
    # Mostra o motivo.
    assert message in page


# Proteção contra pedido forjado por outro site.
def test_import_requires_same_origin():
    # POST sem a origem do console é recusado (proteção contra pedido forjado).
    response = client.post("/transcricoes/importar", data={"evento": "x"}, headers=OWNER)
    # 403 = proibido.
    assert response.status_code == 403


# Drive recusa a transcrição do Meet: mostra o motivo.
def test_meet_transcript_blocked(google):
    # Agenda e Drive falsos.
    _, drive = google
    # O Drive recusa o download (sem permissão no arquivo).
    drive.blocked["doc-transcricao"] = "insufficientFilePermissions"
    # Tenta importar a reunião do Meet sem arquivo.
    page = client.post(
        "/transcricoes/importar", data={"data": "2026-11-02", "evento": "meet1"}, headers=SAME
    ).text
    # Mostra o motivo, sem gravar nada.
    assert "Não consegui baixar a transcrição do Meet" in page


# --- Dia de calendário (página inicial e página Reuniões) ----------------------------


# Posição, altura e colunas das reuniões no dia de calendário.
def test_build_day_positions_and_lanes():
    # Agenda: 7h30 (antes da grade padrão), duas sobrepostas às 10h e uma de 5 minutos.
    calendar = FakeCalendar(
        [
            # Começa antes das 8h: a grade tem de começar às 7h.
            make_event("cedo", "Cedo", "2026-11-02T07:30:00-03:00", "2026-11-02T08:00:00-03:00"),
            # Duas no mesmo horário: lado a lado.
            make_event("a", "A", "2026-11-02T10:00:00-03:00", "2026-11-02T11:30:00-03:00"),
            # Começa no meio da A.
            make_event("b", "B", "2026-11-02T10:30:00-03:00", "2026-11-02T11:00:00-03:00"),
            # Começa quando a B acaba, ainda durante a A: volta para a coluna da B.
            make_event("c", "C", "2026-11-02T11:00:00-03:00", "2026-11-02T11:05:00-03:00"),
        ]
    )
    # Reuniões do dia.
    events = events_on(calendar, date(2026, 11, 2), "America/Sao_Paulo")
    # "Agora" fixo: 02/11 às 12h de Brasília.
    now = datetime.fromisoformat("2026-11-02T12:00:00-03:00")
    # Monta o dia, com a reunião A já tendo transcrição (item 42).
    day = build_day(events, {"a": 42}, date(2026, 11, 2), "America/Sao_Paulo", now=now)
    # A grade começa às 7h (por causa da reunião cedo) e vai até as 19h (padrão).
    assert (day.start_hour, day.end_hour) == (7, 19)
    # Blocos por id.
    blocks = {b.event.id: b for b in day.blocks}
    # A reunião cedo fica a 30 minutos do topo e tem 30 de altura.
    assert (blocks["cedo"].top, blocks["cedo"].height) == (30, 30)
    # A e B dividem duas colunas; C volta para a segunda coluna (a B já acabou).
    assert [(blocks[k].lane, blocks[k].lanes) for k in "abc"] == [(0, 2), (1, 2), (1, 2)]
    # A de 5 minutos ganha a altura mínima.
    assert blocks["c"].height == 25
    # Situação: A ligada à transcrição 42; as outras sem transcrição.
    assert (blocks["a"].status, blocks["a"].item_id, blocks["b"].status) == (
        "ligada",
        42,
        "pendente",
    )
    # A linha do "agora" fica a 5 horas do topo (12h - 7h) e o dia é hoje.
    assert day.now == 300 and day.today
    # Um resumo: uma reunião com transcrição.
    assert day.linked == 1
    # Outro dia: sem linha do "agora".
    assert build_day([], {}, date(2026, 11, 3), "America/Sao_Paulo", now=now).now is None


# O calendário da página Reuniões mostra a situação de cada reunião e leva para o lugar certo.
def test_agenda_links(google, sessions):
    # Uma transcrição já ligada à reunião do Teams.
    with sessions() as s:
        # Item de transcrição com o thread_id que a importação grava.
        item = BronzeItem(
            source=SourceKind.MEET_TRANSCRIPT,
            external_id="ligada-1",
            thread_id="agenda:teams1",
            subject="Comitê do cliente",
            content_sha256="0" * 64,
        )
        # Grava.
        s.add(item)
        # Confirma.
        s.commit()
        # Id gravado.
        item_id = item.id
    # A página Reuniões no dia 02/11.
    card = client.get("/transcricoes/importar?data=2026-11-02", headers=OWNER).text
    # A do Teams abre a transcrição já ligada.
    assert f'href="/conteudo/{item_id}"' in card and "transcrição ligada" in card
    # A do Meet leva para a importação com a reunião já escolhida.
    assert "/transcricoes/importar?data=2026-11-02&evento=meet1" in card
    # E diz que a transcrição do Meet está pronta para baixar.
    assert "transcrição do Meet pronta" in card
    # Resumo no topo do calendário.
    assert "2 reunião(ões) · 1 com transcrição" in card
    # A página de importação aberta por esse link já deixa a reunião escolhida.
    page = client.get("/transcricoes/importar?data=2026-11-02&evento=meet1", headers=OWNER).text
    # A opção da reunião do Meet vem marcada.
    assert '<option value="meet1" selected>' in page
    # Trocar a data devolve a lista e o calendário do novo dia (troca "fora da faixa").
    part = client.get("/transcricoes/importar/reunioes?data=2026-11-03", headers=OWNER).text
    # O calendário vem marcado para ir para o lugar dele.
    assert 'id="agenda-dia" hx-swap-oob="true"' in part and "Amanhã" in part
