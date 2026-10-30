"""Página Configurações (ícone de engrenagem) e navegação pelas pastas do Drive.

Seções: Contas conectadas, Claude (modelo, esforço, gasto, teste), Transcrição (idioma),
Pastas do Drive monitoradas e Tags.

Formulários comuns (sem JavaScript): cada botão envia um POST, a rota faz a mudança e
redireciona de volta para a página (padrão "POST, redireciona, GET"; assim recarregar a
página não repete a ação). O aviso de sucesso ou erro viaja no cookie de sessão
("flash"), em vez de no endereço, para ninguém conseguir montar um link com um aviso
falso.
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
from app.config import get_settings

# Cadastro de pastas e cliente do Drive.
from app.ingestion.drive_sync import add_folder, deactivate_folder, drive_client

# Gasto do mês com o Claude.
from app.llm import month_spend_usd

# Tabelas consultadas.
from app.models import DriveFolder, DriveItem, OAuthToken

# Opções e leitura/gravação das configurações do console.
from app.runtime_settings import (
    EFFORTS,
    LLM_MODELS,
    TRANSCRIPTION_LANGUAGES,
    current,
    set_value,
)

# Tags.
from app.tags import (
    assign_tag,
    create_tag,
    delete_tag,
    effective_tags,
    list_tags,
    rename_tag,
    unassign_tag,
)

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
def _back(url: str = "/config") -> RedirectResponse:
    # Resposta de redirecionamento.
    return RedirectResponse(url, status_code=303)


# Página Configurações.
@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, owner: Owner, db: DB):
    # Configuração do .env.
    settings = get_settings()
    # Contas Google conectadas.
    accounts = db.scalars(select(OAuthToken).order_by(OAuthToken.account_email)).all()
    # Pastas monitoradas (ativas primeiro) e o item da árvore de cada uma.
    folders = db.scalars(
        select(DriveFolder).order_by(DriveFolder.active.desc(), DriveFolder.name)
    ).all()
    # Itens raiz, pelo id do Drive.
    roots = {
        item.drive_id: item
        for item in db.scalars(
            select(DriveItem).where(DriveItem.drive_id.in_([f.drive_id for f in folders]))
        )
    }
    # Tags efetivas das pastas raiz.
    root_tags = effective_tags(db, [item.id for item in roots.values()])
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "config",
            # Aviso pendente (e já apagado do cookie).
            "flash": request.session.pop("flash", None),
            # Contas.
            "accounts": accounts,
            # Valores em vigor.
            "current": current(db, settings),
            # Opções de modelo, esforço e idioma.
            "models": LLM_MODELS,
            "efforts": EFFORTS,
            "languages": TRANSCRIPTION_LANGUAGES,
            # Gasto e teto do mês.
            "spent": month_spend_usd(db),
            "budget": settings.llm_monthly_budget_usd,
            # Pastas, itens raiz e tags deles.
            "folders": folders,
            "roots": roots,
            "root_tags": root_tags,
            # Tags com contagem de uso.
            "tags": list_tags(db),
        },
    )


# Salva modelo e esforço do Claude.
@router.post("/config/claude", dependencies=[SameOrigin])
def save_claude(
    request: Request,
    owner: Owner,
    db: DB,
    # Campo "modelo" do formulário.
    modelo: Annotated[str, Form()],
    # Campo "esforco" do formulário.
    esforco: Annotated[str, Form()],
):
    # Configuração.
    settings = get_settings()
    # Tenta gravar os dois.
    try:
        # Modelo.
        set_value(db, settings, "llm_model", modelo)
        # Esforço.
        set_value(db, settings, "llm_effort", esforco)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(request, "Configuração do Claude salva. Vale a partir da próxima chamada.")
    # Valor fora da lista.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta para a seção.
    return _back("/config#claude")


# Salva o idioma da transcrição.
@router.post("/config/transcricao", dependencies=[SameOrigin])
def save_transcription(request: Request, owner: Owner, db: DB, idioma: Annotated[str, Form()]):
    # Tenta gravar.
    try:
        # Idioma.
        set_value(db, get_settings(), "transcription_language", idioma)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(request, "Idioma salvo. Vale a partir da próxima transcrição iniciada.")
    # Valor fora da lista.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta para a seção.
    return _back("/config#transcricao")


# Cadastra uma pasta do Drive para monitorar.
@router.post("/config/drive/pastas", dependencies=[SameOrigin])
def add_drive_folder(request: Request, owner: Owner, db: DB, link: Annotated[str, Form()]):
    # Tenta cadastrar.
    try:
        # Cliente do Drive com as credenciais gravadas.
        drive = drive_client(db, get_settings())
        # Confere no Drive e cadastra.
        folder = add_folder(db, drive, link)
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
    return _back("/config#drive")


# Para de monitorar uma pasta (nada é apagado).
@router.post("/config/drive/pastas/{folder_id}/remover", dependencies=[SameOrigin])
def remove_drive_folder(request: Request, owner: Owner, db: DB, folder_id: int):
    # Desliga.
    deactivate_folder(db, folder_id)
    # Confirma.
    db.commit()
    # Aviso.
    _flash(request, "Pasta removida do monitoramento. Arquivos e tags já lidos continuam no banco.")
    # Volta.
    return _back("/config#drive")


# Pede uma varredura já (o agendador roda em até 1 minuto).
@router.post("/config/drive/varrer", dependencies=[SameOrigin])
def scan_now(request: Request, owner: Owner, db: DB):
    # Cada pasta ativa.
    for folder in db.scalars(select(DriveFolder).where(DriveFolder.active.is_(True))):
        # "Nunca varrida" faz o agendador varrer na próxima volta.
        folder.last_scan_at = None
    # Confirma.
    db.commit()
    # Aviso.
    _flash(request, "Varredura pedida. O monitor começa em até 1 minuto.")
    # Volta.
    return _back("/config#drive")


# Cria uma tag.
@router.post("/config/tags", dependencies=[SameOrigin])
def new_tag(request: Request, owner: Owner, db: DB, nome: Annotated[str, Form()]):
    # Tenta criar.
    try:
        # Cria.
        tag = create_tag(db, nome)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(request, f"Tag “{tag.name}” criada.")
    # Nome vazio ou repetido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta.
    return _back("/config#tags")


# Renomeia uma tag.
@router.post("/config/tags/{tag_id}/renomear", dependencies=[SameOrigin])
def rename(request: Request, owner: Owner, db: DB, tag_id: int, nome: Annotated[str, Form()]):
    # Tenta renomear.
    try:
        # Renomeia.
        tag = rename_tag(db, tag_id, nome)
        # Confirma.
        db.commit()
        # Aviso.
        _flash(request, f"Tag renomeada para “{tag.name}”.")
    # Tag inexistente ou nome repetido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Aviso de erro.
        _flash(request, str(exc), error=True)
    # Volta.
    return _back("/config#tags")


# Apaga uma tag (some de todas as pastas e arquivos).
@router.post("/config/tags/{tag_id}/apagar", dependencies=[SameOrigin])
def remove_tag(request: Request, owner: Owner, db: DB, tag_id: int):
    # Apaga.
    delete_tag(db, tag_id)
    # Confirma.
    db.commit()
    # Aviso.
    _flash(request, "Tag apagada de todas as pastas e arquivos.")
    # Volta.
    return _back("/config#tags")


# Só aceita voltar para páginas do próprio console (evita redirecionar para outro site).
def _safe_return(url: str | None, default: str) -> str:
    # Endereço interno do console: começa com "/" mas não com "//" (que seria outro site).
    if url and url.startswith("/config") and not url.startswith("//"):
        return url
    # Senão, o padrão.
    return default


# Coloca uma tag num arquivo ou pasta.
@router.post("/config/drive/itens/{item_id}/tags", dependencies=[SameOrigin])
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
    return _back(_safe_return(voltar, f"/config/drive/itens/{item_id}"))


# Retira uma tag colocada diretamente num item.
@router.post("/config/drive/itens/{item_id}/tags/{tag_id}/retirar", dependencies=[SameOrigin])
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
    return _back(_safe_return(voltar, f"/config/drive/itens/{item_id}"))


# Navegação: conteúdo de uma pasta do Drive, com as tags de cada item.
@router.get("/config/drive/itens/{item_id}", response_class=HTMLResponse)
def browse(request: Request, owner: Owner, db: DB, item_id: int, removidos: bool = False):
    # A pasta (ou arquivo) pedida.
    item = db.get(DriveItem, item_id)
    # Não existe.
    if item is None:
        raise HTTPException(status_code=404, detail="Item não encontrado.")
    # Conteúdo direto (só para pastas): subpastas primeiro, depois arquivos, por nome.
    query = (
        select(DriveItem)
        .where(DriveItem.parent_drive_id == item.drive_id)
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
            select(DriveItem).where(DriveItem.drive_id == trail[0].parent_drive_id)
        ).first()
        # Fora do que conhecemos: para.
        if parent is None:
            break
        # Põe no começo.
        trail.insert(0, parent)
    # Tags do item e de cada filho, de uma vez.
    tags = effective_tags(db, [item.id] + [c.id for c in children])
    # Todas as tags, para sugerir no campo de texto.
    all_tags = [tag for tag, _ in list_tags(db)]
    # Monta a página.
    return templates.TemplateResponse(
        request,
        "drive_browse.html",
        {
            "owner": owner,
            "menu": "config",
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
