import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.models import BronzeItem, SourceKind, TranscriptSegment
from app.transcription import routes
from app.transcription.providers import Segment, parse_deepgram_message

OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
SESSION = "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90"
client = TestClient(app)


class FakeStream:
    name = "falso"

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.audio: list[bytes] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def send_audio(self, chunk):
        self.audio.append(chunk)
        await self.queue.put(Segment("bom dia", False, 1, 0, 400))
        await self.queue.put(Segment("Bom dia a todos.", True, 1, 0, 900))

    async def finish(self):
        await self.queue.put(None)

    async def results(self):
        while (segment := await self.queue.get()) is not None:
            yield segment


@pytest.fixture
def fake_stream(monkeypatch):
    stream = FakeStream()
    monkeypatch.setattr(routes, "create_provider", lambda settings: stream)
    return stream


def test_live_transcription_relays_and_saves_final(fake_stream, sessions):
    url = f"/ws/transcricao?sessao={SESSION}&canal=outros"
    with client.websocket_connect(url, headers=OWNER) as ws:
        ws.send_bytes(b"\x00\x01" * 800)
        interim = ws.receive_json()
        final = ws.receive_json()
    assert (interim["final"], interim["texto"]) == (False, "bom dia")
    assert (final["final"], final["falante"]) == (True, "Falante 2")
    assert fake_stream.audio == [b"\x00\x01" * 800]
    with sessions() as s:
        row = s.scalars(select(TranscriptSegment)).one()
        assert (row.channel, row.speaker, row.text, row.provider) == (
            "outros",
            "Falante 2",
            "Bom dia a todos.",
            "falso",
        )


def test_microphone_channel_is_you(fake_stream):
    with client.websocket_connect(
        f"/ws/transcricao?sessao={SESSION}&canal=voce", headers=OWNER
    ) as ws:
        ws.send_bytes(b"\x00\x00")
        ws.receive_json()
        assert ws.receive_json()["falante"] == "Você"


@pytest.mark.parametrize(
    "url,headers",
    [
        (f"/ws/transcricao?sessao={SESSION}&canal=outros", {}),
        (f"/ws/transcricao?sessao={SESSION}&canal=qualquer", OWNER),
        ("/ws/transcricao?sessao=../x&canal=outros", OWNER),
    ],
)
def test_live_transcription_rejects(url, headers, fake_stream):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers=headers) as ws:
            ws.receive_json()


def test_missing_key_is_reported(monkeypatch):
    def broken(settings):
        raise RuntimeError("DEEPGRAM_API_KEY não configurada.")

    monkeypatch.setattr(routes, "create_provider", broken)
    with client.websocket_connect(
        f"/ws/transcricao?sessao={SESSION}&canal=outros", headers=OWNER
    ) as ws:
        assert ws.receive_json() == {
            "tipo": "erro",
            "mensagem": "DEEPGRAM_API_KEY não configurada.",
        }


def _seed_audio(sessions):
    with sessions() as s:
        for channel, speaker, text, start in [
            ("outros", "Falante 1", "Vamos revisar o cronograma.", 2000),
            ("voce", "Você", "Combinado, eu envio até sexta.", 5000),
        ]:
            s.add(
                TranscriptSegment(
                    session_id=SESSION,
                    channel=channel,
                    provider="falso",
                    speaker=speaker,
                    text=text,
                    start_ms=start,
                )
            )
        s.commit()


def test_captions_and_finalize(sessions):
    _seed_audio(sessions)
    response = client.post(
        f"/transcricoes/{SESSION}/legendas",
        json={"trechos": [{"falante": "Ana", "texto": "vamos revisar", "inicio_ms": 1900}]},
        headers=OWNER,
    )
    assert response.json() == {"gravados": 1}

    body = {"titulo": "Status semanal", "url_reuniao": "https://meet.google.com/abc"}
    first = client.post(f"/transcricoes/{SESSION}/finalizar", json=body, headers=OWNER).json()
    again = client.post(f"/transcricoes/{SESSION}/finalizar", json=body, headers=OWNER).json()
    assert first == again

    with sessions() as s:
        item = s.scalars(select(BronzeItem)).one()
    assert item.source == SourceKind.MEET_TRANSCRIPT
    assert item.body_text == (
        "[00:02] Falante 1: Vamos revisar o cronograma.\n"
        "[00:05] Você: Combinado, eu envio até sexta."
    )
    assert item.extra["legendas_meet"] == "[00:01] Ana: vamos revisar"

    page = client.get(f"/transcricoes/{SESSION}", headers=OWNER)
    assert "Combinado, eu envio até sexta." in page.text and "vamos revisar" in page.text


def test_transcription_routes_require_owner():
    assert client.post(f"/transcricoes/{SESSION}/finalizar", json={}).status_code == 403
    assert client.get(f"/transcricoes/{SESSION}").status_code == 403


def test_parse_deepgram_result():
    raw = json.dumps(
        {
            "type": "Results",
            "start": 1.5,
            "duration": 2.0,
            "is_final": True,
            "channel": {
                "alternatives": [
                    {"transcript": "Olá, pessoal.", "words": [{"word": "olá", "speaker": 0}]}
                ]
            },
        }
    )
    assert parse_deepgram_message(raw) == Segment("Olá, pessoal.", True, 0, 1500, 3500)


def test_parse_deepgram_ignores_metadata_and_silence():
    assert parse_deepgram_message(json.dumps({"type": "Metadata"})) is None
    silence = {"type": "Results", "channel": {"alternatives": [{"transcript": ""}]}}
    assert parse_deepgram_message(json.dumps(silence)) is None
