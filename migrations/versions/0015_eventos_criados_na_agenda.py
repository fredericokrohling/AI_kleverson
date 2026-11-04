"""eventos criados na agenda: registro de cada prazo que o assistente pôs no Google

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-04 16:00:00

Segunda peça do bloco 5 (ações). O botão "Pôr na agenda" (em Fatos, Briefing e Alertas)
cria um evento de dia inteiro na agenda da conta Google da Empresa, com uma marca
invisível. Esta tabela registra cada evento criado, para a página "Na agenda" listar e
apagar sem perguntar ao Google, e para o mesmo fato não virar dois eventos.
Cada coluna aqui espelha uma coluna comentada em app/models.py (CreatedEvent).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar índice...).
from alembic import op

# Identificador desta migração.
revision: str = "0015"
# Migração anterior: esta só roda depois da 0014.
down_revision: str | Sequence[str] | None = "0014"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None


# Aplica a migração.
def upgrade() -> None:
    # Tabela dos eventos criados.
    op.create_table(
        # Nome.
        "created_events",
        # Identificador.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Empresa (obrigatória); sem ondelete, como nas outras tabelas.
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        # Fato de origem; apagar o fato só desfaz o vínculo (o evento continua na agenda).
        sa.Column(
            "fact_id",
            sa.BigInteger(),
            sa.ForeignKey("facts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Id do evento no Google.
        sa.Column("google_event_id", sa.String(1024), nullable=True),
        # Título enviado.
        sa.Column("title", sa.String(500), nullable=False),
        # Dia do evento.
        sa.Column("day", sa.Date(), nullable=False),
        # Quem pediu.
        sa.Column("created_by", sa.String(100), nullable=False),
        # Quando foi apagado.
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        # Quem apagou.
        sa.Column("removed_by", sa.String(100), nullable=True),
        # Observação do apagamento.
        sa.Column("removed_note", sa.String(200), nullable=True),
        # Quando a linha nasceu.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Índice: a página filtra pela Empresa.
    op.create_index("ix_created_events_company_id", "created_events", ["company_id"])
    # Índice: os botões procuram o evento de cada fato.
    op.create_index("ix_created_events_fact_id", "created_events", ["fact_id"])
    # Índice: a lista sai em ordem de dia.
    op.create_index("ix_created_events_day", "created_events", ["day"])
    # Índice parcial único: um evento ativo (não apagado) por fato. Dois cliques seguidos
    # no botão esbarram aqui, antes de o segundo chegar ao Google.
    op.create_index(
        "uq_created_events_fact_ativo",
        "created_events",
        ["fact_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )


# Desfaz a migração.
def downgrade() -> None:
    # A tabela (os índices saem junto).
    op.drop_table("created_events")
