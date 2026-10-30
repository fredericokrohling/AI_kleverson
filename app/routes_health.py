"""Página Saúde (ícone de coração): testes dos serviços, versão instalada e terminal.

Os testes rodam sozinhos no serviço coletor (a cada HEALTH_POLL_MINUTES); o botão
"Testar agora" roda na hora, pelo próprio console. Se os resultados automáticos
pararem de chegar, a página avisa: é o sinal de que o serviço coletor caiu.

Os botões de terminal são links kleverson://, atendidos por um pequeno script instalado
no seu computador (scripts/terminal-local/). O console não executa nada no servidor:
quem conecta por SSH e roda o comando é o seu computador, com a sua chave.
"""

# Data e hora com fuso, e intervalos.
from datetime import UTC, datetime, timedelta

# APIRouter agrupa rotas; Request é o pedido.
from fastapi import APIRouter, Request

# Resposta HTML.
from fastapi.responses import HTMLResponse

# Leitura da configuração.
from app.config import get_settings

# Fábrica de sessões (os testes usam a sua própria sessão).
from app.db import get_sessionmaker

# Testes de saúde e versão.
from app.health import last_run_at, latest, run_checks, version_info

# Peças comuns: modelos HTML, dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Ações aceitas pelo script kleverson:// do seu computador, com a explicação de cada uma.
# A mesma lista existe no script; ações fora dela são recusadas lá.
TERMINAL_ACTIONS = [
    # Abre uma sessão SSH interativa no servidor.
    ("terminal", "Abrir terminal no servidor", "ssh no servidor, na pasta do projeto"),
    # Mostra os contêineres e a situação de cada um.
    ("status", "Ver serviços", "docker compose ps"),
    # Últimas linhas do log do coletor e do console.
    ("logs", "Ver logs", "docker compose logs --tail 100 app coletor"),
    # Reinicia os contêineres (pede confirmação no terminal).
    ("reiniciar", "Reiniciar serviços", "docker compose restart"),
    # Baixa a versão nova e reconstrói (pede confirmação no terminal).
    ("atualizar", "Aplicar atualização", "git pull && docker compose up -d --build"),
]


# Dados comuns à tabela de serviços (página inteira e pedaço do HTMX).
def _table_context(db) -> dict:
    # Configuração.
    settings = get_settings()
    # Agora.
    now = datetime.now(UTC)
    # Último teste de qualquer serviço.
    last = last_run_at(db)
    # Sem teste automático há mais de 3 intervalos: o coletor provavelmente parou.
    stale = last is None or now - last > timedelta(minutes=3 * settings.health_poll_minutes)
    # Resultados por serviço.
    services = latest(db)
    # Quantos com problema no último teste.
    problems = sum(1 for s in services if s["last"] is not None and s["last"].ok is False)
    # Junta.
    return {
        "services": services,
        "last": last,
        "stale": stale,
        "problems": problems,
        "minutes_ago": round((now - last).total_seconds() / 60) if last else None,
    }


# Página Saúde.
@router.get("/monitoramento", response_class=HTMLResponse)
def health_page(request: Request, owner: Owner, db: DB):
    # Configuração.
    settings = get_settings()
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "owner": owner,
            "menu": "saude",
            "flash": request.session.pop("flash", None),
            # Tabela de serviços.
            **_table_context(db),
            # Versão instalada x última (usa o cache de 10 min).
            "version": version_info(settings),
            # Repositório, para os links.
            "repo": settings.github_repo,
            # Botões de terminal.
            "actions": TERMINAL_ACTIONS,
        },
    )


# Botão "Testar agora" (HTMX): roda os testes e devolve só a tabela nova.
@router.post("/monitoramento/testar", response_class=HTMLResponse, dependencies=[SameOrigin])
def test_now(request: Request, owner: Owner, db: DB):
    # Roda e grava (sessão própria, fechada ao fim).
    run_checks(get_sessionmaker(), get_settings())
    # Encerra a leitura antiga desta sessão, para enxergar as linhas novas.
    db.commit()
    # Devolve o pedaço da tabela.
    return templates.TemplateResponse(request, "_health_table.html", _table_context(db))


# Botão "Verificar atualização" (HTMX): consulta o GitHub ignorando o cache.
@router.post("/monitoramento/versao", response_class=HTMLResponse, dependencies=[SameOrigin])
def check_version(request: Request, owner: Owner):
    # Configuração.
    settings = get_settings()
    # Devolve o pedaço da versão.
    return templates.TemplateResponse(
        request,
        "_version.html",
        {"version": version_info(settings, force=True), "repo": settings.github_repo},
    )
