#!/bin/sh
# Adds (or with --uninstall removes) one line in ~/.bashrc and/or ~/.zshrc that loads the Backstop hook.
#   sh install.sh            # the rc file of your login shell ($SHELL)
#   sh install.sh bash zsh   # specific shells
#   sh install.sh --uninstall
set -eu

HOOK="$(cd "$(dirname "$0")" && pwd)/backstop.sh"
LINE=". \"$HOOK\"  # backstop"
UNINSTALL=0
SHELLS=""
for arg in "$@"; do
    case "$arg" in
        --uninstall|-u) UNINSTALL=1 ;;
        bash|zsh) SHELLS="$SHELLS $arg" ;;
        *) echo "usage: sh install.sh [bash] [zsh] [--uninstall]" >&2; exit 2 ;;
    esac
done
[ -n "$SHELLS" ] || SHELLS="$(basename "${SHELL:-bash}")"

for sh in $SHELLS; do
    case "$sh" in
        zsh) rc="${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash) rc="$HOME/.bashrc" ;;
        *) echo "Backstop supports bash and zsh, not $sh. Pass one: sh install.sh bash" >&2; exit 2 ;;
    esac
    if [ "$UNINSTALL" = 1 ]; then
        if [ -f "$rc" ]; then
            grep -v '# backstop$' "$rc" > "$rc.backstop-tmp" || true
            cat "$rc.backstop-tmp" > "$rc" && rm -f "$rc.backstop-tmp"
        fi
        echo "Backstop removed from $rc (open a new terminal)."
    else
        touch "$rc"
        grep -q '# backstop$' "$rc" || printf '%s\n' "$LINE" >> "$rc"
        echo "Backstop added to $rc. Open a new terminal."
    fi
done
