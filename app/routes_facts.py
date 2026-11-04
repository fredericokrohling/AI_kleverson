"""Página Fatos e o botão "Extrair de novo" de cada item.

Por que uma página própria: os fatos (tarefas, decisões, riscos, prazos...) de todos os
itens ficam juntos, filtrados por Empresa, projeto e tipo. É a primeira visão da camada
silver, antes dos relatórios: dá para conferir se a extração está boa e ver os suspeitos
(trecho que o código não achou no texto original).

Rotas:
- GET  /fatos: números, filtros e a lista;
- POST /conteudo/{id}/fatos/refazer: apaga os fatos do item e devolve à fila.
"""

# Annotated junta um tipo com informação extra (aqui, campos da URL).
from typing import Annotated

# Rotas, erro, parâmetro da URL e o pedido.
from fastapi import APIRouter, HTTPException, Query, Request

# Respostas em HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Consulta dos projetos para o filtro.
from sqlalchemy import select

# Eventos já criados na agenda (o botão "Pôr na agenda" vira "na agenda ✓").
from app.calendar_events import active_by_fact

# Filtro de Empresa vindo da URL.
from app.companies import filter_id

# Lista, números, nomes dos tipos e "extrair de novo".
from app.facts import KIND_LABELS, list_facts, overview, redo_item

# Tabelas.
from app.models import BronzeItem, FactKind, Project

# Nomes legíveis das fontes.
from app.routes_content import SOURCE_LABELS

# Peças comuns: dono, banco, conferência de origem e modelos HTML.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()


# Página Fatos.
@router.get("/fatos", response_class=HTMLResponse)
def facts_page(
    request: Request,
    owner: Owner,
    db: DB,
    # Empresa mostrada (vazia = todas).
    empresa: Annotated[str | None, Query(max_length=20)] = None,
    # Projeto (vazio = todos).
    projeto: Annotated[str | None, Query(max_length=20)] = None,
    # Tipo de fato (vazio = todos).
    tipo: Annotated[str | None, Query(max_length=30)] = None,
    # "1" = só os suspeitos.
    suspeitos: Annotated[str | None, Query(max_length=5)] = None,
):
    # Empresa do filtro (None = todas).
    company_id = filter_id(empresa)
    # Projeto do filtro (só dígitos contam).
    project_id = int(projeto) if projeto and projeto.isdigit() else None
    # Tipo do filtro (valor desconhecido = todos).
    kind = FactKind(tipo) if tipo in {k.value for k in FactKind} else None
    # Projetos para o combo: os da Empresa escolhida, ou todos.
    query = select(Project).order_by(Project.name)
    # Só os da Empresa.
    if company_id is not None:
        query = query.where(Project.company_id == company_id)
    # Lista (até 200, mais recentes primeiro).
    facts = list_facts(db, company_id, project_id, kind, suspeitos == "1")
    # Página.
    return templates.TemplateResponse(
        request,
        "fatos.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "fatos",
            # Filtros escolhidos.
            "empresa": company_id,
            "projeto": project_id,
            "tipo": kind,
            "suspeitos": suspeitos == "1",
            # Números.
            "resumo": overview(db, company_id),
            # Lista.
            "fatos": facts,
            # Fato → evento já criado na agenda.
            "na_agenda": active_by_fact(db, {row[0].id for row in facts}),
            # Endereço atual (o botão "Pôr na agenda" volta para cá, com os filtros).
            "aqui": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
            # Projetos do combo.
            "projetos": db.scalars(query).all(),
            # Nome de cada tipo.
            "tipos": KIND_LABELS,
            # Nome de cada fonte.
            "labels": SOURCE_LABELS,
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
        },
    )


# Apaga os fatos de um item e devolve à fila (o coletor lê de novo em até 5 minutos).
@router.post("/conteudo/{item_id}/fatos/refazer", dependencies=[SameOrigin])
def redo_facts(request: Request, owner: Owner, db: DB, item_id: int):
    # O item.
    item = db.get(BronzeItem, item_id)
    # Não existe.
    if item is None:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Apaga e registra no diário.
    redo_item(db, item, owner)
    # Grava.
    db.commit()
    # Aviso.
    request.session["flash"] = {
        "texto": "Fatos apagados: o coletor lê este item de novo em até 5 minutos.",
        "erro": False,
    }
    # Volta para o item.
    return RedirectResponse(f"/conteudo/{item.id}", status_code=303)
