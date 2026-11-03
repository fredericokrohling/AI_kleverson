"""Peças comuns às páginas do console: modelos HTML, acesso do dono e proteção de POST.

Proteção contra pedidos forjados por outros sites (CSRF): o console reconhece você pelo
cabeçalho que o Tailscale acrescenta a cada pedido que sai do seu computador. Isso vale
também para pedidos que um site malicioso, aberto no seu navegador, mande para o
endereço do console (um formulário escondido que envia POST para /config/...). O
navegador sempre informa de qual site o pedido saiu (cabeçalho Origin, ou Referer); por
isso todo POST só é aceito se vier do próprio console.
"""

# Data e hora (para o filtro que converte para o fuso do console).
from datetime import datetime

# Path manipula caminhos de arquivos.
from pathlib import Path

# Annotated junta um tipo com informação extra (aqui, qual dependência resolver).
from typing import Annotated

# urlsplit separa um endereço em partes (esquema, servidor, caminho).
from urllib.parse import urlsplit

# Fusos horários pelo nome.
from zoneinfo import ZoneInfo

# Depends declara dependência; HTTPException devolve erro HTTP; Request é o pedido.
from fastapi import Depends, HTTPException, Request

# Jinja2 preenche modelos HTML com dados (os arquivos em app/templates).
from fastapi.templating import Jinja2Templates

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Leitura da configuração.
from app.config import get_settings

# Sessão por requisição.
from app.db import get_session

# Verificação de acesso.
from app.security import require_owner


# Dados que toda página recebe, sem cada rota precisar lembrar: o estado do coração do menu
# e a marca da atualização automática (app/updates.py).
def _menu_state(request: Request) -> dict[str, object]:
    # Imports locais: evitam importação circular (health usa modelos que usam este módulo).
    # Lista das Empresas (para os combos de Empresa de todas as páginas).
    from app.companies import list_companies
    from app.db import get_sessionmaker

    # Teste de "algum problema agora".
    from app.health import has_problem

    # Marca do estado do sistema (atualização automática) e o intervalo.
    from app.updates import REFRESH_SECONDS, stamp

    # Tenta consultar.
    try:
        # Sessão curta só para isso.
        with get_sessionmaker()() as session:
            # Configuração.
            settings = get_settings()
            # Empresas, em ordem alfabética.
            companies = list_companies(session)
            # Vermelho se algum serviço falhou ou se os testes pararam; marca atual; e as
            # Empresas como (id, nome), para os combos, e id → nome, para as listas.
            return {
                "saude_alerta": has_problem(session, settings),
                "novidades_marca": stamp(session, settings),
                "novidades_segundos": REFRESH_SECONDS,
                "empresas_todas": [(c.id, c.name) for c in companies],
                "nomes_empresa": {c.id: c.name for c in companies},
            }
    # Banco fora do ar: isso já é um problema de saúde; marca vazia (a página não atualiza).
    except Exception:
        return {
            "saude_alerta": True,
            "novidades_marca": "",
            "novidades_segundos": REFRESH_SECONDS,
            "empresas_todas": [],
            "nomes_empresa": {},
        }


# Onde ficam os modelos HTML: pasta templates ao lado deste arquivo. context_processors:
# funções que acrescentam dados a toda página. Atenção: o que elas devolvem substitui o
# valor de mesmo nome que a rota passar; por isso os nomes acima são exclusivos.
templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates", context_processors=[_menu_state]
)


# Filtro "local" dos modelos: {{ x.created_at | local }} mostra a hora no fuso do console
# (o banco guarda em UTC).
def _local(value: datetime | None, fmt: str = "%d/%m %H:%M") -> str:
    # Sem data: nada.
    if value is None:
        return ""
    # Converte para o fuso do console e formata.
    return value.astimezone(ZoneInfo(get_settings().console_timezone)).strftime(fmt)


# Registra o filtro.
templates.env.filters["local"] = _local

# Atalho: "owner: Owner" numa rota faz o FastAPI rodar require_owner antes dela.
Owner = Annotated[str, Depends(require_owner)]
# Atalho: "db: DB" numa rota entrega uma sessão do banco aberta para aquela requisição.
DB = Annotated[Session, Depends(get_session)]


# "esquema://servidor[:porta]" de um endereço, para comparar origens.
def _origin_of(url: str) -> str:
    # Separa as partes.
    parts = urlsplit(url)
    # Junta só esquema e servidor, em minúsculas.
    return f"{parts.scheme}://{parts.netloc}".lower()


# Dependência para rotas POST: só aceita pedidos que saíram de uma página do console.
def same_origin(request: Request) -> None:
    """Recusa (403) POST vindo de outro site."""
    # Origens aceitas: o endereço público (Tailscale) e o endereço pelo qual o pedido
    # chegou (útil em testes e no acesso local).
    allowed = {_origin_of(get_settings().public_base_url), _origin_of(str(request.base_url))}
    # Origin: o navegador manda em todo POST.
    origin = request.headers.get("origin")
    # Sem Origin (navegadores antigos), usa o Referer (endereço da página de onde veio).
    if not origin:
        # Referer completo.
        referer = request.headers.get("referer")
        # Só a origem dele.
        origin = _origin_of(referer) if referer else None
    # Ausente ou de outro site: recusa.
    if origin is None or origin.lower() not in allowed:
        # 403 = proibido.
        raise HTTPException(status_code=403, detail="Pedido de outra origem recusado.")


# Atalho para pôr nas rotas POST: dependencies=[SameOrigin].
SameOrigin = Depends(same_origin)
