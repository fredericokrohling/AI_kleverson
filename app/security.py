"""Criptografia dos tokens do Google e verificação de quem acessa o console.

Por que criptografar os tokens: o refresh token do Google dá acesso ao seu Gmail
enquanto não for revogado. Se alguém copiar o banco (um backup vazado, por exemplo),
sem a chave de criptografia ele só vê bytes embaralhados. A chave fica no .env, fora
do banco.

Por que Fernet: é a receita pronta da biblioteca `cryptography` (AES + assinatura).
Ela criptografa e também detecta adulteração. Não inventamos criptografia própria,
que é o erro clássico de segurança.

Por que Tailscale para o acesso: o console não fica exposto na internet. Só aparelhos
da sua rede Tailscale chegam até ele, e o Tailscale informa quem é a pessoa.
"""

# Fernet faz a criptografia; InvalidToken é o erro quando a chave não confere.
from cryptography.fernet import Fernet, InvalidToken

# HTTPException devolve um erro HTTP (ex.: 403); Request é a requisição recebida.
from fastapi import HTTPException, Request

# Configuração (de onde vêm a chave e o login autorizado).
from app.config import get_settings

# O `tailscale serve` repassa o login de quem fez a requisição neste cabeçalho.
TAILSCALE_LOGIN_HEADER = "Tailscale-User-Login"


# Classe que guarda a chave e sabe criptografar e descriptografar textos.
class TokenCipher:
    # __init__ roda quando o objeto é criado: TokenCipher("chave...").
    def __init__(self, key: str) -> None:
        # Sem chave, para tudo com uma mensagem clara em vez de gravar token aberto.
        if not key:
            # raise interrompe a execução e sinaliza o erro para quem chamou.
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY não configurada. Gere com: python -m app.cli gerar-chave"
            )
        # Fernet trabalha com bytes; .encode() converte o texto da chave em bytes.
        # O "_" no começo do nome indica uso interno da classe.
        self._fernet = Fernet(key.encode())

    # Recebe um texto (o token) e devolve bytes criptografados para gravar no banco.
    def encrypt(self, plaintext: str) -> bytes:
        # Converte o texto em bytes e criptografa.
        return self._fernet.encrypt(plaintext.encode())

    # Faz o caminho inverso: bytes do banco viram o texto original.
    def decrypt(self, ciphertext: bytes) -> str:
        # try/except: tenta; se der o erro indicado, trata em vez de quebrar sem explicação.
        try:
            # Descriptografa e converte bytes de volta para texto com .decode().
            return self._fernet.decrypt(ciphertext).decode()
        # InvalidToken acontece quando a chave atual não é a que criptografou o dado.
        except InvalidToken as exc:
            # Troca o erro técnico por uma explicação útil; "from exc" mantém o original.
            raise RuntimeError(
                "Não foi possível descriptografar o token. A TOKEN_ENCRYPTION_KEY mudou?"
            ) from exc


# Gera uma chave nova e aleatória. Usada uma vez, na instalação (comando gerar-chave).
def generate_key() -> str:
    # generate_key devolve bytes; .decode() transforma em texto para colar no .env.
    return Fernet.generate_key().decode()


# Atalho para obter um TokenCipher já com a chave do .env.
def get_cipher() -> TokenCipher:
    # Lê a chave da configuração e cria o objeto.
    return TokenCipher(get_settings().token_encryption_key)


# Verificação comum a páginas (HTTP) e ao WebSocket da transcrição. Recebe só os
# cabeçalhos porque o WebSocket não tem um objeto Request igual ao das páginas.
def owner_login(headers) -> str | None:
    """O login Tailscale de quem fez a requisição, se for o dono; senão None.

    O app escuta apenas em 127.0.0.1 no servidor; o único caminho de fora é o
    `tailscale serve`, que identifica o usuário. Sem login configurado, nada passa.
    """
    # Login autorizado, sem espaços nas pontas (.strip) e em minúsculas (.lower).
    allowed = get_settings().allowed_tailscale_login.strip().lower()
    # Login informado pelo Tailscale; "" se o cabeçalho não veio.
    login = headers.get(TAILSCALE_LOGIN_HEADER, "").strip().lower()
    # Devolve o login só se houver um configurado (falha fechada) e for igual a ele.
    return login if allowed and login == allowed else None


# Dependência do FastAPI: as rotas que a usam só rodam depois desta verificação.
def require_owner(request: Request) -> str:
    """Dependência do FastAPI para páginas e chamadas HTTP: só o dono passa."""
    # Reaproveita a verificação comum.
    login = owner_login(request.headers)
    # None = não é o dono (ou nada foi configurado).
    if login is None:
        # 403 = "proibido". A rota nem chega a rodar.
        raise HTTPException(status_code=403, detail="Acesso negado")
    # Devolve o login para a rota usar (ex.: registrar no log de auditoria).
    return login
