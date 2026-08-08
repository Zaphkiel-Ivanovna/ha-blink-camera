---
name: check
description: >
  Run the full local check suite before committing or opening a PR: ruff check,
  ruff format --check, mypy strict, pytest, then pre-commit on all files. Use
  when asked to "run checks", "verify everything passes", "is it clean", or
  before any commit, PR or release.
allowed-tools: Bash(uv run:*), Bash(pre-commit run:*), Read
---

# Full local check suite

Run every gate CI runs, in CI's order, and stop at the first failure. Do not
silently auto-fix: `ruff --fix` is already wired into the pre-commit hook, and
anything else is a decision the user makes.

## Steps

Run these in order, from the repository root:

1. `uv run ruff check .`
2. `uv run ruff format --check .`
3. `uv run mypy`
4. `uv run pytest -q`
5. `pre-commit run --all-files` — covers yamllint, shellcheck, codespell,
   detect-secrets, zizmor and the file hygiene hooks.

## Failure handling

Stop at the first failing step. Report its **exact output**, not a paraphrase,
then state which rule file governs the failure so the fix is obvious:

| Failing tool | Rule |
|---|---|
| ruff (complexity, C90/PLR) | `.claude/rules/00-code-structure.md` |
| mypy | `.claude/rules/30-python-async.md` |
| pytest | `.claude/rules/40-testing.md` |
| yamllint / shellcheck | `.claude/rules/20-addon-packaging.md` |
| detect-secrets / zizmor | `.claude/rules/10-security-secrets.md` |

If the environment is not set up (`uv` missing, no `.venv`), say so plainly and
stop — do not attempt to install anything.

## Output

Finish with a one-line PASS/FAIL per tool, then a single verdict line. Example:

```
ruff check      PASS
ruff format     PASS
mypy            FAIL  (3 errors in src/ha_blink_camera/stream_relay.py)
pytest          not run
pre-commit      not run

FAIL — fix mypy first, see .claude/rules/30-python-async.md
```
