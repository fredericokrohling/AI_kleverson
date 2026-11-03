"""Reuniões ao vivo: transmissão dos trechos para o console e quem é cada voz.

Por que existe: o Início é o painel de controle da dona, e a transcrição precisa aparecer
lá enquanto a reunião acontece, inclusive de duas reuniões ao mesmo tempo (duas abas ou
janelas do Meet, cada uma com sua captura na extensão).

Como as reuniões não se misturam: cada clique no ícone da extensão gera um id de captura
(session_id). Áudio, trechos, participantes e falantes ficam todos presos a esse id. O
console abre UM canal de eventos e recebe os trechos de todas as reuniões, cada um com
o id; o navegador põe cada trecho na área da reunião certa.

Por que "Server-Sent Events" (SSE) e não WebSocket para o console: o console só recebe;
quem manda é a extensão. O SSE é uma resposta HTTP comum que não termina, o navegador
reconecta sozinho se cair, e passa pelo Tailscale sem configuração nova.

Por que a distribuição fica na memória do processo (LiveHub) e não no banco: o servidor
roda um processo só (o uvicorn do Dockerfile, sem --workers). Com mais de um processo,
cada um teria a sua lista e o console perderia trechos; aí seria preciso o LISTEN/NOTIFY
do Postgres. Os trechos definitivos são gravados no banco do mesmo jeito; a memória só
leva o aviso até a tela.

Empresa da reunião: cada reunião é de uma Empresa (cliente), porque a transcrição vai
para o bronze dessa Empresa e as Pessoas que podem ser participantes são as dela. Ao
começar, o servidor procura o endereço do Meet na agenda de cada Empresa (ver
detect_company); se não achar, você escolhe no Início. Sem Empresa, a transcrição
acontece e aparece ao vivo, mas só é salva no banco depois de você escolher.

Quem é cada voz: o serviço de transcrição separa vozes ("Falante 1", "Falante 2"), mas
não sabe nomes. Você cadastra os participantes da reunião (da página Pessoas, ou só o
nome e o e-mail) e diz qual participante é cada falante. A troca vale para os trechos já
recebidos e para os próximos, e vai para o banco ao salvar a reunião.
"""

# asyncio: filas assíncronas e o laço de eventos de cada conexão do console.
import asyncio

# json: transforma o aviso em texto para o canal de eventos.
import json

# re: expressões regulares, para achar o código da reunião no endereço do Meet.
import re

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Datas, horas e intervalos.
from datetime import UTC, datetime, timedelta

# Consultas.
from sqlalchemy import delete, func, select

# Inserção do Postgres com "se já existir, não faça nada".
from sqlalchemy.dialects.postgresql import insert

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Tabelas.
from app.models import (
    LiveMeeting,
    MeetingParticipant,
    MeetingSpeaker,
    Stakeholder,
    TranscriptSegment,
)

# Forma comparável de um nome (sem acento, minúsculo).
from app.people import normalize

# Reunião sem salvar some do Início depois deste tempo sem nenhum trecho novo (aba
# fechada sem "Parar e salvar"; os trechos continuam no banco).
STALE_AFTER = timedelta(hours=3)
# Reunião salva continua no Início por este tempo, para você ainda acertar os nomes.
KEEP_FINISHED = timedelta(minutes=30)
# Reunião encerrada sem Empresa continua no Início por este tempo, esperando a escolha.
KEEP_PENDING = timedelta(days=7)
# Sem trecho novo há mais que isto, a reunião aparece como "sem áudio".
LIVE_GAP = timedelta(minutes=2)
# Quantos trechos já gravados a área de cada reunião mostra ao abrir a página.
RECENT_LINES = 300
# Código de uma reunião do Meet no endereço (abc-defg-hij).
_MEET_CODE = re.compile(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", re.I)


# --- Distribuição dos avisos para o console ---------------------------------------


# Lista de quem está ouvindo (cada aba do console aberta é um ouvinte).
class LiveHub:
    """Entrega cada aviso a todas as abas do console abertas.

    Cada ouvinte tem uma fila. Os avisos podem nascer no laço assíncrono (WebSocket da
    extensão) ou numa thread (rotas comuns do FastAPI, como cadastrar um participante);
    call_soon_threadsafe põe o aviso na fila pelo laço dono dela, o que é seguro nos
    dois casos.
    """

    # Começa sem ouvintes.
    def __init__(self) -> None:
        # Fila de cada ouvinte → laço de eventos em que ela foi criada.
        self._listeners: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}

    # Novo ouvinte (chamado dentro do laço assíncrono da conexão do console).
    def subscribe(self) -> asyncio.Queue:
        # Fila com limite: uma aba travada não faz a memória crescer sem fim.
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        # Guarda a fila com o laço atual.
        self._listeners[queue] = asyncio.get_running_loop()
        # Devolve a fila para quem vai ler.
        return queue

    # O ouvinte foi embora (aba fechada).
    def unsubscribe(self, queue: asyncio.Queue) -> None:
        # Tira da lista; pop com padrão não dá erro se já saiu.
        self._listeners.pop(queue, None)

    # Quantos ouvintes (para os testes).
    def __len__(self) -> int:
        # Tamanho da lista.
        return len(self._listeners)

    # Manda um aviso a todos.
    def publish(self, event: dict) -> None:
        # Cópia da lista: um ouvinte pode sair enquanto percorremos.
        for queue, loop in list(self._listeners.items()):
            # Tenta entregar.
            try:
                # Agenda a entrega no laço dono da fila.
                loop.call_soon_threadsafe(_offer, queue, event)
            # Laço já fechado (aba que saiu de repente): esquece o ouvinte.
            except RuntimeError:
                # Tira.
                self.unsubscribe(queue)


# Põe o aviso na fila, ou descarta se ela estiver cheia (aba que não lê).
def _offer(queue: asyncio.Queue, event: dict) -> None:
    # Tenta pôr.
    try:
        # Sem esperar.
        queue.put_nowait(event)
    # Fila cheia: perde este aviso (a página recarrega os trechos do banco depois).
    except asyncio.QueueFull:
        # Nada a fazer.
        pass


# O distribuidor único do processo.
hub = LiveHub()


# Texto de um aviso no formato do SSE: "event: tipo", "data: json" e uma linha vazia.
def format_event(event: dict) -> str:
    # ensure_ascii=False mantém os acentos legíveis.
    return f"event: {event['tipo']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


# Chave de um falante no navegador e nos avisos: "canal|rótulo".
def speaker_key(channel: str, label: str | None) -> str:
    # Rótulo vazio vira texto vazio.
    return f"{channel}|{label or ''}"


# --- Reuniões ---------------------------------------------------------------------


# Agora, com fuso (UTC), para comparar com as colunas do banco.
def utc_now() -> datetime:
    # Data e hora atuais em UTC.
    return datetime.now(UTC)


# Cria a reunião se ainda não existir. Devolve (reunião, se foi criada agora).
def ensure_meeting(
    session: Session, session_id: str, title: str | None = None, meeting_url: str | None = None
) -> tuple[LiveMeeting, bool]:
    # INSERT ... ON CONFLICT DO NOTHING: duas conexões ao mesmo tempo (microfone e aba)
    # não criam duas reuniões, nem dão erro.
    created = session.execute(
        # Linha nova com o id da captura.
        insert(LiveMeeting)
        .values(session_id=session_id)
        # Se o id já existe, não faz nada.
        .on_conflict_do_nothing(index_elements=["session_id"])
        # Devolve o id só quando inseriu.
        .returning(LiveMeeting.id)
    ).scalar()
    # A reunião (nova ou antiga).
    meeting = session.scalars(select(LiveMeeting).where(LiveMeeting.session_id == session_id)).one()
    # Título informado (a extensão manda o título da aba).
    if title:
        # Sem o " - Google Meet" do fim do título da aba.
        meeting.title = re.sub(r"\s*-\s*Google Meet$", "", title)[:500] or meeting.title
    # Endereço informado.
    if meeting_url:
        # Corta ao tamanho da coluna.
        meeting.meeting_url = meeting_url[:500]
    # Devolve.
    return meeting, created is not None


# Marca que chegou um trecho novo agora.
def touch(session: Session, session_id: str) -> None:
    # A reunião.
    meeting = session.scalars(
        select(LiveMeeting).where(LiveMeeting.session_id == session_id)
    ).first()
    # Existe (sempre, depois do ensure_meeting da conexão).
    if meeting is not None:
        # Hora do trecho.
        meeting.last_activity_at = utc_now()


# Uma reunião pelo id da captura, ou None.
def get_meeting(session: Session, session_id: str) -> LiveMeeting | None:
    # Busca.
    return session.scalars(select(LiveMeeting).where(LiveMeeting.session_id == session_id)).first()


# Reuniões que aparecem no Início, da mais antiga para a mais nova.
def visible_meetings(session: Session, now: datetime | None = None) -> list[LiveMeeting]:
    """Ao vivo (com trecho nas últimas 3 h), salvas há menos de 30 min, ou encerradas sem
    Empresa (esperando você escolher para salvar), por até 7 dias."""
    # Agora.
    now = now or utc_now()
    # Consulta.
    return session.scalars(
        select(LiveMeeting)
        .where(
            # Ainda sem salvar e com atividade recente...
            (
                (LiveMeeting.finished_at.is_(None))
                & (LiveMeeting.last_activity_at > now - STALE_AFTER)
            )
            # ... ou salva há pouco.
            | (LiveMeeting.finished_at > now - KEEP_FINISHED)
            | (
                (LiveMeeting.finished_at > now - KEEP_PENDING)
                & LiveMeeting.bronze_item_id.is_(None)
            )
        )
        # Ordem de início: a primeira reunião fica na área da esquerda.
        .order_by(LiveMeeting.started_at, LiveMeeting.id)
    ).all()


# Situação de uma reunião para mostrar: "ao_vivo", "sem_audio" ou "salva".
def status(meeting: LiveMeeting, now: datetime | None = None) -> str:
    """ "ao_vivo", "sem_audio", "salva" ou "pendente" (encerrada, falta a Empresa)."""
    # Encerrada sem item no bronze: falta escolher a Empresa.
    if meeting.finished_at is not None and meeting.bronze_item_id is None:
        return "pendente"
    # Já salva.
    if meeting.finished_at is not None:
        return "salva"
    # Agora.
    now = now or utc_now()
    # Trecho recente: no ar.
    if meeting.last_activity_at and now - meeting.last_activity_at <= LIVE_GAP:
        return "ao_vivo"
    # Parada há algum tempo (microfone e aba em silêncio, ou captura caiu).
    return "sem_audio"


# Trechos definitivos já gravados de uma reunião, em ordem de chegada.
def recent_lines(
    session: Session, session_id: str, limit: int = RECENT_LINES
) -> list[TranscriptSegment]:
    # Os mais recentes primeiro, para cortar no limite...
    rows = session.scalars(
        select(TranscriptSegment)
        .where(
            TranscriptSegment.session_id == session_id,
            TranscriptSegment.channel.in_(("voce", "outros")),
        )
        .order_by(TranscriptSegment.id.desc())
        .limit(limit)
    ).all()
    # ... e depois na ordem certa (mais antigo em cima).
    return list(reversed(rows))


# --- Participantes e falantes -----------------------------------------------------


# Participantes de uma reunião, por nome.
def participants(session: Session, meeting: LiveMeeting) -> list[MeetingParticipant]:
    # Consulta.
    return session.scalars(
        select(MeetingParticipant)
        .where(MeetingParticipant.meeting_id == meeting.id)
        .order_by(MeetingParticipant.name_key)
    ).all()


# Inclui um participante: da página Pessoas (person_id) ou só nome e e-mail.
def add_participant(
    session: Session,
    meeting: LiveMeeting,
    *,
    person_id: int | None = None,
    name: str = "",
    email: str = "",
) -> MeetingParticipant:
    """Erros (nome vazio, nome ou e-mail repetido na reunião) viram ValueError.

    A Pessoa escolhida precisa ser da Empresa da reunião; sem Empresa, só entram
    participantes escritos à mão (a ligação pelo e-mail acontece quando a Empresa for
    escolhida)."""
    # Pessoa escolhida na lista.
    person = session.get(Stakeholder, person_id) if person_id else None
    # Id que não existe, ou Pessoa de outra Empresa (formulário antigo ou adulterado).
    if person_id and (person is None or person.company_id != meeting.company_id):
        raise ValueError("Pessoa não encontrada nesta Empresa.")
    # Nome: o da pessoa, ou o escrito (espaços simples, até 200 caracteres).
    clean_name = person.name if person else " ".join(name.split())[:200]
    # E-mail: o da pessoa, ou o escrito, em minúsculas.
    clean_email = person.email if person else (email.strip().lower()[:320] or None)
    # Sem nome.
    if not clean_name:
        raise ValueError("Informe o nome do participante.")
    # E-mail obviamente inválido.
    if clean_email and "@" not in clean_email:
        raise ValueError(f"E-mail inválido: {clean_email}")
    # Forma comparável do nome.
    key = normalize(clean_name)
    # Participantes que já estão na reunião.
    current = participants(session, meeting)
    # Nome repetido (o pedido da dona: o nome é único na reunião).
    if any(p.name_key == key for p in current):
        raise ValueError(f"Já existe um participante chamado “{clean_name}” nesta reunião.")
    # E-mail repetido.
    if clean_email and any(p.email == clean_email for p in current):
        raise ValueError(f"O e-mail {clean_email} já é de outro participante desta reunião.")
    # Sem pessoa escolhida, mas o e-mail é de alguém cadastrado na Empresa da reunião:
    # liga (o e-mail é a chave).
    if person is None and clean_email and meeting.company_id is not None:
        # Procura, só nesta Empresa.
        person = session.scalars(
            select(Stakeholder).where(
                Stakeholder.company_id == meeting.company_id, Stakeholder.email == clean_email
            )
        ).first()
    # Cria.
    participant = MeetingParticipant(
        # Reunião.
        meeting_id=meeting.id,
        # Nome.
        name=clean_name,
        # Nome comparável.
        name_key=key,
        # E-mail.
        email=clean_email,
        # Pessoa, se houver.
        stakeholder_id=person.id if person else None,
    )
    # Prepara.
    session.add(participant)
    # Gera o id.
    session.flush()
    # Devolve.
    return participant


# Tira um participante (as ligações de falante dele somem junto, pelo CASCADE).
def remove_participant(session: Session, meeting: LiveMeeting, participant_id: int) -> None:
    # O participante.
    participant = session.get(MeetingParticipant, participant_id)
    # Só apaga se for desta reunião.
    if participant is not None and participant.meeting_id == meeting.id:
        # Ligações de falante primeiro, depois o participante. Com DELETE direto no banco
        # a ordem é garantida (o banco também apagaria as ligações, pelo CASCADE).
        session.execute(
            delete(MeetingSpeaker).where(MeetingSpeaker.participant_id == participant.id)
        )
        # O participante (as falas já salvas ficam, sem o participante: SET NULL).
        session.execute(delete(MeetingParticipant).where(MeetingParticipant.id == participant.id))
        # Esquece o objeto apagado, para a sessão não tentar gravá-lo de novo.
        session.expunge(participant)


# Diz qual participante é um falante (participant_id None desfaz a ligação).
def assign_speaker(
    session: Session, meeting: LiveMeeting, channel: str, label: str, participant_id: int | None
) -> None:
    # Ligação atual desse falante.
    link = session.scalars(
        select(MeetingSpeaker).where(
            MeetingSpeaker.meeting_id == meeting.id,
            MeetingSpeaker.channel == channel,
            MeetingSpeaker.label == label,
        )
    ).first()
    # Desfazer.
    if participant_id is None:
        # Havia ligação.
        if link is not None:
            # Apaga.
            session.delete(link)
        # Envia ao banco.
        session.flush()
        # Fim.
        return
    # O participante.
    participant = session.get(MeetingParticipant, participant_id)
    # Precisa ser desta reunião.
    if participant is None or participant.meeting_id != meeting.id:
        raise ValueError("Participante não encontrado nesta reunião.")
    # Ligação nova.
    if link is None:
        # Cria.
        link = MeetingSpeaker(meeting_id=meeting.id, channel=channel, label=label[:200])
        # Prepara.
        session.add(link)
    # Aponta para o participante.
    link.participant_id = participant.id
    # Envia ao banco.
    session.flush()


# Falante (canal, rótulo) → participante, para montar a transcrição com nomes.
def participant_map(
    session: Session, meeting: LiveMeeting
) -> dict[tuple[str, str], MeetingParticipant]:
    # Ligações e participantes juntos.
    rows = session.execute(
        select(MeetingSpeaker, MeetingParticipant)
        .join(MeetingParticipant, MeetingParticipant.id == MeetingSpeaker.participant_id)
        .where(MeetingSpeaker.meeting_id == meeting.id)
    ).all()
    # Dicionário.
    return {(link.channel, link.label): participant for link, participant in rows}


# "canal|rótulo" → nome, para o navegador trocar os rótulos pelos nomes.
def name_map(session: Session, meeting: LiveMeeting) -> dict[str, str]:
    # A partir do mapa de participantes.
    return {
        speaker_key(channel, label): participant.name
        for (channel, label), participant in participant_map(session, meeting).items()
    }


# Um falante que apareceu na reunião, com quantas falas tem.
@dataclass(frozen=True)
class SpeakerRow:
    # Canal.
    channel: str
    # Rótulo do serviço.
    label: str
    # Falas definitivas com esse rótulo.
    count: int
    # Participante ligado, se houver.
    participant_id: int | None


# Falantes da reunião: os que já falaram, e o "Você" sempre (para ligar você mesma).
def speakers(session: Session, meeting: LiveMeeting) -> list[SpeakerRow]:
    # Contagem por canal e rótulo.
    counts = session.execute(
        select(TranscriptSegment.channel, TranscriptSegment.speaker, func.count())
        .where(
            TranscriptSegment.session_id == meeting.session_id,
            TranscriptSegment.channel.in_(("voce", "outros")),
        )
        .group_by(TranscriptSegment.channel, TranscriptSegment.speaker)
    ).all()
    # (canal, rótulo) → quantidade.
    found = {(channel, label or ""): n for channel, label, n in counts}
    # Ligações atuais.
    links = {key: p.id for key, p in participant_map(session, meeting).items()}
    # O "Você" aparece mesmo antes da primeira fala.
    found.setdefault(("voce", "Você"), 0)
    # Rótulos ligados que ainda não falaram (raro) também aparecem.
    for key in links:
        # Sem fala: zero.
        found.setdefault(key, 0)

    # "Você" primeiro, depois Falante 1, 2, 3... (o número em ordem numérica).
    def order(key: tuple[str, str]) -> tuple:
        # Número do rótulo, se houver.
        digits = re.findall(r"\d+", key[1])
        # Canal do microfone antes; depois pelo número.
        return (key[0] != "voce", int(digits[0]) if digits else 0, key[1])

    # Monta as linhas.
    return [
        SpeakerRow(c, lb, found[(c, lb)], links.get((c, lb))) for c, lb in sorted(found, key=order)
    ]


# --- Convidados da agenda ---------------------------------------------------------


# Código da reunião no endereço do Meet ("abc-defg-hij"), ou None.
def meet_code(url: str | None) -> str | None:
    # Procura o padrão.
    match = _MEET_CODE.search(url or "")
    # Em minúsculas, para comparar.
    return match.group(1).lower() if match else None


# Evento da agenda com o mesmo endereço do Meet da reunião, entre os eventos dados.
def matching_event(meeting: LiveMeeting, events: list):
    # Código da reunião ao vivo.
    code = meet_code(meeting.meeting_url)
    # Sem código (endereço desconhecido): não dá para achar.
    if code is None:
        return None
    # Primeiro evento com o mesmo código.
    return next((e for e in events if meet_code(e.meeting_url) == code), None)


# Inclui os convidados do evento como participantes. Devolve quantos entraram.
def add_invitees(session: Session, meeting: LiveMeeting, event) -> int:
    # Contador.
    added = 0
    # Cada convidado com e-mail (nome da agenda → e-mail).
    for name, email in event.attendee_emails.items():
        # Tenta incluir.
        try:
            # Nome da agenda e e-mail (se a pessoa estiver em Pessoas, liga pelo e-mail).
            add_participant(session, meeting, name=name, email=email)
            # Conta.
            added += 1
        # Já estava (nome ou e-mail repetido): pula.
        except ValueError:
            # Próximo convidado.
            continue
    # Devolve.
    return added
