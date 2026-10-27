"""Testes de criptografia dos tokens e do controle de acesso."""

# Biblioteca de testes.
import pytest

# Erro HTTP que require_owner levanta ao negar acesso.
from fastapi import HTTPException

# Request do Starlette (base do FastAPI), para montar requisições falsas.
from starlette.requests import Request

# O que estamos testando.
from app.security import TokenCipher, generate_key, require_owner


# Monta uma requisição mínima com os cabeçalhos informados.
def _request(headers: dict[str, str]) -> Request:
    # O formato interno exige nomes em minúsculas e tudo em bytes.
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    # "scope" é o dicionário que descreve a requisição no padrão ASGI.
    return Request({"type": "http", "headers": raw})


# Criptografar e descriptografar devolve o texto original.
def test_cipher_roundtrip():
    # Cifra com uma chave nova.
    cipher = TokenCipher(generate_key())
    # Um token de exemplo.
    secret = "1//refresh-token-exemplo"
    # Criptografa.
    encrypted = cipher.encrypt(secret)
    # O texto original não aparece nos bytes criptografados.
    assert secret.encode() not in encrypted
    # Descriptografar devolve o original.
    assert cipher.decrypt(encrypted) == secret


# Com a chave errada, a mensagem de erro aponta a causa provável.
def test_cipher_with_other_key_fails_clearly():
    # Criptografa com uma chave...
    encrypted = TokenCipher(generate_key()).encrypt("segredo")
    # match confere que a mensagem cita a variável de configuração.
    with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
        # ...e tenta abrir com outra.
        TokenCipher(generate_key()).decrypt(encrypted)


# Sem chave configurada, o sistema se recusa a criar a cifra.
def test_cipher_requires_key():
    # Espera o erro com a mensagem "não configurada".
    with pytest.raises(RuntimeError, match="não configurada"):
        # Chave vazia.
        TokenCipher("")


# Maiúsculas e minúsculas no e-mail não importam.
def test_owner_is_allowed_case_insensitive():
    # O login com maiúsculas passa e volta em minúsculas.
    assert (
        require_owner(_request({"Tailscale-User-Login": "Dona@Exemplo.com"})) == "dona@exemplo.com"
    )


# parametrize roda o mesmo teste com cada valor da lista: sem cabeçalho e com outro login.
@pytest.mark.parametrize("headers", [{}, {"Tailscale-User-Login": "outra@exemplo.com"}])
def test_anyone_else_is_denied(headers):
    # "as exc" guarda o erro para inspecionar depois.
    with pytest.raises(HTTPException) as exc:
        # Tenta passar.
        require_owner(_request(headers))
    # Confere que foi 403.
    assert exc.value.status_code == 403


# Se ninguém foi configurado, ninguém entra (falha fechada).
def test_nobody_passes_without_configured_login(monkeypatch):
    # Import local da configuração.
    from app.config import get_settings

    # monkeypatch troca um valor só durante este teste e desfaz no fim.
    monkeypatch.setattr(get_settings(), "allowed_tailscale_login", "")
    # Mesmo com cabeçalho vazio igual ao configurado vazio, é negado.
    with pytest.raises(HTTPException):
        # Tenta passar.
        require_owner(_request({"Tailscale-User-Login": ""}))
