"""Página "Importar transcrição": escolhe o dia, a reunião da agenda e o arquivo.

Fluxo da página:
1. o calendário de data começa em hoje; trocar a data recarrega só a lista de reuniões
   (HTMX), lida da sua agenda do Google;
2. você escolhe a reunião (ou "Não está na agenda", digitando o título) e o arquivo;
3. se a reunião tem a transcrição do Meet anexada, o arquivo é opcional: o sistema baixa
   a transcrição do Drive;
4. o resultado aparece no topo da página: carregada, já existente, ou o motivo da falha.

Por que o formulário manda só o id da reunião: título, horário e participantes são lidos
de novo da agenda na hora de gravar. Assim o que vai para o banco é o que está na agenda,
e não o que o navegador mandou.
"""

# Data, data e hora.
from datetime import date, datetime

# Annotated junta um tipo com informação extra (aqui, campos de formulário).
from typing import Annotated

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


# Reuniões do dia para a lista, com a mensagem de erro se a agenda não puder ser lida.
def _meetings(db, day: date) -> tuple[list, str | None]:
    # Configuração.
    settings = get_settings()
    # Tenta ler a agenda.
    try:
        # Cliente da agenda.
        calendar = calendar_client(db, settings)
        # Reuniões do dia.
        return events_on(calendar, day, settings.console_timezone), None
    # Sem conta Google conectada.
    except RuntimeError as exc:
        return [], str(exc)
    # A API recusou (sem permissão, fora do ar).
    except HttpError as exc:
        # Código e motivo.
        return [], f"A agenda recusou o pedido ({_google_error(exc)})."


# O dia de calendário: reuniões posicionadas e quais já têm transcrição.
def _agenda(db, day: date, meetings: list):
    # Transcrições já ligadas às reuniões do dia.
    linked = linked_transcripts(db, [m.id for m in meetings])
    # Monta a grade no fuso do console.
    return build_day(meetings, linked, day, get_settings().console_timezone)


# A página. evento (opcional) já deixa a reunião escolhida: é o que acontece quando você
# clica numa reunião sem transcrição no calendário da página inicial.
@router.get("/transcricoes/importar", response_class=HTMLResponse)
def import_page(
    request: Request, owner: Owner, db: DB, data: str | None = None, evento: str | None = None
):
    # Dia mostrado.
    day = _day(data)
    # Reuniões desse dia.
    meetings, calendar_error = _meetings(db, day)
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
            # Reuniões do dia.
            "reunioes": meetings,
            # O mesmo dia desenhado como calendário.
            "agenda": _agenda(db, day, meetings),
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


# Só a lista de reuniões, para o HTMX trocar quando a data muda.
@router.get("/transcricoes/importar/reunioes", response_class=HTMLResponse)
def meetings_list(request: Request, owner: Owner, db: DB, data: str | None = None):
    # Dia pedido.
    day = _day(data)
    # Reuniões do dia.
    meetings, calendar_error = _meetings(db, day)
    # Devolve o pedaço da página.
    return templates.TemplateResponse(
        # A requisição.
        request,
        # Modelo da lista.
        "_reunioes.html",
        {
            # Reuniões do dia.
            "reunioes": meetings,
            # Motivo, se a agenda não pôde ser lida.
            "erro_agenda": calendar_error,
            # Valor da opção "fora da agenda".
            "fora": OUTSIDE_CALENDAR,
            # O calendário do novo dia vai junto e o HTMX troca o da página também
            # ("out of band": um pedaço da resposta que vai para outro lugar da página).
            "agenda": _agenda(db, day, meetings),
            # Liga a troca "out of band" no modelo.
            "agenda_oob": True,
        },
    )


# Guarda o aviso do resultado e volta para a página, no mesmo dia.
def _back(request: Request, day: date, text: str, error: bool = False, link: str | None = None):
    # Aviso para o topo da página.
    request.session["flash"] = {"texto": text, "erro": error, "link": link}
    # 303: depois de um POST, o navegador abre a página com GET (F5 não reenvia o arquivo).
    return RedirectResponse(f"/transcricoes/importar?data={day.isoformat()}", status_code=303)


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
    # Arquivo (opcional quando o evento tem a transcrição do Meet anexada).
    arquivo: Annotated[UploadFile | None, File()] = None,
):
    # Dia.
    day = _day(data)
    # Configuração.
    settings = get_settings()
    # Nada escolhido na lista.
    if not evento:
        return _back(request, day, "Escolha a reunião na lista.", error=True)
    # Tenta importar.
    try:
        # Reunião da agenda, lida de novo (None = fora da agenda).
        event = None
        # Escolheu uma reunião da agenda.
        if evento != OUTSIDE_CALENDAR:
            # Lê o evento.
            event = get_event(calendar_client(db, settings), evento)
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
                drive_client(db, settings), event.transcripts[0]
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
            # Plataforma, quando veio do Meet.
            platform=platform,
        )
    # Motivo conhecido (formato, arquivo vazio, reunião não escolhida...).
    except ImportFailure as exc:
        # Desfaz qualquer gravação pela metade.
        db.rollback()
        # Mostra o motivo.
        return _back(request, day, str(exc), error=True)
    # Sem conta Google conectada.
    except RuntimeError as exc:
        # Desfaz.
        db.rollback()
        # Mostra o motivo.
        return _back(request, day, str(exc), error=True)
    # A agenda ou o Drive recusaram.
    except HttpError as exc:
        # Desfaz.
        db.rollback()
        # Código e motivo.
        return _back(request, day, f"O Google recusou o pedido ({_google_error(exc)}).", error=True)
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
        request, day, f"Transcrição carregada com sucesso: “{result.title}”, {detail}.", link=link
    )
