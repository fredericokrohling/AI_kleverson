"""Preparação comum a todos os testes (o pytest carrega este arquivo sozinho).

Por que testes automáticos: cada mudança no código roda estes testes no CI (GitHub
Actions). Se algo que funcionava quebrar, o PR fica vermelho antes de chegar ao
servidor. Os testes usam um Postgres de verdade, não um banco simulado, para pegar
erros de SQL reais.

"Fixture" é uma função que prepara algo para os testes (banco, dados, objetos).
"""

# os: acesso às variáveis de ambiente.
import os

# Configuração de teste antes de importar o app (as settings são lidas uma vez).
# Endereço do banco de teste; setdefault respeita um valor já definido (como no CI).
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://assistente:teste@localhost:5432/assistente"
)
# Login Tailscale "autorizado" nos testes.
os.environ["ALLOWED_TAILSCALE_LOGIN"] = "dona@exemplo.com"
# Chave Fernet fixa, só para testes (nunca use em produção).
os.environ["TOKEN_ENCRYPTION_KEY"] = "kZ0l2xg2Hn4n0q3m9kz8m2bqzS7a0o4Xb1Yc5V8wq0E="
# Chave falsa: nenhum teste chama a API real da Anthropic.
os.environ["ANTHROPIC_API_KEY"] = "teste"
# Teto de US$ 10, igual ao combinado, para testar o bloqueio.
os.environ["LLM_MONTHLY_BUDGET_USD"] = "10"

# "noqa: E402" avisa o ruff que estes imports vêm depois de código de propósito.
# Biblioteca de testes.
import pytest  # noqa: E402

# command executa comandos do Alembic (upgrade) a partir do Python.
from alembic import command  # noqa: E402

# Config lê o alembic.ini.
from alembic.config import Config  # noqa: E402

# text permite executar SQL escrito à mão.
from sqlalchemy import text  # noqa: E402

# Motor e fábrica de sessões do app.
from app.db import get_engine, get_sessionmaker  # noqa: E402

# Base conhece todas as tabelas.
from app.models import Base  # noqa: E402


# scope="session": roda uma vez para a bateria inteira; autouse: sem precisar pedir.
@pytest.fixture(scope="session", autouse=True)
def _schema():
    # Aplica todas as migrações: os testes também provam que as migrações funcionam.
    command.upgrade(Config("alembic.ini"), "head")


# Roda em volta de cada teste, automaticamente.
@pytest.fixture(autouse=True)
def _clean_tables():
    # yield: aqui o teste roda; o código abaixo é a limpeza depois dele.
    yield
    # Lista de todas as tabelas separadas por vírgula.
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    # begin() abre uma transação e faz commit no fim.
    with get_engine().begin() as conn:
        # Esvazia todas as tabelas e zera os contadores de id, para cada teste começar limpo.
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


# Fixture pedida pelo nome: um teste com parâmetro "sessions" recebe a fábrica.
@pytest.fixture
def sessions():
    # Devolve a fábrica de sessões do banco de teste.
    return get_sessionmaker()
