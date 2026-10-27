# 1. Servidor: Lightsail + Tailscale + Docker

Resultado: um servidor Linux na AWS, sem nenhuma porta aberta para a internet, com o
console acessível só pelos seus aparelhos em `https://assistente.<sua-rede>.ts.net`.

Tempo estimado: 40 minutos.

## 1.1 Tailscale nos seus aparelhos

1. No Linux Mint: `curl -fsSL https://tailscale.com/install.sh | sh` e depois `sudo tailscale up`.
   Abra o link que aparecer e entre com a conta que você criou.
2. No celular: instale o app Tailscale e entre com a mesma conta.
3. No painel do Tailscale (login.tailscale.com), em **DNS**, ative **MagicDNS** e **HTTPS Certificates**.

## 1.2 Criar a instância no Lightsail

1. Lightsail → **Create instance** → Linux/Unix → **OS Only** → **Ubuntu 24.04 LTS**.
2. Plano com **4 GB de RAM** (2 GB funciona no começo, mas fica apertado com o banco).
3. Nome: `assistente`. Crie.
4. Em **Snapshots**, ative **Automatic snapshots** (backup diário da máquina inteira).

## 1.3 Instalar Tailscale e Docker no servidor

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

No Lightsail → instância → **Networking**: apague **todas** as regras de IPv4 e IPv6,
inclusive a SSH (porta 22). Confirme que `ssh ubuntu@assistente` continua funcionando
pelo Tailscale. A partir daqui a máquina é invisível para a internet.

## 1.5 Baixar o código

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
