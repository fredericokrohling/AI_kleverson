"""Serviços de transcrição em streaming.

Cada serviço recebe áudio PCM 16 bits, mono, 16 kHz, e devolve trechos de texto
enquanto a pessoa fala. Trocar de serviço é trocar a classe aqui.

Por que "streaming": em vez de mandar um arquivo de áudio inteiro depois da reunião,
mandamos pedacinhos de áudio (de ~100 ms) enquanto ela acontece e recebemos o texto
em seguida. É isso que permite ver a transcrição ao vivo.

Por que o Deepgram: veja docs/aprendizado/05-transcricao.md (comparação com Whisper,
Google, AssemblyAI e outros).
"""

# json converte texto JSON em dicionário Python e vice-versa.
import json

# AsyncIterator: tipo de algo que entrega valores aos poucos, de forma assíncrona.
from collections.abc import AsyncIterator

# dataclass cria classes simples de dados.
from dataclasses import dataclass

# Protocol descreve um "contrato": quais métodos uma classe precisa ter.
from typing import Protocol

# urlencode monta a parte "?a=1&b=2" de um endereço a partir de um dicionário.
from urllib.parse import urlencode

# Cliente WebSocket: conexão aberta nos dois sentidos, ideal para áudio contínuo.
from websockets.asyncio.client import ClientConnection, connect

# Erro levantado quando a conexão WebSocket fecha.
from websockets.exceptions import ConnectionClosed

# Tipo da configuração (chave e modelo do Deepgram).
from app.config import Settings

# Amostras de áudio por segundo. 16 kHz é o padrão para voz: capta a fala com
# qualidade e usa 1/3 dos dados do áudio de música (48 kHz).
SAMPLE_RATE = 16000


# Um trecho de texto devolvido pelo serviço. frozen=True: não pode ser alterado.
@dataclass(frozen=True)
class Segment:
    # O texto reconhecido.
    text: str
    # False = "rascunho" que ainda pode mudar; True = versão definitiva do trecho.
    is_final: bool
    # Número do falante dado pela separação de vozes (0, 1, 2...), quando houver.
    speaker: int | None
    # Início do trecho em milissegundos desde o começo da captura.
    start_ms: int | None
    # Fim do trecho, na mesma contagem.
    end_ms: int | None


# Contrato que qualquer serviço de transcrição precisa cumprir. Com ele, routes.py
# funciona igual com Deepgram ou com outro serviço que venha a substituí-lo.
class StreamingProvider(Protocol):
    # Nome curto do serviço (gravado em cada trecho).
    name: str

    # "async with provider:" abre a conexão...
    async def __aenter__(self) -> "StreamingProvider": ...
    # ...e fecha no fim, mesmo com erro.
    async def __aexit__(self, *exc) -> None: ...
    # Envia um pedaço de áudio.
    async def send_audio(self, chunk: bytes) -> None: ...
    # Avisa que o áudio acabou.
    async def finish(self) -> None: ...
    # Entrega os trechos de texto conforme chegam.
    def results(self) -> AsyncIterator[Segment]: ...


# Implementação do contrato para o Deepgram.
class DeepgramStream:
    """Streaming do Deepgram: https://developers.deepgram.com/docs/live-streaming-audio"""

    # Nome gravado nos trechos.
    name = "deepgram"
    # Endereço do serviço de streaming. "wss" = WebSocket criptografado.
    _URL = "wss://api.deepgram.com/v1/listen"

    # Recebe chave, modelo e idioma (vindos do .env).
    def __init__(self, api_key: str, model: str, language: str) -> None:
        # Sem chave não há como conectar; avisa com mensagem clara.
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY não configurada.")
        # Guarda a chave para usar na conexão.
        self._api_key = api_key
        # Opções do Deepgram, enviadas no endereço da conexão.
        self._query = urlencode(
            {
                # Modelo de reconhecimento (nova-3).
                "model": model,
                # Idioma (pt-BR).
                "language": language,
                # Formato do áudio: PCM linear de 16 bits (o que a extensão produz).
                "encoding": "linear16",
                # Amostras por segundo.
                "sample_rate": SAMPLE_RATE,
                # Mono: um canal por conexão (você e os outros vão em conexões separadas).
                "channels": 1,
                # Diarização: separa as vozes e numera os falantes.
                "diarize": "true",
                # Devolve rascunhos enquanto a frase ainda está sendo dita.
                "interim_results": "true",
                # Formata números, datas e valores ("dez reais" vira "R$ 10").
                "smart_format": "true",
                # Coloca pontuação.
                "punctuate": "true",
            }
        )
        # A conexão começa vazia; é aberta em __aenter__.
        self._ws: ClientConnection | None = None

    # Abre a conexão quando se entra no "async with".
    async def __aenter__(self) -> "DeepgramStream":
        # await espera a conexão ficar pronta sem travar o resto do servidor.
        self._ws = await connect(
            # Endereço com as opções.
            f"{self._URL}?{self._query}",
            # A chave vai no cabeçalho de autorização, não no endereço (não aparece em logs).
            additional_headers={"Authorization": f"Token {self._api_key}"},
        )
        # Devolve o próprio objeto para o "as" do "async with".
        return self

    # Fecha a conexão ao sair do "async with".
    async def __aexit__(self, *exc) -> None:
        # Só fecha se chegou a abrir.
        if self._ws is not None:
            # Fecha a conexão.
            await self._ws.close()

    # Repassa um pedaço de áudio (bytes) ao Deepgram.
    async def send_audio(self, chunk: bytes) -> None:
        # Envia os bytes pela conexão.
        await self._ws.send(chunk)

    # Avisa o Deepgram que o áudio terminou.
    async def finish(self) -> None:
        # Pede ao Deepgram para devolver o que falta e encerrar.
        try:
            # Mensagem de controle em JSON definida pela API do Deepgram.
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        # Se a conexão já fechou, não há nada a fazer.
        except ConnectionClosed:
            # Ignora de propósito.
            pass

    # Gerador assíncrono: entrega um trecho por vez, conforme as mensagens chegam.
    async def results(self) -> AsyncIterator[Segment]:
        # Protege contra o fechamento da conexão no meio da leitura.
        try:
            # Cada mensagem recebida do Deepgram.
            async for raw in self._ws:
                # Converte a mensagem num Segment (ou None, se não for texto).
                segment = parse_deepgram_message(raw)
                # Só entrega mensagens com texto.
                if segment is not None:
                    # yield entrega o trecho e continua escutando.
                    yield segment
        # Conexão fechou: termina a entrega normalmente.
        except ConnectionClosed:
            # Sai do gerador.
            return


# Função separada (fora da classe) para poder ser testada sem conexão real.
def parse_deepgram_message(raw: str | bytes) -> Segment | None:
    # Converte o JSON recebido em dicionário.
    data = json.loads(raw)
    # O Deepgram manda outros tipos de mensagem (metadados etc.); só "Results" tem texto.
    if data.get("type") != "Results":
        # Não é texto: ignora.
        return None
    # A melhor hipótese de transcrição vem em primeiro lugar na lista.
    alternative = data["channel"]["alternatives"][0]
    # O texto, sem espaços nas pontas.
    text = alternative.get("transcript", "").strip()
    # Silêncio vem como texto vazio: ignora.
    if not text:
        # Nada a entregar.
        return None
    # Lista de palavras com detalhes (inclusive o falante de cada uma).
    words = alternative.get("words") or []
    # Usa o falante da primeira palavra como falante do trecho.
    speaker = words[0].get("speaker") if words else None
    # Início do trecho em segundos.
    start = data.get("start")
    # Duração do trecho em segundos.
    duration = data.get("duration")
    # Monta o Segment convertendo segundos em milissegundos.
    return Segment(
        # Texto.
        text=text,
        # Rascunho ou definitivo.
        is_final=bool(data.get("is_final")),
        # Falante.
        speaker=speaker,
        # Início em ms (round arredonda para inteiro).
        start_ms=round(start * 1000) if start is not None else None,
        # Fim em ms = início + duração.
        end_ms=round((start + duration) * 1000) if start is not None and duration else None,
    )


# "Fábrica": devolve o serviço configurado no .env. Novos serviços entram aqui.
def create_provider(settings: Settings) -> StreamingProvider:
    # Deepgram configurado?
    if settings.transcription_provider == "deepgram":
        # Cria o cliente com chave, modelo e idioma.
        return DeepgramStream(
            settings.deepgram_api_key, settings.deepgram_model, settings.transcription_language
        )
    # Nome desconhecido no .env: erro claro em vez de falha silenciosa.
    raise RuntimeError(f"Serviço de transcrição desconhecido: {settings.transcription_provider}")
