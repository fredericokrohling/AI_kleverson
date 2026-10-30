# Guia de aprendizado

Este projeto também é um curso. Os guias de `docs/setup/` dizem **como** instalar; os
guias desta pasta dizem **por que** cada peça existe, quais alternativas foram
consideradas e onde ela aparece no código.

## Ordem sugerida de leitura

1. [Arquitetura: as camadas e por que um banco de dados](01-arquitetura.md)
2. [Tecnologias: o que cada uma faz e por que foi escolhida](02-tecnologias.md)
3. [Segurança: como o sistema fica fechado para o mundo](03-seguranca.md)
4. [Ciclo de desenvolvimento: Git, PR, testes e CI](04-ciclo-de-desenvolvimento.md)
5. [Transcrição em tempo real: como funciona e por quê (Deepgram, WebSocket, áudio)](05-transcricao.md)
6. [Ingestão de e-mail: janela de coleta, e-mail bruto, anexos e links do Drive](06-ingestao-email.md)

## Como ler o código

Todo arquivo Python tem:

- **Um texto no topo** (a "docstring" do módulo, entre `"""`) dizendo o que o arquivo faz
  e por que ele existe.
- **Um comentário acima de cada linha** (começa com `#`) explicando o que aquela linha faz.

A exceção são as migrações geradas pelo Alembic (`migrations/versions/`): elas têm um
comentário por tabela, porque cada coluna ali repete uma coluna já comentada em
`app/models.py`.

Sugestão de ordem para ler o código, do mais simples ao mais denso:

1. `app/config.py`: de onde vêm as configurações.
2. `app/db.py`: como o Python conversa com o banco.
3. `app/models.py`: as tabelas.
4. `app/security.py`: criptografia e controle de acesso.
5. `app/llm.py`: como chamamos o Claude e controlamos o custo.
6. `app/google_auth.py`: login no Google.
7. `app/main.py`: as páginas do console.
8. `app/transcription/`: a transcrição ao vivo (comece pelo `__init__.py`).
9. `app/ingestion/`: a coleta de e-mail (comece pelo `__init__.py`).
10. `tests/`: os testes mostram, na prática, o que cada parte deve fazer.

## Pequeno glossário de Python que aparece no código

| Termo | O que é |
| --- | --- |
| `import` / `from x import y` | Traz código de outro arquivo ou biblioteca para usar aqui. |
| `def nome(...)` | Define uma função. |
| `class Nome:` | Define uma classe: um molde de objetos com dados e funções. |
| `self` | Dentro de uma classe, o próprio objeto. |
| `-> tipo` | Anotação do tipo que a função devolve. Não muda o comportamento, ajuda a ler e a achar erros. |
| `x: int \| None` | "x é um inteiro ou vazio (None)". |
| `@algo` em cima de uma função | Decorador: embrulha a função com um comportamento extra (cache, rota web, fixture de teste). |
| `with ... as x:` | Abre algo (arquivo, conexão) e garante que será fechado no fim, mesmo com erro. |
| `try` / `except` | Tenta executar; se der o erro indicado, trata em vez de quebrar. |
| `raise` | Levanta um erro de propósito. |
| `yield` | Entrega um valor e pausa a função; usado para "abrir, entregar, fechar depois". |
| `f"texto {variavel}"` | f-string: monta texto com valores dentro. |
| `**kwargs` / `**params` | Junta (na definição) ou espalha (na chamada) argumentos com nome num dicionário. |
| `assert condição` | Nos testes: se a condição for falsa, o teste falha. |
