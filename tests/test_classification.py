"""Testes da classificação por projeto em cascata (app/classification.py).

O que provam:
- cada passo da cascata decide sozinho quando aponta um só projeto (tag à mão, pasta,
  mesma conversa, equipe, nome no texto), sem chamar o Claude;
- o Claude só é chamado quando os sinais empatam ou não existem, e só vê os projetos da
  Empresa do item;
- resposta com pouca confiança, ilegível ou com projeto fora da lista vai para a fila;
- a sua escolha no console trava a decisão; tag nova reabre a decisão automática;
- a página Classificação e as ações do item funcionam.
"""

# Monta as respostas JSON do Claude falso.
import json

# Decimal: confiança exata.
from decimal import Decimal

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Consultas.
from sqlalchemy import select

# O que é testado.
from app.classification import (
    pending_ids,
    run_pending,
    set_manual,
)

# Criação de Empresa.
from app.companies import create_company

# Configuração.
from app.config import Settings, get_settings

# Cliente do Claude (com o Claude falso dentro).
from app.llm import LLMClient

# O app.
from app.main import app

# Tabelas.
from app.models import (
    BronzeItem,
    ClassificationMethod,
    ItemClassification,
    LLMCall,
    SourceKind,
    TaggedItem,
)

# Regras de tag (colocar à mão e aplicar as regras a um item).
from app.project_tags import RULE_LABEL, add_manual, apply_rules

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


# Configuração de teste: confiança mínima 0,7 e até 30 chamadas por rodada.
def _settings() -> Settings:
    # Copia a configuração de teste com os valores da classificação.
    return get_settings().model_copy(
        update={"classify_min_confidence": Decimal("0.7"), "classify_llm_per_run": 30}
    )


# Resposta do Claude falso em JSON.
def _answer(project: int | None, confidence: float, reason: str = "pista no texto") -> str:
    # Mesmo formato pedido no SYSTEM.
    return json.dumps({"projeto": project, "confianca": confidence, "motivo": reason})


# Grava um item do bronze e aplica as regras de tag (como a coleta faz).
def _item(
    session,
    external_id: str,
    *,
    subject: str = "",
    body: str = "",
    author: str = "",
    thread: str | None = None,
    source: SourceKind = SourceKind.GMAIL,
    company: int = EMPRESA,
) -> BronzeItem:
    # Item.
    item = BronzeItem(
        company_id=company,
        source=source,
        external_id=external_id,
        thread_id=thread,
        subject=subject,
        body_text=body,
        author=author,
        content_sha256=external_id.ljust(64, "0")[:64],
    )
    # Prepara.
    session.add(item)
    # Gera o id.
    session.flush()
    # Regras de tag (nome no texto, equipe).
    apply_rules(session, item)
    # Envia ao banco.
    session.flush()
    # Devolve.
    return item


# Dois projetos com equipe: Atlas (Márcia) e Boreal (Beto). Ana está nos dois.
def _projects(session) -> dict:
    # Projeto Atlas.
    atlas = create_project(session, EMPRESA, "Projeto Atlas", "ATL", "Atlas")
    # Projeto Boreal.
    boreal = create_project(session, EMPRESA, "Projeto Boreal", "BOR", "Boreal")
    # Pessoas.
    marcia = save_person(session, None, name="Márcia", email="marcia@cli.com", company_id=EMPRESA)
    # Pessoas.
    beto = save_person(session, None, name="Beto", email="beto@cli.com", company_id=EMPRESA)
    # Ana: dos dois projetos (não decide sozinha).
    ana = save_person(session, None, name="Ana", email="ana@cli.com", company_id=EMPRESA)
    # Equipes.
    set_member(session, atlas.id, marcia.id, "Patrocinadora", True)
    # Beto no Boreal.
    set_member(session, boreal.id, beto.id, "Analista", False)
    # Ana nos dois.
    set_member(session, atlas.id, ana.id, "PMO", False)
    # Ana nos dois.
    set_member(session, boreal.id, ana.id, "PMO", False)
    # Envia.
    session.flush()
    # Ids.
    return {"atlas": atlas, "boreal": boreal}


# Cada passo das regras decide sozinho, sem chamar o Claude.
def test_rules_decide_without_claude(sessions):
    # Claude falso que não deveria ser chamado.
    fake = FakeClaude(_answer(None, 0.9))
    # Uma sessão.
    with sessions() as session:
        # Projetos.
        p = _projects(session)
        # Equipe exclusiva: Márcia só está no Atlas.
        team = _item(
            session, "e1", subject="Status", body="Segue.", author="Márcia <marcia@cli.com>"
        )
        # Nome no texto: "Boreal" no assunto, remetente de fora.
        text = _item(
            session,
            "e2",
            subject="Proposta Boreal",
            body="Ok.",
            author="x@forn.com",
            thread="t-boreal",
        )
        # Mesma conversa da anterior, sem sinal próprio.
        same = _item(
            session,
            "e3",
            subject="Re: proposta",
            body="Fechado.",
            author="y@forn.com",
            thread="t-boreal",
        )
        # Pasta do Gmail ligada ao Atlas (a regra do marcador grava a tag assim).
        folder = _item(session, "e4", subject="Contrato", body="Anexo.", author="z@x.com")
        # Tag de pasta.
        session.add(TaggedItem(bronze_item_id=folder.id, tag_id=p["atlas"].tag_id, rule=RULE_LABEL))
        # Tag à mão do Boreal num e-mail que cita o Atlas: a sua tag vale mais.
        manual = _item(session, "e5", subject="Atlas e outras coisas", body="...", author="w@x.com")
        # Coloca à mão.
        add_manual(session, manual.id, _tag(session, p["boreal"].tag_id))
        # Grava.
        session.commit()
        # Ids.
        ids = {
            k: v.id
            for k, v in {
                "team": team,
                "text": text,
                "same": same,
                "folder": folder,
                "manual": manual,
            }.items()
        }
        # Projetos.
        atlas, boreal = p["atlas"].id, p["boreal"].id
    # Rodada.
    summary = run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Nenhuma chamada ao Claude.
    assert fake.calls == []
    # Cinco pelas regras.
    assert summary.by_rules == 5
    # Conferência.
    with sessions() as session:
        # Método e projeto de cada item.
        got = {k: session.get(ItemClassification, v) for k, v in ids.items()}
    # Equipe exclusiva.
    assert (got["team"].project_id, got["team"].method) == (atlas, ClassificationMethod.STAKEHOLDER)
    # Nome no texto.
    assert (got["text"].project_id, got["text"].method) == (boreal, ClassificationMethod.KEYWORD)
    # Mesma conversa.
    assert (got["same"].project_id, got["same"].method) == (boreal, ClassificationMethod.THREAD)
    # Pasta.
    assert (got["folder"].project_id, got["folder"].method) == (atlas, ClassificationMethod.FOLDER)
    # À mão.
    assert (got["manual"].project_id, got["manual"].method) == (boreal, ClassificationMethod.MANUAL)
    # Nada pendente depois.
    with sessions() as session:
        # Fila vazia.
        assert pending_ids(session) == []


# A tag de um projeto, pelo id.
def _tag(session, tag_id: int):
    # Tabela das tags.
    from app.models import Tag

    # Busca.
    return session.get(Tag, tag_id)


# Sinais em conflito: o Claude escolhe só entre os candidatos.
def test_conflict_goes_to_claude_with_candidates(sessions):
    # Claude escolhe o Boreal com 90%.
    with sessions() as session:
        # Projetos.
        p = _projects(session)
        # Márcia (equipe do Atlas) escreve sobre o Boreal.
        item = _item(session, "c1", subject="Boreal", body="Ajuda?", author="marcia@cli.com")
        # Grava.
        session.commit()
        # Ids.
        item_id, boreal = item.id, p["boreal"].id
    # Claude falso.
    fake = FakeClaude(_answer(boreal, 0.9, "assunto cita Boreal"))
    # Rodada.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Uma chamada, com o modelo Haiku e esforço baixo.
    assert len(fake.calls) == 1
    # Modelo.
    assert fake.calls[0]["model"] == "claude-haiku-5-5"
    # Esforço.
    assert fake.calls[0]["output_config"] == {"effort": "low"}
    # O pedido traz os dois projetos.
    prompt = fake.calls[0]["messages"][0]["content"]
    # Os dois.
    assert "Projeto Atlas" in prompt and "Projeto Boreal" in prompt
    # Decisão gravada.
    with sessions() as session:
        # Linha.
        row = session.get(ItemClassification, item_id)
        # Boreal, pelo Claude, fora da fila.
        assert (row.project_id, row.method, row.needs_review) == (
            boreal,
            ClassificationMethod.LLM,
            False,
        )
        # Candidatos guardados.
        assert len(row.candidates) == 2
        # Motivo do Claude.
        assert row.reason == "assunto cita Boreal"
        # Custo registrado com o motivo "classificacao".
        assert session.scalar(select(LLMCall.purpose)) == "classificacao"


# Sem sinal: o Claude vê só os projetos da Empresa do item; "nenhum" é aceito.
def test_no_signal_sees_only_company_projects(sessions):
    # Uma sessão.
    with sessions() as session:
        # Projetos da Empresa 1.
        _projects(session)
        # Outra Empresa com um projeto próprio.
        other = create_company(session, "Outro Cliente")
        # Projeto de lá.
        create_project(session, other.id, "Projeto Segredo")
        # Newsletter sem sinal nenhum.
        item = _item(session, "n1", subject="Newsletter", body="Promoções", author="news@loja.com")
        # Grava.
        session.commit()
        # Id.
        item_id = item.id
    # Claude diz "nenhum" com certeza.
    fake = FakeClaude(_answer(None, 0.95, "é propaganda"))
    # Rodada.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # O pedido não cita o projeto da outra Empresa (premissa 6).
    prompt = fake.calls[0]["messages"][0]["content"]
    # Só os dois da Empresa 1.
    assert "Segredo" not in prompt and "Projeto Atlas" in prompt
    # Gravado como "sem projeto", sem revisão.
    with sessions() as session:
        # Linha.
        row = session.get(ItemClassification, item_id)
        # Nenhum projeto.
        assert row.project_id is None and not row.needs_review


# Pouca confiança, resposta ilegível ou projeto inventado: fila "A revisar".
def test_doubtful_answers_go_to_review(sessions):
    # Uma sessão.
    with sessions() as session:
        # Projetos.
        p = _projects(session)
        # Três itens sem sinal.
        ids = [_item(session, f"d{i}", subject=f"Assunto {i}", body="?").id for i in range(3)]
        # Grava.
        session.commit()
        # Atlas.
        atlas = p["atlas"].id
    # Respostas: 40% de confiança; texto sem JSON; projeto 999 (fora da lista).
    for item_id, text in zip(
        ids, [_answer(atlas, 0.4), "Acho que é o Atlas.", _answer(999, 0.9)], strict=True
    ):
        # Claude falso desta resposta.
        fake = FakeClaude(text)
        # Rodada com uma chamada: os itens são processados na ordem, então cada rodada
        # pergunta ao Claude sobre o próximo item (os outros ficam esperando).
        run_pending(
            sessions,
            _settings().model_copy(update={"classify_llm_per_run": 1}),
            LLMClient(sessions, _settings(), fake),
        )
        # Conferência.
        with sessions() as session:
            # Linha.
            row = session.get(ItemClassification, item_id)
            # Na fila.
            assert row.needs_review
    # A página mostra a fila.
    page = client.get("/classificacao", headers=OWNER).text
    # Três itens a revisar.
    assert "Assunto 0" in page and "Assunto 2" in page


# A sua escolha trava; tag nova reabre só as automáticas; o atalho não paga de novo.
def test_manual_lock_and_reopen(sessions):
    # Uma sessão.
    with sessions() as session:
        # Projetos.
        p = _projects(session)
        # Ana (dos dois projetos) escreve: conflito.
        auto = _item(session, "m1", subject="Status", body="Ok", author="ana@cli.com")
        # Outro item, que você vai travar.
        locked = _item(session, "m2", subject="Status 2", body="Ok", author="ana@cli.com")
        # Grava.
        session.commit()
        # Ids.
        auto_id, locked_id = auto.id, locked.id
        # Projetos.
        atlas, boreal = p["atlas"].id, p["boreal"].id
        # Tag do Atlas.
        atlas_tag = p["atlas"].tag_id
    # Claude escolhe o Atlas.
    fake = FakeClaude(_answer(atlas, 0.8))
    # Rodada.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Duas chamadas (uma por item).
    assert len(fake.calls) == 2
    # Você escolhe o Boreal no segundo item pela página.
    client.post(f"/conteudo/{locked_id}/projeto", data={"projeto": str(boreal)}, headers=SAME)
    # Você tira a tag do Atlas dos dois (a "equipe" de Ana): só o automático volta à fila.
    with sessions() as session:
        # Tira à mão.
        from app.project_tags import remove_manual

        # Nos dois itens.
        remove_manual(session, auto_id, atlas_tag)
        # No travado também.
        remove_manual(session, locked_id, atlas_tag)
        # Grava.
        session.commit()
        # Só o automático está pendente.
        assert pending_ids(session) == [auto_id]
    # Rodada: sobra só a equipe do Boreal no automático → regras decidem, sem Claude.
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Nenhuma chamada nova.
    assert len(fake.calls) == 2
    # Conferência.
    with sessions() as session:
        # Automático: Boreal pela equipe.
        row = session.get(ItemClassification, auto_id)
        # Equipe.
        assert (row.project_id, row.method) == (boreal, ClassificationMethod.STAKEHOLDER)
        # Travado: continua a sua escolha.
        mine = session.get(ItemClassification, locked_id)
        # Sua.
        assert (mine.project_id, mine.locked) == (boreal, True)
        # Você põe à mão a tag do Atlas de volta no automático.
        add_manual(session, auto_id, _tag(session, atlas_tag))
        # Grava.
        session.commit()
        # A tag recolocada reabre a decisão.
        assert pending_ids(session) == [auto_id]
    # Rodada: a tag à mão decide (vale mais que a equipe).
    run_pending(sessions, _settings(), LLMClient(sessions, _settings(), fake))
    # Conferência.
    with sessions() as session:
        # Atlas, por você (pela tag).
        row = session.get(ItemClassification, auto_id)
        # Manual, mas não travada (tirar a tag reabre).
        assert (row.project_id, row.method, row.locked) == (
            atlas,
            ClassificationMethod.MANUAL,
            False,
        )
    # Devolver o travado à automática apaga a decisão.
    client.post(f"/conteudo/{locked_id}/projeto/automatico", headers=SAME)
    # Conferência.
    with sessions() as session:
        # Sem linha.
        assert session.get(ItemClassification, locked_id) is None


# O projeto de outra Empresa não pode ser escolhido para um item.
def test_manual_rejects_other_company(sessions):
    # Uma sessão.
    with sessions() as session:
        # Projetos da Empresa 1.
        _projects(session)
        # Outra Empresa e um projeto dela.
        other = create_company(session, "Outro Cliente")
        # Projeto de lá.
        foreign = create_project(session, other.id, "Projeto Segredo")
        # Item da Empresa 1.
        item = _item(session, "x1", subject="Oi")
        # Grava.
        session.commit()
        # Tentativa direta.
        try:
            # Deve recusar.
            set_manual(session, item, foreign.id, "dona")
            # Não chegou aqui.
            raise AssertionError("aceitou projeto de outra Empresa")
        # Recusou.
        except ValueError:
            pass
        # Id.
        item_id, foreign_id = item.id, foreign.id
    # Pela página: aviso de erro e nenhuma decisão.
    client.post(f"/conteudo/{item_id}/projeto", data={"projeto": str(foreign_id)}, headers=SAME)
    # Conferência.
    with sessions() as session:
        # Nada gravado.
        assert session.get(ItemClassification, item_id) is None


# Empresa sem projeto ativo: nada fica pendente (não há o que escolher).
def test_company_without_projects_is_not_pending(sessions):
    # Uma sessão.
    with sessions() as session:
        # Item sem projeto nenhum cadastrado.
        _item(session, "z1", subject="Oi")
        # Grava.
        session.commit()
        # Nada pendente.
        assert pending_ids(session) == []


# Sem Claude (sem chave ou teto): as regras decidem, o resto espera.
def test_without_claude_rules_still_run(sessions):
    # Uma sessão.
    with sessions() as session:
        # Projetos.
        _projects(session)
        # Um item que as regras decidem e um que precisa do Claude.
        _item(session, "w1", subject="Atlas", body="x", author="x@x.com")
        # Sem sinal.
        _item(session, "w2", subject="Oi", body="x", author="x@x.com")
        # Grava.
        session.commit()
    # Rodada sem Claude.
    summary = run_pending(sessions, _settings(), None)
    # Um pelas regras, um esperando.
    assert (summary.by_rules, summary.waiting) == (1, 1)
    # A página mostra um classificado e um esperando.
    page = client.get(f"/classificacao?empresa={EMPRESA}", headers=OWNER).text
    # Título.
    assert "Classificação" in page and "Esperando" in page
