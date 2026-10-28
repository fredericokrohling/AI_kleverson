"""Login no Google (OAuth) e guarda segura dos tokens.

O token de atualização (refresh token) permite ler o Gmail sem novo login. Ele é
guardado criptografado. Para não expirar a cada 7 dias, o app OAuth precisa estar
"Em produção" no Google Cloud (ver docs/setup/02-google-oauth.md).

Por que OAuth e não senha: com OAuth você autoriza o sistema no próprio site do
Google, escolhe as permissões e pode revogar a qualquer momento em
myaccount.google.com/permissions. O sistema nunca vê sua senha.

Por que PKCE (o "code_verifier"): é uma senha de uso único criada a cada login.
Mesmo que alguém intercepte o código que o Google devolve, sem o verifier ele não
consegue trocá-lo por um token.
"""

# os dá acesso às variáveis de ambiente do sistema operacional.
import os

# Fuso horário UTC, referência para guardar datas.
from datetime import UTC

# Objeto que a biblioteca do Google usa para fazer requisições HTTP (ao renovar token).
from google.auth.transport.requests import Request as GoogleRequest

# Credentials: objeto com os tokens que as APIs do Google aceitam.
from google.oauth2.credentials import Credentials

# Flow conduz o passo a passo do login OAuth (gerar URL, trocar código por token).
from google_auth_oauthlib.flow import Flow

# build cria um cliente para uma API do Google (Gmail, Agenda, userinfo...).
from googleapiclient.discovery import build

# select monta consultas ao banco.
from sqlalchemy import select

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Permissões pedidas e o tipo da configuração.
from app.config import GOOGLE_SCOPES, Settings

# Tabela onde os tokens ficam e a função de auditoria.
from app.models import OAuthToken, audit

# Classe que criptografa e descriptografa.
from app.security import TokenCipher

# Nome do provedor gravado na tabela oauth_tokens.
PROVIDER = "google"
# Endereço do Google que troca códigos por tokens e renova tokens.
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Na tela de consentimento a pessoa pode desmarcar permissões. Sem esta variável,
# a biblioteca falha com erro genérico; com ela, conferimos os escopos nós mesmos.
# setdefault só define o valor se a variável ainda não existir.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


# Erro específico para "você desmarcou alguma permissão no Google".
class MissingScopesError(RuntimeError):
    # Nada a acrescentar além do nome.
    pass


# Monta o objeto Flow com os dados do app cadastrado no Google Cloud.
# **kwargs repassa argumentos extras (state, code_verifier) quando existirem.
def _flow(settings: Settings, **kwargs) -> Flow:
    # Mesmo formato do JSON que o Google Cloud oferece para download.
    client_config = {
        # "web" = aplicação web (com endereço de retorno).
        "web": {
            # Identificação do app.
            "client_id": settings.google_client_id,
            # Senha do app.
            "client_secret": settings.google_client_secret,
            # Página de login do Google.
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            # Endereço que troca o código pelo token.
            "token_uri": _TOKEN_URI,
        }
    }
    # Cria o Flow a partir da configuração.
    return Flow.from_client_config(
        # Dados do app.
        client_config,
        # Permissões que vamos pedir.
        scopes=GOOGLE_SCOPES,
        # Para onde o Google devolve a pessoa depois do login.
        redirect_uri=settings.google_redirect_uri,
        # Argumentos extras, se houver.
        **kwargs,
    )


# Primeira metade do login: gera o endereço da página de consentimento do Google.
def start_login(settings: Settings) -> tuple[str, str, str]:
    """Devolve (url de login, state, code_verifier). Os dois últimos vão para a sessão."""
    # Cria um Flow novo; ele gera sozinho um code_verifier aleatório (PKCE).
    flow = _flow(settings)
    # Gera a URL e o "state" (valor aleatório que protege contra login forjado).
    url, state = flow.authorization_url(
        # "offline" pede o refresh token, para acessar sem você estar presente.
        access_type="offline",
        # Força o Google a devolver o refresh token mesmo em logins repetidos.
        prompt="consent",
        # Não herda permissões de logins antigos; pede exatamente a lista atual.
        include_granted_scopes="false",
    )
    # Devolve os três valores; quem chama guarda state e verifier na sessão do navegador.
    return url, state, flow.code_verifier


# Segunda metade do login: o Google devolveu a pessoa com um código na URL.
def finish_login(
    # Sessão do banco.
    session: Session,
    # Criptografia dos tokens.
    cipher: TokenCipher,
    # Configuração.
    settings: Settings,
    # Daqui em diante, argumentos só por nome.
    *,
    # A URL completa de retorno (contém o código).
    authorization_response: str,
    # O state guardado no início, para conferência.
    state: str,
    # O code_verifier guardado no início (PKCE).
    code_verifier: str,
) -> str:
    """Troca o código pelo token, confere escopos, guarda criptografado. Devolve o e-mail."""
    # Recria o Flow com o mesmo state e verifier do início do login.
    flow = _flow(settings, state=state, code_verifier=code_verifier)
    # Vai ao Google trocar o código pelos tokens.
    flow.fetch_token(authorization_response=authorization_response)
    # Pega as credenciais resultantes.
    creds = flow.credentials

    # Diferença entre conjuntos: permissões pedidas que não vieram.
    missing = set(GOOGLE_SCOPES) - set(creds.granted_scopes or creds.scopes or [])
    # Se faltou alguma, para e explica qual.
    if missing:
        # sorted ordena; ", ".join junta tudo num texto separado por vírgulas.
        raise MissingScopesError(
            "Permissões não concedidas no Google: " + ", ".join(sorted(missing))
        )

    # Descobre qual conta fez login, perguntando à API userinfo do Google.
    email = (
        # Cria o cliente da API oauth2; cache_discovery=False evita aviso inútil no log.
        build("oauth2", "v2", credentials=creds, cache_discovery=False)
        # Escolhe o recurso userinfo.
        .userinfo()
        # Prepara a leitura.
        .get()
        # Executa a chamada e pega o campo "email" da resposta.
        .execute()["email"]
    )
    # Grava os tokens criptografados.
    _save(session, cipher, email, creds)
    # Registra o login no log de auditoria.
    audit(session, email, "login_google", target=email, escopos=sorted(GOOGLE_SCOPES))
    # Confirma a gravação no banco.
    session.commit()
    # Devolve o e-mail conectado.
    return email


# Grava ou atualiza os tokens de uma conta. O "_" indica uso interno deste módulo.
def _save(session: Session, cipher: TokenCipher, email: str, creds: Credentials) -> None:
    # Procura se essa conta já está gravada.
    row = session.scalar(
        select(OAuthToken).where(OAuthToken.provider == PROVIDER, OAuthToken.account_email == email)
    )
    # Conta nova.
    if row is None:
        # Sem refresh token não dá para acessar depois; melhor falhar agora.
        if not creds.refresh_token:
            raise RuntimeError("O Google não devolveu refresh token. Refaça o login.")
        # Cria a linha nova.
        row = OAuthToken(provider=PROVIDER, account_email=email)
        # Prepara a inserção.
        session.add(row)
    # O Google só manda refresh token em alguns casos; só sobrescreve se veio um.
    if creds.refresh_token:
        # Criptografa antes de gravar.
        row.refresh_token_enc = cipher.encrypt(creds.refresh_token)
    # Guarda as permissões concedidas, em ordem alfabética.
    row.scopes = sorted(creds.granted_scopes or creds.scopes or GOOGLE_SCOPES)
    # Criptografa o access token, se houver; senão grava vazio.
    row.access_token_enc = cipher.encrypt(creds.token) if creds.token else None
    # A biblioteca do Google usa datetime sem fuso, em UTC.
    # replace(tzinfo=UTC) marca explicitamente que é UTC antes de gravar no banco.
    row.access_token_expires_at = creds.expiry.replace(tzinfo=UTC) if creds.expiry else None


# Usado pelos coletores (Gmail, Agenda) para obter credenciais válidas.
def load_credentials(
    session: Session, cipher: TokenCipher, settings: Settings, email: str | None = None
) -> Credentials:
    """Credenciais prontas para usar nas APIs do Google, renovando o acesso se preciso."""
    # Começa a consulta: tokens do Google.
    query = select(OAuthToken).where(OAuthToken.provider == PROVIDER)
    # Se pediram uma conta específica, filtra por ela.
    if email:
        # Acrescenta mais uma condição ao WHERE.
        query = query.where(OAuthToken.account_email == email)
    # Pega a mais recentemente atualizada (útil quando houver pessoal e corporativa).
    row = session.scalars(query.order_by(OAuthToken.updated_at.desc())).first()
    # Nenhuma conta conectada ainda.
    if row is None:
        raise RuntimeError("Nenhuma conta Google conectada. Faça login pelo console.")

    # Monta as credenciais descriptografando os tokens.
    creds = Credentials(
        # Access token, se houver.
        token=cipher.decrypt(row.access_token_enc) if row.access_token_enc else None,
        # Refresh token (sempre existe).
        refresh_token=cipher.decrypt(row.refresh_token_enc),
        # Onde renovar.
        token_uri=_TOKEN_URI,
        # Identificação do app (necessária para renovar).
        client_id=settings.google_client_id,
        # Senha do app (necessária para renovar).
        client_secret=settings.google_client_secret,
        # Permissões gravadas.
        scopes=row.scopes,
        # Validade: converte para UTC e tira o fuso, porque a biblioteca espera assim.
        expiry=row.access_token_expires_at.astimezone(UTC).replace(tzinfo=None)
        # Expressão condicional: só faz a conversão se houver data gravada.
        if row.access_token_expires_at
        else None,
    )
    # valid é False quando o access token expirou ou não existe.
    if not creds.valid:
        # Pede um access token novo ao Google usando o refresh token.
        creds.refresh(GoogleRequest())
        # Grava o token renovado (criptografado).
        _save(session, cipher, row.account_email, creds)
        # Confirma no banco.
        session.commit()
    # Credenciais prontas para usar.
    return creds
