"""Rotas da transcrição: áudio ao vivo (WebSocket), legendas do Meet e finalização.

Por que WebSocket para o áudio: uma requisição HTTP comum é "pergunta e resposta" e
fecha. O WebSocket fica aberto nos dois sentidos durante a reunião inteira: a extensão
manda áudio sem parar e o servidor devolve o texto assim que ele fica pronto.

O servidor funciona como intermediário (extensão → servidor → Deepgram) para que a
chave do Deepgram fique só no servidor, e nunca no navegador.
"""

# asyncio: execução assíncrona (várias tarefas esperando ao mesmo tempo sem travar).
import asyncio

# logging: mensagens no log do servidor.
import logging

# re: expressões regulares (padrões de texto), usadas para validar o id da sessão.
import re

# Path: caminhos de arquivos.
from pathlib import Path

# Annotated: tipo com informação extra (qual dependência resolver).
from typing import Annotated

# Peças do FastAPI: APIRouter agrupa rotas; WebSocket é a conexão contínua.
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket

# Resposta em HTML.
from fastapi.responses import HTMLResponse

# Modelos HTML.
from fastapi.templating import Jinja2Templates

# BaseModel valida o JSON recebido; Field define limites (tamanho máximo etc.).
from pydantic import BaseModel, Field

# Session: conversa com o banco.
from sqlalchemy.orm import Session

# Erro de desconexão e estados possíveis de um WebSocket.
from starlette.websockets import WebSocketDisconnect, WebSocketState

# Configuração.
from app.config import get_settings

# Sessão por requisição e fábrica de sessões.
from app.db import get_session, get_sessionmaker

# Verificação de acesso (versão para cabeçalhos e versão para rotas HTTP).
from app.security import owner_login, require_owner

# Gravação dos trechos e finalização.
from app.transcription import store

# Fábrica do serviço de transcrição configurado.
from app.transcription.providers import create_provider

# Logger deste módulo.
log = logging.getLogger(__name__)
# Grupo de rotas; o main.py acrescenta este grupo ao app.
router = APIRouter()
# Modelos HTML ficam em app/templates (duas pastas acima deste arquivo, mais "templates").
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

# Atalho: exige o login do dono (ver app/security.py).
Owner = Annotated[str, Depends(require_owner)]
# Atalho: sessão do banco por requisição.
DB = Annotated[Session, Depends(get_session)]

# Identificador da sessão de captura, gerado pela extensão (UUID).
# O padrão aceita só letras, números e hífen, com 8 a 64 caracteres.
_SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


# Recusa ids fora do padrão (proteção contra entradas malformadas).
def _check_session_id(session_id: str) -> None:
    # match confere se o texto inteiro segue o padrão.
    if not _SESSION_ID.match(session_id):
        # 400 = requisição inválida.
        raise HTTPException(status_code=400, detail="Identificador de sessão inválido")


# Rota WebSocket. A extensão abre uma conexão por canal: uma para o seu microfone
# ("voce") e outra para o áudio da aba do Meet ("outros").
@router.websocket("/ws/transcricao")
async def live_transcription(websocket: WebSocket, sessao: str, canal: str) -> None:
    """A extensão envia áudio PCM 16 kHz; devolvemos os trechos de texto ao vivo."""
    # Quem está conectando (None se não for o dono).
    actor = owner_login(websocket.headers)
    # Recusa se não for o dono, se o canal for desconhecido ou se o id for inválido.
    if actor is None or canal not in store.AUDIO_CHANNELS or not _SESSION_ID.match(sessao):
        # 1008 = código WebSocket para "violação de política".
        await websocket.close(code=1008)
        # Encerra a função.
        return
    # Aceita a conexão.
    await websocket.accept()

    # Fábrica de sessões do banco, para gravar os trechos.
    sessions = get_sessionmaker()
    # Tenta abrir a conexão com o serviço de transcrição.
    try:
        # Cria o cliente configurado (Deepgram).
        provider = create_provider(get_settings())
        # Abre a conexão com o serviço.
        stream = await provider.__aenter__()
    # Qualquer falha aqui é avisada à extensão.
    except Exception as exc:  # chave ausente, serviço fora do ar
        # Registra o erro completo no log do servidor.
        log.exception("Falha ao abrir o serviço de transcrição")
        # Mostra o motivo no painel da extensão.
        await websocket.send_json({"tipo": "erro", "mensagem": str(exc)})
        # 1011 = código WebSocket para "erro interno".
        await websocket.close(code=1011)
        # Encerra a função.
        return

    # Tarefa 1: da extensão para o serviço (sobe o áudio).
    async def upstream() -> None:
        # Protege contra desconexão da extensão.
        try:
            # Laço até a extensão desconectar.
            while True:
                # Espera a próxima mensagem da extensão.
                message = await websocket.receive()
                # Extensão fechou a conexão.
                if message["type"] == "websocket.disconnect":
                    # Sai do laço.
                    break
                # Mensagens com bytes são áudio.
                if message.get("bytes"):
                    # Repassa o áudio ao serviço.
                    await stream.send_audio(message["bytes"])
        # Desconexão abrupta também encerra o laço.
        except WebSocketDisconnect:
            # Nada a fazer; o finally cuida do resto.
            pass
        # Roda sempre, com ou sem erro.
        finally:
            # Avisa o serviço que o áudio acabou, para ele devolver o que falta.
            await stream.finish()

    # Tarefa 2: do serviço para a extensão e para o banco (desce o texto).
    async def downstream() -> None:
        # Cada trecho de texto que o serviço devolve.
        async for segment in stream.results():
            # Nome a exibir para o falante.
            speaker = store.speaker_label(canal, segment.speaker)
            # Só grava a versão definitiva; os rascunhos servem apenas para exibir.
            if segment.is_final:
                # O banco é síncrono; to_thread roda a gravação em paralelo sem travar.
                await asyncio.to_thread(
                    # Função que grava.
                    _save,
                    # Fábrica de sessões.
                    sessions,
                    # Dados do trecho.
                    session_id=sessao,
                    channel=canal,
                    provider=stream.name,
                    speaker=speaker,
                    text=segment.text,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                )
            # Se a extensão já desconectou, não tenta enviar.
            if websocket.client_state != WebSocketState.CONNECTED:
                # Pula para o próximo trecho (que ainda pode ser gravado).
                continue
            # Tenta mostrar o trecho no painel.
            try:
                # Envia o trecho como JSON.
                await websocket.send_json(
                    {
                        # Tipo de mensagem que o painel entende.
                        "tipo": "trecho",
                        # Canal.
                        "canal": canal,
                        # Falante.
                        "falante": speaker,
                        # Texto.
                        "texto": segment.text,
                        # Rascunho ou definitivo (o painel substitui o rascunho).
                        "final": segment.is_final,
                        # Momento.
                        "inicio_ms": segment.start_ms,
                    }
                )
            # A extensão fechou entre a verificação e o envio.
            except (WebSocketDisconnect, RuntimeError):
                # A extensão fechou; os trechos finais continuam sendo gravados.
                pass

    # Roda as duas tarefas ao mesmo tempo, até as duas terminarem.
    try:
        # gather espera ambas.
        await asyncio.gather(upstream(), downstream())
    # Limpeza, aconteça o que acontecer.
    finally:
        # Fecha a conexão com o serviço.
        await provider.__aexit__(None, None, None)
        # Fecha a conexão com a extensão, se ainda estiver aberta.
        if websocket.client_state == WebSocketState.CONNECTED:
            # Fecha.
            await websocket.close()


# Grava um trecho numa sessão própria (roda numa thread separada, ver to_thread acima).
def _save(sessions, **fields) -> None:
    # Abre e fecha uma sessão só para esta gravação.
    with sessions() as session:
        # Repassa todos os campos para a função de gravação.
        store.save_segment(session, **fields)


# Formato de uma linha de legenda no JSON enviado pela extensão. O pydantic recusa
# automaticamente o que não seguir este formato (com erro 422).
class CaptionIn(BaseModel):
    # Nome do falante, opcional, até 200 caracteres.
    falante: str | None = Field(default=None, max_length=200)
    # Texto, até 5000 caracteres.
    texto: str = Field(max_length=5000)
    # Momento em ms, opcional.
    inicio_ms: int | None = None


# Formato do lote de legendas: no máximo 500 linhas por envio.
class CaptionsIn(BaseModel):
    # Lista de linhas.
    trechos: list[CaptionIn] = Field(max_length=500)


# Recebe as legendas do Meet que a extensão leu da tela.
@router.post("/transcricoes/{session_id}/legendas")
def receive_captions(session_id: str, body: CaptionsIn, owner: Owner, db: DB) -> dict[str, int]:
    """Legendas do próprio Meet, lidas pela extensão, para comparar com o serviço."""
    # Valida o id.
    _check_session_id(session_id)
    # Converte do formato do JSON para o formato interno.
    lines = [store.CaptionLine(t.falante, t.texto, t.inicio_ms) for t in body.trechos]
    # Grava e devolve quantas linhas foram gravadas.
    return {"gravados": store.save_captions(db, session_id, lines)}


# Formato do pedido de finalização.
class FinalizeIn(BaseModel):
    # Título da reunião, opcional.
    titulo: str | None = Field(default=None, max_length=500)
    # Endereço do Meet, opcional.
    url_reuniao: str | None = Field(default=None, max_length=500)


# Botão "Finalizar" do painel: junta os trechos num item do bronze.
@router.post("/transcricoes/{session_id}/finalizar")
def finalize(session_id: str, body: FinalizeIn, owner: Owner, db: DB) -> dict[str, int]:
    # Valida o id.
    _check_session_id(session_id)
    # Monta (ou atualiza) o item do bronze.
    item = store.finalize(
        db, session_id, actor=owner, title=body.titulo, meeting_url=body.url_reuniao
    )
    # Devolve o id do item criado.
    return {"bronze_id": item.id}


# Página de comparação: transcrição do serviço ao lado das legendas do Meet.
@router.get("/transcricoes/{session_id}", response_class=HTMLResponse)
def compare(session_id: str, request: Request, owner: Owner, db: DB):
    """As duas versões lado a lado: serviço de transcrição e legendas do Meet."""
    # Valida o id.
    _check_session_id(session_id)
    # Busca os trechos separados por canal.
    audio, captions = store.segments_by_channel(db, session_id)
    # Preenche o modelo transcricao.html.
    return templates.TemplateResponse(
        # Requisição.
        request,
        # Modelo.
        "transcricao.html",
        # Dados para a página.
        {"owner": owner, "session_id": session_id, "audio": audio, "captions": captions},
    )
