"""Importa a transcrição feita pelo Teams ou pelo Meet e liga à reunião da agenda.

Por que existe: a extensão de transcrição custa por minuto de áudio (Deepgram). Quando o
Teams ou o Meet já transcreveram a reunião, não faz sentido pagar de novo: basta trazer o
arquivo que eles geram.

Como funciona:
1. o arquivo é lido conforme o formato. Legendas (.vtt e .srt, o que o Teams baixa) viram
   uma linha por fala, no mesmo formato da transcrição da extensão:
   "[mm:ss] Nome: texto". Word, PDF e texto (.docx, .pdf, .txt, o que o Meet gera) são
   lidos pelo mesmo extrator dos anexos de e-mail;
2. o texto vira um item do bronze, fonte "transcricao_reuniao" (a mesma da extensão),
   ligado ao evento da agenda pelo id;
3. o arquivo original fica guardado, como os anexos.

A mesma transcrição importada duas vezes não duplica: o id do item é a impressão digital
do texto.
"""

# html.unescape troca "&amp;" por "&" (as legendas usam códigos do HTML).
import html

# re: expressões regulares (padrões de texto).
import re

# dataclass cria classes simples de dados.
from dataclasses import dataclass, field

# Data, data e hora e hora zero.
from datetime import date, datetime, time

# Path pega a extensão do nome do arquivo.
from pathlib import Path

# Fusos horários pelo nome.
from zoneinfo import ZoneInfo

# Consultas.
from sqlalchemy import select

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Reunião de cada transcrição (tabela meetings).
from app import meetings

# Leitura dos formatos que já existem para anexos.
from app.ingestion.extractors import decode_text, extract_text, split_error

# Guarda dos originais e impressão digital.
from app.ingestion.storage import FileStore, safe_suffix, sha256_hex

# Tabelas e o registro de auditoria.
from app.models import BronzeItem, Company, SourceKind, audit

# Gravação das falas, com o reconhecimento de quem falou.
from app.people import Spoken, save_utterances

# Tags automáticas pelas regras dos projetos.
from app.project_tags import apply_rules

# Evento da agenda e transcrição do Meet anexada.
from app.transcription.calendar import GOOGLE_DOC_MIME, CalendarEvent, MeetTranscript

# Formatos aceitos. Legendas: .vtt (Teams, Zoom) e .srt. Documento: .docx (Teams e Meet),
# .pdf e .txt.
CAPTION_SUFFIXES = {".vtt", ".srt"}
# Formatos lidos pelo extrator dos anexos.
DOCUMENT_SUFFIXES = {".docx", ".pdf", ".txt"}
# Todos os aceitos (a página usa para o filtro do seletor de arquivo).
ACCEPTED_SUFFIXES = CAPTION_SUFFIXES | DOCUMENT_SUFFIXES
# Tamanho máximo: uma reunião de 3 horas em .vtt tem uns 500 KB; 10 MB sobra.
MAX_FILE_BYTES = 10 * 1024 * 1024

# Linha de tempo de uma legenda: "00:01:02.345 --> 00:01:05.000" (o .srt usa vírgula).
_CUE_TIME = re.compile(r"^((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->")
# Marcação de quem fala no .vtt do Teams: "<v Ana Souza>texto</v>".
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>")
# Qualquer outra marcação (<i>, </v>, <c.cor>...).
_TAG = re.compile(r"<[^>]+>")
# Espaços repetidos.
_SPACES = re.compile(r"\s+")
# Cabeçalho de fala do .docx do Teams: "Ana Souza   0:03" (nome, espaços, tempo).
_TEAMS_HEADER = re.compile(r"^(\D[^\n]{0,80}?)\s+((?:\d{1,2}:)?\d{1,2}:\d{2})$")
# Linha de tempo sozinha do documento do Meet: "00:05:00".
_MEET_TIME = re.compile(r"^((?:\d{1,2}:)?\d{1,2}:\d{2})$")
# Fala do documento do Meet (e de muitos .txt): "Ana Souza: texto".
_NAME_COLON = re.compile(r"^([^:\d\n][^:\n]{0,59}):\s+(\S.*)$")


# Erro de importação, com a mensagem que aparece para você.
class ImportFailure(ValueError):
    # Nada a acrescentar: o tipo próprio só serve para separar este erro dos outros.
    pass


# Uma fala: momento, quem falou e o texto.
@dataclass
class Turn:
    # Milissegundos desde o começo da gravação.
    start_ms: int
    # Quem falou (None quando o arquivo não diz).
    speaker: str | None
    # O texto.
    text: str


# A transcrição lida, pronta para gravar.
@dataclass
class Transcript:
    # Texto final.
    text: str
    # Formato de origem ("legenda" ou "documento").
    kind: str
    # Quem falou, na ordem em que apareceu (vazio quando o arquivo não diz).
    speakers: list[str] = field(default_factory=list)
    # Número de falas (None quando o arquivo não separa quem falou).
    turns: int | None = None
    # As falas, quando o arquivo separa quem falou (vão para a tabela utterances).
    items: list[Turn] = field(default_factory=list)


# O resultado da importação, para a mensagem da página.
@dataclass(frozen=True)
class ImportResult:
    # Id do item no bronze.
    item_id: int
    # False quando a mesma transcrição já tinha sido importada.
    created: bool
    # Título da reunião.
    title: str
    # Transcrição lida.
    transcript: Transcript


# Lê falas de um documento do Teams (.docx): cabeçalho "Nome  0:03" e o texto abaixo.
def _teams_document(lines: list[str]) -> list[Turn]:
    # Resultado.
    turns: list[Turn] = []
    # Cada linha.
    for line in lines:
        # Cabeçalho de fala?
        header = _TEAMS_HEADER.match(line)
        # Sim: começa uma fala nova, ainda sem texto.
        if header:
            turns.append(Turn(_to_ms(header.group(2) + ".0"), header.group(1).strip(), ""))
        # Texto de uma fala já começada.
        elif turns:
            # Junta (uma fala pode ter vários parágrafos).
            turns[-1].text = f"{turns[-1].text} {line}".strip()
    # Só falas com texto.
    return [t for t in turns if t.text]


# Lê falas no formato "Nome: texto" (documento do Meet, .txt), com linhas de tempo soltas.
def _name_colon_document(lines: list[str]) -> list[Turn]:
    # Resultado.
    turns: list[Turn] = []
    # Último tempo visto (o Meet escreve "00:05:00" a cada poucos minutos).
    current_ms = 0
    # Linhas que não são tempo nem fala (título, lista de participantes...).
    other = 0
    # Cada linha.
    for line in lines:
        # Linha de tempo.
        time_line = _MEET_TIME.match(line)
        # Atualiza o tempo e segue.
        if time_line:
            current_ms = _to_ms(time_line.group(1) + ".0")
            continue
        # Fala "Nome: texto"; nomes com mais de 6 palavras são frase, não nome.
        said = _NAME_COLON.match(line)
        # É fala.
        if said and len(said.group(1).split()) <= 6:
            turns.append(Turn(current_ms, said.group(1).strip(), said.group(2).strip()))
        # Outra linha.
        else:
            other += 1
    # Aceita só se as falas forem a maioria das linhas (senão é um documento comum com
    # algumas linhas "Observação: ...").
    return turns if len(turns) >= 2 and len(turns) >= other else []


# Tenta separar as falas de um documento; lista vazia quando o formato não é reconhecido.
def parse_document_turns(text: str) -> list[Turn]:
    # Linhas com texto, sem espaços nas pontas.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Formato do Teams: pelo menos dois cabeçalhos "Nome  tempo".
    teams = _teams_document(lines)
    # Reconheceu.
    if len(teams) >= 2:
        return teams
    # Formato "Nome: texto".
    return _name_colon_document(lines)


# Converte "01:02:03.450", "02:03.450" ou "00:02:03,450" em milissegundos.
def _to_ms(stamp: str) -> int:
    # Troca a vírgula do .srt por ponto e separa horas, minutos e segundos.
    parts = stamp.replace(",", ".").split(":")
    # Sem horas ("02:03.450"): acrescenta zero horas na frente.
    if len(parts) == 2:
        parts.insert(0, "0")
    # Horas, minutos e segundos (com fração).
    hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    # Total em milissegundos, arredondado.
    return round(((hours * 60 + minutes) * 60 + seconds) * 1000)


# Mostra milissegundos como "mm:ss" (ou "h:mm:ss" a partir de uma hora).
def _clock(ms: int) -> str:
    # Segundos inteiros.
    seconds = ms // 1000
    # Horas, minutos e segundos; divmod devolve o quociente e o resto.
    hours, rest = divmod(seconds, 3600)
    # Minutos e segundos do resto.
    minutes, secs = divmod(rest, 60)
    # Com horas.
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    # Sem horas.
    return f"{minutes:02d}:{secs:02d}"


# Lê um arquivo de legendas (.vtt ou .srt) e devolve as falas.
def parse_captions(content: str) -> list[Turn]:
    """Falas em ordem; trechos seguidos da mesma pessoa viram uma fala só."""
    # Padroniza as quebras de linha (Windows usa \r\n).
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # Resultado.
    turns: list[Turn] = []
    # Cada bloco de legenda é separado por uma linha em branco.
    for block in re.split(r"\n\s*\n", content):
        # Linhas não vazias do bloco.
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        # Posição da linha de tempo ("-->"); o cabeçalho WEBVTT e as notas não têm.
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        # Bloco sem tempo: pula.
        if timing is None:
            continue
        # Lê o começo.
        match = _CUE_TIME.match(lines[timing])
        # Linha de tempo malformada: pula.
        if match is None:
            continue
        # O texto é tudo o que vem depois da linha de tempo.
        body = " ".join(lines[timing + 1 :])
        # Quem fala, se o arquivo marcar.
        voice = _VOICE.search(body)
        # Nome sem espaços nas pontas.
        speaker = voice.group(1).strip() if voice else None
        # Texto sem marcações, sem códigos do HTML e sem espaços repetidos.
        text = _SPACES.sub(" ", html.unescape(_TAG.sub("", body))).strip()
        # Legenda vazia: pula.
        if not text:
            continue
        # O Teams corta uma fala longa em vários blocos: junta com a anterior se for a
        # mesma pessoa.
        if turns and speaker is not None and turns[-1].speaker == speaker:
            # Acrescenta ao texto da fala anterior.
            turns[-1].text += f" {text}"
            # Próximo bloco.
            continue
        # Fala nova.
        turns.append(Turn(_to_ms(match.group(1)), speaker, text))
    # Devolve.
    return turns


# Monta o texto final das legendas, no mesmo formato da transcrição da extensão.
def render_turns(turns: list[Turn]) -> str:
    # Uma linha por fala.
    return "\n".join(
        # Com o nome de quem falou, quando houver.
        f"[{_clock(t.start_ms)}] {t.speaker}: {t.text}"
        if t.speaker
        # Sem nome.
        else f"[{_clock(t.start_ms)}] {t.text}"
        # Cada fala.
        for t in turns
    )


# Lê o arquivo da transcrição, conforme o formato.
def read_transcript(data: bytes, filename: str) -> Transcript:
    """O texto da transcrição. Levanta ImportFailure com o motivo, em português."""
    # Extensão do arquivo, em minúsculas.
    suffix = Path(filename).suffix.lower()
    # Formato não aceito.
    if suffix not in ACCEPTED_SUFFIXES:
        # Lista os aceitos na mensagem.
        accepted = ", ".join(sorted(ACCEPTED_SUFFIXES))
        # Recusa.
        raise ImportFailure(f"Formato {suffix or 'sem extensão'} não aceito. Use {accepted}.")
    # Arquivo vazio.
    if not data:
        raise ImportFailure("O arquivo está vazio.")
    # Arquivo grande demais.
    if len(data) > MAX_FILE_BYTES:
        raise ImportFailure("Arquivo acima de 10 MB; uma transcrição costuma ter menos de 1 MB.")
    # Texto do arquivo, quando ele é texto (legendas e .txt).
    content = decode_text(data) if suffix in CAPTION_SUFFIXES | {".txt"} else ""
    # Legendas, ou um .txt que na verdade é .vtt (começa com "WEBVTT").
    if suffix in CAPTION_SUFFIXES or content.lstrip("﻿").startswith("WEBVTT"):
        # Falas.
        turns = parse_captions(content)
        # Nenhuma fala: o arquivo não é uma legenda.
        if not turns:
            raise ImportFailure("Nenhuma fala encontrada no arquivo de legendas.")
        # Nomes sem repetição, na ordem em que aparecem (dict mantém a ordem).
        speakers = list(dict.fromkeys(t.speaker for t in turns if t.speaker))
        # Pronto.
        return Transcript(render_turns(turns), "legenda", speakers, len(turns), turns)
    # Documento: o mesmo extrator dos anexos de e-mail.
    extraction = extract_text(data, filename, None)
    # Falhou (ex.: PDF digitalizado, Word corrompido).
    if extraction.error:
        # Só a explicação, sem o código entre colchetes.
        raise ImportFailure(f"Não consegui ler o arquivo: {split_error(extraction.error)[1]}")
    # Texto limpo nas pontas.
    text = (extraction.text or "").strip()
    # Documento sem texto.
    if not text:
        raise ImportFailure("O arquivo não tem texto.")
    # Tenta separar quem falou (formatos do Teams e do Meet).
    turns = parse_document_turns(text)
    # Não reconheceu o formato: guarda o texto como veio.
    if not turns:
        return Transcript(text, "documento")
    # Nomes sem repetição, na ordem em que aparecem.
    speakers = list(dict.fromkeys(t.speaker for t in turns if t.speaker))
    # Mesmo formato das legendas e da extensão.
    return Transcript(render_turns(turns), "documento", speakers, len(turns), turns)


# Baixa a transcrição do Meet anexada ao evento (um Google Docs, exportado como Word).
def fetch_meet_transcript(drive, transcript: MeetTranscript) -> tuple[bytes, str]:
    """(conteúdo, nome do arquivo). Levanta ImportFailure se o Drive recusar."""
    # Import local: o módulo do Drive só carrega quando há transcrição do Meet.
    from app.ingestion.drive import download

    # Dados mínimos que o download precisa.
    meta = {"id": transcript.file_id, "name": transcript.title, "mimeType": GOOGLE_DOC_MIME}
    # Exporta.
    found = download(drive, meta, MAX_FILE_BYTES)
    # Falhou (sem acesso, apagado...).
    if found.data is None:
        # Mensagem do Drive, sem o código.
        raise ImportFailure(
            f"Não consegui baixar a transcrição do Meet: {split_error(found.error)[1]}"
        )
    # Conteúdo e nome (já com .docx).
    return found.data, found.filename


# Grava a transcrição no bronze, ligada ao evento (ou a uma reunião fora da agenda).
def import_transcript(
    # Sessão do banco.
    session: Session,
    # Pasta dos originais.
    store: FileStore,
    # Daqui em diante, argumentos só por nome (evita trocar um pelo outro).
    *,
    # Seu login (vai para a auditoria).
    actor: str,
    # Conteúdo do arquivo.
    data: bytes,
    # Nome do arquivo.
    filename: str,
    # Reunião da agenda, ou None.
    event: CalendarEvent | None,
    # Título digitado, para reunião fora da agenda.
    title: str | None,
    # Dia escolhido na página (usado quando não há evento).
    day: date,
    # Fuso do console.
    timezone: str,
    # Empresa da reunião (combo obrigatório da página).
    company_id: int,
    # "teams" ou "meet", quando se sabe pela origem do arquivo.
    platform: str | None = None,
    # Horário de início digitado, para reunião fora da agenda (vazio = só o dia).
    start_time: time | None = None,
) -> ImportResult:
    """Cria o item do bronze na Empresa. Levanta ImportFailure com o motivo."""
    # Sem reunião escolhida e sem título: não dá para saber de que reunião é.
    if event is None and not (title or "").strip():
        raise ImportFailure("Escolha a reunião na lista ou digite o título da reunião.")
    # Lê o arquivo.
    transcript = read_transcript(data, filename)
    # Impressão digital do texto: identifica a transcrição.
    digest = sha256_hex(transcript.text.encode())
    # Id no bronze.
    external_id = f"importada:{digest}"
    # Já importada antes (em qualquer Empresa)?
    existing = session.scalars(
        select(BronzeItem).where(
            BronzeItem.source == SourceKind.MEET_TRANSCRIPT, BronzeItem.external_id == external_id
        )
    ).all()
    # Na mesma Empresa: não duplica.
    for item in existing:
        # Mesma Empresa.
        if item.company_id == company_id:
            return ImportResult(item.id, False, item.subject or "", transcript)
    # Em outra Empresa: recusa. A mesma conversa em duas Empresas misturaria os clientes;
    # se a Empresa de lá estava errada, corrija na própria transcrição.
    if existing:
        # Nome da outra Empresa.
        other = session.get(Company, existing[0].company_id)
        # Motivo.
        raise ImportFailure(
            f"Essa transcrição já foi importada na Empresa {other.name if other else '?'} "
            f"(reunião “{existing[0].subject or ''}”). Se a Empresa de lá está errada, "
            "abra a transcrição e corrija a Empresa nela."
        )
    # Título: o do evento, ou o digitado.
    subject = event.title if event else title.strip()[:500]
    # Quando: início do evento, ou o dia escolhido com o horário digitado (sem horário,
    # meia-noite), no fuso do console.
    when = (
        event.start
        if event
        else datetime.combine(day, start_time or time.min, tzinfo=ZoneInfo(timezone))
    )
    # Plataforma: a da origem do arquivo, ou a da agenda.
    platform = platform or (event.platform if event else None)
    # A reunião: a do evento nesta Empresa (achada ou criada), ou uma nova fora da agenda.
    if event is not None:
        reuniao = meetings.for_event(session, company_id, event, platform)
    # Fora da agenda: uma reunião por transcrição (não há como saber que duas são a mesma).
    else:
        reuniao = meetings.standalone(
            session,
            company_id,
            title=subject,
            starts_at=when,
            time_known=start_time is not None,
            platform=platform,
        )
    # Guarda o original.
    key = store.save(data, safe_suffix(filename))
    # Item novo.
    item = BronzeItem(
        # Empresa da reunião.
        company_id=company_id,
        # A mesma fonte da transcrição feita pela extensão.
        source=SourceKind.MEET_TRANSCRIPT,
        # Id pela impressão digital.
        external_id=external_id,
        # Reunião.
        meeting_id=reuniao.id,
        # Liga ao evento; a transcrição da extensão da mesma reunião tem o mesmo valor
        # (a classificação por projeto trata as duas como a mesma conversa).
        thread_id=f"agenda:{event.id}" if event else None,
        # Quando aconteceu.
        occurred_at=when,
        # Organizador da reunião.
        author=event.organizer if event else None,
        # Título.
        subject=subject,
        # Texto.
        body_text=transcript.text,
        # Impressão digital.
        content_sha256=digest,
        # Detalhes.
        extra={
            # Diferencia da transcrição feita pela extensão.
            "origem": "importada",
            # Teams, Meet ou desconhecida.
            "plataforma": platform,
            # Arquivo original guardado.
            "arquivo": key,
            # Nome do arquivo enviado.
            "nome_arquivo": filename[:300],
            # Legenda ou documento.
            "formato": transcript.kind,
            # Quem falou.
            "falantes": transcript.speakers,
            # Quantas falas.
            "falas": transcript.turns,
            # Dados do evento da agenda (None se a reunião não está na agenda).
            "evento_agenda": event.to_extra() if event else None,
        },
    )
    # Prepara a gravação.
    session.add(item)
    # Envia ao banco para obter o id.
    session.flush()
    # Uma linha por fala, ligada à pessoa cadastrada quando o nome é reconhecido.
    save_utterances(
        # Sessão.
        session,
        # A transcrição.
        item,
        # Falas no formato da tabela.
        [Spoken(t.start_ms, t.speaker, t.text) for t in transcript.items],
    )
    # Tags pelas regras (nome do projeto no título, pessoas da equipe na reunião).
    apply_rules(session, item)
    # Registra no diário (aparece em Processamento).
    audit(
        # Sessão.
        session,
        # Quem importou.
        actor,
        # Nome da ação.
        "transcricao_importada",
        # Reunião.
        target=subject,
        # Item gravado.
        bronze_id=item.id,
        # Evento da agenda.
        evento=event.id if event else None,
        # Arquivo enviado.
        arquivo=filename[:300],
        # Empresa (o diário de Processamento filtra por ela).
        company_id=company_id,
    )
    # Confirma.
    session.commit()
    # Resultado.
    return ImportResult(item.id, True, subject, transcript)
