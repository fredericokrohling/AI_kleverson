#!/usr/bin/env bash
# Atende os links kleverson://<ação> da página Saúde do console.
#
# Roda no SEU computador (Linux Mint), nunca no servidor. O navegador entrega o link
# inteiro (ex.: "kleverson://reiniciar") como primeiro argumento; este script abre um
# terminal visível, conecta ao servidor por SSH (pelo Tailscale) e roda o comando da ação.
#
# Segurança:
# - Só aceita as ações da lista abaixo. O texto do link nunca vira comando: ele só
#   escolhe uma linha fixa da lista. Um site malicioso que mandasse
#   "kleverson://rm -rf" cairia em "ação desconhecida".
# - Ações que mudam o servidor (reiniciar, atualizar) pedem confirmação no terminal.
# - O SSH usa a sua chave e o seu login do Tailscale; o console web não ganha poder nenhum.

# Para no primeiro erro, em variável não definida e em erro dentro de "|".
set -euo pipefail

# Servidor e pasta do projeto: lidos do arquivo de configuração criado pelo instalador.
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/kleverson/terminal.conf"
# Valores padrão (os do guia 01-servidor.md).
SERVIDOR="ubuntu@assistente"
PASTA="AI_kleverson"
# Se o arquivo existir, sobrescreve os padrões.
if [ -f "$CONFIG" ]; then
  # shellcheck source=/dev/null
  . "$CONFIG"
fi

# Ação: o que vem depois de "kleverson://", sem barras nem parâmetros.
ACAO="${1#kleverson://}"
ACAO="${ACAO%%/*}"
ACAO="${ACAO%%\?*}"

# Pergunta antes de rodar (usada nas ações que mudam o servidor).
CONFIRMAR='{ read -r -p "Confirma? Digite s e Enter: " r; [ "$r" = "s" ] || { echo "Cancelado."; exit 0; }; }'

# Comando remoto de cada ação. Qualquer outra ação é recusada.
case "$ACAO" in
  terminal)
    # Sessão interativa, já na pasta do projeto.
    REMOTO="cd ~/$PASTA && exec bash -l"
    TITULO="Servidor (terminal)";;
  status)
    REMOTO="cd ~/$PASTA && docker compose ps"
    TITULO="Serviços";;
  logs)
    REMOTO="cd ~/$PASTA && docker compose logs --tail 100 app coletor"
    TITULO="Logs";;
  reiniciar)
    REMOTO="cd ~/$PASTA && echo 'Reiniciar todos os serviços (o console fica fora do ar por alguns segundos).' && $CONFIRMAR && docker compose restart && docker compose ps"
    TITULO="Reiniciar serviços";;
  atualizar)
    REMOTO="cd ~/$PASTA && git fetch -q && echo 'Commits novos que serão aplicados:' && git log --oneline HEAD..origin/main && $CONFIRMAR && git pull --ff-only && docker compose up -d --build && docker compose ps"
    TITULO="Aplicar atualização";;
  *)
    # Mostra o aviso numa janela, porque não há terminal aberto ainda.
    notify-send "Kleverson A.I." "Ação desconhecida: $ACAO" 2>/dev/null || echo "Ação desconhecida: $ACAO" >&2
    exit 1;;
esac

# O que roda dentro do terminal: SSH com terminal interativo (-t, para a confirmação
# funcionar) e, no fim, espera um Enter para você ler o resultado antes de fechar.
# printf %q protege cada parte com aspas, para nada do texto virar comando local.
DENTRO=$(printf 'echo %q; ssh -t %q %q; echo; read -r -p %q' \
  "== $TITULO ==" "$SERVIDOR" "$REMOTO" "Enter para fechar.")

# Abre o terminal padrão do sistema (no Mint, x-terminal-emulator aponta para ele).
if command -v x-terminal-emulator >/dev/null; then
  exec x-terminal-emulator -e bash -c "$DENTRO"
fi
# Alternativa: o terminal do GNOME/Cinnamon.
exec gnome-terminal -- bash -c "$DENTRO"
