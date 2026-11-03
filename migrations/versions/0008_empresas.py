"""Empresas (clientes): o nível mais alto dos dados, sem mistura entre elas

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03 15:00:00

- companies: as Empresas. A migração cria a Empresa "Teste aplicacao" e entrega a ela
  todos os dados que já existem (pedido da dona: havia dados e nenhuma Empresa);
- company_id obrigatório (NOT NULL) em projetos, pessoas, tags, pastas e arquivos do
  Drive, conteúdo do bronze, contas Google e rodadas de coleta; opcional nas reuniões ao
  vivo (a Empresa pode ser escolhida durante a reunião) e no diário (ações gerais);
- as regras de "não repete" passam a valer dentro da Empresa (duas Empresas podem ter a
  tag "Portal", ou a mesma pessoa com o mesmo e-mail);
- as duas views de tags do Drive passam a comparar a Empresa ao subir pelas pastas;
- travas no banco (gatilhos) que recusam ligar linhas de Empresas diferentes.
Cada coluna aqui espelha uma coluna comentada em app/models.py.
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna e restrições do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema.
from alembic import op

# Identificador desta migração.
revision: str = "0008"
# Migração anterior.
down_revision: str | Sequence[str] | None = "0007"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None

# Nome da Empresa que recebe os dados que já existem.
SEED_COMPANY = "Teste aplicacao"

# Tabelas em que a Empresa é obrigatória.
REQUIRED = [
    "projects",
    "stakeholders",
    "tags",
    "drive_folders",
    "drive_items",
    "bronze_items",
    "oauth_tokens",
    "ingestion_runs",
]
# Tabelas em que a Empresa é opcional.
OPTIONAL = ["live_meetings", "audit_log"]

# Regras de "não repete": (tabela, nome antigo, colunas antigas, colunas novas).
UNIQUES = [
    ("projects", "projects_name_key", ["name"], ["company_id", "name"]),
    ("projects", "projects_code_key", ["code"], ["company_id", "code"]),
    ("stakeholders", "stakeholders_email_key", ["email"], ["company_id", "email"]),
    ("tags", "tags_name_key", ["name"], ["company_id", "name"]),
    ("drive_folders", "drive_folders_drive_id_key", ["drive_id"], ["company_id", "drive_id"]),
    ("drive_items", "drive_items_drive_id_key", ["drive_id"], ["company_id", "drive_id"]),
    (
        "bronze_items",
        "bronze_items_source_external_id_key",
        ["source", "external_id"],
        ["company_id", "source", "external_id"],
    ),
]

# Herança das tags do Drive (mesma view da migração 0004), agora subindo só pelas pastas
# da mesma Empresa: o mesmo id de pasta pode existir em duas Empresas.
_EFFECTIVE_TAGS_VIEW = """
CREATE VIEW drive_item_effective_tags AS
WITH RECURSIVE ancestrais(item_id, ancestral_id, profundidade) AS (
    SELECT id, id, 0 FROM drive_items
    UNION ALL
    SELECT a.item_id, mae.id, a.profundidade + 1
    FROM ancestrais a
    JOIN drive_items filho ON filho.id = a.ancestral_id
    JOIN drive_items mae
      ON mae.drive_id = filho.parent_drive_id AND mae.company_id = filho.company_id
    WHERE a.profundidade < 50
)
SELECT a.item_id AS drive_item_id,
       t.tag_id,
       bool_and(a.profundidade > 0) AS herdada
FROM ancestrais a
JOIN drive_item_tags t ON t.drive_item_id = a.ancestral_id
GROUP BY a.item_id, t.tag_id
"""

# Tags de cada versão do Drive no bronze, ligando versão e arquivo da mesma Empresa.
_BRONZE_TAGS_VIEW = """
CREATE VIEW bronze_item_tags AS
SELECT b.id AS bronze_item_id,
       e.tag_id,
       tg.name AS tag,
       e.herdada
FROM bronze_items b
JOIN drive_items d ON d.drive_id = b.extra ->> 'drive_id' AND d.company_id = b.company_id
JOIN drive_item_effective_tags e ON e.drive_item_id = d.id
JOIN tags tg ON tg.id = e.tag_id
WHERE b.source::text = 'drive'
"""

# As duas views como estavam (para desfazer a migração).
_OLD_EFFECTIVE_TAGS_VIEW = _EFFECTIVE_TAGS_VIEW.replace(
    " AND mae.company_id = filho.company_id", ""
)
_OLD_BRONZE_TAGS_VIEW = _BRONZE_TAGS_VIEW.replace(" AND d.company_id = b.company_id", "")

# Trava 1: as linhas apontadas por uma linha são todas da mesma Empresa.
# Cada par de argumentos do gatilho é (tabela, coluna): a coluna da linha nova aponta para
# o id daquela tabela, e a Empresa de lá é lida. "self" = a própria coluna é a Empresa.
# Valores vazios (NULL) não contam. Duas Empresas diferentes: erro, e nada é gravado.
_SAME_COMPANY_FUNCTION = """
CREATE FUNCTION empresa_coerente() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    dados jsonb := to_jsonb(NEW);
    esperada integer;
    achada integer;
    i integer := 0;
    tabela text;
    coluna text;
    valor text;
BEGIN
    WHILE i < TG_NARGS LOOP
        tabela := TG_ARGV[i];
        coluna := TG_ARGV[i + 1];
        i := i + 2;
        valor := dados ->> coluna;
        IF valor IS NULL THEN
            CONTINUE;
        END IF;
        IF tabela = 'self' THEN
            achada := valor::integer;
        ELSE
            EXECUTE format('SELECT company_id FROM %I WHERE id = $1', tabela)
                INTO achada USING valor::bigint;
        END IF;
        IF achada IS NULL THEN
            CONTINUE;
        END IF;
        IF esperada IS NULL THEN
            esperada := achada;
        ELSIF esperada <> achada THEN
            RAISE EXCEPTION 'Dados de Empresas diferentes não podem ser ligados (%.%).',
                TG_TABLE_NAME, coluna USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NULL;
END
$$
"""

# Gatilhos da trava 1: tabela → pares (tabela apontada, coluna).
_SAME_COMPANY = {
    "projects": ["self", "company_id", "tags", "tag_id"],
    "project_stakeholders": ["projects", "project_id", "stakeholders", "stakeholder_id"],
    "item_tags": ["bronze_items", "bronze_item_id", "tags", "tag_id"],
    "drive_item_tags": ["drive_items", "drive_item_id", "tags", "tag_id"],
    "drive_items": [
        "self",
        "company_id",
        "drive_folders",
        "folder_id",
        "bronze_items",
        "bronze_item_id",
    ],
    "utterances": ["bronze_items", "bronze_item_id", "stakeholders", "stakeholder_id"],
    "meeting_participants": ["live_meetings", "meeting_id", "stakeholders", "stakeholder_id"],
    "live_meetings": ["self", "company_id", "bronze_items", "bronze_item_id"],
    "item_classifications": ["bronze_items", "bronze_item_id", "projects", "project_id"],
    "facts": [
        "projects",
        "project_id",
        "stakeholders",
        "owner_id",
        "bronze_items",
        "source_item_id",
    ],
}

# Trava 2: corrigir a Empresa de um conteúdo (bronze) ou de uma reunião só vale se nada
# ligado a ele ficar na Empresa antiga (tags, pessoas, projetos, participantes).
_MOVED_FUNCTION = """
CREATE FUNCTION empresa_corrigida() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.company_id IS NOT DISTINCT FROM OLD.company_id THEN
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'bronze_items' AND (
        EXISTS (SELECT 1 FROM item_tags x JOIN tags t ON t.id = x.tag_id
                WHERE x.bronze_item_id = NEW.id AND t.company_id <> NEW.company_id)
        OR EXISTS (SELECT 1 FROM utterances u JOIN stakeholders s ON s.id = u.stakeholder_id
                   WHERE u.bronze_item_id = NEW.id AND s.company_id <> NEW.company_id)
        OR EXISTS (SELECT 1 FROM item_classifications c JOIN projects p ON p.id = c.project_id
                   WHERE c.bronze_item_id = NEW.id AND p.company_id <> NEW.company_id)
        OR EXISTS (SELECT 1 FROM facts f WHERE f.source_item_id = NEW.id)
        OR EXISTS (SELECT 1 FROM drive_items d
                   WHERE d.bronze_item_id = NEW.id AND d.company_id <> NEW.company_id)
        OR EXISTS (SELECT 1 FROM live_meetings m
                   WHERE m.bronze_item_id = NEW.id AND m.company_id IS DISTINCT FROM NEW.company_id)
    ) THEN
        RAISE EXCEPTION 'O conteúdo % mudou de Empresa, mas algo ligado a ele ficou na antiga.',
            NEW.id USING ERRCODE = 'check_violation';
    END IF;
    IF TG_TABLE_NAME = 'live_meetings' AND EXISTS (
        SELECT 1 FROM meeting_participants p JOIN stakeholders s ON s.id = p.stakeholder_id
        WHERE p.meeting_id = NEW.id AND s.company_id <> NEW.company_id
    ) THEN
        RAISE EXCEPTION 'A reunião % mudou de Empresa, mas há participantes ligados a pessoas da antiga.',
            NEW.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$
"""

# Trava 3: cadastros e contas não mudam de Empresa (pessoa, tag, projeto, pasta, conta).
# Para "mudar", cadastra-se de novo na outra Empresa; assim o que já foi ligado não muda
# de dono sem ninguém ver.
_FIXED_FUNCTION = """
CREATE FUNCTION empresa_fixa() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
        RAISE EXCEPTION 'Um registro de % não muda de Empresa.', TG_TABLE_NAME
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$
"""
# Tabelas da trava 3.
_FIXED = ["projects", "stakeholders", "tags", "drive_folders", "drive_items", "oauth_tokens"]


# Aplica a migração.
def upgrade() -> None:
    # Tabela das Empresas.
    op.create_table(
        # Nome.
        "companies",
        # Identificador.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Nome da Empresa.
        sa.Column("name", sa.String(200), nullable=False),
        # Idioma padrão da transcrição (vazio = o geral).
        sa.Column("transcription_language", sa.String(20), nullable=True),
        # Quando foi cadastrada.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Nome único sem diferenciar maiúsculas.
    op.execute("CREATE UNIQUE INDEX uq_companies_name_lower ON companies (lower(name))")
    # A Empresa que recebe os dados atuais; RETURNING devolve o id gerado.
    seed = (
        op.get_bind()
        .execute(
            sa.text("INSERT INTO companies (name) VALUES (:n) RETURNING id"), {"n": SEED_COMPANY}
        )
        .scalar()
    )

    # As views leem drive_items; saem antes de mexer nas colunas e voltam no fim.
    op.execute("DROP VIEW IF EXISTS bronze_item_tags")
    # A outra view.
    op.execute("DROP VIEW IF EXISTS drive_item_effective_tags")

    # Empresa nas tabelas: coluna, dados atuais para a Empresa semente, índice.
    for table in REQUIRED + OPTIONAL:
        # Coluna com chave estrangeira (começa aceitando vazio, para preencher).
        op.add_column(
            table,
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
        )
        # Tudo o que existe vai para a Empresa semente.
        op.execute(sa.text(f"UPDATE {table} SET company_id = :c").bindparams(c=seed))
        # Índice para filtrar por Empresa.
        op.create_index(f"ix_{table}_company_id", table, ["company_id"])
    # Nas tabelas principais, a Empresa passa a ser obrigatória.
    for table in REQUIRED:
        # NOT NULL: o banco recusa uma linha sem Empresa.
        op.alter_column(table, "company_id", existing_type=sa.Integer(), nullable=False)

    # "Não repete" passa a valer dentro da Empresa.
    for table, old_name, _, columns in UNIQUES:
        # Tira a regra antiga (valia no sistema todo).
        op.drop_constraint(old_name, table, type_="unique")
        # Põe a nova, com a Empresa na frente.
        op.create_unique_constraint(f"{table}_{'_'.join(columns)}_key", table, columns)
    # Uma conta Google por Empresa.
    op.create_unique_constraint(
        "oauth_tokens_provider_company_id_key", "oauth_tokens", ["provider", "company_id"]
    )

    # Views de volta, agora comparando a Empresa.
    op.execute(_EFFECTIVE_TAGS_VIEW)
    # A outra.
    op.execute(_BRONZE_TAGS_VIEW)

    # Funções das travas.
    op.execute(_SAME_COMPANY_FUNCTION)
    # Correção de Empresa.
    op.execute(_MOVED_FUNCTION)
    # Cadastros fixos.
    op.execute(_FIXED_FUNCTION)
    # Trava 1 em cada tabela. CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED: a
    # conferência acontece no commit, depois de todas as mudanças da transação. Assim
    # dá para corrigir a Empresa de um conteúdo e das coisas ligadas a ele em vários
    # passos, e o banco só confere o resultado final.
    for table, args in _SAME_COMPANY.items():
        # Argumentos entre aspas simples, separados por vírgula.
        quoted = ", ".join(f"'{a}'" for a in args)
        # Um gatilho por tabela.
        op.execute(
            f"CREATE CONSTRAINT TRIGGER empresa_coerente AFTER INSERT OR UPDATE ON {table} "
            f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION empresa_coerente({quoted})"
        )
    # Trava 2 (também no commit).
    for table in ("bronze_items", "live_meetings"):
        # Só quando a Empresa muda.
        op.execute(
            f"CREATE CONSTRAINT TRIGGER empresa_corrigida AFTER UPDATE OF company_id ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION empresa_corrigida()"
        )
    # Trava 3 (na hora: não há passo intermediário legítimo).
    for table in _FIXED:
        # BEFORE UPDATE: recusa antes de gravar.
        op.execute(
            f"CREATE TRIGGER empresa_fixa BEFORE UPDATE OF company_id ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION empresa_fixa()"
        )


# Desfaz a migração. Para se, ao voltar, houver nomes repetidos entre Empresas.
def downgrade() -> None:
    # Travas.
    for table in _FIXED:
        # Trava 3.
        op.execute(f"DROP TRIGGER IF EXISTS empresa_fixa ON {table}")
    # Trava 2.
    for table in ("bronze_items", "live_meetings"):
        # Gatilho.
        op.execute(f"DROP TRIGGER IF EXISTS empresa_corrigida ON {table}")
    # Trava 1.
    for table in _SAME_COMPANY:
        # Gatilho.
        op.execute(f"DROP TRIGGER IF EXISTS empresa_coerente ON {table}")
    # Funções.
    for name in ("empresa_fixa", "empresa_corrigida", "empresa_coerente"):
        # Uma por vez.
        op.execute(f"DROP FUNCTION IF EXISTS {name}()")
    # Views saem para tirar a coluna.
    op.execute("DROP VIEW IF EXISTS bronze_item_tags")
    # A outra.
    op.execute("DROP VIEW IF EXISTS drive_item_effective_tags")
    # Uma conta por Empresa.
    op.drop_constraint("oauth_tokens_provider_company_id_key", "oauth_tokens", type_="unique")
    # Regras de "não repete" como eram (falha se houver repetidos entre Empresas).
    for table, old_name, old_columns, columns in UNIQUES:
        # Tira a da Empresa.
        op.drop_constraint(f"{table}_{'_'.join(columns)}_key", table, type_="unique")
        # Volta a do sistema todo.
        op.create_unique_constraint(old_name, table, old_columns)
    # Colunas.
    for table in REQUIRED + OPTIONAL:
        # Índice.
        op.drop_index(f"ix_{table}_company_id", table_name=table)
        # Coluna (a chave estrangeira sai junto).
        op.drop_column(table, "company_id")
    # Views antigas.
    op.execute(_OLD_EFFECTIVE_TAGS_VIEW)
    # A outra.
    op.execute(_OLD_BRONZE_TAGS_VIEW)
    # Tabela das Empresas (o índice sai junto).
    op.drop_table("companies")
