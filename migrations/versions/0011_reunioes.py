"""reuniões: uma linha por reunião, com participantes ligados às Pessoas

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-03 20:00:00

Até aqui, a reunião só existia dentro de cada transcrição: a importada guardava o evento
da agenda no "extra" (JSON), e a feita pela extensão guardava a reunião ao vivo, sem o
evento. Duas transcrições da mesma reunião não se encontravam, e os convidados da agenda
eram texto, não Pessoas.

Esta migração cria:
- meetings: a reunião (Empresa, evento da agenda, título, início, fim, organizador,
  endereço, plataforma). Um evento da agenda vira uma linha por Empresa;
- meeting_attendees: os participantes, cada um ligado à Pessoa pelo e-mail;
- bronze_items.meeting_id e live_meetings.meeting_id: a transcrição e a captura ao vivo
  apontam para a reunião.

E preenche com o que já existe (ver _backfill). As travas de Empresa da 0008 passam a
valer também aqui: reunião, participantes, transcrição e captura são da mesma Empresa.
Cada coluna aqui espelha uma coluna comentada em app/models.py (Meeting, MeetingAttendee).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
# re: achar o código da reunião no endereço do Meet.
import re

# unicodedata: tirar os acentos dos nomes (forma comparável).
import unicodedata
from collections.abc import Sequence

# datetime: ler as datas guardadas em texto no JSON do evento; timedelta: folga de horário.
from datetime import datetime, timedelta

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar coluna, rodar SQL...).
from alembic import op

# Identificador desta migração.
revision: str = "0011"
# Migração anterior: esta só roda depois da 0010.
down_revision: str | Sequence[str] | None = "0010"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None

# Valor gravado no banco para "transcrição de reunião" (SourceKind.MEET_TRANSCRIPT).
TRANSCRIPT = "transcricao_reuniao"
# Código da reunião no endereço do Meet ("abc-defg-hij"). Cópia do de app/transcription/
# live.py: a migração não importa o código do app, que pode mudar depois dela.
_MEET_CODE = re.compile(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", re.I)
# Sufixos que a agenda e o Teams põem no nome ("(Convidado)"). Cópia de app/people.py.
_SUFFIX = re.compile(r"\s*\((convidad[oa]|guest|external|externo|externa|unverified)\)\s*$", re.I)
# Folga ao comparar o horário da captura ao vivo com o do evento (gente entra antes, sai
# depois, a reunião estoura).
_SLACK = timedelta(hours=1)

# Trava 1 da 0008 nas tabelas novas e nas que ganharam a coluna: tabela → pares
# (tabela apontada, coluna). "self" é a própria Empresa da linha.
_SAME_COMPANY = {
    "meeting_attendees": ["meetings", "meeting_id", "stakeholders", "stakeholder_id"],
    "bronze_items": ["self", "company_id", "meetings", "meeting_id"],
    "live_meetings": [
        "self",
        "company_id",
        "bronze_items",
        "bronze_item_id",
        "meetings",
        "meeting_id",
    ],
}
# O gatilho de live_meetings como era na 0008 (o downgrade volta para ele).
_LIVE_BEFORE = ["self", "company_id", "bronze_items", "bronze_item_id"]


# Nome na forma comparável: sem acento, minúsculo, um espaço. Cópia de app/people.py.
def _normalize(name: str | None) -> str:
    # Sem nome.
    if not name:
        return ""
    # Tira "(Convidado)" e parecidos.
    name = _SUFFIX.sub("", name)
    # Separa letra e acento e joga fora os acentos.
    plain = "".join(
        c for c in unicodedata.normalize("NFKD", name) if unicodedata.category(c) != "Mn"
    )
    # Minúsculas e espaços simples.
    return " ".join(plain.lower().split())


# Código do Meet de um endereço, ou None.
def _code(url: str | None) -> str | None:
    # Procura.
    match = _MEET_CODE.search(url or "")
    # Em minúsculas.
    return match.group(1).lower() if match else None


# Lê uma data ISO 8601 do JSON; None se vier vazia ou estranha.
def _parse(value) -> datetime | None:
    # Tenta.
    try:
        # Python lê "2026-11-03T10:00:00-03:00".
        return datetime.fromisoformat(value) if value else None
    # Formato estranho.
    except (TypeError, ValueError):
        return None


# Gatilho da trava 1 numa tabela, com os pares dados.
def _create_same_company(table: str, args: list[str]) -> None:
    # Argumentos entre aspas simples, separados por vírgula.
    quoted = ", ".join(f"'{a}'" for a in args)
    # Conferência no commit (DEFERRABLE INITIALLY DEFERRED), como na 0008.
    op.execute(
        f"CREATE CONSTRAINT TRIGGER empresa_coerente AFTER INSERT OR UPDATE ON {table} "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION empresa_coerente({quoted})"
    )


# Aplica a migração.
def upgrade() -> None:
    # Tabela das reuniões.
    op.create_table(
        "meetings",
        # Identificador.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Empresa (obrigatória).
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        # Id do evento da agenda; vazio = reunião fora da agenda.
        sa.Column("calendar_event_id", sa.String(1024), nullable=True),
        # Título.
        sa.Column("title", sa.Text(), nullable=False),
        # Início.
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        # Fim.
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        # Falso quando só se sabe o dia.
        sa.Column("time_known", sa.Boolean(), server_default="true", nullable=False),
        # Organizador.
        sa.Column("organizer", sa.String(320), nullable=True),
        # Endereço.
        sa.Column("meeting_url", sa.String(500), nullable=True),
        # Plataforma.
        sa.Column("platform", sa.String(20), nullable=True),
        # Quando a linha foi criada.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # O mesmo evento não se repete na mesma Empresa.
        sa.UniqueConstraint("company_id", "calendar_event_id"),
    )
    # Índices: filtrar por Empresa e ordenar por data.
    op.create_index("ix_meetings_company_id", "meetings", ["company_id"])
    # Data.
    op.create_index("ix_meetings_starts_at", "meetings", ["starts_at"])
    # Participantes.
    op.create_table(
        "meeting_attendees",
        # Identificador.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Reunião (apagou a reunião, apagam os participantes).
        sa.Column(
            "meeting_id",
            sa.Integer(),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Nome.
        sa.Column("name", sa.String(200), nullable=False),
        # Nome comparável.
        sa.Column("name_key", sa.String(200), nullable=False),
        # E-mail.
        sa.Column("email", sa.String(320), nullable=True),
        # Pessoa.
        sa.Column(
            "stakeholder_id",
            sa.Integer(),
            sa.ForeignKey("stakeholders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # O mesmo e-mail uma vez por reunião.
        sa.UniqueConstraint("meeting_id", "email"),
    )
    # Índices das duas chaves estrangeiras.
    op.create_index("ix_meeting_attendees_meeting_id", "meeting_attendees", ["meeting_id"])
    # Pessoa.
    op.create_index("ix_meeting_attendees_stakeholder_id", "meeting_attendees", ["stakeholder_id"])
    # A transcrição e a captura ao vivo apontam para a reunião.
    for table in ("bronze_items", "live_meetings"):
        # Coluna (apagar a reunião só desliga).
        op.add_column(
            table,
            sa.Column(
                "meeting_id",
                sa.Integer(),
                sa.ForeignKey("meetings.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        # Índice.
        op.create_index(f"ix_{table}_meeting_id", table, ["meeting_id"])

    # Preenche com o que já existe.
    _backfill(op.get_bind())

    # Travas de Empresa. live_meetings já tinha a trava 1 (sem a reunião): troca.
    op.execute("DROP TRIGGER empresa_coerente ON live_meetings")
    # Uma por tabela.
    for table, args in _SAME_COMPANY.items():
        _create_same_company(table, args)
    # A Empresa de uma reunião não muda (trava 3): corrigir a Empresa de uma transcrição
    # liga a transcrição a uma reunião da Empresa nova (app/meetings.py).
    op.execute(
        "CREATE TRIGGER empresa_fixa BEFORE UPDATE OF company_id ON meetings "
        "FOR EACH ROW EXECUTE FUNCTION empresa_fixa()"
    )


# Cria as reuniões das transcrições que já existem e liga tudo.
def _backfill(conn) -> None:
    """Três casos, nesta ordem:
    1. transcrição importada de um evento da agenda: uma reunião por (Empresa, evento),
       com os convidados do evento;
    2. transcrição feita pela extensão: se o endereço do Meet e o horário batem com uma
       reunião do passo 1 da mesma Empresa, é a mesma reunião (as duas transcrições ficam
       juntas); senão, uma reunião nova com os participantes cadastrados nela;
    3. o resto (importada fora da agenda, ou da extensão antiga sem captura): uma reunião
       por transcrição, com o título e a data dela.
    """
    # Reuniões da agenda criadas no passo 1: (Empresa, evento) → (id, código, início, fim).
    by_event: dict[tuple[int, str], tuple[int, str | None, datetime, datetime | None]] = {}
    # Transcrições, na ordem em que entraram.
    items = conn.execute(
        sa.text(
            "SELECT b.id, b.company_id, b.subject, b.occurred_at, b.ingested_at, b.author, "
            "b.thread_id, b.extra, m.id AS live_id, m.title AS live_title, "
            "m.meeting_url AS live_url, m.started_at, m.finished_at "
            "FROM bronze_items b LEFT JOIN live_meetings m ON m.bronze_item_id = b.id "
            "WHERE b.source = :s ORDER BY b.id"
        ),
        {"s": TRANSCRIPT},
    ).all()

    # Passo 1: importadas de um evento.
    for row in items:
        # Dados do evento.
        event = (row.extra or {}).get("evento_agenda") or {}
        # Sem evento: fica para os passos seguintes.
        if not event.get("id"):
            continue
        # Chave.
        key = (row.company_id, event["id"])
        # Reunião já criada por outra transcrição do mesmo evento.
        if key not in by_event:
            # Início e fim do evento (sem início válido, a data da transcrição).
            start = _parse(event.get("inicio")) or row.occurred_at or row.ingested_at
            # Fim.
            end = _parse(event.get("fim"))
            # Cria.
            meeting_id = _insert_meeting(
                conn,
                company_id=row.company_id,
                event_id=event["id"],
                title=event.get("titulo") or row.subject or "Reunião sem título",
                starts_at=start,
                ends_at=end,
                time_known=True,
                organizer=event.get("organizador"),
                url=event.get("url_reuniao"),
                platform=(row.extra or {}).get("plataforma"),
            )
            # Convidados: nome → e-mail.
            emails = event.get("participantes_emails") or {}
            # Cada convidado da lista (com o e-mail, se a agenda souber).
            for name in event.get("participantes") or []:
                _add_attendee(conn, meeting_id, name, emails.get(name))
            # Guarda.
            by_event[key] = (meeting_id, _code(event.get("url_reuniao")), start, end)
        # Liga a transcrição.
        _link_item(conn, row.id, by_event[key][0])

    # Passos 2 e 3: as que não vieram de um evento.
    for row in items:
        # Já ligada no passo 1.
        if ((row.extra or {}).get("evento_agenda") or {}).get("id"):
            continue
        # Transcrição da extensão, com a captura ao vivo.
        if row.live_id is not None:
            # Código do Meet e horário da captura.
            code = _code(row.live_url)
            # Início.
            start = row.started_at
            # Reunião da agenda da mesma Empresa com o mesmo endereço e horário próximo.
            meeting_id = next(
                (
                    mid
                    for (company, _), (mid, ev_code, ev_start, ev_end) in by_event.items()
                    if company == row.company_id
                    and code is not None
                    and ev_code == code
                    and ev_start - _SLACK <= start <= (ev_end or ev_start) + _SLACK
                ),
                None,
            )
            # Achou: é a mesma reunião. A transcrição passa a ter o thread_id do evento,
            # como a importada, e a classificação "mesma conversa" junta as duas.
            if meeting_id is not None:
                # O id do evento dessa reunião.
                event_id = next(e for (c, e), v in by_event.items() if v[0] == meeting_id)
                # Grava o thread_id, se a transcrição não tiver um.
                conn.execute(
                    sa.text(
                        "UPDATE bronze_items SET thread_id = :t WHERE id = :i AND thread_id IS NULL"
                    ),
                    {"t": f"agenda:{event_id}", "i": row.id},
                )
            # Não achou: reunião nova, com os dados da captura.
            else:
                meeting_id = _insert_meeting(
                    conn,
                    company_id=row.company_id,
                    event_id=None,
                    title=row.live_title or row.subject or "Reunião sem título",
                    starts_at=start,
                    ends_at=row.finished_at,
                    time_known=True,
                    organizer=None,
                    url=row.live_url,
                    platform="meet" if code else None,
                )
            # Participantes cadastrados na captura.
            for name, email in conn.execute(
                sa.text("SELECT name, email FROM meeting_participants WHERE meeting_id = :m"),
                {"m": row.live_id},
            ).all():
                _add_attendee(conn, meeting_id, name, email)
            # A captura aponta para a reunião.
            conn.execute(
                sa.text("UPDATE live_meetings SET meeting_id = :m WHERE id = :i"),
                {"m": meeting_id, "i": row.live_id},
            )
        # Resto: uma reunião por transcrição.
        else:
            # Importada fora da agenda: a data guardada é meia-noite do dia escolhido.
            imported = (row.extra or {}).get("origem") == "importada"
            # Cria.
            meeting_id = _insert_meeting(
                conn,
                company_id=row.company_id,
                event_id=None,
                title=row.subject or "Reunião sem título",
                starts_at=row.occurred_at or row.ingested_at,
                ends_at=None,
                time_known=not imported,
                organizer=row.author,
                url=(row.extra or {}).get("url_reuniao"),
                platform=(row.extra or {}).get("plataforma"),
            )
            # Participantes guardados na transcrição da extensão antiga.
            for p in (row.extra or {}).get("participantes") or []:
                # Formato {nome, email}.
                if isinstance(p, dict) and p.get("nome"):
                    _add_attendee(conn, meeting_id, p["nome"], p.get("email"))
        # Liga a transcrição.
        _link_item(conn, row.id, meeting_id)

    # Pessoa de cada participante: o e-mail igual ao de uma Pessoa da Empresa da reunião.
    conn.execute(
        sa.text(
            "UPDATE meeting_attendees a SET stakeholder_id = s.id "
            "FROM meetings m, stakeholders s "
            "WHERE a.meeting_id = m.id AND s.company_id = m.company_id "
            "AND lower(s.email) = a.email AND a.stakeholder_id IS NULL"
        )
    )


# Grava uma reunião e devolve o id.
def _insert_meeting(conn, **v) -> int:
    # INSERT ... RETURNING id.
    return conn.execute(
        sa.text(
            "INSERT INTO meetings (company_id, calendar_event_id, title, starts_at, ends_at, "
            "time_known, organizer, meeting_url, platform) VALUES (:company_id, :event_id, "
            ":title, :starts_at, :ends_at, :time_known, :organizer, :url, :platform) RETURNING id"
        ),
        {
            **v,
            # Limites das colunas.
            "organizer": (v["organizer"] or None) and v["organizer"][:320],
            "url": (v["url"] or None) and v["url"][:500],
            "platform": (v["platform"] or None) and v["platform"][:20],
        },
    ).scalar()


# Inclui um participante, sem repetir e-mail (nem nome, quando não há e-mail).
def _add_attendee(conn, meeting_id: int, name: str | None, email: str | None) -> None:
    # Nome limpo (sem nome, o e-mail).
    name = " ".join((name or email or "").split())[:200]
    # E-mail em minúsculas.
    email = (email or "").strip().lower()[:320] or None
    # Sem nada: pula.
    if not name:
        return
    # Já está, pelo e-mail ou (sem e-mail) pelo nome.
    found = conn.execute(
        sa.text(
            "SELECT id FROM meeting_attendees WHERE meeting_id = :m "
            "AND (email = CAST(:e AS varchar) OR (name_key = :k AND (email IS NULL OR CAST(:e AS varchar) IS NULL)))"
        ),
        {"m": meeting_id, "e": email, "k": _normalize(name)},
    ).first()
    # Já estava: completa o e-mail, se faltava.
    if found is not None:
        conn.execute(
            sa.text(
                "UPDATE meeting_attendees SET email = coalesce(email, CAST(:e AS varchar)) "
                "WHERE id = :i"
            ),
            {"e": email, "i": found.id},
        )
        # Pronto.
        return
    # Novo.
    conn.execute(
        sa.text(
            "INSERT INTO meeting_attendees (meeting_id, name, name_key, email) "
            "VALUES (:m, :n, :k, :e)"
        ),
        {"m": meeting_id, "n": name, "k": _normalize(name), "e": email},
    )


# Liga uma transcrição à reunião.
def _link_item(conn, item_id: int, meeting_id: int) -> None:
    # UPDATE simples.
    conn.execute(
        sa.text("UPDATE bronze_items SET meeting_id = :m WHERE id = :i"),
        {"m": meeting_id, "i": item_id},
    )


# Desfaz a migração (as transcrições continuam com o evento no "extra").
def downgrade() -> None:
    # Trava 3 das reuniões.
    op.execute("DROP TRIGGER empresa_fixa ON meetings")
    # Trava 1 das três tabelas.
    for table in _SAME_COMPANY:
        op.execute(f"DROP TRIGGER empresa_coerente ON {table}")
    # live_meetings volta ao gatilho da 0008.
    _create_same_company("live_meetings", _LIVE_BEFORE)
    # Colunas de ligação.
    for table in ("live_meetings", "bronze_items"):
        # Índice.
        op.drop_index(f"ix_{table}_meeting_id", table_name=table)
        # Coluna.
        op.drop_column(table, "meeting_id")
    # Participantes.
    op.drop_table("meeting_attendees")
    # Reuniões.
    op.drop_table("meetings")
