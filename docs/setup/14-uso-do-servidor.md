# 14. Uso do servidor: disco por projeto e gráficos de processador e memória

**O que muda (pedido da dona em 03/11/2026):**

- **Configurações → Servidor** (seção nova, no fim da página):
  - **Disco:** gráfico de rosca com o disco inteiro: cada projeto, o que não tem projeto,
    o banco de dados, o sistema e o livre. Ao lado, a tabela com o tamanho e o % de cada
    fatia, e uma frase com o projeto que mais ocupa;
  - **Uso do servidor:** botões **Últimas 2 horas** e **Últimos 7 dias**, um veredito
    (Folga, Atenção ou Gargalo), cinco números (processador, roubado pela AWS, espera de
    disco, memória e carga) e dois gráficos de barras: processador e memória. Passe o
    mouse numa barra para ver os números daquele pedaço.
- **Coletor:** passa a medir o servidor a cada minuto (tabela nova `server_metrics`,
  migração 0009). As medições começam quando o coletor sobe com esta versão: o gráfico de
  7 dias só fica completo uma semana depois.
- **Saúde → Disco do servidor:** o % ocupado passa a contar a reserva de ~5% do Linux,
  igual ao gráfico. O número pode subir alguns pontos (ex.: de 24% para 28%) sem que nada
  tenha mudado no disco.

O porquê de cada escolha (o que medir, como ler cada número, os limites do veredito)
está em [docs/aprendizado/13-uso-do-servidor.md](../aprendizado/13-uso-do-servidor.md).

## 14.1 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

A migração 0009 roda sozinha na subida. Depois recarregue o console com **Ctrl+Shift+R**.

## 14.2 Roteiro de testes

1. **Disco:** abra Configurações → Servidor. A rosca mostra o % ocupado no centro e o
   tamanho do disco. Some de cabeça as linhas da tabela: dá o tamanho do disco.
2. **Projetos:** os projetos com arquivos aparecem na tabela com o nome da Empresa embaixo.
   Projetos sem nenhum arquivo não aparecem. É esperado que eles tenham fatias muito
   finas: o sistema ocupa muito mais que os dados (ver 13.4 no guia de aprendizado).
3. **Medições:** logo depois de atualizar, o bloco diz "Ainda sem medições". Espere 2 ou
   3 minutos (a página de 2 horas se atualiza sozinha a cada minuto): as primeiras barras
   aparecem à direita, e o texto ao lado dos botões conta os minutos medidos.
4. **Pico de verdade:** em Processamento, rode uma coleta grande (ou reprocessamento).
   Volte em Configurações: a barra daquele momento sobe, e o traço fino do minuto mais
   cheio fica acima dela.
5. **7 dias:** clique em **Últimos 7 dias**. As barras viram 1 hora cada, com o dia da
   semana embaixo. No começo quase tudo aparece vazio, e o veredito avisa que as medições
   cobrem só parte do período.
6. **Comparar com a AWS:** no painel do Lightsail, aba **Métricas** da instância,
   confira **Utilização de CPU** e **Capacidade de pico restante** (*burst capacity*). Se a
   capacidade de pico chegar perto de zero no mesmo horário em que o violeta (roubado)
   sobe aqui, é a confirmação de que o plano está pequeno.

## 14.3 Se algo der errado

- **"Ainda sem medições" depois de 5 minutos:** o coletor não está rodando ou não subiu
  com esta versão. Rode `docker compose ps` (ou o botão **Ver serviços** na Saúde) e
  `docker compose logs --tail 50 coletor`. Procure "Medição do servidor falhou".
- **Buracos no meio do gráfico ("sem medição"):** o coletor ficou parado nesse período
  (reinício, atualização, queda). Uma atualização deixa um buraco de 1 ou 2 minutos.
- **O disco demora a abrir na primeira vez:** a medição passa por todos os arquivos. Ela
  fica guardada por 10 minutos; as aberturas seguintes são imediatas.
