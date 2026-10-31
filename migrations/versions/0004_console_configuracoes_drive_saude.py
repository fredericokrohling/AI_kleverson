"""console: configurações, pastas do Drive, tags e saúde

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30 20:35:25.464828

Melhorias do console (menu lateral, páginas Configurações e Saúde):
- acrescenta "drive" aos tipos de fonte do bronze (arquivos das pastas monitoradas);
- app_settings: configurações trocadas pelo console (modelo, esforço, idioma);
- drive_folders, drive_items: pastas monitoradas e o espelho da árvore delas;
- tags, drive_item_tags: tags e onde cada uma foi colocada diretamente;
- health_checks: resultado de cada teste de saúde;
- views drive_item_effective_tags e bronze_item_tags: calculam a herança das tags.
Cada coluna aqui espelha uma coluna comentada em app/models.py.
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna e restrições do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar índice, apagar...).
from alembic import op

# Tipos próprios do Postgres (JSONB).
from sqlalchemy.dialects import postgresql

# Identificador desta migração.
revision: str = "0004"
# Migração anterior: esta só roda depois da 0003.
down_revision: str | Sequence[str] | None = "0003"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None

# Herança das tags, calculada na hora da consulta.
# "ancestrais" liga cada item a ele mesmo (profundidade 0), à pasta-mãe (1), à avó (2)...
# subindo pelo parent_drive_id. Depois junta com as tags colocadas em qualquer um deles.
# herdada = true quando a tag só vem de cima; false quando foi colocada no próprio item.
# O limite de 50 níveis impede laço infinito se o Drive devolver uma árvore com ciclo.
_EFFECTIVE_TAGS_VIEW = """
CREATE VIEW drive_item_effective_tags AS
WITH RECURSIVE ancestrais(item_id, ancestral_id, profundidade) AS (
    SELECT id, id, 0 FROM drive_items
    UNION ALL
    SELECT a.item_id, mae.id, a.profundidade + 1
    FROM ancestrais a
    JOIN drive_items filho ON filho.id = a.ancestral_id
    JOIN drive_items mae ON mae.drive_id = filho.parent_drive_id
    WHERE a.profundidade < 50
)
SELECT a.item_id AS drive_item_id,
       t.tag_id,
       bool_and(a.profundidade > 0) AS herdada
FROM ancestrais a
JOIN drive_item_tags t ON t.drive_item_id = a.ancestral_id
GROUP BY a.item_id, t.tag_id
"""

# Tags de cada versão de arquivo do Drive gravada no bronze. A ligação é pelo id do
# Drive guardado no JSON extra; por ser view, uma tag nova vale na hora para todas as
# versões antigas e futuras, sem regravar nada.
_BRONZE_TAGS_VIEW = """
CREATE VIEW bronze_item_tags AS
SELECT b.id AS bronze_item_id,
       e.tag_id,
       tg.name AS tag,
       e.herdada
FROM bronze_items b
JOIN drive_items d ON d.drive_id = b.extra ->> 'drive_id'
JOIN drive_item_effective_tags e ON e.drive_item_id = d.id
JOIN tags tg ON tg.id = e.tag_id
WHERE b.source::text = 'drive'
"""


# Aplica a migração.
def upgrade() -> None:
    """Upgrade schema."""
    # Novo valor no tipo source_kind. O Postgres só deixa usar um valor novo depois do
    # commit; autocommit_block roda este comando fora da transação da migração.
    with op.get_context().autocommit_block():
        # IF NOT EXISTS: rodar de novo não dá erro.
        op.execute("ALTER TYPE source_kind ADD VALUE IF NOT EXISTS 'drive'")

    # Tabela app_settings: uma linha por configuração trocada pelo console.
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    # Tabela drive_folders: as pastas que você mandou monitorar.
    op.create_table(
        "drive_folders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("drive_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=1000), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("drive_id"),
    )
    # Tabela health_checks: um resultado de teste de saúde por linha.
    op.create_table(
        "health_checks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service", sa.String(length=50), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Índice por momento do teste (apagar os antigos, mostrar os recentes).
    op.create_index(
        op.f("ix_health_checks_checked_at"), "health_checks", ["checked_at"], unique=False
    )
    # Índice por serviço (achar o último resultado de cada um).
    op.create_index(op.f("ix_health_checks_service"), "health_checks", ["service"], unique=False)
    # Tabela tags: os nomes das tags, sem repetição.
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    # Tabela drive_items: arquivos e subpastas encontrados nas pastas monitoradas.
    op.create_table(
        "drive_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("drive_id", sa.String(length=200), nullable=False),
        sa.Column("folder_id", sa.Integer(), nullable=False),
        sa.Column("parent_drive_id", sa.String(length=200), nullable=True),
        sa.Column("name", sa.String(length=1000), nullable=False),
        sa.Column("path", sa.Text(), server_default="", nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=True),
        sa.Column("is_folder", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("modified_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.String(length=50), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("web_link", sa.Text(), nullable=True),
        sa.Column("removed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bronze_item_id", sa.BigInteger(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["bronze_item_id"], ["bronze_items.id"]),
        sa.ForeignKeyConstraint(["folder_id"], ["drive_folders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("drive_id"),
    )
    # Índice por pasta monitorada.
    op.create_index(op.f("ix_drive_items_folder_id"), "drive_items", ["folder_id"], unique=False)
    # Índice por pasta-mãe (listar o conteúdo de uma pasta e subir na herança).
    op.create_index(
        op.f("ix_drive_items_parent_drive_id"), "drive_items", ["parent_drive_id"], unique=False
    )
    # Tabela drive_item_tags: tags colocadas diretamente num item (a herança é a view).
    op.create_table(
        "drive_item_tags",
        sa.Column("drive_item_id", sa.BigInteger(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["drive_item_id"], ["drive_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("drive_item_id", "tag_id"),
    )
    # View com as tags efetivas (próprias + herdadas) de cada item do Drive.
    op.execute(_EFFECTIVE_TAGS_VIEW)
    # View com as tags de cada item do bronze que veio do Drive.
    op.execute(_BRONZE_TAGS_VIEW)


# Desfaz a migração (na ordem inversa).
def downgrade() -> None:
    """Downgrade schema."""
    # Views primeiro, porque dependem das tabelas.
    op.execute("DROP VIEW IF EXISTS bronze_item_tags")
    # A view de herança.
    op.execute("DROP VIEW IF EXISTS drive_item_effective_tags")
    # Vínculos item-tag.
    op.drop_table("drive_item_tags")
    # Índice por pasta-mãe.
    op.drop_index(op.f("ix_drive_items_parent_drive_id"), table_name="drive_items")
    # Índice por pasta monitorada.
    op.drop_index(op.f("ix_drive_items_folder_id"), table_name="drive_items")
    # Itens do Drive.
    op.drop_table("drive_items")
    # Tags.
    op.drop_table("tags")
    # Índice por serviço.
    op.drop_index(op.f("ix_health_checks_service"), table_name="health_checks")
    # Índice por momento.
    op.drop_index(op.f("ix_health_checks_checked_at"), table_name="health_checks")
    # Testes de saúde.
    op.drop_table("health_checks")
    # Pastas monitoradas.
    op.drop_table("drive_folders")
    # Configurações do console.
    op.drop_table("app_settings")
    # O valor "drive" fica no tipo source_kind: o Postgres não sabe remover um valor de
    # enum, e um valor sem uso não atrapalha.
