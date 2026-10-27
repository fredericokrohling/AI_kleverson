# 2. Google: projeto, APIs e login (OAuth)

Resultado: o assistente lê seu Gmail, sua agenda e seus arquivos do Drive, cria rascunhos
e eventos, e **não consegue enviar e-mail** (essa permissão não é pedida).

Tempo estimado: 30 minutos. Pré-requisito: o console já abre em `https://assistente.<sua-rede>.ts.net`.

## Por que OAuth

OAuth é o jeito oficial de um sistema acessar sua conta Google **sem conhecer sua senha**.
Você autoriza no site do Google, escolhe as permissões e pode revogar quando quiser em
myaccount.google.com/permissions. O Google entrega ao sistema um "refresh token", que fica
criptografado no seu banco. Detalhes em [docs/aprendizado/03-seguranca.md](../aprendizado/03-seguranca.md).

## 2.1 Projeto e APIs

**Por quê:** o Google exige que todo acesso por API pertença a um projeto do Google Cloud,
e cada API precisa ser ligada explicitamente. Ligamos só as três que usamos.

1. Acesse console.cloud.google.com com a conta Gmail que o assistente vai ler.
2. Crie um projeto chamado `assistente-pm`.
3. Em **APIs e serviços → Biblioteca**, ative: **Gmail API**, **Google Calendar API**, **Google Drive API**.

## 2.2 Tela de consentimento

**Por quê:** é a tela que o Google mostra no login, listando o que o app pode fazer. Os
escopos abaixo são o mínimo necessário. **`gmail.send` fica de fora de propósito**: sem
ele, o sistema não consegue enviar e-mail, nem por bug.

Em **Google Auth Platform** (ou **Tela de consentimento OAuth**, conforme a versão do console):

1. Tipo de usuário: **Externo** (Gmail pessoal). Na conta da empresa, depois, será **Interno**.
2. Nome do app: `Assistente PM`. E-mails de suporte e contato: o seu.
3. **Acesso a dados / Escopos**: adicione
   - `.../auth/userinfo.email` e `openid`
   - `.../auth/gmail.readonly` e `.../auth/gmail.compose`
   - `.../auth/calendar.events`
   - `.../auth/drive.readonly`
4. **Público**: clique em **Publicar app** para ir de "Teste" para **Em produção**.
   Não peça verificação. Isso é o que impede o login de expirar a cada 7 dias.

## 2.3 Cliente OAuth

**Por quê:** o cliente é a identidade do seu app perante o Google. O endereço de
redirecionamento precisa ser exatamente igual ao do console; o Google recusa qualquer
outro, o que impede alguém de desviar o login para um site falso.

1. **Clientes → Criar cliente** → tipo **Aplicativo da Web**.
2. **URIs de redirecionamento autorizados**: `https://assistente.<sua-rede>.ts.net/auth/google/callback`
3. Copie o **ID do cliente** e a **chave secreta** para `GOOGLE_CLIENT_ID` e
   `GOOGLE_CLIENT_SECRET` no `.env` do servidor. Rode `docker compose up -d`.

## 2.4 Conectar

**Por quê o teste no servidor:** ele usa o token gravado (sem você no navegador), que é
exatamente o que os coletores de e-mail e agenda vão fazer.

1. No console, clique em **Conectar conta Google**.
2. O Google vai mostrar "**O Google não verificou este app**". É esperado: o app é seu.
   Clique em **Avançado → Acessar Assistente PM (não seguro)**.
3. Marque **todas** as permissões. Se faltar alguma, o console avisa qual.
4. De volta ao console, sua conta aparece em "Contas Google conectadas".

Teste no servidor:

```bash
docker compose run --rm app python -m app.cli testar-google
```

Deve mostrar o total de mensagens do Gmail e os próximos eventos da agenda.

## O que o aviso de app não verificado significa

Só que o Google não auditou o app. Ele vale para até 100 usuários, o que basta. O token
fica criptografado no seu banco, na sua máquina. Se um dia o assistente virar produto
para outras pessoas, aí sim será preciso a verificação formal do Google.
