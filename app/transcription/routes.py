"""Rotas da transcrição: áudio ao vivo (WebSocket), legendas do Meet e finalização."""

import asyncio
import logging
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.config import get_settings
from app.db import get_session, get_sessionmaker
from app.security import owner_login, require_owner
from app.transcription import store
from app.transcription.providers import create_provider

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

Owner = Annotated[str, Depends(require_owner)]
DB = Annotated[Session, Depends(get_session)]

# Identificador da sessão de captura, gerado pela extensão (UUID).
_SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _check_session_id(session_id: str) -> None:
    if not _SESSION_ID.match(session_id):
        raise HTTPException(status_code=400, detail="Identificador de sessão inválido")


@router.websocket("/ws/transcricao")
async def live_transcription(websocket: WebSocket, sessao: str, canal: str) -> None:
    """A extensão envia áudio PCM 16 kHz; devolvemos os trechos de texto ao vivo."""
    actor = owner_login(websocket.headers)
    if actor is None or canal not in store.AUDIO_CHANNELS or not _SESSION_ID.match(sessao):
        await websocket.close(code=1008)
        return
    await websocket.accept()

    sessions = get_sessionmaker()
    try:
        provider = create_provider(get_settings())
        stream = await provider.__aenter__()
    except Exception as exc:  # chave ausente, serviço fora do ar
        log.exception("Falha ao abrir o serviço de transcrição")
        await websocket.send_json({"tipo": "erro", "mensagem": str(exc)})
        await websocket.close(code=1011)
        return

    async def upstream() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes"):
                    await stream.send_audio(message["bytes"])
        except WebSocketDisconnect:
            pass
        finally:
            await stream.finish()

    async def downstream() -> None:
        async for segment in stream.results():
            speaker = store.speaker_label(canal, segment.speaker)
            if segment.is_final:
                await asyncio.to_thread(
                    _save,
                    sessions,
                    session_id=sessao,
                    channel=canal,
                    provider=stream.name,
                    speaker=speaker,
                    text=segment.text,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                )
            if websocket.client_state != WebSocketState.CONNECTED:
                continue
            try:
                await websocket.send_json(
                    {
                        "tipo": "trecho",
                        "canal": canal,
                        "falante": speaker,
                        "texto": segment.text,
                        "final": segment.is_final,
                        "inicio_ms": segment.start_ms,
                    }
                )
            except (WebSocketDisconnect, RuntimeError):
                # A extensão fechou; os trechos finais continuam sendo gravados.
                pass

    try:
        await asyncio.gather(upstream(), downstream())
    finally:
        await provider.__aexit__(None, None, None)
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close()


def _save(sessions, **fields) -> None:
    with sessions() as session:
        store.save_segment(session, **fields)


class CaptionIn(BaseModel):
    falante: str | None = Field(default=None, max_length=200)
    texto: str = Field(max_length=5000)
    inicio_ms: int | None = None


class CaptionsIn(BaseModel):
    trechos: list[CaptionIn] = Field(max_length=500)


@router.post("/transcricoes/{session_id}/legendas")
def receive_captions(session_id: str, body: CaptionsIn, owner: Owner, db: DB) -> dict[str, int]:
    """Legendas do próprio Meet, lidas pela extensão, para comparar com o serviço."""
    _check_session_id(session_id)
    lines = [store.CaptionLine(t.falante, t.texto, t.inicio_ms) for t in body.trechos]
    return {"gravados": store.save_captions(db, session_id, lines)}


class FinalizeIn(BaseModel):
    titulo: str | None = Field(default=None, max_length=500)
    url_reuniao: str | None = Field(default=None, max_length=500)


@router.post("/transcricoes/{session_id}/finalizar")
def finalize(session_id: str, body: FinalizeIn, owner: Owner, db: DB) -> dict[str, int]:
    _check_session_id(session_id)
    item = store.finalize(
        db, session_id, actor=owner, title=body.titulo, meeting_url=body.url_reuniao
    )
    return {"bronze_id": item.id}


@router.get("/transcricoes/{session_id}", response_class=HTMLResponse)
def compare(session_id: str, request: Request, owner: Owner, db: DB):
    """As duas versões lado a lado: serviço de transcrição e legendas do Meet."""
    _check_session_id(session_id)
    audio, captions = store.segments_by_channel(db, session_id)
    return templates.TemplateResponse(
        request,
        "transcricao.html",
        {"owner": owner, "session_id": session_id, "audio": audio, "captions": captions},
    )
