# 15. Classificação de cada item por projeto

**O que muda (bloco 3, primeira parte):**

- **Coletor:** a cada 5 minutos, depois da leitura pelo Claude, classifica por projeto
  os e-mails, transcrições e arquivos do Drive que ainda não têm projeto. As regras
  (tag à mão, pasta, mesma conversa, equipe, nome no texto) decidem sem custo; o Claude
  Haiku só entra quando elas empatam ou não acham nada.
- **Página Classificação** (menu Trabalho): números por passo da cascata, quanto foi
  decidido sem custo, gasto do mês com classificação e a fila **A revisar**.
- **Página de cada item:** linha **Projeto** com o método, a confiança e o motivo
  (passe o mouse no "i"), e um combo para você escolher o projeto (trava a decisão).
- **Processamento → Conteúdo coletado:** coluna **Projeto**.
- **Comando novo:** `classificar [--refazer]`.
- **Banco:** migração 0010 (colunas novas em `item_classifications`).

O porquê de cada escolha está em
[docs/aprendizado/14-classificacao.md](../aprendizado/14-classificacao.md).

## 15.1 Antes de atualizar: cadastros

A qualidade depende dos cadastros. Quanto mais as regras decidem, menos o Claude gasta e
menos erra. Em **Cadastros**, para cada projeto:

1. confira **outros nomes** e **palavras-chave** (como o projeto é chamado nos e-mails);
2. monte a **equipe** (quem é só deste projeto decide sozinho);
3. ligue as **pastas do Gmail** que você já usa para o projeto.

Não precisa estar perfeito: o que ficar errado aparece na fila e pode ser corrigido.

## 15.2 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

A migração 0010 roda sozinha. Opcional no `.env` (os padrões já servem):

```bash
# Modelo da classificação (padrão: Haiku, o mais barato).
CLASSIFY_MODEL=claude-haiku-5-5
# Abaixo disso, a decisão do Claude vai para a fila "A revisar".
CLASSIFY_MIN_CONFIDENCE=0.7
# Máximo de chamadas ao Claude por rodada de 5 minutos.
CLASSIFY_LLM_PER_RUN=30
```

## 15.3 Classificar o que já foi coletado

O coletor faz isso sozinho, 30 chamadas ao Claude por rodada. Para não esperar:

```bash
docker compose exec app python -m app.cli classificar
```

Ele mostra no fim algo como
`Classificação: 412 pelas regras, 57 pelo Claude, 9 para revisar, 0 esperando.`
Custo esperado com o Haiku: menos de US$ 0,05 para algumas centenas de itens.

## 15.4 Roteiro de testes

1. **Página:** abra **Classificação**. "Classificados" mostra o total e o % sem custo de IA.
2. **Regras:** abra um e-mail de alguém que está na equipe de um só projeto. A linha
   **Projeto** diz "por equipe · 85%"; o "i" explica o motivo.
3. **Mesma conversa:** responda um e-mail já classificado. Depois da próxima coleta, a
   resposta aparece com "por mesma conversa".
4. **Pasta:** mova um e-mail para uma pasta do Gmail ligada a um projeto. Em até
   10 minutos (coleta + classificação), ele aparece "por pasta", mesmo que antes estivesse
   em outro projeto.
5. **Claude:** procure um item "por Claude". O "i" traz o motivo que ele deu.
6. **Fila:** em **A revisar**, a sugestão do Claude já vem no combo. Troque se estiver
   errada e clique em **Gravar**. O item sai da fila e a página dele diz "por você".
7. **Travar e soltar:** na página do item, escolha outro projeto e **Gravar projeto**.
   Depois clique em **Voltar à classificação automática**: em até 5 minutos ele é decidido
   de novo.
8. **Duas Empresas:** com a segunda Empresa cadastrada, filtre pelo combo do topo. O
   combo de projeto de cada item só mostra projetos da Empresa dele.
9. **Custo:** em Configurações, o gasto do mês sobe só centavos.

## 15.5 Se algo der errado

- **Tudo "esperando":** a Empresa não tem projeto ativo, falta a chave da Anthropic ou o
  teto do mês foi atingido. Veja `docker compose logs --tail 50 coletor` (linha
  "Classificação: ...").
- **Muitos itens pelo Claude:** os cadastros estão magros (15.1). Complete e rode
  `classificar --refazer`.
- **Muitos itens errados pelo Claude:** troque para `CLASSIFY_MODEL=claude-sonnet-5-5`,
  reinicie e rode `classificar --refazer`. Compare a fila antes e depois.
- **Mudei muitos cadastros:** `docker compose exec app python -m app.cli classificar --refazer`
  (as suas escolhas no console não são apagadas).
