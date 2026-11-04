"""Testes da extração de fatos com trecho conferido (app/facts.py).

O que provam:
- o código confere o trecho citado: acha com aspas, espaços e maiúsculas diferentes e
  com reticências entre partes próximas; recusa trecho inventado, curto ou espalhado;
- só itens com projeto, fora da fila "A revisar" e com os anexos lidos vão ao Claude;
- o Claude recebe o texto próprio do e-mail (sem a conversa citada), o projeto e a
  equipe, e responde no formato fixo (saída estruturada);
- cada fato fica com o projeto do item, o responsável e quem disse ligados às Pessoas da
  Empresa do item, e o suspeito fica marcado;
- item lido não volta ao Claude; a segunda transcrição da mesma reunião não duplica;
- recusa e resposta ilegível ficam registradas sem cobrar de novo;
- o fato segue o projeto quando você corrige a classificação;
- a página Fatos, a seção do item e "Extrair de novo" funcionam.
"""

# Monta as respostas JSON do Claude falso.
import json

# Data e hora (data do item e da reunião).
from datetime import UTC, date, datetime

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import select

# Classificação à mão (para dar projeto ao item).
from app.classification import set_manual

# Criação de Empresa.
from app.companies import create_company

# Configuração.
from app.config import Settings, get_settings

# O que é testado.
from app.facts import SCHEMA, normalize, pending_ids, quote_found, run_pending

# Cliente do Claude (com o Claude falso dentro).
from app.llm import LLMClient

# O app.
from app.main import app

# Tabelas.
from app.models import (
    Attachment,
    BronzeItem,
    Fact,
    FactExtraction,
    FactKind,
    ItemClassification,
    LLMCall,
    Meeting,
    SourceKind,
)

# Falas de uma transcrição.
from app.people import parse_lines, save_utterances

# Cadastro de projetos e pessoas.
from app.projects import create_project, save_person, set_member

# Falsos.
from tests.fakes import EMPRESA, FakeClaude

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient.
SAME = {**OWNER, "Origin": "http://testserver"}
# Um cliente para o arquivo inteiro.
client = TestClient(app)

# E-mail de exemplo: a parte de cima é da Márcia; abaixo de "escreveu:", a conversa citada.
EMAIL_BODY = (
    "Oi, pessoal.\n"
    "Ficou decidido que o go-live do Atlas será em 15/12.\n"
    "Beto, por favor envie o cronograma revisado até sexta.\n"
    "Há risco de atraso se o fornecedor não entregar as licenças.\n"
    "\n"
    "Em seg., 2 de nov. de 2026, Ana <ana@cli.com> escreveu:\n"
    "> segredo antigo da conversa\n"
)


# Configuração de teste: até 20 itens por rodada.
def _settings() -> Settings:
    # Copia a configuração de teste.
    return get_settings().model_copy(update={"facts_llm_per_run": 20})


# Um fato no formato da resposta.
def _fact(kind: str, title: str, quote: str, owner=None, due=None, confidence=0.9) -> dict:
    # Mesmo formato do SCHEMA.
    return {
        "tipo": kind,
        "titulo": title,
        "descricao": None,
        "responsavel": owner,
        "prazo": due,
        "trecho": quote,
        "confianca": confidence,
    }


# Resposta do Claude falso em JSON.
def _answer(*facts: dict) -> str:
    # {"fatos": [...]}.
    return json.dumps({"fatos": list(facts)})


# Projeto Atlas com a Márcia (liderança) e o Beto na equipe.
def _setup(session) -> dict:
    # Projeto.
    atlas = create_project(session, EMPRESA, "Projeto Atlas", "ATL", "Atlas")
    # Pessoas.
    marcia = save_person(session, None, name="Márcia", email="marcia@cli.com", company_id=EMPRESA)
    # Beto.
    beto = save_person(session, None, name="Beto", email="beto@cli.com", company_id=EMPRESA)
    # Equipe.
    set_member(session, atlas.id, marcia.id, "Patrocinadora", True)
    # Beto.
    set_member(session, atlas.id, beto.id, "Analista", False)
    # Envia.
    session.flush()
    # Devolve.
    return {"atlas": atlas, "marcia": marcia, "beto": beto}


# Grava um item do bronze e, se pedido, escolhe o projeto (como se você tivesse escolhido).
def _item(
    session,
    external_id: str,
    *,
    body: str,
    subject: str = "Atlas",
    author: str | None = "Márcia <marcia@cli.com>",
    source: SourceKind = SourceKind.GMAIL,
    project_id: int | None = None,
    meeting_id: int | None = None,
) -> BronzeItem:
    # Item.
    item = BronzeItem(
        company_id=EMPRESA,
        source=source,
        external_id=external_id,
        subject=subject,
        body_text=body,
        author=author,
        occurred_at=datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
        content_sha256=external_id.ljust(64, "0")[:64],
        meeting_id=meeting_id,
    )
    # Prepara.
    session.add(item)
    # Gera o id.
    session.flush()
    # Projeto escolhido à mão (trava; a cascata não muda).
    if project_id is not None:
        set_manual(session, item, project_id, "teste")
    # Devolve.
    return item


# Conferência do trecho: o que passa e o que não passa.
def test_quote_check():
    # Texto original com aspas curvas, quebra de linha e maiúsculas.
    source = normalize("Ficou “decidido” que o go-live\n será em 15/12. Depois, o resto.")
    # Mesmo trecho com aspas retas, um espaço e minúsculas: passa.
    assert quote_found('ficou "decidido" que o go-live será em 15/12', source)
    # Com aspas em volta: passa.
    assert quote_found('"Ficou “decidido” que o go-live será"', source)
    # Inventado: não passa.
    assert not quote_found("Ficou decidido que o go-live será em janeiro", source)
    # Curto demais para provar algo: não passa.
    assert not quote_found("o resto", source)
    # Reticências entre duas partes próximas e na ordem: passa.
    assert quote_found("Ficou “decidido” que ... será em 15/12", source)
    # Partes fora de ordem: não passa.
    assert not quote_found("será em 15/12 ... Ficou “decidido” que", source)
    # Partes longe demais uma da outra: não passa.
    far = normalize("Ficou decidido o seguinte. " + "x " * 300 + "o go-live será em 15/12.")
    # Confere.
    assert not quote_found("Ficou decidido o seguinte ... o go-live será em 15/12", far)


# O esquema segue as regras da saída estruturada (todos obrigatórios, nada a mais).
def test_schema_shape():
    # Campos de um fato.
    item = SCHEMA["properties"]["fatos"]["items"]
    # Todos obrigatórios.
    assert set(item["required"]) == set(item["properties"])
    # Nada além deles.
    assert item["additionalProperties"] is False and SCHEMA["additionalProperties"] is False
    # Os oito tipos da silver.
    assert set(item["properties"]["tipo"]["enum"]) == {k.value for k in FactKind}


# Só itens com projeto, fora da revisão, vão para a fila.
def test_pending_needs_project(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Sem classificação.
        _item(session, "sem", body="Algo.")
        # "Nenhum projeto".
        none = _item(session, "nenhum", body="Newsletter.")
        # Escolha: nenhum.
        set_manual(session, none, None, "teste")
        # Com projeto, mas a revisar.
        review = _item(session, "revisar", body="Talvez Atlas.", project_id=p["atlas"].id)
        # Marca a revisar.
        session.get(ItemClassification, review.id).needs_review = True
        # Com projeto.
        ok = _item(session, "ok", body="Atlas.", project_id=p["atlas"].id)
        # Grava.
        session.commit()
        # Só o último.
        assert pending_ids(session) == [ok.id]


# Caminho completo de um e-mail: chamada, fatos, conferência, pessoas e registro.
def test_email_extraction(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # E-mail com projeto.
        item = _item(session, "e1", body=EMAIL_BODY, project_id=p["atlas"].id)
        # Grava.
        session.commit()
        # Ids.
        item_id, atlas, marcia, beto = item.id, p["atlas"].id, p["marcia"].id, p["beto"].id
    # Claude falso: dois fatos do texto e um inventado.
    fake = FakeClaude(
        _answer(
            _fact(
                "decisao",
                "Go-live do Atlas em 15/12",
                "Ficou decidido que o go-live do Atlas será em 15/12.",
            ),
            _fact(
                "tarefa",
                "Enviar o cronograma revisado",
                "Beto, por favor envie o cronograma revisado até sexta.",
                owner="Beto",
                due="2026-11-06",
            ),
            _fact("risco", "Contrato cancelado", "O cliente vai cancelar o contrato inteiro."),
        )
    )
    # Rodada.
    summary = run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Um item lido, três fatos, dois conferidos.
    assert (summary.items, summary.facts, summary.verified) == (1, 3, 2)
    # Uma chamada.
    assert len(fake.calls) == 1
    # A chamada.
    call = fake.calls[0]
    # Haiku.
    assert call["model"] == "claude-haiku-5-5"
    # Esforço baixo e formato fixo (saída estruturada).
    assert call["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    # O pedido.
    prompt = call["messages"][0]["content"]
    # Traz o projeto, a equipe e a data do item com o dia da semana.
    assert "Projeto: Projeto Atlas" in prompt and "Beto (Analista)" in prompt
    # Data local (10h em Brasília, quarta-feira).
    assert "2026-11-04 (quarta-feira)" in prompt
    # Não traz a conversa citada (já foi lida no e-mail anterior).
    assert "segredo antigo" not in prompt
    # Conferência no banco.
    with sessions() as session:
        # Fatos por título.
        facts = {f.title: f for f in session.scalars(select(Fact))}
        # Registro da extração.
        record = session.get(FactExtraction, item_id)
        # Custo registrado com o motivo certo.
        purposes = list(session.scalars(select(LLMCall.purpose)))
    # Todos no projeto do item.
    assert {f.project_id for f in facts.values()} == {atlas}
    # A tarefa.
    task = facts["Enviar o cronograma revisado"]
    # Tipo, responsável ligado ao Beto, prazo, aberta e conferida.
    assert task.kind == FactKind.TASK and task.owner_id == beto and task.owner_text == "Beto"
    # Prazo e situação.
    assert task.due_date == date(2026, 11, 6) and task.status == "aberto"
    # Quem disse: a Márcia (remetente).
    assert task.said_by_id == marcia and task.quote_verified is True
    # A decisão: sem situação (não tem ciclo de vida).
    assert facts["Go-live do Atlas em 15/12"].status is None
    # O inventado: suspeito.
    assert facts["Contrato cancelado"].quote_verified is False
    # Registro: ok, 3 devolvidos, 2 conferidos.
    assert (record.status, record.facts_found, record.facts_verified) == ("ok", 3, 2)
    # Custo.
    assert purposes == ["extracao_fatos"]
    # Segunda rodada: nada novo vai ao Claude.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Continua uma chamada.
    assert len(fake.calls) == 1


# Responsável de outra Empresa não é ligado (os dados não se misturam).
def test_owner_only_from_same_company(sessions):
    # Uma sessão.
    with sessions() as session:
        # Projeto sem o Beto.
        atlas = create_project(session, EMPRESA, "Projeto Atlas", "ATL", "Atlas")
        # Outra Empresa.
        other = create_company(session, "Outro Cliente")
        # O Beto só existe na outra Empresa.
        save_person(session, None, name="Beto", email="beto@outro.com", company_id=other.id)
        # E-mail.
        _item(session, "e1", body=EMAIL_BODY, project_id=atlas.id, author="x@y.com")
        # Grava.
        session.commit()
    # Claude falso.
    fake = FakeClaude(
        _answer(
            _fact(
                "tarefa",
                "Enviar o cronograma",
                "Beto, por favor envie o cronograma revisado até sexta.",
                owner="Beto",
            )
        )
    )
    # Rodada.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # O fato.
    with sessions() as session:
        # Único.
        fact = session.scalars(select(Fact)).one()
    # Sem Pessoa ligada; o nome fica como está no texto.
    assert fact.owner_id is None and fact.owner_text == "Beto"


# Anexo esperando a leitura pelo Claude: o e-mail espera (lê depois, com o anexo).
def test_waits_for_attachment_reading(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # E-mail.
        item = _item(session, "e1", body=EMAIL_BODY, project_id=p["atlas"].id)
        # Anexo escaneado na fila da leitura.
        session.add(
            Attachment(
                bronze_item_id=item.id,
                filename="ata.pdf",
                object_key="ab/abc.pdf",
                extraction_error="[PDF_SEM_TEXTO] PDF digitalizado.",
            )
        )
        # Grava.
        session.commit()
    # Claude falso.
    fake = FakeClaude(_answer())
    # Rodada.
    summary = run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Nenhuma chamada; um esperando.
    assert fake.calls == [] and summary.waiting == 1
    # O anexo é lido (a fila de leitura grava o texto e tira o erro).
    with sessions() as session:
        # O anexo.
        attachment = session.scalars(select(Attachment)).one()
        # Lido.
        attachment.extracted_text = "Ata: o comitê aprovou o orçamento de 2027."
        # Sem erro.
        attachment.extraction_error = None
        # Grava.
        session.commit()
    # Nova rodada.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Agora vai ao Claude, com o texto do anexo.
    assert "[Anexo: ata.pdf]" in fake.calls[0]["messages"][0]["content"]


# Transcrição: quem disse vem da fala; a segunda transcrição da reunião não duplica.
def test_transcript_speaker_and_sibling(sessions):
    # Texto no formato do sistema.
    body = (
        "[00:01] Márcia: Bom dia a todos.\n"
        "[00:05] Márcia: Decidimos adiar o go-live para dezembro.\n"
        "[00:09] Beto: Combinado, eu atualizo o plano."
    )
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Reunião.
        meeting = Meeting(
            company_id=EMPRESA,
            title="Comitê Atlas",
            starts_at=datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
        )
        # Prepara.
        session.add(meeting)
        # Gera o id.
        session.flush()
        # Duas transcrições da mesma reunião (extensão e importada).
        first = _item(
            session,
            "t1",
            body=body,
            source=SourceKind.MEET_TRANSCRIPT,
            author=None,
            project_id=p["atlas"].id,
            meeting_id=meeting.id,
        )
        # Falas.
        save_utterances(session, first, parse_lines(body))
        # A segunda.
        second = _item(
            session,
            "t2",
            body=body,
            source=SourceKind.MEET_TRANSCRIPT,
            author=None,
            project_id=p["atlas"].id,
            meeting_id=meeting.id,
        )
        # Grava.
        session.commit()
        # Ids.
        first_id, second_id, marcia = first.id, second.id, p["marcia"].id
    # Claude falso.
    fake = FakeClaude(
        _answer(
            _fact(
                "decisao",
                "Go-live adiado para dezembro",
                "Márcia: Decidimos adiar o go-live para dezembro.",
            )
        )
    )
    # Rodada.
    summary = run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Uma chamada só (a segunda transcrição foi pulada).
    assert len(fake.calls) == 1 and summary.skipped == 1
    # Conferência.
    with sessions() as session:
        # O fato.
        fact = session.scalars(select(Fact)).one()
        # Registros.
        first_rec = session.get(FactExtraction, first_id)
        # Da segunda.
        second_rec = session.get(FactExtraction, second_id)
    # Da primeira, dito pela Márcia, conferido.
    assert fact.source_item_id == first_id and fact.said_by_id == marcia
    # Nome como apareceu.
    assert fact.said_by_text == "Márcia" and fact.quote_verified is True
    # A segunda: "duplicada", apontando a primeira.
    assert first_rec.status == "ok" and second_rec.status == "duplicada"
    # Aponta.
    assert str(first_id) in second_rec.note


# Recusa e resposta ilegível ficam registradas e não voltam ao Claude sozinhas.
def test_refusal_and_unreadable(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Dois e-mails.
        _item(session, "a", body=EMAIL_BODY, project_id=p["atlas"].id)
        # O outro.
        _item(session, "b", body=EMAIL_BODY + " ", project_id=p["atlas"].id)
        # Grava.
        session.commit()
    # Primeiro: recusa.
    refusing = FakeClaude("", stop="refusal")
    # Uma rodada com limite 1 (só o primeiro item).
    one = _settings().model_copy(update={"facts_llm_per_run": 1})
    # Rodada.
    run_pending(sessions, one, LLMClient(sessions, one, refusing))
    # Segundo: texto que não é JSON (ex.: resposta cortada).
    broken = FakeClaude('{"fatos": [')
    # Rodada.
    run_pending(sessions, one, LLMClient(sessions, one, broken))
    # Registros.
    with sessions() as session:
        # Situações.
        statuses = sorted(session.scalars(select(FactExtraction.status)))
        # Nada pendente.
        assert pending_ids(session) == []
    # Uma de cada.
    assert statuses == ["ilegivel", "recusa"]


# Você corrige o projeto do item: os fatos vão junto.
def test_facts_follow_project(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Outro projeto.
        boreal = create_project(session, EMPRESA, "Projeto Boreal", "BOR", "Boreal")
        # E-mail.
        item = _item(session, "e1", body=EMAIL_BODY, project_id=p["atlas"].id)
        # Grava.
        session.commit()
        # Ids.
        item_id, boreal_id = item.id, boreal.id
    # Claude falso.
    fake = FakeClaude(
        _answer(_fact("decisao", "Go-live em 15/12", "o go-live do Atlas será em 15/12"))
    )
    # Rodada.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Você troca o projeto do item.
    with sessions() as session:
        # Escolha.
        set_manual(session, session.get(BronzeItem, item_id), boreal_id, "teste")
        # Grava.
        session.commit()
    # Nova rodada (sem chamar o Claude de novo).
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Uma chamada só.
    assert len(fake.calls) == 1
    # O fato mudou de projeto.
    with sessions() as session:
        # Confere.
        assert session.scalars(select(Fact.project_id)).one() == boreal_id


# Página Fatos, seção do item e "Extrair de novo".
def test_console_pages(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # E-mail.
        item = _item(session, "e1", body=EMAIL_BODY, project_id=p["atlas"].id)
        # Grava.
        session.commit()
        # Id.
        item_id = item.id
    # Claude falso.
    fake = FakeClaude(
        _answer(
            _fact("decisao", "Go-live em 15/12", "o go-live do Atlas será em 15/12"),
            _fact("risco", "Fato inventado", "isto não está no texto de jeito nenhum"),
        )
    )
    # Rodada.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Página Fatos.
    page = client.get("/fatos", headers=OWNER)
    # Abre, com os dois fatos e a marca de suspeito.
    assert page.status_code == 200 and "Go-live em 15/12" in page.text
    # Suspeito marcado.
    assert "Fato inventado" in page.text and "suspeito" in page.text
    # Só suspeitos.
    only = client.get("/fatos?suspeitos=1", headers=OWNER)
    # O conferido some.
    assert "Fato inventado" in only.text and "Go-live em 15/12" not in only.text
    # Filtro por tipo.
    decisions = client.get("/fatos?tipo=decisao", headers=OWNER)
    # Só a decisão.
    assert "Go-live em 15/12" in decisions.text and "Fato inventado" not in decisions.text
    # Página do item.
    detail = client.get(f"/conteudo/{item_id}", headers=OWNER)
    # Mostra os fatos e a conferência.
    assert "trecho conferido" in detail.text and "Extrair de novo" in detail.text
    # Extrair de novo.
    redo = client.post(f"/conteudo/{item_id}/fatos/refazer", headers=SAME, follow_redirects=False)
    # Volta para o item.
    assert redo.status_code == 303
    # Fatos e registro apagados; o item voltou à fila.
    with sessions() as session:
        # Sem fatos.
        assert session.scalars(select(Fact)).all() == []
        # Sem registro.
        assert session.get(FactExtraction, item_id) is None
        # Na fila.
        assert pending_ids(session) == [item_id]
