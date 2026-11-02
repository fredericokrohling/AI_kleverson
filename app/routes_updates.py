"""Rota da atualização automática: diz ao navegador se há novidades (ver app/updates.py)."""

# Annotated junta um tipo com informação extra (aqui, parâmetro da URL).
from typing import Annotated

# APIRouter agrupa rotas; Query lê parâmetros; Request é o pedido.
from fastapi import APIRouter, Query, Request

# Resposta em HTML.
from fastapi.responses import HTMLResponse

# Configuração.
from app.config import get_settings

# Marca do estado e o intervalo.
from app.updates import REFRESH_SECONDS, stamp

# Peças comuns: dono, banco e modelos HTML.
from app.web import DB, Owner, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()


# O navegador manda a marca que tem; a resposta traz a marca atual.
@router.get("/novidades", response_class=HTMLResponse)
def updates(
    request: Request,
    owner: Owner,
    db: DB,
    # Marca que a página tem agora.
    marca: Annotated[str, Query(max_length=200)] = "",
):
    # Marca atual do sistema.
    current = stamp(db, get_settings())
    # Devolve o mesmo vigia, já com a marca atual (para a próxima pergunta).
    response = templates.TemplateResponse(
        request,
        "_novidades.html",
        {"novidades_marca": current, "novidades_segundos": REFRESH_SECONDS},
    )
    # Mudou: o cabeçalho HX-Trigger faz o HTMX disparar o evento "novidades" na página,
    # e as seções que escutam se atualizam.
    if marca != current:
        # Evento.
        response.headers["HX-Trigger"] = "novidades"
    # Devolve.
    return response
