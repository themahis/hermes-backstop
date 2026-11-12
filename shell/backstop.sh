# Backstop shell hook for bash and zsh. Source it from ~/.bashrc or ~/.zshrc (see install.sh).
#
# Wraps an opt-in list of build/test commands. The command runs exactly once; its output streams to
# your terminal as usual and is also kept in a per-shell temp file. When it exits with a real failure,
# the tail is written to <HERMES_HOME>/plugin-data/backstop/spool/<id>.json for the Hermes gateway.
#
# Trade-off: a wrapped command writes to a pipe, not a terminal, so it prints without colours.
# Piped, redirected and interactive commands run untouched.

case $- in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac   # interactive shells only

__backstop_home() {
    if [ -n "$HERMES_HOME" ]; then
        if command -v cygpath >/dev/null 2>&1; then cygpath -u "$HERMES_HOME"; else printf '%s\n' "$HERMES_HOME"; fi
    elif [ -n "$LOCALAPPDATA" ] && command -v cygpath >/dev/null 2>&1; then   # Git Bash / MSYS / Cygwin
        printf '%s/hermes\n' "$(cygpath -u "$LOCALAPPDATA")"
    else
        printf '%s/.hermes\n' "$HOME"
    fi
}

BACKSTOP_DATA="$(__backstop_home)/plugin-data/backstop"
BACKSTOP_DEFAULT="npm npx pnpm yarn bun deno tsc node python python3 py pytest cargo go dotnet make gradle mvn eslint jest vitest ruff mypy"
BACKSTOP_WRAPPED=""
[ -n "$ZSH_VERSION" ] && zmodload zsh/datetime 2>/dev/null   # $EPOCHREALTIME

if command -v sha1sum >/dev/null 2>&1; then __backstop_sha1() { sha1sum; }
elif command -v shasum >/dev/null 2>&1; then __backstop_sha1() { shasum -a 1; }
else __backstop_sha1() { openssl sha1 -r; }
fi

# The directory as the gateway sees it (a Windows path under Git Bash).
__backstop_cwd() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -w "$PWD"; else printf '%s\n' "$PWD"; fi
}

# Same key as the PowerShell hook: first 8 bytes of sha1(lower(cwd) + "|" + command).
__backstop_key() {
    printf '%s|%s' "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" "$2" | __backstop_sha1 | cut -c1-16
}

__backstop_ms() {
    if [ -n "$EPOCHREALTIME" ]; then
        local t="${EPOCHREALTIME/[.,]/}"
        printf '%s\n' "${t%???}"
    else
        printf '%s000\n' "$(date +%s)"
    fi
}

# stdin -> one JSON string literal (ANSI colour codes and other control characters dropped).
__backstop_json() {
    local esc tab
    esc="$(printf '\033')"
    tab="$(printf '\t')"
    LC_ALL=C sed -e "s/${esc}\[[0-9;?]*[A-Za-z]//g" \
        | LC_ALL=C tr -d '\000-\010\013\014\015\016-\037' \
        | { if command -v iconv >/dev/null 2>&1; then iconv -c -f UTF-8 -t UTF-8 2>/dev/null; else cat; fi; } \
        | LC_ALL=C sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e "s/${tab}/\\\\t/g" \
        | LC_ALL=C awk 'BEGIN { printf "\"" } { printf "%s%s", (NR > 1 ? "\\n" : ""), $0 } END { printf "\"" }'
}

# __backstop_write <json body without braces> -> prints the record id. Atomic: temp name, then rename.
__backstop_write() {
    local spool="$BACKSTOP_DATA/spool" id shellname
    (umask 077; mkdir -p "$spool") || return 1
    local ms; ms="$(__backstop_ms)"
    id="$(date +%Y%m%d%H%M%S)${ms#"${ms%???}"}-$(printf '%s' "$$|$ms|$1" | __backstop_sha1 | cut -c1-8)"
    if [ -n "$ZSH_VERSION" ]; then shellname="zsh $ZSH_VERSION"; else shellname="bash ${BASH_VERSION%%[(]*}"; fi
    (umask 077; printf '{"v": 1, "id": "%s", "ts": %s, "shell": "%s", %s}\n' "$id" "$(date +%s)" "$shellname" "$1" \
        > "$spool/.$id.tmp") && mv -f "$spool/.$id.tmp" "$spool/$id.json" && printf '%s\n' "$id"
}

__backstop_tty() { [ -t 0 ] && [ -t 1 ]; }   # tests override this

__backstop_interactive() {
    local name="$1" first="$2" a
    shift
    if [ $# -eq 0 ]; then
        case "$name" in node|python|python3|py|deno|bun) return 0 ;; esac   # REPL
    fi
    case "$first" in -i|--interactive|repl|login|adduser|init|create|publish|shell) return 0 ;; esac
    for a in "$@"; do
        case "$a" in --watch|-w|watch) return 0 ;; esac
        case "$name:$first:$a" in
            npm:run:dev|npm:run:start|npm:run:serve|npm:run:watch|npm:start:*|\
            pnpm:run:dev|pnpm:run:start|pnpm:run:serve|pnpm:start:*|pnpm:dev:*|\
            yarn:run:dev|yarn:run:start|yarn:run:serve|yarn:start:*|yarn:dev:*|\
            bun:run:dev|bun:run:start|bun:run:serve|bun:dev:*) return 0 ;;     # dev servers
        esac
    done
    [ "$name:$first" = "npm:start" ] && return 0
    return 1
}

__backstop_run() {
    local name="$1" code start cwd cmd marker failed id body out
    shift
    # Piped, redirected, captured ($(...)) or interactive: run untouched.
    if [ "$BACKSTOP_DISABLE" = "1" ] || ! __backstop_tty || __backstop_interactive "$name" "$@"; then
        command "$name" "$@"
        return
    fi
    # Private capture file: unpredictable name, mode 0600 (mktemp), removed after the run.
    out="$(mktemp "${TMPDIR:-/tmp}/backstop.XXXXXXXX")" || { command "$name" "$@"; return; }
    start="$(__backstop_ms)"
    command "$name" "$@" 2>&1 | tee "$out"
    code="${PIPESTATUS[0]-}${pipestatus[1]-}"   # bash / zsh: the command's exit, not tee's
    cwd="$(__backstop_cwd)"
    cmd="$name${*:+ $*}"
    marker="$BACKSTOP_DATA/open/$(__backstop_key "$cwd" "$cmd").txt"
    body="\"cwd\": $(printf '%s' "$cwd" | __backstop_json), \"command\": $(printf '%s' "$cmd" | __backstop_json), \"exit\": $code, \"duration_ms\": $(( $(__backstop_ms) - start ))"
    if [ "$code" = 0 ]; then
        # Same command, same directory, now passing: close the open incident (if any).
        if [ -s "$marker" ]; then
            failed="$(cat "$marker")"
            if __backstop_write "\"kind\": \"resolved\", \"resolves\": \"$failed\", $body" >/dev/null; then
                : > "$marker"; rm -f "$marker"
                printf '\033[32m[backstop] passes now - incident closed\033[0m\n' >&2
            fi
        fi
    elif [ "$code" != 130 ] && [ "$code" != 143 ]; then   # Ctrl-C / SIGTERM are not failures
        if id="$(__backstop_write "$body, \"output\": $(tail -n 2000 "$out" | tail -c 64000 | __backstop_json)")"; then
            mkdir -p "$BACKSTOP_DATA/open" && printf '%s' "$id" > "$marker"
            printf '\033[36m[backstop] exit %s - sent to Hermes for a read-only diagnosis\033[0m\n' "$code" >&2
        else
            printf '[backstop] could not record this run\n' >&2
        fi
    fi
    rm -f "$out"
    return "$code"
}

backstop_status() {
    printf 'Spool:    %s\nWrapped:  %s\nDisabled: %s\n' "$BACKSTOP_DATA/spool" "${BACKSTOP_WRAPPED# }" "${BACKSTOP_DISABLE:-0}"
}

backstop_disable() {
    local n
    for n in $(printf '%s' "$BACKSTOP_WRAPPED"); do unset -f "$n"; done
    BACKSTOP_WRAPPED=""
}

__backstop_install() {
    local n
    while IFS= read -r n; do
        [ -n "$n" ] || continue
        alias "$n" >/dev/null 2>&1 && continue            # leave user aliases alone
        command -v "$n" >/dev/null 2>&1 || continue       # not installed
        eval "function $n { __backstop_run $n \"\$@\"; }"
        BACKSTOP_WRAPPED="$BACKSTOP_WRAPPED $n"
    done <<EOF
$(printf '%s\n' "${BACKSTOP_COMMANDS:-$BACKSTOP_DEFAULT}" | tr ',; ' '\n\n\n')
EOF
}
__backstop_install
