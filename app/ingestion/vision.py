"""Leitura de imagens e PDFs digitalizados pelo Claude (visão).

Por que o Claude e não um OCR (Tesseract): o que chega aqui é, na maior parte, diagrama
exportado do Lucid ou do Canva, print de tela e documento digitalizado. O OCR devolve as
palavras soltas e perde o desenho (quem liga em quem, a ordem das etapas). O Claude
transcreve o texto e descreve a estrutura. O preço: custa dinheiro por arquivo e pode
errar uma leitura; por isso cada texto começa com um aviso de que foi lido pelo Claude.

Por que numa etapa separada da coleta: a coleta do Gmail e do Drive não pode depender da
API da Anthropic. Se a API cair, ou o teto mensal acabar, os e-mails continuam entrando;
as imagens ficam na fila (códigos IMAGEM_SEM_OCR e PDF_SEM_TEXTO) e são lidas depois,
pela rodada do coletor (vision_queue.py) ou pelo "Tentar de novo" do console.

Limites da API respeitados aqui:
- imagem: até 2576 pixels no lado maior e 5 MB; reduzimos antes de enviar;
- PDF: o pedido inteiro tem até 32 MB; limitamos o arquivo a 20 MB (vira ~27 MB em
  base64) e a VISION_MAX_PDF_PAGES páginas, porque o custo cresce por página.
"""

# base64 transforma bytes em texto, o formato que a API aceita para arquivos.
import base64

# io.BytesIO faz bytes na memória se comportarem como um arquivo aberto.
import io

# math.sqrt calcula a redução proporcional da imagem.
import math

# SDK da Anthropic, para reconhecer os erros da API.
import anthropic

# Configuração.
from app.config import Settings

# Extração comum, mensagens com código e detecção de formato.
from app.ingestion.extractors import Extraction, coded, detect_kind, extract_text, split_error

# Cliente do Claude (com registro de custo e teto) e o erro de teto atingido.
from app.llm import BudgetExceededError, LLMClient

# Códigos que significam "aguardando leitura pelo Claude".
PENDING_CODES = frozenset({"IMAGEM_SEM_OCR", "PDF_SEM_TEXTO"})

# Lado maior máximo de uma imagem nos modelos atuais, em pixels.
_MAX_SIDE = 2576
# Total máximo de pixels de uma imagem (3,75 megapixels).
_MAX_PIXELS = 3_750_000
# Tamanho máximo da imagem enviada: 3,7 MB viram ~4,9 MB em base64 (limite da API: 5 MB).
_MAX_IMAGE_BYTES = 3_700_000
# Tamanho máximo do PDF enviado: 20 MB viram ~27 MB em base64 (limite do pedido: 32 MB).
_MAX_PDF_BYTES = 20 * 1024 * 1024
# Limite de tamanho da resposta, em tokens (cerca de 30 páginas transcritas).
_MAX_TOKENS = 16000

# Instrução fixa enviada em toda leitura. Em português, porque o texto vai para você.
_SYSTEM = """Você transcreve arquivos para o assistente de uma gerente de projetos.
O resultado é guardado como texto e lido depois por outra IA, que responde perguntas
sobre os projetos. Devolva só o conteúdo do arquivo, sem comentar a tarefa.

Regras:
1. Transcreva todo o texto legível, na ordem de leitura, mantendo títulos e listas.
   Não traduza: mantenha o idioma original.
2. Tabelas: uma linha por linha da tabela, com as células separadas por " | ".
3. Diagramas, fluxogramas, organogramas e cronogramas: depois do texto, escreva
   "Estrutura:" e descreva os elementos e as ligações (ex.: "Cadastro → Aprovação"),
   com responsáveis, datas e status quando aparecerem.
4. Foto sem texto: descreva em uma ou duas frases o que aparece.
5. Não invente nem complete: trecho ilegível vira [ilegível].
6. PDF: comece cada página com "### Página N"."""


# Diz se o erro gravado significa "aguardando leitura pelo Claude".
def is_pending(error: str | None) -> bool:
    # Compara só o código, não a explicação (que pode mudar de redação).
    return split_error(error)[0] in PENDING_CODES


# Prepara uma imagem para a API: gira pela orientação da câmera, reduz e comprime.
def prepare_image(data: bytes) -> tuple[str, bytes]:
    """Devolve (tipo MIME, bytes) dentro dos limites da API. Erro se não abrir."""
    # Import local: o Pillow só carrega se aparecer uma imagem.
    from PIL import Image, ImageOps

    # Abre a imagem (em GIF animado ou TIFF de várias páginas, pega o primeiro quadro).
    with Image.open(io.BytesIO(data)) as original:
        # Gira conforme a orientação gravada pela câmera do celular; devolve uma cópia.
        image = ImageOps.exif_transpose(original)
    # Largura e altura.
    width, height = image.size
    # Fator de redução: respeita o lado maior e o total de pixels; nunca amplia.
    scale = min(1.0, _MAX_SIDE / max(width, height), math.sqrt(_MAX_PIXELS / (width * height)))
    # Precisa reduzir.
    if scale < 1:
        # LANCZOS é o filtro que melhor preserva letras pequenas ao reduzir.
        image = image.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS
        )
    # Imagem com transparência fica em RGBA; o resto vira RGB (formato que o PNG aceita).
    has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    # Converte.
    image = image.convert("RGBA" if has_alpha else "RGB")
    # Primeiro tenta PNG: sem perda, melhor para diagramas e prints com texto.
    buffer = io.BytesIO()
    # Grava em PNG com compressão máxima.
    image.save(buffer, format="PNG", optimize=True)
    # Coube no limite.
    if buffer.tell() <= _MAX_IMAGE_BYTES:
        return "image/png", buffer.getvalue()
    # Grande demais (costuma ser foto): JPEG, que comprime muito mais.
    if has_alpha:
        # O JPEG não tem transparência: põe a imagem sobre um fundo branco.
        background = Image.new("RGB", image.size, "white")
        # Cola usando a transparência como máscara.
        background.paste(image, mask=image.getchannel("A"))
        # Passa a usar o fundo branco.
        image = background
    # Qualidade 85 lê bem o texto; se ainda não couber, baixa a qualidade aos poucos.
    for quality in (85, 70, 50):
        # Novo buffer.
        buffer = io.BytesIO()
        # Grava em JPEG.
        image.save(buffer, format="JPEG", quality=quality)
        # Coube.
        if buffer.tell() <= _MAX_IMAGE_BYTES:
            break
    # Devolve (na pior das hipóteses, a de qualidade 50).
    return "image/jpeg", buffer.getvalue()


# Lê imagens e PDFs digitalizados com o Claude.
class VisionReader:
    # Recebe o cliente do Claude e a configuração.
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        # Cliente com registro de custo e teto mensal.
        self._llm = llm
        # Modelo da leitura; None = o escolhido no console.
        self._model = settings.vision_model or None
        # Máximo de páginas de PDF.
        self._max_pages = settings.vision_max_pdf_pages

    # Lê um arquivo. Erros viram mensagem com código; só o teto mensal levanta erro.
    def read(self, data: bytes, filename: str | None, mime_type: str | None) -> Extraction:
        """Devolve o texto lido pelo Claude. Levanta BudgetExceededError no teto mensal."""
        # Formato.
        kind = detect_kind(filename, mime_type)
        # Imagem.
        if kind == "imagem":
            # Monta o bloco da imagem (ou o erro).
            block, label, error = self._image_block(data)
        # PDF.
        elif kind == "pdf":
            # Monta o bloco do PDF (ou o erro).
            block, label, error = self._pdf_block(data)
        # Outro formato não tem o que ler aqui.
        else:
            # Erro.
            return Extraction(
                None, coded("TIPO_NAO_SUPORTADO", f"O Claude só lê imagem e PDF aqui ({kind}).")
            )
        # Não deu para montar o pedido.
        if error is not None:
            return Extraction(None, error)
        # Chama o Claude: o arquivo e o pedido na mesma mensagem.
        try:
            # Esforço baixo: transcrever não exige raciocínio longo e custa menos.
            result = self._llm.generate(
                purpose="leitura_visual",
                prompt=[block, {"type": "text", "text": f"Transcreva este {label}."}],
                system=_SYSTEM,
                max_tokens=_MAX_TOKENS,
                effort="low",
                model=self._model,
            )
        # Falha da API (rede, sobrecarga, chave inválida); o SDK já tentou de novo sozinho.
        except anthropic.APIError as exc:
            # Fica na lista para "Tentar de novo"; a rodada automática não insiste.
            return Extraction(
                None,
                coded(
                    "CLAUDE_FALHOU",
                    f"A API do Claude falhou ({type(exc).__name__}). Use Tentar de novo.",
                ),
            )
        # O modelo se recusou (política de segurança).
        if result.refused:
            # Erro.
            return Extraction(None, coded("CLAUDE_RECUSOU", "O Claude recusou ler este arquivo."))
        # Aviso de origem, para você e para a IA do bloco 3 saberem como o texto nasceu.
        note = (
            f"[Lido pelo Claude ({result.model}) a partir de {label}; "
            "confira números e nomes importantes no original.]"
        )
        # Resposta cortada pelo limite de tamanho.
        if result.stop_reason == "max_tokens":
            # Avisa que falta o fim.
            note += " [Texto cortado: o arquivo é longo demais para uma leitura só.]"
        # Aviso, linha em branco e o texto.
        return Extraction(f"{note}\n\n{result.text.strip()}")

    # Bloco de imagem para a API. Devolve (bloco, rótulo, erro).
    def _image_block(self, data: bytes) -> tuple[dict | None, str, str | None]:
        # Tenta preparar.
        try:
            # Tipo e bytes já reduzidos.
            media_type, prepared = prepare_image(data)
        # Formato que o Pillow não abre (ex.: HEIC) ou arquivo corrompido.
        except Exception as exc:
            # Erro com o que fazer.
            return (
                None,
                "imagem",
                coded(
                    "IMAGEM_FORMATO",
                    f"Formato de imagem não reconhecido ({type(exc).__name__}); "
                    "salve como PNG ou JPG na pasta monitorada.",
                ),
            )
        # Bloco no formato da API: a imagem em base64.
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(prepared).decode("ascii"),
            },
        }
        # Devolve.
        return block, "imagem", None

    # Bloco de PDF para a API. Devolve (bloco, rótulo, erro).
    def _pdf_block(self, data: bytes) -> tuple[dict | None, str, str | None]:
        # Import local: só carrega se aparecer um PDF.
        from pypdf import PdfReader

        # Arquivo grande demais para um pedido.
        if len(data) > _MAX_PDF_BYTES:
            # Erro com o tamanho.
            return (
                None,
                "PDF digitalizado",
                coded(
                    "CLAUDE_ARQUIVO_GRANDE",
                    f"PDF digitalizado de {len(data) // (1024 * 1024)} MB; o limite para o "
                    "Claude é 20 MB. Divida o arquivo em partes.",
                ),
            )
        # Conta as páginas.
        pages = len(PdfReader(io.BytesIO(data)).pages)
        # Páginas demais: o custo cresce por página, então há um limite configurável.
        if pages > self._max_pages:
            # Erro com o que fazer.
            return (
                None,
                "PDF digitalizado",
                coded(
                    "PDF_LONGO",
                    f"PDF digitalizado com {pages} páginas; o limite é {self._max_pages} "
                    "(VISION_MAX_PDF_PAGES no .env).",
                ),
            )
        # Bloco no formato da API: o PDF inteiro em base64.
        block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode("ascii"),
            },
        }
        # Devolve.
        return block, "PDF digitalizado", None


# Extrai com as bibliotecas e, se o arquivo estiver na fila do Claude, lê com o Claude.
def extract_or_read(
    data: bytes, filename: str | None, mime_type: str | None, vision: VisionReader | None
) -> tuple[Extraction, str | None]:
    """Usado pelo "Tentar de novo". Devolve (resultado a gravar, aviso que não é gravado).

    O aviso explica por que o Claude não leu agora (sem chave, teto do mês); o arquivo
    continua na fila com o código de espera.
    """
    # Primeiro as bibliotecas (rápido e de graça).
    result = extract_text(data, filename, mime_type)
    # Não é caso para o Claude: devolve o resultado como está.
    if not is_pending(result.error):
        return result, None
    # Sem chave da Anthropic configurada.
    if vision is None:
        # Fica na fila; o aviso diz o que falta.
        return result, coded(
            "CLAUDE_SEM_CHAVE", "Leitura pelo Claude desligada: falta ANTHROPIC_API_KEY no .env."
        )
    # Lê com o Claude.
    try:
        # Resultado novo.
        return vision.read(data, filename, mime_type), None
    # Teto do mês atingido: nada foi gasto; o arquivo fica na fila.
    except BudgetExceededError as exc:
        # Aviso.
        return result, coded(
            "CLAUDE_TETO_MENSAL", f"{exc} O arquivo fica na fila e é lido quando houver saldo."
        )


# Monta o leitor a partir da configuração; None se não houver chave da Anthropic.
def vision_reader(settings: Settings) -> VisionReader | None:
    # Sem chave, não há leitura.
    if not settings.anthropic_api_key:
        return None
    # Import local: evita carregar o banco ao importar este módulo.
    from app.db import get_sessionmaker

    # Leitor com o cliente real do Claude.
    return VisionReader(LLMClient(get_sessionmaker(), settings), settings)
