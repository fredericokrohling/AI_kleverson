"""Camada única de acesso ao Claude.

Todo o sistema chama o modelo por aqui. Isso garante três coisas:
1. cada chamada fica registrada com tokens e custo (tabela llm_calls);
2. o teto mensal é respeitado antes de gastar;
3. trocar de modelo ou provedor no futuro mexe só neste arquivo.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import anthropic
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import CACHE_WRITE_MULTIPLIER, MODEL_PRICES_USD_PER_MTOK, Settings, get_settings
from app.models import LLMCall, audit

log = logging.getLogger(__name__)

# Modelos que aceitam o fallback do servidor: se o modelo recusar por política de
# segurança, a própria API refaz a chamada em outro modelo, dentro da mesma requisição.
_SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
_SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_MTOK = Decimal(1_000_000)


class BudgetExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    stop_reason: str | None
    cost_usd: Decimal
    request_id: str | None

    @property
    def refused(self) -> bool:
        return self.stop_reason == "refusal"


def estimate_cost(model: str, usage) -> Decimal:
    """Custo em dólares de uma resposta, a partir do uso de tokens."""
    input_price, output_price, cache_read_price = MODEL_PRICES_USD_PER_MTOK.get(
        model, MODEL_PRICES_USD_PER_MTOK["claude-opus-5-5"]
    )
    cache_write = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0
    return (
        usage.input_tokens * input_price
        + usage.output_tokens * output_price
        + cache_read * cache_read_price
        + cache_write * input_price * CACHE_WRITE_MULTIPLIER
    ) / _MTOK


def month_spend_usd(session: Session, now: datetime | None = None) -> Decimal:
    now = now or datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = session.scalar(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
            LLMCall.created_at >= month_start
        )
    )
    return Decimal(total)


class LLMClient:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()
        self._client = client or anthropic.Anthropic(api_key=self._settings.anthropic_api_key)

    def generate(
        self,
        *,
        purpose: str,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 16000,
        effort: str = "medium",
    ) -> LLMResult:
        """Uma pergunta, uma resposta em texto. `purpose` identifica a chamada no log."""
        model = self._settings.llm_model
        budget = self._settings.llm_monthly_budget_usd

        with self._sessions() as session:
            spent_before = month_spend_usd(session)
        if spent_before >= budget:
            raise BudgetExceededError(
                f"Teto mensal de US$ {budget} atingido (gasto: US$ {spent_before:.2f})."
            )

        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": effort},
        }
        if system:
            params["system"] = system
        if model in _SERVER_FALLBACK_MODELS:
            params["betas"] = [_SERVER_FALLBACK_BETA]
            params["fallbacks"] = "default"

        try:
            response = self._client.beta.messages.create(**params)
        except anthropic.APIError as exc:
            self._record(
                LLMCall(purpose=purpose, model=model, error=f"{type(exc).__name__}: {exc}")
            )
            raise

        cost = estimate_cost(response.model, response.usage)
        text = "".join(block.text for block in response.content if block.type == "text")
        self._record(
            LLMCall(
                purpose=purpose,
                model=response.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_input_tokens or 0,
                cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
                cost_usd=cost,
                stop_reason=response.stop_reason,
                request_id=response._request_id,
            ),
            alert_if_crossing=(spent_before, spent_before + cost),
        )
        return LLMResult(
            text=text,
            model=response.model,
            stop_reason=response.stop_reason,
            cost_usd=cost,
            request_id=response._request_id,
        )

    def _record(
        self, call: LLMCall, alert_if_crossing: tuple[Decimal, Decimal] | None = None
    ) -> None:
        with self._sessions() as session:
            session.add(call)
            if alert_if_crossing:
                before, after = alert_if_crossing
                threshold = (
                    self._settings.llm_monthly_budget_usd * self._settings.llm_budget_alert_ratio
                )
                if before < threshold <= after:
                    log.warning("Gasto com LLM passou de %s do teto mensal", threshold)
                    audit(
                        session,
                        "sistema",
                        "alerta_orcamento_llm",
                        gasto_usd=str(after),
                        teto_usd=str(self._settings.llm_monthly_budget_usd),
                    )
            session.commit()
