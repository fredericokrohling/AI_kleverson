"""Serviços de transcrição em streaming.

Cada serviço recebe áudio PCM 16 bits, mono, 16 kHz, e devolve trechos de texto
enquanto a pessoa fala. Trocar de serviço é trocar a classe aqui.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.config import Settings

SAMPLE_RATE = 16000


@dataclass(frozen=True)
class Segment:
    text: str
    is_final: bool
    # Número do falante dado pela separação de vozes (0, 1, 2...), quando houver.
    speaker: int | None
    start_ms: int | None
    end_ms: int | None


class StreamingProvider(Protocol):
    name: str

    async def __aenter__(self) -> "StreamingProvider": ...
    async def __aexit__(self, *exc) -> None: ...
    async def send_audio(self, chunk: bytes) -> None: ...
    async def finish(self) -> None: ...
    def results(self) -> AsyncIterator[Segment]: ...


class DeepgramStream:
    """Streaming do Deepgram: https://developers.deepgram.com/docs/live-streaming-audio"""

    name = "deepgram"
    _URL = "wss://api.deepgram.com/v1/listen"

    def __init__(self, api_key: str, model: str, language: str) -> None:
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY não configurada.")
        self._api_key = api_key
        self._query = urlencode(
            {
                "model": model,
                "language": language,
                "encoding": "linear16",
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "diarize": "true",
                "interim_results": "true",
                "smart_format": "true",
                "punctuate": "true",
            }
        )
        self._ws: ClientConnection | None = None

    async def __aenter__(self) -> "DeepgramStream":
        self._ws = await connect(
            f"{self._URL}?{self._query}",
            additional_headers={"Authorization": f"Token {self._api_key}"},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send_audio(self, chunk: bytes) -> None:
        await self._ws.send(chunk)

    async def finish(self) -> None:
        # Pede ao Deepgram para devolver o que falta e encerrar.
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        except ConnectionClosed:
            pass

    async def results(self) -> AsyncIterator[Segment]:
        try:
            async for raw in self._ws:
                segment = parse_deepgram_message(raw)
                if segment is not None:
                    yield segment
        except ConnectionClosed:
            return


def parse_deepgram_message(raw: str | bytes) -> Segment | None:
    data = json.loads(raw)
    if data.get("type") != "Results":
        return None
    alternative = data["channel"]["alternatives"][0]
    text = alternative.get("transcript", "").strip()
    if not text:
        return None
    words = alternative.get("words") or []
    speaker = words[0].get("speaker") if words else None
    start = data.get("start")
    duration = data.get("duration")
    return Segment(
        text=text,
        is_final=bool(data.get("is_final")),
        speaker=speaker,
        start_ms=round(start * 1000) if start is not None else None,
        end_ms=round((start + duration) * 1000) if start is not None and duration else None,
    )


def create_provider(settings: Settings) -> StreamingProvider:
    if settings.transcription_provider == "deepgram":
        return DeepgramStream(
            settings.deepgram_api_key, settings.deepgram_model, settings.transcription_language
        )
    raise RuntimeError(f"Serviço de transcrição desconhecido: {settings.transcription_provider}")
