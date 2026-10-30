"""Peças falsas para testar a coleta sem acessar o Google de verdade.

Por que falsos: os testes rodam no CI, sem conta Google e sem internet. Estas classes
imitam só o pedaço das APIs do Gmail e do Drive que o coletor usa, com o mesmo formato
de chamada (gmail.users().messages().get(...).execute()).

Também ficam aqui os geradores de arquivos de exemplo (PDF, DOCX, XLSX, PPTX) e de
e-mails, montados na hora para cada teste.
"""

# base64 codifica o e-mail como o Gmail entrega.
import base64

# io.BytesIO: arquivo na memória, para salvar os documentos gerados.
import io

# EmailMessage monta e-mails no formato padrão.
from email.message import EmailMessage

# httplib2.Response imita a resposta HTTP que acompanha um erro da API do Google.
import httplib2

# HttpError é o erro que a biblioteca do Google levanta.
from googleapiclient.errors import HttpError


# Uma chamada preparada: imita o objeto que a biblioteca devolve antes do .execute().
class _Call:
    # Guarda a função que produz a resposta.
    def __init__(self, fn):
        # A função só roda no execute, como na biblioteca real.
        self.fn = fn

    # num_retries existe na biblioteca real; aqui é aceito e ignorado.
    def execute(self, num_retries: int = 0):
        # Produz a resposta (ou levanta o erro).
        return self.fn()


# Gmail falso, com as mensagens guardadas num dicionário.
class FakeGmail:
    # mensagens: id → {"raw": bytes do e-mail, "labels": [...], "thread": "..."}.
    def __init__(self, emails: dict[str, dict], account: str = "dona@exemplo.com"):
        # As mensagens disponíveis.
        self.emails = emails
        # Conta "conectada".
        self.account = account
        # Buscas recebidas, para os testes conferirem a janela.
        self.queries: list[str] = []
        # Ids baixados, para conferir que os repetidos não são baixados de novo.
        self.fetched: list[str] = []
        # Ids que devem falhar ao baixar (simula erro de rede).
        self.failing: set[str] = set()

    # gmail.users() devolve o próprio objeto (a cadeia de chamadas é só formato).
    def users(self):
        return self

    # gmail.users().messages() também.
    def messages(self):
        return self

    # Perfil da conta.
    def getProfile(self, userId):  # noqa: N802 (nome igual ao da API do Google)
        # Só o campo usado pelo coletor.
        return _Call(lambda: {"emailAddress": self.account})

    # Lista os ids, duas por página, para exercitar a paginação.
    def list(self, userId, q, maxResults, pageToken=None):
        # Registra a busca.
        self.queries.append(q)
        # Todos os ids.
        ids = list(self.emails)
        # Página atual: o marcador é a posição inicial em texto.
        start = int(pageToken or 0)
        # Ids desta página.
        page = ids[start : start + 2]
        # Resposta no formato da API.
        response = {"messages": [{"id": i} for i in page]}
        # Se sobrou algo, informa a próxima página.
        if start + 2 < len(ids):
            # Marcador da próxima.
            response["nextPageToken"] = str(start + 2)
        # Devolve a chamada preparada.
        return _Call(lambda: response)

    # Baixa uma mensagem em formato bruto.
    def get(self, userId, id, format):  # noqa: A002 (nome igual ao da API do Google)
        # Função que produz a resposta.
        def respond():
            # Registra o download.
            self.fetched.append(id)
            # Simula falha, se pedido.
            if id in self.failing:
                raise ConnectionError("rede caiu")
            # Dados da mensagem.
            email = self.emails[id]
            # Formato da API: e-mail em base64 "para URL", sem os "=" do fim.
            return {
                "id": id,
                "threadId": email.get("thread", "t-" + id),
                "labelIds": email.get("labels", ["INBOX"]),
                "internalDate": "1761800000000",
                "snippet": "prévia",
                "raw": base64.urlsafe_b64encode(email["raw"]).decode().rstrip("="),
            }

        # Devolve a chamada preparada.
        return _Call(respond)


# Monta um HttpError com o código pedido (403, 404...).
def http_error(status: int) -> HttpError:
    # A resposta HTTP falsa e um corpo qualquer.
    return HttpError(httplib2.Response({"status": status}), b"erro")


# Drive falso: arquivos por id, com dados e tipo.
class FakeDrive:
    # files: id → {"name", "mimeType", "data", "parents"}; pastas entram só com "parents".
    # Ids ausentes dão 404.
    def __init__(self, files: dict[str, dict]):
        # Os arquivos.
        self.files_by_id = files
        # Quantas vezes cada arquivo foi baixado ou exportado.
        self.downloads: list[str] = []

    # drive.files() devolve o próprio objeto.
    def files(self):
        return self

    # Metadados.
    def get(self, fileId, fields, supportsAllDrives):  # noqa: N803 (nome da API)
        # Função que produz a resposta.
        def respond():
            # Arquivo inexistente: 404.
            if fileId not in self.files_by_id:
                raise http_error(404)
            # Dados do arquivo.
            f = self.files_by_id[fileId]
            # Campos pedidos pelo coletor; "parents" são as pastas onde o item está.
            return {
                "id": fileId,
                "name": f.get("name", fileId),
                "mimeType": f.get("mimeType", "application/vnd.google-apps.folder"),
                "size": "10",
                "parents": f.get("parents", []),
            }

        # Devolve a chamada preparada.
        return _Call(respond)

    # Exportação de arquivos nativos do Google.
    def export(self, fileId, mimeType):  # noqa: N803 (nome da API)
        # Registra e devolve os bytes "exportados".
        def respond():
            # Registra.
            self.downloads.append(fileId)
            # Conteúdo.
            return self.files_by_id[fileId]["data"]

        # Devolve a chamada preparada.
        return _Call(respond)

    # Download de arquivos comuns.
    def get_media(self, fileId, supportsAllDrives):  # noqa: N803 (nome da API)
        # Mesmo comportamento da exportação.
        return self.export(fileId, None)


# --- Geradores de arquivos de exemplo -------------------------------------------


# PDF mínimo escrito à mão, com um texto na página. Sem texto, simula um escaneado.
def make_pdf(text: str = "") -> bytes:
    # Comando de desenho do texto (vazio se não houver texto).
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    # Os cinco objetos de um PDF de uma página: catálogo, páginas, página, conteúdo, fonte.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    # Cabeçalho do arquivo.
    out = bytearray(b"%PDF-1.4\n")
    # Posição de cada objeto no arquivo (o índice "xref" do PDF exige isso).
    offsets = []
    # Escreve cada objeto numerado.
    for number, body in enumerate(objects, start=1):
        # Guarda a posição.
        offsets.append(len(out))
        # Objeto no formato "N 0 obj ... endobj".
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    # Posição do índice.
    xref = len(out)
    # Índice: uma linha por objeto com a posição em 10 dígitos.
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    # Linhas dos objetos.
    for offset in offsets:
        # Posição em 10 dígitos, como o formato exige.
        out += b"%010d 00000 n \n" % offset
    # Rodapé que aponta para o catálogo e para o índice.
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    # Bytes finais.
    return bytes(out)


# Word com um parágrafo e uma tabela.
def make_docx() -> bytes:
    # Biblioteca do Word.
    import docx

    # Documento novo.
    document = docx.Document()
    # Parágrafo.
    document.add_paragraph("Ata da reunião do Projeto Atlas")
    # Tabela de 2 linhas e 2 colunas.
    table = document.add_table(rows=2, cols=2)
    # Cabeçalho.
    table.cell(0, 0).text, table.cell(0, 1).text = "Tarefa", "Responsável"
    # Linha de dados.
    table.cell(1, 0).text, table.cell(1, 1).text = "Revisar escopo", "Ana"
    # Salva na memória.
    buffer = io.BytesIO()
    # Grava.
    document.save(buffer)
    # Bytes.
    return buffer.getvalue()


# Excel com duas abas, uma delas um mini plano de projeto com data.
def make_xlsx() -> bytes:
    # Data para a célula.
    from datetime import date

    # Biblioteca do Excel.
    from openpyxl import Workbook

    # Planilha nova.
    workbook = Workbook()
    # Primeira aba.
    sheet = workbook.active
    # Nome da aba.
    sheet.title = "Cronograma"
    # Cabeçalho.
    sheet.append(["Entrega", "Prazo"])
    # Linha com data.
    sheet.append(["Homologação", date(2026, 11, 20)])
    # Segunda aba.
    workbook.create_sheet("Riscos").append(["Atraso do fornecedor"])
    # Salva na memória.
    buffer = io.BytesIO()
    # Grava.
    workbook.save(buffer)
    # Bytes.
    return buffer.getvalue()


# PowerPoint com um slide de título e anotações.
def make_pptx() -> bytes:
    # Biblioteca do PowerPoint.
    from pptx import Presentation

    # Apresentação nova.
    presentation = Presentation()
    # Slide com o layout de título.
    slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    # Título.
    slide.shapes.title.text = "Status semanal"
    # Anotações do apresentador.
    slide.notes_slide.notes_text_frame.text = "Falar do risco de prazo"
    # Salva na memória.
    buffer = io.BytesIO()
    # Grava.
    presentation.save(buffer)
    # Bytes.
    return buffer.getvalue()


# Monta um e-mail completo com corpo em texto e HTML e os anexos pedidos.
def make_email(
    subject: str = "Cronograma do Projeto Atlas",
    attachments: list[tuple[str, str, bytes]] | None = None,
    html_link: str | None = None,
) -> bytes:
    # E-mail novo.
    msg = EmailMessage()
    # Remetente com acento (vira "=?utf-8?..." no bruto; o leitor tem que decodificar).
    msg["From"] = "Ana Souza Ribeiro <ana@cliente.com>"
    # Destinatário.
    msg["To"] = "Dona <dona@exemplo.com>"
    # Cópia com dois endereços.
    msg["Cc"] = "Bruno <BRUNO@cliente.com>, carla@cliente.com"
    # Assunto.
    msg["Subject"] = subject
    # Id mundial.
    msg["Message-ID"] = "<abc@cliente.com>"
    # Corpo em texto.
    msg.set_content("Olá, segue o cronograma revisado.\nAbraços, Ana")
    # Versão em HTML, com o link do Drive só no href (como o Gmail faz com os "chips").
    link = f'<a href="{html_link}">planilha</a>' if html_link else ""
    # Acrescenta o HTML como alternativa ao texto.
    msg.add_alternative(f"<p>Olá, segue o cronograma revisado. {link}</p>", subtype="html")
    # Anexos (nome, tipo MIME, bytes).
    for filename, mime, data in attachments or []:
        # Separa "application/pdf" em "application" e "pdf".
        maintype, subtype = mime.split("/")
        # Acrescenta o anexo.
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    # Bytes do e-mail.
    return msg.as_bytes()
