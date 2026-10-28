"""Testes do console web: acesso, páginas e login do Google."""

# TestClient simula um navegador chamando o app, sem subir servidor.
from fastapi.testclient import TestClient

# Erro que o banco devolve quando uma regra de "não repetir" é violada.
from sqlalchemy.exc import IntegrityError

# O app que vamos testar.
from app.main import app

# Tabela bronze e tipos de fonte.
from app.models import BronzeItem, SourceKind

# Cabeçalho que o Tailscale enviaria para a dona do sistema.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Um cliente compartilhado pelos testes deste arquivo.
client = TestClient(app)


# A rota de saúde não exige login (o Docker precisa acessá-la).
def test_health_is_open():
    # assert: se a condição for falsa, o teste falha.
    assert client.get("/saude").json() == {"status": "ok"}


# Sem o login certo, o console recusa.
def test_console_requires_owner():
    # Sem cabeçalho nenhum: 403.
    assert client.get("/").status_code == 403
    # Com outro login: também 403.
    assert (
        client.get("/", headers={"Tailscale-User-Login": "intruso@exemplo.com"}).status_code == 403
    )


# Com o login certo, a página abre.
def test_console_renders_for_owner():
    # Pede a página inicial como dona.
    response = client.get("/", headers=OWNER)
    # 200 = sucesso.
    assert response.status_code == 200
    # Banco vazio: a página avisa que não há conta conectada.
    assert "Nenhuma conta conectada" in response.text
    # Mostra o modelo configurado.
    assert "claude-opus-5-5" in response.text


# O botão de login manda para o Google com os parâmetros certos.
def test_google_login_redirects_to_google():
    # follow_redirects=False: queremos ver o redirecionamento, não segui-lo.
    response = client.get("/auth/google/login", headers=OWNER, follow_redirects=False)
    # 307 = redirecionamento temporário.
    assert response.status_code == 307
    # Endereço de destino.
    location = response.headers["location"]
    # Vai para a página de login do Google.
    assert location.startswith("https://accounts.google.com/")
    # Pede acesso offline (refresh token).
    assert "access_type=offline" in location
    # Nunca pede permissão de enviar e-mail.
    assert "gmail.send" not in location
    # Usa PKCE.
    assert "code_challenge=" in location


# Retorno do Google com state desconhecido é recusado (proteção contra login forjado).
def test_google_callback_rejects_unknown_state():
    # Simula um retorno com state inventado.
    response = client.get("/auth/google/callback?state=falso&code=x", headers=OWNER)
    # 400 = requisição inválida.
    assert response.status_code == 400


# Gravar o mesmo item bruto duas vezes é bloqueado pelo banco.
def test_bronze_is_idempotent(sessions):
    # Import local: só este teste usa pytest.raises aqui.
    import pytest

    # Abre uma sessão.
    with sessions() as s:
        # Primeiro item.
        s.add(BronzeItem(source=SourceKind.GMAIL, external_id="m1", content_sha256="a"))
        # Grava com sucesso.
        s.commit()
        # Mesmo item de novo (mesma fonte e mesmo id externo).
        s.add(BronzeItem(source=SourceKind.GMAIL, external_id="m1", content_sha256="a"))
        # pytest.raises: o teste passa só se o erro esperado acontecer.
        with pytest.raises(IntegrityError):
            # A gravação falha por causa da regra única.
            s.commit()
