# 18. Alertas de liderança

**O que muda (bloco 3, terceira parte):**

- **Página Alertas** (menu Trabalho, abaixo de Fatos, com o número de novos ao lado):
  tarefas, decisões, riscos e prazos ditos por **lideranças**, os novos e de peso alto
  primeiro, cada um com o trecho, quem disse, o projeto, o responsável, o prazo e o item
  de origem. Botões **Visto**, **Resolvido**, **Descartar** e **Reabrir**.
- **Regras editáveis** na mesma página: cada regra é um filtro (tipos de fato, que
  liderança disse, condição do responsável, prazo em até N dias, peso, Empresa). Quatro
  já vêm prontas.
- **Coletor:** a cada 5 minutos, depois dos fatos, aplica as regras. **Sem custo de
  IA**: são consultas ao banco.
- **Comando novo:** `gerar-alertas`.
- **Banco:** migração 0013 (tabelas `alert_rules` e `alerts`, com as quatro regras).

O porquê de cada escolha está em
[docs/aprendizado/17-alertas.md](../aprendizado/17-alertas.md).

> Este PR é construído em cima do PR #19 (extração de fatos). Faça o merge do #19 antes.

## 18.1 Antes de atualizar: marcar as lideranças

Sem liderança marcada, nenhum alerta nasce. Em **Cadastros**:

1. **Liderança na empresa:** em **Pessoas**, abra a pessoa (diretoria, gerência) e
   marque **Liderança na organização**;
2. **Liderança no projeto:** em cada projeto → **Equipe**, marque **Liderança no
   projeto** em quem lidera aquele projeto (patrocinadora, PO, gerente);
3. **Você como Pessoa** (para a regra "o responsável sou eu"): cadastre-se como Pessoa
   em cada Empresa, com o e-mail do login do Tailscale **ou** com o e-mail da conta
   Google conectada naquela Empresa.

## 18.2 Atualizar o servidor

```bash
ssh ubuntu@assistente
cd ~/AI_kleverson
git pull
docker compose up -d --build
```

A migração 0013 roda sozinha e cria as quatro regras-padrão. Opcional no `.env`:

```bash
# Quantos dias para trás uma regra olha (padrão: 30). Itens mais antigos não disparam
# alerta; as regras de prazo olham a data do prazo.
ALERTS_LOOKBACK_DAYS=30
```

## 18.3 Gerar os alertas do que já foi extraído

O coletor faz isso sozinho em até 5 minutos. Para ver na hora:

```bash
docker compose exec app python -m app.cli gerar-alertas
```

Ele mostra algo como
`Alertas: 7 novo(s) (Pedido de liderança: 3; Decisão de liderança: 1; ...).`
Se mostrar `nenhum novo`, veja a seção 18.5.

## 18.4 Roteiro de testes

1. **Página:** abra **Alertas**. O número ao lado do menu é o de "Novos".
2. **Pedido de diretoria:** abra um alerta "Pedido de liderança". **Quem disse** mostra
   o nome e "(liderança na empresa)" ou "(liderança no projeto)". Clique na origem e
   confira o trecho no texto.
3. **Um fato, uma linha:** um pedido com prazo em até 3 dias aparece **uma vez**, com
   "também: Pedido de liderança" embaixo do nome da regra.
4. **Visto:** clique em **Visto**. O número do menu cai; a linha continua em "Abertos".
5. **Resolvido:** clique em **Resolvido**. A linha sai de "Abertos" e o fato fica
   concluído (a regra de prazo não avisa mais sobre ele). **Reabrir**, no filtro
   "Resolvido", volta o alerta e o fato.
6. **Regra nova:** em **Nova regra**, crie "Pedido sem dono": Tarefa, liderança
   qualquer, responsável **Sem responsável**, peso Alta. Ao gravar, o aviso diz quantos
   alertas nasceram, e a lista mostra só essa regra.
7. **Para mim:** crie uma regra com responsável **O responsável sou eu**. Só aparecem os
   pedidos em que você é a responsável (precisa do passo 3 da seção 18.1).
8. **Desligar:** desmarque **Ligada** numa regra. Os alertas dela ficam; novos não
   nascem.
9. **Duas Empresas:** crie uma regra só para a Empresa B. Fatos da Empresa A não
   disparam essa regra; o combo de Empresa no topo filtra a lista e os números.

## 18.5 Se algo der errado

- **Nenhum alerta:** (a) ninguém está marcado como liderança (seção 18.1); (b) os fatos
  não têm **quem disse** reconhecido: na página Fatos, a coluna mostra o nome cinza
  (não cadastrado) ou "—"; cadastre a pessoa com o e-mail certo e use **Extrair de
  novo** no item; (c) os fatos são **suspeitos** (trecho não conferido), que de
  propósito não viram alerta; (d) o item tem mais de 30 dias.
- **Alerta com a pessoa errada:** **Descartar**, e corrija a Pessoa (outros nomes) em
  Cadastros. O alerta descartado fica registrado.
- **Alertas demais:** desligue a regra de "Decisão" ou troque "qualquer" por "Só
  liderança na empresa". Regras são suas: ajuste até a lista caber numa olhada por dia.
- **"Extrair de novo" num item** apaga os fatos dele e, junto, os alertas (inclusive os
  vistos e resolvidos). Os fatos novos geram alertas novos.
