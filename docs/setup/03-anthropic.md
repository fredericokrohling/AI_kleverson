# 3. Claude: chave da API

A API é um produto separado da assinatura Claude Pro: conta, cobrança e chave próprias.
A conta pode estar no seu nome mesmo que o Pro esteja em outro nome; não há conflito.

Tempo estimado: 10 minutos.

1. Acesse console.anthropic.com e crie a conta no seu nome.
2. **Billing**: compre créditos (comece com US$ 10).
3. **Limits**: defina o limite de gasto mensal (o mesmo valor). Esse é o freio definitivo,
   do lado da Anthropic.
4. **API Keys → Create Key**, nome `assistente-servidor`. Copie a chave (ela aparece uma vez só)
   para `ANTHROPIC_API_KEY` no `.env` do servidor e rode `docker compose up -d`.

Teste:

```bash
docker compose run --rm app python -m app.cli testar-llm
```

Ou pelo botão **Testar Claude** no console. Cada chamada fica registrada com tokens e custo,
e o console mostra o gasto do mês.

## Dois freios de custo

- **No console da Anthropic** (passo 3): bloqueia na fonte.
- **Na aplicação** (`LLM_MONTHLY_BUDGET_USD`): recusa chamadas ao atingir o teto e registra
  um alerta no log quando passa de 80%. Os preços usados no cálculo estão em `app/config.py`.

## Modelo

`LLM_MODEL` define o modelo. O padrão é `claude-opus-5-5`, o mais capaz da linha atual.
Para reduzir custo, `claude-sonnet-5-5` custa cerca de metade e `claude-haiku-5-5` cerca de
40 vezes menos. Vamos medir com seus e-mails reais no bloco 2 antes de decidir.
