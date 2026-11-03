"""Pastas do Drive monitoradas (seção da página Cadastros) e navegação pelas pastas.

Ficavam em Configurações até 03/11/2026; a dona pediu para ficarem em Cadastros, junto
com projetos, pessoas e tags: cadastrar uma pasta é cadastro (o que o sistema lê), não
configuração (como o sistema funciona). Os endereços passaram de /config/drive/... para
/cadastros/drive/....

Formulários comuns (sem JavaScript): cada botão envia um POST, a rota faz a mudança e
redireciona de volta (padrão "POST, redireciona, GET"). O aviso viaja no cookie de sessão.

Empresa: cada pasta é cadastrada numa Empresa (combo obrigatório) e conferida com a conta
Google dessa Empresa. As tags que você põe numa pasta são da Empresa dela.
"""

# Annotated junta um tipo com informação extra (aqui, campos de formulário).
from typing import Annotated

# APIRouter agrupa rotas; Form lê campos de formulário; HTTPException devolve erro HTTP.
from fastapi import APIRouter, Form, HTTPException, Request

# Tipos de resposta: página HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Consultas ao banco.
from sqlalchemy import select

# Leitura da configuração.
from app.companies import filter_id, parse_company
from app.config import get_settings

# Cadastro de pastas e cliente do Drive.
from app.ingestion.drive_sync import add_folder, deactivate_folder, drive_client

# Tabelas consultadas.
from app.models import DriveFolder, DriveItem

# Tags.
from app.tags import assign_tag, effective_tags, list_tags, unassign_tag

# Peças comuns: modelos HTML, dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Quantos níveis subir, no máximo, para montar o caminho de pastas no topo da página.
_MAX_BREADCRUMB = 50


# Guarda um aviso para mostrar na próxima página.
def _flash(request: Request, message: str, error: bool = False) -> None:
    # O cookie de sessão guarda texto e tipo.
    request.session["flash"] = {"texto": message, "erro": error}


# Redireciona com 303 (o navegador faz GET no novo endereço).
def _back(url: str) -> RedirectResponse:
    # Resposta de redirecionamento.
    return RedirectResponse(url, status_code=303)


# Dados da seção "Pastas do Drive" da página Cadastros.
def folders_context(db, company_id: int | None = None) -> dict:
    """company_id: só as pastas de uma Empresa; None = todas."""
    # Pastas monitoradas (ativas primeiro).
    query = select(DriveFolder).order_by(DriveFolder.active.desc(), DriveFolder.name)
    # Só uma Empresa, se pedido.
    if company_id is not None:
        query = query.where(DriveFolder.company_id == company_id)
    # Executa.
    folders = db.scalars(query).all()
    # Pares (Empresa, id do Drive) das pastas: o mesmo id pode estar em duas Empresas.
    keys = {(f.company_id, f.drive_id) for f in folders}
    # Itens raiz, por (Empresa, id do Drive).
    roots = {
        (item.company_id, item.drive_id): item
        for item in db.scalars(
            select(DriveItem).where(DriveItem.drive_id.in_([f.drive_id for f in folders]))
        )
        if (item.company_id, item.drive_id) in keys
    }
    # Pastas, itens raiz e as tags efetivas das pastas raiz.
    return {
        "folders": folders,
        "roots": roots,
        "root_tags": effective_tags(db, [item.id for item in roots.values()]),
    }


# Cadastra uma pasta do Drive para monitorar.
@router.post("/cadastros/drive/pastas", dependencies=[SameOrigin])
def add_drive_folder(
    request: Request,
    owner: Owner,
    db: DB,
    link: Annotated[str, Form()],
    # Empresa da pasta (combo obrigatório).
    empresa: Annotated[str, Form()] = "",
):
    # Tenta cadastrar.
    try:
        # Empresa escolhida (sem ela, não cadastra).
        company = parse_company(db, empresa)
        # Cliente do Drive da conta dessa Empresa.
        drive = drive_client(db, get_settings(), company.id)
        # Confere no Drive e cadastra na Empresa.
        folder = add_folder(db, drive, link, company.id)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(
            request, f"Pasta “{folder.name}” cadastrada. A primeira varredura começa em até 1 min."
        )
    # Link inválido, sem acesso ou Google desconectado.
    except (ValueError, RuntimeError) as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta para a seção.
    return _back("/cadastros#drive")


# Para de monitorar uma pasta (nada é apagado).
@router.post("/cadastros/drive/pastas/{folder_id}/remover", dependencies=[SameOrigin])
def remove_drive_folder(request: Request, owner: Owner, db: DB, folder_id: int):
    # Desliga.
    deactivate_folder(db, folder_id)
    # Confirma.
    db.commit()
    # Aviso.
    _flash(request, "Pasta removida do monitoramento. Arquivos e tags já lidos continuam no banco.")
    # Volta.
    return _back("/cadastros#drive")


# Pede uma varredura já (o agendador roda em até 1 minuto).
@router.post("/cadastros/drive/varrer", dependencies=[SameOrigin])
def scan_now(request: Request, owner: Owner, db: DB, empresa: Annotated[str, Form()] = ""):
    # Pastas ativas.
    query = select(DriveFolder).where(DriveFolder.active.is_(True))
    # Só as da Empresa mostrada na página, se houver filtro.
    if (company_id := filter_id(empresa)) is not None:
        query = query.where(DriveFolder.company_id == company_id)
    # Cada pasta.
    for folder in db.scalars(query):
        # "Nunca varrida" faz o agendador varrer na próxima volta.
        folder.last_scan_at = None
    # Confirma.
    db.commit()
    # Aviso.
    _flash(request, "Varredura pedida. O monitor começa em até 1 minuto.")
    # Volta.
    return _back("/cadastros#drive")


# Só aceita voltar para páginas do próprio console (evita redirecionar para outro site).
def _safe_return(url: str | None, default: str) -> str:
    # Endereço interno do console: começa com "/" mas não com "//" (que seria outro site).
    if url and url.startswith("/cadastros") and not url.startswith("//"):
        return url
    # Senão, o padrão.
    return default


# Coloca uma tag num arquivo ou pasta.
@router.post("/cadastros/drive/itens/{item_id}/tags", dependencies=[SameOrigin])
def tag_item(
    request: Request,
    owner: Owner,
    db: DB,
    item_id: int,
    # Nome da tag (existente ou nova).
    nome: Annotated[str, Form()],
    # Página para onde voltar.
    voltar: Annotated[str | None, Form()] = None,
):
    # Tenta colocar.
    try:
        # Coloca (cria a tag se o nome for novo).
        tag = assign_tag(db, item_id, nome)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(request, f"Tag “{tag.name}” aplicada. Tudo dentro desta pasta herda a tag.")
    # Item inexistente ou nome inválido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta.
    return _back(_safe_return(voltar, f"/cadastros/drive/itens/{item_id}"))


# Retira uma tag colocada diretamente num item.
@router.post("/cadastros/drive/itens/{item_id}/tags/{tag_id}/retirar", dependencies=[SameOrigin])
def untag_item(
    request: Request,
    owner: Owner,
    db: DB,
    item_id: int,
    tag_id: int,
    # Página para onde voltar.
    voltar: Annotated[str | None, Form()] = None,
):
    # Retira.
    unassign_tag(db, item_id, tag_id)
    # Confirma.
    db.commit()
    # Aviso.
    _flash(request, "Tag retirada.")
    # Volta.
    return _back(_safe_return(voltar, f"/cadastros/drive/itens/{item_id}"))


# Navegação: conteúdo de uma pasta do Drive, com as tags de cada item.
@router.get("/cadastros/drive/itens/{item_id}", response_class=HTMLResponse)
def browse(request: Request, owner: Owner, db: DB, item_id: int, removidos: bool = False):
    # A pasta (ou arquivo) pedida.
    item = db.get(DriveItem, item_id)
    # Não existe.
    if item is None:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Conteúdo direto (só para pastas): subpastas primeiro, depois arquivos, por nome.
    query = (
        select(DriveItem)
        .where(
            DriveItem.company_id == item.company_id,
            DriveItem.parent_drive_id == item.drive_id,
        )
        .order_by(DriveItem.is_folder.desc(), DriveItem.name)
    )
    # Sem pedir os removidos, esconde o que saiu da pasta.
    if not removidos:
        # Só os presentes.
        query = query.where(DriveItem.removed.is_(False))
    # Executa.
    children = db.scalars(query).all() if item.is_folder else []
    # Caminho de pastas até aqui (do topo até o item).
    trail = [item]
    # Sobe pela pasta-mãe.
    while trail[0].parent_drive_id and len(trail) < _MAX_BREADCRUMB:
        # Pasta-mãe.
        parent = db.scalars(
            select(DriveItem).where(
                DriveItem.company_id == item.company_id,
                DriveItem.drive_id == trail[0].parent_drive_id,
            )
        ).first()
        # Fora do que conhecemos: para.
        if parent is None:
            break
        # Põe no começo.
        trail.insert(0, parent)
    # Tags do item e de cada filho, de uma vez.
    tags = effective_tags(db, [item.id] + [c.id for c in children])
    # Todas as tags, para sugerir no campo de texto.
    all_tags = [tag for tag, _ in list_tags(db, item.company_id)]
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "drive_browse.html",
        {
            "owner": owner,
            "menu": "cadastros",
            "flash": request.session.pop("flash", None),
            # A pasta atual.
            "item": item,
            # Conteúdo.
            "children": children,
            # Caminho de pastas.
            "trail": trail,
            # Tags por item.
            "tags": tags,
            # Sugestões de nomes.
            "all_tags": all_tags,
            # Mostrando removidos?
            "removidos": removidos,
            # Endereço desta página, para voltar depois de um POST.
            "here": str(request.url.path) + ("?removidos=true" if removidos else ""),
        },
    )
