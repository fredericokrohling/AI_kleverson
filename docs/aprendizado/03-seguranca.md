# 3. Segurança: como o sistema fica fechado para o mundo

Seu requisito: "garantir que o mundo externo não terá acesso a meus dados e ao
desenvolvimento". Isso é atendido em camadas. Se uma falhar, as outras seguram.

## Camada 1: o servidor é invisível na internet

- O firewall do Lightsail fica **sem nenhuma regra** de entrada (guia 01, passo 1.4).
  Nem a porta 22 (SSH) fica aberta.
- O console escuta só em `127.0.0.1` (o próprio servidor). Veja `ports:` em
  `docker-compose.yml`.
- O único caminho de entrada é o **Tailscale**, uma rede privada em que só os seus
  aparelhos estão.

**Por quê:** um servidor com porta aberta recebe tentativas de invasão automatizadas em
minutos. Um servidor sem porta aberta nem aparece para esses robôs.

## Camada 2: só você entra no console

- O `tailscale serve` repassa ao app o login de quem fez a requisição, no cabeçalho
  `Tailscale-User-Login`.
- Toda página (exceto `/saude`) exige que esse login seja igual a `ALLOWED_TAILSCALE_LOGIN`.
- Se `ALLOWED_TAILSCALE_LOGIN` estiver vazio, **ninguém** entra. Isso se chama "falhar
  fechado": um erro de configuração bloqueia em vez de liberar.

**Onde:** `require_owner` em `app/security.py`; testes em `tests/test_security.py`.

## Camada 3: OAuth com permissões mínimas

- O sistema nunca vê sua senha do Google. Você autoriza no site do Google e pode
  revogar em myaccount.google.com/permissions.
- **Nenhuma permissão de escrita no Gmail.** Não pedimos `gmail.send`, `gmail.compose`
  nem `gmail.modify`. Atenção à pegadinha: `gmail.compose` parece ser só "criar rascunhos",
  mas o Google a descreve como "gerenciar rascunhos e enviar e-mails"; a `gmail.modify`
  também permite enviar. Sem nenhuma delas, mesmo com um bug, o sistema não consegue
  enviar e-mail. Rascunhos de resposta, quando existirem, aparecem no console para você
  copiar. Um teste automático (`tests/test_app.py`) falha se alguma dessas permissões
  voltar a ser pedida.
- **PKCE** (`code_verifier`): uma senha de uso único por login. Quem interceptar o código
  que o Google devolve não consegue usá-lo.
- **`state`**: valor aleatório conferido na volta do Google. Impede que alguém induza o
  seu navegador a concluir um login que não foi você que começou.

**Onde:** `app/google_auth.py`, `app/main.py` (rotas `/auth/google/...`).

## Camada 4: tokens criptografados no banco

- O refresh token do Google é gravado criptografado com **Fernet** (AES com verificação
  de integridade), com a chave `TOKEN_ENCRYPTION_KEY` que fica só no `.env`.
- Se alguém obtiver uma cópia do banco sem o `.env`, vê apenas bytes embaralhados.

**Por que Fernet:** é uma receita pronta e revisada da biblioteca `cryptography`. Regra de
ouro de segurança: nunca invente sua própria criptografia.

**Onde:** `TokenCipher` em `app/security.py`.

## Camada 5: o código e as chaves

- O repositório no GitHub é **privado**.
- O servidor baixa o código com uma **deploy key somente leitura**: se o servidor for
  comprometido, a chave não permite alterar o código.
- `.env` está no `.gitignore`: senhas nunca vão para o GitHub.
- O `.env` no servidor tem permissão `600` (só o dono lê).

## Camada 6: custo sob controle

- Dois tetos de gasto com o Claude: um no console da Anthropic e outro na aplicação
  (`LLM_MONTHLY_BUDGET_USD`). Uma chave vazada ou um laço infinito não geram uma conta alta.

## O que ainda não está protegido (honestamente)

- **Backup:** os snapshots do Lightsail ficam na sua conta AWS, sem criptografia própria
  além da padrão da AWS. Para dados pessoais, é aceitável.
- **Provedores externos:** o conteúdo enviado ao Claude e ao Deepgram passa pelos
  servidores deles. As duas empresas declaram não treinar modelos com dados da API, mas
  o dado sai da sua máquina. É o preço de usar modelos na nuvem.
