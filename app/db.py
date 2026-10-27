"""Conexão com o Postgres.

Por que SQLAlchemy: ele traduz classes Python (em models.py) para tabelas e consultas
SQL. Assim escrevemos Python em vez de SQL na maior parte do tempo, e o mesmo código
funciona com qualquer banco relacional se um dia for preciso trocar.
"""

# Iterator descreve uma função que "entrega" valores com yield (usado em get_session).
from collections.abc import Iterator

# lru_cache: cria o objeto uma vez e reaproveita nas próximas chamadas.
from functools import lru_cache

# create_engine abre o "motor": o objeto que sabe falar com o banco e mantém conexões.
from sqlalchemy import create_engine

# Engine é o tipo do motor, usado só para anotar o retorno da função.
from sqlalchemy.engine import Engine

# Session é uma conversa com o banco; sessionmaker é a fábrica que cria sessões.
from sqlalchemy.orm import Session, sessionmaker

# Configuração do sistema (endereço do banco vem daqui).
from app.config import get_settings


# Um motor só para o sistema todo: abrir motores é caro, reaproveitar é barato.
@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping testa a conexão antes de usar; evita erro se o banco reiniciou.
    return create_engine(get_settings().database_url, pool_pre_ping=True)


# Uma fábrica de sessões só, também reaproveitada.
@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    # expire_on_commit=False: depois de salvar, os objetos continuam legíveis sem nova
    # consulta ao banco (útil para mostrar o que acabou de ser gravado).
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """Dependência do FastAPI: uma sessão por requisição."""
    # O "with" garante que a sessão é fechada no fim, mesmo se der erro no meio.
    with get_sessionmaker()() as session:
        # yield entrega a sessão para a rota; quando a rota termina, o código volta
        # aqui e o "with" fecha a sessão.
        yield session
