# 12. Empresas: separar os dados de cada cliente em todas as camadas

Pedido da dona (03/11/2026): ela atende vários clientes, e cada cliente (uma **Empresa**)
tem um ou mais projetos. A regra mais importante é a premissa 1: **os dados de uma
Empresa não se misturam com os de outra**. A premissa 6 diz por quê: um comentário de um
cliente não pode influenciar a resposta que o assistente dá a outro.

Este guia explica onde a Empresa entrou, por que a separação está no banco e não só no
código, e o que ainda vai precisar de cuidado quando o chat com a IA chegar.

## Onde a Empresa entrou

A Empresa é uma tabela nova (`companies`) e uma coluna `company_id` nas tabelas que
guardam dados:

| Tabela | Empresa | Por quê |
| --- | --- | --- |
| projetos, pessoas, tags, pastas e arquivos do Drive | obrigatória | todo cadastro é de um cliente |
| conteúdo do bronze (e-mails, versões do Drive, transcrições) | obrigatória | é o que a IA vai ler |
| contas Google e rodadas de coleta | obrigatória | cada conta é a caixa de um cliente |
| reuniões ao vivo | opcional | a Empresa pode ser escolhida durante a reunião |
| diário (Processamento) | opcional | testes de saúde e ações gerais não são de ninguém |

As tabelas de ligação (tag de um e-mail, pessoa num projeto, fala de uma pessoa) não têm
a coluna: a Empresa delas é a das duas pontas, e o banco confere que as duas pontas são da
mesma Empresa (ver abaixo).

## Por que a separação está no banco

Havia dois caminhos:

- **Só no código:** cada consulta lembra de filtrar pela Empresa. É o que todo sistema faz,
  e o que falha primeiro: basta uma consulta esquecer o filtro para misturar clientes, e
  nenhum teste avisa se ninguém pensou naquele caso.
- **No banco também:** o próprio Postgres recusa gravar uma mistura, venha ela de onde vier.

> **Decisão:** Avaliamos só no código e no código mais no banco. Optamos por **também no
> banco**, porque a premissa 1 é a regra mais importante do sistema e o custo é pequeno:
> três funções de gatilho na migração 0008.

As travas do banco são:

1. **`NOT NULL`**: conteúdo, cadastro ou conta sem Empresa não é gravado (premissa 15).
2. **Regras de "não repete" dentro da Empresa**: o nome do projeto, o nome da tag e o
   e-mail da pessoa não se repetem **na mesma Empresa**; em Empresas diferentes, podem
   (premissas 3 e 4).
3. **`empresa_coerente()`**: um gatilho em cada ligação (tag de um e-mail, membro de um
   projeto, fala de uma pessoa, pasta de um arquivo...) que recusa ligar linhas de
   Empresas diferentes, com a mensagem "Dados de Empresas diferentes não podem ser
   ligados". O gatilho é **adiado para o fim da transação** (`DEFERRABLE INITIALLY
   DEFERRED`): a correção de Empresa de uma transcrição muda várias linhas em passos, e
   só o resultado final precisa estar coerente.
4. **`empresa_fixa()`**: a Empresa de um projeto, pessoa, tag, pasta, arquivo ou conta não
   muda. Mudar a Empresa de uma pessoa, por exemplo, deixaria as falas e projetos dela na
   Empresa antiga.
5. **`empresa_corrigida()`**: quando uma transcrição ou reunião **muda** de Empresa (a
   única mudança permitida), confere no fim que nada ligado a ela ficou na antiga.

**Sobre o e-mail da Pessoa (decisão da dona, 03/11/2026):** único dentro de cada Empresa,
não no sistema todo. A mesma pessoa que trabalha com dois clientes seus fica cadastrada
duas vezes, uma em cada, porque Pessoas não são compartilhadas (premissa 3).

## A conta Google de cada Empresa

Cada Empresa tem uma conta (premissa 2). A coleta de e-mail e a varredura do Drive rodam
**uma vez por conta**, e tudo o que entra recebe a Empresa da conta. A trava que impede
duas coletas ao mesmo tempo (`pg_try_advisory_lock`) passou a ter dois números, a trava e
a Empresa: a coleta de um cliente não espera a do outro.

No login, a Empresa é escolhida **antes** de ir ao Google (o Google não sabe de Empresas)
e guardada no cookie de sessão até a volta. O console recusa uma conta já ligada a outra
Empresa e uma Empresa que já tem outra conta, em vez de trocar em silêncio: a troca
misturaria a caixa nova com o histórico da antiga.

## Reuniões: de que Empresa é?

Uma reunião ao vivo começa sem Empresa. Quando a extensão avisa o começo, o console
procura o endereço do Meet nos eventos de hoje **da agenda de cada conta**:

- achou em **uma** agenda: a reunião é daquela Empresa;
- achou em duas (as duas contas foram convidadas) ou em nenhuma: o console não chuta. A
  área da reunião no Início mostra o combo **Escolha a Empresa**.

Sem Empresa, a reunião aparece ao vivo, mas **não vai para o banco** ao ser encerrada:
fica esperando (até 7 dias) e é salva quando você escolhe. A alternativa seria salvar numa
Empresa "padrão" e corrigir depois, mas aí as regras de tag daquela Empresa já teriam
rodado na conversa de outro cliente.

O **idioma** da transcrição é o da Empresa, escolhido quando o áudio conecta ao Deepgram.
Por isso a extensão 0.3.0 espera a resposta do aviso de começo (até 6 segundos) antes de
abrir o áudio.

## Corrigir a Empresa de uma transcrição

Só transcrições mudam de Empresa. E-mails e arquivos do Drive vieram da caixa de uma
conta, e a conta é de uma Empresa: mudá-los misturaria as caixas.

A correção (`move_item` em `app/companies.py`) refaz as camadas que dependem da Empresa:

1. apaga as tags, a classificação em projeto e os fatos extraídos (eram de projetos da
   Empresa antiga), inclusive as tags colocadas à mão;
2. muda a Empresa da transcrição e da reunião ao vivo, se houver;
3. procura de novo a Pessoa de cada participante (pelo e-mail) e de cada fala (pelo nome),
   entre as Pessoas da Empresa nova;
4. roda as regras de tag dos projetos da Empresa nova;
5. registra no diário das duas Empresas.

O texto original não muda. Se a mesma transcrição já foi importada na Empresa nova, a
correção é recusada (ficariam duas iguais).

## O que ainda vai precisar de cuidado

- **Chat e relatórios (premissas 5 e 6):** ainda não existem. Quando chegarem, **toda**
  busca de contexto para a IA precisa filtrar pela Empresa da conversa, e o relatório de
  um projeto só pode somar dados da Empresa dele. O banco já garante que cada pedaço de
  conteúdo tem uma Empresa só; falta a consulta usar isso.
- **Microfone aberto nas duas reuniões (backlog):** a ideia mais segura é a extensão de
  cada computador mandar ao console o estado do microfone no Meet (ela já lê esse estado),
  e o Início avisar quando os dois estiverem abertos. Comparar a sua voz nas duas
  transcrições fica como reserva, porque só avisa depois que você já falou.

## Onde está no código

- `migrations/versions/0008_empresas.py`: a tabela, as colunas, as regras de "não repete"
  e os três gatilhos.
- `app/companies.py`: cadastro da Empresa, idioma, descoberta da Empresa da reunião e a
  correção de Empresa.
- `app/google_auth.py`: o login ligado a uma Empresa.
- `app/ingestion/gmail.py` e `app/ingestion/drive_sync.py`: uma coleta por conta.
- `app/project_tags.py` e `app/people.py`: regras de tag e reconhecimento de quem falou,
  só entre os projetos e Pessoas da Empresa do conteúdo.
- `tests/test_companies.py`: os testes que provam a separação.
