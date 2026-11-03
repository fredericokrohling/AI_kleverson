"""Página Processamento: números da coleta, diário de ações e o conteúdo coletado.

Por que existe: sem esta tela, conferir se um anexo foi bem lido exigia consultar o
banco pelo terminal. Aqui ficam, de cima para baixo:

1. quatro números de resumo (e-mails no bronze, anexos, versões do Drive, transcrições);
2. a tabela Coleta de e-mail (as últimas rodadas do Gmail);
3. o diário de ações do dia (seção Processamento, app/activity.py);
4. o Conteúdo coletado: o texto exatamente como a IA vai ler, um dia por vez.

O Conteúdo coletado mostra o dia escolhido (hoje, por padrão) numa tabela com rolagem de
10 linhas, setas para o dia anterior e o seguinte e um calendário para escolher a data.
Trocar a fonte, a tag ou a data já troca a tabela (HTMX); "Procurar" junta a palavra a
esses filtros. Com a data apagada, a busca vale para todas as datas (os 200 mais recentes).
Linha com erro de processamento (anexo ou arquivo sem texto) fica roxa.

O endereço continua /conteudo, para não quebrar os links /conteudo/<id> já existentes.
"""

# Data, data e hora, hora zero e intervalo de tempo.
from datetime import date, datetime, time, timedelta

# Annotated junta um tipo com informação extra (aqui, parâmetros da URL).
from typing import Annotated

# Fusos horários pelo nome.
from zoneinfo import ZoneInfo

# APIRouter agrupa rotas; HTTPException devolve erro; Query lê parâmetros; Request é o pedido.
from fastapi import APIRouter, Form, HTTPException, Query, Request

# Resposta em HTML.
from fastapi.responses import HTMLResponse, RedirectResponse

# Consultas; or_ junta condições com "ou".
from sqlalchemy import func, or_, select

# Seção Processamento (diário de ações por dia).
from app.activity import activity_page, parse_day

# Empresas: filtro da página e correção da Empresa de uma transcrição.
from app.companies import filter_id, move_item, parse_company

# Configuração (fuso do console).
from app.config import get_settings

# Resumo da coleta de e-mail.
from app.ingestion.gmail import collection_overview

# Tabelas.
from app.models import (
    Attachment,
    BronzeItem,
    Company,
    DriveFolder,
    DriveItem,
    LiveMeeting,
    Project,
    ProjectStakeholder,
    SourceKind,
    Stakeholder,
    Utterance,
)

# Tags de projeto: as de cada item, colocar e tirar à mão.
from app.project_tags import (
    RULE_LABELS,
    add_manual,
    items_with_tag,
    remove_manual,
    tag_names,
    tags_of,
)

# Acha ou cria uma tag pelo nome; lista as tags de uma Empresa.
from app.tags import find_or_create_tag, list_tags

# Transmissão ao Início (avisa que uma reunião mudou).
from app.transcription import live

# Peças comuns: dono, banco e modelos HTML.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Quantos itens a lista mostra quando a busca vale para todas as datas.
_LIMIT = 200

# Nome legível de cada fonte.
SOURCE_LABELS = {
    SourceKind.GMAIL: "E-mail",
    SourceKind.DRIVE: "Drive",
    SourceKind.MEET_TRANSCRIPT: "Reunião",
    SourceKind.CALENDAR: "Agenda",
    SourceKind.GOOGLE_CHAT: "Chat",
    SourceKind.MANUAL: "Manual",
}


# Os quatro números do topo da página, de uma Empresa (ou de todas, com None).
def _summary(db, company_id: int | None = None) -> dict:
    # Totais do bronze (e-mails, anexos, anexos sem texto) e as últimas rodadas da coleta.
    coleta = collection_overview(db, company_id=company_id)
    # Filtros de Empresa de cada tabela (vazios quando são todas).
    of_folder = [DriveFolder.company_id == company_id] if company_id is not None else []
    of_drive = [DriveItem.company_id == company_id] if company_id is not None else []
    of_item = [BronzeItem.company_id == company_id] if company_id is not None else []
    # Monta o resumo.
    return {
        # Rodadas e totais da coleta de e-mail.
        "coleta": coleta,
        # Pastas do Drive monitoradas e ligadas.
        "folders": db.scalar(
            # Conta as pastas...
            select(func.count())
            # ... da tabela de pastas monitoradas...
            .select_from(DriveFolder)
            # ... que estão ligadas.
            .where(DriveFolder.active.is_(True), *of_folder)
        ),
        # Arquivos (não pastas) ainda presentes nas pastas monitoradas.
        "files": db.scalar(
            # Conta os itens...
            select(func.count())
            # ... do espelho do Drive...
            .select_from(DriveItem)
            # ... que são arquivos, não saíram da pasta e estão numa pasta monitorada.
            .where(
                DriveItem.is_folder.is_(False),
                DriveItem.removed.is_(False),
                DriveItem.folder_id.is_not(None),
                *of_drive,
            )
        ),
        # Arquivos que chegaram por link num e-mail (fora das pastas).
        "linked": db.scalar(
            # Conta os itens...
            select(func.count())
            # ... do espelho do Drive...
            .select_from(DriveItem)
            # ... que são arquivos e não estão em pasta monitorada.
            .where(DriveItem.is_folder.is_(False), DriveItem.folder_id.is_(None), *of_drive)
        ),
        # Versões de arquivos do Drive gravadas no bronze.
        "versions": db.scalar(
            # Conta as linhas...
            select(func.count())
            # ... do bronze...
            .select_from(BronzeItem)
            # ... que vieram do Drive.
            .where(BronzeItem.source == SourceKind.DRIVE, *of_item)
        ),
        # Transcrições de reunião (importadas e da extensão).
        "transcripts": db.scalar(
            # Conta as linhas...
            select(func.count())
            # ... do bronze...
            .select_from(BronzeItem)
            # ... de transcrição de reunião.
            .where(BronzeItem.source == SourceKind.MEET_TRANSCRIPT, *of_item)
        ),
    }


# Hoje, no fuso do console.
def _today() -> date:
    # Data atual no fuso configurado.
    return datetime.now(ZoneInfo(get_settings().console_timezone)).date()


# Lê o dia do Conteúdo coletado. None = parâmetro ausente (hoje); "" = todas as datas.
def _content_day(value: str | None) -> date | None:
    """Devolve o dia escolhido, ou None para "todas as datas"."""
    # Sem o parâmetro: hoje.
    if value is None:
        return _today()
    # Campo apagado: todas as datas.
    if not value.strip():
        return None
    # Tenta ler "AAAA-MM-DD"; inválido volta para hoje.
    try:
        # Data do texto.
        return date.fromisoformat(value.strip())
    # Formato inválido.
    except ValueError:
        return _today()


# Motivo do erro de processamento de cada item da lista (sem erro, o item não aparece).
def _error_reasons(db, items: list[BronzeItem]) -> dict[int, str]:
    """Anexo sem texto (e não dispensado) ou arquivo do Drive sem texto."""
    # Resultado: id do item → motivo.
    reasons: dict[int, str] = {}
    # Arquivos do Drive: o erro da extração fica no próprio item.
    for item in items:
        # Erro gravado na extração da versão.
        if item.extra.get("erro_extracao"):
            # Guarda o motivo.
            reasons[item.id] = str(item.extra["erro_extracao"])
    # Ids da lista.
    ids = [i.id for i in items]
    # Sem itens, sem consulta.
    if not ids:
        return reasons
    # Anexos com erro dos e-mails da lista (os dispensados já foram aceitos por você).
    rows = db.execute(
        # Item, nome do arquivo e erro.
        select(Attachment.bronze_item_id, Attachment.filename, Attachment.extraction_error)
        # Só os da lista, com erro e não dispensados.
        .where(
            Attachment.bronze_item_id.in_(ids),
            Attachment.extraction_error.is_not(None),
            Attachment.dismissed_at.is_(None),
        )
        # Ordem estável.
        .order_by(Attachment.id)
    ).all()
    # Cada anexo com erro.
    for item_id, filename, error in rows:
        # Guarda o primeiro motivo de cada e-mail.
        reasons.setdefault(item_id, f"{filename}: {error}")
    # Devolve.
    return reasons


# Página Processamento (no endereço /conteudo). Com o cabeçalho HX-Target do HTMX pedindo
# "conteudo-lista", devolve só a tabela do Conteúdo coletado.
@router.get("/conteudo", response_class=HTMLResponse)
def content_list(
    request: Request,
    owner: Owner,
    db: DB,
    # Palavra procurada (vazia = tudo).
    q: Annotated[str, Query(max_length=200)] = "",
    # Fonte ("gmail", "drive"...); vazia = todas.
    fonte: Annotated[str, Query(max_length=50)] = "",
    # Id da tag (vazio = todas).
    tag: Annotated[str, Query(max_length=20)] = "",
    # Dia do Conteúdo coletado (ausente = hoje; vazio = todas as datas).
    dia: Annotated[str | None, Query(max_length=20)] = None,
    # Dia e página da seção Processamento (diário de ações), quando o navegador não usa HTMX.
    data: str | None = None,
    # Página do diário.
    pagina: int | None = None,
    # Empresa mostrada (vazia = todas).
    empresa: Annotated[str | None, Query(max_length=20)] = None,
):
    # Fuso do console.
    timezone = get_settings().console_timezone
    # Empresa do filtro (None = TODOS).
    company_id = filter_id(empresa)
    # Dia escolhido (None = todas as datas).
    day = _content_day(dia)
    # Hoje.
    today = _today()
    # Um dia no futuro não tem conteúdo: volta para hoje.
    if day is not None and day > today:
        day = today
    # Consulta base: mais recentes primeiro.
    query = select(BronzeItem).order_by(BronzeItem.occurred_at.desc().nulls_last())
    # Um dia escolhido: da meia-noite à meia-noite seguinte, no fuso do console.
    if day is not None:
        # Meia-noite do dia.
        start = datetime.combine(day, time.min, tzinfo=ZoneInfo(timezone))
        # Só os itens do dia (o dia inteiro cabe na rolagem).
        query = query.where(
            BronzeItem.occurred_at >= start, BronzeItem.occurred_at < start + timedelta(days=1)
        )
    # Todas as datas: só os mais recentes.
    else:
        query = query.limit(_LIMIT)
    # Só a Empresa escolhida.
    if company_id is not None:
        query = query.where(BronzeItem.company_id == company_id)
    # Filtro de fonte, se for uma fonte conhecida.
    if fonte in {s.value for s in SourceKind}:
        # Só essa fonte.
        query = query.where(BronzeItem.source == SourceKind(fonte))
    # Busca por palavra no assunto ou no texto (sem diferenciar maiúsculas).
    if q.strip():
        # Padrão "contém" do SQL.
        pattern = f"%{q.strip()}%"
        # Assunto ou corpo.
        query = query.where(
            or_(BronzeItem.subject.ilike(pattern), BronzeItem.body_text.ilike(pattern))
        )
    # Filtro de tag (o projeto): e-mails, transcrições e versões do Drive com a tag.
    if tag.isdigit():
        # Só os itens com a tag.
        query = query.where(BronzeItem.id.in_(items_with_tag(int(tag))))
    # Executa.
    items = db.scalars(query).all()
    # Dados da tabela do Conteúdo coletado.
    content = {
        # Itens.
        "items": items,
        # Palavra procurada.
        "q": q,
        # Fonte escolhida.
        "fonte": fonte,
        # Tag escolhida.
        "tag": tag,
        # Dia mostrado (None = todas as datas).
        "dia": day,
        # Hoje (limite do calendário e do botão "Voltar para hoje").
        "hoje": today,
        # Dia anterior, para a seta da esquerda (sem data escolhida, não há seta).
        "ontem": day - timedelta(days=1) if day else None,
        # Dia seguinte, para a seta da direita (só até hoje).
        "amanha": day + timedelta(days=1) if day and day < today else None,
        # Limite quando a busca vale para todas as datas.
        "limit": _LIMIT,
        # Nome de cada fonte.
        "labels": SOURCE_LABELS,
        # Tags para o filtro (só as da Empresa escolhida, ou todas).
        "tags": [t for t, _ in list_tags(db, company_id)],
        # Empresa do filtro (None = todas).
        "empresa": company_id,
        # Tags de cada item da lista.
        "item_tags": tag_names(db, [i.id for i in items]),
        # Motivo do erro de processamento de cada item com erro (linha roxa).
        "erros": _error_reasons(db, items),
    }
    # Pedido do HTMX só para a tabela: devolve só ela.
    if request.headers.get("HX-Target") == "conteudo-lista":
        return templates.TemplateResponse(request, "_conteudo_lista.html", content)
    # Página inteira.
    return templates.TemplateResponse(
        request,
        "conteudo.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "processamento",
            # Os quatro números e a coleta de e-mail.
            "resumo": _summary(db, company_id),
            # Diário de ações do dia (hoje, por padrão).
            "p": activity_page(db, timezone, parse_day(data), pagina, company_id=company_id),
            # A tabela do Conteúdo coletado.
            **content,
        },
    )


# Um item: o texto completo e os arquivos ligados a ele.
@router.get("/conteudo/{item_id}", response_class=HTMLResponse)
def content_item(request: Request, owner: Owner, db: DB, item_id: int):
    # O item.
    item = db.get(BronzeItem, item_id)
    # Não existe.
    if item is None:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Arquivos do e-mail: anexos, links do Drive e links externos, na ordem de gravação.
    attachments = db.scalars(
        select(Attachment).where(Attachment.bronze_item_id == item.id).order_by(Attachment.id)
    ).all()
    # Arquivos do Drive citados por link: id do Drive → item (para mostrar nome e versão lida).
    linked_ids = [a.external_ref for a in attachments if a.origin == "link_drive"]
    # Busca os itens desses ids.
    linked = (
        {
            d.drive_id: d
            for d in db.scalars(
                select(DriveItem).where(
                    DriveItem.company_id == item.company_id, DriveItem.drive_id.in_(linked_ids)
                )
            )
        }
        if linked_ids
        else {}
    )
    # Outras versões do mesmo arquivo do Drive (thread_id = id do arquivo), mais novas primeiro.
    versions = (
        db.scalars(
            select(BronzeItem)
            .where(
                BronzeItem.company_id == item.company_id,
                BronzeItem.source == SourceKind.DRIVE,
                BronzeItem.thread_id == item.thread_id,
            )
            .order_by(BronzeItem.occurred_at.desc().nulls_last(), BronzeItem.id.desc())
        ).all()
        if item.source == SourceKind.DRIVE
        else []
    )
    # Quem falou (só transcrições): nome, pessoa reconhecida e número de falas.
    speakers = (
        db.execute(
            # Nome como apareceu, pessoa e quantas falas.
            select(Utterance.speaker, Stakeholder, func.count(Utterance.id))
            # Pessoa reconhecida (pode não haver).
            .outerjoin(Stakeholder, Stakeholder.id == Utterance.stakeholder_id)
            # Desta transcrição.
            .where(Utterance.bronze_item_id == item.id)
            # Um grupo por nome e pessoa.
            .group_by(Utterance.speaker, Stakeholder.id)
            # Quem mais falou primeiro.
            .order_by(func.count(Utterance.id).desc())
        ).all()
        if item.source == SourceKind.MEET_TRANSCRIPT
        else []
    )
    # Projetos em que cada pessoa é liderança: id da pessoa → nomes (para a marca na tela).
    project_leaders: dict[int, list[str]] = {}
    # Cada vínculo de liderança, com o nome do projeto.
    for person_id, project_name in db.execute(
        # Pessoa e nome do projeto...
        select(ProjectStakeholder.stakeholder_id, Project.name)
        # ... ligados pelo projeto...
        .join(Project, Project.id == ProjectStakeholder.project_id)
        # ... só onde ela é liderança, nos projetos da Empresa do item...
        .where(ProjectStakeholder.is_leader.is_(True), Project.company_id == item.company_id)
        # ... em ordem de nome.
        .order_by(Project.name)
    ):
        # Acrescenta o projeto à lista da pessoa.
        project_leaders.setdefault(person_id, []).append(project_name)
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "conteudo_item.html",
        {
            "owner": owner,
            "menu": "processamento",
            "item": item,
            "label": SOURCE_LABELS.get(item.source, item.source),
            "attachments": attachments,
            "linked": linked,
            "versions": versions,
            # Aviso pendente (ex.: nome de tag inválido).
            "flash": request.session.pop("flash", None),
            # Tags do item (e-mails e transcrições: com a regra; Drive: das pastas).
            "tags": tags_of(db, [item.id]).get(item.id, []),
            # Tags do Drive (vêm das pastas; mudam em Configurações).
            "drive_tags": tag_names(db, [item.id]).get(item.id, [])
            if item.source == SourceKind.DRIVE
            else [],
            # Nome de cada regra.
            "rule_labels": RULE_LABELS,
            # Quem falou.
            "speakers": speakers,
            # Lideranças de projeto.
            "project_leaders": project_leaders,
            # Empresa do item.
            "company": db.get(Company, item.company_id),
            # Reunião ao vivo que gerou a transcrição (a correção a move junto).
            "meeting": db.scalars(
                select(LiveMeeting).where(LiveMeeting.bronze_item_id == item.id)
            ).first(),
        },
    )


# Coloca uma tag à mão num e-mail ou transcrição.
@router.post("/conteudo/{item_id}/tags", dependencies=[SameOrigin])
def add_tag(request: Request, owner: Owner, db: DB, item_id: int, nome: Annotated[str, Form()]):
    # O item.
    item = db.get(BronzeItem, item_id)
    # Não existe, ou é do Drive (as tags do Drive vêm das pastas).
    if item is None or item.source == SourceKind.DRIVE:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Tenta.
    try:
        # Acha ou cria a tag e coloca.
        add_manual(db, item.id, find_or_create_tag(db, item.company_id, nome))
        # Grava.
        db.commit()
    # Nome inválido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso.
        request.session["flash"] = {"texto": str(exc), "erro": True}
    # Volta para o item.
    return RedirectResponse(f"/conteudo/{item.id}", status_code=303)


# Tira uma tag à mão: as regras não a colocam de novo neste item.
@router.post("/conteudo/{item_id}/tags/{tag_id}/remover", dependencies=[SameOrigin])
def drop_tag(request: Request, owner: Owner, db: DB, item_id: int, tag_id: int):
    # O item.
    item = db.get(BronzeItem, item_id)
    # Não existe, ou é do Drive.
    if item is None or item.source == SourceKind.DRIVE:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Tira (marca como tirada).
    remove_manual(db, item.id, tag_id)
    # Grava.
    db.commit()
    # Volta para o item.
    return RedirectResponse(f"/conteudo/{item.id}", status_code=303)


# Corrige a Empresa de uma transcrição (atribuída errado na importação ou na reunião).
# Refaz as camadas que dependem da Empresa: tags, classificação, fatos e pessoas das falas.
@router.post("/conteudo/{item_id}/empresa", dependencies=[SameOrigin])
def change_company(
    request: Request, owner: Owner, db: DB, item_id: int, empresa: Annotated[str, Form()] = ""
):
    # O item.
    item = db.get(BronzeItem, item_id)
    # Não existe.
    if item is None:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Tenta.
    try:
        # Empresa nova.
        company = parse_company(db, empresa)
        # Já é esta: nada a fazer.
        if company.id == item.company_id:
            raise ValueError(f"A transcrição já é da Empresa {company.name}.")
        # Move e refaz as camadas.
        added = move_item(db, item, company, owner)
        # Grava (as regras do banco conferem que nada ficou ligado à Empresa antiga).
        db.commit()
    # Sem Empresa, não é transcrição ou já existe igual na outra.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso.
        request.session["flash"] = {"texto": str(exc), "erro": True}
        # Volta.
        return RedirectResponse(f"/conteudo/{item.id}", status_code=303)
    # O Início redesenha as reuniões (a área da reunião mostra a Empresa nova).
    live.hub.publish({"tipo": "reunioes"})
    # Aviso.
    request.session["flash"] = {
        "texto": f"Transcrição movida para {company.name}. Tags refeitas pelas regras de lá "
        f"({added} tag(s)); fatos e classificação serão extraídos de novo; as falas foram "
        "ligadas às Pessoas dessa Empresa.",
        "erro": False,
    }
    # Volta para o item.
    return RedirectResponse(f"/conteudo/{item.id}", status_code=303)
