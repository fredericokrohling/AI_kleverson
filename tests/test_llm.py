"""Testes da camada do Claude: custo, registro, teto e fallback.

Nenhum teste chama a API real. Usamos um cliente falso ("fake") que devolve respostas
prontas. Assim os testes são grátis, rápidos e não dependem de internet.
"""

# Decimal para comparar valores em dólar sem erro de arredondamento.
from decimal import Decimal

# SimpleNamespace cria objetos com atributos livres: SimpleNamespace(a=1).a == 1.
from types import SimpleNamespace

# SDK da Anthropic (só para usar a classe de erro real).
import anthropic

# httpx: biblioteca HTTP; o erro da Anthropic exige um objeto de requisição dela.
import httpx

# Biblioteca de testes.
import pytest

# select monta consultas.
from sqlalchemy import select

# Configuração.
from app.config import get_settings

# O que estamos testando.
from app.llm import BudgetExceededError, LLMClient, estimate_cost, month_spend_usd

# Tabelas consultadas para conferir os registros.
from app.models import AuditLog, LLMCall


# Monta um objeto de uso de tokens no mesmo formato que a API devolve.
def _usage(inp=1000, out=500, cache_read=0, cache_write=0):
    # Devolve um objeto com os quatro contadores.
    return SimpleNamespace(
        # Tokens de entrada.
        input_tokens=inp,
        # Tokens de saída.
        output_tokens=out,
        # Tokens lidos do cache.
        cache_read_input_tokens=cache_read,
        # Tokens gravados no cache.
        cache_creation_input_tokens=cache_write,
    )


# Imita client.beta.messages: guarda as chamadas recebidas e devolve a resposta pronta.
class FakeMessages:
    # Recebe a resposta ou o erro que deve devolver.
    def __init__(self, response=None, error=None):
        # Guarda tudo; calls começa vazia e registra cada chamada.
        self.response, self.error, self.calls = response, error, []

    # Mesmo nome do método real, para o LLMClient não perceber a diferença.
    def create(self, **params):
        # Anota os parâmetros recebidos, para o teste conferir depois.
        self.calls.append(params)
        # Se foi configurado um erro, levanta.
        if self.error:
            raise self.error
        # Senão, devolve a resposta pronta.
        return self.response


# Monta o cliente falso inteiro: fake.beta.messages.create(...).
def _client(response=None, error=None):
    # A parte que registra as chamadas.
    messages = FakeMessages(response, error)
    # Devolve o cliente e também messages, para o teste inspecionar.
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


# Monta uma resposta no formato da API.
def _response(text="Sim, funcionando.", model="claude-opus-5-5", usage=None, stop="end_turn"):
    # Objeto com os campos que o LLMClient lê.
    return SimpleNamespace(
        # Um bloco de raciocínio (deve ser ignorado) e um de texto.
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        # Modelo que "respondeu".
        model=model,
        # Uso de tokens (padrão: 1000 entrada, 500 saída).
        usage=usage or _usage(),
        # Motivo da parada.
        stop_reason=stop,
        # Id da requisição.
        _request_id="req_teste",
    )


# Confere a conta de custo do Opus.
def test_cost_opus():
    # 1000 entrada x 4 + 500 saída x 20 por milhão = 0,004 + 0,010
    assert estimate_cost("claude-opus-5-5", _usage()) == Decimal("0.014")


# Confere que leitura e gravação de cache entram no custo.
def test_cost_counts_cache():
    # Um milhão de tokens lidos do cache e um milhão gravados.
    cost = estimate_cost(
        "claude-opus-5-5", _usage(inp=0, out=0, cache_read=1_000_000, cache_write=1_000_000)
    )
    # Leitura: US$ 0,20. Gravação: 4,00 x 1,25 = US$ 5,00.
    assert cost == Decimal("0.20") + Decimal("5.00")


# Uma chamada normal: texto certo, custo certo, registro gravado e fallback ligado.
def test_generate_records_call_and_uses_fallback(sessions):
    # Cliente falso com uma resposta padrão.
    fake, messages = _client(_response())
    # Chama o generate.
    result = LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")

    # Pegou só o bloco de texto.
    assert result.text == "Sim, funcionando."
    # Custo calculado.
    assert result.cost_usd == Decimal("0.014")
    # Parâmetros que chegaram à "API".
    params = messages.calls[0]
    # Fallback do servidor ligado.
    assert params["fallbacks"] == "default"
    # Recurso beta pedido.
    assert params["betas"] == ["server-side-fallback-2026-07-01"]
    # Esforço padrão.
    assert params["output_config"] == {"effort": "medium"}
    # Confere o que foi gravado no banco.
    with sessions() as s:
        # .one() exige exatamente uma linha.
        call = s.scalars(select(LLMCall)).one()
        # Confere três campos de uma vez, comparando tuplas.
        assert (call.purpose, call.input_tokens, call.request_id) == ("teste", 1000, "req_teste")
        # O gasto do mês já inclui esta chamada.
        assert month_spend_usd(s) == Decimal("0.014")


# O Haiku não aceita o fallback do servidor; ele não deve ser pedido.
def test_no_fallback_for_haiku(sessions, monkeypatch):
    # Cópia da configuração trocando só o modelo.
    settings = get_settings().model_copy(update={"llm_model": "claude-haiku-5-5"})
    # Cliente falso.
    fake, messages = _client(_response(model="claude-haiku-5-5"))
    # Chama.
    LLMClient(sessions, settings, fake).generate(purpose="teste", prompt="oi")
    # Nenhum parâmetro de fallback foi enviado.
    assert "fallbacks" not in messages.calls[0]


# Com o teto atingido, a API nem é chamada.
def test_budget_blocks_before_spending(sessions):
    # Simula US$ 10 já gastos no mês.
    with sessions() as s:
        # Uma chamada antiga de US$ 10.
        s.add(LLMCall(purpose="x", model="claude-opus-5-5", cost_usd=Decimal("10")))
        # Grava.
        s.commit()
    # Cliente falso.
    fake, messages = _client(_response())
    # Espera o erro de teto.
    with pytest.raises(BudgetExceededError):
        # Tenta chamar.
        LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")
    # Nenhuma chamada chegou à "API".
    assert messages.calls == []


# Ao cruzar 80% do teto, um alerta vai para o log de auditoria.
def test_alert_when_crossing_80_percent(sessions):
    # Gasto anterior logo abaixo da linha de US$ 8.
    with sessions() as s:
        # US$ 7,99 já gastos.
        s.add(LLMCall(purpose="x", model="claude-opus-5-5", cost_usd=Decimal("7.99")))
        # Grava.
        s.commit()
    # Cliente falso; a resposta custa US$ 0,014 e cruza a linha.
    fake, _ = _client(_response())
    # Chama.
    LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")
    # Confere o log de auditoria.
    with sessions() as s:
        # Exatamente um alerta registrado.
        assert s.scalars(select(AuditLog.action)).all() == ["alerta_orcamento_llm"]


# Um erro da API é registrado no banco e repassado para quem chamou.
def test_api_error_is_recorded_and_raised(sessions):
    # Requisição de exemplo, exigida pelo construtor do erro.
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    # Erro de conexão, como se a internet tivesse caído.
    error = anthropic.APIConnectionError(request=request)
    # Cliente falso que levanta esse erro.
    fake, _ = _client(error=error)
    # O erro chega até aqui.
    with pytest.raises(anthropic.APIConnectionError):
        # Tenta chamar.
        LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")
    # E ficou registrado no banco.
    with sessions() as s:
        # A coluna error contém o nome do erro.
        assert "APIConnectionError" in s.scalars(select(LLMCall.error)).one()


# Uma recusa do modelo aparece em result.refused.
def test_refusal_is_reported(sessions):
    # Resposta vazia com motivo "refusal".
    fake, _ = _client(_response(text="", stop="refusal"))
    # Chama.
    result = LLMClient(sessions, get_settings(), fake).generate(purpose="teste", prompt="oi")
    # O atalho refused indica a recusa.
    assert result.refused
