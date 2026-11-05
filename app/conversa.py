"""Conversa com os dados pelo Claude (bloco 7): as consultas que as ferramentas do MCP usam.

Como funciona: o Claude Desktop (ou o Claude Code) roda no seu computador um programa
pequeno, o servidor MCP local (mcp_local/kleverson_mcp.py). Quando você pergunta "o que
vence esta semana no projeto X?", o Claude escolhe uma ferramenta desse programa, e ele
chama a API do console (app/routes_conversa.py) pela rede Tailscale. A API chama as funções
deste arquivo, que só fazem consultas SQL e devolvem dicionários, que viram JSON.

Três regras valem para todas as funções:
1. Uma Empresa por vez. Toda função recebe a Empresa e filtra por ela; um id de outra
   Empresa responde como se não existisse. Os dados de um cliente nunca entram numa
   resposta sobre outro.
2. Sempre com a fonte. Todo resultado traz de onde veio (tipo, título, data, autor) e o
   link da página do console, para você e o Claude conferirem.
3. Sem custo de IA aqui. Quem lê e responde é o Claude da sua assinatura; o servidor não
   chama a API da Anthropic nesta parte.
"""

# date = só a data; datetime = data com hora; time = só a hora; timedelta = intervalo;
# UTC = o fuso de referência do banco.
from datetime import UTC, date, datetime, time, timedelta

# Fusos horários pelo nome.
from zoneinfo import ZoneInfo

# Peças de consulta do SQLAlchemy: or_ junta condições com OU; func chama funções SQL;
# literal_column escreve um trecho de SQL fixo (o nome da configuração de busca); select
# monta a consulta; exists confere se uma linha existe.
from sqlalchemy import exists, func, literal_column, or_, select

# aliased: um apelido para usar a mesma tabela duas vezes (responsável e quem disse).
from sqlalchemy.orm import Session, aliased

# Regras dos alertas: abertos, por que disparou e "o responsável sou eu".
from app.alerts import group_by_fact, list_alerts, owner_is_me, why

# Briefing de um dia.
from app.briefing import of_day

# Eventos já criados por fato (para dizer "já está na agenda") e a criação.
from app.calendar_events import EventError, active_by_fact, create_for_fact

# Tipo da configuração.
from app.config import Settings

# Nomes legíveis dos tipos de fato e "concluído".
from app.facts import KIND_LABELS

# Tabelas consultadas e o diário.
from app.models import (
    Alert,
    Attachment,
    BronzeItem,
    Company,
    Fact,
    FactKind,
    ItemClassification,
    Meeting,
    MeetingAttendee,
    OAuthToken,
    Project,
    SourceKind,
    Stakeholder,
    audit,
)

# Texto próprio de um e-mail (sem a conversa citada).
from app.project_tags import own_text

# Nome de cada fonte numa resposta (o Claude repete esse nome para você).
SOURCE_LABELS = {
    SourceKind.GMAIL: "e-mail",
    SourceKind.MEET_TRANSCRIPT: "transcrição de reunião",
    SourceKind.DRIVE: "arquivo do Drive",
    SourceKind.CALENDAR: "evento da agenda",
    SourceKind.GOOGLE_CHAT: "mensagem do Chat",
    SourceKind.MANUAL: "anotação",
}
# Fontes que o filtro "fonte" da busca aceita, pelo nome que o Claude usa.
SOURCE_FILTERS = {
    "email": SourceKind.GMAIL,
    "transcricao": SourceKind.MEET_TRANSCRIPT,
    "drive": SourceKind.DRIVE,
}
# Tipos de fato pelo nome que o Claude usa (sem acento, minúsculas).
KIND_FILTERS = {
    "tarefa": FactKind.TASK,
    "decisao": FactKind.DECISION,
    "risco": FactKind.RISK,
    "problema": FactKind.ISSUE,
    "premissa": FactKind.ASSUMPTION,
    "dependencia": FactKind.DEPENDENCY,
    "prazo": FactKind.DEADLINE,
    "definicao": FactKind.DEFINITION,
}
# Máximo de resultados por página, mesmo se o Claude pedir mais. Acima disso a resposta
# ocupa espaço demais na conversa.
MAX_LIMIT = 50
# Tamanho máximo da pergunta da busca (uma frase, não um texto colado).
MAX_QUERY = 200
# Fatos do item listados ao ler uma fonte.
FACTS_OF_SOURCE = 20
# Opções do trecho da busca: até 2 pedaços de 15 a 35 palavras, com a palavra achada
# entre « », e " … " entre os pedaços.
_HEADLINE = (
    'MaxFragments=2, MaxWords=35, MinWords=15, StartSel=«, StopSel=», FragmentDelimiter=" … "'
)
# Nomes das configurações de busca da migração 0017, como SQL fixo.
_PT = literal_column("'portuguese'::regconfig")
# A sem acento.
_PT_PLAIN = literal_column("'kleverson_pt'::regconfig")


# Pedido que não pode ser atendido (Empresa ou item inexistente, filtro inválido). A API
# devolve a frase para o Claude, que explica para você.
class ConversaError(Exception):
    # status: o código HTTP da resposta (404 = não existe; 400 = pedido inválido).
    def __init__(self, message: str, status: int = 400) -> None:
        # Guarda a frase como mensagem da exceção.
        super().__init__(message)
        # Guarda o código.
        self.status = status


# --- Peças comuns --------------------------------------------------------------


# Limite de resultados: o pedido, até MAX_LIMIT; sem pedido, o padrão da configuração.
def page_size(settings: Settings, limit: int | None) -> int:
    # Sem pedido: o padrão (MCP_MAX_RESULTS).
    if not limit:
        return min(settings.mcp_max_results, MAX_LIMIT)
    # Com pedido: entre 1 e MAX_LIMIT.
    return max(1, min(limit, MAX_LIMIT))


# A Empresa pedida. Toda ferramenta começa por aqui.
def company_or_error(session: Session, company_id: int) -> Company:
    # Busca pelo id.
    company = session.get(Company, company_id)
    # Não existe: diz quais existem, para o Claude escolher certo na próxima.
    if company is None:
        # Nomes e ids.
        names = ", ".join(f"{c.id} = {c.name}" for c in session.scalars(select(Company)))
        # 404 = não existe.
        raise ConversaError(f"Empresa {company_id} não existe. Empresas: {names}.", status=404)
    # Devolve.
    return company


# Um projeto da Empresa (None quando o filtro não foi pedido).
def project_or_error(session: Session, company_id: int, project_id: int | None) -> Project | None:
    # Sem filtro.
    if project_id is None:
        return None
    # O projeto.
    project = session.get(Project, project_id)
    # Não existe ou é de outra Empresa: mesma resposta (não revela o que há em outra).
    if project is None or project.company_id != company_id:
        raise ConversaError(f"Projeto {project_id} não existe nesta Empresa.", status=404)
    # Devolve.
    return project


# Início de um dia no fuso do console, em UTC (o banco guarda em UTC).
def day_start(day: date, settings: Settings) -> datetime:
    # Meia-noite local, convertida para UTC.
    return datetime.combine(day, time.min, ZoneInfo(settings.console_timezone)).astimezone(UTC)


# Data e hora no fuso do console, em texto ISO ("2026-11-05T09:30-03:00"), ou None.
def local_iso(value: datetime | None, settings: Settings) -> str | None:
    # Sem data.
    if value is None:
        return None
    # Converte e escreve até os minutos.
    return value.astimezone(ZoneInfo(settings.console_timezone)).isoformat(timespec="minutes")


# Link de uma página do console (o Claude mostra para você clicar).
def console_link(settings: Settings, path: str) -> str:
    # Endereço do console na rede Tailscale + caminho, sem barra repetida.
    return settings.public_base_url.rstrip("/") + path


# A fonte de um item, no formato que toda resposta usa.
def source_ref(item: BronzeItem, settings: Settings) -> dict:
    # Dicionário (vira JSON).
    return {
        # Id do item (a ferramenta ler_fonte recebe este número).
        "id": item.id,
        # Tipo legível ("e-mail", "transcrição de reunião").
        "tipo": SOURCE_LABELS.get(item.source, str(item.source)),
        # Assunto ou título.
        "titulo": item.subject or "(sem título)",
        # Quem enviou ou falou.
        "autor": item.author,
        # Quando aconteceu, no fuso do console.
        "data": local_iso(item.occurred_at, settings),
        # Página do item no console.
        "link": console_link(settings, f"/conteudo/{item.id}"),
    }


# Projeto de cada item, pela classificação (bloco 3): id do item → nome do projeto.
def item_projects(session: Session, item_ids: list[int]) -> dict[int, str]:
    # Nada para buscar.
    if not item_ids:
        return {}
    # Consulta: classificação com projeto.
    rows = session.execute(
        select(ItemClassification.bronze_item_id, Project.name)
        .join(Project, Project.id == ItemClassification.project_id)
        .where(ItemClassification.bronze_item_id.in_(item_ids))
    ).all()
    # Item → nome.
    return dict(rows)


# Registra no diário (Processamento) o que a conversa leu: o que saiu do servidor para
# o Claude fica visível, com a Empresa.
def log_use(session: Session, actor: str, tool: str, company_id: int | None, **details) -> None:
    # Ação "conversa.<ferramenta>"; detalhes sem valores vazios, para o diário ficar limpo.
    audit(
        session,
        actor,
        f"conversa.{tool}",
        company_id=company_id,
        **{k: v for k, v in details.items() if v not in (None, "", [], {})},
    )


# --- Empresas e projetos ---------------------------------------------------------


# As Empresas, com os projetos e o que está aberto em cada um. É a primeira ferramenta
# que o Claude chama: sem ela, ele não sabe qual número é qual Empresa.
def companies_overview(session: Session, settings: Settings) -> list[dict]:
    # Fatos abertos por projeto e tipo: (projeto, tipo) → quantidade.
    open_counts: dict[tuple[int, FactKind], int] = {
        (project_id, kind): total
        for project_id, kind, total in session.execute(
            select(Fact.project_id, Fact.kind, func.count())
            .where(
                Fact.superseded_by_id.is_(None),
                Fact.status == "aberto",
                Fact.quote_verified.is_(True),
                Fact.project_id.is_not(None),
            )
            .group_by(Fact.project_id, Fact.kind)
        )
    }
    # Alertas abertos (novo ou visto) por Empresa, contando cada fato uma vez.
    alert_counts = dict(
        session.execute(
            select(Alert.company_id, func.count(func.distinct(Alert.fact_id)))
            .where(Alert.status.in_(("novo", "visto")))
            .group_by(Alert.company_id)
        ).all()
    )
    # Conta Google de cada Empresa.
    accounts = dict(session.execute(select(OAuthToken.company_id, OAuthToken.account_email)).all())
    # Saída.
    result = []
    # Cada Empresa, em ordem alfabética.
    for company in session.scalars(select(Company).order_by(func.lower(Company.name))):
        # Projetos dela, ativos primeiro.
        projects = session.scalars(
            select(Project)
            .where(Project.company_id == company.id)
            .order_by(Project.active.desc(), func.lower(Project.name))
        ).all()
        # Linha da Empresa.
        result.append(
            {
                # Id (todas as outras ferramentas recebem este número).
                "id": company.id,
                # Nome.
                "nome": company.name,
                # Conta Google conectada (ou None).
                "conta_google": accounts.get(company.id),
                # Alertas abertos.
                "alertas_abertos": alert_counts.get(company.id, 0),
                # Projetos.
                "projetos": [
                    {
                        # Id (filtro "projeto" das outras ferramentas).
                        "id": p.id,
                        # Nome e código.
                        "nome": p.name,
                        "codigo": p.code,
                        # Ativo ou encerrado.
                        "ativo": p.active,
                        # Fatos abertos e conferidos, por tipo (só os tipos com algum).
                        "abertos": {
                            KIND_LABELS[kind]: open_counts[(p.id, kind)]
                            for kind in KIND_LABELS
                            if open_counts.get((p.id, kind))
                        },
                    }
                    for p in projects
                ],
            }
        )
    # Devolve.
    return result


# --- Busca por palavra -------------------------------------------------------------


# A pergunta da busca como tsquery: a mesma frase pelas duas configurações, com OU (||).
# O índice tem as palavras pelas duas (migração 0017), então tanto "reunião" quanto
# "reuniao" acham. websearch_to_tsquery entende a sintaxe de buscador: "frase exata",
# OR e -palavra (excluir).
def _tsquery(text: str):
    # Com acento OU sem acento.
    return func.websearch_to_tsquery(_PT, text).op("||")(func.websearch_to_tsquery(_PT_PLAIN, text))


# Busca palavras nos itens (e-mails, transcrições, arquivos do Drive) e nos anexos de
# uma Empresa. Devolve os mais relevantes, com um trecho de cada.
def search(
    session: Session,
    settings: Settings,
    company_id: int,
    query: str,
    *,
    project_id: int | None = None,
    source: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    # A Empresa existe.
    company_or_error(session, company_id)
    # O projeto, se pedido, é dela.
    project_or_error(session, company_id, project_id)
    # Pergunta sem espaços nas pontas.
    query = (query or "").strip()
    # Vazia: não há o que buscar.
    if not query:
        raise ConversaError("Diga as palavras a buscar.")
    # Longa demais: provavelmente um texto colado.
    if len(query) > MAX_QUERY:
        raise ConversaError(f"A busca aceita até {MAX_QUERY} caracteres.")
    # Fonte: só as conhecidas.
    if source and source not in SOURCE_FILTERS:
        raise ConversaError(f"Fonte desconhecida: use {', '.join(SOURCE_FILTERS)}.")
    # Tamanho da página.
    size = page_size(settings, limit)
    # Posição de início, nunca negativa.
    offset = max(0, offset)
    # A pergunta como tsquery.
    tsq = _tsquery(query)
    # Condições comuns aos itens e aos anexos: a Empresa e os filtros.
    filters = [BronzeItem.company_id == company_id]
    # Uma fonte.
    if source:
        filters.append(BronzeItem.source == SOURCE_FILTERS[source])
    # A partir de um dia (inclusive).
    if since:
        filters.append(BronzeItem.occurred_at >= day_start(since, settings))
    # Até um dia (inclusive: antes do começo do dia seguinte).
    if until:
        filters.append(BronzeItem.occurred_at < day_start(until + timedelta(days=1), settings))
    # Um projeto: o item tem essa classificação.
    if project_id is not None:
        filters.append(
            exists().where(
                ItemClassification.bronze_item_id == BronzeItem.id,
                ItemClassification.project_id == project_id,
            )
        )
    # Quantos buscar de cada lado: o suficiente para montar a página depois de juntar.
    wanted = offset + size
    # Itens cujo texto tem as palavras, com a nota de relevância (ts_rank_cd: mais alta
    # quando as palavras aparecem muitas vezes e perto umas das outras).
    item_rank = func.ts_rank_cd(BronzeItem.search_vector, tsq)
    # Consulta dos itens.
    item_hits = session.execute(
        select(BronzeItem.id, item_rank)
        .where(BronzeItem.search_vector.op("@@")(tsq), *filters)
        .order_by(item_rank.desc(), BronzeItem.occurred_at.desc().nulls_last())
        .limit(wanted)
    ).all()
    # Nota dos anexos.
    attachment_rank = func.ts_rank_cd(Attachment.search_vector, tsq)
    # Anexos cujo texto tem as palavras, com o item a que pertencem.
    attachment_hits = session.execute(
        select(Attachment.id, Attachment.bronze_item_id, attachment_rank)
        .join(BronzeItem, BronzeItem.id == Attachment.bronze_item_id)
        .where(Attachment.search_vector.op("@@")(tsq), *filters)
        .order_by(attachment_rank.desc(), Attachment.id.desc())
        .limit(wanted)
    ).all()
    # Junta os dois lados numa lista só: (nota, "item" ou "anexo", id, id do item).
    merged = [(rank, "item", item_id, item_id) for item_id, rank in item_hits]
    # Os anexos.
    merged += [(rank, "anexo", att_id, item_id) for att_id, item_id, rank in attachment_hits]
    # Mais relevantes primeiro.
    merged.sort(key=lambda hit: hit[0], reverse=True)
    # A página pedida.
    page = merged[offset : offset + size]
    # Itens da página (para o título, a data e o autor).
    items = {
        i.id: i
        for i in session.scalars(
            select(BronzeItem).where(BronzeItem.id.in_({hit[3] for hit in page}))
        )
    }
    # Trechos dos itens: ts_headline marca as palavras achadas dentro do texto.
    item_ids = [hit[2] for hit in page if hit[1] == "item"]
    # Consulta dos trechos (só para os itens da página: o trecho é calculado na hora).
    item_snippets = dict(
        session.execute(
            select(
                BronzeItem.id,
                func.ts_headline(
                    _PT_PLAIN,
                    func.left(
                        func.coalesce(BronzeItem.subject, "") + "\n" + BronzeItem.body_text, 100000
                    ),
                    tsq,
                    _HEADLINE,
                ),
            ).where(BronzeItem.id.in_(item_ids))
        ).all()
    )
    # Anexos da página.
    attachment_ids = [hit[2] for hit in page if hit[1] == "anexo"]
    # Nome e trecho de cada anexo.
    attachment_rows = {
        att_id: (filename, snippet)
        for att_id, filename, snippet in session.execute(
            select(
                Attachment.id,
                Attachment.filename,
                func.ts_headline(
                    _PT_PLAIN,
                    func.left(func.coalesce(Attachment.extracted_text, ""), 100000),
                    tsq,
                    _HEADLINE,
                ),
            ).where(Attachment.id.in_(attachment_ids))
        )
    }
    # Projeto de cada item.
    projects = item_projects(session, list(items))
    # Resultados.
    results = []
    # Cada acerto, na ordem de relevância.
    for rank, kind, hit_id, item_id in page:
        # O item (do acerto ou dono do anexo).
        item = items[item_id]
        # Linha comum.
        row = {
            # De onde veio.
            "fonte": source_ref(item, settings),
            # Projeto do item, pela classificação.
            "projeto": projects.get(item_id),
            # Nota de relevância (só para ordenar; não é porcentagem).
            "relevancia": round(float(rank), 4),
        }
        # Achado no texto do item.
        if kind == "item":
            # Onde: o texto do item.
            row["onde"] = "texto"
            # Trecho.
            row["trecho"] = item_snippets.get(hit_id, "")
        # Achado num anexo.
        else:
            # Nome e trecho do anexo.
            filename, snippet = attachment_rows[hit_id]
            # Onde: o anexo, com o id para ler inteiro.
            row["onde"] = f"anexo: {filename}"
            row["anexo_id"] = hit_id
            # Trecho.
            row["trecho"] = snippet
        # Acrescenta.
        results.append(row)
    # Há mais além desta página? (algum dos lados encheu o que foi pedido)
    more = len(merged) > offset + size
    # Devolve.
    return {
        "resultados": results,
        # Próxima posição, para pedir a página seguinte (None = acabou).
        "proximo_inicio": offset + size if more else None,
    }


# --- Fatos -----------------------------------------------------------------------


# Um fato no formato das respostas.
def fact_dict(
    fact: Fact,
    item: BronzeItem,
    project: Project | None,
    owner: Stakeholder | None,
    speaker: Stakeholder | None,
    settings: Settings,
    on_agenda: bool,
) -> dict:
    # Dicionário (vira JSON).
    return {
        # Id (a ferramenta por_na_agenda recebe este número).
        "id": fact.id,
        # Tipo legível.
        "tipo": KIND_LABELS.get(fact.kind, str(fact.kind)),
        # Título e descrição.
        "titulo": fact.title,
        "descricao": fact.description,
        # Projeto.
        "projeto": project.name if project else None,
        # Responsável: a Pessoa cadastrada ou o nome como apareceu no texto.
        "responsavel": owner.name if owner else fact.owner_text,
        # Quem disse.
        "quem_disse": speaker.name if speaker else fact.said_by_text,
        # Prazo (ISO, AAAA-MM-DD).
        "prazo": fact.due_date.isoformat() if fact.due_date else None,
        # Situação (aberto, concluido; vazio nos tipos sem ciclo, como decisão).
        "situacao": fact.status,
        # Trecho literal da fonte que sustenta o fato.
        "trecho": fact.source_quote,
        # True: o código achou o trecho no texto original. False: suspeito.
        "conferido": fact.quote_verified,
        # Já tem evento na agenda criado pelo assistente.
        "na_agenda": on_agenda,
        # De onde saiu.
        "fonte": source_ref(item, settings),
    }


# Fatos de uma Empresa com filtros (tarefas, riscos, decisões, prazos...).
def facts(
    session: Session,
    settings: Settings,
    company_id: int,
    *,
    kind: str | None = None,
    project_id: int | None = None,
    open_only: bool = True,
    owner: str | None = None,
    due_until: date | None = None,
    since: date | None = None,
    include_suspicious: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    # A Empresa existe.
    company_or_error(session, company_id)
    # O projeto é dela.
    project_or_error(session, company_id, project_id)
    # Tipo: só os conhecidos.
    if kind and kind not in KIND_FILTERS:
        raise ConversaError(f"Tipo desconhecido: use {', '.join(KIND_FILTERS)}.")
    # Tamanho da página.
    size = page_size(settings, limit)
    # Dois apelidos de Pessoa: responsável e quem disse.
    owner_person = aliased(Stakeholder)
    # Quem disse.
    speaker = aliased(Stakeholder)
    # Consulta: fato, item de origem, projeto e as duas Pessoas.
    query = (
        select(Fact, BronzeItem, Project, owner_person, speaker)
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        .outerjoin(Project, Project.id == Fact.project_id)
        .outerjoin(owner_person, owner_person.id == Fact.owner_id)
        .outerjoin(speaker, speaker.id == Fact.said_by_id)
        # Só fatos valendo (os substituídos por "Extrair de novo" ficam fora) e da Empresa.
        .where(Fact.superseded_by_id.is_(None), BronzeItem.company_id == company_id)
    )
    # Suspeitos ficam fora, salvo pedido: o trecho deles não foi achado no original.
    if not include_suspicious:
        query = query.where(Fact.quote_verified.is_(True))
    # Um tipo.
    if kind:
        query = query.where(Fact.kind == KIND_FILTERS[kind])
    # Um projeto.
    if project_id is not None:
        query = query.where(Fact.project_id == project_id)
    # Só abertos: situação "aberto" (decisões e definições não têm situação e saem).
    if open_only:
        query = query.where(Fact.status == "aberto")
    # Responsável: "eu" usa a mesma regra dos alertas; outro texto procura no nome.
    if owner:
        # Eu.
        if owner.strip().lower() == "eu":
            query = query.where(owner_is_me(settings))
        # Parte do nome, sem diferença de maiúsculas, na Pessoa ou no texto do fato.
        else:
            # Padrão "contém".
            pattern = f"%{owner.strip()}%"
            # Na Pessoa cadastrada ou no nome como apareceu no texto.
            query = query.where(
                or_(owner_person.name.ilike(pattern), Fact.owner_text.ilike(pattern))
            )
    # Prazo até um dia (inclusive): só fatos com prazo.
    if due_until:
        query = query.where(Fact.due_date.is_not(None), Fact.due_date <= due_until)
    # Ditos a partir de um dia.
    if since:
        query = query.where(BronzeItem.occurred_at >= day_start(since, settings))
    # Ordem: com prazo, o prazo mais perto primeiro; depois os mais recentes.
    query = query.order_by(
        Fact.due_date.asc().nulls_last(), BronzeItem.occurred_at.desc().nulls_last(), Fact.id
    )
    # Uma linha a mais que a página: se vier, há próxima página.
    rows = session.execute(query.offset(max(0, offset)).limit(size + 1)).all()
    # Há mais?
    more = len(rows) > size
    # Só a página.
    rows = rows[:size]
    # Quais já estão na agenda.
    on_agenda = active_by_fact(session, [row[0].id for row in rows])
    # Devolve.
    return {
        "fatos": [
            fact_dict(f, item, project, person, said, settings, f.id in on_agenda)
            for f, item, project, person, said in rows
        ],
        # Próxima posição (None = acabou).
        "proximo_inicio": max(0, offset) + size if more else None,
    }


# --- Alertas e briefing ----------------------------------------------------------------


# Alertas abertos (novo ou visto) de uma Empresa: uma linha por fato, a regra mais forte.
def alerts(session: Session, settings: Settings, company_id: int, limit: int | None = None) -> dict:
    # A Empresa existe.
    company_or_error(session, company_id)
    # Tamanho da página.
    size = page_size(settings, limit)
    # Abertos, já em ordem (novos e fortes primeiro), agrupados por fato.
    grouped = group_by_fact(list_alerts(session, company_id, status="abertos"))
    # Quais fatos já estão na agenda.
    on_agenda = active_by_fact(session, [row[2].id for row, _ in grouped[:size]])
    # Saída.
    result = []
    # Cada linha: (alerta, regra, fato, item, projeto, quem disse, responsável) e as
    # outras regras do mesmo fato.
    for (alert, rule, fact, item, project, speaker, owner), others in grouped[:size]:
        # Linha.
        result.append(
            {
                # Peso (alta, media, baixa) e situação (novo, visto).
                "peso": alert.severity,
                "situacao": alert.status,
                # Regra que disparou e as outras.
                "regra": rule.name,
                "outras_regras": others,
                # Por que: quem disse e a liderança.
                "por_que": why(alert, speaker),
                # Quando o alerta nasceu.
                "desde": local_iso(alert.created_at, settings),
                # O fato completo, com a fonte.
                "fato": fact_dict(
                    fact, item, project, owner, speaker, settings, fact.id in on_agenda
                ),
                # Página Alertas da Empresa (onde se marca visto ou resolvido).
                "link": console_link(settings, f"/alertas?empresa={company_id}"),
            }
        )
    # Devolve, com o total (pode haver mais que a página).
    return {"alertas": result, "total_abertos": len(grouped)}


# O briefing de um dia (hoje, por padrão) de uma Empresa, como foi montado às 7h.
def briefing(session: Session, settings: Settings, company_id: int, day: date) -> dict:
    # A Empresa existe.
    company_or_error(session, company_id)
    # Briefing do dia.
    found = of_day(session, day, company_id)
    # Não montado: diz por quê, para o Claude não inventar um.
    if not found:
        return {
            "briefing": None,
            "aviso": (
                f"Não há briefing de {day.strftime('%d/%m/%Y')} para esta Empresa. O coletor "
                f"monta a partir das {settings.briefing_hour}h; antes disso, ou com o coletor "
                "parado, ele não existe."
            ),
        }
    # O briefing (um por Empresa por dia).
    snapshot = found[0]
    # Devolve. Ler pelo Claude não marca como lido: a janela de "o que mudou" do próximo
    # briefing continua contando a partir da última vez que você abriu a página.
    return {
        "briefing": snapshot.content,
        # Quando foi montado e desde quando conta "o que mudou".
        "montado_em": local_iso(snapshot.generated_at, settings),
        "desde": local_iso(snapshot.since, settings),
        # Página do briefing.
        "link": console_link(settings, f"/briefing?empresa={company_id}&data={day.isoformat()}"),
    }


# --- Reuniões ------------------------------------------------------------------------


# Reuniões de uma Empresa num período, com participantes e transcrições.
def meetings(
    session: Session,
    settings: Settings,
    company_id: int,
    *,
    since: date | None = None,
    until: date | None = None,
    title: str | None = None,
    limit: int | None = None,
) -> dict:
    # A Empresa existe.
    company_or_error(session, company_id)
    # Tamanho da página.
    size = page_size(settings, limit)
    # Hoje no fuso do console.
    today = datetime.now(UTC).astimezone(ZoneInfo(settings.console_timezone)).date()
    # Período padrão: as duas últimas semanas e a próxima.
    since = since or today - timedelta(days=14)
    # Até.
    until = until or today + timedelta(days=7)
    # Período invertido.
    if until < since:
        raise ConversaError("O fim do período vem antes do início.")
    # Consulta: reuniões da Empresa no período, as mais recentes primeiro.
    query = (
        select(Meeting)
        .where(
            Meeting.company_id == company_id,
            Meeting.starts_at >= day_start(since, settings),
            Meeting.starts_at < day_start(until + timedelta(days=1), settings),
        )
        .order_by(Meeting.starts_at.desc())
        .limit(size)
    )
    # Parte do título.
    if title:
        query = query.where(Meeting.title.ilike(f"%{title.strip()}%"))
    # Executa.
    found = session.scalars(query).all()
    # Participantes de todas, de uma vez: reunião → [nomes].
    people: dict[int, list[str]] = {}
    # Nome do participante, preferindo o da Pessoa cadastrada.
    for meeting_id, name, person in session.execute(
        select(MeetingAttendee.meeting_id, MeetingAttendee.name, Stakeholder.name)
        .outerjoin(Stakeholder, Stakeholder.id == MeetingAttendee.stakeholder_id)
        .where(MeetingAttendee.meeting_id.in_([m.id for m in found]))
        .order_by(MeetingAttendee.name_key)
    ):
        # Acrescenta.
        people.setdefault(meeting_id, []).append(person or name)
    # Transcrições de todas: reunião → [itens].
    transcripts: dict[int, list[BronzeItem]] = {}
    # Itens ligados às reuniões.
    for item in session.scalars(
        select(BronzeItem)
        .where(BronzeItem.meeting_id.in_([m.id for m in found]))
        .order_by(BronzeItem.id)
    ):
        # Acrescenta.
        transcripts.setdefault(item.meeting_id, []).append(item)
    # Devolve.
    return {
        "reunioes": [
            {
                # Título.
                "titulo": m.title,
                # Início e fim no fuso do console.
                "inicio": local_iso(m.starts_at, settings),
                "fim": local_iso(m.ends_at, settings),
                # Horário conhecido (False: importada sem agenda, só com o dia).
                "horario_conhecido": m.time_known,
                # Organizador e plataforma.
                "organizador": m.organizer,
                "plataforma": m.platform,
                # Participantes.
                "participantes": people.get(m.id, []),
                # Transcrições (ler com ler_fonte).
                "transcricoes": [source_ref(t, settings) for t in transcripts.get(m.id, [])],
            }
            for m in found
        ]
    }


# --- Ler uma fonte -----------------------------------------------------------------------


# Um pedaço de um texto longo: o texto, o total e a próxima posição.
def _page_of(text: str, start: int, size: int) -> dict:
    # Posição de início, dentro do texto.
    start = max(0, min(start, len(text)))
    # Fim do pedaço.
    end = min(len(text), start + size)
    # Devolve.
    return {
        "texto": text[start:end],
        # Tamanho total, para o Claude saber quanto falta.
        "total_caracteres": len(text),
        "inicio": start,
        # Próxima posição (None = acabou).
        "proximo_inicio": end if end < len(text) else None,
    }


# O texto de um item (ou de um anexo dele), em páginas de MCP_SOURCE_CHARS caracteres.
def read_source(
    session: Session,
    settings: Settings,
    company_id: int,
    item_id: int,
    *,
    attachment_id: int | None = None,
    start: int = 0,
    full_thread: bool = False,
) -> dict:
    # A Empresa existe.
    company_or_error(session, company_id)
    # O item.
    item = session.get(BronzeItem, item_id)
    # Não existe ou é de outra Empresa: mesma resposta.
    if item is None or item.company_id != company_id:
        raise ConversaError(f"Item {item_id} não existe nesta Empresa.", status=404)
    # Anexos do item, com texto ou não.
    attachments = session.scalars(
        select(Attachment).where(Attachment.bronze_item_id == item.id).order_by(Attachment.id)
    ).all()
    # Um anexo pedido.
    if attachment_id is not None:
        # Procura entre os do item (um anexo de outro item não serve).
        chosen = next((a for a in attachments if a.id == attachment_id), None)
        # Não é deste item.
        if chosen is None:
            raise ConversaError(f"O item {item_id} não tem o anexo {attachment_id}.", status=404)
        # Sem texto.
        if not (chosen.extracted_text or "").strip():
            raise ConversaError(
                f"O anexo {chosen.filename} não tem texto lido"
                + (f" ({chosen.extraction_error})." if chosen.extraction_error else ".")
            )
        # O pedaço do texto do anexo, com a fonte.
        return {
            "fonte": source_ref(item, settings),
            "anexo": chosen.filename,
            **_page_of(chosen.extracted_text, start, settings.mcp_source_chars),
        }
    # E-mail: o texto próprio (sem a conversa citada abaixo), salvo pedido da conversa
    # inteira. As mensagens antigas da conversa são itens próprios e aparecem na busca.
    if item.source == SourceKind.GMAIL and not full_thread:
        text = own_text(item.subject, item.body_text)
    # Outras fontes (e a conversa inteira): o texto como chegou.
    else:
        text = item.body_text or ""
    # Fatos extraídos deste item (para o Claude ligar o texto ao que já foi apurado).
    item_facts = session.scalars(
        select(Fact)
        .where(Fact.source_item_id == item.id, Fact.superseded_by_id.is_(None))
        .order_by(Fact.id)
        .limit(FACTS_OF_SOURCE)
    ).all()
    # Participantes, se for uma transcrição ligada a uma reunião.
    attendees = (
        session.scalars(
            select(MeetingAttendee.name)
            .where(MeetingAttendee.meeting_id == item.meeting_id)
            .order_by(MeetingAttendee.name_key)
        ).all()
        if item.meeting_id
        else []
    )
    # Destinatários do e-mail (gravados no extra pela coleta).
    extra = item.extra or {}
    # Devolve.
    return {
        "fonte": source_ref(item, settings),
        # Projeto pela classificação.
        "projeto": item_projects(session, [item.id]).get(item.id),
        # Para e Cc (e-mail).
        "para": extra.get("para"),
        "cc": extra.get("cc"),
        # Participantes (reunião).
        "participantes": attendees,
        # Anexos: id, nome e se têm texto (ler com anexo_id).
        "anexos": [
            {
                "id": a.id,
                "nome": a.filename,
                "tem_texto": bool((a.extracted_text or "").strip()),
                "origem": a.origin,
            }
            for a in attachments
        ],
        # Fatos já extraídos deste item (id, tipo, título, conferido).
        "fatos": [
            {
                "id": f.id,
                "tipo": KIND_LABELS.get(f.kind, str(f.kind)),
                "titulo": f.title,
                "conferido": f.quote_verified,
            }
            for f in item_facts
        ],
        # O pedaço do texto.
        **_page_of(text, start, settings.mcp_source_chars),
    }


# --- Escrita: pôr um prazo na agenda -------------------------------------------------------


# Cria o evento de um fato na agenda da Empresa, com as regras do PR #22 (dia inteiro,
# livre, privado, sem convidados, marca invisível). É a única ferramenta que escreve.
def put_on_agenda(
    session: Session,
    settings: Settings,
    company_id: int,
    fact_id: int,
    actor: str,
    *,
    calendar_for=None,
) -> dict:
    # A Empresa existe.
    company_or_error(session, company_id)
    # A Empresa do fato (pelo item de onde saiu).
    fact_company = session.scalar(
        select(BronzeItem.company_id)
        .join(Fact, Fact.source_item_id == BronzeItem.id)
        .where(Fact.id == fact_id)
    )
    # Fato de outra Empresa: mesma resposta de inexistente.
    if fact_company is not None and fact_company != company_id:
        raise ConversaError(f"Fato {fact_id} não existe nesta Empresa.", status=404)
    # Cria (as regras de "pode ou não pode" ficam em create_for_fact).
    try:
        row = create_for_fact(session, settings, fact_id, actor, calendar_for=calendar_for)
    # Não pode (sem prazo, suspeito, concluído, já na agenda, Google fora): a frase volta.
    except EventError as exc:
        raise ConversaError(str(exc)) from exc
    # Devolve o que foi criado.
    return {
        "criado": True,
        "titulo": row.title,
        "dia": row.day.isoformat(),
        # Página Na agenda (onde se apaga, um por um ou todos).
        "link": console_link(settings, f"/eventos?empresa={company_id}"),
        "aviso": "Evento de dia inteiro, livre e particular, sem convidados.",
    }
