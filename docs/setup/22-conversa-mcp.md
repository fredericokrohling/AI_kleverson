# 22. Conversar com os dados pelo Claude (MCP)

**O que muda (bloco 7):**

- **No servidor:** uma API nova em `/api/conversa/...`, que só responde ao seu login do
  Tailscale e a pedidos com o cabeçalho do servidor MCP. A migração 0017 cria o índice da
  busca por palavra (com e sem acento) nos itens e nos anexos.
- **No seu computador:** o programa `mcp_local/kleverson_mcp.py`, que o Claude Desktop (ou o
  Claude Code) liga sozinho. Ele dá ao Claude oito ferramentas: listar Empresas, buscar,
  listar fatos, alertas abertos, briefing, reuniões, ler uma fonte e pôr um prazo na agenda.
- **Custo:** zero na API. A conversa usa a sua assinatura do Claude; o servidor só faz
  consultas SQL.
- **Nenhuma porta nova e nenhuma permissão nova no Google.**

O porquê de cada escolha (por que o MCP roda no seu computador, por que busca por palavra
e não por significado, por que só uma ferramenta escreve) está em
[docs/aprendizado/21-conversa-mcp.md](../aprendizado/21-conversa-mcp.md).

Tempo estimado: 20 minutos.

## 22.1 Antes de começar: a conta do Claude

O que as ferramentas devolvem entra na conversa e fica no histórico da conta do Claude
que você usar.

1. Decida **qual conta** vai conversar com os dados dos clientes (veja o ponto de atenção
   "de quem é a conta" no guia de aprendizado 21).
2. Nessa conta, abra **Configurações → Privacidade** e **desligue** a opção de usar as
   conversas para melhorar o Claude (o nome exato pode variar). Em conta pessoal, ela
   pode vir ligada.

## 22.2 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

A migração 0017 roda sozinha. Ela reescreve as tabelas `bronze_items` e `attachments` uma
vez, para calcular a coluna de busca de tudo o que já foi coletado. Com o volume de hoje,
leva segundos.

As variáveis novas têm valor padrão e só precisam ir para o `.env` se você quiser mudar
algo:

```bash
# Máximo de resultados por pergunta (busca, fatos, alertas, reuniões). Máximo 50.
MCP_MAX_RESULTS=20
# Máximo de caracteres de uma fonte por leitura (textos longos saem em páginas).
MCP_SOURCE_CHARS=12000
```

## 22.3 Preparar o seu computador

No **seu computador** (não no servidor):

1. **Atualize o repositório** local, para ter o `mcp_local/`:

   ```bash
   cd ~/Python_projects/Kleverson/Recursos/AI_kleverson
   git pull
   ```

2. **Confira o uv.** Ele instala as bibliotecas do programa num ambiente só dele:

   ```bash
   uv --version
   ```

   Se o comando não existir, instale com `curl -LsSf https://astral.sh/uv/install.sh | sh`
   e abra um terminal novo.

3. **Anote o caminho completo do uv** (o Claude Desktop não lê o PATH do terminal):

   ```bash
   which uv
   ```

   Normalmente é `/home/SEU-USUARIO/.local/bin/uv`.

4. **Teste o programa sozinho.** Troque o endereço pelo do seu console (o mesmo do
   navegador):

   ```bash
   KLEVERSON_URL=https://assistente.<sua-rede>.ts.net \
     uv run --script mcp_local/kleverson_mcp.py
   ```

   Na primeira vez o uv baixa as bibliotecas (alguns segundos). Depois o programa fica
   parado, esperando o Claude, sem mostrar nada: isso é o certo. Saia com **Ctrl+C**.

## 22.4 Conectar ao Claude Desktop

1. Instale o Claude Desktop para Linux (beta), se ainda não tiver, pelo endereço
   `claude.com/download`, e entre com a conta escolhida em 22.1.
2. Abra **Configurações → Desenvolvedor → Editar configuração**. Isso abre o arquivo
   `~/.config/Claude/claude_desktop_config.json`.
3. Ponha o conteúdo abaixo, trocando os três caminhos e o endereço. Se o arquivo já
   tiver um `"mcpServers"`, acrescente só o bloco `"kleverson"` dentro dele.

   ```json
   {
     "mcpServers": {
       "kleverson": {
         "command": "/home/SEU-USUARIO/.local/bin/uv",
         "args": [
           "run",
           "--script",
           "/home/SEU-USUARIO/Python_projects/Kleverson/Recursos/AI_kleverson/mcp_local/kleverson_mcp.py"
         ],
         "env": {
           "KLEVERSON_URL": "https://assistente.<sua-rede>.ts.net"
         }
       }
     }
   }
   ```

4. **Feche e abra** o Claude Desktop (a configuração só é lida ao abrir).
5. Numa conversa nova, o ícone de ferramentas mostra **kleverson** com as oito
   ferramentas.

**Se o Claude Desktop não abrir ou não mostrar as ferramentas no Mint**, use o Claude
Code (22.5). O programa é o mesmo.

## 22.5 Plano B: Claude Code no terminal

1. Instale o Claude Code, se ainda não tiver, e entre com a conta escolhida em 22.1.
2. Registre o servidor uma vez (troque os caminhos e o endereço):

   ```bash
   claude mcp add kleverson --scope user \
     -e KLEVERSON_URL=https://assistente.<sua-rede>.ts.net \
     -- /home/SEU-USUARIO/.local/bin/uv run --script \
     /home/SEU-USUARIO/Python_projects/Kleverson/Recursos/AI_kleverson/mcp_local/kleverson_mcp.py
   ```

3. Rode `claude` em qualquer pasta e digite `/mcp`. **kleverson** aparece como conectado.

## 22.6 Roteiro de testes

1. **Empresas:** pergunte "quais Empresas e projetos você vê?". O Claude chama
   `listar_empresas` e lista as Empresas com os projetos e quantos fatos abertos há.
2. **Uma Empresa por vez:** pergunte "quais riscos estão abertos?", sem dizer a Empresa.
   Ele deve perguntar qual. Escolha uma e confira que nada da outra aparece.
3. **Fonte:** em toda resposta, cada afirmação vem com o título, a data e o link do
   console. Clique num link: abre a página do item no console.
4. **Busca sem acento:** "busque reuniao de orcamento na Empresa X". Acha os e-mails com
   "reunião" e "orçamento".
5. **Anexo:** busque uma palavra que só existe num PDF anexado. O resultado diz
   "anexo: nome.pdf", e "leia esse anexo" traz o texto.
6. **Prazos:** "o que vence até sexta no projeto Atlas, e o que é meu?". Vêm os fatos com
   prazo, o mais perto primeiro, e os seus marcados (os seus dependem de você estar
   cadastrada como Pessoa com o seu e-mail).
7. **Briefing:** "resuma o briefing de hoje da Empresa X". Antes das 7h, ele diz que o
   briefing ainda não foi montado, em vez de inventar.
8. **Reunião:** "o que foi dito na última reunião do comitê?". O Claude acha a reunião,
   lê a transcrição em páginas e resume com a fonte.
9. **Agenda:** "ponha o prazo do fato N na agenda". O Claude Desktop pede a sua permissão
   antes. O evento aparece no Google Agenda da conta da Empresa (dia inteiro, livre,
   particular, sem convidados) e na página **Na agenda**. Pedir de novo responde que já
   está na agenda.
10. **Diário:** em **Processamento**, o diário do dia mostra uma linha
    `conversa.<ferramenta>` para cada chamada, com a Empresa.
11. **Proteção:** no navegador, abra `https://assistente.<sua-rede>.ts.net/api/conversa/empresas`.
    A resposta é **403** (falta o cabeçalho do servidor MCP), mesmo com você logada no
    Tailscale.
12. **Tailscale desligado:** desligue o Tailscale e pergunte algo. O Claude mostra "Não
    consegui falar com o console. O Tailscale está ligado neste computador...".

## 22.7 Problemas comuns

| Sintoma | Causa provável | O que fazer |
| --- | --- | --- |
| "KLEVERSON_URL não configurada" | falta o `env` no JSON (22.4) | confira o bloco `"env"` e reabra o app |
| "O console recusou o acesso (403)" | Tailscale ligado com outra conta | `tailscale status` deve mostrar a sua conta |
| "Não consegui falar com o console" | Tailscale desligado ou servidor parado | ligue o Tailscale; no servidor, `docker compose ps` |
| kleverson não aparece no Claude Desktop | caminho do uv ou do arquivo errado | rode o comando de 22.3, passo 4, com os mesmos caminhos |
| a busca responde "O console respondeu 500" | servidor sem a migração 0017 | `docker compose logs migrate` no servidor |
