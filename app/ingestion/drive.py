"""Baixa arquivos do Google Drive citados por link nos e-mails.

Por que: em empresas que usam Google Workspace, o plano do projeto raramente vem anexado;
vem um link para a planilha no Drive. Sem seguir o link, o assistente veria só
"segue o link do cronograma" e nada do cronograma.

Como:
- arquivos nativos do Google (Docs, Sheets, Slides) não têm um "arquivo" para baixar; a
  API os exporta para o formato do Office (DOCX, XLSX, PPTX). Assim o mesmo extrator
  dos anexos serve para os dois casos, e a planilha exportada mantém todas as abas
  (exportar para CSV traria só a primeira);
- arquivos comuns (um PDF guardado no Drive) são baixados como estão.

Usa só a permissão drive.readonly: lê, nunca altera.

Só baixa arquivos de dentro da pasta autorizada (DRIVE_FOLDER_ID), inclusive de
subpastas dela. Atenção: esse limite é do código, não da permissão. A drive.readonly dá
leitura do Drive inteiro; o Google não oferece permissão de leitura restrita a uma pasta
(a drive.file só enxerga arquivos criados pelo próprio app). O limite existe para não
encher o disco com arquivos que não interessam.
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# HttpError é o erro que a biblioteca do Google levanta quando a API recusa o pedido.
from googleapiclient.errors import HttpError

# Quantas pastas subir, no máximo, procurando a pasta autorizada (evita laço infinito
# e chamadas demais em estruturas de pastas muito fundas).
_MAX_ANCESTORS = 30

# Formato do Office para o qual cada tipo nativo do Google é exportado, e a extensão.
_EXPORTS = {
    # Google Docs → Word.
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    # Google Sheets → Excel.
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    # Google Slides → PowerPoint.
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}


# Resultado de baixar um arquivo do Drive.
@dataclass(frozen=True)
class DriveFile:
    # Id do arquivo no Drive.
    file_id: str
    # Nome do arquivo (com a extensão do formato exportado, se for nativo do Google).
    filename: str
    # Tipo MIME do conteúdo baixado.
    mime_type: str | None
    # Conteúdo; None quando não foi possível baixar.
    data: bytes | None
    # Motivo de não ter baixado; None quando deu certo.
    error: str | None = None


# Traduz erros da API do Google em frases que explicam o que aconteceu.
def _explain(exc: HttpError) -> str:
    # Código HTTP da resposta (403, 404...).
    status = exc.resp.status
    # 403/404: sem acesso ao arquivo (não foi compartilhado com você) ou apagado.
    if status in (403, 404):
        return f"Sem acesso ao arquivo no Drive ou arquivo apagado (HTTP {status})."
    # Outros erros: mostra o código e o motivo informado pelo Google.
    return f"O Drive recusou o download (HTTP {status}): {exc.reason}"


# Confere se o arquivo está dentro da pasta autorizada, subindo pelas pastas-mãe.
def is_inside_folder(
    drive, parents: list[str], folder_id: str, cache: dict[str, list[str]]
) -> bool:
    """True se alguma pasta acima do arquivo (em qualquer nível) for a pasta autorizada."""
    # Pastas ainda a examinar; começa pelas pastas-mãe diretas do arquivo.
    pending = list(parents)
    # Pastas já examinadas, para não repetir.
    seen: set[str] = set()
    # Examina até acabar ou atingir o limite.
    while pending and len(seen) < _MAX_ANCESTORS:
        # Próxima pasta.
        parent = pending.pop()
        # Achou a pasta autorizada.
        if parent == folder_id:
            return True
        # Já examinada: pula.
        if parent in seen:
            continue
        # Marca como examinada.
        seen.add(parent)
        # Pasta ainda não consultada nesta rodada: pergunta ao Drive quem está acima dela.
        if parent not in cache:
            # A consulta pode falhar (pasta de outra pessoa, sem acesso).
            try:
                # Só o campo "parents".
                cache[parent] = (
                    drive.files()
                    .get(fileId=parent, fields="parents", supportsAllDrives=True)
                    .execute(num_retries=3)
                    .get("parents", [])
                )
            # Sem acesso à pasta: trata como fim do caminho.
            except HttpError:
                # Nenhuma pasta acima.
                cache[parent] = []
        # Acrescenta as pastas de cima à lista.
        pending.extend(cache[parent])
    # Não achou.
    return False


# Baixa um arquivo pelo id. "drive" é o cliente da API (build("drive", "v3", ...)).
def fetch_drive_file(
    # Cliente da API do Drive.
    drive,
    # Id do arquivo.
    file_id: str,
    # Tamanho máximo para baixar.
    max_bytes: int,
    # Id da pasta autorizada.
    folder_id: str,
    # Pastas-mãe já consultadas (compartilhado na rodada, para não repetir chamadas).
    parents_cache: dict[str, list[str]],
) -> DriveFile:
    """Baixa (ou exporta) um arquivo do Drive. Nunca levanta erro: falhas vão em `error`."""
    # Primeiro pede os dados do arquivo: nome, tipo e tamanho.
    try:
        # fields limita a resposta ao necessário; supportsAllDrives inclui drives
        # compartilhados de empresas; num_retries repete em falhas temporárias.
        meta = (
            drive.files()
            .get(fileId=file_id, fields="id,name,mimeType,size,parents", supportsAllDrives=True)
            .execute(num_retries=3)
        )
    # Sem acesso ou inexistente.
    except HttpError as exc:
        # Devolve o motivo; o id fica registrado para você conferir o link.
        return DriveFile(file_id, f"drive:{file_id}", None, None, _explain(exc))
    # Nome e tipo do arquivo.
    name, mime = meta.get("name") or file_id, meta.get("mimeType")
    # Fora da pasta autorizada: não baixa, só registra o link com o motivo.
    if not is_inside_folder(drive, meta.get("parents", []), folder_id, parents_cache):
        # Motivo explicando como liberar.
        return DriveFile(
            file_id, name, mime, None, "Fora da pasta autorizada do Drive (DRIVE_FOLDER_ID)."
        )
    # Tenta baixar o conteúdo.
    try:
        # Arquivo nativo do Google que sabemos exportar.
        if mime in _EXPORTS:
            # Formato e extensão de destino.
            export_mime, suffix = _EXPORTS[mime]
            # Exporta. A API limita exportações a 10 MB; acima disso devolve erro.
            data = drive.files().export(fileId=file_id, mimeType=export_mime).execute(num_retries=3)
            # Devolve com a extensão do Office no nome, para o extrator reconhecer.
            return DriveFile(file_id, f"{name}{suffix}", export_mime, data)
        # Outro tipo nativo do Google (Formulários, Desenhos): não há exportação útil.
        if mime and mime.startswith("application/vnd.google-apps."):
            return DriveFile(file_id, name, mime, None, f"Tipo do Google não exportável: {mime}.")
        # Arquivo comum maior que o limite: não baixa, para não lotar o disco.
        if int(meta.get("size") or 0) > max_bytes:
            # Mostra o tamanho em MB.
            size_mb = int(meta["size"]) / 1024 / 1024
            # Mensagem com o limite configurado.
            return DriveFile(
                file_id,
                name,
                mime,
                None,
                f"Arquivo de {size_mb:.0f} MB acima do limite DRIVE_MAX_FILE_MB.",
            )
        # Arquivo comum: baixa o conteúdo como está.
        data = (
            drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute(num_retries=3)
        )
        # Devolve o arquivo.
        return DriveFile(file_id, name, mime, data)
    # Falha no download.
    except HttpError as exc:
        # Devolve o motivo.
        return DriveFile(file_id, name, mime, None, _explain(exc))
