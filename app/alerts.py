"""Alertas sobre o que lideranças pediram, decidiram ou apontaram (bloco 3, terceira parte).

O que resolve: a dona pediu (02/11/2026) alertas baseados em comentários e pedidos de
stakeholders em posição de liderança NA EMPRESA e NO PROJETO. Com a extração de fatos
(app/facts.py), cada tarefa, decisão ou risco já sabe quem disse (facts.said_by_id); e
os cadastros sabem quem é liderança (stakeholders.is_leader e
project_stakeholders.is_leader). Falta cruzar as duas coisas e avisar.

Como funciona:
1. uma REGRA é um filtro que a dona monta no console: tipos de fato (tarefa, decisão...),
   que liderança disse (na empresa, no projeto ou qualquer uma), condição do responsável
   (qualquer, sem responsável ou ela mesma), prazo em até N dias e o peso do alerta;
2. a cada 5 minutos o coletor aplica cada regra ligada com uma consulta SQL e grava um
   ALERTA por fato novo que passou. O par (regra, fato) é único: nada se repete;
3. a página Alertas mostra os novos primeiro, com o trecho, quem disse e por que
   disparou; a dona marca como visto, resolvido (fecha o fato) ou descartado.

Por que filtros e não "regra em texto livre para o Claude": cada alerta fica explicável
e conferível ("disparou porque a Márcia, liderança no projeto, pediu uma tarefa"), o
custo é zero e a resposta é a mesma toda vez. Pedir ao Claude para julgar cada fato a
cada 5 minutos custaria dinheiro e daria respostas diferentes para o mesmo fato.

Por que só fatos com o trecho CONFERIDO e quem disse RECONHECIDO: "a diretora pediu X"
atribuído à pessoa errada, ou com um pedido inventado, leva a uma ação errada com cara de
certeza. Nenhum alerta é melhor que um alerta falso (mesma regra do PR #10 para falas).
Os suspeitos continuam na página Fatos, à vista.

Separação por Empresa (premissa 6): o alerta leva a Empresa do item de onde o fato saiu;
a regra pode valer para uma Empresa ou para todas; o banco confere que alerta e regra
não são de Empresas diferentes (gatilho da 0013).
"""

# dataclass: classes simples de dados (o resumo da rodada).
from dataclasses import dataclass, field

# Data, data e hora com fuso e intervalos.
from datetime import UTC, date, datetime, timedelta

# ZoneInfo: o "hoje" do prazo é o dia no fuso do console, não o de Londres (UTC).
from zoneinfo import ZoneInfo

# Consultas: or_ junta condições, case ordena, exists pergunta "existe linha?", false é
# a condição que nunca vale.
from sqlalchemy import case, exists, false, func, or_, select

# Session é a conversa com o banco; aliased dá dois apelidos à mesma tabela.
from sqlalchemy.orm import Session, aliased

# Configuração (fuso, janela de dias, login da dona).
from app.config import Settings

# Nome de cada tipo de fato na tela.
from app.facts import KIND_LABELS

# Tabelas.
from app.models import (
    Alert,
    AlertRule,
    BronzeItem,
    Company,
    Fact,
    FactKind,
    OAuthToken,
    Project,
    ProjectStakeholder,
    Stakeholder,
    audit,
)

# Que liderança disse: valor gravado → texto da tela.
LEADERSHIP = {
    # Liderança na empresa OU no projeto do fato.
    "qualquer": "Liderança na empresa ou no projeto",
    # Só quem tem a marca na Pessoa (diretoria, gerência).
    "empresa": "Só liderança na empresa",
    # Só quem é liderança na equipe do projeto do fato (patrocinadora, PO).
    "projeto": "Só liderança no projeto",
}
# Condição do responsável: valor gravado → texto da tela.
OWNER_RULES = {
    # O responsável não importa.
    "qualquer": "Qualquer responsável",
    # Pedido sem dono: ninguém foi nomeado (é o que mais cai no esquecimento).
    "sem_responsavel": "Sem responsável",
    # O responsável é a dona do console (a Pessoa com o e-mail dela).
    "eu": "O responsável sou eu",
}
# Peso: valor gravado → texto da tela, do mais forte ao mais fraco.
SEVERITIES = {"alta": "Alta", "media": "Média", "baixa": "Baixa"}
# Situações do alerta: valor gravado → texto da tela.
STATUSES = {
    # Acabou de nascer; conta no número do menu.
    "novo": "Novo",
    # Você leu, mas ainda não resolveu.
    "visto": "Visto",
    # Resolvido: o fato aberto (tarefa, prazo...) é fechado junto.
    "resolvido": "Resolvido",
    # Não procede (ex.: quem disse foi reconhecido errado).
    "descartado": "Descartado",
}
# Maior prazo aceito numa regra, em dias (60 dias já é planejamento, não alerta).
MAX_DUE_DAYS = 60
# Situação que o fato ganha quando o alerta é resolvido.
DONE = "concluido"
# Situações "abertas" (ainda pedem algo de você).
OPEN = ("novo", "visto")


# Erro de preenchimento de uma regra (a página mostra o texto).
class RuleError(ValueError):
    pass


# Resumo de uma rodada, para o log do coletor e o comando gerar-alertas.
@dataclass
class AlertSummary:
    # Alertas criados por regra: nome da regra → quantos.
    by_rule: dict[str, int] = field(default_factory=dict)

    # Total criado.
    @property
    def created(self) -> int:
        # Soma das regras.
        return sum(self.by_rule.values())

    # Uma linha legível.
    def describe(self) -> str:
        # Nenhum novo.
        if not self.created:
            return "Alertas: nenhum novo."
        # "Pedido de liderança: 2; Decisão de liderança: 1".
        parts = "; ".join(f"{name}: {n}" for name, n in self.by_rule.items() if n)
        # Linha.
        return f"Alertas: {self.created} novo(s) ({parts})."


# Dia de hoje no fuso do console.
def today_local(settings: Settings, now: datetime | None = None) -> date:
    # Agora em UTC (ou o momento dado pelo teste), convertido para o fuso.
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(settings.console_timezone)).date()


# Confere e grava uma regra (nova quando rule_id é None). Devolve a regra.
def save_rule(
    session: Session,
    rule_id: int | None,
    *,
    name: str,
    company_id: int | None,
    kinds: list[str],
    leadership: str,
    owner_rule: str,
    due_within_days: int | None,
    severity: str,
    active: bool,
    actor: str,
) -> AlertRule:
    # Nome sem espaços nas pontas.
    name = (name or "").strip()
    # Nome obrigatório.
    if not name:
        raise RuleError("Dê um nome à regra.")
    # Nome curto (é o título do alerta).
    if len(name) > 120:
        raise RuleError("O nome da regra tem no máximo 120 caracteres.")
    # Tipos conhecidos (valores de FactKind).
    known = {k.value for k in FactKind}
    # Algum tipo desconhecido (formulário adulterado).
    if any(k not in known for k in kinds):
        raise RuleError("Tipo de fato desconhecido.")
    # Liderança válida.
    if leadership not in LEADERSHIP:
        raise RuleError("Escolha que liderança conta.")
    # Responsável válido.
    if owner_rule not in OWNER_RULES:
        raise RuleError("Escolha a condição do responsável.")
    # Peso válido.
    if severity not in SEVERITIES:
        raise RuleError("Escolha o peso do alerta.")
    # Prazo entre 0 e MAX_DUE_DAYS (0 = vence hoje ou já venceu).
    if due_within_days is not None and not 0 <= due_within_days <= MAX_DUE_DAYS:
        raise RuleError(f"O prazo vai de 0 a {MAX_DUE_DAYS} dias.")
    # Empresa, quando dada, precisa existir.
    if company_id is not None and session.get(Company, company_id) is None:
        raise RuleError("Empresa não encontrada.")
    # Regra nova.
    if rule_id is None:
        # Cria.
        rule = AlertRule()
        # Prepara a gravação.
        session.add(rule)
    # Regra existente.
    else:
        # Busca.
        rule = session.get(AlertRule, rule_id)
        # Não existe.
        if rule is None:
            raise RuleError("Regra não encontrada.")
    # Campos.
    rule.name = name
    # Empresa (vazia = todas).
    rule.company_id = company_id
    # Tipos sem repetição, na ordem da lista de tipos (vazia = todos).
    rule.fact_kinds = [k.value for k in FactKind if k.value in kinds]
    # Liderança.
    rule.leadership = leadership
    # Responsável.
    rule.owner_rule = owner_rule
    # Prazo.
    rule.due_within_days = due_within_days
    # Peso.
    rule.severity = severity
    # Ligada ou não.
    rule.active = active
    # Envia ao banco (para ter o id no diário).
    session.flush()
    # Diário: quem mudou a regra e como ficou.
    audit(
        session,
        actor,
        "alertas.regra",
        target=str(rule.id),
        company_id=company_id,
        nome=name,
        tipos=rule.fact_kinds,
        lideranca=leadership,
        responsavel=owner_rule,
        prazo_dias=due_within_days,
        peso=severity,
        ativa=active,
    )
    # Devolve.
    return rule


# Apaga uma regra e os alertas dela (o banco apaga em cascata).
def delete_rule(session: Session, rule_id: int, actor: str) -> None:
    # A regra.
    rule = session.get(AlertRule, rule_id)
    # Não existe: nada a fazer.
    if rule is None:
        return
    # Diário antes de apagar (depois não há mais o nome).
    audit(session, actor, "alertas.regra_apagada", target=str(rule.id), nome=rule.name)
    # Apaga.
    session.delete(rule)


# Condição "o responsável sou eu": a Pessoa do fato tem o e-mail da dona.
def _owner_is_me(settings: Settings):
    """O e-mail da dona é o login do Tailscale ou a conta Google conectada na Empresa."""
    # Login do Tailscale (o console só abre para ele), em minúsculas.
    login = (settings.allowed_tailscale_login or "").strip().lower()
    # Conta Google da mesma Empresa da Pessoa com o mesmo e-mail.
    google = exists().where(
        OAuthToken.company_id == Stakeholder.company_id,
        func.lower(OAuthToken.account_email) == Stakeholder.email,
    )
    # Pessoas que são a dona (pelo login ou pela conta Google).
    me = select(Stakeholder.id).where(or_(Stakeholder.email == login, google) if login else google)
    # O responsável do fato é uma delas.
    return Fact.owner_id.in_(me)


# Fatos que passam numa regra e ainda não têm alerta dela.
def matching(session: Session, rule: AlertRule, settings: Settings, now: datetime) -> list:
    """Linhas (id do fato, Empresa, liderança na empresa, liderança no projeto)."""
    # Quem disse (a Pessoa).
    speaker = aliased(Stakeholder)
    # Quem disse é liderança na equipe do projeto do fato.
    project_leader = exists().where(
        ProjectStakeholder.project_id == Fact.project_id,
        ProjectStakeholder.stakeholder_id == Fact.said_by_id,
        ProjectStakeholder.is_leader.is_(True),
    )
    # Quem disse é liderança na empresa (marca na Pessoa).
    company_leader = speaker.is_leader.is_(True)
    # Condição de liderança da regra.
    leader_condition = {
        "qualquer": or_(company_leader, project_leader),
        "empresa": company_leader,
        "projeto": project_leader,
    }.get(rule.leadership, false())
    # Esta regra já disparou para o fato.
    already = exists().where(Alert.rule_id == rule.id, Alert.fact_id == Fact.id)
    # Consulta base: o fato, a Empresa do item e as duas marcas de liderança.
    query = (
        select(Fact.id, BronzeItem.company_id, company_leader, project_leader)
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        # Só fatos com quem disse reconhecido (join, não outerjoin).
        .join(speaker, speaker.id == Fact.said_by_id)
        .where(
            # Fato valendo (não substituído).
            Fact.superseded_by_id.is_(None),
            # Trecho conferido pelo código (suspeito não vira alerta).
            Fact.quote_verified.is_(True),
            # Com projeto (a liderança no projeto depende dele).
            Fact.project_id.is_not(None),
            # Não concluído (resolver um alerta fecha o fato; as outras regras param).
            Fact.status.is_distinct_from(DONE),
            # Quem disse é da mesma Empresa do item (o gatilho da 0012 já garante; aqui
            # fica explícito para quem lê a consulta).
            speaker.company_id == BronzeItem.company_id,
            # Liderança.
            leader_condition,
            # Ainda sem alerta desta regra.
            ~already,
        )
    )
    # Regra de uma Empresa só.
    if rule.company_id is not None:
        query = query.where(BronzeItem.company_id == rule.company_id)
    # Tipos escolhidos (vazio = todos).
    if rule.fact_kinds:
        query = query.where(Fact.kind.in_([FactKind(k) for k in rule.fact_kinds]))
    # Responsável: ninguém nomeado (nem cadastrado, nem como escrito no texto).
    if rule.owner_rule == "sem_responsavel":
        query = query.where(
            Fact.owner_id.is_(None), func.coalesce(func.trim(Fact.owner_text), "") == ""
        )
    # Responsável: a dona.
    elif rule.owner_rule == "eu":
        query = query.where(_owner_is_me(settings))
    # Janela de dias para trás.
    lookback = timedelta(days=settings.alerts_lookback_days)
    # Regra de prazo: vence em até N dias (ou venceu há menos que a janela).
    if rule.due_within_days is not None:
        # Hoje no fuso do console.
        today = today_local(settings, now)
        # Prazo dentro da faixa.
        query = query.where(
            Fact.due_date.is_not(None),
            Fact.due_date <= today + timedelta(days=rule.due_within_days),
            Fact.due_date >= today - lookback,
        )
    # Regra sem prazo: só itens recentes (data do item, ou da coleta quando não há data).
    else:
        query = query.where(
            func.coalesce(BronzeItem.occurred_at, BronzeItem.ingested_at) >= now - lookback
        )
    # Executa.
    return list(session.execute(query).all())


# Uma rodada: aplica cada regra ligada e grava os alertas novos.
def generate(session: Session, settings: Settings, now: datetime | None = None) -> AlertSummary:
    # Momento da rodada (o teste passa um fixo).
    now = now or datetime.now(UTC)
    # Resumo.
    summary = AlertSummary()
    # Regras ligadas, na ordem de criação.
    rules = session.scalars(
        select(AlertRule).where(AlertRule.active.is_(True)).order_by(AlertRule.id)
    ).all()
    # Cada regra.
    for rule in rules:
        # Fatos que passam.
        rows = matching(session, rule, settings, now)
        # Cada um vira um alerta.
        for fact_id, company_id, by_company, by_project in rows:
            session.add(
                Alert(
                    # Empresa do item.
                    company_id=company_id,
                    # Regra.
                    rule_id=rule.id,
                    # Fato.
                    fact_id=fact_id,
                    # Peso da regra, copiado.
                    severity=rule.severity,
                    # Marcas de liderança no momento.
                    by_company_leader=bool(by_company),
                    by_project_leader=bool(by_project),
                )
            )
        # Conta (mesmo zero, para o resumo listar a regra).
        summary.by_rule[rule.name] = summary.by_rule.get(rule.name, 0) + len(rows)
        # Envia ao banco antes da próxima regra.
        session.flush()
    # Devolve.
    return summary


# Conveniência para o agendador e o comando: abre a sessão, roda e grava.
def run_from_settings(settings: Settings) -> AlertSummary | None:
    """None quando nada novo nasceu (para não encher o log a cada 5 minutos)."""
    # Import local: o banco só é carregado quando a rodada roda de verdade.
    from app.db import get_sessionmaker

    # Uma sessão.
    with get_sessionmaker()() as session:
        # Roda.
        summary = generate(session, settings)
        # Grava.
        session.commit()
    # Só devolve quando há o que registrar no log.
    return summary if summary.created else None


# Muda a situação de um alerta (botões da página) e dos outros alertas abertos do mesmo fato.
def set_status(session: Session, alert: Alert, status: str, actor: str) -> None:
    """Por que o fato inteiro: a página mostra um fato por linha, mesmo quando duas regras
    dispararam para ele (ex.: "Pedido de liderança" e "Prazo em até 3 dias"). Marcar como
    visto, resolvido ou descartado vale para a linha que você vê. "Reabrir" vale só para o
    alerta clicado."""
    # Situação válida.
    if status not in STATUSES:
        raise ValueError("Situação desconhecida.")
    # Alerta aberto (novo ou visto) indo para outra situação que não "novo": leva junto os
    # outros alertas abertos do mesmo fato.
    if alert.status in OPEN and status != "novo":
        # Os outros abertos do mesmo fato.
        siblings = session.scalars(
            select(Alert).where(
                Alert.fact_id == alert.fact_id, Alert.id != alert.id, Alert.status.in_(OPEN)
            )
        ).all()
        # Cada um muda junto (sem mexer no fato: isso é feito uma vez, abaixo).
        for other in siblings:
            # Situação.
            other.status = status
            # Momento.
            other.status_at = datetime.now(UTC)
    # O fato do alerta.
    fact = session.get(Fact, alert.fact_id)
    # Resolver fecha o fato aberto: as outras regras (ex.: a do prazo) param de disparar.
    if status == "resolvido" and fact is not None and fact.status == "aberto":
        fact.status = DONE
    # Voltar de resolvido reabre o fato que este alerta fechou.
    if alert.status == "resolvido" and status != "resolvido" and fact is not None:
        # Só se ainda estiver concluído.
        if fact.status == DONE:
            fact.status = "aberto"
    # Situação nova.
    alert.status = status
    # Momento (vazio de novo quando volta a "novo").
    alert.status_at = None if status == "novo" else datetime.now(UTC)
    # Diário.
    audit(
        session,
        actor,
        f"alertas.{status}",
        target=str(alert.id),
        company_id=alert.company_id,
        fato=alert.fact_id,
    )


# Quantos fatos com alerta novo (o número ao lado de "Alertas" no menu). Conta fatos, não
# alertas: um fato com duas regras é uma linha só na página e deve contar um.
def count_new(session: Session, company_id: int | None = None) -> int:
    # Consulta: fatos distintos com alerta novo.
    query = select(func.count(func.distinct(Alert.fact_id))).where(Alert.status == "novo")
    # Uma Empresa.
    if company_id is not None:
        query = query.where(Alert.company_id == company_id)
    # Executa (None vira 0).
    return session.scalar(query) or 0


# Alertas para a página, com filtros.
def list_alerts(
    session: Session,
    company_id: int | None,
    status: str | None = None,
    rule_id: int | None = None,
    limit: int = 200,
) -> list[tuple]:
    """Linhas (alerta, regra, fato, item, projeto, quem disse, responsável ou None)."""
    # Quem disse.
    speaker = aliased(Stakeholder)
    # Responsável.
    owner = aliased(Stakeholder)
    # Ordem do peso: alta primeiro.
    weight = case({"alta": 0, "media": 1, "baixa": 2}, value=Alert.severity, else_=3)
    # Ordem da situação: novo primeiro, depois visto, depois o resto.
    stage = case({"novo": 0, "visto": 1}, value=Alert.status, else_=2)
    # Consulta.
    query = (
        select(Alert, AlertRule, Fact, BronzeItem, Project, speaker, owner)
        .join(AlertRule, AlertRule.id == Alert.rule_id)
        .join(Fact, Fact.id == Alert.fact_id)
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        .outerjoin(Project, Project.id == Fact.project_id)
        .outerjoin(speaker, speaker.id == Fact.said_by_id)
        .outerjoin(owner, owner.id == Fact.owner_id)
        # Novos e fortes primeiro; dentro disso, os mais recentes.
        .order_by(stage, weight, Alert.created_at.desc(), Alert.id.desc())
        .limit(limit)
    )
    # Uma Empresa.
    if company_id is not None:
        query = query.where(Alert.company_id == company_id)
    # "abertos" = novo ou visto (o filtro padrão da página).
    if status == "abertos":
        query = query.where(Alert.status.in_(OPEN))
    # Uma situação.
    elif status in STATUSES:
        query = query.where(Alert.status == status)
    # Uma regra.
    if rule_id is not None:
        query = query.where(Alert.rule_id == rule_id)
    # Executa.
    return list(session.execute(query).all())


# Uma linha por fato: o alerta mais forte (o primeiro, pela ordem da lista) e os nomes das
# outras regras que dispararam para o mesmo fato.
def group_by_fact(rows: list[tuple]) -> list[tuple[tuple, list[str]]]:
    """[(linha de list_alerts, ["outra regra", ...])], na ordem recebida."""
    # Fato → posição na lista de saída.
    seen: dict[int, int] = {}
    # Saída.
    grouped: list[tuple[tuple, list[str]]] = []
    # Cada linha (alerta, regra, fato, ...).
    for row in rows:
        # Id do fato.
        fact_id = row[2].id
        # Primeira vez: a linha fica.
        if fact_id not in seen:
            # Guarda a posição.
            seen[fact_id] = len(grouped)
            # Linha com a lista de outras regras vazia.
            grouped.append((row, []))
        # Repetida: só o nome da regra entra na linha que já existe.
        else:
            grouped[seen[fact_id]][1].append(row[1].name)
    # Devolve.
    return grouped


# Regras para a página: as de todas as Empresas e as da Empresa escolhida.
def list_rules(session: Session, company_id: int | None) -> list[tuple[AlertRule, int]]:
    """Linhas (regra, alertas abertos dela na Empresa escolhida)."""
    # Alertas abertos (novo ou visto) de cada regra.
    open_count = (
        select(func.count())
        .select_from(Alert)
        .where(Alert.rule_id == AlertRule.id, Alert.status.in_(("novo", "visto")))
    )
    # Na Empresa escolhida.
    if company_id is not None:
        open_count = open_count.where(Alert.company_id == company_id)
    # Consulta: a regra e a contagem.
    query = select(AlertRule, open_count.scalar_subquery()).order_by(AlertRule.id)
    # Empresa escolhida: as gerais e as dela (as de outra Empresa não aparecem).
    if company_id is not None:
        query = query.where(or_(AlertRule.company_id.is_(None), AlertRule.company_id == company_id))
    # Executa.
    return [(rule, n or 0) for rule, n in session.execute(query).all()]


# Números da página.
def overview(session: Session, company_id: int | None) -> dict:
    # Filtro de Empresa (vazio = todas).
    of_company = [Alert.company_id == company_id] if company_id is not None else []
    # Fatos distintos (um fato com duas regras conta um, como na lista).
    facts = func.count(func.distinct(Alert.fact_id))
    # Contagem por situação.
    by_status = dict(
        session.execute(select(Alert.status, facts).where(*of_company).group_by(Alert.status)).all()
    )
    # Novos de peso alto.
    high = session.scalar(
        select(facts).where(Alert.status == "novo", Alert.severity == "alta", *of_company)
    )
    # Devolve.
    return {
        # Por situação, na ordem da lista.
        "por_situacao": {s: by_status.get(s, 0) for s in STATUSES},
        # Novos de peso alto.
        "alta": high or 0,
        # Abertos (novo + visto).
        "abertos": by_status.get("novo", 0) + by_status.get("visto", 0),
    }


# Texto curto da regra, para a tabela ("Tarefa · liderança no projeto · prazo em 3 dias").
def describe_rule(rule: AlertRule) -> str:
    # Tipos (vazio = todos).
    kinds = (
        ", ".join(KIND_LABELS[FactKind(k)] for k in rule.fact_kinds)
        if rule.fact_kinds
        else "Todos os tipos"
    )
    # Partes.
    parts = [kinds, LEADERSHIP.get(rule.leadership, rule.leadership).lower()]
    # Responsável, quando importa.
    if rule.owner_rule != "qualquer":
        parts.append(OWNER_RULES[rule.owner_rule].lower())
    # Prazo, quando importa.
    if rule.due_within_days is not None:
        parts.append(
            "prazo vencido ou hoje"
            if rule.due_within_days == 0
            else f"prazo em até {rule.due_within_days} dia(s)"
        )
    # Junta.
    return " · ".join(parts)


# Por que um alerta disparou, em texto ("Márcia, liderança no projeto, pediu").
def why(alert: Alert, speaker: Stakeholder | None) -> str:
    # Marcas de liderança gravadas no alerta.
    marks = []
    # Na empresa.
    if alert.by_company_leader:
        marks.append("liderança na empresa")
    # No projeto.
    if alert.by_project_leader:
        marks.append("liderança no projeto")
    # Nome de quem disse (a Pessoa pode ter sido apagada depois).
    who = speaker.name if speaker else "Pessoa apagada"
    # "Márcia (liderança na empresa e liderança no projeto)".
    return f"{who} ({' e '.join(marks)})" if marks else who
