"""Configuração lida do ambiente (arquivo .env no servidor).

Por que assim: senhas e chaves nunca ficam no código (que vai para o GitHub).
Elas ficam no arquivo .env, que só existe no servidor. Este módulo lê esse arquivo
e entrega os valores já com o tipo certo (texto, número decimal etc.).
"""

# Decimal é um tipo de número exato, sem os erros de arredondamento do float.
# Usamos para dinheiro: 0.1 + 0.2 em float dá 0.30000000000000004; em Decimal dá 0.3.
from decimal import Decimal

# lru_cache guarda o resultado de uma função: chamadas seguintes devolvem o mesmo objeto.
from functools import lru_cache

# BaseSettings (biblioteca pydantic-settings) lê variáveis de ambiente e do arquivo .env
# e valida cada uma contra o tipo declarado na classe.
from pydantic_settings import BaseSettings, SettingsConfigDict

# Permissões (escopos) que pedimos ao Google no login. Só o mínimo necessário.
# gmail.send fica de fora de propósito: o sistema não consegue enviar e-mail, nem com bug.
GOOGLE_SCOPES = [
    # Identifica a pessoa que fez login (padrão OpenID Connect).
    "openid",
    # Permite ler o endereço de e-mail da conta conectada.
    "https://www.googleapis.com/auth/userinfo.email",
    # Ler e-mails e anexos, sem poder alterar nada.
    "https://www.googleapis.com/auth/gmail.readonly",
    # Criar rascunhos. Enviar continua sendo uma ação manual sua no Gmail.
    "https://www.googleapis.com/auth/gmail.compose",
    # Ler e criar eventos na agenda.
    "https://www.googleapis.com/auth/calendar.events",
    # Ler arquivos do Drive (Docs, Sheets, PDFs anexados por link).
    "https://www.googleapis.com/auth/drive.readonly",
]

# Preço em dólares por milhão de tokens: (entrada, saída, leitura de cache).
# Token é o pedaço de texto que o modelo processa (mais ou menos 3/4 de uma palavra).
# Valores de referência de out/2026; confira em https://www.anthropic.com/pricing.
# Haiku: sem preço de leitura de cache aqui, então usa o de entrada (estimativa por cima).
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    # Opus 5.5: o mais capaz da linha atual, e o mais caro.
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00"), Decimal("0.20")),
    # Sonnet 5.5: intermediário, cerca de metade do preço do Opus.
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00"), Decimal("0.20")),
    # Haiku 5.5: o mais barato, cerca de 40 vezes menos que o Opus.
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.10")),
}
# Gravar um trecho no cache do modelo custa 1,25x o preço normal de entrada;
# ler depois do cache sai bem mais barato. Usado no cálculo de custo.
CACHE_WRITE_MULTIPLIER = Decimal("1.25")


# Cada atributo desta classe vira uma configuração. O nome em maiúsculas no .env
# (ex.: DATABASE_URL) preenche o atributo de mesmo nome em minúsculas (database_url).
# O valor depois do "=" é o padrão, usado quando a variável não existe no .env.
class Settings(BaseSettings):
    # Diz onde está o arquivo .env e manda ignorar variáveis que a classe não conhece.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Endereço do banco: driver+usuário:senha@servidor:porta/nome_do_banco.
    database_url: str = "postgresql+psycopg://assistente:teste@localhost:5432/assistente"

    # Chave que criptografa os tokens do Google guardados no banco.
    token_encryption_key: str = ""
    # Chave que assina o cookie do console, para ninguém forjar uma sessão.
    session_secret: str = "troque-em-producao"
    # O único login Tailscale que pode acessar o console.
    allowed_tailscale_login: str = ""

    # Chave da API da Anthropic (cobrança separada da assinatura Claude Pro).
    anthropic_api_key: str = ""
    # Qual modelo do Claude usar.
    llm_model: str = "claude-opus-5-5"
    # Teto de gasto mensal com o Claude, em dólares.
    llm_monthly_budget_usd: Decimal = Decimal("10")
    # Fração do teto que dispara o alerta no log de auditoria (0.8 = 80%).
    llm_budget_alert_ratio: Decimal = Decimal("0.8")

    # Identificação do app no Google Cloud (criada no guia 02-google-oauth.md).
    google_client_id: str = ""
    # Senha do app no Google Cloud; fica só no .env.
    google_client_secret: str = ""
    # Endereço do console na rede Tailscale; o Google devolve a pessoa para cá após o login.
    public_base_url: str = "http://localhost:8000"

    # @property transforma o método num atributo calculado: settings.google_redirect_uri.
    @property
    def google_redirect_uri(self) -> str:
        # Endereço para onde o Google redireciona depois do login. Precisa ser idêntico
        # ao cadastrado no Google Cloud, senão o Google recusa.
        return f"{self.public_base_url}/auth/google/callback"


# Com @lru_cache, o .env é lido uma vez só; todo o sistema compartilha o mesmo objeto.
@lru_cache
def get_settings() -> Settings:
    # Cria o objeto: neste momento o pydantic lê o .env e valida os tipos.
    return Settings()
