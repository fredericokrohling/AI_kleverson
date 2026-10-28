"""Testes da transcrição ao vivo: WebSocket, gravação, legendas e finalização.

Nenhum teste chama o Deepgram de verdade. Um serviço falso (FakeStream) devolve
trechos prontos, assim os testes são grátis e não dependem de internet.
"""

# asyncio: a fila assíncrona usada pelo serviço falso.
import asyncio

# json: monta mensagens no formato do Deepgram.
import json

# Biblioteca de testes.
import pytest

# Cliente que simula navegador e extensão (inclusive WebSocket).
from fastapi.testclient import TestClient

# select monta consultas.
from sqlalchemy import select

# Erro levantado quando o servidor fecha o WebSocket.
from starlette.websockets import WebSocketDisconnect

# O app.
from app.main import app

# Tabelas consultadas para conferir as gravações.
from app.models import BronzeItem, SourceKind, TranscriptSegment

# Módulo de rotas (para trocar o serviço real pelo falso).
from app.transcription import routes

# Formato de trecho e a função que lê mensagens do Deepgram.
from app.transcription.providers import Segment, parse_deepgram_message

# Cabeçalho do dono.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Um id de sessão válido (formato UUID, como a extensão gera).
SESSION = "3f2a9c1e-5b7d-4e8a-9c21-7d4e5f6a8b90"
# Cliente compartilhado.
client = TestClient(app)


# Serviço de transcrição falso: cumpre o mesmo contrato do DeepgramStream.
class FakeStream:
    # Nome gravado nos trechos.
    name = "falso"

    # Prepara a fila de trechos e a lista de áudio recebido.
    def __init__(self):
        # Fila assíncrona: results() espera aqui até chegar algo.
        self.queue: asyncio.Queue = asyncio.Queue()
        # Áudio recebido, para o teste conferir.
        self.audio: list[bytes] = []

    # Abrir conexão: nada a fazer.
    async def __aenter__(self):
        # Devolve o próprio objeto.
        return self

    # Fechar conexão: nada a fazer.
    async def __aexit__(self, *exc):
        # Nada.
        pass

    # Ao receber áudio, "transcreve" com um rascunho e uma versão final.
    async def send_audio(self, chunk):
        # Guarda o áudio recebido.
        self.audio.append(chunk)
        # Rascunho (is_final=False), falante 1.
        await self.queue.put(Segment("bom dia", False, 1, 0, 400))
        # Versão definitiva do mesmo trecho.
        await self.queue.put(Segment("Bom dia a todos.", True, 1, 0, 900))

    # Fim do áudio: None na fila sinaliza o fim dos resultados.
    async def finish(self):
        # Coloca o marcador de fim.
        await self.queue.put(None)

    # Entrega os trechos da fila até encontrar None.
    async def results(self):
        # ":=" lê da fila e testa ao mesmo tempo.
        while (segment := await self.queue.get()) is not None:
            # Entrega o trecho.
            yield segment


# Fixture: troca o serviço real pelo falso durante o teste.
@pytest.fixture
def fake_stream(monkeypatch):
    # Um serviço falso novo.
    stream = FakeStream()
    # routes.create_provider passa a devolver o falso.
    monkeypatch.setattr(routes, "create_provider", lambda settings: stream)
    # Entrega o falso ao teste, para inspeção.
    return stream


# O servidor repassa rascunho e versão final, e grava só a final.
def test_live_transcription_relays_and_saves_final(fake_stream, sessions):
    # Endereço do WebSocket no canal "outros".
    url = f"/ws/transcricao?sessao={SESSION}&canal=outros"
    # Abre a conexão como dona.
    with client.websocket_connect(url, headers=OWNER) as ws:
        # Envia 1600 bytes de "áudio".
        ws.send_bytes(b"\x00\x01" * 800)
        # Primeira mensagem de volta: o rascunho.
        interim = ws.receive_json()
        # Segunda: a versão final.
        final = ws.receive_json()
    # Rascunho com o texto provisório.
    assert (interim["final"], interim["texto"]) == (False, "bom dia")
    # Final com o falante numerado (1 vira "Falante 2", porque a contagem começa em 0).
    assert (final["final"], final["falante"]) == (True, "Falante 2")
    # O áudio chegou intacto ao serviço.
    assert fake_stream.audio == [b"\x00\x01" * 800]
    # Confere o banco.
    with sessions() as s:
        # Só um trecho gravado (o rascunho não é gravado).
        row = s.scalars(select(TranscriptSegment)).one()
        # Com os dados certos.
        assert (row.channel, row.speaker, row.text, row.provider) == (
            "outros",
            "Falante 2",
            "Bom dia a todos.",
            "falso",
        )


# O canal do microfone sempre aparece como "Você".
def test_microphone_channel_is_you(fake_stream):
    # Abre no canal "voce".
    with client.websocket_connect(
        f"/ws/transcricao?sessao={SESSION}&canal=voce", headers=OWNER
    ) as ws:
        # Envia um áudio mínimo.
        ws.send_bytes(b"\x00\x00")
        # Descarta o rascunho.
        ws.receive_json()
        # A versão final vem como "Você".
        assert ws.receive_json()["falante"] == "Você"


# Conexões recusadas: sem login, canal inválido, id malicioso.
@pytest.mark.parametrize(
    "url,headers",
    [
        # Sem cabeçalho de login.
        (f"/ws/transcricao?sessao={SESSION}&canal=outros", {}),
        # Canal que não existe.
        (f"/ws/transcricao?sessao={SESSION}&canal=qualquer", OWNER),
        # Id de sessão tentando "subir de pasta".
        ("/ws/transcricao?sessao=../x&canal=outros", OWNER),
    ],
)
def test_live_transcription_rejects(url, headers, fake_stream):
    # Espera que o servidor feche a conexão.
    with pytest.raises(WebSocketDisconnect):
        # Tenta conectar.
        with client.websocket_connect(url, headers=headers) as ws:
            # Tenta ler algo.
            ws.receive_json()


# Sem chave do Deepgram, o painel recebe uma mensagem clara.
def test_missing_key_is_reported(monkeypatch):
    # Fábrica que falha como se a chave estivesse vazia.
    def broken(settings):
        # Mesmo erro que o DeepgramStream levanta.
        raise RuntimeError("DEEPGRAM_API_KEY não configurada.")

    # Usa a fábrica quebrada.
    monkeypatch.setattr(routes, "create_provider", broken)
    # Conecta.
    with client.websocket_connect(
        f"/ws/transcricao?sessao={SESSION}&canal=outros", headers=OWNER
    ) as ws:
        # Recebe o erro em formato que o painel mostra.
        assert ws.receive_json() == {
            "tipo": "erro",
            "mensagem": "DEEPGRAM_API_KEY não configurada.",
        }


# Grava dois trechos de áudio de exemplo no banco.
def _seed_audio(sessions):
    # Abre uma sessão.
    with sessions() as s:
        # Para cada trecho de exemplo (canal, falante, texto, início em ms)...
        for channel, speaker, text, start in [
            ("outros", "Falante 1", "Vamos revisar o cronograma.", 2000),
            ("voce", "Você", "Combinado, eu envio até sexta.", 5000),
        ]:
            # ...prepara a gravação.
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
        # Grava.
        s.commit()


# Fluxo completo: legendas, finalização (duas vezes) e página de comparação.
def test_captions_and_finalize(sessions):
    # Prepara o áudio.
    _seed_audio(sessions)
    # Envia uma linha de legenda do Meet.
    response = client.post(
        f"/transcricoes/{SESSION}/legendas",
        json={"trechos": [{"falante": "Ana", "texto": "vamos revisar", "inicio_ms": 1900}]},
        headers=OWNER,
    )
    # Uma linha gravada.
    assert response.json() == {"gravados": 1}

    # Dados da finalização.
    body = {"titulo": "Status semanal", "url_reuniao": "https://meet.google.com/abc"}
    # Finaliza uma vez...
    first = client.post(f"/transcricoes/{SESSION}/finalizar", json=body, headers=OWNER).json()
    # ...e de novo.
    again = client.post(f"/transcricoes/{SESSION}/finalizar", json=body, headers=OWNER).json()
    # Mesmo item do bronze nas duas vezes (não duplica).
    assert first == again

    # Confere o item no bronze.
    with sessions() as s:
        # Exatamente um item.
        item = s.scalars(select(BronzeItem)).one()
    # Fonte correta.
    assert item.source == SourceKind.MEET_TRANSCRIPT
    # Texto em ordem de tempo, no formato "[mm:ss] Falante: texto".
    assert item.body_text == (
        "[00:02] Falante 1: Vamos revisar o cronograma.\n"
        "[00:05] Você: Combinado, eu envio até sexta."
    )
    # As legendas ficam guardadas à parte, para comparação.
    assert item.extra["legendas_meet"] == "[00:01] Ana: vamos revisar"

    # A página de comparação mostra as duas versões.
    page = client.get(f"/transcricoes/{SESSION}", headers=OWNER)
    # Os dois textos aparecem.
    assert "Combinado, eu envio até sexta." in page.text and "vamos revisar" in page.text


# As rotas HTTP da transcrição também exigem o dono.
def test_transcription_routes_require_owner():
    # Finalizar sem login: 403.
    assert client.post(f"/transcricoes/{SESSION}/finalizar", json={}).status_code == 403
    # Ver a página sem login: 403.
    assert client.get(f"/transcricoes/{SESSION}").status_code == 403


# Leitura de uma mensagem real do Deepgram.
def test_parse_deepgram_result():
    # Mensagem no formato da documentação do Deepgram.
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
    # 1,5 s vira 1500 ms; o fim é 1,5 + 2,0 = 3,5 s = 3500 ms.
    assert parse_deepgram_message(raw) == Segment("Olá, pessoal.", True, 0, 1500, 3500)


# Metadados e silêncio são ignorados.
def test_parse_deepgram_ignores_metadata_and_silence():
    # Mensagem de metadados: nada.
    assert parse_deepgram_message(json.dumps({"type": "Metadata"})) is None
    # Resultado com texto vazio (silêncio).
    silence = {"type": "Results", "channel": {"alternatives": [{"transcript": ""}]}}
    # Também nada.
    assert parse_deepgram_message(json.dumps(silence)) is None
