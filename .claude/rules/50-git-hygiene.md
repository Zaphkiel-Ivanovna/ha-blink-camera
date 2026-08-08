---
description: Commit conventions, versioning and the local pre-commit gate.
---

# Git Hygiene

## Commits

- Conventional commits: `type(scope): summary`, with `feat`, `fix`, `docs`,
  `chore`, `refactor`, `test`, `ci`.
- Update `CHANGELOG.md` (keepachangelog.com format) with any user-visible change,
  in the same commit.
- Never commit on `main` directly — branch first. Enforced by the
  `no-commit-to-branch` pre-commit hook.

## Versioning

- `version` in `config.yaml` is the **sole** update trigger for Supervisor. Bump
  it on every release. If `image:` is set, it MUST equal the pushed Docker tag.
- Use `breaking_versions:` in `config.yaml` to force a manual update when a
  release requires user action, such as a config schema change.

## Never commit

`.env`, real credentials, `/data/options.json`, large binaries.
**Do** commit `uv.lock` — it is the reproducibility anchor, not a secret.

## Pre-commit

`.pre-commit-config.yaml` is committed and mirrors CI. Verify the `rev` pins are
still current before first use:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.1
    hooks:
      - id: ruff-check
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/adrienverge/yamllint
    rev: v1.38.0
    hooks: [{id: yamllint}]
  - repo: https://github.com/shellcheck-py/shellcheck-py
    rev: v0.11.0.1
    hooks: [{id: shellcheck}]
  - repo: https://github.com/codespell-project/codespell
    rev: v2.4.3
    hooks: [{id: codespell}]
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v6.0.0
    hooks:
      - id: end-of-file-fixer
      - id: trailing-whitespace
      - id: check-yaml
      - id: check-json
      - id: check-added-large-files
      - id: check-merge-conflict
      - id: detect-private-key
      - id: no-commit-to-branch
        args: [--branch=main]
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
        args: ["--baseline", ".secrets.baseline"]
  - repo: https://github.com/zizmorcore/zizmor-pre-commit
    rev: v1.28.0
    hooks:
      - id: zizmor
        args: [--pedantic]
```

`mypy` runs as a `repo: local` hook, deliberately — not `mirrors-mypy`. It must
run inside the real project venv so it sees blinkpy's actual (missing) types
instead of needing duplicated `additional_dependencies`.

## Actions must be SHA-pinned

`actions/checkout@<full-sha> # v7.0.1`, never a bare tag. Checked by `zizmor`
in both pre-commit and CI.
