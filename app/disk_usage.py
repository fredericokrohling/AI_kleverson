"""Quanto do disco cada projeto ocupa, e quanto é do sistema.

De onde vem cada fatia:
- projetos: os arquivos originais guardados na pasta de arquivos (e-mails .eml, anexos,
  arquivos do Drive, imagens tiradas de PowerPoint e Word), somados pelo projeto do item a
  que pertencem. "Projeto do item" é a mesma regra do resto do console: o item tem a tag
  do projeto (regra automática, pasta do Gmail, à mão ou, no Drive, a tag da pasta);
- sem projeto: arquivos de itens sem tag de projeto (e arquivos soltos na pasta);
- banco de dados: o tamanho do Postgres (textos extraídos, transcrições, cadastros). Não é
  dividido por projeto porque o Postgres não informa o espaço por linha; estimar seria
  inventar número. Na prática, o banco é pequeno perto dos arquivos;
- sistema: o resto do que está ocupado no disco: Linux, Docker e as imagens dos
  contêineres, registros (logs) e os 5% que o Linux reserva para si;
- livre.

Arquivo usado por mais de um projeto (o mesmo PDF anexado em e-mails de dois projetos):
os arquivos são guardados uma vez só (o nome é o SHA-256 do conteúdo), então o espaço é
dividido em partes iguais entre os projetos. Assim as fatias somam o disco de verdade, sem
contar o mesmo arquivo duas vezes.

O tamanho de cada arquivo é o espaço que ele ocupa no disco (blocos), não o tamanho do
conteúdo: um arquivo de 300 bytes ocupa um bloco de 4 KB. É o mesmo número que o comando
`du` mostra, e é o que enche o disco.

Por que guardar o resultado por 10 minutos: medir passa por todos os arquivos da pasta.
Com dezenas de milhares de arquivos isso leva alguns segundos, e o disco não muda de
forma relevante em 10 minutos.
"""

# os.walk percorre pastas; os.stat lê o tamanho de cada arquivo.
import os

# shutil.disk_usage informa o tamanho total e o espaço livre do disco.
import shutil

# dataclass cria classes simples de dados.
from dataclasses import dataclass, field

# Data e hora com fuso, e intervalos.
from datetime import UTC, datetime, timedelta

# Path manipula caminhos de arquivos.
from pathlib import Path

# Consultas.
from sqlalchemy import column, select, table, text

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Tipo da configuração.
from app.config import Settings

# Tabelas.
from app.models import Attachment, BronzeItem, Company, Project, TaggedItem

# View com as tags das versões do Drive (herdadas das pastas).
_DRIVE_TAGS = table("bronze_item_tags", column("bronze_item_id"), column("tag_id"))

# Quantos projetos aparecem com fatia própria; os demais viram "Outros projetos".
MAX_PROJECT_SLICES = 5

# Cores dos projetos, na ordem (paleta validada para daltonismo sobre o fundo escuro;
# ver docs/aprendizado/13-uso-do-servidor.md). Azul, laranja, verde-água, amarelo, rosa.
PROJECT_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
# "Sem projeto": violeta, a próxima cor da paleta.
COLOR_NO_PROJECT = "#9085e9"
# Banco e sistema: cinzas, porque não são projeto (a cor fica para o que é seu).
COLOR_DATABASE = "#a3a3a3"
# Sistema.
COLOR_SYSTEM = "#5f5f5f"
# Livre: o tom do trilho do gráfico.
COLOR_FREE = "#262626"

# Quanto tempo o resultado vale.
_CACHE_TTL = timedelta(minutes=10)
# Último resultado e quando foi calculado.
_cache: dict[str, object] = {}


# Uma fatia do gráfico de rosca.
@dataclass
class Slice:
    # Nome mostrado.
    label: str
    # Bytes.
    size: int
    # Cor.
    color: str
    # Explicação curta (aparece ao passar o mouse e na tabela).
    detail: str = ""
    # % do disco inteiro (preenchido no fim).
    percent: float = 0.0


# O disco inteiro, fatiado.
@dataclass
class DiskBreakdown:
    # Tamanho do disco.
    total: int
    # Espaço livre para uso.
    free: int
    # Fatias na ordem do gráfico (projetos, sem projeto, banco, sistema, livre).
    slices: list[Slice] = field(default_factory=list)
    # Quando foi medido.
    measured_at: datetime | None = None

    # Fatias que são dados do sistema (projetos, sem projeto, banco), sem sistema e livre.
    @property
    def data_slices(self) -> list[Slice]:
        # As duas últimas são sempre Sistema e Livre.
        return self.slices[:-2]

    # Só as fatias de projeto (inclui "Outros projetos"): tira Sem projeto, Banco, Sistema
    # e Livre, que são sempre as quatro últimas.
    @property
    def project_slices(self) -> list[Slice]:
        # Tudo antes das quatro fixas.
        return self.slices[:-4]

    # Soma dos dados guardados pelo sistema.
    @property
    def data_total(self) -> int:
        # Soma das fatias de dados.
        return sum(s.size for s in self.data_slices)

    # % ocupado (tudo menos o livre), o número do centro da rosca.
    @property
    def used_percent(self) -> float:
        # Sem disco (não deveria acontecer): zero.
        return (self.total - self.free) / self.total * 100 if self.total else 0.0


# Espaço ocupado por cada arquivo dentro de `root`: caminho relativo → bytes.
def file_sizes(root: Path) -> dict[str, int]:
    # Resultado.
    sizes: dict[str, int] = {}
    # Pasta não existe (fora do servidor): nada.
    if not root.exists():
        return sizes
    # os.walk visita cada pasta e lista os arquivos dela.
    for folder, _dirs, files in os.walk(root):
        # Cada arquivo.
        for name in files:
            # Caminho completo.
            path = Path(folder) / name
            # Tenta ler o tamanho.
            try:
                # lstat: não segue atalhos (um atalho não ocupa o espaço do destino).
                info = path.lstat()
            # Sumiu entre listar e ler (o coletor apagou um temporário): ignora.
            except FileNotFoundError:
                continue
            # st_blocks conta blocos de 512 bytes realmente ocupados no disco.
            sizes[path.relative_to(root).as_posix()] = info.st_blocks * 512
    # Devolve.
    return sizes


# Arquivos de cada item do bronze: id do item → caminhos relativos a "originais".
def item_files(session: Session) -> dict[int, set[str]]:
    # Resultado.
    files: dict[int, set[str]] = {}
    # E-mails (.eml em extra.eml) e versões do Drive (original em extra.arquivo).
    rows = session.execute(
        select(BronzeItem.id, BronzeItem.extra["eml"].astext, BronzeItem.extra["arquivo"].astext)
    ).all()
    # Cada item.
    for item_id, eml, original in rows:
        # Guarda os caminhos que existirem.
        files[item_id] = {key for key in (eml, original) if key}
    # Anexos e imagens tiradas de documentos.
    attachments = session.execute(
        select(Attachment.bronze_item_id, Attachment.object_key).where(
            Attachment.object_key.is_not(None)
        )
    ).all()
    # Cada anexo vai para o item dele.
    for item_id, key in attachments:
        # setdefault cria o conjunto se o item ainda não tinha.
        files.setdefault(item_id, set()).add(key)
    # Devolve.
    return files


# Projetos de cada item, pela tag do projeto: id do item → ids dos projetos.
def item_projects(session: Session) -> dict[int, set[int]]:
    # E-mails e transcrições: tags valendo que são de projeto.
    own = session.execute(
        select(TaggedItem.bronze_item_id, Project.id)
        .join(Project, Project.tag_id == TaggedItem.tag_id)
        .where(TaggedItem.removed_at.is_(None))
    ).all()
    # Versões do Drive: tags das pastas que são de projeto.
    drive = session.execute(
        select(_DRIVE_TAGS.c.bronze_item_id, Project.id).join(
            Project, Project.tag_id == _DRIVE_TAGS.c.tag_id
        )
    ).all()
    # Junta.
    result: dict[int, set[int]] = {}
    # Cada par.
    for item_id, project_id in [*own, *drive]:
        # Acrescenta.
        result.setdefault(item_id, set()).add(project_id)
    # Devolve.
    return result


# Tamanho do banco de dados, em bytes.
def database_size(session: Session) -> int:
    # pg_database_size: função do Postgres com o tamanho do banco atual no disco.
    return int(session.execute(text("SELECT pg_database_size(current_database())")).scalar_one())


# Formata bytes para leitura humana (1,2 GB; 340 MB).
def human(size: float) -> str:
    # Unidades em ordem.
    for unit in ("B", "KB", "MB", "GB"):
        # Cabe nesta unidade.
        if abs(size) < 1024:
            # Inteiro para B e KB; uma casa para MB e GB. Vírgula, como no Brasil.
            text_ = f"{size:.0f}" if unit in ("B", "KB") else f"{size:.1f}"
            # Devolve com a unidade.
            return f"{text_.replace('.', ',')} {unit}"
        # Passa para a próxima unidade.
        size /= 1024
    # Terabytes.
    return f"{size:.1f} TB".replace(".", ",")


# Formata uma porcentagem com vírgula; abaixo de 0,1% mostra "<0,1%" em vez de "0,0%",
# para não parecer que o projeto não ocupa nada.
def percent_text(value: float) -> str:
    # Zero de verdade.
    if value <= 0:
        return "0%"
    # Pequeno demais para uma casa decimal.
    if value < 0.1:
        return "<0,1%"
    # Uma casa, com vírgula.
    return f"{value:.1f}%".replace(".", ",")


# Mede o disco e monta as fatias (sem cache).
def breakdown(
    session: Session,
    settings: Settings,
    *,
    # Total e livre do disco; None = mede o disco de verdade (os testes passam valores).
    disk: tuple[int, int] | None = None,
    # Momento da medição.
    now: datetime | None = None,
) -> DiskBreakdown:
    """Fatias do disco: projetos, sem projeto, banco, sistema e livre."""
    # Pasta dos arquivos.
    data_dir = Path(settings.data_dir)
    # Disco da pasta dos arquivos (fora do servidor, a raiz).
    if disk is None:
        # Total, usado e livre.
        usage = shutil.disk_usage(data_dir if data_dir.exists() else "/")
        # Só total e livre importam: o resto é calculado.
        disk = (usage.total, usage.free)
    # Separa.
    total, free = disk
    # Todos os arquivos da pasta de dados.
    all_files = file_sizes(data_dir)
    # Os originais ficam em "originais/"; o banco guarda o caminho sem esse começo.
    prefix = "originais/"
    # Projetos de cada caminho guardado.
    key_projects: dict[str, set[int]] = {}
    # Projetos de cada item.
    projects_of = item_projects(session)
    # Cada item e os seus arquivos.
    for item_id, keys in item_files(session).items():
        # Projetos deste item (vazio = sem projeto).
        projects = projects_of.get(item_id, set())
        # Cada arquivo do item.
        for key in keys:
            # Junta os projetos de todos os itens que usam o arquivo.
            key_projects.setdefault(key, set()).update(projects)
    # Bytes por projeto.
    per_project: dict[int, float] = {}
    # Bytes sem projeto.
    no_project = 0.0
    # Cada arquivo no disco.
    for path, size in all_files.items():
        # Caminho como o banco guarda (sem "originais/").
        key = path[len(prefix) :] if path.startswith(prefix) else None
        # Projetos do arquivo.
        projects = key_projects.get(key, set()) if key else set()
        # Sem projeto (ou arquivo solto, fora dos originais).
        if not projects:
            # Soma no "sem projeto".
            no_project += size
            # Próximo.
            continue
        # Divide em partes iguais entre os projetos que usam o arquivo.
        for project_id in projects:
            # Parte deste projeto.
            per_project[project_id] = per_project.get(project_id, 0.0) + size / len(projects)
    # Soma de todos os arquivos.
    files_total = sum(all_files.values())
    # Tamanho do banco.
    db_size = database_size(session)
    # Sistema: tudo o que está ocupado e não é arquivo nem banco (nunca negativo).
    system = max(total - free - files_total - db_size, 0)
    # Nomes de projeto e Empresa, para os rótulos.
    names = {
        project_id: (project_name, company_name)
        for project_id, project_name, company_name in session.execute(
            select(Project.id, Project.name, Company.name).join(
                Company, Company.id == Project.company_id
            )
        )
    }
    # Projetos com espaço, do maior para o menor.
    ranked = sorted(
        (pid for pid, size in per_project.items() if size > 0),
        key=lambda pid: -per_project[pid],
    )
    # Mais projetos que fatias: os últimos viram "Outros projetos".
    shown = ranked if len(ranked) <= MAX_PROJECT_SLICES else ranked[: MAX_PROJECT_SLICES - 1]
    # Cor de cada projeto mostrado pela ordem de cadastro (id), não pelo tamanho: assim um
    # projeto não muda de cor quando outro passa à frente dele.
    color_of = {pid: PROJECT_COLORS[i] for i, pid in enumerate(sorted(shown))}
    # Fatias dos projetos.
    slices = [
        Slice(
            # Nome do projeto.
            label=names.get(pid, ("Projeto apagado", ""))[0],
            # Espaço.
            size=round(per_project[pid]),
            # Cor fixa.
            color=color_of[pid],
            # Empresa do projeto.
            detail=names.get(pid, ("", ""))[1],
        )
        for pid in shown
    ]
    # Restante dos projetos.
    rest = ranked[len(shown) :]
    # Se houver, uma fatia só para eles.
    if rest:
        slices.append(
            Slice(
                # Rótulo com a quantidade.
                label=f"Outros projetos ({len(rest)})",
                # Soma.
                size=round(sum(per_project[pid] for pid in rest)),
                # A última cor de projeto que sobrou.
                color=PROJECT_COLORS[MAX_PROJECT_SLICES - 1],
                # Nomes.
                detail=", ".join(names.get(pid, ("?", ""))[0] for pid in rest),
            )
        )
    # As fatias fixas.
    slices += [
        # Arquivos de itens sem tag de projeto.
        Slice("Sem projeto", round(no_project), COLOR_NO_PROJECT, "Arquivos sem tag de projeto"),
        # Banco.
        Slice("Banco de dados", db_size, COLOR_DATABASE, "Textos, transcrições e cadastros"),
        # Sistema.
        Slice("Sistema", system, COLOR_SYSTEM, "Linux, Docker, logs e reserva do Linux"),
        # Livre.
        Slice("Livre", free, COLOR_FREE, "Espaço disponível"),
    ]
    # % de cada fatia sobre o disco inteiro.
    for item in slices:
        # Proporção.
        item.percent = item.size / total * 100 if total else 0.0
    # Resultado.
    return DiskBreakdown(
        total=total, free=free, slices=slices, measured_at=now or datetime.now(UTC)
    )


# Mesmo resultado, guardado por 10 minutos.
def cached_breakdown(session: Session, settings: Settings) -> DiskBreakdown:
    # Agora.
    now = datetime.now(UTC)
    # Último resultado.
    last = _cache.get("valor")
    # Ainda vale: devolve sem medir.
    if isinstance(last, DiskBreakdown) and last.measured_at and now - last.measured_at < _CACHE_TTL:
        return last
    # Mede.
    value = breakdown(session, settings, now=now)
    # Guarda.
    _cache["valor"] = value
    # Devolve.
    return value
