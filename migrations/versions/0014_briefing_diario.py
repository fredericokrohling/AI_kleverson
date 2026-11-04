"""briefing diário: uma foto por Empresa por dia do que precisa de você

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-04 12:00:00

Primeira peça do bloco 5 (ações). O coletor monta, de manhã, o briefing de cada Empresa:
a agenda do dia, os alertas novos, os prazos, o que mudou por projeto desde o último
briefing que você abriu e as pendências do sistema. O conteúdo é guardado como uma foto
(JSON), porque a janela de "o que mudou" precisa ficar fixa e a agenda vem do Google,
que pode estar fora do ar quando você abre a página. Tudo é montado por SQL, sem IA.
Cada coluna aqui espelha uma coluna comentada em app/models.py (Briefing).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar índice...).
from alembic import op

# JSONB: coluna de JSON indexável do Postgres (o conteúdo do briefing).
from sqlalchemy.dialects.postgresql import JSONB

# Identificador desta migração.
revision: str = "0014"
# Migração anterior: esta só roda depois da 0013.
down_revision: str | Sequence[str] | None = "0013"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None


# Aplica a migração.
def upgrade() -> None:
    # Tabela dos briefings.
    op.create_table(
        # Nome.
        "briefings",
        # Identificador.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Empresa (obrigatória). Sem ondelete: o banco não deixa apagar uma Empresa com
        # dados, como nas outras tabelas.
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        # Dia do briefing, no fuso do console.
        sa.Column("day", sa.Date(), nullable=False),
        # Quando o conteúdo foi montado.
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        # Início da janela de "o que mudou".
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        # Conteúdo das seções.
        sa.Column("content", JSONB(), server_default="{}", nullable=False),
        # agendado ou manual.
        sa.Column("trigger", sa.String(20), server_default="agendado", nullable=False),
        # Quando você abriu pela primeira vez.
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True),
        # Quando a linha nasceu.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Um por Empresa por dia.
        sa.UniqueConstraint("company_id", "day"),
    )
    # Índice: a página e o Início filtram pela Empresa.
    op.create_index("ix_briefings_company_id", "briefings", ["company_id"])
    # Índice: a página e o menu procuram o briefing de um dia.
    op.create_index("ix_briefings_day", "briefings", ["day"])


# Desfaz a migração.
def downgrade() -> None:
    # A tabela (os índices saem junto).
    op.drop_table("briefings")
