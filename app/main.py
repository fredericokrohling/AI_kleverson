"""Console web (FastAPI + HTMX).

Por que FastAPI: é o framework web Python mais usado hoje para APIs. Ele valida
entradas pelos tipos declarados, tem suporte a WebSocket (usado na transcrição) e a
"dependências" (funções que rodam antes da rota, como a verificação de acesso).

Por que HTMX: o console precisa ser simples. Com HTMX o servidor devolve pedaços de
HTML e a página troca só aquele pedaço, sem escrever um app JavaScript separado
(React etc.). Menos código, menos coisa para aprender agora.
"""

# Path manipula caminhos de arquivos de forma independente do sistema operacional.
from pathlib import Path

# Annotated junta um tipo com informação extra (aqui, qual dependência resolver).
from typing import Annotated

# Peças do FastAPI: Depends declara dependência; HTTPException devolve erro HTTP;
# FastAPI é o aplicativo; Request é a requisição recebida.
from fastapi import Depends, FastAPI, HTTPException, Request

# Tipos de resposta: página HTML e redirecionamento para outro endereço.
from fastapi.responses import HTMLResponse, RedirectResponse

# Serve arquivos fixos (o htmx.min.js) direto de uma pasta.
from fastapi.staticfiles import StaticFiles

# Jinja2 preenche modelos HTML com dados (os arquivos em app/templates).
from fastapi.templating import Jinja2Templates

# select monta consultas ao banco.
from sqlalchemy import select

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Guarda dados entre requisições num cookie assinado (usado no login do Google).
from starlette.middleware.sessions import SessionMiddleware

# Módulo de login no Google.
from app import google_auth

# Leitura da configuração.
from app.config import get_settings

# Sessão por requisição e fábrica de sessões.
from app.db import get_session, get_sessionmaker

# Cliente do Claude, erro de teto e soma do gasto mensal.
from app.llm import BudgetExceededError, LLMClient, month_spend_usd

# Tabelas consultadas na página inicial.
from app.models import AuditLog, OAuthToken

# Criptografia e verificação de acesso.
from app.security import get_cipher, require_owner

# Carrega a configuração uma vez, ao iniciar.
settings = get_settings()
# Cria o app. docs_url/redoc_url/openapi_url=None desligam as páginas automáticas de
# documentação da API, para não expor a estrutura do sistema.
app = FastAPI(title="Assistente PM", docs_url=None, redoc_url=None, openapi_url=None)
# Middleware é uma camada que roda em toda requisição. Esta cuida do cookie de sessão.
app.add_middleware(
    # Classe da camada.
    SessionMiddleware,
    # Chave que assina o cookie; sem ela ninguém consegue forjar uma sessão.
    secret_key=settings.session_secret,
    # Em HTTPS, o cookie só viaja por conexão criptografada.
    https_only=settings.public_base_url.startswith("https://"),
    # "lax": o cookie vai junto ao voltar do Google, mas não em ataques de outros sites.
    same_site="lax",
)
# Onde ficam os modelos HTML: pasta templates ao lado deste arquivo.
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# htmx é baixado na construção da imagem Docker (ver Dockerfile).
# mount publica a pasta static no endereço /static.
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# Atalho: "owner: Owner" numa rota faz o FastAPI rodar require_owner antes dela.
Owner = Annotated[str, Depends(require_owner)]
# Atalho: "db: DB" numa rota entrega uma sessão do banco aberta para aquela requisição.
DB = Annotated[Session, Depends(get_session)]


# @app.get registra a função como resposta a GET /saude.
@app.get("/saude")
def health() -> dict[str, str]:
    """Verificação de vida, sem dados. Usada pelo Docker."""
    # O FastAPI converte o dicionário em JSON automaticamente.
    return {"status": "ok"}


# Página inicial; response_class diz que a resposta é HTML.
@app.get("/", response_class=HTMLResponse)
def index(request: Request, owner: Owner, db: DB):
    # Contas Google conectadas, em ordem alfabética.
    accounts = db.scalars(select(OAuthToken).order_by(OAuthToken.account_email)).all()
    # As 20 ações mais recentes do log de auditoria.
    recent = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(20)).all()
    # Preenche o modelo index.html com os dados abaixo.
    return templates.TemplateResponse(
        # A requisição (o Jinja2 precisa dela para montar links).
        request,
        # Qual modelo usar.
        "index.html",
        # Variáveis disponíveis dentro do HTML.
        {
            # Seu login.
            "owner": owner,
            # Contas conectadas.
            "accounts": accounts,
            # Ações recentes.
            "recent": recent,
            # Gasto do mês com o Claude.
            "spent": month_spend_usd(db),
            # Teto mensal.
            "budget": settings.llm_monthly_budget_usd,
            # Modelo em uso.
            "model": settings.llm_model,
        },
    )


# Botão "Conectar Google": inicia o login.
@app.get("/auth/google/login")
def google_login(request: Request, owner: Owner):
    # Gera o endereço do Google e os valores de segurança.
    url, state, verifier = google_auth.start_login(settings)
    # Guarda o state no cookie de sessão para conferir na volta.
    request.session["oauth_state"] = state
    # Guarda o code_verifier (PKCE) para usar na troca do código.
    request.session["oauth_verifier"] = verifier
    # Manda o navegador para a página do Google.
    return RedirectResponse(url)


# Endereço para onde o Google devolve a pessoa depois do consentimento.
@app.get("/auth/google/callback")
def google_callback(request: Request, owner: Owner, db: DB):
    # pop lê e apaga ao mesmo tempo: cada state só vale uma vez.
    state = request.session.pop("oauth_state", None)
    # Mesmo para o verifier.
    verifier = request.session.pop("oauth_verifier", None)
    # O state que voltou na URL tem que ser igual ao guardado; senão é login forjado.
    if not state or request.query_params.get("state") != state:
        # 400 = requisição inválida.
        raise HTTPException(status_code=400, detail="Login expirado ou inválido. Tente de novo.")
    # ":=" (operador morsa) atribui e testa ao mesmo tempo: se veio "error" na URL...
    if error := request.query_params.get("error"):
        # ...mostra o motivo que o Google informou (ex.: access_denied).
        raise HTTPException(status_code=400, detail=f"O Google recusou o login: {error}")
    # Por trás do tailscale serve a URL interna é http; o Google exige a URL pública.
    authorization_response = f"{settings.google_redirect_uri}?{request.url.query}"
    # Tenta concluir o login.
    try:
        # Troca o código pelo token e grava criptografado.
        google_auth.finish_login(
            # Sessão do banco.
            db,
            # Criptografia.
            get_cipher(),
            # Configuração.
            settings,
            # URL de retorno com o código.
            authorization_response=authorization_response,
            # Valores de segurança guardados no início.
            state=state,
            code_verifier=verifier,
        )
    # Se alguma permissão foi desmarcada, mostra quais.
    except google_auth.MissingScopesError as exc:
        # Converte em erro HTTP 400 com a mensagem explicativa.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Volta para a página inicial. 303 faz o navegador usar GET no novo endereço.
    return RedirectResponse("/", status_code=303)


# Botão "Testar Claude" (chamado via HTMX, devolve só um pedaço de HTML).
@app.post("/testes/llm", response_class=HTMLResponse)
def test_llm(request: Request, owner: Owner):
    """Uma chamada mínima ao Claude, para provar chave, registro e custo."""
    # Cria o cliente com a fábrica de sessões e a configuração.
    client = LLMClient(get_sessionmaker(), settings)
    # Tenta chamar.
    try:
        # Pergunta curta, esforço baixo: custa frações de centavo.
        result = client.generate(
            # Identificação no registro de custo.
            purpose="teste_console",
            # A pergunta.
            prompt="Responda em uma frase curta, em português: você está funcionando?",
            # Limite de tamanho da resposta.
            max_tokens=2000,
            # Esforço mínimo.
            effort="low",
        )
    # Teto atingido: mostra o aviso no lugar da resposta.
    except BudgetExceededError as exc:
        # Pedaço de HTML que o HTMX coloca na página.
        return HTMLResponse(f"<p class='erro'>{exc}</p>")
    # Preenche o modelo _llm_result.html com o resultado.
    return templates.TemplateResponse(request, "_llm_result.html", {"result": result})
