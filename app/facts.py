"""Extração de fatos com trecho literal verificado por código (bloco 3, segunda parte).

O que resolve: com cada item já ligado a um projeto (app/classification.py), falta tirar
dele o que um gerente de projetos usa: tarefas, decisões, riscos, problemas, premissas,
dependências, prazos e definições (a base do RAID e do plano de ação). Isso vai para a
tabela facts, a camada silver.

Como funciona, em quatro passos:
1. o item espera ter projeto (e estar fora da fila "A revisar") e ter os anexos lidos;
2. o Claude lê o texto próprio do item (sem o texto citado de respostas anteriores) e
   devolve uma lista de fatos num JSON de formato fixo (saída estruturada: a API garante
   o formato, então o código não precisa adivinhar onde está o JSON);
3. o CÓDIGO confere se o trecho que o Claude citou existe mesmo no texto. Fato com
   trecho achado fica com quote_verified = true; sem trecho achado fica marcado como
   suspeito (quote_verified = false). É o controle de alucinação da Arquitetura: o fato
   inventado não tem de onde citar;
4. o código liga o responsável e quem disse às Pessoas cadastradas da Empresa do item.

Por que o código confere, e não o próprio Claude: pedir ao modelo "confira se você não
inventou" usa o mesmo modelo que pode ter errado. Procurar um texto dentro de outro é
uma operação exata, de custo zero e que não alucina.

Separação por Empresa (premissa 6): o Claude vê só o item, o projeto dele e a equipe do
projeto. O responsável e quem disse só são ligados a Pessoas da Empresa do item, e o
banco recusa (gatilho da 0008, estendido na 0012) um fato ligado a outra Empresa.

Custo: Haiku (FACTS_MODEL), esforço baixo, até FACTS_LLM_PER_RUN itens por rodada, cada
chamada no registro de custo com o motivo "extracao_fatos" e sob o teto mensal.
"""

# json: lê a resposta do Claude.
import json

# logging: mensagens no log do coletor.
import logging

# re: acha o e-mail dentro de "Nome <email>".
import re

# unicodedata: deixa aspas, traços e espaços especiais na mesma forma, para comparar.
import unicodedata

# dataclass: classes simples de dados.
from dataclasses import dataclass

# Data (prazo) e data e hora com fuso.
from datetime import UTC, date, datetime

# Decimal: confiança com casas exatas.
from decimal import Decimal, InvalidOperation

# ZoneInfo: fuso do console, para dizer ao Claude o dia local do item.
from zoneinfo import ZoneInfo

# Erros da API da Anthropic.
import anthropic

# Consultas: and_ junta condições; exists pergunta "existe alguma linha?".
from sqlalchemy import and_, delete, exists, func, or_, select, update

# Session é a conversa com o banco; sessionmaker é a fábrica de sessões.
from sqlalchemy.orm import Session, aliased, sessionmaker

# Configuração.
from app.config import Settings

# Códigos de "esperando a leitura pelo Claude" e a regra para reconhecê-los.
from app.ingestion.vision import PENDING_CODES, is_pending

# Cliente do Claude, teto e gasto do mês.
from app.llm import BudgetExceededError, LLMClient, month_spend_usd

# Tabelas.
from app.models import (
    Attachment,
    BronzeItem,
    Fact,
    FactExtraction,
    FactKind,
    ItemClassification,
    LLMCall,
    Project,
    ProjectStakeholder,
    SourceKind,
    Stakeholder,
    Utterance,
    audit,
)

# Reconhecer pessoas pelo nome ou e-mail, e os participantes da reunião.
from app.people import PeopleIndex, attendees_of

# Texto próprio do e-mail (sem a parte citada de respostas anteriores e sem assinatura).
from app.project_tags import own_text

# Logger deste módulo.
log = logging.getLogger(__name__)

# Fontes lidas (as mesmas da classificação).
SOURCES = (SourceKind.GMAIL, SourceKind.MEET_TRANSCRIPT, SourceKind.DRIVE)
# Motivo gravado no registro de custo de cada chamada.
PURPOSE = "extracao_fatos"
# Trecho mais curto aceito como prova, em caracteres. "sim" ou "ok" existem em quase
# qualquer texto e não provam nada.
MIN_QUOTE_CHARS = 12
# Distância máxima entre as partes de um trecho com reticências ("A ... B"): partes
# muito distantes podem juntar frases que, separadas, dizem outra coisa.
MAX_GAP_CHARS = 200
# Máximo de fatos gravados por item (um e-mail com 80 "fatos" é sinal de erro).
MAX_FACTS_PER_ITEM = 40
# Máximo de pessoas da equipe mostradas ao Claude.
MAX_TEAM_IN_PROMPT = 40
# Origens de anexo que têm texto próprio (os links do Drive viram itens separados).
_ATTACHMENT_ORIGINS = ("anexo", "imagem_embutida")

# Nome de cada tipo na tela.
KIND_LABELS = {
    FactKind.TASK: "Tarefa",
    FactKind.DECISION: "Decisão",
    FactKind.RISK: "Risco",
    FactKind.ISSUE: "Problema",
    FactKind.ASSUMPTION: "Premissa",
    FactKind.DEPENDENCY: "Dependência",
    FactKind.DEADLINE: "Prazo",
    FactKind.DEFINITION: "Definição",
}
# Tipos que nascem "aberto" (têm ciclo de vida: alguém resolve ou fecha depois).
OPEN_KINDS = {
    FactKind.TASK,
    FactKind.RISK,
    FactKind.ISSUE,
    FactKind.DEPENDENCY,
    FactKind.DEADLINE,
}

# Texto ou vazio, no formato do esquema JSON (o esquema não aceita "type": [..]).
_TEXT_OR_NULL = {"anyOf": [{"type": "string"}, {"type": "null"}]}

# Formato fixo da resposta. Com a saída estruturada, a API só devolve JSON neste formato.
# Todos os campos são obrigatórios (vazio vem como null), como a API exige.
SCHEMA = {
    # Um objeto...
    "type": "object",
    # ...com a lista "fatos".
    "properties": {
        # A lista.
        "fatos": {
            # Lista...
            "type": "array",
            # ...de objetos, cada um um fato.
            "items": {
                # Objeto.
                "type": "object",
                # Campos de cada fato.
                "properties": {
                    # Tipo: só os oito da silver.
                    "tipo": {"type": "string", "enum": [k.value for k in FactKind]},
                    # Resumo curto.
                    "titulo": {"type": "string"},
                    # Detalhe (ou null).
                    "descricao": _TEXT_OR_NULL,
                    # Responsável como está no texto (ou null).
                    "responsavel": _TEXT_OR_NULL,
                    # Prazo em AAAA-MM-DD (ou null).
                    "prazo": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
                    # Cópia exata do texto que sustenta o fato.
                    "trecho": {"type": "string"},
                    # Confiança de 0 a 1.
                    "confianca": {"type": "number"},
                },
                # Todos obrigatórios.
                "required": [
                    "tipo",
                    "titulo",
                    "descricao",
                    "responsavel",
                    "prazo",
                    "trecho",
                    "confianca",
                ],
                # Nenhum campo além destes.
                "additionalProperties": False,
            },
        }
    },
    # "fatos" é obrigatório (lista vazia quando não há nenhum).
    "required": ["fatos"],
    # Nada além de "fatos".
    "additionalProperties": False,
}

# Instrução fixa do Claude: igual em todas as chamadas (o que permite cache no futuro).
SYSTEM = (
    "Você extrai fatos de gestão de projetos de conteúdo de trabalho (e-mail, transcrição "
    "de reunião ou documento) de uma gerente de projetos. Tipos: "
    "tarefa (alguém precisa fazer algo); decisao (algo foi decidido ou aprovado); "
    "risco (algo pode dar errado); problema (algo já deu errado ou está bloqueado); "
    "premissa (algo assumido como verdade sem confirmação); dependencia (uma entrega "
    "depende de outra entrega ou de terceiros); prazo (data combinada para uma entrega "
    "ou marco); definicao (termo, regra ou critério definido no projeto).\n"
    "Regras:\n"
    "- Extraia só o que está escrito. Não deduza, não invente responsável nem data.\n"
    '- "trecho" é uma cópia exata, caractere por caractere, de uma frase do texto que '
    "sustenta o fato: sem reescrever, sem traduzir, sem reticências e sem juntar partes "
    "distantes. Em transcrição, o trecho fica dentro de uma única fala. Use de 5 a 40 "
    "palavras.\n"
    '- "titulo": frase curta em português; no infinitivo para tarefa ("Enviar o '
    'cronograma revisado") e afirmativa para os demais tipos.\n'
    '- "responsavel": quem deve fazer, com o nome como está no texto (prefira o nome da '
    "equipe do projeto quando for a mesma pessoa); null se o texto não diz.\n"
    '- "prazo": data AAAA-MM-DD só quando o texto diz a data, ou uma expressão que a data '
    'do item resolve ("sexta", "amanhã"); senão null.\n'
    "- Cumprimentos, assinaturas, avisos legais e conversa sem conteúdo de projeto não "
    "geram fatos. O mesmo fato dito duas vezes vira um só.\n"
    "- Sem fato nenhum, devolva a lista vazia.\n"
    '- "confianca": de 0 a 1, o quanto o trecho sustenta o fato.'
)

# Aspas, traços, espaços e reticências especiais → forma simples, para comparar textos.
_SAME_CHAR = str.maketrans(
    {
        # Aspas curvas simples.
        "‘": "'",
        "’": "'",
        # Aspas curvas duplas e as de citação.
        "“": '"',
        "”": '"',
        "«": '"',
        "»": '"',
        # Traços.
        "–": "-",
        "—": "-",
        # Espaço que não quebra.
        " ": " ",
        # Reticências num caractere só.
        "…": "...",
    }
)
# E-mail dentro de "Nome <email>".
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


# Resumo de uma rodada, para o log do coletor e o comando extrair-fatos.
@dataclass
class ExtractSummary:
    # Itens lidos pelo Claude.
    items: int = 0
    # Fatos gravados.
    facts: int = 0
    # Fatos com o trecho conferido.
    verified: int = 0
    # Itens registrados sem chamada (sem texto, ou outra transcrição da reunião já lida).
    skipped: int = 0
    # Esperando (limite da rodada, teto, erro da API, anexo esperando a leitura).
    waiting: int = 0
    # True se o teto do mês foi atingido.
    budget_hit: bool = False

    # Uma linha legível.
    def describe(self) -> str:
        # Texto base.
        text = (
            f"Fatos: {self.items} item(ns) lido(s), {self.facts} fato(s), "
            f"{self.verified} com trecho conferido, {self.waiting} esperando."
        )
        # Aviso do teto.
        return text + (" Teto mensal atingido." if self.budget_hit else "")


# Um fato lido da resposta, antes de gravar.
@dataclass
class Candidate:
    # Tipo.
    kind: FactKind
    # Resumo.
    title: str
    # Detalhe.
    description: str | None
    # Responsável como está no texto.
    owner: str | None
    # Prazo.
    due: date | None
    # Trecho citado.
    quote: str
    # Confiança.
    confidence: Decimal


# Forma comparável de um texto: mesmas aspas e traços, sem maiúsculas, um espaço só.
def normalize(text: str | None) -> str:
    # Sem texto.
    if not text:
        return ""
    # NFKC junta letras compostas (é = e + acento) numa forma só, como no original.
    text = unicodedata.normalize("NFKC", text)
    # Aspas, traços, espaços e reticências.
    text = text.translate(_SAME_CHAR)
    # casefold: minúsculas, inclusive em casos especiais ("ß" → "ss").
    text = text.casefold()
    # Qualquer sequência de espaços, tabs e quebras de linha vira um espaço.
    return " ".join(text.split())


# Confere se o trecho citado existe no texto (já normalizado). É o coração anti-alucinação.
def quote_found(quote: str, source_norm: str) -> bool:
    """Aceita o trecho exato ou, se o Claude usou reticências, as partes na ordem e perto."""
    # Forma comparável, sem as aspas que o Claude às vezes põe em volta.
    q = normalize(quote).strip(" \"'")
    # Curto demais para provar alguma coisa.
    if len(q) < MIN_QUOTE_CHARS:
        return False
    # Achou inteiro: confirmado.
    if q in source_norm:
        return True
    # Partes separadas por reticências ("A ... B"), sem as vazias.
    parts = [p.strip(" ,;") for p in q.split("...") if p.strip(" ,;")]
    # Sem reticências (uma parte só): não achou.
    if len(parts) < 2:
        return False
    # Cada parte também precisa ter tamanho de prova.
    if any(len(p) < MIN_QUOTE_CHARS for p in parts):
        return False
    # Procura cada parte depois da anterior.
    position = source_norm.find(parts[0])
    # A primeira não está no texto.
    if position < 0:
        return False
    # Fim da primeira parte.
    end = position + len(parts[0])
    # As outras partes.
    for part in parts[1:]:
        # Procura depois do fim da anterior.
        found = source_norm.find(part, end)
        # Não achou, ou achou longe demais.
        if found < 0 or found - end > MAX_GAP_CHARS:
            return False
        # Avança.
        end = found + len(part)
    # Todas as partes, na ordem e perto: confirmado.
    return True


# O texto que o Claude lê. É também o texto em que o código procura o trecho citado.
def source_text(session: Session, item: BronzeItem) -> str:
    # E-mail: assunto e texto próprio (sem a conversa citada, que já foi lida antes).
    if item.source == SourceKind.GMAIL:
        # Texto próprio.
        text = own_text(item.subject, item.body_text).strip()
        # Anexos com texto (PDF, planilha, imagem lida), na ordem em que chegaram.
        rows = session.execute(
            select(Attachment.filename, Attachment.extracted_text)
            .where(
                Attachment.bronze_item_id == item.id,
                Attachment.origin.in_(_ATTACHMENT_ORIGINS),
                Attachment.extracted_text.is_not(None),
            )
            .order_by(Attachment.id)
        ).all()
        # Junta cada anexo com um cabeçalho, para o Claude saber de onde veio.
        for filename, extracted in rows:
            # Só anexos com texto de verdade.
            if extracted.strip():
                text += f"\n\n[Anexo: {filename}]\n{extracted.strip()}"
        # Devolve.
        return text
    # Transcrição e arquivo do Drive: título e texto inteiro.
    return f"{item.subject or ''}\n{item.body_text or ''}".strip()


# Item espera algum arquivo ser lido pelo Claude (anexo ou versão do Drive)?
def waiting_for_reading(session: Session, item: BronzeItem) -> bool:
    # Drive: o erro de leitura fica no "extra" da versão.
    if item.source == SourceKind.DRIVE:
        return is_pending((item.extra or {}).get("erro_extracao"))
    # E-mail: anexo com código de espera, original guardado e aviso não dispensado (as
    # mesmas condições da fila de leitura, app/ingestion/vision_queue.py).
    if item.source == SourceKind.GMAIL:
        # Condição "o erro começa com um dos códigos de espera".
        pending = or_(*(Attachment.extraction_error.like(f"[{code}]%") for code in PENDING_CODES))
        # Existe algum?
        return bool(
            session.scalar(
                select(
                    exists().where(
                        Attachment.bronze_item_id == item.id,
                        Attachment.origin.in_(_ATTACHMENT_ORIGINS),
                        Attachment.object_key.is_not(None),
                        Attachment.dismissed_at.is_(None),
                        pending,
                    )
                )
            )
        )
    # Transcrição: nada a esperar.
    return False


# Ids dos itens prontos para a extração: com projeto, fora da revisão e ainda não lidos.
def pending_ids(session: Session, limit: int | None = None) -> list[int]:
    # Já tem registro de extração?
    extracted = exists().where(FactExtraction.bronze_item_id == BronzeItem.id)
    # Consulta: mais antigos primeiro (um prazo "sexta" depende da data do item).
    query = (
        select(BronzeItem.id)
        .join(ItemClassification, ItemClassification.bronze_item_id == BronzeItem.id)
        .where(
            BronzeItem.source.in_(SOURCES),
            # Com projeto: um item "de projeto nenhum" (newsletter) não gasta Claude.
            ItemClassification.project_id.is_not(None),
            # Fora da fila "A revisar": o projeto pode mudar quando você decidir.
            ItemClassification.needs_review.is_(False),
            ~extracted,
        )
        .order_by(BronzeItem.occurred_at.asc().nulls_first(), BronzeItem.id)
    )
    # Limite pedido (ex.: 1, só para saber se há algo).
    if limit is not None:
        query = query.limit(limit)
    # Executa.
    return list(session.scalars(query))


# A equipe do projeto, como o Claude a vê (nome, papel e se é liderança).
def team_text(session: Session, project_id: int) -> str:
    # Pessoas do projeto.
    rows = session.execute(
        select(Stakeholder.name, ProjectStakeholder.role_in_project)
        .join(ProjectStakeholder, ProjectStakeholder.stakeholder_id == Stakeholder.id)
        .where(ProjectStakeholder.project_id == project_id)
        .order_by(Stakeholder.name)
        .limit(MAX_TEAM_IN_PROMPT)
    ).all()
    # Uma pessoa por item da lista.
    return ", ".join(f"{name} ({role})" if role else name for name, role in rows)


# Monta o pedido ao Claude.
def build_prompt(
    session: Session, item: BronzeItem, project: Project, text: str, settings: Settings
) -> str:
    # Linhas do cabeçalho.
    head: list[str] = []
    # Tipo do item.
    kind = {
        SourceKind.GMAIL: "e-mail",
        SourceKind.MEET_TRANSCRIPT: "transcrição de reunião",
        SourceKind.DRIVE: "arquivo do Google Drive",
    }[item.source]
    # Tipo.
    head.append(f"Tipo: {kind}")
    # Data do item no fuso do console (para resolver "sexta", "amanhã").
    if item.occurred_at:
        # Dia local, com o dia da semana.
        local = item.occurred_at.astimezone(ZoneInfo(settings.console_timezone))
        # Ex.: 2026-11-04 (quarta-feira).
        weekday = [
            "segunda-feira",
            "terça-feira",
            "quarta-feira",
            "quinta-feira",
            "sexta-feira",
            "sábado",
            "domingo",
        ][local.weekday()]
        # Linha.
        head.append(f"Data do item: {local.date().isoformat()} ({weekday})")
    # Quem escreveu (e-mail).
    if item.source == SourceKind.GMAIL and item.author:
        head.append(f"De: {item.author}")
    # Projeto.
    head.append(f"Projeto: {project.name}")
    # Equipe cadastrada.
    team = team_text(session, project.id)
    # Mostra.
    if team:
        head.append(f"Equipe do projeto: {team}")
    # Junta.
    return "\n".join(head) + "\n\nTexto:\n" + text


# Lê a resposta do Claude. Erro de formato vira ValueError com o motivo.
def parse_answer(text: str) -> list[Candidate]:
    # Lê o JSON (a saída estruturada garante o formato; o "try" cobre resposta cortada).
    try:
        data = json.loads(text or "")
    # JSON quebrado (ex.: a resposta bateu no limite de tamanho).
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido ({exc.msg})") from exc
    # A lista.
    raw = data.get("fatos") if isinstance(data, dict) else None
    # Não é lista.
    if not isinstance(raw, list):
        raise ValueError("a resposta não trouxe a lista de fatos")
    # Resultado.
    found: list[Candidate] = []
    # Cada fato.
    for entry in raw:
        # Tenta ler; um fato com defeito é pulado, os outros ficam.
        try:
            found.append(_candidate(entry))
        # Defeito.
        except (KeyError, TypeError, ValueError, InvalidOperation):
            log.warning("Fato ignorado (formato inesperado): %r", entry)
    # Devolve.
    return found


# Um fato da resposta, conferido campo a campo.
def _candidate(entry: dict) -> Candidate:
    # Tipo (fora da lista levanta ValueError).
    kind = FactKind(entry["tipo"])
    # Título obrigatório.
    title = str(entry["titulo"]).strip()
    # Vazio não serve.
    if not title:
        raise ValueError("título vazio")
    # Prazo: só data válida (o Claude às vezes manda "2026-02-30").
    raw_due = entry.get("prazo")
    # Lê.
    due = date.fromisoformat(raw_due) if raw_due else None
    # Confiança entre 0 e 1, com 3 casas.
    confidence = Decimal(str(entry.get("confianca", 0)))
    # Limites.
    confidence = min(max(confidence, Decimal(0)), Decimal(1)).quantize(Decimal("0.001"))
    # Texto opcional, sem espaços nas pontas (vazio vira None).
    description = (str(entry.get("descricao") or "").strip()) or None
    # Responsável.
    owner = (str(entry.get("responsavel") or "").strip()) or None
    # Fato.
    return Candidate(
        kind,
        title[:500],
        description,
        owner[:200] if owner else None,
        due,
        str(entry.get("trecho") or "").strip(),
        confidence,
    )


# Quem disse o trecho: (pessoa, nome como apareceu).
def speaker_of(
    session: Session,
    item: BronzeItem,
    quote: str,
    people: PeopleIndex,
    utterances: list[Utterance],
) -> tuple[int | None, str | None]:
    # E-mail: quem escreveu é o remetente.
    if item.source == SourceKind.GMAIL:
        # E-mail dentro do campo "De".
        match = _EMAIL.search(item.author or "")
        # Pessoa pelo e-mail (só da Empresa do item: o índice é dela).
        person = people.by_email.get(match.group(0).lower()) if match else None
        # Nome como apareceu.
        return person, (item.author or "")[:200] or None
    # Transcrição: a fala que contém o trecho.
    if item.source == SourceKind.MEET_TRANSCRIPT:
        # Começo do trecho (antes de reticências), na forma comparável.
        start = normalize(quote).strip(" \"'").split("...")[0].strip()
        # Curto demais para achar a fala com segurança.
        if len(start) < MIN_QUOTE_CHARS:
            return None, None
        # Cada fala, procurando o trecho em "Nome: texto" (o Claude às vezes cita o nome).
        for u in utterances:
            # Texto comparável da fala.
            said = normalize(f"{u.speaker or ''}: {u.text}")
            # Está nesta fala.
            if start in said:
                return u.stakeholder_id, (u.speaker or "")[:200] or None
    # Drive (autor desconhecido) ou trecho fora de qualquer fala.
    return None, None


# Grava o registro da extração (cria ou troca).
def _mark(session: Session, item: BronzeItem, status: str, **values) -> FactExtraction:
    # Registro existente.
    row = session.get(FactExtraction, item.id)
    # Não existe: cria.
    if row is None:
        row = FactExtraction(bronze_item_id=item.id, status=status)
        # Prepara.
        session.add(row)
    # Situação.
    row.status = status
    # Momento.
    row.extracted_at = datetime.now(UTC)
    # Modelo.
    row.model = values.get("model")
    # Fatos devolvidos.
    row.facts_found = values.get("found", 0)
    # Fatos conferidos.
    row.facts_verified = values.get("verified", 0)
    # Corte.
    row.truncated = values.get("truncated", False)
    # Observação.
    row.note = values.get("note")
    # Devolve.
    return row


# Outra transcrição da mesma reunião já teve os fatos extraídos? Devolve o id dela.
def _sibling_done(session: Session, item: BronzeItem) -> int | None:
    # Só transcrições ligadas a uma reunião.
    if item.source != SourceKind.MEET_TRANSCRIPT or item.meeting_id is None:
        return None
    # Outra transcrição da reunião, lida com sucesso.
    return session.scalar(
        select(BronzeItem.id)
        .join(FactExtraction, FactExtraction.bronze_item_id == BronzeItem.id)
        .where(
            BronzeItem.meeting_id == item.meeting_id,
            BronzeItem.id != item.id,
            FactExtraction.status == "ok",
        )
        .limit(1)
    )


# Extrai os fatos de um item. Devolve "lido", "pulado" ou "esperando".
def extract_item(
    session: Session, item: BronzeItem, settings: Settings, llm: LLMClient | None
) -> tuple[str, int, int]:
    """Devolve (situação, fatos gravados, fatos conferidos). Erro da API sobe."""
    # Classificação do item (o projeto do fato).
    classification = session.get(ItemClassification, item.id)
    # Sem projeto (mudou desde a consulta): nada a fazer agora.
    if classification is None or classification.project_id is None:
        return "esperando", 0, 0
    # Arquivo ainda esperando a leitura pelo Claude: lê depois, com o texto completo.
    if waiting_for_reading(session, item):
        return "esperando", 0, 0
    # Outra transcrição da mesma reunião já foi lida: não duplica os fatos.
    sibling = _sibling_done(session, item)
    # Já lida.
    if sibling is not None:
        # Registra sem chamar o Claude.
        _mark(
            session,
            item,
            "duplicada",
            note=f"Fatos já extraídos da outra transcrição desta reunião (item {sibling}).",
        )
        # Pulado.
        return "pulado", 0, 0
    # O texto.
    text = source_text(session, item)
    # Sem texto: registra e não chama.
    if not text.strip():
        # Registro.
        _mark(session, item, "sem_texto", note="O item não tem texto.")
        # Pulado.
        return "pulado", 0, 0
    # Claude indisponível agora (sem chave, teto ou limite da rodada): espera.
    if llm is None:
        return "esperando", 0, 0
    # Corte no limite, avisando o Claude.
    truncated = len(text) > settings.facts_max_chars
    # Corta.
    if truncated:
        text = text[: settings.facts_max_chars]
    # Projeto.
    project = session.get(Project, classification.project_id)
    # Pedido.
    prompt = build_prompt(session, item, project, text, settings)
    # Aviso de corte.
    if truncated:
        prompt += f"\n[texto cortado: só os primeiros {settings.facts_max_chars} caracteres]"
    # Chamada (teto e registro de custo ficam no LLMClient).
    result = llm.generate(
        purpose=PURPOSE,
        prompt=prompt,
        system=SYSTEM,
        # Espaço para o raciocínio curto e até 40 fatos.
        max_tokens=12000,
        # Ler e copiar trechos não pede raciocínio longo.
        effort="low",
        # Haiku por padrão (FACTS_MODEL).
        model=settings.facts_model,
        # Formato fixo da resposta.
        json_schema=SCHEMA,
    )
    # Recusa: registra (a página mostra) e não tenta de novo sozinho.
    if result.refused:
        # Registro.
        _mark(session, item, "recusa", model=result.model, note="O Claude recusou ler este item.")
        # Lido (pagou a chamada).
        return "lido", 0, 0
    # Lê a resposta.
    try:
        candidates = parse_answer(result.text)
    # Resposta ilegível (ex.: cortada no limite): registra, sem pagar de novo.
    except ValueError as exc:
        # Registro.
        _mark(session, item, "ilegivel", model=result.model, truncated=truncated, note=str(exc))
        # Lido.
        return "lido", 0, 0
    # Fatos antigos deste item saem (refazer não duplica).
    session.execute(delete(Fact).where(Fact.source_item_id == item.id))
    # Texto comparável, uma vez só.
    source_norm = normalize(text)
    # Pessoas da Empresa do item.
    people = PeopleIndex(session, item.company_id)
    # Participantes da reunião (nome → e-mail), para reconhecer o responsável.
    attendees = attendees_of(session, item)
    # Falas da transcrição, na ordem (para achar quem disse).
    utterances = (
        list(
            session.scalars(
                select(Utterance)
                .where(Utterance.bronze_item_id == item.id)
                .order_by(Utterance.position)
            )
        )
        if item.source == SourceKind.MEET_TRANSCRIPT
        else []
    )
    # Fatos já vistos neste item (tipo + título), para não gravar o mesmo duas vezes.
    seen: set[tuple[str, str]] = set()
    # Contadores.
    saved = verified = 0
    # Cada fato, até o máximo.
    for c in candidates[:MAX_FACTS_PER_ITEM]:
        # Chave do repetido.
        key = (c.kind.value, normalize(c.title))
        # Repetido: pula.
        if key in seen:
            continue
        # Marca.
        seen.add(key)
        # O código confere o trecho.
        ok = quote_found(c.quote, source_norm)
        # Quem disse.
        said_by, said_by_text = speaker_of(session, item, c.quote, people, utterances)
        # Grava.
        session.add(
            Fact(
                # Projeto do item.
                project_id=classification.project_id,
                # Tipo.
                kind=c.kind,
                # Resumo.
                title=c.title,
                # Detalhe.
                description=c.description,
                # Responsável cadastrado (só da Empresa do item).
                owner_id=people.match(c.owner, attendees),
                # Responsável como está no texto.
                owner_text=c.owner,
                # Prazo.
                due_date=c.due,
                # Aberto para os tipos com ciclo de vida.
                status="aberto" if c.kind in OPEN_KINDS else None,
                # Origem.
                source_item_id=item.id,
                # Trecho citado (vazio vira texto vazio: a coluna é obrigatória).
                source_quote=c.quote,
                # Resultado da conferência.
                quote_verified=ok,
                # Confiança.
                confidence=c.confidence,
                # Quem disse.
                said_by_id=said_by,
                # Como apareceu.
                said_by_text=said_by_text,
            )
        )
        # Conta.
        saved += 1
        # Conta os conferidos.
        verified += 1 if ok else 0
    # Registro.
    _mark(
        session,
        item,
        "ok",
        model=result.model,
        found=len(candidates),
        verified=verified,
        truncated=truncated,
        note=None,
    )
    # Devolve.
    return "lido", saved, verified


# Fatos seguem o projeto do item: se a classificação mudou, o projeto do fato muda junto.
def realign_projects(session: Session) -> int:
    """Uma consulta só, sem custo. Devolve quantos fatos mudaram de projeto."""
    # UPDATE facts SET project_id = (classificação) WHERE diferente.
    result = session.execute(
        update(Fact)
        .where(
            Fact.source_item_id == ItemClassification.bronze_item_id,
            Fact.project_id.is_distinct_from(ItemClassification.project_id),
        )
        .values(project_id=ItemClassification.project_id)
        # "fetch" não é preciso: nenhum objeto Fact está carregado nesta sessão.
        .execution_options(synchronize_session=False)
    )
    # Quantos.
    return result.rowcount or 0


# Uma rodada: realinha os projetos e lê os itens pendentes, até o limite da rodada.
def run_pending(
    sessions: sessionmaker[Session], settings: Settings, llm: LLMClient | None
) -> ExtractSummary:
    # Resumo.
    summary = ExtractSummary()
    # Chamadas que ainda cabem nesta rodada.
    calls_left = settings.facts_llm_per_run
    # Uma sessão para a rodada.
    with sessions() as session:
        # Projeto dos fatos igual ao do item.
        realign_projects(session)
        # Grava.
        session.commit()
        # Cada item pronto.
        for item_id in pending_ids(session):
            # O item.
            item = session.get(BronzeItem, item_id)
            # Claude disponível só enquanto houver chamadas na rodada e o teto não bateu.
            available = llm if calls_left > 0 and not summary.budget_hit else None
            # Tenta.
            try:
                # Extrai.
                status, saved, verified = extract_item(session, item, settings, available)
            # Teto atingido no meio: o resto espera.
            except BudgetExceededError:
                # Marca.
                summary.budget_hit = True
                # Desfaz o que estava pela metade neste item.
                session.rollback()
                # Espera.
                status, saved, verified = "esperando", 0, 0
            # Erro da API (rede, chave): o item espera a próxima rodada.
            except anthropic.APIError:
                # Registra.
                log.exception("Extração de fatos do item %s falhou no Claude", item_id)
                # Desfaz o que estava pela metade.
                session.rollback()
                # Espera.
                status, saved, verified = "esperando", 0, 0
            # Chamou o Claude: gasta uma chamada da rodada.
            if status == "lido":
                # Conta.
                calls_left -= 1
                # Item lido.
                summary.items += 1
            # Registrado sem chamada.
            elif status == "pulado":
                summary.skipped += 1
            # Esperando.
            else:
                summary.waiting += 1
            # Fatos.
            summary.facts += saved
            # Conferidos.
            summary.verified += verified
            # Grava item a item: uma falha depois não desfaz o que já foi lido.
            session.commit()
    # Devolve.
    return summary


# Conveniência para o agendador: monta tudo a partir da configuração.
def run_from_settings(settings: Settings) -> ExtractSummary | None:
    """None quando não há nada pendente (para não encher o log a cada 5 minutos)."""
    # Import local: o banco só é carregado quando a rodada roda de verdade.
    from app.db import get_sessionmaker

    # Fábrica de sessões.
    sessions = get_sessionmaker()
    # Confere se há algo pendente e se o teto permite o Claude.
    with sessions() as session:
        # Fatos fora do projeto do item (classificação mudou): realinha mesmo sem pendente.
        moved = realign_projects(session)
        # Grava.
        session.commit()
        # Nada pendente.
        if not pending_ids(session, 1):
            # Só registra no log quando algo mudou de projeto.
            if moved:
                log.info("Fatos: %s fato(s) seguiram o novo projeto do item.", moved)
            # Nada a ler.
            return None
        # Teto atingido: nada é lido.
        over = month_spend_usd(session) >= settings.llm_monthly_budget_usd
    # Cliente do Claude (sem chave ou com teto: os itens esperam).
    llm = LLMClient(sessions, settings) if settings.anthropic_api_key and not over else None
    # Roda.
    summary = run_pending(sessions, settings, llm)
    # Marca o teto no resumo.
    summary.budget_hit = summary.budget_hit or over
    # Devolve.
    return summary


# Apaga os fatos e os registros, para a próxima rodada ler de novo (extrair-fatos --refazer).
def reset(session: Session, company_id: int | None = None) -> int:
    """Devolve quantos itens voltaram para a fila."""
    # Itens da Empresa (ou de todas).
    items = select(BronzeItem.id)
    # Filtro.
    if company_id is not None:
        items = items.where(BronzeItem.company_id == company_id)
    # Fatos.
    session.execute(delete(Fact).where(Fact.source_item_id.in_(items)))
    # Registros.
    result = session.execute(delete(FactExtraction).where(FactExtraction.bronze_item_id.in_(items)))
    # Quantos.
    return result.rowcount or 0


# Um item volta para a fila (botão "Extrair de novo" no console).
def redo_item(session: Session, item: BronzeItem, actor: str) -> None:
    # Fatos do item.
    session.execute(delete(Fact).where(Fact.source_item_id == item.id))
    # Registro do item.
    session.execute(delete(FactExtraction).where(FactExtraction.bronze_item_id == item.id))
    # Diário.
    audit(session, actor, "fatos.refazer", target=str(item.id), company_id=item.company_id)


# Fatos de um item e o registro da extração, para a página do item.
def of_item(session: Session, item_id: int) -> tuple[list[tuple], FactExtraction | None]:
    """Linhas (fato, responsável ou None, quem disse ou None)."""
    # Responsável e quem disse são a mesma tabela: dois apelidos.
    owner = aliased(Stakeholder)
    # Quem disse.
    speaker = aliased(Stakeholder)
    # Fatos com as duas pessoas.
    rows = session.execute(
        select(Fact, owner, speaker)
        .outerjoin(owner, owner.id == Fact.owner_id)
        .outerjoin(speaker, speaker.id == Fact.said_by_id)
        .where(Fact.source_item_id == item_id, Fact.superseded_by_id.is_(None))
        .order_by(Fact.kind, Fact.id)
    ).all()
    # Junto com o registro.
    return list(rows), session.get(FactExtraction, item_id)


# Fatos para a página Fatos, com filtros.
def list_facts(
    session: Session,
    company_id: int | None,
    project_id: int | None = None,
    kind: FactKind | None = None,
    only_suspicious: bool = False,
    limit: int = 200,
) -> list[tuple]:
    """Linhas (fato, item, projeto, responsável ou None, quem disse ou None)."""
    # Dois apelidos de Pessoa.
    owner = aliased(Stakeholder)
    # Quem disse.
    speaker = aliased(Stakeholder)
    # Consulta.
    query = (
        select(Fact, BronzeItem, Project, owner, speaker)
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        .outerjoin(Project, Project.id == Fact.project_id)
        .outerjoin(owner, owner.id == Fact.owner_id)
        .outerjoin(speaker, speaker.id == Fact.said_by_id)
        .where(Fact.superseded_by_id.is_(None))
        # Mais recentes primeiro (o que aconteceu agora é o que mais interessa).
        .order_by(BronzeItem.occurred_at.desc().nulls_last(), Fact.id)
        .limit(limit)
    )
    # Uma Empresa.
    if company_id is not None:
        query = query.where(BronzeItem.company_id == company_id)
    # Um projeto.
    if project_id is not None:
        query = query.where(Fact.project_id == project_id)
    # Um tipo.
    if kind is not None:
        query = query.where(Fact.kind == kind)
    # Só os suspeitos.
    if only_suspicious:
        query = query.where(Fact.quote_verified.is_(False))
    # Executa.
    return list(session.execute(query).all())


# Números da página Fatos.
def overview(session: Session, company_id: int | None, now: datetime | None = None) -> dict:
    # Filtro de Empresa (vazio = todas).
    of_company = [BronzeItem.company_id == company_id] if company_id is not None else []
    # Fatos valendo, por tipo.
    by_kind = dict(
        session.execute(
            select(Fact.kind, func.count())
            .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
            .where(Fact.superseded_by_id.is_(None), *of_company)
            .group_by(Fact.kind)
        ).all()
    )
    # Suspeitos (trecho não achado).
    suspicious = session.scalar(
        select(func.count())
        .select_from(Fact)
        .join(BronzeItem, BronzeItem.id == Fact.source_item_id)
        .where(Fact.superseded_by_id.is_(None), Fact.quote_verified.is_(False), *of_company)
    )
    # Itens lidos.
    read = session.scalar(
        select(func.count())
        .select_from(FactExtraction)
        .join(BronzeItem, BronzeItem.id == FactExtraction.bronze_item_id)
        .where(*of_company)
    )
    # Itens esperando (com projeto e ainda não lidos).
    waiting = session.scalar(
        select(func.count())
        .select_from(BronzeItem)
        .join(ItemClassification, ItemClassification.bronze_item_id == BronzeItem.id)
        .where(
            BronzeItem.source.in_(SOURCES),
            ItemClassification.project_id.is_not(None),
            ItemClassification.needs_review.is_(False),
            ~exists().where(FactExtraction.bronze_item_id == BronzeItem.id),
            *of_company,
        )
    )
    # Recusas e respostas ilegíveis (para a dona saber que existem).
    problems = session.scalar(
        select(func.count())
        .select_from(FactExtraction)
        .join(BronzeItem, BronzeItem.id == FactExtraction.bronze_item_id)
        .where(FactExtraction.status.in_(("recusa", "ilegivel")), *of_company)
    )
    # Início do mês (para o gasto).
    now = now or datetime.now(UTC)
    # Dia 1, meia-noite.
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Gasto do mês com a extração (todas as Empresas: o teto é um só).
    cost = session.scalar(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
            and_(LLMCall.purpose == PURPOSE, LLMCall.created_at >= month_start)
        )
    )
    # Total de fatos.
    total = sum(by_kind.values())
    # Devolve.
    return {
        # Por tipo, na ordem da lista de tipos.
        "por_tipo": [(k, KIND_LABELS[k], by_kind.get(k, 0)) for k in KIND_LABELS],
        # Total.
        "total": total,
        # Suspeitos.
        "suspeitos": suspicious or 0,
        # % com trecho conferido.
        "conferidos_pct": round(100 * (total - (suspicious or 0)) / total) if total else None,
        # Itens lidos.
        "lidos": read or 0,
        # Esperando.
        "esperando": waiting or 0,
        # Recusas e ilegíveis.
        "problemas": problems or 0,
        # Custo do mês.
        "custo_mes": Decimal(cost or 0),
    }
