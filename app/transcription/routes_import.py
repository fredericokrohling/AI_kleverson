"""Página Reuniões: dois calendários do dia (um por Empresa) e a importação de transcrição.

Fluxo da página:
1. a data começa em hoje. Os dois calendários lado a lado mostram o dia na agenda da
   conta de cada Empresa (combo no topo de cada um; o segundo é roxo com reuniões
   laranja, para não confundir). Trocar a data ou uma Empresa redesenha só os
   calendários e a lista de reuniões (HTMX busca esta mesma página e recorta os pedaços);
2. no formulário você escolhe a Empresa (obrigatória: a transcrição é dela), a reunião
   da agenda dessa Empresa (ou "Não está na agenda", digitando o título) e o arquivo;
3. se a reunião tem a transcrição do Meet anexada, o arquivo é opcional: o sistema baixa
   a transcrição do Drive;
4. o resultado aparece no topo da página: carregada, já existente, ou o motivo da falha.

Por que o formulário manda só o id da reunião: título, horário e participantes são lidos
de novo da agenda na hora de gravar. Assim o que vai para o banco é o que está na agenda,
e não o que o navegador mandou.
"""

# Data, data e hora, e só a hora (horário de início digitado).
from datetime import date, datetime, time

# Annotated junta um tipo com informação extra (aqui, campos de formulário).
from typing import Annotated

# Monta o "?a=1&b=2" do endereço.
from urllib.parse import urlencode

# Fusos horários pelo nome.
from zoneinfo import ZoneInfo

# Peças do FastAPI: rotas, campos de formulário, arquivo enviado e o pedido.
from fastapi import APIRouter, File, Form, Request, UploadFile

# Respostas HTML e de redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Erro devolvido pelas APIs do Google.
from googleapiclient.errors import HttpError

# Consultas.
from sqlalchemy import select

# Empresas: lista, conta de cada uma e a escolhida no combo.
from app.companies import accounts_by_company, filter_id, list_companies, parse_company

# Configuração.
from app.config import get_settings

# Cliente do Drive (para a transcrição do Meet).
from app.ingestion.drive_sync import drive_client

# Guarda dos originais.
from app.ingestion.storage import FileStore

# Tabelas.
from app.models import BronzeItem, SourceKind

# Dia da agenda desenhado como calendário.
from app.transcription.agenda import build_day, linked_transcripts

# Leitura da agenda.
from app.transcription.calendar import calendar_client, events_on, get_event

# Importação.
from app.transcription.importer import (
    ACCEPTED_SUFFIXES,
    MAX_FILE_BYTES,
    ImportFailure,
    fetch_meet_transcript,
    import_transcript,
)

# Peças comuns: modelos HTML, dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Valor da opção "Não está na agenda" na lista de reuniões.
OUTSIDE_CALENDAR = "__fora__"


# Erro do Google em uma linha: código HTTP e motivo informado.
def _google_error(exc: HttpError) -> str:
    # Ex.: "HTTP 403: Forbidden".
    return f"HTTP {exc.resp.status}: {exc.reason}"


# Hoje, no fuso do console.
def _today() -> date:
    # Data atual no fuso configurado.
    return datetime.now(ZoneInfo(get_settings().console_timezone)).date()


# Lê a data do calendário da página; inválida ou vazia vira hoje.
def _day(value: str | None) -> date:
    # Tenta ler "AAAA-MM-DD".
    try:
        # Data do texto.
        return date.fromisoformat(value) if value else _today()
    # Formato inválido.
    except ValueError:
        return _today()


# Reuniões do dia na agenda de uma Empresa, com a mensagem de erro se ela não puder ser
# lida. Sem Empresa, nenhuma (e nenhum erro: só falta escolher).
def _meetings(db, day: date, company_id: int | None) -> tuple[list, str | None]:
    # Sem Empresa.
    if company_id is None:
        return [], None
    # Configuração.
    settings = get_settings()
    # Tenta ler a agenda.
    try:
        # Cliente da agenda da conta da Empresa.
        calendar = calendar_client(db, settings, company_id)
        # Reuniões do dia.
        return events_on(calendar, day, settings.console_timezone), None
    # Sem conta Google conectada nessa Empresa.
    except RuntimeError as exc:
        return [], str(exc)
    # A API recusou (sem permissão, fora do ar).
    except HttpError as exc:
        # Código e motivo.
        return [], f"A agenda recusou o pedido ({_google_error(exc)})."


# Empresas dos dois calendários: as pedidas ou, na primeira visita, as duas primeiras
# (as que têm conta Google vêm antes).
def _calendar_companies(db, e1: str | None, e2: str | None) -> tuple[int | None, int | None]:
    # Ids existentes, em ordem alfabética.
    ids = [c.id for c in list_companies(db)]
    # Com conta.
    accounts = accounts_by_company(db)
    # Com conta primeiro.
    ordered = [i for i in ids if i in accounts] + [i for i in ids if i not in accounts]
    # Primeiro calendário.
    first = filter_id(e1) if filter_id(e1) in ids else (ordered[0] if ordered else None)
    # Segundo: o pedido, ou a próxima Empresa diferente da primeira.
    second = (
        filter_id(e2) if filter_id(e2) in ids else next((i for i in ordered if i != first), None)
    )
    # Os dois.
    return first, second


# Os dois calendários do dia, com as mesmas horas (para ficarem alinhados lado a lado).
def _calendars(db, day: date, companies: tuple[int | None, int | None]) -> list[dict]:
    # Fuso do console.
    zone = get_settings().console_timezone
    # Dados de cada calendário.
    result = []
    # Cada Empresa.
    for company_id in companies:
        # Reuniões e erro.
        events, error = _meetings(db, day, company_id)
        # Transcrições desta Empresa já ligadas às reuniões.
        linked = linked_transcripts(db, [e.id for e in events], company_id) if company_id else {}
        # Guarda (o dia é montado abaixo, depois de saber as horas dos dois).
        result.append({"empresa": company_id, "eventos": events, "erro": error, "ligadas": linked})
    # Primeira montagem: horas de cada um.
    first = [build_day(c["eventos"], c["ligadas"], day, zone) for c in result]
    # Horas que cobrem os dois.
    hours = (min(a.start_hour for a in first), max(a.end_hour for a in first))
    # Monta de novo com as mesmas horas.
    for c in result:
        # Dia desenhado.
        c["agenda"] = build_day(c["eventos"], c["ligadas"], day, zone, hours=hours)
    # Os dois.
    return result


# A página. Também atende o HTMX (trocar data ou Empresa): ele busca esta página e
# recorta só os calendários e a lista de reuniões (hx-select).
@router.get("/transcricoes/importar", response_class=HTMLResponse)
def import_page(
    request: Request,
    owner: Owner,
    db: DB,
    # Dia mostrado.
    data: str | None = None,
    # Reunião já escolhida (clique no calendário).
    evento: str | None = None,
    # Empresa do formulário de importação.
    empresa: str | None = None,
    # Empresas do primeiro e do segundo calendário.
    e1: str | None = None,
    e2: str | None = None,
):
    # Dia mostrado.
    day = _day(data)
    # Empresas dos calendários.
    companies = _calendar_companies(db, e1, e2)
    # Os dois calendários.
    calendars = _calendars(db, day, companies)
    # Empresa do formulário (só se existir).
    form_company = filter_id(empresa) if filter_id(empresa) in dict(_names(db)) else None
    # Reuniões da Empresa do formulário: aproveita a leitura de um calendário, se for a
    # mesma Empresa (uma consulta à agenda a menos).
    same = next((c for c in calendars if c["empresa"] == form_company), None)
    # Lista e erro.
    meetings, calendar_error = (
        (same["eventos"], same["erro"]) if same else _meetings(db, day, form_company)
    )
    # Últimas transcrições (importadas e da extensão), mais recentes primeiro.
    recent = db.scalars(
        # Itens do bronze...
        select(BronzeItem)
        # ... de transcrição de reunião...
        .where(BronzeItem.source == SourceKind.MEET_TRANSCRIPT)
        # ... mais recentes primeiro (sem data vão para o fim)...
        .order_by(BronzeItem.occurred_at.desc().nulls_last(), BronzeItem.id.desc())
        # ... até 10.
        .limit(10)
    ).all()
    # Monta a página.
    return templates.TemplateResponse(
        # A requisição.
        request,
        # Modelo da página.
        "transcricao_importar.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "reunioes",
            # Resultado da última importação.
            "flash": request.session.pop("flash", None),
            # Dia escolhido.
            "dia": day,
            # Hoje (limite do calendário: transcrição do futuro não existe).
            "hoje": _today(),
            # Os dois calendários.
            "calendarios": calendars,
            # Empresa do formulário.
            "empresa_form": form_company,
            # Reuniões da Empresa do formulário.
            "reunioes": meetings,
            # Reunião já escolhida na lista (vinda de um clique no calendário).
            "escolhido": evento,
            # Motivo, se a agenda não pôde ser lida.
            "erro_agenda": calendar_error,
            # Valor da opção "fora da agenda".
            "fora": OUTSIDE_CALENDAR,
            # Extensões aceitas (filtro do seletor de arquivo).
            "aceitos": ",".join(sorted(ACCEPTED_SUFFIXES)),
            # Últimas transcrições.
            "recentes": recent,
        },
    )


# (id, nome) das Empresas.
def _names(db) -> list[tuple[int, str]]:
    # Na ordem alfabética.
    return [(c.id, c.name) for c in list_companies(db)]


# Guarda o aviso do resultado e volta para a página, no mesmo dia e com as mesmas
# Empresas nos calendários e no formulário.
def _back(
    request: Request,
    day: date,
    text: str,
    error: bool = False,
    link: str | None = None,
    keep: dict[str, str] | None = None,
):
    # Aviso para o topo da página.
    request.session["flash"] = {"texto": text, "erro": error, "link": link}
    # Parâmetros do endereço: o dia e as Empresas escolhidas.
    params = {"data": day.isoformat(), **{k: v for k, v in (keep or {}).items() if v}}
    # 303: depois de um POST, o navegador abre a página com GET (F5 não reenvia o arquivo).
    return RedirectResponse(f"/transcricoes/importar?{urlencode(params)}", status_code=303)


# Recebe o formulário.
@router.post("/transcricoes/importar", dependencies=[SameOrigin])
def import_submit(
    request: Request,
    owner: Owner,
    db: DB,
    # Dia escolhido.
    data: Annotated[str, Form()] = "",
    # Id do evento, OUTSIDE_CALENDAR, ou vazio (nada escolhido).
    evento: Annotated[str, Form()] = "",
    # Título, para reunião fora da agenda.
    titulo: Annotated[str, Form()] = "",
    # Horário de início (HH:MM), para reunião fora da agenda; opcional.
    hora: Annotated[str, Form()] = "",
    # Arquivo (opcional quando o evento tem a transcrição do Meet anexada).
    arquivo: Annotated[UploadFile | None, File()] = None,
    # Empresa da transcrição (combo obrigatório).
    empresa: Annotated[str, Form()] = "",
    # Empresas dos calendários (para voltar com eles como estavam).
    e1: Annotated[str, Form()] = "",
    e2: Annotated[str, Form()] = "",
):
    # Dia.
    day = _day(data)
    # Configuração.
    settings = get_settings()
    # O que a página volta mostrando.
    keep = {"empresa": empresa, "e1": e1, "e2": e2}
    # Tenta a Empresa.
    try:
        # Sem ela, não importa (a transcrição seria de ninguém).
        company = parse_company(db, empresa)
    # Não escolheu ou não existe.
    except ValueError as exc:
        return _back(request, day, str(exc), error=True, keep=keep)
    # Nada escolhido na lista.
    if not evento:
        return _back(request, day, "Escolha a reunião na lista.", error=True, keep=keep)
    # Horário digitado (o navegador manda "HH:MM"); vazio = só o dia.
    try:
        # Lê.
        start_time = time.fromisoformat(hora) if hora.strip() else None
    # Formato estranho (formulário antigo ou adulterado).
    except ValueError:
        return _back(request, day, "Horário inválido; use HH:MM.", error=True, keep=keep)
    # Tenta importar.
    try:
        # Reunião da agenda, lida de novo (None = fora da agenda).
        event = None
        # Escolheu uma reunião da agenda.
        if evento != OUTSIDE_CALENDAR:
            # Lê o evento.
            event = get_event(calendar_client(db, settings, company.id), evento)
            # Apagado da agenda entre abrir a página e enviar.
            if event is None:
                raise ImportFailure("Essa reunião não está mais na agenda. Atualize a página.")
        # Conteúdo do arquivo; lê um byte a mais que o limite para saber se passou.
        content = arquivo.file.read(MAX_FILE_BYTES + 1) if arquivo and arquivo.filename else b""
        # Nome do arquivo.
        filename = arquivo.filename if arquivo and arquivo.filename else ""
        # Plataforma conhecida pela origem do arquivo.
        platform = None
        # Sem arquivo, mas com a transcrição do Meet anexada ao evento: baixa do Drive.
        if not content and event is not None and event.transcripts:
            # Primeira transcrição anexada.
            content, filename = fetch_meet_transcript(
                drive_client(db, settings, company.id), event.transcripts[0]
            )
            # Veio do Meet.
            platform = "meet"
        # Sem arquivo e sem transcrição anexada.
        if not filename:
            raise ImportFailure("Escolha o arquivo da transcrição.")
        # Grava.
        result = import_transcript(
            # Sessão do banco.
            db,
            # Pasta dos originais.
            FileStore(settings.data_dir),
            # Seu login.
            actor=owner,
            # Conteúdo.
            data=content,
            # Nome do arquivo.
            filename=filename,
            # Reunião da agenda (ou None).
            event=event,
            # Título digitado.
            title=titulo,
            # Dia escolhido.
            day=day,
            # Fuso do console.
            timezone=settings.console_timezone,
            # Empresa.
            company_id=company.id,
            # Plataforma, quando veio do Meet.
            platform=platform,
            # Horário digitado (só vale fora da agenda).
            start_time=start_time,
        )
    # Motivo conhecido (formato, arquivo vazio, reunião não escolhida...).
    except ImportFailure as exc:
        # Desfaz qualquer gravação pela metade.
        db.rollback()
        # Mostra o motivo.
        return _back(request, day, str(exc), error=True, keep=keep)
    # Sem conta Google conectada.
    except RuntimeError as exc:
        # Desfaz.
        db.rollback()
        # Mostra o motivo.
        return _back(request, day, str(exc), error=True, keep=keep)
    # A agenda ou o Drive recusaram.
    except HttpError as exc:
        # Desfaz.
        db.rollback()
        # Código e motivo.
        return _back(
            request,
            day,
            f"O Google recusou o pedido ({_google_error(exc)}).",
            error=True,
            keep=keep,
        )
    # Link para o conteúdo gravado.
    link = f"/conteudo/{result.item_id}"
    # Já existia.
    if not result.created:
        return _back(
            request,
            day,
            f"Essa transcrição já tinha sido importada (reunião “{result.title}”). "
            "Nada foi duplicado.",
            link=link,
            keep=keep,
        )
    # Resumo do que foi lido.
    t = result.transcript
    # Legenda: conta falas e pessoas.
    if t.turns:
        # Pessoas que falaram, quando o arquivo diz quem fala.
        people = f"{len(t.speakers)} pessoas" if t.speakers else "falantes não identificados"
        # Resumo.
        detail = f"{t.turns} falas, {people}"
    # Documento: só o tamanho do texto.
    else:
        # Resumo.
        detail = f"{len(t.text)} caracteres"
    # Sucesso.
    return _back(
        request,
        day,
        f"Transcrição carregada com sucesso em {company.name}: “{result.title}”, {detail}.",
        link=link,
        keep=keep,
    )
