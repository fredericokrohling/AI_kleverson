"""Página Configurações (ícone de engrenagem).

Seções: Contas conectadas, Claude (modelo, esforço, gasto, teste) e Transcrição (idioma).
As pastas do Drive monitoradas foram para Cadastros (pedido da dona em 03/11/2026; ver
app/routes_drive.py), junto com projetos, pessoas e tags.

Formulários comuns (sem JavaScript): cada botão envia um POST, a rota faz a mudança e
redireciona de volta para a página (padrão "POST, redireciona, GET"; assim recarregar a
página não repete a ação). O aviso de sucesso ou erro viaja no cookie de sessão
("flash"), em vez de no endereço, para ninguém conseguir montar um link com um aviso
falso.
"""

# Annotated junta um tipo com informação extra (aqui, campos de formulário).
from typing import Annotated

# APIRouter agrupa rotas; Form lê campos de formulário.
from fastapi import APIRouter, Form, Request

# Tipos de resposta: página HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Consultas ao banco.
from sqlalchemy import select

# Leitura da configuração.
from app.config import get_settings

# Gasto do mês com o Claude.
from app.llm import month_spend_usd

# Tabela consultada.
from app.models import OAuthToken

# Opções e leitura/gravação das configurações do console.
from app.runtime_settings import (
    EFFORTS,
    LLM_MODELS,
    TRANSCRIPTION_LANGUAGES,
    current,
    set_value,
)

# Peças comuns: modelos HTML, dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()


# Guarda um aviso para mostrar na próxima página.
def _flash(request: Request, message: str, error: bool = False) -> None:
    # O cookie de sessão guarda texto e tipo.
    request.session["flash"] = {"texto": message, "erro": error}


# Redireciona com 303 (o navegador faz GET no novo endereço).
def _back(url: str = "/config") -> RedirectResponse:
    # Resposta de redirecionamento.
    return RedirectResponse(url, status_code=303)


# Página Configurações.
@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, owner: Owner, db: DB):
    # Configuração do .env.
    settings = get_settings()
    # Contas Google conectadas.
    accounts = db.scalars(select(OAuthToken).order_by(OAuthToken.account_email)).all()
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "config",
            # Aviso pendente (e já apagado do cookie).
            "flash": request.session.pop("flash", None),
            # Contas.
            "accounts": accounts,
            # Valores em vigor.
            "current": current(db, settings),
            # Opções de modelo, esforço e idioma.
            "models": LLM_MODELS,
            "efforts": EFFORTS,
            "languages": TRANSCRIPTION_LANGUAGES,
            # Gasto e teto do mês.
            "spent": month_spend_usd(db),
            "budget": settings.llm_monthly_budget_usd,
        },
    )


# Salva modelo e esforço do Claude.
@router.post("/config/claude", dependencies=[SameOrigin])
def save_claude(
    request: Request,
    owner: Owner,
    db: DB,
    # Campo "modelo" do formulário.
    modelo: Annotated[str, Form()],
    # Campo "esforco" do formulário.
    esforco: Annotated[str, Form()],
):
    # Configuração.
    settings = get_settings()
    # Tenta gravar os dois.
    try:
        # Modelo.
        set_value(db, settings, "llm_model", modelo)
        # Esforço.
        set_value(db, settings, "llm_effort", esforco)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(request, "Configuração do Claude salva. Vale a partir da próxima chamada.")
    # Valor fora da lista.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta para a seção.
    return _back("/config#claude")


# Salva o idioma da transcrição.
@router.post("/config/transcricao", dependencies=[SameOrigin])
def save_transcription(request: Request, owner: Owner, db: DB, idioma: Annotated[str, Form()]):
    # Tenta gravar.
    try:
        # Idioma.
        set_value(db, get_settings(), "transcription_language", idioma)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(request, "Idioma salvo. Vale a partir da próxima transcrição iniciada.")
    # Valor fora da lista.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta para a seção.
    return _back("/config#transcricao")
