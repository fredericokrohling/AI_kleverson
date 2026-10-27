from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models import BronzeItem, SourceKind

OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
client = TestClient(app)


def test_health_is_open():
    assert client.get("/saude").json() == {"status": "ok"}


def test_console_requires_owner():
    assert client.get("/").status_code == 403
    assert (
        client.get("/", headers={"Tailscale-User-Login": "intruso@exemplo.com"}).status_code == 403
    )


def test_console_renders_for_owner():
    response = client.get("/", headers=OWNER)
    assert response.status_code == 200
    assert "Nenhuma conta conectada" in response.text
    assert "claude-opus-5-5" in response.text


def test_google_login_redirects_to_google():
    response = client.get("/auth/google/login", headers=OWNER, follow_redirects=False)
    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/")
    assert "access_type=offline" in location
    assert "gmail.send" not in location
    assert "code_challenge=" in location


def test_google_callback_rejects_unknown_state():
    response = client.get("/auth/google/callback?state=falso&code=x", headers=OWNER)
    assert response.status_code == 400


def test_bronze_is_idempotent(sessions):
    import pytest

    with sessions() as s:
        s.add(BronzeItem(source=SourceKind.GMAIL, external_id="m1", content_sha256="a"))
        s.commit()
        s.add(BronzeItem(source=SourceKind.GMAIL, external_id="m1", content_sha256="a"))
        with pytest.raises(IntegrityError):
            s.commit()
