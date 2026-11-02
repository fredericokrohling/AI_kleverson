"""Cadastro de projetos e pessoas (página Cadastros).

Regras:
- criar um projeto cria a tag de mesmo nome (ou usa a tag que já existe com esse nome);
  renomear o projeto renomeia a tag;
- uma pessoa é única pelo e-mail: a mesma pessoa pode estar em vários projetos, com uma
  função e uma marca de liderança diferentes em cada um;
- "liderança na empresa" é da pessoa; "liderança no projeto" é da pessoa naquele projeto;
- depois de qualquer mudança, as falas sem pessoa são reconhecidas de novo e as regras de
  tag rodam de novo (um nome novo pode reconhecer falas e e-mails antigos).
"""

# Consultas.
from sqlalchemy import delete, func, select

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Tabelas e auditoria.
from app.models import Project, ProjectStakeholder, Stakeholder, audit

# Reconhecimento de quem falou.
from app.people import relink_utterances

# Regras de tag.
from app.project_tags import apply_rules_to_all

# Tags.
from app.tags import find_or_create_tag, rename_tag


# Separa "a, b; c" numa lista limpa, sem repetição.
def split_list(value: str | None) -> list[str]:
    # Troca ";" por "," e separa.
    parts = (value or "").replace(";", ",").split(",")
    # Sem espaços nas pontas, sem vazios, sem repetição (mantendo a ordem).
    return list(dict.fromkeys(" ".join(p.split()) for p in parts if p.strip()))


# Texto limpo, ou None se vazio.
def _clean(value: str | None, size: int) -> str | None:
    # Junta espaços e corta ao tamanho da coluna.
    text = " ".join((value or "").split())[:size]
    # Vazio vira None.
    return text or None


# Depois de mudar projetos ou pessoas: religa falas e reaplica as regras de tag.
def refresh(session: Session) -> tuple[int, int]:
    """(falas religadas, tags novas)."""
    # Envia as mudanças ao banco antes de consultar.
    session.flush()
    # Falas sem pessoa.
    linked = relink_utterances(session)
    # Tags.
    tagged = apply_rules_to_all(session)
    # Devolve.
    return linked, tagged


# Cria um projeto e a tag dele.
def create_project(
    session: Session, name: str, code: str = "", aliases: str = "", keywords: str = ""
) -> Project:
    """Erros (nome vazio ou repetido, tag de outro projeto) viram ValueError."""
    # Nome limpo.
    name = _clean(name, 200)
    # Vazio.
    if not name:
        raise ValueError("Informe o nome do projeto.")
    # Já existe projeto com esse nome.
    if session.scalar(select(Project.id).where(func.lower(Project.name) == name.lower())):
        raise ValueError(f"O projeto “{name}” já existe.")
    # Código limpo.
    code = _clean(code, 50)
    # Código repetido.
    if code and session.scalar(select(Project.id).where(func.lower(Project.code) == code.lower())):
        raise ValueError(f"O código “{code}” já é de outro projeto.")
    # Tag de mesmo nome (nova ou existente).
    tag = find_or_create_tag(session, name)
    # A tag já é de outro projeto (nome igual com outras maiúsculas).
    if session.scalar(select(Project.id).where(Project.tag_id == tag.id)):
        raise ValueError(f"A tag “{tag.name}” já é de outro projeto.")
    # Projeto novo.
    project = Project(
        # Nome.
        name=name,
        # Código.
        code=code,
        # Outros nomes.
        aliases=split_list(aliases),
        # Palavras-chave.
        keywords=split_list(keywords),
        # Tag.
        tag_id=tag.id,
    )
    # Prepara.
    session.add(project)
    # Gera o id.
    session.flush()
    # Auditoria.
    audit(session, "dona", "projeto.criar", target=name)
    # Reaplica as regras (e-mails antigos que citam o projeto ganham a tag).
    refresh(session)
    # Devolve.
    return project


# Atualiza um projeto.
def update_project(
    session: Session,
    project_id: int,
    *,
    # Nome.
    name: str,
    # Código.
    code: str,
    # Outros nomes, separados por vírgula.
    aliases: str,
    # Palavras-chave, separadas por vírgula.
    keywords: str,
    # Ativo?
    active: bool,
) -> Project:
    # Busca.
    project = session.get(Project, project_id)
    # Não existe.
    if project is None:
        raise ValueError("Projeto não encontrado.")
    # Nome limpo.
    clean_name = _clean(name, 200)
    # Vazio.
    if not clean_name:
        raise ValueError("Informe o nome do projeto.")
    # Outro projeto com esse nome.
    other = session.scalar(
        select(Project.id).where(
            func.lower(Project.name) == clean_name.lower(), Project.id != project_id
        )
    )
    # Conflito.
    if other:
        raise ValueError(f"O projeto “{clean_name}” já existe.")
    # Código limpo.
    clean_code = _clean(code, 50)
    # Outro projeto com esse código.
    if clean_code and session.scalar(
        select(Project.id).where(
            func.lower(Project.code) == clean_code.lower(), Project.id != project_id
        )
    ):
        raise ValueError(f"O código “{clean_code}” já é de outro projeto.")
    # Nome mudou e há tag: renomeia junto.
    if clean_name != project.name and project.tag_id:
        rename_tag(session, project.tag_id, clean_name, allow_project=True)
    # Atualiza os campos.
    project.name, project.code, project.active = clean_name, clean_code, active
    # Listas.
    project.aliases, project.keywords = split_list(aliases), split_list(keywords)
    # Auditoria.
    audit(session, "dona", "projeto.alterar", target=clean_name)
    # Reaplica.
    refresh(session)
    # Devolve.
    return project


# Cria ou atualiza uma pessoa (única pelo e-mail).
def save_person(
    session: Session,
    person_id: int | None,
    *,
    # Nome.
    name: str,
    # E-mail.
    email: str = "",
    # Empresa.
    organization: str = "",
    # Cargo na empresa.
    role: str = "",
    # Liderança na empresa.
    is_leader: bool = False,
    # Outros nomes, separados por vírgula.
    aliases: str = "",
) -> Stakeholder:
    """person_id None = pessoa nova, ou a existente com o mesmo e-mail."""
    # Nome limpo.
    clean_name = _clean(name, 200)
    # Vazio.
    if not clean_name:
        raise ValueError("Informe o nome da pessoa.")
    # E-mail em minúsculas.
    clean_email = (_clean(email, 320) or "").lower() or None
    # E-mail obviamente inválido.
    if clean_email and "@" not in clean_email:
        raise ValueError(f"E-mail inválido: {clean_email}")
    # Pessoa com esse e-mail.
    same_email = (
        session.scalar(select(Stakeholder).where(func.lower(Stakeholder.email) == clean_email))
        if clean_email
        else None
    )
    # Pessoa pedida pelo id.
    person = session.get(Stakeholder, person_id) if person_id else same_email
    # Id informado e inexistente.
    if person_id and person is None:
        raise ValueError("Pessoa não encontrada.")
    # E-mail já é de outra pessoa.
    if same_email is not None and person is not None and same_email.id != person.id:
        raise ValueError(f"O e-mail {clean_email} já é de {same_email.name}.")
    # Pessoa nova.
    if person is None:
        # Cria.
        person = Stakeholder()
        # Prepara.
        session.add(person)
    # Campos.
    person.name, person.email = clean_name, clean_email
    # Empresa e cargo.
    person.organization, person.role = _clean(organization, 200), _clean(role, 200)
    # Liderança e outros nomes.
    person.is_leader, person.aliases = is_leader, split_list(aliases)
    # Gera o id.
    session.flush()
    # Auditoria.
    audit(session, "dona", "pessoa.salvar", target=clean_name)
    # Religa falas e reaplica tags.
    refresh(session)
    # Devolve.
    return person


# Põe (ou atualiza) uma pessoa na equipe de um projeto.
def set_member(
    session: Session, project_id: int, person_id: int, role_in_project: str, is_leader: bool
) -> None:
    # Projeto existe?
    if session.get(Project, project_id) is None:
        raise ValueError("Projeto não encontrado.")
    # Pessoa existe?
    if session.get(Stakeholder, person_id) is None:
        raise ValueError("Pessoa não encontrada.")
    # Vínculo existente.
    link = session.get(ProjectStakeholder, (project_id, person_id))
    # Novo.
    if link is None:
        # Cria.
        link = ProjectStakeholder(project_id=project_id, stakeholder_id=person_id)
        # Prepara.
        session.add(link)
    # Função e liderança no projeto.
    link.role_in_project, link.is_leader = _clean(role_in_project, 200), is_leader
    # Reaplica as regras (remetente da equipe).
    refresh(session)


# Tira uma pessoa da equipe (a pessoa continua cadastrada).
def remove_member(session: Session, project_id: int, person_id: int) -> None:
    # Apaga o vínculo.
    session.execute(
        delete(ProjectStakeholder).where(
            ProjectStakeholder.project_id == project_id,
            ProjectStakeholder.stakeholder_id == person_id,
        )
    )


# Equipe de um projeto: [(pessoa, vínculo)], lideranças primeiro.
def team_of(session: Session, project_id: int) -> list[tuple[Stakeholder, ProjectStakeholder]]:
    # Pessoas e vínculos.
    rows = session.execute(
        select(Stakeholder, ProjectStakeholder)
        .join(ProjectStakeholder, ProjectStakeholder.stakeholder_id == Stakeholder.id)
        .where(ProjectStakeholder.project_id == project_id)
        .order_by(ProjectStakeholder.is_leader.desc(), Stakeholder.name)
    ).all()
    # Tuplas simples.
    return [(person, link) for person, link in rows]


# Projetos de cada pessoa: id da pessoa → [(projeto, vínculo)].
def projects_by_person(session: Session) -> dict[int, list[tuple[Project, ProjectStakeholder]]]:
    # Vínculos com o projeto.
    rows = session.execute(
        select(ProjectStakeholder, Project)
        .join(Project, Project.id == ProjectStakeholder.project_id)
        .order_by(Project.name)
    ).all()
    # Agrupa.
    result: dict[int, list[tuple[Project, ProjectStakeholder]]] = {}
    # Cada vínculo.
    for link, project in rows:
        # Acrescenta.
        result.setdefault(link.stakeholder_id, []).append((project, link))
    # Devolve.
    return result
