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

# Peças do FastAPI: HTTPException devolve erro HTTP; FastAPI é o aplicativo; Request é a
# requisição recebida.
from fastapi import FastAPI, HTTPException, Request

# Tipos de resposta: página HTML e redirecionamento para outro endereço.
from fastapi.responses import HTMLResponse, RedirectResponse

# Serve arquivos fixos (o htmx.min.js) direto de uma pasta.
from fastapi.staticfiles import StaticFiles

# select monta consultas ao banco.
from sqlalchemy import select

# Guarda dados entre requisições num cookie assinado (usado no login do Google).
from starlette.middleware.sessions import SessionMiddleware

# Módulo de login no Google.
from app import google_auth

# Seção Processamento (diário de ações por dia).
from app.activity import activity_page, parse_day

# Hoje no fuso do console (o quadro do briefing no Início).
from app.alerts import today_local

# Briefings de hoje (o quadro do Início).
from app.briefing import of_day

# Empresas: a escolhida nos formulários e a conta de cada uma.
from app.companies import accounts_by_company, filter_id, list_companies, parse_company

# Leitura da configuração.
from app.config import get_settings

# Fábrica de sessões.
from app.db import get_sessionmaker

# Último teste de saúde (o indicador do Início não diz "tudo funcionando" sem teste).
from app.health import last_run_at

# Lista de arquivos sem texto.
from app.ingestion.failures import list_failures

# Cliente do Claude e erro de teto.
from app.llm import BudgetExceededError, LLMClient

# Tabelas consultadas na página inicial.
from app.models import BronzeItem, OAuthToken, SourceKind

# Projetos de cada item (para mostrar ao lado da Empresa).
from app.project_tags import project_names

# Página Alertas e as regras de alerta de liderança (bloco 3).
from app.routes_alerts import router as alerts_router

# Página Briefing (bloco 5).
from app.routes_briefing import router as briefing_router

# Rotas da página Classificação (fila "A revisar" e projeto de cada item).
from app.routes_classification import router as classification_router

# Rotas da página Configurações.
from app.routes_config import router as config_router

# Rotas da página Processamento (números, coleta, diário e conteúdo coletado).
from app.routes_content import router as content_router

# Pastas do Drive monitoradas (em Cadastros) e a navegação pelas pastas.
from app.routes_drive import router as drive_router

# Página Fatos e "Extrair de novo" (bloco 3).
from app.routes_facts import router as facts_router

# Botões "Tentar de novo" dos arquivos sem texto.
from app.routes_failures import router as failures_router

# Rotas da página Saúde.
from app.routes_health import router as health_router

# Rotas da página Cadastros (projetos, pessoas e tags).
from app.routes_registry import router as registry_router

# Rota da atualização automática (pergunta "há novidades?").
from app.routes_updates import router as updates_router

# Criptografia.
from app.security import get_cipher

# Rotas da transcrição ao vivo (WebSocket, legendas, finalização), num módulo separado.
from app.transcription.routes import router as transcription_router

# Rotas da importação de transcrições.
from app.transcription.routes_import import router as import_router

# Rotas do Início ao vivo (canal de eventos, áreas das reuniões, participantes).
from app.transcription.routes_live import live_context
from app.transcription.routes_live import router as live_router

# Peças comuns: modelos HTML, dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin, templates

# Carrega a configuração uma vez, ao iniciar.
settings = get_settings()
# Cria o app. docs_url/redoc_url/openapi_url=None desligam as páginas automáticas de
# documentação da API, para não expor a estrutura do sistema.
app = FastAPI(title="Kleverson A.I.", docs_url=None, redoc_url=None, openapi_url=None)
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
# Página "Importar transcrição" (Teams e Meet). Vem antes das rotas da transcrição ao
# vivo: "/transcricoes/importar" também combinaria com "/transcricoes/{sessão}", e o
# FastAPI usa a primeira rota que combina.
app.include_router(import_router)
# Acrescenta ao app as rotas de transcrição definidas em app/transcription/routes.py.
app.include_router(transcription_router)
# Rotas da página Cadastros.
app.include_router(registry_router)
# Rotas da página Classificação e do projeto de cada item.
app.include_router(classification_router)
# Fatos.
app.include_router(facts_router)
# Alertas.
app.include_router(alerts_router)
# Briefing diário.
app.include_router(briefing_router)
# Rotas da página Configurações.
app.include_router(config_router)
# Rotas das pastas do Drive.
app.include_router(drive_router)
# Rotas do Início ao vivo.
app.include_router(live_router)
# Rotas da página Saúde.
app.include_router(health_router)
# Rotas de "Tentar de novo".
app.include_router(failures_router)
# Conteúdo coletado.
app.include_router(content_router)
# Acrescenta a rota da atualização automática.
app.include_router(updates_router)
# htmx é baixado na construção da imagem Docker (ver Dockerfile).
# mount publica a pasta static no endereço /static.
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# @app.get registra a função como resposta a GET /saude.
@app.get("/saude")
def health() -> dict[str, str]:
    """Verificação de vida, sem dados. Usada pelo Docker."""
    # O FastAPI converte o dicionário em JSON automaticamente.
    return {"status": "ok"}


# Só a seção Processamento, para o HTMX trocar de dia ou de página sem recarregar tudo.
@app.get("/processamento", response_class=HTMLResponse)
def processing(
    request: Request,
    owner: Owner,
    db: DB,
    data: str | None = None,
    pagina: int | None = None,
    empresa: str | None = None,
):
    # Eventos do dia e da página pedidos (só da Empresa do filtro, se houver).
    activity = activity_page(
        db,
        get_settings().console_timezone,
        parse_day(data),
        pagina,
        company_id=filter_id(empresa),
    )
    # Devolve só o pedaço da página.
    return templates.TemplateResponse(request, "_processamento.html", {"p": activity})


# Página inicial; response_class diz que a resposta é HTML. Mostra se os serviços estão
# funcionando, as últimas transcrições e os arquivos sem texto. Os números, a coleta e o
# diário de ações ficam na página Processamento (app/routes_content.py).
@app.get("/", response_class=HTMLResponse)
def index(request: Request, owner: Owner, db: DB):
    # Contas Google conectadas (a página só avisa se não houver nenhuma).
    accounts = db.scalars(select(OAuthToken).order_by(OAuthToken.account_email)).all()
    # Últimas 5 transcrições de reunião (importadas ou da extensão).
    meetings = db.scalars(
        # Itens do bronze...
        select(BronzeItem)
        # ... de transcrição de reunião...
        .where(BronzeItem.source == SourceKind.MEET_TRANSCRIPT)
        # ... mais recentes primeiro (sem data vão para o fim)...
        .order_by(BronzeItem.occurred_at.desc().nulls_last(), BronzeItem.id.desc())
        # ... só 5.
        .limit(5)
    ).all()
    # Empresas sem conta Google conectada (a página avisa).
    with_account = accounts_by_company(db)
    # Arquivos sem texto.
    failures = list_failures(db)
    # Preenche o modelo index.html com os dados abaixo.
    return templates.TemplateResponse(
        # A requisição (o Jinja2 precisa dela para montar links).
        request,
        # Qual modelo usar.
        "index.html",
        # Variáveis disponíveis dentro do HTML (o indicador de saúde usa saude_alerta, que
        # toda página já recebe; ver app/web.py).
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "inicio",
            # Aviso pendente (ex.: depois do login no Google).
            "flash": request.session.pop("flash", None),
            # Contas conectadas.
            "accounts": accounts,
            # Empresas que ainda não têm conta.
            "sem_conta": [c for c in list_companies(db) if c.id not in with_account],
            # Arquivos sem texto (anexos e Drive), com código, motivo, Empresa e projetos.
            "failures": failures,
            # Últimas transcrições de reunião.
            "reunioes": meetings,
            # Projetos de cada transcrição (pelas tags de projeto).
            "projetos_item": project_names(db, [r.id for r in meetings]),
            # Briefings de hoje (o quadro com os números e o link para a página Briefing).
            "briefings_hoje": of_day(db, today_local(settings)),
            # Hora em que o coletor monta o briefing (o quadro avisa antes dela).
            "briefing_hora": settings.briefing_hour,
            # Já houve algum teste automático de saúde?
            "saude_testada": last_run_at(db) is not None,
            # Reuniões ao vivo (ou salvas há pouco), cada uma com suas falas; o "**" junta
            # as chaves desse dicionário a este.
            **live_context(db),
        },
    )


# Botão "Conectar conta Google" (Configurações): inicia o login da conta de uma Empresa.
@app.get("/auth/google/login")
def google_login(request: Request, owner: Owner, db: DB, empresa: str | None = None):
    # A conta é sempre de uma Empresa: sem ela escolhida, não começa.
    try:
        # Empresa do combo.
        company = parse_company(db, empresa)
    # Não escolheu ou não existe.
    except ValueError as exc:
        # Aviso no topo de Configurações.
        request.session["flash"] = {"texto": str(exc), "erro": True}
        # Volta.
        return RedirectResponse("/config#contas", status_code=303)
    # Guarda a Empresa para a volta do Google (o Google não a conhece).
    request.session["oauth_company"] = company.id
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
    # Mesmo para a Empresa escolhida no início.
    company_id = request.session.pop("oauth_company", None)
    # O state que voltou na URL tem que ser igual ao guardado; senão é login forjado.
    if not state or not company_id or request.query_params.get("state") != state:
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
            # Empresa dona da conta.
            company_id=company_id,
        )
    # Se alguma permissão foi desmarcada, mostra quais.
    except google_auth.MissingScopesError as exc:
        # Converte em erro HTTP 400 com a mensagem explicativa.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Conta já ligada a outra Empresa, ou a Empresa já tem outra conta.
    except google_auth.AccountCompanyError as exc:
        # Aviso no topo de Configurações (nada foi gravado).
        request.session["flash"] = {"texto": str(exc), "erro": True}
        # Volta.
        return RedirectResponse("/config#contas", status_code=303)
    # Volta para as Configurações. 303 faz o navegador usar GET no novo endereço.
    return RedirectResponse("/config#contas", status_code=303)


# Botão "Testar Claude" (chamado via HTMX, devolve só um pedaço de HTML).
# dependencies=[SameOrigin]: recusa o pedido se ele vier de outro site (ver app/web.py).
@app.post("/testes/llm", response_class=HTMLResponse, dependencies=[SameOrigin])
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
