"""Tags: etiquetas que dizem a que um conteúdo do Drive se refere ("Projeto CRM").

Regras:
- Uma tag colocada numa pasta vale para tudo dentro dela, em qualquer nível (herança).
- Um item pode ter várias tags, próprias e herdadas.
- A herança não é copiada para os itens: a view drive_item_effective_tags (migração
  0004) calcula na hora da consulta, subindo pelas pastas-mãe.

Por que calcular em vez de copiar: com cópia, renomear, apagar ou tirar uma tag de uma
pasta exigiria atualizar milhares de linhas, e um arquivo novo precisaria "lembrar" de
copiar as tags das pastas acima. Com a view, cada mudança é uma linha só, e um arquivo
novo já nasce com as tags das pastas acima, sem código extra. O custo é uma consulta um
pouco mais pesada, irrelevante no volume de uma pessoa.
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Funções de consulta do SQLAlchemy; text permite escrever SQL à mão.
from sqlalchemy import bindparam, delete, func, select, text

# Session é a conversa com o banco.
from sqlalchemy.orm import Session

# Tabelas usadas.
from app.models import DriveItem, DriveItemTag, Tag, audit

# Tamanho máximo do nome (igual à coluna tags.name).
_MAX_NAME = 100


# Uma tag de um item, como aparece na tela.
@dataclass(frozen=True)
class ItemTag:
    # Id da tag.
    tag_id: int
    # Nome.
    name: str
    # True = vem de uma pasta acima; False = colocada no próprio item.
    inherited: bool


# Limpa e valida um nome de tag.
def _clean_name(name: str) -> str:
    # Junta espaços repetidos e tira os das pontas.
    name = " ".join(name.split())
    # Vazio.
    if not name:
        raise ValueError("Informe um nome para a tag.")
    # Longo demais.
    if len(name) > _MAX_NAME:
        raise ValueError(f"Nome de tag com mais de {_MAX_NAME} caracteres.")
    # Nome limpo.
    return name


# Procura uma tag pelo nome, sem diferenciar maiúsculas ("crm" acha "CRM").
def _find_by_name(session: Session, name: str) -> Tag | None:
    # lower() dos dois lados.
    return session.scalars(select(Tag).where(func.lower(Tag.name) == name.lower())).first()


# Lista todas as tags com quantos itens têm cada uma diretamente.
def list_tags(session: Session) -> list[tuple[Tag, int]]:
    """[(tag, número de itens com a tag colocada diretamente)], em ordem alfabética."""
    # LEFT JOIN: tags sem uso aparecem com 0.
    rows = session.execute(
        select(Tag, func.count(DriveItemTag.drive_item_id))
        .outerjoin(DriveItemTag, DriveItemTag.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(func.lower(Tag.name))
    ).all()
    # Converte cada linha numa tupla simples.
    return [(tag, count) for tag, count in rows]


# Cria uma tag.
def create_tag(session: Session, name: str) -> Tag:
    """Cria a tag. Nome repetido (mesmo com outras maiúsculas) vira ValueError."""
    # Valida.
    name = _clean_name(name)
    # Já existe.
    if _find_by_name(session, name):
        raise ValueError(f"A tag “{name}” já existe.")
    # Nova linha.
    tag = Tag(name=name)
    # Prepara.
    session.add(tag)
    # Gera o id.
    session.flush()
    # Auditoria.
    audit(session, "dona", "tag.criar", target=name)
    # Devolve.
    return tag


# Renomeia uma tag; vale na hora para todos os itens (pastas e arquivos) que a usam.
def rename_tag(session: Session, tag_id: int, name: str) -> Tag:
    """Troca o nome. Erros (tag inexistente, nome repetido) viram ValueError."""
    # Busca.
    tag = session.get(Tag, tag_id)
    # Não existe.
    if tag is None:
        raise ValueError("Tag não encontrada.")
    # Valida o novo nome.
    name = _clean_name(name)
    # Outra tag já tem esse nome.
    other = _find_by_name(session, name)
    # Só é conflito se for outra tag (trocar maiúsculas da própria pode).
    if other is not None and other.id != tag.id:
        raise ValueError(f"A tag “{name}” já existe.")
    # Nome antigo, para a auditoria.
    old = tag.name
    # Troca.
    tag.name = name
    # Auditoria.
    audit(session, "dona", "tag.renomear", target=str(tag.id), de=old, para=name)
    # Devolve.
    return tag


# Apaga uma tag; some de todos os itens (o banco apaga os vínculos em cascata).
def delete_tag(session: Session, tag_id: int) -> None:
    """Apaga a tag e todos os vínculos dela."""
    # Busca.
    tag = session.get(Tag, tag_id)
    # Já não existe: nada a fazer.
    if tag is None:
        return
    # Auditoria antes de apagar (depois o nome não existe mais).
    audit(session, "dona", "tag.apagar", target=tag.name)
    # Apaga os vínculos explicitamente (a sessão não conhece o CASCADE do banco).
    session.execute(delete(DriveItemTag).where(DriveItemTag.tag_id == tag_id))
    # Apaga a tag.
    session.delete(tag)


# Coloca uma tag num item; cria a tag se o nome for novo.
def assign_tag(session: Session, item_id: int, name: str) -> Tag:
    """Coloca a tag no arquivo ou pasta. Já colocada: nada muda."""
    # Item existe?
    item = session.get(DriveItem, item_id)
    # Não existe.
    if item is None:
        raise ValueError("Item do Drive não encontrado.")
    # Valida o nome.
    name = _clean_name(name)
    # Tag existente com esse nome, ou uma nova.
    tag = _find_by_name(session, name) or create_tag(session, name)
    # Já colocada diretamente neste item?
    if session.get(DriveItemTag, (item.id, tag.id)) is None:
        # Cria o vínculo.
        session.add(DriveItemTag(drive_item_id=item.id, tag_id=tag.id))
        # Auditoria.
        audit(session, "dona", "tag.colocar", target=item.path or item.name, tag=tag.name)
    # Devolve a tag.
    return tag


# Tira uma tag colocada diretamente num item (as herdadas se tiram na pasta de cima).
def unassign_tag(session: Session, item_id: int, tag_id: int) -> None:
    """Remove o vínculo direto. Não mexe em tags herdadas."""
    # Vínculo.
    link = session.get(DriveItemTag, (item_id, tag_id))
    # Não existe: nada a fazer.
    if link is None:
        return
    # Item e tag, para a auditoria.
    item, tag = session.get(DriveItem, item_id), session.get(Tag, tag_id)
    # Auditoria.
    audit(
        session,
        "dona",
        "tag.retirar",
        target=(item.path or item.name) if item else str(item_id),
        tag=tag.name if tag else str(tag_id),
    )
    # Apaga o vínculo.
    session.delete(link)


# Consulta das tags efetivas de vários itens de uma vez (usa a view da migração 0004).
_EFFECTIVE = text(
    "SELECT e.drive_item_id, t.id, t.name, e.herdada "
    "FROM drive_item_effective_tags e JOIN tags t ON t.id = e.tag_id "
    "WHERE e.drive_item_id IN :ids ORDER BY lower(t.name)"
).bindparams(bindparam("ids", expanding=True))


# Tags efetivas (próprias e herdadas) de cada item pedido.
def effective_tags(session: Session, item_ids: list[int]) -> dict[int, list[ItemTag]]:
    """{id do item: [tags]}; itens sem tag ficam com lista vazia."""
    # Começa com listas vazias.
    result: dict[int, list[ItemTag]] = {i: [] for i in item_ids}
    # Nada pedido: evita um IN vazio.
    if not item_ids:
        return result
    # Cada linha da consulta.
    for item_id, tag_id, name, inherited in session.execute(_EFFECTIVE, {"ids": item_ids}):
        # Acrescenta à lista do item.
        result[item_id].append(ItemTag(tag_id, name, inherited))
    # Devolve.
    return result
