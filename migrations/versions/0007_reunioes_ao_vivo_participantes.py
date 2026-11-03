"""reuniões ao vivo, participantes, falantes e e-mail obrigatório na pessoa

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03 14:00:00

- live_meetings: cada captura da extensão (uma aba do Meet), do clique até ser salva;
- meeting_participants: quem participa de cada reunião, com nome único na reunião;
- meeting_speakers: qual participante é cada voz separada pelo serviço ("Falante 2");
- utterances.participant_id: a fala aponta para o participante da reunião;
- stakeholders.email: passa a ser obrigatório e sempre em minúsculas (é a chave da pessoa).
Cada coluna aqui espelha uma coluna comentada em app/models.py.
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna e restrições do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema.
from alembic import op

# Identificador desta migração.
revision: str = "0007"
# Migração anterior.
down_revision: str | Sequence[str] | None = "0006"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None


# Aplica a migração.
def upgrade() -> None:
    # Conexão da migração, para conferir os dados antes de mudar a coluna.
    conn = op.get_bind()
    # Pessoas sem e-mail: a coluna não pode ficar obrigatória enquanto elas existirem.
    missing = (
        conn.execute(
            sa.text("SELECT name FROM stakeholders WHERE email IS NULL OR btrim(email) = ''")
        )
        .scalars()
        .all()
    )
    # Há alguma: para com uma mensagem que diz o que fazer (nada foi mudado ainda).
    if missing:
        # O texto aparece no log do "alembic upgrade".
        raise RuntimeError(
            "Estas pessoas estão sem e-mail, que agora é obrigatório: "
            + ", ".join(missing)
            + ". Preencha o e-mail delas em Cadastros → Pessoas (na versão atual) e rode de novo."
        )
    # E-mails que ficariam repetidos ao passar para minúsculas ("Ana@x.com" e "ana@x.com").
    repeated = (
        conn.execute(
            sa.text(
                "SELECT lower(email) FROM stakeholders GROUP BY lower(email) HAVING count(*) > 1"
            )
        )
        .scalars()
        .all()
    )
    # Há algum: mesma parada, com o e-mail repetido.
    if repeated:
        # Mensagem.
        raise RuntimeError(
            "Estes e-mails estão em mais de uma pessoa: "
            + ", ".join(repeated)
            + ". Junte ou corrija essas pessoas em Cadastros e rode de novo."
        )
    # Todo e-mail passa a ser guardado em minúsculas e sem espaços nas pontas.
    op.execute("UPDATE stakeholders SET email = lower(btrim(email))")
    # E a coluna fica obrigatória.
    op.alter_column("stakeholders", "email", existing_type=sa.String(320), nullable=False)

    # Reuniões ao vivo.
    op.create_table(
        # Nome.
        "live_meetings",
        # Id interno.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Id da captura (o mesmo dos trechos), sem repetição.
        sa.Column("session_id", sa.String(64), nullable=False, unique=True),
        # Título da aba do Meet.
        sa.Column("title", sa.Text()),
        # Endereço da reunião.
        sa.Column("meeting_url", sa.String(500)),
        # Começo da captura.
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Último trecho recebido.
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Quando foi salva.
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        # Item do bronze criado ao salvar.
        sa.Column(
            "bronze_item_id",
            sa.BigInteger(),
            sa.ForeignKey("bronze_items.id", ondelete="SET NULL"),
        ),
    )
    # Participantes de cada reunião.
    op.create_table(
        # Nome.
        "meeting_participants",
        # Id interno.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Reunião (apagada, leva os participantes junto).
        sa.Column(
            "meeting_id",
            sa.Integer(),
            sa.ForeignKey("live_meetings.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Nome como escrito.
        sa.Column("name", sa.String(200), nullable=False),
        # Nome comparável (sem acento, minúsculo).
        sa.Column("name_key", sa.String(200), nullable=False),
        # E-mail, opcional.
        sa.Column("email", sa.String(320)),
        # Pessoa cadastrada, quando houver.
        sa.Column(
            "stakeholder_id",
            sa.Integer(),
            sa.ForeignKey("stakeholders.id", ondelete="SET NULL"),
            index=True,
        ),
        # Nome não se repete na reunião.
        sa.UniqueConstraint("meeting_id", "name_key"),
        # E-mail não se repete na reunião.
        sa.UniqueConstraint("meeting_id", "email"),
    )
    # Qual participante é cada voz.
    op.create_table(
        # Nome.
        "meeting_speakers",
        # Id interno.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Reunião.
        sa.Column(
            "meeting_id",
            sa.Integer(),
            sa.ForeignKey("live_meetings.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Canal ("voce" ou "outros").
        sa.Column("channel", sa.String(30), nullable=False),
        # Rótulo do serviço ("Falante 2").
        sa.Column("label", sa.String(200), nullable=False),
        # Participante (tirado da reunião, a ligação some).
        sa.Column(
            "participant_id",
            sa.Integer(),
            sa.ForeignKey("meeting_participants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Um participante por rótulo de cada canal.
        sa.UniqueConstraint("meeting_id", "channel", "label"),
    )
    # A fala passa a apontar para o participante.
    op.add_column(
        # Tabela.
        "utterances",
        # Coluna nova, opcional, com índice.
        sa.Column(
            "participant_id",
            sa.Integer(),
            sa.ForeignKey("meeting_participants.id", ondelete="SET NULL"),
        ),
    )
    # Índice para "todas as falas deste participante".
    op.create_index("ix_utterances_participant_id", "utterances", ["participant_id"])


# Desfaz a migração (na ordem inversa).
def downgrade() -> None:
    # Índice das falas.
    op.drop_index("ix_utterances_participant_id", table_name="utterances")
    # Coluna das falas.
    op.drop_column("utterances", "participant_id")
    # Falantes.
    op.drop_table("meeting_speakers")
    # Participantes.
    op.drop_table("meeting_participants")
    # Reuniões.
    op.drop_table("live_meetings")
    # E-mail volta a ser opcional.
    op.alter_column("stakeholders", "email", existing_type=sa.String(320), nullable=True)
