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
from fastapi import APIRouter, HTTPException, Query, Request

# Resposta em HTML.
from fastapi.responses import HTMLResponse

# Consultas; or_ junta condições com "ou".
from sqlalchemy import or_, select

# Tabelas.
from app.models import Attachment, BronzeItem, DriveItem, SourceKind

# Peças comuns: dono, banco e modelos HTML.
from app.web import DB, Owner, templates

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
    # Executa.
    items = db.scalars(query).all()
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "conteudo.html",
        {
            "owner": owner,
            "menu": "inicio",
            "items": items,
            "q": q,
            "fonte": fonte,
            "labels": SOURCE_LABELS,
            "limit": _LIMIT,
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
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "conteudo_item.html",
        {
            "owner": owner,
            "menu": "inicio",
            "item": item,
            "label": SOURCE_LABELS.get(item.source, item.source),
            "attachments": attachments,
            "linked": linked,
            "versions": versions,
        },
    )
