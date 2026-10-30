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
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# HttpError é o erro que a biblioteca do Google levanta quando a API recusa o pedido.
from googleapiclient.errors import HttpError

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


# Baixa um arquivo pelo id. "drive" é o cliente da API (build("drive", "v3", ...)).
def fetch_drive_file(drive, file_id: str, max_bytes: int) -> DriveFile:
    """Baixa (ou exporta) um arquivo do Drive. Nunca levanta erro: falhas vão em `error`."""
    # Primeiro pede os dados do arquivo: nome, tipo e tamanho.
    try:
        # fields limita a resposta ao necessário; supportsAllDrives inclui drives
        # compartilhados de empresas; num_retries repete em falhas temporárias.
        meta = (
            drive.files()
            .get(fileId=file_id, fields="id,name,mimeType,size", supportsAllDrives=True)
            .execute(num_retries=3)
        )
    # Sem acesso ou inexistente.
    except HttpError as exc:
        # Devolve o motivo; o id fica registrado para você conferir o link.
        return DriveFile(file_id, f"drive:{file_id}", None, None, _explain(exc))
    # Nome e tipo do arquivo.
    name, mime = meta.get("name") or file_id, meta.get("mimeType")
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
