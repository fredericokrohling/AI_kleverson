# 13. Empresas: cada cliente separado, com conta Google, projetos e pessoas próprios

**O que muda (pedido da dona em 03/11/2026):**

- **Empresa** é o cliente que você atende. Ela fica acima de tudo: projetos, pessoas,
  tags, pastas do Drive, e-mails, transcrições e o diário de Processamento pertencem a
  uma Empresa. Os dados de uma nunca se misturam com os de outra.
- **Uma conta Google por Empresa.** Os e-mails, o Drive e a agenda de cada conta entram
  só na Empresa dela.
- **Pessoas e tags não são compartilhadas.** O e-mail continua sendo a chave da Pessoa,
  mas só dentro da Empresa: a mesma pessoa pode existir em duas Empresas, uma em cada.
  Duas Empresas podem ter a tag "Portal", cada uma a sua.
- **Telas:**
  - **Cadastros:** seção **Empresas** com o botão **Nova Empresa** e, abaixo dela, um único
    combo **Empresa** (preso no topo ao rolar) que vale para Projetos, Pessoas, Pastas do
    Drive e Tags: filtra as listas e decide onde o cadastro novo entra. Até o ajuste de
    03/11/2026 eram cinco combos (um filtro e um por formulário);
  - **Configurações:** a conta Google de cada Empresa (você escolhe a Empresa antes de
    conectar) e o idioma das reuniões de cada Empresa. O modelo do Claude é um só;
  - **Início:** Empresa e projeto nas Transcrições recentes e nos Arquivos sem texto. Cada
    reunião ao vivo mostra a Empresa dela;
  - **Reuniões:** dois calendários lado a lado, cada um com o combo de Empresa no topo. O
    segundo é roxo escuro, com as reuniões em laranja. A importação de transcrição pede a
    Empresa;
  - **Processamento:** combo de Empresa no topo, com a opção Todas as Empresas;
  - **Página de uma transcrição:** **Corrigir Empresa**, para quando ela foi parar na
    Empresa errada.
- **Extensão 0.3.0:** espera o console descobrir a Empresa da reunião antes de começar a
  transcrever (para usar o idioma da Empresa) e mostra a Empresa no painel.

O porquê de cada escolha está em
[docs/aprendizado/12-empresas.md](../aprendizado/12-empresas.md).

## 13.0 Antes de atualizar

Nada a preparar. A atualização do banco (migração 0008) cria a Empresa
**Teste aplicacao** e entrega a ela tudo o que já existe, inclusive a conta Google já
conectada. Depois você pode renomeá-la em Cadastros → Empresas.

## 13.1 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

Depois recarregue o console com **Ctrl+Shift+R**.

## 13.2 Atualizar a extensão (no seu computador e no segundo computador)

```bash
cd ~/Python_projects/Kleverson/Recursos/AI_kleverson
git pull
```

Em `chrome://extensions`, clique em **recarregar** no cartão **Assistente PM: transcrição**.
A versão passa a ser **0.3.0**. Faça o mesmo nos dois computadores do KVM.

## 13.3 Cadastrar a segunda Empresa e a conta dela

1. **Cadastros → Empresas:** escreva o nome do cliente, escolha o idioma das reuniões dele
   (ou deixe o padrão geral) e clique em **Nova Empresa**.
2. **Configurações → Contas conectadas:** escolha a Empresa nova no combo e clique em
   **Conectar conta Google**. O Google pergunta qual conta usar: escolha a conta de e-mail
   desse cliente.
   - O app no Google Cloud já está publicado ("Em produção", guia 02), então qualquer conta
     Google consegue entrar; o aviso "app não verificado" é esperado.
   - Se a conta do cliente for de uma empresa (Google Workspace) que bloqueia apps de
     terceiros, o login é recusado pelo Google. Nesse caso, o administrador do cliente
     precisa liberar o app, ou você usa outra conta.
   - Cada conta pertence a uma Empresa só, e cada Empresa tem uma conta só. Se você entrar
     com a conta errada, o console recusa e diz qual conta aquela Empresa usa.
3. Em até alguns minutos a coleta de e-mail da conta nova começa sozinha (Processamento
   mostra a rodada, com a Empresa).

## 13.4 Roteiro de testes

1. **Dados atuais:** em Cadastros, com a Empresa "Teste aplicacao" no combo, tudo o que
   existia aparece.
2. **Combo único:** em Cadastros só existe um combo de Empresa, na barra abaixo da tabela
   de Empresas. Role a página: a barra fica presa no topo. Crie um projeto, uma pessoa e
   uma tag: os três entram na Empresa do combo. Pedido sem Empresa (só por fora da tela)
   continua recusado com "Escolha a Empresa.".
3. **Mesmo nome em Empresas diferentes:** crie a tag "Portal" nas duas Empresas: as duas
   ficam, separadas. Na mesma Empresa, a segunda é recusada.
4. **Troca de Empresa:** estando em Tags, troque a Empresa no combo: a página recarrega
   em Tags, com as listas da outra Empresa. Saia e volte pelo menu Cadastros: ela abre na
   última Empresa escolhida.
5. **Reuniões:** os dois calendários mostram a agenda de cada conta. Troque a Empresa de um
   deles no combo; troque o dia. Clique numa reunião sem transcrição do calendário roxo: o
   formulário passa para a Empresa daquele calendário, com a reunião escolhida.
6. **Reunião ao vivo:** comece a transcrição numa reunião marcada na agenda de uma das
   contas. A área no Início já mostra a Empresa certa, e o painel da extensão também.
   - Numa reunião que não está em nenhuma agenda, a área mostra **Escolha a Empresa** em
     laranja. Se você encerrar sem escolher, a reunião fica no Início (até 7 dias) como
     "encerrada, falta a Empresa" e **não vai para o banco**; ao escolher, ela é salva.
7. **Corrigir Empresa:** abra uma transcrição (Início → Transcrições recentes), escolha a
   outra Empresa em **Empresa da transcrição** e clique em **Corrigir Empresa**. As tags
   saem e voltam pelas regras da Empresa nova, e quem falou passa a ser procurado entre as
   Pessoas dela.
8. **Idioma:** em Configurações, deixe uma Empresa em inglês. Uma reunião dessa Empresa
   (com a Empresa vinda da agenda) é transcrita em inglês.
9. **Processamento:** escolha uma Empresa no topo: os números, a coleta, o diário e o
   conteúdo passam a ser só dela.

## 13.5 Se algo der errado

- **A Empresa da reunião não foi descoberta:** ela só é descoberta quando o endereço do
  Meet está num evento da agenda de **uma** das contas. Se as duas contas foram convidadas
  para a mesma reunião, o console não escolhe por você: escolha no Início. Reunião do
  Teams não tem endereço do Meet; escolha no Início também.
- **Reunião transcrita no idioma errado:** o idioma é escolhido quando o áudio conecta.
  Se a Empresa só foi escolhida depois, valeu o padrão geral. Encerre e comece de novo, ou
  corrija a Empresa mesmo assim (o texto fica como foi transcrito).
- **"A conta x já é da Empresa y":** essa conta já está ligada a outra Empresa. Uma conta
  não muda de Empresa (os e-mails dela já foram gravados lá).
- **"A Empresa x já tem a conta y":** você entrou no Google com outra conta. Entre com a
  conta que a Empresa já usa (o Google pergunta qual conta usar).
- **Extensão 0.3.0 demora um pouco a começar:** ela espera até 6 segundos o console ler as
  agendas. Sem resposta nesse tempo, começa mesmo assim, com o idioma padrão.
