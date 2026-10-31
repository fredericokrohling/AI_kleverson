#!/usr/bin/env bash
# Instala, no seu computador (Linux Mint), o atendente dos links kleverson:// da página
# Saúde do console. Uso, de dentro desta pasta:
#   bash instalar.sh                        (servidor ubuntu@assistente, o padrão do guia)
#   bash instalar.sh usuario@outro-servidor (se o seu for diferente)
#
# O que faz (tudo só no seu usuário, sem sudo):
# 1. Copia kleverson-terminal.sh para ~/.local/bin.
# 2. Cria ~/.config/kleverson/terminal.conf com o servidor.
# 3. Cria um atalho (.desktop) que diz ao sistema: "links kleverson:// abrem este script".
# 4. Registra esse atalho como o responsável pelos links kleverson://.
#
# Para desinstalar: apague os três arquivos citados e rode
#   update-desktop-database ~/.local/share/applications

# Para no primeiro erro.
set -euo pipefail

# Servidor informado ou o padrão.
SERVIDOR="${1:-ubuntu@assistente}"
# Pasta deste script (para achar o kleverson-terminal.sh ao lado).
AQUI="$(cd "$(dirname "$0")" && pwd)"

# 1. Script no ~/.local/bin, executável.
mkdir -p "$HOME/.local/bin"
install -m 755 "$AQUI/kleverson-terminal.sh" "$HOME/.local/bin/kleverson-terminal"

# 2. Configuração com o servidor.
mkdir -p "$HOME/.config/kleverson"
printf 'SERVIDOR="%s"\nPASTA="AI_kleverson"\n' "$SERVIDOR" > "$HOME/.config/kleverson/terminal.conf"

# 3. Atalho que associa o tipo de link ao script. NoDisplay: não aparece no menu.
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/kleverson-terminal.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Kleverson A.I. (terminal)
Exec=$HOME/.local/bin/kleverson-terminal %u
MimeType=x-scheme-handler/kleverson;
NoDisplay=true
Terminal=false
DESKTOP

# 4. Registra o atalho como responsável pelos links kleverson://.
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
xdg-mime default kleverson-terminal.desktop x-scheme-handler/kleverson

# Confirmação.
echo "Pronto. Links kleverson:// agora abrem um terminal conectado a $SERVIDOR."
echo "Teste: xdg-open kleverson://status"
