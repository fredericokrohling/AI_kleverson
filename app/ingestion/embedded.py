"""Imagens embutidas em PowerPoint e Word: viram itens da fila de leitura do Claude.

Por que existe: num deck de status ou numa especificação, o diagrama de arquitetura e o
print do cronograma costumam estar colados como imagem. O extrator de texto
(extractors.py) lê só as caixas de texto; sem este módulo, essas imagens ficam de fora.

Como funciona: ao gravar um .pptx ou .docx (anexo de e-mail ou versão do Drive), cada
imagem de dentro dele vira uma linha em `attachments`, origem "imagem_embutida", ligada ao
mesmo item do bronze (o e-mail ou a versão do arquivo). A linha nasce com o código de
espera IMAGEM_SEM_OCR e a fila do Claude (vision_queue.py) lê como lê qualquer imagem.

Regras para não gastar à toa (além das da fila):
- a mesma imagem repetida no arquivo (o logo em todos os slides) vira uma linha só;
- ícones (lado maior abaixo de 200 pixels) e formatos que não abrem são ignorados aqui,
  sem aviso: não são conteúdo e encheriam a lista de "arquivos sem texto";
- no máximo MAX_IMAGES_PER_FILE imagens por arquivo;
- imagem já lida antes (mesma impressão digital, em qualquer lugar) já nasce com o texto.

O nome da linha diz de onde a imagem veio ("Status.pptx › slide 3 › image2.png"), para
você e a IA do bloco 3 saberem em que slide ela está.
"""

# io.BytesIO faz bytes na memória se comportarem como um arquivo aberto.
import io

# Path pega o nome do arquivo de dentro do pacote do Word.
from pathlib import Path

# Consultas.
from sqlalchemy import select

# Session é a conversa com o banco; sessionmaker é a fábrica de sessões.
from sqlalchemy.orm import Session, sessionmaker

# Extração (para conferir a imagem) e detecção de formato.
from app.ingestion.extractors import detect_kind, extract_text

# Guarda dos originais e impressão digital.
from app.ingestion.storage import FileStore, safe_suffix, sha256_hex

# Regra de "está na fila do Claude".
from app.ingestion.vision import is_pending

# Tabelas usadas.
from app.models import Attachment, BronzeItem, DriveItem

# Origem gravada nas linhas de imagem embutida.
ORIGIN = "imagem_embutida"
# Máximo de imagens lidas por arquivo (um deck de 60 slides com prints custaria caro).
MAX_IMAGES_PER_FILE = 20


# Lista as imagens de dentro de um arquivo do Office: (nome, bytes), sem repetição.
def find_images(
    data: bytes, filename: str | None, mime_type: str | None
) -> list[tuple[str, bytes]]:
    """Imagens de um .pptx ou .docx, na ordem do documento. Outros formatos: lista vazia."""
    # Formato.
    kind = detect_kind(filename, mime_type)
    # Tenta abrir.
    try:
        # PowerPoint.
        if kind == "pptx":
            # Imagens slide a slide.
            found = _pptx_images(data)
        # Word.
        elif kind == "docx":
            # Imagens do corpo do documento.
            found = _docx_images(data)
        # Outros formatos não têm imagens a procurar.
        else:
            return []
    # Arquivo corrompido: o extrator de texto já registrou o motivo no próprio arquivo.
    except Exception:
        return []
    # Impressões digitais já vistas.
    seen: set[str] = set()
    # Resultado sem repetição.
    unique: list[tuple[str, bytes]] = []
    # Percorre na ordem.
    for name, blob in found:
        # Impressão digital da imagem.
        digest = sha256_hex(blob)
        # Já apareceu (o mesmo logo em vários slides): pula.
        if digest in seen:
            continue
        # Marca.
        seen.add(digest)
        # Guarda.
        unique.append((name, blob))
    # Devolve.
    return unique


# PowerPoint: imagens de cada slide, inclusive dentro de grupos de formas.
def _pptx_images(data: bytes) -> list[tuple[str, bytes]]:
    # Import local: só carrega se aparecer um PowerPoint.
    from pptx import Presentation

    # Tipo "grupo de formas".
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    # Resultado.
    found: list[tuple[str, bytes]] = []

    # Percorre as formas; um grupo tem formas dentro, então a função chama a si mesma.
    def walk(shapes, slide_number: int) -> None:
        # Cada forma.
        for shape in shapes:
            # Grupo: desce nele.
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                # Formas do grupo.
                walk(shape.shapes, slide_number)
                # Próxima forma.
                continue
            # Só figuras têm o atributo "image"; as outras formas levantam erro.
            try:
                # A imagem da figura.
                image = shape.image
            # Não é figura (caixa de texto, tabela, gráfico...).
            except (AttributeError, KeyError, ValueError):
                continue
            # Nome com o número do slide.
            found.append((f"slide {slide_number} › {image.filename}", image.blob))

    # Abre a apresentação.
    presentation = Presentation(io.BytesIO(data))
    # Numera os slides a partir de 1.
    for number, slide in enumerate(presentation.slides, start=1):
        # Formas do slide.
        walk(slide.shapes, number)
    # Devolve.
    return found


# Word: imagens ligadas ao corpo do documento (cabeçalho e rodapé ficam de fora: é onde
# costuma estar o logo da empresa).
def _docx_images(data: bytes) -> list[tuple[str, bytes]]:
    # Import local: só carrega se aparecer um Word.
    import docx

    # Tipos de ligação do pacote do Office (aqui interessa a de imagem).
    from docx.opc.constants import RELATIONSHIP_TYPE

    # Abre o documento.
    document = docx.Document(io.BytesIO(data))
    # Resultado.
    found: list[tuple[str, bytes]] = []
    # Cada ligação do corpo do documento com outra parte do pacote.
    for rel in document.part.rels.values():
        # Só imagens guardadas dentro do arquivo (não links para fora).
        if rel.reltype == RELATIONSHIP_TYPE.IMAGE and not rel.is_external:
            # Nome do arquivo dentro do pacote (ex.: image1.png).
            name = Path(str(rel.target_part.partname)).name
            # Guarda com a numeração.
            found.append((f"imagem {len(found) + 1} › {name}", rel.target_part.blob))
    # Devolve.
    return found


# Monta as linhas de imagem embutida de um arquivo do Office (sem gravar: quem chama liga
# ao item do bronze e grava).
def embedded_rows(
    session: Session, store: FileStore, parent_name: str, data: bytes, mime_type: str | None
) -> list[Attachment]:
    """Uma linha por imagem que vale a leitura, até MAX_IMAGES_PER_FILE."""
    # Impressão digital do arquivo do Office: liga a imagem ao arquivo de onde ela saiu.
    parent_sha = sha256_hex(data)
    # Linhas.
    rows: list[Attachment] = []
    # Cada imagem.
    for name, blob in find_images(data, parent_name, mime_type):
        # Confere a imagem (tamanho e formato) pelo mesmo extrator dos anexos.
        check = extract_text(blob, name, None)
        # Ícone, formato ilegível ou vetorial (EMF): ignora sem aviso.
        if not is_pending(check.error):
            continue
        # Chegou ao limite do arquivo.
        if len(rows) >= MAX_IMAGES_PER_FILE:
            break
        # Impressão digital da imagem.
        digest = sha256_hex(blob)
        # Mesma imagem já lida pelo Claude em outro lugar: reaproveita o texto.
        previous = session.scalars(
            select(Attachment)
            .where(Attachment.content_sha256 == digest, Attachment.extracted_text.is_not(None))
            .limit(1)
        ).first()
        # Linha nova.
        rows.append(
            Attachment(
                # Arquivo e posição da imagem (cortado ao tamanho da coluna).
                filename=f"{parent_name} › {name}"[:500],
                # Original da imagem no disco.
                object_key=store.save(blob, safe_suffix(name)),
                # Texto reaproveitado, se houver.
                extracted_text=previous.extracted_text if previous else None,
                # Na fila do Claude, a menos que o texto já exista.
                extraction_error=None if previous else check.error,
                # Origem.
                origin=ORIGIN,
                # Arquivo do Office de onde saiu.
                external_ref=parent_sha,
                # Tamanho.
                size_bytes=len(blob),
                # Impressão digital.
                content_sha256=digest,
            )
        )
    # Devolve.
    return rows


# Diz se as imagens de um arquivo já foram registradas para um item do bronze.
def _already_done(session: Session, bronze_item_id: int, parent_sha: str) -> bool:
    # Procura uma linha de imagem embutida desse arquivo nesse item.
    return (
        session.scalar(
            select(Attachment.id)
            .where(
                Attachment.bronze_item_id == bronze_item_id,
                Attachment.origin == ORIGIN,
                Attachment.external_ref == parent_sha,
            )
            .limit(1)
        )
        is not None
    )


# Grava as imagens de um arquivo para um item do bronze. Devolve quantas foram gravadas.
def add_embedded(
    session: Session,
    store: FileStore,
    bronze_item_id: int,
    parent_name: str,
    data: bytes,
    mime_type: str | None,
) -> int:
    # Já registradas antes: não duplica.
    if _already_done(session, bronze_item_id, sha256_hex(data)):
        return 0
    # Linhas novas.
    rows = embedded_rows(session, store, parent_name, data, mime_type)
    # Cada linha.
    for row in rows:
        # Liga ao item.
        row.bronze_item_id = bronze_item_id
        # Prepara a gravação.
        session.add(row)
    # Quantas.
    return len(rows)


# Para os arquivos que chegaram antes desta versão: procura imagens nos PowerPoint e Word
# já guardados (anexos e versão atual dos arquivos do Drive).
def backfill(sessions: sessionmaker[Session], store: FileStore) -> int:
    """Registra as imagens embutidas dos arquivos já coletados. Devolve quantas."""
    # Contador.
    total = 0
    # Uma sessão.
    with sessions() as session:
        # Anexos de e-mail com original guardado.
        attachments = session.scalars(
            select(Attachment).where(
                Attachment.origin == "anexo", Attachment.object_key.is_not(None)
            )
        ).all()
        # Cada anexo do Office.
        for row in attachments:
            # Só PowerPoint e Word.
            if detect_kind(row.filename, row.mime_type) in {"pptx", "docx"}:
                # Registra as imagens.
                total += add_embedded(
                    session,
                    store,
                    row.bronze_item_id,
                    row.filename,
                    store.read(row.object_key),
                    row.mime_type,
                )
        # Versão atual de cada arquivo do Drive.
        pairs = session.execute(
            select(DriveItem, BronzeItem).join(
                BronzeItem, BronzeItem.id == DriveItem.bronze_item_id
            )
        ).all()
        # Cada arquivo.
        for item, bronze in pairs:
            # Original guardado.
            key = (bronze.extra or {}).get("arquivo")
            # Só PowerPoint e Word (a extensão do original diz o formato).
            if key and detect_kind(key, None) in {"pptx", "docx"}:
                # Registra as imagens.
                total += add_embedded(session, store, bronze.id, item.name, store.read(key), None)
        # Grava.
        session.commit()
    # Devolve.
    return total
