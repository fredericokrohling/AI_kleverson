"""Guarda os arquivos originais (e-mails inteiros e anexos) no disco do servidor.

Por que fora do banco: um PDF de 10 MB dentro do Postgres deixa backups e consultas
pesados sem ganho nenhum. O banco guarda só o caminho (object_key) e o texto extraído.

Por que "endereçado pelo conteúdo": o nome de cada arquivo é a sua impressão digital
(SHA-256). Duas consequências úteis:
- o mesmo anexo encaminhado em dez e-mails ocupa espaço uma vez só;
- gravar de novo o mesmo arquivo não faz nada, então repetir uma coleta é seguro.

O caminho fica em duas pastas pelos primeiros caracteres (ex.: ab/cd/abcd...pdf) para
nenhuma pasta acumular centenas de milhares de arquivos.
"""

# hashlib calcula a impressão digital SHA-256.
import hashlib

# os.replace troca arquivos de forma atômica (ou o arquivo inteiro aparece, ou nada).
import os

# tempfile cria um arquivo temporário com nome único.
import tempfile

# Path manipula caminhos de arquivos.
from pathlib import Path

# Extensões aceitas no nome do arquivo guardado. Qualquer outra vira ".bin": o nome do
# arquivo vem do e-mail (de fora), então não deixamos texto livre entrar no caminho.
_SAFE_SUFFIX_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789")


# Calcula a impressão digital de um conteúdo.
def sha256_hex(data: bytes) -> str:
    # hexdigest devolve os 32 bytes do SHA-256 como 64 caracteres hexadecimais.
    return hashlib.sha256(data).hexdigest()


# Normaliza a extensão (".PDF" vira ".pdf"; algo estranho vira ".bin").
def safe_suffix(filename: str | None) -> str:
    # Path(...).suffix pega o trecho a partir do último ponto; lower() padroniza.
    suffix = Path(filename or "").suffix.lower()
    # Tira o ponto do começo para conferir só as letras.
    letters = suffix[1:]
    # Aceita só extensões curtas feitas de letras e números.
    if 0 < len(letters) <= 8 and set(letters) <= _SAFE_SUFFIX_CHARS:
        # Extensão aprovada, com o ponto.
        return suffix
    # Qualquer outra coisa vira ".bin" (binário genérico).
    return ".bin"


# A pasta de arquivos originais.
class FileStore:
    """Pasta de arquivos originais, endereçada pelo conteúdo."""

    # Recebe a pasta raiz (no Docker, o volume montado em /dados).
    def __init__(self, root: str | Path) -> None:
        # Guarda a raiz como Path; os arquivos de e-mail ficam na subpasta "originais".
        self.root = Path(root) / "originais"

    # Grava um conteúdo e devolve a chave (caminho relativo) que vai para o banco.
    def save(self, data: bytes, suffix: str = ".bin") -> str:
        # Impressão digital do conteúdo; vira o nome do arquivo.
        digest = sha256_hex(data)
        # Chave: duas pastas com os 4 primeiros caracteres, depois o nome completo.
        key = f"{digest[:2]}/{digest[2:4]}/{digest}{suffix}"
        # Caminho absoluto no disco.
        path = self.root / key
        # Já existe: mesmo conteúdo, nada a fazer.
        if path.exists():
            # Devolve a mesma chave.
            return key
        # Cria as pastas que faltarem; exist_ok evita erro se outra coleta criou antes.
        path.parent.mkdir(parents=True, exist_ok=True)
        # Escreve primeiro num temporário na mesma pasta. Se o processo cair no meio,
        # fica um temporário perdido, nunca um arquivo final pela metade.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        # try/except garante que o temporário some se a escrita falhar.
        try:
            # Abre o temporário pelo descritor e escreve os bytes.
            with os.fdopen(fd, "wb") as fh:
                # Grava o conteúdo.
                fh.write(data)
            # Renomeia para o nome final, de uma vez.
            os.replace(tmp, path)
        # BaseException pega tudo (até Ctrl+C) só para limpar e repassar o erro.
        except BaseException:
            # Apaga o temporário; missing_ok ignora se ele já não existir.
            Path(tmp).unlink(missing_ok=True)
            # Repassa o erro original para quem chamou.
            raise
        # Devolve a chave para gravar no banco.
        return key

    # Lê de volta um arquivo pela chave (usado para extrair o texto de novo).
    def read(self, key: str) -> bytes:
        # A chave vem do banco, mas conferimos mesmo assim: "../" não pode sair da pasta.
        path = (self.root / key).resolve()
        # is_relative_to confere que o caminho final continua dentro da raiz.
        if not path.is_relative_to(self.root.resolve()):
            # Chave suspeita: recusa.
            raise ValueError(f"Chave de arquivo inválida: {key}")
        # Devolve o conteúdo.
        return path.read_bytes()
