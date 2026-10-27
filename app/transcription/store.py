"""Guarda os trechos e, ao fim da reunião, monta o item no bronze."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BronzeItem, SourceKind, TranscriptSegment, audit

# Canais que vêm do serviço de transcrição; as legendas do Meet ficam à parte para comparação.
AUDIO_CHANNELS = ("voce", "outros")
CAPTIONS_CHANNEL = "legenda_meet"


@dataclass(frozen=True)
class CaptionLine:
    speaker: str | None
    text: str
    start_ms: int | None


def speaker_label(channel: str, speaker: int | None) -> str:
    if channel == "voce":
        return "Você"
    return f"Falante {speaker + 1}" if speaker is not None else "Participante"


def save_segment(
    session: Session,
    *,
    session_id: str,
    channel: str,
    provider: str,
    speaker: str | None,
    text: str,
    start_ms: int | None,
    end_ms: int | None,
) -> None:
    session.add(
        TranscriptSegment(
            session_id=session_id,
            channel=channel,
            provider=provider,
            speaker=speaker,
            text=text,
            start_ms=start_ms,
            end_ms=end_ms,
        )
    )
    session.commit()


def save_captions(session: Session, session_id: str, lines: Iterable[CaptionLine]) -> int:
    count = 0
    for line in lines:
        if not line.text.strip():
            continue
        session.add(
            TranscriptSegment(
                session_id=session_id,
                channel=CAPTIONS_CHANNEL,
                provider="meet",
                speaker=line.speaker,
                text=line.text.strip(),
                start_ms=line.start_ms,
            )
        )
        count += 1
    session.commit()
    return count


def _timestamp(ms: int | None) -> str:
    if ms is None:
        return "--:--"
    seconds = ms // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def render(segments: Iterable[TranscriptSegment]) -> str:
    return "\n".join(f"[{_timestamp(s.start_ms)}] {s.speaker}: {s.text}" for s in segments)


def segments_by_channel(
    session: Session, session_id: str
) -> tuple[list[TranscriptSegment], list[TranscriptSegment]]:
    """(áudio transcrito, legendas do Meet), cada um em ordem de tempo."""
    rows = session.scalars(
        select(TranscriptSegment)
        .where(TranscriptSegment.session_id == session_id)
        .order_by(TranscriptSegment.start_ms.nulls_last(), TranscriptSegment.id)
    ).all()
    audio = [r for r in rows if r.channel in AUDIO_CHANNELS]
    captions = [r for r in rows if r.channel == CAPTIONS_CHANNEL]
    return audio, captions


def finalize(
    session: Session, session_id: str, *, actor: str, title: str | None, meeting_url: str | None
) -> BronzeItem:
    """Cria (ou atualiza) o item do bronze desta reunião. Pode ser chamado de novo sem duplicar."""
    audio, captions = segments_by_channel(session, session_id)
    body = render(audio)
    item = session.scalar(
        select(BronzeItem).where(
            BronzeItem.source == SourceKind.MEET_TRANSCRIPT, BronzeItem.external_id == session_id
        )
    )
    if item is None:
        item = BronzeItem(source=SourceKind.MEET_TRANSCRIPT, external_id=session_id)
        session.add(item)
    item.subject = title or "Reunião sem título"
    item.body_text = body
    item.occurred_at = audio[0].received_at if audio else None
    item.content_sha256 = hashlib.sha256(body.encode()).hexdigest()
    item.extra = {
        "url_reuniao": meeting_url,
        "trechos_audio": len(audio),
        "legendas_meet": render(captions),
        "trechos_legenda": len(captions),
    }
    session.flush()
    audit(session, actor, "transcricao_finalizada", target=session_id, bronze_id=item.id)
    session.commit()
    return item
