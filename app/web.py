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


# Dados que toda página recebe, sem cada rota precisar lembrar: o estado do coração do menu.
def _menu_state(request: Request) -> dict[str, bool]:
    # Imports locais: evitam importação circular (health usa modelos que usam este módulo).
    from app.db import get_sessionmaker

    # Teste de "algum problema agora".
    from app.health import has_problem

    # Tenta consultar.
    try:
        # Sessão curta só para isso.
        with get_sessionmaker()() as session:
            # Vermelho se algum serviço falhou ou se os testes pararam.
            return {"saude_alerta": has_problem(session, get_settings())}
    # Banco fora do ar: isso já é um problema de saúde.
    except Exception:
        return {"saude_alerta": True}


# Onde ficam os modelos HTML: pasta templates ao lado deste arquivo. context_processors:
# funções que acrescentam dados a toda página.
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
