# 8. Importar transcrições do Teams e do Meet

**O que muda:** o console ganha a página **Reuniões** (ícone de calendário no menu, ou o
botão **Importar transcrição** na página inicial; até o guia 10 era o microfone). Você escolhe o dia, a reunião da sua agenda e o
arquivo da transcrição que o Teams ou o Meet já fizeram. O texto entra no banco ligado à
reunião, como se tivesse sido feito pela extensão, sem custo de transcrição.

O porquê de cada escolha está em
[docs/aprendizado/09-importar-transcricoes.md](../aprendizado/09-importar-transcricoes.md).

**Nada muda no Google:** a página lê a agenda com a permissão que já existe desde o guia
2, e a transcrição do Meet com a permissão de leitura do Drive. Não há migração de banco.

## 8.1 Instalar

No servidor:

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

Ou use o botão **Atualizar** da página Saúde (6.1).

## 8.2 Baixar a transcrição

Os menus do Teams e do Meet mudam com as versões; se o caminho abaixo não bater, procure
por "Transcrição" ou "Transcript" na reunião.

**Teams.** Abra a reunião no calendário do Teams (ou o chat da reunião) → **Recapitulação**
→ **Transcrição** → **Baixar** → **Baixar como .vtt**. Prefira o `.vtt` ao `.docx`: ele traz
o nome de quem falou em cada trecho de um jeito que o sistema separa com segurança. Só
quem organizou a reunião (ou quem tem permissão) consegue baixar.

**Meet.** Quando a transcrição está ligada, o Meet salva um Google Docs na pasta
**Meet Recordings** do Drive de quem organizou e anexa esse documento ao evento da agenda.
Nesse caso **você não precisa baixar nada**: veja 8.3. Se o documento não estiver anexado
ao evento, abra-o no Drive e baixe como `.docx` (Arquivo → Fazer download → Microsoft Word).

A transcrição do Meet só existe em algumas edições pagas do Google Workspace. Numa conta
Gmail pessoal gratuita, em geral a opção não aparece. Nesse caso, use a extensão (guia 4)
nas reuniões do Meet que você quer transcrever.

## 8.3 Importar

1. Clique em **Reuniões** no menu (ou em **Importar transcrição** na página inicial). Clicar
   numa reunião sem transcrição no calendário da página escolhe a reunião no formulário.
2. **Dia da reunião:** começa em hoje. Ao trocar a data, a lista de reuniões é lida de novo
   da sua agenda.
3. **Reunião:** a lista mostra as reuniões com horário daquele dia, com a plataforma
   (Meet ou Teams) quando o convite diz. Eventos de dia inteiro (feriado, férias) não
   aparecem.
   - Reunião marcada com **transcrição do Meet anexada:** pode deixar o arquivo em branco.
     O sistema baixa a transcrição do Drive.
   - Reunião que não está na sua agenda: escolha **Não está na agenda** e digite o título.
4. **Arquivo:** `.vtt`, `.srt`, `.docx`, `.txt` ou `.pdf`, até 10 MB.
5. **Importar transcrição.** O resultado aparece no topo da página:
   - **Transcrição carregada com sucesso**, com o número de falas e de pessoas (no `.vtt`, e no `.docx` no padrão do Teams ou do Meet)
     ou o tamanho do texto (nos outros formatos), e o link **Ver**;
   - **já tinha sido importada:** o mesmo texto já estava no banco; nada foi duplicado;
   - ou o motivo da falha (formato, arquivo vazio, reunião não escolhida, Drive sem acesso).

A transcrição aparece em **Últimas transcrições** (na mesma página), na página inicial
(**Transcrições de reunião**), em **Ver o conteúdo coletado** (tipo "Reunião") e em
**Processamento** como `transcricao_importada`.

## 8.4 Testar

1. Baixe o `.vtt` de uma reunião do Teams de hoje.
2. Importe escolhendo essa reunião na lista.
3. Confira o aviso: o número de pessoas deve bater com quem falou.
4. Clique em **Ver**: o texto deve ter uma linha por fala, no formato
   `[mm:ss] Nome: texto`, e o topo deve mostrar a reunião da agenda e os participantes.
5. Importe o mesmo arquivo de novo: deve aparecer "já tinha sido importada".

## Limitações conhecidas

- **Ligação errada não tem botão de desfazer.** Se você escolher a reunião errada, a
  transcrição fica ligada a ela. Como o item do bronze não é editado, a correção hoje é
  pelo banco; um botão de "trocar a reunião" fica para o bloco 3, quando as reuniões
  ganharem página própria.
- **No `.docx` e no `.txt`, quem falou só é separado quando o arquivo segue o padrão do
  Teams ou do Meet** (desde o [guia 9](09-projetos-equipe-tags.md)). Fora do padrão, e
  sempre no `.pdf`, o texto entra como está e a contagem de pessoas não aparece. Por isso
  o `.vtt` continua o preferido.
- **Só a agenda principal** da conta conectada é lida. Reuniões que estão só no Outlook
  (e não foram aceitas no Google Agenda) não aparecem: use "Não está na agenda".
- **A transcrição do Meet não é importada sozinha.** O sistema só baixa quando você
  escolhe a reunião e clica em importar. Importar automaticamente todas as reuniões com
  transcrição anexada é possível, mas depende de a sua conta ter a transcrição do Meet.
