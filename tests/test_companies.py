"""Testes da separação por Empresa (cada cliente da dona é uma Empresa).

O que se prova aqui:
- o banco recusa dado sem Empresa e recusa ligar linhas de Empresas diferentes (mesmo
  que o código tenha um erro, a mistura não chega a ser gravada);
- nomes e e-mails só não se repetem dentro da Empresa;
- as regras de tag e o reconhecimento de quem falou só olham a Empresa do conteúdo;
- a correção da Empresa de uma transcrição refaz tags e pessoas na Empresa nova;
- reunião sem Empresa não é salva até você escolher;
- as telas exigem a Empresa nos cadastros e filtram por ela.
"""

# Data de exemplo.
from datetime import date

# Biblioteca de testes.
import pytest

# Simula o navegador.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import select

# Erros do banco: regra violada.
from sqlalchemy.exc import DBAPIError, IntegrityError

# Empresas: criar, corrigir a Empresa de uma transcrição e o idioma.
from app.companies import create_company, move_item, transcription_language

# Configuração.
from app.config import get_settings

# Guarda dos originais.
from app.ingestion.storage import FileStore

# O app.
from app.main import app

# Tabelas.
from app.models import (
    BronzeItem,
    Company,
    LiveMeeting,
    Project,
    SourceKind,
    TaggedItem,
    TranscriptSegment,
    Utterance,
)

# Projetos e pessoas.
from app.projects import create_project, save_person

# Tags.
from app.tags import create_tag

# Importação de transcrição.
from app.transcription.importer import ImportFailure, import_transcript

# Empresa que todo teste tem.
from tests.fakes import EMPRESA

# Navegador simulado.
client = TestClient(app)

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}

# POST vindo do próprio console.
SAME = {**OWNER, "Origin": "http://testserver"}

# Legenda do Teams com uma fala da Márcia.
VTT = """WEBVTT

00:00:03.000 --> 00:00:05.000
<v Márcia Lopes>Precisamos antecipar a entrega do Atlas.</v>
"""


# Uma segunda Empresa (a primeira, EMPRESA, vem do conftest).
@pytest.fixture
def outra(sessions) -> int:
    # Cria.
    with sessions() as s:
        # Empresa nova.
        company = create_company(s, "Cliente B", "en")
        # Grava.
        s.commit()
        # Id.
        return company.id


# Importa a legenda numa Empresa; devolve o id do item.
def _importar(session, tmp_path, company_id: int, title: str = "Atlas semanal") -> int:
    # Importa fora da agenda.
    result = import_transcript(
        session,
        FileStore(str(tmp_path)),
        actor="dona",
        data=VTT.encode(),
        filename="ata.vtt",
        event=None,
        title=title,
        day=date(2026, 11, 3),
        timezone="America/Sao_Paulo",
        company_id=company_id,
    )
    # Grava.
    session.commit()
    # Id.
    return result.item_id


# --- Regras do banco ------------------------------------------------------------------


# Sem Empresa, o banco não grava.
def test_company_is_required(sessions):
    # Tenta um projeto sem Empresa.
    with sessions() as s:
        # Projeto sem company_id.
        s.add(Project(name="Solto"))
        # O banco recusa (NOT NULL).
        with pytest.raises(IntegrityError):
            s.commit()


# Ligar uma tag de uma Empresa a um projeto de outra é recusado pelo banco.
def test_database_refuses_mixing(sessions, outra):
    # Projeto na Empresa 1 e tag na outra.
    with sessions() as s:
        # Projeto.
        project = create_project(s, EMPRESA, "Atlas")
        # Tag da outra Empresa.
        tag = create_tag(s, outra, "Jurídico")
        # Grava.
        s.commit()
        # Liga a tag da outra Empresa ao projeto (como um código com erro faria).
        project.tag_id = tag.id
        # O gatilho recusa no commit.
        with pytest.raises(DBAPIError, match="Empresas diferentes"):
            s.commit()


# A Empresa de um projeto não muda (tudo dele é da mesma Empresa).
def test_company_of_project_is_fixed(sessions, outra):
    # Projeto.
    with sessions() as s:
        # Cria.
        project = create_project(s, EMPRESA, "Atlas")
        # Grava.
        s.commit()
        # Tenta mudar.
        project.company_id = outra
        # Recusado.
        with pytest.raises(DBAPIError, match="não muda"):
            s.commit()


# Nomes e e-mails só não se repetem dentro da Empresa.
def test_unique_within_company(sessions, outra):
    # Cadastros.
    with sessions() as s:
        # A mesma tag nas duas Empresas.
        create_tag(s, EMPRESA, "Portal")
        # Pode.
        create_tag(s, outra, "Portal")
        # A mesma pessoa (mesmo e-mail) nas duas.
        save_person(s, None, company_id=EMPRESA, name="Ana", email="ana@x.com")
        # Pode.
        save_person(s, None, company_id=outra, name="Ana", email="ana@x.com")
        # O mesmo projeto nas duas.
        create_project(s, EMPRESA, "Atlas")
        # Pode.
        create_project(s, outra, "Atlas")
        # Grava.
        s.commit()
        # Repetida na mesma Empresa: recusada.
        with pytest.raises(ValueError, match="já existe nesta Empresa"):
            create_tag(s, EMPRESA, "portal")


# --- Regras de tag, pessoas e correção ------------------------------------------------


# A transcrição recebe só a tag do projeto da Empresa dela, e as falas só se ligam às
# Pessoas dela. Corrigir a Empresa refaz as duas coisas na Empresa nova.
def test_rules_and_people_per_company_and_move(sessions, outra, tmp_path):
    # Projeto "Atlas" e a Márcia nas duas Empresas.
    with sessions() as s:
        # Projetos.
        atlas_1 = create_project(s, EMPRESA, "Atlas")
        atlas_2 = create_project(s, outra, "Atlas")
        # Pessoas.
        marcia_1 = save_person(s, None, company_id=EMPRESA, name="Márcia Lopes", email="m@a.com")
        marcia_2 = save_person(s, None, company_id=outra, name="Márcia Lopes", email="m@b.com")
        # Grava.
        s.commit()
        # Ids.
        tags = (atlas_1.tag_id, atlas_2.tag_id)
        people = (marcia_1.id, marcia_2.id)
        # Importa na Empresa 1.
        item_id = _importar(s, tmp_path, EMPRESA)
        # Tags valendo.
        tagged = set(
            s.scalars(select(TaggedItem.tag_id).where(TaggedItem.bronze_item_id == item_id))
        )
        # Só a da Empresa 1.
        assert tagged == {tags[0]}
        # A fala ligada à Márcia da Empresa 1.
        assert s.scalars(select(Utterance.stakeholder_id)).one() == people[0]
    # Corrige pela tela.
    response = client.post(
        f"/conteudo/{item_id}/empresa",
        data={"empresa": outra},
        headers=SAME,
        follow_redirects=False,
    )
    # Volta para o item.
    assert response.status_code == 303
    # Confere.
    with sessions() as s:
        # O item.
        item = s.get(BronzeItem, item_id)
        # Na Empresa nova.
        assert item.company_id == outra
        # Tags valendo.
        tagged = set(
            s.scalars(select(TaggedItem.tag_id).where(TaggedItem.bronze_item_id == item_id))
        )
        # Só a da Empresa nova.
        assert tagged == {tags[1]}
        # A fala agora é da Márcia da Empresa nova.
        assert s.scalars(select(Utterance.stakeholder_id)).one() == people[1]
    # A página do item mostra o aviso.
    assert (
        "Transcrição movida para Cliente B"
        in client.get(f"/conteudo/{item_id}", headers=OWNER).text
    )


# E-mail não muda de Empresa (é da caixa da conta que coletou).
def test_only_transcripts_move(sessions, outra):
    # Um e-mail.
    with sessions() as s:
        # Item.
        email = BronzeItem(
            company_id=EMPRESA, source=SourceKind.GMAIL, external_id="m1", content_sha256="a"
        )
        # Grava.
        s.add(email)
        s.commit()
        # Recusado.
        with pytest.raises(ValueError, match="Só transcrições"):
            move_item(s, email, s.get(Company, outra), "dona")


# A mesma transcrição importada em outra Empresa é recusada (não duplica entre clientes).
def test_import_duplicate_in_other_company(sessions, outra, tmp_path):
    # Importa na Empresa 1.
    with sessions() as s:
        # Primeira.
        _importar(s, tmp_path, EMPRESA)
        # Na outra Empresa: recusada, dizendo onde está.
        with pytest.raises(ImportFailure, match="já foi importada na Empresa Empresa Teste"):
            _importar(s, tmp_path, outra)


# --- Reunião ao vivo sem Empresa ------------------------------------------------------


# Reunião encerrada sem Empresa fica esperando; ao escolher, vai para o banco.
def test_meeting_waits_for_company(sessions):
    # Reunião com uma fala, sem Empresa.
    with sessions() as s:
        # Reunião.
        s.add(LiveMeeting(session_id="sem-empresa-1", title="Conversa"))
        # Uma fala.
        s.add(
            TranscriptSegment(
                session_id="sem-empresa-1",
                channel="outros",
                provider="falso",
                speaker="Falante 1",
                text="Bom dia.",
                start_ms=0,
            )
        )
        # Grava.
        s.commit()
    # Encerra pela extensão.
    body = client.post("/transcricoes/sem-empresa-1/finalizar", json={}, headers=OWNER).json()
    # Pendente.
    assert body == {"bronze_id": None, "pendente": "empresa"}
    # Nada no banco ainda.
    with sessions() as s:
        # Nenhum item.
        assert s.scalars(select(BronzeItem)).first() is None
    # O Início mostra a área pedindo a Empresa.
    assert "escolha para salvar no banco" in client.get("/", headers=OWNER).text
    # Você escolhe a Empresa.
    client.post("/ao-vivo/sem-empresa-1/empresa", data={"empresa": EMPRESA}, headers=SAME)
    # Agora está salva, na Empresa escolhida.
    with sessions() as s:
        # O item.
        item = s.scalars(select(BronzeItem)).one()
        # Empresa.
        assert item.company_id == EMPRESA


# --- Telas ----------------------------------------------------------------------------


# Sem Empresa escolhida, os cadastros são recusados.
def test_registry_requires_company(sessions):
    # Cada formulário, sem a Empresa.
    for url, data in (
        ("/cadastros/projetos", {"nome": "Atlas"}),
        ("/cadastros/pessoas", {"nome": "Ana", "email": "ana@x.com"}),
        ("/cadastros/tags", {"nome": "Portal"}),
    ):
        # Envia.
        client.post(url, data=data, headers=SAME, follow_redirects=False)
        # Aviso.
        assert "Escolha a Empresa" in client.get("/cadastros", headers=OWNER).text
    # Nada gravado.
    with sessions() as s:
        # Nenhum projeto.
        assert s.scalars(select(Project)).first() is None


# Nova Empresa pela tela; o filtro "Mostrar" separa os cadastros.
def test_new_company_and_filter(sessions):
    # Cadastra.
    client.post("/cadastros/empresas", data={"nome": "Cliente C"}, headers=SAME)
    # Id da nova.
    with sessions() as s:
        # Projeto em cada Empresa.
        create_project(s, EMPRESA, "Projeto Um")
        create_project(s, 2, "Projeto Dois")
        # Grava.
        s.commit()
    # Só a nova.
    page = client.get("/cadastros?empresa=2", headers=OWNER).text
    # Só o dela.
    assert "Projeto Dois" in page and "Projeto Um" not in page
    # Todas: os dois, com a coluna Empresa.
    page = client.get("/cadastros", headers=OWNER).text
    # Os dois.
    assert "Projeto Dois" in page and "Projeto Um" in page and "<th>Empresa</th>" in page
    # Nome repetido (sem diferenciar maiúsculas): recusado.
    client.post(
        "/cadastros/empresas", data={"nome": "cliente c"}, headers=SAME, follow_redirects=False
    )
    # Aviso.
    assert "já existe" in client.get("/cadastros", headers=OWNER).text


# Processamento filtra o conteúdo pela Empresa.
def test_processing_filter(sessions, outra):
    # Um e-mail em cada Empresa.
    with sessions() as s:
        # Itens.
        for company_id, subject in ((EMPRESA, "Assunto Um"), (outra, "Assunto Dois")):
            # Item.
            s.add(
                BronzeItem(
                    company_id=company_id,
                    source=SourceKind.GMAIL,
                    external_id=subject,
                    content_sha256=subject,
                    subject=subject,
                )
            )
        # Grava.
        s.commit()
    # Só a outra Empresa, todas as datas.
    page = client.get(f"/conteudo?empresa={outra}&dia=", headers=OWNER).text
    # Só o dela.
    assert "Assunto Dois" in page and "Assunto Um" not in page
    # Todas.
    page = client.get("/conteudo?dia=", headers=OWNER).text
    # Os dois.
    assert "Assunto Dois" in page and "Assunto Um" in page


# Idioma da transcrição: o da Empresa, ou o geral.
def test_language_per_company(sessions, outra):
    # A outra Empresa foi criada com inglês; a 1 usa o geral.
    with sessions() as s:
        # Inglês.
        assert transcription_language(s, get_settings(), outra) == "en"
        # O geral.
        assert (
            transcription_language(s, get_settings(), EMPRESA)
            == get_settings().transcription_language
        )
    # Troca o da Empresa 1 em Configurações.
    client.post(f"/config/transcricao/{EMPRESA}", data={"idioma": "multi"}, headers=SAME)
    # Vale.
    with sessions() as s:
        # Automático.
        assert transcription_language(s, get_settings(), EMPRESA) == "multi"
