"""Página Rascunhos e o botão "Rascunhar resposta" de cada e-mail.

Por que uma página própria: responder e-mail é trabalho de todo dia, e a lista "Para
responder" (sem custo) diz onde a conversa espera você. Cada e-mail tem a sua página,
com a conversa, o campo "O que responder" e os rascunhos já feitos, prontos para copiar.

Rotas:
- GET  /rascunhos: números, a lista "Para responder" e os rascunhos recentes;
- GET  /rascunhos/email/{id}: a conversa do e-mail e os rascunhos dele;
- POST /rascunhos/email/{id}/escrever: pede um rascunho ao Claude (o único ponto pago);
- POST /rascunhos/email/{id}/dispensar: tira o e-mail da lista "Para responder";
- POST /rascunhos/email/{id}/voltar: devolve o e-mail à lista.
"""

# Annotated junta um tipo com informação extra (campos da URL e do formulário).
from typing import Annotated

# Erros da API da Anthropic (rede, chave).
import anthropic

# Rotas, erro, formulário, parâmetro da URL e o pedido.
from fastapi import APIRouter, Form, HTTPException, Query, Request

# Respostas em HTML e redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Consulta dos anexos.
from sqlalchemy import select

# Filtro de Empresa vindo da URL.
from app.companies import filter_id

# Configuração.
from app.config import Settings, get_settings

# Banco (o cliente do Claude grava cada chamada numa sessão própria).
from app.db import get_sessionmaker

# Regras dos rascunhos.
from app.drafts import (
    MAX_INSTRUCTIONS,
    DraftError,
    account_of,
    address,
    dismiss,
    gmail_link,
    is_latest,
    my_emails,
    of_item,
    overview,
    recent,
    thread_of,
    to_answer,
    undismiss,
    write_draft,
)

# Cliente do Claude e o erro de teto geral.
from app.llm import BudgetExceededError, LLMClient

# Tabelas.
from app.models import (
    Attachment,
    BronzeItem,
    Company,
    ItemClassification,
    Project,
    ReplyDismissal,
    SourceKind,
)

# Texto próprio de cada mensagem (sem a parte citada).
from app.project_tags import own_text

# Peças comuns: dono, banco, conferência de origem e modelos HTML.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()


# Cliente do Claude para os rascunhos; None sem a chave. Uma função à parte para os testes
# trocarem por um Claude falso (nenhum teste chama a API de verdade).
def draft_writer(settings: Settings) -> LLMClient | None:
    # Sem chave: o botão avisa em vez de tentar.
    if not settings.anthropic_api_key:
        return None
    # Cliente real.
    return LLMClient(get_sessionmaker(), settings)


# O e-mail pedido, ou 404 (só e-mails do Gmail têm resposta).
def _email(db, item_id: int) -> BronzeItem:
    # O item.
    item = db.get(BronzeItem, item_id)
    # Não existe ou não é e-mail.
    if item is None or item.source != SourceKind.GMAIL:
        raise HTTPException(status_code=404, detail="E-mail não encontrado.")
    # Devolve.
    return item


# Página Rascunhos.
@router.get("/rascunhos", response_class=HTMLResponse)
def drafts_page(
    request: Request,
    owner: Owner,
    db: DB,
    # Empresa mostrada (vazia = todas).
    empresa: Annotated[str | None, Query(max_length=20)] = None,
):
    # Empresa do filtro (None = todas).
    company_id = filter_id(empresa)
    # Configuração.
    settings = get_settings()
    # Página.
    return templates.TemplateResponse(
        request,
        "rascunhos.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "rascunhos",
            # Filtro escolhido.
            "empresa": company_id,
            # Números.
            "resumo": overview(db, settings, company_id),
            # A lista "Para responder".
            "para_responder": to_answer(db, settings, company_id),
            # Janela da lista, em dias.
            "dias": settings.drafts_lookback_days,
            # Rascunhos recentes.
            "recentes": recent(db, company_id),
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
        },
    )


# Página de um e-mail: a conversa, o pedido e os rascunhos.
@router.get("/rascunhos/email/{item_id}", response_class=HTMLResponse)
def email_page(request: Request, owner: Owner, db: DB, item_id: int):
    # O e-mail.
    item = _email(db, item_id)
    # Configuração.
    settings = get_settings()
    # Seus e-mails nesta Empresa (para marcar as suas mensagens na conversa).
    mine = my_emails(db, settings, item.company_id)
    # A conversa até este e-mail: (mensagem, texto próprio, é sua?).
    conversa = [
        (m, own_text(None, m.body_text).strip(), address(m.author) in mine)
        for m in thread_of(db, item)
    ]
    # Projeto do e-mail.
    classification = db.get(ItemClassification, item.id)
    # O projeto, se houver.
    project = (
        db.get(Project, classification.project_id)
        if classification and classification.project_id
        else None
    )
    # Página.
    return templates.TemplateResponse(
        request,
        "rascunho_email.html",
        {
            "owner": owner,
            "menu": "rascunhos",
            # O e-mail.
            "item": item,
            # Empresa.
            "company": db.get(Company, item.company_id),
            # Projeto.
            "projeto": project,
            # Conversa: (mensagem, texto próprio, é sua?).
            "conversa": conversa,
            # Anexos da mensagem (só os nomes; o texto vai ao Claude).
            "anexos": db.scalars(
                select(Attachment.filename).where(
                    Attachment.bronze_item_id == item.id,
                    Attachment.origin.in_(("anexo", "imagem_embutida")),
                    Attachment.extracted_text.is_not(None),
                )
            ).all(),
            # Há mensagem mais nova na conversa?
            "ultima": is_latest(db, item),
            # Tirado da lista "Para responder"?
            "dispensado": db.get(ReplyDismissal, item.id) is not None,
            # Rascunhos deste e-mail, o mais novo primeiro.
            "rascunhos": of_item(db, item.id),
            # Link da conversa no Gmail, na conta da Empresa.
            "link_gmail": gmail_link(account_of(db, item.company_id), item.thread_id),
            # Números (gasto e teto).
            "resumo": overview(db, settings, None),
            # Tamanho máximo do pedido.
            "max_pedido": MAX_INSTRUCTIONS,
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
        },
    )


# Pede um rascunho ao Claude.
@router.post("/rascunhos/email/{item_id}/escrever", dependencies=[SameOrigin])
def write(
    request: Request,
    owner: Owner,
    db: DB,
    item_id: int,
    # O que você quer responder (opcional).
    pedido: Annotated[str, Form(max_length=MAX_INSTRUCTIONS * 2)] = "",
):
    # O e-mail (404 se não for e-mail).
    item = _email(db, item_id)
    # Endereço da página do e-mail.
    page = f"/rascunhos/email/{item.id}"
    # Configuração.
    settings = get_settings()
    # Tenta.
    try:
        # Escreve e grava.
        draft = write_draft(db, settings, draft_writer(settings), item.id, pedido, owner)
    # Regra (sem chave, teto dos rascunhos): nada foi cobrado.
    except DraftError as exc:
        # Desfaz o que estiver pendente.
        db.rollback()
        # Aviso vermelho.
        request.session["flash"] = {"texto": str(exc), "erro": True}
        # Volta.
        return RedirectResponse(page, status_code=303)
    # Teto geral do mês.
    except BudgetExceededError as exc:
        # Desfaz.
        db.rollback()
        # Aviso.
        request.session["flash"] = {"texto": str(exc), "erro": True}
        # Volta.
        return RedirectResponse(page, status_code=303)
    # Erro da API (rede, chave inválida): a chamada fica registrada no custo, com o erro.
    except anthropic.APIError as exc:
        # Desfaz.
        db.rollback()
        # Aviso.
        request.session["flash"] = {
            "texto": f"O Claude não respondeu ({type(exc).__name__}). Tente de novo.",
            "erro": True,
        }
        # Volta.
        return RedirectResponse(page, status_code=303)
    # Grava.
    db.commit()
    # Aviso conforme a situação.
    if draft.status == "ok":
        # Pronto.
        request.session["flash"] = {"texto": f"Rascunho pronto (US$ {draft.cost_usd:.3f})."}
    # Recusa.
    elif draft.status == "recusa":
        # Aviso.
        request.session["flash"] = {
            "texto": "O Claude recusou escrever esta resposta.",
            "erro": True,
        }
    # Ilegível.
    else:
        # Aviso.
        request.session["flash"] = {
            "texto": "A resposta do Claude veio cortada ou fora do formato. Tente de novo.",
            "erro": True,
        }
    # Volta para o rascunho novo.
    return RedirectResponse(f"{page}#rascunho-{draft.id}", status_code=303)


# Tira o e-mail da lista "Para responder".
@router.post("/rascunhos/email/{item_id}/dispensar", dependencies=[SameOrigin])
def dismiss_email(
    request: Request,
    owner: Owner,
    db: DB,
    item_id: int,
    # Para onde voltar ("lista" = página Rascunhos).
    volta: Annotated[str | None, Form(max_length=20)] = None,
):
    # O e-mail.
    item = _email(db, item_id)
    # Dispensa.
    dismiss(db, item, owner)
    # Grava.
    db.commit()
    # Aviso.
    request.session["flash"] = {"texto": "Saiu da lista Para responder."}
    # Volta para a lista ou para o e-mail.
    target = "/rascunhos" if volta == "lista" else f"/rascunhos/email/{item.id}"
    # Redireciona.
    return RedirectResponse(target, status_code=303)


# Devolve o e-mail à lista.
@router.post("/rascunhos/email/{item_id}/voltar", dependencies=[SameOrigin])
def undismiss_email(request: Request, owner: Owner, db: DB, item_id: int):
    # O e-mail.
    item = _email(db, item_id)
    # Devolve.
    undismiss(db, item, owner)
    # Grava.
    db.commit()
    # Aviso.
    request.session["flash"] = {"texto": "Voltou para a lista Para responder."}
    # Volta.
    return RedirectResponse(f"/rascunhos/email/{item.id}", status_code=303)
