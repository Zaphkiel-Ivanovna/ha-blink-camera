# PLAN.md — ha-blink-camera

Development plan for the Home Assistant add-on. Status: no add-on code exists yet; `../blink-test/blink_live.py` is the proven client-side reference.

Facts marked **[V]** were verified today against `blinkpy 0.25.9` source (paths relative to the installed package). Facts marked **[?]** are still unverified and are scheduled inside the milestone that depends on them — do not build on them before then.

---

## 1. Decisions on the three hard problems

### (a) First-time 2FA on a headless add-on

**Decision — two-phase.**
- **v0.1 (M1–M4): session-file import.** The user pre-authenticates once with the existing `blink_live.py` prototype on a laptop, and copies the resulting session JSON (password stripped) into the add-on's `/data/session.json`. No 2FA code ever transits the add-on.
- **v1.0 (M5): Ingress form.** A one-route aiohttp page served through Supervisor's authenticated reverse proxy, POSTing the code into the **live** process, which then calls `blink.send_2fa_code(code)`. This requires an explicit, deliberate amendment to `00-code-structure.md`'s fixed module list (adding `setup_ui.py`), committed in the same change.

**Why the process must stay alive: [V]** `Auth._oauth_csrf_token` and `Auth._oauth_code_verifier` are plain in-memory attributes set in `_oauth_login_flow()` (`auth.py:337-338`) and `delattr`-ed in `complete_2fa_login()` (`auth.py:428-429`); the signin cookies live in the same in-memory `aiohttp` jar. Any mechanism that restarts the container between "Blink sent the code" and "user entered the code" throws that state away.

| Rejected alternative | One-line reason |
|---|---|
| `two_factor_code` add-on option + restart | Restart destroys the CSRF/PKCE/cookie state **[V]**; and the add-on cannot blank the option afterwards without `hassio_api`, so a stale code is resubmitted on every boot. |
| Live polling of `/data/options.json` from the running process | Supervisor writes that file when it *starts* the container **[?]** — a live process would never see the edit. |
| `blink.prompt_2fa()` (prototype path) | Calls `input()` **[V]** (`blinkpy.py:93-96`); there is no stdin in the container. |
| Supervisor notification + `input_*` helper round-trip | A notification is one-way; reading the value back needs Core API access — strictly more privilege than Ingress, for worse UX. |
| Drop-file at `/share/blink_2fa_code.txt` | Works and is zero-privilege, but needs `map: share` (readable by every other add-on) plus a file-editor add-on. Kept as documented last resort only. |

### (b) Session lifecycle vs "always-on" camera

**Decision — demand-gated, close-and-reopen, with a hard ceiling.** The relay's TCP listener is bound once for the process lifetime and always accepts. **No Blink liveview session exists until a downstream client connects.** While a client is attached, the session is renegotiated on expiry (close, reopen, resume writing to the *same* still-open client socket). When the last client leaves, the session is closed after a short linger. A minimum reopen interval and a hard cap of *N* session opens per hour bound the worst case.

| Rejected alternative | One-line reason |
|---|---|
| Always-on renegotiation every ~5 min | ~250 liveview commands/camera/day against an unofficial API with no published quota — exactly the shape that gets consumer cloud-camera APIs throttled. |
| Overlapping old+new sessions with an atomic broadcast swap | Requires two concurrent liveview commands on one camera (unverified, likely forbidden), and splices two independent MPEG-TS timebases into one socket → non-monotonic DTS and A/V desync under `-c copy`. |
| GOP cache / keyframe-aligned late-join | Container-parsing subsystem in a "remux only" project; solves a problem the single-bridge v1 does not have. |

### (c) Failure behaviour (restart, network loss, bad credentials, camera offline)

**Decision — typed transient/fatal split; fatal idles in-process, never exits.**
- `TransientBlinkError` → bounded exponential backoff, capped, with the hourly session-open ceiling on top.
- `FatalConfigError` → log **one** actionable line, then idle with a repeating WARNING heartbeat and **zero further Blink calls**. The process never exits, so S6 never respawn-storms Blink's login endpoint.
- Empty `username`/`password` on a fresh install is its own `NOT_CONFIGURED` idle state with a friendly line — not a traceback.
- Credentials-vs-network is disambiguated by a connectivity pre-check before any login attempt is declared fatal.

| Rejected alternative | One-line reason |
|---|---|
| `cli.main()` returns 1 on fatal | Under an S6 `longrun` that is an immediate respawn loop hammering the login endpoint. |
| Mapping `blinkpy.LoginError` → fatal | **[V]** `Blink.start()` catches `(LoginError, TokenRefreshFailed, BlinkSetupError)`, logs, and returns `False` (`blinkpy.py:152-176`). That `except` clause is dead code; classification must use the return value + `blink.available`. |
| Retrying a fatal condition forever | Bad credentials / wrong camera name will never fix themselves; retrying risks account lockout. |

---

## 2. Architecture decisions — what differs from the prototype, and why

| # | Change vs `blink_live.py` | Why |
|---|---|---|
| 1 | **Never call `BlinkLiveStream.start()` / rely on its server.** `stream_relay.py` owns one `asyncio.Server` bound once for the process lifetime; `blink_client.py` appends a duck-typed sink (`write` / `drain` / `is_closing` / `close`) to `stream.clients`. | **[V]** `start()` binds its own server (`livestream.py:102-104`); `feed()`'s `finally` calls `stop()` (`livestream.py:123-133`); `stop()` closes the listen server **and every client writer** (`livestream.py:343-357`); `join()`'s `finally` calls `stop()` as soon as the last client leaves (`livestream.py:168-176`). The prototype therefore drops every downstream client on each renegotiation and tears the server down when ffmpeg blips. **This also makes CLAUDE.md's architecture line `stream_relay.py (BlinkLiveStream.start(host, port))` wrong — fix it in M1.** |
| 2 | **Never call `blink.save()`.** Serialize the session cache ourselves: strip `password`, write `tmp` + `os.replace`, `chmod 0600` **before** the rename. | **[V]** `Auth.login_attributes` returns `self.data` **by reference** (`auth.py:80-92`) and `validate_login()` puts the plaintext password into `self.data` (`auth.py:106-112`); `Blink.save()` json-dumps that verbatim (`blinkpy.py:352-354`). The prototype leaks the password at rest. The prototype also `chmod`s *after* writing, leaving a 0644 window containing a live token. |
| 3 | **Re-persist the session on token rotation** by passing `Auth(callback=...)`. | **[V]** `_process_token_data()` replaces `self.refresh_token` on every refresh (`auth.py:365-368`), and `query()` invokes `self.callback` right after `refresh_tokens()` (`auth.py:263-270`). Without this the on-disk `refresh_token` goes stale within ~an hour of uptime and the next restart falls through to a full password login → 2FA → dead end. |
| 4 | **Non-interactive 2FA**: `await blink.send_2fa_code(code)`, never `prompt_2fa()`. | **[V]** `prompt_2fa()` calls `input()` (`blinkpy.py:93-96`). |
| 5 | **Guard `init_livestream()` against a `None` response.** | **[V]** `camera.py:470-480` does `response["server"].startswith(...)` with no guard, and `Auth.query()` swallows `ClientConnectionError`/`TimeoutError` and returns `None` (`auth.py:280-287`). A throttled account raises `TypeError`, not `NotImplementedError`. |
| 6 | **Demand-gated lifecycle** instead of "open a session because the user asked". | The prototype is human-driven and short-lived; a 24/7 add-on must self-limit (see decision (b)). |
| 7 | **Never log the account email**, at any level; cap `blinkpy.*` loggers at INFO regardless of our own level. | Add-on stdout ends up in HA support bundles. blinkpy logs token and OAuth detail at DEBUG — a `log_level: debug` option would violate `10-security-secrets.md` outright. Hence: no `log_level` option in v1. |
| 8 | **Options surface reduced to `username`, `password`, `camera_name`.** Relay port is a `Final` constant (8554) mapped in `ports:`; users remap the host side in Supervisor's Network UI. | A `relay_port` option cannot change the container-side literal in `ports:` — it is a trap, not a setting. `reset_session` cannot self-clear; changing `username` already invalidates the cache. |
| 9 | Delivery layer for v1 = **raw MPEG-TS on tcp://:8554** + a *verified, copy-pasteable* go2rtc `exec:` snippet in DOCS.md. | Matches the settled architecture; bundling MediaMTX+ffmpeg adds a second supply chain, per-arch checksums and a second S6 service before the Python side exists. |

---

## 3. Milestones

Each milestone is independently shippable and independently verifiable. Delegate as noted; run `code-reviewer` before every commit and `security-reviewer` on anything touching credentials, `/data`, privileges or dependencies (per CLAUDE.md).

---

### M1 — A working relay you can point ffplay at (no container, no 2FA)

**Goal.** One process on a dev machine authenticates from an imported session file, opens a liveview, and re-broadcasts MPEG-TS on a fixed local TCP port for the life of one Blink session (~5 min). This is the whole risk of the project, proven first.

**Files created**
```
pyproject.toml                       # ruff/mypy/pytest config per rules 00/30/40; blinkpy pinned exactly
uv.lock                              # committed
.gitignore                           # .env, .env.*, *.local, options.json, session.json, .secrets.baseline
README.md
src/ha_blink_camera/__init__.py
src/ha_blink_camera/exceptions.py    # BlinkCameraError -> Transient/Fatal + InvalidCredentials, TwoFactorRequired, CameraNotFound
src/ha_blink_camera/logging_setup.py # one StreamHandler(stdout), explicit Formatter, blinkpy.* capped at INFO
src/ha_blink_camera/config.py        # sole reader of <data_dir>/options.json -> frozen Config
src/ha_blink_camera/blinkpy_patches.py  # the two patches, ported verbatim, with guards
src/ha_blink_camera/blink_client.py  # sole blinkpy importer for API calls; session cache; camera resolution; queue sink
src/ha_blink_camera/stream_relay.py  # owns the asyncio.Server + one TaskGroup; single session for now
src/ha_blink_camera/cli.py           # entrypoint; resolves data dir from ADDON_DATA_DIR (default /data)
tools/import_session.py              # one-shot: prototype creds JSON -> /data/session.json, password stripped
tests/unit/test_exceptions.py
tests/unit/test_config.py
```
**Files changed** — `CLAUDE.md`: correct the architecture diagram line (see AD #1) and add AD #1–#5 to "Settled facts".

**Acceptance criteria**
```bash
uv sync --locked && uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q

mkdir -p /tmp/blinkdata
uv run python tools/import_session.py ~/.blink_creds.json /tmp/blinkdata/session.json
python3 -c "import json;d=json.load(open('/tmp/blinkdata/session.json'));assert 'password' not in d;print('no password at rest: OK')"
stat -f '%Lp' /tmp/blinkdata/session.json        # must print 600
printf '{"username":"me@example.com","password":"","camera_name":"Salon"}' > /tmp/blinkdata/options.json

ADDON_DATA_DIR=/tmp/blinkdata uv run python -m ha_blink_camera.cli &
ffmpeg -f mpegts -i tcp://127.0.0.1:8554 -c copy -bsf:a aac_adtstoasc \
       -movflags +frag_keyframe+empty_moov -t 30 -y /tmp/m1.mp4
ffprobe -v error -show_streams /tmp/m1.mp4 | grep -E 'codec_name|width|height|sample_rate'
```
- PASS = `h264` / `1920` / `1080` / `aac` / `16000`, file plays, **and** the log contains no email address, no token, no password (`grep -iE 'token|password|@' addon.log` returns nothing meaningful).
- PASS = killing the process with SIGTERM exits within 5 s with no traceback.

**Delegate to.** `python-async-expert` (all of `src/`), `security-reviewer` (session cache + `tools/import_session.py`), `code-reviewer`.

---

### M2 — The patches are pinned down, and CI enforces it

**Goal.** The two blinkpy patches become checked guarantees against a **real** fragmenting TCP server, and the four-command gate runs on every push.

**Files created**
```
tests/integration/fake_relay.py      # real asyncio server, IMMI framing (9-byte header, big-endian len @ [5:9]);
                                     # two strategies only: BYTE_AT_A_TIME and COALESCED
tests/conftest.py
tests/unit/test_blinkpy_patches.py   # recv: header split, payload split, coalesced frames, non-video msgtype
                                     #       dropped, non-0x47 payload dropped, clean EOF, 1-byte-at-a-time
                                     # poll: single non-908 tolerated, N consecutive stops cleanly and still
                                     #       calls request_command_done exactly once, counter resets on success,
                                     #       cancellation still releases the command
tests/unit/test_blink_client.py      # account-switch cache discard, camera resolution, password stripped on write
tests/unit/test_logging_setup.py
.pre-commit-config.yaml
.github/workflows/ci.yaml            # ruff check -> ruff format --check -> mypy src -> pytest -> docker build (single arch, no push)
```
**Files changed** — `.claude/rules/00-code-structure.md`: amend the checklist to name `blinkpy_patches.py` as the one permitted `import blinkpy.livestream` exception (it cannot monkeypatch `BlinkLiveStream` otherwise). One-line justification in the same commit.

**Acceptance criteria**
```bash
uv run pytest -q tests/unit/test_blinkpy_patches.py -v        # all green
# prove the tests actually catch the bug they exist for:
git stash && uv run pytest -q tests/unit/test_blinkpy_patches.py   # must FAIL without the patches
git stash pop
pre-commit run --all-files
gh pr create --draft && gh run watch                          # CI green
```
- PASS = the recv suite fails when `readexactly` is reverted to `read`, and the poll suite fails when the failure counter is removed. A suite that stays green without the patch is worthless.

**Delegate to.** `test-writer` (primary), `ha-addon-expert` (CI workflow), `code-reviewer`.

---

### M3 — Lifecycle: demand-gating, renegotiation, failure classification

**Goal.** The relay survives indefinitely with a bridge attached, opens a Blink session only on demand, renegotiates across the ~5 min expiry without dropping the downstream socket, and handles every failure mode from the brief with a named state.

**Files changed**
```
src/ha_blink_camera/stream_relay.py  # IDLE -> NEGOTIATING -> STREAMING -> DRAINING -> IDLE
                                     # Final constants: SESSION_LIFETIME_S, RENEGOTIATE_MARGIN_S,
                                     # IDLE_LINGER_S, MIN_REOPEN_INTERVAL_S, MAX_OPENS_PER_HOUR,
                                     # BACKOFF_INITIAL_S / _MAX_S, NO_FRAMES_TIMEOUT_S
src/ha_blink_camera/blink_client.py  # Auth(callback=) re-persist on token rotation; connectivity pre-check;
                                     # None-guard around init_livestream(); classify start()==False + blink.available
src/ha_blink_camera/cli.py           # NOT_CONFIGURED / fatal-idle states + heartbeat WARNING; SIGTERM path
src/ha_blink_camera/exceptions.py
tests/unit/test_stream_relay.py      # state machine transitions, backoff, ceiling (pure, no sockets)
tests/unit/test_cli.py
tests/integration/test_stream_relay.py
```
**Behaviours that must each have a test and a distinct log line:** session expiry · upstream EOF mid-stream · no downstream client (→ no Blink call at all) · rapid client reconnect (debounced, session reused) · network loss (retry with backoff, never fatal) · wrong credentials (idle, one line, zero further calls) · camera present but sending no frames within `NO_FRAMES_TIMEOUT_S` (give up with a cool-down, loud line) · hourly ceiling reached (refuse + log) · SIGTERM with a client attached · restart with a valid cached session (no password login).

**Acceptance criteria**
```bash
uv run pytest -q                                              # unit + integration green

# 30-minute soak with a real account, one attached consumer:
ADDON_DATA_DIR=/tmp/blinkdata uv run python -m ha_blink_camera.cli 2>&1 | tee /tmp/soak.log &
ffmpeg -f mpegts -i tcp://127.0.0.1:8554 -c copy -bsf:a aac_adtstoasc \
       -movflags +frag_keyframe+empty_moov -t 1800 -y /tmp/soak.mp4
ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/soak.mp4   # >= ~1750
grep -c 'renegotiat' /tmp/soak.log                            # ~5-6, i.e. it really cycled
grep -c 'client disconnected' /tmp/soak.log                   # 0 — the downstream socket never dropped
```
- PASS = one continuous 30-minute recording spanning ≥5 renegotiations, downstream socket never closed.
- PASS = with **no** consumer attached for 10 minutes, `grep -c init_livestream /tmp/soak.log` is `0`.
- PASS = pull the network for 60 s mid-stream → recovers on its own; the log says transient, never "bad credentials".

**Delegate to.** `python-async-expert` (primary), `test-writer`, `code-reviewer`.

---

### M4 — It is an add-on: installs and runs in a real Supervisor

**Goal.** Ship v0.1.0, installable from a local repository, consumed by go2rtc as a Home Assistant camera entity. Authentication still bootstrapped by the imported session file — documented as the v0.1 first-run path.

**Files created**
```
repository.yaml
config.yaml                          # slug blink_camera; arch [aarch64, amd64]; init: false; startup: services;
                                     # map: data; ports: {8554/tcp: 8554};
                                     # options/schema: username(email) / password / camera_name(str?)
                                     # NO ingress, NO elevated keys, NO relay_port / log_level / reset_session
Dockerfile                           # ARG BUILD_FROM=ghcr.io/home-assistant/base-python:<VERIFY tag>
                                     # uv sync --locked; COPY rootfs /; no ENTRYPOINT, no USER
apparmor.txt                         # official template, profile renamed blink_camera, nothing invented
rootfs/etc/s6-overlay/s6-rc.d/blink-camera/{run,type,dependencies.d/base}
rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/blink-camera
DOCS.md
CHANGELOG.md
icon.png                             # 1:1 ~128x128
logo.png                             # ~250x100
translations/en.yaml
```
**DOCS.md must contain, verbatim and tested:** the first-run session-import walkthrough; the working go2rtc `exec:` snippet (a full `ffmpeg … {output}` command — `tcp://…` is *not* a valid go2rtc source on its own); the "do not attach a thumbnail-polling camera card / do not set Frigate `record: always`" warning, because both silently defeat demand-gating and can produce *more* cloud calls than an always-on design; the behaviour table (the ten states from M3); one camera per install; and the security-rating statement (base 5 + custom apparmor = 6, zero elevated keys, no deviation to justify).

**Acceptance criteria**
```bash
docker build -t local/ha-blink-camera .                       # succeeds on arm64 and amd64
# On a real HA install, repository added as a local add-on repo (image: commented out to force local build):
ha addons install local_blink_camera && ha addons start local_blink_camera
ha addons logs local_blink_camera                             # "Starting Blink camera streamer", then STREAMING on first connect
ha addons info local_blink_camera | grep rating                # 6
```
- PASS = go2rtc snippet from DOCS.md pasted into HA's go2rtc config yields a working `camera.*` entity showing live video.
- PASS = `ha addons restart` recovers with no password prompt and no 2FA (cached session path).
- PASS = a wrong `camera_name` produces one clear log line and an idle add-on that stays "running" — not a crash loop.

**Delegate to.** `ha-addon-expert` (primary, all packaging files), `security-reviewer` (config.yaml privilege keys, apparmor), `code-reviewer`.

**Verify before writing (do not carry over from the rules file):** the exact `base-python` tag; whether `arch:` should still include `armv7`; the S6 v3 layout against current HA base images.

---

### M5 — Headless 2FA, end to end, inside Home Assistant

**Goal.** A user with no laptop and no prototype can install the add-on, enter credentials, receive a Blink code, and finish login from the HA UI. Removes the only reason the add-on is not self-contained.

**Prerequisite — verify first, then build (blocking):**
1. Does Supervisor rewrite `/data/options.json` for a *running* container on options-save, or only at container start? **[?]** The whole v0.1 fallback story depends on the answer.
2. Ingress authentication semantics — all logged-in users, or admins only? **[?]**
3. Blink's 2FA code expiry window (empirically, on a disposable account). **[?]**

**Files created / changed**
```
.claude/rules/00-code-structure.md   # AMEND the mandated layout: add setup_ui.py, with a one-line justification
                                     # (blinkpy's OAuth state is per-process, so the code must reach the LIVE process)
src/ha_blink_camera/setup_ui.py      # aiohttp app: GET / (state), POST /verify (code), POST /forget (drop session)
                                     # POST-only, no CDN assets, no templating of user input, code never echoed back
src/ha_blink_camera/blink_client.py  # begin_login() / submit_two_factor(); attempt counter is a local variable
src/ha_blink_camera/cli.py           # WAITING_2FA state; ingress app runs in the same TaskGroup
config.yaml                          # ingress: true, ingress_port: 8099, panel_icon: mdi:cctv
apparmor.txt, DOCS.md, translations/en.yaml, tests/unit/test_setup_ui.py
```

**Acceptance criteria**
```bash
ha addons stop local_blink_camera && rm /addon_config_or_data/session.json
ha addons start local_blink_camera
ha addons logs local_blink_camera        # "ACTION REQUIRED: open the add-on page and enter the code Blink sent you"
# open the add-on's Web UI panel in HA, enter the emailed code, submit
ha addons logs local_blink_camera        # "Authenticated", then STREAMING on first client connect
```
- PASS = full first-run login with **no** stdin, **no** restart between code-request and code-entry, **no** imported file.
- PASS = a wrong code shows a generic error and does not exit the process; the code never appears in the log or the HTTP response.
- PASS = `ha addons info` still reports rating 6 (ingress +2 and apparmor +1 both clamp at the published maximum).

**Delegate to.** `python-async-expert` (`setup_ui.py`, `blink_client.py`), `ha-addon-expert` (ingress keys), `security-reviewer` (new HTTP surface + the rule amendment), `test-writer`, `code-reviewer`.

---

### M6 — Release engineering

**Goal.** Tagged, multi-arch, signed releases; the packaging linters that were deliberately deferred.

**Files created / changed**
```
.github/workflows/ci.yaml            # add addon-linter, hadolint, shellcheck, yamllint, zizmor + per-arch build matrix
.github/workflows/release.yaml       # on v*.*.*: assert tag == config.yaml version, build+push+cosign per arch
config.yaml                          # re-enable image: ghcr.io/<owner>/{arch}-addon-blink-camera
.devcontainer/devcontainer.json      # from home-assistant/devcontainer (CLAUDE.md TODO)
CHANGELOG.md, DOCS.md
```
**Acceptance criteria**
```bash
git tag v1.0.0 && git push --tags && gh run watch
docker pull ghcr.io/<owner>/aarch64-addon-blink-camera:1.0.0
cosign verify ghcr.io/<owner>/aarch64-addon-blink-camera:1.0.0 --certificate-identity-regexp '.*'
# fresh HA install: add the GitHub repo URL as an add-on repository, install from the store, camera works
```
- PASS = a mismatched tag/version fails the workflow loudly.
- PASS = every third-party action is pinned to a full SHA (`zizmor` green).

**Delegate to.** `ha-addon-expert` (primary), `security-reviewer` (action pinning, cosign, GHCR permissions).

---

## 4. Risks

| # | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| 1 | Supervisor's options-save semantics **[?]** invalidate the fallback 2FA story | Med / Med | Blocking check at the top of M5. v0.1 ships session-import, which is immune either way. |
| 2 | The `stream.clients` injection is an internal blinkpy detail, not a public API | Med / High | Regression test in M2 asserting every byte reaches the sink; blinkpy pinned to an exact version; bumping it is its own PR with `security-reviewer` sign-off. The sink must implement `close()` too — **[V]** `stop()` calls `writer.close()` on every entry (`livestream.py:353-356`). |
| 3 | Blink throttles or locks the account despite demand-gating | Low-Med / High | Hourly session-open ceiling; minimum reopen interval; give-up state with cool-down on a no-frames camera; fatal conditions never retry; DOCS.md warns about thumbnail-polling cards and `record: always`. No published quota exists to design against — this is mitigation, not proof. |
| 4 | Stale `refresh_token` after long uptime bricks the next restart | High if unmitigated / High | `Auth(callback=)` re-persists the cache on every rotation (M3) — **[V]** `auth.py:263-270`. Explicit test: rotate, restart, resume with no password. |
| 5 | Password leaking into the session cache at rest | Certain if ported naively / High | Never call `blink.save()`; strip `password`, atomic write, `0600` before rename. Test asserts the key is absent (M1). |
| 6 | Token leakage into HA support bundles via DEBUG logs | Med / High | No `log_level` option in v1; `blinkpy.*` loggers capped at INFO regardless of our level; username never logged at any level. Revisit only behind a redacting `logging.Filter`. |
| 7 | Transient network failure misclassified as bad credentials → permanent idle | High if unmitigated / High | **[V]** `Auth.query()` swallows `ClientConnectionError`/`TimeoutError` and returns `None` (`auth.py:280-287`), so both surface identically. Connectivity pre-check + bounded retry before anything is declared fatal (M3). Also covers RTC-less hosts whose clock has not yet NTP-synced. |
| 8 | `NotImplementedError`-only handling around `init_livestream()` | Med / Med | **[V]** a throttled account yields `TypeError` (`camera.py:478`, unguarded `response["server"]`). Guard for `None` first, classify separately (M3). |
| 9 | MPEG-TS discontinuity across a close-and-reopen renegotiation upsets `ffmpeg -c copy` | Med / Med | Measured in M3's soak (recording must span ≥5 renegotiations). If ffmpeg chokes, the fix is `-fflags +genpts` / a bridge restart policy in DOCS.md — **not** the overlapping-session design. |
| 10 | Base image tag, `arch:` list and action SHAs in the rules file are stale | Med / Low | Every one is re-verified inside M4/M6 rather than trusted. |
| 11 | Ingress requires amending a rule declared non-negotiable | Certain / Low | Amended deliberately and explicitly in M5's own commit, with the external constraint (blinkpy's per-process OAuth state) recorded as the justification. |

---

## 5. Out of scope for v1

- **Bundled MediaMTX / go2rtc / ffmpeg inside the add-on image.** v1 exposes raw MPEG-TS and documents a verified go2rtc `exec:` bridge. Revisit only if the documented snippet proves to be a real adoption barrier.
- **Overlapping dual-session renegotiation** and any zero-gap guarantee.
- **GOP cache, PAT/PMT parsing, keyframe-aligned late-join.**
- **Multiple cameras per install** (one install = one container = one camera; DOCS.md states this out loud) and **multiple simultaneous downstream consumers** (one bridge is supported; more is untested).
- **Recording, clips, snapshots, still images, motion events** — the add-on relays live video and nothing else.
- **`armv7` / `armhf` / `i386`** until upstream base-image arch support is confirmed.
- **`log_level` / `debug`, `reset_session`, `relay_port`** options.
- **HACS or official Community Add-ons store submission**; `frenck/action-addon-linter` stays at `community: false`.
- **Translations beyond `en`.**
- **Any attempt at local capture.** Settled: the Blink Mini exposes no LAN service; the stream always transits Blink's cloud relay.

---

## 6. Still uncertain — resolve, do not assume

1. **[?]** Does Supervisor rewrite `/data/options.json` for a *running* container on options-save? (Blocks M5's design choice; irrelevant to M1–M4.)
2. **[?]** Ingress auth scope: every logged-in HA user, or admins only?
3. **[?]** Blink's 2FA code expiry window and any server-side lockout threshold — needs a disposable test account.
4. **[?]** Whether a Blink sync module permits a liveview on camera B while camera A is streaming.
5. **[?]** Whether IMMI payloads are always a whole number of 188-byte TS packets (only matters if a TS parser is ever added — it is out of scope for v1).
6. **[?]** Current `ghcr.io/home-assistant/base-python` tag, the supported `arch:` list, and every third-party action SHA.
7. **[?]** Real upstream issue URLs for the two blinkpy patches — `00-code-structure.md` requires the comment link; the bugs are confirmed in 0.25.9's source but the tracker was not consulted.
