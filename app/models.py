"""Esquema do banco, organizado em camadas (modelo medallion).

- Cadastro: projetos e stakeholders, editados por você no console.
- Bronze: conteúdo bruto como chegou (e-mail, transcrição, mensagem). Nunca é editado.
- Silver: fatos extraídos (tarefa, decisão, risco...), sempre com trecho literal da fonte.
- Sugestões da IA: separadas, não contaminam bronze nem silver.
- Gold: virá como views SQL (KPIs calculados por código, nunca pelo LLM).
- Operação: tokens OAuth criptografados, chamadas ao LLM com custo, log de auditoria.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _enum(cls: type[StrEnum], name: str) -> Enum:
    # Guarda o valor (texto em português) no banco, não o nome Python.
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class SourceKind(StrEnum):
    GMAIL = "gmail"
    CALENDAR = "calendar"
    MEET_TRANSCRIPT = "transcricao_reuniao"
    GOOGLE_CHAT = "google_chat"
    MANUAL = "manual"


class FactKind(StrEnum):
    TASK = "tarefa"
    DECISION = "decisao"
    RISK = "risco"
    ISSUE = "problema"
    ASSUMPTION = "premissa"
    DEPENDENCY = "dependencia"
    DEADLINE = "prazo"
    DEFINITION = "definicao"


class ClassificationMethod(StrEnum):
    THREAD = "mesma_conversa"
    STAKEHOLDER = "stakeholder"
    KEYWORD = "palavra_chave"
    LLM = "llm"
    MANUAL = "manual"


class SuggestionStatus(StrEnum):
    PROPOSED = "proposta"
    ACCEPTED = "aceita"
    DISCARDED = "descartada"


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Cadastro -----------------------------------------------------------------


class Project(Timestamped, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    code: Mapped[str | None] = mapped_column(String(50), unique=True)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    notes: Mapped[str | None] = mapped_column(Text)


class Stakeholder(Timestamped, Base):
    __tablename__ = "stakeholders"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    organization: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(200))


class ProjectStakeholder(Base):
    __tablename__ = "project_stakeholders"

    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    stakeholder_id: Mapped[int] = mapped_column(
        ForeignKey("stakeholders.id", ondelete="CASCADE"), primary_key=True
    )
    role_in_project: Mapped[str | None] = mapped_column(String(200))


# --- Bronze -------------------------------------------------------------------


class BronzeItem(Base):
    __tablename__ = "bronze_items"
    # Reprocessar a mesma mensagem não duplica nada.
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"))
    external_id: Mapped[str] = mapped_column(String(500))
    thread_id: Mapped[str | None] = mapped_column(String(500), index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    author: Mapped[str | None] = mapped_column(String(500))
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text, server_default="")
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    content_sha256: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bronze_item_id: Mapped[int] = mapped_column(
        ForeignKey("bronze_items.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str | None] = mapped_column(String(200))
    # Caminho do arquivo original no bucket privado; o binário não fica no banco.
    object_key: Mapped[str | None] = mapped_column(String(1000))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_error: Mapped[str | None] = mapped_column(Text)


class ItemClassification(Base):
    """A qual projeto um item bruto pertence, e como isso foi decidido."""

    __tablename__ = "item_classifications"

    bronze_item_id: Mapped[int] = mapped_column(
        ForeignKey("bronze_items.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    method: Mapped[ClassificationMethod] = mapped_column(
        _enum(ClassificationMethod, "classification_method")
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Silver -------------------------------------------------------------------


class Fact(Timestamped, Base):
    __tablename__ = "facts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[FactKind] = mapped_column(_enum(FactKind, "fact_kind"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("stakeholders.id"))
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    status: Mapped[str | None] = mapped_column(String(50))
    source_item_id: Mapped[int] = mapped_column(ForeignKey("bronze_items.id"), index=True)
    # Trecho literal da fonte que sustenta o fato. O código confere se ele existe no bronze.
    source_quote: Mapped[str] = mapped_column(Text)
    quote_verified: Mapped[bool] = mapped_column(Boolean, server_default="false")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    # Um fato corrigido não é apagado: aponta para o fato que o substituiu.
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("facts.id"))


# --- Sugestões da IA ----------------------------------------------------------


class AISuggestion(Timestamped, Base):
    __tablename__ = "ai_suggestions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[SuggestionStatus] = mapped_column(
        _enum(SuggestionStatus, "suggestion_status"),
        server_default=SuggestionStatus.PROPOSED.value,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Operação -----------------------------------------------------------------


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    __table_args__ = (UniqueConstraint("provider", "account_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    account_email: Mapped[str] = mapped_column(String(320))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # Tokens criptografados com TOKEN_ENCRYPTION_KEY; nunca em texto puro.
    refresh_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    purpose: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    stop_reason: Mapped[str | None] = mapped_column(String(50))
    request_id: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100), index=True)
    target: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


def audit(session, actor: str, action: str, target: str | None = None, **details: Any) -> None:
    """Registra uma ação no log de auditoria (quem fez o quê, em quê)."""
    session.add(AuditLog(actor=actor, action=action, target=target, details=details))
