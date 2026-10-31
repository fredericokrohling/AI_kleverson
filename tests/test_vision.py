"""Testes da leitura de imagens e PDFs digitalizados pelo Claude (com um Claude falso).

O que importa provar: o pedido sai no formato da API, o texto volta com o aviso de
origem, a fila não paga duas vezes pelo mesmo arquivo, o teto mensal para tudo sem
gastar, e o "Tentar de novo" do console usa o Claude.
"""

# io.BytesIO: abrir a imagem preparada para conferir o tamanho.
import io

# os.urandom gera ruído (uma "foto" que não comprime).
import os

# Data e hora fixas para a coleta.
from datetime import UTC, datetime

# Dinheiro sem erro de arredondamento.
from decimal import Decimal

# httpx monta o pedido que acompanha um erro de rede do SDK.
import httpx

# Biblioteca de testes.
import pytest

# Erros do SDK da Anthropic.
from anthropic import APIConnectionError

# Cliente de teste do FastAPI.
from fastapi.testclient import TestClient

# Biblioteca de imagens.
from PIL import Image

# Consultas.
from sqlalchemy import select

# Configuração.
from app.config import Settings

# Fila do Claude.
from app.ingestion import vision_queue

# Varredura do Drive.
from app.ingestion.drive_sync import add_folder, retry_drive_item, sync_drive

# Extração e detecção de formato.
from app.ingestion.extractors import detect_kind, extract_text, split_error

# Lista e "Tentar de novo".
from app.ingestion.failures import dismiss, list_failures, retry

# Coleta do Gmail.
from app.ingestion.gmail import collect_gmail

# Guarda dos originais.
from app.ingestion.storage import FileStore

# Leitor do Claude e preparação de imagens.
from app.ingestion.vision import _MAX_IMAGE_BYTES, VisionReader, prepare_image

# Cliente do Claude com registro de custo.
from app.llm import LLMClient

# Aplicação web.
from app.main import app

# Tabelas.
from app.models import Attachment, AuditLog, BronzeItem, DriveItem, LLMCall

# Falsos e geradores de arquivos.
from tests.fakes import FakeClaude, FakeDrive, FakeGmail, make_email, make_pdf, make_png

# Momento fixo da coleta.
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
# Pasta monitorada do Drive falso.
ROOT = "pastaCRM000001"
# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console (exigida nos POST).
SAME = {**OWNER, "Origin": "http://testserver"}


# Configuração com pasta temporária e um modelo próprio para a leitura.
@pytest.fixture
def settings(tmp_path):
    # Haiku na leitura, para conferir que o modelo do .env é respeitado.
    return Settings(data_dir=str(tmp_path), vision_model="claude-haiku-5-5")


# Guarda dos originais.
@pytest.fixture
def store(settings):
    # Na pasta temporária.
    return FileStore(settings.data_dir)


# Claude falso.
@pytest.fixture
def claude():
    # Responde com um fluxo curto.
    return FakeClaude()


# Leitor com o Claude falso.
@pytest.fixture
def reader(sessions, settings, claude):
    # Mesmo cliente do sistema, com o falso no lugar da API.
    return VisionReader(LLMClient(sessions, settings, claude), settings)


# Esgota o teto do mês com uma chamada fictícia de US$ 10.
def _spend_budget(sessions) -> None:
    # Uma sessão.
    with sessions() as s:
        # Chamada que custou o teto inteiro.
        s.add(LLMCall(purpose="teste", model="claude-opus-5-5", cost_usd=Decimal("10")))
        # Grava.
        s.commit()


# O extrator decide o que vai para o Claude e o que não vale a chamada.
@pytest.mark.parametrize(
    ("data", "filename", "mime", "code"),
    [
        # Diagrama: vai para a fila.
        (make_png(800, 600), "fluxo.png", "image/png", "IMAGEM_SEM_OCR"),
        # Ícone de assinatura: não vale a chamada.
        (make_png(64, 64), "logo.png", "image/png", "IMAGEM_PEQUENA"),
        # Formato que o Pillow não abre (HEIC do iPhone) ou arquivo corrompido.
        (b"nao e imagem", "foto.heic", "image/heic", "IMAGEM_FORMATO"),
        # Imagem sem tipo MIME (releitura do Drive): reconhecida pela extensão.
        (make_png(800, 600), "abc123.png", None, "IMAGEM_SEM_OCR"),
        # PDF digitalizado: vai para a fila.
        (make_pdf(), "scan.pdf", "application/pdf", "PDF_SEM_TEXTO"),
    ],
)
def test_extractor_queues_images(data, filename, mime, code):
    # Código do resultado.
    assert split_error(extract_text(data, filename, mime).error)[0] == code


# Imagens são reduzidas e comprimidas para caber nos limites da API.
def test_prepare_image():
    # Diagrama muito largo: o lado maior cai para 2576 pixels, em PNG.
    media_type, data = prepare_image(make_png(6000, 1500))
    # Abre o resultado.
    image = Image.open(io.BytesIO(data))
    # PNG com o lado maior no limite.
    assert media_type == "image/png" and max(image.size) == 2576
    # Imagem com transparência continua com transparência.
    media_type, data = prepare_image(make_png(800, 600, mode="RGBA"))
    # Confere o modo.
    assert media_type == "image/png" and Image.open(io.BytesIO(data)).mode == "RGBA"
    # "Foto" de ruído não comprime em PNG: vira JPEG dentro do limite.
    noise = Image.frombytes("RGB", (1900, 1900), os.urandom(1900 * 1900 * 3))
    # Grava o ruído em PNG.
    buffer = io.BytesIO()
    # PNG.
    noise.save(buffer, format="PNG")
    # Prepara.
    media_type, data = prepare_image(buffer.getvalue())
    # JPEG dentro do limite.
    assert media_type == "image/jpeg" and len(data) <= _MAX_IMAGE_BYTES
    # Garantia extra: a detecção de imagem pela extensão.
    assert detect_kind("diagrama.webp", None) == "imagem"


# Leitura de uma imagem: formato do pedido, aviso de origem e registro de custo.
def test_reader_reads_image(sessions, reader, claude):
    # Lê.
    result = reader.read(make_png(), "fluxo.png", "image/png")
    # Texto com o aviso de origem e o conteúdo.
    assert result.error is None
    # Aviso com o modelo que leu.
    assert result.text.startswith("[Lido pelo Claude (claude-haiku-5-5) a partir de imagem")
    # Conteúdo devolvido pelo Claude.
    assert "Cadastro → Aprovação" in result.text
    # Pedido enviado.
    params = claude.calls[0]
    # Modelo do .env (VISION_MODEL) e esforço baixo.
    assert params["model"] == "claude-haiku-5-5" and params["output_config"] == {"effort": "low"}
    # Primeiro bloco: a imagem em base64, como PNG.
    block = params["messages"][0]["content"][0]
    # Tipo e formato.
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
    # Instrução fixa com as regras.
    assert "[ilegível]" in params["system"]
    # Custo registrado com o propósito da leitura.
    with sessions() as s:
        # A chamada.
        call = s.scalar(select(LLMCall))
        # Propósito e custo.
        assert call.purpose == "leitura_visual" and call.cost_usd > 0


# PDF digitalizado e os erros: páginas demais, resposta cortada, recusa, API fora.
def test_reader_pdf_and_errors(sessions, settings, reader, claude):
    # PDF: vai como documento.
    result = reader.read(make_pdf(), "scan.pdf", "application/pdf")
    # Bloco de documento.
    assert claude.calls[0]["messages"][0]["content"][0]["type"] == "document"
    # Aviso fala em PDF digitalizado.
    assert "a partir de PDF digitalizado" in result.text
    # Limite de páginas zero: nem chama o Claude.
    strict = VisionReader(
        LLMClient(sessions, settings, claude),
        Settings(data_dir=settings.data_dir, vision_max_pdf_pages=0),
    )
    # Chamadas até aqui.
    before = len(claude.calls)
    # Erro de PDF longo.
    assert split_error(strict.read(make_pdf(), "scan.pdf", None).error)[0] == "PDF_LONGO"
    # Nenhuma chamada nova.
    assert len(claude.calls) == before
    # Imagem corrompida: erro claro, sem chamada.
    assert split_error(reader.read(b"x", "a.png", "image/png").error)[0] == "IMAGEM_FORMATO"
    # Resposta cortada pelo limite de tamanho: guarda o texto com o aviso.
    claude.stop = "max_tokens"
    # Lê.
    assert "Texto cortado" in reader.read(make_png(), "a.png", "image/png").text
    # Recusa.
    claude.stop = "refusal"
    # Código da recusa.
    assert split_error(reader.read(make_png(), "a.png", None).error)[0] == "CLAUDE_RECUSOU"
    # API fora do ar.
    claude.error = APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    # Código da falha.
    assert split_error(reader.read(make_png(), "a.png", None).error)[0] == "CLAUDE_FALHOU"


# Fila: o mesmo anexo em dois e-mails é lido uma vez; aviso dispensado não é lido.
def test_queue_reads_email_images(sessions, store, settings, reader, claude):
    # O mesmo diagrama em dois e-mails, e uma foto diferente num terceiro.
    diagram = make_png(900, 700)
    # Três e-mails.
    gmail = FakeGmail(
        {
            "m1": {"raw": make_email(attachments=[("fluxo.png", "image/png", diagram)])},
            "m2": {"raw": make_email("Re: fluxo", [("fluxo.png", "image/png", diagram)])},
            "m3": {"raw": make_email("Foto", [("foto.png", "image/png", make_png(1200, 900))])},
        }
    )
    # Coleta: as imagens entram na fila.
    collect_gmail(sessions, gmail, store, settings, now=NOW)
    # Dispensa o aviso da foto (você decidiu que não interessa).
    with sessions() as s:
        # Id da foto.
        photo_id = s.scalar(select(Attachment.id).where(Attachment.filename == "foto.png"))
        # Dispensa.
        dismiss(s, "anexo", photo_id)
        # Grava.
        s.commit()
    # Rodada da fila.
    summary = vision_queue.run_pending(sessions, store, reader, limit=10)
    # Um arquivo lido (as duas cópias do diagrama), uma chamada só.
    assert (summary.read, summary.failed, len(claude.calls)) == (1, 0, 1)
    # Confere o banco.
    with sessions() as s:
        # As duas cópias do diagrama.
        copies = s.scalars(select(Attachment).where(Attachment.filename == "fluxo.png")).all()
        # Ambas com texto e sem erro.
        assert len(copies) == 2
        # Cada uma.
        assert all(c.extraction_error is None and "Cadastro" in c.extracted_text for c in copies)
        # A foto dispensada continua na fila, sem gasto.
        assert split_error(s.get(Attachment, photo_id).extraction_error)[0] == "IMAGEM_SEM_OCR"
        # Lista de arquivos sem texto vazia (o diagrama saiu, a foto está dispensada).
        assert list_failures(s) == []
        # Registro no Processamento.
        assert s.scalar(select(AuditLog).where(AuditLog.action == "claude.leitura_visual"))


# Teto do mês: a rodada para sem gastar e o arquivo continua na fila.
def test_queue_stops_at_budget(sessions, store, settings, reader, claude, monkeypatch):
    # Um e-mail com um PDF digitalizado.
    gmail = FakeGmail(
        {"m1": {"raw": make_email(attachments=[("scan.pdf", "application/pdf", make_pdf())])}}
    )
    # Coleta.
    collect_gmail(sessions, gmail, store, settings, now=NOW)
    # Teto esgotado.
    _spend_budget(sessions)
    # Rodada direta: para no primeiro arquivo.
    summary = vision_queue.run_pending(sessions, store, reader, limit=10)
    # Teto atingido, nada lido, nenhuma chamada.
    assert summary.budget_hit and summary.read == 0 and claude.calls == []
    # O arquivo continua na fila.
    with sessions() as s:
        # Código de espera.
        assert split_error(s.scalar(select(Attachment.extraction_error)))[0] == "PDF_SEM_TEXTO"
    # A rodada do coletor nem começa com o teto atingido.
    monkeypatch.setattr(vision_queue, "vision_reader", lambda _: reader)
    # Nada a fazer.
    assert vision_queue.run_from_settings(settings) is None


# Erro inesperado num arquivo: sai da fila com o motivo e não trava as rodadas seguintes.
def test_queue_unexpected_error(sessions, store, settings, reader, monkeypatch):
    # Um e-mail com uma imagem.
    gmail = FakeGmail({"m1": {"raw": make_email(attachments=[("a.png", "image/png", make_png())])}})
    # Coleta.
    collect_gmail(sessions, gmail, store, settings, now=NOW)

    # Leitura que quebra (ex.: original apagado do disco).
    def broken(*_):
        raise OSError("arquivo sumiu")

    # Troca a leitura.
    monkeypatch.setattr(reader, "read", broken)
    # Rodada.
    summary = vision_queue.run_pending(sessions, store, reader, limit=10)
    # Contado como erro.
    assert summary.failed == 1
    # Motivo gravado: saiu da fila.
    with sessions() as s:
        # Código.
        assert split_error(s.scalar(select(Attachment.extraction_error)))[0] == "FALHA_LEITURA"
    # A rodada seguinte não tem o que ler.
    assert vision_queue.run_pending(sessions, store, reader, limit=10).failed == 0


# Drive: imagem numa pasta monitorada é lida pela fila; "Tentar de novo" também usa o Claude.
def test_queue_reads_drive_image(sessions, store, settings, reader, claude):
    # Pasta com uma imagem e um PDF digitalizado.
    drive = FakeDrive(
        {
            ROOT: {"name": "CRM"},
            "img1": {
                "name": "Fluxo.png",
                "mimeType": "image/png",
                "data": make_png(),
                "parents": [ROOT],
            },
            "pdf1": {
                "name": "Scan.pdf",
                "mimeType": "application/pdf",
                "data": make_pdf(),
                "parents": [ROOT],
            },
        }
    )
    # Cadastra e varre.
    with sessions() as s:
        # Cadastra a pasta.
        add_folder(s, drive, ROOT)
        # Grava.
        s.commit()
    # Varre: os dois arquivos entram na fila.
    sync_drive(sessions, drive, store, settings, now=NOW)
    # Rodada com limite 1: só a imagem.
    assert vision_queue.run_pending(sessions, store, reader, limit=1).read == 1
    # Confere a imagem.
    with sessions() as s:
        # O item.
        item = s.scalar(select(DriveItem).where(DriveItem.drive_id == "img1"))
        # A versão atual.
        bronze = s.get(BronzeItem, item.bronze_item_id)
        # Texto lido, sem erro, e a impressão digital passa a ser a do texto.
        assert "Cadastro" in bronze.body_text and bronze.extra["erro_extracao"] is None
        # Mesma impressão nos dois lugares (a próxima varredura compara certo).
        assert bronze.content_sha256 == item.content_sha256
        # O PDF: "Tentar de novo" lê com o Claude na hora.
        pdf = s.scalar(select(DriveItem).where(DriveItem.drive_id == "pdf1"))
        # Tenta.
        assert retry_drive_item(s, drive, store, settings, pdf.id, reader) is None
        # Grava.
        s.commit()
    # Duas chamadas no total.
    assert len(claude.calls) == 2


# Console: "Tentar de novo" com o Claude, sem chave e com o teto atingido.
def test_retry_with_claude(sessions, store, settings, reader, claude, monkeypatch):
    # Dois e-mails com PDFs digitalizados diferentes.
    gmail = FakeGmail(
        {
            "m1": {"raw": make_email(attachments=[("a.pdf", "application/pdf", make_pdf())])},
            "m2": {"raw": make_email("Outro", [("b.png", "image/png", make_png(700, 500))])},
            # O mesmo PDF encaminhado: uma cópia que também espera o Claude.
            "m3": {"raw": make_email("Fwd", [("a.pdf", "application/pdf", make_pdf())])},
        }
    )
    # Coleta.
    collect_gmail(sessions, gmail, store, settings, now=NOW)
    # Ids dos anexos.
    with sessions() as s:
        # Por nome.
        ids = dict(s.execute(select(Attachment.filename, Attachment.id)).all())
    # Sem chave: o aviso diz o que falta e o arquivo continua na fila.
    with sessions() as s:
        # Tenta sem leitor.
        error = retry(s, store, settings, "anexo", ids["a.pdf"])
        # Aviso.
        assert error.startswith("[CLAUDE_SEM_CHAVE]")
        # Na fila.
        assert split_error(s.get(Attachment, ids["a.pdf"]).extraction_error)[0] == "PDF_SEM_TEXTO"
    # Pelo console, com o Claude falso: funciona.
    from app import routes_failures

    # Configuração do teste.
    monkeypatch.setattr(routes_failures, "get_settings", lambda: settings)
    # Leitor com o Claude falso.
    monkeypatch.setattr(routes_failures, "vision_reader", lambda _: reader)
    # Cliente.
    client = TestClient(app)
    # Tenta o PDF.
    client.post(
        "/arquivos/tentar",
        data={"origem": "anexo", "id": ids["a.pdf"]},
        headers=SAME,
        follow_redirects=False,
    )
    # Aviso de sucesso.
    assert "Funcionou" in client.get("/", headers=OWNER).text
    # A cópia do PDF no e-mail encaminhado também ficou resolvida, com uma chamada só.
    with sessions() as s:
        # Todas as cópias de a.pdf.
        copies = s.scalars(select(Attachment).where(Attachment.filename == "a.pdf")).all()
        # Duas, ambas com texto.
        assert len(copies) == 2 and all(c.extraction_error is None for c in copies)
    # Uma chamada só ao Claude.
    assert len(claude.calls) == 1
    # Teto esgotado: a imagem fica na fila e o aviso diz por quê.
    _spend_budget(sessions)
    # Tenta a imagem.
    with sessions() as s:
        # Aviso do teto.
        assert retry(s, store, settings, "anexo", ids["b.png"], vision=reader).startswith(
            "[CLAUDE_TETO_MENSAL]"
        )
        # Continua na fila.
        assert split_error(s.get(Attachment, ids["b.png"]).extraction_error)[0] == "IMAGEM_SEM_OCR"
