"""Testes dos alertas de liderança (app/alerts.py e a página Alertas).

O que provam:
- a regra distingue liderança na empresa (marca na Pessoa) e no projeto (marca na equipe);
- só vira alerta o fato com trecho conferido, com quem disse reconhecido e valendo;
- o mesmo par (regra, fato) não dispara duas vezes;
- tipos, "sem responsável", "o responsável sou eu" e "prazo em até N dias" filtram certo;
- regra nova não dispara para itens mais antigos que a janela (ALERTS_LOOKBACK_DAYS);
- regra de uma Empresa não alerta a outra, e o banco recusa misturar as duas;
- resolver fecha o fato (e as outras regras param), reabrir abre de novo;
- "Extrair de novo" (apagar o fato) apaga o alerta junto;
- a página, o formulário da regra, os botões, o número do menu e o comando funcionam.
"""

# Data e hora (data do item, prazos e o "agora" fixo dos testes).
from datetime import UTC, date, datetime, timedelta

# Biblioteca de testes (pytest.raises confere que um erro acontece).
import pytest

# Cliente HTTP de teste do FastAPI.
from fastapi.testclient import TestClient

# Consultas e apagar.
from sqlalchemy import delete, func, select

# Erro do banco quando uma trava recusa a gravação.
from sqlalchemy.exc import IntegrityError

# O que é testado.
from app.alerts import RuleError, count_new, generate, save_rule, set_status

# Comando de terminal (gerar-alertas).
from app.cli import main as cli_main

# Criação de Empresa.
from app.companies import create_company

# Configuração.
from app.config import get_settings

# O app.
from app.main import app

# Tabelas.
from app.models import Alert, AlertRule, BronzeItem, Fact, FactKind, SourceKind

# Cadastro de projetos e pessoas.
from app.projects import create_project, save_person, set_member

# Empresa de todo teste.
from tests.fakes import EMPRESA

# Cabeçalho do Tailscale da dona.
OWNER = {"Tailscale-User-Login": "dona@exemplo.com"}
# Origem do próprio console no TestClient.
SAME = {**OWNER, "Origin": "http://testserver"}
# Um cliente para o arquivo inteiro.
client = TestClient(app)
# "Agora" fixo: 04/11/2026, 15h em UTC (meio-dia em Brasília).
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
# Hoje em Brasília.
TODAY = date(2026, 11, 4)


# Cada teste começa sem regras. Por quê: a migração 0013 cria as quatro regras-padrão, e
# só a limpeza depois de cada teste (tests/conftest.py) as apaga; o primeiro teste a rodar
# as veria e os outros não. Aqui, todos começam iguais.
@pytest.fixture(autouse=True)
def _no_rules(sessions):
    # Uma sessão.
    with sessions() as session:
        # Apaga as regras (os alertas vão junto, em cascata).
        session.execute(delete(AlertRule))
        # Grava.
        session.commit()


# Configuração de teste com a janela padrão de 30 dias.
def _settings():
    # Copia a configuração de teste.
    return get_settings().model_copy(update={"alerts_lookback_days": 30})


# Cadastros: projeto Atlas; Márcia (liderança na empresa), Paula (liderança só no
# projeto), Beto (sem liderança) e a própria dona como Pessoa.
def _setup(session) -> dict:
    # Projeto.
    atlas = create_project(session, EMPRESA, "Projeto Atlas", "ATL")
    # Márcia: diretora, liderança na empresa; na equipe sem liderança no projeto.
    marcia = save_person(
        session, None, name="Márcia", email="marcia@cli.com", company_id=EMPRESA, is_leader=True
    )
    # Paula: PO, liderança só no projeto.
    paula = save_person(session, None, name="Paula", email="paula@cli.com", company_id=EMPRESA)
    # Beto: analista.
    beto = save_person(session, None, name="Beto", email="beto@cli.com", company_id=EMPRESA)
    # A dona, cadastrada como Pessoa com o login do Tailscale.
    dona = save_person(session, None, name="Natália", email="dona@exemplo.com", company_id=EMPRESA)
    # Equipe.
    set_member(session, atlas.id, marcia.id, "Patrocinadora", False)
    # Paula com liderança no projeto.
    set_member(session, atlas.id, paula.id, "PO", True)
    # Beto.
    set_member(session, atlas.id, beto.id, "Analista", False)
    # Envia.
    session.flush()
    # Devolve.
    return {"atlas": atlas, "marcia": marcia, "paula": paula, "beto": beto, "dona": dona}


# Um item do bronze (e-mail) na data dada.
def _item(session, external_id: str, when: datetime = NOW, company_id: int = EMPRESA):
    # Item.
    item = BronzeItem(
        company_id=company_id,
        source=SourceKind.GMAIL,
        external_id=external_id,
        subject="Atlas",
        body_text="texto",
        occurred_at=when,
        content_sha256=external_id.ljust(64, "0")[:64],
    )
    # Prepara.
    session.add(item)
    # Gera o id.
    session.flush()
    # Devolve.
    return item


# Um fato já extraído (como app/facts.py gravaria).
def _fact(session, item, project, said_by, kind=FactKind.TASK, **extra) -> Fact:
    # Fato.
    fact = Fact(
        project_id=project.id if project else None,
        kind=kind,
        title=extra.pop("title", f"{kind.value} de {said_by.name if said_by else 'ninguém'}"),
        source_item_id=item.id,
        source_quote="trecho do texto original",
        quote_verified=extra.pop("verified", True),
        said_by_id=said_by.id if said_by else None,
        status=extra.pop("status", "aberto"),
        **extra,
    )
    # Prepara.
    session.add(fact)
    # Gera o id.
    session.flush()
    # Devolve.
    return fact


# Uma regra com os valores mais comuns, trocáveis por parâmetro.
def _rule(session, **values) -> AlertRule:
    # Valores-padrão.
    fields = {
        "name": "Pedido de liderança",
        "company_id": None,
        "kinds": ["tarefa"],
        "leadership": "qualquer",
        "owner_rule": "qualquer",
        "due_within_days": None,
        "severity": "alta",
        "active": True,
        "actor": "teste",
    }
    # Troca os pedidos.
    fields.update(values)
    # Grava pela mesma função do console.
    return save_rule(session, None, **fields)


# Alertas gravados: {(id da regra, id do fato): alerta}.
def _alerts(session) -> dict:
    # Todos.
    return {(a.rule_id, a.fact_id): a for a in session.scalars(select(Alert))}


# Liderança na empresa, no projeto e "qualquer uma".
def test_leadership_scopes(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # E-mail.
        item = _item(session, "e1")
        # Tarefa pedida por cada um.
        f_marcia = _fact(session, item, p["atlas"], p["marcia"])
        # Paula.
        f_paula = _fact(session, item, p["atlas"], p["paula"])
        # Beto.
        f_beto = _fact(session, item, p["atlas"], p["beto"])
        # Três regras, uma por escopo.
        any_ = _rule(session, name="Qualquer")
        # Só empresa.
        company = _rule(session, name="Empresa", leadership="empresa")
        # Só projeto.
        project = _rule(session, name="Projeto", leadership="projeto")
        # Aplica.
        summary = generate(session, _settings(), NOW)
        # Grava.
        session.commit()
        # Alertas.
        got = _alerts(session)
        # "Qualquer": Márcia e Paula; Beto não é liderança.
        assert (any_.id, f_marcia.id) in got and (any_.id, f_paula.id) in got
        # Beto nunca.
        assert not any(fact == f_beto.id for _, fact in got)
        # Empresa: só a Márcia.
        assert {f for r, f in got if r == company.id} == {f_marcia.id}
        # Projeto: só a Paula.
        assert {f for r, f in got if r == project.id} == {f_paula.id}
        # Marcas gravadas no alerta.
        assert got[(any_.id, f_marcia.id)].by_company_leader
        # A Márcia não é liderança no projeto.
        assert not got[(any_.id, f_marcia.id)].by_project_leader
        # A Paula é liderança no projeto, não na empresa.
        assert got[(any_.id, f_paula.id)].by_project_leader
        # Idem.
        assert not got[(any_.id, f_paula.id)].by_company_leader
        # Peso e Empresa copiados.
        assert got[(any_.id, f_marcia.id)].severity == "alta"
        # Empresa do item.
        assert got[(any_.id, f_marcia.id)].company_id == EMPRESA
        # Resumo: 2 + 1 + 1.
        assert summary.created == 4


# Só fato conferido, com quem disse reconhecido, valendo e com projeto.
def test_only_trustworthy_facts(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # E-mail.
        item = _item(session, "e1")
        # Suspeito (trecho não achado).
        _fact(session, item, p["atlas"], p["marcia"], verified=False)
        # Quem disse não reconhecido (só o texto).
        _fact(session, item, p["atlas"], None, said_by_text="Falante 2")
        # Sem projeto.
        _fact(session, item, None, p["marcia"])
        # Concluído.
        _fact(session, item, p["atlas"], p["marcia"], status="concluido")
        # Um bom.
        good = _fact(session, item, p["atlas"], p["marcia"], title="vale")
        # Substituído por outro fato: não conta.
        _fact(session, item, p["atlas"], p["marcia"], superseded_by_id=good.id)
        # Regra.
        _rule(session)
        # Aplica.
        generate(session, _settings(), NOW)
        # Grava.
        session.commit()
        # Só o bom.
        assert [f for _, f in _alerts(session)] == [good.id]


# Rodar de novo não repete.
def test_no_repeat(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Um pedido.
        _fact(session, _item(session, "e1"), p["atlas"], p["marcia"])
        # Regra.
        _rule(session)
        # Primeira rodada: 1.
        assert generate(session, _settings(), NOW).created == 1
        # Grava.
        session.commit()
        # Segunda: nada.
        assert generate(session, _settings(), NOW).created == 0
        # Continua um só.
        assert session.scalar(select(func.count()).select_from(Alert)) == 1


# Tipos, responsável e prazo.
def test_kinds_owner_and_due(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # E-mail.
        item = _item(session, "e1")
        # Decisão da Márcia.
        decision = _fact(session, item, p["atlas"], p["marcia"], kind=FactKind.DECISION)
        # Tarefa sem responsável.
        orphan = _fact(session, item, p["atlas"], p["marcia"], title="sem dono")
        # Tarefa com responsável só como texto ("o João"): tem dono.
        _fact(session, item, p["atlas"], p["marcia"], title="joão", owner_text="o João")
        # Tarefa para a dona.
        mine = _fact(session, item, p["atlas"], p["marcia"], title="minha", owner_id=p["dona"].id)
        # Tarefa para o Beto, que vence em 2 dias.
        soon = _fact(
            session,
            item,
            p["atlas"],
            p["marcia"],
            title="logo",
            owner_id=p["beto"].id,
            due_date=TODAY + timedelta(days=2),
        )
        # Vencida ontem.
        late = _fact(
            session, item, p["atlas"], p["marcia"], title="atrasada", due_date=TODAY - timedelta(1)
        )
        # Vence daqui a 10 dias: fora da regra de 3.
        _fact(session, item, p["atlas"], p["marcia"], title="longe", due_date=TODAY + timedelta(10))
        # Venceu há 90 dias: fora da janela (não é alerta, é passado).
        _fact(session, item, p["atlas"], p["marcia"], title="velha", due_date=TODAY - timedelta(90))
        # Regra só de decisão.
        r_decision = _rule(session, name="Decisão", kinds=["decisao"])
        # Regra "sem responsável".
        r_orphan = _rule(session, name="Sem dono", owner_rule="sem_responsavel")
        # Regra "o responsável sou eu".
        r_mine = _rule(session, name="Para mim", owner_rule="eu")
        # Regra de prazo em até 3 dias.
        r_due = _rule(session, name="Prazo", due_within_days=3)
        # Aplica.
        generate(session, _settings(), NOW)
        # Grava.
        session.commit()
        # Alertas por regra.
        got = _alerts(session)
        # Por regra.
        by = {r: {f for rr, f in got if rr == r} for r in (r_decision.id, r_orphan.id)}
        # Decisão: só a decisão.
        assert by[r_decision.id] == {decision.id}
        # Sem dono: a sem responsável e a atrasada (as duas sem ninguém nomeado).
        assert orphan.id in by[r_orphan.id] and late.id in by[r_orphan.id]
        # "o João" tem dono escrito, e as com Pessoa também.
        assert mine.id not in by[r_orphan.id] and soon.id not in by[r_orphan.id]
        # Para mim: só a minha.
        assert {f for r, f in got if r == r_mine.id} == {mine.id}
        # Prazo: a de 2 dias e a vencida ontem.
        assert {f for r, f in got if r == r_due.id} == {soon.id, late.id}


# "O responsável sou eu" também reconhece a conta Google conectada na Empresa.
def test_owner_is_me_by_google_account(sessions):
    # Tokens: tabela da conta Google.
    from app.models import OAuthToken

    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # A dona usa outro e-mail no cliente, cadastrado como Pessoa.
        work = save_person(
            session, None, name="Natália (cliente)", email="natalia@cli.com", company_id=EMPRESA
        )
        # Essa é a conta Google conectada na Empresa.
        session.add(
            OAuthToken(
                company_id=EMPRESA,
                provider="google",
                account_email="Natalia@cli.com",
                scopes=[],
                refresh_token_enc=b"x",
            )
        )
        # Tarefa para ela.
        fact = _fact(session, _item(session, "e1"), p["atlas"], p["marcia"], owner_id=work.id)
        # Regra.
        rule = _rule(session, owner_rule="eu")
        # Aplica.
        generate(session, _settings(), NOW)
        # Grava.
        session.commit()
        # Disparou.
        assert (rule.id, fact.id) in _alerts(session)


# Item mais antigo que a janela não dispara (regra sem prazo).
def test_lookback_window(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # E-mail de 40 dias atrás.
        old = _item(session, "velho", when=NOW - timedelta(days=40))
        # Pedido antigo.
        _fact(session, old, p["atlas"], p["marcia"])
        # E-mail de 10 dias atrás.
        recent = _item(session, "recente", when=NOW - timedelta(days=10))
        # Pedido recente.
        fact = _fact(session, recent, p["atlas"], p["marcia"])
        # Regra.
        _rule(session)
        # Aplica.
        generate(session, _settings(), NOW)
        # Grava.
        session.commit()
        # Só o recente.
        assert [f for _, f in _alerts(session)] == [fact.id]


# Regra de uma Empresa não alerta a outra; o banco recusa misturar.
def test_company_scope(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros da Empresa 1.
        p = _setup(session)
        # Pedido na Empresa 1.
        fact = _fact(session, _item(session, "e1"), p["atlas"], p["marcia"])
        # Outra Empresa.
        other = create_company(session, "Cliente B")
        # Regra só da Empresa B.
        rule_b = _rule(session, name="Só B", company_id=other.id)
        # Aplica.
        generate(session, _settings(), NOW)
        # Grava.
        session.commit()
        # Nada: o fato é da Empresa 1.
        assert _alerts(session) == {}
        # Gravar à força um alerta da Empresa 1 com a regra da B.
        session.add(Alert(company_id=EMPRESA, rule_id=rule_b.id, fact_id=fact.id, severity="alta"))
        # A trava do banco recusa no commit.
        with pytest.raises(IntegrityError):
            session.commit()


# Validação da regra.
def test_rule_validation(sessions):
    # Uma sessão.
    with sessions() as session:
        # Tipo desconhecido.
        with pytest.raises(RuleError):
            _rule(session, kinds=["fofoca"])
        # Prazo fora da faixa.
        with pytest.raises(RuleError):
            _rule(session, due_within_days=99)
        # Sem nome.
        with pytest.raises(RuleError):
            _rule(session, name="  ")
        # Liderança inventada.
        with pytest.raises(RuleError):
            _rule(session, leadership="todo mundo")


# Resolver fecha o fato; reabrir abre; apagar o fato apaga o alerta.
def test_status_and_fact_lifecycle(sessions):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Pedido que vence amanhã.
        fact = _fact(
            session,
            _item(session, "e1"),
            p["atlas"],
            p["marcia"],
            due_date=TODAY + timedelta(days=1),
        )
        # Regra de pedido.
        rule = _rule(session)
        # Aplica.
        generate(session, _settings(), NOW)
        # O alerta.
        alert = _alerts(session)[(rule.id, fact.id)]
        # Resolve.
        set_status(session, alert, "resolvido", "teste")
        # Grava.
        session.commit()
        # Fato fechado.
        assert session.get(Fact, fact.id).status == "concluido"
        # Regra de prazo criada depois: não dispara para o fato fechado.
        _rule(session, name="Prazo", due_within_days=3)
        # Aplica.
        assert generate(session, _settings(), NOW).created == 0
        # Reabre.
        set_status(session, alert, "novo", "teste")
        # Grava.
        session.commit()
        # Fato aberto de novo, alerta sem data de mudança.
        assert session.get(Fact, fact.id).status == "aberto" and alert.status_at is None
        # "Extrair de novo" apaga o fato.
        session.execute(delete(Fact).where(Fact.id == fact.id))
        # Grava.
        session.commit()
        # O alerta foi junto (cascata no banco).
        assert session.scalar(select(func.count()).select_from(Alert)) == 0


# Página, formulário, botões, número do menu e comando.
def test_page_forms_and_menu(sessions, capsys):
    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Um pedido da Márcia.
        _fact(session, _item(session, "e1"), p["atlas"], p["marcia"], title="Enviar cronograma")
        # Grava.
        session.commit()
    # Página sem regras: aviso.
    page = client.get("/alertas", headers=OWNER)
    # Abre.
    assert page.status_code == 200 and "Nenhuma regra" in page.text
    # Regra com prazo inválido: 400 com a mensagem.
    bad = client.post(
        "/alertas/regras",
        headers=SAME,
        data={
            "nome": "X",
            "tipos": "tarefa",
            "lideranca": "qualquer",
            "responsavel": "qualquer",
            "peso": "alta",
            "prazo": "99",
            "ativa": "1",
        },
    )
    # Recusada.
    assert bad.status_code == 400 and "O prazo vai de 0 a 60 dias" in bad.text
    # Regra válida: grava e já aplica.
    ok = client.post(
        "/alertas/regras",
        headers=SAME,
        data={
            "nome": "Pedido da diretoria",
            "tipos": ["tarefa", "prazo"],
            "lideranca": "empresa",
            "responsavel": "qualquer",
            "peso": "alta",
            "prazo": "",
            "ativa": "1",
        },
        follow_redirects=False,
    )
    # Volta para a página filtrada pela regra.
    assert ok.status_code == 303 and "regra=" in ok.headers["location"]
    # Página: o alerta, quem disse e o número no menu.
    page = client.get("/alertas", headers=OWNER)
    # O fato.
    assert "Enviar cronograma" in page.text and "Márcia (liderança na empresa)" in page.text
    # O número de novos no menu.
    assert 'class="contador"' in page.text
    # O alerta gravado.
    with sessions() as session:
        # Um só.
        alert = session.scalars(select(Alert)).one()
        # Regra gravada com os dois tipos.
        rule = session.get(AlertRule, alert.rule_id)
        # Confere.
        assert rule.fact_kinds == ["tarefa", "prazo"] and rule.leadership == "empresa"
        # Ids para os próximos passos.
        alert_id, rule_id = alert.id, rule.id
    # Pedido de outro site é recusado.
    assert (
        client.post(
            f"/alertas/{alert_id}/situacao",
            headers={**OWNER, "Origin": "https://mal.com"},
            data={"situacao": "visto"},
        ).status_code
        == 403
    )
    # Marca como visto, com volta para a página filtrada.
    seen = client.post(
        f"/alertas/{alert_id}/situacao",
        headers=SAME,
        data={"situacao": "visto", "volta": "/alertas?situacao=todos"},
        follow_redirects=False,
    )
    # Volta para onde estava.
    assert seen.status_code == 303 and seen.headers["location"] == "/alertas?situacao=todos"
    # Volta para outro site é ignorada.
    evil = client.post(
        f"/alertas/{alert_id}/situacao",
        headers=SAME,
        data={"situacao": "visto", "volta": "//mal.com"},
        follow_redirects=False,
    )
    # Página de Alertas.
    assert evil.headers["location"] == "/alertas"
    # Sem novos: o menu não mostra número.
    with sessions() as session:
        # Zero.
        assert count_new(session) == 0
    # Formulário da regra abre com o nome.
    assert "Pedido da diretoria" in client.get(f"/alertas/regras/{rule_id}", headers=OWNER).text
    # Comando de terminal: nada novo.
    cli_main(["gerar-alertas"])
    # Mostra.
    assert "nenhum novo" in capsys.readouterr().out
    # Apaga a regra: os alertas vão junto.
    client.post(f"/alertas/regras/{rule_id}/apagar", headers=SAME)
    # Confere.
    with sessions() as session:
        # Nenhum alerta.
        assert session.scalar(select(func.count()).select_from(Alert)) == 0


# As quatro regras-padrão da migração (os testes esvaziam as tabelas, então o texto da
# migração é conferido diretamente).
def test_default_rules_are_valid(sessions):
    # importlib carrega a migração pelo caminho do arquivo.
    import importlib.util

    # Caminho.
    spec = importlib.util.spec_from_file_location(
        "m0013", "migrations/versions/0013_alertas_de_lideranca.py"
    )
    # Módulo.
    module = importlib.util.module_from_spec(spec)
    # Executa o arquivo (só define constantes e funções).
    spec.loader.exec_module(module)
    # Cada regra-padrão passa na mesma validação do console.
    with sessions() as session:
        # Cada uma.
        for name, kinds, leadership, owner, days, severity in module.DEFAULT_RULES:
            # Grava sem erro.
            _rule(
                session,
                name=name,
                kinds=kinds,
                leadership=leadership,
                owner_rule=owner,
                due_within_days=days,
                severity=severity,
            )
        # Quatro.
        assert session.scalar(select(func.count()).select_from(AlertRule)) == 4


# Duas regras para o mesmo fato: uma linha na página, um no número, botões para as duas.
def test_one_row_per_fact(sessions):
    # Agrupamento usado pela página.
    from app.alerts import group_by_fact, list_alerts, overview

    # Uma sessão.
    with sessions() as session:
        # Cadastros.
        p = _setup(session)
        # Pedido da Márcia que vence amanhã.
        fact = _fact(
            session, _item(session, "e1"), p["atlas"], p["marcia"], due_date=TODAY + timedelta(1)
        )
        # Regra de pedido.
        _rule(session)
        # Regra de prazo.
        _rule(session, name="Prazo", due_within_days=3)
        # Aplica: dois alertas.
        assert generate(session, _settings(), NOW).created == 2
        # Grava.
        session.commit()
        # Uma linha, com a outra regra em "também".
        rows = group_by_fact(list_alerts(session, None, "abertos"))
        # Confere: a linha mostra uma regra e cita a outra (as duas têm o mesmo peso).
        assert len(rows) == 1 and {rows[0][0][1].name, *rows[0][1]} == {
            "Pedido de liderança",
            "Prazo",
        }
        # O número do menu e o da página contam o fato uma vez.
        assert count_new(session) == 1 and overview(session, None)["por_situacao"]["novo"] == 1
        # "Visto" na linha vale para os dois alertas.
        set_status(session, rows[0][0][0], "visto", "teste")
        # Grava.
        session.commit()
        # Os dois vistos.
        assert {a.status for a in session.scalars(select(Alert))} == {"visto"}
        # O fato continua aberto (visto não fecha).
        assert session.get(Fact, fact.id).status == "aberto"
