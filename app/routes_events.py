"""Página Na agenda e os botões "Pôr na agenda" / "Apagar da agenda".

Por que uma página própria: o assistente agora escreve na sua agenda, e tudo o que ele
escreve precisa estar à vista e ser fácil de desfazer. A página lista cada evento criado
(de qual fato, quem pediu, quando) e tem os três jeitos de tirar: um por um, "Apagar
todos" de uma Empresa e "Conferir com a agenda" (acerta o registro com o que você mudou
direto no Google).

Rotas:
- GET  /eventos: números, lista e os botões de cada Empresa;
- POST /eventos/criar: cria o evento de um fato (o botão das páginas Fatos, Briefing e
  Alertas) e volta para a página de onde veio;
- POST /eventos/{id}/apagar: tira um evento da agenda;
- POST /eventos/conferir: compara a agenda de uma Empresa com os registros;
- POST /eventos/apagar-todos: tira da agenda de uma Empresa todos os eventos com a marca.
"""

# Annotated junta um tipo com informação extra (campos da URL e do formulário).
from typing import Annotated

# Rotas, erro, formulário, parâmetro da URL e o pedido.
from fastapi import APIRouter, Form, HTTPException, Query, Request

# Respostas em HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Hoje no fuso do console.
from app.alerts import today_local

# Criação, apagamento, conferência e listas dos eventos.
from app.calendar_events import (
    EventError,
    create_for_fact,
    list_created,
    overview,
    remove,
    sync_company,
)

# Filtro de Empresa vindo da URL e a lista das Empresas.
from app.companies import filter_id, list_companies

# Configuração.
from app.config import get_settings

# Nomes legíveis dos tipos de fato.
from app.facts import KIND_LABELS

# Tabela dos eventos criados.
from app.models import CreatedEvent

# Peças comuns: dono, banco, conferência de origem e modelos HTML.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Páginas para onde um botão pode voltar (um link forjado não leva a outro site).
BACK_PAGES = ("/fatos", "/briefing", "/alertas", "/eventos")


# Endereço de volta seguro: só as páginas acima, nunca "//outro-site".
def _back(value: str | None) -> str:
    # Começa por uma das páginas e não por // (que o navegador leria como outro site).
    if value and value.startswith(BACK_PAGES) and not value.startswith("//"):
        return value
    # Senão, a página Na agenda.
    return "/eventos"


# Página Na agenda.
@router.get("/eventos", response_class=HTMLResponse)
def events_page(
    request: Request,
    owner: Owner,
    db: DB,
    # Empresa mostrada (vazia = todas).
    empresa: Annotated[str | None, Query(max_length=20)] = None,
    # "1" = mostra também os apagados.
    apagados: Annotated[str | None, Query(max_length=5)] = None,
):
    # Empresa do filtro (None = todas).
    company_id = filter_id(empresa)
    # Hoje no fuso do console.
    today = today_local(get_settings())
    # Página.
    return templates.TemplateResponse(
        request,
        "eventos.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "eventos",
            # Filtros escolhidos.
            "empresa": company_id,
            "com_apagados": apagados == "1",
            # Endereço atual (para os botões voltarem com os mesmos filtros).
            "aqui": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
            # Números.
            "resumo": overview(db, company_id, today),
            # Lista.
            "eventos": list_created(db, company_id, include_removed=apagados == "1"),
            # Empresas para os botões em lote (uma Empresa por vez).
            "empresas": [c for c in list_companies(db) if company_id in (None, c.id)],
            # Nomes dos tipos de fato.
            "tipos": KIND_LABELS,
            # Hoje.
            "hoje": today,
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
        },
    )


# Cria o evento de um fato.
@router.post("/eventos/criar", dependencies=[SameOrigin])
def create_event(
    request: Request,
    owner: Owner,
    db: DB,
    # O fato (só o id: título, dia e descrição vêm do banco, não do formulário).
    fato_id: Annotated[int, Form()],
    # Para onde voltar.
    volta: Annotated[str | None, Form(max_length=500)] = None,
):
    # Tenta criar.
    try:
        # Cria na agenda da Empresa do fato.
        row = create_for_fact(db, get_settings(), fato_id, owner)
    # Não deu: o aviso diz por quê, e nada fica gravado.
    except EventError as exc:
        # Desfaz o que estiver pendente.
        db.rollback()
        # Aviso vermelho.
        request.session["flash"] = {"texto": str(exc), "erro": True}
        # Volta.
        return RedirectResponse(_back(volta), status_code=303)
    # Grava.
    db.commit()
    # Aviso, com o link para a página Na agenda.
    request.session["flash"] = {
        "texto": f"“{row.title}” foi para a agenda em {row.day.strftime('%d/%m/%Y')}.",
        "link": "/eventos",
    }
    # Volta.
    return RedirectResponse(_back(volta), status_code=303)


# Tira um evento da agenda.
@router.post("/eventos/{row_id}/apagar", dependencies=[SameOrigin])
def remove_event(
    request: Request,
    owner: Owner,
    db: DB,
    row_id: int,
    # Para onde voltar.
    volta: Annotated[str | None, Form(max_length=500)] = None,
):
    # O registro.
    row = db.get(CreatedEvent, row_id)
    # Não existe.
    if row is None:
        raise HTTPException(status_code=404, detail="Evento não encontrado.")
    # Tenta apagar.
    try:
        # Apaga (confere a marca antes).
        message = remove(db, get_settings(), row, owner)
    # Não deu.
    except EventError as exc:
        # Desfaz o que estiver pendente.
        db.rollback()
        # Aviso vermelho.
        request.session["flash"] = {"texto": str(exc), "erro": True}
        # Volta.
        return RedirectResponse(_back(volta), status_code=303)
    # Grava.
    db.commit()
    # Aviso.
    request.session["flash"] = {"texto": message}
    # Volta.
    return RedirectResponse(_back(volta), status_code=303)


# Confere a agenda de uma Empresa, ou apaga todos os eventos com a marca.
def _sync(request: Request, owner: str, db, empresa: str | None, remove_all: bool):
    # Empresa (obrigatória: a conta Google é de uma Empresa).
    company_id = filter_id(empresa)
    # Sem Empresa.
    if company_id is None:
        raise HTTPException(status_code=400, detail="Escolha uma Empresa.")
    # Tenta.
    try:
        # Confere ou apaga.
        summary = sync_company(db, get_settings(), company_id, owner, remove_all=remove_all)
    # A agenda não respondeu.
    except EventError as exc:
        # Desfaz o que estiver pendente (inclusive o que já tinha sido apagado nesta
        # rodada continua apagado no Google; "Conferir" acerta os registros depois).
        db.rollback()
        # Aviso vermelho.
        request.session["flash"] = {"texto": str(exc), "erro": True}
        # Volta.
        return RedirectResponse(f"/eventos?empresa={company_id}", status_code=303)
    # Grava.
    db.commit()
    # Aviso.
    request.session["flash"] = {"texto": summary.describe()}
    # Volta para a Empresa.
    return RedirectResponse(f"/eventos?empresa={company_id}", status_code=303)


# Conferir com a agenda.
@router.post("/eventos/conferir", dependencies=[SameOrigin])
def check_agenda(
    request: Request,
    owner: Owner,
    db: DB,
    # Empresa.
    empresa: Annotated[str | None, Form(max_length=20)] = None,
):
    # Só confere.
    return _sync(request, owner, db, empresa, remove_all=False)


# Apagar todos os eventos do assistente na agenda de uma Empresa.
@router.post("/eventos/apagar-todos", dependencies=[SameOrigin])
def remove_all_events(
    request: Request,
    owner: Owner,
    db: DB,
    # Empresa.
    empresa: Annotated[str | None, Form(max_length=20)] = None,
):
    # Apaga.
    return _sync(request, owner, db, empresa, remove_all=True)
