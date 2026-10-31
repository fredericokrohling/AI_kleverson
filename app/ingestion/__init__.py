"""Ingestão: leva o conteúdo das fontes (hoje, o Gmail) para a camada bronze.

Ordem de leitura sugerida:
1. storage.py     onde os arquivos originais ficam guardados;
2. extractors.py  como cada tipo de arquivo (PDF, DOCX, XLSX, PPTX) vira texto;
3. email_parser.py como um e-mail bruto vira corpo, cabeçalhos e anexos;
4. drive.py       como os links do Google Drive citados no e-mail são baixados;
5. gmail.py       o coletor, que junta tudo e grava no banco.

O porquê de cada escolha está em docs/aprendizado/06-ingestao-email.md.
"""
