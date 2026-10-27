"""Comandos de linha de comando.

Uso no servidor:  docker compose run --rm app python -m app.cli <comando>
"""

import argparse
import sys

from app.config import get_settings
from app.db import get_sessionmaker
from app.security import generate_key, get_cipher


def cmd_gerar_chave(_: argparse.Namespace) -> None:
    """Gera uma TOKEN_ENCRYPTION_KEY nova para o .env."""
    print(generate_key())


def cmd_testar_llm(_: argparse.Namespace) -> None:
    """Faz uma chamada mínima ao Claude e mostra custo e gasto do mês."""
    from app.llm import LLMClient, month_spend_usd

    sessions = get_sessionmaker()
    result = LLMClient(sessions).generate(
        purpose="teste_cli",
        prompt="Responda em uma frase curta, em português: você está funcionando?",
        max_tokens=2000,
        effort="low",
    )
    print(result.text or f"(sem texto; motivo de parada: {result.stop_reason})")
    with sessions() as session:
        spent = month_spend_usd(session)
    print(f"Modelo {result.model} · custo US$ {result.cost_usd:.6f} · gasto no mês US$ {spent:.4f}")


def cmd_testar_google(_: argparse.Namespace) -> None:
    """Confere se o token salvo ainda funciona: conta marcadores do Gmail e eventos futuros."""
    from datetime import UTC, datetime

    from googleapiclient.discovery import build

    from app.google_auth import load_credentials

    settings = get_settings()
    with get_sessionmaker()() as session:
        creds = load_credentials(session, get_cipher(), settings)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = gmail.users().getProfile(userId="me").execute()
    print(f"Gmail: {profile['emailAddress']} · {profile['messagesTotal']} mensagens")
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    events = (
        calendar.events()
        .list(
            calendarId="primary",
            timeMin=datetime.now(UTC).isoformat(),
            maxResults=5,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    print(f"Agenda: {len(events)} próximos eventos")
    for event in events:
        start = event["start"].get("dateTime", event["start"].get("date"))
        print(f"  {start}  {event.get('summary', '(sem título)')}")


COMMANDS = {
    "gerar-chave": cmd_gerar_chave,
    "testar-llm": cmd_testar_llm,
    "testar-google": cmd_testar_google,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in COMMANDS.items():
        sub.add_parser(name, help=func.__doc__).set_defaults(func=func)
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
