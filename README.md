# Kleverson A.I. (Assistente PM)

Assistente de IA para o dia a dia de program e project management: lê e-mails, agenda,
reuniões e chats, organiza tudo por projeto e gera os artefatos de gestão.

## Arquitetura em uma página

| Camada | O que guarda | Onde |
| --- | --- | --- |
| Cadastro | Projetos e stakeholders, editados por você | `projects`, `stakeholders` |
| Bronze | Conteúdo bruto como chegou, nunca editado | `bronze_items`, `attachments` |
| Silver | Fatos extraídos com trecho literal da fonte | `facts`, `item_classifications` |
| Sugestões da IA | Propostas separadas, que não contaminam as camadas acima | `ai_suggestions` |
| Gold | KPIs calculados por SQL, nunca pelo LLM | views (a partir da fase 2) |
| Operação | Tokens criptografados, custo de cada chamada ao Claude, auditoria | `oauth_tokens`, `llm_calls`, `audit_log` |

Regras que o código garante:

- **Não envia e-mail.** Nenhuma permissão de escrita no Gmail é pedida ao Google
  (`gmail.send`, `gmail.compose` e `gmail.modify` ficam de fora, porque todas permitem enviar).
- **Nada público na internet.** O console escuta só em `127.0.0.1`; o acesso é pela rede
  privada do Tailscale, e só o login configurado em `ALLOWED_TAILSCALE_LOGIN` entra.
- **Só leitura no Gmail e no Drive.** A coleta de e-mail usa `gmail.readonly` e `drive.readonly`.
- **Todo uso do Claude passa por `app/llm.py`**, que registra tokens e custo e respeita o teto mensal.

## Aprender com o projeto

Este projeto também é material de estudo. Comece por [docs/aprendizado/](docs/aprendizado/README.md):
lá está o **porquê** de cada escolha (arquitetura, tecnologias, segurança, fluxo de
desenvolvimento). Todo arquivo Python tem um comentário acima de cada linha explicando o
que ela faz.

## Instalação

Siga na ordem:

1. [Servidor: Lightsail + Tailscale + Docker](docs/setup/01-servidor.md)
2. [Google: projeto, APIs e login](docs/setup/02-google-oauth.md)
3. [Claude: chave da API](docs/setup/03-anthropic.md)
4. [Transcrição em tempo real](docs/setup/04-transcricao.md)
5. [Coleta de e-mail](docs/setup/05-email.md)
6. [Console: pastas do Drive, tags, configurações e saúde](docs/setup/06-console.md)

## Desenvolvimento local

Requer [uv](https://docs.astral.sh/uv/) e Docker.

```bash
uv sync
docker run -d --name pgdev -e POSTGRES_USER=assistente -e POSTGRES_PASSWORD=teste \
  -e POSTGRES_DB=assistente -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16
uv run pytest            # aplica as migrações e roda os testes
uv run ruff check . && uv run ruff format --check .
```

Mudou `app/models.py`? Gere a migração com
`uv run alembic revision --autogenerate -m "descrição"` e revise o arquivo gerado.

## Estrutura

```
app/
  config.py        configuração (.env), escopos do Google, preços dos modelos
  models.py        esquema do banco (camadas medallion)
  llm.py           camada única de acesso ao Claude, com custo e teto
  google_auth.py   login Google e tokens criptografados
  security.py      criptografia e controle de acesso pelo Tailscale
  main.py          console web (FastAPI + HTMX)
  cli.py           comandos: gerar-chave, testar-llm, testar-google, coletar-gmail, reextrair-anexos
  transcription/   transcrição ao vivo: serviço de streaming, trechos, finalização
  ingestion/       coleta do Gmail: e-mail bruto, anexos, links do Drive, extração de texto
extensao/          extensão do Chrome que captura o áudio do Meet
migrations/        migrações do banco (Alembic)
docs/setup/        guias de instalação (como)
docs/aprendizado/  por que cada escolha foi feita
```

## Andamento

| Bloco | Estado |
| --- | --- |
| 0. Fundação | Em operação |
| 1. Teste de transcrição em tempo real | Em operação; avaliação de qualidade pendente |
| 2. E-mail para bronze | Código pronto; falta instalar e importar um mês |
| 3. Silver: classificação e extração | A fazer |
| 4. Console: cadastro, fila, fatos, busca | A fazer |
| 5. Ações: rascunhos, agenda, briefing | A fazer |
| 6. Transcrição em produção | A fazer |
| 7. Servidor MCP | A fazer |
| 8. MD de auditoria no Git | A fazer |
