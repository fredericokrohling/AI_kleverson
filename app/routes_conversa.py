"""API da conversa com os dados (bloco 7): o que o servidor MCP local chama.

Quem chama: o programa mcp_local/kleverson_mcp.py, que roda no seu computador dentro do
Claude Desktop (ou do Claude Code). Ele não é um navegador: pede JSON e devolve JSON ao
Claude. As páginas do console continuam em HTML; esta parte é só para ele.

Proteção, em três camadas:
1. Tailscale: como todo o console, só responde a quem chega pela sua rede privada com o
   seu login (require_owner, em app/security.py).
2. Cabeçalho próprio: todo pedido precisa do cabeçalho "X-Kleverson-Cliente: mcp". Um site
   malicioso aberto no seu navegador consegue mandar um pedido simples para o console (e o
   Tailscale põe o seu login nele), mas o navegador não deixa um site de fora acrescentar
   um cabeçalho próprio sem perguntar antes ao console (o "preflight" do CORS), e o
   console nunca autoriza. Sem o cabeçalho, o pedido é recusado.
3. Origem: se o pedido vier com o cabeçalho Origin (só navegadores mandam), ele precisa
   ser do próprio console.

Rotas (todas sob /api/conversa; a Empresa é obrigatória, exceto na primeira):
- GET  /empresas: Empresas, projetos e o que está aberto;
- GET  /buscar: busca por palavra em e-mails, transcrições, Drive e anexos;
- GET  /fatos: tarefas, decisões, riscos, prazos, com filtros;
- GET  /alertas: alertas abertos;
- GET  /briefing: o briefing de um dia;
- GET  /reunioes: reuniões num período;
- GET  /fontes/{id}: o texto de um item ou de um anexo, em páginas;
- POST /fatos/{id}/agenda: põe o prazo de um fato na agenda (a única que escreve).
"""

# date = só a data (os filtros de período).
from datetime import date

# Annotated junta um tipo com informação extra (limites dos parâmetros).
from typing import Annotated

# Rotas, dependência, erro HTTP, parâmetros da URL e o pedido.
from fastapi import APIRouter, Depends, HTTPException, Query, Request

# Resposta em JSON.
from fastapi.responses import JSONResponse

# Hoje no fuso do console.
from app.alerts import today_local

# Configuração.
from app.config import get_settings

# As consultas.
from app.conversa import (
    ConversaError,
    alerts,
    briefing,
    companies_overview,
    facts,
    log_use,
    meetings,
    put_on_agenda,
    read_source,
    search,
)

# Peças comuns: dono e banco.
from app.web import DB, Owner, origin_of

# Nome e valor do cabeçalho que só o servidor MCP local manda.
CLIENT_HEADER = "X-Kleverson-Cliente"
# Valor esperado.
CLIENT_VALUE = "mcp"


# Dependência de todas as rotas desta API: o cabeçalho próprio e, se houver, a origem.
def require_mcp_client(request: Request) -> None:
    """Recusa (403) pedido sem o cabeçalho do servidor MCP ou vindo de outro site."""
    # Cabeçalho próprio ausente ou diferente.
    if request.headers.get(CLIENT_HEADER, "").strip().lower() != CLIENT_VALUE:
        raise HTTPException(status_code=403, detail="Pedido sem o cabeçalho do servidor MCP.")
    # Origem: só navegadores mandam; o servidor MCP local não manda.
    origin = request.headers.get("origin")
    # Se veio, precisa ser o próprio console.
    if origin:
        # Origens aceitas: o endereço do console e o endereço pelo qual o pedido chegou.
        allowed = {
            origin_of(get_settings().public_base_url),
            origin_of(str(request.base_url)),
        }
        # De outro site: recusa.
        if origin.lower() not in allowed:
            raise HTTPException(status_code=403, detail="Pedido de outra origem recusado.")


# Grupo de rotas: prefixo comum e a dependência acima em todas.
router = APIRouter(prefix="/api/conversa", dependencies=[Depends(require_mcp_client)])

# Tipo do id da Empresa: obrigatório e positivo.
CompanyParam = Annotated[int, Query(ge=1, description="Id da Empresa (veja /empresas).")]
# Tipo do limite de resultados.
LimitParam = Annotated[int | None, Query(ge=1, le=50)]
# Tipo da posição de início (página seguinte).
StartParam = Annotated[int, Query(ge=0)]


# Erro das consultas → JSON com a frase, no código HTTP certo. O servidor MCP local
# mostra a frase ao Claude, que explica para você.
def _error(exc: ConversaError) -> JSONResponse:
    # {"erro": "..."} com 400 ou 404.
    return JSONResponse({"erro": str(exc)}, status_code=exc.status)


# Empresas, projetos e contagens.
@router.get("/empresas")
def companies_route(owner: Owner, db: DB):
    # Consulta.
    result = companies_overview(db, get_settings())
    # Diário.
    log_use(db, owner, "empresas", None)
    # Grava o diário.
    db.commit()
    # Devolve.
    return {"empresas": result}


# Busca por palavra.
@router.get("/buscar")
def search_route(
    owner: Owner,
    db: DB,
    empresa: CompanyParam,
    # As palavras.
    q: Annotated[str, Query(min_length=1, max_length=200)],
    # Filtros.
    projeto: Annotated[int | None, Query(ge=1)] = None,
    fonte: Annotated[str | None, Query(max_length=20)] = None,
    desde: date | None = None,
    ate: date | None = None,
    limite: LimitParam = None,
    inicio: StartParam = 0,
):
    # Tenta.
    try:
        # Consulta.
        result = search(
            db,
            get_settings(),
            empresa,
            q,
            project_id=projeto,
            source=fonte,
            since=desde,
            until=ate,
            limit=limite,
            offset=inicio,
        )
    # Pedido inválido.
    except ConversaError as exc:
        return _error(exc)
    # Diário: a pergunta e quantos resultados saíram.
    log_use(db, owner, "buscar", empresa, pergunta=q, resultados=len(result["resultados"]))
    # Grava.
    db.commit()
    # Devolve.
    return result


# Fatos.
@router.get("/fatos")
def facts_route(
    owner: Owner,
    db: DB,
    empresa: CompanyParam,
    # Tipo (tarefa, decisao, risco...).
    tipo: Annotated[str | None, Query(max_length=20)] = None,
    projeto: Annotated[int | None, Query(ge=1)] = None,
    # "abertos" (padrão) ou "todos".
    situacao: Annotated[str, Query(pattern="^(abertos|todos)$")] = "abertos",
    # Responsável: "eu" ou parte do nome.
    responsavel: Annotated[str | None, Query(max_length=100)] = None,
    prazo_ate: date | None = None,
    desde: date | None = None,
    # Incluir os suspeitos (trecho não achado no original).
    suspeitos: bool = False,
    limite: LimitParam = None,
    inicio: StartParam = 0,
):
    # Tenta.
    try:
        # Consulta.
        result = facts(
            db,
            get_settings(),
            empresa,
            kind=tipo,
            project_id=projeto,
            open_only=situacao == "abertos",
            owner=responsavel,
            due_until=prazo_ate,
            since=desde,
            include_suspicious=suspeitos,
            limit=limite,
            offset=inicio,
        )
    # Pedido inválido.
    except ConversaError as exc:
        return _error(exc)
    # Diário.
    log_use(db, owner, "fatos", empresa, tipo=tipo, resultados=len(result["fatos"]))
    # Grava.
    db.commit()
    # Devolve.
    return result


# Alertas abertos.
@router.get("/alertas")
def alerts_route(owner: Owner, db: DB, empresa: CompanyParam, limite: LimitParam = None):
    # Tenta.
    try:
        # Consulta.
        result = alerts(db, get_settings(), empresa, limit=limite)
    # Empresa inexistente.
    except ConversaError as exc:
        return _error(exc)
    # Diário.
    log_use(db, owner, "alertas", empresa, resultados=len(result["alertas"]))
    # Grava.
    db.commit()
    # Devolve.
    return result


# Briefing de um dia (vazio = hoje).
@router.get("/briefing")
def briefing_route(owner: Owner, db: DB, empresa: CompanyParam, dia: date | None = None):
    # Configuração.
    settings = get_settings()
    # Dia pedido ou hoje no fuso do console.
    day = dia or today_local(settings)
    # Tenta.
    try:
        # Consulta.
        result = briefing(db, settings, empresa, day)
    # Empresa inexistente.
    except ConversaError as exc:
        return _error(exc)
    # Diário.
    log_use(db, owner, "briefing", empresa, dia=day.isoformat())
    # Grava.
    db.commit()
    # Devolve.
    return result


# Reuniões num período.
@router.get("/reunioes")
def meetings_route(
    owner: Owner,
    db: DB,
    empresa: CompanyParam,
    desde: date | None = None,
    ate: date | None = None,
    # Parte do título.
    titulo: Annotated[str | None, Query(max_length=100)] = None,
    limite: LimitParam = None,
):
    # Tenta.
    try:
        # Consulta.
        result = meetings(
            db, get_settings(), empresa, since=desde, until=ate, title=titulo, limit=limite
        )
    # Pedido inválido.
    except ConversaError as exc:
        return _error(exc)
    # Diário.
    log_use(db, owner, "reunioes", empresa, resultados=len(result["reunioes"]))
    # Grava.
    db.commit()
    # Devolve.
    return result


# O texto de um item (ou de um anexo dele).
@router.get("/fontes/{item_id}")
def source_route(
    owner: Owner,
    db: DB,
    item_id: int,
    empresa: CompanyParam,
    # Um anexo do item.
    anexo: Annotated[int | None, Query(ge=1)] = None,
    inicio: StartParam = 0,
    # E-mail: a conversa inteira em vez do texto próprio.
    conversa_inteira: bool = False,
):
    # Tenta.
    try:
        # Consulta.
        result = read_source(
            db,
            get_settings(),
            empresa,
            item_id,
            attachment_id=anexo,
            start=inicio,
            full_thread=conversa_inteira,
        )
    # Item de outra Empresa ou inexistente.
    except ConversaError as exc:
        return _error(exc)
    # Diário: qual item (e anexo) saiu para o Claude.
    log_use(db, owner, "ler_fonte", empresa, item=item_id, anexo=anexo, inicio=inicio or None)
    # Grava.
    db.commit()
    # Devolve.
    return result


# Põe o prazo de um fato na agenda.
@router.post("/fatos/{fact_id}/agenda")
def agenda_route(owner: Owner, db: DB, fact_id: int, empresa: CompanyParam):
    # Tenta.
    try:
        # Cria (o diário "agenda.evento_criado" é gravado lá dentro).
        result = put_on_agenda(db, get_settings(), empresa, fact_id, owner)
    # Não pode.
    except ConversaError as exc:
        # Desfaz o que tiver ficado pela metade.
        db.rollback()
        # Frase para o Claude.
        return _error(exc)
    # Diário: foi pela conversa.
    log_use(db, owner, "por_na_agenda", empresa, fato=fact_id)
    # Grava.
    db.commit()
    # Devolve.
    return result
