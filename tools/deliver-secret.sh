#!/usr/bin/env bash
# Deliver one or more secrets to CT110's harness secrets file, typed at a prompt on YOUR terminal.
# Usage (PowerShell):  & "C:\Program Files\Git\bin\bash.exe" /c/tmp/deliver-secret.sh NAME [NAME...]
# Names ending in PASSWORD, KEY, SECRET or TOKEN are read silently. Values never appear on a command line.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: deliver-secret.sh NAME [NAME...]"; exit 2; }
for n in "$@"; do [[ "$n" =~ ^[A-Z][A-Z0-9_]*$ ]] || { echo "bad variable name: $n"; exit 2; }; done
HOST=${DELIVER_HOST:-root@192.168.21.151}
REMOTE=$(cat <<'EOS'
set -eu
F=${SECRETS_FILE:-/home/harness/.lh-harness-secrets.env}
[ -f "$F" ] || { echo "STOP: $F not found"; exit 1; }
for n in $NAMES; do
  if grep -q "^$n=" "$F"; then echo "STOP: $n is already in the secrets file - nothing changed"; exit 1; fi
done
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
for n in $NAMES; do
  case "$n" in
    *PASSWORD*|*KEY*|*SECRET*|*TOKEN*) printf '%s (hidden): ' "$n"; IFS= read -rs v; echo;;
    *) printf '%s: ' "$n"; IFS= read -r v;;
  esac
  [ -n "$v" ] || { echo "STOP: empty value for $n - nothing changed"; exit 1; }
  printf '%s=%s\n' "$n" "$v" >> "$TMP"
done
BAK="$F.bak-$(date +%Y%m%d-%H%M%S)"; cp -p "$F" "$BAK"
[ -z "$(tail -c1 "$F")" ] || echo >> "$F"
cat "$TMP" >> "$F"
chown --reference="$BAK" "$F"; chmod 600 "$F"
echo "backup: $BAK"
for n in $NAMES; do echo "$n lines: $(grep -c "^$n=" "$F")"; done
ls -l "$F" | awk '{print $1,$3,$4}'
rm -f '/root/;' 2>/dev/null || true
EOS
)
B64=$(printf '%s' "$REMOTE" | base64 -w0)
if [ "${DELIVER_LOCAL_TEST:-}" = 1 ]; then NAMES="$*" SECRETS_FILE="$SECRETS_FILE" bash -c "$(echo "$B64" | base64 -d)"; exit $?; fi
ssh -t "$HOST" "pct exec 110 -- env NAMES='$*' bash -c \"\$(echo $B64 | base64 -d)\""
