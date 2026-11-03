"""Página Configurações (ícone de engrenagem).

Seções: Contas conectadas (uma conta Google por Empresa), Claude (modelo, esforço, gasto,
teste; o mesmo para todas as Empresas), Transcrição (idioma padrão geral e o de cada
Empresa, porque um cliente pode ser do Brasil e outro de fora) e Servidor (disco por
projeto e uso de processador e memória nas últimas 2 horas ou 7 dias; ver
app/disk_usage.py e app/server_metrics.py).
As pastas do Drive monitoradas foram para Cadastros (pedido da dona em 03/11/2026; ver
app/routes_drive.py), junto com projetos, pessoas e tags.

Formulários comuns (sem JavaScript): cada botão envia um POST, a rota faz a mudança e
redireciona de volta para a página (padrão "POST, redireciona, GET"; assim recarregar a
página não repete a ação). O aviso de sucesso ou erro viaja no cookie de sessão
("flash"), em vez de no endereço, para ninguém conseguir montar um link com um aviso
falso.
"""

# os.cpu_count: quantos núcleos o processador do servidor tem.
import os

# Data e hora com fuso.
from datetime import UTC, datetime

# Annotated junta um tipo com informação extra (aqui, campos de formulário).
from typing import Annotated

# APIRouter agrupa rotas; Form lê campos de formulário.
from fastapi import APIRouter, Form, Request

# Tipos de resposta: página HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Consultas ao banco.
from sqlalchemy import select

# Geometria dos gráficos (arcos da rosca e barras).
from app.charts import bar_chart, donut_arcs

# Empresas: lista e troca do idioma.
from app.companies import list_companies, update_company

# Leitura da configuração.
from app.config import get_settings

# Fatias do disco (projetos, sistema, livre) e formatação de bytes.
from app.disk_usage import cached_breakdown, human, percent_text

# Gasto do mês com o Claude.
from app.llm import month_spend_usd

# Tabela consultada.
from app.models import Company, OAuthToken

# Opções e leitura/gravação das configurações do console.
from app.runtime_settings import (
    EFFORTS,
    LLM_MODELS,
    TRANSCRIPTION_LANGUAGES,
    current,
    set_value,
)

# Medições do servidor: períodos, barras e veredito.
from app.server_metrics import CPU_LIMIT, MEM_LIMIT, PERIODS, buckets, load_rows, summarize

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


# Dica da barra do processador.
def _describe_cpu(bucket) -> str:
    # Médias do pedaço, pico do minuto mais cheio e carga.
    return (
        f"trabalhando {bucket.cpu:.0f}% · espera de disco {bucket.iowait:.0f}% · "
        f"roubado {bucket.steal:.0f}% · minuto mais cheio {bucket.cpu_peak:.0f}% · "
        f"carga até {bucket.load_peak:.1f}"
    ).replace(".", ",")


# Dica da barra da memória.
def _describe_mem(bucket) -> str:
    # Média, pico e swap (se houver).
    text = f"memória {bucket.mem:.0f}% · pico {bucket.mem_peak:.0f}%"
    # Swap só quando o servidor tem.
    if bucket.swap_peak is not None:
        # Acrescenta.
        text += f" · swap até {bucket.swap_peak:.0f}%"
    # Devolve.
    return text


# Dados do bloco "Uso do servidor" para um período ("2h" ou "7d").
def _usage_context(db, periodo: str) -> dict:
    # Período pedido; qualquer outro valor vira 2 horas.
    key = periodo if periodo in PERIODS else "2h"
    # Definição do período.
    period = PERIODS[key]
    # Agora.
    now = datetime.now(UTC)
    # Medições do período.
    rows = load_rows(db, period, now)
    # Barras, no fuso do console.
    groups = buckets(rows, period, now, get_settings().console_timezone)
    # 7 dias = rótulos por dia.
    long = key == "7d"
    # Junta.
    return {
        # Período em uso.
        "periodo": key,
        # Opções dos botões.
        "periodos": PERIODS,
        # Resumo e veredito.
        "resumo": summarize(rows, period),
        # Núcleos do processador (a carga só faz sentido comparada a eles). Dentro do
        # contêiner, os.cpu_count() enxerga os núcleos do servidor.
        "nucleos": os.cpu_count() or 1,
        # Processador: trabalhando + espera de disco + roubado, empilhados.
        "grafico_cpu": bar_chart(
            groups, ["cpu", "iowait", "steal"], "cpu_peak", _describe_cpu, long, CPU_LIMIT
        ),
        # Memória: uma série só.
        "grafico_mem": bar_chart(groups, ["mem"], "mem_peak", _describe_mem, long, MEM_LIMIT),
    }


# Dados do bloco "Disco": fatias, arcos e textos.
def _disk_context(db) -> dict:
    # Fatias (guardadas por 10 minutos).
    disk = cached_breakdown(db, get_settings())
    # Raio do círculo da rosca, em unidades do desenho.
    radius = 70
    # Junta.
    return {
        # Fatias e totais.
        "disco": disk,
        # Arcos, na mesma ordem das fatias.
        "arcos": donut_arcs([s.percent for s in disk.slices], radius),
        # Raio, para o modelo desenhar o círculo.
        "raio": radius,
        # Formatação de bytes.
        "humano": human,
        # Formatação de porcentagens pequenas.
        "pct": percent_text,
    }


# Pedaço "Uso do servidor" (HTMX): troca de período e atualização a cada minuto.
@router.get("/config/servidor", response_class=HTMLResponse)
def server_usage(request: Request, owner: Owner, db: DB, periodo: str = "2h"):
    # Só o bloco dos gráficos.
    return templates.TemplateResponse(request, "_servidor_uso.html", _usage_context(db, periodo))


# Página Configurações.
@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, owner: Owner, db: DB, periodo: str = "2h"):
    # Configuração do .env.
    settings = get_settings()
    # Contas Google conectadas, na ordem das Empresas.
    accounts = db.scalars(select(OAuthToken).order_by(OAuthToken.account_email)).all()
    # Empresas (para o combo de conectar e a tabela de idiomas).
    companies = list_companies(db)
    # Empresas que já têm conta (o combo de conectar mostra as outras primeiro).
    with_account = {a.company_id for a in accounts}
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
            # Empresas.
            "companies": companies,
            # Ids das Empresas que já têm conta.
            "with_account": with_account,
            # Valores em vigor.
            "current": current(db, settings),
            # Opções de modelo, esforço e idioma.
            "models": LLM_MODELS,
            "efforts": EFFORTS,
            "languages": TRANSCRIPTION_LANGUAGES,
            # Gasto e teto do mês.
            "spent": month_spend_usd(db),
            "budget": settings.llm_monthly_budget_usd,
            # Disco por projeto (rosca).
            **_disk_context(db),
            # Uso do servidor (barras); ?periodo=7d abre direto nos 7 dias.
            **_usage_context(db, periodo),
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


# Salva o idioma padrão das reuniões de uma Empresa ("" volta para o padrão geral).
@router.post("/config/transcricao/{company_id}", dependencies=[SameOrigin])
def save_company_language(
    request: Request, owner: Owner, db: DB, company_id: int, idioma: Annotated[str, Form()] = ""
):
    # Tenta gravar.
    try:
        # Empresa (para manter o nome como está).
        company = db.get(Company, company_id)
        # Não existe.
        if company is None:
            raise ValueError("Empresa não encontrada. Recarregue a página.")
        # Só o idioma muda.
        update_company(db, company.id, company.name, idioma)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(request, f"Idioma de {company.name} salvo. Vale na próxima reunião dessa Empresa.")
    # Valor fora da lista ou Empresa inexistente.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta para a seção.
    return _back("/config#transcricao")
