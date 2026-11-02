"""projetos, equipe com liderança, falas por pessoa e tags de e-mail

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02 16:30:00

- projects.tag_id: a tag criada junto com o projeto (mesmo nome);
- stakeholders.aliases e is_leader: outros nomes da pessoa (como aparece nas reuniões) e
  "liderança na empresa";
- project_stakeholders.is_leader: "liderança no projeto";
- utterances: uma linha por fala de uma transcrição, com quem falou e a pessoa
  cadastrada, quando o sistema consegue ligar;
- item_tags: tags de e-mails e transcrições, com a regra que colocou cada uma; uma tag
  tirada à mão fica marcada (removed_at) para a regra não colocar de novo;
- gmail_label_tags: pastas (marcadores) do Gmail ligadas a tags.
Cada coluna aqui espelha uma coluna comentada em app/models.py.
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna e restrições do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema.
from alembic import op

# Tipos próprios do Postgres (ARRAY).
from sqlalchemy.dialects import postgresql

# Identificador desta migração.
revision: str = "0006"
# Migração anterior.
down_revision: str | Sequence[str] | None = "0005"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None


# Aplica a migração.
def upgrade() -> None:
    # Tag do projeto; apagar a tag não apaga o projeto (fica sem tag).
    op.add_column(
        # Tabela.
        "projects",
        # Coluna nova, única (uma tag por projeto).
        sa.Column(
            "tag_id", sa.Integer(), sa.ForeignKey("tags.id", ondelete="SET NULL"), unique=True
        ),
    )
    # Outros nomes da pessoa ("Ana", "Ana S."), usados para reconhecer quem fala.
    op.add_column(
        # Tabela.
        "stakeholders",
        # Lista de textos, vazia por padrão.
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
    )
    # Liderança na empresa.
    op.add_column(
        # Tabela.
        "stakeholders",
        # Sim/não, "não" por padrão.
        sa.Column("is_leader", sa.Boolean(), server_default="false", nullable=False),
    )
    # Liderança no projeto.
    op.add_column(
        # Tabela.
        "project_stakeholders",
        # Sim/não, "não" por padrão.
        sa.Column("is_leader", sa.Boolean(), server_default="false", nullable=False),
    )
    # Tabela utterances: uma fala por linha.
    op.create_table(
        # Nome da tabela.
        "utterances",
        # Número da fala.
        sa.Column("id", sa.BigInteger(), nullable=False),
        # Transcrição; apagar a transcrição apaga as falas.
        sa.Column(
            "bronze_item_id",
            sa.BigInteger(),
            sa.ForeignKey("bronze_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Ordem da fala na reunião (0, 1, 2...).
        sa.Column("position", sa.Integer(), nullable=False),
        # Momento, em ms desde o começo (vazio quando o arquivo não diz).
        sa.Column("start_ms", sa.Integer(), nullable=True),
        # Nome como apareceu na transcrição.
        sa.Column("speaker", sa.String(length=200), nullable=True),
        # Pessoa cadastrada reconhecida (vazio = não reconhecida ou ambígua).
        sa.Column(
            "stakeholder_id",
            sa.Integer(),
            sa.ForeignKey("stakeholders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # O que foi dito.
        sa.Column("text", sa.Text(), nullable=False),
        # Chave.
        sa.PrimaryKeyConstraint("id"),
        # Uma fala por posição em cada transcrição.
        sa.UniqueConstraint("bronze_item_id", "position"),
    )
    # Índice por transcrição (mostrar as falas de uma reunião).
    op.create_index("ix_utterances_bronze_item_id", "utterances", ["bronze_item_id"])
    # Índice por pessoa (tudo o que uma pessoa falou).
    op.create_index("ix_utterances_stakeholder_id", "utterances", ["stakeholder_id"])
    # Tabela item_tags: tags de e-mails e transcrições.
    op.create_table(
        # Nome da tabela.
        "item_tags",
        # E-mail ou transcrição; apagar o item apaga as tags dele.
        sa.Column(
            "bronze_item_id",
            sa.BigInteger(),
            sa.ForeignKey("bronze_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Tag; apagar a tag apaga as linhas.
        sa.Column(
            "tag_id", sa.Integer(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
        ),
        # Regra que colocou (assunto_corpo, marcador_gmail, equipe, manual).
        sa.Column("rule", sa.String(length=30), nullable=False),
        # Quando foi colocada.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Quando você tirou à mão (a linha fica para as regras não recolocarem).
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        # Uma linha por item e tag.
        sa.PrimaryKeyConstraint("bronze_item_id", "tag_id"),
    )
    # Índice por tag (listar o que tem uma tag).
    op.create_index("ix_item_tags_tag_id", "item_tags", ["tag_id"])
    # Tabela gmail_label_tags: pasta do Gmail → tag.
    op.create_table(
        # Nome da tabela.
        "gmail_label_tags",
        # Id da pasta (marcador) no Gmail.
        sa.Column("label_id", sa.String(length=200), nullable=False),
        # Tag que a pasta coloca.
        sa.Column(
            "tag_id", sa.Integer(), sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
        ),
        # Nome da pasta quando foi ligada (só para mostrar).
        sa.Column("label_name", sa.String(length=500), nullable=False),
        # Uma linha por pasta e tag.
        sa.PrimaryKeyConstraint("label_id", "tag_id"),
    )


# Desfaz a migração.
def downgrade() -> None:
    # Apaga as tabelas novas.
    op.drop_table("gmail_label_tags")
    # Tags dos itens.
    op.drop_table("item_tags")
    # Falas.
    op.drop_table("utterances")
    # Colunas novas.
    op.drop_column("project_stakeholders", "is_leader")
    # Liderança na empresa.
    op.drop_column("stakeholders", "is_leader")
    # Outros nomes.
    op.drop_column("stakeholders", "aliases")
    # Tag do projeto.
    op.drop_column("projects", "tag_id")
