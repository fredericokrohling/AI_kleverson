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
# Nenhuma permissão de escrita no Gmail: gmail.send, gmail.compose e gmail.modify ficam de
# fora de propósito. As duas últimas também permitem enviar e-mail (o Google descreve a
# gmail.compose como "gerenciar rascunhos e enviar e-mails"), então sem elas o sistema
# não consegue enviar nada, nem com bug.
GOOGLE_SCOPES = [
    # Identifica a pessoa que fez login (padrão OpenID Connect).
    "openid",
    # Permite ler o endereço de e-mail da conta conectada.
    "https://www.googleapis.com/auth/userinfo.email",
    # Ler e-mails e anexos, sem poder alterar nada.
    "https://www.googleapis.com/auth/gmail.readonly",
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
    # Qual modelo do Claude usar (padrão; pode ser trocado pelo console).
    llm_model: str = "claude-opus-5-5"
    # Esforço padrão do Claude: low, medium, high, xhigh ou max (também trocável no console).
    llm_effort: str = "medium"
    # Teto de gasto mensal com o Claude, em dólares.
    llm_monthly_budget_usd: Decimal = Decimal("10")
    # Fração do teto que dispara o alerta no log de auditoria (0.8 = 80%).
    llm_budget_alert_ratio: Decimal = Decimal("0.8")

    # Transcrição em tempo real (bloco 1).
    # Qual serviço transcreve o áudio. Hoje só "deepgram"; a troca fica isolada em
    # app/transcription/providers.py.
    transcription_provider: str = "deepgram"
    # Chave da API do Deepgram (console.deepgram.com).
    deepgram_api_key: str = ""
    # Modelo de transcrição do Deepgram; nova-3 é o mais recente com português.
    deepgram_model: str = "nova-3"
    # Idioma falado nas reuniões.
    transcription_language: str = "pt-BR"

    # Coleta de e-mail (bloco 2).
    # Pasta onde ficam os arquivos originais (e-mails .eml e anexos). No Docker é um
    # volume, para sobreviver a atualizações do contêiner.
    data_dir: str = "/dados"
    # Filtro de busca do Gmail, na mesma sintaxe da caixa de pesquisa. Por padrão tira
    # promoções, redes sociais e rascunhos, que só gastariam IA no bloco 3.
    gmail_query: str = "-category:promotions -category:social -in:drafts"
    # Na primeira coleta (banco vazio), quantos dias para trás importar. 0 = começa a
    # partir do momento da instalação, sem trazer o histórico.
    gmail_initial_days: int = 0
    # Intervalo entre coletas no modo contínuo, em minutos.
    gmail_poll_minutes: int = 5
    # Folga, em minutos, que cada coleta volta antes do fim da anterior. Cobre e-mails
    # que o Gmail indexa com atraso; os repetidos são ignorados pelo id.
    gmail_overlap_minutes: int = 60
    # Tamanho máximo, em MB, de um arquivo baixado do Drive (anexos do Gmail já vêm
    # limitados a 25 MB pelo próprio Gmail).
    drive_max_file_mb: int = 25
    # Intervalo entre varreduras das pastas monitoradas do Drive, em minutos. Uma pasta
    # recém-cadastrada é varrida na rodada seguinte, sem esperar este intervalo.
    drive_poll_minutes: int = 30

    # Saúde (página do coração).
    # Intervalo entre testes de saúde automáticos, em minutos.
    health_poll_minutes: int = 10
    # Onde o .git do repositório aparece dentro do contêiner (montado só para leitura),
    # para mostrar qual versão está instalada.
    git_dir: str = "/repo-git"
    # Repositório no GitHub, para conferir se há versão nova.
    github_repo: str = "fredericokrohling/AI_kleverson"

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
