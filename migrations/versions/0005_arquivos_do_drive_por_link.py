"""arquivos do Drive citados por link nos e-mails

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31 02:10:00

Os arquivos do Drive citados por link num e-mail passam a ser lidos, mesmo fora das
pastas monitoradas. Eles entram na mesma tabela drive_items, sem pasta monitorada: por
isso a coluna folder_id passa a aceitar vazio (vazio = chegou por link).

Também acrescenta dismissed_at em attachments e drive_items: quando você dispensa um
aviso da lista "Arquivos sem texto" (por exemplo, depois de exportar à mão um diagrama
do Lucid para a pasta), a linha sai da lista, mas o registro continua.
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema.
from alembic import op

# Identificador desta migração.
revision: str = "0005"
# Migração anterior.
down_revision: str | Sequence[str] | None = "0004"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None


# Aplica a migração.
def upgrade() -> None:
    # folder_id passa a aceitar vazio: arquivo que chegou por link, sem pasta monitorada.
    op.alter_column("drive_items", "folder_id", existing_type=sa.Integer(), nullable=True)
    # Quando o aviso do anexo foi dispensado; vazio = aparece na lista.
    op.add_column("attachments", sa.Column("dismissed_at", sa.DateTime(timezone=True)))
    # O mesmo para os arquivos do Drive.
    op.add_column("drive_items", sa.Column("dismissed_at", sa.DateTime(timezone=True)))


# Desfaz a migração.
def downgrade() -> None:
    # Remove as colunas de aviso dispensado.
    op.drop_column("drive_items", "dismissed_at")
    # Nos anexos também.
    op.drop_column("attachments", "dismissed_at")
    # Apaga os itens que chegaram por link (sem pasta), senão a coluna não volta a ser obrigatória.
    op.execute("DELETE FROM drive_items WHERE folder_id IS NULL")
    # Volta a exigir a pasta.
    op.alter_column("drive_items", "folder_id", existing_type=sa.Integer(), nullable=False)
