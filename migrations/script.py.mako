"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Lembrete: comente por tabela/operação o que esta migração faz (ver docs/aprendizado/).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna e restrições do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar índice, apagar...).
from alembic import op
${imports if imports else ""}

# Identificador desta migração.
revision: str = ${repr(up_revision)}
# Migração anterior (a que precisa ter sido aplicada antes desta).
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
# Dependências de outras migrações (não usamos).
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


# Aplica a migração.
def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


# Desfaz a migração.
def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
