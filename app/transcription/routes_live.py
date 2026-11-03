"""Rotas do Início ao vivo: canal de eventos, áreas das reuniões e participantes.

- GET /ao-vivo/eventos: canal SSE que o Início mantém aberto (ver app/transcription/live.py);
- GET /ao-vivo: as áreas das reuniões, redesenhadas quando uma reunião começa ou é salva;
- POST /ao-vivo/{sessao}/empresa: a Empresa da reunião (quando a agenda não disse, ou
  para corrigir). Sem Empresa a reunião aparece ao vivo, mas só é salva no banco depois
  que você escolhe: dado sem Empresa não entra (premissa 1 da separação por Empresa);
- /ao-vivo/{sessao}/participantes, /falantes e /convidados: quem participa e quem é
  cada voz. Respondem com o bloco de participantes redesenhado (HTMX troca só ele) e
  avisam o Início para trocar "Falante 2" pelo nome em todas as falas da reunião.

Depois que a reunião foi salva, mudar quem é cada voz refaz a transcrição salva (texto
e falas no banco), para os nomes ficarem certos também no que a IA vai ler.
"""

# asyncio: espera com tempo limite no canal de eventos.
import asyncio

# Annotated junta um tipo com informação extra (campos de formulário).
from typing import Annotated

# Peças do FastAPI.
from fastapi import APIRouter, Form, HTTPException, Request

# Respostas: HTML e fluxo contínuo (o canal de eventos).
from fastapi.responses import HTMLResponse, StreamingResponse

# Erro devolvido pelas APIs do Google.
from googleapiclient.errors import HttpError

# Consultas.
from sqlalchemy import func, select

# Empresa escolhida e a troca de Empresa da reunião.
from app.companies import parse_company, set_meeting_company

# Configuração.
from app.config import get_settings

# Liga a captura à reunião do evento da agenda.
from app.meetings import link_live_to_event

# Pessoas cadastradas.
from app.models import LiveMeeting, Stakeholder

# Reuniões ao vivo e transmissão; finalização.
from app.transcription import live, store

# Agenda: cliente e reuniões do dia.
from app.transcription.calendar import calendar_client, events_on

# Peças comuns: modelos HTML, dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Atalho para campos de formulário de texto (vazio quando não vem).
Text = Annotated[str, Form()]

# A cada quantos segundos sem aviso o canal manda um "estou vivo" (comentário SSE), para
# o Tailscale e o navegador não acharem que a conexão morreu.
_PING_SECONDS = 15


# Dados de uma área de reunião (título, situação, falas e nomes).
def meeting_view(db, meeting: LiveMeeting) -> dict:
    # Monta o dicionário que o modelo usa.
    return {
        # A reunião.
        "m": meeting,
        # "ao_vivo", "sem_audio" ou "salva".
        "status": live.status(meeting),
        # Falas já gravadas.
        "lines": live.recent_lines(db, meeting.session_id),
        # "canal|rótulo" → nome.
        "names": live.name_map(db, meeting),
    }


# Contexto das áreas do Início (usado aqui e no main.py). erros: id da captura → aviso
# mostrado na área daquela reunião (ex.: Empresa inválida).
def live_context(db, erros: dict[str, str] | None = None) -> dict:
    # Reuniões visíveis, cada uma com seus dados.
    return {
        # Áreas.
        "ao_vivo": [meeting_view(db, m) for m in live.visible_meetings(db)],
        # Avisos por reunião.
        "erros_vivo": erros or {},
    }


# Dados do bloco de participantes de uma reunião.
def participants_context(
    db, meeting: LiveMeeting, erro: str | None = None, aviso: str | None = None
) -> dict:
    # Falantes que já apareceram.
    speakers = live.speakers(db, meeting)
    # Monta.
    return {
        # A reunião.
        "m": meeting,
        # Participantes.
        "participantes": live.participants(db, meeting),
        # Falantes.
        "falantes": speakers,
        # "canal|rótulo" de cada falante, para o navegador saber quais já conhece.
        "chaves": [live.speaker_key(s.channel, s.label) for s in speakers],
        # Pessoas da Empresa da reunião, para escolher na lista (sem Empresa, nenhuma:
        # Pessoas de um cliente não aparecem na reunião de outro).
        "pessoas": db.scalars(
            select(Stakeholder)
            .where(Stakeholder.company_id == meeting.company_id)
            .order_by(func.lower(Stakeholder.name))
        ).all()
        if meeting.company_id is not None
        else [],
        # Mensagem de erro (nome repetido etc.).
        "erro": erro,
        # Mensagem de sucesso.
        "aviso": aviso,
    }


# A reunião pelo id da captura, ou 404.
def _meeting(db, sessao: str) -> LiveMeeting:
    # Busca.
    meeting = live.get_meeting(db, sessao)
    # Não existe.
    if meeting is None:
        raise HTTPException(status_code=404, detail="Reunião não encontrada.")
    # Devolve.
    return meeting


# Depois de mudar participantes ou falantes: grava, refaz a transcrição salva e avisa.
def _changed(db, meeting: LiveMeeting, owner: str) -> None:
    # Reunião já salva: refaz texto e falas com os nomes novos (pendente de Empresa
    # ainda não tem o que refazer; salva quando a Empresa for escolhida).
    if meeting.finished_at is not None and meeting.company_id is not None:
        # Mesmo título e endereço (a função usa os guardados na reunião).
        store.finalize(db, meeting.session_id, actor=owner, title=None, meeting_url=None)
    # Grava.
    db.commit()
    # Avisa o Início (todas as abas abertas) dos nomes novos desta reunião.
    live.hub.publish(
        {"tipo": "falantes", "sessao": meeting.session_id, "nomes": live.name_map(db, meeting)}
    )


# Bloco de participantes redesenhado.
def _block(
    request: Request, db, meeting: LiveMeeting, erro: str | None = None, aviso: str | None = None
):
    # Só o pedaço do modelo.
    return templates.TemplateResponse(
        request, "_participantes.html", participants_context(db, meeting, erro, aviso)
    )


# Canal de eventos: fica aberto enquanto o Início estiver aberto.
@router.get("/ao-vivo/eventos")
async def events(request: Request, owner: Owner) -> StreamingResponse:
    # Entra na lista de ouvintes.
    queue = live.hub.subscribe()

    # Gerador: cada "yield" manda um pedaço da resposta sem fechá-la.
    async def stream():
        # Garante a saída da lista, aconteça o que acontecer.
        try:
            # O navegador espera 3 s para reconectar se a conexão cair.
            yield "retry: 3000\n\n"
            # Laço até a aba fechar.
            while not await request.is_disconnected():
                # Espera um aviso, no máximo alguns segundos.
                try:
                    # Próximo aviso.
                    event = await asyncio.wait_for(queue.get(), timeout=_PING_SECONDS)
                # Nada chegou: manda o "estou vivo" e volta a esperar.
                except TimeoutError:
                    # Linha que começa com ":" é comentário no SSE (o navegador ignora).
                    yield ": ping\n\n"
                    # Volta ao laço.
                    continue
                # Manda o aviso.
                yield live.format_event(event)
        # Sempre.
        finally:
            # Sai da lista.
            live.hub.unsubscribe(queue)

    # Resposta contínua do tipo "text/event-stream" (o formato do SSE).
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        # Sem cache e sem acumular no caminho (cada aviso sai na hora).
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Áreas das reuniões (o Início pede de novo quando uma reunião começa ou é salva).
@router.get("/ao-vivo", response_class=HTMLResponse)
def live_section(request: Request, owner: Owner, db: DB):
    # Só o pedaço do modelo.
    return templates.TemplateResponse(request, "_ao_vivo.html", live_context(db))


# Escolhe ou corrige a Empresa de uma reunião (combo na área dela). Devolve a seção
# inteira, porque a situação muda (pendente vira salva) e as outras abas recarregam.
@router.post("/ao-vivo/{sessao}/empresa", response_class=HTMLResponse, dependencies=[SameOrigin])
def choose_company(request: Request, owner: Owner, db: DB, sessao: str, empresa: Text = ""):
    # A reunião.
    meeting = _meeting(db, sessao)
    # Avisos por reunião.
    erros: dict[str, str] = {}
    # Tenta.
    try:
        # Empresa escolhida.
        company = parse_company(db, empresa)
        # Troca (e salva, se a reunião já acabou).
        set_meeting_company(db, meeting, company, owner)
        # Procura o evento na agenda da Empresa escolhida, para ligar a reunião a ele.
        _link_event(db, meeting)
        # Grava.
        db.commit()
    # Sem Empresa, inexistente ou já existe uma transcrição igual na outra Empresa.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso na área da reunião.
        erros[sessao] = str(exc)
    # Outras abas abertas redesenham as áreas.
    live.hub.publish({"tipo": "reunioes"})
    # Seção redesenhada.
    return templates.TemplateResponse(request, "_ao_vivo.html", live_context(db, erros))


# Liga a captura ao evento da agenda da Empresa dela com o mesmo endereço do Meet.
def _link_event(db, meeting: LiveMeeting) -> None:
    """Melhor esforço: sem endereço do Meet, sem conta Google, agenda fora do ar ou
    nenhum evento com o endereço, a reunião fica fora da agenda (criada ao salvar)."""
    # Sem endereço ou sem Empresa, não há o que procurar.
    if live.meet_code(meeting.meeting_url) is None or meeting.company_id is None:
        return
    # Configuração.
    settings = get_settings()
    # Tenta ler a agenda do dia em que a reunião começou.
    try:
        # Dia no fuso do console.
        day = meeting.started_at.astimezone(_zone(settings)).date()
        # Reuniões do dia.
        events = events_on(
            calendar_client(db, settings, meeting.company_id), day, settings.console_timezone
        )
    # Sem conta (RuntimeError) ou a agenda recusou (HttpError): fica sem o evento.
    except (RuntimeError, HttpError):
        return
    # O evento com o mesmo endereço.
    event = live.matching_event(meeting, events)
    # Achou: liga.
    if event is not None:
        link_live_to_event(db, meeting, event)


# Bloco de participantes de uma reunião (o Início pede quando aparece um falante novo).
@router.get("/ao-vivo/{sessao}/participantes", response_class=HTMLResponse)
def participants_block(request: Request, owner: Owner, db: DB, sessao: str):
    # Redesenha.
    return _block(request, db, _meeting(db, sessao))


# Inclui um participante (da página Pessoas, ou nome e e-mail).
@router.post(
    "/ao-vivo/{sessao}/participantes", response_class=HTMLResponse, dependencies=[SameOrigin]
)
def add_participant(
    request: Request,
    owner: Owner,
    db: DB,
    sessao: str,
    # Id de uma pessoa cadastrada (vazio = escrever nome e e-mail).
    pessoa: Text = "",
    # Nome.
    nome: Text = "",
    # E-mail.
    email: Text = "",
):
    # A reunião.
    meeting = _meeting(db, sessao)
    # Tenta.
    try:
        # Inclui.
        participant = live.add_participant(
            db, meeting, person_id=int(pessoa) if pessoa.isdigit() else None, name=nome, email=email
        )
        # Grava e avisa.
        _changed(db, meeting, owner)
    # Nome vazio ou repetido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Bloco com o erro.
        return _block(request, db, meeting, erro=str(exc))
    # Bloco com o aviso.
    return _block(request, db, meeting, aviso=f"“{participant.name}” incluído.")


# Tira um participante da reunião.
@router.post(
    "/ao-vivo/{sessao}/participantes/{participant_id}/remover",
    response_class=HTMLResponse,
    dependencies=[SameOrigin],
)
def remove_participant(request: Request, owner: Owner, db: DB, sessao: str, participant_id: int):
    # A reunião.
    meeting = _meeting(db, sessao)
    # Tira.
    live.remove_participant(db, meeting, participant_id)
    # Grava e avisa.
    _changed(db, meeting, owner)
    # Bloco redesenhado.
    return _block(request, db, meeting, aviso="Participante retirado.")


# Diz qual participante é um falante.
@router.post("/ao-vivo/{sessao}/falantes", response_class=HTMLResponse, dependencies=[SameOrigin])
def assign_speaker(
    request: Request,
    owner: Owner,
    db: DB,
    sessao: str,
    # Canal do falante ("voce" ou "outros").
    canal: Text,
    # Rótulo ("Falante 2").
    rotulo: Text,
    # Id do participante (vazio = desfazer).
    participante: Text = "",
):
    # A reunião.
    meeting = _meeting(db, sessao)
    # Canal desconhecido.
    if canal not in store.AUDIO_CHANNELS:
        raise HTTPException(status_code=400, detail="Canal inválido.")
    # Tenta.
    try:
        # Liga (ou desfaz).
        live.assign_speaker(
            db, meeting, canal, rotulo, int(participante) if participante.isdigit() else None
        )
        # Grava e avisa.
        _changed(db, meeting, owner)
    # Participante de outra reunião.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Bloco com o erro.
        return _block(request, db, meeting, erro=str(exc))
    # Bloco redesenhado.
    return _block(request, db, meeting)


# Traz os convidados do evento da agenda com o mesmo endereço do Meet.
@router.post("/ao-vivo/{sessao}/convidados", response_class=HTMLResponse, dependencies=[SameOrigin])
def add_invitees(request: Request, owner: Owner, db: DB, sessao: str):
    # A reunião.
    meeting = _meeting(db, sessao)
    # Sem endereço do Meet, não há como achar o evento.
    if live.meet_code(meeting.meeting_url) is None:
        return _block(
            request, db, meeting, erro="A extensão não informou o endereço do Meet desta reunião."
        )
    # Sem Empresa, não se sabe de qual agenda ler.
    if meeting.company_id is None:
        return _block(
            request,
            db,
            meeting,
            erro="Escolha antes a Empresa da reunião (a agenda é da conta dela).",
        )
    # Configuração.
    settings = get_settings()
    # Tenta ler a agenda do dia em que a reunião começou.
    try:
        # Dia no fuso do console.
        day = meeting.started_at.astimezone(_zone(settings)).date()
        # Reuniões do dia, na agenda da conta da Empresa da reunião.
        events = events_on(
            calendar_client(db, settings, meeting.company_id), day, settings.console_timezone
        )
    # Sem conta Google.
    except RuntimeError as exc:
        return _block(request, db, meeting, erro=str(exc))
    # A agenda recusou.
    except HttpError as exc:
        return _block(
            request, db, meeting, erro=f"A agenda recusou o pedido (HTTP {exc.resp.status})."
        )
    # Evento com o mesmo endereço.
    event = live.matching_event(meeting, events)
    # Não achou.
    if event is None:
        return _block(
            request,
            db,
            meeting,
            erro="Nenhuma reunião da agenda de hoje tem este endereço do Meet.",
        )
    # Inclui os convidados.
    added = live.add_invitees(db, meeting, event)
    # A captura passa a ser da reunião do evento (data, horário e convidados da agenda).
    link_live_to_event(db, meeting, event)
    # Grava e avisa.
    _changed(db, meeting, owner)
    # Bloco com o resultado.
    return _block(
        request, db, meeting, aviso=f"{added} convidado(s) de “{event.title}” incluído(s)."
    )


# Fuso do console.
def _zone(settings):
    # Import local: só aqui precisa.
    from zoneinfo import ZoneInfo

    # Fuso configurado.
    return ZoneInfo(settings.console_timezone)
