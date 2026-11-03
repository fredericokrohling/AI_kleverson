# 10. Novo visual do console e agenda em formato de calendário

**O que muda:**

- Visual escuro com três tons de preto (menu, fundo e cartões), fonte Inter e ícones
  menores, de traço fino.
- Menu em grupos (Trabalho, Organização, Sistema), com nomes ao lado dos ícones, item novo
  **Conteúdo** e botão para recolher o menu só nos ícones.
- As explicações de cada caixa viraram o ícone ⓘ ao lado do título: passe o mouse (ou use
  Tab) para ler.
- As reuniões do dia num dia de calendário, com a situação da transcrição de cada uma (página
  Reuniões).

> Os ajustes de 03/11/2026 (cores `#333333` e `#0f0f0f`, página Processamento, indicador de
> saúde no Início, agenda só em Reuniões) estão no [guia 11](11-pagina-processamento.md). Os
> passos 3 e 4 do roteiro abaixo foram trocados por ele.
- A página de importar transcrição virou **Reuniões**, com o calendário do dia ao lado do
  formulário.

O porquê de cada escolha está em
[docs/aprendizado/07-console-drive-saude.md](../aprendizado/07-console-drive-saude.md)
(seções "O visual do console" e "Agenda em formato de dia do calendário").

**Nada muda no Google nem no banco:** sem permissão nova e sem migração. A agenda usa a
mesma leitura que a importação de transcrições já fazia (guia 8).

## 10.1 Instalar

No servidor:

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

Depois, no navegador, recarregue o console com **Ctrl+Shift+R** (força baixar o CSS e a
fonte novos, em vez de usar a cópia guardada pelo navegador).

## 10.2 Roteiro de testes

1. **Menu:** os três grupos aparecem; o item da página aberta fica destacado em azul. O botão
   ao lado do logo recolhe o menu; recarregue a página e ele continua recolhido. Clique de
   novo para abrir.
2. **Dicas:** passe o mouse no ⓘ ao lado de "Coleta de e-mail". A explicação aparece e some
   ao tirar o mouse. Com Tab, chegue ao ⓘ: a explicação aparece também.
3. **Números de resumo:** cada número leva ao detalhe (e-mails → Conteúdo; anexos → Arquivos
   sem texto; Drive → Configurações; transcrições → Reuniões).
4. **Agenda na página inicial:** mostra as reuniões de hoje no horário certo, lado a lado as
   que se sobrepõem, com a linha vermelha do "agora". As setas trocam o dia; **Hoje** volta.
   - Reunião com transcrição (bolinha verde): o clique abre a transcrição.
   - Reunião sem transcrição (bolinha cinza): o clique abre Reuniões com ela já escolhida.
5. **Reuniões:** clique numa reunião sem transcrição no calendário: ela fica com borda azul e
   aparece escolhida na lista. Troque o dia da reunião: o calendário e a lista mudam juntos.
6. **Celular:** abra o console no celular (pelo Tailscale). O menu fica só nos ícones e
   nada obriga a rolar para o lado.

## 10.3 Se algo der errado

- **Página sem estilo ou com a fonte antiga:** o navegador usou a cópia antiga. Recarregue
  com Ctrl+Shift+R.
- **Cartão Agenda com "Não consegui ler a agenda":** é o mesmo erro da página Reuniões
  (conta Google desconectada ou permissão da Agenda). Veja o guia 8.
- **Reunião com transcrição aparecendo como "sem transcrição":** transcrições feitas pela
  extensão ainda não guardam o id do evento da agenda; só as importadas são reconhecidas
  (limitação registrada na arquitetura, seção 10).
