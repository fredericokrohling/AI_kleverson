"""Coletor do Gmail: lê e-mails novos e grava no bronze, com anexos e links do Drive.

Fluxo de uma rodada:
1. Garante que só uma coleta roda por vez (trava no Postgres).
2. Calcula a janela: desde o fim da última rodada sem erro, menos uma folga.
3. Pede ao Gmail a lista de ids na janela e separa os que ainda não estão no banco.
4. Para cada id novo: baixa o e-mail bruto, guarda o .eml, desmonta, guarda os anexos,
   extrai o texto, segue os links do Drive e grava tudo numa transação só.
5. Registra a rodada em ingestion_runs.

Por que janela por data + id em vez do "histórico" do Gmail (history.list): o histórico
dá só as mudanças desde um marcador, mas o marcador expira em cerca de uma semana e
exige um caminho alternativo para a primeira carga. A janela por data serve igual para
a primeira carga e para as seguintes, e repetir uma janela é inofensivo, porque o par
único (fonte, id) no banco impede duplicar. Custo: a lista de ids da folga é pedida
de novo a cada rodada, o que é barato (500 ids por chamada).

Erros: uma mensagem que falha (rede, API) não derruba a rodada. Ela é contada, a rodada
termina "com_erros" e a próxima rodada recomeça da última janela sem erro, tentando de
novo só o que falta. Falha ao ler um anexo não é erro de mensagem: o e-mail é gravado e
o motivo fica em attachments.extraction_error.
"""

# base64 decodifica o e-mail bruto, que o Gmail entrega em base64 "para URL".
import base64

# logging escreve mensagens de andamento (aparecem em `docker compose logs coletor`).
import logging

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Data e hora com fuso, e intervalos de tempo.
from datetime import UTC, datetime, timedelta

# Tipos para anotar funções.
from typing import Any

# Funções de consulta do SQLAlchemy; text permite escrever SQL à mão.
from sqlalchemy import func, select, text

# Session e a fábrica de sessões.
from sqlalchemy.orm import Session, sessionmaker

# Tipo da configuração.
from app.config import Settings

# Baixar arquivos do Drive.
from app.ingestion.drive import DriveFile, fetch_drive_file

# Desmontar o e-mail.
from app.ingestion.email_parser import parse_email

# Extração de texto.
from app.ingestion.extractors import Extraction, extract_text

# Guardar os originais.
from app.ingestion.storage import FileStore, safe_suffix, sha256_hex

# Tabelas usadas.
from app.models import Attachment, BronzeItem, IngestionRun, SourceKind, audit

# Logger com o nome deste módulo.
log = logging.getLogger(__name__)

# Número que identifica a trava da coleta do Gmail no Postgres. Qualquer número serve,
# desde que nenhuma outra parte do sistema use o mesmo.
_LOCK_KEY = 20_002
# Quantos ids o Gmail devolve por página (500 é o máximo da API).
_PAGE_SIZE = 500
# Quantos erros de mensagem guardar em detalhe por rodada (o total é sempre contado).
_MAX_ERROR_DETAILS = 20


# Resumo de uma rodada, para o terminal e para os testes.
@dataclass
class RunSummary:
    # True quando outra coleta já estava rodando e esta nem começou.
    skipped: bool = False
    # Id da linha em ingestion_runs.
    run_id: int | None = None
    # Situação final.
    status: str = ""
    # Mensagens que a busca devolveu.
    messages_seen: int = 0
    # Mensagens novas gravadas.
    messages_new: int = 0
    # Arquivos gravados.
    attachments_saved: int = 0
    # Arquivos sem texto extraído.
    extraction_errors: int = 0
    # Mensagens que falharam.
    message_errors: int = 0

    # Texto de uma linha para o terminal.
    def describe(self) -> str:
        # Caso da coleta pulada.
        if self.skipped:
            return "Outra coleta já está em andamento; nada feito."
        # Caso normal.
        return (
            f"Coleta {self.run_id} ({self.status}): {self.messages_seen} na janela, "
            f"{self.messages_new} novas, {self.attachments_saved} arquivos, "
            f"{self.extraction_errors} sem texto, {self.message_errors} mensagens com erro."
        )


# O Postgres não aceita o caractere nulo (\x00) em texto; alguns PDFs o produzem.
def _clean(value: str | None) -> str | None:
    # Remove o caractere nulo, preservando None.
    return value.replace("\x00", "") if value is not None else None


# Corta textos ao tamanho máximo de uma coluna (nomes de arquivo podem ser enormes).
def _cut(value: str | None, size: int) -> str | None:
    # Limpa e corta; None continua None.
    return _clean(value)[:size] if value is not None else None


# Onde começa a próxima janela.
def window_start_for(session: Session, settings: Settings, account: str, now: datetime) -> datetime:
    """Fim da última rodada sem erro, menos a folga; na primeira vez, N dias atrás.

    A folga nunca volta antes do início da primeira rodada: se a coleta começou "a partir
    de agora", e-mails anteriores à instalação continuam de fora.
    """
    # Início mais antigo entre todas as rodadas desta conta (o "marco zero" da coleta).
    first_start = session.scalar(
        select(func.min(IngestionRun.window_start)).where(
            # Só Gmail.
            IngestionRun.source == SourceKind.GMAIL,
            # Só esta conta.
            IngestionRun.account_email == account,
        )
    )
    # Maior fim de janela entre as rodadas "ok" desta conta.
    last_ok = session.scalar(
        select(func.max(IngestionRun.window_end)).where(
            # Só Gmail.
            IngestionRun.source == SourceKind.GMAIL,
            # Só esta conta.
            IngestionRun.account_email == account,
            # Só rodadas sem nenhum erro.
            IngestionRun.status == "ok",
        )
    )
    # Já houve rodada boa: continua dela, voltando a folga.
    if last_ok is not None:
        # Fim da última rodada boa menos a folga.
        start = last_ok - timedelta(minutes=settings.gmail_overlap_minutes)
        # Nunca antes do marco zero.
        return max(start, first_start)
    # Já houve rodada, mas nenhuma terminou "ok": recomeça do marco zero.
    if first_start is not None:
        return first_start
    # Primeira coleta de todas: N dias atrás (0 = a partir de agora).
    return now - timedelta(days=settings.gmail_initial_days)


# Gera as páginas de ids que o Gmail devolve para a busca.
def _list_ids(gmail, query: str):
    # Marcador de página; None na primeira.
    page_token = None
    # Repete até acabar as páginas.
    while True:
        # Pede uma página. num_retries repete sozinho em erros temporários (429, 5xx).
        response = (
            gmail.users()
            .messages()
            .list(userId="me", q=query, maxResults=_PAGE_SIZE, pageToken=page_token)
            .execute(num_retries=3)
        )
        # Entrega os ids desta página.
        yield [m["id"] for m in response.get("messages", [])]
        # Marcador da próxima página.
        page_token = response.get("nextPageToken")
        # Sem próxima página: fim.
        if not page_token:
            return


# Monta a linha de anexo, reaproveitando a extração se o mesmo arquivo já foi lido.
def _attachment_row(
    # Sessão do banco (para procurar extrações anteriores).
    session: Session,
    # Pasta dos originais.
    store: FileStore,
    # Daqui em diante, argumentos só por nome.
    *,
    # Nome do arquivo.
    filename: str,
    # Tipo MIME.
    mime_type: str | None,
    # Conteúdo.
    data: bytes,
    # Texto já pronto, quando houver (e-mail encaminhado).
    ready_text: str | None = None,
    # "anexo" ou "link_drive".
    origin: str = "anexo",
    # Id na origem (id do Drive).
    external_ref: str | None = None,
) -> Attachment:
    # Impressão digital do arquivo.
    digest = sha256_hex(data)
    # Guarda o original (não faz nada se o mesmo conteúdo já estiver no disco).
    key = store.save(data, safe_suffix(filename))
    # Procura uma extração anterior do mesmo arquivo (o mesmo anexo em vários e-mails).
    previous = session.scalars(
        select(Attachment).where(Attachment.content_sha256 == digest).limit(1)
    ).first()
    # Texto já pronto (e-mail encaminhado como anexo).
    if ready_text is not None:
        # Usa o texto pronto.
        result = Extraction(ready_text)
    # Mesmo arquivo já extraído antes: copia o resultado, sem ler de novo.
    elif previous is not None:
        # Reaproveita texto e erro.
        result = Extraction(previous.extracted_text, previous.extraction_error)
    # Arquivo inédito: extrai.
    else:
        # Lê o texto.
        result = extract_text(data, filename, mime_type)
    # Monta a linha.
    return Attachment(
        # Nome cortado ao tamanho da coluna.
        filename=_cut(filename, 500),
        # Tipo MIME cortado ao tamanho da coluna.
        mime_type=_cut(mime_type, 200),
        # Caminho do original no disco.
        object_key=key,
        # Texto sem caractere nulo.
        extracted_text=_clean(result.text),
        # Motivo da falha, se houver.
        extraction_error=result.error,
        # Anexo ou link do Drive.
        origin=origin,
        # Id na origem.
        external_ref=external_ref,
        # Tamanho em bytes.
        size_bytes=len(data),
        # Impressão digital.
        content_sha256=digest,
    )


# Grava uma mensagem. Devolve (arquivos, arquivos sem texto), ou None se foi pulada.
def _ingest_message(
    # Sessão do banco (o commit é feito por quem chama).
    session: Session,
    # Cliente da API do Gmail.
    gmail,
    # Cliente da API do Drive.
    drive,
    # Pasta dos originais.
    store: FileStore,
    # Configuração.
    settings: Settings,
    # Id da mensagem no Gmail.
    message_id: str,
    # Arquivos do Drive já baixados nesta rodada.
    drive_cache: dict[str, DriveFile],
    # Pastas-mãe já consultadas no Drive nesta rodada (id → ids das pastas acima dela).
    parents_cache: dict[str, list[str]],
) -> tuple[int, int] | None:
    # Baixa o e-mail completo em formato bruto.
    response = (
        gmail.users()
        .messages()
        .get(userId="me", id=message_id, format="raw")
        .execute(num_retries=3)
    )
    # Marcadores do Gmail (INBOX, SENT, IMPORTANT...).
    labels = response.get("labelIds", [])
    # Rascunho: não é um fato, é um texto ainda não enviado. Não grava.
    if "DRAFT" in labels:
        # None sinaliza "pulada".
        return None
    # O base64 "para URL" às vezes vem sem os "=" do fim; completa antes de decodificar.
    raw_b64 = response["raw"]
    # Bytes do e-mail original.
    raw = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
    # Guarda o .eml original no disco.
    raw_key = store.save(raw, ".eml")
    # Desmonta o e-mail.
    parsed = parse_email(raw)
    # internalDate: quando o Gmail recebeu, em milissegundos desde 1970. Mais confiável
    # que o cabeçalho Date, que é escrito pelo remetente.
    internal_ms = response.get("internalDate")
    # Converte para data e hora em UTC.
    occurred_at = datetime.fromtimestamp(int(internal_ms) / 1000, UTC) if internal_ms else None
    # Linha do bronze.
    item = BronzeItem(
        # Fonte.
        source=SourceKind.GMAIL,
        # Id da mensagem no Gmail.
        external_id=message_id,
        # Id da conversa no Gmail.
        thread_id=response.get("threadId"),
        # Quando chegou.
        occurred_at=occurred_at,
        # Remetente.
        author=_cut(parsed.sender, 500),
        # Assunto.
        subject=_clean(parsed.subject),
        # Corpo.
        body_text=_clean(parsed.body_text) or "",
        # Impressão digital do e-mail inteiro.
        content_sha256=sha256_hex(raw),
        # Metadados extras em JSON.
        extra={
            # Destinatários.
            "para": parsed.to,
            # Em cópia.
            "cc": parsed.cc,
            # Marcadores do Gmail.
            "marcadores": labels,
            # Id mundial da mensagem.
            "message_id": parsed.message_id,
            # Resposta a.
            "em_resposta_a": parsed.in_reply_to,
            # Data escrita pelo remetente.
            "cabecalho_data": parsed.date_header,
            # Prévia curta do Gmail.
            "previa": response.get("snippet"),
            # Caminho do .eml original.
            "eml": raw_key,
        },
    )
    # Prepara a gravação do item.
    session.add(item)
    # flush manda o INSERT agora (sem commit) para o banco gerar o id do item.
    session.flush()
    # Linhas de arquivo deste e-mail.
    rows: list[Attachment] = []
    # Anexos de dentro do e-mail.
    for att in parsed.attachments:
        # Monta a linha.
        rows.append(
            _attachment_row(
                session,
                store,
                filename=att.filename,
                mime_type=att.mime_type,
                data=att.data,
                ready_text=att.text,
            )
        )
    # Links do Drive, só se uma pasta autorizada estiver configurada.
    if settings.drive_folder_id and drive is not None:
        # Limite em bytes.
        max_bytes = settings.drive_max_file_mb * 1024 * 1024
        # Cada id encontrado.
        for file_id in parsed.drive_file_ids:
            # Mesmo arquivo citado em outro e-mail desta rodada: baixa uma vez só.
            if file_id not in drive_cache:
                # Baixa (se estiver na pasta autorizada) e guarda no cache da rodada.
                drive_cache[file_id] = fetch_drive_file(
                    drive, file_id, max_bytes, settings.drive_folder_id, parents_cache
                )
            # Resultado do download.
            found = drive_cache[file_id]
            # Baixou: guarda e extrai como um anexo.
            if found.data is not None:
                # Monta a linha.
                rows.append(
                    _attachment_row(
                        session,
                        store,
                        filename=found.filename,
                        mime_type=found.mime_type,
                        data=found.data,
                        origin="link_drive",
                        external_ref=file_id,
                    )
                )
            # Não baixou: registra o link com o motivo, para você saber que existe.
            else:
                # Linha sem arquivo.
                rows.append(
                    Attachment(
                        filename=_cut(found.filename, 500),
                        mime_type=_cut(found.mime_type, 200),
                        extraction_error=found.error,
                        origin="link_drive",
                        external_ref=file_id,
                    )
                )
    # Liga cada arquivo ao item e prepara a gravação.
    for row in rows:
        # Id do item gerado pelo flush.
        row.bronze_item_id = item.id
        # Prepara.
        session.add(row)
    # Quantos arquivos e quantos ficaram sem texto.
    return len(rows), sum(1 for r in rows if r.extraction_error)


# Ponto de entrada do módulo: uma rodada completa.
def collect_gmail(
    # Fábrica de sessões do banco.
    sessions: sessionmaker[Session],
    # Cliente da API do Gmail (nos testes, um falso).
    gmail,
    # Cliente da API do Drive (nos testes, um falso).
    drive,
    # Pasta dos originais.
    store: FileStore,
    # Configuração.
    settings: Settings,
    # Daqui em diante, argumentos só por nome.
    *,
    # Início da janela pedido à mão (--desde); None = calcular sozinho.
    since: datetime | None = None,
    # "Agora"; os testes fixam um valor para não depender do relógio.
    now: datetime | None = None,
) -> RunSummary:
    """Uma rodada de coleta. gmail e drive são os clientes das APIs do Google."""
    # Momento de referência da rodada.
    now = now or datetime.now(UTC)
    # Resumo que será devolvido.
    summary = RunSummary()
    # Sessão só para a trava. A trava do Postgres pertence à conexão que a pegou, então
    # esta sessão fica aberta até o fim da rodada.
    with sessions() as lock_session:
        # pg_try_advisory_lock devolve True se conseguiu a trava, False se alguém a tem.
        if not lock_session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}):
            # Outra coleta rodando (ex.: o serviço contínuo e um comando manual).
            summary.skipped = True
            # Devolve sem fazer nada.
            return summary
        # try/finally: a trava é solta mesmo se algo der errado.
        try:
            # Faz o trabalho.
            _run(sessions, gmail, drive, store, settings, since, now, summary)
        # Sempre executa.
        finally:
            # Solta a trava.
            lock_session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            # Confirma (fecha a transação da sessão da trava).
            lock_session.commit()
    # Devolve o resumo.
    return summary


# O trabalho da rodada, já com a trava pega.
def _run(sessions, gmail, drive, store, settings, since, now, summary: RunSummary) -> None:
    # Qual conta está conectada (getProfile é uma chamada barata).
    account = gmail.users().getProfile(userId="me").execute(num_retries=3)["emailAddress"]
    # Sessão principal da rodada.
    with sessions() as session:
        # Início da janela: o pedido explícito ou o calculado.
        start = since or window_start_for(session, settings, account, now)
        # Registra a rodada como "em andamento".
        run = IngestionRun(
            source=SourceKind.GMAIL, account_email=account, window_start=start, window_end=now
        )
        # Prepara.
        session.add(run)
        # Grava já, para a rodada aparecer no console enquanto roda.
        session.commit()
        # Guarda o id no resumo.
        summary.run_id = run.id
        # Busca do Gmail: o filtro configurado mais "depois de" em segundos desde 1970.
        query = f"{settings.gmail_query} after:{int(start.timestamp())}".strip()
        # Detalhes dos erros desta rodada.
        errors: list[str] = []
        # Arquivos do Drive já baixados nesta rodada.
        drive_cache: dict[str, DriveFile] = {}
        # Pastas do Drive já consultadas nesta rodada.
        parents_cache: dict[str, list[str]] = {}
        # Registra o começo no log.
        log.info("Coleta %s: buscando %r", run.id, query)
        # A listagem também pode falhar (rede); nesse caso a rodada fica "com_erros".
        try:
            # Percorre as páginas de ids.
            for ids in _list_ids(gmail, query):
                # Conta os vistos.
                summary.messages_seen += len(ids)
                # Página vazia: nada a fazer.
                if not ids:
                    continue
                # Quais destes ids já estão no banco (uma consulta por página).
                known = set(
                    session.scalars(
                        select(BronzeItem.external_id).where(
                            BronzeItem.source == SourceKind.GMAIL,
                            BronzeItem.external_id.in_(ids),
                        )
                    )
                )
                # Só os novos.
                for message_id in (i for i in ids if i not in known):
                    # Cada mensagem é independente.
                    try:
                        # Grava a mensagem.
                        result = _ingest_message(
                            session,
                            gmail,
                            drive,
                            store,
                            settings,
                            message_id,
                            drive_cache,
                            parents_cache,
                        )
                        # Confirma esta mensagem (o que já foi gravado não se perde).
                        session.commit()
                    # Qualquer erro nesta mensagem.
                    except Exception as exc:
                        # Desfaz o que estava pela metade.
                        session.rollback()
                        # Conta o erro.
                        summary.message_errors += 1
                        # Guarda o detalhe, até o limite.
                        if len(errors) < _MAX_ERROR_DETAILS:
                            # Id, tipo e mensagem do erro.
                            errors.append(f"{message_id}: {type(exc).__name__}: {exc}"[:500])
                        # Registra no log com o rastro completo.
                        log.exception("Falha na mensagem %s", message_id)
                        # Segue para a próxima.
                        continue
                    # Mensagem pulada (rascunho).
                    if result is None:
                        continue
                    # Conta a mensagem nova.
                    summary.messages_new += 1
                    # Soma os arquivos gravados.
                    summary.attachments_saved += result[0]
                    # Soma os arquivos sem texto.
                    summary.extraction_errors += result[1]
                    # A cada 100 novas, avisa no log (útil numa importação com --desde, que é longa).
                    if summary.messages_new % 100 == 0:
                        # Mensagem de andamento.
                        log.info("Coleta %s: %s novas até agora", run.id, summary.messages_new)
        # Falha ao listar.
        except Exception as exc:
            # Desfaz qualquer coisa pendente.
            session.rollback()
            # Conta como erro.
            summary.message_errors += 1
            # Detalhe.
            errors.append(f"listagem: {type(exc).__name__}: {exc}"[:500])
            # Rastro completo no log.
            log.exception("Falha ao listar mensagens")
        # Situação final.
        summary.status = "ok" if summary.message_errors == 0 else "com_erros"
        # Copia o resumo para a linha da rodada.
        _finish(run, summary, errors)
        # Prepara a atualização.
        session.add(run)
        # Só registra na auditoria se algo aconteceu, para não encher o diário a cada 5 min.
        if summary.messages_new or summary.message_errors:
            # Ação "gmail.coleta" com as contagens.
            audit(
                session,
                "sistema",
                "gmail.coleta",
                target=account,
                rodada=run.id,
                novas=summary.messages_new,
                arquivos=summary.attachments_saved,
                erros=summary.message_errors,
            )
        # Grava o fim da rodada.
        session.commit()
        # Linha final no log.
        log.info(summary.describe())


# Copia as contagens do resumo para a linha da rodada.
def _finish(run: IngestionRun, summary: RunSummary, errors: list[str]) -> None:
    # Fim agora.
    run.finished_at = datetime.now(UTC)
    # Situação.
    run.status = summary.status
    # Mensagens vistas na janela.
    run.messages_seen = summary.messages_seen
    # Mensagens novas gravadas.
    run.messages_new = summary.messages_new
    # Arquivos gravados.
    run.attachments_saved = summary.attachments_saved
    # Arquivos sem texto.
    run.extraction_errors = summary.extraction_errors
    # Mensagens que falharam.
    run.message_errors = summary.message_errors
    # Erros, um por linha; None se não houve.
    run.error_detail = "\n".join(errors) or None


# Extrai de novo os arquivos que falharam, a partir dos originais guardados.
def reextract_attachments(sessions: sessionmaker[Session], store: FileStore) -> tuple[int, int]:
    """Tenta de novo os anexos com erro de extração. Devolve (tentados, resolvidos)."""
    # Contadores.
    tried = fixed = 0
    # Uma sessão para tudo.
    with sessions() as session:
        # Anexos com erro e com original guardado (links sem acesso não têm original).
        rows = session.scalars(
            select(Attachment).where(
                Attachment.extraction_error.is_not(None), Attachment.object_key.is_not(None)
            )
        ).all()
        # Percorre cada um.
        for row in rows:
            # Conta a tentativa.
            tried += 1
            # Lê o original do disco e extrai.
            result = extract_text(store.read(row.object_key), row.filename, row.mime_type)
            # Atualiza o texto (o original no disco não muda; o texto é derivado dele).
            row.extracted_text = _clean(result.text)
            # Atualiza o erro (None se agora deu certo).
            row.extraction_error = result.error
            # Conta os resolvidos.
            fixed += result.error is None
        # Grava tudo.
        session.commit()
    # Devolve as contagens.
    return tried, fixed


# Conveniência para a linha de comando: monta tudo a partir da configuração.
def run_from_settings(settings: Settings, since: datetime | None = None) -> RunSummary:
    """Carrega as credenciais do banco, cria os clientes do Google e roda uma coleta."""
    # Imports locais: só carregam quando a coleta de verdade roda (os testes usam falsos).
    from googleapiclient.discovery import build

    # Fábrica de sessões.
    from app.db import get_sessionmaker

    # Leitura das credenciais do Google gravadas no banco.
    from app.google_auth import load_credentials

    # Criptografia dos tokens.
    from app.security import get_cipher

    # Fábrica de sessões.
    sessions = get_sessionmaker()
    # Credenciais válidas (renovadas se preciso).
    with sessions() as session:
        # Lê e descriptografa.
        creds = load_credentials(session, get_cipher(), settings)
    # Cliente do Gmail.
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    # Cliente do Drive.
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    # Roda a coleta.
    return collect_gmail(
        sessions, gmail, drive, FileStore(settings.data_dir), settings, since=since
    )


# Dicionário usado pelo console: últimas rodadas e totais.
def collection_overview(session: Session, limit: int = 5) -> dict[str, Any]:
    """Resumo para a página inicial: últimas rodadas, total de e-mails e de arquivos."""
    # Últimas rodadas do Gmail, mais recentes primeiro.
    runs = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.source == SourceKind.GMAIL)
        .order_by(IngestionRun.started_at.desc())
        .limit(limit)
    ).all()
    # Total de e-mails no bronze.
    emails = session.scalar(
        select(func.count()).select_from(BronzeItem).where(BronzeItem.source == SourceKind.GMAIL)
    )
    # Total de arquivos.
    files = session.scalar(select(func.count()).select_from(Attachment))
    # Arquivos sem texto.
    files_without_text = session.scalar(
        select(func.count()).select_from(Attachment).where(Attachment.extraction_error.is_not(None))
    )
    # Junta tudo.
    return {
        "runs": runs,
        "emails": emails,
        "files": files,
        "files_without_text": files_without_text,
    }
