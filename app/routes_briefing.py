"""Página Briefing: o que precisa de você hoje, uma Empresa embaixo da outra.

Por que uma página própria, e não só um quadro no Início: o briefing tem cinco seções
por Empresa e um histórico por dia; no Início fica só o resumo em números, com o link.

Rotas:
- GET  /briefing: o briefing do dia escolhido (hoje, por padrão). Abrir o de hoje marca
  como lido, e é isso que define onde começa o "o que mudou" do próximo;
- POST /briefing/gerar: monta agora o de hoje (de uma Empresa ou de todas), com os dados
  deste momento. Serve antes da hora do coletor ou depois de mexer nos cadastros.
"""

# Data (o dia escolhido na página).
from datetime import date

# Annotated junta um tipo com informação extra (campos da URL e do formulário).
from typing import Annotated

# Rotas, formulário, parâmetro da URL e o pedido.
from fastapi import APIRouter, Form, Query, Request

# Respostas em HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Hoje no fuso do console (a mesma conta dos alertas).
from app.alerts import today_local

# Montagem, consulta e leitura dos briefings.
from app.briefing import generate, mark_viewed, neighbor_days, of_day

# Eventos já criados na agenda (para o botão "Pôr na agenda" de cada prazo).
from app.calendar_events import active_by_fact

# Filtro de Empresa vindo da URL e a lista das Empresas.
from app.companies import filter_id, list_companies

# Configuração (hora do briefing, dias de prazo, fuso).
from app.config import get_settings

# Peças comuns: dono, banco, conferência de origem e modelos HTML.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()


# Lê a data da URL ("AAAA-MM-DD"); vazia, inválida ou no futuro vira hoje.
def _day(value: str | None, today: date) -> date:
    # Tenta ler.
    try:
        # Data do texto.
        day = date.fromisoformat(value) if value else today
    # Formato inválido.
    except ValueError:
        return today
    # Futuro não tem briefing.
    return min(day, today)


# Página Briefing.
@router.get("/briefing", response_class=HTMLResponse)
def briefing_page(
    request: Request,
    owner: Owner,
    db: DB,
    # Empresa mostrada (vazia = todas).
    empresa: Annotated[str | None, Query(max_length=20)] = None,
    # Dia (vazio = hoje).
    data: Annotated[str | None, Query(max_length=10)] = None,
):
    # Configuração.
    settings = get_settings()
    # Hoje no fuso do console.
    today = today_local(settings)
    # Empresa do filtro (None = todas).
    company_id = filter_id(empresa)
    # Dia mostrado.
    day = _day(data, today)
    # Briefings do dia.
    briefings = of_day(db, day, company_id)
    # Abrir o de hoje conta como leitura (os de dias passados não mudam a janela).
    if day == today and mark_viewed(briefings):
        # Grava a leitura.
        db.commit()
    # Dias vizinhos com briefing (setas).
    before, after = neighbor_days(db, day, company_id)
    # Empresas com briefing neste dia.
    with_briefing = {b.company_id for b in briefings}
    # Empresas que deviam aparecer e ainda não têm o de hoje (antes da hora do coletor).
    missing = [
        c
        for c in list_companies(db)
        if c.id not in with_briefing and (company_id is None or c.id == company_id)
    ]
    # Fatos dos prazos de todos os briefings mostrados (para os botões "Pôr na agenda").
    deadline_facts = {
        p["fato_id"]
        for b in briefings
        for group in b.sections["prazos"].values()
        for p in group
        if p.get("fato_id")
    }
    # Página.
    return templates.TemplateResponse(
        request,
        "briefing.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "briefing",
            # Filtros escolhidos.
            "empresa": company_id,
            "dia": day,
            "hoje": today,
            # Setas.
            "anterior": before,
            "seguinte": after,
            # Os briefings do dia.
            "briefings": briefings,
            # Fato → evento já criado na agenda.
            "na_agenda": active_by_fact(db, deadline_facts),
            # Endereço atual (o botão "Pôr na agenda" volta para cá).
            "aqui": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
            # Empresas sem o briefing do dia.
            "faltando": missing,
            # Hora em que o coletor monta.
            "hora": settings.briefing_hour,
            # Dias à frente dos prazos.
            "dias_prazo": settings.briefing_due_days,
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
        },
    )


# Monta agora o briefing de hoje.
@router.post("/briefing/gerar", dependencies=[SameOrigin])
def generate_now(
    request: Request,
    owner: Owner,
    db: DB,
    # Uma Empresa (vazia = todas).
    empresa: Annotated[str | None, Form(max_length=20)] = None,
):
    # Configuração.
    settings = get_settings()
    # Empresa escolhida (None = todas).
    company_id = filter_id(empresa)
    # Empresas a montar.
    companies = [c for c in list_companies(db) if company_id is None or c.id == company_id]
    # Cada uma.
    for company in companies:
        # Monta com os dados de agora (a agenda é lida de novo).
        generate(db, settings, company.id, trigger="manual", actor=owner)
    # Grava.
    db.commit()
    # Aviso no topo.
    request.session["flash"] = {
        "texto": f"Briefing montado agora para {len(companies)} Empresa(s)."
        if companies
        else "Nenhuma Empresa cadastrada."
    }
    # Volta para a página (abrir marca como lido).
    return RedirectResponse(
        "/briefing" + (f"?empresa={company_id}" if company_id is not None else ""),
        status_code=303,
    )
