# 9. Importar transcrições do Teams e do Meet

Este guia explica o porquê da importação. O passo a passo está em
[docs/setup/08-importar-transcricoes.md](../setup/08-importar-transcricoes.md).

Código: `app/transcription/calendar.py` (agenda), `app/transcription/importer.py` (leitura e
gravação) e `app/transcription/routes_import.py` (página).

## O problema

A extensão do bloco 1 transcreve qualquer reunião, mas paga o Deepgram por minuto de áudio.
O Teams e o Meet já transcrevem muitas reuniões de graça. Pagar de novo por elas é
desperdício. O que faltava era um jeito de trazer o arquivo que eles geram e dizer de que
reunião ele é.

## Ligar à reunião da agenda

Uma transcrição solta não diz de que reunião é. Avaliamos duas formas de ligar:

- **Tabela nova de reuniões:** uma linha por reunião, e a transcrição apontando para ela.
  É o modelo "certo" a longo prazo, mas exige migração, telas de cadastro e regras para
  reunião recorrente. É trabalho do bloco 3, quando as reuniões ganharem fatos, ata e
  decisões.
- **O id do evento da agenda no próprio item (escolhido):** o item do bronze guarda
  `thread_id = "agenda:<id do evento>"` e, em `extra`, título, horário, organizador e
  participantes. Nenhuma migração. Quando o bloco 3 criar a entidade "reunião", ela nasce
  desses dados.

O `thread_id` é a mesma coluna que junta os e-mails de uma conversa e as versões de um
arquivo do Drive. Aqui ele junta tudo o que pertence a uma reunião. Numa reunião
recorrente, cada ocorrência tem um id próprio na agenda, então a reunião de segunda e a de
terça não se misturam.

## Ler o evento de novo na hora de gravar

A página manda só o id da reunião escolhida. Avaliamos aceitar do formulário o título e o
horário (já estão na página) e ler o evento de novo no servidor. Optamos por ler de novo:
o que vai para o banco é o que está na agenda, não o que o navegador mandou. Custa uma
chamada à API por importação, o que é irrelevante.

## Mesma fonte da extensão

Avaliamos criar uma fonte nova (`transcricao_importada`) e reaproveitar a fonte da
extensão (`transcricao_reuniao`). Optamos por reaproveitar: para a IA do bloco 3, uma
transcrição é uma transcrição, venha de onde vier. A diferença fica em `extra.origem =
"importada"` e `extra.plataforma`. Uma fonte nova também exigiria migração, porque a lista
de fontes é um tipo fechado no Postgres.

## Um formato só para as falas

O `.vtt` do Teams vem assim:

```
00:00:03.120 --> 00:00:05.440
<v Ana Souza>Bom dia a todos.</v>
```

Avaliamos guardar o arquivo como veio e converter para o formato da extensão
(`[mm:ss] Nome: texto`, uma linha por fala). Optamos por converter:

- a IA lê um formato só, venha a transcrição do Deepgram ou do Teams;
- o Teams corta uma fala longa em vários blocos; juntar os blocos seguidos da mesma pessoa
  deixa o texto legível e economiza tokens no bloco 3;
- o original continua guardado (`extra.arquivo`), então nada se perde.

O `.docx` e o `.txt` não têm marcação confiável de quem fala (cada programa escreve de um
jeito), então entram como texto, pelo mesmo extrator dos anexos de e-mail.

## Importar duas vezes não duplica

O id do item é a impressão digital (SHA-256) do texto. Importar o mesmo arquivo de novo,
mesmo com outro nome, devolve "já tinha sido importada". Um arquivo corrigido (texto
diferente) entra como item novo, como as versões de um arquivo do Drive.

## A transcrição do Meet

Quando a transcrição do Meet está ligada, ela vira um Google Docs anexado ao evento da
agenda. A API da agenda devolve esses anexos junto com o evento, e a permissão de leitura
do Drive (que o sistema já tem) basta para exportar o documento. O sistema reconhece a
transcrição pelo título ("Transcript" ou "Transcrição"); as anotações do Gemini também são
Docs anexados, mas ficam de fora.

Avaliamos importar essas transcrições sozinhas, numa rodada do `coletor`, e importar só
quando você escolhe a reunião na página. Optamos pela página, por enquanto: a transcrição
do Meet só existe em algumas edições pagas do Workspace, e a sua conta atual (Gmail
pessoal) provavelmente não tem. Construir uma rodada automática que talvez nunca rode seria
código sem uso. Se a conta corporativa entrar e tiver transcrição, a rodada automática é
pequena: ela reaproveita tudo deste módulo.

## Por que não a API do Teams

A Microsoft tem uma API (Graph) que entrega as transcrições do Teams. Ela exige registrar
um aplicativo no Azure da empresa, permissões aprovadas por um administrador do Microsoft
365 e um segundo login no sistema. Para quem participa de reuniões de vários clientes,
cada um com o seu Microsoft 365, isso não é viável. Baixar o `.vtt` leva 10 segundos.
