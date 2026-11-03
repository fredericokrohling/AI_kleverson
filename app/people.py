"""Pessoas nas reuniões: grava cada fala e reconhece quem falou entre as pessoas cadastradas.

Por que existe: as regras de alerta que a dona vai criar dependem de saber *quem* pediu ou
decidiu algo, e se essa pessoa é liderança no projeto ou na empresa. O nome na
transcrição ("Ana Souza (Convidado)", "ana.souza@cliente.com", "Ana") precisa virar a
pessoa cadastrada.

Como reconhece, nesta ordem (só aceita quando a resposta é uma pessoa só):
1. o nome é um e-mail de pessoa cadastrada;
2. o nome bate com o nome ou com um "outro nome" (apelido) de uma pessoa cadastrada,
   sem diferenciar maiúsculas e acentos;
3. o nome bate com um participante do convite da agenda, e o e-mail desse participante é
   de uma pessoa cadastrada.

Nome que bate com duas pessoas fica sem ligação: errar quem falou é pior que não saber.

Só valem as pessoas da Empresa da transcrição: a "Ana" de um cliente nunca é reconhecida
numa reunião de outro (pessoas não são compartilhadas entre Empresas).

Na transcrição ao vivo, o serviço só separa vozes ("Falante 2"). Quem diz quem é cada voz
é você, no Início (ver app/transcription/live.py): a fala aponta para o participante da
reunião e, se ele tiver o e-mail de uma pessoa cadastrada, para a pessoa também.
"""

# re: expressões regulares (padrões de texto).
import re

# unicodedata separa as letras dos acentos (para comparar "João" com "Joao").
import unicodedata

# Iterable: qualquer coisa que dá para percorrer com "for".
from collections.abc import Iterable

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Consultas.
from sqlalchemy import delete, select, update

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Tabelas.
from app.models import (
    BronzeItem,
    LiveMeeting,
    MeetingAttendee,
    MeetingParticipant,
    SourceKind,
    Stakeholder,
    Utterance,
)

# Marcas que o Teams e o Meet põem depois do nome ("Ana Souza (Convidado)").
_SUFFIX = re.compile(r"\s*\((convidad[oa]|guest|external|externo|externa|unverified)\)\s*$", re.I)
# Linha de transcrição no formato do sistema: "[mm:ss] Nome: texto" ou "[h:mm:ss] ...".
_LINE = re.compile(r"^\[(?:(\d+):)?(\d{1,3}):(\d{2})\]\s+(?:([^:\n]{1,200}):\s+)?(.*)$")


# Uma fala, antes de ir para o banco.
@dataclass
class Spoken:
    # Momento, em ms desde o começo (None quando não se sabe).
    start_ms: int | None
    # Nome como apareceu (None quando o arquivo não diz).
    speaker: str | None
    # Texto.
    text: str
    # Participante da reunião ao vivo, quando você disse quem é aquela voz.
    participant_id: int | None = None
    # Pessoa cadastrada já conhecida (a do participante); vazio = reconhecer pelo nome.
    stakeholder_id: int | None = None


# Forma comparável de um nome: sem acento, sem marca de convidado, minúsculo, um espaço.
def normalize(name: str | None) -> str:
    # Sem nome.
    if not name:
        return ""
    # Tira "(Convidado)" e parecidos.
    name = _SUFFIX.sub("", name)
    # Separa letra e acento (NFKD) e joga fora os acentos (categoria "Mn").
    decomposed = unicodedata.normalize("NFKD", name)
    # Mantém só o que não é acento.
    plain = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    # Minúsculas e espaços simples.
    return " ".join(plain.lower().split())


# Índice das pessoas cadastradas de uma Empresa, para reconhecer nomes rápido.
class PeopleIndex:
    """Monta, uma vez, os dicionários nome → pessoas e e-mail → pessoa (de uma Empresa)."""

    # Lê as pessoas da Empresa no banco.
    def __init__(self, session: Session, company_id: int) -> None:
        # Nome normalizado → ids das pessoas com esse nome (pode haver mais de uma).
        self.by_name: dict[str, set[int]] = {}
        # E-mail em minúsculas → id da pessoa.
        self.by_email: dict[str, int] = {}
        # Cada pessoa cadastrada nesta Empresa (as das outras não contam).
        for person in session.scalars(
            select(Stakeholder).where(Stakeholder.company_id == company_id)
        ):
            # Nome e outros nomes.
            for name in [person.name, *(person.aliases or [])]:
                # Forma comparável.
                key = normalize(name)
                # Nome vazio não conta.
                if key:
                    # Acrescenta a pessoa ao conjunto desse nome.
                    self.by_name.setdefault(key, set()).add(person.id)
            # E-mail, se houver.
            if person.email:
                # Guarda em minúsculas.
                self.by_email[person.email.lower()] = person.id

    # Pessoa de um nome, ou None.
    def match(self, speaker: str | None, attendees: dict[str, str] | None = None) -> int | None:
        """attendees: nome normalizado do participante do convite → e-mail."""
        # Sem nome.
        if not speaker:
            return None
        # 1. O "nome" é um e-mail.
        email = speaker.strip().lower()
        # Parece e-mail e é de alguém cadastrado.
        if "@" in email and email in self.by_email:
            return self.by_email[email]
        # 2. Nome ou outro nome cadastrado.
        key = normalize(speaker)
        # Pessoas com esse nome.
        found = self.by_name.get(key, set())
        # Uma só: é ela.
        if len(found) == 1:
            return next(iter(found))
        # Duas ou mais: ambíguo, não liga.
        if len(found) > 1:
            return None
        # 3. Participante do convite com esse nome, e o e-mail dele é de alguém cadastrado.
        email = (attendees or {}).get(key)
        # Achou pelo e-mail do convite.
        if email:
            return self.by_email.get(email.lower())
        # Não reconheceu.
        return None


# Participantes da reunião da transcrição: nome normalizado → e-mail.
def attendees_of(session: Session, item: BronzeItem) -> dict[str, str]:
    """Vem da tabela de participantes da reunião (convidados da agenda e quem você
    cadastrou ao vivo). Transcrição sem reunião: nenhum."""
    # Sem reunião.
    if item.meeting_id is None:
        return {}
    # Pares (nome comparável, e-mail) de quem tem e-mail.
    rows = session.execute(
        select(MeetingAttendee.name_key, MeetingAttendee.email).where(
            MeetingAttendee.meeting_id == item.meeting_id, MeetingAttendee.email.is_not(None)
        )
    ).all()
    # Dicionário.
    return {key: email for key, email in rows}


# Grava as falas de uma transcrição, trocando as que já existiam. Devolve quantas.
def save_utterances(session: Session, item: BronzeItem, spoken: Iterable[Spoken]) -> int:
    # Apaga as falas antigas desta transcrição (refazer não duplica).
    session.execute(delete(Utterance).where(Utterance.bronze_item_id == item.id))
    # Pessoas cadastradas na Empresa da transcrição.
    people = PeopleIndex(session, item.company_id)
    # Participantes do convite.
    attendees = attendees_of(session, item)
    # Contador.
    count = 0
    # Cada fala.
    for position, s in enumerate(spoken):
        # Linha nova.
        session.add(
            Utterance(
                # Transcrição.
                bronze_item_id=item.id,
                # Ordem.
                position=position,
                # Momento.
                start_ms=s.start_ms,
                # Nome como apareceu (cortado ao tamanho da coluna).
                speaker=s.speaker[:200] if s.speaker else None,
                # Pessoa: a do participante, ou a reconhecida pelo nome.
                stakeholder_id=s.stakeholder_id or people.match(s.speaker, attendees),
                # Participante da reunião ao vivo.
                participant_id=s.participant_id,
                # Texto.
                text=s.text,
            )
        )
        # Conta.
        count += 1
    # Devolve.
    return count


# Lê as falas do texto de uma transcrição no formato do sistema ("[mm:ss] Nome: texto").
def parse_lines(text: str) -> list[Spoken]:
    """Serve para as transcrições gravadas antes desta versão. Linha fora do formato
    é juntada à fala anterior (texto que quebrou linha)."""
    # Resultado.
    spoken: list[Spoken] = []
    # Cada linha.
    for line in text.splitlines():
        # Tenta o formato.
        match = _LINE.match(line.strip())
        # Fora do formato.
        if match is None:
            # Junta à fala anterior, se houver e se a linha tiver texto.
            if spoken and line.strip():
                spoken[-1].text += f" {line.strip()}"
            # Próxima linha.
            continue
        # Partes: horas (opcional), minutos, segundos, nome (opcional), texto.
        hours, minutes, seconds, speaker, said = match.groups()
        # Momento em ms.
        start = ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000
        # Guarda.
        spoken.append(Spoken(start, speaker.strip() if speaker else None, said.strip()))
    # Devolve.
    return spoken


# Refaz as falas das transcrições que ainda não têm (as gravadas antes desta versão).
def backfill_utterances(session: Session) -> int:
    """Devolve quantas transcrições ganharam falas."""
    # Transcrições sem nenhuma fala gravada.
    items = session.scalars(
        # Itens de transcrição...
        select(BronzeItem)
        # ... da fonte de reuniões...
        .where(BronzeItem.source == SourceKind.MEET_TRANSCRIPT)
        # ... sem falas.
        .where(~select(Utterance.id).where(Utterance.bronze_item_id == BronzeItem.id).exists())
    ).all()
    # Contador.
    done = 0
    # Cada transcrição.
    for item in items:
        # Falas lidas do texto.
        spoken = parse_lines(item.body_text)
        # Texto fora do formato (ex.: .docx importado como veio): não há o que gravar.
        if spoken:
            # Grava.
            save_utterances(session, item, spoken)
            # Conta.
            done += 1
    # Devolve.
    return done


# Índices de pessoas por Empresa, montados só quando a Empresa aparece.
class _Indexes(dict):
    # Guarda a sessão para montar os índices.
    def __init__(self, session: Session) -> None:
        # Dicionário vazio.
        super().__init__()
        # Sessão.
        self.session = session

    # Chamado pelo dict quando a Empresa ainda não tem índice.
    def __missing__(self, company_id: int) -> PeopleIndex:
        # Monta e guarda.
        self[company_id] = PeopleIndex(self.session, company_id)
        # Devolve.
        return self[company_id]


# Liga à Pessoa os participantes de reunião sem ligação que têm o e-mail dela.
def link_participants_by_email(session: Session, company_id: int | None = None) -> int:
    """Devolve quantos participantes foram ligados. As falas deles seguem junto.

    Só liga dentro da Empresa da reunião; reunião sem Empresa fica sem ligação."""
    # Pessoas por e-mail, por Empresa.
    indexes = _Indexes(session)
    # Contador.
    linked = 0
    # Participantes com e-mail e sem pessoa, de reuniões que já têm Empresa.
    query = (
        select(MeetingParticipant, LiveMeeting.company_id)
        .join(LiveMeeting, LiveMeeting.id == MeetingParticipant.meeting_id)
        .where(
            MeetingParticipant.stakeholder_id.is_(None),
            MeetingParticipant.email.is_not(None),
            LiveMeeting.company_id.is_not(None),
        )
    )
    # Só uma Empresa, se pedido.
    if company_id is not None:
        query = query.where(LiveMeeting.company_id == company_id)
    # Cada participante.
    for participant, meeting_company in session.execute(query).all():
        # Pessoa com esse e-mail na Empresa da reunião.
        person = indexes[meeting_company].by_email.get(participant.email)
        # Achou.
        if person is not None:
            # Liga o participante.
            participant.stakeholder_id = person
            # E todas as falas dele.
            session.execute(
                update(Utterance)
                .where(Utterance.participant_id == participant.id)
                .values(stakeholder_id=person)
            )
            # Conta.
            linked += 1
    # Devolve.
    return linked


# Reconhece de novo as falas sem pessoa (depois de cadastrar alguém ou um apelido).
def relink_utterances(session: Session, company_id: int | None = None) -> int:
    """Devolve quantas falas passaram a ter pessoa. company_id: só uma Empresa."""
    # Primeiro os participantes de reuniões ao vivo com o e-mail de alguém cadastrado.
    link_participants_by_email(session, company_id)
    # E os participantes das reuniões (convidados da agenda), pelo mesmo e-mail. Import
    # local: o módulo das reuniões importa este.
    from app.meetings import link_attendees_by_email

    # Liga.
    link_attendees_by_email(session, company_id)
    # Pessoas cadastradas, por Empresa.
    indexes = _Indexes(session)
    # Falas sem pessoa, com o item (para os participantes do convite).
    query = (
        # Fala e transcrição...
        select(Utterance, BronzeItem)
        # ... ligadas pelo id...
        .join(BronzeItem, BronzeItem.id == Utterance.bronze_item_id)
        # ... só as que têm nome e ainda não têm pessoa. As de um participante sem e-mail
        # cadastrado ficam como estão: quem é aquela voz, você já disse.
        .where(
            Utterance.stakeholder_id.is_(None),
            Utterance.speaker.is_not(None),
            Utterance.participant_id.is_(None),
        )
    )
    # Só uma Empresa, se pedido.
    if company_id is not None:
        query = query.where(BronzeItem.company_id == company_id)
    # Executa.
    rows = session.execute(query).all()
    # Participantes por item (calcula uma vez por transcrição).
    cache: dict[int, dict[str, str]] = {}
    # Contador.
    linked = 0
    # Cada fala.
    for utterance, item in rows:
        # Participantes do convite desta transcrição.
        attendees = cache.setdefault(item.id, attendees_of(session, item))
        # Tenta reconhecer entre as pessoas da Empresa da transcrição.
        person = indexes[item.company_id].match(utterance.speaker, attendees)
        # Reconheceu.
        if person is not None:
            # Liga.
            utterance.stakeholder_id = person
            # Conta.
            linked += 1
    # Devolve.
    return linked
