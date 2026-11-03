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

# Leitor do Claude para imagens e PDFs digitalizados (None sem chave da Anthropic).
from app.ingestion.vision import vision_reader

# Peças comuns: dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()


# Função "Empresa → cliente do Drive da conta dela" (None se a Empresa não tem conta).
# Cada arquivo é baixado de novo com a conta da Empresa dele, nunca com a de outra.
def _drive_for(db):
    # Clientes já montados nesta requisição.
    cache: dict[int, object] = {}

    # A função devolvida.
    def get(company_id: int):
        # Primeira vez desta Empresa: monta.
        if company_id not in cache:
            # Tenta.
            try:
                # Cliente da conta da Empresa.
                cache[company_id] = drive_client(db, get_settings(), company_id)
            # Empresa sem conta: a tentativa diz isso na mensagem.
            except RuntimeError:
                # Sem cliente.
                cache[company_id] = None
        # Devolve.
        return cache[company_id]

    # Devolve a função.
    return get


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
        # Resultado: None = funcionou. O cliente do Drive só é criado se for preciso.
        error = retry(
            db,
            FileStore(settings.data_dir),
            settings,
            origem,
            id,
            vision=vision_reader(settings),
            drive_for=_drive_for(db),
        )
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
    tried, fixed = retry_all(
        db,
        FileStore(settings.data_dir),
        settings,
        vision=vision_reader(settings),
        drive_for=_drive_for(db),
    )
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
