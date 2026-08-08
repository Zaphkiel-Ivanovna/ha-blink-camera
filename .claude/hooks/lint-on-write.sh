#!/bin/bash
# PostToolUse hook: lint the file that was just edited or written.
#
# Deliberately NON-BLOCKING: it reports through additionalContext and always
# exits 0, so a transient lint error mid-edit does not halt work. The hard gate
# is the /check skill and CI. To make it blocking, emit
# {"decision":"block","reason":...} instead of the hookSpecificOutput below.
set -uo pipefail

INPUT=$(cat)
FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE_PATH" ] && exit 0
[ -f "$FILE_PATH" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

MSG=""
EXIT_CODE=0

case "$FILE_PATH" in
  *.py)
    command -v uv >/dev/null 2>&1 || exit 0
    # Stay silent until the project env exists: a missing tool is not a lint finding.
    uv run ruff --version >/dev/null 2>&1 || exit 0
    MSG=$(uv run ruff check "$FILE_PATH" 2>&1) || EXIT_CODE=$?
    ;;
  *.yaml | *.yml)
    command -v yamllint >/dev/null 2>&1 || exit 0
    MSG=$(yamllint "$FILE_PATH" 2>&1) || EXIT_CODE=$?
    ;;
  *.sh)
    command -v shellcheck >/dev/null 2>&1 || exit 0
    MSG=$(shellcheck "$FILE_PATH" 2>&1) || EXIT_CODE=$?
    ;;
  */rootfs/*)
    # S6 run/finish scripts carry no extension: shellcheck them if they look like shell.
    if head -n 1 "$FILE_PATH" | grep -qE '^#!.*(bash|bashio|sh)'; then
      command -v shellcheck >/dev/null 2>&1 || exit 0
      MSG=$(shellcheck "$FILE_PATH" 2>&1) || EXIT_CODE=$?
    else
      exit 0
    fi
    ;;
  *)
    exit 0
    ;;
esac

if [ "$EXIT_CODE" -ne 0 ] && [ -n "$MSG" ]; then
  jq -nc --arg msg "$MSG" \
    '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $msg}}'
fi

exit 0
