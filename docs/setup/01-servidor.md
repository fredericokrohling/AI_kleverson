# 1. Servidor: Lightsail + Tailscale + Docker

Resultado: um servidor Linux na AWS, sem nenhuma porta aberta para a internet, com o
console acessível só pelos seus aparelhos em `https://assistente.<sua-rede>.ts.net`.

Tempo estimado: 40 minutos.

> **Onde rodar cada comando.** Cada passo diz em qual máquina digitar. O começo da linha
> do terminal mostra onde você está: `ubuntu@assistente:~$` é o servidor; o nome do seu
> computador é o Linux Mint. Para entrar no servidor a partir do seu computador:
> `ssh ubuntu@assistente`. Para sair: `exit`.

## Por que este desenho

- **Lightsail** é o servidor da AWS mais simples de operar, com preço fixo e backup
  (snapshot) em um clique. Você preferiu nuvem a um computador em casa.
- **Tailscale** cria uma rede privada só com os seus aparelhos. Com ele, o servidor pode
  ficar **sem nenhuma porta aberta** na internet: nem a de SSH. Robôs que varrem a
  internet atrás de servidores vulneráveis simplesmente não o enxergam.
- **Docker** empacota o app com todas as dependências. O que roda no servidor é
  exatamente o que foi testado no CI.

Detalhes em [docs/aprendizado/03-seguranca.md](../aprendizado/03-seguranca.md).

## 1.1 Tailscale nos seus aparelhos

**Onde:** no seu computador e no celular.

**Por quê:** cada aparelho que vai acessar o console precisa estar na sua rede Tailscale.
MagicDNS dá um nome fixo ao servidor; HTTPS Certificates permite o cadeado no navegador,
que o login do Google exige.

1. No Linux Mint: `curl -fsSL https://tailscale.com/install.sh | sh` e depois `sudo tailscale up`.
   Abra o link que aparecer e entre com a conta que você criou.
2. No celular: instale o app Tailscale e entre com a mesma conta.
3. No painel do Tailscale (login.tailscale.com), em **DNS**, ative **MagicDNS** e **HTTPS Certificates**.

## 1.2 Criar a instância no Lightsail

**Onde:** no site da AWS (lightsail.aws.amazon.com), pelo navegador.

**Por quê:** Ubuntu LTS tem atualizações de segurança por 5 anos. 4 GB de RAM acomodam
o Postgres, o console e, mais tarde, a transcrição sem travar.

1. Lightsail → **Create instance** → Linux/Unix → **OS Only** → **Ubuntu 24.04 LTS**.
2. Plano com **4 GB de RAM** (2 GB funciona no começo, mas fica apertado com o banco).
3. Nome: `assistente`. Crie.
4. Em **Snapshots**, ative **Automatic snapshots** (backup diário da máquina inteira).

## 1.3 Instalar Tailscale e Docker no servidor

**Onde:** no **servidor**, pelo terminal do navegador do Lightsail (o Tailscale ainda não está
instalado nele).

**Por quê:** o Tailscale vai substituir o SSH público (`--ssh` faz o próprio Tailscale
autenticar o acesso ao terminal). O Docker roda o banco e o app.

Conecte pelo botão **Connect using SSH** do próprio Lightsail e rode:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh --hostname assistente
# abra o link que aparecer e autorize a máquina na sua conta

curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
```

Saia e entre de novo. A partir daqui, conecte do seu computador com `ssh ubuntu@assistente`
(o Tailscale cuida da autenticação).

## 1.4 Fechar o firewall

**Onde:** no site do Lightsail, e o teste no terminal do seu computador.

**Por quê:** este é o passo que torna o servidor invisível. Faça só depois de confirmar
que o acesso pelo Tailscale funciona, senão você perde o acesso à máquina.

No Lightsail → instância → **Networking**: apague **todas** as regras de IPv4 e IPv6,
inclusive a SSH (porta 22). Confirme que `ssh ubuntu@assistente` continua funcionando
pelo Tailscale. A partir daqui a máquina é invisível para a internet.

## 1.5 Baixar o código

**Onde:** no **servidor** (`ssh ubuntu@assistente`). A chave precisa ser criada no servidor,
porque é ele que baixa o código. A chave privada nunca sai de lá; só a pública
(`github_deploy.pub`) é copiada para o GitHub.

**Por quê de uma deploy key:** é uma chave que só permite **ler** este repositório. Se
alguém invadir o servidor, não consegue alterar o código no GitHub nem acessar seus
outros repositórios.

O repositório é privado. Crie uma chave só de leitura para o servidor:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

No GitHub: repositório → **Settings → Deploy keys → Add deploy key**, cole a chave,
deixe **Allow write access desmarcado**. Depois:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
EOF
git clone git@github.com:fredericokrohling/AI_kleverson.git
cd AI_kleverson
```

## 1.6 Configurar e subir

**Onde:** no **servidor**, dentro da pasta `AI_kleverson`.

**Por quê:** o `.env` guarda senhas e chaves fora do código. `chmod 600` faz só o dono do
arquivo conseguir lê-lo. A chave gerada por `gerar-chave` é a que criptografa os tokens
do Google no banco.

**Atenção:** o arquivo `.env.example` só existe na `main` depois do merge dos PRs. Se a pasta
tiver só o `README.md`, faça os merges no GitHub e rode `git pull` antes.

Rode **um passo de cada vez** (os passos 3 e 5 pedem que você preencha valores):

1. Crie o arquivo de configuração e restrinja o acesso:

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

2. Gere dois valores aleatórios (rode duas vezes): o primeiro é a **senha do banco**, o
   segundo é o `SESSION_SECRET`.

   ```bash
   openssl rand -hex 32
   ```

   **Por que `-hex`:** só usa letras e números. Outros formatos podem gerar `/` ou `+`, que
   quebram o endereço do banco em `DATABASE_URL`.

3. Edite o arquivo com `nano .env`:
   - troque `troque-por-uma-senha-longa` pela senha do banco **nas duas linhas** em que
     aparece (`POSTGRES_PASSWORD` e `DATABASE_URL`);
   - `SESSION_SECRET`: o segundo valor;
   - `ALLOWED_TAILSCALE_LOGIN`: o e-mail que aparece no painel do Tailscale como dono da rede;
   - deixe Google, Anthropic, Deepgram e `PUBLIC_BASE_URL` vazios por enquanto (guias 02 a 04
     e passo 1.7).

   Para salvar e sair do nano: **Ctrl+O**, **Enter**, **Ctrl+X**.

4. Construa a imagem (alguns minutos):

   ```bash
   docker compose build
   ```

   Se aparecer "permission denied", saia (`exit`) e entre de novo no servidor, para valer a
   permissão do Docker dada no passo 1.3.

5. Gere a chave de criptografia dos tokens:

   ```bash
   docker compose run --rm app python -m app.cli gerar-chave
   ```

   Copie a linha que aparecer, abra `nano .env` de novo e cole depois de
   `TOKEN_ENCRYPTION_KEY=`.

6. Suba tudo e confira:

   ```bash
   docker compose up -d
   docker compose ps
   ```

   `db` e `app` devem aparecer em execução; `migrate` aparece encerrado (ele aplica as
   migrações e termina).

## 1.7 Publicar o console na sua rede privada

**Onde:** no **servidor**; o teste final, no navegador do seu computador.

**Por quê:** `tailscale serve` recebe as conexões da sua rede privada, cuida do HTTPS e
repassa para o console em `127.0.0.1:8000`, informando quem é você. É assim que o
console sabe que o acesso é seu.

```bash
sudo tailscale serve --bg 8000
tailscale serve status
```

O endereço que aparece (`https://assistente.<sua-rede>.ts.net`) é o do console. Coloque-o
em `PUBLIC_BASE_URL` no `.env` e rode `docker compose up -d` de novo.

Teste: abra o endereço no computador com Tailscale ligado. Deve aparecer o console.
Desligue o Tailscale e tente de novo: não deve abrir. É assim que tem que ser.

## Atualizar depois

**Onde:** no **servidor**.

```bash
cd ~/AI_kleverson && git pull && docker compose up -d --build
```
