"""Botões "Tentar de novo" e "Dispensar" da lista de arquivos sem texto (página inicial).

O resultado aparece como aviso no topo da página: "funcionou" ou o erro com o código.
"""

# Annotated junta um tipo com informação extra (aqui, campos de formulário).
from typing import Annotated

# APIRouter agrupa rotas; Form lê campos de formulário; Request é o pedido.
from fastapi import APIRouter, Form, Request

# Redirecionamento.
from fastapi.responses import RedirectResponse

# Leitura da configuração.
from app.config import get_settings

# Cliente do Drive com as credenciais gravadas.
from app.ingestion.drive_sync import drive_client

# Tentativas.
from app.ingestion.failures import dismiss, retry, retry_all

# Guarda dos originais.
from app.ingestion.storage import FileStore

# Peças comuns: dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()


# Cliente do Drive, ou None se o Google não estiver conectado.
def _drive_or_none(db):
    # Tenta criar.
    try:
        # Cliente com as credenciais gravadas.
        return drive_client(db, get_settings())
    # Sem conta Google: as tentativas que precisam do Drive dizem isso na mensagem.
    except RuntimeError:
        return None


# Tenta de novo um arquivo.
@router.post("/arquivos/tentar", dependencies=[SameOrigin])
def retry_one(
    request: Request,
    owner: Owner,
    db: DB,
    # "anexo" ou "drive".
    origem: Annotated[str, Form()],
    # Id interno do arquivo.
    id: Annotated[int, Form()],
):
    # Configuração.
    settings = get_settings()
    # Tenta.
    try:
        # O cliente do Drive só é criado para arquivos do Drive.
        drive = _drive_or_none(db) if origem == "drive" else None
        # Resultado: None = funcionou.
        error = retry(db, FileStore(settings.data_dir), settings, origem, id, drive)
        # Grava o resultado (texto novo ou erro novo).
        db.commit()
    # Arquivo inexistente ou origem inválida.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Erro como resultado.
        error = str(exc)
    # Aviso para a página.
    request.session["flash"] = (
        {"texto": "Funcionou: o texto foi extraído.", "erro": False}
        if error is None
        else {"texto": f"Ainda com erro: {error}", "erro": True}
    )
    # Volta para a lista.
    return RedirectResponse("/#erros", status_code=303)


# Tenta de novo todos os arquivos da lista.
@router.post("/arquivos/tentar-todos", dependencies=[SameOrigin])
def retry_everything(request: Request, owner: Owner, db: DB):
    # Configuração.
    settings = get_settings()
    # Tenta todos.
    tried, fixed = retry_all(db, FileStore(settings.data_dir), settings, _drive_or_none(db))
    # Grava.
    db.commit()
    # Aviso com as contagens.
    request.session["flash"] = {
        "texto": f"{tried} arquivo(s) tentado(s), {fixed} agora com texto.",
        "erro": fixed < tried,
    }
    # Volta para a lista.
    return RedirectResponse("/#erros", status_code=303)


# Dispensa o aviso de um arquivo (some da lista; nada é apagado).
@router.post("/arquivos/dispensar", dependencies=[SameOrigin])
def dismiss_one(
    request: Request,
    owner: Owner,
    db: DB,
    # "anexo" ou "drive".
    origem: Annotated[str, Form()],
    # Id interno do arquivo.
    id: Annotated[int, Form()],
):
    # Tenta.
    try:
        # Marca como dispensado.
        dismiss(db, origem, id)
        # Grava.
        db.commit()
        # Aviso de sucesso.
        request.session["flash"] = {"texto": "Aviso dispensado.", "erro": False}
    # Arquivo inexistente ou origem inválida.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        request.session["flash"] = {"texto": str(exc), "erro": True}
    # Volta para a lista.
    return RedirectResponse("/#erros", status_code=303)
