"""Camada única de acesso ao Claude.

Todo o sistema chama o modelo por aqui. Isso garante três coisas:
1. cada chamada fica registrada com tokens e custo (tabela llm_calls);
2. o teto mensal é respeitado antes de gastar;
3. trocar de modelo ou provedor no futuro mexe só neste arquivo.

Por que confiar no nosso próprio cálculo de custo: o painel da Anthropic mostra o
gasto com atraso. Calculando aqui a cada resposta, o teto de US$ 10 é aplicado na hora.
"""

# logging escreve mensagens no log do servidor (o que aparece em `docker compose logs`).
import logging

# dataclass cria classes simples de dados sem escrever __init__ à mão.
from dataclasses import dataclass

# UTC é o fuso de referência; datetime representa data e hora.
from datetime import UTC, datetime

# Decimal para dinheiro (sem erro de arredondamento).
from decimal import Decimal

# SDK oficial da Anthropic: a biblioteca que conversa com a API do Claude.
import anthropic

# func gera funções SQL (SUM, COALESCE); select monta consultas.
from sqlalchemy import func, select

# Session é uma conversa com o banco; sessionmaker é a fábrica de sessões.
from sqlalchemy.orm import Session, sessionmaker

# Preços, configuração e a função que lê a configuração.
from app.config import CACHE_WRITE_MULTIPLIER, MODEL_PRICES_USD_PER_MTOK, Settings, get_settings

# Tabela de chamadas ao LLM e a função que grava no log de auditoria.
from app.models import LLMCall, audit

# Modelo e esforço escolhidos no console.
from app.runtime_settings import current

# Cria um "logger" com o nome deste módulo (app.llm), para identificar a origem da mensagem.
log = logging.getLogger(__name__)

# Modelos que aceitam o fallback do servidor: se o modelo recusar por política de
# segurança, a própria API refaz a chamada em outro modelo, dentro da mesma requisição.
_SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
# Nome do recurso beta que liga esse fallback (a API exige que ele seja pedido).
_SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Um milhão, pois os preços são por milhão de tokens. O "_" separa os milhares.
_MTOK = Decimal(1_000_000)


# Erro próprio para "teto atingido". Ter um tipo específico deixa quem chama tratar
# só este caso (ex.: mostrar um aviso no console) sem confundir com outros erros.
class BudgetExceededError(RuntimeError):
    # Nada a acrescentar: o nome da classe já diz tudo.
    pass


# frozen=True: depois de criado, o resultado não pode ser alterado (evita bugs).
@dataclass(frozen=True)
class LLMResult:
    # Texto da resposta.
    text: str
    # Modelo que respondeu (pode ser outro se o fallback entrou em ação).
    model: str
    # Motivo da parada: "end_turn" (terminou), "max_tokens" (cortou), "refusal" etc.
    stop_reason: str | None
    # Quanto a chamada custou.
    cost_usd: Decimal
    # Id da requisição na Anthropic.
    request_id: str | None

    # Atalho: result.refused é True se o modelo se recusou a responder.
    @property
    def refused(self) -> bool:
        # Compara o motivo da parada com "refusal".
        return self.stop_reason == "refusal"


# usage é o objeto de contagem de tokens que vem em cada resposta da API.
def estimate_cost(model: str, usage) -> Decimal:
    """Custo em dólares de uma resposta, a partir do uso de tokens."""
    # Busca os preços do modelo; se for um modelo desconhecido, usa o do Opus
    # (o mais caro), para errar para cima e nunca subestimar o gasto.
    input_price, output_price, cache_read_price = MODEL_PRICES_USD_PER_MTOK.get(
        model, MODEL_PRICES_USD_PER_MTOK["claude-opus-5-5"]
    )
    # Tokens gravados no cache; "or 0" troca None (sem informação) por zero.
    cache_write = usage.cache_creation_input_tokens or 0
    # Tokens lidos do cache.
    cache_read = usage.cache_read_input_tokens or 0
    # Soma cada tipo de token vezes seu preço e divide por um milhão.
    return (
        # Entrada normal.
        usage.input_tokens * input_price
        # Saída (o texto gerado), a parte mais cara.
        + usage.output_tokens * output_price
        # Leitura de cache, a mais barata.
        + cache_read * cache_read_price
        # Gravação de cache: preço de entrada com acréscimo de 25%.
        + cache_write * input_price * CACHE_WRITE_MULTIPLIER
    ) / _MTOK


# Soma quanto já foi gasto no mês corrente. "now" pode ser passado nos testes.
def month_spend_usd(session: Session, now: datetime | None = None) -> Decimal:
    # Se ninguém passou uma data, usa o momento atual em UTC.
    now = now or datetime.now(UTC)
    # Primeiro instante do mês: dia 1, meia-noite.
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # session.scalar executa a consulta e devolve um único valor.
    total = session.scalar(
        # SELECT COALESCE(SUM(cost_usd), 0): soma os custos; sem linhas, devolve 0.
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
            # WHERE created_at >= início do mês.
            LLMCall.created_at >= month_start
        )
    )
    # Garante que o resultado é Decimal (o banco pode devolver int quando é zero).
    return Decimal(total)


# Cliente do Claude com registro de custo e teto. Todo o sistema usa esta classe.
class LLMClient:
    # Recebe as dependências de fora (injeção de dependência): nos testes passamos
    # um cliente falso no lugar do anthropic.Anthropic e nada é cobrado.
    def __init__(
        self,
        # Fábrica de sessões do banco (onde gravar as chamadas).
        session_factory: sessionmaker[Session],
        # Configuração; se não vier, lê do .env.
        settings: Settings | None = None,
        # Cliente da Anthropic; se não vier, cria o real.
        client: anthropic.Anthropic | None = None,
    ) -> None:
        # Guarda a fábrica de sessões.
        self._sessions = session_factory
        # Usa a configuração recebida ou a padrão.
        self._settings = settings or get_settings()
        # Usa o cliente recebido ou cria um real com a chave da API.
        self._client = client or anthropic.Anthropic(api_key=self._settings.anthropic_api_key)

    # O "*" obriga a passar os argumentos pelo nome: generate(purpose=..., prompt=...).
    # Isso evita trocar a ordem de dois textos sem perceber.
    def generate(
        self,
        *,
        # Para que serve a chamada (vai para o registro de custo).
        purpose: str,
        # A pergunta ou instrução: texto, ou uma lista de blocos (imagem, PDF e texto).
        prompt: str | list[dict],
        # Instrução de sistema opcional (papel, regras fixas).
        system: str | None = None,
        # Limite de tamanho da resposta, em tokens.
        max_tokens: int = 16000,
        # Quanto o modelo deve "pensar" (low a max). None = o escolhido no console.
        effort: str | None = None,
        # Modelo para esta chamada. None = o escolhido no console.
        model: str | None = None,
    ) -> LLMResult:
        """Uma pergunta, uma resposta em texto. `purpose` identifica a chamada no log."""
        # Teto mensal configurado.
        budget = self._settings.llm_monthly_budget_usd

        # Abre uma sessão só para consultar o gasto e as escolhas do console.
        with self._sessions() as session:
            # Quanto já foi gasto neste mês.
            spent_before = month_spend_usd(session)
            # Modelo e esforço em vigor (console; se nunca mudou, o .env).
            config = current(session, self._settings)
        # Modelo pedido por quem chamou ou, se não pediu, o escolhido no console.
        model = model or config.llm_model
        # Esforço pedido por quem chamou ou, se não pediu, o escolhido no console.
        effort = effort or config.llm_effort
        # Se já atingiu o teto, nem chama a API.
        if spent_before >= budget:
            # f"..." é uma f-string: insere valores no texto; ":.2f" mostra 2 casas.
            raise BudgetExceededError(
                f"Teto mensal de US$ {budget} atingido (gasto: US$ {spent_before:.2f})."
            )

        # Monta os parâmetros da chamada num dicionário.
        params: dict = {
            # Qual modelo.
            "model": model,
            # Tamanho máximo da resposta.
            "max_tokens": max_tokens,
            # A conversa: aqui, uma única mensagem sua (texto ou blocos).
            "messages": [{"role": "user", "content": prompt}],
            # Nível de esforço do modelo.
            "output_config": {"effort": effort},
        }
        # Só inclui a instrução de sistema se ela existir.
        if system:
            # Adiciona a chave "system" ao dicionário.
            params["system"] = system
        # Liga o fallback do servidor apenas nos modelos que o aceitam.
        if model in _SERVER_FALLBACK_MODELS:
            # Pede o recurso beta.
            params["betas"] = [_SERVER_FALLBACK_BETA]
            # Usa a cadeia de fallback padrão definida pela Anthropic.
            params["fallbacks"] = "default"

        # Tenta chamar a API.
        try:
            # "**params" espalha o dicionário como argumentos com nome.
            response = self._client.beta.messages.create(**params)
        # Qualquer erro da API (rede, chave inválida, limite) cai aqui.
        except anthropic.APIError as exc:
            # Registra a falha no banco para você ver depois no histórico.
            self._record(
                LLMCall(purpose=purpose, model=model, error=f"{type(exc).__name__}: {exc}")
            )
            # "raise" sem nada repassa o mesmo erro para quem chamou.
            raise

        # Calcula o custo com o modelo que realmente respondeu.
        cost = estimate_cost(response.model, response.usage)
        # A resposta vem em blocos; junta só os blocos de texto numa string.
        text = "".join(block.text for block in response.content if block.type == "text")
        # Grava a chamada com todos os números de uso.
        self._record(
            LLMCall(
                # Para que serviu.
                purpose=purpose,
                # Modelo que respondeu.
                model=response.model,
                # Tokens de entrada.
                input_tokens=response.usage.input_tokens,
                # Tokens de saída.
                output_tokens=response.usage.output_tokens,
                # Tokens lidos do cache.
                cache_read_tokens=response.usage.cache_read_input_tokens or 0,
                # Tokens gravados no cache.
                cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
                # Custo calculado.
                cost_usd=cost,
                # Motivo da parada.
                stop_reason=response.stop_reason,
                # Id da requisição na Anthropic.
                request_id=response._request_id,
            ),
            # Gasto antes e depois desta chamada, para saber se cruzou a linha de alerta.
            alert_if_crossing=(spent_before, spent_before + cost),
        )
        # Devolve o resultado num objeto simples para quem chamou.
        return LLMResult(
            # Texto da resposta.
            text=text,
            # Modelo que respondeu.
            model=response.model,
            # Motivo da parada.
            stop_reason=response.stop_reason,
            # Custo.
            cost_usd=cost,
            # Id da requisição.
            request_id=response._request_id,
        )

    # Grava uma chamada no banco e, se for o caso, registra o alerta de orçamento.
    def _record(
        self, call: LLMCall, alert_if_crossing: tuple[Decimal, Decimal] | None = None
    ) -> None:
        # Abre uma sessão própria para a gravação.
        with self._sessions() as session:
            # Prepara a gravação da chamada.
            session.add(call)
            # Só verifica o alerta quando recebeu os valores antes/depois.
            if alert_if_crossing:
                # Desempacota a tupla em duas variáveis.
                before, after = alert_if_crossing
                # Linha de alerta: teto x 0.8 (US$ 8, com o teto de US$ 10).
                threshold = (
                    self._settings.llm_monthly_budget_usd * self._settings.llm_budget_alert_ratio
                )
                # Alerta só na chamada que cruzou a linha, não em todas depois dela.
                if before < threshold <= after:
                    # Escreve o aviso no log do servidor.
                    log.warning("Gasto com LLM passou de %s do teto mensal", threshold)
                    # Registra o alerta no log de auditoria, visível no console.
                    audit(
                        session,
                        # Quem fez.
                        "sistema",
                        # O que aconteceu.
                        "alerta_orcamento_llm",
                        # Detalhes em texto (JSON não aceita Decimal direto).
                        gasto_usd=str(after),
                        teto_usd=str(self._settings.llm_monthly_budget_usd),
                    )
            # Grava tudo de uma vez no banco.
            session.commit()
