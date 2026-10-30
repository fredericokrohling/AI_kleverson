"""coleta de e-mail

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30 18:53:03.560040

Bloco 2 (e-mail para bronze):
- cria a tabela ingestion_runs, com uma linha por rodada do coletor do Gmail;
- acrescenta à tabela attachments a origem do arquivo (anexo ou link do Drive), o id na
  origem, o tamanho e a impressão digital (SHA-256).
Cada coluna aqui espelha uma coluna comentada em app/models.py.
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna e restrições do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar índice, apagar...).
from alembic import op

# ENUM do Postgres, que aceita reaproveitar um tipo já criado.
from sqlalchemy.dialects.postgresql import ENUM

# Identificador desta migração.
revision: str = "0003"
# Migração anterior: esta só roda depois da 0002.
down_revision: str | Sequence[str] | None = "0002"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None


# Aplica a migração.
def upgrade() -> None:
    """Upgrade schema."""
    # Tabela ingestion_runs: uma rodada do coletor por linha (janela, contagens, erros).
    # O tipo source_kind já existe desde a 0001; create_type=False evita criá-lo de novo.
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source",
            ENUM(
                "gmail",
                "calendar",
                "transcricao_reuniao",
                "google_chat",
                "manual",
                name="source_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("account_email", sa.String(length=320), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="em_andamento", nullable=False),
        sa.Column("messages_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("messages_new", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attachments_saved", sa.Integer(), server_default="0", nullable=False),
        sa.Column("extraction_errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message_errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Índice por fonte: achar a última rodada do Gmail fica rápido.
    op.create_index(op.f("ix_ingestion_runs_source"), "ingestion_runs", ["source"], unique=False)
    # attachments.origin: "anexo" ou "link_drive"; linhas antigas ficam como "anexo".
    op.add_column(
        "attachments",
        sa.Column("origin", sa.String(length=20), server_default="anexo", nullable=False),
    )
    # attachments.external_ref: id do arquivo no Drive, quando veio por link.
    op.add_column("attachments", sa.Column("external_ref", sa.String(length=500), nullable=True))
    # attachments.size_bytes: tamanho do original.
    op.add_column("attachments", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    # attachments.content_sha256: impressão digital do original.
    op.add_column("attachments", sa.Column("content_sha256", sa.String(length=64), nullable=True))
    # Índice pela impressão digital: achar o mesmo arquivo em vários e-mails fica rápido.
    op.create_index(
        op.f("ix_attachments_content_sha256"), "attachments", ["content_sha256"], unique=False
    )


# Desfaz a migração, na ordem inversa.
def downgrade() -> None:
    """Downgrade schema."""
    # Remove o índice da impressão digital.
    op.drop_index(op.f("ix_attachments_content_sha256"), table_name="attachments")
    # Remove as quatro colunas novas de attachments.
    op.drop_column("attachments", "content_sha256")
    op.drop_column("attachments", "size_bytes")
    op.drop_column("attachments", "external_ref")
    op.drop_column("attachments", "origin")
    # Remove o índice e a tabela de rodadas (o tipo source_kind continua, é da 0001).
    op.drop_index(op.f("ix_ingestion_runs_source"), table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
