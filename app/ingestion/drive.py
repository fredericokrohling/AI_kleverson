"""Baixa o conteúdo de um arquivo do Google Drive, já convertido para um formato que o
extrator de texto entende.

Por que existe: em empresas que usam Google Workspace, o plano do projeto raramente é um
arquivo do Office; é uma planilha do Google. Usado pelo monitor de pastas
(app/ingestion/drive_sync.py).

Como:
- arquivos nativos do Google (Docs, Sheets, Slides) não têm um "arquivo" para baixar; a
  API os exporta para o formato do Office (DOCX, XLSX, PPTX). Assim o mesmo extrator
  dos anexos serve para os dois casos, e a planilha exportada mantém todas as abas
  (exportar para CSV traria só a primeira);
- arquivos comuns (um PDF guardado no Drive) são baixados como estão, até o limite
  DRIVE_MAX_FILE_MB.

Usa só a permissão drive.readonly: lê, nunca altera. Atenção: o limite de pastas é do
código, não da permissão. A drive.readonly dá leitura do Drive inteiro; o Google não
oferece permissão de leitura restrita a uma pasta (a drive.file só enxerga arquivos
criados pelo próprio app). O sistema só lê as pastas que você cadastra no console.
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
def explain(exc: HttpError) -> str:
    # Código HTTP da resposta (403, 404...).
    status = exc.resp.status
    # 403/404: sem acesso ao arquivo (não foi compartilhado com você) ou apagado.
    if status in (403, 404):
        return f"Sem acesso ao arquivo no Drive ou arquivo apagado (HTTP {status})."
    # Outros erros: mostra o código e o motivo informado pelo Google.
    return f"O Drive recusou o pedido (HTTP {status}): {exc.reason}"


# Tipo MIME de pasta no Drive.
FOLDER_MIME = "application/vnd.google-apps.folder"


# Baixa (ou exporta) um arquivo cujos dados (meta) já vieram de files.list ou files.get.
def download(drive, meta: dict, max_bytes: int) -> DriveFile:
    """Baixa o conteúdo. Nunca levanta erro de API: falhas vão em `error`."""
    # Id, nome e tipo do arquivo.
    file_id, name, mime = meta["id"], meta.get("name") or meta["id"], meta.get("mimeType")
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
        # Outro tipo nativo do Google (Formulários, Desenhos, atalhos): sem exportação útil.
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
        return DriveFile(file_id, name, mime, None, explain(exc))
