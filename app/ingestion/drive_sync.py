"""Monitor das pastas do Google Drive: espelha a árvore e grava cada versão no bronze.

Você cadastra uma pasta no console (Configurações → Pastas do Drive). A cada rodada
(DRIVE_POLL_MINUTES, padrão 30 min) este módulo:
1. Percorre a pasta e todas as subpastas, nível por nível (files.list).
2. Atualiza a tabela drive_items, que espelha a árvore (nome, caminho, pasta-mãe).
   É por ela que as tags são herdadas: a view drive_item_effective_tags sobe pela
   pasta-mãe de cada item.
3. Para cada arquivo cuja versão mudou: baixa, extrai o texto e, se o texto mudou de
   verdade, grava um novo bronze_item (fonte "drive", id "<id do Drive>:<versão>").
4. Marca como removidos os itens que sumiram da pasta (o histórico no bronze fica).

Por que comparar o texto, e não só a versão: o Drive muda a versão por qualquer motivo
(renomear, mexer em permissões, abrir um comentário). Exportar o mesmo Google Docs duas
vezes também gera bytes diferentes. Comparar a impressão digital do texto evita gravar
cópias iguais e, depois, pagar a IA para ler a mesma coisa de novo.

Por que uma versão por item no bronze: o bronze é imutável. Uma planilha de cronograma
que muda toda semana vira uma linha por mudança, e o bloco 3 pode comparar as datas
entre versões (é assim que se descobre que um marco escorregou).

Arquivos citados por link nos e-mails: o coletor do Gmail só registra o id (origem
"link_drive"). Depois das pastas, a rodada lê cada arquivo citado a que a sua conta tem
acesso, mesmo fora das pastas monitoradas (de outra pessoa, de um drive corporativo).
Eles entram na mesma tabela drive_items, sem pasta (folder_id vazio), e viram versões no
bronze do mesmo jeito. Um link novo é lido na volta seguinte do agendador; os já lidos
são conferidos a cada DRIVE_POLL_MINUTES. Se o arquivo também estiver numa pasta
monitorada, a pasta manda (é ela que dá as tags herdadas).

Erros: uma pasta que falha (sem acesso, apagada) não impede as outras; o motivo fica em
drive_folders.last_error. Um arquivo que não dá para baixar fica com o motivo em
drive_items.error e é tentado de novo na rodada seguinte.
"""

# hashlib calcula impressões digitais (SHA-256) do texto extraído.
import hashlib

# logging escreve mensagens de andamento (aparecem em `docker compose logs coletor`).
import logging

# re: expressões regulares, para achar o id da pasta dentro de um link.
import re

# deque é uma fila eficiente: a varredura usa como lista de pastas a visitar.
from collections import deque

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Data e hora com fuso, e intervalos de tempo.
from datetime import UTC, datetime, timedelta

# Tipos para anotar funções.
from typing import Any

# HttpError é o erro que a biblioteca do Google levanta quando a API recusa o pedido.
from googleapiclient.errors import HttpError

# Funções de consulta do SQLAlchemy; text permite escrever SQL à mão.
from sqlalchemy import func, select, text, update

# Session e a fábrica de sessões.
from sqlalchemy.orm import Session, sessionmaker

# Tipo da configuração.
from app.config import Settings

# Download do conteúdo e tradução de erros do Drive.
from app.ingestion.drive import FOLDER_MIME, download, explain

# Extração de texto (a mesma dos anexos de e-mail) e mensagens com código.
from app.ingestion.extractors import Extraction, coded, extract_text

# Guardar os originais.
from app.ingestion.storage import FileStore, safe_suffix

# Leitura pelo Claude no "Tentar de novo" (imagens e PDFs digitalizados).
from app.ingestion.vision import VisionReader, extract_or_read, is_pending

# Tabelas usadas.
from app.models import Attachment, BronzeItem, DriveFolder, DriveItem, SourceKind, audit

# Logger com o nome do módulo.
log = logging.getLogger(__name__)

# Número da trava consultiva do Postgres (diferente da do Gmail, 20_002).
_LOCK_KEY = 20_003

# Campos pedidos ao Drive para cada item; pedir só o necessário deixa a resposta menor.
_ITEM_FIELDS = (
    "id, name, mimeType, modifiedTime, version, size, webViewLink,"
    " lastModifyingUser(displayName, emailAddress)"
)

# Na listagem, os mesmos campos para cada item, mais o marcador da próxima página.
_FIELDS = f"nextPageToken, files({_ITEM_FIELDS})"

# Limite de pastas por varredura de uma raiz (protege contra árvores gigantes ou ciclos).
_MAX_FOLDERS = 5_000

# Id do Drive dentro de um link: ".../folders/ID" ou "...?id=ID".
_LINK_ID = re.compile(r"(?:/folders/|[?&]id=)([A-Za-z0-9_-]{10,})")

# Um id sozinho, colado sem link.
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")


# Resumo de uma rodada, para o log e para a linha de comando.
@dataclass
class SyncSummary:
    # True quando outra varredura já estava rodando.
    skipped: bool = False
    # Pastas monitoradas varridas.
    folders: int = 0
    # Itens (arquivos e pastas) vistos.
    items_seen: int = 0
    # Versões novas gravadas no bronze.
    new_versions: int = 0
    # Arquivos que não deu para baixar.
    file_errors: int = 0
    # Pastas monitoradas que falharam.
    folder_errors: int = 0
    # Arquivos citados por link conferidos nesta rodada.
    links: int = 0

    # Uma linha legível.
    def describe(self) -> str:
        # Varredura pulada.
        if self.skipped:
            return "Outra varredura do Drive já está em andamento; esta foi pulada."
        # Contagens.
        return (
            f"Drive: {self.folders} pasta(s), {self.items_seen} item(ns) vistos, "
            f"{self.new_versions} versão(ões) nova(s), {self.file_errors} arquivo(s) com erro, "
            f"{self.folder_errors} pasta(s) com erro, {self.links} link(s) de e-mail conferido(s)."
        )


# Acha o id da pasta num link do Drive ou num id colado.
def parse_folder_ref(ref: str) -> str | None:
    """Devolve o id da pasta, ou None se o texto não parecer um link ou id do Drive."""
    # Tira espaços das pontas.
    ref = ref.strip()
    # Procura o padrão de link.
    match = _LINK_ID.search(ref)
    # Achou dentro de um link.
    if match:
        return match.group(1)
    # Um id solto também vale.
    return ref if _BARE_ID.match(ref) else None


# Converte a data do Drive ("2026-10-30T12:00:00.000Z") em datetime com fuso.
def _parse_time(value: str | None) -> datetime | None:
    # Sem data.
    if not value:
        return None
    # fromisoformat entende o "Z" (UTC) a partir do Python 3.11.
    return datetime.fromisoformat(value)


# Cadastra (ou reativa) uma pasta para monitorar.
def add_folder(session: Session, drive, ref: str) -> DriveFolder:
    """Confere no Drive que é uma pasta acessível e cadastra. Erros viram ValueError."""
    # Id a partir do link.
    folder_id = parse_folder_ref(ref)
    # Texto que não é link nem id.
    if folder_id is None:
        raise ValueError("Não reconheci o link. Cole o endereço da pasta aberta no Drive.")
    # Pergunta ao Drive o que é esse id.
    try:
        # Só os campos usados.
        meta = (
            drive.files()
            .get(fileId=folder_id, fields="id,name,mimeType,webViewLink", supportsAllDrives=True)
            .execute(num_retries=3)
        )
    # Sem acesso ou inexistente.
    except HttpError as exc:
        raise ValueError(explain(exc)) from exc
    # É um arquivo, não uma pasta.
    if meta.get("mimeType") != FOLDER_MIME:
        raise ValueError(f"“{meta.get('name')}” não é uma pasta.")
    # Já cadastrada antes?
    folder = session.scalars(select(DriveFolder).where(DriveFolder.drive_id == folder_id)).first()
    # Nova: cria.
    if folder is None:
        # Linha da pasta monitorada.
        folder = DriveFolder(drive_id=folder_id, name=meta["name"])
        # Prepara.
        session.add(folder)
        # flush gera o id, usado logo abaixo.
        session.flush()
    # Já existia (talvez desligada): reativa e atualiza o nome.
    else:
        # Liga de novo.
        folder.active = True
        # Nome atual no Drive.
        folder.name = meta["name"]
        # Zera a última varredura, para a próxima rodada varrer na hora.
        folder.last_scan_at = None
    # A própria pasta vira um item da árvore (é nela que você põe as tags da pasta toda).
    _upsert_root_item(session, folder, meta)
    # Registra na auditoria.
    audit(session, "dona", "drive.pasta.adicionar", target=folder_id, nome=meta["name"])
    # Devolve a pasta (o commit é de quem chamou).
    return folder


# Cria ou atualiza o item que representa a própria pasta monitorada.
def _upsert_root_item(session: Session, folder: DriveFolder, meta: dict[str, Any]) -> DriveItem:
    # Procura o item pelo id do Drive.
    item = session.scalars(select(DriveItem).where(DriveItem.drive_id == folder.drive_id)).first()
    # Ainda não existe.
    if item is None:
        # Sem pasta-mãe: é o topo desta árvore.
        item = DriveItem(drive_id=folder.drive_id, folder_id=folder.id, is_folder=True)
        # Prepara.
        session.add(item)
    # Se a pasta já existia como subpasta de outra pasta monitorada, a pasta-mãe dela é
    # mantida: assim continua herdando as tags de cima.
    # Nome atual.
    item.name = meta["name"]
    # Caminho: o próprio nome, se ainda não tiver um caminho de outra raiz.
    item.path = item.path or meta["name"]
    # Link para abrir no Drive.
    item.web_link = meta.get("webViewLink")
    # Tipo de pasta.
    item.mime_type = FOLDER_MIME
    # Visível de novo, se tinha sido removida.
    item.removed = False
    # Devolve o item.
    return item


# Desliga o monitoramento de uma pasta (arquivos, versões e tags ficam guardados).
def deactivate_folder(session: Session, folder_id: int) -> None:
    """Para de varrer a pasta. Nada é apagado: cadastrar de novo retoma de onde parou."""
    # Busca pela chave primária.
    folder = session.get(DriveFolder, folder_id)
    # Não existe: nada a fazer.
    if folder is None:
        return
    # Desliga.
    folder.active = False
    # Registra na auditoria.
    audit(session, "dona", "drive.pasta.remover", target=folder.drive_id, nome=folder.name)


# A varredura está atrasada? (usado pelo agendador)
def drive_scan_due(session: Session, settings: Settings, now: datetime) -> bool:
    """True se há pasta ou link novo, ou se passou o intervalo desde a última conferência."""
    # Pasta ativa ainda não varrida (recém-cadastrada): varre já.
    never = session.scalar(
        select(func.count())
        .select_from(DriveFolder)
        .where(DriveFolder.active.is_(True), DriveFolder.last_scan_at.is_(None))
    )
    # Tem pasta nova.
    if never:
        return True
    # Link citado num e-mail e ainda não lido (não há item com esse id): lê já.
    if session.scalar(
        select(func.count())
        .select_from(Attachment)
        .where(
            Attachment.origin == "link_drive",
            ~select(DriveItem.id).where(DriveItem.drive_id == Attachment.external_ref).exists(),
        )
    ):
        return True
    # Varredura mais antiga entre as pastas ativas.
    oldest_folder = session.scalar(
        select(func.min(DriveFolder.last_scan_at)).where(DriveFolder.active.is_(True))
    )
    # Conferência mais antiga entre os arquivos que chegaram por link.
    oldest_link = session.scalar(
        select(func.min(DriveItem.last_seen_at)).where(DriveItem.folder_id.is_(None))
    )
    # As datas que existem (sem pasta e sem link, a lista fica vazia).
    dates = [d for d in (oldest_folder, oldest_link) if d is not None]
    # Nada a acompanhar.
    if not dates:
        return False
    # Atrasada se a mais antiga passou do intervalo.
    return now - min(dates) >= timedelta(minutes=settings.drive_poll_minutes)


# Ponto de entrada: uma rodada completa em todas as pastas ativas.
def sync_drive(
    # Fábrica de sessões do banco.
    sessions: sessionmaker[Session],
    # Cliente da API do Drive (nos testes, um falso).
    drive,
    # Pasta dos originais.
    store: FileStore,
    # Configuração.
    settings: Settings,
    # Daqui em diante, argumentos só por nome.
    *,
    # "Agora"; os testes fixam um valor para não depender do relógio.
    now: datetime | None = None,
) -> SyncSummary:
    """Varre todas as pastas ativas. Só uma varredura por vez (trava no Postgres)."""
    # Momento de referência.
    now = now or datetime.now(UTC)
    # Resumo devolvido.
    summary = SyncSummary()
    # Sessão só para a trava (a trava pertence à conexão, que fica aberta até o fim).
    with sessions() as lock_session:
        # Tenta pegar a trava sem esperar.
        if not lock_session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}):
            # Outra varredura rodando.
            summary.skipped = True
            # Sai sem fazer nada.
            return summary
        # try/finally: solta a trava mesmo com erro.
        try:
            # Sessão de trabalho.
            with sessions() as session:
                # Pastas ativas.
                folders = session.scalars(
                    select(DriveFolder).where(DriveFolder.active.is_(True)).order_by(DriveFolder.id)
                ).all()
                # Uma por vez.
                for folder in folders:
                    # Conta a pasta.
                    summary.folders += 1
                    # Cada pasta é independente.
                    try:
                        # Varre.
                        _scan_folder(session, drive, store, settings, folder, now, summary)
                        # Varredura completa.
                        folder.last_scan_at = now
                        # Sem erro.
                        folder.last_error = None
                    # Pasta inteira falhou (sem acesso, apagada, rede).
                    except Exception as exc:
                        # Desfaz o que estava pela metade.
                        session.rollback()
                        # Conta.
                        summary.folder_errors += 1
                        # Mensagem legível.
                        message = explain(exc) if isinstance(exc, HttpError) else str(exc)
                        # Guarda o motivo na pasta (recarregada depois do rollback).
                        folder.last_error = f"{type(exc).__name__}: {message}"[:1000]
                        # Mesmo com erro, conta como tentativa, para não repetir a cada minuto.
                        folder.last_scan_at = now
                        # Rastro completo no log.
                        log.exception("Falha ao varrer a pasta %s", folder.drive_id)
                    # Grava o estado da pasta.
                    session.commit()
                # Depois das pastas, os arquivos citados por link nos e-mails.
                try:
                    # Lê os novos e confere os já lidos.
                    _sync_links(session, drive, store, settings, now, summary)
                # Falha inesperada (rede): as pastas já foram gravadas; a próxima rodada tenta.
                except Exception:
                    # Desfaz o que estava pela metade.
                    session.rollback()
                    # Rastro completo no log.
                    log.exception("Falha ao ler os arquivos citados por link")
                # Registra na auditoria só quando algo aconteceu.
                if summary.new_versions or summary.folder_errors:
                    # Ação "drive.varredura" com as contagens.
                    audit(
                        session,
                        "sistema",
                        "drive.varredura",
                        versoes_novas=summary.new_versions,
                        pastas_com_erro=summary.folder_errors,
                        arquivos_com_erro=summary.file_errors,
                    )
                    # Grava.
                    session.commit()
        # Sempre executa.
        finally:
            # Solta a trava.
            lock_session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            # Fecha a transação da sessão da trava.
            lock_session.commit()
    # Linha no log.
    log.info(summary.describe())
    # Devolve.
    return summary


# Varre uma pasta monitorada e tudo dentro dela.
def _scan_folder(
    session: Session,
    drive,
    store: FileStore,
    settings: Settings,
    folder: DriveFolder,
    now: datetime,
    summary: SyncSummary,
) -> None:
    # Dados atuais da pasta raiz (nome pode ter mudado; falha aqui = sem acesso).
    meta = (
        drive.files()
        .get(fileId=folder.drive_id, fields="id,name,mimeType,webViewLink", supportsAllDrives=True)
        .execute(num_retries=3)
    )
    # Atualiza o nome da pasta monitorada.
    folder.name = meta["name"]
    # Atualiza o item raiz.
    root = _upsert_root_item(session, folder, meta)
    # Visto agora.
    root.last_seen_at = now
    # Limite em bytes para download.
    max_bytes = settings.drive_max_file_mb * 1024 * 1024
    # Fila de pastas a visitar: (id do Drive, caminho legível).
    queue: deque[tuple[str, str]] = deque([(folder.drive_id, root.path)])
    # Pastas já visitadas (protege contra ciclos).
    visited: set[str] = set()
    # Enquanto houver pasta na fila.
    while queue:
        # Próxima pasta.
        parent_id, parent_path = queue.popleft()
        # Já visitada ou limite atingido: pula.
        if parent_id in visited or len(visited) >= _MAX_FOLDERS:
            continue
        # Marca como visitada.
        visited.add(parent_id)
        # Cada item dentro dela.
        for child in _list_children(drive, parent_id):
            # Conta.
            summary.items_seen += 1
            # Atualiza o espelho e, se for arquivo novo ou alterado, grava a versão.
            item = _sync_item(
                session,
                drive,
                store,
                folder,
                child,
                (parent_id, parent_path),
                now,
                max_bytes,
                summary,
            )
            # Subpasta: entra na fila.
            if item.is_folder:
                # Visita depois, com o caminho dela.
                queue.append((item.drive_id, item.path))
            # Confirma item por item: uma falha adiante não desfaz o que já foi lido.
            session.commit()
    # O que não apareceu nesta varredura saiu da pasta (apagado ou movido).
    session.execute(
        update(DriveItem)
        .where(
            DriveItem.folder_id == folder.id,
            DriveItem.drive_id != folder.drive_id,
            (DriveItem.last_seen_at < now) | DriveItem.last_seen_at.is_(None),
        )
        .values(removed=True)
    )


# Lista o conteúdo direto de uma pasta, página por página.
def _list_children(drive, parent_id: str):
    """Gera os itens (dicionários do Drive) dentro da pasta, sem os da lixeira."""
    # Marcador de página; None = primeira.
    token = None
    # Repete até acabar.
    while True:
        # files.list com filtro "dentro desta pasta e fora da lixeira". supportsAllDrives,
        # includeItemsFromAllDrives e corpora="allDrives" incluem drives compartilhados de
        # empresas (sem corpora, a busca pode se limitar ao "Meu Drive" e ao que foi
        # compartilhado diretamente com você).
        response = (
            drive.files()
            .list(
                q=f"'{parent_id}' in parents and trashed = false",
                fields=_FIELDS,
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
            )
            .execute(num_retries=3)
        )
        # Entrega os itens desta página.
        yield from response.get("files", [])
        # Próxima página, se houver.
        token = response.get("nextPageToken")
        # Acabou.
        if not token:
            return


# Atualiza (ou cria) o item no espelho e grava uma versão nova quando for o caso.
def _sync_item(
    session: Session,
    drive,
    store: FileStore,
    folder: DriveFolder,
    meta: dict[str, Any],
    # (id do Drive, caminho legível) da pasta que está sendo listada.
    parent: tuple[str, str],
    now: datetime,
    max_bytes: int,
    summary: SyncSummary,
) -> DriveItem:
    # Id e caminho da pasta-mãe.
    parent_id, parent_path = parent
    # Procura pelo id do Drive.
    item = session.scalars(select(DriveItem).where(DriveItem.drive_id == meta["id"])).first()
    # Novo.
    if item is None:
        # Cria.
        item = DriveItem(drive_id=meta["id"])
        # Prepara.
        session.add(item)
    # Pertence à pasta monitorada que o encontrou (mesmo que antes tenha chegado por link:
    # a pasta manda, porque é ela que dá as tags herdadas).
    item.folder_id = folder.id
    # Pasta-mãe: a pasta que estamos listando.
    item.parent_drive_id = parent_id
    # Nome, tipo, data, tamanho e link.
    _apply_meta(item, meta)
    # Caminho legível.
    item.path = f"{parent_path}/{item.name}"
    # Visto agora; se tinha sumido e voltou, deixa de estar removido.
    item.last_seen_at, item.removed = now, False
    # Pastas não têm conteúdo para baixar.
    if item.is_folder:
        return item
    # Lê a versão, se for nova.
    _read_version(session, drive, store, item, meta, max_bytes, summary)
    # Devolve.
    return item


# Copia para o item os dados que o Drive informou.
def _apply_meta(item: DriveItem, meta: dict[str, Any]) -> None:
    # Nome atual.
    item.name = meta.get("name") or meta["id"]
    # Tipo.
    item.mime_type = meta.get("mimeType")
    # Pasta?
    item.is_folder = meta.get("mimeType") == FOLDER_MIME
    # Última alteração.
    item.modified_time = _parse_time(meta.get("modifiedTime"))
    # Tamanho (vazio para Docs, Sheets e Slides).
    item.size_bytes = int(meta["size"]) if meta.get("size") else None
    # Link.
    item.web_link = meta.get("webViewLink")


# Mensagem para link de pasta: o sistema não varre pastas que só foram citadas.
_FOLDER_LINK = coded(
    "DRIVE_LINK_PASTA",
    "O link aponta para uma pasta. Para ler o conteúdo, cadastre-a em Configurações → "
    "Pastas do Drive.",
)


# Lê os arquivos do Drive citados por link nos e-mails, fora das pastas monitoradas.
def _sync_links(
    session: Session,
    drive,
    store: FileStore,
    settings: Settings,
    now: datetime,
    summary: SyncSummary,
) -> None:
    # Ids de arquivos citados em algum e-mail.
    refs = session.scalars(
        select(Attachment.external_ref)
        .where(Attachment.origin == "link_drive", Attachment.external_ref.is_not(None))
        .distinct()
    ).all()
    # Nenhum link: nada a fazer.
    if not refs:
        return
    # Pastas monitoradas ligadas.
    active = set(session.scalars(select(DriveFolder.id).where(DriveFolder.active.is_(True))))
    # Itens que já existem, por id do Drive.
    known = {
        i.drive_id: i
        for i in session.scalars(select(DriveItem).where(DriveItem.drive_id.in_(refs)))
    }
    # Limite em bytes para download (o mesmo das pastas).
    max_bytes = settings.drive_max_file_mb * 1024 * 1024
    # Intervalo entre conferências de um arquivo já lido.
    interval = timedelta(minutes=settings.drive_poll_minutes)
    # Cada arquivo citado.
    for ref in refs:
        # O item, se existir.
        item = known.get(ref)
        # Já está numa pasta monitorada ativa: a varredura da pasta cuida dele.
        if item is not None and item.folder_id in active and not item.removed:
            continue
        # Chegou por link e foi conferido há pouco: espera o intervalo.
        if (
            item is not None
            and item.folder_id is None
            and item.last_seen_at is not None
            and now - item.last_seen_at < interval
        ):
            continue
        # Link novo: cria o item, por enquanto só com o id.
        if item is None:
            # Nome provisório; o Drive informa o real logo abaixo.
            item = DriveItem(drive_id=ref, name=ref, path=ref)
            # Prepara.
            session.add(item)
        # Daqui em diante é acompanhado pelo link: sem pasta, sem pasta-mãe, presente.
        item.folder_id, item.parent_drive_id, item.removed = None, None, False
        # Conferido agora.
        item.last_seen_at = now
        # Conta.
        summary.links += 1
        # Pergunta ao Drive os dados atuais.
        try:
            # Mesmos campos da listagem das pastas.
            meta = (
                drive.files()
                .get(fileId=ref, fields=_ITEM_FIELDS, supportsAllDrives=True)
                .execute(num_retries=3)
            )
        # Sem acesso, apagado, bloqueado: guarda o motivo (aparece na página inicial).
        except HttpError as exc:
            # Motivo com código.
            item.error = explain(exc)
            # Conta.
            summary.file_errors += 1
            # Grava.
            session.commit()
            # Segue para o próximo.
            continue
        # Nome, tipo, data, tamanho e link.
        _apply_meta(item, meta)
        # Sem pasta, o caminho é só o nome.
        item.path = item.name
        # Link de pasta: avisa como ler o conteúdo, sem varrer.
        if item.is_folder:
            # Aviso no lugar do erro.
            item.error = _FOLDER_LINK
        # Arquivo: lê a versão, se for nova.
        else:
            # Baixa, extrai e grava, como nas pastas.
            _read_version(session, drive, store, item, meta, max_bytes, summary)
        # Confirma item por item.
        session.commit()


# Baixa, extrai e grava a versão atual de um arquivo, se for nova (ou se force=True).
def _read_version(
    session: Session,
    drive,
    store: FileStore,
    item: DriveItem,
    meta: dict[str, Any],
    max_bytes: int,
    summary: SyncSummary,
    force: bool = False,
) -> None:
    # Versão informada pelo Drive.
    version = str(meta.get("version") or meta.get("modifiedTime") or "")
    # Mesma versão já lida com sucesso: nada a fazer (a menos que peçam para forçar).
    if version == item.version and item.error is None and not force:
        return
    # Baixa (ou exporta).
    found = download(drive, meta, max_bytes)
    # Não deu para baixar: guarda o motivo; a próxima rodada tenta de novo.
    if found.data is None:
        # Motivo.
        item.error = found.error
        # Conta.
        summary.file_errors += 1
        # Fim.
        return
    # Lê o texto.
    result = extract_text(found.data, found.filename, found.mime_type)
    # Impressão digital do texto (ou dos bytes, se não houver texto).
    basis = result.text.encode() if result.text else found.data
    # SHA-256 em hexadecimal.
    digest = hashlib.sha256(basis).hexdigest()
    # Versão nova do Drive, conteúdo igual ao já gravado: só atualiza a versão.
    if digest == item.content_sha256:
        # Versão atual.
        item.version, item.error = version, None
        # Fim.
        return
    # Id único desta versão no bronze.
    external_id = f"{item.drive_id}:{version}"
    # Pode já existir (ex.: rodada anterior caiu depois de gravar).
    bronze = session.scalars(
        select(BronzeItem).where(
            BronzeItem.source == SourceKind.DRIVE, BronzeItem.external_id == external_id
        )
    ).first()
    # Não existe: grava.
    if bronze is None:
        # Guarda o original no disco (nome = SHA-256 do conteúdo).
        key = store.save(found.data, safe_suffix(found.filename))
        # Quem alterou por último, se o Drive informar.
        user = meta.get("lastModifyingUser") or {}
        # Linha do bronze.
        bronze = BronzeItem(
            # Fonte.
            source=SourceKind.DRIVE,
            # Id do arquivo + versão.
            external_id=external_id,
            # Agrupa as versões do mesmo arquivo, como uma conversa agrupa e-mails.
            thread_id=item.drive_id,
            # Quando foi alterado.
            occurred_at=item.modified_time,
            # Quem alterou.
            author=(user.get("emailAddress") or user.get("displayName") or None),
            # Nome do arquivo.
            subject=item.name[:1000],
            # Texto extraído, sem caractere nulo (o Postgres não aceita).
            body_text=(result.text or "").replace("\x00", ""),
            # Impressão digital do texto.
            content_sha256=digest,
            # Metadados.
            extra={
                # Id no Drive (as tags chegam por aqui, pela view bronze_item_tags).
                "drive_id": item.drive_id,
                # Caminho legível no momento da coleta.
                "caminho": item.path,
                # Tipo original no Drive.
                "mime": item.mime_type,
                # Versão do Drive.
                "versao": version,
                # Onde está o original no disco.
                "arquivo": key,
                # Motivo de não ter texto, se for o caso.
                "erro_extracao": result.error,
                # Link para abrir.
                "link": item.web_link,
            },
        )
        # Prepara.
        session.add(bronze)
        # Gera o id.
        session.flush()
        # Conta.
        summary.new_versions += 1
    # Aponta o item para a versão mais recente.
    item.bronze_item_id = bronze.id
    # Guarda a impressão digital, a versão e limpa o erro.
    item.content_sha256, item.version, item.error = digest, version, None


# Motivo de um arquivo do Drive não ter texto agora (download ou extração), ou None.
def item_failure(session: Session, item: DriveItem) -> str | None:
    """O erro de download do item ou, se baixou, o erro de extração da versão atual."""
    # Falhou no download.
    if item.error:
        return item.error
    # Versão atual no bronze.
    bronze = session.get(BronzeItem, item.bronze_item_id) if item.bronze_item_id else None
    # Erro de extração guardado nos metadados da versão.
    return (bronze.extra or {}).get("erro_extracao") if bronze is not None else None


# "Tentar de novo" do console para um arquivo do Drive.
def retry_drive_item(
    session: Session,
    drive,
    store: FileStore,
    settings: Settings,
    item_id: int,
    vision: VisionReader | None = None,
) -> str | None:
    """Tenta ler o arquivo agora. Devolve None se deu certo; senão o erro (com código)."""
    # O item.
    item = session.get(DriveItem, item_id)
    # Não existe, ou é pasta de uma pasta monitorada (pasta citada por link pode ser
    # tentada: talvez o link agora aponte para um arquivo acessível).
    if item is None or (item.is_folder and item.folder_id is not None):
        raise ValueError("Arquivo do Drive não encontrado.")
    # Versão atual no bronze.
    bronze = session.get(BronzeItem, item.bronze_item_id) if item.bronze_item_id else None
    # Já baixado, só a extração falhou: extrai de novo do original guardado, sem o Drive.
    if item.error is None and bronze is not None and bronze.extra.get("arquivo"):
        return _reextract_version(store, item, bronze, vision)
    # Falhou no download e o Google não está conectado: não há como tentar.
    if drive is None:
        return coded("GOOGLE_DESCONECTADO", "Conecte a conta Google para baixar de novo.")
    # Falhou no download: pergunta ao Drive os dados atuais do arquivo.
    try:
        # Mesmos campos da listagem.
        meta = (
            drive.files()
            .get(fileId=item.drive_id, fields=_ITEM_FIELDS, supportsAllDrives=True)
            .execute(num_retries=3)
        )
    # Sem acesso, apagado, bloqueado.
    except HttpError as exc:
        # Guarda e devolve o motivo.
        item.error = explain(exc)
        # Motivo.
        return item.error
    # Dados atuais no item.
    _apply_meta(item, meta)
    # Chegou por link (sem pasta): o caminho é só o nome, que agora o Drive informou.
    if item.folder_id is None:
        # Caminho atualizado.
        item.path = item.name
    # Pasta citada por link: continua sem varrer, com o mesmo aviso.
    if item.is_folder:
        # Aviso.
        item.error = _FOLDER_LINK
        # Devolve.
        return item.error
    # Lê de novo, mesmo que a versão seja a mesma.
    _read_version(
        session,
        drive,
        store,
        item,
        meta,
        settings.drive_max_file_mb * 1024 * 1024,
        SyncSummary(),
        force=True,
    )
    # Resultado: o erro de download ou de extração, ou None.
    error = item_failure(session, item)
    # Baixou uma imagem ou PDF digitalizado: lê com o Claude agora, sem esperar a fila.
    if is_pending(error):
        # Versão recém-gravada.
        bronze = session.get(BronzeItem, item.bronze_item_id)
        # Lê e devolve o resultado.
        return _reextract_version(store, item, bronze, vision)
    # Devolve.
    return error


# Extrai de novo o texto de uma versão a partir do original guardado no disco.
def _reextract_version(
    store: FileStore, item: DriveItem, bronze: BronzeItem, vision: VisionReader | None
) -> str | None:
    # Caminho do original (o nome tem a extensão, que diz o formato ao extrator).
    key = bronze.extra["arquivo"]
    # Extrai; imagem ou PDF digitalizado vai para o Claude.
    result, notice = extract_or_read(store.read(key), key, item.mime_type, vision)
    # Grava o resultado na versão.
    apply_version_text(item, bronze, result)
    # O aviso (sem chave, teto do mês) ou o erro, se ainda houver.
    return notice or result.error


# Grava o texto (ou o erro) de uma versão já baixada. Usado aqui e na fila do Claude.
def apply_version_text(item: DriveItem, bronze: BronzeItem, result: Extraction) -> None:
    # Texto derivado do original (o original não muda): pode ser atualizado.
    bronze.body_text = (result.text or "").replace("\x00", "")
    # Novo dicionário: o SQLAlchemy só percebe a mudança do JSON se o objeto for outro.
    bronze.extra = {**bronze.extra, "erro_extracao": result.error}
    # Com texto, a impressão digital passa a ser a do texto (como numa leitura normal).
    if result.text:
        # Atualiza nos dois lugares, para a próxima varredura comparar certo.
        bronze.content_sha256 = item.content_sha256 = hashlib.sha256(
            result.text.encode()
        ).hexdigest()


# Conveniência para a linha de comando e o agendador: monta tudo a partir da configuração.
def run_from_settings(settings: Settings) -> SyncSummary:
    """Carrega as credenciais do banco, cria o cliente do Drive e roda uma varredura."""
    # Imports locais: só carregam quando a varredura de verdade roda.
    from googleapiclient.discovery import build

    # Fábrica de sessões.
    from app.db import get_sessionmaker

    # Leitura das credenciais do Google.
    from app.google_auth import load_credentials

    # Criptografia dos tokens.
    from app.security import get_cipher

    # Fábrica de sessões.
    sessions = get_sessionmaker()
    # Credenciais válidas.
    with sessions() as session:
        # Lê e descriptografa.
        creds = load_credentials(session, get_cipher(), settings)
    # Cliente do Drive.
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    # Varre.
    return sync_drive(sessions, drive, FileStore(settings.data_dir), settings)


# Cliente do Drive para o console (cadastrar pasta), a partir das credenciais gravadas.
def drive_client(session: Session, settings: Settings):
    """Devolve um cliente da API do Drive, ou levanta erro se o Google não estiver conectado."""
    # Imports locais.
    from googleapiclient.discovery import build

    # Leitura das credenciais.
    from app.google_auth import load_credentials

    # Criptografia.
    from app.security import get_cipher

    # Credenciais.
    creds = load_credentials(session, get_cipher(), settings)
    # Cliente.
    return build("drive", "v3", credentials=creds, cache_discovery=False)
