# 10. Projetos, equipe, quem falou e tags nos e-mails

Como instalar e usar: [docs/setup/09-projetos-equipe-tags.md](../setup/09-projetos-equipe-tags.md).

## O problema

As regras de alerta que vêm depois vão perguntar coisas como "a patrocinadora do projeto
Atlas pediu algo na reunião de ontem?" ou "um diretor mandou e-mail sobre o Atlas e
ninguém respondeu?". Para responder, o banco precisa saber três coisas que não sabia:

1. **quem disse cada frase** numa reunião, ligado à pessoa cadastrada (não só o nome como
   apareceu na tela do Teams);
2. **quem é liderança**, na empresa e em cada projeto;
3. **a que projeto** cada e-mail e cada reunião se referem.

Antes desta versão, a transcrição era um texto corrido (`[mm:ss] Nome: texto`). Dava para
ler, mas não para consultar.

## Uma fala por linha, e não só o texto

Avaliamos duas formas de guardar quem falou:

- **A. Continuar só com o texto** e deixar a IA descobrir quem falou na hora da análise.
- **B. Uma tabela `utterances`**, uma linha por fala: transcrição, ordem, momento, nome
  como apareceu, pessoa reconhecida e texto.

**Optamos por B.** Com A, cada regra de alerta teria de mandar a reunião inteira para o
Claude só para achar as falas de uma pessoa: caro, lento e sujeito a erro de leitura. Com
B, "todas as falas de lideranças do projeto Atlas nesta semana" é uma consulta SQL, e só
essas falas vão para o Claude. O texto corrido continua no item do bronze (que nunca é
editado); a tabela de falas é derivada dele e pode ser refeita (`extrair-falas`).

Duas colunas guardam a pessoa: `speaker` (o nome **como apareceu**, nunca muda) e
`stakeholder_id` (a pessoa **reconhecida**, pode ser preenchida depois). Separar as duas
permite cadastrar alguém amanhã e reconhecer as falas de ontem, sem reimportar nada.

## Reconhecer quem falou

O nome na transcrição raramente é igual ao do cadastro: "Ana Souza (Convidado)",
"ana.souza@cliente.com", "Ana". O reconhecimento tenta, nesta ordem:

1. **o nome é um e-mail** cadastrado (o Teams faz isso com convidados externos);
2. **nome ou "outro nome"** cadastrado, sem diferenciar maiúsculas e acentos, ignorando
   "(Convidado)";
3. **participante do convite** da agenda com esse nome, cujo e-mail é de alguém
   cadastrado. Por isso a importação passou a guardar o par nome → e-mail dos
   participantes (`participantes_emails`).

Avaliamos dois comportamentos para nome que bate com duas pessoas:

- **A. Escolher uma** (a primeira, ou a que está na equipe do projeto).
- **B. Não ligar** e deixar a fala sem pessoa.

**Optamos por B.** Um alerta "a diretora pediu X" atribuído à pessoa errada é pior que
nenhum alerta: leva a uma ação errada com aparência de certeza. A fala sem pessoa continua
visível, e um "outro nome" no cadastro resolve.

Não usamos semelhança aproximada ("Ana Souza" ≈ "Ana Sousa") pelo mesmo motivo: o ganho é
pequeno e o erro é silencioso.

## Liderança em dois lugares

Avaliamos guardar a liderança:

- **A. Só na pessoa** (é ou não é liderança).
- **B. Na pessoa (liderança na empresa) e no vínculo com o projeto (liderança no
  projeto).**

**Optamos por B**, porque é o que você descreveu: um gerente de outra área pode ser
liderança na empresa e só participante no seu projeto; uma analista pode ser a dona do
produto (liderança) num projeto e não ser liderança na empresa. As regras de alerta vão
poder combinar as duas.

## Tags de e-mails: uma tabela própria, com "tirada à mão"

As tags do Drive são herdadas das pastas e calculadas por uma view
(`bronze_item_tags`, guia 7). E-mail não tem pasta no Drive, e as regras são outras. Para
e-mails e reuniões, criamos a tabela `item_tags` com a regra que colocou a tag e um campo
`removed_at`.

Avaliamos duas formas de tratar a tag que você tira à mão:

- **A. Apagar a linha.**
- **B. Manter a linha marcada como tirada** (`removed_at`).

**Optamos por B.** Com A, a próxima coleta ou o próximo "reaplicar" veria que o e-mail
cita o projeto e colocaria a tag de novo: você tiraria a mesma tag para sempre. A linha
marcada diz às regras "a dona já decidiu sobre este item".

## As três regras

**a) Nome no texto.** Procura nome, código, outros nomes e palavras-chave do projeto no
assunto e no texto **escrito pela pessoa**. O texto citado de uma resposta ("Em 2 de nov.,
Ana escreveu:", linhas com `>`, "----- Mensagem original -----") e a assinatura ficam de
fora: sem isso, uma conversa que citou o projeto uma vez marcaria todas as respostas
seguintes, sobre qualquer assunto. A exceção é o e-mail **encaminhado**, em que o
conteúdo que importa é justamente o citado. Só vale a palavra inteira, e termos com menos
de 3 letras são ignorados, para "TI" não marcar metade da caixa de entrada.

Nas reuniões, só o **título** conta. Uma reunião de duas horas cita de passagem vários
projetos; usar o texto todo marcaria quase tudo.

**b) Pasta do Gmail.** Avaliamos:

- **A. Olhar os marcadores de cada e-mail na hora da coleta.**
- **B. Perguntar ao Gmail, a cada coleta, quais e-mails estão em cada pasta ligada.**

**Optamos por B.** Na hora da coleta o e-mail quase nunca está na pasta ainda: você (ou um
filtro) move depois. E o item do bronze guarda os marcadores do momento da coleta, que não
mudam. Perguntar a lista atual da pasta pega quem entrou depois e permite **tirar** a tag
de quem saiu. Só tiramos quando a lista veio inteira (até 2.500 e-mails por pasta): lista
cortada não prova que o e-mail saiu.

**c) Equipe.** O remetente é da equipe do projeto; numa reunião, alguém da equipe foi
convidado ou falou. É a regra mais "larga": pega também o e-mail pessoal de quem está na
equipe. Ficou porque é a que mais acerta no dia a dia de projeto, e a tag tirada à mão
resolve as exceções.

As regras **colocam**, mas só a pasta do Gmail **tira**. Avaliamos tirar a tag sempre
que a regra deixa de valer (alguém sai da equipe, uma palavra-chave muda) e não fizemos:
um e-mail de ontem de quem estava na equipe ontem continua sendo sobre o projeto.

## A tag do projeto acompanha o projeto

Criar o projeto cria a tag de mesmo nome; renomear o projeto renomeia a tag; a tag de um
projeto não pode ser renomeada nem apagada à parte. Avaliamos deixar projeto e tag
independentes e não fizemos: você teria duas listas para manter iguais, e uma tag
renomeada sozinha faria o filtro "Projeto Atlas" sumir sem aviso. Para encerrar um
projeto, desative-o: as regras param, o histórico fica.

## Por que uma página só para Cadastros

Avaliamos:

- **A. Manter tags em Configurações** e pôr projetos e pessoas lá também.
- **B. Uma página própria, Cadastros** (ícone de lâmpada), com projetos, pessoas e tags.

**Optamos por B**, a seu pedido e pelo motivo de fundo: Configurações é o que se mexe
uma vez (contas, modelo, pastas do Drive); Cadastros é o que muda toda semana (gente
entrando em projeto, projeto novo). Misturar os dois esconde o que é de uso diário atrás
do que é de instalação.

## Onde está no código

| Arquivo | O que faz |
| --- | --- |
| `migrations/versions/0006_projetos_equipe_falas_tags.py` | Tabelas `utterances`, `item_tags`, `gmail_label_tags` e os campos novos |
| `app/people.py` | Reconhecer quem falou e gravar as falas |
| `app/project_tags.py` | As três regras, a sincronização com as pastas do Gmail e as tags à mão |
| `app/projects.py` | Criar e alterar projetos, pessoas e equipe |
| `app/routes_registry.py` | Página Cadastros |
| `app/transcription/importer.py` | Separar as falas do `.docx` do Teams e do Meet |
| `app/routes_content.py` | Tags e "Quem falou" na página do item; filtro por tag |
