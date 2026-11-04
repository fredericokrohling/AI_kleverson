"""Pôr prazos na agenda do Google, com marca invisível, e tirar de lá pelo console.

Segunda peça do bloco 5 (ações). Até aqui o sistema só lia a agenda. Agora ele escreve,
e por isso este módulo segue quatro regras, cada uma com um teste:

1. **Só a pedido.** Nada é criado sozinho: o evento nasce do botão "Pôr na agenda" (em
   Fatos, Briefing e Alertas). O coletor não cria eventos.
2. **Sem convidados.** O evento não leva "attendees" e é criado com sendUpdates="none".
   Um convite é uma mensagem enviada em seu nome para outra pessoa, e o projeto decidiu
   que o assistente não manda mensagem a ninguém (a mesma regra do gmail.send).
3. **Marca invisível.** Todo evento leva a propriedade privada "kleverson=1" (só a dona
   da agenda vê; não aparece no evento nem para convidados). É por ela que "Apagar todos"
   acha os eventos do assistente, mesmo que o registro no banco se perca.
4. **Só apaga o que é seu.** Antes de apagar, o código lê o evento e confere a marca.
   Evento sem a marca nunca é apagado, mesmo que o id venha de um registro do banco.

Que fatos podem ir para a agenda: os que têm prazo, trecho conferido, estão valendo e
não foram concluídos. É a mesma régua dos alertas e do briefing: um evento errado na
agenda é pior que nenhum.
"""

# Callable: o tipo "função" (a fábrica de clientes da agenda).
from collections.abc import Callable

# dataclass cria classes simples de dados (o resumo de "Apagar todos").
from dataclasses import dataclass

# Data, data e hora, fuso UTC e intervalo de tempo.
from datetime import UTC, date, datetime, timedelta

# Erro devolvido pelas APIs do Google.
from googleapiclient.errors import HttpError

# Consultas ao banco.
from sqlalchemy import func, select

# Erro do banco quando uma regra de "não pode repetir" é quebrada.
from sqlalchemy.exc import IntegrityError

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Fato concluído (fato fechado não vai para a agenda).
from app.alerts import DONE

# Configuração.
from app.config import Settings

# Nomes legíveis dos tipos de fato ("Prazo", "Tarefa").
from app.facts import KIND_LABELS

# Tabelas e o diário.
from app.models import BronzeItem, CreatedEvent, Fact, Project, Stakeholder, audit

# Cliente da agenda da conta de uma Empresa.
from app.transcription.calendar import calendar_client

# A marca invisível: nome e valor da propriedade privada de todo evento do assistente.
MARK_KEY = "kleverson"
# Valor da marca.
MARK_VALUE = "1"
# Propriedade privada com o id do registro no banco (liga o evento à linha da tabela).
ROW_KEY = "kleverson_registro"
# Propriedade privada com o id do fato (ajuda a achar a origem pelo próprio evento).
FACT_KEY = "kleverson_fato"
# Tamanho máximo do título no evento (a agenda aceita mais, mas a tela corta antes).
TITLE_MAX = 200
# Tamanho máximo do trecho citado na descrição.
QUOTE_MAX = 500

# Tipo da fábrica de clientes da agenda: recebe o id da Empresa e devolve o cliente.
CalendarFactory = Callable[[int], object]


# Erro com uma frase para mostrar no console ("Este fato já está na agenda").
class EventError(Exception):
    # Nada a acrescentar além do nome: a mensagem é o texto do aviso.
    pass


# Resumo de "Apagar todos" e de "Conferir com a agenda".
@dataclass
class AgendaSummary:
    # Eventos apagados da agenda agora.
    removed: int = 0
    # Registros cujo evento já não estava na agenda (você apagou pelo Google).
    gone: int = 0
    # Eventos com a marca que estão na agenda e continuam lá.
    present: int = 0
    # Eventos com a marca sem registro no banco (ex.: banco restaurado de um backup).
    orphans: int = 0

    # Frase para o aviso do console e para o terminal.
    def describe(self) -> str:
        # Partes, só as que aconteceram.
        parts = []
        # Apagados.
        if self.removed:
            parts.append(f"{self.removed} evento(s) apagado(s) da agenda")
        # Já tinham sumido.
        if self.gone:
            parts.append(f"{self.gone} já tinha(m) sido apagado(s) pelo Google")
        # Continuam.
        if self.present:
            parts.append(f"{self.present} continua(m) na agenda")
        # Sem registro.
        if self.orphans:
            parts.append(f"{self.orphans} com a marca, mas sem registro no console")
        # Junta, ou diz que não havia nada.
        return ("; ".join(parts) + ".") if parts else "Nenhum evento do assistente na agenda."


# A fábrica padrão: o cliente da agenda da conta da Empresa (os testes passam um falso).
def _default_factory(session: Session, settings: Settings) -> CalendarFactory:
    # Uma função que recebe o id da Empresa.
    return lambda company_id: calendar_client(session, settings, company_id)


# Frase de erro do Google para o aviso do console.
def google_error(exc: Exception) -> str:
    # Sem conta Google conectada na Empresa (load_credentials explica o que fazer).
    if isinstance(exc, RuntimeError):
        return str(exc)
    # 403: a permissão de agenda foi tirada, ou a API está desligada no Google Cloud.
    if isinstance(exc, HttpError) and exc.resp.status == 403:
        return "A agenda recusou o pedido (HTTP 403): confira a permissão de agenda da conta."
    # Outro erro da API.
    if isinstance(exc, HttpError):
        return f"A agenda recusou o pedido (HTTP {exc.resp.status})."
    # Outros (rede fora, acesso revogado): o nome do erro ajuda a achar.
    return f"Não foi possível falar com a agenda ({type(exc).__name__})."


# True quando o evento tem a marca do assistente.
def is_ours(raw: dict) -> bool:
    # Propriedades privadas do evento (podem não existir).
    private = (raw.get("extendedProperties") or {}).get("private") or {}
    # A marca precisa estar lá, com o valor certo.
    return private.get(MARK_KEY) == MARK_VALUE


# Por que um fato não pode ir para a agenda; None quando pode.
def why_not(fact: Fact | None) -> str | None:
    # Não existe (apagado por "Extrair de novo", ou id inventado).
    if fact is None:
        return "Fato não encontrado (ele pode ter sido extraído de novo)."
    # Substituído por outro.
    if fact.superseded_by_id is not None:
        return "Este fato foi substituído por outro; use o mais novo."
    # Sem data, não há dia para pôr na agenda.
    if fact.due_date is None:
        return "Este fato não tem prazo."
    # Suspeito: o trecho não foi achado no texto original.
    if not fact.quote_verified:
        return "Fato suspeito (trecho não achado no texto): confira na origem antes."
    # Fechado.
    if fact.status == DONE:
        return "Este fato já foi concluído."
    # Pode.
    return None


# Título do evento: o tipo na frente, para ver na agenda que é um prazo ("Prazo: ...").
def event_title(fact: Fact) -> str:
    # Nome do tipo ("Tarefa", "Prazo", "Dependência").
    label = KIND_LABELS.get(fact.kind, str(fact.kind))
    # Junta e corta no limite.
    return f"{label}: {fact.title}"[:TITLE_MAX]


# Corpo do evento no formato da API do Google Calendar.
def event_body(
    fact: Fact,
    *,
    project: str | None,
    owner: str | None,
    settings: Settings,
    row_id: int,
) -> dict:
    """Evento de dia inteiro, sem convidados, livre na agenda, privado e com a marca."""
    # Trecho citado, cortado para a descrição não virar o texto inteiro do e-mail.
    quote = fact.source_quote[:QUOTE_MAX] + ("…" if len(fact.source_quote) > QUOTE_MAX else "")
    # Linhas da descrição.
    lines = [
        # O que é e de onde veio.
        f"{KIND_LABELS.get(fact.kind, str(fact.kind))} achado pelo Kleverson A.I."
        + (f" no projeto {project}." if project else "."),
        # Responsável, cadastrado ou como escrito no texto.
        f"Responsável: {owner or 'ninguém'}",
        # A prova no texto.
        f"Trecho: “{quote}”",
        # Link para a origem no console (só abre dentro da sua rede Tailscale).
        f"Origem: {settings.public_base_url.rstrip('/')}/conteudo/{fact.source_item_id}",
        # Linha em branco.
        "",
        # Como tirar.
        "Criado pelo assistente a seu pedido. Para tirar, apague aqui ou na página "
        "Na agenda do console.",
    ]
    # Monta.
    return {
        # Título.
        "summary": event_title(fact),
        # Descrição.
        "description": "\n".join(lines),
        # Dia inteiro: "date" sem hora. O fim é exclusivo (o dia seguinte), como manda a API.
        "start": {"date": fact.due_date.isoformat()},
        "end": {"date": (fact.due_date + timedelta(days=1)).isoformat()},
        # "Livre": o prazo não bloqueia horário nem impede que marquem reunião com você.
        "transparency": "transparent",
        # Privado: quem vê a sua agenda (ex.: colegas na conta da empresa) vê só "ocupado".
        "visibility": "private",
        # Lembretes: os seus lembretes padrão de evento de dia inteiro.
        "reminders": {"useDefault": True},
        # A marca invisível e os ids que ligam o evento ao console.
        "extendedProperties": {
            "private": {MARK_KEY: MARK_VALUE, ROW_KEY: str(row_id), FACT_KEY: str(fact.id)}
        },
    }


# Eventos ativos (não apagados) dos fatos pedidos: fato → registro. Serve aos botões.
def active_by_fact(session: Session, fact_ids: list[int] | set[int]) -> dict[int, CreatedEvent]:
    # Sem fatos, sem consulta.
    if not fact_ids:
        return {}
    # Registros ativos.
    rows = session.scalars(
        select(CreatedEvent).where(
            CreatedEvent.fact_id.in_(list(fact_ids)), CreatedEvent.removed_at.is_(None)
        )
    )
    # Fato → registro.
    return {row.fact_id: row for row in rows}


# Cria o evento de um fato na agenda da Empresa dele.
def create_for_fact(
    session: Session,
    settings: Settings,
    fact_id: int,
    actor: str,
    *,
    calendar_for: CalendarFactory | None = None,
) -> CreatedEvent:
    """Devolve o registro criado. Levanta EventError com a frase para o console."""
    # O fato.
    fact = session.get(Fact, fact_id)
    # Pode ir para a agenda?
    reason = why_not(fact)
    # Não pode: diz por quê.
    if reason:
        raise EventError(reason)
    # Já tem um evento ativo.
    if active_by_fact(session, [fact.id]):
        raise EventError("Este fato já está na agenda (veja em Na agenda).")
    # Empresa: a do item de onde o fato saiu (é a conta Google dela que recebe o evento).
    company_id = session.scalar(
        select(BronzeItem.company_id).where(BronzeItem.id == fact.source_item_id)
    )
    # Projeto e responsável, para a descrição.
    project = session.get(Project, fact.project_id) if fact.project_id else None
    # Responsável cadastrado.
    person = session.get(Stakeholder, fact.owner_id) if fact.owner_id else None
    # Registro, antes de chamar o Google: o id dele vai na marca, e o índice único barra
    # um segundo clique antes de o segundo evento existir.
    row = CreatedEvent(
        company_id=company_id,
        fact_id=fact.id,
        title=event_title(fact),
        day=fact.due_date,
        created_by=actor,
    )
    # "Ponto de salvamento": se o Google falhar, só esta parte é desfeita.
    savepoint = session.begin_nested()
    # Prepara a gravação.
    session.add(row)
    # Tenta gravar (aqui o índice único é conferido).
    try:
        # Envia ao banco, ainda sem commit.
        session.flush()
    # Outro pedido criou o evento deste fato nesse meio-tempo (clique duplo).
    except IntegrityError as exc:
        # Desfaz o registro.
        savepoint.rollback()
        # Avisa.
        raise EventError("Este fato já está na agenda (veja em Na agenda).") from exc
    # Fábrica padrão, se nenhuma foi passada.
    calendar_for = calendar_for or _default_factory(session, settings)
    # Tenta criar no Google.
    try:
        # Cliente da agenda da conta da Empresa.
        calendar = calendar_for(company_id)
        # Corpo do evento.
        body = event_body(
            fact,
            project=project.name if project else None,
            owner=person.name if person else fact.owner_text,
            settings=settings,
            row_id=row.id,
        )
        # Cria na agenda principal; sendUpdates="none": ninguém recebe aviso.
        raw = (
            calendar.events()
            .insert(calendarId="primary", body=body, sendUpdates="none")
            .execute(num_retries=2)
        )
    # Sem conta, sem permissão, rede fora: nada fica gravado.
    except Exception as exc:
        # Desfaz o registro.
        savepoint.rollback()
        # Frase para o console.
        raise EventError(google_error(exc)) from exc
    # Guarda o id do evento.
    row.google_event_id = raw["id"]
    # Fecha o ponto de salvamento (o commit de verdade é de quem chamou).
    savepoint.commit()
    # Diário.
    audit(
        session,
        actor,
        "agenda.evento_criado",
        target=str(fact.id),
        company_id=company_id,
        evento=raw["id"],
        dia=fact.due_date.isoformat(),
        titulo=row.title,
    )
    # Devolve.
    return row


# Marca um registro como apagado.
def _mark_removed(row: CreatedEvent, actor: str, note: str | None, now: datetime) -> None:
    # Momento.
    row.removed_at = now
    # Quem.
    row.removed_by = actor
    # Observação.
    row.removed_note = note


# Tira da agenda o evento de um registro.
def remove(
    session: Session,
    settings: Settings,
    row: CreatedEvent,
    actor: str,
    *,
    calendar_for: CalendarFactory | None = None,
    now: datetime | None = None,
) -> str:
    """Devolve a frase do resultado. Levanta EventError se não deu para apagar."""
    # Já apagado.
    if row.removed_at is not None:
        raise EventError("Este evento já foi apagado.")
    # Momento.
    now = now or datetime.now(UTC)
    # Fábrica padrão.
    calendar_for = calendar_for or _default_factory(session, settings)
    # Tenta.
    try:
        # Cliente da agenda.
        calendar = calendar_for(row.company_id)
        # Lê o evento para conferir a marca (regra 4).
        raw = (
            calendar.events()
            .get(calendarId="primary", eventId=row.google_event_id)
            .execute(num_retries=2)
        )
    # 404/410: você já apagou pelo Google.
    except HttpError as exc:
        # Não existe mais.
        if exc.resp.status in (404, 410):
            # Só atualiza o registro.
            _mark_removed(row, actor, "já tinha sido apagado na agenda", now)
            # Diário.
            audit(
                session,
                actor,
                "agenda.evento_sumiu",
                target=str(row.id),
                company_id=row.company_id,
                evento=row.google_event_id,
            )
            # Frase.
            return "O evento já não estava na agenda; o registro foi atualizado."
        # Outro erro.
        raise EventError(google_error(exc)) from exc
    # Sem conta, rede fora.
    except Exception as exc:
        raise EventError(google_error(exc)) from exc
    # Cancelado no Google (fica na lixeira): conta como apagado.
    if raw.get("status") == "cancelled":
        # Só o registro.
        _mark_removed(row, actor, "já tinha sido apagado na agenda", now)
        # Frase.
        return "O evento já não estava na agenda; o registro foi atualizado."
    # Sem a marca: não é do assistente. Nunca apaga.
    if not is_ours(raw):
        raise EventError("O evento na agenda não tem a marca do assistente; nada foi apagado.")
    # Apaga, sem avisar ninguém.
    try:
        # Pedido de apagar.
        calendar.events().delete(
            calendarId="primary", eventId=row.google_event_id, sendUpdates="none"
        ).execute(num_retries=2)
    # Sumiu entre a leitura e o apagar: dá no mesmo.
    except HttpError as exc:
        # Outro erro sobe.
        if exc.resp.status not in (404, 410):
            raise EventError(google_error(exc)) from exc
    # Registro.
    _mark_removed(row, actor, None, now)
    # Diário.
    audit(
        session,
        actor,
        "agenda.evento_apagado",
        target=str(row.id),
        company_id=row.company_id,
        evento=row.google_event_id,
        titulo=row.title,
    )
    # Frase.
    return f"“{row.title}” saiu da agenda."


# Todos os eventos com a marca na agenda da Empresa (a busca é feita pelo próprio Google).
def marked_events(calendar) -> list[dict]:
    # Resultado.
    found: list[dict] = []
    # Marcador de página.
    token = None
    # Página por página.
    while True:
        # Pede uma página.
        page = (
            calendar.events()
            .list(
                # Agenda principal.
                calendarId="primary",
                # Filtro do Google: só eventos com esta propriedade privada.
                privateExtendedProperty=f"{MARK_KEY}={MARK_VALUE}",
                # Apagados ficam de fora.
                showDeleted=False,
                # Página grande.
                maxResults=250,
                # Página seguinte (None na primeira).
                pageToken=token,
            )
            .execute(num_retries=2)
        )
        # Só os que têm a marca de verdade (conferência dupla, além do filtro do Google).
        found.extend(e for e in page.get("items", []) if is_ours(e))
        # Próxima página.
        token = page.get("nextPageToken")
        # Acabou.
        if not token:
            return found


# Compara a agenda com os registros: atualiza o dia (se você moveu o evento) e marca
# como apagados os que sumiram. Com remove_all=True, também apaga todos da agenda.
def sync_company(
    session: Session,
    settings: Settings,
    company_id: int,
    actor: str,
    *,
    remove_all: bool = False,
    calendar_for: CalendarFactory | None = None,
    now: datetime | None = None,
) -> AgendaSummary:
    """Levanta EventError se a agenda não responder."""
    # Momento.
    now = now or datetime.now(UTC)
    # Fábrica padrão.
    calendar_for = calendar_for or _default_factory(session, settings)
    # Tenta ler a agenda.
    try:
        # Cliente.
        calendar = calendar_for(company_id)
        # Eventos com a marca.
        events = marked_events(calendar)
    # Sem conta, sem permissão, rede fora.
    except Exception as exc:
        raise EventError(google_error(exc)) from exc
    # Registros ativos da Empresa, pelo id do evento.
    active = {
        row.google_event_id: row
        for row in session.scalars(
            select(CreatedEvent).where(
                CreatedEvent.company_id == company_id, CreatedEvent.removed_at.is_(None)
            )
        )
    }
    # Resumo.
    summary = AgendaSummary()
    # Cada evento com a marca.
    for raw in events:
        # Registro dele (None = sem registro).
        row = active.pop(raw["id"], None)
        # Modo "apagar todos".
        if remove_all:
            # Apaga, sem avisar ninguém.
            try:
                # Pedido.
                calendar.events().delete(
                    calendarId="primary", eventId=raw["id"], sendUpdates="none"
                ).execute(num_retries=2)
            # Sumiu nesse meio-tempo: segue; outros erros param e avisam.
            except HttpError as exc:
                # Outro erro.
                if exc.resp.status not in (404, 410):
                    raise EventError(google_error(exc)) from exc
            # Conta.
            summary.removed += 1
            # Atualiza o registro, se houver.
            if row is not None:
                _mark_removed(row, actor, None, now)
            # Próximo.
            continue
        # Só conferir: o evento continua.
        summary.present += 1
        # Sem registro.
        if row is None:
            # Conta.
            summary.orphans += 1
            # Próximo.
            continue
        # Dia na agenda (você pode ter arrastado o evento para outro dia).
        start = (raw.get("start") or {}).get("date")
        # Mudou: acompanha.
        if start and start != row.day.isoformat():
            row.day = date.fromisoformat(start)
    # Os registros que sobraram não têm evento na agenda: você apagou pelo Google.
    for row in active.values():
        # Marca.
        _mark_removed(row, actor, "já tinha sido apagado na agenda", now)
        # Conta.
        summary.gone += 1
    # Diário.
    audit(
        session,
        actor,
        "agenda.apagar_todos" if remove_all else "agenda.conferir",
        company_id=company_id,
        apagados=summary.removed,
        sumiram=summary.gone,
        continuam=summary.present,
        sem_registro=summary.orphans,
    )
    # Devolve.
    return summary


# Lista da página Na agenda: (registro, fato ou None, projeto ou None).
def list_created(
    session: Session, company_id: int | None, *, include_removed: bool = False, limit: int = 200
) -> list[tuple]:
    # Consulta: o fato e o projeto podem não existir mais.
    query = (
        select(CreatedEvent, Fact, Project)
        .outerjoin(Fact, Fact.id == CreatedEvent.fact_id)
        .outerjoin(Project, Project.id == Fact.project_id)
        # Os ativos primeiro, e entre eles o dia mais próximo.
        .order_by(CreatedEvent.removed_at.is_not(None), CreatedEvent.day, CreatedEvent.id)
        .limit(limit)
    )
    # Uma Empresa.
    if company_id is not None:
        query = query.where(CreatedEvent.company_id == company_id)
    # Sem os apagados.
    if not include_removed:
        query = query.where(CreatedEvent.removed_at.is_(None))
    # Executa.
    return list(session.execute(query).all())


# Números da página Na agenda.
def overview(session: Session, company_id: int | None, today: date) -> dict:
    # Filtro de Empresa (vazio = todas).
    of_company = [CreatedEvent.company_id == company_id] if company_id is not None else []

    # Contagem com condições.
    def count(*where) -> int:
        # Quantos registros.
        return session.scalar(
            select(func.count()).select_from(CreatedEvent).where(*of_company, *where)
        )

    # Os três números.
    return {
        # Na agenda agora.
        "ativos": count(CreatedEvent.removed_at.is_(None)),
        # Na agenda, de hoje até 7 dias.
        "proximos": count(
            CreatedEvent.removed_at.is_(None),
            CreatedEvent.day >= today,
            CreatedEvent.day <= today + timedelta(days=7),
        ),
        # Já apagados.
        "apagados": count(CreatedEvent.removed_at.is_not(None)),
    }
