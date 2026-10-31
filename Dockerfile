# Receita da imagem Docker do console. Uma "imagem" é um pacote com o sistema, o Python,
# as bibliotecas e o nosso código. O servidor roda exatamente este pacote, e o CI
# constrói o mesmo pacote a cada push, então o que passa no CI sobe igual no servidor.

# Parte de uma imagem oficial com Python 3.12; "slim" é a versão enxuta (menor e com
# menos coisa para ter falha de segurança).
FROM python:3.12-slim

# Copia o executável do uv (gerenciador de pacotes) de uma imagem oficial, versão fixa.
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
# UV_COMPILE_BYTECODE: pré-compila o Python (o app sobe mais rápido).
# UV_LINK_MODE=copy: copia arquivos em vez de criar atalhos (necessário dentro do Docker).
# PYTHONUNBUFFERED: os logs aparecem na hora em `docker compose logs`.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Pasta de trabalho dentro da imagem.
WORKDIR /srv
# Primeiro só a lista de dependências: o Docker guarda esta etapa em cache e não
# reinstala tudo quando só o nosso código muda.
COPY pyproject.toml uv.lock README.md ./
# Instala as bibliotecas nas versões exatas do uv.lock, sem as de desenvolvimento.
RUN uv sync --frozen --no-dev --no-install-project

# Agora copia o código (o .dockerignore exclui .env, .git e outros).
COPY . .
# Instala o próprio projeto.
RUN uv sync --frozen --no-dev

# htmx servido pelo próprio app (versão fixa), sem depender de CDN no navegador.
ADD https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js app/static/htmx.min.js

# Cria um usuário comum: o app não roda como administrador (root), o que limita o
# estrago se um dia houver uma falha. chmod deixa o htmx legível por esse usuário.
# /dados é a pasta dos arquivos originais (e-mails e anexos); ela pertence a esse usuário
# e, ao montar o volume pela primeira vez, o Docker copia essa permissão para ele.
RUN useradd --create-home assistente && chmod a+r app/static/htmx.min.js \
    && mkdir -p /dados && chown assistente:assistente /dados
# Daqui em diante tudo roda como esse usuário.
USER assistente
# Coloca as ferramentas instaladas pelo uv (uvicorn, alembic) no caminho de comandos.
ENV PATH="/srv/.venv/bin:$PATH"

# Documenta a porta em que o app escuta dentro do contêiner.
EXPOSE 8000
# Comando que sobe o console: uvicorn é o servidor web que roda o FastAPI.
# --proxy-headers: confia nos cabeçalhos do tailscale serve (endereço e HTTPS originais).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
