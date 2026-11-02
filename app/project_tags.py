"""Tags automáticas de projeto em e-mails e transcrições, pelas regras pedidas pela dona.

As três regras (cada projeto tem a sua tag, com o mesmo nome):
a) "assunto_corpo": o nome do projeto (ou código, outro nome, palavra-chave) aparece no
   assunto ou no corpo do e-mail. Na transcrição, só no título da reunião;
b) "marcador_gmail": o e-mail está numa pasta (marcador) do Gmail ligada à tag. Vale
   também quando você move o e-mail depois de coletado: a rodada do coletor confere as
   pastas a cada coleta e tira a tag quando o e-mail sai da pasta;
c) "equipe": o remetente do e-mail é da equipe do projeto. Na transcrição: alguém da
   equipe foi convidado para a reunião ou falou nela.

Cuidados contra tag errada (falso positivo) na regra a):
- o texto citado de respostas ("Em ... escreveu:", linhas com ">", "De:" do Outlook) e a
  assinatura ("-- ") ficam de fora; em e-mail encaminhado ("Enc:", "Fwd:") o corpo
  inteiro conta, porque o assunto está no conteúdo encaminhado;
- a palavra tem de aparecer inteira ("CRM" não acha "CRMs" nem "acrm");
- termos com menos de 3 letras são ignorados (ex.: um projeto chamado "TI").

Tag tirada à mão não volta: a linha fica marcada como removida e as regras a respeitam.
"""

# re: expressões regulares (padrões de texto).
import re

# dataclass cria classes simples de dados.
from dataclasses import dataclass, field

# Data e hora com fuso.
from datetime import UTC, datetime

# parseaddr separa "Ana <ana@x.com>" em nome e e-mail.
from email.utils import parseaddr

# Consultas.
from sqlalchemy import column, delete, select, table

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Tabelas.
from app.models import (
    BronzeItem,
    GmailLabelTag,
    Project,
    ProjectStakeholder,
    SourceKind,
    Stakeholder,
    Tag,
    TaggedItem,
    Utterance,
)

# Comparação sem acento e sem diferença de maiúsculas.
from app.people import normalize

# Nomes das regras, como ficam gravados.
RULE_TEXT = "assunto_corpo"
# Pasta do Gmail.
RULE_LABEL = "marcador_gmail"
# Pessoa da equipe.
RULE_TEAM = "equipe"
# Colocada por você.
RULE_MANUAL = "manual"
# Como cada regra aparece na tela.
RULE_LABELS = {
    # a) nome, código, outro nome ou palavra-chave do projeto no assunto ou no corpo.
    RULE_TEXT: "nome no texto",
    # b) e-mail numa pasta (marcador) do Gmail ligada ao projeto.
    RULE_LABEL: "pasta do Gmail",
    # c) remetente (ou participante da reunião) da equipe.
    RULE_TEAM: "equipe",
    # Colocada à mão.
    RULE_MANUAL: "manual",
}
# Termos mais curtos que isso são ignorados (muito risco de achar por acaso).
MIN_TERM = 3
# Ids de mensagens pedidos ao Gmail por pasta, no máximo (5 páginas de 500).
MAX_LABEL_MESSAGES = 2500

# Começo do texto citado numa resposta ou da assinatura: daqui para baixo não conta.
_QUOTE_START = re.compile(
    # "Em 2 de nov., Ana escreveu:" (Gmail em português).
    r"^(em .{0,300}escreveu:?"
    # "On Nov 2, Ana wrote:" (Gmail em inglês).
    r"|on .{0,300}wrote:?"
    # "----- Mensagem original -----" (Outlook) e parecidos.
    r"|-{2,}\s*(original message|mensagem original|forwarded message|mensagem encaminhada)\s*-{2,}"
    # "De: Ana <ana@...>" (cabeçalho da mensagem citada no Outlook).
    r"|(de|from):\s.+"
    # "-- " (separador padrão de assinatura).
    r"|-- ?)$",
    # Sem diferenciar maiúsculas.
    re.IGNORECASE,
)
# Assunto de e-mail encaminhado.
_FORWARD = re.compile(r"^\s*(fwd?|enc|encaminhada?)\s*:", re.IGNORECASE)


# As regras de um projeto, prontas para comparar.
@dataclass
class ProjectRule:
    # Id da tag do projeto.
    tag_id: int
    # Padrões dos termos (nome, código, outros nomes, palavras-chave).
    patterns: list[re.Pattern] = field(default_factory=list)
    # E-mails da equipe, em minúsculas.
    emails: set[str] = field(default_factory=set)
    # Ids das pessoas da equipe.
    people: set[int] = field(default_factory=set)

    # O texto (já normalizado) cita o projeto?
    def mentioned_in(self, text: str) -> bool:
        # Algum padrão aparece.
        return any(p.search(text) for p in self.patterns)


# Padrão de "termo inteiro", sem acento e sem diferença de maiúsculas.
def _pattern(term: str) -> re.Pattern | None:
    # Forma comparável.
    plain = normalize(term)
    # Curto demais: ignorado.
    if len(plain) < MIN_TERM:
        return None
    # (?<!\w) e (?!\w): antes e depois não pode ter letra ou número.
    return re.compile(rf"(?<!\w){re.escape(plain)}(?!\w)")


# Lê as regras de todos os projetos ativos que têm tag.
def load_rules(session: Session) -> list[ProjectRule]:
    # Resultado.
    rules: list[ProjectRule] = []
    # Projetos ativos com tag.
    projects = session.scalars(
        select(Project).where(Project.active.is_(True), Project.tag_id.is_not(None))
    ).all()
    # Cada projeto.
    for project in projects:
        # Regras deste projeto.
        rule = ProjectRule(project.tag_id)
        # Nome, código, outros nomes e palavras-chave.
        terms = [project.name, project.code or "", *project.aliases, *project.keywords]
        # Padrões válidos.
        rule.patterns = [p for p in (_pattern(t) for t in terms if t) if p is not None]
        # Pessoas da equipe.
        members = session.execute(
            select(Stakeholder.id, Stakeholder.email)
            .join(ProjectStakeholder, ProjectStakeholder.stakeholder_id == Stakeholder.id)
            .where(ProjectStakeholder.project_id == project.id)
        ).all()
        # Ids.
        rule.people = {m.id for m in members}
        # E-mails.
        rule.emails = {m.email.lower() for m in members if m.email}
        # Guarda.
        rules.append(rule)
    # Devolve.
    return rules


# O texto do próprio e-mail: assunto mais o corpo sem o texto citado e sem a assinatura.
def own_text(subject: str | None, body: str | None) -> str:
    # E-mail encaminhado: o conteúdo encaminhado é o que interessa; conta o corpo inteiro.
    if subject and _FORWARD.match(subject):
        return f"{subject}\n{body or ''}"
    # Linhas que contam.
    kept: list[str] = []
    # Cada linha do corpo.
    for line in (body or "").splitlines():
        # Começo do texto citado ou da assinatura: para aqui.
        if _QUOTE_START.match(line.strip()):
            break
        # Linha citada com ">": pula.
        if line.lstrip().startswith(">"):
            continue
        # Conta.
        kept.append(line)
    # Assunto e corpo próprio.
    return "\n".join([subject or "", *kept])


# E-mails das pessoas de uma transcrição: convidados da agenda e quem falou (reconhecido).
def _meeting_people(session: Session, item: BronzeItem) -> tuple[set[str], set[int]]:
    # Dados do evento.
    event = (item.extra or {}).get("evento_agenda") or {}
    # E-mails dos convidados.
    emails = {e.lower() for e in (event.get("participantes_emails") or {}).values() if e}
    # Pessoas reconhecidas nas falas.
    people = set(
        session.scalars(
            select(Utterance.stakeholder_id).where(
                Utterance.bronze_item_id == item.id, Utterance.stakeholder_id.is_not(None)
            )
        )
    )
    # Devolve os dois.
    return emails, people


# Tags que as regras a) e c) dão a um item: tag → regra.
def rule_tags(session: Session, item: BronzeItem, rules: list[ProjectRule]) -> dict[int, str]:
    # Resultado.
    found: dict[int, str] = {}
    # E-mail.
    if item.source == SourceKind.GMAIL:
        # Texto próprio, normalizado.
        text = normalize(own_text(item.subject, item.body_text))
        # E-mail do remetente.
        sender = parseaddr(item.author or "")[1].lower()
        # Cada projeto.
        for rule in rules:
            # c) remetente da equipe.
            if sender and sender in rule.emails:
                found[rule.tag_id] = RULE_TEAM
            # a) nome no texto.
            elif rule.mentioned_in(text):
                found[rule.tag_id] = RULE_TEXT
    # Transcrição de reunião.
    elif item.source == SourceKind.MEET_TRANSCRIPT:
        # Título normalizado.
        title = normalize(item.subject)
        # Convidados e quem falou.
        emails, people = _meeting_people(session, item)
        # Cada projeto.
        for rule in rules:
            # c) alguém da equipe convidado ou falando.
            if emails & rule.emails or people & rule.people:
                found[rule.tag_id] = RULE_TEAM
            # a) nome no título.
            elif rule.mentioned_in(title):
                found[rule.tag_id] = RULE_TEXT
    # Devolve.
    return found


# Aplica as regras a um item. Devolve quantas tags novas foram colocadas.
def apply_rules(session: Session, item: BronzeItem, rules: list[ProjectRule] | None = None) -> int:
    # Regras (lidas do banco se quem chamou não passou).
    rules = load_rules(session) if rules is None else rules
    # Tags que o item já tem ou que você tirou (as duas a regra não mexe).
    existing = set(
        session.scalars(select(TaggedItem.tag_id).where(TaggedItem.bronze_item_id == item.id))
    )
    # Contador.
    added = 0
    # Cada tag que as regras dão.
    for tag_id, rule in rule_tags(session, item, rules).items():
        # Já tem (ou foi tirada à mão): não mexe.
        if tag_id in existing:
            continue
        # Linha nova.
        session.add(TaggedItem(bronze_item_id=item.id, tag_id=tag_id, rule=rule))
        # Conta.
        added += 1
    # Devolve.
    return added


# Aplica as regras a todos os e-mails e transcrições (depois de mudar projeto ou equipe).
def apply_rules_to_all(session: Session) -> int:
    """Só acrescenta: tags colocadas antes continuam (inclusive as que a regra não daria
    mais), para nada sumir sem você ver. Devolve quantas tags novas."""
    # Regras lidas uma vez.
    rules = load_rules(session)
    # Sem projeto com tag: nada a fazer.
    if not rules:
        return 0
    # Contador.
    added = 0
    # Todos os e-mails e transcrições.
    items = session.scalars(
        select(BronzeItem).where(
            BronzeItem.source.in_([SourceKind.GMAIL, SourceKind.MEET_TRANSCRIPT])
        )
    )
    # Cada item.
    for item in items:
        # Aplica.
        added += apply_rules(session, item, rules)
    # Devolve.
    return added


# Ids das mensagens numa pasta do Gmail; o segundo valor diz se a lista veio inteira.
def _label_message_ids(gmail, label_id: str) -> tuple[set[str], bool]:
    # Ids.
    ids: set[str] = set()
    # Marcador de página.
    token = None
    # Repete até acabar ou chegar ao limite.
    while len(ids) < MAX_LABEL_MESSAGES:
        # Pede uma página.
        response = (
            gmail.users()
            .messages()
            .list(userId="me", labelIds=[label_id], maxResults=500, pageToken=token)
            .execute(num_retries=3)
        )
        # Acrescenta os ids.
        ids.update(m["id"] for m in response.get("messages", []))
        # Próxima página.
        token = response.get("nextPageToken")
        # Acabou: lista inteira.
        if not token:
            return ids, True
    # Parou no limite: a lista não está inteira.
    return ids, False


# Confere as pastas do Gmail ligadas a tags e acerta as tags dos e-mails coletados.
def sync_label_tags(session: Session, gmail) -> tuple[int, int]:
    """(tags colocadas, tags tiradas). Roda a cada coleta do Gmail."""
    # Ligações pasta → tag.
    links = session.scalars(select(GmailLabelTag)).all()
    # Nenhuma: nada a fazer.
    if not links:
        return 0, 0
    # Mensagens por tag (união das pastas da tag) e se todas as listas vieram inteiras.
    by_tag: dict[int, set[str]] = {}
    # Tags cuja lista está completa (só nelas a tag pode ser tirada com segurança).
    complete: dict[int, bool] = {}
    # Cada ligação.
    for link in links:
        # Ids da pasta.
        ids, whole = _label_message_ids(gmail, link.label_id)
        # Junta aos da tag.
        by_tag.setdefault(link.tag_id, set()).update(ids)
        # Completa só se todas as pastas da tag vieram inteiras.
        complete[link.tag_id] = complete.get(link.tag_id, True) and whole
    # Contadores.
    added = removed = 0
    # Cada tag.
    for tag_id, message_ids in by_tag.items():
        # Itens do bronze dessas mensagens.
        items = (
            session.execute(
                select(BronzeItem.id, BronzeItem.external_id).where(
                    BronzeItem.source == SourceKind.GMAIL,
                    BronzeItem.external_id.in_(message_ids),
                )
            ).all()
            if message_ids
            else []
        )
        # Ids internos.
        inside = {row.id for row in items}
        # Linhas desta tag que já existem (inclusive as tiradas à mão).
        rows = {
            row.bronze_item_id: row
            for row in session.scalars(select(TaggedItem).where(TaggedItem.tag_id == tag_id))
        }
        # E-mails na pasta sem a tag: coloca.
        for item_id in inside - rows.keys():
            # Linha nova.
            session.add(TaggedItem(bronze_item_id=item_id, tag_id=tag_id, rule=RULE_LABEL))
            # Conta.
            added += 1
        # Lista incompleta: não tira nada (o e-mail pode estar além do limite).
        if not complete[tag_id]:
            continue
        # Tags postas pela pasta em e-mails que saíram dela: tira.
        for item_id, row in rows.items():
            # Só as postas por esta regra, ainda valendo, de e-mails fora da pasta.
            if row.rule == RULE_LABEL and row.removed_at is None and item_id not in inside:
                # Apaga a linha (não é tag tirada à mão: outra regra pode dar de novo).
                session.delete(row)
                # Conta.
                removed += 1
    # Devolve.
    return added, removed


# Coloca uma tag à mão (cria a tag se o nome for novo).
def add_manual(session: Session, item_id: int, tag: Tag) -> None:
    # Linha existente (ativa ou tirada antes).
    row = session.get(TaggedItem, (item_id, tag.id))
    # Não existe: cria.
    if row is None:
        session.add(TaggedItem(bronze_item_id=item_id, tag_id=tag.id, rule=RULE_MANUAL))
        # Pronto.
        return
    # Existe: volta a valer e passa a ser "manual".
    row.removed_at = None
    # Regra.
    row.rule = RULE_MANUAL


# Tira uma tag à mão: a linha fica marcada, e as regras não a colocam de novo.
def remove_manual(session: Session, item_id: int, tag_id: int) -> None:
    # Linha.
    row = session.get(TaggedItem, (item_id, tag_id))
    # Não existe: grava a marca mesmo assim (a regra não deve colocar depois).
    if row is None:
        session.add(
            TaggedItem(
                bronze_item_id=item_id,
                tag_id=tag_id,
                rule=RULE_MANUAL,
                removed_at=datetime.now(UTC),
            )
        )
        # Pronto.
        return
    # Marca como tirada.
    row.removed_at = datetime.now(UTC)


# Tags valendo de vários itens: id do item → [(tag, regra)].
def tags_of(session: Session, item_ids: list[int]) -> dict[int, list[tuple[Tag, str]]]:
    # Sem itens.
    if not item_ids:
        return {}
    # Linhas valendo, com a tag.
    rows = session.execute(
        select(TaggedItem, Tag)
        .join(Tag, Tag.id == TaggedItem.tag_id)
        .where(TaggedItem.bronze_item_id.in_(item_ids), TaggedItem.removed_at.is_(None))
        .order_by(Tag.name)
    ).all()
    # Agrupa por item.
    result: dict[int, list[tuple[Tag, str]]] = {}
    # Cada linha.
    for row, tag in rows:
        # Acrescenta.
        result.setdefault(row.bronze_item_id, []).append((tag, row.rule))
    # Devolve.
    return result


# Liga uma pasta do Gmail à tag de um projeto.
def link_label(session: Session, label_id: str, label_name: str, tag_id: int) -> None:
    # Já ligada: nada a fazer.
    if session.get(GmailLabelTag, (label_id, tag_id)) is not None:
        return
    # Ligação nova (a próxima coleta põe a tag nos e-mails da pasta).
    session.add(GmailLabelTag(label_id=label_id, tag_id=tag_id, label_name=label_name[:500]))


# Desliga uma pasta do Gmail de uma tag e tira as tags que essa ligação tinha posto.
def unlink_label(session: Session, label_id: str, tag_id: int) -> None:
    # Apaga a ligação.
    session.execute(
        delete(GmailLabelTag).where(
            GmailLabelTag.label_id == label_id, GmailLabelTag.tag_id == tag_id
        )
    )
    # Tira as tags postas pela regra da pasta (se outra pasta da mesma tag continuar
    # ligada, a próxima coleta põe de novo nos e-mails dela).
    session.execute(
        delete(TaggedItem).where(
            TaggedItem.tag_id == tag_id,
            TaggedItem.rule == RULE_LABEL,
            TaggedItem.removed_at.is_(None),
        )
    )


# Visão "bronze_item_tags" (migração 0004): tags das versões do Drive, com a herança das
# pastas. Declarada aqui só com as colunas usadas, para consultar sem SQL escrito à mão.
_DRIVE_TAGS = table("bronze_item_tags", column("bronze_item_id"), column("tag_id"), column("tag"))


# Nomes das tags de vários itens, de qualquer fonte: id do item → [nomes].
def tag_names(session: Session, item_ids: list[int]) -> dict[int, list[str]]:
    # Sem itens.
    if not item_ids:
        return {}
    # Resultado.
    result: dict[int, list[str]] = {}
    # E-mails e transcrições.
    for item_id, tags in tags_of(session, item_ids).items():
        # Só os nomes.
        result[item_id] = [tag.name for tag, _ in tags]
    # Versões do Drive (tags das pastas).
    rows = session.execute(
        select(_DRIVE_TAGS.c.bronze_item_id, _DRIVE_TAGS.c.tag).where(
            _DRIVE_TAGS.c.bronze_item_id.in_(item_ids)
        )
    ).all()
    # Cada linha.
    for item_id, name in rows:
        # Acrescenta.
        result.setdefault(item_id, []).append(name)
    # Devolve, em ordem alfabética.
    return {k: sorted(set(v)) for k, v in result.items()}


# Ids dos itens com uma tag, de qualquer fonte (para o filtro do Conteúdo coletado).
def items_with_tag(tag_id: int):
    """Subconsulta: e-mails e transcrições com a tag valendo, mais as versões do Drive."""
    # E-mails e transcrições.
    own = select(TaggedItem.bronze_item_id).where(
        TaggedItem.tag_id == tag_id, TaggedItem.removed_at.is_(None)
    )
    # Drive.
    drive = select(_DRIVE_TAGS.c.bronze_item_id).where(_DRIVE_TAGS.c.tag_id == tag_id)
    # União das duas.
    return own.union(drive)
