"""Comandos de linha de comando.

Uso no servidor:  docker compose run --rm app python -m app.cli <comando>

Por que uma linha de comando além do console: algumas tarefas acontecem antes de o
console existir (gerar a chave de criptografia) ou servem para diagnóstico rápido
pelo terminal, sem abrir o navegador.
"""

# argparse lê os argumentos digitados no terminal e gera a ajuda (--help).
import argparse

# sys dá acesso ao encerramento do programa com código de saída.
import sys

# Leitura da configuração.
from app.config import get_settings

# Fábrica de sessões do banco.
from app.db import get_sessionmaker

# Gerar chave nova e obter o objeto de criptografia.
from app.security import generate_key, get_cipher


# Cada comando é uma função que recebe os argumentos já lidos.
# "_" como nome indica que o argumento existe mas não é usado aqui.
def cmd_gerar_chave(_: argparse.Namespace) -> None:
    """Gera uma TOKEN_ENCRYPTION_KEY nova para o .env."""
    # Mostra a chave no terminal para você copiar para o .env.
    print(generate_key())


# Teste ponta a ponta do Claude: chave, chamada, registro de custo.
def cmd_testar_llm(_: argparse.Namespace) -> None:
    """Faz uma chamada mínima ao Claude e mostra custo e gasto do mês."""
    # Import dentro da função: só carrega a biblioteca da Anthropic se este comando rodar.
    from app.llm import LLMClient, month_spend_usd

    # Fábrica de sessões do banco.
    sessions = get_sessionmaker()
    # Faz a chamada mínima.
    result = LLMClient(sessions).generate(
        # Identificação no registro de custo.
        purpose="teste_cli",
        # A pergunta.
        prompt="Responda em uma frase curta, em português: você está funcionando?",
        # Limite de tamanho da resposta.
        max_tokens=2000,
        # Esforço mínimo, custo mínimo.
        effort="low",
    )
    # Mostra o texto; se vier vazio, mostra o motivo da parada.
    print(result.text or f"(sem texto; motivo de parada: {result.stop_reason})")
    # Abre uma sessão para somar o gasto do mês.
    with sessions() as session:
        # Soma já incluindo esta chamada.
        spent = month_spend_usd(session)
    # Resumo: modelo, custo desta chamada (6 casas) e total do mês (4 casas).
    print(f"Modelo {result.model} · custo US$ {result.cost_usd:.6f} · gasto no mês US$ {spent:.4f}")


# Teste do acesso ao Google com o token já gravado.
def cmd_testar_google(_: argparse.Namespace) -> None:
    """Confere se o token salvo ainda funciona: conta marcadores do Gmail e eventos futuros."""
    # Data e hora com fuso, para pedir "eventos a partir de agora".
    from datetime import UTC, datetime

    # Cria clientes das APIs do Google.
    from googleapiclient.discovery import build

    # Lê e renova as credenciais gravadas.
    from app.google_auth import load_credentials

    # Configuração.
    settings = get_settings()
    # get_sessionmaker() devolve a fábrica; o segundo () cria a sessão.
    with get_sessionmaker()() as session:
        # Credenciais válidas (renova se tiver expirado).
        creds = load_credentials(session, get_cipher(), settings)
    # Cliente da API do Gmail, versão 1.
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    # Lê o perfil da conta ("me" = a conta dona do token).
    profile = gmail.users().getProfile(userId="me").execute()
    # Mostra o e-mail e o total de mensagens: prova que a leitura funciona.
    print(f"Gmail: {profile['emailAddress']} · {profile['messagesTotal']} mensagens")
    # Cliente da API da Agenda, versão 3.
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    # Lista os próximos eventos.
    events = (
        # Recurso de eventos.
        calendar.events()
        # Monta a listagem.
        .list(
            # Agenda principal da conta.
            calendarId="primary",
            # A partir de agora, no formato que a API espera (ISO 8601).
            timeMin=datetime.now(UTC).isoformat(),
            # No máximo 5.
            maxResults=5,
            # Expande eventos recorrentes em ocorrências individuais.
            singleEvents=True,
            # Ordena pelo horário de início.
            orderBy="startTime",
        )
        # Executa a chamada.
        .execute()
        # Pega a lista de eventos; [] se não houver nenhum.
        .get("items", [])
    )
    # Quantos eventos vieram.
    print(f"Agenda: {len(events)} próximos eventos")
    # Mostra cada evento.
    for event in events:
        # Eventos com hora têm "dateTime"; eventos de dia inteiro têm só "date".
        start = event["start"].get("dateTime", event["start"].get("date"))
        # Início e título (ou um texto padrão se não tiver título).
        print(f"  {start}  {event.get('summary', '(sem título)')}")


# Coleta do Gmail: uma rodada, ou sem parar (modo usado pelo serviço "coletor").
def cmd_coletar_gmail(args: argparse.Namespace) -> None:
    """Lê os e-mails novos do Gmail e grava no bronze (com anexos e links do Drive)."""
    # logging mostra o andamento no terminal e em `docker compose logs coletor`.
    import logging

    # time.sleep espera entre uma rodada e outra.
    import time

    # Data e hora com fuso.
    from datetime import UTC, datetime

    # A rodada de coleta.
    from app.ingestion.gmail import run_from_settings

    # Formato das mensagens: hora, nível e texto.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Configuração.
    settings = get_settings()
    # --desde AAAA-MM-DD: transforma o texto em data e hora UTC (meia-noite daquele dia).
    since = datetime.fromisoformat(args.desde).replace(tzinfo=UTC) if args.desde else None
    # Repete enquanto estiver no modo contínuo.
    while True:
        # Uma rodada.
        try:
            # Roda e mostra o resumo.
            print(run_from_settings(settings, since=since).describe(), flush=True)
        # Erro na rodada inteira (ex.: Google não conectado, banco fora do ar).
        except Exception as exc:
            # Sem modo contínuo, o erro aparece completo e o comando termina com falha.
            if not args.continuo:
                raise
            # No modo contínuo, registra e tenta de novo na próxima rodada.
            logging.exception("Coleta falhou: %s", exc)
        # Uma rodada só: termina.
        if not args.continuo:
            break
        # A data inicial só vale para a primeira rodada; as seguintes continuam sozinhas.
        since = None
        # Espera o intervalo configurado.
        time.sleep(settings.gmail_poll_minutes * 60)


# Extrai de novo o texto dos arquivos que falharam (útil depois de melhorar um extrator).
def cmd_reextrair_anexos(_: argparse.Namespace) -> None:
    """Tenta extrair de novo o texto dos anexos com erro, a partir dos originais guardados."""
    # A função que faz o trabalho.
    from app.ingestion.gmail import reextract_attachments

    # A pasta dos originais.
    from app.ingestion.storage import FileStore

    # Roda e recebe as contagens.
    tried, fixed = reextract_attachments(get_sessionmaker(), FileStore(get_settings().data_dir))
    # Mostra o resultado.
    print(f"{tried} arquivos tentados, {fixed} agora com texto.")


# Liga o nome digitado no terminal à função que executa o comando.
COMMANDS = {
    # python -m app.cli gerar-chave
    "gerar-chave": cmd_gerar_chave,
    # python -m app.cli testar-llm
    "testar-llm": cmd_testar_llm,
    # python -m app.cli testar-google
    "testar-google": cmd_testar_google,
    # python -m app.cli coletar-gmail [--desde AAAA-MM-DD] [--continuo]
    "coletar-gmail": cmd_coletar_gmail,
    # python -m app.cli reextrair-anexos
    "reextrair-anexos": cmd_reextrair_anexos,
}


# Ponto de entrada. argv permite passar argumentos nos testes; None = lê do terminal.
def main(argv: list[str] | None = None) -> int:
    # Cria o leitor de argumentos; prog é o nome mostrado na ajuda.
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    # Subcomandos (gerar-chave, testar-llm...); required=True exige que um seja escolhido.
    sub = parser.add_subparsers(dest="command", required=True)
    # Registra cada comando do dicionário COMMANDS.
    for name, func in COMMANDS.items():
        # Cria o subcomando; a ajuda vem da docstring da função.
        command = sub.add_parser(name, help=func.__doc__)
        # Guarda qual função rodar.
        command.set_defaults(func=func)
        # Opções que só o coletar-gmail aceita.
        if name == "coletar-gmail":
            # Data inicial da janela, para importar um período específico.
            command.add_argument("--desde", help="importa e-mails a partir desta data (AAAA-MM-DD)")
            # Modo contínuo: repete a cada GMAIL_POLL_MINUTES.
            command.add_argument("--continuo", action="store_true", help="repete sem parar")
    # Lê o que foi digitado.
    args = parser.parse_args(argv)
    # Executa a função do comando escolhido.
    args.func(args)
    # 0 = terminou sem erro (convenção de terminal).
    return 0


# Só roda quando o arquivo é executado diretamente (python -m app.cli),
# e não quando é importado por outro módulo.
if __name__ == "__main__":
    # Encerra o programa com o código devolvido por main().
    sys.exit(main())
