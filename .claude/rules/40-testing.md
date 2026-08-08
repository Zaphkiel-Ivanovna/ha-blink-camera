---
paths:
  - "tests/**"
  - "src/**/*.py"
---

# Testing

## Configuration

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

With `asyncio_mode = "auto"`, async tests need no `@pytest.mark.asyncio`.

## Fakes over mocks at the socket layer

The `read()`-versus-`readexactly()` bug this project patches in blinkpy is
precisely the class of bug a `Mock(spec=StreamReader)` hides: a mock returns
whatever you told it to, so it never produces the short read that breaks real
code. Therefore:

- Spin up a **real** asyncio TCP server bound to `127.0.0.1:0` inside the test,
  serving a scripted byte sequence — and deliberately fragment its writes at
  hostile boundaries.
- Point the code under test at that server's actual socket, and assert on what
  arrives after a genuine read / cancellation round-trip.
- Reserve mocks for the HTTP/Blink-cloud boundary only, i.e. `blink_client.py`'s
  calls into blinkpy. Never mock the raw socket layer.

## Required regression tests

- `recv()` patch: a fake server writing in chunks smaller than the frame size
  must not truncate or kill the stream. Assert every byte is relayed.
- `poll()` patch: a single non-908 status must **not** tear down the session;
  N consecutive failures must stop it cleanly and still release the command.
- Session renegotiation: the TaskGroup survives a simulated expiry and
  reconnects on the same `host:port`.
- Account switch: a cached session for another username is discarded rather
  than silently reused.

## Layout

`tests/unit/` mirrors `src/ha_blink_camera/` one-to-one. `tests/integration/`
holds the fake-TCP-server end-to-end tests. **No test ever calls the real Blink
cloud** — no credentials in CI, no flakiness, no rate limiting.

## CI gate order

`ruff check` -> `ruff format --check` -> `mypy` -> `pytest` -> Docker build.
`mypy` takes no path argument: `pyproject.toml` sets `files = ["src", "tools"]`
so the credential-handling importer is checked too.
All four must pass before the image build step runs.
