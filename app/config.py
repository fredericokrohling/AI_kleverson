"""Configuração lida do ambiente (arquivo .env no servidor)."""

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Escopos mínimos. gmail.send fica de fora de propósito: o sistema não consegue
# enviar e-mail, nem com bug. Rascunhos usam gmail.compose.
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Preço em dólares por milhão de tokens: (entrada, saída, leitura de cache).
# Valores de referência de out/2026; confira em https://www.anthropic.com/pricing.
# Haiku: sem preço de leitura de cache aqui, então usa o de entrada (estimativa por cima).
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00"), Decimal("0.20")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00"), Decimal("0.20")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.10")),
}
# Gravar no cache custa 1,25x o preço de entrada.
CACHE_WRITE_MULTIPLIER = Decimal("1.25")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://assistente:teste@localhost:5432/assistente"

    token_encryption_key: str = ""
    session_secret: str = "troque-em-producao"
    allowed_tailscale_login: str = ""

    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_monthly_budget_usd: Decimal = Decimal("10")
    # Fração do teto que dispara o alerta no log de auditoria.
    llm_budget_alert_ratio: Decimal = Decimal("0.8")

    google_client_id: str = ""
    google_client_secret: str = ""
    public_base_url: str = "http://localhost:8000"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.public_base_url}/auth/google/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
