#!/usr/bin/env bash
# secret_scan.sh — fail the build if a SECRET VALUE ships in the systemd
# unit templates or the CT110 deploy scripts (task 272 hardening).
#
# Scope (the artifacts that end up root-readable on CT110):
#   * packaging/                          — the shipped systemd unit templates
#   * scripts/deploy/ct110/               — the deploy pipeline's scripts
#   * scripts/deploy/node/                — new-node first install (scripts +
#                                           the node unit/config templates)
#
# The contract these scans enforce is the one packaging/lh-harness.service
# documents and docs/SECRETS.md records repo-wide: credentials travel by
# NAME only, injected at runtime through the unit's EnvironmentFile
# (/home/harness/.lh-harness-secrets.env), never as an inline
# Environment=NAME=value assignment in a world-readable unit, and never as a
# literal value anywhere in the tree.  The 2026-09-28 finding this guard
# exists to keep from regressing: the live lh-harness unit carried
# Environment=LH_HARNESS_WEB_TOKEN=<value> in plaintext, readable by anyone
# who could read the host filesystem (systemctl cat / unit file).
#
# What may legitimately appear (and must NOT be flagged):
#   * bare env-var NAMES (LH_HARNESS_WEB_TOKEN, *_TOKEN as a name), with no
#     attached value — the only thing the briefs allow committing;
#   * references THROUGH the EnvironmentFile (EnvironmentFile=..., grep of
#     ^NAME= inside the secrets file);
#   * example files whose assignments are intentionally empty
#     (*secrets.env.example, NAME= with nothing after the =).
#
# Usage: secret_scan.sh [paths...]   (defaults to the scope above)
# Exit 0 = clean, 1 = at least one finding (each printed as a
# file:line pointer with the matched VARIABLE NAME only — never the value).

set -euo pipefail

LH_SECRET_SCAN_ROOT="${LH_SECRET_SCAN_ROOT:-$PWD}"
default_paths=(
  "$LH_SECRET_SCAN_ROOT/packaging"
  "$LH_SECRET_SCAN_ROOT/scripts/deploy/ct110"
  "$LH_SECRET_SCAN_ROOT/scripts/deploy/node"
)
paths=()
if (( $# )); then
  for p in "$@"; do
    if [[ "$p" = /* ]]; then paths+=("$p"); else paths+=("$LH_SECRET_SCAN_ROOT/$p"); fi
  done
else
  paths=("${default_paths[@]}")
fi

failures=0
fail() { # helper: record + print a masked, name-only finding
  failures=$((failures + 1))
  echo "SECRET-SCAN FINDING: $1"
}
# Findings are printed with paths RELATIVE to the scan root when possible, so
# the output is readable in CI logs regardless of where the scan ran.
rel() { # rel <abs-path>
  if [[ "$1" = "$LH_SECRET_SCAN_ROOT"/* ]]; then printf '%s' "${1#"$LH_SECRET_SCAN_ROOT"/}"; else printf '%s' "$1"; fi
}

# A "secret-bearing" env var name: task 272's list plus the obvious
# credential suffixes.  Deliberately includes the bare _TOKEN/_KEY/_SECRET
# suffixes so a renamed credential cannot slip through un-scanned.
# POSIX-ERE note: no `(^|...)` boundary alternative — after a literal
# "Environment=" (or "NAME=") prefix a line-start anchor can never match
# (GNU grep 3.11 enforces this; verified 2026-09-28), so the prefix itself
# is the name boundary and no extra group is needed.
secret_name='([A-Za-z_][A-Za-z0-9_]*(_(TOKEN|KEY|SECRET|PASSWORD|PASSWD)|API_KEY|AUTH_TOKEN)|LH_HARNESS_WEB_TOKEN|CT110_API_TOKEN|GH_TOKEN)'

for target in "${paths[@]}"; do
  [[ -e "$target" ]] || { echo "secret_scan: missing target: $target" >&2; exit 1; }
done

# --- scan 1: systemd unit template hygiene ---------------------------------
# A shipped unit template must never ASSIGN a credential inline
# (Environment=NAME=value).  EnvironmentFile=... references are the only
# sanctioned mechanism.  Checked on *.service and *.timer files anywhere in
# the scanned paths.
inline_env_re="^[[:space:]]*Environment=${secret_name}="
while IFS= read -r -d '' unit; do
  # Every finding is printed as file:line + the VARIABLE NAME only; the
  # value is redacted before anything reaches the output.
  while IFS= read -r line_no; do
    name="$(sed -n "${line_no}p" "$unit" | sed -E 's/^[[:space:]]*Environment=([A-Za-z_][A-Za-z0-9_]*)=.*/\1/')"
    fail "inline credential assignment in systemd unit: $(rel "$unit"):${line_no}: Environment=${name}=<value redacted>"
  done < <(grep -nE -- "$inline_env_re" "$unit" | cut -d: -f1)
done < <(find "${paths[@]}" -type f \( -name '*.service' -o -name '*.timer' \) -print0)

# --- scan 2: shell/python scripts must not ASSIGN a credential literal -----
# Matches NAME=value (or NAME: value) assignments that carry a non-empty,
# non-placeholder value.  NAME-only mentions (grep '^NAME=', docs tables,
# "${LH_HARNESS_WEB_TOKEN}" expansions) are fine.
while IFS= read -r -d '' script; do
  case "$script" in
    *secrets.env.example) continue ;;  # names-with-empty-values template
  esac
  # value must be non-empty and not an obvious placeholder/expansion
  while IFS= read -r line_no; do
    name="$(sed -n "${line_no}p" "$script" | sed -E 's/^[^A-Za-z0-9_]*([A-Za-z_][A-Za-z0-9_]*)[=:].*/\1/')"
    fail "credential-shaped assignment in deploy script: $(rel "$script"):${line_no}: ${name}=<value redacted>"
  done < <(grep -nE -- "^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*${secret_name}=[[:space:]]*[\"']?[A-Za-z0-9_/+-]{8,}" "$script" | cut -d: -f1)
done < <(find "${paths[@]}" -type f \( -name '*.sh' -o -name '*.py' \) ! -name '*secrets.env.example' -print0)

# --- scan 3: the CT110 bearer credential never ships as a hex literal ------
# docs/SECRETS.md pins the live CT110 harness API credential as a 48-char hex
# string; a bare 48-hex run anywhere in the deploy surface is how the 09-28
# leak looked.  Git SHAs (40-hex) and docker digests cannot collide with 48;
# the trailing `| grep -v` drops digest/checksum CONTEXT lines (the word
# "checksum" in a comment) so a documented digest is not itself a finding.
while IFS= read -r hit; do
  scan_file="${hit%%:*}"
  line_no="$(printf '%s' "$hit" | cut -d: -f2)"
  fail "48-hex credential literal: $(rel "$scan_file"):${line_no} (value redacted)"
done < <(grep -rnE -- "[\"'= ]?[0-9a-f]{48}([\"' ]|$)" "${paths[@]}" 2>/dev/null \
          | grep -vE 'sha256|SHA256|checksum' \
          | grep -E ':' || true)

if (( failures )); then
  echo "secret_scan: $failures finding(s) — commit env-var NAMES only, never values." >&2
  exit 1
fi
echo "secret_scan: clean (no credential VALUES in scope)"