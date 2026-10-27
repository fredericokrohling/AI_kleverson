FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /srv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# htmx servido pelo próprio app (versão fixa), sem depender de CDN no navegador.
ADD https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js app/static/htmx.min.js

RUN useradd --create-home assistente && chmod a+r app/static/htmx.min.js
USER assistente
ENV PATH="/srv/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
