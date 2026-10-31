"""Testes do bloco 2: e-mail e anexos para o bronze.

Cobrem as quatro peças (guarda de arquivos, extratores, leitura do e-mail e coletor) e
as regras que importam no dia a dia: não duplicar, não perder e-mail por causa de um
anexo ruim, tentar de novo o que falhou e nunca rodar duas coletas ao mesmo tempo.
"""

# Data e hora com fuso, e intervalos.
from datetime import UTC, datetime, timedelta

# EmailMessage para montar e-mails especiais (encaminhado, imagem embutida).
from email.message import EmailMessage

# Biblioteca de testes.
import pytest

# select monta consultas; text executa SQL escrito à mão.
from sqlalchemy import select, text

# Configuração.
from app.config import Settings

# O que estamos testando: leitura do e-mail...
from app.ingestion.email_parser import find_drive_ids, find_external_links, parse_email

# ...extratores...
from app.ingestion.extractors import detect_kind, extract_text, html_to_text

# ...coletor...
from app.ingestion.gmail import collect_gmail, reextract_attachments

# ...e guarda de arquivos.
from app.ingestion.storage import FileStore, safe_suffix

# Tabelas.
from app.models import Attachment, BronzeItem, IngestionRun, SourceKind

# Falsos do Google e geradores de arquivos.
from tests.fakes import (
    FakeGmail,
    make_docx,
    make_email,
    make_pdf,
    make_pptx,
    make_xlsx,
)

# Tipos MIME usados nos anexos de exemplo. PDF.
PDF = "application/pdf"
# Word.
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Excel.
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# PowerPoint.
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
# Um id de arquivo do Drive com o formato real (letras, números, "-" e "_").
SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789"
# Momento fixo, para os testes não dependerem do relógio.
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


# Configuração de teste com a pasta de arquivos num diretório temporário do pytest.
@pytest.fixture
def settings(tmp_path):
    # tmp_path é uma pasta nova e vazia para cada teste.
    return Settings(data_dir=str(tmp_path))


# Pasta de originais do teste.
@pytest.fixture
def store(settings):
    # Mesma raiz da configuração.
    return FileStore(settings.data_dir)


# --- Guarda de arquivos --------------------------------------------------------


# O mesmo conteúdo gera a mesma chave e não duplica no disco.
def test_store_is_content_addressed(store):
    # Grava duas vezes o mesmo conteúdo.
    first = store.save(b"conteudo", ".pdf")
    # Segunda gravação do mesmo conteúdo.
    second = store.save(b"conteudo", ".pdf")
    # Mesma chave.
    assert first == second
    # Lê de volta o mesmo conteúdo.
    assert store.read(first) == b"conteudo"
    # Conteúdo diferente, chave diferente.
    assert store.save(b"outro", ".pdf") != first


# Uma chave com "../" não sai da pasta.
def test_store_rejects_path_traversal(store):
    # Espera o erro de chave inválida.
    with pytest.raises(ValueError):
        # Tenta ler fora da pasta.
        store.read("../../etc/passwd")


# Extensões estranhas no nome do anexo viram ".bin".
@pytest.mark.parametrize(
    ("filename", "expected"),
    [("Plano.XLSX", ".xlsx"), ("sem_extensao", ".bin"), ("x.p/df", ".bin"), (None, ".bin")],
)
def test_safe_suffix(filename, expected):
    # Confere cada caso.
    assert safe_suffix(filename) == expected


# --- Extratores ----------------------------------------------------------------


# Cada formato suportado devolve o texto esperado.
def test_extract_pdf_docx_xlsx_pptx():
    # PDF com texto.
    assert "Cronograma aprovado" in extract_text(make_pdf("Cronograma aprovado"), "a.pdf", PDF).text
    # Word: parágrafo e tabela.
    word = extract_text(make_docx(), "ata.docx", DOCX).text
    # O parágrafo saiu.
    assert "Ata da reunião do Projeto Atlas" in word
    # A tabela saiu com as células separadas.
    assert "Revisar escopo | Ana" in word
    # Excel: as duas abas, com a data em formato padrão.
    sheet = extract_text(make_xlsx(), "plano.xlsx", XLSX).text
    # Aba do cronograma, com a data.
    assert "### Planilha: Cronograma" in sheet and "Homologação\t2026-11-20" in sheet
    # Segunda aba.
    assert "### Planilha: Riscos" in sheet
    # PowerPoint: título e anotações.
    slides = extract_text(make_pptx(), "status.pptx", PPTX).text
    # Título do slide.
    assert "### Slide 1" in slides and "Status semanal" in slides
    # Anotações.
    assert "[Anotações] Falar do risco de prazo" in slides


# Tipo genérico (octet-stream) com extensão conhecida é lido pela extensão.
def test_extension_wins_over_generic_mime():
    # O Gmail às vezes manda "application/octet-stream" para planilhas.
    assert detect_kind("plano.xlsx", "application/octet-stream") == "xlsx"


# Casos que não dão texto viram mensagens claras, sem levantar erro.
@pytest.mark.parametrize(
    ("data", "filename", "mime", "expected"),
    [
        # PDF escaneado (sem texto).
        (make_pdf(""), "scan.pdf", PDF, "PDF_SEM_TEXTO"),
        # Word antigo.
        (b"\xd0\xcf\x11\xe0", "velho.doc", "application/msword", "Formato antigo"),
        # Imagem corrompida (ou formato que o Pillow não abre, como HEIC).
        (b"\x89PNG", "foto.png", "image/png", "IMAGEM_FORMATO"),
        # Arquivo corrompido que diz ser Excel.
        (b"isto nao e um zip", "quebrado.xlsx", XLSX, "Falha ao ler xlsx"),
        # Tipo desconhecido.
        (b"\x00\x01", "programa.exe", "application/x-msdownload", "não suportado"),
    ],
)
def test_extraction_failures_are_reported(data, filename, mime, expected):
    # Extrai.
    result = extract_text(data, filename, mime)
    # Sem texto.
    assert result.text is None
    # Com o motivo esperado.
    assert expected in result.error


# HTML vira texto legível, sem scripts e com células separadas.
def test_html_to_text():
    # HTML com estilo, script, parágrafos e tabela.
    html = (
        "<html><head><style>p{}</style></head><body><script>x()</script>"
        "<p>Olá&nbsp;Ana</p><table><tr><td>A</td><td>B</td></tr></table></body></html>"
    )
    # Converte.
    result = html_to_text(html)
    # Texto visível presente, script e estilo ausentes.
    assert "Olá" in result and "x()" not in result and "p{}" not in result
    # Células separadas.
    assert "A | B" in result


# --- Leitura do e-mail ---------------------------------------------------------


# Cabeçalhos, corpo, anexos e link do Drive saem certos.
def test_parse_email_full():
    # E-mail com um PDF e um link do Drive no HTML.
    raw = make_email(
        attachments=[("plano.pdf", PDF, make_pdf("Plano"))],
        html_link=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0",
    )
    # Desmonta.
    parsed = parse_email(raw)
    # Remetente com acento decodificado.
    assert parsed.sender == "Ana Souza Ribeiro <ana@cliente.com>"
    # Assunto.
    assert parsed.subject == "Cronograma do Projeto Atlas"
    # Corpo preferindo o texto simples.
    assert parsed.body_text.startswith("Olá, segue o cronograma")
    # Endereços em minúsculas.
    assert parsed.cc == ["bruno@cliente.com", "carla@cliente.com"]
    # Um anexo, com o nome certo.
    assert [a.filename for a in parsed.attachments] == ["plano.pdf"]
    # O link do Drive foi achado no HTML.
    assert parsed.drive_file_ids == [SHEET_ID]


# Logo embutido na assinatura não vira anexo; e-mail encaminhado vira, com texto.
def test_parse_skips_inline_images_and_reads_forwarded():
    # E-mail interno, que será encaminhado como anexo.
    inner = EmailMessage()
    # Remetente do interno.
    inner["From"] = "fornecedor@exemplo.com"
    # Assunto do interno.
    inner["Subject"] = "Proposta comercial"
    # Corpo do interno.
    inner.set_content("Valor total: 10 mil")
    # E-mail externo.
    outer = EmailMessage()
    # Assunto do externo.
    outer["Subject"] = "Fwd: proposta"
    # Corpo do externo.
    outer.set_content("Veja abaixo")
    # Logo embutido (inline com Content-ID), como numa assinatura.
    outer.add_attachment(
        b"\x89PNG",
        maintype="image",
        subtype="png",
        filename="logo.png",
        disposition="inline",
        cid="<logo>",
    )
    # O e-mail interno anexado.
    outer.add_attachment(inner)
    # Desmonta.
    parsed = parse_email(outer.as_bytes())
    # Só o encaminhado virou anexo.
    assert len(parsed.attachments) == 1
    # Com o texto do e-mail interno.
    assert "Valor total: 10 mil" in parsed.attachments[0].text
    # E o assunto no nome.
    assert parsed.attachments[0].filename == "Proposta comercial.eml"


# Formatos de link do Drive reconhecidos e ignorados.
def test_find_drive_ids():
    # Texto com vários links.
    found = find_drive_ids(
        f"https://docs.google.com/document/d/{SHEET_ID}/edit "
        f"https://drive.google.com/file/d/{SHEET_ID}x/view "
        f"https://drive.google.com/open?id={SHEET_ID}y "
        # Link publicado ("/d/e/") não é id de arquivo e fica de fora.
        "https://docs.google.com/document/d/e/2PACX-1vQabcdefghijklmnopqrstuvwxyz/pub "
        # Repetido: aparece uma vez só.
        f"https://docs.google.com/document/d/{SHEET_ID}/edit"
    )
    # Três ids distintos.
    assert found == [SHEET_ID, SHEET_ID + "x", SHEET_ID + "y"]


# Links do Lucid e do Canva: sem pontuação final, sem "&amp;" e sem repetição.
def test_find_external_links():
    # Texto e HTML com os mesmos links.
    text = (
        "veja https://lucid.app/lucidchart/abc-123/edit?a=1&amp;b=2. e "
        '<a href="https://www.canva.com/design/DAF/view">x</a> '
        "https://lucid.app/lucidchart/abc-123/edit?a=1&b=2"
    )
    # Um de cada serviço.
    assert find_external_links(text) == [
        ("Lucid", "https://lucid.app/lucidchart/abc-123/edit?a=1&b=2"),
        ("Canva", "https://www.canva.com/design/DAF/view"),
    ]


# --- Coletor ---------------------------------------------------------------------


# Monta o Gmail falso com três e-mails: um com anexos e link, um rascunho e um simples.
def _gmail() -> FakeGmail:
    # Dicionário de mensagens.
    return FakeGmail(
        {
            # E-mail rico.
            "m1": {
                "raw": make_email(
                    attachments=[
                        ("plano.xlsx", XLSX, make_xlsx()),
                        ("scan.pdf", PDF, make_pdf("")),
                        ("notas.txt", "text/plain", b"linha\x00com nulo"),
                    ],
                    html_link=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
                ),
                "thread": "conversa-1",
            },
            # Rascunho: não pode ser gravado.
            "m2": {"raw": make_email(subject="rascunho"), "labels": ["DRAFT"]},
            # E-mail simples.
            "m3": {"raw": make_email(subject="Re: Cronograma"), "thread": "conversa-1"},
        }
    )


# Primeira coleta: grava e-mails, anexos, o registro do link do Drive e a rodada.
def test_first_collection(sessions, settings, store):
    # Falsos.
    gmail = _gmail()
    # Roda.
    summary = collect_gmail(sessions, gmail, store, settings, now=NOW)
    # Rodada sem erro.
    assert summary.status == "ok"
    # 3 vistos, 2 novos (o rascunho foi pulado).
    assert (summary.messages_seen, summary.messages_new) == (3, 2)
    # 3 anexos (o link do Drive não conta como arquivo); 1 sem texto (o PDF escaneado).
    assert (summary.attachments_saved, summary.extraction_errors) == (3, 1)
    # Primeira coleta começa "a partir de agora" (GMAIL_INITIAL_DAYS=0), com o filtro padrão.
    # (O Gmail falso ignora a data e devolve tudo, para testar a gravação.)
    assert gmail.queries[0] == f"{settings.gmail_query} after:{int(NOW.timestamp())}"
    # Confere o banco.
    with sessions() as s:
        # Item do e-mail rico.
        item = s.scalar(select(BronzeItem).where(BronzeItem.external_id == "m1"))
        # Fonte, conversa e remetente.
        assert item.source == SourceKind.GMAIL and item.thread_id == "conversa-1"
        # Remetente.
        assert item.author == "Ana Souza Ribeiro <ana@cliente.com>"
        # Metadados: destinatários e marcadores.
        assert item.extra["para"] == ["dona@exemplo.com"] and item.extra["marcadores"] == ["INBOX"]
        # O .eml original foi guardado e é idêntico ao que o Gmail entregou.
        assert store.read(item.extra["eml"]) == gmail.emails["m1"]["raw"]
        # Arquivos do item, por nome.
        files = {
            a.filename: a
            for a in s.scalars(select(Attachment).where(Attachment.bronze_item_id == item.id))
        }
        # Planilha anexada com texto.
        assert "Homologação" in files["plano.xlsx"].extracted_text
        # PDF escaneado com o motivo.
        assert "PDF_SEM_TEXTO" in files["scan.pdf"].extraction_error
        # O caractere nulo foi removido (o Postgres não aceita).
        assert files["notas.txt"].extracted_text == "linhacom nulo"
        # Link do Drive: só registrado, sem arquivo e sem erro.
        link = files[f"drive:{SHEET_ID}"]
        # Origem e id do arquivo no Drive.
        assert link.origin == "link_drive" and link.external_ref == SHEET_ID
        # Nada baixado.
        assert link.object_key is None and link.extraction_error is None
        # O rascunho não entrou.
        assert s.scalar(select(BronzeItem).where(BronzeItem.external_id == "m2")) is None
        # A rodada ficou registrada.
        run = s.scalar(select(IngestionRun))
        # Situação e janela.
        assert run.status == "ok" and run.window_end == NOW


# Coletas seguintes: continuam do fim da anterior (menos a folga), sem voltar antes do
# início da primeira coleta, e não duplicam nada.
def test_next_collections_are_incremental_and_idempotent(sessions, settings, store):
    # Mesmos falsos em todas as rodadas.
    gmail = _gmail()
    # Primeira rodada: marco zero em NOW.
    collect_gmail(sessions, gmail, store, settings, now=NOW)
    # Downloads feitos na primeira.
    first_fetches = len(gmail.fetched)
    # Segunda rodada, 2 horas depois.
    later = NOW + timedelta(hours=2)
    # Roda de novo.
    summary = collect_gmail(sessions, gmail, store, settings, now=later)
    # Nada novo.
    assert summary.messages_new == 0
    # Fim da anterior menos 60 min cairia antes do marco zero; a janela fica no marco zero.
    assert gmail.queries[-1].endswith(f"after:{int(NOW.timestamp())}")
    # Terceira rodada, mais 1 hora depois.
    collect_gmail(sessions, gmail, store, settings, now=later + timedelta(hours=1))
    # Agora sim: fim da segunda menos a folga de 60 minutos.
    since = int((later - timedelta(minutes=60)).timestamp())
    # Confere a busca.
    assert gmail.queries[-1].endswith(f"after:{since}")
    # Só o rascunho (que não é gravado) foi baixado de novo; os outros foram reconhecidos.
    assert gmail.fetched[first_fetches:] == ["m2", "m2"]
    # Continua com 2 e-mails no banco.
    with sessions() as s:
        # Conta os itens.
        assert len(s.scalars(select(BronzeItem)).all()) == 2


# Uma mensagem com erro não derruba a rodada, e a próxima tenta de novo.
def test_failed_message_is_retried(sessions, settings, store):
    # Falsos.
    gmail = _gmail()
    # A mensagem m3 vai falhar.
    gmail.failing.add("m3")
    # Primeira rodada.
    first = collect_gmail(sessions, gmail, store, settings, now=NOW)
    # Termina com erro, mas grava a m1.
    assert first.status == "com_erros" and first.messages_new == 1
    # A rede volta.
    gmail.failing.clear()
    # Cinco minutos depois.
    later = NOW + timedelta(minutes=5)
    # Segunda rodada.
    second = collect_gmail(sessions, gmail, store, settings, now=later)
    # A janela não avançou: como nenhuma rodada terminou "ok", recomeça do início da
    # primeira rodada, em vez de partir do fim da rodada com erro.
    assert gmail.queries[-1].endswith(f"after:{int(NOW.timestamp())}")
    # A m3 entrou agora.
    assert second.status == "ok" and second.messages_new == 1
    # O detalhe do erro ficou registrado na primeira rodada.
    with sessions() as s:
        # Rodada com erro.
        run = s.scalar(select(IngestionRun).where(IngestionRun.id == first.run_id))
        # Id e motivo.
        assert "m3: ConnectionError: rede caiu" in run.error_detail


# Links do Drive não contam nos totais de arquivos da página inicial.
def test_drive_links_do_not_count_as_files(sessions, settings, store):
    # Coleta.
    collect_gmail(sessions, _gmail(), store, settings, now=NOW)
    # Resumo da página inicial.
    from app.ingestion.gmail import collection_overview

    # Lê.
    with sessions() as s:
        # Totais.
        overview = collection_overview(s)
    # 3 arquivos (sem o link), 1 sem texto.
    assert (overview["files"], overview["files_without_text"]) == (3, 1)


# Duas coletas ao mesmo tempo: a segunda desiste sem fazer nada.
def test_only_one_collection_at_a_time(sessions, settings, store):
    # Outra "coleta" segura a trava numa conexão separada.
    with sessions() as other:
        # Pega a trava.
        other.execute(text("SELECT pg_try_advisory_lock(20002)"))
        # Tenta coletar.
        summary = collect_gmail(sessions, _gmail(), store, settings, now=NOW)
        # Solta a trava.
        other.execute(text("SELECT pg_advisory_unlock(20002)"))
    # Desistiu.
    assert summary.skipped
    # Nenhuma rodada registrada.
    with sessions() as s:
        # Tabela vazia.
        assert s.scalar(select(IngestionRun)) is None


# Data explícita (--desde) substitui a janela calculada.
def test_explicit_since(sessions, settings, store):
    # Gmail falso.
    gmail = _gmail()
    # Data pedida.
    since = datetime(2026, 9, 1, tzinfo=UTC)
    # Roda com a data.
    collect_gmail(sessions, gmail, store, settings, since=since, now=NOW)
    # A busca usa a data pedida.
    assert gmail.queries[0].endswith(f"after:{int(since.timestamp())}")


# O mesmo anexo em dois e-mails é extraído uma vez e guardado uma vez.
def test_same_attachment_is_reused(sessions, settings, store, monkeypatch):
    # Mesmo PDF nos dois e-mails.
    pdf = make_pdf("Contrato assinado")
    # Gmail com os dois.
    gmail = FakeGmail(
        {
            "a": {"raw": make_email(attachments=[("contrato.pdf", PDF, pdf)])},
            "b": {"raw": make_email(attachments=[("contrato (1).pdf", PDF, pdf)])},
        }
    )
    # Contador de extrações.
    calls = []
    # Extrator original.
    from app.ingestion import gmail as gmail_module

    # Guarda a função original.
    original = gmail_module.extract_text
    # Troca por uma versão que conta as chamadas.
    monkeypatch.setattr(gmail_module, "extract_text", lambda *a: calls.append(a[1]) or original(*a))
    # Roda.
    collect_gmail(sessions, gmail, store, settings, now=NOW)
    # Extraído uma vez só.
    assert len(calls) == 1
    # As duas linhas têm o mesmo texto e apontam para o mesmo arquivo.
    with sessions() as s:
        # Linhas de anexo.
        rows = s.scalars(select(Attachment)).all()
        # Mesmo arquivo no disco.
        assert rows[0].object_key == rows[1].object_key
        # Mesmo texto.
        assert rows[0].extracted_text == rows[1].extracted_text


# reextrair-anexos: depois de "consertar" um extrator, o texto aparece.
def test_reextract_attachments(sessions, settings, store, monkeypatch):
    # Primeira coleta: o PDF escaneado fica sem texto.
    collect_gmail(sessions, _gmail(), store, settings, now=NOW)
    # Simula um extrator melhor que agora lê o PDF escaneado.
    from app.ingestion import gmail as gmail_module

    # Resultado de extração, para o extrator simulado devolver.
    from app.ingestion.extractors import Extraction

    # Troca o extrator.
    monkeypatch.setattr(gmail_module, "extract_text", lambda *a: Extraction("texto por OCR"))
    # Roda a reextração.
    tried, fixed = reextract_attachments(sessions, store)
    # Um tentado, um resolvido.
    assert (tried, fixed) == (1, 1)
    # O texto foi gravado.
    with sessions() as s:
        # Linha do PDF.
        row = s.scalar(select(Attachment).where(Attachment.filename == "scan.pdf"))
        # Texto novo, sem erro.
        assert row.extracted_text == "texto por OCR" and row.extraction_error is None
