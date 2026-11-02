# 9. Projetos, equipe, quem falou nas reuniões e tags nos e-mails

**O que muda:**

- O console ganha a página **Cadastros** (ícone de **lâmpada acesa** no menu), com
  **projetos**, **pessoas** e **tags**. As tags saíram de Configurações e foram para lá.
- Cada pessoa pode ser marcada como **liderança na empresa**, e cada pessoa da equipe de um
  projeto como **liderança no projeto**.
- Cada transcrição de reunião (importada do Teams ou do Meet, ou feita pela extensão) passa
  a guardar **cada fala numa linha do banco**, com o nome de quem falou e, quando dá para
  reconhecer, a **pessoa cadastrada**. É a base das regras de alerta sobre pedidos e
  decisões de lideranças.
- E-mails e reuniões recebem a **tag do projeto sozinhos**, por três regras (9.4), e você
  pode colocar e tirar tags à mão na página do item.

O porquê de cada escolha está em
[docs/aprendizado/10-projetos-equipe-falas-tags.md](../aprendizado/10-projetos-equipe-falas-tags.md).

**Nada muda no Google:** as pastas do Gmail são lidas com a permissão `gmail.readonly` que
já existe desde o guia 5. O sistema **não** mexe nas pastas nem nos e-mails.

## 9.1 Instalar

No servidor:

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

O serviço `migrate` aplica a migração `0006`: tabela de falas (`utterances`), tabela de
tags de e-mails e reuniões (`item_tags`), tabela de pastas do Gmail ligadas a projetos
(`gmail_label_tags`), e os campos novos (tag do projeto, outros nomes e liderança das
pessoas, liderança no projeto). Nada existente é apagado.

Depois, **uma vez**, transforme em falas as transcrições que já estavam no banco:

```bash
docker compose run --rm coletor python -m app.cli extrair-falas
```

O comando lê o texto de cada transcrição antiga no formato `[mm:ss] Nome: texto` e grava
as falas. Transcrições antigas importadas de `.docx` ou `.pdf` ficaram com o texto como
veio e são puladas (veja Limitações). Rodar de novo não duplica.

## 9.2 Cadastrar um projeto

1. Clique na **lâmpada** do menu.
2. Em **Projetos**, preencha o **nome** e, se quiser, o **código** (ex.: `ATL`), **outros
   nomes** (ex.: `CRM, Atlas`) e **palavras-chave** (ex.: `migração do ERP`), separados por
   vírgula. Esses termos são o que a regra "nome no texto" procura (9.4).
3. **Criar projeto.** A tag de mesmo nome é criada (ou, se já existia uma tag com esse
   nome, ela passa a ser a tag do projeto), e os e-mails e reuniões que já estão no banco
   recebem a tag.
4. Clique no nome do projeto para a página dele: **Equipe** e **Pastas do Gmail**.

**Renomear** o projeto renomeia a tag. **Desativar** (desmarcar "ativo") faz as regras
pararem de colocar a tag em itens novos; as tags já colocadas ficam.

## 9.3 Cadastrar pessoas e a equipe

**Pessoas** (página Cadastros): nome, e-mail, cargo, empresa, **outros nomes** e a caixa
**Liderança na empresa**. A pessoa é única pelo e-mail: salvar de novo com o mesmo e-mail
atualiza a mesma pessoa.

**Outros nomes é o campo mais importante para as reuniões.** O nome que aparece na
transcrição muitas vezes não é o nome do cadastro: "Ana", "Ana S.", "ana.souza". Coloque
aqui como a pessoa aparece no Teams e no Meet.

**Equipe** (página do projeto): escolha uma pessoa já cadastrada ou preencha os dados de
uma nova, a **função no projeto** (ex.: Patrocinadora, PO) e a caixa **Liderança no
projeto**. Para mudar a função ou a liderança, use a linha da pessoa na tabela da equipe.
**Tirar** retira a pessoa da equipe; o cadastro dela continua, e as tags que os e-mails
dela já tinham recebido ficam.

Toda mudança em pessoas e projetos reconhece de novo as falas sem pessoa e reaplica as
regras de tag. O botão **Conferir falas e tags de novo** faz o mesmo à mão.

## 9.4 Como as tags entram sozinhas

Um e-mail ou uma reunião recebe a tag do projeto por qualquer uma destas regras. A página
do item mostra qual regra colocou cada tag.

| Regra | E-mail | Reunião |
| --- | --- | --- |
| **nome no texto** | Nome, código, outro nome ou palavra-chave do projeto no **assunto** ou no **texto escrito pela pessoa** (o texto citado da resposta e a assinatura não contam, a não ser em e-mail encaminhado) | Só no **título** da reunião |
| **pasta do Gmail** | O e-mail está numa pasta (marcador) do Gmail ligada ao projeto | Não se aplica |
| **equipe** | Quem enviou é da equipe do projeto | Alguém da equipe foi convidado ou falou |

Detalhes:

- A comparação ignora maiúsculas e acentos e só aceita a **palavra inteira** ("Atlas" não
  acha "Atlântico"). Termos com menos de 3 letras são ignorados.
- **Pastas do Gmail:** na página do projeto, escolha a pasta e clique **Ligar pasta**. Os
  e-mails da pasta recebem a tag na hora. Depois, a cada coleta (5 min), o sistema confere
  a pasta: e-mail que entrou na pasta ganha a tag; e-mail que **saiu** da pasta perde a
  tag que a pasta tinha colocado. **Desligar** a pasta tira as tags que ela colocou.
- A regra "equipe" pega todo e-mail de alguém da equipe, inclusive assunto pessoal. Se a
  pessoa está em vários projetos, o e-mail dela recebe a tag de todos.

## 9.5 Tags à mão e o filtro

Na página de um e-mail ou reunião (**Ver o conteúdo coletado** → clique no item):

- digite o nome da tag e clique **+ tag**; se a tag não existir, ela é criada;
- o **×** tira a tag. **Tirar à mão é definitivo para aquele item:** as regras não colocam
  de novo, mesmo que o e-mail continue citando o projeto. Para voltar, coloque à mão.

Em **Ver o conteúdo coletado**, a lista tem a coluna **Tags** e o filtro **Todas as tags**.
Clicar numa tag em Cadastros abre a lista filtrada por ela.

Arquivos do Drive continuam com as tags das pastas (guia 6.3); a página deles mostra as
tags, mas não tem o campo **+ tag**.

## 9.6 Quem falou

Na página de uma transcrição, a tabela **Quem falou** mostra cada nome como apareceu, a
pessoa cadastrada reconhecida, quantas falas ela teve e as marcas **liderança na
empresa** e **liderança no** (nome do projeto).

O sistema reconhece a pessoa nesta ordem, e só liga quando a resposta é **uma pessoa só**:

1. o nome na transcrição é o e-mail de uma pessoa cadastrada;
2. o nome bate com o nome ou com um "outro nome" de uma pessoa cadastrada (sem diferenciar
   maiúsculas e acentos; "(Convidado)" e "(Guest)" são ignorados);
3. o nome bate com um participante do convite da agenda, e o e-mail desse participante é
   de uma pessoa cadastrada.

Se o nome bate com duas pessoas (dois "João" cadastrados, por exemplo), a fala fica **sem
pessoa**. Corrija acrescentando um "outro nome" que diferencie, e clique **Conferir falas
e tags de novo**.

Na página da pessoa (Cadastros → clique no nome) aparecem os projetos dela e as últimas
falas reconhecidas.

## 9.7 Testar

1. Cadastre um projeto com o nome de um projeto real seu e uma pessoa da equipe com o
   e-mail dela, marcando **Liderança no projeto**.
2. Em **Ver o conteúdo coletado**, filtre pela tag do projeto: devem aparecer os e-mails
   que citam o projeto e os enviados por essa pessoa.
3. Ligue uma pasta do Gmail ao projeto: o aviso diz quantos e-mails ganharam a tag.
4. Importe o `.vtt` de uma reunião do Teams com essa pessoa (guia 8). Na página da
   transcrição, a tabela **Quem falou** deve mostrar a pessoa com a marca de liderança.
5. Tire a tag de um e-mail à mão e clique **Conferir falas e tags de novo**: a tag não
   volta.

## Limitações conhecidas

- **`.docx` e `.pdf` fora do padrão.** As falas do `.docx` são separadas quando o arquivo
  segue o padrão do Teams (nome e horário numa linha, texto na seguinte) ou do Meet
  ("Nome: texto"). Fora disso, o texto entra inteiro, sem falas. O `.pdf` nunca é separado.
  Prefira o `.vtt`.
- **A extensão não sabe nomes.** Na transcrição ao vivo, quem fala do seu microfone é
  "Você" e os outros são "Falante 1", "Falante 2"... Essas falas ficam gravadas, mas sem
  pessoa. Para reconhecer quem falou, prefira importar a transcrição do Teams ou do Meet
  da mesma reunião.
- **Uma pasta do Gmail com mais de 2.500 e-mails** é lida só até 2.500; nesse caso as tags
  de quem saiu da pasta **não** são tiradas (o sistema não sabe se o e-mail saiu ou só não
  foi listado).
- **As regras só colocam tags, não tiram** (a exceção é a pasta do Gmail). Tirar alguém
  da equipe, mudar as palavras-chave ou desativar o projeto não tira as tags já
  colocadas; tire à mão as que estiverem erradas.
- **Falas não são editáveis pela tela.** Se o reconhecimento errar (dois apelidos
  parecidos), corrija o cadastro; não há botão para trocar a pessoa de uma fala.
