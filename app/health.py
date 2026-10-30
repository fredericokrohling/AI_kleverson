"""Saúde do sistema: testes periódicos dos serviços e versão instalada.

Dois tipos de teste:
- Funcionais: o próprio sistema usa o serviço do jeito que usa no dia a dia (consulta o
  banco, pergunta ao Gmail qual conta está conectada, valida a chave do Claude...). Se
  passar, está funcionando para nós, que é o que importa.
- Páginas de status públicas (Anthropic, GitHub, Deepgram, Tailscale): mostram se o
  fornecedor declarou um incidente. Servem de explicação quando um teste funcional
  falha ("o Claude está com incidente, não é problema nosso").

Google e AWS não publicam status num formato simples de consultar por programa; para
eles valem os testes funcionais (Gmail, Drive) e o fato de o console abrir (Lightsail).

Os testes rodam a cada HEALTH_POLL_MINUTES no serviço coletor e quando você aperta
"Testar agora". Cada resultado vira uma linha em health_checks; guardamos 7 dias.

Por que não há botão de reiniciar aqui: para reiniciar contêineres, o console precisaria
do "socket" do Docker, que dá poder de administrador sobre o servidor inteiro. Quem
invadisse o console (ou um bug nele) teria o servidor. Os comandos de reiniciar e
atualizar rodam no seu computador, por SSH (ver docs/setup/06-console.md).
"""

# json lê as respostas das páginas de status e do GitHub.
import json

# shutil.disk_usage informa o espaço do disco.
import shutil

# time.monotonic mede quanto um teste demorou.
import time

# urllib faz pedidos HTTP sem biblioteca extra; error traz os tipos de erro de rede.
import urllib.error

# request monta e envia os pedidos.
import urllib.request

# Callable: tipo "função", usado no registro de testes.
from collections.abc import Callable

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Data e hora com fuso, e intervalos de tempo.
from datetime import UTC, datetime, timedelta

# Path manipula caminhos de arquivos (leitura do .git).
from pathlib import Path

# Tipos para anotar funções.
from typing import Any

# Funções de consulta do SQLAlchemy.
from sqlalchemy import delete, func, select, text

# Session e a fábrica de sessões.
from sqlalchemy.orm import Session, sessionmaker

# Tipo da configuração.
from app.config import Settings

# Tabelas usadas.
from app.models import DriveFolder, HealthCheck, IngestionRun, SourceKind

# Tempo máximo de espera de cada teste de rede, em segundos.
_TIMEOUT = 8

# Quantos dias de histórico manter.
_KEEP_DAYS = 7

# Páginas de status no padrão Statuspage (/api/v2/status.json): serviço → endereço.
STATUS_PAGES: dict[str, str] = {
    # Anthropic (Claude).
    "status:anthropic": "https://status.claude.com/api/v2/status.json",
    # GitHub (código e atualizações).
    "status:github": "https://www.githubstatus.com/api/v2/status.json",
    # Deepgram (transcrição).
    "status:deepgram": "https://status.deepgram.com/api/v2/status.json",
    # Tailscale (acesso ao console).
    "status:tailscale": "https://status.tailscale.com/api/v2/status.json",
}

# Nome amigável de cada serviço, na ordem em que aparecem na página.
LABELS: dict[str, str] = {
    # Banco de dados.
    "banco": "Banco de dados",
    # Coleta do Gmail.
    "coletor": "Coletor de e-mail",
    # Varredura do Drive.
    "coleta_drive": "Monitor do Drive",
    # API do Gmail.
    "gmail": "Gmail (API)",
    # API do Drive.
    "drive": "Google Drive (API)",
    # API do Claude.
    "claude": "Claude (API)",
    # API do Deepgram.
    "deepgram": "Deepgram (API)",
    # Disco do servidor.
    "disco": "Disco do servidor",
    # Status declarado pelos fornecedores.
    "status:anthropic": "Anthropic (status público)",
    # GitHub.
    "status:github": "GitHub (status público)",
    # Deepgram.
    "status:deepgram": "Deepgram (status público)",
    # Tailscale.
    "status:tailscale": "Tailscale (status público)",
}


# Resultado de um teste: True = ok, False = problema, None = não deu para saber.
@dataclass(frozen=True)
class Result:
    # Situação.
    ok: bool | None
    # Explicação curta.
    detail: str


# Pedido HTTP GET que devolve o JSON da resposta.
def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    # Monta o pedido; User-Agent é exigido pela API do GitHub.
    request = urllib.request.Request(url, headers={"User-Agent": "kleverson-ai", **(headers or {})})
    # Abre a conexão com tempo máximo.
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        # Lê e converte o JSON.
        return json.loads(response.read())


# --- Testes individuais -------------------------------------------------------------


# Banco: um SELECT 1 responde?
def check_database(session: Session, settings: Settings, ctx: dict) -> Result:
    # A consulta mais simples possível.
    session.execute(text("SELECT 1"))
    # Chegou aqui: respondeu.
    return Result(True, "Respondendo.")


# Coletor de e-mail: a última rodada é recente?
def check_collector(session: Session, settings: Settings, ctx: dict) -> Result:
    # Última rodada do Gmail.
    run = session.scalars(
        select(IngestionRun)
        .where(IngestionRun.source == SourceKind.GMAIL)
        .order_by(IngestionRun.started_at.desc())
        .limit(1)
    ).first()
    # Nunca rodou.
    if run is None:
        return Result(False, "Nenhuma coleta registrada. O serviço coletor está no ar?")
    # Há quanto tempo, em minutos.
    age = (ctx["now"] - run.started_at).total_seconds() / 60
    # Mais de 3 intervalos sem rodar: provavelmente parado.
    if age > 3 * settings.gmail_poll_minutes:
        return Result(False, f"Última coleta há {age:.0f} min. O serviço coletor parou?")
    # Rodou recentemente, mas com erro.
    if run.status == "com_erros":
        return Result(False, f"Última coleta há {age:.0f} min, com erros (veja a página inicial).")
    # Tudo certo.
    return Result(True, f"Última coleta há {age:.0f} min ({run.status}).")


# Monitor do Drive: as pastas foram varridas recentemente e sem erro?
def check_drive_sync(session: Session, settings: Settings, ctx: dict) -> Result:
    # Pastas ativas.
    folders = session.scalars(select(DriveFolder).where(DriveFolder.active.is_(True))).all()
    # Nenhuma: nada a monitorar (não é problema).
    if not folders:
        return Result(True, "Nenhuma pasta cadastrada.")
    # Pastas com erro na última varredura.
    failed = [f.name for f in folders if f.last_error]
    # Alguma falhou.
    if failed:
        return Result(False, "Erro na varredura de: " + ", ".join(failed))
    # Varreduras mais antigas que 3 intervalos (ou nunca feitas).
    limit = ctx["now"] - timedelta(minutes=3 * settings.drive_poll_minutes)
    # Atrasadas.
    late = [f.name for f in folders if f.last_scan_at is None or f.last_scan_at < limit]
    # Alguma atrasada.
    if late:
        return Result(False, "Varredura atrasada em: " + ", ".join(late))
    # Tudo certo.
    return Result(True, f"{len(folders)} pasta(s) em dia.")


# Gmail: a API responde com as credenciais gravadas?
def check_gmail(session: Session, settings: Settings, ctx: dict) -> Result:
    # Cliente do Gmail (criado uma vez por rodada de testes).
    gmail = ctx["google"]("gmail", "v1")
    # getProfile é a chamada mais barata: devolve a conta.
    profile = gmail.users().getProfile(userId="me").execute()
    # Conta conectada.
    return Result(True, f"Conectado como {profile['emailAddress']}.")


# Drive: a API responde?
def check_drive(session: Session, settings: Settings, ctx: dict) -> Result:
    # Cliente do Drive.
    drive = ctx["google"]("drive", "v3")
    # about.get com o campo user: chamada barata.
    about = drive.about().get(fields="user(emailAddress)").execute()
    # Conta conectada.
    return Result(True, f"Conectado como {about['user']['emailAddress']}.")


# Claude: a chave vale e o modelo escolhido existe? (consulta gratuita, não gera texto)
def check_claude(session: Session, settings: Settings, ctx: dict) -> Result:
    # Import local: só carrega quando o teste roda.
    import anthropic

    # Modelo em vigor (console ou .env).
    from app.runtime_settings import get_value

    # Sem chave: nem tenta.
    if not settings.anthropic_api_key:
        return Result(False, "ANTHROPIC_API_KEY vazia no .env.")
    # Modelo escolhido.
    model = get_value(session, settings, "llm_model")
    # Cliente com tempo máximo curto.
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=_TIMEOUT)
    # Pede os dados do modelo: valida chave e nome sem custo.
    info = client.models.retrieve(model)
    # Respondeu.
    return Result(True, f"Chave válida; modelo {info.id}.")


# Deepgram: a chave vale? (lista de projetos: consulta gratuita)
def check_deepgram(session: Session, settings: Settings, ctx: dict) -> Result:
    # Sem chave: nem tenta.
    if not settings.deepgram_api_key:
        return Result(False, "DEEPGRAM_API_KEY vazia no .env.")
    # A chave vai no cabeçalho Authorization, no formato do Deepgram.
    _get_json(
        "https://api.deepgram.com/v1/projects",
        {"Authorization": f"Token {settings.deepgram_api_key}"},
    )
    # Respondeu sem erro.
    return Result(True, "Chave válida.")


# Disco: quanto do disco dos arquivos está ocupado?
def check_disk(session: Session, settings: Settings, ctx: dict) -> Result:
    # Pasta dos arquivos; se não existir (fora do Docker), a raiz.
    path = settings.data_dir if Path(settings.data_dir).exists() else "/"
    # Total, usado e livre, em bytes.
    usage = shutil.disk_usage(path)
    # Porcentagem ocupada.
    percent = usage.used / usage.total * 100
    # Livre em GB.
    free_gb = usage.free / 1024**3
    # Texto.
    detail = f"{percent:.0f}% ocupado, {free_gb:.1f} GB livres."
    # Acima de 85%: hora de agir (limpar ou aumentar o disco).
    return Result(percent < 85, detail)


# Página de status pública: o fornecedor declarou incidente?
def check_status_page(url: str) -> Callable[[Session, Settings, dict], Result]:
    """Cria um teste para uma página de status no padrão Statuspage."""

    # O teste em si.
    def run(session: Session, settings: Settings, ctx: dict) -> Result:
        # Tenta ler a página.
        try:
            # JSON com status.indicator e status.description.
            status = _get_json(url)["status"]
        # Página fora do ar ou bloqueada: não sabemos.
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            return Result(None, f"Página de status inacessível: {exc}")
        # none = tudo normal; minor = incidente leve; major/critical = grave.
        indicator = status.get("indicator")
        # Descrição em inglês do fornecedor (ex.: "All Systems Operational").
        description = status.get("description", "")
        # Grave: problema.
        if indicator in ("major", "critical"):
            return Result(False, description)
        # Normal ou leve: funcionando (o leve aparece no texto).
        return Result(True, description)

    # Devolve a função.
    return run


# Todos os testes, na ordem da página.
CHECKS: dict[str, Callable[[Session, Settings, dict], Result]] = {
    # Banco.
    "banco": check_database,
    # Coletor de e-mail.
    "coletor": check_collector,
    # Monitor do Drive.
    "coleta_drive": check_drive_sync,
    # Gmail.
    "gmail": check_gmail,
    # Drive.
    "drive": check_drive,
    # Claude.
    "claude": check_claude,
    # Deepgram.
    "deepgram": check_deepgram,
    # Disco.
    "disco": check_disk,
    # Uma página de status para cada fornecedor.
    **{name: check_status_page(url) for name, url in STATUS_PAGES.items()},
}


# Fábrica de clientes do Google para os testes, com as credenciais gravadas.
def _google_factory(session: Session, settings: Settings) -> Callable[[str, str], Any]:
    # Credenciais lidas uma vez só, na primeira vez que um teste pedir.
    cache: dict[str, Any] = {}

    # Cria um cliente da API pedida.
    def build_client(api: str, version: str) -> Any:
        # Imports locais: só carregam quando necessário.
        from googleapiclient.discovery import build

        # Leitura das credenciais.
        from app.google_auth import load_credentials

        # Criptografia.
        from app.security import get_cipher

        # Primeira vez: carrega as credenciais.
        if "creds" not in cache:
            # Lê e descriptografa (renova o acesso se preciso).
            cache["creds"] = load_credentials(session, get_cipher(), settings)
        # Cliente.
        return build(api, version, credentials=cache["creds"], cache_discovery=False)

    # Devolve a fábrica.
    return build_client


# Roda todos os testes e grava os resultados.
def run_checks(
    sessions: sessionmaker[Session],
    settings: Settings,
    *,
    # Testes a rodar; None = todos (os testes automáticos passam versões falsas).
    checks: dict[str, Callable[[Session, Settings, dict], Result]] | None = None,
    # Fábrica de clientes do Google; None = a real.
    google: Callable[[str, str], Any] | None = None,
    # "Agora"; os testes fixam um valor.
    now: datetime | None = None,
) -> list[HealthCheck]:
    """Roda os testes, grava em health_checks, apaga o histórico antigo e devolve as linhas."""
    # Momento de referência.
    now = now or datetime.now(UTC)
    # Linhas gravadas.
    rows: list[HealthCheck] = []
    # Uma sessão para tudo.
    with sessions() as session:
        # Contexto compartilhado pelos testes.
        ctx = {"now": now, "google": google or _google_factory(session, settings)}
        # Cada teste.
        for service, check in (checks or CHECKS).items():
            # Início da medição.
            started = time.monotonic()
            # Um teste que falha não impede os outros.
            try:
                # Roda.
                result = check(session, settings, ctx)
            # Qualquer erro vira "problema", com o motivo.
            except Exception as exc:
                # Desfaz algo pendente na sessão (ex.: consulta que falhou). As linhas de
                # resultado só entram na sessão no fim, então nada delas se perde aqui.
                session.rollback()
                # Tipo e mensagem do erro, curtos.
                result = Result(False, f"{type(exc).__name__}: {exc}"[:500])
            # Tempo em milissegundos.
            latency = round((time.monotonic() - started) * 1000)
            # Linha do resultado (ainda fora da sessão).
            rows.append(
                HealthCheck(
                    checked_at=now,
                    service=service,
                    ok=result.ok,
                    latency_ms=latency,
                    detail=result.detail[:1000],
                )
            )
        # Agora sim, prepara a gravação de todas as linhas.
        session.add_all(rows)
        # Apaga o histórico mais antigo que o limite.
        session.execute(
            delete(HealthCheck).where(HealthCheck.checked_at < now - timedelta(days=_KEEP_DAYS))
        )
        # Grava tudo.
        session.commit()
        # Carrega os valores antes de fechar a sessão (para usar fora dela).
        for row in rows:
            # Relê a linha do banco (id gerado).
            session.refresh(row)
        # Desliga as linhas da sessão.
        session.expunge_all()
    # Devolve.
    return rows


# O último resultado de cada serviço e o histórico curto, para a página.
def latest(session: Session, history: int = 24) -> list[dict[str, Any]]:
    """[{service, label, last, history}] na ordem de LABELS."""
    # Momento do teste mais recente de cada serviço.
    newest = (
        select(HealthCheck.service, func.max(HealthCheck.checked_at).label("m"))
        .group_by(HealthCheck.service)
        .subquery()
    )
    # As linhas correspondentes.
    last_rows = session.scalars(
        select(HealthCheck).join(
            newest,
            (HealthCheck.service == newest.c.service) & (HealthCheck.checked_at == newest.c.m),
        )
    ).all()
    # Por serviço.
    by_service = {row.service: row for row in last_rows}
    # Resultado.
    result = []
    # Na ordem da página (serviços sem teste ainda aparecem vazios).
    for service, label in LABELS.items():
        # Últimos N resultados, do mais antigo ao mais recente (para a faixa de bolinhas).
        recent = session.scalars(
            select(HealthCheck.ok)
            .where(HealthCheck.service == service)
            .order_by(HealthCheck.checked_at.desc())
            .limit(history)
        ).all()
        # Monta o item.
        result.append(
            {
                "service": service,
                "label": label,
                "last": by_service.get(service),
                "history": list(reversed(recent)),
            }
        )
    # Devolve.
    return result


# Momento do teste mais recente (de qualquer serviço).
def last_run_at(session: Session) -> datetime | None:
    # O maior checked_at.
    return session.scalar(select(func.max(HealthCheck.checked_at)))


# --- Versão instalada -----------------------------------------------------------------


# Lê o commit instalado a partir da pasta .git montada no contêiner (só leitura).
def installed_commit(git_dir: str) -> str | None:
    """SHA do commit em uso no servidor, ou None se o .git não estiver montado."""
    # Caminho da pasta.
    root = Path(git_dir)
    # Arquivo HEAD: diz em que ramo (ou commit) o repositório está.
    head_file = root / "HEAD"
    # Sem .git montado.
    if not head_file.is_file():
        return None
    # Conteúdo, ex.: "ref: refs/heads/main" ou um SHA direto.
    head = head_file.read_text().strip()
    # SHA direto (repositório "solto" num commit).
    if not head.startswith("ref: "):
        return head
    # Nome da referência (ex.: refs/heads/main).
    ref = head[5:]
    # Referência guardada como arquivo próprio.
    ref_file = root / ref
    # Existe: o SHA está nele.
    if ref_file.is_file():
        return ref_file.read_text().strip()
    # Senão, pode estar no arquivo compactado packed-refs.
    packed = root / "packed-refs"
    # Existe: procura a linha "<sha> <ref>".
    if packed.is_file():
        # Cada linha.
        for line in packed.read_text().splitlines():
            # Separa SHA e nome.
            parts = line.split()
            # Achou.
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    # Não encontrado.
    return None


# Cache da consulta ao GitHub: (momento, resultado). Evita estourar o limite de 60
# consultas por hora da API pública a cada recarga da página.
_VERSION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

# Validade do cache, em segundos.
_VERSION_TTL = 600


# PR que levou um commit para a main (a API do GitHub liga commits a PRs).
def _pr_for(repo: str, sha: str) -> dict[str, Any] | None:
    # Lista de PRs associados ao commit.
    pulls = _get_json(f"https://api.github.com/repos/{repo}/commits/{sha}/pulls")
    # O primeiro já mesclado, se houver.
    merged = [p for p in pulls if p.get("merged_at")]
    # Escolhe o mesclado ou, na falta, o primeiro.
    pr = (merged or pulls or [None])[0]
    # Nenhum PR.
    if pr is None:
        return None
    # Só os campos usados na página.
    return {"number": pr["number"], "title": pr["title"], "url": pr["html_url"]}


# Compara a versão instalada com a main do GitHub.
def version_info(settings: Settings, *, force: bool = False) -> dict[str, Any]:
    """{installed, installed_pr, latest, latest_pr, behind, error}. force ignora o cache."""
    # Commit instalado.
    sha = installed_commit(settings.git_dir)
    # Chave do cache: o próprio commit.
    key = sha or "-"
    # Cache válido e sem pedido de atualização forçada.
    cached = _VERSION_CACHE.get(key)
    # Usa o cache.
    if cached and not force and time.monotonic() - cached[0] < _VERSION_TTL:
        return cached[1]
    # Resultado padrão.
    info: dict[str, Any] = {
        "installed": sha,
        "installed_pr": None,
        "latest": None,
        "latest_pr": None,
        "behind": None,
        "error": None,
        "checked_at": datetime.now(UTC),
    }
    # Repositório no GitHub.
    repo = settings.github_repo
    # A consulta pode falhar (sem rede, limite da API).
    try:
        # Último commit da main.
        latest = _get_json(f"https://api.github.com/repos/{repo}/commits/main")["sha"]
        # Guarda.
        info["latest"] = latest
        # PR do último commit da main.
        info["latest_pr"] = _pr_for(repo, latest)
        # Com o commit instalado conhecido, compara.
        if sha:
            # PR do commit instalado.
            info["installed_pr"] = info["latest_pr"] if sha == latest else _pr_for(repo, sha)
            # compare: quantos commits a main tem além do instalado (ahead_by).
            diff = _get_json(f"https://api.github.com/repos/{repo}/compare/{sha}...main")
            # Commits que faltam instalar.
            info["behind"] = diff.get("ahead_by", 0)
    # Falha: mostra o motivo na página.
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
        # Motivo legível.
        info["error"] = f"Não foi possível consultar o GitHub: {exc}"
    # Guarda no cache.
    _VERSION_CACHE[key] = (time.monotonic(), info)
    # Devolve.
    return info
