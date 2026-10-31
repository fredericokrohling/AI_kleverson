"""Transforma um e-mail bruto (formato RFC 822, o ".eml") em partes úteis.

Por que pedir o e-mail bruto ao Gmail (format="raw") em vez do JSON pronto (format="full"):
- uma chamada por mensagem traz tudo, inclusive os anexos; no JSON, cada anexo exige
  outra chamada à API;
- o .eml é o original completo. Guardado no disco, ele permite reprocessar qualquer coisa
  no futuro (um cabeçalho que hoje não usamos, por exemplo) sem depender do Gmail;
- quem lê o .eml é o módulo `email` do próprio Python, maduro e testado há décadas.

O resultado é um ParsedEmail: cabeçalhos, corpo em texto, anexos e links do Drive.
"""

# re: expressões regulares, para achar links do Drive no texto.
import re

# dataclass e field: classes simples de dados; field cria listas vazias por objeto.
from dataclasses import dataclass, field

# policy.default ativa a interface moderna: cabeçalhos já decodificados (acentos
# corretos em "=?UTF-8?..."), get_body() para achar o corpo, entre outros.
from email import policy

# BytesParser lê um e-mail a partir de bytes.
from email.parser import BytesParser

# getaddresses separa listas de destinatários em pares (nome, e-mail).
from email.utils import getaddresses

# Funções de texto do módulo vizinho.
from app.ingestion.extractors import decode_text, html_to_text

# Links do Drive que dá para baixar: documento, planilha, apresentação e arquivo comum.
# O id de um arquivo do Drive tem letras, números, "-" e "_", com 20 caracteres ou mais
# (isso exclui links publicados "/d/e/...", que não são ids de arquivo).
_DRIVE_PATTERNS = [
    # docs.google.com/document/d/ID, /spreadsheets/d/ID, /presentation/d/ID.
    re.compile(
        r"https://docs\.google\.com/(?:document|spreadsheets|presentation)/(?:u/\d+/)?d/"
        r"([A-Za-z0-9_-]{20,})"
    ),
    # drive.google.com/file/d/ID.
    re.compile(r"https://drive\.google\.com/file/(?:u/\d+/)?d/([A-Za-z0-9_-]{20,})"),
    # drive.google.com/open?id=ID.
    re.compile(r"https://drive\.google\.com/open\?id=([A-Za-z0-9_-]{20,})"),
]

# Links de diagramas guardados fora do Google (Lucid, Canva). O conteúdo fica nos
# servidores desses serviços, e não temos acesso a eles: o link só é avisado no console,
# para você exportar o diagrama como PDF ou PNG. O grupo 1 é o domínio, que diz o serviço.
_EXTERNAL_LINK = re.compile(
    r"https://(?:[\w-]+\.)?(lucid\.app|lucidchart\.com|canva\.com)/[^\s\"'<>)\]]+"
)

# Nome legível de cada domínio.
_EXTERNAL_SERVICES = {"lucid.app": "Lucid", "lucidchart.com": "Lucid", "canva.com": "Canva"}


# Um anexo encontrado dentro do e-mail.
@dataclass(frozen=True)
class ParsedAttachment:
    # Nome do arquivo.
    filename: str
    # Tipo MIME declarado no e-mail.
    mime_type: str
    # Conteúdo do arquivo.
    data: bytes
    # Texto já pronto, para e-mails encaminhados como anexo; None nos demais.
    text: str | None = None


# O e-mail desmontado.
@dataclass
class ParsedEmail:
    # Remetente como aparece no e-mail (ex.: "Ana Souza <ana@exemplo.com>").
    sender: str | None
    # Assunto.
    subject: str | None
    # Corpo em texto.
    body_text: str
    # Destinatários (só os e-mails, em minúsculas).
    to: list[str] = field(default_factory=list)
    # Em cópia.
    cc: list[str] = field(default_factory=list)
    # Data informada pelo remetente (texto do cabeçalho Date).
    date_header: str | None = None
    # Identificador mundial da mensagem (cabeçalho Message-ID).
    message_id: str | None = None
    # A qual mensagem esta responde.
    in_reply_to: str | None = None
    # Anexos encontrados.
    attachments: list[ParsedAttachment] = field(default_factory=list)
    # Ids de arquivos do Drive citados no e-mail, sem repetição, na ordem em que aparecem.
    drive_file_ids: list[str] = field(default_factory=list)
    # Links de diagramas fora do Google: pares (serviço, endereço), sem repetição.
    external_links: list[tuple[str, str]] = field(default_factory=list)


# Lê um cabeçalho sem quebrar a coleta se ele vier malformado.
def _header(message, name: str) -> str | None:
    # Tenta ler e decodificar.
    try:
        # get devolve None se o cabeçalho não existir.
        value = message.get(name)
    # Cabeçalhos quebrados podem levantar erros variados na decodificação.
    except Exception:
        # Sem o cabeçalho, o resto do e-mail ainda é aproveitado.
        return None
    # str() converte o objeto de cabeçalho em texto; strip tira espaços das pontas.
    return str(value).strip() if value is not None else None


# Extrai só os endereços de um cabeçalho de destinatários.
def _addresses(message, name: str) -> list[str]:
    # Lê o cabeçalho; pode haver vários (get_all devolve todos).
    try:
        # Lista de valores ou lista vazia.
        values = [str(v) for v in message.get_all(name, [])]
    # Cabeçalho ilegível.
    except Exception:
        # Sem endereços.
        return []
    # getaddresses devolve pares (nome, e-mail); ficamos com o e-mail em minúsculas.
    return [addr.lower() for _, addr in getaddresses(values) if addr]


# Conteúdo de uma parte de texto, já como str.
def _part_text(part) -> str:
    # get_content decodifica base64/quoted-printable e a codificação de caracteres.
    try:
        # Texto pronto.
        return part.get_content()
    # Codificação desconhecida ou conteúdo corrompido.
    except Exception:
        # Pega os bytes crus e decodifica do nosso jeito tolerante.
        raw = part.get_payload(decode=True) or b""
        # Usa a codificação declarada, se houver.
        return decode_text(raw, part.get_content_charset())


# Encontra os ids do Drive num texto.
def find_drive_ids(text: str) -> list[str]:
    # Lista final, sem repetição e na ordem em que aparecem.
    found: list[str] = []
    # Testa cada padrão.
    for pattern in _DRIVE_PATTERNS:
        # finditer percorre todas as ocorrências.
        for match in pattern.finditer(text):
            # group(1) é o trecho entre parênteses no padrão: o id.
            file_id = match.group(1)
            # Só acrescenta se ainda não estiver na lista.
            if file_id not in found:
                # Guarda.
                found.append(file_id)
    # Devolve os ids.
    return found


# Encontra links do Lucid e do Canva num texto.
def find_external_links(text: str) -> list[tuple[str, str]]:
    # Lista final de (serviço, endereço).
    found: list[tuple[str, str]] = []
    # Cada ocorrência.
    for match in _EXTERNAL_LINK.finditer(text):
        # Endereço sem pontuação final (o ponto final de uma frase não faz parte do link)
        # e sem o "&amp;" do HTML.
        url = match.group(0).rstrip(".,;:!?").replace("&amp;", "&")
        # Serviço pelo domínio.
        pair = (_EXTERNAL_SERVICES[match.group(1)], url)
        # Sem repetição (o mesmo link costuma vir no texto e no HTML).
        if pair not in found:
            # Guarda.
            found.append(pair)
    # Devolve.
    return found


# Decide se uma parte do e-mail é um anexo que vale guardar.
def _is_attachment(part) -> bool:
    # "attachment", "inline" ou None.
    disposition = part.get_content_disposition()
    # Nome do arquivo, se houver.
    filename = part.get_filename()
    # Imagens embutidas no corpo (logo da assinatura, por exemplo) são ruído: o e-mail
    # inteiro já fica guardado no .eml, então nada se perde ao pular essas.
    if part.get_content_maintype() == "image" and (
        disposition == "inline" or part.get("Content-ID")
    ):
        # Não é anexo.
        return False
    # Anexo declarado ou qualquer parte com nome de arquivo.
    return disposition == "attachment" or bool(filename)


# Função principal: bytes do e-mail entram, ParsedEmail sai.
def parse_email(raw: bytes) -> ParsedEmail:
    """Desmonta um e-mail bruto em cabeçalhos, corpo, anexos e links do Drive."""
    # Lê o e-mail com a interface moderna.
    message = BytesParser(policy=policy.default).parsebytes(raw)
    # get_body escolhe o corpo principal, preferindo texto simples ao HTML.
    body_part = message.get_body(preferencelist=("plain", "html"))
    # Texto do corpo; vazio se o e-mail não tiver corpo.
    body = _part_text(body_part) if body_part is not None else ""
    # Corpo só em HTML: converte para texto.
    if body_part is not None and body_part.get_content_subtype() == "html":
        # Tira as tags.
        body = html_to_text(body)

    # Monta o resultado com os cabeçalhos.
    parsed = ParsedEmail(
        # Remetente.
        sender=_header(message, "From"),
        # Assunto.
        subject=_header(message, "Subject"),
        # Corpo sem espaços sobrando nas pontas.
        body_text=body.strip(),
        # Destinatários.
        to=_addresses(message, "To"),
        # Em cópia.
        cc=_addresses(message, "Cc"),
        # Data.
        date_header=_header(message, "Date"),
        # Id da mensagem.
        message_id=_header(message, "Message-ID"),
        # Resposta a.
        in_reply_to=_header(message, "In-Reply-To"),
    )

    # Texto onde procurar links do Drive: todas as partes de texto e HTML do e-mail.
    # O HTML entra cru de propósito: o link costuma estar no href, não no texto visível.
    searchable: list[str] = []
    # walk percorre todas as partes, inclusive as aninhadas.
    for part in message.walk():
        # E-mail encaminhado como anexo: vira um anexo com o texto do e-mail interno.
        if part.get_content_type() == "message/rfc822":
            # O e-mail interno é o primeiro item da carga.
            inner = part.get_payload(0)
            # Corpo do e-mail interno.
            inner_body = inner.get_body(preferencelist=("plain", "html"))
            # Texto do corpo interno, convertido se for HTML.
            inner_text = _part_text(inner_body) if inner_body is not None else ""
            # Converte HTML em texto quando for o caso.
            if inner_body is not None and inner_body.get_content_subtype() == "html":
                # Tira as tags.
                inner_text = html_to_text(inner_text)
            # Assunto do e-mail interno, para dar nome ao anexo.
            inner_subject = _header(inner, "Subject") or "mensagem encaminhada"
            # Guarda como anexo .eml, com o texto já pronto.
            parsed.attachments.append(
                ParsedAttachment(
                    # Nome: o arquivo indicado ou o assunto.
                    filename=part.get_filename() or f"{inner_subject}.eml",
                    # Tipo MIME.
                    mime_type="message/rfc822",
                    # Bytes do e-mail interno.
                    data=inner.as_bytes(),
                    # Texto com remetente e assunto no topo.
                    text=f"De: {_header(inner, 'From')}\nAssunto: {inner_subject}\n\n"
                    + inner_text.strip(),
                )
            )
            # Segue para a próxima parte (walk vai descer nos anexos do e-mail interno).
            continue
        # Partes "contêiner" (multipart) não têm conteúdo próprio.
        if part.is_multipart():
            continue
        # Anexo de verdade.
        if _is_attachment(part):
            # get_payload(decode=True) devolve os bytes já decodificados do base64.
            data = part.get_payload(decode=True) or b""
            # Guarda o anexo.
            parsed.attachments.append(
                ParsedAttachment(
                    # Nome do arquivo (ou um nome genérico).
                    filename=part.get_filename() or "anexo_sem_nome",
                    # Tipo MIME declarado.
                    mime_type=part.get_content_type(),
                    # Conteúdo.
                    data=data,
                )
            )
        # Parte de texto ou HTML do corpo: entra na busca de links.
        elif part.get_content_maintype() == "text":
            # Texto cru da parte.
            searchable.append(_part_text(part))
    # Procura os links do Drive em todo o texto juntado.
    parsed.drive_file_ids = find_drive_ids("\n".join(searchable))
    # E os links do Lucid e do Canva.
    parsed.external_links = find_external_links("\n".join(searchable))
    # Devolve o e-mail desmontado.
    return parsed
