"""Leitura da agenda do Google para ligar uma transcrição importada à reunião certa.

Por que ler a agenda: uma transcrição solta ("arquivo.vtt") não diz de que reunião é.
Ligada ao evento da agenda, ela ganha título, horário, participantes e o id do evento,
que é o mesmo para a transcrição importada e para a feita pela extensão. É assim que a
IA do bloco 3 vai juntar tudo o que se falou numa reunião.

Só leitura: o módulo lista eventos e lê um evento. Nada é criado nem alterado na agenda.
"""

# dataclass cria classes simples de dados.
from dataclasses import dataclass, field

# Data, data e hora, hora zero e intervalo de tempo.
from datetime import date, datetime, time, timedelta

# Fusos horários pelo nome (ex.: America/Sao_Paulo).
from zoneinfo import ZoneInfo

# Erro devolvido pelas APIs do Google.
from googleapiclient.errors import HttpError

# Session: conversa com o banco (para ler as credenciais).
from sqlalchemy.orm import Session

# Configuração.
from app.config import Settings

# Tipo MIME de um Google Docs (a transcrição do Meet é salva assim no Drive).
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


# Um arquivo anexado ao evento que parece ser a transcrição do Meet.
@dataclass(frozen=True)
class MeetTranscript:
    # Id do arquivo no Drive.
    file_id: str
    # Título do arquivo (ex.: "Status semanal - Transcrição").
    title: str


# Uma reunião da agenda, só com o que a importação usa.
@dataclass(frozen=True)
class CalendarEvent:
    # Id do evento na agenda (numa reunião recorrente, o id da ocorrência daquele dia).
    id: str
    # Título.
    title: str
    # Início, com fuso.
    start: datetime
    # Fim, com fuso.
    end: datetime
    # E-mail de quem organizou.
    organizer: str | None = None
    # Participantes (nome ou e-mail).
    attendees: list[str] = field(default_factory=list)
    # "meet", "teams" ou None quando não dá para saber.
    platform: str | None = None
    # Endereço da reunião on-line, quando houver.
    meeting_url: str | None = None
    # Transcrições do Meet anexadas ao evento.
    transcripts: list[MeetTranscript] = field(default_factory=list)

    # Dados para guardar no "extra" do item do bronze.
    def to_extra(self) -> dict:
        # Um dicionário simples, que vira JSON no banco.
        return {
            # Id do evento.
            "id": self.id,
            # Título.
            "titulo": self.title,
            # Início em texto ISO 8601 (o JSON não tem tipo de data).
            "inicio": self.start.isoformat(),
            # Fim, no mesmo formato.
            "fim": self.end.isoformat(),
            # Organizador.
            "organizador": self.organizer,
            # Participantes.
            "participantes": self.attendees,
            # Endereço da reunião.
            "url_reuniao": self.meeting_url,
        }


# Cliente da API da Agenda com as credenciais gravadas (erro se o Google não estiver conectado).
def calendar_client(session: Session, settings: Settings):
    # Imports locais: só carregam quando a agenda é usada.
    from googleapiclient.discovery import build

    # Leitura das credenciais.
    from app.google_auth import load_credentials

    # Criptografia dos tokens.
    from app.security import get_cipher

    # Credenciais (renova se tiverem expirado).
    creds = load_credentials(session, get_cipher(), settings)
    # Cliente da Agenda, versão 3.
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# Lê o horário de um evento ("dateTime" com hora; "date" só em evento de dia inteiro).
def _when(value: dict) -> datetime | None:
    # Evento com hora.
    if value.get("dateTime"):
        # A API devolve no formato ISO 8601, com fuso (ex.: 2026-11-02T14:00:00-03:00).
        return datetime.fromisoformat(value["dateTime"])
    # Dia inteiro: não é uma reunião com horário.
    return None


# Descobre a plataforma e o endereço da reunião.
def _platform(raw: dict) -> tuple[str | None, str | None]:
    # Evento criado com Meet: a API já traz o link.
    if raw.get("hangoutLink"):
        return "meet", raw["hangoutLink"]
    # Pontos de entrada de outras soluções de conferência (o Teams aparece aqui quando o
    # convite vem do Outlook com o complemento instalado).
    for entry in (raw.get("conferenceData") or {}).get("entryPoints", []):
        # Endereço.
        uri = entry.get("uri") or ""
        # Link do Teams.
        if "teams.microsoft.com" in uri or "teams.live.com" in uri:
            return "teams", uri
    # Convite do Teams recebido por e-mail: o link fica na descrição ou no local.
    text = f"{raw.get('location') or ''} {raw.get('description') or ''}"
    # Procura o link do Teams no texto.
    if "teams.microsoft.com" in text or "teams.live.com" in text:
        return "teams", None
    # Não deu para saber.
    return None, None


# Transcrições do Meet anexadas ao evento.
def _transcripts(raw: dict) -> list[MeetTranscript]:
    # Resultado.
    found: list[MeetTranscript] = []
    # Cada anexo do evento.
    for item in raw.get("attachments", []):
        # Título do anexo.
        title = item.get("title") or ""
        # O Meet salva a transcrição como Google Docs com "Transcript"/"Transcrição" no
        # nome; as anotações do Gemini também são Docs, mas sem essa palavra.
        if item.get("mimeType") == GOOGLE_DOC_MIME and "transcri" in title.lower():
            # Guarda o id do arquivo no Drive.
            if item.get("fileId"):
                found.append(MeetTranscript(item["fileId"], title))
    # Devolve.
    return found


# Converte o evento da API no formato usado aqui; None para evento sem horário.
def parse_event(raw: dict) -> CalendarEvent | None:
    # Início e fim.
    start, end = _when(raw.get("start", {})), _when(raw.get("end", {}))
    # Evento de dia inteiro (feriado, férias): fica fora da lista.
    if start is None or end is None:
        return None
    # Plataforma e endereço.
    platform, url = _platform(raw)
    # Participantes: o nome, se a agenda souber; senão o e-mail. Salas de reunião saem.
    attendees = [
        # Nome mostrado pela agenda, ou o e-mail.
        a.get("displayName") or a.get("email", "")
        # Cada convidado.
        for a in raw.get("attendees", [])
        # Salas e equipamentos ("resource") não são pessoas.
        if not a.get("resource")
    ]
    # Monta.
    return CalendarEvent(
        # Id do evento.
        id=raw["id"],
        # Título, ou um texto padrão.
        title=raw.get("summary") or "(sem título)",
        # Início.
        start=start,
        # Fim.
        end=end,
        # E-mail do organizador.
        organizer=(raw.get("organizer") or {}).get("email"),
        # Participantes sem nomes vazios.
        attendees=[a for a in attendees if a],
        # Plataforma.
        platform=platform,
        # Endereço.
        meeting_url=url,
        # Transcrições do Meet anexadas.
        transcripts=_transcripts(raw),
    )


# Reuniões de um dia, no fuso do console, em ordem de horário.
def events_on(calendar, day: date, timezone: str) -> list[CalendarEvent]:
    """Eventos com horário do dia. Levanta HttpError se a API recusar."""
    # Fuso do console.
    zone = ZoneInfo(timezone)
    # Meia-noite do dia, no fuso.
    start = datetime.combine(day, time.min, tzinfo=zone)
    # Meia-noite do dia seguinte.
    end = start + timedelta(days=1)
    # Pede a lista.
    raw = (
        # Recurso de eventos.
        calendar.events()
        # Monta a listagem.
        .list(
            # Agenda principal da conta.
            calendarId="primary",
            # Do começo do dia...
            timeMin=start.isoformat(),
            # ... ao começo do dia seguinte.
            timeMax=end.isoformat(),
            # Reuniões recorrentes viram ocorrências separadas (cada uma com seu id).
            singleEvents=True,
            # Pelo horário de início.
            orderBy="startTime",
            # Um dia cabe com folga.
            maxResults=100,
        )
        # Executa, repetindo até 2 vezes em falha passageira de rede.
        .execute(num_retries=2)
        # A lista de eventos; [] se não houver nenhum.
        .get("items", [])
    )
    # Converte, deixando de fora os eventos de dia inteiro e os cancelados.
    events = [parse_event(e) for e in raw if e.get("status") != "cancelled"]
    # Só os que têm horário.
    return [e for e in events if e is not None]


# Um evento pelo id (a página manda só o id; os dados vêm da agenda, não do formulário).
def get_event(calendar, event_id: str) -> CalendarEvent | None:
    """O evento, ou None se não existir. Outros erros da API sobem."""
    # Tenta ler.
    try:
        # Lê o evento.
        raw = calendar.events().get(calendarId="primary", eventId=event_id).execute(num_retries=2)
    # Erro da API.
    except HttpError as exc:
        # 404/410: o evento não existe (apagado, ou id inválido).
        if exc.resp.status in (404, 410):
            return None
        # Outros erros sobem para quem chamou mostrar.
        raise
    # Converte.
    return parse_event(raw)
