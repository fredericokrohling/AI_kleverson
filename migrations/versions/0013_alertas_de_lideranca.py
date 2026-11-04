"""alertas de liderança: regras editáveis e os alertas que elas disparam

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-04 03:00:00

Os fatos da 0012 já sabem quem disse cada coisa (facts.said_by_id), e as Pessoas e a
equipe sabem quem é liderança (stakeholders.is_leader na empresa e
project_stakeholders.is_leader no projeto). Esta migração acrescenta o que falta para
avisar a dona do que as lideranças pediram, decidiram ou apontaram:
- alert_rules: as regras, que são filtros (tipos de fato, que liderança, condição do
  responsável, prazo em até N dias, peso). Ela edita na página Alertas;
- alerts: um alerta por par (regra, fato), com a situação (novo, visto, resolvido,
  descartado). O par único impede que o coletor, que roda a cada 5 minutos, repita o
  mesmo alerta;
- quatro regras-padrão, valendo para todas as Empresas, para a página não nascer vazia;
- a trava de Empresa da 0008 também em alert_rules e alerts (o alerta e a regra da
  mesma Empresa; regra sem Empresa vale para todas e não é conferida).
Cada coluna aqui espelha uma coluna comentada em app/models.py (AlertRule, Alert).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar índice, rodar SQL...).
from alembic import op

# ARRAY: coluna de lista (os tipos de fato da regra).
from sqlalchemy.dialects.postgresql import ARRAY

# Identificador desta migração.
revision: str = "0013"
# Migração anterior: esta só roda depois da 0012.
down_revision: str | Sequence[str] | None = "0012"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None

# Regras-padrão: (nome, tipos, liderança, responsável, prazo em dias, peso).
# Por que estas quatro: são as perguntas do requisito da dona ("o que lideranças pediram
# ou decidiram") mais as duas que um gerente de projetos faz em seguida (o que elas
# apontaram como risco e o que delas vence logo). Ela liga, desliga e edita no console.
DEFAULT_RULES = [
    ("Pedido de liderança", ["tarefa"], "qualquer", "qualquer", None, "alta"),
    ("Decisão de liderança", ["decisao"], "qualquer", "qualquer", None, "media"),
    (
        "Risco ou problema apontado por liderança",
        ["risco", "problema"],
        "qualquer",
        "qualquer",
        None,
        "media",
    ),
    (
        "Prazo de liderança em até 3 dias",
        ["tarefa", "prazo", "dependencia"],
        "qualquer",
        "qualquer",
        3,
        "alta",
    ),
]


# Aplica a migração.
def upgrade() -> None:
    # Tabela das regras.
    rules = op.create_table(
        # Nome.
        "alert_rules",
        # Identificador.
        sa.Column("id", sa.Integer(), primary_key=True),
        # Empresa (vazia = todas as Empresas).
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=True),
        # Nome mostrado no alerta.
        sa.Column("name", sa.String(120), nullable=False),
        # Ligada ou desligada.
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        # Tipos de fato que contam ("{}" = todos).
        sa.Column("fact_kinds", ARRAY(sa.Text()), server_default="{}", nullable=False),
        # Que liderança: qualquer, empresa ou projeto.
        sa.Column("leadership", sa.String(20), server_default="qualquer", nullable=False),
        # Condição do responsável: qualquer, sem_responsavel ou eu.
        sa.Column("owner_rule", sa.String(20), server_default="qualquer", nullable=False),
        # Prazo em até N dias (vazio = o prazo não importa).
        sa.Column("due_within_days", sa.Integer(), nullable=True),
        # Peso: alta, media ou baixa.
        sa.Column("severity", sa.String(10), server_default="media", nullable=False),
        # Quando foi criada.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Última alteração.
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Índice: a página e o coletor filtram as regras pela Empresa.
    op.create_index("ix_alert_rules_company_id", "alert_rules", ["company_id"])

    # Tabela dos alertas.
    op.create_table(
        # Nome.
        "alerts",
        # Identificador (BigInteger: muitos alertas ao longo dos anos).
        sa.Column("id", sa.BigInteger(), primary_key=True),
        # Empresa do item de onde o fato saiu.
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        # Regra que disparou; apagar a regra apaga os alertas dela.
        sa.Column(
            "rule_id",
            sa.Integer(),
            sa.ForeignKey("alert_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Fato que disparou; apagar o fato (Extrair de novo) apaga o alerta.
        sa.Column(
            "fact_id",
            sa.BigInteger(),
            sa.ForeignKey("facts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Peso copiado da regra.
        sa.Column("severity", sa.String(10), nullable=False),
        # Quem disse era liderança na empresa.
        sa.Column("by_company_leader", sa.Boolean(), server_default="false", nullable=False),
        # Quem disse era liderança no projeto.
        sa.Column("by_project_leader", sa.Boolean(), server_default="false", nullable=False),
        # Situação: novo, visto, resolvido ou descartado.
        sa.Column("status", sa.String(12), server_default="novo", nullable=False),
        # Quando a situação mudou.
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=True),
        # Quando o alerta nasceu.
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # O mesmo par (regra, fato) só uma vez.
        sa.UniqueConstraint("rule_id", "fact_id"),
    )
    # Índice: a página filtra pela Empresa.
    op.create_index("ix_alerts_company_id", "alerts", ["company_id"])
    # Índice: o menu conta os novos em toda página.
    op.create_index("ix_alerts_status", "alerts", ["status"])
    # Índice: achar os alertas de um fato.
    op.create_index("ix_alerts_fact_id", "alerts", ["fact_id"])

    # Trava de Empresa (função da 0008): alerta e regra da mesma Empresa. A regra sem
    # Empresa tem company_id vazio e a função pula valores vazios, como deve.
    op.execute(
        "CREATE CONSTRAINT TRIGGER empresa_coerente AFTER INSERT OR UPDATE ON alerts "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION empresa_coerente('self', 'company_id', 'alert_rules', 'rule_id')"
    )

    # Regras-padrão, para todas as Empresas.
    op.bulk_insert(
        rules,
        [
            {
                "name": name,
                "fact_kinds": kinds,
                "leadership": leadership,
                "owner_rule": owner,
                "due_within_days": days,
                "severity": severity,
            }
            for name, kinds, leadership, owner, days, severity in DEFAULT_RULES
        ],
    )


# Desfaz a migração.
def downgrade() -> None:
    # A trava dos alertas.
    op.execute("DROP TRIGGER empresa_coerente ON alerts")
    # Alertas (os índices saem junto com a tabela).
    op.drop_table("alerts")
    # Regras.
    op.drop_table("alert_rules")
