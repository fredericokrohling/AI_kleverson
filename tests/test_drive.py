"""Testes do monitor de pastas do Drive, das tags (com herança) e das páginas de navegação.

Nenhum teste chama o Google: o FakeDrive (tests/fakes.py) imita files.get, files.list,
export e get_media.
"""

# Data e hora com fuso, e intervalos.
from datetime import UTC, datetime, timedelta

# Biblioteca de testes.
import pytest

# Simula o navegador chamando o console.
from fastapi.testclient import TestClient

# Consultas e SQL escrito à mão.
from sqlalchemy import select, text

# Configuração.
from app.config import Settings

# Explicação dos erros da API do Drive.
from app.ingestion.drive import explain

# O que está sendo testado: cadastro e varredura.
from app.ingestion.drive_sync import (
    add_folder,
    deactivate_folder,
    drive_scan_due,
    parse_folder_ref,
    sync_drive,
)

# Extração, para simular um extrator melhor, e separação do código do erro.
from app.ingestion.extractors import Extraction, split_error

# Arquivos sem texto: lista e "Tentar de novo".
from app.ingestion.failures import list_failures, retry

# Coleta do Gmail, para gerar um anexo com erro.
from app.ingestion.gmail import collect_gmail

# Guarda dos originais.
from app.ingestion.storage import FileStore

# O app, para as páginas.
from app.main import app

# Tabelas.
from app.models import Attachment, AuditLog, BronzeItem, DriveFolder, DriveItem, SourceKind

# Tags.
from app.tags import (
    assign_tag,
    create_tag,
    delete_tag,
    effective_tags,
    rename_tag,
    unassign_tag,
)

# Falsos e geradores de arquivos.
from tests.fakes import FakeDrive, FakeGmail, http_error, make_docx, make_email, make_pdf, make_xlsx

# Id da pasta monitorada (com o tamanho mínimo de um id do Drive).
ROOT = "pastaCRM000001"
# Subpasta dentro dela.
SUB = "subContratos01"
# Momento fixo.
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient (exigida nos POST).
SAME = {**OWNER, "Origin": "http://testserver"}
# Tipo das planilhas do Google.
SHEET = "application/vnd.google-apps.spreadsheet"


# Configuração com a pasta de arquivos temporária.
@pytest.fixture
def settings(tmp_path):
    # Pasta nova a cada teste.
    return Settings(data_dir=str(tmp_path))


# Guarda dos originais.
@pytest.fixture
def store(settings):
    # Na pasta temporária.
    return FileStore(settings.data_dir)


# Drive falso com uma árvore: CRM / {Contratos / Contrato, Cronograma, Ata.pdf, Video.pdf}.
@pytest.fixture
def drive():
    # Árvore de exemplo.
    return FakeDrive(
        {
            # A pasta monitorada.
            ROOT: {"name": "CRM"},
            # Subpasta.
            SUB: {"name": "Contratos", "parents": [ROOT]},
            # Google Docs dentro da subpasta.
            "doc1": {
                "name": "Contrato",
                "mimeType": "application/vnd.google-apps.document",
                "data": make_docx(),
                "parents": [SUB],
            },
            # Google Sheets na raiz.
            "plan1": {
                "name": "Cronograma",
                "mimeType": SHEET,
                "data": make_xlsx(),
                "parents": [ROOT],
            },
            # PDF comum.
            "pdf1": {
                "name": "Ata.pdf",
                "mimeType": "application/pdf",
                "data": make_pdf("Ata da reuniao"),
                "parents": [ROOT],
            },
            # PDF grande demais.
            "big1": {
                "name": "Video.pdf",
                "mimeType": "application/pdf",
                "data": b"x",
                "size": 999_999_999,
                "parents": [ROOT],
            },
            # Arquivo fora da pasta: nunca deve aparecer.
            "fora": {"name": "Pessoal.pdf", "mimeType": "application/pdf", "data": b"x"},
        }
    )


# Cadastra a pasta e grava.
def _add(sessions, drive) -> int:
    # Uma sessão.
    with sessions() as s:
        # Cadastra pelo link, como no console.
        folder = add_folder(s, drive, f"https://drive.google.com/drive/u/0/folders/{ROOT}?usp=x")
        # Grava.
        s.commit()
        # Id.
        return folder.id


# Item pelo id do Drive.
def _item(s, drive_id: str) -> DriveItem:
    # Consulta.
    return s.scalar(select(DriveItem).where(DriveItem.drive_id == drive_id))


# Nomes das tags efetivas de um item, pelo id do Drive.
def _names(s, drive_id: str) -> set[str]:
    # Id interno.
    item_id = _item(s, drive_id).id
    # Nomes.
    return {t.name for t in effective_tags(s, [item_id])[item_id]}


# Links e ids colados são reconhecidos; texto qualquer não.
@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (f"https://drive.google.com/drive/folders/{ROOT}", ROOT),
        (f"https://drive.google.com/drive/u/1/folders/{ROOT}?usp=sharing", ROOT),
        (f"https://drive.google.com/open?id={ROOT}", ROOT),
        (f"  {ROOT} ", ROOT),
        ("minha pasta", None),
    ],
)
def test_parse_folder_ref(ref, expected):
    # Confere.
    assert parse_folder_ref(ref) == expected


# Cadastro: só pastas acessíveis.
def test_add_folder_validates(sessions, drive):
    # Um arquivo com id de tamanho real.
    drive.files_by_id["arquivoPdf001"] = {"name": "a.pdf", "mimeType": "application/pdf"}
    # Uma sessão.
    with sessions() as s:
        # Arquivo, não pasta.
        with pytest.raises(ValueError, match="não é uma pasta"):
            # Tenta cadastrar um PDF.
            add_folder(s, drive, "arquivoPdf001")
        # Inexistente ou sem acesso.
        with pytest.raises(ValueError, match="Sem acesso"):
            # Tenta um id inexistente.
            add_folder(s, drive, "naoexiste123")
        # Texto que não é link.
        with pytest.raises(ValueError, match="Não reconheci"):
            # Tenta um texto qualquer.
            add_folder(s, drive, "a pasta do CRM")


# Primeira varredura: espelha a árvore e grava uma versão por arquivo lido.
def test_first_scan(sessions, drive, store, settings):
    # Cadastra.
    _add(sessions, drive)
    # Varre.
    summary = sync_drive(sessions, drive, store, settings, now=NOW)
    # Raiz: 4 itens (paginados de 2 em 2); subpasta: 1. O PDF grande falha.
    assert (summary.items_seen, summary.new_versions, summary.file_errors) == (5, 3, 1)
    # Confere o banco.
    with sessions() as s:
        # Caminho legível do documento dentro da subpasta.
        doc = _item(s, "doc1")
        # Caminho e pasta-mãe.
        assert doc.path == "CRM/Contratos/Contrato" and doc.parent_drive_id == SUB
        # Versão no bronze.
        bronze = s.get(BronzeItem, doc.bronze_item_id)
        # Fonte, id com versão e texto exportado do Google Docs.
        assert bronze.source == SourceKind.DRIVE and bronze.external_id == "doc1:1"
        # Metadados.
        assert bronze.extra["drive_id"] == "doc1" and bronze.extra["caminho"] == doc.path
        # O original exportado foi guardado.
        assert store.read(bronze.extra["arquivo"])[:2] == b"PK"
        # Planilha: texto com as abas.
        plan = s.get(BronzeItem, _item(s, "plan1").bronze_item_id)
        # Aba do cronograma.
        assert "### Planilha: Cronograma" in plan.body_text
        # PDF grande: sem versão, com o motivo.
        big = _item(s, "big1")
        # Motivo.
        assert big.bronze_item_id is None and "DRIVE_MAX_FILE_MB" in big.error
        # O arquivo de fora nunca entrou.
        assert _item(s, "fora") is None
        # A pasta ficou com a varredura registrada.
        assert s.scalar(select(DriveFolder)).last_scan_at == NOW


# Varreduras seguintes: só grava o que mudou de verdade.
def test_versions_only_when_text_changes(sessions, drive, store, settings):
    # Cadastra e varre.
    _add(sessions, drive)
    # Primeira.
    sync_drive(sessions, drive, store, settings, now=NOW)
    # Downloads da primeira.
    first = len(drive.downloads)
    # Segunda, nada mudou.
    again = sync_drive(sessions, drive, store, settings, now=NOW + timedelta(minutes=30))
    # Nenhuma versão nova e nenhum download (a versão do Drive é a mesma).
    assert again.new_versions == 0 and len(drive.downloads) == first
    # A planilha muda de versão sem mudar o texto (ex.: alguém só renomeou uma aba de volta).
    drive.files_by_id["plan1"]["version"] = 2
    # O PDF muda de verdade.
    drive.files_by_id["pdf1"].update(version=2, data=make_pdf("Ata revisada"))
    # Terceira.
    third = sync_drive(sessions, drive, store, settings, now=NOW + timedelta(hours=1))
    # Só o PDF virou versão nova.
    assert third.new_versions == 1
    # Confere.
    with sessions() as s:
        # Versões do PDF.
        versions = s.scalars(
            select(BronzeItem.external_id).where(BronzeItem.thread_id == "pdf1")
        ).all()
        # As duas, com a antiga preservada.
        assert sorted(versions) == ["pdf1:1", "pdf1:2"]
        # A planilha ficou com a versão 2 registrada, mas uma linha só no bronze.
        assert _item(s, "plan1").version == "2"
        # Uma linha só.
        assert (
            s.scalar(
                select(text("count(*)"))
                .select_from(BronzeItem)
                .where(BronzeItem.thread_id == "plan1")
            )
            == 1
        )


# Arquivo que sai da pasta fica marcado; o que volta, desmarcado.
def test_removed_items(sessions, drive, store, settings):
    # Cadastra e varre.
    _add(sessions, drive)
    # Primeira.
    sync_drive(sessions, drive, store, settings, now=NOW)
    # O PDF vai para a lixeira.
    drive.files_by_id["pdf1"]["trashed"] = True
    # Varre de novo.
    sync_drive(sessions, drive, store, settings, now=NOW + timedelta(minutes=30))
    # Confere.
    with sessions() as s:
        # Marcado como removido; a versão no bronze continua.
        pdf = _item(s, "pdf1")
        # Removido, com versão.
        assert pdf.removed and pdf.bronze_item_id is not None
        # A raiz nunca é marcada.
        assert not _item(s, ROOT).removed


# Uma pasta que falha não impede as outras e guarda o motivo.
def test_folder_error_is_recorded(sessions, drive, store, settings):
    # Segunda pasta monitorada, independente.
    drive.files_by_id["outraPasta001"] = {"name": "Outra"}
    # Arquivo nela.
    drive.files_by_id["nota1"] = {
        "name": "nota.txt",
        "mimeType": "text/plain",
        "data": b"oi",
        "parents": ["outraPasta001"],
    }
    # Cadastra as duas.
    _add(sessions, drive)
    # Segunda.
    with sessions() as s:
        # Pelo id.
        add_folder(s, drive, "outraPasta001")
        # Grava.
        s.commit()
    # A listagem da subpasta do CRM falha.
    drive.failing.add(SUB)
    # Varre.
    summary = sync_drive(sessions, drive, store, settings, now=NOW)
    # Uma pasta com erro; a outra foi lida.
    assert summary.folder_errors == 1 and summary.new_versions >= 1
    # Confere.
    with sessions() as s:
        # Pastas por nome.
        folders = {f.name: f for f in s.scalars(select(DriveFolder))}
        # O motivo ficou na pasta CRM.
        assert "HttpError" in folders["CRM"].last_error
        # A outra está em dia.
        assert folders["Outra"].last_error is None and _item(s, "nota1").bronze_item_id


# Agendador: pasta nova é varrida já; depois, só no intervalo.
def test_drive_scan_due(sessions, drive, store, settings):
    # Sem pasta: nada a fazer.
    with sessions() as s:
        # Nada.
        assert not drive_scan_due(s, settings, NOW)
    # Cadastra: nunca varrida.
    folder_id = _add(sessions, drive)
    # Na hora.
    with sessions() as s:
        # Precisa.
        assert drive_scan_due(s, settings, NOW)
    # Varre.
    sync_drive(sessions, drive, store, settings, now=NOW)
    # Logo depois: não; depois do intervalo: sim.
    with sessions() as s:
        # 10 minutos depois.
        assert not drive_scan_due(s, settings, NOW + timedelta(minutes=10))
        # 30 minutos depois.
        assert drive_scan_due(s, settings, NOW + timedelta(minutes=30))
        # Desligada, não conta mais.
        deactivate_folder(s, folder_id)
        # Grava.
        s.commit()
        # Nada.
        assert not drive_scan_due(s, settings, NOW + timedelta(days=1))


# Tags: CRUD com nomes únicos, sem diferenciar maiúsculas.
def test_tag_crud(sessions):
    # Uma sessão.
    with sessions() as s:
        # Cria.
        tag = create_tag(s, "  Projeto   CRM ")
        # Espaços normalizados.
        assert tag.name == "Projeto CRM"
        # Repetida com outras maiúsculas.
        with pytest.raises(ValueError, match="já existe"):
            # Mesmo nome, minúsculas.
            create_tag(s, "projeto crm")
        # Vazia.
        with pytest.raises(ValueError):
            # Só espaços.
            create_tag(s, "   ")
        # Outra.
        other = create_tag(s, "Arquitetura")
        # Renomear para um nome em uso.
        with pytest.raises(ValueError, match="já existe"):
            # Nome da outra tag.
            rename_tag(s, other.id, "PROJETO CRM")
        # Renomear a própria (só maiúsculas) pode.
        assert rename_tag(s, tag.id, "projeto crm").name == "projeto crm"
        # Grava.
        s.commit()
        # Cada ação ficou na auditoria.
        actions = s.scalars(select(AuditLog.action)).all()
        # Criar e renomear.
        assert actions.count("tag.criar") == 2 and "tag.renomear" in actions


# Herança: tag na pasta vale para tudo dentro, inclusive arquivos que chegam depois.
def test_tags_are_inherited(sessions, drive, store, settings):
    # Cadastra e varre.
    _add(sessions, drive)
    # Varre.
    sync_drive(sessions, drive, store, settings, now=NOW)
    # Tags na raiz e no documento.
    with sessions() as s:
        # Raiz: Projeto CRM.
        crm = assign_tag(s, _item(s, ROOT).id, "Projeto CRM")
        # Raiz também: Arquitetura.
        assign_tag(s, _item(s, ROOT).id, "Arquitetura")
        # Documento: Jurídico (própria) e Projeto CRM (repetida, própria também).
        assign_tag(s, _item(s, "doc1").id, "Jurídico")
        # Repetida.
        assign_tag(s, _item(s, "doc1").id, "projeto crm")
        # Grava.
        s.commit()
        # Tags efetivas do documento (dois níveis abaixo da raiz).
        doc_tags = effective_tags(s, [_item(s, "doc1").id])[_item(s, "doc1").id]
        # Nome → herdada?
        names = {t.name: t.inherited for t in doc_tags}
        # Arquitetura vem de cima; Jurídico e Projeto CRM foram colocadas nele.
        assert names == {"Arquitetura": True, "Jurídico": False, "Projeto CRM": False}
        # A subpasta herda as da raiz.
        sub_tags = effective_tags(s, [_item(s, SUB).id])[_item(s, SUB).id]
        # Só herdadas.
        assert {(t.name, t.inherited) for t in sub_tags} == {
            ("Arquitetura", True),
            ("Projeto CRM", True),
        }
    # Um arquivo novo chega na subpasta depois das tags.
    drive.files_by_id["novo1"] = {
        "name": "Aditivo.pdf",
        "mimeType": "application/pdf",
        "data": make_pdf("Aditivo"),
        "parents": [SUB],
    }
    # Varre.
    sync_drive(sessions, drive, store, settings, now=NOW + timedelta(minutes=30))
    # A versão no bronze já nasce com as tags (pela view bronze_item_tags).
    with sessions() as s:
        # Versão do arquivo novo.
        bronze_id = _item(s, "novo1").bronze_item_id
        # Tags pela view.
        rows = s.execute(
            text(
                "SELECT tag, herdada FROM bronze_item_tags WHERE bronze_item_id = :i ORDER BY tag"
            ),
            {"i": bronze_id},
        ).all()
        # As duas da raiz, herdadas.
        assert [tuple(r) for r in rows] == [("Arquitetura", True), ("Projeto CRM", True)]
        # Renomear vale na hora para todos.
        rename_tag(s, crm.id, "CRM")
        # Grava.
        s.commit()
        # Nome novo.
        assert "CRM" in _names(s, "novo1")
        # Tirar da raiz: some dos herdeiros, mas fica onde foi colocada diretamente.
        unassign_tag(s, _item(s, ROOT).id, crm.id)
        # Grava.
        s.commit()
        # O arquivo novo perdeu; o documento mantém a própria.
        assert "CRM" not in _names(s, "novo1")
        # O documento.
        assert "CRM" in _names(s, "doc1")
        # Apagar a tag: some de todos.
        delete_tag(s, crm.id)
        # Grava.
        s.commit()
        # Ninguém mais tem.
        assert "CRM" not in _names(s, "doc1")


# Páginas: Configurações, navegação e as ações de tag pelo formulário.
def test_config_and_browse_pages(sessions, drive, store, settings, monkeypatch):
    # O console usa o Drive falso ao cadastrar.
    from app import routes_config

    # Troca a fábrica do cliente.
    monkeypatch.setattr(routes_config, "drive_client", lambda db, settings: drive)
    # Cliente do navegador simulado.
    client = TestClient(app)
    # Cadastra a pasta pelo formulário.
    response = client.post(
        "/config/drive/pastas",
        data={"link": f"https://drive.google.com/drive/folders/{ROOT}"},
        headers=SAME,
        follow_redirects=False,
    )
    # Redireciona para a seção.
    assert response.status_code == 303 and response.headers["location"] == "/config#drive"
    # A página mostra a pasta e o aviso.
    page = client.get("/config", headers=OWNER).text
    # Nome e aviso.
    assert "CRM" in page and "cadastrada" in page
    # Varre (como o agendador faria).
    sync_drive(sessions, drive, store, settings, now=NOW)
    # Ids dos itens.
    with sessions() as s:
        # Raiz e subpasta.
        root_id, sub_id = _item(s, ROOT).id, _item(s, SUB).id
    # Coloca uma tag na raiz pelo formulário, voltando para a página da raiz.
    client.post(
        f"/config/drive/itens/{root_id}/tags",
        data={"nome": "Projeto CRM", "voltar": f"/config/drive/itens/{root_id}"},
        headers=SAME,
    )
    # Página da subpasta: mostra o documento e a tag herdada.
    page = client.get(f"/config/drive/itens/{sub_id}", headers=OWNER).text
    # Documento e tag tracejada.
    assert "Contrato" in page and 'class="tag herdada"' in page and "Projeto CRM" in page
    # Caminho de pastas no topo.
    assert f'href="/config/drive/itens/{root_id}">CRM</a>' in page
    # "voltar" para fora do console é ignorado (não vira redirecionamento para outro site).
    response = client.post(
        f"/config/drive/itens/{root_id}/tags",
        data={"nome": "X", "voltar": "https://exemplo.com"},
        headers=SAME,
        follow_redirects=False,
    )
    # Volta para a própria página.
    assert response.headers["location"] == f"/config/drive/itens/{root_id}"
    # Para de monitorar.
    with sessions() as s:
        # Id da pasta.
        folder_id = s.scalar(select(DriveFolder.id))
    # Pelo formulário.
    client.post(f"/config/drive/pastas/{folder_id}/remover", headers=SAME)
    # Desligada, mas os itens continuam.
    with sessions() as s:
        # Pasta.
        assert not s.get(DriveFolder, folder_id).active and _item(s, "doc1") is not None


# Cadastro com Google desconectado: aviso de erro, nada gravado.
def test_add_folder_without_google(sessions):
    # Cliente.
    client = TestClient(app)
    # Sem conta Google no banco, o cliente do Drive não pode ser criado. Sem seguir o
    # redirecionamento, para o aviso ser lido pela página abaixo.
    client.post("/config/drive/pastas", data={"link": ROOT}, headers=SAME, follow_redirects=False)
    # O aviso aparece.
    assert "Nenhuma conta Google conectada" in client.get("/config", headers=OWNER).text
    # Nada gravado.
    with sessions() as s:
        # Sem pastas.
        assert s.scalar(select(DriveFolder)) is None


# --- Arquivos sem texto e "Tentar de novo" ---------------------------------------


# O motivo informado pelo Google vira código e explicação; sem motivo, vale o HTTP.
@pytest.mark.parametrize(
    ("status", "reason", "code"),
    [
        (403, "cannotDownloadFile", "DRIVE_DOWNLOAD_BLOQUEADO"),
        (403, "cannotExportFile", "DRIVE_DOWNLOAD_BLOQUEADO"),
        (403, "exportSizeLimitExceeded", "DRIVE_EXPORTACAO_GRANDE"),
        (403, "domainPolicy", "DRIVE_POLITICA_EMPRESA"),
        (403, "insufficientFilePermissions", "DRIVE_SEM_ACESSO"),
        (404, None, "DRIVE_SEM_ACESSO"),
        (500, None, "DRIVE_HTTP_500"),
    ],
)
def test_explain_reasons(status, reason, code):
    # Separa o código da mensagem.
    found, message = split_error(explain(http_error(status, reason)))
    # Código esperado, com o HTTP na explicação.
    assert found == code and f"HTTP {status}" in message


# Mensagens antigas, sem código, continuam aparecendo.
def test_split_error_without_code():
    # Sem código.
    assert split_error("Formato antigo") == (None, "Formato antigo")
    # Sem erro.
    assert split_error(None) == (None, "")


# Download bloqueado pelo dono: aparece na lista; depois de liberado, "Tentar de novo" lê.
def test_retry_blocked_download(sessions, drive, store, settings):
    # O dono do PDF bloqueou o download para quem só lê.
    drive.blocked["pdf1"] = "cannotDownloadFile"
    # Cadastra e varre.
    _add(sessions, drive)
    # Varre.
    sync_drive(sessions, drive, store, settings, now=NOW)
    # Confere a lista.
    with sessions() as s:
        # Linhas por nome.
        rows = {f.name: f for f in list_failures(s)}
        # O bloqueado e o grande demais, ambos com código; pastas e o resto não entram.
        assert set(rows) == {"Ata.pdf", "Video.pdf"}
        # Código do bloqueio.
        assert rows["Ata.pdf"].code == "DRIVE_DOWNLOAD_BLOQUEADO"
        # Código do tamanho.
        assert rows["Video.pdf"].code == "DRIVE_ARQUIVO_GRANDE"
        # Id para o botão.
        pdf_id = rows["Ata.pdf"].ref_id
        # Sem a conta Google, não dá para baixar de novo.
        assert retry(s, store, settings, "drive", pdf_id).startswith("[GOOGLE_DESCONECTADO]")
        # Ainda bloqueado: a mesma explicação volta.
        assert "DRIVE_DOWNLOAD_BLOQUEADO" in retry(s, store, settings, "drive", pdf_id, drive)
        # O dono libera o download.
        del drive.blocked["pdf1"]
        # Agora funciona.
        assert retry(s, store, settings, "drive", pdf_id, drive) is None
        # Grava.
        s.commit()
    # Confere.
    with sessions() as s:
        # O PDF ganhou a versão com texto e saiu da lista.
        assert "Ata da reuniao" in s.get(BronzeItem, _item(s, "pdf1").bronze_item_id).body_text
        # Sai da lista.
        assert {f.name for f in list_failures(s)} == {"Video.pdf"}
        # As três tentativas ficaram na auditoria.
        assert (
            s.scalar(
                select(text("count(*)"))
                .select_from(AuditLog)
                .where(AuditLog.action == "arquivo.tentar_de_novo")
            )
            == 3
        )


# Arquivo baixado mas sem texto: "Tentar de novo" relê o original, sem chamar o Drive.
def test_retry_reextracts_drive_version(sessions, drive, store, settings, monkeypatch):
    # Um PDF escaneado (sem texto) na pasta.
    drive.files_by_id["scan1"] = {
        "name": "Arquitetura.pdf",
        "mimeType": "application/pdf",
        "data": make_pdf(),
        "parents": [ROOT],
    }
    # Cadastra e varre.
    _add(sessions, drive)
    # Varre.
    sync_drive(sessions, drive, store, settings, now=NOW)
    # Downloads até aqui.
    downloads = len(drive.downloads)
    # Simula um extrator melhor (por exemplo, com leitura de imagem).
    from app.ingestion import drive_sync

    # Troca o extrator.
    monkeypatch.setattr(drive_sync, "extract_text", lambda *a: Extraction("Diagrama: CRM -> ERP"))
    # Tenta de novo.
    with sessions() as s:
        # Linha do escaneado.
        row = next(f for f in list_failures(s) if f.name == "Arquitetura.pdf")
        # Código do PDF sem texto.
        assert row.code == "PDF_SEM_TEXTO"
        # Sem cliente do Drive: não precisa dele.
        assert retry(s, store, settings, "drive", row.ref_id) is None
        # Grava.
        s.commit()
    # Confere.
    with sessions() as s:
        # Versão atual.
        bronze = s.get(BronzeItem, _item(s, "scan1").bronze_item_id)
        # Texto novo, sem erro.
        assert bronze.body_text == "Diagrama: CRM -> ERP" and bronze.extra["erro_extracao"] is None
    # Nenhum download novo.
    assert len(drive.downloads) == downloads


# Anexo de e-mail: lista na página inicial, botões e avisos com o código.
def test_failures_on_home_page(sessions, store, settings, monkeypatch):
    # Um e-mail com um PDF escaneado.
    gmail = FakeGmail(
        {"m1": {"raw": make_email(attachments=[("scan.pdf", "application/pdf", make_pdf())])}}
    )
    # Coleta.
    collect_gmail(sessions, gmail, store, settings, now=NOW)
    # As rotas usam a configuração do teste (mesma pasta de originais).
    from app import routes_failures

    # Troca a configuração.
    monkeypatch.setattr(routes_failures, "get_settings", lambda: settings)
    # Cliente.
    client = TestClient(app)
    # A página inicial mostra o anexo, o assunto e o código.
    page = client.get("/", headers=OWNER).text
    # Nome, origem e código.
    assert "scan.pdf" in page and "Cronograma do Projeto Atlas" in page and "PDF_SEM_TEXTO" in page
    # Id do anexo.
    with sessions() as s:
        # O anexo.
        attachment_id = s.scalar(select(Attachment.id).where(Attachment.filename == "scan.pdf"))
    # Tenta de novo: continua sem texto, e o aviso mostra o código.
    client.post(
        "/arquivos/tentar",
        data={"origem": "anexo", "id": attachment_id},
        headers=SAME,
        follow_redirects=False,
    )
    # Aviso de erro.
    assert "Ainda com erro: [PDF_SEM_TEXTO]" in client.get("/", headers=OWNER).text
    # Origem adulterada: aviso, sem quebrar.
    client.post(
        "/arquivos/tentar",
        data={"origem": "outra", "id": attachment_id},
        headers=SAME,
        follow_redirects=False,
    )
    # Aviso.
    assert "Origem desconhecida" in client.get("/", headers=OWNER).text
    # Simula um extrator melhor.
    from app.ingestion import failures

    # Troca o extrator.
    monkeypatch.setattr(failures, "extract_text", lambda *a: Extraction("texto por OCR"))
    # Tenta todos.
    client.post("/arquivos/tentar-todos", headers=SAME, follow_redirects=False)
    # Aviso com as contagens; a lista some.
    page = client.get("/", headers=OWNER).text
    # Contagens.
    assert "1 arquivo(s) tentado(s), 1 agora com texto." in page
    # O anexo ficou com texto.
    with sessions() as s:
        # Texto gravado.
        assert s.get(Attachment, attachment_id).extracted_text == "texto por OCR"
    # Sem o cabeçalho de origem, o POST é recusado.
    assert client.post("/arquivos/tentar-todos", headers=OWNER).status_code == 403
