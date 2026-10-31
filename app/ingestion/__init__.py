"""Ingestão: leva o conteúdo das fontes (Gmail e Drive) para a camada bronze.

Ordem de leitura sugerida:
1. storage.py      onde os arquivos originais ficam guardados;
2. extractors.py   como cada tipo de arquivo (PDF, DOCX, XLSX, PPTX) vira texto;
3. email_parser.py como um e-mail bruto vira corpo, cabeçalhos e anexos;
4. drive.py        como os arquivos do Google Drive são baixados;
5. gmail.py        o coletor, que junta tudo e grava no banco;
6. drive_sync.py   a varredura das pastas monitoradas e dos links do Drive;
7. vision.py       a leitura de imagens e PDFs digitalizados pelo Claude;
8. vision_queue.py a fila dessa leitura, que o coletor roda a cada 5 minutos;
9. failures.py     a lista de arquivos sem texto e o "Tentar de novo".

O porquê de cada escolha está em docs/aprendizado/06-ingestao-email.md,
07-console-drive-saude.md e 08-leitura-pelo-claude.md.
"""
