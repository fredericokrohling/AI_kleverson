"""Testes do Início ao vivo: duas reuniões ao mesmo tempo, participantes e falantes.

O serviço de transcrição é falso (devolve como texto o próprio "áudio" recebido), e os
avisos para o console são capturados numa lista em vez de irem para um canal de verdade.
"""

# asyncio: o distribuidor de avisos usa filas assíncronas.
import asyncio

# threading: publica um aviso de outra thread, como fazem as rotas comuns.
import threading

# Datas e intervalos, para testar quando uma reunião sai do Início.
from datetime import UTC, datetime, timedelta

# Biblioteca de testes.
import pytest

# Cliente que simula o navegador e a extensão.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import select

# O app.
from app.main import app

# Tabelas.
from app.models import (
    BronzeItem,
    LiveMeeting,
    MeetingParticipant,
    MeetingSpeaker,
    OAuthToken,
    SourceKind,
    TranscriptSegment,
    Utterance,
)

# Cadastro de pessoas.
from app.projects import save_person

# Módulos trocados nos testes.
from app.transcription import calendar as calendar_module
from app.transcription import live, routes, routes_live

# Formato de trecho do serviço de transcrição.
from app.transcription.providers import Segment

# Agenda falsa.
from tests.fakes import EMPRESA, FakeCalendar

# Cabeçalho do dono.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Pedido do próprio console (POST exige a origem).
SAME = {**OWNER, "Origin": "http://testserver"}
# Duas capturas, uma em cada aba do Meet.
REUNIAO_A = "aaaaaaaa-0000-4000-8000-000000000001"
REUNIAO_B = "bbbbbbbb-0000-4000-8000-000000000002"
# Cliente compartilhado.
client = TestClient(app)
# Quantos "estou vivo" os serviços falsos receberam.
KEEP_ALIVES: list[int] = []


# Serviço falso: cada bloco de "áudio" vira um trecho definitivo com aquele texto.
class EchoStream:
    # Nome gravado nos trechos.
    name = "falso"

    # Fila de trechos.
    def __init__(self):
        # Fila assíncrona.
        self.queue: asyncio.Queue = asyncio.Queue()

    # Abrir: nada a fazer.
    async def __aenter__(self):
        return self

    # Fechar: nada a fazer.
    async def __aexit__(self, *exc):
        pass

    # O "áudio" é o texto, em UTF-8; o falante é o 1 (vira "Falante 2").
    async def send_audio(self, chunk):
        # Rascunho e definitivo.
        await self.queue.put(Segment(chunk.decode()[:3], False, 1, 0, 100))
        # Definitivo.
        await self.queue.put(Segment(chunk.decode(), True, 1, 0, 900))

    # Fim do áudio.
    async def finish(self):
        await self.queue.put(None)

    # "Estou vivo" (microfone mudo): conta, e devolve um rascunho para o teste esperar.
    async def keep_alive(self):
        # Conta na lista compartilhada.
        KEEP_ALIVES.append(1)
        # Rascunho vazio, só para o teste saber que chegou.
        await self.queue.put(Segment("", False, None, 0, 0))

    # Entrega os trechos até o fim.
    async def results(self):
        while (segment := await self.queue.get()) is not None:
            yield segment


# Troca o serviço real pelo falso (um novo por conexão) e captura os avisos.
@pytest.fixture
def ao_vivo(monkeypatch):
    # Avisos publicados.
    events: list[dict] = []
    # Cada conexão ganha o seu serviço falso.
    monkeypatch.setattr(routes, "create_provider", lambda settings: EchoStream())
    # Em vez de mandar para o console, guarda na lista.
    monkeypatch.setattr(live.hub, "publish", events.append)
    # Entrega a lista.
    return events


# Manda um texto como "áudio" numa captura e espera o trecho voltar.
def _falar(sessao: str, canal: str, texto: str) -> None:
    # Conecta como a extensão.
    with client.websocket_connect(
        f"/ws/transcricao?sessao={sessao}&canal={canal}", headers=OWNER
    ) as ws:
        # Envia.
        ws.send_bytes(texto.encode())
        # Rascunho.
        ws.receive_json()
        # Definitivo (já gravado quando chega aqui).
        ws.receive_json()


# --- Distribuidor de avisos ---------------------------------------------------------


# Um aviso publicado de outra thread chega à fila de quem ouve, no formato do SSE.
def test_hub_delivers_across_threads():
    # Distribuidor novo (não o do app).
    hub = live.LiveHub()

    # Ouvinte dentro de um laço assíncrono, como a rota do canal de eventos.
    async def ouvir():
        # Entra.
        queue = hub.subscribe()
        # Publica de outra thread (como uma rota comum do FastAPI).
        threading.Thread(target=hub.publish, args=({"tipo": "reunioes"},)).start()
        # Recebe.
        event = await asyncio.wait_for(queue.get(), timeout=2)
        # Sai.
        hub.unsubscribe(queue)
        # Devolve.
        return event

    # Roda o laço.
    assert asyncio.run(ouvir()) == {"tipo": "reunioes"}
    # Ninguém mais ouvindo.
    assert len(hub) == 0
    # Formato do SSE: tipo, JSON com acentos legíveis e linha vazia no fim.
    assert live.format_event({"tipo": "trecho", "texto": "Olá"}) == (
        'event: trecho\ndata: {"tipo": "trecho", "texto": "Olá"}\n\n'
    )


# --- Duas reuniões ao mesmo tempo ---------------------------------------------------


# Duas capturas não se misturam: trechos, reuniões e avisos separados pelo id.
def test_two_meetings_do_not_mix(ao_vivo, sessions):
    # A extensão avisa o começo de cada uma, com o título da aba.
    for sessao, titulo in [(REUNIAO_A, "Status Atlas - Google Meet"), (REUNIAO_B, "Comitê CRM")]:
        # Aviso de início.
        assert (
            client.post(
                f"/transcricoes/{sessao}/iniciar",
                json={"titulo": titulo, "url_reuniao": "https://meet.google.com/abc-defg-hij"},
                headers=OWNER,
            ).status_code
            == 200
        )
    # Fala em cada reunião, intercalando.
    _falar(REUNIAO_A, "outros", "cronograma do Atlas")
    _falar(REUNIAO_B, "outros", "orçamento do CRM")
    _falar(REUNIAO_A, "voce", "eu envio o plano")
    # No banco, cada trecho ficou na sua reunião.
    with sessions() as s:
        # Textos por reunião.
        by_session = {
            sessao: [
                t.text
                for t in s.scalars(
                    select(TranscriptSegment).where(TranscriptSegment.session_id == sessao)
                )
            ]
            for sessao in (REUNIAO_A, REUNIAO_B)
        }
        # Duas reuniões, com o título sem o " - Google Meet".
        titles = sorted(m.title for m in s.scalars(select(LiveMeeting)))
    # Separados.
    assert by_session == {
        REUNIAO_A: ["cronograma do Atlas", "eu envio o plano"],
        REUNIAO_B: ["orçamento do CRM"],
    }
    # Títulos.
    assert titles == ["Comitê CRM", "Status Atlas"]
    # Os avisos de trecho levam o id da reunião certa.
    trechos = [(e["sessao"], e["texto"]) for e in ao_vivo if e["tipo"] == "trecho" and e["final"]]
    # Na ordem em que chegaram.
    assert trechos == [
        (REUNIAO_A, "cronograma do Atlas"),
        (REUNIAO_B, "orçamento do CRM"),
        (REUNIAO_A, "eu envio o plano"),
    ]
    # O Início mostra as duas áreas, cada uma com as suas falas.
    page = client.get("/", headers=OWNER).text
    # Áreas.
    assert f'data-sessao="{REUNIAO_A}"' in page and f'data-sessao="{REUNIAO_B}"' in page
    # Nenhuma área livre sobrando (já são duas reuniões).
    assert "Área livre" not in page
    # O pedaço que o Início pede de novo quando uma reunião começa.
    section = client.get("/ao-vivo", headers=OWNER).text
    # A fala de cada uma está lá.
    assert "cronograma do Atlas" in section and "orçamento do CRM" in section


# Sem reunião, o Início mostra as duas áreas livres; reunião salva há muito tempo some.
def test_home_areas_and_visibility(sessions):
    # Sem reunião.
    page = client.get("/", headers=OWNER).text
    # Duas áreas livres.
    assert page.count('class="reuniao-viva vazia"') == 2
    # O script do canal de eventos.
    assert '<script src="/static/ao-vivo.js" defer></script>' in page
    # Agora.
    now = datetime.now(UTC)
    # Reuniões: no ar, salva há 10 min, salva há 2 h, encerrada sem Empresa há 2 h (fica
    # esperando a escolha) e uma abandonada há 5 h.
    with sessions() as s:
        # Transcrições salvas das duas que já foram para o banco.
        salvas = [
            BronzeItem(
                company_id=EMPRESA,
                source=SourceKind.MEET_TRANSCRIPT,
                external_id=f"reuniao:{n}",
                content_sha256=n,
            )
            for n in ("a", "b")
        ]
        # Prepara.
        s.add_all(salvas)
        # Gera os ids.
        s.flush()
        # No ar.
        s.add(LiveMeeting(session_id="no-ar-0001", title="No ar", last_activity_at=now))
        # Salva há pouco.
        s.add(
            LiveMeeting(
                session_id="salva-0001",
                title="Salva agora",
                finished_at=now - timedelta(minutes=10),
                company_id=EMPRESA,
                bronze_item_id=salvas[0].id,
            )
        )
        # Salva há muito.
        s.add(
            LiveMeeting(
                session_id="salva-0002",
                title="Salva antes",
                finished_at=now - timedelta(hours=2),
                company_id=EMPRESA,
                bronze_item_id=salvas[1].id,
            )
        )
        # Encerrada sem Empresa: não foi para o banco; continua no Início.
        s.add(
            LiveMeeting(
                session_id="pendente-01", title="Sem Empresa", finished_at=now - timedelta(hours=2)
            )
        )
        # Abandonada.
        s.add(
            LiveMeeting(
                session_id="velha-0001",
                title="Abandonada",
                last_activity_at=now - timedelta(hours=5),
            )
        )
        # Grava.
        s.commit()
        # Visíveis.
        visible = [m.title for m in live.visible_meetings(s)]
        # Situações.
        statuses = {m.title: live.status(m) for m in live.visible_meetings(s)}
    # As duas recentes e a que espera a Empresa.
    assert sorted(visible) == ["No ar", "Salva agora", "Sem Empresa"]
    # Situação de cada uma.
    assert statuses == {"No ar": "ao_vivo", "Salva agora": "salva", "Sem Empresa": "pendente"}


# --- Participantes e falantes -------------------------------------------------------


# Uma reunião com uma fala do "Falante 2" e uma sua; devolve o id da reunião.
def _reuniao(ao_vivo, sessions) -> int:
    # Começa.
    client.post(f"/transcricoes/{REUNIAO_A}/iniciar", json={"titulo": "Status"}, headers=OWNER)
    # Sem agenda, a Empresa não foi descoberta: você escolhe no Início.
    client.post(f"/ao-vivo/{REUNIAO_A}/empresa", data={"empresa": EMPRESA}, headers=SAME)
    # O outro lado fala.
    _falar(REUNIAO_A, "outros", "o marco atrasou")
    # Você responde.
    _falar(REUNIAO_A, "voce", "vou replanejar")
    # Id interno.
    with sessions() as s:
        return live.get_meeting(s, REUNIAO_A).id


# Nome único na reunião (sem diferenciar maiúscula e acento), e-mail liga à Pessoa.
def test_participants_unique_name_and_email_key(ao_vivo, sessions):
    # Reunião.
    _reuniao(ao_vivo, sessions)
    # Uma pessoa cadastrada.
    with sessions() as s:
        # Márcia, com e-mail.
        marcia = save_person(
            s, None, name="Márcia Lopes", email="marcia@cliente.com", company_id=EMPRESA
        ).id
        # Grava.
        s.commit()
    # Endereço do bloco.
    url = f"/ao-vivo/{REUNIAO_A}/participantes"
    # Da página Pessoas.
    page = client.post(url, data={"pessoa": str(marcia)}, headers=SAME).text
    # Incluída, marcada como Pessoa.
    assert "“Márcia Lopes” incluído." in page and ">Pessoa</span>" in page
    # Só nome e e-mail (pessoa que não está em Pessoas).
    page = client.post(url, data={"nome": "João  Lima", "email": "JOAO@x.com"}, headers=SAME).text
    # Incluído.
    assert "“João Lima” incluído." in page
    # O mesmo nome com outra grafia é recusado.
    page = client.post(url, data={"nome": "joao lima"}, headers=SAME).text
    # Mensagem.
    assert "Já existe um participante chamado “joao lima” nesta reunião." in page
    # O mesmo e-mail também.
    page = client.post(url, data={"nome": "Outro", "email": "joao@x.com"}, headers=SAME).text
    # Mensagem.
    assert "O e-mail joao@x.com já é de outro participante" in page
    # Sem nome.
    assert "Informe o nome do participante." in client.post(url, data={}, headers=SAME).text
    # Um participante escrito à mão com o e-mail de uma Pessoa liga à Pessoa.
    with sessions() as s:
        # Beto cadastrado depois.
        beto = save_person(
            s, None, name="Roberto Lima", email="beto@cliente.com", company_id=EMPRESA
        ).id
        # Grava.
        s.commit()
    # Escrito à mão, com o e-mail dele.
    client.post(url, data={"nome": "Beto", "email": "beto@cliente.com"}, headers=SAME)
    # Confere.
    with sessions() as s:
        # Participantes por nome.
        people = {
            p.name: (p.email, p.stakeholder_id) for p in s.scalars(select(MeetingParticipant))
        }
    # Três, e-mails em minúsculas, Márcia e Beto ligados às Pessoas.
    assert people == {
        "Márcia Lopes": ("marcia@cliente.com", marcia),
        "João Lima": ("joao@x.com", None),
        "Beto": ("beto@cliente.com", beto),
    }
    # A lista de Pessoas do formulário não oferece quem já está na reunião.
    page = client.get(url, headers=OWNER).text
    # Márcia e Beto saem da lista.
    assert f'<option value="{marcia}">' not in page and f'<option value="{beto}">' not in page


# Dizer quem é cada voz: vale para as falas, vai para o banco e refaz depois de salva.
def test_assign_speakers_and_finalize(ao_vivo, sessions):
    # Reunião.
    _reuniao(ao_vivo, sessions)
    # Endereço base.
    base = f"/ao-vivo/{REUNIAO_A}"
    # Uma Pessoa e um participante sem cadastro.
    with sessions() as s:
        # Márcia.
        marcia = save_person(
            s, None, name="Márcia Lopes", email="marcia@cliente.com", company_id=EMPRESA
        ).id
        # Grava.
        s.commit()
    # Márcia e você (Natália) na reunião.
    client.post(f"{base}/participantes", data={"pessoa": str(marcia)}, headers=SAME)
    client.post(f"{base}/participantes", data={"nome": "Natália"}, headers=SAME)
    # Ids.
    with sessions() as s:
        # Participantes por nome.
        ids = {p.name: p.id for p in s.scalars(select(MeetingParticipant))}
    # O bloco lista os falantes que já falaram, com "Você" primeiro.
    block = client.get(f"{base}/participantes", headers=OWNER).text
    # Ordem: Você, depois Falante 2.
    assert block.index("Você <span") < block.index("Falante 2 <span")
    # Falante 2 é a Márcia.
    client.post(
        f"{base}/falantes",
        data={"canal": "outros", "rotulo": "Falante 2", "participante": str(ids["Márcia Lopes"])},
        headers=SAME,
    )
    # Você é a Natália.
    client.post(
        f"{base}/falantes",
        data={"canal": "voce", "rotulo": "Você", "participante": str(ids["Natália"])},
        headers=SAME,
    )
    # O Início recebeu os nomes novos desta reunião.
    assert ao_vivo[-1] == {
        "tipo": "falantes",
        "sessao": REUNIAO_A,
        "nomes": {"outros|Falante 2": "Márcia Lopes", "voce|Você": "Natália"},
    }
    # A área no Início já mostra os nomes nas falas.
    section = client.get("/ao-vivo", headers=OWNER).text
    # Márcia no lugar do Falante 2.
    assert '<b class="quem">Márcia Lopes</b><span>o marco atrasou</span>' in section
    # A extensão salva.
    client.post(f"/transcricoes/{REUNIAO_A}/finalizar", json={}, headers=OWNER)
    # Confere o banco.
    with sessions() as s:
        # Transcrição.
        item = s.scalars(select(BronzeItem)).one()
        # Falas.
        falas = [
            (u.speaker, u.participant_id, u.stakeholder_id)
            for u in s.scalars(select(Utterance).order_by(Utterance.position))
        ]
        # Reunião salva.
        meeting = live.get_meeting(s, REUNIAO_A)
    # Texto com os nomes.
    assert (
        "Márcia Lopes: o marco atrasou" in item.body_text
        and "Natália: vou replanejar" in item.body_text
    )
    # Título guardado no começo.
    assert item.subject == "Status"
    # Falas com participante; a da Márcia também com a Pessoa.
    assert falas == [
        ("Márcia Lopes", ids["Márcia Lopes"], marcia),
        ("Natália", ids["Natália"], None),
    ]
    # Reunião marcada como salva, apontando para a transcrição.
    assert meeting.finished_at is not None and meeting.bronze_item_id == item.id
    # Depois de salva, tirar a ligação do Falante 2 refaz a transcrição salva.
    client.post(f"{base}/falantes", data={"canal": "outros", "rotulo": "Falante 2"}, headers=SAME)
    # Confere.
    with sessions() as s:
        # Texto refeito.
        body = s.scalars(select(BronzeItem)).one().body_text
    # Voltou o rótulo.
    assert "Falante 2: o marco atrasou" in body and "Natália: vou replanejar" in body
    # Tirar a Natália da reunião desfaz a ligação dela.
    client.post(f"{base}/participantes/{ids['Natália']}/remover", headers=SAME)
    # Nenhuma ligação sobrando.
    with sessions() as s:
        # Ligações.
        assert s.scalars(select(MeetingSpeaker)).all() == []


# Participante sem Pessoa: cadastrar a Pessoa depois, com o mesmo e-mail, liga as falas.
def test_person_created_later_links_by_email(ao_vivo, sessions):
    # Reunião.
    _reuniao(ao_vivo, sessions)
    # Endereço base.
    base = f"/ao-vivo/{REUNIAO_A}"
    # Participante só da reunião.
    client.post(
        f"{base}/participantes", data={"nome": "Carla", "email": "carla@cliente.com"}, headers=SAME
    )
    # Id.
    with sessions() as s:
        # O participante.
        carla = s.scalars(select(MeetingParticipant)).one().id
    # Carla é o Falante 2.
    client.post(
        f"{base}/falantes",
        data={"canal": "outros", "rotulo": "Falante 2", "participante": str(carla)},
        headers=SAME,
    )
    # Salva.
    client.post(f"/transcricoes/{REUNIAO_A}/finalizar", json={}, headers=OWNER)
    # Cadastra a Carla em Pessoas, com o mesmo e-mail.
    client.post(
        "/cadastros/pessoas",
        data={"nome": "Carla Dias", "email": "carla@cliente.com", "empresa": EMPRESA},
        headers=SAME,
    )
    # Confere.
    with sessions() as s:
        # A pessoa.
        person = s.scalars(select(MeetingParticipant)).one().stakeholder_id
        # A fala dela.
        fala = s.scalars(select(Utterance).where(Utterance.participant_id == carla)).one()
    # Ligados.
    assert person is not None and fala.stakeholder_id == person


# "Trazer convidados da agenda": acha o evento pelo endereço do Meet.
def test_invitees_from_calendar(ao_vivo, sessions, monkeypatch):
    # A reunião começa agora (o evento da agenda é de hoje).
    start = datetime.now(UTC)
    # Agenda com o evento daquele endereço.
    calendar = FakeCalendar(
        [
            {
                "id": "ev1",
                "summary": "Status semanal",
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
                "hangoutLink": "https://meet.google.com/abc-defg-hij",
                "attendees": [
                    {"email": "ana@cliente.com", "displayName": "Ana Souza"},
                    {"email": "joao@cliente.com"},
                    {"email": "sala@recurso.com", "resource": True},
                ],
            }
        ]
    )
    # A rota de convidados e a descoberta da Empresa usam a agenda falsa.
    monkeypatch.setattr(routes_live, "calendar_client", lambda db, settings, *_a: calendar)
    # A descoberta importa a função na hora; troca no módulo da agenda.
    monkeypatch.setattr(calendar_module, "calendar_client", lambda db, settings, *_a: calendar)
    # A Empresa tem conta Google (só essas agendas são consultadas).
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
    # Começa com o endereço do Meet.
    response = client.post(
        f"/transcricoes/{REUNIAO_A}/iniciar",
        json={"titulo": "Status", "url_reuniao": "https://meet.google.com/abc-defg-hij?authuser=0"},
        headers=OWNER,
    )
    # A Empresa veio da agenda: o endereço do Meet está num evento dela.
    assert response.json()["empresa"] == "Empresa Teste"
    # Pede os convidados.
    page = client.post(f"/ao-vivo/{REUNIAO_A}/convidados", headers=SAME).text
    # Dois convidados (a sala fica de fora).
    assert "2 convidado(s) de “Status semanal” incluído(s)." in page
    # Pedir de novo não duplica.
    assert "0 convidado(s)" in client.post(f"/ao-vivo/{REUNIAO_A}/convidados", headers=SAME).text
    # Nomes: o da agenda, ou o e-mail quando a agenda não sabe o nome.
    with sessions() as s:
        # Nomes.
        assert sorted(p.name for p in s.scalars(select(MeetingParticipant))) == [
            "Ana Souza",
            "joao@cliente.com",
        ]


# As rotas novas exigem o dono, e os POSTs exigem a origem do console.
def test_live_routes_require_owner(ao_vivo, sessions):
    # Reunião.
    _reuniao(ao_vivo, sessions)
    # Sem login.
    assert client.get("/ao-vivo").status_code == 403
    # Canal de eventos sem login.
    assert client.get("/ao-vivo/eventos").status_code == 403
    # POST sem a origem do console.
    assert (
        client.post(
            f"/ao-vivo/{REUNIAO_A}/participantes", data={"nome": "X"}, headers=OWNER
        ).status_code
        == 403
    )
    # Reunião que não existe.
    assert client.get("/ao-vivo/nao-existe-00/participantes", headers=OWNER).status_code == 404


# Microfone mudo: a extensão manda "manter" no lugar do áudio, e nada é gravado.
def test_muted_microphone_keeps_connection(ao_vivo, sessions):
    # Zera a contagem.
    KEEP_ALIVES.clear()
    # Conecta no canal do microfone.
    with client.websocket_connect(
        f"/ws/transcricao?sessao={REUNIAO_A}&canal=voce", headers=OWNER
    ) as ws:
        # "Estou vivo".
        ws.send_text("manter")
        # O serviço recebeu (o falso devolve um rascunho vazio).
        assert ws.receive_json()["final"] is False
    # Um "estou vivo" repassado.
    assert KEEP_ALIVES == [1]
    # Nada gravado.
    with sessions() as s:
        # Sem trechos.
        assert s.scalars(select(TranscriptSegment)).all() == []
