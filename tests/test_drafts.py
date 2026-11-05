"""Testes dos rascunhos de resposta de e-mail (app/drafts.py e app/routes_drafts.py).

O que provam:
- a lista "Para responder" mostra só a conversa que espera você: a última mensagem não é
  sua, você está no "Para", o remetente não é automático nem a mensagem é em massa, e
  quem escreveu é Pessoa cadastrada ou o e-mail tem projeto; a sua resposta coletada e
  o "Dispensar" tiram a conversa da lista;
- o Claude recebe a conversa sem as partes citadas, os fatos abertos e conferidos do
  projeto, os seus e-mails como exemplo de tom e o seu pedido, e nada de outra Empresa;
- a chamada usa o modelo e o esforço do .env, a saída estruturada e o motivo próprio no
  registro de custo; o teto dos rascunhos bloqueia antes de chamar;
- o código acha os números e datas do rascunho que não estão nas mensagens, e junta as
  lacunas marcadas entre colchetes;
- o banco recusa um rascunho ligado a um e-mail de outra Empresa;
- as páginas e os botões funcionam, e POST de outro site é recusado.
"""

# Monta as respostas JSON do Claude falso.
import json

# Data e hora das mensagens.
from datetime import UTC, datetime, timedelta

# Decimal: dinheiro exato.
from decimal import Decimal

# Biblioteca de testes.
import pytest

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Erro do banco quando uma trava recusa.
from sqlalchemy.exc import DBAPIError

# Classificação à mão (para dar projeto ao e-mail).
from app.classification import set_manual

# Criação de Empresa.
from app.companies import create_company

# Configuração.
from app.config import get_settings

# O que é testado.
from app.drafts import (
    PURPOSE,
    SCHEMA,
    DraftError,
    build_prompt,
    dismiss,
    gmail_link,
    parse_answer,
    to_answer,
    unverified_numbers,
    write_draft,
)

# Cliente do Claude (com o Claude falso dentro).
from app.llm import LLMClient

# O app.
from app.main import app

# Tabelas.
from app.models import BronzeItem, EmailDraft, Fact, FactKind, LLMCall, SourceKind

# Cadastro de projetos e pessoas.
from app.projects import create_project, save_person

# Falsos.
from tests.fakes import EMPRESA, FakeClaude

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient.
SAME = {**OWNER, "Origin": "http://testserver"}
# Um cliente para o arquivo inteiro.
client = TestClient(app)
# "Agora" fixo dos testes de regra (as páginas usam o relógio real).
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)

# Primeira mensagem: a Márcia pergunta.
FIRST = "Oi, Natália.\nConsegue mandar o cronograma do Atlas até 12/11?\nAbraço, Márcia"
# Segunda mensagem (sua), com a primeira citada embaixo.
SECOND = (
    "Oi, Márcia.\nMando sim, só preciso fechar as horas com o Beto.\n"
    "\nEm qua., 4 de nov. de 2026, Márcia <marcia@cli.com> escreveu:\n> Consegue mandar...\n"
)
# Terceira mensagem: a Márcia de novo, com a conversa citada.
THIRD = (
    "Perfeito. A reunião de quinta às 14h30 está mantida?\n"
    "\nEm qua., 4 de nov. de 2026, Natália <dona@exemplo.com> escreveu:\n> segredo citado\n"
)


# Configuração de teste: modelo e teto dos rascunhos padrão, janela de 7 dias.
def _settings(**changes):
    # Copia a configuração de teste com as mudanças pedidas.
    return get_settings().model_copy(update=changes)


# Grava um e-mail do bronze.
def _email(
    session,
    external_id: str,
    *,
    body: str,
    author: str = "Márcia <marcia@cli.com>",
    to: list[str] | None = None,
    thread: str = "t1",
    when: datetime | None = None,
    company_id: int = EMPRESA,
    labels: list[str] | None = None,
    subject: str = "Cronograma Atlas",
) -> BronzeItem:
    # Item.
    item = BronzeItem(
        company_id=company_id,
        source=SourceKind.GMAIL,
        external_id=external_id,
        thread_id=thread,
        subject=subject,
        body_text=body,
        author=author,
        occurred_at=when or NOW - timedelta(hours=1),
        content_sha256=external_id.ljust(64, "0")[:64],
        extra={
            # Por padrão, para você.
            "para": to if to is not None else ["Natália <dona@exemplo.com>"],
            "cc": [],
            "marcadores": labels or ["INBOX"],
        },
    )
    # Prepara.
    session.add(item)
    # Gera o id.
    session.flush()
    # Devolve.
    return item


# Cadastros: projeto Atlas, a Márcia (liderança) e você.
def _setup(session, company_id: int = EMPRESA) -> dict:
    # Projeto.
    atlas = create_project(session, company_id, "Projeto Atlas", "ATL")
    # Márcia.
    marcia = save_person(
        session, None, name="Márcia", email="marcia@cli.com", company_id=company_id, is_leader=True
    )
    # Você.
    dona = save_person(
        session, None, name="Natália", email="dona@exemplo.com", company_id=company_id
    )
    # Envia.
    session.flush()
    # Devolve.
    return {"atlas": atlas, "marcia": marcia, "dona": dona}


# A conversa de três mensagens: Márcia, você, Márcia (a última espera você).
def _thread(session) -> list[BronzeItem]:
    # Primeira.
    a = _email(session, "m1", body=FIRST, when=NOW - timedelta(days=1, hours=3))
    # Sua, para a Márcia.
    b = _email(
        session,
        "m2",
        body=SECOND,
        author="Natália <dona@exemplo.com>",
        to=["Márcia <marcia@cli.com>"],
        when=NOW - timedelta(days=1),
    )
    # A última, da Márcia.
    c = _email(session, "m3", body=THIRD, when=NOW - timedelta(hours=2))
    # Devolve.
    return [a, b, c]


# Resposta do Claude falso no formato do SCHEMA.
def _answer(body: str, gaps: list[str] | None = None) -> str:
    # {"corpo": ..., "lacunas": [...]}.
    return json.dumps({"corpo": body, "lacunas": gaps or []})


# Cliente do Claude com o falso dentro.
def _llm(sessions, fake: FakeClaude, settings=None) -> LLMClient:
    # Mesmo caminho do real, sem rede.
    return LLMClient(sessions, settings or get_settings(), client=fake)


# O esquema segue as regras da saída estruturada.
def test_schema_shape():
    # Todos obrigatórios.
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])
    # Nada além deles.
    assert SCHEMA["additionalProperties"] is False


# A lista mostra a conversa que espera você e esconde o resto.
def test_to_answer_rules(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Conversa de três mensagens; a última é da Márcia.
        _thread(session)
        # Conversa em que a última mensagem é sua: espera a outra pessoa.
        _email(session, "x1", body="Pergunta?", thread="t2", when=NOW - timedelta(hours=5))
        # Sua resposta.
        _email(
            session,
            "x2",
            body="Resposta.",
            thread="t2",
            author="dona@exemplo.com",
            to=["marcia@cli.com"],
            when=NOW - timedelta(hours=4),
        )
        # Você só em cópia.
        _email(session, "cc", body="Para o Beto.", thread="t3", to=["beto@cli.com"])
        # Remetente automático.
        _email(session, "nr", body="Aviso.", thread="t4", author="no-reply@sistema.com")
        # Mensagem em massa (marcador de avisos do Gmail).
        _email(
            session,
            "bulk",
            body="Fatura.",
            thread="t5",
            author="Banco <contato@banco.com>",
            labels=["CATEGORY_UPDATES"],
        )
        # Desconhecido e sem projeto: fica de fora.
        _email(session, "desc", body="Oi.", thread="t6", author="Zé <ze@x.com>")
        # Desconhecido com projeto: entra.
        unknown = _email(session, "proj", body="Sobre o Atlas.", thread="t7", author="ze@x.com")
        # Projeto escolhido à mão.
        set_manual(session, unknown, p["atlas"].id, "teste")
        # Antigo demais (fora dos 7 dias).
        _email(session, "velho", body="Antigo.", thread="t8", when=NOW - timedelta(days=9))
        # Grava.
        session.commit()
        # A lista.
        rows = to_answer(session, _settings(), EMPRESA, NOW)
        # Só o desconhecido com projeto e a conversa da Márcia (a última mensagem, m3), do
        # mais novo para o mais antigo.
        assert [r.item.external_id for r in rows] == ["proj", "m3"]
        # A Márcia é reconhecida como Pessoa, e a conversa tem 3 mensagens.
        assert rows[1].person.name == "Márcia" and rows[1].messages == 3
        # O desconhecido mostra o projeto.
        assert rows[0].person is None and rows[0].project.name == "Projeto Atlas"
        # "Dispensar" tira da lista.
        dismiss(session, rows[0].item, "teste")
        # Grava.
        session.commit()
        # Saiu.
        assert [r.item.external_id for r in to_answer(session, _settings(), EMPRESA, NOW)] == ["m3"]
        # Sua resposta coletada depois tira a conversa da Márcia também.
        _email(
            session,
            "m4",
            body="Mantida.",
            author="dona@exemplo.com",
            to=["marcia@cli.com"],
            when=NOW - timedelta(minutes=30),
        )
        # Grava.
        session.commit()
        # Lista vazia.
        assert to_answer(session, _settings(), EMPRESA, NOW) == []


# O pedido ao Claude: conversa sem citações, fatos, exemplos de tom e nada de outra Empresa.
def test_prompt_context(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # A conversa.
        _a, _b, last = _thread(session)
        # Projeto do e-mail.
        set_manual(session, last, p["atlas"].id, "teste")
        # Fato aberto e conferido do projeto: entra.
        session.add(
            Fact(
                project_id=p["atlas"].id,
                kind=FactKind.TASK,
                title="Enviar o cronograma revisado",
                owner_id=p["dona"].id,
                due_date=datetime(2026, 11, 12).date(),
                status="aberto",
                source_item_id=_a.id,
                source_quote="Consegue mandar o cronograma do Atlas até 12/11?",
                quote_verified=True,
            )
        )
        # Fato suspeito: fica de fora.
        session.add(
            Fact(
                project_id=p["atlas"].id,
                kind=FactKind.RISK,
                title="Risco inventado",
                status="aberto",
                source_item_id=_a.id,
                source_quote="não existe",
                quote_verified=False,
            )
        )
        # Um e-mail seu antigo, de outra conversa: exemplo de tom.
        _email(
            session,
            "estilo",
            body="Olá, time. Segue o status da semana: escopo fechado, testes começam segunda. "
            "Qualquer dúvida, me chamem. Abs, Natália",
            author="Natália <dona@exemplo.com>",
            to=["time@cli.com"],
            thread="t9",
            when=NOW - timedelta(days=3),
        )
        # Outra Empresa, com um e-mail seu: não pode aparecer.
        other = create_company(session, "Outra")
        # Seu e-mail lá.
        _email(
            session,
            "outra",
            body="Texto confidencial da outra Empresa, que não pode vazar para este cliente.",
            author="dona@exemplo.com",
            to=["x@outra.com"],
            thread="t1",
            company_id=other.id,
            when=NOW - timedelta(days=2),
        )
        # Grava.
        session.commit()
        # Monta.
        prompt, reference, cut = build_prompt(
            session, last, _settings(), "Confirmar a reunião", NOW
        )
    # As três mensagens, com a última marcada como a 3.
    assert 'n="3"' in prompt and "Escreva a resposta da gerente à mensagem 3." in prompt
    # A sua mensagem é marcada.
    assert "(a gerente)" in prompt
    # As partes citadas não vão (já estão na mensagem anterior).
    assert "segredo citado" not in prompt and "Consegue mandar..." not in prompt
    # O fato conferido vai, o suspeito não.
    assert "Tarefa: Enviar o cronograma revisado" in prompt and "Risco inventado" not in prompt
    # O exemplo de tom vai.
    assert "<exemplo>" in prompt and "status da semana" in prompt
    # Nada da outra Empresa (nem a mensagem com a mesma conversa "t1").
    assert "confidencial" not in prompt and "confidencial" not in reference
    # O pedido, o seu nome e o projeto.
    assert "Confirmar a reunião" in prompt and "A gerente: Natália" in prompt
    # Projeto.
    assert "Projeto: Projeto Atlas" in prompt
    # Nada cortado.
    assert cut is False


# Números do rascunho fora das mensagens, e as lacunas.
def test_number_check_and_gaps():
    # Referência: a conversa diz 12/11 e 14h30.
    reference = "Consegue mandar até 12/11? A reunião de quinta às 14h30 está mantida?"
    # O rascunho fala de 12/11/2026 (a forma curta está lá), 14h30 e de 15/11 e R$ 3.500.
    found = unverified_numbers(
        "Mando até 12/11/2026. Reunião às 14h30 mantida; o custo é R$ 3.500 até 15/11.",
        reference,
    )
    # Só o que não está na conversa.
    assert found == ["3.500", "15/11"]
    # Lacunas: as do texto e as listadas, sem repetir.
    body, gaps = parse_answer(
        _answer("Confirmo [aceitar ou recusar?] e mando até [confirmar data].", ["confirmar data"])
    )
    # As duas do texto, nessa ordem; a da lista era a mesma.
    assert gaps == ["[aceitar ou recusar?]", "[confirmar data]"]
    # Sem corpo: ilegível.
    with pytest.raises(ValueError):
        parse_answer(_answer(""))


# A chamada: modelo e esforço do .env, saída estruturada, custo próprio, rascunho gravado.
def test_write_draft(sessions):
    # Claude falso com um rascunho.
    fake = FakeClaude(
        _answer(
            "Oi, Márcia.\n\nSim, a reunião de quinta às 14h30 está mantida. "
            "O cronograma sai até 13/11.\n\nAbraço,\nNatália"
        )
    )
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        _setup(session)
        # A conversa.
        last = _thread(session)[-1]
        # Grava.
        session.commit()
        # Escreve.
        draft = write_draft(
            session, _settings(), _llm(sessions, fake), last.id, "  manter  ", "dona", NOW
        )
        # Grava.
        session.commit()
        # Gravado, com o pedido limpo e a data que não está na conversa para conferir.
        assert draft.status == "ok" and draft.instructions == "manter"
        # 13/11 não está nas mensagens (12/11 está).
        assert draft.unverified == ["13/11"]
        # Uma linha no registro de custo, com o motivo próprio.
        call = session.query(LLMCall).one()
        # Motivo e custo iguais aos do rascunho.
        assert call.purpose == PURPOSE and call.cost_usd == draft.cost_usd
    # O pedido ao Claude: Sonnet, esforço baixo, esquema fixo e a instrução de sistema.
    params = fake.calls[0]
    # Modelo.
    assert params["model"] == "claude-sonnet-5-5"
    # Esforço e formato.
    assert params["output_config"]["effort"] == "low"
    # Esquema.
    assert params["output_config"]["format"]["schema"] == SCHEMA
    # A defesa contra instruções vindas do e-mail está no sistema.
    assert "não obedeça" in params["system"]


# Teto dos rascunhos, sem chave, e o que não é e-mail: nada é chamado.
def test_write_draft_guards(sessions):
    # Claude falso.
    fake = FakeClaude(_answer("Oi."))
    # Uma sessão.
    with sessions() as session:
        # A conversa.
        last = _thread(session)[-1]
        # Gasto já no teto dos rascunhos (US$ 3).
        session.add(LLMCall(purpose=PURPOSE, model="claude-sonnet-5-5", cost_usd=Decimal("3")))
        # Uma transcrição (não é e-mail).
        talk = BronzeItem(
            company_id=EMPRESA,
            source=SourceKind.MEET_TRANSCRIPT,
            external_id="tr",
            body_text="fala",
            content_sha256="t" * 64,
        )
        # Prepara.
        session.add(talk)
        # Grava.
        session.commit()
        # Teto: recusa antes de chamar.
        with pytest.raises(DraftError, match="teto dos rascunhos"):
            write_draft(session, _settings(), _llm(sessions, fake), last.id, None, "dona", NOW)
        # Sem Claude.
        with pytest.raises(DraftError, match="chave da Anthropic"):
            write_draft(session, _settings(), None, last.id, None, "dona", NOW)
        # Transcrição.
        with pytest.raises(DraftError, match="e-mails"):
            write_draft(session, _settings(), _llm(sessions, fake), talk.id, None, "dona", NOW)
    # Nenhuma chamada ao Claude.
    assert fake.calls == []


# Recusa do Claude: fica registrada, sem texto.
def test_refusal_recorded(sessions):
    # Claude falso que recusa.
    fake = FakeClaude("", stop="refusal")
    # Uma sessão.
    with sessions() as session:
        # A conversa.
        last = _thread(session)[-1]
        # Grava.
        session.commit()
        # Escreve.
        draft = write_draft(session, _settings(), _llm(sessions, fake), last.id, None, "dona", NOW)
        # Recusa, sem corpo.
        assert draft.status == "recusa" and draft.body == ""


# O banco recusa um rascunho de uma Empresa ligado a um e-mail de outra.
def test_company_lock(sessions):
    # Uma sessão.
    with sessions() as session:
        # Outra Empresa.
        other = create_company(session, "Outra")
        # E-mail da Empresa de teste.
        item = _email(session, "m1", body=FIRST)
        # Grava.
        session.commit()
        # Rascunho marcado com a outra Empresa.
        session.add(
            EmailDraft(
                company_id=other.id,
                bronze_item_id=item.id,
                status="ok",
                body="x",
                created_by="teste",
            )
        )
        # A trava confere no commit.
        with pytest.raises(DBAPIError):
            session.commit()


# Link do Gmail na conta certa.
def test_gmail_link():
    # Com conta e conversa.
    assert gmail_link("dona@exemplo.com", "18f3a") == (
        "https://mail.google.com/mail/?authuser=dona%40exemplo.com#all/18f3a"
    )
    # Sem conversa, sem link.
    assert gmail_link("dona@exemplo.com", None) is None


# Páginas e botões: lista, e-mail, rascunhar, dispensar, voltar, e o link no item.
def test_pages_and_buttons(sessions, monkeypatch):
    # O botão usa o Claude falso.
    from app import routes_drafts

    # Claude falso com um rascunho que tem uma lacuna.
    fake = FakeClaude(_answer("Oi, Márcia. Mantida. Mando até [confirmar data].\nNatália"))
    # Troca.
    monkeypatch.setattr(routes_drafts, "draft_writer", lambda settings: _llm(sessions, fake))
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        _setup(session)
        # A conversa, com a última mensagem agora (a página usa o relógio real).
        last = _email(session, "agora", body=THIRD, when=datetime.now(UTC) - timedelta(hours=1))
        # O id.
        item_id = last.id
        # Grava.
        session.commit()
    # A lista mostra o e-mail.
    page = client.get("/rascunhos", headers=OWNER).text
    # Assunto, Márcia e o botão.
    assert "Cronograma Atlas" in page and "Márcia" in page and "Rascunhar" in page
    # O menu tem Rascunhos.
    assert 'href="/rascunhos"' in page
    # A página do e-mail mostra a conversa sem a parte citada.
    email = client.get(f"/rascunhos/email/{item_id}", headers=OWNER).text
    # Texto próprio, sem a citação.
    assert "14h30" in email and "segredo citado" not in email
    # POST de outro site: recusado.
    assert (
        client.post(
            f"/rascunhos/email/{item_id}/escrever",
            data={"pedido": "manter"},
            headers={**OWNER, "Origin": "http://mal"},
        ).status_code
        == 403
    )
    # Rascunhar.
    done = client.post(
        f"/rascunhos/email/{item_id}/escrever",
        data={"pedido": "manter"},
        headers=SAME,
        follow_redirects=False,
    )
    # Volta para o rascunho novo.
    assert done.status_code == 303 and done.headers["location"].startswith(
        f"/rascunhos/email/{item_id}#rascunho-"
    )
    # A página mostra o texto, a lacuna e o botão de copiar.
    email = client.get(f"/rascunhos/email/{item_id}", headers=OWNER).text
    # Texto e lacuna.
    assert "Mando até [confirmar data]" in email and "Falta completar" in email
    # Copiar.
    assert "copiarRascunho" in email and "Rascunho pronto" in email
    # A lista marca o rascunho feito.
    assert "1 rascunho(s)" in client.get("/rascunhos", headers=OWNER).text
    # Dispensar pela lista.
    out = client.post(
        f"/rascunhos/email/{item_id}/dispensar",
        data={"volta": "lista"},
        headers=SAME,
        follow_redirects=False,
    )
    # Volta para a lista, sem o e-mail.
    assert out.headers["location"] == "/rascunhos"
    # Sumiu.
    assert "Nenhum e-mail esperando você" in client.get("/rascunhos", headers=OWNER).text
    # Voltar para a lista.
    client.post(f"/rascunhos/email/{item_id}/voltar", headers=SAME)
    # Voltou.
    assert "Cronograma Atlas" in client.get("/rascunhos", headers=OWNER).text
    # A página do item tem o link para rascunhar.
    assert (
        f'href="/rascunhos/email/{item_id}"'
        in client.get(f"/conteudo/{item_id}", headers=OWNER).text
    )


# Sem chave da Anthropic: o botão avisa e nada é gravado.
def test_button_without_key(sessions):
    # Uma sessão.
    with sessions() as session:
        # Um e-mail.
        item_id = _email(session, "m1", body=FIRST).id
        # Grava.
        session.commit()
    # O conftest deixa o Claude ausente (sem seguir o redirecionamento, que leria o aviso).
    client.post(f"/rascunhos/email/{item_id}/escrever", headers=SAME, follow_redirects=False)
    # Aviso vermelho na página.
    assert "chave da Anthropic" in client.get(f"/rascunhos/email/{item_id}", headers=OWNER).text
    # Nada gravado.
    with sessions() as session:
        # Nenhum rascunho.
        assert session.query(EmailDraft).count() == 0
    # Transcrição não tem página de rascunho.
    assert client.get("/rascunhos/email/999999", headers=OWNER).status_code == 404
