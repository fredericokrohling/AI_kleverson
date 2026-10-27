from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.llm import BudgetExceededError, LLMClient, estimate_cost, month_spend_usd
from app.models import AuditLog, LLMCall


def _usage(inp=1000, out=500, cache_read=0, cache_write=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def create(self, **params):
        self.calls.append(params)
        if self.error:
            raise self.error
        return self.response


def _client(response=None, error=None):
    messages = FakeMessages(response, error)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def _response(text="Sim, funcionando.", model="claude-opus-5-5", usage=None, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        model=model,
        usage=usage or _usage(),
        stop_reason=stop,
        _request_id="req_teste",
    )


def test_cost_opus():
    # 1000 entrada x 4 + 500 saída x 20 por milhão = 0,004 + 0,010
    assert estimate_cost("claude-opus-5-5", _usage()) == Decimal("0.014")


def test_cost_counts_cache():
    cost = estimate_cost(
        "claude-opus-5-5", _usage(inp=0, out=0, cache_read=1_000_000, cache_write=1_000_000)
    )
    assert cost == Decimal("0.20") + Decimal("5.00")


def test_generate_records_call_and_uses_fallback(sessions):
    fake, messages = _client(_response())
    result = LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")

    assert result.text == "Sim, funcionando."
    assert result.cost_usd == Decimal("0.014")
    params = messages.calls[0]
    assert params["fallbacks"] == "default"
    assert params["betas"] == ["server-side-fallback-2026-07-01"]
    assert params["output_config"] == {"effort": "medium"}
    with sessions() as s:
        call = s.scalars(select(LLMCall)).one()
        assert (call.purpose, call.input_tokens, call.request_id) == ("teste", 1000, "req_teste")
        assert month_spend_usd(s) == Decimal("0.014")


def test_no_fallback_for_haiku(sessions, monkeypatch):
    settings = get_settings().model_copy(update={"llm_model": "claude-haiku-5-5"})
    fake, messages = _client(_response(model="claude-haiku-5-5"))
    LLMClient(sessions, settings, fake).generate(purpose="teste", prompt="oi")
    assert "fallbacks" not in messages.calls[0]


def test_budget_blocks_before_spending(sessions):
    with sessions() as s:
        s.add(LLMCall(purpose="x", model="claude-opus-5-5", cost_usd=Decimal("10")))
        s.commit()
    fake, messages = _client(_response())
    with pytest.raises(BudgetExceededError):
        LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")
    assert messages.calls == []


def test_alert_when_crossing_80_percent(sessions):
    with sessions() as s:
        s.add(LLMCall(purpose="x", model="claude-opus-5-5", cost_usd=Decimal("7.99")))
        s.commit()
    fake, _ = _client(_response())
    LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")
    with sessions() as s:
        assert s.scalars(select(AuditLog.action)).all() == ["alerta_orcamento_llm"]


def test_api_error_is_recorded_and_raised(sessions):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(request=request)
    fake, _ = _client(error=error)
    with pytest.raises(anthropic.APIConnectionError):
        LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")
    with sessions() as s:
        assert "APIConnectionError" in s.scalars(select(LLMCall.error)).one()


def test_refusal_is_reported(sessions):
    fake, _ = _client(_response(text="", stop="refusal"))
    result = LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")
    assert result.refused
