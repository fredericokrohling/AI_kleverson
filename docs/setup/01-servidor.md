# 1. Servidor: Lightsail + Tailscale + Docker

Resultado: um servidor Linux na AWS, sem nenhuma porta aberta para a internet, com o
console acessível só pelos seus aparelhos em `https://assistente.<sua-rede>.ts.net`.

Tempo estimado: 40 minutos.

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

**Por quê:** cada aparelho que vai acessar o console precisa estar na sua rede Tailscale.
MagicDNS dá um nome fixo ao servidor; HTTPS Certificates permite o cadeado no navegador,
que o login do Google exige.

1. No Linux Mint: `curl -fsSL https://tailscale.com/install.sh | sh` e depois `sudo tailscale up`.
   Abra o link que aparecer e entre com a conta que você criou.
2. No celular: instale o app Tailscale e entre com a mesma conta.
3. No painel do Tailscale (login.tailscale.com), em **DNS**, ative **MagicDNS** e **HTTPS Certificates**.

## 1.2 Criar a instância no Lightsail

**Por quê:** Ubuntu LTS tem atualizações de segurança por 5 anos. 4 GB de RAM acomodam
o Postgres, o console e, mais tarde, a transcrição sem travar.

1. Lightsail → **Create instance** → Linux/Unix → **OS Only** → **Ubuntu 24.04 LTS**.
2. Plano com **4 GB de RAM** (2 GB funciona no começo, mas fica apertado com o banco).
3. Nome: `assistente`. Crie.
4. Em **Snapshots**, ative **Automatic snapshots** (backup diário da máquina inteira).

## 1.3 Instalar Tailscale e Docker no servidor

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

**Por quê:** este é o passo que torna o servidor invisível. Faça só depois de confirmar
que o acesso pelo Tailscale funciona, senão você perde o acesso à máquina.

No Lightsail → instância → **Networking**: apague **todas** as regras de IPv4 e IPv6,
inclusive a SSH (porta 22). Confirme que `ssh ubuntu@assistente` continua funcionando
pelo Tailscale. A partir daqui a máquina é invisível para a internet.

## 1.5 Baixar o código

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

**Por quê:** o `.env` guarda senhas e chaves fora do código. `chmod 600` faz só o dono do
arquivo conseguir lê-lo. A chave gerada por `gerar-chave` é a que criptografa os tokens
do Google no banco.

```bash
cp .env.example .env
chmod 600 .env
nano .env   # preencha banco, SESSION_SECRET e ALLOWED_TAILSCALE_LOGIN
docker compose build
docker compose run --rm app python -m app.cli gerar-chave   # cole em TOKEN_ENCRYPTION_KEY
docker compose up -d
```

Para gerar senha e `SESSION_SECRET`: `openssl rand -base64 32`.
`ALLOWED_TAILSCALE_LOGIN` é o e-mail que aparece no painel do Tailscale como dono da rede.

## 1.7 Publicar o console na sua rede privada

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

```bash
cd ~/AI_kleverson && git pull && docker compose up -d --build
```
