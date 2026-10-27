"""Criptografia dos tokens do Google e verificação de quem acessa o console."""

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

from app.config import get_settings

# O `tailscale serve` repassa o login de quem fez a requisição neste cabeçalho.
TAILSCALE_LOGIN_HEADER = "Tailscale-User-Login"


class TokenCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY não configurada. Gere com: python -m app.cli gerar-chave"
            )
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as exc:
            raise RuntimeError(
                "Não foi possível descriptografar o token. A TOKEN_ENCRYPTION_KEY mudou?"
            ) from exc


def generate_key() -> str:
    return Fernet.generate_key().decode()


def get_cipher() -> TokenCipher:
    return TokenCipher(get_settings().token_encryption_key)


def require_owner(request: Request) -> str:
    """Dependência do FastAPI: só o login Tailscale autorizado passa.

    O app escuta apenas em 127.0.0.1 no servidor; o único caminho de fora é o
    `tailscale serve`, que identifica o usuário. Sem login configurado, nada passa.
    """
    allowed = get_settings().allowed_tailscale_login.strip().lower()
    login = request.headers.get(TAILSCALE_LOGIN_HEADER, "").strip().lower()
    if not allowed or login != allowed:
        raise HTTPException(status_code=403, detail="Acesso negado")
    return login
