"""Transcrição em tempo real de reuniões (bloco 1: teste de viabilidade).

Um arquivo __init__.py transforma a pasta num "pacote" Python: é o que permite
escrever `from app.transcription.store import finalize`. Este não tem código, só
esta explicação.

Como as peças se encaixam:
- extensao/ (Chrome) captura o áudio e manda para o servidor por WebSocket;
- routes.py recebe o áudio e repassa ao serviço de transcrição;
- providers.py conversa com o serviço (hoje, o Deepgram) e devolve o texto;
- store.py guarda os trechos e, no fim, monta a transcrição completa no bronze.

O porquê de cada escolha está em docs/aprendizado/05-transcricao.md.
"""
