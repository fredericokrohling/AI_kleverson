"""Conteúdo coletado: ver o que entrou no banco (e-mails, arquivos do Drive, transcrições).

Por que existe: sem esta tela, conferir se um anexo foi bem lido exigia consultar o
banco pelo terminal. Aqui você vê o texto exatamente como a IA vai ler: o corpo do
e-mail, o texto extraído de cada anexo e o de cada versão de arquivo do Drive.

É uma versão simples da busca prevista para o bloco 4: procura por palavra no assunto e
no texto, mostra os 50 mais recentes.
"""

# Annotated junta um tipo com informação extra (aqui, parâmetros da URL).
from typing import Annotated

# APIRouter agrupa rotas; HTTPException devolve erro; Query lê parâmetros; Request é o pedido.
from fastapi import APIRouter, Form, HTTPException, Query, Request

# Resposta em HTML.
from fastapi.responses import HTMLResponse, RedirectResponse

# Consultas; or_ junta condições com "ou".
from sqlalchemy import func, or_, select

# Tabelas.
from app.models import (
    Attachment,
    BronzeItem,
    DriveItem,
    Project,
    ProjectStakeholder,
    SourceKind,
    Stakeholder,
    Tag,
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

# Acha ou cria uma tag pelo nome.
from app.tags import find_or_create_tag

# Peças comuns: dono, banco e modelos HTML.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Quantos itens a lista mostra.
_LIMIT = 50

# Nome legível de cada fonte.
SOURCE_LABELS = {
    SourceKind.GMAIL: "E-mail",
    SourceKind.DRIVE: "Drive",
    SourceKind.MEET_TRANSCRIPT: "Reunião",
    SourceKind.CALENDAR: "Agenda",
    SourceKind.GOOGLE_CHAT: "Chat",
    SourceKind.MANUAL: "Manual",
}


# Lista do conteúdo coletado, com busca por palavra.
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
):
    # Consulta base: mais recentes primeiro.
    query = select(BronzeItem).order_by(BronzeItem.occurred_at.desc().nulls_last()).limit(_LIMIT)
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
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "conteudo.html",
        {
            "owner": owner,
            "menu": "conteudo",
            "items": items,
            "q": q,
            "fonte": fonte,
            "labels": SOURCE_LABELS,
            "limit": _LIMIT,
            # Tags para o filtro.
            "tags": db.scalars(select(Tag).order_by(func.lower(Tag.name))).all(),
            # Tag escolhida.
            "tag": tag,
            # Tags de cada item da lista.
            "item_tags": tag_names(db, [i.id for i in items]),
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
            for d in db.scalars(select(DriveItem).where(DriveItem.drive_id.in_(linked_ids)))
        }
        if linked_ids
        else {}
    )
    # Outras versões do mesmo arquivo do Drive (thread_id = id do arquivo), mais novas primeiro.
    versions = (
        db.scalars(
            select(BronzeItem)
            .where(BronzeItem.source == SourceKind.DRIVE, BronzeItem.thread_id == item.thread_id)
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
        # ... só onde ela é liderança...
        .where(ProjectStakeholder.is_leader.is_(True))
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
            "menu": "conteudo",
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
        add_manual(db, item.id, find_or_create_tag(db, nome))
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
