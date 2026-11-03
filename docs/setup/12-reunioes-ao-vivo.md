# 12. Reuniões ao vivo no Início, participantes e pastas do Drive em Cadastros

**O que muda (pedido da dona em 03/11/2026):**

- **Início é o painel de controle.** No topo, **Reuniões ao vivo**: a transcrição da
  extensão aparece enquanto a reunião acontece. São **duas áreas** lado a lado; com duas
  reuniões ao mesmo tempo, cada uma fica na sua área, sem misturar.
- **Participantes e falantes**, em cada área: você inclui quem está na reunião (da página
  Pessoas ou só com nome e e-mail) e diz quem é cada voz. "Falante 2" vira "Márcia Lopes"
  em todas as falas, inclusive nas que já passaram, e é assim que a reunião vai para o
  banco. O nome não se repete na reunião. O botão **Trazer convidados da agenda** inclui
  os convidados do evento com o mesmo endereço do Meet.
- **Pessoa: o e-mail virou a chave.** É obrigatório e não se repete. Um participante
  escrito à mão com o e-mail de uma Pessoa fica ligado a ela. Se você cadastrar a Pessoa
  depois, com o mesmo e-mail, as falas dele passam a ser dela.
- **Extensão 0.2.0:** várias reuniões ao mesmo tempo (uma captura por aba do Meet). O seu
  microfone só vai para a reunião em que você está com o microfone **aberto no Meet**, e
  cada painel tem um botão para desligá-lo só naquela reunião. Fechar a aba do Meet salva
  a reunião sozinho.
- **Pastas do Drive** saíram de Configurações e foram para **Cadastros**.

O porquê de cada escolha está em
[docs/aprendizado/11-reunioes-ao-vivo.md](../aprendizado/11-reunioes-ao-vivo.md).

## 12.0 Antes de atualizar: todas as Pessoas precisam de e-mail

A atualização do banco (migração 0007) torna o e-mail obrigatório. Se alguma Pessoa
estiver sem e-mail, a migração **para sem mudar nada** e diz quem está sem.

Por isso, **antes do `git pull`**, abra **Cadastros → Pessoas** no console atual e
preencha o e-mail de quem estiver com a coluna vazia. Se duas pessoas tiverem o mesmo
e-mail com maiúsculas diferentes (`Ana@x.com` e `ana@x.com`), junte-as numa só.

Se você esquecer: o console não sobe, porque ele só sobe depois de a migração terminar sem
erro (`docker compose logs migrate` mostra a mensagem com os nomes). Para corrigir, volte
à versão anterior, preencha os e-mails e atualize de novo:

```bash
git checkout HEAD~1 && docker compose up -d --build   # versão anterior (sem a migração)
# preencha os e-mails em Cadastros → Pessoas, no navegador
git checkout main && docker compose up -d --build     # esta versão de novo
```

## 12.1 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

Depois recarregue o console com **Ctrl+Shift+R**.

## 12.2 Atualizar a extensão (no seu computador)

A extensão roda do seu clone local do repositório.

```bash
cd ~/Python_projects/Kleverson/Recursos/AI_kleverson
git pull
```

Em `chrome://extensions`, no cartão **Assistente PM: transcrição**, clique no ícone de
**recarregar** (a seta circular). A versão passa a ser **0.2.0**.

**Duas reuniões ao mesmo tempo:** abra cada reunião numa aba (ou janela) do Chrome e clique
no ícone da extensão em cada uma. Veja a nota sobre janelas anônimas em 12.4.

## 12.3 Roteiro de testes

1. **Pastas do Drive:** Configurações não tem mais a seção; ela está em **Cadastros →
   Pastas do Drive**, com as mesmas pastas. Clique no nome de uma pasta: a navegação e as
   tags funcionam como antes.
2. **E-mail obrigatório:** em Cadastros, tente salvar uma pessoa sem e-mail: o console
   recusa com "Informe o e-mail da pessoa".
3. **Início sem reunião:** a seção **Reuniões ao vivo** mostra duas áreas livres e, à
   direita, "recebendo ao vivo" (a conexão com o servidor está aberta).
4. **Uma reunião:** entre numa reunião de teste no Meet e clique no ícone da extensão. Em
   poucos segundos a área da reunião aparece no Início com o título, e as falas chegam
   enquanto são ditas (em itálico enquanto o serviço ainda corrige a frase).
5. **Quem é cada voz:** abra **Participantes e falantes**:
   - escolha uma Pessoa na lista e clique em **Incluir**; inclua também alguém só com nome;
   - tente incluir o mesmo nome de novo (mesmo com outra grafia, como "joao" e "João"):
     o console recusa;
   - na linha "Falante 1", escolha o participante: as falas desse falante, as antigas e as
     novas, passam a mostrar o nome;
   - na linha "Você", escolha você mesma.
6. **Convidados da agenda:** numa reunião marcada na agenda, clique em **Trazer convidados
   da agenda**: os convidados entram como participantes.
7. **Duas reuniões:** abra outra reunião em outra aba e clique no ícone. Uma segunda área
   aparece. Fale em cada uma: cada fala vai só para a área da reunião dela.
   - Feche o microfone no Meet da reunião A e abra na B: o painel da extensão da aba A diz
     "Mudo no Meet: seu microfone não vai para esta reunião".
8. **Salvar:** clique em **Parar e salvar** no painel da extensão (ou feche a aba do Meet).
   A área mostra "salva" e um link para a transcrição, que já sai com os nomes. A área fica
   no Início por 30 minutos; depois disso, os participantes podem ser acertados pela página
   da transcrição (link no painel da extensão).
9. **Pessoa depois:** inclua numa reunião um participante só com nome e e-mail, ligue-o a
   um falante e salve. Depois cadastre essa pessoa em Cadastros, com o mesmo e-mail: na
   página da pessoa, as falas dela aparecem.

## 12.4 Se algo der errado

- **"reconectando…" no Início o tempo todo:** a conexão contínua com o servidor caiu e não
  volta. Recarregue a página. Se continuar, veja `docker compose logs app`.
- **A área da reunião não aparece:** confira se a extensão é a 0.2.0 (`chrome://extensions`).
  A versão antiga transcreve, mas não avisa o Início do começo da reunião (a área só
  aparece na primeira fala).
- **Janela anônima:** a extensão só funciona numa janela anônima se você ligar
  **Permitir no modo anônimo** nos detalhes dela, em `chrome://extensions`. Não consegui
  testar a captura de áudio de aba anônima; se não funcionar, use abas ou janelas comuns.
  Atenção: todas as janelas anônimas abertas ao mesmo tempo dividem o mesmo login. Se a
  ideia é entrar com duas contas Google diferentes, o caminho seguro são dois **perfis**
  do Chrome (cada um com a extensão instalada e o mesmo endereço do console).
- **Sua voz apareceu nas duas reuniões:** o Meet mudou a página e a extensão não achou o
  botão do microfone. Use o botão **Desligar meu microfone aqui** no painel da reunião em
  que você não está falando.
- **"Falante 1" e "Falante 2" trocados no meio da reunião:** o serviço de transcrição
  separa as vozes pelo som, e às vezes junta duas pessoas parecidas ou troca o número.
  Corrija a ligação na hora; ela vale para todas as falas daquele rótulo.
