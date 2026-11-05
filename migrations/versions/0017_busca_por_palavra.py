"""busca por palavra: índice de texto em português, sem acento, nos itens e nos anexos

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-05 16:00:00

Primeira peça do bloco 7 (conversa com os dados pelo Claude, via MCP). A ferramenta
"buscar" procura palavras em e-mails, transcrições, arquivos do Drive e anexos. Para isso
cada item e cada anexo ganha uma coluna "search_vector": a lista das palavras do texto já
reduzidas à raiz ("reuniões" e "reunião" viram a mesma palavra) e sem acento ("reuniao"
também acha). O Postgres calcula a coluna sozinho a cada gravação (coluna gerada), e um
índice GIN deixa a busca rápida mesmo com dezenas de milhares de itens.
Cada coluna aqui espelha uma coluna comentada em app/models.py (BronzeItem e Attachment).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar coluna, criar índice...).
from alembic import op

# TSVECTOR: o tipo do Postgres que guarda as palavras de um texto, prontas para a busca.
from sqlalchemy.dialects.postgresql import TSVECTOR

# Identificador desta migração.
revision: str = "0017"
# Migração anterior: esta só roda depois da 0016.
down_revision: str | Sequence[str] | None = "0016"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None

# Texto indexado de cada item: assunto, autor e corpo.
# left(..., 100000): só os primeiros 100 mil caracteres entram no índice. O Postgres
# recusa uma lista de palavras acima de 1 MB, e a recusa travaria a gravação do item;
# com o corte, um arquivo enorme do Drive é gravado e só o fim dele fica fora da busca.
_ITEM_TEXT = (
    "left(coalesce(subject, '') || ' ' || coalesce(author, '') || ' ' || body_text, 100000)"
)
# Anexo: o nome do arquivo e o texto extraído (PDF, planilha, imagem lida pelo Claude).
_ATTACHMENT_TEXT = "left(filename || ' ' || coalesce(extracted_text, ''), 100000)"


# Fórmula da coluna gerada: as palavras pelas duas configurações, juntas (||).
# Por que duas: o redutor de raiz do português trata o acento antes de cortar a palavra
# ("aprovação" vira "aprov"), e por isso não pode receber o texto já sem acento. A
# "portuguese" acerta quando o texto e a pergunta têm acento; a "kleverson_pt" (sem
# acento) acerta quando um dos dois não tem. Com as duas no índice, a busca acha nos
# dois casos. Igual à fórmula de app/models.py (o Alembic compara).
def formula(text_sql: str) -> str:
    # Uma lista de palavras por configuração, unidas.
    return (
        f"to_tsvector('portuguese'::regconfig, {text_sql}) || "
        f"to_tsvector('kleverson_pt'::regconfig, {text_sql})"
    )


# Fórmula dos itens.
ITEM_FORMULA = formula(_ITEM_TEXT)
# Fórmula dos anexos.
ATTACHMENT_FORMULA = formula(_ATTACHMENT_TEXT)


# Aplica a migração.
def upgrade() -> None:
    # unaccent: extensão que vem com o Postgres e tira os acentos ("ç" vira "c").
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    # Configuração de busca própria: cópia da "portuguese" (raiz das palavras e palavras
    # vazias como "de" e "para") com um passo a mais, o de tirar acento. Assim "reuniao"
    # e "reunião" dão o mesmo resultado, e quem digita sem acento acha o que procura.
    op.execute("CREATE TEXT SEARCH CONFIGURATION kleverson_pt (COPY = portuguese)")
    # As palavras (word, hword...) passam primeiro pelo unaccent e depois pela raiz.
    op.execute(
        "ALTER TEXT SEARCH CONFIGURATION kleverson_pt "
        "ALTER MAPPING FOR hword, hword_part, word WITH unaccent, portuguese_stem"
    )
    # Coluna gerada nos itens: o Postgres calcula ao gravar; ninguém escreve nela.
    # Adicionar uma coluna gerada reescreve a tabela uma vez (segundos, com o volume de
    # hoje).
    op.add_column(
        "bronze_items",
        sa.Column("search_vector", TSVECTOR(), sa.Computed(ITEM_FORMULA, persisted=True)),
    )
    # Índice GIN: o tipo de índice feito para "quais linhas têm esta palavra".
    op.create_index(
        "ix_bronze_items_search_vector",
        "bronze_items",
        ["search_vector"],
        postgresql_using="gin",
    )
    # Mesma coisa nos anexos.
    op.add_column(
        "attachments",
        sa.Column("search_vector", TSVECTOR(), sa.Computed(ATTACHMENT_FORMULA, persisted=True)),
    )
    # Índice dos anexos.
    op.create_index(
        "ix_attachments_search_vector",
        "attachments",
        ["search_vector"],
        postgresql_using="gin",
    )


# Desfaz a migração.
def downgrade() -> None:
    # Índices (antes das colunas).
    op.drop_index("ix_attachments_search_vector", table_name="attachments")
    # O dos itens.
    op.drop_index("ix_bronze_items_search_vector", table_name="bronze_items")
    # Colunas.
    op.drop_column("attachments", "search_vector")
    # A dos itens.
    op.drop_column("bronze_items", "search_vector")
    # A configuração de busca.
    op.execute("DROP TEXT SEARCH CONFIGURATION kleverson_pt")
    # A extensão fica: outra coisa pode passar a usar e ela não ocupa espaço.
