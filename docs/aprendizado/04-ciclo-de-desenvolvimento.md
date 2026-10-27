# 4. Ciclo de desenvolvimento: Git, PR, testes e CI

## O caminho de uma mudança

```
editar código → rodar testes local → commit → push numa branch → PR no GitHub
→ CI roda sozinho → você revisa e faz merge → no servidor: git pull + docker compose up
```

## Por que branches e Pull Requests (PR)

- **Branch** é uma linha paralela de trabalho. Cada bloco do plano tem a sua
  (`bloco-0-fundacao`, `bloco-1-transcricao`). A `main` só recebe o que está pronto.
- **PR** é o pedido para juntar a branch na `main`. Ele mostra exatamente o que mudou,
  linha a linha, e é onde o CI dá o veredito.
- **Por quê:** você revisa antes de entrar. Se algo der errado, a `main` continua estável e
  dá para desfazer um PR inteiro.

**Ordem atual:** o PR #2 (transcrição) foi criado em cima do PR #1 (fundação). Faça o merge
do #1 primeiro; depois o #2 passa a apontar para a `main`.

## O que o CI verifica, e por quê

O arquivo `.github/workflows/ci.yml` roda a cada push. Se qualquer passo falhar, o PR fica
vermelho.

| Passo | O que confere | Que erro ele evita |
| --- | --- | --- |
| `ruff check` | Erros comuns e imports | Código que nem chegaria a rodar |
| `ruff format --check` | Formatação padronizada | Diferenças de espaço poluindo a revisão |
| `pytest` | Os testes automáticos, com Postgres de verdade | Regressões: algo que funcionava e quebrou |
| `alembic check` | Se `models.py` bate com as migrações | Esquecer de gerar a migração de uma tabela nova |
| `docker build` | Se a imagem do servidor constrói | Descobrir no servidor que o deploy não sobe |

## Rodar localmente no Linux Mint

```bash
# 1. Instalar o uv (uma vez)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Instalar as dependências do projeto
uv sync

# 3. Subir um Postgres de teste (uma vez; depois use "docker start pgdev")
docker run -d --name pgdev -e POSTGRES_USER=assistente -e POSTGRES_PASSWORD=teste \
  -e POSTGRES_DB=assistente -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16

# 4. Rodar os mesmos passos do CI
uv run ruff check . && uv run ruff format --check .
uv run pytest
uv run alembic check
```

`uv run` executa o comando dentro do ambiente do projeto, com as versões do `uv.lock`.

## Mudou uma tabela?

1. Edite `app/models.py`.
2. Gere a migração: `uv run alembic revision --autogenerate -m "o que mudou"`.
3. **Leia o arquivo gerado** em `migrations/versions/`. O Alembic às vezes erra (por
   exemplo, ao renomear coluna ele apaga e recria, perdendo os dados).
4. Rode `uv run pytest`: os testes aplicam a migração num banco limpo.

## Convenção de comentários

Todo código Python deste projeto tem um comentário acima de cada linha explicando o que
ela faz, e um texto no topo do arquivo explicando por que o arquivo existe. Ao alterar
código, mantenha os comentários atualizados: um comentário errado é pior que nenhum.
