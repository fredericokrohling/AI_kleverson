"""Página Alertas: o que lideranças pediram, decidiram ou apontaram, e as regras.

Por que uma página própria: os alertas são a lista de "o que precisa de você" vinda das
lideranças, separada dos fatos em geral (que são muitos). Ela mostra os novos e os de
peso alto primeiro, cada um com o trecho, quem disse e a regra que disparou; e embaixo
as regras, que você liga, desliga, edita e cria sem mexer em código.

Rotas:
- GET  /alertas: números, filtros, a lista e as regras;
- POST /alertas/{id}/situacao: visto, resolvido, descartado ou novo de novo;
- GET  /alertas/regras/nova e /alertas/regras/{id}: formulário da regra;
- POST /alertas/regras e /alertas/regras/{id}: grava (e já aplica a regra);
- POST /alertas/regras/{id}/apagar: apaga a regra e os alertas dela.
"""

# Annotated junta um tipo com informação extra (campos da URL e do formulário).
from typing import Annotated

# Rotas, erro, formulário, parâmetro da URL e o pedido.
from fastapi import APIRouter, Form, HTTPException, Query, Request

# Respostas em HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Regras, situações, geração e consultas da página.
from app.alerts import (
    LEADERSHIP,
    MAX_DUE_DAYS,
    OWNER_RULES,
    SEVERITIES,
    STATUSES,
    RuleError,
    delete_rule,
    describe_rule,
    generate,
    group_by_fact,
    list_alerts,
    list_rules,
    overview,
    save_rule,
    set_status,
    why,
)

# Eventos já criados na agenda (para o botão "Pôr na agenda" de cada prazo).
from app.calendar_events import active_by_fact

# Filtro de Empresa vindo da URL.
from app.companies import filter_id

# Configuração (janela de dias e fuso, usados pela geração).
from app.config import get_settings

# Nome de cada tipo de fato.
from app.facts import KIND_LABELS

# Tabelas.
from app.models import Alert, AlertRule

# Nomes legíveis das fontes.
from app.routes_content import SOURCE_LABELS

# Peças comuns: dono, banco, conferência de origem e modelos HTML.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Filtros de situação da página: valor da URL → texto do combo.
VIEWS = {"abertos": "Abertos (novos e vistos)", **STATUSES, "todos": "Todos"}


# Endereço de volta seguro: só páginas de Alertas (um link forjado não leva a outro site).
def _back(value: str | None) -> str:
    # Começa com /alertas e não com // (que o navegador leria como outro site).
    if value and value.startswith("/alertas") and not value.startswith("//"):
        return value
    # Senão, a página sem filtros.
    return "/alertas"


# Página Alertas.
@router.get("/alertas", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    owner: Owner,
    db: DB,
    # Empresa mostrada (vazia = todas).
    empresa: Annotated[str | None, Query(max_length=20)] = None,
    # Situação (vazia = abertos).
    situacao: Annotated[str | None, Query(max_length=20)] = None,
    # Uma regra (vazia = todas).
    regra: Annotated[str | None, Query(max_length=20)] = None,
):
    # Empresa do filtro (None = todas).
    company_id = filter_id(empresa)
    # Situação do filtro (desconhecida = abertos).
    view = situacao if situacao in VIEWS else "abertos"
    # Regra do filtro (só dígitos contam).
    rule_id = int(regra) if regra and regra.isdigit() else None
    # Lista, uma linha por fato (com as outras regras que dispararam para ele).
    alerts = group_by_fact(list_alerts(db, company_id, None if view == "todos" else view, rule_id))
    # Página.
    return templates.TemplateResponse(
        request,
        "alertas.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "alertas",
            # Filtros escolhidos.
            "empresa": company_id,
            "situacao": view,
            "regra": rule_id,
            # Endereço atual (para os botões voltarem com os mesmos filtros).
            "aqui": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
            # Números.
            "resumo": overview(db, company_id),
            # Lista.
            "alertas": alerts,
            # Fato → evento já criado na agenda (o botão "Pôr na agenda" vira "na agenda ✓").
            "na_agenda": active_by_fact(db, {row[2].id for row, _ in alerts}),
            # Regras com os abertos de cada uma.
            "regras": list_rules(db, company_id),
            # Textos.
            "situacoes": STATUSES,
            "vistas": VIEWS,
            "pesos": SEVERITIES,
            "tipos": KIND_LABELS,
            "labels": SOURCE_LABELS,
            # Funções de texto usadas no modelo.
            "descrever": describe_rule,
            # Dias para trás que uma regra olha.
            "janela": get_settings().alerts_lookback_days,
            "porque": why,
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
        },
    )


# Muda a situação de um alerta.
@router.post("/alertas/{alert_id}/situacao", dependencies=[SameOrigin])
def change_status(
    request: Request,
    owner: Owner,
    db: DB,
    alert_id: int,
    # Situação nova.
    situacao: Annotated[str, Form(max_length=20)],
    # Para onde voltar (a página com os filtros de antes).
    volta: Annotated[str | None, Form(max_length=500)] = None,
):
    # O alerta.
    alert = db.get(Alert, alert_id)
    # Não existe.
    if alert is None:
        raise HTTPException(status_code=404, detail="Alerta não encontrado.")
    # Situação desconhecida.
    if situacao not in STATUSES:
        raise HTTPException(status_code=400, detail="Situação desconhecida.")
    # Muda (e fecha ou reabre o fato) e registra no diário.
    set_status(db, alert, situacao, owner)
    # Grava.
    db.commit()
    # Volta para onde estava.
    return RedirectResponse(_back(volta), status_code=303)


# Dados do formulário da regra.
def _form_context(request: Request, owner: str, rule: AlertRule | None, erro: str | None = None):
    # Tudo o que o modelo usa.
    return {
        # Seu login.
        "owner": owner,
        # Item do menu.
        "menu": "alertas",
        # A regra (None = nova).
        "rule": rule,
        # Opções.
        "tipos": KIND_LABELS,
        "liderancas": LEADERSHIP,
        "responsaveis": OWNER_RULES,
        "pesos": SEVERITIES,
        "max_dias": MAX_DUE_DAYS,
        # Erro de preenchimento.
        "erro": erro,
        # Aviso pendente.
        "flash": request.session.pop("flash", None),
    }


# Formulário de regra nova.
@router.get("/alertas/regras/nova", response_class=HTMLResponse)
def new_rule_page(request: Request, owner: Owner):
    # Página com o formulário vazio.
    return templates.TemplateResponse(
        request, "alertas_regra.html", _form_context(request, owner, None)
    )


# Formulário de uma regra existente.
@router.get("/alertas/regras/{rule_id}", response_class=HTMLResponse)
def rule_page(request: Request, owner: Owner, db: DB, rule_id: int):
    # A regra.
    rule = db.get(AlertRule, rule_id)
    # Não existe.
    if rule is None:
        raise HTTPException(status_code=404, detail="Regra não encontrada.")
    # Página.
    return templates.TemplateResponse(
        request, "alertas_regra.html", _form_context(request, owner, rule)
    )


# Grava uma regra (nova ou existente) a partir do formulário.
def _save(
    request: Request,
    owner: str,
    db,
    rule_id: int | None,
    nome: str,
    empresa: str | None,
    tipos: list[str],
    lideranca: str,
    responsavel: str,
    prazo: str | None,
    peso: str,
    ativa: str | None,
):
    # Prazo: vazio = não importa; senão, um número inteiro.
    prazo = (prazo or "").strip()
    # Tenta gravar.
    try:
        # Prazo que não é número.
        if prazo and not prazo.isdigit():
            raise RuleError("O prazo é um número de dias (ou vazio).")
        # Confere e grava.
        rule = save_rule(
            db,
            rule_id,
            name=nome,
            company_id=filter_id(empresa),
            kinds=tipos,
            leadership=lideranca,
            owner_rule=responsavel,
            due_within_days=int(prazo) if prazo else None,
            severity=peso,
            active=ativa == "1",
            actor=owner,
        )
        # Aplica as regras agora: você vê o resultado sem esperar o coletor.
        summary = generate(db, get_settings())
        # Grava tudo junto.
        db.commit()
    # Preenchimento inválido: mostra o formulário de novo, com o erro.
    except RuleError as exc:
        # Desfaz o que estava pela metade.
        db.rollback()
        # Regra como estava (None = nova).
        current = db.get(AlertRule, rule_id) if rule_id is not None else None
        # Página com o erro (400 = pedido inválido).
        return templates.TemplateResponse(
            request,
            "alertas_regra.html",
            _form_context(request, owner, current, str(exc)),
            status_code=400,
        )
    # Novos desta regra, para o aviso.
    created = summary.by_rule.get(rule.name, 0)
    # Aviso.
    request.session["flash"] = {
        "texto": f"Regra “{rule.name}” gravada; {created} alerta(s) novo(s) com ela agora.",
        "erro": False,
    }
    # Volta para a página, filtrada pela regra.
    return RedirectResponse(f"/alertas?regra={rule.id}&situacao=todos", status_code=303)


# Campos do formulário (os mesmos na regra nova e na existente).
Nome = Annotated[str, Form(max_length=200)]
# Empresa ("" = todas).
Empresa = Annotated[str | None, Form(max_length=20)]
# Tipos marcados (caixas de seleção com o mesmo nome).
Tipos = Annotated[list[str] | None, Form()]
# Opções curtas.
Curto = Annotated[str, Form(max_length=20)]
# Opcionais curtos.
CurtoOpcional = Annotated[str | None, Form(max_length=20)]


# Cria uma regra.
@router.post("/alertas/regras", dependencies=[SameOrigin])
def create_rule(
    request: Request,
    owner: Owner,
    db: DB,
    nome: Nome,
    lideranca: Curto,
    responsavel: Curto,
    peso: Curto,
    empresa: Empresa = None,
    tipos: Tipos = None,
    prazo: CurtoOpcional = None,
    ativa: CurtoOpcional = None,
):
    # Grava.
    return _save(
        request,
        owner,
        db,
        None,
        nome,
        empresa,
        tipos or [],
        lideranca,
        responsavel,
        prazo,
        peso,
        ativa,
    )


# Grava uma regra existente.
@router.post("/alertas/regras/{rule_id}", dependencies=[SameOrigin])
def update_rule(
    request: Request,
    owner: Owner,
    db: DB,
    rule_id: int,
    nome: Nome,
    lideranca: Curto,
    responsavel: Curto,
    peso: Curto,
    empresa: Empresa = None,
    tipos: Tipos = None,
    prazo: CurtoOpcional = None,
    ativa: CurtoOpcional = None,
):
    # Grava.
    return _save(
        request,
        owner,
        db,
        rule_id,
        nome,
        empresa,
        tipos or [],
        lideranca,
        responsavel,
        prazo,
        peso,
        ativa,
    )


# Apaga uma regra (e os alertas dela).
@router.post("/alertas/regras/{rule_id}/apagar", dependencies=[SameOrigin])
def remove_rule(request: Request, owner: Owner, db: DB, rule_id: int):
    # Apaga e registra no diário.
    delete_rule(db, rule_id, owner)
    # Grava.
    db.commit()
    # Aviso.
    request.session["flash"] = {"texto": "Regra apagada, com os alertas dela.", "erro": False}
    # Volta.
    return RedirectResponse("/alertas", status_code=303)
