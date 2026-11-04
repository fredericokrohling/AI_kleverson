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

    # Consultas.
    from sqlalchemy import select

    # Lê e renova as credenciais gravadas.
    from app.google_auth import load_credentials

    # Tabelas das contas e das Empresas.
    from app.models import Company, OAuthToken

    # Configuração.
    settings = get_settings()
    # get_sessionmaker() devolve a fábrica; o segundo () cria a sessão.
    with get_sessionmaker()() as session:
        # Contas conectadas, com a Empresa de cada uma.
        rows = session.execute(
            select(OAuthToken.company_id, Company.name).join(
                Company, Company.id == OAuthToken.company_id
            )
        ).all()
        # Nenhuma.
        if not rows:
            print("Nenhuma conta Google conectada.")
        # Cada conta.
        for company_id, company_name in rows:
            # Cabeçalho com a Empresa.
            print(f"Empresa {company_name}:")
            # Credenciais válidas da conta desta Empresa (renova se tiver expirado).
            creds = load_credentials(session, get_cipher(), settings, company_id=company_id)
            # Testa Gmail e Agenda desta conta.
            _print_google(creds, datetime.now(UTC))


# Mostra o perfil do Gmail e os próximos eventos de uma conta.
def _print_google(creds, now) -> None:
    # Cria clientes das APIs do Google.
    from googleapiclient.discovery import build

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
            timeMin=now.isoformat(),
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
            # Uma linha por conta (uma conta por Empresa).
            for summary in run_from_settings(settings, since=since):
                # Resumo da rodada.
                print(summary.describe(), flush=True)
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
    # Uma linha por Empresa.
    for summary in run_from_settings(get_settings()):
        # Resumo da varredura.
        print(summary.describe())


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


# Recupera as imagens dos e-mails já coletados que o filtro antigo descartou.
def cmd_recuperar_imagens_emails(_: argparse.Namespace) -> None:
    # Import local, como nos outros comandos.
    from app.ingestion.gmail import recover_email_images

    # Guarda dos originais.
    from app.ingestion.storage import FileStore

    # Recupera a partir dos .eml guardados; a fila do Claude lê nas próximas rodadas.
    total = recover_email_images(get_sessionmaker(), FileStore(get_settings().data_dir))
    # Mostra quantas entraram na fila.
    print(f"{total} imagem(ns) de e-mail recuperada(s) e posta(s) na fila do Claude.")


# Procura imagens dentro dos PowerPoint e Word que chegaram antes desta versão.
def cmd_extrair_imagens_embutidas(_: argparse.Namespace) -> None:
    # Import local, como nos outros comandos.
    from app.ingestion.embedded import backfill

    # Guarda dos originais.
    from app.ingestion.storage import FileStore

    # Registra as imagens; a fila do Claude lê nas próximas rodadas.
    total = backfill(get_sessionmaker(), FileStore(get_settings().data_dir))
    # Mostra quantas entraram na fila.
    print(f"{total} imagem(ns) embutida(s) na fila do Claude.")


# Cria as falas das transcrições gravadas antes desta versão e reconhece quem falou.
def cmd_extrair_falas(_: argparse.Namespace) -> None:
    """Uma linha por fala nas transcrições antigas, ligada às pessoas cadastradas."""
    # Gravação das falas e reconhecimento.
    from app.people import backfill_utterances, relink_utterances

    # Uma sessão.
    with get_sessionmaker()() as session:
        # Transcrições que ganharam falas.
        done = backfill_utterances(session)
        # Falas ligadas a pessoas.
        linked = relink_utterances(session)
        # Grava.
        session.commit()
    # Mostra.
    print(f"{done} transcrição(ões) com falas novas; {linked} fala(s) ligada(s) a pessoas.")


# Aplica as regras de tag dos projetos a todos os e-mails e reuniões já coletados.
def cmd_reaplicar_tags(_: argparse.Namespace) -> None:
    """Regras de tag (nome no texto, equipe) em tudo; só acrescenta, nunca tira."""
    # Regras.
    from app.project_tags import apply_rules_to_all

    # Uma sessão.
    with get_sessionmaker()() as session:
        # Aplica.
        added = apply_rules_to_all(session)
        # Grava.
        session.commit()
    # Mostra.
    print(f"{added} tag(s) nova(s).")


# Classifica por projeto o que estiver pendente (o coletor já faz isso a cada 5 minutos).
def cmd_classificar(args: argparse.Namespace) -> None:
    """Classifica cada item por projeto: regras primeiro, Claude só no que sobrar."""
    # Rodada, limpeza e cliente do Claude.
    from app.classification import ClassifySummary, reset, run_pending

    # Cliente do Claude e gasto do mês.
    from app.llm import LLMClient, month_spend_usd

    # Configuração.
    settings = get_settings()
    # Fábrica de sessões.
    sessions = get_sessionmaker()
    # --refazer: apaga as decisões automáticas antes (as escolhidas por você ficam).
    if args.refazer:
        # Uma sessão.
        with sessions() as session:
            # Apaga.
            removed = reset(session)
            # Grava.
            session.commit()
        # Mostra.
        print(f"{removed} decisão(ões) automática(s) apagada(s); refazendo.")
    # Teto do mês.
    with sessions() as session:
        # Já atingido?
        over = month_spend_usd(session) >= settings.llm_monthly_budget_usd
    # Cliente do Claude (sem chave ou com teto: só as regras).
    llm = LLMClient(sessions, settings) if settings.anthropic_api_key and not over else None
    # Totais de todas as rodadas.
    total = ClassifySummary()
    # Rodadas até não sobrar nada que o Claude possa decidir agora.
    while True:
        # Uma rodada (até CLASSIFY_LLM_PER_RUN chamadas ao Claude).
        summary = run_pending(sessions, settings, llm)
        # Soma os decididos pelas regras.
        total.by_rules += summary.by_rules
        # Os decididos pelo Claude.
        total.by_claude += summary.by_claude
        # Os que foram para a fila "A revisar".
        total.to_review += summary.to_review
        # Os revistos sem mudança.
        total.kept += summary.kept
        # O teto bateu em alguma rodada.
        total.budget_hit = total.budget_hit or summary.budget_hit
        # O que ficou esperando nesta rodada.
        total.waiting = summary.waiting
        # Para quando o Claude não decidiu nada novo (fila vazia, sem chave ou teto).
        if summary.by_claude + summary.to_review == 0 or summary.budget_hit:
            break
    # Mostra.
    print(total.describe())


# Extrai os fatos (tarefa, decisão, risco...) do que estiver pendente (o coletor também faz).
def cmd_extrair_fatos(args: argparse.Namespace) -> None:
    """Extrai tarefas, decisões, riscos e prazos dos itens com projeto, com trecho conferido."""
    # Rodada, limpeza e resumo.
    from app.facts import ExtractSummary, reset, run_pending

    # Cliente do Claude e gasto do mês.
    from app.llm import LLMClient, month_spend_usd

    # Configuração.
    settings = get_settings()
    # Fábrica de sessões.
    sessions = get_sessionmaker()
    # --refazer: apaga os fatos e os registros antes (todos os itens são lidos de novo).
    if args.refazer:
        # Uma sessão.
        with sessions() as session:
            # Apaga.
            removed = reset(session)
            # Grava.
            session.commit()
        # Mostra.
        print(f"{removed} item(ns) voltaram para a fila; lendo de novo.")
    # Teto do mês.
    with sessions() as session:
        # Já atingido?
        over = month_spend_usd(session) >= settings.llm_monthly_budget_usd
    # Cliente do Claude (sem chave ou com teto: nada é lido).
    llm = LLMClient(sessions, settings) if settings.anthropic_api_key and not over else None
    # Totais de todas as rodadas.
    total = ExtractSummary()
    # Rodadas até não sobrar nada que o Claude possa ler agora.
    while True:
        # Uma rodada (até FACTS_LLM_PER_RUN itens).
        summary = run_pending(sessions, settings, llm)
        # Soma os itens lidos.
        total.items += summary.items
        # Os fatos.
        total.facts += summary.facts
        # Os conferidos.
        total.verified += summary.verified
        # Os registrados sem chamada.
        total.skipped += summary.skipped
        # O teto bateu em alguma rodada.
        total.budget_hit = total.budget_hit or summary.budget_hit
        # O que ficou esperando nesta rodada.
        total.waiting = summary.waiting
        # Para quando nada novo foi lido (fila vazia, sem chave ou teto).
        if summary.items + summary.skipped == 0 or summary.budget_hit:
            break
    # Mostra.
    print(total.describe())


# Aplica as regras de alerta de liderança agora (o coletor também faz, a cada 5 minutos).
def cmd_gerar_alertas(_: argparse.Namespace) -> None:
    """Aplica as regras de alerta aos fatos (sem custo de IA) e mostra quantos nasceram."""
    # Rodada dos alertas.
    from app.alerts import generate

    # Configuração.
    settings = get_settings()
    # Uma sessão.
    with get_sessionmaker()() as session:
        # Aplica todas as regras ligadas.
        summary = generate(session, settings)
        # Grava.
        session.commit()
    # Mostra.
    print(summary.describe())


# Monta o briefing de hoje agora (o coletor também monta, a partir de BRIEFING_HOUR).
def cmd_gerar_briefing(args: argparse.Namespace) -> None:
    """Monta o briefing de hoje das Empresas que ainda não têm (--refazer: de todas)."""
    # Data e hora (o "hoje" no fuso do console).
    from datetime import UTC, datetime

    # Consulta ao banco.
    from sqlalchemy import select

    # Hoje no fuso do console.
    from app.alerts import today_local

    # Montagem do briefing.
    from app.briefing import BriefingSummary, generate

    # Lista das Empresas.
    from app.companies import list_companies

    # Tabela, para saber quais já têm o de hoje.
    from app.models import Briefing

    # Configuração.
    settings = get_settings()
    # Resumo.
    summary = BriefingSummary()
    # Uma sessão.
    with get_sessionmaker()() as session:
        # Hoje.
        today = today_local(settings, datetime.now(UTC))
        # Empresas que já têm o de hoje.
        done = set(session.scalars(select(Briefing.company_id).where(Briefing.day == today)))
        # Cada Empresa.
        for company in list_companies(session):
            # Já tem e não pediu para refazer: pula.
            if company.id in done and not args.refazer:
                continue
            # Monta (a hora do coletor não importa: foi você quem pediu).
            generate(session, settings, company.id, trigger="manual", actor="terminal")
            # Conta.
            summary.generated.append(company.name)
        # Grava.
        session.commit()
    # Mostra.
    print(summary.describe())


# O serviço "coletor": um laço que dispara cada tarefa no seu intervalo.
def cmd_agendador(_: argparse.Namespace) -> None:
    """Roda sem parar: Gmail, Drive, leitura, classificação, fatos, alertas, briefing,
    saúde e servidor."""
    # logging mostra o andamento em `docker compose logs coletor`.
    import logging

    # time.sleep espera entre uma volta e outra.
    import time

    # Data e hora com fuso, e intervalos.
    from datetime import UTC, datetime, timedelta

    # Rodada dos alertas de liderança (bloco 3).
    from app.alerts import run_from_settings as run_alerts

    # Briefing diário (bloco 5).
    from app.briefing import run_from_settings as run_briefing

    # Rodada de classificação por projeto (bloco 3).
    from app.classification import run_from_settings as run_classify

    # Rodada de extração de fatos (bloco 3).
    from app.facts import run_from_settings as run_facts

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

    # Medição do servidor (processador, memória) a cada minuto, numa thread à parte.
    from app.server_metrics import start_thread as start_metrics

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
        "classificacao": None,
        "fatos": None,
        "alertas": None,
        "briefing": None,
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
            # Tarefas por Empresa devolvem uma lista de resumos; as outras, um só.
            for item in result if isinstance(result, list) else [result]:
                # Mostra o resumo, se houver.
                if hasattr(item, "describe"):
                    # Linha no log.
                    logging.info(item.describe())
            # Deu certo.
            return True
        # Qualquer erro.
        except Exception:
            # Rastro completo no log.
            logging.exception("%s falhou", label)
            # Falhou.
            return False

    # Liga a medição do servidor. Ela roda em paralelo, no seu próprio ritmo de 1 minuto,
    # para não ficar sem medir enquanto uma coleta longa ocupa este laço.
    start_metrics(sessions)
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
        # Classificação por projeto a cada 5 minutos, depois da leitura (um anexo lido
        # agora já ajuda). Sem nada pendente, não faz nada nem escreve no log.
        if due("classificacao", 5, now):
            # Marca.
            last["classificacao"] = now
            # Classifica.
            safe("Classificação por projeto", lambda: run_classify(settings))
        # Extração de fatos a cada 5 minutos, depois da classificação (só lê itens que já
        # têm projeto). Sem nada pendente, não faz nada nem escreve no log.
        if due("fatos", 5, now):
            # Marca.
            last["fatos"] = now
            # Extrai.
            safe("Extração de fatos", lambda: run_facts(settings))
        # Alertas a cada 5 minutos, depois dos fatos (um pedido lido agora já avisa). Só
        # consultas SQL, sem custo; sem alerta novo, não escreve no log.
        if due("alertas", 5, now):
            # Marca.
            last["alertas"] = now
            # Aplica as regras.
            safe("Alertas de liderança", lambda: run_alerts(settings))
        # Briefing a cada 5 minutos, depois dos alertas (o das 7h já sai com os alertas
        # da madrugada). Só monta quando deu a hora e a Empresa ainda não tem o de hoje.
        if due("briefing", 5, now):
            # Marca.
            last["briefing"] = now
            # Monta o que falta.
            safe("Briefing diário", lambda: run_briefing(settings))
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
    # python -m app.cli recuperar-imagens-emails
    "recuperar-imagens-emails": cmd_recuperar_imagens_emails,
    # python -m app.cli extrair-imagens-embutidas
    "extrair-imagens-embutidas": cmd_extrair_imagens_embutidas,
    # python -m app.cli extrair-falas
    "extrair-falas": cmd_extrair_falas,
    # python -m app.cli reaplicar-tags
    "reaplicar-tags": cmd_reaplicar_tags,
    # python -m app.cli classificar [--refazer]
    "classificar": cmd_classificar,
    # python -m app.cli extrair-fatos [--refazer]
    "extrair-fatos": cmd_extrair_fatos,
    # python -m app.cli gerar-alertas
    "gerar-alertas": cmd_gerar_alertas,
    # python -m app.cli gerar-briefing [--refazer]
    "gerar-briefing": cmd_gerar_briefing,
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
        # Opção do classificar: apaga as decisões automáticas e refaz.
        if name == "classificar":
            # As escolhidas por você no console não são apagadas.
            command.add_argument(
                "--refazer",
                action="store_true",
                help="refaz as decisões automáticas (as suas ficam)",
            )
        # Opção do extrair-fatos: apaga os fatos e lê tudo de novo.
        if name == "extrair-fatos":
            # Custa de novo: cada item volta a ir ao Claude.
            command.add_argument(
                "--refazer",
                action="store_true",
                help="apaga os fatos e lê todos os itens de novo (custa de novo)",
            )
        # Opção do gerar-briefing: monta de novo também o das Empresas que já têm.
        if name == "gerar-briefing":
            # Substitui a foto de hoje pelos dados de agora.
            command.add_argument(
                "--refazer",
                action="store_true",
                help="monta de novo também o das Empresas que já têm o de hoje",
            )
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
