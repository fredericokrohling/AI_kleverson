"""Arquivos sem texto: a lista mostrada na página inicial e o "Tentar de novo".

Por que existe: um anexo ou arquivo do Drive que não virou texto fica invisível para a IA.
A coleta não para por causa dele (o e-mail entra do mesmo jeito), então é preciso um lugar
onde você veja o que ficou de fora, o motivo (com código) e possa pedir outra tentativa,
por exemplo depois de pedir ao dono do arquivo que libere o download.

Origens:
- anexos de e-mail (tabela attachments, coluna extraction_error);
- links do Lucid e do Canva citados em e-mails (attachments, origem "link_externo"):
  não são lidos, então ficam sempre na lista até você dispensar o aviso;
- arquivos do Drive, das pastas monitoradas ou citados por link num e-mail: erro de
  download (drive_items.error) ou de extração da versão atual
  (bronze_items.extra["erro_extracao"]). Arquivos acima do limite de tamanho entram aqui.

Para o que veio de e-mail, a linha mostra o remetente e a data do e-mail, para você
achar a mensagem. "Dispensar" tira a linha da lista sem apagar nada (por exemplo, depois
de exportar o diagrama à mão para a pasta monitorada).

Tentar de novo:
- anexo: extrai de novo a partir do original guardado no disco;
- Drive: se o arquivo já foi baixado, extrai de novo do original; se o download falhou,
  pergunta ao Drive e baixa outra vez.
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Data e hora com fuso.
from datetime import UTC, datetime

# Consultas; or_ junta condições com "ou".
from sqlalchemy import or_, select

# Session é a conversa com o banco.
from sqlalchemy.orm import Session

# Tipo da configuração.
from app.config import Settings

# Tentativa de novo e motivo atual de um arquivo do Drive.
from app.ingestion.drive_sync import item_failure, retry_drive_item

# Extração, mensagens com código.
from app.ingestion.extractors import coded, extract_text, split_error

# Guarda dos originais.
from app.ingestion.storage import FileStore

# Tabelas usadas.
from app.models import Attachment, BronzeItem, DriveItem, audit


# Uma linha da lista "Arquivos sem texto".
@dataclass(frozen=True)
class Failure:
    # "anexo" (de e-mail) ou "drive".
    kind: str
    # Id interno (attachments.id ou drive_items.id), usado no botão.
    ref_id: int
    # Nome do arquivo.
    name: str
    # De onde veio: assunto do e-mail ou caminho no Drive.
    origin: str
    # Quando chegou (data do e-mail ou da alteração no Drive).
    when: datetime | None
    # Código do erro (ex.: PDF_SEM_TEXTO); None em mensagens antigas, sem código.
    code: str | None
    # Explicação.
    message: str
    # Remetente do e-mail, quando veio de e-mail.
    sender: str | None = None
    # False quando "Tentar de novo" não adianta (link do Lucid ou do Canva).
    retryable: bool = True


# Lista os arquivos que estão sem texto agora.
def list_failures(session: Session, limit: int = 100) -> list[Failure]:
    """Anexos e arquivos do Drive com erro, mais recentes primeiro."""
    # Resultado.
    result: list[Failure] = []
    # Anexos e links externos com erro, não dispensados, com o e-mail de origem.
    rows = session.execute(
        select(Attachment, BronzeItem)
        .join(BronzeItem, BronzeItem.id == Attachment.bronze_item_id)
        .where(Attachment.extraction_error.is_not(None), Attachment.dismissed_at.is_(None))
        .order_by(BronzeItem.occurred_at.desc().nulls_last())
        .limit(limit)
    ).all()
    # Cada anexo.
    for attachment, item in rows:
        # Separa código e explicação.
        code, message = split_error(attachment.extraction_error)
        # Linha da lista.
        result.append(
            Failure(
                "anexo",
                attachment.id,
                attachment.filename,
                item.subject or "(sem assunto)",
                item.occurred_at,
                code,
                message,
                sender=item.author,
                retryable=attachment.origin != "link_externo",
            )
        )
    # Arquivos do Drive presentes e não dispensados. Pastas só entram quando chegaram por
    # link (aviso de que o link aponta para uma pasta).
    items = session.scalars(
        select(DriveItem).where(
            or_(DriveItem.is_folder.is_(False), DriveItem.folder_id.is_(None)),
            DriveItem.removed.is_(False),
            DriveItem.dismissed_at.is_(None),
        )
    ).all()
    # Os que têm erro agora, com o motivo.
    failing = [(item, error) for item in items if (error := item_failure(session, item))]
    # E-mail mais recente que cita cada arquivo que chegou por link.
    emails = _citing_emails(session, [i.drive_id for i, _ in failing if i.folder_id is None])
    # Cada arquivo com erro.
    for item, error in failing:
        # Separa código e explicação.
        code, message = split_error(error)
        # E-mail que citou o arquivo, se chegou por link.
        email = emails.get(item.drive_id)
        # Linha da lista: por link, a origem é o e-mail (assunto, remetente e data).
        result.append(
            Failure(
                "drive",
                item.id,
                item.name,
                f"Link no e-mail: {email.subject or '(sem assunto)'}" if email else item.path,
                email.occurred_at if email else item.modified_time,
                code,
                message,
                sender=email.author if email else None,
            )
        )
    # Mais recentes primeiro (sem data vão para o fim) e corta no limite.
    result.sort(key=lambda f: f.when.timestamp() if f.when else 0, reverse=True)
    # Devolve.
    return result[:limit]


# E-mail mais recente que cita cada arquivo do Drive (por id).
def _citing_emails(session: Session, drive_ids: list[str]) -> dict[str, BronzeItem]:
    # Nenhum id: nada a buscar.
    if not drive_ids:
        return {}
    # Links registrados para esses ids, com o e-mail, mais recentes primeiro.
    rows = session.execute(
        select(Attachment.external_ref, BronzeItem)
        .join(BronzeItem, BronzeItem.id == Attachment.bronze_item_id)
        .where(Attachment.origin == "link_drive", Attachment.external_ref.in_(drive_ids))
        .order_by(BronzeItem.occurred_at.desc().nulls_last())
    ).all()
    # Resultado: o primeiro (mais recente) de cada id.
    found: dict[str, BronzeItem] = {}
    # Cada par.
    for ref, email in rows:
        # setdefault só grava se o id ainda não tiver e-mail.
        found.setdefault(ref, email)
    # Devolve.
    return found


# "Tentar de novo" para um anexo de e-mail.
def retry_attachment(session: Session, store: FileStore, attachment_id: int) -> str | None:
    """Extrai de novo a partir do original. Devolve None se deu certo; senão o erro."""
    # O anexo.
    row = session.get(Attachment, attachment_id)
    # Não existe.
    if row is None:
        raise ValueError("Anexo não encontrado.")
    # Link do Lucid ou do Canva: não há o que ler; o aviso continua o mesmo.
    if row.origin == "link_externo":
        return row.extraction_error
    # Sem original guardado (registros antigos de link do Drive): não há o que reler.
    if row.object_key is None:
        # Erro explicando.
        row.extraction_error = coded(
            "SEM_ORIGINAL", "Não há arquivo guardado para este registro; nada a reprocessar."
        )
        # Devolve.
        return row.extraction_error
    # Lê o original e extrai.
    result = extract_text(store.read(row.object_key), row.filename, row.mime_type)
    # Texto sem caractere nulo (o Postgres não aceita).
    row.extracted_text = result.text.replace("\x00", "") if result.text else None
    # Erro atual (None se agora deu certo).
    row.extraction_error = result.error
    # Devolve.
    return result.error


# "Tentar de novo" de uma linha, qualquer que seja a origem. Registra na auditoria.
def retry(
    session: Session,
    store: FileStore,
    settings: Settings,
    kind: str,
    ref_id: int,
    drive=None,
) -> str | None:
    """Devolve None se deu certo; senão o erro (com código). drive: cliente da API do Drive."""
    # Anexo de e-mail.
    if kind == "anexo":
        # Reextrai.
        error = retry_attachment(session, store, ref_id)
    # Arquivo do Drive.
    elif kind == "drive":
        # Tenta (o cliente só é usado se o download tiver falhado).
        error = retry_drive_item(session, drive, store, settings, ref_id)
    # Origem desconhecida (formulário adulterado).
    else:
        raise ValueError(f"Origem desconhecida: {kind}")
    # Tentar de novo devolve à lista um aviso que tinha sido dispensado (se ainda falhar).
    _row(session, kind, ref_id).dismissed_at = None
    # Auditoria: o que foi tentado e o resultado.
    audit(
        session,
        "dona",
        "arquivo.tentar_de_novo",
        target=f"{kind}:{ref_id}",
        resultado="ok" if error is None else error[:200],
    )
    # Devolve.
    return error


# A linha do banco de um item da lista.
def _row(session: Session, kind: str, ref_id: int) -> Attachment | DriveItem:
    # Tabela conforme a origem.
    row = session.get(Attachment if kind == "anexo" else DriveItem, ref_id)
    # Não existe.
    if row is None:
        raise ValueError("Arquivo não encontrado.")
    # Devolve.
    return row


# "Dispensar": tira o aviso da lista, sem apagar nada.
def dismiss(session: Session, kind: str, ref_id: int) -> None:
    """Marca o aviso como dispensado (some da lista; o registro e o motivo ficam)."""
    # Origem desconhecida (formulário adulterado).
    if kind not in ("anexo", "drive"):
        raise ValueError(f"Origem desconhecida: {kind}")
    # A linha.
    row = _row(session, kind, ref_id)
    # Marca.
    row.dismissed_at = datetime.now(UTC)
    # Auditoria.
    audit(session, "dona", "arquivo.dispensar", target=f"{kind}:{ref_id}")


# "Tentar todos de novo".
def retry_all(
    session: Session, store: FileStore, settings: Settings, drive=None
) -> tuple[int, int]:
    """Tenta cada arquivo da lista. Devolve (tentados, resolvidos)."""
    # Contadores.
    tried = fixed = 0
    # Cada linha da lista atual (sem limite).
    for failure in list_failures(session, limit=10_000):
        # Links do Lucid e do Canva: tentar de novo não adianta.
        if not failure.retryable:
            continue
        # Conta a tentativa.
        tried += 1
        # Tenta; um erro inesperado num arquivo não impede os outros.
        try:
            # Resultado.
            error = retry(session, store, settings, failure.kind, failure.ref_id, drive)
        # Erro inesperado (rede, disco).
        except Exception:
            # Segue para o próximo.
            continue
        # Conta os resolvidos.
        fixed += error is None
    # Devolve.
    return tried, fixed
