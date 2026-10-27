"""Console web (FastAPI + HTMX)."""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import google_auth
from app.config import get_settings
from app.db import get_session, get_sessionmaker
from app.llm import BudgetExceededError, LLMClient, month_spend_usd
from app.models import AuditLog, OAuthToken
from app.security import get_cipher, require_owner
from app.transcription.routes import router as transcription_router

settings = get_settings()
app = FastAPI(title="Assistente PM", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.public_base_url.startswith("https://"),
    same_site="lax",
)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# htmx é baixado na construção da imagem Docker (ver Dockerfile).
app.include_router(transcription_router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

Owner = Annotated[str, Depends(require_owner)]
DB = Annotated[Session, Depends(get_session)]


@app.get("/saude")
def health() -> dict[str, str]:
    """Verificação de vida, sem dados. Usada pelo Docker."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, owner: Owner, db: DB):
    accounts = db.scalars(select(OAuthToken).order_by(OAuthToken.account_email)).all()
    recent = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(20)).all()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "owner": owner,
            "accounts": accounts,
            "recent": recent,
            "spent": month_spend_usd(db),
            "budget": settings.llm_monthly_budget_usd,
            "model": settings.llm_model,
        },
    )


@app.get("/auth/google/login")
def google_login(request: Request, owner: Owner):
    url, state, verifier = google_auth.start_login(settings)
    request.session["oauth_state"] = state
    request.session["oauth_verifier"] = verifier
    return RedirectResponse(url)


@app.get("/auth/google/callback")
def google_callback(request: Request, owner: Owner, db: DB):
    state = request.session.pop("oauth_state", None)
    verifier = request.session.pop("oauth_verifier", None)
    if not state or request.query_params.get("state") != state:
        raise HTTPException(status_code=400, detail="Login expirado ou inválido. Tente de novo.")
    if error := request.query_params.get("error"):
        raise HTTPException(status_code=400, detail=f"O Google recusou o login: {error}")
    # Por trás do tailscale serve a URL interna é http; o Google exige a URL pública.
    authorization_response = f"{settings.google_redirect_uri}?{request.url.query}"
    try:
        google_auth.finish_login(
            db,
            get_cipher(),
            settings,
            authorization_response=authorization_response,
            state=state,
            code_verifier=verifier,
        )
    except google_auth.MissingScopesError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/", status_code=303)


@app.post("/testes/llm", response_class=HTMLResponse)
def test_llm(request: Request, owner: Owner):
    """Uma chamada mínima ao Claude, para provar chave, registro e custo."""
    client = LLMClient(get_sessionmaker(), settings)
    try:
        result = client.generate(
            purpose="teste_console",
            prompt="Responda em uma frase curta, em português: você está funcionando?",
            max_tokens=2000,
            effort="low",
        )
    except BudgetExceededError as exc:
        return HTMLResponse(f"<p class='erro'>{exc}</p>")
    return templates.TemplateResponse(request, "_llm_result.html", {"result": result})
