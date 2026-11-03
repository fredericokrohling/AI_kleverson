"""classificação de cada item por projeto: método "pasta", motivo, candidatos e revisão

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-03 19:00:00

A tabela item_classifications existe desde a 0001, mas ficou vazia até o bloco 3. Esta
migração acrescenta o que a classificação em cascata (app/classification.py) precisa:
- o método "pasta" (marcador do Gmail ou pasta do Drive ligada ao projeto);
- o motivo da decisão em texto, para você auditar sem abrir o código;
- os projetos que estavam na disputa;
- a marca "a revisar", que alimenta a fila da página Classificação;
- a marca "escolhida por você", que nenhuma regra nem o Claude muda depois.
Cada coluna aqui espelha uma coluna comentada em app/models.py (ItemClassification).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar coluna, criar índice, apagar...).
from alembic import op

# postgresql: tipos que só o Postgres tem (ARRAY).
from sqlalchemy.dialects import postgresql

# Identificador desta migração.
revision: str = "0010"
# Migração anterior: esta só roda depois da 0009.
down_revision: str | Sequence[str] | None = "0009"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None


# Aplica a migração.
def upgrade() -> None:
    # Valor novo na lista fechada de métodos. IF NOT EXISTS: rodar duas vezes não quebra.
    # O Postgres aceita ADD VALUE dentro da transação, desde que o valor novo não seja
    # usado nela mesma (e não é: só o código o usa, depois).
    op.execute("ALTER TYPE classification_method ADD VALUE IF NOT EXISTS 'pasta'")
    # Motivo da decisão (texto livre).
    op.add_column("item_classifications", sa.Column("reason", sa.Text(), nullable=True))
    # Projetos na disputa; "{}" = lista vazia para as linhas que já existirem.
    op.add_column(
        "item_classifications",
        sa.Column(
            "candidates",
            postgresql.ARRAY(sa.Integer()),
            server_default="{}",
            nullable=False,
        ),
    )
    # Marca "a revisar"; as linhas antigas (se houver) ficam fora da fila.
    op.add_column(
        "item_classifications",
        sa.Column("needs_review", sa.Boolean(), server_default="false", nullable=False),
    )
    # Marca "escolhida por você": as regras e o Claude não mexem mais nesta linha.
    op.add_column(
        "item_classifications",
        sa.Column("locked", sa.Boolean(), server_default="false", nullable=False),
    )
    # Índice: a fila "A revisar" filtra por esta coluna.
    op.create_index(
        "ix_item_classifications_needs_review", "item_classifications", ["needs_review"]
    )


# Desfaz a migração.
def downgrade() -> None:
    # Apaga o índice.
    op.drop_index("ix_item_classifications_needs_review", table_name="item_classifications")
    # Apaga as quatro colunas.
    op.drop_column("item_classifications", "locked")
    op.drop_column("item_classifications", "needs_review")
    op.drop_column("item_classifications", "candidates")
    op.drop_column("item_classifications", "reason")
    # Classificações pelo método "pasta" viram "manual" antes de o valor deixar de ser
    # usado: o Postgres não sabe tirar um valor de uma lista fechada (ENUM), então o
    # valor continua existindo no tipo, só sem uso.
    op.execute("UPDATE item_classifications SET method = 'manual' WHERE method = 'pasta'")
