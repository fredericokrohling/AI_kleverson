"""Visão de um dia da agenda, desenhada como uma coluna de calendário.

Por que um dia de calendário, e não uma lista: ver as reuniões no lugar do horário mostra
de cara o que já tem transcrição e o que ficou sem, e deixa ligar a transcrição clicando
na própria reunião. É o mesmo formato do Google Agenda e do Outlook, que você já conhece.

Este módulo só faz contas (posição, altura, colunas lado a lado); o desenho fica no
modelo HTML _agenda_dia.html. Separar assim deixa as contas testáveis sem navegador.
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Data, data e hora, hora zero.
from datetime import date, datetime, time

# Fusos horários pelo nome.
from zoneinfo import ZoneInfo

# Consultas.
from sqlalchemy import select

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Tabela do bronze e o tipo "transcrição de reunião".
from app.models import BronzeItem, SourceKind

# Reunião lida da agenda.
from app.transcription.calendar import CalendarEvent

# Horário padrão mostrado: das 8h às 19h, um dia de trabalho comum.
DEFAULT_START_HOUR = 8
# Hora em que a grade termina, se nenhuma reunião passar dela.
DEFAULT_END_HOUR = 19
# Altura mínima de uma reunião, em minutos da grade: reunião de 5 minutos ainda precisa
# de espaço para o título.
MIN_MINUTES = 25


# Uma reunião já posicionada na grade.
@dataclass(frozen=True)
class AgendaBlock:
    # A reunião lida da agenda.
    event: CalendarEvent
    # Minutos entre o topo da grade e o início da reunião.
    top: int
    # Altura, em minutos.
    height: int
    # Coluna da reunião quando há outras no mesmo horário (0 = primeira).
    lane: int
    # Quantas colunas o grupo de reuniões sobrepostas usa.
    lanes: int
    # Id da transcrição já ligada a esta reunião (None = ainda sem transcrição).
    item_id: int | None
    # "ligada" (já tem transcrição), "meet" (o Meet tem transcrição pronta para baixar)
    # ou "pendente" (sem transcrição).
    status: str


# O dia inteiro, pronto para desenhar.
@dataclass(frozen=True)
class AgendaDay:
    # Dia mostrado.
    day: date
    # Primeira hora da grade.
    start_hour: int
    # Hora em que a grade termina.
    end_hour: int
    # Reuniões posicionadas.
    blocks: list[AgendaBlock]
    # Minutos entre o topo da grade e agora (None quando o dia não é hoje ou já passou da grade).
    now: int | None
    # O dia mostrado é hoje? (o título diz "Hoje" em vez do dia da semana)
    today: bool = False

    # Horas da grade, para as linhas e os rótulos (8, 9, ..., 18).
    @property
    def hours(self) -> list[int]:
        # Uma linha por hora inteira.
        return list(range(self.start_hour, self.end_hour))

    # Altura total da grade, em minutos.
    @property
    def total_minutes(self) -> int:
        # Horas vezes 60.
        return (self.end_hour - self.start_hour) * 60

    # Quantas reuniões já têm transcrição (resumo no topo do cartão).
    @property
    def linked(self) -> int:
        # Conta as ligadas.
        return sum(1 for b in self.blocks if b.status == "ligada")


# Transcrições já ligadas a cada reunião: id do evento → id do item no bronze.
def linked_transcripts(session: Session, event_ids: list[str]) -> dict[str, int]:
    """A importação grava thread_id = "agenda:<id do evento>"; é por ele que se liga."""
    # Sem reuniões, sem consulta.
    if not event_ids:
        return {}
    # Os thread_id procurados.
    threads = [f"agenda:{event_id}" for event_id in event_ids]
    # Itens de transcrição com esses thread_id; o mais antigo primeiro (fica o primeiro).
    rows = session.execute(
        # Só as duas colunas usadas.
        select(BronzeItem.thread_id, BronzeItem.id)
        # Transcrições de reunião ligadas a um destes eventos.
        .where(BronzeItem.source == SourceKind.MEET_TRANSCRIPT, BronzeItem.thread_id.in_(threads))
        # Ordem estável.
        .order_by(BronzeItem.id)
    ).all()
    # Resultado.
    found: dict[str, int] = {}
    # Cada linha.
    for thread_id, item_id in rows:
        # Tira o prefixo "agenda:" e guarda só a primeira transcrição de cada reunião.
        found.setdefault(thread_id.removeprefix("agenda:"), item_id)
    # Devolve.
    return found


# Minutos desde a meia-noite do dia, recortados ao próprio dia (reunião que vira a noite).
def _minute_of_day(moment: datetime, day: date, zone: ZoneInfo) -> int:
    # Meia-noite do dia, no fuso do console.
    midnight = datetime.combine(day, time.min, tzinfo=zone)
    # Diferença em minutos (o horário pode vir em outro fuso; a subtração cuida disso).
    minutes = int((moment - midnight).total_seconds() // 60)
    # Entre 0 (meia-noite) e 1440 (meia-noite seguinte).
    return max(0, min(24 * 60, minutes))


# Monta o dia: posição, altura e coluna de cada reunião.
def build_day(
    events: list[CalendarEvent],
    linked: dict[str, int],
    day: date,
    timezone: str,
    now: datetime | None = None,
) -> AgendaDay:
    """Reuniões sobrepostas ficam lado a lado, como no Google Agenda."""
    # Fuso do console.
    zone = ZoneInfo(timezone)
    # Início e fim de cada reunião, em minutos do dia, já em ordem de início.
    spans = sorted(
        # (início, fim, reunião) de cada uma.
        (
            (
                # Início em minutos.
                _minute_of_day(e.start, day, zone),
                # Fim em minutos.
                _minute_of_day(e.end, day, zone),
                # A reunião.
                e,
            )
            # Cada reunião.
            for e in events
        ),
        # Ordena pelo início e, empatando, pela mais longa primeiro.
        key=lambda s: (s[0], -s[1]),
    )
    # A grade cresce para caber reuniões fora do horário padrão.
    start_hour = min([DEFAULT_START_HOUR] + [s // 60 for s, _, _ in spans])
    # Hora em que a grade termina: arredonda o fim para cima (-(-x // 60) é a divisão para cima).
    end_hour = max([DEFAULT_END_HOUR] + [-(-max(f, s + MIN_MINUTES) // 60) for s, f, _ in spans])
    # Nunca passa de meia-noite.
    end_hour = min(24, end_hour)
    # Topo da grade, em minutos do dia.
    origin = start_hour * 60
    # Coluna de cada reunião (mesma ordem de spans).
    lanes: list[int] = []
    # Grupos de reuniões sobrepostas: cada grupo é uma lista de índices em spans.
    groups: list[list[int]] = []
    # Fim de cada coluna do grupo atual (minuto em que a coluna fica livre).
    lane_ends: list[int] = []
    # Fim do grupo atual (a reunião que termina mais tarde).
    group_end = -1
    # Cada reunião, na ordem de início.
    for index, (start, end, _) in enumerate(spans):
        # Fim usado nas contas: no mínimo a altura mínima, para o título caber.
        end = max(end, start + MIN_MINUTES)
        # Começa depois de tudo do grupo atual terminar: abre um grupo novo.
        if start >= group_end:
            # Grupo novo, só com esta reunião.
            groups.append([])
            # Colunas zeradas.
            lane_ends = []
        # Primeira coluna já livre neste horário.
        lane = next((i for i, free_at in enumerate(lane_ends) if free_at <= start), None)
        # Nenhuma livre: abre uma coluna nova.
        if lane is None:
            # A nova coluna é a última.
            lane = len(lane_ends)
            # Acrescenta.
            lane_ends.append(end)
        # Achou uma livre: ocupa até o fim desta reunião.
        else:
            lane_ends[lane] = end
        # Guarda a coluna.
        lanes.append(lane)
        # Põe a reunião no grupo atual.
        groups[-1].append(index)
        # O grupo vai até a reunião que termina mais tarde.
        group_end = max(group_end, end)
    # Quantas colunas cada reunião divide (o máximo do seu grupo).
    width = [1] * len(spans)
    # Cada grupo.
    for group in groups:
        # Colunas usadas pelo grupo.
        count = max(lanes[i] for i in group) + 1
        # Vale para todas as reuniões do grupo.
        for i in group:
            width[i] = count
    # Monta os blocos.
    blocks = []
    # Cada reunião.
    for index, (start, end, event) in enumerate(spans):
        # Transcrição ligada, se houver.
        item_id = linked.get(event.id)
        # Situação: ligada > Meet com transcrição anexada > pendente.
        status = "ligada" if item_id else ("meet" if event.transcripts else "pendente")
        # O bloco.
        blocks.append(
            AgendaBlock(
                # Reunião.
                event=event,
                # Distância do topo da grade.
                top=start - origin,
                # Altura, com o mínimo.
                height=max(end - start, MIN_MINUTES),
                # Coluna.
                lane=lanes[index],
                # Colunas do grupo.
                lanes=width[index],
                # Transcrição ligada.
                item_id=item_id,
                # Situação.
                status=status,
            )
        )
    # Linha do "agora": só no dia de hoje e dentro da grade.
    now_line = None
    # Hora atual no fuso do console (o teste pode passar uma fixa).
    current = (now or datetime.now(zone)).astimezone(zone)
    # É hoje?
    is_today = current.date() == day
    # Só hoje tem a linha do "agora".
    if is_today:
        # Minutos desde o topo da grade.
        minute = current.hour * 60 + current.minute - origin
        # Só se estiver dentro da grade.
        if 0 <= minute <= (end_hour - start_hour) * 60:
            now_line = minute
    # O dia pronto.
    return AgendaDay(day, start_hour, end_hour, blocks, now_line, is_today)
