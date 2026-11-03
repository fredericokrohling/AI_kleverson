"""Classificação de cada item por projeto, em cascata (bloco 3, primeira parte).

O que resolve: até aqui, um e-mail podia ter as tags de vários projetos (as regras do
PR #10 colocam todas as que acharem). Para extrair fatos (tarefa, decisão, risco) e, no
futuro, montar relatórios por projeto, cada item precisa de UM projeto principal, ou da
resposta "não é de projeto nenhum". Esta é a tabela item_classifications.

A cascata vai do sinal mais barato e confiável ao mais caro (Arquitetura, seção 8):

1. você escolheu no console ........................ "manual" (travada: nada muda)
2. você pôs à mão a tag de um só projeto ........... "manual"
3. pasta ligada a um só projeto .................... "pasta" (marcador do Gmail ou Drive)
4. mesma conversa de um item já classificado ....... "mesma_conversa"
5. equipe e nome no texto apontam um só projeto .... "stakeholder" ou "palavra_chave"
6. sobrou disputa (ou nenhum sinal) ................ "llm": o Claude escolhe
7. confiança do Claude abaixo do mínimo ............ fila "A revisar" (você decide)

Por que as regras vêm das tags, e não de uma segunda leitura do texto: as tags já
guardam o que cada regra achou (pasta, equipe, nome no texto, à mão), e respeitam as
tags que você tirou. Ler as tags mantém uma fonte só da verdade; se a regra de tag
melhorar, a classificação melhora junto.

Separação por Empresa (premissa 6): o Claude só vê o item e os projetos da Empresa do
item. Nada de outra Empresa entra no pedido.

Custo: as regras não custam nada. O Claude só entra no que sobra, com o modelo Haiku
(CLASSIFY_MODEL) e esforço baixo; cada chamada fica no registro de custo com o motivo
"classificacao" e respeita o teto mensal.
"""

# json: lê a resposta do Claude, que vem como JSON dentro do texto.
import json

# logging: mensagens no log do coletor.
import logging

# re: acha o bloco JSON dentro da resposta.
import re

# dataclass: classes simples de dados.
from dataclasses import dataclass, field

# Data e hora com fuso.
from datetime import UTC, datetime

# Decimal: confiança com casas exatas.
from decimal import Decimal, InvalidOperation

# Erros da API da Anthropic.
import anthropic

# Consultas: and_ e or_ juntam condições; exists pergunta "existe alguma linha?".
from sqlalchemy import and_, delete, exists, func, or_, select

# Session é a conversa com o banco; sessionmaker é a fábrica de sessões.
from sqlalchemy.orm import Session, sessionmaker

# Configuração.
from app.config import Settings

# Cliente do Claude, teto e gasto do mês.
from app.llm import BudgetExceededError, LLMClient, month_spend_usd

# Tabelas.
from app.models import (
    Attachment,
    BronzeItem,
    ClassificationMethod,
    ItemClassification,
    LLMCall,
    Project,
    SourceKind,
    TaggedItem,
    audit,
)

# Texto próprio do e-mail (sem citação e assinatura), regras de tag e a view do Drive.
from app.project_tags import _DRIVE_TAGS, RULE_LABEL, RULE_MANUAL, RULE_TEAM, RULE_TEXT, own_text

# Logger deste módulo.
log = logging.getLogger(__name__)

# Fontes classificadas. Agenda, Chat e manual ficam para quando forem coletados.
SOURCES = (SourceKind.GMAIL, SourceKind.MEET_TRANSCRIPT, SourceKind.DRIVE)
# Motivo gravado no registro de custo de cada chamada ao Claude.
PURPOSE = "classificacao"
# Máximo de projetos mostrados ao Claude quando não há sinal nenhum (lista curta = barato).
MAX_PROJECTS_IN_PROMPT = 40

# Confiança de cada regra. Não é estatística: é a ordem de força dos sinais, para a
# tela mostrar e para você comparar com a nota do Claude.
CONFIDENCE = {
    # Você escolheu.
    ClassificationMethod.MANUAL: Decimal("1.000"),
    # Pasta ligada ao projeto: organização feita por você.
    ClassificationMethod.FOLDER: Decimal("0.950"),
    # Mesma conversa de um item já decidido.
    ClassificationMethod.THREAD: Decimal("0.900"),
    # Remetente ou participante da equipe de um só projeto.
    ClassificationMethod.STAKEHOLDER: Decimal("0.850"),
    # Nome, código ou palavra-chave do projeto no texto.
    ClassificationMethod.KEYWORD: Decimal("0.800"),
}

# Nome de cada método na tela.
METHOD_LABELS = {
    ClassificationMethod.MANUAL: "você",
    ClassificationMethod.FOLDER: "pasta",
    ClassificationMethod.THREAD: "mesma conversa",
    ClassificationMethod.STAKEHOLDER: "equipe",
    ClassificationMethod.KEYWORD: "nome no texto",
    ClassificationMethod.LLM: "Claude",
}

# Instrução fixa do Claude. Fica igual em todas as chamadas (o que permitiria cache) e
# diz exatamente o formato da resposta, para o código conferir.
SYSTEM = (
    "Você classifica conteúdo de trabalho (e-mails, transcrições de reunião e documentos) "
    "de uma gerente de projetos. Recebe um item e uma lista de projetos de UM cliente. "
    "Escolha o projeto a que o item se refere principalmente, ou null se ele não for de "
    "nenhum projeto da lista (newsletter, assunto pessoal, outro cliente, administrativo). "
    "Use só o que está escrito no item; não invente relação. Responda apenas com um objeto "
    'JSON, sem texto em volta: {"projeto": <id da lista ou null>, "confianca": <número de 0 '
    'a 1>, "motivo": "<uma frase curta em português, citando a pista do texto>"}'
)

# Bloco {...} dentro do texto (o Claude às vezes põe ```json em volta).
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


# A decisão sobre um item, antes de gravar.
@dataclass
class Decision:
    # Projeto escolhido (None = não é de nenhum projeto).
    project_id: int | None
    # Regra que decidiu.
    method: ClassificationMethod
    # Confiança de 0 a 1.
    confidence: Decimal
    # Motivo em português.
    reason: str
    # Projetos que estavam na disputa.
    candidates: list[int] = field(default_factory=list)
    # True = vai para a fila "A revisar".
    needs_review: bool = False


# Os sinais de um item, cada um como um conjunto de ids de projeto.
@dataclass
class Signals:
    # Tags de projeto colocadas à mão.
    manual: set[int] = field(default_factory=set)
    # Pasta do Gmail ou do Drive ligada ao projeto.
    folder: set[int] = field(default_factory=set)
    # Projetos de outros itens da mesma conversa (já decididos e fora da revisão).
    thread: set[int] = field(default_factory=set)
    # Remetente ou participante da equipe.
    team: set[int] = field(default_factory=set)
    # Nome, código ou palavra-chave no texto.
    text: set[int] = field(default_factory=set)


# Resumo de uma rodada, para o log do coletor e para o comando classificar.
@dataclass
class ClassifySummary:
    # Decididos pelas regras (sem custo).
    by_rules: int = 0
    # Decididos pelo Claude.
    by_claude: int = 0
    # Mandados para a fila "A revisar".
    to_review: int = 0
    # Esperando o Claude (sem chave, limite da rodada, teto ou erro da API).
    waiting: int = 0
    # Revistos sem mudança: o Claude já tinha decidido entre os mesmos projetos.
    kept: int = 0
    # True se o teto do mês foi atingido.
    budget_hit: bool = False

    # Uma linha legível.
    def describe(self) -> str:
        # Texto base.
        text = (
            f"Classificação: {self.by_rules} pelas regras, {self.by_claude} pelo Claude, "
            f"{self.to_review} para revisar, {self.waiting} esperando."
        )
        # Aviso do teto.
        return text + (" Teto mensal atingido." if self.budget_hit else "")


# Projetos ativos de uma Empresa: id → projeto.
def active_projects(session: Session, company_id: int) -> dict[int, Project]:
    # Só ativos: projeto encerrado não recebe classificação automática.
    rows = session.scalars(
        select(Project)
        .where(Project.company_id == company_id, Project.active.is_(True))
        .order_by(Project.name)
    )
    # Dicionário pelo id.
    return {p.id: p for p in rows}


# Lê os sinais de um item.
def signals_of(session: Session, item: BronzeItem, projects: dict[int, Project]) -> Signals:
    # Resultado vazio.
    signals = Signals()
    # Tag → projeto (só os projetos ativos da Empresa do item).
    by_tag = {p.tag_id: p.id for p in projects.values() if p.tag_id is not None}
    # Drive: as tags vêm das pastas (view bronze_item_tags), todas valem como "pasta".
    if item.source == SourceKind.DRIVE:
        # Tags da versão.
        for tag_id in session.scalars(
            select(_DRIVE_TAGS.c.tag_id).where(_DRIVE_TAGS.c.bronze_item_id == item.id)
        ):
            # Tag de projeto: sinal de pasta.
            if tag_id in by_tag:
                signals.folder.add(by_tag[tag_id])
    # E-mail e transcrição: tags valendo, cada uma com a regra que a colocou.
    else:
        # Linhas valendo (as tiradas à mão ficam de fora).
        rows = session.execute(
            select(TaggedItem.tag_id, TaggedItem.rule).where(
                TaggedItem.bronze_item_id == item.id, TaggedItem.removed_at.is_(None)
            )
        )
        # Destino de cada regra.
        target = {
            RULE_MANUAL: signals.manual,
            RULE_LABEL: signals.folder,
            RULE_TEAM: signals.team,
            RULE_TEXT: signals.text,
        }
        # Cada tag.
        for tag_id, rule in rows:
            # Só tags de projeto, de regras conhecidas.
            if tag_id in by_tag and rule in target:
                # Guarda o projeto no conjunto da regra.
                target[rule].add(by_tag[tag_id])
    # Mesma conversa: projetos já decididos de outros itens com o mesmo thread_id.
    if item.thread_id:
        # Consulta.
        decided = session.scalars(
            select(ItemClassification.project_id)
            .join(BronzeItem, BronzeItem.id == ItemClassification.bronze_item_id)
            .where(
                # Mesma Empresa e mesma conversa.
                BronzeItem.company_id == item.company_id,
                BronzeItem.thread_id == item.thread_id,
                # Outro item.
                BronzeItem.id != item.id,
                # Com projeto e sem dúvida pendente (dúvida não se espalha).
                ItemClassification.project_id.is_not(None),
                ItemClassification.needs_review.is_(False),
            )
        )
        # Só projetos ainda ativos.
        signals.thread = {p for p in decided if p in projects}
    # Devolve.
    return signals


# Aplica as regras. Devolve a decisão ou a lista de candidatos para o Claude.
def decide_by_rules(signals: Signals, projects: dict[int, Project]) -> Decision | list[int]:
    """Lista vazia = nenhum sinal (o Claude vê todos os projetos ativos)."""

    # Nome do projeto para o motivo.
    def name(project_id: int) -> str:
        # Nome entre aspas.
        return f"“{projects[project_id].name}”"

    # 2) tag de projeto posta à mão.
    if signals.manual:
        # Uma só: é a sua escolha.
        if len(signals.manual) == 1:
            # O projeto.
            (pid,) = signals.manual
            # Decisão.
            return Decision(
                pid,
                ClassificationMethod.MANUAL,
                CONFIDENCE[ClassificationMethod.MANUAL],
                f"Você colocou à mão a tag do projeto {name(pid)}.",
                [pid],
            )
        # Várias à mão: o Claude escolhe só entre elas.
        return sorted(signals.manual)
    # 3) pasta do Gmail ou do Drive.
    if signals.folder:
        # Uma só pasta de projeto.
        if len(signals.folder) == 1:
            # O projeto.
            (pid,) = signals.folder
            # Decisão.
            return Decision(
                pid,
                ClassificationMethod.FOLDER,
                CONFIDENCE[ClassificationMethod.FOLDER],
                f"Está numa pasta ligada ao projeto {name(pid)}.",
                [pid],
            )
        # Pastas de projetos diferentes: o Claude escolhe entre elas.
        return sorted(signals.folder)
    # 4) mesma conversa.
    if len(signals.thread) == 1:
        # O projeto.
        (pid,) = signals.thread
        # Decisão.
        return Decision(
            pid,
            ClassificationMethod.THREAD,
            CONFIDENCE[ClassificationMethod.THREAD],
            f"Outro item da mesma conversa já é do projeto {name(pid)}.",
            [pid],
        )
    # 5) equipe e nome no texto (e a conversa, se ela estava dividida).
    others = signals.team | signals.text | signals.thread
    # Todos apontam o mesmo projeto.
    if len(others) == 1:
        # O projeto.
        (pid,) = others
        # Pela equipe (o sinal mais forte dos dois).
        if pid in signals.team:
            # Motivo, citando o texto quando ele também aponta o projeto.
            extra = " e o nome do projeto aparece no texto" if pid in signals.text else ""
            # Decisão.
            return Decision(
                pid,
                ClassificationMethod.STAKEHOLDER,
                CONFIDENCE[ClassificationMethod.STAKEHOLDER],
                f"Quem enviou (ou participou) é da equipe só do projeto {name(pid)}{extra}.",
                [pid],
            )
        # Pelo nome no texto.
        return Decision(
            pid,
            ClassificationMethod.KEYWORD,
            CONFIDENCE[ClassificationMethod.KEYWORD],
            f"O nome, código ou palavra-chave do projeto {name(pid)} aparece no texto.",
            [pid],
        )
    # Sinais de projetos diferentes (ou nenhum): o Claude decide.
    return sorted(others)


# O texto que o Claude lê sobre o item (só o necessário para classificar).
def item_text(session: Session, item: BronzeItem, max_chars: int) -> str:
    # Linhas do cabeçalho.
    head: list[str] = []
    # Corpo.
    body = item.body_text or ""
    # E-mail.
    if item.source == SourceKind.GMAIL:
        # Tipo.
        head.append("Tipo: e-mail")
        # Remetente.
        head.append(f"De: {item.author or '(desconhecido)'}")
        # Destinatários, quando gravados.
        to = (item.extra or {}).get("para")
        # Mostra.
        if to:
            head.append(f"Para: {to if isinstance(to, str) else ', '.join(map(str, to))}")
        # Nomes dos arquivos anexados (dizem muito: "Cronograma Atlas v3.xlsx").
        files = session.scalars(
            select(Attachment.filename).where(Attachment.bronze_item_id == item.id)
        ).all()
        # Mostra.
        if files:
            head.append(f"Anexos: {', '.join(files[:15])}")
        # Corpo sem o texto citado e sem a assinatura (o assunto vai à parte).
        body = own_text(None, body).strip()
    # Transcrição.
    elif item.source == SourceKind.MEET_TRANSCRIPT:
        # Tipo.
        head.append("Tipo: transcrição de reunião")
        # Convidados da agenda.
        event = (item.extra or {}).get("evento_agenda") or {}
        # Mostra.
        if event.get("participantes"):
            head.append(f"Participantes: {', '.join(event['participantes'][:20])}")
    # Drive.
    else:
        # Tipo.
        head.append("Tipo: arquivo do Google Drive")
        # Caminho da pasta.
        path = (item.extra or {}).get("caminho")
        # Mostra.
        if path:
            head.append(f"Pasta: {path}")
    # Assunto ou título.
    head.append(f"Assunto/título: {item.subject or '(sem assunto)'}")
    # Texto cortado no limite, avisando o Claude do corte.
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n[texto cortado: só os primeiros {max_chars} caracteres]"
    # Junta.
    return "\n".join(head) + "\n\nTexto:\n" + (body or "(sem texto)")


# A lista de projetos que o Claude vê.
def projects_text(projects: list[Project]) -> str:
    # Uma linha por projeto.
    lines: list[str] = []
    # Cada projeto.
    for p in projects:
        # Partes conhecidas.
        parts = [f"id {p.id}: {p.name}"]
        # Código.
        if p.code:
            parts.append(f"código {p.code}")
        # Outros nomes.
        if p.aliases:
            parts.append("também chamado " + ", ".join(p.aliases))
        # Palavras-chave.
        if p.keywords:
            parts.append("palavras-chave " + ", ".join(p.keywords))
        # Observações (curtas).
        if p.notes:
            parts.append("notas: " + p.notes.strip().replace("\n", " ")[:200])
        # Linha.
        lines.append("- " + "; ".join(parts))
    # Junta.
    return "\n".join(lines)


# Lê a resposta do Claude. Erro de formato vira ValueError com o motivo.
def parse_answer(text: str, allowed: set[int]) -> tuple[int | None, Decimal, str]:
    # Acha o bloco JSON.
    match = _JSON_BLOCK.search(text or "")
    # Não há.
    if match is None:
        raise ValueError("a resposta não trouxe JSON")
    # Lê.
    try:
        data = json.loads(match.group(0))
    # JSON quebrado.
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido ({exc.msg})") from exc
    # Projeto escolhido.
    raw = data.get("projeto")
    # null = nenhum projeto.
    if raw is None:
        project_id = None
    # Senão, um id da lista (o Claude às vezes manda "3" em texto).
    else:
        try:
            project_id = int(raw)
        # Não é número.
        except (TypeError, ValueError) as exc:
            raise ValueError(f"projeto “{raw}” não é um id") from exc
        # Fora da lista: não aceita (o Claude não pode inventar projeto nem trazer outro
        # da mesma Empresa que não estava na disputa).
        if project_id not in allowed:
            raise ValueError(f"projeto {project_id} não estava na lista")
    # Confiança.
    try:
        confidence = Decimal(str(data.get("confianca", 0)))
    # Não é número.
    except InvalidOperation as exc:
        raise ValueError("confiança não é número") from exc
    # Entre 0 e 1, com 3 casas (o tamanho da coluna).
    confidence = min(max(confidence, Decimal(0)), Decimal(1)).quantize(Decimal("0.001"))
    # Motivo curto.
    reason = str(data.get("motivo") or "").strip()[:500]
    # Devolve.
    return project_id, confidence, reason


# Pergunta ao Claude. Erros da API sobem para quem chamou (o item espera a próxima rodada).
def ask_claude(
    session: Session,
    llm: LLMClient,
    settings: Settings,
    item: BronzeItem,
    projects: dict[int, Project],
    candidates: list[int],
) -> Decision:
    # Lista mostrada: os candidatos, ou (sem sinal) os projetos ativos até o limite.
    shown = [projects[c] for c in candidates] if candidates else list(projects.values())
    # Limite.
    shown = shown[:MAX_PROJECTS_IN_PROMPT]
    # Ids aceitos na resposta.
    allowed = {p.id for p in shown}
    # Explicação do contexto para o Claude.
    context = (
        "Os sinais automáticos apontaram estes projetos e não chegaram a um só"
        if candidates
        else "Nenhum sinal automático apontou projeto; estes são os projetos ativos do cliente"
    )
    # Pedido.
    prompt = (
        f"{context}:\n{projects_text(shown)}\n\n"
        f"Item:\n{item_text(session, item, settings.classify_max_chars)}"
    )
    # Chamada (registro de custo e teto ficam no LLMClient).
    result = llm.generate(
        purpose=PURPOSE,
        prompt=prompt,
        system=SYSTEM,
        # Espaço para o raciocínio curto e a resposta.
        max_tokens=2000,
        # Escolher numa lista curta não pede raciocínio longo.
        effort="low",
        # Haiku por padrão (CLASSIFY_MODEL).
        model=settings.classify_model,
    )
    # Recusa (raro aqui): vai para você.
    if result.refused:
        return Decision(
            None,
            ClassificationMethod.LLM,
            Decimal(0),
            "O Claude recusou classificar este item.",
            sorted(allowed),
            True,
        )
    # Lê a resposta.
    try:
        project_id, confidence, reason = parse_answer(result.text, allowed)
    # Resposta ilegível: vai para você, sem pagar de novo na próxima rodada.
    except ValueError as exc:
        return Decision(
            None,
            ClassificationMethod.LLM,
            Decimal(0),
            f"Resposta do Claude ilegível: {exc}.",
            sorted(allowed),
            True,
        )
    # Abaixo do mínimo: decisão fica, mas vai para a fila.
    review = confidence < settings.classify_min_confidence
    # Decisão.
    return Decision(
        project_id,
        ClassificationMethod.LLM,
        confidence,
        reason or "(sem motivo)",
        candidates,
        review,
    )


# Grava uma decisão (cria ou troca a linha do item).
def save(session: Session, item: BronzeItem, decision: Decision) -> ItemClassification:
    # Linha existente.
    row = session.get(ItemClassification, item.id)
    # Não existe: cria.
    if row is None:
        row = ItemClassification(bronze_item_id=item.id)
        # Prepara.
        session.add(row)
    # Projeto.
    row.project_id = decision.project_id
    # Método.
    row.method = decision.method
    # Confiança.
    row.confidence = decision.confidence
    # Motivo.
    row.reason = decision.reason
    # Candidatos.
    row.candidates = list(decision.candidates)
    # Revisão.
    row.needs_review = decision.needs_review
    # Automática (só o console trava).
    row.locked = False
    # Momento (marca também que os sinais até aqui já foram vistos).
    row.decided_at = datetime.now(UTC)
    # Devolve.
    return row


# Classifica um item. Devolve "regras", "claude", "revisar", "mantida", "esperando" ou
# "travada". "claude" e "revisar" são as duas saídas que pagaram uma chamada.
def classify_item(
    session: Session,
    item: BronzeItem,
    settings: Settings,
    llm: LLMClient | None,
    projects: dict[int, Project] | None = None,
) -> str:
    """llm None = só regras (sem chave, limite da rodada ou teto): o item espera."""
    # Linha atual.
    row = session.get(ItemClassification, item.id)
    # Escolhida por você: ninguém mexe.
    if row is not None and row.locked:
        return "travada"
    # Projetos ativos da Empresa do item.
    projects = active_projects(session, item.company_id) if projects is None else projects
    # Sem projeto ativo: nada a escolher (o item espera você cadastrar um).
    if not projects:
        return "esperando"
    # Regras.
    outcome = decide_by_rules(signals_of(session, item, projects), projects)
    # Regras decidiram.
    if isinstance(outcome, Decision):
        # Grava.
        save(session, item, outcome)
        # Conta.
        return "regras"
    # Já decidido pelo Claude com os mesmos candidatos: não paga de novo, só marca que
    # os sinais novos foram vistos.
    if (
        row is not None
        and row.method == ClassificationMethod.LLM
        and sorted(row.candidates or []) == sorted(outcome)
    ):
        # Marca.
        row.decided_at = datetime.now(UTC)
        # Decisão anterior mantida, sem chamada nova.
        return "mantida"
    # Precisa do Claude e ele não está disponível agora.
    if llm is None:
        return "esperando"
    # Pergunta.
    decision = ask_claude(session, llm, settings, item, projects, outcome)
    # Grava.
    save(session, item, decision)
    # Conta.
    return "revisar" if decision.needs_review else "claude"


# Ids dos itens que precisam de classificação: sem linha, ou com tags mudadas depois da
# decisão. Só ids (leve): uma rodada percorre todos, sem limite, para um item que espera
# o Claude nunca impedir as regras de decidirem os itens que vêm depois dele.
def pending_ids(session: Session, limit: int | None = None) -> list[int]:
    # Linha de classificação do item (pode não existir).
    row_exists = exists().where(ItemClassification.bronze_item_id == BronzeItem.id)
    # Tag colocada ou tirada depois da decisão (não vale para linha travada).
    tag_changed = exists().where(
        TaggedItem.bronze_item_id == BronzeItem.id,
        ItemClassification.bronze_item_id == BronzeItem.id,
        ItemClassification.locked.is_(False),
        or_(
            TaggedItem.created_at > ItemClassification.decided_at,
            TaggedItem.removed_at > ItemClassification.decided_at,
        ),
    )
    # Empresas com pelo menos um projeto ativo (nas outras não há o que escolher).
    with_projects = select(Project.company_id).where(Project.active.is_(True))
    # Consulta: mais antigos primeiro, para a conversa ser decidida na ordem em que
    # aconteceu (a primeira mensagem ensina as seguintes).
    query = (
        select(BronzeItem.id)
        .where(
            BronzeItem.source.in_(SOURCES),
            BronzeItem.company_id.in_(with_projects),
            or_(~row_exists, tag_changed),
        )
        .order_by(BronzeItem.occurred_at.asc().nulls_first(), BronzeItem.id)
    )
    # Limite pedido (ex.: 1, só para saber se há algo).
    if limit is not None:
        query = query.limit(limit)
    # Executa.
    return list(session.scalars(query))


# Uma rodada: regras em tudo o que está pendente, Claude até o limite da rodada.
def run_pending(
    sessions: sessionmaker[Session], settings: Settings, llm: LLMClient | None
) -> ClassifySummary:
    # Resumo.
    summary = ClassifySummary()
    # Chamadas ao Claude que ainda cabem nesta rodada.
    budget_calls = settings.classify_llm_per_run
    # Uma sessão para a rodada.
    with sessions() as session:
        # Projetos por Empresa, lidos uma vez.
        cache: dict[int, dict[int, Project]] = {}
        # Cada item pendente.
        for item_id in pending_ids(session):
            # O item.
            item = session.get(BronzeItem, item_id)
            # Projetos da Empresa do item.
            projects = cache.setdefault(item.company_id, active_projects(session, item.company_id))
            # Claude disponível só enquanto houver chamadas na rodada e o teto não bateu.
            available = llm if budget_calls > 0 and not summary.budget_hit else None
            # Tenta.
            try:
                # Classifica.
                result = classify_item(session, item, settings, available, projects)
            # Teto atingido no meio: o resto espera o mês virar (ou você aumentar o teto).
            except BudgetExceededError:
                # Marca.
                summary.budget_hit = True
                # Espera.
                result = "esperando"
            # Erro da API (rede, chave): o item espera a próxima rodada; nada foi cobrado.
            except anthropic.APIError:
                # Registra.
                log.exception("Classificação do item %s falhou no Claude", item.id)
                # Espera.
                result = "esperando"
            # Chamou o Claude: gasta uma chamada da rodada.
            if result in {"claude", "revisar"}:
                budget_calls -= 1
            # Conta.
            if result == "regras":
                summary.by_rules += 1
            elif result == "claude":
                summary.by_claude += 1
            elif result == "revisar":
                summary.to_review += 1
            elif result == "esperando":
                summary.waiting += 1
            elif result == "mantida":
                summary.kept += 1
            # Grava item a item: uma falha depois não desfaz o que já foi decidido.
            session.commit()
    # Devolve.
    return summary


# Conveniência para o agendador: monta tudo a partir da configuração.
def run_from_settings(settings: Settings) -> ClassifySummary | None:
    """None quando não há nada pendente (para não encher o log a cada 5 minutos)."""
    # Import local: o banco só é carregado quando a rodada roda de verdade.
    from app.db import get_sessionmaker

    # Fábrica de sessões.
    sessions = get_sessionmaker()
    # Confere se há algo pendente e se o teto permite o Claude.
    with sessions() as session:
        # Nada pendente.
        if not pending_ids(session, 1):
            return None
        # Teto atingido: só as regras rodam.
        over = month_spend_usd(session) >= settings.llm_monthly_budget_usd
    # Cliente do Claude (sem chave ou com teto atingido: só regras).
    llm = LLMClient(sessions, settings) if settings.anthropic_api_key and not over else None
    # Roda.
    summary = run_pending(sessions, settings, llm)
    # Marca o teto no resumo.
    summary.budget_hit = summary.budget_hit or over
    # Devolve.
    return summary


# Você escolhe o projeto no console (None = não é de nenhum projeto). Fica travada.
def set_manual(session: Session, item: BronzeItem, project_id: int | None, actor: str) -> None:
    """Erro (projeto de outra Empresa ou inexistente) vira ValueError."""
    # Projeto escolhido.
    if project_id is not None:
        # Busca.
        project = session.get(Project, project_id)
        # Não existe ou é de outra Empresa.
        if project is None or project.company_id != item.company_id:
            raise ValueError("Escolha um projeto da Empresa deste item.")
    # Decisão.
    row = save(
        session,
        item,
        Decision(
            project_id,
            ClassificationMethod.MANUAL,
            CONFIDENCE[ClassificationMethod.MANUAL],
            "Escolhido por você no console.",
            [project_id] if project_id else [],
        ),
    )
    # Travada.
    row.locked = True
    # Diário.
    audit(
        session,
        actor,
        "classificacao.manual",
        target=str(item.id),
        company_id=item.company_id,
        projeto=project_id,
    )


# Devolve o item à classificação automática (a próxima rodada decide de novo).
def release(session: Session, item: BronzeItem, actor: str) -> None:
    # Apaga a linha.
    session.execute(delete(ItemClassification).where(ItemClassification.bronze_item_id == item.id))
    # Diário.
    audit(
        session, actor, "classificacao.automatica", target=str(item.id), company_id=item.company_id
    )


# Apaga as decisões automáticas, para a próxima rodada refazer (comando classificar --refazer).
def reset(session: Session, company_id: int | None = None) -> int:
    """As escolhidas por você ficam. Devolve quantas foram apagadas."""
    # Itens da Empresa (ou de todas).
    items = select(BronzeItem.id)
    # Filtro.
    if company_id is not None:
        items = items.where(BronzeItem.company_id == company_id)
    # Apaga.
    result = session.execute(
        delete(ItemClassification).where(
            ItemClassification.locked.is_(False), ItemClassification.bronze_item_id.in_(items)
        )
    )
    # Quantas.
    return result.rowcount or 0


# Classificação de um item, pronta para a tela (ou None).
def of_item(session: Session, item_id: int) -> tuple[ItemClassification, Project | None] | None:
    # Linha.
    row = session.get(ItemClassification, item_id)
    # Sem classificação.
    if row is None:
        return None
    # Com o projeto.
    return row, session.get(Project, row.project_id) if row.project_id else None


# Projetos de vários itens: id → nome (só itens com projeto), para as listas.
def project_of(session: Session, item_ids: list[int]) -> dict[int, str]:
    # Sem itens.
    if not item_ids:
        return {}
    # Consulta.
    rows = session.execute(
        select(ItemClassification.bronze_item_id, Project.name)
        .join(Project, Project.id == ItemClassification.project_id)
        .where(ItemClassification.bronze_item_id.in_(item_ids))
    )
    # Dicionário.
    return dict(rows.all())


# Fila "A revisar": decisões que precisam de você, mais antigas primeiro.
def review_queue(session: Session, company_id: int | None, limit: int = 100) -> list[tuple]:
    """Linhas (item, classificação, projeto sugerido ou None)."""
    # Consulta.
    query = (
        select(BronzeItem, ItemClassification, Project)
        .join(ItemClassification, ItemClassification.bronze_item_id == BronzeItem.id)
        .outerjoin(Project, Project.id == ItemClassification.project_id)
        .where(ItemClassification.needs_review.is_(True))
        .order_by(BronzeItem.occurred_at.asc().nulls_first(), BronzeItem.id)
        .limit(limit)
    )
    # Uma Empresa.
    if company_id is not None:
        query = query.where(BronzeItem.company_id == company_id)
    # Executa.
    return list(session.execute(query).all())


# Números da página Classificação.
def overview(session: Session, company_id: int | None, now: datetime | None = None) -> dict:
    # Filtro de Empresa (vazio = todas).
    of_company = [BronzeItem.company_id == company_id] if company_id is not None else []
    # Quantos itens por método.
    by_method = dict(
        session.execute(
            select(ItemClassification.method, func.count())
            .join(BronzeItem, BronzeItem.id == ItemClassification.bronze_item_id)
            .where(*of_company)
            .group_by(ItemClassification.method)
        ).all()
    )
    # Classificados sem projeto ("não é de projeto nenhum").
    without = session.scalar(
        select(func.count())
        .select_from(ItemClassification)
        .join(BronzeItem, BronzeItem.id == ItemClassification.bronze_item_id)
        .where(ItemClassification.project_id.is_(None), *of_company)
    )
    # Na fila "A revisar".
    review = session.scalar(
        select(func.count())
        .select_from(ItemClassification)
        .join(BronzeItem, BronzeItem.id == ItemClassification.bronze_item_id)
        .where(ItemClassification.needs_review.is_(True), *of_company)
    )
    # Ainda sem classificação.
    waiting = session.scalar(
        select(func.count())
        .select_from(BronzeItem)
        .where(
            BronzeItem.source.in_(SOURCES),
            ~exists().where(ItemClassification.bronze_item_id == BronzeItem.id),
            *of_company,
        )
    )
    # Início do mês (para o gasto com classificação).
    now = now or datetime.now(UTC)
    # Dia 1, meia-noite.
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Gasto do mês com classificação (todas as Empresas: o registro de custo não guarda
    # a Empresa, e o teto também é um só).
    cost = session.scalar(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
            and_(LLMCall.purpose == PURPOSE, LLMCall.created_at >= month_start)
        )
    )
    # Total classificado.
    total = sum(by_method.values())
    # Devolve.
    return {
        # Por método, na ordem da cascata.
        "por_metodo": [(METHOD_LABELS[m], by_method.get(m, 0)) for m in METHOD_LABELS],
        # Total.
        "total": total,
        # Sem projeto.
        "sem_projeto": without or 0,
        # A revisar.
        "revisar": review or 0,
        # Esperando.
        "esperando": waiting or 0,
        # Custo do mês.
        "custo_mes": Decimal(cost or 0),
        # % decidido sem custo (regras e você).
        "sem_custo_pct": round(100 * (total - by_method.get(ClassificationMethod.LLM, 0)) / total)
        if total
        else None,
    }
