"""Ambiente do Alembic: usa a DATABASE_URL do .env e o esquema de app.models.

Por que Alembic: o banco evolui junto com o código (tabelas novas, colunas novas).
Cada mudança vira um arquivo de "migração" numerado em migrations/versions/. Rodar
`alembic upgrade head` aplica, em ordem, as migrações que o banco ainda não tem.
Assim o banco do servidor e o do seu computador ficam sempre iguais, sem SQL manual.

Este arquivo é chamado pelo próprio Alembic toda vez que você roda um comando dele.
"""

# Configura o log a partir da seção [loggers] do alembic.ini.
from logging.config import fileConfig

# context dá acesso à configuração e às funções de execução do Alembic.
from alembic import context

# engine_from_config cria a conexão a partir da configuração; pool controla conexões.
from sqlalchemy import engine_from_config, pool

# Leitura do .env (endereço do banco).
from app.config import get_settings

# Base conhece todas as tabelas declaradas em app/models.py.
from app.models import Base

# Objeto de configuração (conteúdo do alembic.ini).
config = context.config
# Substitui o endereço do banco pelo do .env, para a senha não ficar no alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().database_url)
# Se o Alembic foi chamado com um arquivo .ini...
if config.config_file_name is not None:
    # ...configura o log conforme esse arquivo.
    fileConfig(config.config_file_name)

# O "modelo desejado" do banco. O Alembic compara com o banco real para gerar
# migrações (autogenerate) e para o `alembic check` do CI.
target_metadata = Base.metadata


# Modo offline: gera o SQL sem conectar no banco (útil para revisar antes de aplicar).
def run_migrations_offline() -> None:
    # Configura o contexto só com o endereço, sem conexão aberta.
    context.configure(
        # Endereço do banco.
        url=config.get_main_option("sqlalchemy.url"),
        # Esquema desejado.
        target_metadata=target_metadata,
        # Escreve os valores direto no SQL gerado, em vez de parâmetros.
        literal_binds=True,
        # Estilo dos parâmetros no SQL (":nome").
        dialect_opts={"paramstyle": "named"},
    )
    # Abre uma transação: ou tudo é aplicado, ou nada.
    with context.begin_transaction():
        # Executa as migrações pendentes.
        context.run_migrations()


# Modo online (o normal): conecta no banco e aplica as migrações.
def run_migrations_online() -> None:
    # Cria a conexão a partir das opções "sqlalchemy.*" da configuração.
    connectable = engine_from_config(
        # Seção principal do alembic.ini.
        config.get_section(config.config_ini_section, {}),
        # Só as opções que começam com "sqlalchemy.".
        prefix="sqlalchemy.",
        # NullPool: abre uma conexão, usa e fecha (não precisa de pool num comando único).
        poolclass=pool.NullPool,
    )
    # Abre a conexão e garante o fechamento no fim.
    with connectable.connect() as connection:
        # Liga o contexto do Alembic à conexão aberta.
        context.configure(connection=connection, target_metadata=target_metadata)
        # Transação: se uma migração falhar no meio, o banco volta ao estado anterior.
        with context.begin_transaction():
            # Executa as migrações pendentes.
            context.run_migrations()


# O Alembic informa o modo conforme o comando digitado (--sql = offline).
if context.is_offline_mode():
    # Gera SQL sem conectar.
    run_migrations_offline()
# Caso normal.
else:
    # Conecta e aplica.
    run_migrations_online()
