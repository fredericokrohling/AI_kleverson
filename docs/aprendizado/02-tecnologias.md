# 2. Tecnologias: o que cada uma faz e por que foi escolhida

Critérios usados em todas as escolhas, nesta ordem:

1. **Seus dados não saem do seu controle.**
2. **Simples de operar sozinha**, sem equipe de infraestrutura.
3. **Boa para aprender**: tecnologia popular, com documentação e exemplos abundantes.
4. **Barata** no volume de uma pessoa.

## Linguagem e ferramentas de desenvolvimento

### Python 3.12
- **O que é:** a linguagem do projeto.
- **Por quê:** você quer aprender Python; é a linguagem dominante em IA e dados, e todas
  as bibliotecas de que precisamos (Google, Anthropic, banco) são oficiais em Python.

### uv
- **O que é:** gerenciador de pacotes e de ambientes Python (instala bibliotecas).
- **Por quê:** muito mais rápido que o `pip`, e grava as versões exatas em `uv.lock`. Assim o
  servidor, o CI e o seu computador usam exatamente as mesmas versões.
- **Alternativas:** `pip` + `venv` (mais lento, sem trava de versões nativa), Poetry (mais lento).
- **Onde:** `pyproject.toml` (o que o projeto usa) e `uv.lock` (versões exatas).

### ruff
- **O que é:** verificador de estilo e de erros comuns ("linter") e formatador de código.
- **Por quê:** pega erros antes de rodar (import não usado, variável inexistente) e deixa
  o código sempre com a mesma formatação, o que facilita ler as diferenças nos PRs.
- **Onde:** seção `[tool.ruff]` do `pyproject.toml`.

### pytest
- **O que é:** a ferramenta de testes automáticos.
- **Por quê:** é o padrão do mercado em Python. Os testes provam que o código faz o que
  deve e avisam quando uma mudança quebra algo.
- **Onde:** pasta `tests/`.

## Aplicação web

### FastAPI
- **O que é:** framework para criar o servidor web (as páginas do console e as rotas).
- **Por quê:** valida automaticamente o que chega pelos tipos declarados, tem suporte nativo
  a WebSocket (necessário para a transcrição em tempo real) e um sistema de
  "dependências" que usamos para exigir login em todas as páginas.
- **Alternativas:** Flask (mais antigo, sem WebSocket nativo), Django (completo demais para
  um usuário só).
- **Onde:** `app/main.py`.

### HTMX + Jinja2
- **O que é:** Jinja2 preenche modelos HTML com dados; HTMX é uma biblioteca pequena que
  deixa um botão atualizar só um pedaço da página.
- **Por quê:** o console precisa ser mínimo. Sem HTMX, teríamos que escrever um segundo
  aplicativo em JavaScript (React, Vue), com outra linguagem e outro processo de build.
- **Onde:** `app/templates/`.

### MCP (fase posterior, bloco 7)
- **O que é:** Model Context Protocol, um padrão aberto para o Claude (no app ou no
  Claude Desktop) chamar ferramentas externas.
- **Por quê:** é a interface de conversa. Em vez de construir um chat próprio, você conversa
  com o Claude que já usa, e ele consulta o seu banco pelo servidor MCP.

## Dados

### PostgreSQL 16
- **O que é:** banco de dados relacional.
- **Por quê:** robusto, gratuito e com tipos que usamos muito: `JSONB` (dados livres de
  cada fonte), `ARRAY` (listas de palavras-chave) e extensões como o pgvector.
- **Alternativas:** SQLite (simples, mas fraco com acessos simultâneos e sem pgvector),
  MySQL (sem JSONB e pgvector equivalentes).

### pgvector
- **O que é:** extensão do Postgres para guardar "embeddings" (números que representam o
  significado de um texto) e buscar por semelhança.
- **Por quê:** permite perguntar "o que já foi dito sobre atraso de fornecedor?" sem as
  palavras exatas. Fica no mesmo banco: um serviço a menos para operar.
- **Alternativa considerada:** um banco vetorial separado (Qdrant, Pinecone). Só vale a
  pena com muito mais volume ou se o assistente virar produto. Revisitaremos então.

### SQLAlchemy
- **O que é:** ORM: traduz classes Python em tabelas e consultas SQL.
- **Por quê:** você escreve Python em vez de SQL na maior parte do tempo, com verificação
  de tipos. É o ORM mais usado em Python.
- **Onde:** `app/models.py` (tabelas) e `app/db.py` (conexão).

### Alembic
- **O que é:** controle de versões do esquema do banco ("migrações").
- **Por quê:** quando uma tabela muda, a mudança vira um arquivo numerado. O servidor
  aplica só as que faltam. Sem isso, cada mudança exigiria SQL manual no servidor.
- **Onde:** `migrations/`. O CI roda `alembic check` para garantir que nenhuma mudança
  em `models.py` ficou sem migração.

### pydantic-settings
- **O que é:** lê variáveis do arquivo `.env` e valida os tipos.
- **Por quê:** senhas e chaves ficam fora do código (o código vai para o GitHub, o `.env`
  não). Se faltar ou vier errado, o erro aparece na hora de subir, não no meio do uso.
- **Onde:** `app/config.py`.

## Serviços externos

### Claude (API da Anthropic)
- **Por quê:** é o modelo que lê, classifica e extrai os fatos. Toda chamada passa por
  `app/llm.py`, que registra tokens e custo e respeita o teto de US$ 10 por mês.
- **Fallback do servidor:** se o modelo recusar uma resposta, a própria API tenta outro
  modelo na mesma chamada. Ligado só nos modelos que aceitam (Opus e Sonnet).

### APIs do Google (Gmail, Agenda, Drive)
- **Por quê:** são as fontes. O acesso é por OAuth (explicado em
  [03-seguranca.md](03-seguranca.md)), e só com as permissões mínimas.
- **Por que consulta periódica e não "push":** o Gmail pode avisar o servidor de cada
  e-mail novo, mas para isso o servidor teria que ser público na internet. Preferimos
  perguntar a cada poucos minutos e manter o servidor fechado.

## Infraestrutura

### AWS Lightsail
- **O que é:** servidor virtual simples da AWS, com preço fixo mensal.
- **Por quê:** você preferiu nuvem. Lightsail é a porta de entrada mais simples da AWS,
  com snapshots (backup da máquina inteira) em um clique.
- **Alternativas:** EC2 (mais flexível e mais complexo), servidor em casa (sem custo de
  nuvem, mas depende da sua internet e energia).

### Docker e Docker Compose
- **O que é:** Docker empacota o app com tudo de que ele precisa numa "imagem"; o Compose
  sobe vários contêineres juntos (banco, migração, console).
- **Por quê:** o mesmo pacote roda igual no CI, no servidor e no seu computador. Atualizar
  é `git pull` e `docker compose up -d --build`.
- **Onde:** `Dockerfile` e `docker-compose.yml` (os dois estão comentados).

### Tailscale
- **O que é:** rede privada entre os seus aparelhos (VPN moderna).
- **Por quê:** o servidor não tem nenhuma porta aberta na internet. Só seus aparelhos
  conectados ao Tailscale chegam ao console, e o Tailscale informa quem é você.
- **Detalhes:** [03-seguranca.md](03-seguranca.md).

### GitHub Actions (CI)
- **O que é:** roda os testes automaticamente a cada push.
- **Por quê:** nenhum código chega ao servidor sem passar por lint, testes, conferência
  das migrações e construção da imagem Docker.
- **Onde:** `.github/workflows/ci.yml`. Mais em
  [04-ciclo-de-desenvolvimento.md](04-ciclo-de-desenvolvimento.md).
