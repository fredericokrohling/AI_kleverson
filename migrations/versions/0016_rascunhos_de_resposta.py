"""rascunhos de resposta: os textos que o Claude escreveu e os e-mails dispensados

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-05 14:00:00

Terceira peça do bloco 5 (ações). Na página Rascunhos, o botão "Rascunhar resposta" pede
ao Claude um rascunho de resposta para um e-mail; você copia e envia pelo Gmail (o
assistente não grava no Gmail). A tabela email_drafts guarda cada rascunho, com o custo e
o que o código não conseguiu conferir. A tabela reply_dismissals guarda os e-mails que
você tirou da lista "Para responder".
Cada coluna aqui espelha uma coluna comentada em app/models.py (EmailDraft e
ReplyDismissal).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar índice...).
from alembic import op

# JSONB: tipo JSON do Postgres (as listas de lacunas e de números a conferir).
from sqlalchemy.dialects.postgresql import JSONB

# Identificador desta migração.
revision: str = "0016"
# Migração anterior: esta só roda depois da 0015.
down_revision: str | Sequence[str] | None = "0015"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None

# Trava de Empresa (função empresa_coerente, da 0008): a linha e o e-mail apontado são da
# mesma Empresa. Um rascunho nunca fica ligado a um e-mail de outro cliente.
_SAME_COMPANY = (
    "CREATE CONSTRAINT TRIGGER empresa_coerente AFTER INSERT OR UPDATE ON {table} "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
    "EXECUTE FUNCTION empresa_coerente('self', 'company_id', 'bronze_items', 'bronze_item_id')"
)


# Aplica a migração.
def upgrade() -> None:
    # Tabela dos rascunhos.
    op.create_table(
        # Nome.
        "email_drafts",
        # Identificador.
        sa.Column("id", sa.BigInteger(), primary_key=True),
        # Empresa (obrigatória); sem ondelete, como nas outras tabelas.
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        # E-mail respondido; o bronze nunca é apagado, mas se for, o rascunho vai junto.
        sa.Column(
            "bronze_item_id",
            sa.BigInteger(),
            sa.ForeignKey("bronze_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # O que você pediu.
        sa.Column("instructions", sa.Text(), nullable=True),
        # Situação (ok, recusa, ilegivel).
        sa.Column("status", sa.String(20), nullable=False),
        # Texto do rascunho.
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        # Lacunas marcadas entre colchetes.
        sa.Column("gaps", JSONB(), server_default="[]", nullable=False),
        # Números e datas não achados nas mensagens.
        sa.Column("unverified", JSONB(), server_default="[]", nullable=False),
        # Modelo que escreveu.
        sa.Column("model", sa.String(100), nullable=True),
        # Custo.
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        # Quem pediu.
        sa.Column("created_by", sa.String(100), nullable=False),
        # Quando nasceu.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Índice: a página filtra pela Empresa.
    op.create_index("ix_email_drafts_company_id", "email_drafts", ["company_id"])
    # Índice: a página do e-mail lista os rascunhos dele.
    op.create_index("ix_email_drafts_bronze_item_id", "email_drafts", ["bronze_item_id"])
    # Trava de Empresa.
    op.execute(_SAME_COMPANY.format(table="email_drafts"))

    # Tabela dos e-mails dispensados da lista "Para responder".
    op.create_table(
        # Nome.
        "reply_dismissals",
        # O e-mail (chave primária: um registro por e-mail).
        sa.Column(
            "bronze_item_id",
            sa.BigInteger(),
            sa.ForeignKey("bronze_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Empresa.
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        # Quem dispensou.
        sa.Column("dismissed_by", sa.String(100), nullable=False),
        # Quando.
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Índice: a lista filtra pela Empresa.
    op.create_index("ix_reply_dismissals_company_id", "reply_dismissals", ["company_id"])
    # Trava de Empresa.
    op.execute(_SAME_COMPANY.format(table="reply_dismissals"))


# Desfaz a migração.
def downgrade() -> None:
    # As travas.
    op.execute("DROP TRIGGER empresa_coerente ON reply_dismissals")
    # A dos rascunhos.
    op.execute("DROP TRIGGER empresa_coerente ON email_drafts")
    # As tabelas (os índices saem junto).
    op.drop_table("reply_dismissals")
    # Rascunhos.
    op.drop_table("email_drafts")
