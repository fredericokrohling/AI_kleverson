import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.security import TokenCipher, generate_key, require_owner


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


def test_cipher_roundtrip():
    cipher = TokenCipher(generate_key())
    secret = "1//refresh-token-exemplo"
    encrypted = cipher.encrypt(secret)
    assert secret.encode() not in encrypted
    assert cipher.decrypt(encrypted) == secret


def test_cipher_with_other_key_fails_clearly():
    encrypted = TokenCipher(generate_key()).encrypt("segredo")
    with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
        TokenCipher(generate_key()).decrypt(encrypted)


def test_cipher_requires_key():
    with pytest.raises(RuntimeError, match="não configurada"):
        TokenCipher("")


def test_owner_is_allowed_case_insensitive():
    assert (
        require_owner(_request({"Tailscale-User-Login": "Dona@Exemplo.com"})) == "dona@exemplo.com"
    )


@pytest.mark.parametrize("headers", [{}, {"Tailscale-User-Login": "outra@exemplo.com"}])
def test_anyone_else_is_denied(headers):
    with pytest.raises(HTTPException) as exc:
        require_owner(_request(headers))
    assert exc.value.status_code == 403


def test_nobody_passes_without_configured_login(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "allowed_tailscale_login", "")
    with pytest.raises(HTTPException):
        require_owner(_request({"Tailscale-User-Login": ""}))
