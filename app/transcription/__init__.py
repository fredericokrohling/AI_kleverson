"""Transcrição de reuniões: ao vivo (bloco 1) e importada do Teams e do Meet.

Um arquivo __init__.py transforma a pasta num "pacote" Python: é o que permite
escrever `from app.transcription.store import finalize`. Este não tem código, só
esta explicação.

Como as peças se encaixam:
- extensao/ (Chrome) captura o áudio e manda para o servidor por WebSocket;
- routes.py recebe o áudio e repassa ao serviço de transcrição;
- providers.py conversa com o serviço (hoje, o Deepgram) e devolve o texto;
- store.py guarda os trechos e, no fim, monta a transcrição completa no bronze;
- calendar.py lê as reuniões da agenda do Google;
- importer.py lê o arquivo do Teams ou do Meet e grava no bronze, ligado à reunião;
- routes_import.py é a página "Importar transcrição".

O porquê de cada escolha está em docs/aprendizado/05-transcricao.md (ao vivo) e
docs/aprendizado/09-importar-transcricoes.md (importação).
"""
