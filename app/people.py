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
from app.models import BronzeItem, MeetingParticipant, SourceKind, Stakeholder, Utterance

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


# Índice das pessoas cadastradas para reconhecer nomes rápido.
class PeopleIndex:
    """Monta, uma vez, os dicionários nome → pessoas e e-mail → pessoa."""

    # Lê as pessoas do banco.
    def __init__(self, session: Session) -> None:
        # Nome normalizado → ids das pessoas com esse nome (pode haver mais de uma).
        self.by_name: dict[str, set[int]] = {}
        # E-mail em minúsculas → id da pessoa.
        self.by_email: dict[str, int] = {}
        # Cada pessoa cadastrada.
        for person in session.scalars(select(Stakeholder)):
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


# Participantes do convite guardados no item: nome normalizado → e-mail.
def attendees_of(item: BronzeItem) -> dict[str, str]:
    # Dados do evento da agenda, se a transcrição foi ligada a um.
    event = (item.extra or {}).get("evento_agenda") or {}
    # Pares (nome, e-mail) guardados na importação.
    pairs = event.get("participantes_emails") or {}
    # Chave comparável.
    return {normalize(name): email for name, email in pairs.items() if email}


# Grava as falas de uma transcrição, trocando as que já existiam. Devolve quantas.
def save_utterances(session: Session, item: BronzeItem, spoken: Iterable[Spoken]) -> int:
    # Apaga as falas antigas desta transcrição (refazer não duplica).
    session.execute(delete(Utterance).where(Utterance.bronze_item_id == item.id))
    # Pessoas cadastradas.
    people = PeopleIndex(session)
    # Participantes do convite.
    attendees = attendees_of(item)
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


# Liga à Pessoa os participantes de reunião sem ligação que têm o e-mail dela.
def link_participants_by_email(session: Session) -> int:
    """Devolve quantos participantes foram ligados. As falas deles seguem junto."""
    # Pessoas por e-mail.
    people = PeopleIndex(session)
    # Contador.
    linked = 0
    # Participantes com e-mail e sem pessoa.
    for participant in session.scalars(
        select(MeetingParticipant).where(
            MeetingParticipant.stakeholder_id.is_(None), MeetingParticipant.email.is_not(None)
        )
    ):
        # Pessoa com esse e-mail.
        person = people.by_email.get(participant.email)
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
def relink_utterances(session: Session) -> int:
    """Devolve quantas falas passaram a ter pessoa."""
    # Primeiro os participantes de reuniões ao vivo com o e-mail de alguém cadastrado.
    link_participants_by_email(session)
    # Pessoas cadastradas.
    people = PeopleIndex(session)
    # Falas sem pessoa, com o item (para os participantes do convite).
    rows = session.execute(
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
    ).all()
    # Participantes por item (calcula uma vez por transcrição).
    cache: dict[int, dict[str, str]] = {}
    # Contador.
    linked = 0
    # Cada fala.
    for utterance, item in rows:
        # Participantes do convite desta transcrição.
        attendees = cache.setdefault(item.id, attendees_of(item))
        # Tenta reconhecer.
        person = people.match(utterance.speaker, attendees)
        # Reconheceu.
        if person is not None:
            # Liga.
            utterance.stakeholder_id = person
            # Conta.
            linked += 1
    # Devolve.
    return linked
