"""Fila de leitura pelo Claude: a rodada do coletor que lê imagens e PDFs digitalizados.

Por que uma fila e não leitura na hora da coleta: ver vision.py. Aqui fica a rodada:
a cada 5 minutos, o coletor pega até VISION_FILES_PER_RUN arquivos que estão com código
de espera (IMAGEM_SEM_OCR ou PDF_SEM_TEXTO) e manda ao Claude.

Regras para não gastar à toa:
- o mesmo arquivo (mesma impressão digital) anexado em vários e-mails é lido uma vez só,
  e o texto vale para todas as cópias;
- do Drive, só a versão atual de cada arquivo é lida;
- aviso dispensado no console não é lido;
- com o teto do mês atingido, a rodada não começa (nada é gasto, a fila espera);
- se a API falhar num arquivo, ele sai da fila com CLAUDE_FALHOU e só volta pelo
  "Tentar de novo": uma falha repetida não vira gasto repetido.
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Consultas; or_ junta condições com "ou".
from sqlalchemy import or_, select

# Session é a conversa com o banco; sessionmaker é a fábrica de sessões.
from sqlalchemy.orm import Session, sessionmaker

# Configuração.
from app.config import Settings

# Gravação do texto de uma versão do Drive.
from app.ingestion.drive_sync import apply_version_text

# Resultado de extração e mensagens com código.
from app.ingestion.extractors import Extraction, coded

# Guarda dos originais.
from app.ingestion.storage import FileStore

# Leitor, códigos de espera e a regra de "está na fila".
from app.ingestion.vision import PENDING_CODES, VisionReader, is_pending, vision_reader

# Teto mensal e gasto do mês.
from app.llm import BudgetExceededError, month_spend_usd

# Tabelas usadas e o registro de auditoria.
from app.models import Attachment, BronzeItem, DriveItem, audit


# Resumo de uma rodada, para o log do coletor.
@dataclass
class VisionSummary:
    # Arquivos lidos com sucesso.
    read: int = 0
    # Arquivos que o Claude não conseguiu ler (com o motivo gravado).
    failed: int = 0
    # True se o teto do mês foi atingido no meio da rodada.
    budget_hit: bool = False

    # Uma linha legível.
    def describe(self) -> str:
        # Texto base.
        text = f"Leitura pelo Claude: {self.read} lido(s), {self.failed} com erro."
        # Acrescenta o aviso do teto.
        return text + (" Teto mensal atingido; o resto fica na fila." if self.budget_hit else "")


# Anexos de e-mail na fila, um por conteúdo (as cópias do mesmo arquivo vêm juntas).
def _pending_attachments(session: Session) -> list[Attachment]:
    # Condição "o erro começa com um dos códigos de espera".
    waiting = or_(*(Attachment.extraction_error.like(f"[{code}]%") for code in PENDING_CODES))
    # Anexos com original guardado e aviso não dispensado, os mais antigos primeiro.
    rows = session.scalars(
        select(Attachment)
        .where(
            waiting,
            Attachment.origin == "anexo",
            Attachment.object_key.is_not(None),
            Attachment.dismissed_at.is_(None),
        )
        .order_by(Attachment.id)
    ).all()
    # Um por impressão digital.
    unique: dict[str | None, Attachment] = {}
    # Percorre.
    for row in rows:
        # setdefault guarda só o primeiro de cada conteúdo.
        unique.setdefault(row.content_sha256 or f"id:{row.id}", row)
    # Devolve.
    return list(unique.values())


# Arquivos do Drive cuja versão atual está na fila.
def _pending_versions(session: Session) -> list[tuple[DriveItem, BronzeItem]]:
    # Itens baixados (sem erro de download), não dispensados, com a versão atual.
    pairs = session.execute(
        select(DriveItem, BronzeItem)
        .join(BronzeItem, BronzeItem.id == DriveItem.bronze_item_id)
        .where(DriveItem.error.is_(None), DriveItem.dismissed_at.is_(None))
        .order_by(DriveItem.id)
    ).all()
    # Só os que esperam o Claude e têm o original guardado.
    return [
        (item, bronze)
        for item, bronze in pairs
        if is_pending((bronze.extra or {}).get("erro_extracao")) and bronze.extra.get("arquivo")
    ]


# Uma rodada: lê até `limit` arquivos da fila.
def run_pending(
    sessions: sessionmaker[Session], store: FileStore, reader: VisionReader, limit: int
) -> VisionSummary:
    """Lê a fila em ordem de chegada (e-mail primeiro, depois Drive)."""
    # Resumo.
    summary = VisionSummary()
    # Uma sessão para a rodada; cada arquivo é confirmado logo depois de lido.
    with sessions() as session:
        # Fila completa (é pequena: só imagens e PDFs digitalizados).
        jobs: list = _pending_attachments(session) + _pending_versions(session)
        # Até o limite da rodada.
        for job in jobs[:limit]:
            # Tenta ler.
            try:
                # Anexo de e-mail.
                if isinstance(job, Attachment):
                    # Lê e grava em todas as cópias.
                    error = _read_attachment(session, store, reader, job)
                # Versão do Drive.
                else:
                    # Lê e grava na versão.
                    error = _read_version(store, reader, *job)
            # Teto do mês: para a rodada; nada foi gasto neste arquivo.
            except BudgetExceededError:
                # Marca.
                summary.budget_hit = True
                # Para.
                break
            # Erro inesperado (original sumido do disco, PDF corrompido): grava o motivo,
            # para o arquivo sair da fila e não travar as rodadas seguintes.
            except Exception as exc:
                # Desfaz o que ficou pela metade.
                session.rollback()
                # Motivo com código.
                error = coded("FALHA_LEITURA", f"Falha ao preparar para o Claude: {exc}")
                # Grava no arquivo.
                _mark_failed(job, error)
            # Conta.
            if error is None:
                # Lido.
                summary.read += 1
            # Erro gravado.
            else:
                # Com erro.
                summary.failed += 1
            # Confirma este arquivo antes de ir ao próximo.
            session.commit()
        # Auditoria, só quando algo foi tentado (aparece no Processamento).
        if summary.read or summary.failed:
            # Registro.
            audit(
                session,
                "sistema",
                "claude.leitura_visual",
                lidos=summary.read,
                com_erro=summary.failed,
            )
            # Grava.
            session.commit()
    # Devolve.
    return summary


# Grava um erro num item da fila (anexo ou versão do Drive).
def _mark_failed(job, error: str) -> None:
    # Anexo de e-mail.
    if isinstance(job, Attachment):
        # Erro no anexo.
        job.extraction_error = error
    # Versão do Drive: (item, versão).
    else:
        # Mesmo caminho de uma leitura sem texto.
        apply_version_text(*job, Extraction(None, error))


# Lê um anexo e grava o resultado em todas as cópias do mesmo conteúdo.
def _read_attachment(
    session: Session, store: FileStore, reader: VisionReader, row: Attachment
) -> str | None:
    # Lê com o Claude a partir do original guardado.
    result = reader.read(store.read(row.object_key), row.filename, row.mime_type)
    # Grava no anexo e nas cópias.
    apply_attachment_text(session, row, result)
    # Devolve o erro.
    return result.error


# Grava o resultado num anexo e nas cópias do mesmo conteúdo que ainda esperam o Claude.
# Usado aqui e no "Tentar de novo", para a mesma imagem nunca ser paga duas vezes.
def apply_attachment_text(session: Session, row: Attachment, result: Extraction) -> None:
    # Cópias do mesmo arquivo (ou só o próprio, se não houver impressão digital).
    copies = (
        session.scalars(
            select(Attachment).where(Attachment.content_sha256 == row.content_sha256)
        ).all()
        if row.content_sha256
        else [row]
    )
    # Grava no próprio anexo e nas cópias que ainda esperam (não mexe em cópia resolvida).
    for copy in copies:
        # O próprio ou uma cópia na fila.
        if copy is row or is_pending(copy.extraction_error):
            # Texto sem caractere nulo (o Postgres não aceita).
            copy.extracted_text = result.text.replace("\x00", "") if result.text else None
            # Erro (None se deu certo).
            copy.extraction_error = result.error


# Lê a versão atual de um arquivo do Drive e grava o texto.
def _read_version(
    store: FileStore, reader: VisionReader, item: DriveItem, bronze: BronzeItem
) -> str | None:
    # Caminho do original (a extensão no nome diz o formato).
    key = bronze.extra["arquivo"]
    # Lê com o Claude.
    result = reader.read(store.read(key), key, item.mime_type)
    # Grava o texto na versão, como numa leitura normal.
    apply_version_text(item, bronze, result)
    # Devolve o erro.
    return result.error


# Conveniência para o agendador: monta tudo a partir da configuração.
def run_from_settings(settings: Settings) -> VisionSummary | None:
    """Uma rodada. Devolve None quando não há o que fazer (sem chave, teto, fila vazia)."""
    # Import local: o banco só é carregado quando a rodada roda de verdade.
    from app.db import get_sessionmaker

    # Leitor; sem chave da Anthropic, não há leitura.
    reader = vision_reader(settings)
    # Sem chave.
    if reader is None:
        return None
    # Fábrica de sessões.
    sessions = get_sessionmaker()
    # Confere a fila e o teto antes de abrir a rodada.
    with sessions() as session:
        # Teto atingido: não começa (e não enche o log a cada 5 minutos).
        if month_spend_usd(session) >= settings.llm_monthly_budget_usd:
            return None
        # Fila vazia: nada a fazer.
        if not _pending_attachments(session) and not _pending_versions(session):
            return None
    # Roda.
    return run_pending(
        sessions, FileStore(settings.data_dir), reader, settings.vision_files_per_run
    )
