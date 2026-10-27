import os

# Configuração de teste antes de importar o app (as settings são lidas uma vez).
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://assistente:teste@localhost:5432/assistente"
)
os.environ["ALLOWED_TAILSCALE_LOGIN"] = "dona@exemplo.com"
os.environ["TOKEN_ENCRYPTION_KEY"] = "kZ0l2xg2Hn4n0q3m9kz8m2bqzS7a0o4Xb1Yc5V8wq0E="
os.environ["ANTHROPIC_API_KEY"] = "teste"
os.environ["LLM_MONTHLY_BUDGET_USD"] = "10"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import get_engine, get_sessionmaker  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def sessions():
    return get_sessionmaker()
