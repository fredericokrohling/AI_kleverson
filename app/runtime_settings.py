"""Configurações que você troca pelo console, sem mexer no .env nem reiniciar nada.

Por que no banco e não no .env: o .env só é lido quando o contêiner sobe; trocar o
modelo do Claude ali exigiria entrar no servidor, editar o arquivo e reiniciar. No banco,
a troca vale na próxima chamada, e cada mudança fica registrada na auditoria.

O .env continua valendo como padrão: enquanto você não mudar nada pelo console, o valor
é o do .env (LLM_MODEL, LLM_EFFORT, TRANSCRIPTION_LANGUAGE). Senhas e chaves continuam
só no .env; aqui ficam apenas escolhas sem segredo.
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Session é a conversa com o banco.
from sqlalchemy.orm import Session

# Configuração do .env (os padrões).
from app.config import Settings

# Tabelas usadas.
from app.models import AppSetting, audit

# Modelos do Claude oferecidos no console: id da API → descrição para você escolher.
# Os preços ficam em app/config.py (MODEL_PRICES_USD_PER_MTOK).
LLM_MODELS: dict[str, str] = {
    # O mais capaz; padrão.
    "claude-opus-5-5": "Opus 5.5: o mais capaz (padrão)",
    # Metade do preço do Opus.
    "claude-sonnet-5-5": "Sonnet 5.5: equilíbrio entre qualidade e custo",
    # O mais barato e rápido.
    "claude-haiku-5-5": "Haiku 5.5: o mais rápido e barato, para tarefas simples",
}

# Níveis de esforço da API (output_config.effort): quanto o modelo pensa antes de
# responder. Mais esforço, respostas mais cuidadosas, mais tokens e mais demora.
EFFORTS: dict[str, str] = {
    # Respostas rápidas e curtas.
    "low": "Baixo",
    # Padrão do Opus 5.5 e do Haiku 5.5.
    "medium": "Médio (padrão)",
    # Para análises que exigem mais raciocínio.
    "high": "Alto",
    # Ainda mais raciocínio; caro.
    "xhigh": "Extra alto",
    # O máximo; reserve para casos muito difíceis.
    "max": "Máximo",
}

# Idiomas de transcrição (parâmetro language do Deepgram nova-3).
TRANSCRIPTION_LANGUAGES: dict[str, str] = {
    # Português do Brasil.
    "pt-BR": "Português (Brasil)",
    # Inglês.
    "en": "Inglês",
    # O Deepgram detecta sozinho e aceita troca de idioma no meio da fala.
    "multi": "Automático (português e inglês na mesma reunião)",
}

# Opções válidas de cada chave; qualquer outro valor é recusado.
_OPTIONS: dict[str, dict[str, str]] = {
    # Modelo.
    "llm_model": LLM_MODELS,
    # Esforço.
    "llm_effort": EFFORTS,
    # Idioma.
    "transcription_language": TRANSCRIPTION_LANGUAGES,
}


# Os valores em vigor, já combinando banco e .env.
@dataclass(frozen=True)
class RuntimeConfig:
    # Modelo do Claude.
    llm_model: str
    # Esforço do Claude.
    llm_effort: str
    # Idioma da transcrição.
    transcription_language: str


# Lê uma configuração: a do banco, se existir; senão, a do .env.
def get_value(session: Session, settings: Settings, key: str) -> str:
    """Valor em vigor de uma das chaves de _OPTIONS."""
    # Chave desconhecida: erro de programação, não de uso.
    if key not in _OPTIONS:
        raise KeyError(key)
    # Procura no banco pela chave primária.
    row = session.get(AppSetting, key)
    # Guardado no banco: vale ele.
    if row is not None:
        return str(row.value)
    # Senão, o padrão do .env (atributo de mesmo nome).
    return str(getattr(settings, key))


# Lê as três configurações de uma vez.
def current(session: Session, settings: Settings) -> RuntimeConfig:
    """Valores em vigor agora."""
    # Monta com cada valor.
    return RuntimeConfig(
        llm_model=get_value(session, settings, "llm_model"),
        llm_effort=get_value(session, settings, "llm_effort"),
        transcription_language=get_value(session, settings, "transcription_language"),
    )


# Grava uma configuração escolhida no console.
def set_value(session: Session, settings: Settings, key: str, value: str) -> None:
    """Valida e grava. Valor fora da lista vira ValueError (o console mostra o motivo)."""
    # Chave desconhecida.
    if key not in _OPTIONS:
        raise ValueError(f"Configuração desconhecida: {key}")
    # Valor fora das opções (ex.: formulário adulterado).
    if value not in _OPTIONS[key]:
        raise ValueError(f"Valor não permitido para {key}: {value}")
    # Valor anterior, para a auditoria.
    old = get_value(session, settings, key)
    # Mesmo valor: nada a fazer.
    if old == value:
        return
    # Linha existente, se houver.
    row = session.get(AppSetting, key)
    # Não existe: cria.
    if row is None:
        # Nova linha com o valor.
        session.add(AppSetting(key=key, value=value))
    # Existe: atualiza.
    else:
        # Troca o valor.
        row.value = value
    # Registra quem mudou o quê (o commit é de quem chamou).
    audit(session, "dona", "config.alterar", target=key, de=old, para=value)
