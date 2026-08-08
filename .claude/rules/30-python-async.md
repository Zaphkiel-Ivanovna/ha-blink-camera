---
paths:
  - "src/**/*.py"
---

# Python & Async Conventions

## Concurrency

- Run the poll loop, the `BlinkLiveStream.feed()` task and the TCP re-broadcast
  server inside **one `asyncio.TaskGroup()`**. A single unhandled exception then
  cancels its siblings deterministically instead of leaking an orphan task.
- Catch the resulting `ExceptionGroup` with `except*` at the boundary that
  decides retry-versus-exit, routing on the `BlinkCameraError` subtype from
  `exceptions.py`. A `FatalConfigError` (bad credentials) must never be
  retried forever; a `TransientBlinkError` must never crash the add-on.
- Session renegotiation at the ~5-6 minute expiry is a `TransientBlinkError`
  handled inside that supervising loop — it is expected behaviour, not a fault.
- **Keep the relay's `host:port` constant across renegotiations.** The
  downstream go2rtc/MediaMTX bridge reconnects on its own restart/backoff
  policy; a moving port breaks it silently. This is an architecture decision of
  this project, not an upstream requirement.
- Reading a framed protocol uses `readexactly(n)`, never `read(n)`. `read(n)`
  returns *up to* n bytes and short reads are normal — this is exactly the
  upstream blinkpy bug this project patches. Assume any new framing code will
  hit it too.

## Shutdown

- Register `SIGTERM` and `SIGINT` with `loop.add_signal_handler(sig, cb)`,
  **not** `signal.signal`, so the handler can await coroutine cleanup: close
  the blinkpy session, cancel the TaskGroup, let each task's `finally` close
  its connection. S6 sends SIGTERM on container stop.
- Best-effort cleanup calls in a `finally` are wrapped in
  `contextlib.suppress(Exception)`; they must never mask the original error nor
  emit an alarming log line on a normal shutdown.

## Logging

- `PYTHONUNBUFFERED=1` in the Dockerfile. Without it, stdout is block-buffered
  in a non-TTY container and lines reach the Supervisor log viewer late or not
  at all.
- stdlib `logging` to stdout/stderr, single-line human-readable text with an
  explicit `Formatter` carrying timestamp and level. The Supervisor viewer is a
  raw line viewer; it adds no formatting. JSON logs are not appropriate here.
- Never `print()` in library code.
- Log the *decision*, not the payload: reconnect counts, session lifetimes and
  state transitions — never credentials, tokens or frame contents.

## Typing

```toml
[tool.mypy]
strict = true

[[tool.mypy.overrides]]
module = ["blinkpy.*"]
ignore_missing_imports = true
```

Strict from day one — the codebase is small and new. Silence missing stubs for
`blinkpy` only, never globally.

## Ruff

```toml
[tool.ruff]
line-length = 88
target-version = "py313"   # keep in sync with the base image's Python

[tool.ruff.lint]
extend-select = ["I", "C90", "PLR0912", "PLR0915", "B", "UP"]
```

`I` isort ordering, `B` bugbear, `UP` pyupgrade; complexity rules come from
`00-code-structure.md`.
