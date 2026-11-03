# 11. Página Processamento, cores novas e indicador de saúde

**O que muda (pedido da dona em 03/11/2026):**

- **Cores:** fundo cinza `#333333`; menu, cartões e números em `#0f0f0f`.
- **Menu:** **Conteúdo** virou **Processamento**.
- **Página Processamento**, de cima para baixo:
  1. os quatro números (e-mails no bronze, anexos, Drive e transcrições), que estavam no Início;
  2. a tabela **Coleta de e-mail**, igual à que estava no Início;
  3. a tabela **Processamento** (diário de ações), agora no mesmo desenho da Coleta, com
     rolagem de 10 linhas e aberta no dia de hoje;
  4. o **Conteúdo coletado**: 10 linhas com rolagem para o resto do dia, setas para os dias
     anteriores, calendário para escolher a data, fonte e tag que trocam a tabela sem
     clicar, **Procurar** que junta a palavra à data, à fonte e à tag, e **linha roxa**
     (`#550a58`) no item com erro de processamento.
- **Início:** sem a agenda (o mesmo calendário está em Reuniões) e com um **indicador** que
  diz só se todos os serviços estão funcionando. Qual serviço falhou fica na página Saúde.

O porquê de cada escolha está em
[docs/aprendizado/07-console-drive-saude.md](../aprendizado/07-console-drive-saude.md)
(seções "Processamento: o diário por dia", "Conteúdo coletado" e "O visual do console").

**Nada muda no Google nem no banco:** sem permissão nova e sem migração.

## 11.1 Instalar

No servidor:

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

Depois, no navegador, recarregue o console com **Ctrl+Shift+R**.

## 11.2 Roteiro de testes

1. **Cores:** o fundo é cinza e os cartões e o menu, quase pretos.
2. **Início:** o indicador mostra a bolinha verde e "Todos os serviços funcionando". Se o
   coração do menu estiver vermelho, o indicador também fica vermelho, sem dizer qual
   serviço; o link leva à Saúde. A agenda não aparece mais aqui.
3. **Menu:** o item **Processamento** abre a página nova (o endereço continua `/conteudo`).
4. **Números:** os quatro números aparecem no topo; e-mails abre o Conteúdo coletado só com
   e-mails, em todas as datas.
5. **Tabela Processamento:** mesmo desenho da Coleta de e-mail, 10 linhas visíveis, rolagem
   para o resto e o dia de hoje. Troque o dia no calendário da seção: a tabela muda sem
   recarregar a página.
6. **Conteúdo coletado:**
   - abre em hoje, com 10 linhas e rolagem se houver mais;
   - **‹** vai para o dia anterior e **›** para o seguinte (não passa de hoje);
   - trocar **Fonte** ou **Tag** muda a tabela na hora;
   - digite uma palavra e clique em **Procurar**: vale junto com a data, a fonte e a tag;
   - apague a data e clique em **Procurar**: procura em todas as datas;
   - aperte F5: a página volta com os mesmos filtros (eles ficam no endereço);
   - um e-mail com anexo sem texto aparece com a linha roxa; passe o mouse para ver o motivo.

## 11.3 Se algo der errado

- **Visual antigo:** recarregue com Ctrl+Shift+R.
- **Conteúdo coletado vazio, mas havia itens:** a tabela mostra só o dia escolhido. Use as
  setas, escolha a data no calendário ou apague a data para ver todas.
- **Indicador cinza ("Nenhum teste automático ainda") por mais de 15 minutos:** os testes
  rodam no serviço `coletor`. Veja a página Saúde e `docker compose ps`.
