"""Guarda os trechos e, ao fim da reunião, monta o item no bronze.

Por que guardar trecho a trecho: se a conexão cair no meio da reunião, o que já foi
transcrito está salvo. No fim, os trechos viram um único texto no bronze, que é o
formato que o resto do sistema (classificação, extração de fatos) entende.
"""

# hashlib calcula a "impressão digital" (SHA-256) do texto.
import hashlib

# Iterable: qualquer coisa que dá para percorrer com "for" (lista, tupla...).
from collections.abc import Iterable

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# select monta consultas.
from sqlalchemy import select

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Tabelas e a função de auditoria.
from app.models import BronzeItem, SourceKind, TranscriptSegment, audit

# Canais que vêm do serviço de transcrição; as legendas do Meet ficam à parte para comparação.
AUDIO_CHANNELS = ("voce", "outros")
# Nome do canal das legendas do Meet.
CAPTIONS_CHANNEL = "legenda_meet"


# Uma linha de legenda lida da tela do Meet pela extensão.
@dataclass(frozen=True)
class CaptionLine:
    # Nome que o Meet mostra ao lado da legenda.
    speaker: str | None
    # Texto da legenda.
    text: str
    # Momento em ms desde o início da captura.
    start_ms: int | None


# Nome a exibir para o falante de um trecho de áudio.
def speaker_label(channel: str, speaker: int | None) -> str:
    # Tudo que vem do seu microfone é você.
    if channel == "voce":
        # Rótulo fixo.
        return "Você"
    # Nos outros, usa o número da diarização (0 vira "Falante 1"); sem número, genérico.
    return f"Falante {speaker + 1}" if speaker is not None else "Participante"


# Grava um trecho de áudio já transcrito.
def save_segment(
    # Sessão do banco.
    session: Session,
    # Argumentos só por nome daqui em diante.
    *,
    # Identificador da reunião.
    session_id: str,
    # Canal ("voce" ou "outros").
    channel: str,
    # Serviço que transcreveu.
    provider: str,
    # Rótulo do falante.
    speaker: str | None,
    # Texto.
    text: str,
    # Início em ms.
    start_ms: int | None,
    # Fim em ms.
    end_ms: int | None,
) -> None:
    # Prepara a gravação da linha.
    session.add(
        TranscriptSegment(
            # Reunião.
            session_id=session_id,
            # Canal.
            channel=channel,
            # Serviço.
            provider=provider,
            # Falante.
            speaker=speaker,
            # Texto.
            text=text,
            # Início.
            start_ms=start_ms,
            # Fim.
            end_ms=end_ms,
        )
    )
    # Grava na hora: se a conexão cair depois, este trecho não se perde.
    session.commit()


# Grava as legendas do Meet enviadas pela extensão. Devolve quantas foram gravadas.
def save_captions(session: Session, session_id: str, lines: Iterable[CaptionLine]) -> int:
    # Contador de linhas gravadas.
    count = 0
    # Percorre cada linha recebida.
    for line in lines:
        # Pula linhas vazias ou só com espaços.
        if not line.text.strip():
            # Vai para a próxima linha.
            continue
        # Prepara a gravação.
        session.add(
            TranscriptSegment(
                # Reunião.
                session_id=session_id,
                # Canal das legendas.
                channel=CAPTIONS_CHANNEL,
                # Quem "transcreveu" foi o próprio Meet.
                provider="meet",
                # Nome mostrado pelo Meet.
                speaker=line.speaker,
                # Texto limpo.
                text=line.text.strip(),
                # Momento.
                start_ms=line.start_ms,
            )
        )
        # Soma um ao contador.
        count += 1
    # Grava todas de uma vez.
    session.commit()
    # Devolve o total.
    return count


# Converte milissegundos em "mm:ss" para exibir. O "_" indica uso interno.
def _timestamp(ms: int | None) -> str:
    # Sem tempo conhecido: marcador neutro.
    if ms is None:
        # Texto fixo.
        return "--:--"
    # "//" é divisão inteira: 65 432 ms viram 65 segundos.
    seconds = ms // 1000
    # Minutos e segundos com dois dígitos (":02d"); "%" é o resto da divisão.
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# Monta o texto final: uma linha "[mm:ss] Falante: texto" por trecho.
def render(segments: Iterable[TranscriptSegment]) -> str:
    # "\n".join junta as linhas com quebra de linha entre elas.
    return "\n".join(f"[{_timestamp(s.start_ms)}] {s.speaker}: {s.text}" for s in segments)


# Busca os trechos de uma reunião, separados em áudio e legendas.
def segments_by_channel(
    session: Session, session_id: str
) -> tuple[list[TranscriptSegment], list[TranscriptSegment]]:
    """(áudio transcrito, legendas do Meet), cada um em ordem de tempo."""
    # Todos os trechos da reunião.
    rows = session.scalars(
        # SELECT * FROM transcript_segments
        select(TranscriptSegment)
        # WHERE session_id = ...
        .where(TranscriptSegment.session_id == session_id)
        # Ordena pelo tempo (sem tempo vão para o fim) e, no empate, pela ordem de chegada.
        .order_by(TranscriptSegment.start_ms.nulls_last(), TranscriptSegment.id)
    ).all()
    # Só os trechos de áudio (você e os outros).
    audio = [r for r in rows if r.channel in AUDIO_CHANNELS]
    # Só as legendas.
    captions = [r for r in rows if r.channel == CAPTIONS_CHANNEL]
    # Devolve as duas listas.
    return audio, captions


# Fecha a reunião: junta os trechos num item do bronze.
def finalize(
    session: Session, session_id: str, *, actor: str, title: str | None, meeting_url: str | None
) -> BronzeItem:
    """Cria (ou atualiza) o item do bronze desta reunião. Pode ser chamado de novo sem duplicar."""
    # Trechos da reunião.
    audio, captions = segments_by_channel(session, session_id)
    # Texto final da transcrição (só o áudio; as legendas vão para "extra").
    body = render(audio)
    # Procura se esta reunião já foi finalizada antes.
    item = session.scalar(
        select(BronzeItem).where(
            BronzeItem.source == SourceKind.MEET_TRANSCRIPT, BronzeItem.external_id == session_id
        )
    )
    # Primeira finalização: cria o item.
    if item is None:
        # O id da reunião é o id externo; a regra única impede duplicar.
        item = BronzeItem(source=SourceKind.MEET_TRANSCRIPT, external_id=session_id)
        # Prepara a inserção.
        session.add(item)
    # Título informado ou um padrão.
    item.subject = title or "Reunião sem título"
    # A transcrição completa.
    item.body_text = body
    # Quando a reunião aconteceu: chegada do primeiro trecho.
    item.occurred_at = audio[0].received_at if audio else None
    # Impressão digital do texto (detecta mudanças em finalizações repetidas).
    item.content_sha256 = hashlib.sha256(body.encode()).hexdigest()
    # Dados extras, inclusive as legendas do Meet para comparar a qualidade.
    item.extra = {
        # Endereço da reunião.
        "url_reuniao": meeting_url,
        # Quantos trechos de áudio.
        "trechos_audio": len(audio),
        # Texto das legendas do Meet.
        "legendas_meet": render(captions),
        # Quantas linhas de legenda.
        "trechos_legenda": len(captions),
    }
    # flush envia ao banco sem confirmar ainda; serve para obter o item.id.
    session.flush()
    # Registra a finalização no log de auditoria.
    audit(session, actor, "transcricao_finalizada", target=session_id, bronze_id=item.id)
    # Confirma tudo.
    session.commit()
    # Devolve o item.
    return item
