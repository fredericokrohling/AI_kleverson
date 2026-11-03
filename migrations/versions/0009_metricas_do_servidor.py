"""métricas do servidor: uso de processador e memória a cada minuto

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-03 18:10:00

Cria server_metrics, onde o serviço coletor grava uma medição por minuto (processador,
espera de disco, tempo roubado pela AWS, memória, swap e carga). A página Configurações
desenha com ela os gráficos das últimas 2 horas e dos últimos 7 dias.
Cada coluna aqui espelha uma coluna comentada em app/models.py (ServerMetric).
"""

# Sequence: tipo "lista ou tupla", usado nas anotações abaixo.
from collections.abc import Sequence

# sa: tipos de coluna do SQLAlchemy.
import sqlalchemy as sa

# op: operações de esquema (criar tabela, criar índice, apagar...).
from alembic import op

# Identificador desta migração.
revision: str = "0009"
# Migração anterior: esta só roda depois da 0008.
down_revision: str | Sequence[str] | None = "0008"
# Rótulos de ramificação (não usamos).
branch_labels: str | Sequence[str] | None = None
# Dependências de outras migrações (nenhuma além da anterior).
depends_on: str | Sequence[str] | None = None


# Aplica a migração.
def upgrade() -> None:
    # Tabela server_metrics: uma linha por minuto medido.
    op.create_table(
        "server_metrics",
        # Identificador interno.
        sa.Column("id", sa.BigInteger(), primary_key=True),
        # Fim do minuto medido, com fuso.
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        # % do processador trabalhando.
        sa.Column("cpu_pct", sa.Float(), nullable=False),
        # % esperando o disco.
        sa.Column("iowait_pct", sa.Float(), nullable=False),
        # % roubado pelo hipervisor da AWS.
        sa.Column("steal_pct", sa.Float(), nullable=False),
        # % da memória em uso.
        sa.Column("mem_pct", sa.Float(), nullable=False),
        # % do swap em uso (vazio sem swap).
        sa.Column("swap_pct", sa.Float(), nullable=True),
        # Carga média de 1 minuto.
        sa.Column("load1", sa.Float(), nullable=False),
    )
    # Índice por momento: os gráficos e a limpeza filtram por período.
    op.create_index("ix_server_metrics_sampled_at", "server_metrics", ["sampled_at"])


# Desfaz a migração.
def downgrade() -> None:
    # Apaga o índice.
    op.drop_index("ix_server_metrics_sampled_at", table_name="server_metrics")
    # Apaga a tabela.
    op.drop_table("server_metrics")
