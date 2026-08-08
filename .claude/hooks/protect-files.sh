#!/bin/bash
# PreToolUse hook: block Edit/Write on credential-shaped paths before they happen.
# Exit 2 blocks the tool call and feeds stderr back to Claude.
# See .claude/rules/10-security-secrets.md
set -uo pipefail

INPUT=$(cat)
FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE_PATH" ] && exit 0

PROTECTED_PATTERNS=(".env" "options.json" ".git/" "credentials" ".secrets.baseline" ".blink_creds")

for pattern in "${PROTECTED_PATTERNS[@]}"; do
  case "$FILE_PATH" in
    *"$pattern"*)
      echo "Blocked: '$FILE_PATH' matches protected pattern '$pattern'." >&2
      echo "Secrets never live in the repository. See .claude/rules/10-security-secrets.md" >&2
      exit 2
      ;;
  esac
done

exit 0
