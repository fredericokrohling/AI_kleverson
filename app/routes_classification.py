"""Página Classificação e as ações de projeto de cada item.

Por que uma página própria: a cascata (app/classification.py) decide sozinha a maioria
dos itens, mas o passo final dela é você. A fila "A revisar" junta o que o Claude decidiu
com pouca confiança (ou não conseguiu decidir); escolher o projeto ali trava a decisão.

Rotas:
- GET  /classificacao: números por método, gasto do mês e a fila "A revisar";
- POST /conteudo/{id}/projeto: você escolhe o projeto (ou "nenhum") de um item;
- POST /conteudo/{id}/projeto/automatico: devolve o item à classificação automática.
"""

# Annotated junta um tipo com informação extra (aqui, campos de formulário e da URL).
from typing import Annotated

# Rotas, formulário, erro, parâmetro da URL e o pedido.
from fastapi import APIRouter, Form, HTTPException, Query, Request

# Respostas em HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Fila, números, escolha manual e devolução à automática.
from app.classification import (
    METHOD_LABELS,
    active_projects,
    overview,
    release,
    review_queue,
    set_manual,
)

# Filtro de Empresa vindo da URL.
from app.companies import filter_id

# Configuração (confiança mínima, para mostrar na página).
from app.config import get_settings

# Tabelas.
from app.models import BronzeItem

# Nomes legíveis das fontes.
from app.routes_content import SOURCE_LABELS

# Peças comuns: dono, banco e modelos HTML.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()


# Página Classificação.
@router.get("/classificacao", response_class=HTMLResponse)
def classification_page(
    request: Request,
    owner: Owner,
    db: DB,
    # Empresa mostrada (vazia = todas).
    empresa: Annotated[str | None, Query(max_length=20)] = None,
):
    # Empresa do filtro (None = todas).
    company_id = filter_id(empresa)
    # Fila "A revisar".
    queue = review_queue(db, company_id)
    # Projetos ativos de cada Empresa que aparece na fila (para o combo de cada linha).
    projects = {
        cid: list(active_projects(db, cid).values()) for cid in {i.company_id for i, _, _ in queue}
    }
    # Página.
    return templates.TemplateResponse(
        request,
        "classificacao.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "classificacao",
            # Empresa do filtro.
            "empresa": company_id,
            # Números.
            "resumo": overview(db, company_id),
            # Fila.
            "fila": queue,
            # Projetos por Empresa.
            "projetos": projects,
            # Nome de cada fonte.
            "labels": SOURCE_LABELS,
            # Nome de cada método.
            "metodos": METHOD_LABELS,
            # Confiança mínima (para a explicação).
            "minimo": get_settings().classify_min_confidence,
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
        },
    )


# Endereço de volta: a página de onde o formulário veio (fila ou item), sem sair do console.
def _back(voltar: str, item_id: int) -> str:
    # Só caminhos internos conhecidos (nada de redirecionar para outro site).
    return voltar if voltar.startswith("/classificacao") else f"/conteudo/{item_id}"


# Você escolhe o projeto de um item. projeto vazio = "não é de nenhum projeto".
@router.post("/conteudo/{item_id}/projeto", dependencies=[SameOrigin])
def choose_project(
    request: Request,
    owner: Owner,
    db: DB,
    item_id: int,
    # Id do projeto ("" = nenhum).
    projeto: Annotated[str, Form()] = "",
    # Para onde voltar.
    voltar: Annotated[str, Form()] = "",
):
    # O item.
    item = db.get(BronzeItem, item_id)
    # Não existe.
    if item is None:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Tenta.
    try:
        # Grava e trava.
        set_manual(db, item, int(projeto) if projeto.isdigit() else None, owner)
        # Grava no banco.
        db.commit()
        # Aviso.
        request.session["flash"] = {
            "texto": "Projeto gravado; as regras não mudam mais este item.",
            "erro": False,
        }
    # Projeto de outra Empresa (o banco também recusaria).
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso.
        request.session["flash"] = {"texto": str(exc), "erro": True}
    # Volta.
    return RedirectResponse(_back(voltar, item.id), status_code=303)


# Devolve o item à classificação automática.
@router.post("/conteudo/{item_id}/projeto/automatico", dependencies=[SameOrigin])
def automatic_project(request: Request, owner: Owner, db: DB, item_id: int):
    # O item.
    item = db.get(BronzeItem, item_id)
    # Não existe.
    if item is None:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Apaga a decisão.
    release(db, item, owner)
    # Grava.
    db.commit()
    # Aviso.
    request.session["flash"] = {
        "texto": "Classificação automática: o projeto será decidido de novo em até 5 minutos.",
        "erro": False,
    }
    # Volta para o item.
    return RedirectResponse(f"/conteudo/{item.id}", status_code=303)
