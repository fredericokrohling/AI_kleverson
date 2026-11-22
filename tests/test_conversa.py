"""Testes da conversa com os dados pelo Claude (bloco 7): a API e o servidor MCP local.

O que estes testes garantem:
- a API só responde ao seu login do Tailscale, com o cabeçalho do servidor MCP, e recusa
  pedido vindo de outro site;
- uma Empresa por vez: nada de outra Empresa aparece em nenhuma ferramenta, e um id de
  outra Empresa responde como inexistente;
- a busca acha com e sem acento, nos anexos, com filtros e em páginas; um texto enorme
  não trava a gravação do item;
- fatos: só conferidos e abertos por padrão, com filtros de tipo, responsável e prazo;
- alertas, briefing, reuniões e a leitura de fontes trazem a fonte com o link;
- "pôr na agenda" segue as regras do PR #22 e não cria duas vezes;
- o diário registra o que saiu para o Claude;
- o servidor MCP local lista as ferramentas e conversa com a API de verdade.
"""

# asyncio roda as funções assíncronas do cliente MCP dentro de um teste comum.
import asyncio

# importlib carrega o servidor MCP local pelo caminho do arquivo (ele não é um pacote).
import importlib.util

# json lê o texto das respostas das ferramentas.
import json

# Data, data e hora e fuso UTC.
from datetime import UTC, date, datetime, timedelta

# Path monta o caminho do arquivo do servidor MCP local.
from pathlib import Path

# Biblioteca de testes.
import pytest

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Cliente MCP da biblioteca oficial (nos testes, conecta direto ao servidor, sem stdio).
from mcp import Client

# Consultas.
from sqlalchemy import select

# A agenda (os testes trocam o cliente do Google por um falso).
from app import calendar_events

# Criação de Empresa.
from app.companies import create_company

# Configuração.
from app.config import get_settings

# O app.
from app.main import app

# Tabelas.
from app.models import (
    Alert,
    AlertRule,
    Attachment,
    AuditLog,
    Briefing,
    BronzeItem,
    ClassificationMethod,
    CreatedEvent,
    Fact,
    FactKind,
    ItemClassification,
    Meeting,
    MeetingAttendee,
    SourceKind,
)

# Cadastro de projetos e pessoas.
from app.projects import create_project, save_person

# Empresa de todo teste e a agenda falsa.
from tests.fakes import EMPRESA, FakeCalendar

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Cabeçalhos do servidor MCP local: o login e o cabeçalho próprio.
MCP = {**OWNER, "X-Kleverson-Cliente": "mcp"}
# Um cliente para o arquivo inteiro.
client = TestClient(app)
# Caminho do servidor MCP local.
LOCAL_SCRIPT = Path(__file__).parent.parent / "mcp_local" / "kleverson_mcp.py"


# Carrega o servidor MCP local como módulo (cada teste recebe uma cópia nova).
def _load_local():
    # Especificação: nome e caminho.
    spec = importlib.util.spec_from_file_location("kleverson_mcp_teste", LOCAL_SCRIPT)
    # Módulo vazio a partir da especificação.
    module = importlib.util.module_from_spec(spec)
    # Executa o arquivo dentro do módulo.
    spec.loader.exec_module(module)
    # Devolve.
    return module


# Um item no bronze, com contador para ids e impressões digitais diferentes.
_counter = {"n": 0}


# Cria um item (e-mail por padrão) e devolve.
def _item(session, company_id: int = EMPRESA, **values) -> BronzeItem:
    # Próximo número.
    _counter["n"] += 1
    # Número.
    n = _counter["n"]
    # Item com valores padrão, trocáveis pelo teste.
    item = BronzeItem(
        company_id=company_id,
        source=values.pop("source", SourceKind.GMAIL),
        external_id=f"item-{n}",
        subject=values.pop("subject", f"Assunto {n}"),
        body_text=values.pop("body_text", "texto"),
        author=values.pop("author", "Márcia <marcia@cli.com>"),
        occurred_at=values.pop("occurred_at", datetime(2026, 11, 3, 12, tzinfo=UTC)),
        content_sha256=str(n).rjust(64, "0"),
        **values,
    )
    # Prepara.
    session.add(item)
    # Gera o id.
    session.flush()
    # Devolve.
    return item


# Classifica um item num projeto.
def _classify(session, item: BronzeItem, project_id: int) -> None:
    # Classificação manual.
    session.add(
        ItemClassification(
            bronze_item_id=item.id, project_id=project_id, method=ClassificationMethod.MANUAL
        )
    )


# Um fato de um item.
def _fact(session, item: BronzeItem, project_id: int | None, **values) -> Fact:
    # Fato com valores padrão.
    fact = Fact(
        project_id=project_id,
        kind=values.pop("kind", FactKind.TASK),
        title=values.pop("title", "Enviar cronograma"),
        source_item_id=item.id,
        source_quote=values.pop("quote", "enviar o cronograma até segunda"),
        quote_verified=values.pop("verified", True),
        status=values.pop("status", "aberto"),
        **values,
    )
    # Prepara.
    session.add(fact)
    # Gera o id.
    session.flush()
    # Devolve.
    return fact


# Duas Empresas com a mesma palavra: a base de vários testes de isolamento.
def _two_companies(session) -> dict:
    # Segunda Empresa.
    other = create_company(session, "Outra Cliente")
    # Projeto na Empresa 1.
    atlas = create_project(session, EMPRESA, "Atlas", "AT")
    # Projeto na outra.
    boreal = create_project(session, other.id, "Boreal", "BO")
    # E-mail da Empresa 1 com acento.
    mine = _item(session, subject="Reunião de orçamento", body_text="Vamos revisar o orçamento.")
    # Classificado no Atlas.
    _classify(session, mine, atlas.id)
    # E-mail da outra Empresa com as mesmas palavras.
    theirs = _item(
        session, other.id, subject="Reunião de orçamento", body_text="Orçamento da outra."
    )
    # Classificado no Boreal.
    _classify(session, theirs, boreal.id)
    # Devolve tudo o que os testes usam.
    return {"outra": other.id, "atlas": atlas.id, "boreal": boreal.id, "meu": mine.id}


# --- Proteção ------------------------------------------------------------------------------


# Sem o cabeçalho próprio, sem o login ou de outro site: recusado.
def test_protection():
    # Sem o cabeçalho do servidor MCP (um site no navegador não consegue pôr).
    assert client.get("/api/conversa/empresas", headers=OWNER).status_code == 403
    # Sem o login do Tailscale.
    assert (
        client.get("/api/conversa/empresas", headers={"X-Kleverson-Cliente": "mcp"}).status_code
        == 403
    )
    # Com Origin de outro site.
    bad = {**MCP, "Origin": "https://site-malicioso.com"}
    # Recusado.
    assert client.get("/api/conversa/empresas", headers=bad).status_code == 403
    # Origem do próprio console: aceito.
    same = {**MCP, "Origin": "http://testserver"}
    # Aceito.
    assert client.get("/api/conversa/empresas", headers=same).status_code == 200
    # O caso normal: o servidor MCP local não manda Origin.
    assert client.get("/api/conversa/empresas", headers=MCP).status_code == 200


# --- Empresas ------------------------------------------------------------------------------


# Empresas com projetos e contagem de fatos abertos e conferidos.
def test_companies(sessions):
    # Prepara.
    with sessions() as session:
        # Duas Empresas.
        ids = _two_companies(session)
        # Item do Atlas.
        item = session.get(BronzeItem, ids["meu"])
        # Duas tarefas abertas e conferidas.
        _fact(session, item, ids["atlas"])
        _fact(session, item, ids["atlas"], title="Outra tarefa")
        # Um risco suspeito (não conta).
        _fact(session, item, ids["atlas"], kind=FactKind.RISK, verified=False)
        # Grava.
        session.commit()
    # Pede.
    body = client.get("/api/conversa/empresas", headers=MCP).json()
    # Por nome.
    by_name = {c["nome"]: c for c in body["empresas"]}
    # As duas.
    assert set(by_name) == {"Empresa Teste", "Outra Cliente"}
    # Atlas: duas tarefas; o risco suspeito não conta.
    atlas = by_name["Empresa Teste"]["projetos"][0]
    # Confere.
    assert atlas["nome"] == "Atlas" and atlas["abertos"] == {"Tarefa": 2}
    # A outra Empresa só tem o projeto dela.
    assert [p["nome"] for p in by_name["Outra Cliente"]["projetos"]] == ["Boreal"]


# --- Busca ---------------------------------------------------------------------------------


# Com ou sem acento, só na Empresa pedida, com a fonte e o projeto.
def test_search_accents_and_isolation(sessions):
    # Prepara.
    with sessions() as session:
        # Duas Empresas com a mesma palavra.
        ids = _two_companies(session)
        # Grava.
        session.commit()
    # Sem acento acha o texto com acento.
    for words in ("reuniao orcamento", "reunião orçamento", "Reunião orcamento"):
        # Pede.
        body = client.get(
            "/api/conversa/buscar", params={"empresa": EMPRESA, "q": words}, headers=MCP
        ).json()
        # Um resultado: o da Empresa 1 (o da outra não aparece).
        assert [r["fonte"]["id"] for r in body["resultados"]] == [ids["meu"]], words
    # O resultado traz a fonte completa e o projeto.
    hit = body["resultados"][0]
    # Fonte.
    assert hit["fonte"]["tipo"] == "e-mail"
    # Link do console.
    assert hit["fonte"]["link"].endswith(f"/conteudo/{ids['meu']}")
    # Projeto.
    assert hit["projeto"] == "Atlas"
    # Trecho com a palavra marcada.
    assert "«" in hit["trecho"]
    # Na outra Empresa, só o dela.
    body = client.get(
        "/api/conversa/buscar", params={"empresa": ids["outra"], "q": "orcamento"}, headers=MCP
    ).json()
    # Um, da outra.
    assert len(body["resultados"]) == 1 and body["resultados"][0]["projeto"] == "Boreal"
    # Projeto da outra Empresa pedido na Empresa 1: inexistente.
    response = client.get(
        "/api/conversa/buscar",
        params={"empresa": EMPRESA, "q": "orcamento", "projeto": ids["boreal"]},
        headers=MCP,
    )
    # 404 com a frase.
    assert response.status_code == 404 and "Projeto" in response.json()["erro"]


# Anexos, filtros de fonte, período e projeto, páginas e pedidos inválidos.
def test_search_attachments_filters_pages(sessions):
    # Prepara.
    with sessions() as session:
        # Projeto.
        atlas = create_project(session, EMPRESA, "Atlas", "AT")
        # E-mail sem a palavra, com um anexo que tem.
        mail = _item(session, subject="Segue o anexo", body_text="ver anexo")
        # Anexo com texto.
        session.add(
            Attachment(
                bronze_item_id=mail.id,
                filename="contrato.pdf",
                extracted_text="Cláusula de multa por atraso na entrega.",
            )
        )
        # Transcrição com a palavra, de outro dia.
        talk = _item(
            session,
            source=SourceKind.MEET_TRANSCRIPT,
            subject="Reunião semanal",
            body_text="Falamos da multa e do atraso.",
            occurred_at=datetime(2026, 10, 20, 15, tzinfo=UTC),
        )
        # Só a transcrição no projeto.
        _classify(session, talk, atlas.id)
        # Cinco e-mails com "atraso", para as páginas.
        for n in range(5):
            _item(session, subject=f"Atraso {n}", body_text="atraso na obra")
        # Grava.
        session.commit()
        # Ids.
        mail_id, talk_id, atlas_id = mail.id, talk.id, atlas.id

    # Atalho de busca.
    def find(**params):
        # Pede e devolve o JSON.
        return client.get(
            "/api/conversa/buscar", params={"empresa": EMPRESA, **params}, headers=MCP
        ).json()

    # "multa": o anexo e a transcrição.
    hits = find(q="multa")["resultados"]
    # O anexo aparece com o nome e o id para ler inteiro.
    attachment = next(h for h in hits if h["onde"].startswith("anexo"))
    # Confere.
    assert attachment["onde"] == "anexo: contrato.pdf" and attachment["fonte"]["id"] == mail_id
    # Tem anexo_id.
    assert attachment["anexo_id"] > 0
    # Só transcrições.
    assert [h["fonte"]["id"] for h in find(q="multa", fonte="transcricao")["resultados"]] == [
        talk_id
    ]
    # Só o projeto Atlas.
    assert [h["fonte"]["id"] for h in find(q="multa", projeto=atlas_id)["resultados"]] == [talk_id]
    # Só a partir de 01/11 (a transcrição é de 20/10).
    recent = find(q="multa", desde="2026-11-01")["resultados"]
    # Só o anexo.
    assert [h["fonte"]["id"] for h in recent] == [mail_id]
    # Até 25/10: só a transcrição.
    old = find(q="multa", ate="2026-10-25")["resultados"]
    # Confere.
    assert [h["fonte"]["id"] for h in old] == [talk_id]
    # Páginas de 3: "atraso" acha 5 e-mails, o anexo e a transcrição (7).
    first = find(q="atraso", limite=3)
    # Três e a próxima posição.
    assert len(first["resultados"]) == 3 and first["proximo_inicio"] == 3
    # Última página.
    last = find(q="atraso", limite=3, inicio=6)
    # Uma e acabou.
    assert len(last["resultados"]) == 1 and last["proximo_inicio"] is None
    # Fonte desconhecida: 400 com as opções.
    bad = client.get(
        "/api/conversa/buscar", params={"empresa": EMPRESA, "q": "x", "fonte": "x"}, headers=MCP
    )
    # Confere.
    assert bad.status_code == 400 and "email" in bad.json()["erro"]
    # Empresa inexistente: 404 com a lista das Empresas.
    missing = client.get("/api/conversa/buscar", params={"empresa": 99, "q": "x"}, headers=MCP)
    # Confere.
    assert missing.status_code == 404 and "Empresa Teste" in missing.json()["erro"]


# Um texto enorme (um arquivo grande do Drive) é gravado e o começo dele é achado.
def test_huge_text_is_saved(sessions):
    # Prepara.
    with sessions() as session:
        # 600 mil caracteres de palavras diferentes (sem o corte de 100 mil, a lista de
        # palavras passaria de 1 MB e o Postgres recusaria a gravação).
        words = " ".join(f"palavra{n}" for n in range(60000))
        # Item do Drive.
        _item(session, source=SourceKind.DRIVE, subject="Planilha gigante", body_text=words)
        # Grava (aqui falharia sem o corte).
        session.commit()
    # O começo é achado.
    body = client.get(
        "/api/conversa/buscar", params={"empresa": EMPRESA, "q": "palavra10"}, headers=MCP
    ).json()
    # Um resultado.
    assert len(body["resultados"]) == 1


# --- Fatos ---------------------------------------------------------------------------------


# Só abertos e conferidos por padrão; filtros de tipo, responsável, prazo; Empresa.
def test_facts(sessions):
    # Prepara.
    with sessions() as session:
        # Duas Empresas.
        ids = _two_companies(session)
        # Item da Empresa 1.
        item = session.get(BronzeItem, ids["meu"])
        # Márcia, responsável.
        marcia = save_person(
            session, None, name="Márcia Lima", email="marcia@cli.com", company_id=1
        )
        # Você, cadastrada com o login do Tailscale.
        me = save_person(session, None, name="Natália", email="dona@exemplo.com", company_id=1)
        # Tarefa da Márcia, com prazo.
        _fact(session, item, ids["atlas"], owner_id=marcia.id, due_date=date(2026, 11, 10))
        # Tarefa sua, com prazo mais perto.
        _fact(
            session,
            item,
            ids["atlas"],
            title="Aprovar escopo",
            owner_id=me.id,
            due_date=date(2026, 11, 7),
        )
        # Tarefa concluída.
        _fact(session, item, ids["atlas"], title="Feita", status="concluido")
        # Risco suspeito.
        _fact(session, item, ids["atlas"], kind=FactKind.RISK, title="Risco", verified=False)
        # Decisão (sem situação).
        _fact(
            session, item, ids["atlas"], kind=FactKind.DECISION, title="Fornecedor X", status=None
        )
        # Fato da outra Empresa.
        theirs = session.get(BronzeItem, ids["meu"] + 1)
        # Tarefa lá.
        _fact(session, theirs, ids["boreal"], title="Da outra")
        # Grava.
        session.commit()

    # Atalho.
    def titles(**params):
        # Pede.
        body = client.get(
            "/api/conversa/fatos", params={"empresa": EMPRESA, **params}, headers=MCP
        ).json()
        # Títulos na ordem.
        return [f["titulo"] for f in body["fatos"]]

    # Padrão: abertos e conferidos, prazo mais perto primeiro; nada da outra Empresa.
    assert titles() == ["Aprovar escopo", "Enviar cronograma"]
    # Os seus.
    assert titles(responsavel="eu") == ["Aprovar escopo"]
    # Pelo nome, sem acento e maiúscula exatos.
    assert titles(responsavel="márcia") == ["Enviar cronograma"]
    # Prazo até 08/11.
    assert titles(prazo_ate="2026-11-08") == ["Aprovar escopo"]
    # Decisões: só com "todos".
    assert titles(tipo="decisao") == []
    # Com "todos".
    assert titles(tipo="decisao", situacao="todos") == ["Fornecedor X"]
    # Suspeito só a pedido.
    assert titles(tipo="risco", suspeitos="true") == ["Risco"]
    # Um fato traz o trecho, a fonte e "na_agenda".
    fact = client.get("/api/conversa/fatos", params={"empresa": EMPRESA}, headers=MCP).json()
    # O primeiro.
    first = fact["fatos"][0]
    # Confere.
    assert first["trecho"] and first["conferido"] is True and first["na_agenda"] is False
    # Fonte com link.
    assert first["fonte"]["id"] == ids["meu"] and first["prazo"] == "2026-11-07"
    # Tipo desconhecido: 400.
    bad = client.get("/api/conversa/fatos", params={"empresa": EMPRESA, "tipo": "x"}, headers=MCP)
    # Confere.
    assert bad.status_code == 400


# --- Alertas, briefing e reuniões ----------------------------------------------------------


# Alertas abertos, uma linha por fato, com o porquê.
def test_alerts(sessions):
    # Prepara.
    with sessions() as session:
        # Duas Empresas.
        ids = _two_companies(session)
        # Item.
        item = session.get(BronzeItem, ids["meu"])
        # Liderança que disse.
        boss = save_person(session, None, name="Diretora", email="dir@cli.com", company_id=1)
        # Fato dito por ela.
        fact = _fact(session, item, ids["atlas"], said_by_id=boss.id)
        # Duas regras.
        rules = [AlertRule(name=n, company_id=None) for n in ("Pedidos", "Tudo")]
        # Prepara.
        session.add_all(rules)
        # Gera os ids.
        session.flush()
        # Dois alertas do mesmo fato (um por regra) e um resolvido de outro fato.
        session.add_all(
            [
                Alert(
                    company_id=EMPRESA,
                    rule_id=rules[0].id,
                    fact_id=fact.id,
                    severity="alta",
                    by_company_leader=True,
                ),
                Alert(company_id=EMPRESA, rule_id=rules[1].id, fact_id=fact.id, severity="baixa"),
                Alert(
                    company_id=EMPRESA,
                    rule_id=rules[1].id,
                    fact_id=_fact(session, item, ids["atlas"], title="Velho").id,
                    severity="alta",
                    status="resolvido",
                ),
            ]
        )
        # Grava.
        session.commit()
    # Pede.
    body = client.get("/api/conversa/alertas", params={"empresa": EMPRESA}, headers=MCP).json()
    # Um aberto (o resolvido fica fora), com a regra mais forte e a outra.
    assert body["total_abertos"] == 1
    # A linha.
    alert = body["alertas"][0]
    # Regra forte e a outra.
    assert alert["regra"] == "Pedidos" and alert["outras_regras"] == ["Tudo"]
    # Porquê.
    assert alert["por_que"] == "Diretora (liderança na empresa)"
    # O fato com a fonte.
    assert alert["fato"]["fonte"]["id"] == ids["meu"]
    # Na outra Empresa, nenhum.
    other = client.get("/api/conversa/alertas", params={"empresa": ids["outra"]}, headers=MCP)
    # Confere.
    assert other.json()["alertas"] == []


# Briefing do dia: o conteúdo gravado, ou o aviso de que não existe.
def test_briefing(sessions):
    # Sem briefing: aviso, sem inventar.
    body = client.get(
        "/api/conversa/briefing", params={"empresa": EMPRESA, "dia": "2026-11-05"}, headers=MCP
    ).json()
    # Confere.
    assert body["briefing"] is None and "05/11/2026" in body["aviso"]
    # Grava um.
    with sessions() as session:
        # Briefing do dia 05/11.
        session.add(
            Briefing(
                company_id=EMPRESA,
                day=date(2026, 11, 5),
                generated_at=datetime(2026, 11, 5, 10, tzinfo=UTC),
                since=datetime(2026, 11, 4, 10, tzinfo=UTC),
                content={"agenda": [{"titulo": "Comitê"}]},
            )
        )
        # Grava.
        session.commit()
    # Pede.
    body = client.get(
        "/api/conversa/briefing", params={"empresa": EMPRESA, "dia": "2026-11-05"}, headers=MCP
    ).json()
    # O conteúdo, a hora de Brasília e o link.
    assert body["briefing"]["agenda"][0]["titulo"] == "Comitê"
    # 10h UTC = 7h em Brasília.
    assert body["montado_em"].startswith("2026-11-05T07:00")
    # Link com a Empresa e o dia.
    assert body["link"].endswith("/briefing?empresa=1&data=2026-11-05")
    # Ler pelo Claude não marca como lido.
    with sessions() as session:
        # Confere.
        assert session.scalars(select(Briefing)).one().viewed_at is None


# Reuniões do período, com participantes e transcrições; nada da outra Empresa.
def test_meetings(sessions):
    # Prepara.
    with sessions() as session:
        # Outra Empresa.
        other = create_company(session, "Outra Cliente")
        # Agora (as reuniões ficam no período padrão).
        now = datetime.now(UTC)
        # Reunião de ontem.
        meeting = Meeting(company_id=EMPRESA, title="Comitê Atlas", starts_at=now - timedelta(1))
        # Reunião da outra Empresa.
        theirs = Meeting(company_id=other.id, title="Comitê Boreal", starts_at=now - timedelta(1))
        # Reunião antiga (fora do período padrão).
        old = Meeting(company_id=EMPRESA, title="Antiga", starts_at=now - timedelta(60))
        # Prepara.
        session.add_all([meeting, theirs, old])
        # Gera os ids.
        session.flush()
        # Participante.
        session.add(MeetingAttendee(meeting_id=meeting.id, name="Márcia", name_key="marcia"))
        # Transcrição ligada.
        talk = _item(session, source=SourceKind.MEET_TRANSCRIPT, meeting_id=meeting.id)
        # Grava.
        session.commit()
        # Id.
        talk_id = talk.id
    # Período padrão.
    body = client.get("/api/conversa/reunioes", params={"empresa": EMPRESA}, headers=MCP).json()
    # Só a de ontem.
    assert [m["titulo"] for m in body["reunioes"]] == ["Comitê Atlas"]
    # Participantes e transcrição.
    assert body["reunioes"][0]["participantes"] == ["Márcia"]
    # Transcrição com a fonte.
    assert body["reunioes"][0]["transcricoes"][0]["id"] == talk_id
    # Com período largo, a antiga também.
    wide = client.get(
        "/api/conversa/reunioes",
        params={"empresa": EMPRESA, "desde": "2020-01-01", "titulo": "antiga"},
        headers=MCP,
    ).json()
    # Confere.
    assert [m["titulo"] for m in wide["reunioes"]] == ["Antiga"]


# --- Ler uma fonte -------------------------------------------------------------------------


# Texto próprio do e-mail, a conversa inteira, páginas, anexos e Empresa.
def test_read_source(sessions, monkeypatch):
    # Prepara.
    with sessions() as session:
        # Outra Empresa e um item dela.
        other = create_company(session, "Outra Cliente")
        # Item de lá.
        theirs = _item(session, other.id)
        # E-mail com resposta e a conversa citada abaixo.
        mail = _item(
            session,
            subject="Re: cronograma",
            body_text="Combinado, envio amanhã.\n\nEm 03/11, Márcia escreveu:\n> Pode enviar?",
            extra={"para": ["dona@exemplo.com"], "cc": []},
        )
        # Anexo com texto e anexo sem texto.
        doc = Attachment(bronze_item_id=mail.id, filename="cron.xlsx", extracted_text="A" * 30)
        # Sem texto.
        empty = Attachment(bronze_item_id=mail.id, filename="foto.heic", extraction_error="formato")
        # Prepara.
        session.add_all([doc, empty])
        # Um fato do e-mail.
        _fact(session, mail, None)
        # Grava.
        session.commit()
        # Ids.
        mail_id, theirs_id, doc_id, empty_id = mail.id, theirs.id, doc.id, empty.id

    # Atalho.
    def read(item_id, **params):
        # Pede.
        return client.get(
            f"/api/conversa/fontes/{item_id}", params={"empresa": EMPRESA, **params}, headers=MCP
        )

    # Texto próprio, sem a conversa citada.
    body = read(mail_id).json()
    # Confere.
    assert "Combinado" in body["texto"] and "Pode enviar" not in body["texto"]
    # Destinatários, anexos e fatos.
    assert body["para"] == ["dona@exemplo.com"]
    # Anexos com e sem texto.
    assert [(a["nome"], a["tem_texto"]) for a in body["anexos"]] == [
        ("cron.xlsx", True),
        ("foto.heic", False),
    ]
    # Fato do item.
    assert body["fatos"][0]["titulo"] == "Enviar cronograma"
    # A conversa inteira, a pedido.
    assert "Pode enviar" in read(mail_id, conversa_inteira="true").json()["texto"]
    # Páginas de 10 caracteres no anexo de 30.
    monkeypatch.setattr(get_settings(), "mcp_source_chars", 10)
    # Primeira página.
    page = read(mail_id, anexo=doc_id).json()
    # Dez caracteres, total 30, próxima em 10.
    assert (len(page["texto"]), page["total_caracteres"], page["proximo_inicio"]) == (10, 30, 10)
    # Última.
    assert read(mail_id, anexo=doc_id, inicio=20).json()["proximo_inicio"] is None
    # Anexo sem texto: a frase com o motivo.
    no_text = read(mail_id, anexo=empty_id)
    # Confere.
    assert no_text.status_code == 400 and "formato" in no_text.json()["erro"]
    # Anexo de outro item: inexistente.
    assert read(theirs_id, anexo=doc_id).status_code == 404
    # Item de outra Empresa: inexistente (e nada dele sai).
    response = read(theirs_id)
    # Confere.
    assert response.status_code == 404 and "texto" not in response.json()


# --- Pôr na agenda -------------------------------------------------------------------------


# Cria uma vez, com as regras do PR #22; a segunda vez e outra Empresa são recusadas.
def test_put_on_agenda(sessions, monkeypatch):
    # Agenda falsa.
    calendar = FakeCalendar([])
    # No lugar da do Google.
    monkeypatch.setattr(calendar_events, "calendar_client", lambda db, settings, cid: calendar)
    # Prepara.
    with sessions() as session:
        # Duas Empresas.
        ids = _two_companies(session)
        # Fato com prazo.
        fact = _fact(
            session, session.get(BronzeItem, ids["meu"]), ids["atlas"], due_date=date(2026, 11, 10)
        )
        # Fato sem prazo.
        no_date = _fact(session, session.get(BronzeItem, ids["meu"]), ids["atlas"], title="Sem")
        # Grava.
        session.commit()
        # Ids.
        fact_id, no_date_id = fact.id, no_date.id

    # Atalho.
    def put(fid, company=EMPRESA):
        # POST.
        return client.post(
            f"/api/conversa/fatos/{fid}/agenda", params={"empresa": company}, headers=MCP
        )

    # Cria.
    response = put(fact_id)
    # Confere.
    assert response.status_code == 200 and response.json()["dia"] == "2026-11-10"
    # Foi ao Google sem convidados e sem aviso.
    body, send_updates = calendar.inserted[0]
    # Confere.
    assert "attendees" not in body and send_updates == "none"
    # Segunda vez: recusado com a frase.
    again = put(fact_id)
    # Confere.
    assert again.status_code == 400 and "já está na agenda" in again.json()["erro"]
    # Sem prazo: a frase do PR #22.
    assert "não tem prazo" in put(no_date_id).json()["erro"]
    # Pela outra Empresa: inexistente.
    assert put(fact_id, ids["outra"]).status_code == 404
    # Um registro só.
    with sessions() as session:
        # Confere.
        assert len(session.scalars(select(CreatedEvent)).all()) == 1


# --- Diário --------------------------------------------------------------------------------


# O diário registra o que saiu para o Claude, com a Empresa.
def test_audit(sessions):
    # Prepara.
    with sessions() as session:
        # Um item.
        _item(session, subject="Orçamento")
        # Grava.
        session.commit()
    # Uma busca.
    client.get("/api/conversa/buscar", params={"empresa": EMPRESA, "q": "orcamento"}, headers=MCP)
    # O registro.
    with sessions() as session:
        # Último.
        row = session.scalars(select(AuditLog).where(AuditLog.action == "conversa.buscar")).one()
    # Quem, Empresa, pergunta e quantos.
    assert row.actor == "dona@exemplo.com" and row.company_id == EMPRESA
    # Detalhes.
    assert row.details == {"pergunta": "orcamento", "resultados": 1}


# --- Servidor MCP local --------------------------------------------------------------------


# O JSON de uma resposta de ferramenta (o primeiro bloco de texto).
def _json(result) -> dict:
    # Texto → dicionário.
    return json.loads(result.content[0].text)


# Roda uma função assíncrona com o cliente MCP ligado ao servidor local.
def _with_local(module, fn):
    # Função assíncrona que abre o cliente e chama fn.
    async def run():
        # Cliente MCP direto no servidor (sem stdio).
        async with Client(module.mcp) as mcp_client:
            # O que o teste quer.
            return await fn(mcp_client)

    # Roda até o fim.
    return asyncio.run(run())


# As ferramentas existem, com as dicas certas.
def test_local_tools():
    # Carrega.
    module = _load_local()

    # Lista.
    async def tools(mcp_client):
        # Pede a lista.
        return (await mcp_client.list_tools()).tools

    # Nome → ferramenta.
    found = {t.name: t for t in _with_local(module, tools)}
    # As oito.
    assert set(found) == {
        "listar_empresas",
        "buscar",
        "listar_fatos",
        "alertas_abertos",
        "briefing",
        "reunioes",
        "ler_fonte",
        "por_na_agenda",
    }
    # Só a da agenda escreve.
    writers = {name for name, t in found.items() if not t.annotations.read_only_hint}
    # Confere.
    assert writers == {"por_na_agenda"}
    # Toda ferramenta, menos a primeira, pede a Empresa.
    for name, tool in found.items():
        # A primeira não pede.
        if name != "listar_empresas":
            assert "empresa" in tool.input_schema["required"], name


# Sem KLEVERSON_URL: a frase que diz o que configurar.
def test_local_without_url(monkeypatch):
    # Sem a variável.
    monkeypatch.delenv("KLEVERSON_URL", raising=False)
    # Carrega.
    module = _load_local()

    # Chama.
    async def call(mcp_client):
        # Pede as Empresas.
        return await mcp_client.call_tool("listar_empresas", {})

    # Resultado.
    result = _with_local(module, call)
    # Erro com a frase.
    assert result.is_error and "KLEVERSON_URL" in result.content[0].text


# De ponta a ponta: o servidor local chama a API de verdade (pelo cliente de teste).
def test_local_end_to_end(sessions, monkeypatch):
    # Agenda falsa.
    calendar = FakeCalendar([])
    # No lugar da do Google.
    monkeypatch.setattr(calendar_events, "calendar_client", lambda db, settings, cid: calendar)
    # Prepara.
    with sessions() as session:
        # Duas Empresas.
        ids = _two_companies(session)
        # Fato com prazo.
        fact = _fact(
            session, session.get(BronzeItem, ids["meu"]), ids["atlas"], due_date=date(2026, 11, 10)
        )
        # Grava.
        session.commit()
        # Id.
        fact_id = fact.id
    # Carrega o servidor local.
    module = _load_local()
    # O cliente HTTP dele vira o cliente de teste, com o login do Tailscale (no seu
    # computador, quem põe o login é o Tailscale; o cabeçalho próprio, o programa).
    module._client = TestClient(app, headers={**OWNER, **module.CLIENT_HEADER})

    # A conversa: Empresas, busca, erro e agenda.
    async def chat(mcp_client):
        # Empresas.
        companies = await mcp_client.call_tool("listar_empresas", {})
        # Busca sem acento.
        found = await mcp_client.call_tool("buscar", {"empresa": EMPRESA, "palavras": "orcamento"})
        # Empresa inexistente.
        missing = await mcp_client.call_tool("buscar", {"empresa": 99, "palavras": "x"})
        # Pôr na agenda.
        agenda = await mcp_client.call_tool(
            "por_na_agenda", {"empresa": EMPRESA, "fato_id": fact_id}
        )
        # Devolve os quatro.
        return companies, found, missing, agenda

    # Roda.
    companies, found, missing, agenda = _with_local(module, chat)
    # Empresas: as duas (o JSON da resposta vem como texto para o Claude).
    names = {c["nome"] for c in _json(companies)["empresas"]}
    # Confere.
    assert names == {"Empresa Teste", "Outra Cliente"}
    # Busca: só o e-mail da Empresa 1.
    hits = _json(found)["resultados"]
    # Confere.
    assert [h["fonte"]["id"] for h in hits] == [ids["meu"]]
    # Erro: a frase da API chega ao Claude.
    assert missing.is_error and "Empresa 99 não existe" in missing.content[0].text
    # Agenda: criado.
    assert _json(agenda)["criado"] is True and len(calendar.inserted) == 1


# O servidor local também roda como programa (stdio), do jeito que o Claude Desktop liga.
@pytest.mark.parametrize("mode", ["legacy"])
def test_local_runs_over_stdio(mode):
    # Peças do cliente por stdio.
    import sys

    from mcp import StdioServerParameters

    # Parâmetros: o Python atual rodando o arquivo, com um endereço que não responde.
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(LOCAL_SCRIPT)],
        env={"KLEVERSON_URL": "http://127.0.0.1:9"},
    )

    # Conecta, lista e chama.
    async def run():
        # mode="legacy": o aperto de mão clássico (initialize), o que o Claude Desktop usa.
        async with Client(params, mode=mode) as mcp_client:
            # Ferramentas.
            tools = (await mcp_client.list_tools()).tools
            # Uma chamada (o console não responde nesse endereço).
            result = await mcp_client.call_tool("listar_empresas", {})
            # Devolve.
            return tools, result

    # Roda.
    tools, result = asyncio.run(run())
    # Oito ferramentas.
    assert len(tools) == 8
    # Erro claro de rede.
    assert result.is_error and "Tailscale" in result.content[0].text
