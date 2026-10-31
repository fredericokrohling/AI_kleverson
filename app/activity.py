"""Seção "Processamento" da página inicial: o diário de ações (auditoria), dia a dia.

Regras pedidas pela dona:
- Sem data escolhida, mostra o dia atual, já rolado até os 6 eventos mais recentes.
- Com uma data escolhida no calendário, mostra aquele dia a partir do primeiro evento.
- Os eventos vêm em ordem cronológica (o primeiro do dia em cima).
- Cabem 6 linhas visíveis; a rolagem vai até 30 eventos. Mais que isso, setas trocam de
  página (30 eventos por página).

O "dia" é o do fuso do console (CONSOLE_TIMEZONE, padrão America/Sao_Paulo): o banco
guarda em UTC, e sem a conversão o "dia atual" viraria às 21h de Brasília.
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Data, data e hora, hora zero, intervalo e o fuso UTC.
from datetime import UTC, date, datetime, time, timedelta

# ceil arredonda para cima (número de páginas).
from math import ceil

# Fusos horários pelo nome (ex.: America/Sao_Paulo).
from zoneinfo import ZoneInfo

# Consultas.
from sqlalchemy import func, select

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# A tabela do diário de ações.
from app.models import AuditLog

# Eventos por página (o máximo que a rolagem mostra).
PAGE_SIZE = 30


# O que a seção precisa para se desenhar.
@dataclass
class ActivityPage:
    # Dia mostrado (no fuso do console).
    day: date
    # Hoje, no fuso do console (limite do calendário).
    today: date
    # Página atual (começa em 1).
    page: int
    # Total de páginas do dia (pelo menos 1).
    pages: int
    # Total de eventos do dia.
    total: int
    # Eventos da página, em ordem cronológica.
    rows: list[AuditLog]

    # True quando o dia mostrado é hoje.
    @property
    def is_today(self) -> bool:
        return self.day == self.today

    # A rolagem abre no fim (eventos mais recentes) só na última página de hoje.
    @property
    def scroll_to_end(self) -> bool:
        return self.is_today and self.page == self.pages


# Converte o texto do calendário (AAAA-MM-DD) em data; inválido ou vazio vira None.
def parse_day(value: str | None) -> date | None:
    # Tenta converter.
    try:
        # Formato do <input type="date">.
        return date.fromisoformat(value) if value else None
    # Texto adulterado: ignora e usa o padrão.
    except ValueError:
        return None


# Monta a página de eventos de um dia.
def activity_page(
    session: Session,
    timezone: str,
    day: date | None = None,
    page: int | None = None,
    now: datetime | None = None,
) -> ActivityPage:
    """Sem dia: hoje, na última página. Com dia: aquele dia, na primeira página."""
    # Fuso do console.
    tz = ZoneInfo(timezone)
    # Hoje nesse fuso.
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    # Dia escolhido; sem escolha (ou no futuro), hoje.
    day = day if day is not None and day <= today else today
    # Início do dia no fuso do console.
    start = datetime.combine(day, time.min, tzinfo=tz)
    # Início do dia seguinte.
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    # Filtro do dia.
    in_day = (AuditLog.created_at >= start, AuditLog.created_at < end)
    # Quantos eventos no dia.
    total = session.scalar(select(func.count()).select_from(AuditLog).where(*in_day)) or 0
    # Páginas (pelo menos uma, mesmo vazia).
    pages = max(1, ceil(total / PAGE_SIZE))
    # Sem página pedida: hoje abre na última (mais recentes); outro dia, na primeira.
    if page is None:
        # Escolhe a página inicial.
        page = pages if day == today else 1
    # Mantém dentro dos limites.
    page = min(max(page, 1), pages)
    # Eventos da página, do mais antigo ao mais recente (id desempata o mesmo instante).
    rows = session.scalars(
        select(AuditLog)
        .where(*in_day)
        .order_by(AuditLog.created_at, AuditLog.id)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    # Devolve.
    return ActivityPage(day, today, page, pages, total, list(rows))
