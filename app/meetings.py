"""Reuniões: a reunião em si, acima das transcrições dela.

Por que uma camada própria: a transcrição é o texto de uma reunião, mas a reunião é o que
a ata e os relatórios contam (quando foi, quem estava, de que projeto). Antes, cada
transcrição guardava a sua cópia da reunião, e duas transcrições da mesma reunião (uma
importada do Meet, outra feita pela extensão) não se encontravam.

Regras:
- uma reunião da agenda é uma linha por Empresa, achada pelo id do evento;
- uma reunião fora da agenda vira uma linha nova a cada transcrição (não há como saber
  que duas são a mesma);
- os participantes são linhas ligadas à Pessoa pelo e-mail, dentro da Empresa;
- a Empresa de uma reunião não muda (trava do banco). Corrigir a Empresa de uma
  transcrição liga a transcrição a uma reunião da Empresa nova (move_item_meeting).
"""

# datetime: tipo das datas.
from datetime import datetime

# Consultas e alterações.
from sqlalchemy import select, update

# Sessão do banco.
from sqlalchemy.orm import Session

# Tabelas.
from app.models import BronzeItem, LiveMeeting, Meeting, MeetingAttendee, Stakeholder

# Forma comparável dos nomes.
from app.people import normalize

# Plataforma padrão de uma captura da extensão (ela só roda no Meet).
LIVE_PLATFORM = "meet"


# --- Participantes -------------------------------------------------------------


# Participantes de uma reunião, em ordem de nome.
def attendees(session: Session, meeting: Meeting) -> list[MeetingAttendee]:
    # Consulta.
    return session.scalars(
        select(MeetingAttendee)
        .where(MeetingAttendee.meeting_id == meeting.id)
        .order_by(MeetingAttendee.name_key)
    ).all()


# Inclui um participante sem repetir. Devolve o participante (novo ou o que já estava).
def add_attendee(
    session: Session,
    meeting: Meeting,
    name: str | None,
    email: str | None,
    stakeholder_id: int | None = None,
) -> MeetingAttendee | None:
    """Repetido = mesmo e-mail, ou mesmo nome quando um dos dois não tem e-mail (o
    convite da agenda traz "Ana Souza" com e-mail; você pode ter escrito "Ana" sem).
    Sem nome nem e-mail: não inclui (devolve None)."""
    # Nome em espaços simples (sem nome, o e-mail), no tamanho da coluna.
    clean_name = " ".join((name or email or "").split())[:200]
    # E-mail em minúsculas.
    clean_email = (email or "").strip().lower()[:320] or None
    # Sem nada.
    if not clean_name:
        return None
    # Forma comparável.
    key = normalize(clean_name)
    # Procura um igual.
    found = None
    # Cada participante atual.
    for current in attendees(session, meeting):
        # Mesmo e-mail.
        if clean_email and current.email == clean_email:
            found = current
            break
        # Mesmo nome, e um dos dois sem e-mail.
        if current.name_key == key and (current.email is None or clean_email is None):
            found = current
            break
    # Pessoa: a informada, ou a da Empresa da reunião com esse e-mail.
    person = stakeholder_id or _person_by_email(session, meeting.company_id, clean_email)
    # Já estava: completa o que faltava.
    if found is not None:
        # E-mail que faltava.
        found.email = found.email or clean_email
        # Pessoa que faltava.
        found.stakeholder_id = found.stakeholder_id or person
        # Envia ao banco.
        session.flush()
        # Devolve.
        return found
    # Novo.
    attendee = MeetingAttendee(
        meeting_id=meeting.id,
        name=clean_name,
        name_key=key,
        email=clean_email,
        stakeholder_id=person,
    )
    # Prepara.
    session.add(attendee)
    # Gera o id.
    session.flush()
    # Devolve.
    return attendee


# Pessoa de uma Empresa com um e-mail, ou None.
def _person_by_email(session: Session, company_id: int, email: str | None) -> int | None:
    # Sem e-mail, não há chave.
    if not email:
        return None
    # Só na Empresa da reunião.
    return session.scalar(
        select(Stakeholder.id).where(
            Stakeholder.company_id == company_id, Stakeholder.email == email
        )
    )


# Liga à Pessoa os participantes sem ligação que têm o e-mail dela. Devolve quantos.
def link_attendees_by_email(session: Session, company_id: int | None = None) -> int:
    """Roda depois de cadastrar uma Pessoa (ou mudar o e-mail dela): os convidados de
    reuniões antigas com esse e-mail passam a ser ela. Só dentro da Empresa."""
    # Participantes sem Pessoa, com e-mail, e a Pessoa com esse e-mail na mesma Empresa.
    query = (
        select(MeetingAttendee, Stakeholder.id)
        .join(Meeting, Meeting.id == MeetingAttendee.meeting_id)
        .join(
            Stakeholder,
            (Stakeholder.company_id == Meeting.company_id)
            & (Stakeholder.email == MeetingAttendee.email),
        )
        .where(MeetingAttendee.stakeholder_id.is_(None))
    )
    # Só uma Empresa, se pedido.
    if company_id is not None:
        query = query.where(Meeting.company_id == company_id)
    # Contador.
    linked = 0
    # Cada par.
    for attendee, person in session.execute(query).all():
        # Liga.
        attendee.stakeholder_id = person
        # Conta.
        linked += 1
    # Envia ao banco.
    session.flush()
    # Devolve.
    return linked


# --- Reunião -------------------------------------------------------------------


# Reunião da agenda numa Empresa: acha pelo id do evento ou cria. Atualiza os dados.
def for_event(session: Session, company_id: int, event, platform: str | None = None) -> Meeting:
    """event: um CalendarEvent (app/transcription/calendar.py). Os dados da agenda valem
    sobre os guardados (a reunião pode ter mudado de horário), e os convidados novos
    entram; ninguém sai (quem você incluiu à mão continua)."""
    # Procura.
    meeting = session.scalar(
        select(Meeting).where(
            Meeting.company_id == company_id, Meeting.calendar_event_id == event.id
        )
    )
    # Não existe: cria.
    if meeting is None:
        meeting = Meeting(company_id=company_id, calendar_event_id=event.id)
        # Prepara.
        session.add(meeting)
    # Título.
    meeting.title = event.title or "Reunião sem título"
    # Início e fim.
    meeting.starts_at, meeting.ends_at = event.start, event.end
    # A agenda sabe o horário.
    meeting.time_known = True
    # Organizador.
    meeting.organizer = (event.organizer or "")[:320] or None
    # Endereço (o da agenda, ou o que já estava).
    meeting.meeting_url = event.meeting_url or meeting.meeting_url
    # Plataforma (a da agenda, a informada, ou a que já estava).
    meeting.platform = event.platform or platform or meeting.platform
    # Gera o id.
    session.flush()
    # Convidados.
    for name in event.attendees:
        # Com o e-mail, quando a agenda sabe.
        add_attendee(session, meeting, name, event.attendee_emails.get(name))
    # Devolve.
    return meeting


# Reunião nova fora da agenda.
def standalone(
    session: Session,
    company_id: int,
    *,
    title: str | None,
    starts_at: datetime,
    ends_at: datetime | None = None,
    time_known: bool = True,
    organizer: str | None = None,
    meeting_url: str | None = None,
    platform: str | None = None,
) -> Meeting:
    # Linha nova.
    meeting = Meeting(
        company_id=company_id,
        title=(title or "").strip() or "Reunião sem título",
        starts_at=starts_at,
        ends_at=ends_at,
        time_known=time_known,
        organizer=(organizer or "")[:320] or None,
        meeting_url=(meeting_url or "")[:500] or None,
        platform=platform,
    )
    # Prepara.
    session.add(meeting)
    # Gera o id.
    session.flush()
    # Devolve.
    return meeting


# Liga uma captura ao vivo ao evento da agenda achado pelo endereço do Meet.
def link_live_to_event(session: Session, live: LiveMeeting, event) -> Meeting | None:
    """Só com Empresa: a reunião é da Empresa da captura. Já salva no bronze, a
    transcrição passa a apontar para a reunião do evento também."""
    # Sem Empresa, não se sabe em qual Empresa criar.
    if live.company_id is None:
        return None
    # A reunião do evento nesta Empresa.
    meeting = for_event(session, live.company_id, event, LIVE_PLATFORM)
    # Reunião anterior desta captura (criada sem o evento), para apagar se sobrar.
    previous = live.meeting_id
    # Aponta.
    live.meeting_id = meeting.id
    # Transcrição já salva: aponta também, com o thread_id do evento.
    if live.bronze_item_id is not None:
        # A transcrição.
        item = session.get(BronzeItem, live.bronze_item_id)
        # Reunião.
        item.meeting_id = meeting.id
        # Mesma conversa que a transcrição importada deste evento.
        item.thread_id = item.thread_id or f"agenda:{event.id}"
    # Participantes da captura entram na reunião.
    _merge_live_participants(session, live, meeting)
    # Envia ao banco.
    session.flush()
    # A reunião anterior ficou sem nada: apaga.
    if previous is not None and previous != meeting.id:
        delete_if_orphan(session, previous)
    # Devolve.
    return meeting


# Garante a reunião de uma captura ao vivo que vai ser salva (Empresa já conhecida).
def attach_live(session: Session, live: LiveMeeting) -> Meeting:
    """Sem reunião (a agenda não tinha o endereço do Meet): cria uma, com o título da aba,
    a hora em que a captura começou e a em que acabou. Os participantes que você
    cadastrou durante a reunião entram como participantes dela."""
    # Reunião atual.
    meeting = session.get(Meeting, live.meeting_id) if live.meeting_id else None
    # De outra Empresa (a Empresa da captura mudou): não serve.
    if meeting is not None and meeting.company_id != live.company_id:
        # Desliga.
        live.meeting_id = None
        # Envia ao banco.
        session.flush()
        # Apaga se sobrou vazia.
        delete_if_orphan(session, meeting.id)
        # Procura de novo abaixo.
        meeting = None
    # Sem reunião: cria.
    if meeting is None:
        meeting = standalone(
            session,
            live.company_id,
            title=live.title,
            starts_at=live.started_at,
            ends_at=live.finished_at,
            meeting_url=live.meeting_url,
            platform=LIVE_PLATFORM,
        )
        # Aponta.
        live.meeting_id = meeting.id
    # Fora da agenda: o fim é a hora em que a captura parou.
    elif meeting.calendar_event_id is None:
        meeting.ends_at = live.finished_at or meeting.ends_at
    # Participantes.
    _merge_live_participants(session, live, meeting)
    # Devolve.
    return meeting


# Desliga uma captura da reunião quando ela muda de Empresa antes de ser salva.
def detach_live(session: Session, live: LiveMeeting) -> None:
    # Sem reunião: nada a fazer.
    if live.meeting_id is None:
        return
    # Guarda o id.
    previous = live.meeting_id
    # Desliga.
    live.meeting_id = None
    # Envia ao banco.
    session.flush()
    # Apaga se sobrou vazia.
    delete_if_orphan(session, previous)


# Os participantes cadastrados numa captura entram na reunião.
def _merge_live_participants(session: Session, live: LiveMeeting, meeting: Meeting) -> None:
    # Import local: o módulo da reunião ao vivo importa coisas que importam este.
    from app.transcription.live import participants

    # Cada participante.
    for p in participants(session, live):
        # Mesma Pessoa só se for da Empresa da reunião (a trava do banco confere).
        person = p.stakeholder_id if live.company_id == meeting.company_id else None
        # Inclui (ou completa).
        add_attendee(session, meeting, p.name, p.email, person)


# Apaga uma reunião que ficou sem transcrição e sem captura ao vivo.
def delete_if_orphan(session: Session, meeting_id: int) -> bool:
    """Uma reunião só existe por causa de uma transcrição ou captura. Sem nenhuma, era
    um resto (de uma correção de Empresa, por exemplo)."""
    # Alguma transcrição aponta para ela?
    used = session.scalar(select(BronzeItem.id).where(BronzeItem.meeting_id == meeting_id))
    # Alguma captura?
    used = used or session.scalar(
        select(LiveMeeting.id).where(LiveMeeting.meeting_id == meeting_id)
    )
    # Usada: fica.
    if used:
        return False
    # A reunião.
    meeting = session.get(Meeting, meeting_id)
    # Apaga (os participantes vão junto, pelo CASCADE).
    if meeting is not None:
        session.delete(meeting)
        # Envia ao banco.
        session.flush()
    # Apagou.
    return True


# Liga uma transcrição a uma reunião equivalente na Empresa nova (correção de Empresa).
def move_item_meeting(session: Session, item: BronzeItem, company_id: int) -> Meeting | None:
    """A reunião antiga é da Empresa antiga e não muda de Empresa. Na nova, usa a reunião
    do mesmo evento (se já houver) ou cria uma cópia, com os participantes ligados às
    Pessoas da Empresa nova. A antiga é apagada se ficar vazia."""
    # Reunião atual.
    old = session.get(Meeting, item.meeting_id) if item.meeting_id else None
    # Sem reunião: nada a copiar.
    if old is None:
        return None
    # Já é da Empresa nova.
    if old.company_id == company_id:
        return old
    # Mesmo evento já existe na Empresa nova.
    new = None
    # Só reunião da agenda tem como ser "a mesma".
    if old.calendar_event_id is not None:
        new = session.scalar(
            select(Meeting).where(
                Meeting.company_id == company_id,
                Meeting.calendar_event_id == old.calendar_event_id,
            )
        )
    # Senão: cópia.
    if new is None:
        new = Meeting(
            company_id=company_id,
            calendar_event_id=old.calendar_event_id,
            title=old.title,
            starts_at=old.starts_at,
            ends_at=old.ends_at,
            time_known=old.time_known,
            organizer=old.organizer,
            meeting_url=old.meeting_url,
            platform=old.platform,
        )
        # Prepara.
        session.add(new)
        # Gera o id.
        session.flush()
    # Participantes: nome e e-mail; a Pessoa é procurada de novo na Empresa nova.
    for a in attendees(session, old):
        add_attendee(session, new, a.name, a.email)
    # A transcrição aponta para a nova.
    item.meeting_id = new.id
    # A captura ao vivo dela também.
    session.execute(
        update(LiveMeeting).where(LiveMeeting.bronze_item_id == item.id).values(meeting_id=new.id)
    )
    # Envia ao banco.
    session.flush()
    # A antiga, se sobrou vazia.
    delete_if_orphan(session, old.id)
    # Devolve.
    return new


# --- Leitura -------------------------------------------------------------------


# Transcrições de uma reunião, a mais antiga primeiro.
def transcripts(session: Session, meeting: Meeting) -> list[BronzeItem]:
    # Consulta.
    return session.scalars(
        select(BronzeItem).where(BronzeItem.meeting_id == meeting.id).order_by(BronzeItem.id)
    ).all()


# Pessoas e participantes que contam para as regras: e-mails dos participantes.
def attendee_emails(session: Session, meeting_id: int | None) -> set[str]:
    # Sem reunião.
    if meeting_id is None:
        return set()
    # E-mails, em minúsculas.
    return set(
        session.scalars(
            select(MeetingAttendee.email).where(
                MeetingAttendee.meeting_id == meeting_id, MeetingAttendee.email.is_not(None)
            )
        )
    )


# Nomes dos participantes (para o Claude ver na classificação).
def attendee_names(session: Session, meeting_id: int | None) -> list[str]:
    # Sem reunião.
    if meeting_id is None:
        return []
    # Em ordem de nome.
    return list(
        session.scalars(
            select(MeetingAttendee.name)
            .where(MeetingAttendee.meeting_id == meeting_id)
            .order_by(MeetingAttendee.name_key)
        )
    )
