"""Briefing diário (bloco 5): o que precisa de você hoje, por Empresa, numa página só.

O que resolve: até aqui, saber "o que eu preciso fazer hoje" exigia abrir quatro páginas
(a agenda em Reuniões, Alertas, Fatos e Classificação) e cruzar tudo de cabeça. O
briefing junta, toda manhã, cinco perguntas que um gerente de projetos faz antes do
primeiro café:
1. Agenda: que reuniões eu tenho hoje, com que lideranças, e o que está aberto com quem
   vai estar lá?
2. Alertas: o que as lideranças pediram, decidiram ou apontaram e eu ainda não vi?
3. Prazos: o que venceu e o que vence nos próximos dias (marcando o que é meu)?
4. O que mudou por projeto desde o último briefing que eu abri: quantos e-mails,
   reuniões e arquivos chegaram, e quais decisões, riscos e problemas apareceram?
5. Pendências do sistema: o que espera uma decisão minha (fila A revisar, fatos
   suspeitos, arquivos sem texto)?

Como funciona:
- a partir de BRIEFING_HOUR (7h, no fuso do console), o coletor monta um briefing por
  Empresa por dia e grava uma "foto" (JSON) na tabela briefings. Se o coletor estiver
  parado às 7h, ele monta quando voltar, no mesmo dia;
- a página Briefing mostra a foto; ao abrir, o briefing de hoje fica marcado como lido;
- "Gerar de novo" refaz a foto do dia com os dados de agora (a janela continua a mesma).

Por que sem IA: tudo aqui é contagem e lista do que já está no banco, e cada fato já
traz o trecho conferido. Um resumo escrito pelo Claude custaria dinheiro todo dia e
poderia dizer algo que o banco não diz ("números vêm de código, não da IA"). A decisão
está registrada na Arquitetura (seção 11), com o que faria rever.

Por que "desde o último briefing que você abriu", e não "desde ontem": na segunda-feira,
"desde ontem" esconderia o que chegou no sábado; depois de dois dias sem abrir o console,
esconderia o primeiro deles. A janela começa onde você parou de ler (com o limite de
ALERTS_LOOKBACK_DAYS, para a primeira manhã depois das férias não virar um livro).

Separação por Empresa (premissa 6): cada briefing é de uma Empresa só, com a agenda da
conta Google dela e só os dados dela. A página mostra uma Empresa ou todas, uma embaixo
da outra, sem misturar.
"""

# Callable: tipo "função" (a fábrica de clientes da agenda, trocada nos testes).
from collections.abc import Callable

# dataclass: classes simples de dados (o resumo da rodada).
from dataclasses import dataclass, field

# Data, data e hora com fuso e intervalos.
from datetime import UTC, date, datetime, timedelta

# Fusos horários pelo nome (o "agora" no fuso do console decide a hora do briefing).
from zoneinfo import ZoneInfo

# Erro devolvido pelas APIs do Google (agenda fora do ar, sem permissão).
from googleapiclient.errors import HttpError

# Consultas: func gera funções SQL, or_ junta condições, select monta a consulta.
from sqlalchemy import func, or_, select

# Session é a conversa com o banco; aliased dá dois apelidos à mesma tabela.
from sqlalchemy.orm import Session, aliased

# Regras e consultas dos alertas, reaproveitadas para não ter duas versões da verdade.
from app.alerts import DONE, OPEN, group_by_fact, list_alerts, owner_is_me, today_local, why

# Configuração (hora do briefing, dias de prazo, fuso).
from app.config import Settings

# Nome de cada tipo de fato na tela.
from app.facts import KIND_LABELS

# Arquivos sem texto (pendência que só você resolve).
from app.ingestion.failures import list_failures

# Tabelas.
from app.models import (
    Alert,
    Briefing,
    BronzeItem,
    Company,
    Fact,
    FactKind,
    ItemClassification,
    OAuthToken,
    Project,
    ProjectStakeholder,
    SourceKind,
    Stakeholder,
    audit,
)

# Leitura da agenda do Google (só leitura).
from app.transcription.calendar import calendar_client, events_on

# Quantos alertas novos a seção mostra (o resto fica a um clique, na página Alertas).
ALERTS_SHOWN = 15
# Quantos prazos a seção mostra, somando vencidos, de hoje e próximos.
DEADLINES_SHOWN = 30
# Quantos destaques (decisões, riscos, problemas) cada projeto mostra em "O que mudou".
HIGHLIGHTS_PER_PROJECT = 5
# Tipos de fato que viram destaque em "O que mudou": os que mudam o rumo do projeto.
# Tarefas e prazos já aparecem nas seções Alertas e Prazos.
HIGHLIGHT_KINDS = (FactKind.DECISION, FactKind.RISK, FactKind.ISSUE)
# Janela do primeiro briefing de uma Empresa (ainda sem nenhum anterior).
FIRST_WINDOW = timedelta(hours=24)
# Nome de cada fonte em "O que mudou", com o plural entre parênteses (vem depois do número).
SOURCE_PLURALS = {
    # E-mails do Gmail.
    SourceKind.GMAIL: "e-mail(s)",
    # Transcrições de reunião (importadas ou da extensão).
    SourceKind.MEET_TRANSCRIPT: "transcrição(ões)",
    # Versões de arquivos das pastas do Drive.
    SourceKind.DRIVE: "arquivo(s) do Drive",
    # Eventos da agenda gravados no bronze (raros hoje).
    SourceKind.CALENDAR: "evento(s)",
    # Mensagens do Google Chat (fase 2).
    SourceKind.GOOGLE_CHAT: "mensagem(ns) do Chat",
    # Itens digitados à mão.
    SourceKind.MANUAL: "item(ns) manual(is)",
}

# Tipo da fábrica de clientes da agenda: recebe o id da Empresa e devolve o cliente.
CalendarFactory = Callable[[int], object]


# Resumo de uma rodada, para o log do coletor e o comando gerar-briefing.
@dataclass
class BriefingSummary:
    # Nomes das Empresas cujo briefing foi montado nesta rodada.
    generated: list[str] = field(default_factory=list)

    # Uma linha legível.
    def describe(self) -> str:
        # Nenhum montado.
        if not self.generated:
            return "Briefing: nenhum novo."
        # "Briefing: montado para Empresa A, Empresa B."
        return f"Briefing: montado para {', '.join(self.generated)}."


# Hora e minuto no fuso do console ("09:30").
def _hhmm(moment: datetime, zone: ZoneInfo) -> str:
    # Converte e formata.
    return moment.astimezone(zone).strftime("%H:%M")


# Data curta brasileira ("05/11").
def _ddmm(day: date) -> str:
    # Formata.
    return day.strftime("%d/%m")


# Início da janela de "o que mudou" para o briefing de um dia.
def window_start(
    session: Session, settings: Settings, company_id: int, day: date, now: datetime
) -> datetime:
    """O momento em que foi montado o último briefing (de um dia anterior) que você abriu.

    Sem nenhum aberto: o começo da janela do primeiro briefing ainda não lido (assim, dias
    sem abrir o console se somam). Sem nenhum briefing anterior: as últimas 24 horas.
    Nunca mais antigo que ALERTS_LOOKBACK_DAYS."""
    # Limite para trás (a mesma janela dos alertas).
    floor = now - timedelta(days=settings.alerts_lookback_days)
    # Briefings de dias anteriores desta Empresa (o de hoje não conta: "Gerar de novo"
    # não pode encolher a janela só porque você abriu o de hoje de manhã).
    earlier = [Briefing.company_id == company_id, Briefing.day < day]
    # O último aberto.
    last_viewed = session.scalar(
        select(func.max(Briefing.generated_at)).where(*earlier, Briefing.viewed_at.is_not(None))
    )
    # Achou: a janela começa nele.
    if last_viewed is not None:
        return max(last_viewed, floor)
    # Nenhum aberto: o começo do primeiro que existir (os não lidos se somam).
    first_since = session.scalar(select(func.min(Briefing.since)).where(*earlier))
    # Nenhum briefing anterior: as últimas 24 horas.
    start = first_since if first_since is not None else now - FIRST_WINDOW
    # Com o limite.
    return max(start, floor)


# E-mails da dona nesta Empresa: o login do Tailscale e a conta Google conectada.
def _my_emails(session: Session, settings: Settings, company_id: int) -> set[str]:
    # Contas Google da Empresa, em minúsculas.
    emails = {
        e.lower()
        for e in session.scalars(
            select(OAuthToken.account_email).where(OAuthToken.company_id == company_id)
        )
        if e
    }
    # Login do Tailscale.
    login = (settings.allowed_tailscale_login or "").strip().lower()
    # Junta, se houver.
    if login:
        emails.add(login)
    # Devolve.
    return emails


# Mensagem curta para um erro de leitura da agenda (o resto do briefing sai mesmo assim).
def _agenda_error(exc: Exception) -> str:
    # Sem conta Google conectada na Empresa (load_credentials explica).
    if isinstance(exc, RuntimeError):
        return str(exc)
    # A API recusou (sem permissão, fora do ar).
    if isinstance(exc, HttpError):
        return f"A agenda recusou o pedido (HTTP {exc.resp.status})."
    # Outros (ex.: acesso revogado no Google, rede fora): o nome do erro ajuda a achar.
    return f"Não foi possível ler a agenda ({type(exc).__name__})."


# Seção 1: as reuniões do dia na agenda da Empresa, com lideranças e o que está aberto.
def agenda_section(
    session: Session,
    settings: Settings,
    company_id: int,
    day: date,
    calendar_for: CalendarFactory,
) -> tuple[list[dict], str | None]:
    """(reuniões, erro). Sem agenda (conta desconectada, Google fora do ar), a lista vem
    vazia com o motivo, e o resto do briefing é montado normalmente."""
    # Fuso do console.
    zone = ZoneInfo(settings.console_timezone)
    # Tenta ler a agenda.
    try:
        # Cliente da agenda da conta da Empresa.
        calendar = calendar_for(company_id)
        # Reuniões com horário do dia (dia inteiro e canceladas ficam de fora).
        events = events_on(calendar, day, settings.console_timezone)
    # Qualquer erro vira um aviso na seção, não uma falha do briefing inteiro.
    except Exception as exc:
        return [], _agenda_error(exc)
    # Seus e-mails (você não conta como "liderança presente" nem como "aberto com").
    mine = _my_emails(session, settings, company_id)
    # Todos os e-mails convidados do dia, em minúsculas, sem os seus.
    all_emails = {
        e.lower() for ev in events for e in ev.attendee_emails.values() if e.lower() not in mine
    }
    # Pessoas cadastradas na Empresa com esses e-mails: e-mail → Pessoa.
    people = (
        {
            p.email: p
            for p in session.scalars(
                select(Stakeholder).where(
                    Stakeholder.company_id == company_id, Stakeholder.email.in_(all_emails)
                )
            )
        }
        if all_emails
        else {}
    )
    # Ids dessas Pessoas.
    ids = [p.id for p in people.values()]
    # Quem delas é liderança em algum projeto (marca na equipe).
    project_leaders = (
        set(
            session.scalars(
                select(ProjectStakeholder.stakeholder_id).where(
                    ProjectStakeholder.stakeholder_id.in_(ids),
                    ProjectStakeholder.is_leader.is_(True),
                )
            )
        )
        if ids
        else set()
    )
    # Fatos com alerta aberto (novo ou visto) ditos por elas ou com elas como responsáveis.
    open_rows = (
        session.execute(
            select(Fact.id, Fact.said_by_id, Fact.owner_id)
            .join(Alert, Alert.fact_id == Fact.id)
            .where(
                Alert.company_id == company_id,
                Alert.status.in_(OPEN),
                or_(Fact.said_by_id.in_(ids), Fact.owner_id.in_(ids)),
            )
            .distinct()
        ).all()
        if ids
        else []
    )
    # Resultado.
    meetings = []
    # Cada reunião do dia.
    for event in events:
        # Pessoas cadastradas desta reunião.
        here = [people[e.lower()] for e in event.attendee_emails.values() if e.lower() in people]
        # Ids delas.
        here_ids = {p.id for p in here}
        # Lideranças presentes (na empresa ou em algum projeto), sem repetir.
        leaders = sorted(
            {p.name for p in here if p.is_leader or p.id in project_leaders}, key=str.lower
        )
        # Fatos abertos com quem vai estar (cada fato conta uma vez).
        open_facts = {
            fact_id
            for fact_id, said_by, owner in open_rows
            if said_by in here_ids or owner in here_ids
        }
        # A reunião, já com os textos prontos para a página.
        meetings.append(
            {
                # Id do evento (liga à página Reuniões).
                "id": event.id,
                # Título.
                "titulo": event.title,
                # Início e fim no fuso do console.
                "inicio": _hhmm(event.start, zone),
                "fim": _hhmm(event.end, zone),
                # Meet, Teams ou nada.
                "plataforma": event.platform,
                # Link da reunião on-line.
                "url": event.meeting_url,
                # Quantos convidados (sem salas).
                "participantes": len(event.attendees),
                # Nomes das lideranças presentes.
                "liderancas": leaders,
                # Quantos fatos com alerta aberto envolvem quem vai estar.
                "alertas_abertos": len(open_facts),
            }
        )
    # Devolve sem erro.
    return meetings, None


# Seção 2: alertas ainda não vistos, um fato por linha (como na página Alertas).
def alerts_section(session: Session, company_id: int, since: datetime) -> tuple[list[dict], int]:
    """(os primeiros ALERTS_SHOWN, total de fatos com alerta novo)."""
    # Alertas novos da Empresa, já na ordem da página (peso alto primeiro), um por fato.
    rows = group_by_fact(list_alerts(session, company_id, "novo"))
    # Resultado.
    shown = []
    # Os primeiros.
    for (alert, rule, fact, item, project, speaker, _owner), others in rows[:ALERTS_SHOWN]:
        # Linha.
        shown.append(
            {
                # Fato (liga à página Alertas e a Conteúdo coletado).
                "fato_id": fact.id,
                # Regra que disparou e as outras do mesmo fato.
                "regra": rule.name,
                "outras": others,
                # Peso.
                "peso": alert.severity,
                # Tipo, em texto.
                "tipo": KIND_LABELS.get(fact.kind, str(fact.kind)),
                # Resumo e trecho.
                "titulo": fact.title,
                "trecho": fact.source_quote,
                # "Márcia (liderança no projeto)".
                "quem": why(alert, speaker),
                # Projeto.
                "projeto": project.name if project else None,
                # Prazo, se houver.
                "prazo": _ddmm(fact.due_date) if fact.due_date else None,
                # Item de origem.
                "item_id": item.id,
                # Chegou depois do último briefing que você abriu.
                "chegou": alert.created_at >= since,
            }
        )
    # Devolve com o total.
    return shown, len(rows)


# Seção 3: prazos vencidos, de hoje e dos próximos BRIEFING_DUE_DAYS dias.
def deadlines_section(
    session: Session, settings: Settings, company_id: int, today: date
) -> dict[str, list[dict]]:
    """{"vencidos": [...], "hoje": [...], "proximos": [...]}. Só fatos com trecho
    conferido e não concluídos (os suspeitos ficam na página Fatos, como nos alertas)."""
    # Responsável.
    owner = aliased(Stakeholder)
    # O responsável é você (mesma regra do alerta "o responsável sou eu").
    mine = owner_is_me(settings)
    # Consulta.
    rows = session.execute(
        select(Fact, Project, owner, mine)
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        .outerjoin(Project, Project.id == Fact.project_id)
        .outerjoin(owner, owner.id == Fact.owner_id)
        .where(
            # Da Empresa.
            BronzeItem.company_id == company_id,
            # Valendo.
            Fact.superseded_by_id.is_(None),
            # Conferido.
            Fact.quote_verified.is_(True),
            # Aberto.
            Fact.status.is_distinct_from(DONE),
            # Com prazo dentro da faixa.
            Fact.due_date.is_not(None),
            Fact.due_date >= today - timedelta(days=settings.alerts_lookback_days),
            Fact.due_date <= today + timedelta(days=settings.briefing_due_days),
        )
        # O mais urgente primeiro.
        .order_by(Fact.due_date, Fact.id)
        .limit(DEADLINES_SHOWN)
    ).all()
    # Três grupos.
    groups: dict[str, list[dict]] = {"vencidos": [], "hoje": [], "proximos": []}
    # Cada prazo.
    for fact, project, person, is_mine in rows:
        # Grupo pelo dia.
        key = (
            "vencidos"
            if fact.due_date < today
            else ("hoje" if fact.due_date == today else "proximos")
        )
        # Linha.
        groups[key].append(
            {
                # Fato.
                "fato_id": fact.id,
                # Tipo.
                "tipo": KIND_LABELS.get(fact.kind, str(fact.kind)),
                # Resumo.
                "titulo": fact.title,
                # Prazo em texto e quantos dias faltam (negativo = atrasado).
                "prazo": _ddmm(fact.due_date),
                "dias": (fact.due_date - today).days,
                # Projeto.
                "projeto": project.name if project else None,
                # Responsável cadastrado, ou como escrito no texto.
                "responsavel": person.name if person else (fact.owner_text or None),
                # É seu.
                "meu": bool(is_mine),
                # Item de origem.
                "item_id": fact.source_item_id,
            }
        )
    # Devolve.
    return groups


# Seção 4: o que chegou e o que apareceu em cada projeto desde o começo da janela.
def changes_section(
    session: Session, company_id: int, since: datetime, now: datetime
) -> list[dict]:
    """Uma entrada por projeto com novidade, em ordem alfabética; "sem projeto" no fim."""
    # Itens que entraram na janela, por projeto e fonte (sem classificação = sem projeto).
    items = session.execute(
        select(ItemClassification.project_id, BronzeItem.source, func.count())
        .select_from(BronzeItem)
        .outerjoin(ItemClassification, ItemClassification.bronze_item_id == BronzeItem.id)
        .where(
            BronzeItem.company_id == company_id,
            BronzeItem.ingested_at >= since,
            BronzeItem.ingested_at <= now,
        )
        .group_by(ItemClassification.project_id, BronzeItem.source)
    ).all()
    # Condições dos fatos novos: da Empresa, da janela, valendo e conferidos.
    new_facts = [
        BronzeItem.company_id == company_id,
        Fact.created_at >= since,
        Fact.created_at <= now,
        Fact.superseded_by_id.is_(None),
        Fact.quote_verified.is_(True),
    ]
    # Fatos novos por projeto e tipo.
    kinds = session.execute(
        select(Fact.project_id, Fact.kind, func.count())
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        .where(*new_facts)
        .group_by(Fact.project_id, Fact.kind)
    ).all()
    # Destaques: decisões, riscos e problemas novos, os mais recentes primeiro.
    highlights = session.scalars(
        select(Fact)
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        .where(*new_facts, Fact.kind.in_(HIGHLIGHT_KINDS))
        .order_by(Fact.created_at.desc(), Fact.id.desc())
    ).all()
    # Nomes dos projetos da Empresa.
    names = {
        pid: name
        for pid, name in session.execute(
            select(Project.id, Project.name).where(Project.company_id == company_id)
        )
    }
    # Projeto → entrada (montada aos poucos).
    by_project: dict[int | None, dict] = {}

    # Entrada de um projeto (cria na primeira vez).
    def entry(project_id: int | None) -> dict:
        # Cria se ainda não existe.
        if project_id not in by_project:
            by_project[project_id] = {
                # Id (None = sem projeto).
                "projeto_id": project_id,
                # Nome.
                "projeto": names.get(project_id) if project_id else None,
                # Fonte → quantos itens chegaram.
                "itens": {},
                # Tipo de fato → quantos novos.
                "fatos": {},
                # Decisões, riscos e problemas novos.
                "destaques": [],
            }
        # Devolve.
        return by_project[project_id]

    # Itens que chegaram.
    for project_id, source, count in items:
        # Soma na fonte (duas linhas da mesma fonte não acontecem, mas somar é seguro).
        target = entry(project_id)["itens"]
        # Nome da fonte no plural.
        label = SOURCE_PLURALS.get(source, str(source))
        # Soma.
        target[label] = target.get(label, 0) + count
    # Fatos novos, na ordem dos tipos.
    for project_id, kind, count in sorted(kinds, key=lambda r: list(KIND_LABELS).index(r[1])):
        # Soma no tipo.
        entry(project_id)["fatos"][KIND_LABELS[kind]] = count
    # Destaques, até HIGHLIGHTS_PER_PROJECT por projeto.
    for fact in highlights:
        # Lista do projeto.
        target = entry(fact.project_id)["destaques"]
        # Ainda cabe.
        if len(target) < HIGHLIGHTS_PER_PROJECT:
            target.append(
                {
                    # Fato.
                    "fato_id": fact.id,
                    # Tipo.
                    "tipo": KIND_LABELS.get(fact.kind, str(fact.kind)),
                    # Resumo.
                    "titulo": fact.title,
                    # Item de origem.
                    "item_id": fact.source_item_id,
                }
            )
    # Projetos com nome em ordem alfabética; "sem projeto" no fim.
    return sorted(
        by_project.values(),
        key=lambda e: (e["projeto_id"] is None, (e["projeto"] or "").lower()),
    )


# Seção 5: o que espera uma decisão sua, em números (cada um leva à página que resolve).
def pending_section(session: Session, company_id: int) -> dict[str, int]:
    # Itens na fila "A revisar" da Classificação.
    review = session.scalar(
        select(func.count())
        .select_from(ItemClassification)
        .join(BronzeItem, BronzeItem.id == ItemClassification.bronze_item_id)
        .where(ItemClassification.needs_review.is_(True), BronzeItem.company_id == company_id)
    )
    # Fatos suspeitos (trecho não achado no texto).
    suspicious = session.scalar(
        select(func.count())
        .select_from(Fact)
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        .where(
            Fact.superseded_by_id.is_(None),
            Fact.quote_verified.is_(False),
            BronzeItem.company_id == company_id,
        )
    )
    # Arquivos sem texto não dispensados (a lista do Início, só desta Empresa).
    no_text = len(list_failures(session, limit=1000, company_id=company_id))
    # Devolve.
    return {"revisar": review or 0, "suspeitos": suspicious or 0, "sem_texto": no_text}


# Monta o conteúdo das cinco seções.
def build_content(
    session: Session,
    settings: Settings,
    company_id: int,
    day: date,
    since: datetime,
    now: datetime,
    calendar_for: CalendarFactory,
) -> dict:
    # Agenda (ou o motivo de não ter).
    agenda, agenda_error = agenda_section(session, settings, company_id, day, calendar_for)
    # Alertas novos.
    alerts, alerts_total = alerts_section(session, company_id, since)
    # Devolve tudo num dicionário (vira o JSON da coluna content).
    return {
        # Reuniões do dia.
        "agenda": agenda,
        # Motivo de a agenda não ter sido lida.
        "agenda_erro": agenda_error,
        # Alertas novos mostrados e o total.
        "alertas": alerts,
        "alertas_total": alerts_total,
        # Quantos chegaram depois do último briefing que você abriu (entre os mostrados).
        "alertas_chegaram": sum(1 for a in alerts if a["chegou"]),
        # Prazos.
        "prazos": deadlines_section(session, settings, company_id, day),
        # O que mudou por projeto.
        "mudancas": changes_section(session, company_id, since, now),
        # Pendências.
        "pendencias": pending_section(session, company_id),
    }


# Monta (ou remonta) o briefing de hoje de uma Empresa e grava.
def generate(
    session: Session,
    settings: Settings,
    company_id: int,
    *,
    now: datetime | None = None,
    calendar_for: CalendarFactory | None = None,
    trigger: str = "agendado",
    actor: str = "sistema",
) -> Briefing:
    # Momento da montagem (o teste passa um fixo).
    now = now or datetime.now(UTC)
    # Dia no fuso do console.
    day = today_local(settings, now)
    # Cliente da agenda: o da conta da Empresa (os testes passam um falso).
    calendar_for = calendar_for or (lambda cid: calendar_client(session, settings, cid))
    # Começo da janela de "o que mudou".
    since = window_start(session, settings, company_id, day, now)
    # Conteúdo.
    content = build_content(session, settings, company_id, day, since, now, calendar_for)
    # Briefing de hoje desta Empresa, se já existir ("Gerar de novo").
    briefing = session.scalar(
        select(Briefing).where(Briefing.company_id == company_id, Briefing.day == day)
    )
    # Ainda não existe: cria.
    if briefing is None:
        # Novo.
        briefing = Briefing(company_id=company_id, day=day)
        # Prepara a gravação.
        session.add(briefing)
    # Momento, janela, conteúdo e quem pediu.
    briefing.generated_at = now
    briefing.since = since
    briefing.content = content
    briefing.trigger = trigger
    # Conteúdo novo: volta a "não lido" (abrir a página marca de novo).
    briefing.viewed_at = None
    # Envia ao banco (para ter o id no diário).
    session.flush()
    # Prazos, para o diário.
    deadlines = content["prazos"]
    # Diário (aparece em Processamento): quando e com quanto saiu.
    audit(
        session,
        actor,
        "briefing.gerado",
        target=str(briefing.id),
        company_id=company_id,
        dia=day.isoformat(),
        reunioes=len(content["agenda"]),
        agenda_erro=content["agenda_erro"],
        alertas_novos=content["alertas_total"],
        prazos_vencidos=len(deadlines["vencidos"]),
        prazos_proximos=len(deadlines["hoje"]) + len(deadlines["proximos"]),
        projetos_com_mudanca=len(content["mudancas"]),
    )
    # Devolve.
    return briefing


# Empresas que já deviam ter o briefing de hoje e ainda não têm.
def due_companies(session: Session, settings: Settings, now: datetime) -> list[Company]:
    """Vazio antes de BRIEFING_HOUR (no fuso do console)."""
    # Agora no fuso do console.
    local = now.astimezone(ZoneInfo(settings.console_timezone))
    # Ainda não deu a hora.
    if local.hour < settings.briefing_hour:
        return []
    # Empresas sem briefing hoje, em ordem alfabética.
    done = select(Briefing.company_id).where(Briefing.day == local.date())
    # Consulta.
    return list(
        session.scalars(
            select(Company).where(Company.id.not_in(done)).order_by(func.lower(Company.name))
        )
    )


# Uma rodada do coletor: monta o briefing das Empresas que ainda não têm o de hoje.
def run_due(
    session: Session,
    settings: Settings,
    now: datetime | None = None,
    calendar_for: CalendarFactory | None = None,
) -> BriefingSummary:
    # Momento.
    now = now or datetime.now(UTC)
    # Resumo.
    summary = BriefingSummary()
    # Cada Empresa atrasada.
    for company in due_companies(session, settings, now):
        # Monta.
        generate(session, settings, company.id, now=now, calendar_for=calendar_for)
        # Conta.
        summary.generated.append(company.name)
    # Devolve.
    return summary


# Conveniência para o agendador: abre a sessão, roda e grava.
def run_from_settings(settings: Settings) -> BriefingSummary | None:
    """None quando nada foi montado (para não encher o log a cada 5 minutos)."""
    # Import local: o banco só é carregado quando a rodada roda de verdade.
    from app.db import get_sessionmaker

    # Uma sessão.
    with get_sessionmaker()() as session:
        # Roda.
        summary = run_due(session, settings)
        # Grava.
        session.commit()
    # Só devolve quando há o que registrar no log.
    return summary if summary.generated else None


# Briefings de um dia (de uma Empresa, ou de todas em ordem alfabética).
def of_day(session: Session, day: date, company_id: int | None = None) -> list[Briefing]:
    # Consulta: o briefing com a Empresa (para ordenar pelo nome).
    query = (
        select(Briefing)
        .join(Company, Company.id == Briefing.company_id)
        .where(Briefing.day == day)
        .order_by(func.lower(Company.name))
    )
    # Uma Empresa.
    if company_id is not None:
        query = query.where(Briefing.company_id == company_id)
    # Executa.
    return list(session.scalars(query))


# Marca como lidos (só os que ainda não estavam: a primeira leitura é a que conta).
def mark_viewed(briefings: list[Briefing], now: datetime | None = None) -> int:
    # Momento.
    now = now or datetime.now(UTC)
    # Quantos foram marcados agora.
    marked = 0
    # Cada briefing.
    for briefing in briefings:
        # Ainda não lido.
        if briefing.viewed_at is None:
            # Marca.
            briefing.viewed_at = now
            # Conta.
            marked += 1
    # Devolve.
    return marked


# Há briefing de hoje ainda não lido? (o ponto ao lado de "Briefing" no menu)
def unread_today(session: Session, settings: Settings, now: datetime | None = None) -> bool:
    # Hoje no fuso do console.
    today = today_local(settings, now)
    # Existe algum de hoje sem leitura.
    return bool(
        session.scalar(
            select(func.count())
            .select_from(Briefing)
            .where(Briefing.day == today, Briefing.viewed_at.is_(None))
        )
    )


# Dias vizinhos com briefing (as setas da página), de uma Empresa ou de qualquer uma.
def neighbor_days(
    session: Session, day: date, company_id: int | None = None
) -> tuple[date | None, date | None]:
    """(dia anterior com briefing, dia seguinte com briefing)."""
    # Filtro de Empresa (vazio = todas).
    of_company = [Briefing.company_id == company_id] if company_id is not None else []
    # Anterior.
    before = session.scalar(select(func.max(Briefing.day)).where(Briefing.day < day, *of_company))
    # Seguinte.
    after = session.scalar(select(func.min(Briefing.day)).where(Briefing.day > day, *of_company))
    # Devolve.
    return before, after
