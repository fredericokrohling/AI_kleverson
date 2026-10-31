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
    """Lê os e-mails novos do Gmail e grava no bronze, com os anexos."""
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


# Varredura das pastas monitoradas do Drive (uma rodada).
def cmd_coletar_drive(_: argparse.Namespace) -> None:
    """Varre agora as pastas do Drive cadastradas no console e grava as versões novas."""
    # A rodada de varredura.
    from app.ingestion.drive_sync import run_from_settings

    # Roda e mostra o resumo.
    print(run_from_settings(get_settings()).describe())


# Testes de saúde (uma rodada), com o resultado no terminal.
def cmd_verificar_saude(_: argparse.Namespace) -> None:
    """Testa agora os serviços (banco, Google, Claude, Deepgram, disco, status públicos)."""
    # Os testes.
    from app.health import LABELS, run_checks

    # Roda e grava.
    rows = run_checks(get_sessionmaker(), get_settings())
    # Uma linha por serviço.
    for row in rows:
        # OK, PROBLEMA ou ? (não deu para saber).
        mark = {True: "OK      ", False: "PROBLEMA", None: "?       "}[row.ok]
        # Mostra.
        print(f"{mark} {LABELS.get(row.service, row.service)}: {row.detail}")


# Lê agora, com o Claude, as imagens e PDFs digitalizados que estão na fila.
def cmd_ler_imagens(_: argparse.Namespace) -> None:
    # Import local, como nos outros comandos.
    from app.ingestion.vision_queue import run_from_settings

    # Uma rodada.
    summary = run_from_settings(get_settings())
    # Mostra o resumo, ou por que não rodou.
    print(summary.describe() if summary else "Nada a ler (fila vazia, sem chave ou teto do mês).")


# O serviço "coletor": um laço que dispara cada tarefa no seu intervalo.
def cmd_agendador(_: argparse.Namespace) -> None:
    """Roda sem parar: Gmail, Drive, leitura pelo Claude e saúde, cada um no seu intervalo."""
    # logging mostra o andamento em `docker compose logs coletor`.
    import logging

    # time.sleep espera entre uma volta e outra.
    import time

    # Data e hora com fuso, e intervalos.
    from datetime import UTC, datetime, timedelta

    # Testes de saúde.
    from app.health import run_checks

    # Varredura do Drive e a regra de "está na hora?".
    from app.ingestion.drive_sync import drive_scan_due

    # Rodada do Drive (renomeada para não confundir com a do Gmail).
    from app.ingestion.drive_sync import run_from_settings as run_drive

    # Coleta do Gmail.
    from app.ingestion.gmail import run_from_settings as run_gmail

    # Rodada de leitura de imagens e PDFs digitalizados pelo Claude.
    from app.ingestion.vision_queue import run_from_settings as run_vision

    # Formato das mensagens: hora, nível e texto.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Configuração.
    settings = get_settings()
    # Fábrica de sessões.
    sessions = get_sessionmaker()
    # Última vez que cada tarefa rodou (None = ainda não rodou desde que o serviço subiu).
    last: dict[str, datetime | None] = {
        "gmail": None,
        "drive_falha": None,
        "visao": None,
        "saude": None,
    }

    # Diz se já passou o intervalo desde a última vez.
    def due(name: str, minutes: int, now: datetime) -> bool:
        # Nunca rodou ou passou o intervalo.
        return last[name] is None or now - last[name] >= timedelta(minutes=minutes)

    # Roda uma tarefa protegida: erro numa tarefa não derruba o serviço nem as outras.
    def safe(label: str, job) -> bool:
        # Tenta.
        try:
            # Executa.
            result = job()
            # Mostra o resumo, se a tarefa devolver um.
            if hasattr(result, "describe"):
                # Uma linha no log.
                logging.info(result.describe())
            # Deu certo.
            return True
        # Qualquer erro.
        except Exception:
            # Rastro completo no log.
            logging.exception("%s falhou", label)
            # Falhou.
            return False

    # Laço sem fim: uma volta por minuto.
    while True:
        # Momento desta volta.
        now = datetime.now(UTC)
        # Gmail a cada GMAIL_POLL_MINUTES.
        if due("gmail", settings.gmail_poll_minutes, now):
            # Marca antes de rodar: com erro, também espera o intervalo para tentar de novo.
            last["gmail"] = now
            # Coleta.
            safe("Coleta do Gmail", lambda: run_gmail(settings))
        # Drive: quando alguma pasta está atrasada ou acabou de ser cadastrada.
        with sessions() as session:
            # Pergunta ao banco.
            drive_due = drive_scan_due(session, settings, now)
        # Depois de uma falha geral (ex.: Google desconectado), espera 5 min para insistir.
        if drive_due and due("drive_falha", 5, now):
            # Varre; se falhar, guarda o momento da falha.
            if not safe("Varredura do Drive", lambda: run_drive(settings)):
                # Momento da falha.
                last["drive_falha"] = now
        # Leitura pelo Claude a cada 5 minutos (depois da coleta, para pegar o que acabou
        # de chegar). Sem chave, com o teto atingido ou com a fila vazia, não faz nada.
        if due("visao", 5, now):
            # Marca.
            last["visao"] = now
            # Lê a fila.
            safe("Leitura pelo Claude", lambda: run_vision(settings))
        # Saúde a cada HEALTH_POLL_MINUTES.
        if due("saude", settings.health_poll_minutes, now):
            # Marca.
            last["saude"] = now
            # Testa e grava.
            safe("Testes de saúde", lambda: run_checks(sessions, settings))
        # Espera um minuto até a próxima volta.
        time.sleep(60)


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
    # python -m app.cli coletar-drive
    "coletar-drive": cmd_coletar_drive,
    # python -m app.cli ler-imagens
    "ler-imagens": cmd_ler_imagens,
    # python -m app.cli verificar-saude
    "verificar-saude": cmd_verificar_saude,
    # python -m app.cli agendador (o serviço "coletor" do Docker Compose roda este)
    "agendador": cmd_agendador,
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
