"""Login no Google (OAuth) e guarda segura dos tokens.

O token de atualização (refresh token) permite ler o Gmail sem novo login. Ele é
guardado criptografado. Para não expirar a cada 7 dias, o app OAuth precisa estar
"Em produção" no Google Cloud (ver docs/setup/02-google-oauth.md).
"""

import os
from datetime import UTC

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import GOOGLE_SCOPES, Settings
from app.models import OAuthToken, audit
from app.security import TokenCipher

PROVIDER = "google"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Na tela de consentimento a pessoa pode desmarcar permissões. Sem esta variável,
# a biblioteca falha com erro genérico; com ela, conferimos os escopos nós mesmos.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class MissingScopesError(RuntimeError):
    pass


def _flow(settings: Settings, **kwargs) -> Flow:
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": _TOKEN_URI,
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=settings.google_redirect_uri,
        **kwargs,
    )


def start_login(settings: Settings) -> tuple[str, str, str]:
    """Devolve (url de login, state, code_verifier). Os dois últimos vão para a sessão."""
    flow = _flow(settings)
    url, state = flow.authorization_url(
        access_type="offline",
        # Força o Google a devolver o refresh token mesmo em logins repetidos.
        prompt="consent",
        include_granted_scopes="false",
    )
    return url, state, flow.code_verifier


def finish_login(
    session: Session,
    cipher: TokenCipher,
    settings: Settings,
    *,
    authorization_response: str,
    state: str,
    code_verifier: str,
) -> str:
    """Troca o código pelo token, confere escopos, guarda criptografado. Devolve o e-mail."""
    flow = _flow(settings, state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials

    missing = set(GOOGLE_SCOPES) - set(creds.granted_scopes or creds.scopes or [])
    if missing:
        raise MissingScopesError(
            "Permissões não concedidas no Google: " + ", ".join(sorted(missing))
        )

    email = (
        build("oauth2", "v2", credentials=creds, cache_discovery=False)
        .userinfo()
        .get()
        .execute()["email"]
    )
    _save(session, cipher, email, creds)
    audit(session, email, "login_google", target=email, escopos=sorted(GOOGLE_SCOPES))
    session.commit()
    return email


def _save(session: Session, cipher: TokenCipher, email: str, creds: Credentials) -> None:
    row = session.scalar(
        select(OAuthToken).where(OAuthToken.provider == PROVIDER, OAuthToken.account_email == email)
    )
    if row is None:
        if not creds.refresh_token:
            raise RuntimeError("O Google não devolveu refresh token. Refaça o login.")
        row = OAuthToken(provider=PROVIDER, account_email=email)
        session.add(row)
    if creds.refresh_token:
        row.refresh_token_enc = cipher.encrypt(creds.refresh_token)
    row.scopes = sorted(creds.granted_scopes or creds.scopes or GOOGLE_SCOPES)
    row.access_token_enc = cipher.encrypt(creds.token) if creds.token else None
    # A biblioteca do Google usa datetime sem fuso, em UTC.
    row.access_token_expires_at = creds.expiry.replace(tzinfo=UTC) if creds.expiry else None


def load_credentials(
    session: Session, cipher: TokenCipher, settings: Settings, email: str | None = None
) -> Credentials:
    """Credenciais prontas para usar nas APIs do Google, renovando o acesso se preciso."""
    query = select(OAuthToken).where(OAuthToken.provider == PROVIDER)
    if email:
        query = query.where(OAuthToken.account_email == email)
    row = session.scalars(query.order_by(OAuthToken.updated_at.desc())).first()
    if row is None:
        raise RuntimeError("Nenhuma conta Google conectada. Faça login pelo console.")

    creds = Credentials(
        token=cipher.decrypt(row.access_token_enc) if row.access_token_enc else None,
        refresh_token=cipher.decrypt(row.refresh_token_enc),
        token_uri=_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=row.scopes,
        expiry=row.access_token_expires_at.astimezone(UTC).replace(tzinfo=None)
        if row.access_token_expires_at
        else None,
    )
    if not creds.valid:
        creds.refresh(GoogleRequest())
        _save(session, cipher, row.account_email, creds)
        session.commit()
    return creds
