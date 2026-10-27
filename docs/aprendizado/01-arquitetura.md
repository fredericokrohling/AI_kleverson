# 1. Arquitetura: as camadas e por que um banco de dados

## O problema

E-mails, reuniões e chats chegam todos os dias, misturados entre projetos. O objetivo é
transformar esse fluxo em informação de gestão confiável: tarefas, decisões, riscos,
prazos, RAID, RACI, KPIs. O risco central de usar IA nisso é a **alucinação**: o modelo
inventar um fato ou um prazo que ninguém disse.

## Por que um banco de dados e não arquivos Markdown

A primeira ideia era guardar tudo em arquivos `.md` num repositório Git. Funciona para
ler, mas falha no que o assistente precisa fazer:

| Necessidade | Arquivos MD | Banco (Postgres) |
| --- | --- | --- |
| "Quais tarefas vencem esta semana em todos os projetos?" | Abrir e interpretar centenas de arquivos | Uma consulta SQL de milissegundos |
| Não gravar o mesmo e-mail duas vezes | Lógica manual frágil | Regra `UNIQUE` garantida pelo banco |
| Duas coisas gravando ao mesmo tempo | Conflito de arquivo | Transações resolvem |
| KPIs (prazo médio, riscos abertos) | Código para cada cálculo | `SUM`, `COUNT`, `GROUP BY` |
| Busca por significado (fase 2) | Não tem | Extensão pgvector |

O Markdown continua no plano, mas como **saída** (relatórios e trilha de auditoria no
Git, bloco 8), não como armazenamento principal.

## O modelo medallion (bronze, silver, gold)

É um padrão comum em engenharia de dados. Cada camada tem uma regra clara:

- **Bronze**: o conteúdo bruto, exatamente como chegou (e-mail, transcrição). **Nunca é
  editado.** Se a extração estiver errada, o original continua aqui e dá para refazer.
- **Silver**: fatos extraídos (tarefa, decisão, risco...). Cada fato **obrigatoriamente**
  aponta para o item bronze de origem e traz o **trecho literal** que o sustenta. O código
  confere se o trecho existe mesmo no original (`quote_verified`). Esse é o principal
  freio contra alucinação.
- **Gold**: indicadores (KPIs) calculados por **SQL**, nunca pelo modelo. Número de gestão
  precisa ser reproduzível; o LLM não é uma calculadora confiável.

Mais duas áreas completam o desenho:

- **Sugestões da IA** (`ai_suggestions`): quando a IA opina ("isso parece um risco novo"),
  a opinião fica separada. Ela não se mistura com os fatos extraídos de fontes reais.
- **Operação**: tokens criptografados, custo de cada chamada ao Claude e o log de auditoria.

## Por que você não precisa aprovar tudo

Você pediu para não ter uma fila de aprovação. A arquitetura compensa isso de três formas:

1. O bronze é imutável: nada se perde.
2. Todo fato carrega a fonte e o trecho: você confere em um clique.
3. O `audit_log` registra tudo que o sistema faz sozinho: dá para revisar depois.

## Onde isso está no código

- Tabelas: `app/models.py` (cada camada tem um bloco marcado com `# --- Bronze ---` etc.).
- Criação das tabelas no banco: `migrations/versions/0001_esquema_inicial.py`.
