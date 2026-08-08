# ha-blink-camera

## Purpose

A Home Assistant Supervisor add-on that authenticates to the Blink cloud via
`blinkpy` (>=0.25.9), opens a camera liveview session, and re-broadcasts the
resulting MPEG-TS stream on the LAN so Home Assistant / go2rtc / Frigate can
consume it as a normal camera.

This is **not** a `custom_components` integration. It is a Docker container
managed by Supervisor: `config.yaml` + `Dockerfile` + `rootfs/`.

## Architecture

```
Blink cloud (HTTPS + proprietary "immis" over TLS:443)
      │  blinkpy
      ▼
blink_client.py  ──patched by──  blinkpy_patches.py
      │  camera.init_livestream() -> BlinkLiveStream; .feed() as an asyncio task
      │  a duck-typed sink is appended to stream.clients (NOT stream.start())
      ▼
stream_relay.py  owns the one asyncio.Server, bound once for the process lifetime
      │  raw MPEG-TS over tcp://0.0.0.0:<PORT>   <- PORT stays constant across
      │                                             session renegotiations
      ▼
remux bridge (go2rtc `exec:` or MediaMTX `runOnInit`, ffmpeg -c copy, never transcode)
      │  RTSP
      ▼
Home Assistant Generic Camera / go2rtc / Frigate
```

## Settled facts — do not re-research

- The stream is H.264 High 1920x1080 at ~14 fps + AAC 16 kHz mono, ~1.15 Mbit/s
  total. It is already in a standard container: **remux only, never transcode**.
- Blink liveview sessions **do not have a predictable lifetime**. Measured over
  a 32-minute continuous soak on 2026-08-08 (13 sessions, one camera):
  Blink ended 9 of 12 itself, between 2 s and 244 s, median 152 s. Only 3
  reached our own 270 s renegotiation deadline. Design for "Blink will end this
  whenever it likes"; the scheduled renegotiation is a safety net, not the
  normal path. The earlier "~5-6 minutes" came from short prototype runs and was
  wrong.
- **Do not reopen a liveview in the same second as closing one.** Two of the
  three scheduled renegotiations in that soak produced a replacement session
  that Blink killed after 2 s having sent 0 bytes; every session that followed a
  *Blink-initiated* close was fine. `POST_CLOSE_COOLDOWN_S` spaces them. The
  open-side interval cannot catch this — the previous open was minutes earlier.
- Continuous viewing costs ~24 liveview commands/hour, not the ~13 a 270 s
  session length would suggest. `MAX_OPENS_PER_HOUR` must clear that with room
  for retries.
- The Blink Mini exposes **no LAN service**; the stream always transits Blink's
  cloud relay. "Local" in this project means locally *re-broadcast*, never
  locally *captured*. Do not spend effort trying to reach the camera directly.
- Two upstream `blinkpy` bugs must be patched (see `blinkpy_patches.py`):
  1. `BlinkLiveStream.recv()` frames with `read(n)`, which returns *up to* n
     bytes; a normal short TCP/TLS read makes it log `Insufficient data for
     payload` and **break out of the loop**, killing the stream. Fix:
     `readexactly(n)`.
  2. `BlinkLiveStream.poll()` exits the loop on the first response whose
     `status_code != 908`, and its `finally` then sends `request_command_done`,
     asking Blink to close the session. A single network hiccup kills the live.
     Fix: tolerate N consecutive failures.
- go2rtc and MediaMTX have **no native `tcp://` MPEG-TS ingest** — the add-on
  must bridge through ffmpeg (`exec:` source, or `runOnInit`).

Verified against the installed blinkpy 0.25.9 source on 2026-08-01, and binding
on the design (see `PLAN.md` §2 for the full table):

- **Never call `BlinkLiveStream.start()`.** Its `stop()` closes the listen server
  *and every client writer* (`livestream.py:343-357`), and `join()` calls `stop()`
  as soon as the last client leaves (`livestream.py:168-176`). Owning our own
  server is the only way a renegotiation does not drop downstream clients.
- **Never call `blink.save()`.** `Auth.login_attributes` returns `self.data` by
  reference (`auth.py:80-92`) and `validate_login()` has already put the
  plaintext password in it (`auth.py:106-112`), so `save()` writes the password
  to disk. Serialize the session ourselves, password stripped, `0600` set before
  the rename.
- **Pass `Auth(callback=...)`** to re-persist the session on token rotation:
  `_process_token_data()` replaces `refresh_token` on every refresh
  (`auth.py:365-368`). Without it the cached token goes stale within about an
  hour of uptime and the next restart falls back to a full password login.
- **`init_livestream()` has no guard at all**: `camera.py:470-480` subscripts
  whatever `Auth.query()` returned, so every cloud-side problem arrives as a raw
  builtin. All four must be caught and classified, or they reach `cli.py`'s
  top-level handler and exit non-zero — which is a respawn loop under S6:
  `TypeError` (query returned `None` on a connection error or a throttled
  account, `auth.py:280-287`), `KeyError` (`validate_response` hands back the
  parsed body for every status except 101/401/404, so a Blink error payload has
  no `"server"`), `UnauthorizedError` (raised at `auth.py:219`, never caught by
  `query()`), and `NotImplementedError` (an rtsps-only camera).
- **Cap `blinkpy.auth` and `blinkpy.api` at CRITICAL, not INFO.** Both dump whole
  HTTP bodies *above* INFO — `auth.py:169` logs the raw token-endpoint response
  at ERROR, and it is reachable in normal operation because
  `extract_login_info()` hard-indexes `login_response["refresh_token"]` while
  RFC 6749 §6 lets the server omit it on a refresh. One `KeyError` would put a
  live access token in the next support bundle.
- **`prompt_2fa()` calls `input()`** (`blinkpy.py:93-96`) — unusable in a
  container. Use `send_2fa_code(code)`.
- **`Blink.start()` swallows `LoginError` and returns `False`**
  (`blinkpy.py:152-176`), so classification must use the return value plus
  `blink.available`, never an `except LoginError` clause.

A working reference implementation of the client side lives outside this repo
at `../blink-test/blink_live.py` (prototype CLI). Read it before reimplementing
the relay; it already carries both patches and their regression tests.

## Status

`PLAN.md` holds the six milestones. **M1 and M3 are done**, both verified
against the real account on 2026-08-08. The 32-minute soak produced one
continuous 1800.03 s recording (h264 1920x1080 + aac 16 kHz) spanning 12 session
swaps, with the downstream socket never dropped, 0 Blink calls while no consumer
was attached, no traceback and no credential in the log.

Two caveats that soak also established, both worth knowing before M4:

- **Each renegotiation costs ~12 s of dead air** — 1945 s of wall clock produced
  1800 s of video. That is Blink's liveview startup latency, not our overhead.
- **ffmpeg logs a timestamp discontinuity per swap** (plus "corrupt input
  packet"). It coped — exit 0, full duration, valid streams — but DOCS.md should
  ship `-fflags +genpts` in the go2rtc snippet rather than let users discover it.

M2 is next and still owes: the `poll()` regression matrix, `.pre-commit-config.yaml`
and CI. `recv()` is already covered — `tests/unit/test_blinkpy_patches.py` runs it
against a real fragmenting TCP server and pins the upstream bug by asserting the
*unpatched* implementation truncates, so reverting the patch turns the suite red.

Two traps found the hard way in M1, both now covered by tests:

- **Close the consumer sockets before `Server.wait_closed()`.** Since CPython
  3.12.1 that call also waits for every accepted connection to detach, and a
  consumer parked on `read()` never does — so `async with server` hangs shutdown
  until S6 resorts to SIGKILL.
- **Dropping a consumer means closing its socket, not just detaching it.**
  Otherwise it holds a live connection that never delivers another byte and
  never reaches EOF, so ffmpeg/go2rtc never runs its reconnect policy.

## Rules

`.claude/rules/*.md` is auto-loaded. Files without `paths:` frontmatter load at
session start; files with `paths:` load lazily when a matching file is read.

| Rule | Scope |
|------|-------|
| `.claude/rules/00-code-structure.md` | **Hard requirement.** Package layout, module boundaries, complexity limits. |
| `.claude/rules/10-security-secrets.md` | Credentials, container privileges, supply chain. |
| `.claude/rules/20-addon-packaging.md` | `config.yaml`, Dockerfile, S6, AppArmor, CI. Lazy. |
| `.claude/rules/30-python-async.md` | asyncio, signals, logging, typing. Lazy. |
| `.claude/rules/40-testing.md` | pytest-asyncio, fakes over mocks. Lazy. |
| `.claude/rules/50-git-hygiene.md` | Commits, versioning, pre-commit gate. |

If your Claude Code build does not auto-load `.claude/rules/`, import the two
always-on rules explicitly by adding `@.claude/rules/00-code-structure.md` and
`@.claude/rules/10-security-secrets.md` here.

## Commands

```bash
uv sync --locked                 # install exact locked dependencies
uv run ruff check .              # lint
uv run ruff format --check .     # format check
uv run mypy                      # strict type check (src + tools)
uv run pytest -q                 # unit + integration tests
pre-commit run --all-files       # full local gate, mirrors CI
/check                           # all of the above, in order, via the skill

docker build -t local/ha-blink-camera .
```

To force a local (non-registry) build in Supervisor, comment out `image:` in
`config.yaml`.

Two environment variables exist for development only; nothing sets them inside
the container. `ADDON_DATA_DIR` relocates `options.json` / `session.json` away
from `/data`, and `ADDON_RELAY_PORT` moves the listen port off 8554 (Docker
Desktop already publishes that port on some machines). Neither is an add-on
option — see PLAN.md AD #8 for why a `relay_port` option would be a trap.

```bash
mkdir -p /tmp/blinkdata
uv run python tools/import_session.py ~/.blink_creds.json /tmp/blinkdata/session.json
ADDON_DATA_DIR=/tmp/blinkdata ADDON_RELAY_PORT=18554 uv run python -m ha_blink_camera.cli
```

## Subagents

Delegate rather than doing everything inline:

- `ha-addon-expert` — any change to `config.yaml`, `Dockerfile`, `rootfs/`, CI.
- `python-async-expert` — `stream_relay.py`, `blink_client.py`, `cli.py`.
- `code-reviewer` — after any non-trivial edit, before a commit or PR.
- `security-reviewer` — credential handling, privilege keys, new dependencies.
- `test-writer` — after implementing or changing any relay/client module.

## Conventions

- **src layout**: importable code under `src/ha_blink_camera/`, tests under
  `tests/`, outside `src/`. See `00-code-structure.md`.
- One exception hierarchy rooted at `BlinkCameraError`, split transient vs fatal.
- No `build.yaml` — this is a new repo, so keep everything in the Dockerfile
  (`ARG BUILD_FROM`). See `20-addon-packaging.md`.
- Never commit secrets. Enforced by the `protect-files` PreToolUse hook and by
  `deny` rules in `.claude/settings.json`.

## Verify before relying on it

These were researched on 2026-07-31 but drift or could not be confirmed from a
primary source. Check them at implementation time rather than trusting this file:

1. The exact base image tag (`ghcr.io/home-assistant/base-python:<py>-alpine<n>`)
   — pinned tags move fast; check `home-assistant/docker-base`.
2. S6-overlay: this repo picks the **native v3 `s6-rc.d` layout**, but the
   official example scaffold still teaches the legacy `services.d` layout. Both
   are live conventions. Judgment call, not a mandate.
3. Pinned SHAs for `home-assistant/builder` composite actions and
   `frenck/action-addon-linter`.
4. Whether Home Assistant's 2026.2 "add-on" -> "app" terminology rename affects
   anything beyond prose. Every file name and `config.yaml` key in these rules
   is the pre-rename one, which is what Supervisor actually reads.
5. No devcontainer is scaffolded yet — wire up `.devcontainer/devcontainer.json`
   from `home-assistant/devcontainer` before the first release. **TODO.**
