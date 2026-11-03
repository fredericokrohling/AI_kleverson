"""Empresas (os clientes que a dona atende): cadastro, idioma e correção de Empresa.

Por que a Empresa é o nível mais alto: cada cliente tem os seus projetos, pessoas, tags,
pastas do Drive e uma conta de e-mail própria, e nada de um pode aparecer para outro.
O pedido da dona (03/11/2026) lista as premissas; as que mudam o código:

1. os dados de uma Empresa não se misturam com os de outra. Por isso a coluna company_id
   é obrigatória no banco e uma trava (gatilho da migração 0008) recusa ligar linhas de
   Empresas diferentes, mesmo que o código erre;
2. cada Empresa tem uma única conta de e-mail: o que a conta coleta é daquela Empresa;
3. pessoas e tags não são compartilhadas: o mesmo e-mail (ou nome de tag) em duas
   Empresas são dois cadastros;
6. a IA nunca lê conteúdo de uma Empresa para responder sobre outra.

Correção de Empresa: uma transcrição (importada ou da extensão) pode ter sido posta na
Empresa errada. Corrigir não é só trocar uma coluna: as tags, as pessoas ligadas às
falas, a classificação em projeto e os fatos foram calculados com os cadastros da
Empresa antiga. move_item desfaz tudo isso e refaz com os cadastros da nova.
"""

# Consultas.
from sqlalchemy import delete, func, select

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Configuração (idioma geral).
from app.config import Settings

# Tabelas.
from app.models import (
    BronzeItem,
    Company,
    Fact,
    ItemClassification,
    LiveMeeting,
    MeetingParticipant,
    OAuthToken,
    SourceKind,
    Stakeholder,
    TaggedItem,
    Utterance,
    audit,
)

# Reconhecimento de pessoas nas falas.
from app.people import PeopleIndex, attendees_of

# Regras de tag de projeto.
from app.project_tags import apply_rules

# Idiomas aceitos e o idioma geral.
from app.runtime_settings import TRANSCRIPTION_LANGUAGES, get_value

# Tamanho máximo do nome (igual à coluna companies.name).
_MAX_NAME = 200


# Todas as Empresas, em ordem alfabética.
def list_companies(session: Session) -> list[Company]:
    # lower(): ordem alfabética sem separar maiúsculas de minúsculas.
    return list(session.scalars(select(Company).order_by(func.lower(Company.name))))


# id → nome, para mostrar a Empresa nas listas sem uma consulta por linha.
def company_names(session: Session) -> dict[int, str]:
    # Um dicionário a partir das linhas.
    return {c.id: c.name for c in list_companies(session)}


# Empresa escolhida num formulário. Vazia ou inexistente: erro com o motivo.
def parse_company(session: Session, value: str | int | None) -> Company:
    """O combo de Empresa é obrigatório nos cadastros: sem ele, nada é gravado."""
    # Texto do formulário (ou número já convertido).
    text = str(value or "").strip()
    # Não é um número: nada escolhido.
    if not text.isdigit():
        raise ValueError("Escolha a Empresa.")
    # Busca pela chave.
    company = session.get(Company, int(text))
    # Id que não existe (Empresa apagada, formulário antigo).
    if company is None:
        raise ValueError("Empresa não encontrada. Recarregue a página.")
    # Achou.
    return company


# Filtro de Empresa das telas: número = aquela Empresa; vazio ou "todos" = todas.
def filter_id(value: str | None) -> int | None:
    # Só dígitos contam.
    text = (value or "").strip()
    # Número: a Empresa; qualquer outra coisa: todas.
    return int(text) if text.isdigit() else None


# Limpa e valida o nome de uma Empresa.
def _clean_name(name: str) -> str:
    # Espaços repetidos e das pontas.
    name = " ".join((name or "").split())
    # Vazio.
    if not name:
        raise ValueError("Informe o nome da Empresa.")
    # Longo demais.
    if len(name) > _MAX_NAME:
        raise ValueError(f"Nome de Empresa com mais de {_MAX_NAME} caracteres.")
    # Pronto.
    return name


# Idioma escolhido: vazio = usa o geral; fora da lista = erro.
def _clean_language(language: str | None) -> str | None:
    # Vazio vira None (vale o idioma geral de Configurações).
    language = (language or "").strip() or None
    # Fora da lista.
    if language is not None and language not in TRANSCRIPTION_LANGUAGES:
        raise ValueError(f"Idioma não permitido: {language}")
    # Pronto.
    return language


# Outra Empresa com o mesmo nome (sem diferenciar maiúsculas)?
def _same_name(session: Session, name: str, other_than: int | None = None) -> Company | None:
    # Consulta pelo nome em minúsculas.
    query = select(Company).where(func.lower(Company.name) == name.lower())
    # Ao renomear, a própria Empresa não conta.
    if other_than is not None:
        query = query.where(Company.id != other_than)
    # A primeira (só pode haver uma).
    return session.scalars(query).first()


# Cadastra uma Empresa.
def create_company(session: Session, name: str, language: str | None = None) -> Company:
    """Nome repetido ou vazio vira ValueError."""
    # Valida.
    name, language = _clean_name(name), _clean_language(language)
    # Repetido.
    if _same_name(session, name):
        raise ValueError(f"A Empresa “{name}” já existe.")
    # Nova linha.
    company = Company(name=name, transcription_language=language)
    # Prepara.
    session.add(company)
    # Gera o id.
    session.flush()
    # Diário.
    audit(session, "dona", "empresa.criar", target=name, company_id=company.id)
    # Devolve.
    return company


# Renomeia uma Empresa e troca o idioma padrão dela.
def update_company(session: Session, company_id: int, name: str, language: str | None) -> Company:
    # Busca.
    company = session.get(Company, company_id)
    # Não existe.
    if company is None:
        raise ValueError("Empresa não encontrada.")
    # Valida.
    name, language = _clean_name(name), _clean_language(language)
    # Nome de outra Empresa.
    if _same_name(session, name, other_than=company.id):
        raise ValueError(f"A Empresa “{name}” já existe.")
    # Diário com o antes e o depois.
    audit(
        session,
        "dona",
        "empresa.alterar",
        target=name,
        company_id=company.id,
        de=company.name,
        idioma=language or "padrão geral",
    )
    # Troca.
    company.name, company.transcription_language = name, language
    # Devolve.
    return company


# Conta Google de cada Empresa: id da Empresa → linha do token.
def accounts_by_company(session: Session) -> dict[int, OAuthToken]:
    # Cada Empresa tem no máximo uma (regra do banco).
    return {t.company_id: t for t in session.scalars(select(OAuthToken))}


# Idioma da transcrição de uma reunião: o da Empresa, ou o geral.
def transcription_language(session: Session, settings: Settings, company_id: int | None) -> str:
    # Empresa conhecida e com idioma próprio: ele vale.
    company = session.get(Company, company_id) if company_id else None
    # Idioma próprio.
    if company is not None and company.transcription_language:
        return company.transcription_language
    # Senão, o geral (Configurações, ou o .env).
    return get_value(session, settings, "transcription_language")


# --- Correção de Empresa -------------------------------------------------------


# Acerta a Pessoa de cada participante de uma reunião depois de trocar a Empresa dela.
def relink_participants(session: Session, meeting: LiveMeeting) -> None:
    """Pessoa de outra Empresa sai; o e-mail religa à Pessoa da Empresa nova, se houver."""
    # Sem Empresa: nenhum participante pode ficar ligado a uma Pessoa.
    people = PeopleIndex(session, meeting.company_id) if meeting.company_id else None
    # Cada participante.
    for participant in session.scalars(
        select(MeetingParticipant).where(MeetingParticipant.meeting_id == meeting.id)
    ):
        # Pessoa atual.
        person = (
            session.get(Stakeholder, participant.stakeholder_id)
            if participant.stakeholder_id
            else None
        )
        # Pessoa de outra Empresa (ou reunião sem Empresa): desliga.
        if person is not None and person.company_id != meeting.company_id:
            participant.stakeholder_id = None
        # Sem Pessoa, com e-mail, e a Empresa nova tem alguém com esse e-mail: liga.
        if participant.stakeholder_id is None and participant.email and people is not None:
            participant.stakeholder_id = people.by_email.get(participant.email)
    # Envia ao banco.
    session.flush()


# Muda uma transcrição de Empresa e refaz tudo o que dependia da Empresa antiga.
def move_item(session: Session, item: BronzeItem, company: Company, actor: str) -> int:
    """Devolve quantas tags a transcrição ganhou na Empresa nova.

    Camadas refeitas:
    - tags (as da Empresa antiga saem, inclusive as colocadas à mão; as regras dos
      projetos da Empresa nova rodam de novo);
    - classificação em projeto e fatos extraídos (eram de projetos da Empresa antiga;
      são apagados para a extração rodar de novo com os projetos certos);
    - falas: quem falou é reconhecido de novo entre as Pessoas da Empresa nova;
    - reunião ao vivo e participantes: mudam junto, e a Pessoa de cada participante é
      procurada de novo pelo e-mail.
    O texto original (bronze) não muda: só a Empresa dele.
    """
    # Já está nesta Empresa: nada a fazer.
    if item.company_id == company.id:
        return 0
    # E-mail e Drive são da Empresa da conta que coletou: mudar um deles misturaria a
    # caixa de uma Empresa com a outra.
    if item.source != SourceKind.MEET_TRANSCRIPT:
        raise ValueError("Só transcrições de reunião mudam de Empresa.")
    # A mesma transcrição já existe na Empresa nova (importada lá também).
    duplicate = session.scalar(
        select(BronzeItem.id).where(
            BronzeItem.company_id == company.id,
            BronzeItem.source == item.source,
            BronzeItem.external_id == item.external_id,
        )
    )
    # Existe: corrigir criaria duas iguais.
    if duplicate is not None:
        raise ValueError(f"Essa transcrição já existe em {company.name} (item {duplicate}).")
    # Nome da Empresa antiga, para o diário.
    old = session.get(Company, item.company_id)
    # Tags (todas eram da Empresa antiga).
    session.execute(delete(TaggedItem).where(TaggedItem.bronze_item_id == item.id))
    # Classificação em projeto.
    session.execute(delete(ItemClassification).where(ItemClassification.bronze_item_id == item.id))
    # Fatos extraídos desta transcrição.
    session.execute(delete(Fact).where(Fact.source_item_id == item.id))
    # A Empresa nova.
    item.company_id = company.id
    # Reunião ao vivo que gerou esta transcrição, se houver.
    meeting = session.scalars(
        select(LiveMeeting).where(LiveMeeting.bronze_item_id == item.id)
    ).first()
    # Existe: muda junto, com os participantes.
    if meeting is not None:
        # Mesma Empresa.
        meeting.company_id = company.id
        # Pessoas dos participantes.
        relink_participants(session, meeting)
    # Pessoas da Empresa nova.
    people = PeopleIndex(session, company.id)
    # Convidados da agenda guardados na transcrição.
    attendees = attendees_of(item)
    # Cada fala.
    for utterance in session.scalars(select(Utterance).where(Utterance.bronze_item_id == item.id)):
        # Fala de um participante: a Pessoa dele (já acertada acima).
        if utterance.participant_id is not None:
            # O participante.
            participant = session.get(MeetingParticipant, utterance.participant_id)
            # Pessoa do participante, se houver.
            utterance.stakeholder_id = participant.stakeholder_id if participant else None
        # Senão: reconhece pelo nome entre as Pessoas da Empresa nova.
        else:
            utterance.stakeholder_id = people.match(utterance.speaker, attendees)
    # Envia ao banco antes de rodar as regras (elas leem as falas).
    session.flush()
    # Regras dos projetos da Empresa nova.
    added = apply_rules(session, item)
    # Diário (aparece no filtro das duas Empresas: uma linha em cada).
    for company_id in (old.id if old else None, company.id):
        # Uma linha por Empresa envolvida.
        audit(
            session,
            actor,
            "empresa.corrigir",
            target=item.subject,
            company_id=company_id,
            bronze_id=item.id,
            de=old.name if old else None,
            para=company.name,
        )
    # Devolve.
    return added


# --- Empresa das reuniões ao vivo ----------------------------------------------


# Descobre a Empresa de uma reunião pela agenda: o endereço do Meet está no convite.
def detect_company(session: Session, meeting: LiveMeeting, events_for) -> Company | None:
    """events_for(id da Empresa) devolve os eventos de hoje da agenda dessa Empresa (ou
    levanta erro). A Empresa só é escolhida quando exatamente uma agenda tem a reunião:
    se as duas têm (um convite para as duas contas), quem decide é você."""
    # Imports locais: o módulo da reunião ao vivo importa este (evita import circular).
    from app.transcription.live import matching_event, meet_code

    # Sem endereço do Meet: não há o que procurar.
    if meet_code(meeting.meeting_url) is None:
        return None
    # Empresas cuja agenda tem a reunião.
    found: list[Company] = []
    # Cada Empresa com conta conectada.
    for company_id in accounts_by_company(session):
        # A agenda pode falhar (rede, permissão): essa Empresa só não conta.
        try:
            # Eventos de hoje.
            events = events_for(company_id)
        # Qualquer erro.
        except Exception:
            # Próxima Empresa.
            continue
        # O endereço do Meet está num evento desta agenda.
        if matching_event(meeting, events) is not None:
            # Guarda.
            found.append(session.get(Company, company_id))
    # Uma só: é ela.
    return found[0] if len(found) == 1 else None


# Define (ou corrige) a Empresa de uma reunião ao vivo.
def set_meeting_company(
    session: Session, meeting: LiveMeeting, company: Company, actor: str
) -> None:
    """Durante a reunião: só muda a Empresa e acerta as Pessoas dos participantes.
    Encerrada e pendente: salva no bronze agora. Já salva: corrige a Empresa da
    transcrição em todas as camadas (move_item)."""
    # Import local (o módulo store importa este indiretamente).
    from app.transcription import store

    # Já é esta: nada a fazer.
    if meeting.company_id == company.id:
        return
    # Já salva no bronze: a correção completa cuida da reunião também.
    if meeting.bronze_item_id is not None:
        # A transcrição salva.
        item = session.get(BronzeItem, meeting.bronze_item_id)
        # Corrige tudo.
        move_item(session, item, company, actor)
        # Pronto.
        return
    # A Empresa nova.
    meeting.company_id = company.id
    # Pessoas dos participantes, na Empresa nova.
    relink_participants(session, meeting)
    # Diário.
    audit(
        session,
        actor,
        "reuniao.empresa",
        target=meeting.title or meeting.session_id,
        company_id=company.id,
    )
    # Encerrada sem Empresa: agora dá para salvar.
    if meeting.finished_at is not None:
        # Salva no bronze (o commit é feito lá).
        store.finalize(session, meeting.session_id, actor=actor, title=None, meeting_url=None)
