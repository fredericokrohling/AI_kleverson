"""Testes de Cadastros: projetos, pessoas, falas por pessoa e tags automáticas."""

# io.BytesIO: arquivo na memória.
import io

# Data e hora com fuso.
from datetime import UTC, datetime

# Monta e-mails de teste.
from email.message import EmailMessage

# Biblioteca de testes.
import pytest

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import select

# Rotas (para trocar a agenda e o Gmail pelos falsos).
from app import routes_registry

# Configuração.
from app.config import Settings, get_settings

# Coleta do Gmail.
from app.ingestion.gmail import collect_gmail

# Guarda dos originais.
from app.ingestion.storage import FileStore

# O app.
from app.main import app

# Tabelas.
from app.models import (
    BronzeItem,
    Project,
    SourceKind,
    Stakeholder,
    Tag,
    TaggedItem,
    TranscriptSegment,
    Utterance,
)

# Reconhecimento de pessoas e falas.
from app.people import PeopleIndex, backfill_utterances, normalize

# Regras de tag.
from app.project_tags import apply_rules_to_all, own_text

# Cadastro.
from app.projects import create_project, save_person, set_member
from app.transcription import routes_import

# Leitura de documentos de transcrição.
from app.transcription.importer import read_transcript

# Finalização da transcrição ao vivo.
from app.transcription.store import finalize

# Falsos.
from tests.fakes import FakeCalendar, FakeGmail

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient.
SAME = {**OWNER, "Origin": "http://testserver"}
# Um cliente para o arquivo inteiro.
client = TestClient(app)
# Momento fixo.
NOW = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)

# Legenda do Teams com três pessoas: uma pelo nome com "(Convidado)", uma por apelido e
# uma desconhecida.
VTT = """WEBVTT

00:00:03.000 --> 00:00:05.000
<v Márcia Lopes (Convidado)>Precisamos antecipar a entrega do Atlas.</v>

00:00:06.000 --> 00:00:08.000
<v Beto>Vou verificar com o fornecedor.</v>

00:00:09.000 --> 00:00:10.000
<v Zé Desconhecido>Ok.</v>
"""


# Um e-mail simples em texto.
def email(sender: str, subject: str, body: str) -> bytes:
    # E-mail novo.
    msg = EmailMessage()
    # Remetente.
    msg["From"] = sender
    # Destinatário.
    msg["To"] = "dona@exemplo.com"
    # Assunto.
    msg["Subject"] = subject
    # Corpo.
    msg.set_content(body)
    # Bytes.
    return msg.as_bytes()


# Um .docx com os parágrafos dados.
def docx(paragraphs: list[str]) -> bytes:
    # Biblioteca do Word.
    import docx as word

    # Documento novo.
    document = word.Document()
    # Cada parágrafo.
    for text in paragraphs:
        # Acrescenta.
        document.add_paragraph(text)
    # Salva na memória.
    buffer = io.BytesIO()
    # Grava.
    document.save(buffer)
    # Bytes.
    return buffer.getvalue()


# Projeto Atlas com Márcia (diretora, liderança na empresa e no projeto) e Roberto ("Beto").
@pytest.fixture
def atlas(sessions):
    # Uma sessão.
    with sessions() as session:
        # Projeto (cria a tag "Projeto Atlas").
        project = create_project(session, "Projeto Atlas", "ATL", "Atlas")
        # Diretora.
        marcia = save_person(
            session,
            None,
            name="Márcia Lopes",
            email="marcia@cliente.com",
            role="Diretora",
            is_leader=True,
        )
        # Analista, conhecido como "Beto".
        beto = save_person(
            session, None, name="Roberto Lima", email="beto@cliente.com", aliases="Beto"
        )
        # Equipe: Márcia é patrocinadora e liderança no projeto.
        set_member(session, project.id, marcia.id, "Patrocinadora", True)
        # Roberto, analista.
        set_member(session, project.id, beto.id, "Analista", False)
        # Grava.
        session.commit()
        # Ids para os testes.
        return {"project": project.id, "tag": project.tag_id, "marcia": marcia.id, "beto": beto.id}


# Nomes são comparados sem acento, sem maiúsculas e sem "(Convidado)".
def test_normalize_and_match(sessions, atlas):
    # Forma comparável.
    assert normalize("  Márcia  LOPES (Convidado) ") == "marcia lopes"
    # Índice das pessoas.
    with sessions() as session:
        # Monta.
        people = PeopleIndex(session)
        # Pelo nome, pelo apelido e pelo e-mail.
        assert people.match("marcia lopes") == atlas["marcia"]
        # Apelido.
        assert people.match("BETO") == atlas["beto"]
        # E-mail.
        assert people.match("beto@cliente.com") == atlas["beto"]
        # Pelo e-mail de um convidado da agenda com esse nome.
        assert people.match("M. Lopes", {"m. lopes": "marcia@cliente.com"}) == atlas["marcia"]
        # Desconhecido.
        assert people.match("Zé") is None
        # Duas pessoas com o mesmo nome: não liga (errar é pior que não saber).
        save_person(session, None, name="Roberto Lima", email="outro@x.com")
        # Ambíguo.
        assert PeopleIndex(session).match("Roberto Lima") is None


# Falas do .docx do Teams e do documento do Meet são separadas por pessoa.
def test_document_speakers():
    # Formato do Teams: "Nome   0:03" e o texto abaixo.
    teams = read_transcript(
        docx(
            [
                "Status semanal",
                "Transcrição",
                "Ana Souza   0:03",
                "Bom dia.",
                "João Lima   1:10",
                "O marco atrasou.",
                "Continua aqui.",
            ]
        ),
        "Status.docx",
    )
    # Duas falas, com o tempo.
    assert teams.text.splitlines() == [
        "[00:03] Ana Souza: Bom dia.",
        "[01:10] João Lima: O marco atrasou. Continua aqui.",
    ]
    # Formato do Meet: linhas de tempo soltas e "Nome: texto".
    meet = read_transcript(
        docx(
            [
                "Status - Transcrição",
                "00:00:00",
                "Ana Souza: Bom dia.",
                "João Lima: Oi.",
                "00:05:00",
                "Ana Souza: Próximo item.",
            ]
        ),
        "Status - Transcrição.docx",
    )
    # Três falas; a última no tempo 05:00.
    assert meet.text.splitlines()[-1] == "[05:00] Ana Souza: Próximo item."
    # Quem falou.
    assert meet.speakers == ["Ana Souza", "João Lima"]
    # Ata comum com poucas linhas "Tópico: ..." não vira transcrição.
    ata = read_transcript(
        docx(
            [
                "Ata da reunião",
                "Participantes presentes",
                "Decisão: aprovar o escopo",
                "Próximos passos definidos",
                "Revisar o cronograma",
            ]
        ),
        "ata.docx",
    )
    # Sem falas.
    assert ata.turns is None


# A transcrição importada grava uma fala por linha, ligada às pessoas, e recebe a tag.
def test_imported_transcript_links_people(sessions, atlas, monkeypatch, tmp_path):
    # Originais numa pasta temporária.
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    # Agenda sem reuniões (importação fora da agenda).
    monkeypatch.setattr(routes_import, "calendar_client", lambda db, s: FakeCalendar([]))
    # Importa.
    page = client.post(
        "/transcricoes/importar",
        data={"data": "2026-11-02", "evento": routes_import.OUTSIDE_CALENDAR, "titulo": "Comitê"},
        files={"arquivo": ("comite.vtt", VTT.encode(), "text/vtt")},
        headers=SAME,
    ).text
    # Sucesso.
    assert "3 falas, 3 pessoas" in page
    # Falas gravadas.
    with sessions() as session:
        # Em ordem.
        rows = session.scalars(select(Utterance).order_by(Utterance.position)).all()
        # Márcia (nome com "(Convidado)"), Beto (apelido) e um desconhecido.
        assert [r.stakeholder_id for r in rows] == [atlas["marcia"], atlas["beto"], None]
        # O texto da fala da diretora.
        assert rows[0].text == "Precisamos antecipar a entrega do Atlas."
        # A reunião recebeu a tag do projeto pela equipe.
        item = session.scalars(select(BronzeItem)).one()
        # Tag e regra.
        assert session.scalars(select(TaggedItem.rule)).all() == ["equipe"]
    # A página da reunião mostra quem falou e a liderança.
    detail = client.get(f"/conteudo/{item.id}", headers=OWNER).text
    # Liderança na empresa e no projeto.
    assert "liderança na empresa" in detail and "liderança no Projeto Atlas" in detail
    # O desconhecido aparece como não reconhecido.
    assert "não reconhecida" in detail
    # Sem e-mail (a chave da pessoa), o cadastro é recusado.
    client.post(
        "/cadastros/pessoas",
        data={"nome": "José Silva", "outros_nomes": "Zé Desconhecido"},
        headers=SAME,
        # Sem seguir o redirecionamento, para o aviso ser lido pela página abaixo.
        follow_redirects=False,
    )
    # O aviso diz o motivo.
    assert "Informe o e-mail" in client.get("/cadastros", headers=OWNER).text
    # Cadastrar o desconhecido, com e-mail, liga as falas antigas na hora.
    client.post(
        "/cadastros/pessoas",
        data={"nome": "José Silva", "email": "jose@x.com", "outros_nomes": "Zé Desconhecido"},
        headers=SAME,
    )
    # Agora as três falas têm pessoa.
    with sessions() as session:
        # Nenhuma sem pessoa.
        assert session.scalar(select(Utterance).where(Utterance.stakeholder_id.is_(None))) is None


# A transcrição da extensão também vira falas; as antigas são refeitas pelo comando.
def test_live_and_backfill_utterances(sessions):
    # Uma sessão.
    with sessions() as session:
        # Dois trechos de áudio da extensão.
        session.add_all(
            [
                TranscriptSegment(
                    session_id="sessao-123",
                    channel="voce",
                    provider="deepgram",
                    speaker="Você",
                    text="Bom dia.",
                    start_ms=1000,
                ),
                TranscriptSegment(
                    session_id="sessao-123",
                    channel="outros",
                    provider="deepgram",
                    speaker="Falante 1",
                    text="Oi.",
                    start_ms=2000,
                ),
            ]
        )
        # Grava.
        session.commit()
        # Finaliza a reunião.
        item = finalize(session, "sessao-123", actor="dona", title="Status", meeting_url=None)
        # Duas falas.
        assert [u.speaker for u in session.scalars(select(Utterance))] == ["Você", "Falante 1"]
        # Uma transcrição antiga, sem falas (gravada antes desta versão).
        session.add(
            BronzeItem(
                source=SourceKind.MEET_TRANSCRIPT,
                external_id="antiga",
                content_sha256="x",
                body_text="[00:01] Ana: Olá\n[1:00:02] Bruno: Tchau\ncontinua",
            )
        )
        # Grava.
        session.commit()
        # Refaz as falas das que não têm.
        assert backfill_utterances(session) == 1
        # Falas da antiga, com o tempo e a linha quebrada juntada.
        old = session.scalars(
            select(Utterance)
            .where(Utterance.bronze_item_id != item.id)
            .order_by(Utterance.position)
        ).all()
        # Conferência.
        assert [(u.start_ms, u.speaker, u.text) for u in old] == [
            (1000, "Ana", "Olá"),
            (3602000, "Bruno", "Tchau continua"),
        ]


# Texto citado e assinatura não contam; e-mail encaminhado conta inteiro.
def test_own_text():
    # Resposta com citação.
    body = "Ok, combinado.\n\nEm seg., 2 de nov., Ana escreveu:\n> Sobre o Atlas..."
    # Só a parte própria.
    assert "Atlas" not in own_text("Re: reunião", body)
    # Encaminhado: conta tudo.
    assert "Atlas" in own_text("Enc: reunião", body)
    # Linhas com ">" e assinatura.
    assert own_text("x", "a\n> Atlas\nb\n-- \nAtlas S.A.") == "x\na\nb"


# As três regras nos e-mails, a pasta do Gmail e a tag tirada à mão.
def test_email_rules_and_labels(sessions, atlas, tmp_path, monkeypatch):
    # Gmail falso com quatro e-mails.
    gmail = FakeGmail(
        {
            # a) nome do projeto no corpo.
            "m1": {"raw": email("x@fornecedor.com", "Proposta", "Segue a proposta do Atlas.")},
            # Nada: o projeto só aparece no texto citado.
            "m2": {
                "raw": email("y@fornecedor.com", "Re: reunião", "Ok.\nOn Mon, Ana wrote:\n> Atlas")
            },
            # c) remetente da equipe.
            "m3": {"raw": email("Beto <BETO@cliente.com>", "Planilha", "Segue.")},
            # b) na pasta "Clientes/Atlas" do Gmail.
            "m4": {"raw": email("z@x.com", "Contrato", "Anexo."), "labels": ["INBOX", "L1"]},
        }
    )
    # Pasta criada no Gmail.
    gmail.user_labels = [("L1", "Clientes/Atlas")]
    # Liga a pasta ao projeto pela página (o Gmail falso no lugar do real).
    monkeypatch.setattr(routes_registry, "gmail_client", lambda db, s: gmail)
    # Liga.
    page = client.post(
        f"/cadastros/projetos/{atlas['project']}/pastas", data={"pasta": "L1"}, headers=SAME
    ).text
    # Ligada (ainda sem e-mails coletados).
    assert "Pasta “Clientes/Atlas” ligada" in page
    # Coleta (as regras rodam em cada e-mail novo e a pasta é conferida no fim).
    collect_gmail(sessions, gmail, FileStore(tmp_path), Settings(data_dir=str(tmp_path)), now=NOW)
    # Tags por e-mail.
    with sessions() as session:
        # Id do Gmail → regra.
        rules = dict(
            session.execute(
                select(BronzeItem.external_id, TaggedItem.rule).join(
                    TaggedItem, TaggedItem.bronze_item_id == BronzeItem.id
                )
            ).all()
        )
    # m2 sem tag; os outros, cada um pela sua regra.
    assert rules == {"m1": "assunto_corpo", "m3": "equipe", "m4": "marcador_gmail"}
    # Você tira o e-mail da pasta no Gmail.
    gmail.emails["m4"]["labels"] = ["INBOX"]
    # Próxima coleta: a tag da pasta sai.
    collect_gmail(sessions, gmail, FileStore(tmp_path), Settings(data_dir=str(tmp_path)), now=NOW)
    # Conferência.
    with sessions() as session:
        # Sem tag no m4.
        m4 = session.scalar(select(BronzeItem).where(BronzeItem.external_id == "m4"))
        # Nenhuma linha.
        assert session.get(TaggedItem, (m4.id, atlas["tag"])) is None
        # O m1.
        m1 = session.scalar(select(BronzeItem).where(BronzeItem.external_id == "m1"))
    # Você tira a tag do m1 à mão.
    client.post(f"/conteudo/{m1.id}/tags/{atlas['tag']}/remover", headers=SAME)
    # Reaplicar as regras não coloca de volta.
    with sessions() as session:
        # Reaplica.
        apply_rules_to_all(session)
        # Grava.
        session.commit()
        # Continua tirada.
        assert session.get(TaggedItem, (m1.id, atlas["tag"])).removed_at is not None
    # Colocar à mão uma tag nova cria a tag.
    client.post(f"/conteudo/{m1.id}/tags", data={"nome": "Jurídico"}, headers=SAME)
    # O filtro por tag acha o e-mail.
    with sessions() as session:
        # A tag nova.
        legal = session.scalar(select(Tag).where(Tag.name == "Jurídico"))
    # Lista filtrada.
    listing = client.get(f"/conteudo?tag={legal.id}&dia=", headers=OWNER).text
    # Só o e-mail com a tag.
    assert "Proposta" in listing and "Planilha" not in listing


# Página Cadastros: projeto cria tag; a tag de projeto não é apagada nem renomeada à parte.
def test_registry_pages(sessions, atlas, monkeypatch):
    # Página principal.
    page = client.get("/cadastros", headers=OWNER).text
    # Projeto, pessoas, liderança e tag.
    assert "Projeto Atlas" in page and "Márcia Lopes" in page and "liderança na empresa" in page
    # Projeto repetido.
    page = client.post("/cadastros/projetos", data={"nome": "projeto atlas"}, headers=SAME).text
    # Recusado.
    assert "já existe" in page
    # Apagar a tag do projeto: recusado.
    page = client.post(f"/cadastros/tags/{atlas['tag']}/apagar", headers=SAME).text
    # Mensagem.
    assert "é de um projeto" in page
    # Renomear o projeto renomeia a tag.
    client.post(
        f"/cadastros/projetos/{atlas['project']}",
        data={"nome": "Atlas 2027", "codigo": "ATL", "ativo": "1"},
        headers=SAME,
    )
    # Conferência.
    with sessions() as session:
        # Tag com o nome novo.
        assert session.get(Tag, atlas["tag"]).name == "Atlas 2027"
    # Página do projeto (Gmail sem conta: mostra o motivo, sem quebrar).
    monkeypatch.setattr(
        routes_registry,
        "gmail_client",
        lambda db, s: (_ for _ in ()).throw(RuntimeError("Sem conta.")),
    )
    # Abre.
    page = client.get(f"/cadastros/projetos/{atlas['project']}", headers=OWNER).text
    # Equipe com função e o motivo do Gmail.
    assert "Patrocinadora" in page and "Sem conta." in page
    # Inclui uma pessoa nova na equipe, com liderança no projeto.
    client.post(
        f"/cadastros/projetos/{atlas['project']}/equipe",
        data={
            "nome": "Carla Dias",
            "email": "carla@cliente.com",
            "funcao": "Gerente",
            "lider_projeto": "1",
        },
        headers=SAME,
    )
    # A mesma pessoa pelo e-mail não duplica (e-mail com outras maiúsculas).
    page = client.post(
        "/cadastros/pessoas", data={"nome": "Carla D.", "email": "CARLA@cliente.com"}, headers=SAME
    ).text
    # Conferência.
    with sessions() as session:
        # Uma Carla só, com o nome atualizado.
        assert [
            p.name
            for p in session.scalars(
                select(Stakeholder).where(Stakeholder.email == "carla@cliente.com")
            )
        ] == ["Carla D."]
        # Projeto continua um só.
        assert len(session.scalars(select(Project)).all()) == 1
    # Página da pessoa.
    page = client.get(f"/cadastros/pessoas/{atlas['marcia']}", headers=OWNER).text
    # Projeto e liderança.
    assert "Atlas 2027" in page and "liderança no projeto" in page
