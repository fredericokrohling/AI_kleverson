"""extração de fatos: quem disse, responsável como escrito e o registro de cada extração

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-04 01:00:00

A tabela facts existe desde a 0001 (camada silver), mas ficou vazia até aqui. Esta
migração acrescenta o que a extração (app/facts.py) precisa:
- facts.said_by_id: a Pessoa que disse ou escreveu o trecho (o remetente do e-mail, ou
  quem falou na reunião). É a base das regras de alerta sobre o que lideranças pediram
  ou decidiram, que vêm depois: com esta coluna, a pergunta vira uma consulta SQL;
- facts.said_by_text: o nome como apareceu ("Falante 2", "ana@cliente.com"), para quando
  a pessoa não está cadastrada;
- facts.owner_text: o responsável como está escrito no texto ("o João"), mesmo quando
  não dá para ligar a uma Pessoa cadastrada (owner_id fica vazio nesse caso);
- fact_extractions: uma linha por item já lido pelo Claude, com ou sem fatos. Sem ela,
  um e-mail sem fato nenhum ("obrigada!") seria mandado ao Claude de novo a cada rodada.
A trava de Empresa da 0008 em facts passa a conferir também said_by_id.
Cada coluna aqui espelha uma coluna comentada em app/models.py (Fact, FactExtraction).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar coluna, rodar SQL...).
from alembic import op

# Identificador desta migração.
revision: str = "0012"
# Migração anterior: esta só roda depois da 0011.
down_revision: str | Sequence[str] | None = "0011"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None

# Trava 1 da 0008 em facts, como era (o downgrade volta para ela).
_FACTS_BEFORE = [
    "projects",
    "project_id",
    "stakeholders",
    "owner_id",
    "bronze_items",
    "source_item_id",
]
# A mesma trava, agora com quem disse (a função aceita a mesma tabela duas vezes).
_FACTS_AFTER = [*_FACTS_BEFORE, "stakeholders", "said_by_id"]


# Gatilho da trava 1 em facts, com os pares dados.
def _same_company(args: list[str]) -> None:
    # Argumentos entre aspas simples, separados por vírgula.
    quoted = ", ".join(f"'{a}'" for a in args)
    # Conferência no commit (DEFERRABLE INITIALLY DEFERRED), como na 0008.
    op.execute(
        "CREATE CONSTRAINT TRIGGER empresa_coerente AFTER INSERT OR UPDATE ON facts "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION empresa_coerente({quoted})"
    )


# Aplica a migração.
def upgrade() -> None:
    # Quem disse: a Pessoa. SET NULL: apagar a Pessoa não apaga o fato.
    op.add_column(
        "facts",
        sa.Column(
            "said_by_id",
            sa.Integer(),
            sa.ForeignKey("stakeholders.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Índice: "fatos ditos por lideranças" filtra por esta coluna.
    op.create_index("ix_facts_said_by_id", "facts", ["said_by_id"])
    # Quem disse, como apareceu.
    op.add_column("facts", sa.Column("said_by_text", sa.String(200), nullable=True))
    # Responsável como está escrito.
    op.add_column("facts", sa.Column("owner_text", sa.String(200), nullable=True))

    # Registro de cada extração.
    op.create_table(
        # Nome.
        "fact_extractions",
        # O item lido; é a chave (um item, um registro). Apagou o item, apaga o registro.
        sa.Column(
            "bronze_item_id",
            sa.BigInteger(),
            sa.ForeignKey("bronze_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Quando foi lido.
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Resultado: ok, sem_texto, recusa ou ilegivel.
        sa.Column("status", sa.String(20), nullable=False),
        # Modelo que leu (vazio quando não houve chamada).
        sa.Column("model", sa.String(100), nullable=True),
        # Quantos fatos o Claude devolveu.
        sa.Column("facts_found", sa.Integer(), server_default="0", nullable=False),
        # Quantos tiveram o trecho conferido pelo código.
        sa.Column("facts_verified", sa.Integer(), server_default="0", nullable=False),
        # True quando o texto foi cortado no limite (FACTS_MAX_CHARS).
        sa.Column("truncated", sa.Boolean(), server_default="false", nullable=False),
        # Observação (motivo da recusa, erro de leitura da resposta).
        sa.Column("note", sa.Text(), nullable=True),
    )

    # Trava de Empresa em facts: troca pela versão com quem disse.
    op.execute("DROP TRIGGER empresa_coerente ON facts")
    # Cria a nova.
    _same_company(_FACTS_AFTER)


# Desfaz a migração.
def downgrade() -> None:
    # Trava de volta à da 0008.
    op.execute("DROP TRIGGER empresa_coerente ON facts")
    # Recria a antiga.
    _same_company(_FACTS_BEFORE)
    # Registro das extrações.
    op.drop_table("fact_extractions")
    # Colunas novas de facts.
    op.drop_column("facts", "owner_text")
    # Quem disse, como apareceu.
    op.drop_column("facts", "said_by_text")
    # Índice de quem disse.
    op.drop_index("ix_facts_said_by_id", table_name="facts")
    # Quem disse.
    op.drop_column("facts", "said_by_id")
