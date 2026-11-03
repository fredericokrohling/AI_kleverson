"""Página Cadastros (lâmpada no menu): projetos, pessoas, pastas do Drive e tags.

Pedido da dona: o cadastro fica numa página própria, separada de Configurações. Em
Configurações ficam as contas, o Claude e a transcrição. As pastas do Drive vieram para
cá em 03/11/2026 (rotas em app/routes_drive.py).

Todo POST volta para uma página (padrão "POST, redireciona, GET"): apertar F5 não repete
o cadastro, e o resultado aparece como aviso no topo.

Empresas (pedido de 03/11/2026): a página começa pelas Empresas (botão "Nova Empresa").
Projeto, pessoa, pasta do Drive e tag só são cadastrados com a Empresa escolhida no combo
do formulário; o servidor confere de novo (o navegador pode ser enganado). O combo
"Mostrar" no topo filtra as listas por Empresa e já deixa essa Empresa escolhida nos
formulários.
"""

# Annotated junta um tipo com informação extra (aqui, campos de formulário).
from typing import Annotated

# Peças do FastAPI.
from fastapi import APIRouter, Form, HTTPException, Request

# Respostas HTML e de redirecionamento.
from fastapi.responses import HTMLResponse, RedirectResponse

# Erro devolvido pelas APIs do Google.
from googleapiclient.errors import HttpError

# Consultas.
from sqlalchemy import func, select

# Tabelas.
from app.companies import (
    accounts_by_company,
    create_company,
    filter_id,
    list_companies,
    parse_company,
    update_company,
)

# Configuração.
from app.config import get_settings
from app.models import (
    Company,
    GmailLabelTag,
    Project,
    ProjectStakeholder,
    Stakeholder,
    Tag,
    TaggedItem,
    Utterance,
)

# Regras de tag: pastas do Gmail.
from app.project_tags import link_label, sync_label_tags, unlink_label

# Cadastro de projetos e pessoas.
from app.projects import (
    create_project,
    projects_by_person,
    refresh,
    remove_member,
    save_person,
    set_member,
    team_of,
    update_project,
)

# Seção das pastas do Drive.
from app.routes_drive import folders_context

# Tags.
from app.runtime_settings import TRANSCRIPTION_LANGUAGES
from app.tags import create_tag, delete_tag, list_tags, rename_tag

# Peças comuns: modelos HTML, dono, banco e proteção de POST.
from app.web import DB, Owner, SameOrigin, templates

# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()

# Atalho para campos de formulário de texto (vazio quando não vem).
Text = Annotated[str, Form()]


# Cliente da API do Gmail com as credenciais gravadas (erro se o Google não estiver conectado).
def gmail_client(db, settings, company_id: int):
    # Import local: só carrega quando a página precisa do Gmail.
    from googleapiclient.discovery import build

    # Leitura das credenciais.
    from app.google_auth import load_credentials

    # Criptografia.
    from app.security import get_cipher

    # Credenciais da conta da Empresa.
    creds = load_credentials(db, get_cipher(), settings, company_id=company_id)
    # Cliente.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# Pastas (marcadores) criadas por você no Gmail da Empresa: [(id, nome)], por nome.
def gmail_labels(db, company_id: int) -> tuple[list[tuple[str, str]], str | None]:
    """(pastas, motivo se não deu para ler). Só as pastas da conta da Empresa."""
    # Tenta.
    try:
        # Cliente da conta da Empresa.
        gmail = gmail_client(db, get_settings(), company_id)
        # Lista os marcadores.
        response = gmail.users().labels().list(userId="me").execute()
    # Sem conta Google.
    except RuntimeError as exc:
        return [], str(exc)
    # A API recusou.
    except HttpError as exc:
        return [], f"O Gmail recusou o pedido (HTTP {exc.resp.status})."
    # Só os criados pelo usuário (os do sistema, como INBOX, ficam de fora).
    labels = [
        (lb["id"], lb["name"]) for lb in response.get("labels", []) if lb.get("type") == "user"
    ]
    # Em ordem de nome.
    return sorted(labels, key=lambda pair: pair[1].lower()), None


# Guarda o aviso e redireciona.
def _back(request: Request, url: str, text: str, error: bool = False) -> RedirectResponse:
    # Aviso para o topo da página.
    request.session["flash"] = {"texto": text, "erro": error}
    # 303: o navegador abre a página com GET.
    return RedirectResponse(url, status_code=303)


# Caixa de seleção marcada? (HTML só manda o campo quando está marcada.)
def _checked(value: str) -> bool:
    # Qualquer valor conta como marcado.
    return bool(value)


# Página principal: projetos, pessoas e tags.
@router.get("/cadastros", response_class=HTMLResponse)
def registry_page(request: Request, owner: Owner, db: DB, empresa: str | None = None):
    # Empresa do filtro "Mostrar" (None = todas).
    company_id = filter_id(empresa)
    # Projetos (ativos primeiro) com o tamanho da equipe.
    query = (
        # Projeto, tag e quantas pessoas.
        select(Project, Tag, func.count(ProjectStakeholder.stakeholder_id))
        # Tag do projeto (pode não haver).
        .outerjoin(Tag, Tag.id == Project.tag_id)
        # Equipe (pode estar vazia).
        .outerjoin(ProjectStakeholder, ProjectStakeholder.project_id == Project.id)
        # Um grupo por projeto.
        .group_by(Project.id, Tag.id)
        # Ativos primeiro, depois por nome.
        .order_by(Project.active.desc(), func.lower(Project.name))
    )
    # Pessoas, por nome.
    people_query = select(Stakeholder).order_by(func.lower(Stakeholder.name))
    # Filtro de Empresa.
    if company_id is not None:
        # Projetos da Empresa.
        query = query.where(Project.company_id == company_id)
        # Pessoas da Empresa.
        people_query = people_query.where(Stakeholder.company_id == company_id)
    # Executa.
    projects = db.execute(query).all()
    # Pessoas.
    people = db.scalars(people_query).all()
    # Quantos projetos e pessoas cada Empresa tem (para a tabela de Empresas).
    counts = {
        "projetos": dict(
            db.execute(select(Project.company_id, func.count()).group_by(Project.company_id)).all()
        ),
        "pessoas": dict(
            db.execute(
                select(Stakeholder.company_id, func.count()).group_by(Stakeholder.company_id)
            ).all()
        ),
    }
    # Monta a página.
    return templates.TemplateResponse(
        # A requisição.
        request,
        # Modelo.
        "cadastros.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu destacado.
            "menu": "cadastros",
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
            # Projetos.
            "projects": projects,
            # Pessoas.
            "people": people,
            # Projetos de cada pessoa.
            "memberships": projects_by_person(db),
            # Tags com contagem de uso.
            "tags": list_tags(db, company_id),
            # Ids das tags que são de projeto (não podem ser apagadas aqui).
            "project_tags": set(
                db.scalars(select(Project.tag_id).where(Project.tag_id.is_not(None)))
            ),
            # Empresas, com a conta e as contagens.
            "companies": list_companies(db),
            # Conta Google de cada Empresa.
            "accounts": accounts_by_company(db),
            # Projetos e pessoas por Empresa.
            "counts": counts,
            # Idiomas de transcrição (para a Empresa nova).
            "languages": TRANSCRIPTION_LANGUAGES,
            # Empresa do filtro (None = todas); também vem escolhida nos formulários.
            "filtro": company_id,
            # Pastas do Drive monitoradas (folders, roots, root_tags).
            **folders_context(db, company_id),
        },
    )


# Endereço da página Cadastros, mantendo o filtro de Empresa.
def _registry(empresa: str = "", anchor: str = "") -> str:
    # Com filtro.
    if filter_id(empresa) is not None:
        return f"/cadastros?empresa={filter_id(empresa)}#{anchor}"
    # Sem filtro.
    return f"/cadastros#{anchor}"


# Cadastra uma Empresa.
@router.post("/cadastros/empresas", dependencies=[SameOrigin])
def new_company(request: Request, owner: Owner, db: DB, nome: Text, idioma: Text = ""):
    # Tenta.
    try:
        # Cria.
        company = create_company(db, nome, idioma)
        # Confirma.
        db.commit()
    # Nome vazio ou repetido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Erro.
        return _back(request, "/cadastros#empresas", str(exc), error=True)
    # Volta já filtrando pela Empresa nova (os formulários vêm com ela escolhida).
    return _back(
        request,
        f"/cadastros?empresa={company.id}#empresas",
        f"Empresa “{company.name}” cadastrada. Conecte a conta Google dela em Configurações.",
    )


# Renomeia uma Empresa ou troca o idioma padrão dela.
@router.post("/cadastros/empresas/{company_id}", dependencies=[SameOrigin])
def edit_company(
    request: Request, owner: Owner, db: DB, company_id: int, nome: Text, idioma: Text = ""
):
    # Tenta.
    try:
        # Atualiza.
        company = update_company(db, company_id, nome, idioma)
        # Confirma.
        db.commit()
    # Inexistente, nome vazio ou repetido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Erro.
        return _back(request, "/cadastros#empresas", str(exc), error=True)
    # Pronto.
    return _back(request, "/cadastros#empresas", f"Empresa “{company.name}” salva.")


# Cria um projeto.
@router.post("/cadastros/projetos", dependencies=[SameOrigin])
def new_project(
    request: Request,
    owner: Owner,
    db: DB,
    # Campo "nome" do formulário.
    nome: Text,
    # Código do projeto (opcional).
    codigo: Text = "",
    # Outros nomes, separados por vírgula.
    outros_nomes: Text = "",
    # Palavras-chave, separadas por vírgula.
    palavras: Text = "",
    # Empresa (combo obrigatório).
    empresa: Text = "",
):
    # Tenta.
    try:
        # Empresa escolhida (sem ela, não cadastra).
        company = parse_company(db, empresa)
        # Cria o projeto e a tag.
        project = create_project(db, company.id, nome, codigo, outros_nomes, palavras)
        # Grava.
        db.commit()
    # Nome vazio ou repetido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, _registry(empresa, "projetos"), str(exc), error=True)
    # Abre o projeto para cadastrar a equipe.
    return _back(
        request,
        f"/cadastros/projetos/{project.id}",
        f"Projeto “{project.name}” criado, com a tag de mesmo nome. Agora cadastre a equipe.",
    )


# Página de um projeto: dados, equipe e pastas do Gmail.
@router.get("/cadastros/projetos/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, owner: Owner, db: DB, project_id: int):
    # O projeto.
    project = db.get(Project, project_id)
    # Não existe.
    if project is None:
        raise HTTPException(status_code=404, detail="Projeto não encontrado.")
    # Pastas do Gmail ligadas à tag do projeto.
    linked = (
        db.scalars(select(GmailLabelTag).where(GmailLabelTag.tag_id == project.tag_id)).all()
        if project.tag_id
        else []
    )
    # Pastas existentes no Gmail da conta da Empresa do projeto (para escolher).
    labels, labels_error = gmail_labels(db, project.company_id)
    # Quantos itens têm a tag do projeto (e-mails e transcrições).
    tagged = (
        db.scalar(
            select(func.count())
            .select_from(TaggedItem)
            .where(TaggedItem.tag_id == project.tag_id, TaggedItem.removed_at.is_(None))
        )
        if project.tag_id
        else 0
    )
    # Monta a página.
    return templates.TemplateResponse(
        # A requisição.
        request,
        # Modelo.
        "cadastros_projeto.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu.
            "menu": "cadastros",
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
            # O projeto.
            "project": project,
            # Tag do projeto.
            "tag": db.get(Tag, project.tag_id) if project.tag_id else None,
            # Equipe.
            "team": team_of(db, project.id),
            # Empresa do projeto.
            "company": db.get(Company, project.company_id),
            # Pessoas da Empresa que ainda não estão na equipe (para escolher).
            "others": db.scalars(
                select(Stakeholder)
                .where(
                    Stakeholder.company_id == project.company_id,
                    ~Stakeholder.id.in_(
                        select(ProjectStakeholder.stakeholder_id).where(
                            ProjectStakeholder.project_id == project.id
                        )
                    ),
                )
                .order_by(func.lower(Stakeholder.name))
            ).all(),
            # Pastas ligadas.
            "linked": linked,
            # Pastas do Gmail ainda não ligadas.
            "labels": [lb for lb in labels if lb[0] not in {x.label_id for x in linked}],
            # Motivo, se o Gmail não pôde ser lido.
            "labels_error": labels_error,
            # E-mails e transcrições com a tag.
            "tagged": tagged,
        },
    )


# Atualiza os dados de um projeto.
@router.post("/cadastros/projetos/{project_id}", dependencies=[SameOrigin])
def edit_project(
    request: Request,
    owner: Owner,
    db: DB,
    project_id: int,
    # Campo "nome" do formulário.
    nome: Text,
    # Código do projeto (opcional).
    codigo: Text = "",
    # Outros nomes, separados por vírgula.
    outros_nomes: Text = "",
    # Palavras-chave, separadas por vírgula.
    palavras: Text = "",
    # Caixa "ativo" (marcada = "1").
    ativo: Text = "",
):
    # Endereço da página do projeto.
    url = f"/cadastros/projetos/{project_id}"
    # Tenta.
    try:
        # Atualiza.
        update_project(
            db,
            project_id,
            # Nome.
            name=nome,
            # Código.
            code=codigo,
            # Outros nomes.
            aliases=outros_nomes,
            # Palavras-chave.
            keywords=palavras,
            # Ativo?
            active=_checked(ativo),
        )
        # Grava.
        db.commit()
    # Erro de validação.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, url, str(exc), error=True)
    # Sucesso.
    return _back(request, url, "Projeto salvo. As regras de tag foram reaplicadas.")


# Põe uma pessoa na equipe (existente, ou nova com os dados do formulário).
@router.post("/cadastros/projetos/{project_id}/equipe", dependencies=[SameOrigin])
def add_member(
    request: Request,
    owner: Owner,
    db: DB,
    project_id: int,
    # Id de uma pessoa já cadastrada (vazio = pessoa nova).
    pessoa: Text = "",
    # Campo "nome" do formulário.
    nome: Text = "",
    # E-mail.
    email: Text = "",
    # Cargo.
    cargo: Text = "",
    # Organização ou área (texto livre; não é a Empresa cliente).
    organizacao: Text = "",
    # Outros nomes, separados por vírgula.
    outros_nomes: Text = "",
    # Caixa "liderança na organização".
    lider_empresa: Text = "",
    # Função no projeto.
    funcao: Text = "",
    # Caixa "liderança no projeto".
    lider_projeto: Text = "",
):
    # Endereço da página do projeto.
    url = f"/cadastros/projetos/{project_id}#equipe"
    # Tenta.
    try:
        # Pessoa escolhida na lista.
        if pessoa.isdigit():
            # Id.
            person_id = int(pessoa)
        # Pessoa nova (ou a existente com o mesmo e-mail).
        else:
            # Projeto (a pessoa nova entra na Empresa dele).
            project = db.get(Project, project_id)
            # Não existe.
            if project is None:
                raise ValueError("Projeto não encontrado.")
            # Cria ou atualiza, na Empresa do projeto.
            person_id = save_person(
                db,
                None,
                # Empresa do projeto.
                company_id=project.company_id,
                # Nome.
                name=nome,
                # E-mail.
                email=email,
                # Organização ou área.
                organization=organizacao,
                # Cargo na empresa.
                role=cargo,
                # Liderança na empresa.
                is_leader=_checked(lider_empresa),
                # Outros nomes.
                aliases=outros_nomes,
            ).id
        # Liga ao projeto.
        set_member(db, project_id, person_id, funcao, _checked(lider_projeto))
        # Grava.
        db.commit()
    # Erro de validação.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, url, str(exc), error=True)
    # Sucesso.
    return _back(request, url, "Pessoa incluída na equipe.")


# Atualiza a função e a liderança de uma pessoa no projeto.
@router.post("/cadastros/projetos/{project_id}/equipe/{person_id}", dependencies=[SameOrigin])
def edit_member(
    request: Request,
    owner: Owner,
    db: DB,
    project_id: int,
    person_id: int,
    # Função no projeto.
    funcao: Text = "",
    # Caixa "liderança no projeto".
    lider_projeto: Text = "",
):
    # Endereço da página do projeto.
    url = f"/cadastros/projetos/{project_id}#equipe"
    # Tenta.
    try:
        # Atualiza.
        set_member(db, project_id, person_id, funcao, _checked(lider_projeto))
        # Grava.
        db.commit()
    # Erro.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, url, str(exc), error=True)
    # Sucesso.
    return _back(request, url, "Função na equipe salva.")


# Tira uma pessoa da equipe.
@router.post(
    "/cadastros/projetos/{project_id}/equipe/{person_id}/remover", dependencies=[SameOrigin]
)
def drop_member(request: Request, owner: Owner, db: DB, project_id: int, person_id: int):
    # Tira.
    remove_member(db, project_id, person_id)
    # Grava.
    db.commit()
    # Volta.
    return _back(
        request,
        f"/cadastros/projetos/{project_id}#equipe",
        "Pessoa tirada da equipe (o cadastro dela continua). Tags já colocadas ficam.",
    )


# Liga uma pasta do Gmail à tag do projeto.
@router.post("/cadastros/projetos/{project_id}/pastas", dependencies=[SameOrigin])
def add_label(request: Request, owner: Owner, db: DB, project_id: int, pasta: Text):
    # Endereço da página do projeto.
    url = f"/cadastros/projetos/{project_id}#pastas"
    # O projeto.
    project = db.get(Project, project_id)
    # Sem projeto ou sem tag.
    if project is None or project.tag_id is None:
        return _back(request, url, "Projeto sem tag.", error=True)
    # Pastas do Gmail da Empresa do projeto, para conferir o id e pegar o nome.
    labels, error = gmail_labels(db, project.company_id)
    # Nome da pasta escolhida.
    name = dict(labels).get(pasta)
    # Não achou (Gmail fora, ou pasta apagada).
    if name is None:
        return _back(request, url, error or "Pasta não encontrada no Gmail.", error=True)
    # Liga.
    link_label(db, pasta, name, project.tag_id)
    # Tenta já colocar a tag nos e-mails da pasta (senão, na próxima coleta).
    try:
        # Confere as pastas agora.
        added, _ = sync_label_tags(
            db, gmail_client(db, get_settings(), project.company_id), project.company_id
        )
    # Falhou: a próxima coleta faz.
    except (RuntimeError, HttpError):
        added = 0
    # Grava.
    db.commit()
    # Sucesso.
    return _back(request, url, f"Pasta “{name}” ligada: {added} e-mail(s) receberam a tag agora.")


# Desliga uma pasta do Gmail da tag do projeto.
@router.post(
    "/cadastros/projetos/{project_id}/pastas/{label_id}/remover", dependencies=[SameOrigin]
)
def drop_label(request: Request, owner: Owner, db: DB, project_id: int, label_id: str):
    # O projeto.
    project = db.get(Project, project_id)
    # Existe e tem tag.
    if project is not None and project.tag_id is not None:
        # Desliga e tira as tags postas por ela.
        unlink_label(db, label_id, project.tag_id)
        # Grava.
        db.commit()
    # Volta.
    return _back(
        request,
        f"/cadastros/projetos/{project_id}#pastas",
        "Pasta desligada; as tags que ela tinha colocado foram tiradas.",
    )


# Cria uma pessoa (sem projeto: por exemplo, uma diretora que participa de vários).
@router.post("/cadastros/pessoas", dependencies=[SameOrigin])
def new_person(
    request: Request,
    owner: Owner,
    db: DB,
    # Campo "nome" do formulário.
    nome: Text,
    # E-mail.
    email: Text = "",
    # Cargo.
    cargo: Text = "",
    # Organização ou área (texto livre; não é a Empresa cliente).
    organizacao: Text = "",
    # Outros nomes, separados por vírgula.
    outros_nomes: Text = "",
    # Caixa "liderança na organização".
    lider_empresa: Text = "",
    # Empresa (combo obrigatório).
    empresa: Text = "",
):
    # Tenta.
    try:
        # Empresa escolhida (sem ela, não cadastra).
        company = parse_company(db, empresa)
        # Cria (ou atualiza a de mesmo e-mail nesta Empresa).
        person = save_person(
            db,
            None,
            # Empresa.
            company_id=company.id,
            # Nome.
            name=nome,
            # E-mail.
            email=email,
            # Organização ou área.
            organization=organizacao,
            # Cargo na empresa.
            role=cargo,
            # Liderança na empresa.
            is_leader=_checked(lider_empresa),
            # Outros nomes.
            aliases=outros_nomes,
        )
        # Grava.
        db.commit()
    # Erro de validação.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, _registry(empresa, "pessoas"), str(exc), error=True)
    # Sucesso.
    return _back(request, _registry(empresa, "pessoas"), f"Pessoa “{person.name}” salva.")


# Página de uma pessoa: dados, projetos e falas reconhecidas.
@router.get("/cadastros/pessoas/{person_id}", response_class=HTMLResponse)
def person_page(request: Request, owner: Owner, db: DB, person_id: int):
    # A pessoa.
    person = db.get(Stakeholder, person_id)
    # Não existe.
    if person is None:
        raise HTTPException(status_code=404, detail="Pessoa não encontrada.")
    # Monta a página.
    return templates.TemplateResponse(
        # A requisição.
        request,
        # Modelo.
        "cadastros_pessoa.html",
        {
            # Seu login.
            "owner": owner,
            # Item do menu.
            "menu": "cadastros",
            # Aviso pendente.
            "flash": request.session.pop("flash", None),
            # A pessoa.
            "person": person,
            # Empresa dela.
            "company": db.get(Company, person.company_id),
            # Projetos dela.
            "memberships": projects_by_person(db).get(person.id, []),
            # Total de falas reconhecidas.
            "spoken": db.scalar(
                select(func.count())
                .select_from(Utterance)
                .where(Utterance.stakeholder_id == person.id)
            ),
            # Últimas 20 falas, mais recentes primeiro.
            "recent": db.scalars(
                select(Utterance)
                .where(Utterance.stakeholder_id == person.id)
                .order_by(Utterance.bronze_item_id.desc(), Utterance.position.desc())
                .limit(20)
            ).all(),
        },
    )


# Atualiza uma pessoa.
@router.post("/cadastros/pessoas/{person_id}", dependencies=[SameOrigin])
def edit_person(
    request: Request,
    owner: Owner,
    db: DB,
    person_id: int,
    # Campo "nome" do formulário.
    nome: Text,
    # E-mail.
    email: Text = "",
    # Cargo.
    cargo: Text = "",
    # Organização ou área (texto livre; não é a Empresa cliente).
    organizacao: Text = "",
    # Outros nomes, separados por vírgula.
    outros_nomes: Text = "",
    # Caixa "liderança na organização".
    lider_empresa: Text = "",
):
    # Endereço da página da pessoa.
    url = f"/cadastros/pessoas/{person_id}"
    # Tenta.
    try:
        # Atualiza.
        save_person(
            db,
            person_id,
            # Nome.
            name=nome,
            # E-mail.
            email=email,
            # Organização ou área.
            organization=organizacao,
            # Cargo na empresa.
            role=cargo,
            # Liderança na empresa.
            is_leader=_checked(lider_empresa),
            # Outros nomes.
            aliases=outros_nomes,
        )
        # Grava.
        db.commit()
    # Erro.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, url, str(exc), error=True)
    # Sucesso.
    return _back(request, url, "Pessoa salva. Falas e tags foram conferidas de novo.")


# Reaplica as regras de tag e religa as falas (depois de uma mudança grande).
@router.post("/cadastros/reaplicar", dependencies=[SameOrigin])
def reapply(request: Request, owner: Owner, db: DB, empresa: Text = ""):
    # Religa falas e reaplica tags (só da Empresa do filtro, ou de todas).
    linked, tagged = refresh(db, filter_id(empresa))
    # Grava.
    db.commit()
    # Volta.
    return _back(
        request,
        _registry(empresa, "pessoas"),
        f"{linked} fala(s) ligada(s) a pessoas e {tagged} tag(s) nova(s).",
    )


# Cria uma tag.
@router.post("/cadastros/tags", dependencies=[SameOrigin])
def new_tag(request: Request, owner: Owner, db: DB, nome: Text, empresa: Text = ""):
    # Tenta.
    try:
        # Empresa escolhida (sem ela, não cadastra).
        company = parse_company(db, empresa)
        # Cria.
        tag = create_tag(db, company.id, nome)
        # Grava.
        db.commit()
    # Sem Empresa, nome vazio ou repetido.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, _registry(empresa, "tags"), str(exc), error=True)
    # Sucesso.
    return _back(request, _registry(empresa, "tags"), f"Tag “{tag.name}” criada.")


# Renomeia uma tag.
@router.post("/cadastros/tags/{tag_id}/renomear", dependencies=[SameOrigin])
def rename(request: Request, owner: Owner, db: DB, tag_id: int, nome: Text):
    # Tenta.
    try:
        # Renomeia.
        tag = rename_tag(db, tag_id, nome)
        # Grava.
        db.commit()
    # Tag inexistente, nome repetido ou tag de projeto.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, "/cadastros#tags", str(exc), error=True)
    # Sucesso.
    return _back(request, "/cadastros#tags", f"Tag renomeada para “{tag.name}”.")


# Apaga uma tag (some de pastas, arquivos, e-mails e transcrições).
@router.post("/cadastros/tags/{tag_id}/apagar", dependencies=[SameOrigin])
def remove_tag(request: Request, owner: Owner, db: DB, tag_id: int):
    # Tenta.
    try:
        # Apaga.
        delete_tag(db, tag_id)
        # Grava.
        db.commit()
    # Tag de projeto.
    except ValueError as exc:
        # Desfaz.
        db.rollback()
        # Volta com o erro.
        return _back(request, "/cadastros#tags", str(exc), error=True)
    # Sucesso.
    return _back(request, "/cadastros#tags", "Tag apagada de pastas, arquivos, e-mails e reuniões.")
