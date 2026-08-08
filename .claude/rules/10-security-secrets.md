---
description: Credential, token and container-privilege handling. Non-negotiable.
---

# Security & Secrets (MUST)

## Credentials

- Blink username, password and 2FA code are add-on **options**, declared under
  `options:`/`schema:` in `config.yaml` and delivered to the container at
  runtime as `/data/options.json`. Never hardcode them, never commit a `.env`
  with real values, and **never log the password, the 2FA code or a session
  token — not even at DEBUG level**.
- `config.py` reads `/data/options.json` once at startup into a typed `Config`.
  Nothing else touches that file.
- The persisted blinkpy session (tokens, `refresh_token`, `hardware_id`) lives
  under `/data/`, never in the image and never in git. Write it `0600`.
- A cached session belongs to one account: if the configured username differs
  from the cached one, discard the cache. Otherwise the stored `refresh_token`
  silently re-authenticates the *previous* account.
- If `hassio_api: true` is ever added, treat `SUPERVISOR_TOKEN` exactly like the
  Blink password: never logged, never persisted outside memory.

## config.yaml privilege minimisation

Supervisor computes a security rating (1-6, starting at 5) from these keys.
Any deviation from the safe side MUST be justified in `DOCS.md`.

| Key | Effect on rating |
|-----|------------------|
| `ingress: true` | +2 |
| `auth_api: true` (overridden by ingress) | +1 |
| custom `apparmor.txt` | +1 |
| `apparmor: false` | -1 |
| `privileged:` NET_ADMIN / SYS_ADMIN / SYS_RAWIO / SYS_PTRACE / SYS_MODULE / DAC_READ_SEARCH, or `kernel_modules:` | -1 |
| `hassio_role: manager` | -1 |
| `host_network: true` | -1 |
| `hassio_role: admin` | -2 |
| `host_pid: true` | -2 |
| `full_access: true` or `docker_api: true` | forced to 1 |

This add-on needs **none** of `privileged`, `full_access`, `docker_api`,
`host_network`, `host_pid`, or an elevated `hassio_role`. It makes outbound
connections to Blink's cloud and serves a local TCP/RTSP port — nothing else.
If a change appears to need one of these keys, that is a design smell: flag it,
do not add the key.

Ship a custom `apparmor.txt` rather than setting `apparmor: false`.

## Protected files

`.env`, `*.secrets*`, `/data/options.json`, any credential dump and any file
holding a real Blink account are blocked by the `protect-files` PreToolUse hook
in `.claude/settings.json`, and denied in `permissions.deny`. Do not work around
either — fix the design instead.

`.gitignore` MUST contain at least: `.env`, `.env.*`, `*.local`, `options.json`,
`.secrets.baseline` artifacts. `uv.lock` is the deliberate exception: **commit
it** (see `50-git-hygiene.md`).

## Supply chain

- Dependencies are locked in `uv.lock`, committed. The Docker build runs
  `uv sync --locked`, which fails closed on a stale lock instead of silently
  re-resolving.
- Third-party GitHub Actions are pinned to a full commit SHA with the tag as a
  trailing comment: `actions/checkout@<sha> # v7.0.1`. Enforced by `zizmor`,
  both as a pre-commit hook and in CI.
- A new dependency is a security decision: it needs the `security-reviewer`
  subagent's sign-off before merge.
