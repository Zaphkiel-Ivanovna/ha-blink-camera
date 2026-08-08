---
description: Mandatory package structure, module boundaries and complexity limits. Non-negotiable.
---

# Code Structure (MUST)

This is an explicit, hard requirement: the codebase stays irreproachable in
structure. Violations block merge. When a change does not fit the structure,
change the design — not the rule.

## 1. Layout (MUST match exactly)

```
ha-blink-camera/
  src/ha_blink_camera/
    __init__.py
    config.py            # /data/options.json -> typed Config object
    blink_client.py      # ALL blinkpy interaction funnels through here
    blinkpy_patches.py   # the two upstream bug patches, isolated
    stream_relay.py      # BlinkLiveStream lifecycle, TCP re-broadcast, renegotiation
    exceptions.py        # BlinkCameraError hierarchy
    logging_setup.py     # stdlib logging configuration
    cli.py               # entrypoint (python -m ha_blink_camera.cli)
  tests/
    unit/                # mirrors src/ha_blink_camera/ one-to-one
    integration/         # fake-TCP-server end-to-end tests
  rootfs/                # S6 services, see 20-addon-packaging.md
  config.yaml
  Dockerfile
```

`src/` layout, not flat: it prevents the working-directory copy of the package
from shadowing the installed one during tests and CI
(packaging.python.org, "src layout vs flat layout").

## 2. Module responsibility (MUST be single-purpose)

- **`blink_client.py` is the only module allowed to `import blinkpy` for API
  calls.** Every other module talks to the thin interface it exposes. This
  contains upstream API drift — and blinkpy drifts: Blink broke authentication
  in 0.25.3 and the liveview endpoints in 0.25.6.
  - **One exception: `blinkpy_patches.py`**, which imports
    `blinkpy.livestream` and `blinkpy.api`. Amended 2026-08-08 (M1) for an
    external reason, not convenience: monkeypatching `BlinkLiveStream` is
    impossible without holding the class. The exception is bounded to that
    module, whose whole content is the patches.
- **`blinkpy_patches.py`** holds both monkeypatches and nothing else. Each patch
  MUST have: a comment linking the upstream issue, a guard that detects whether
  the installed blinkpy already ships the fix (warn, never double-patch), and a
  dedicated regression test in `tests/unit/`.
- **`stream_relay.py`** owns the `asyncio.TaskGroup` running the poll loop, the
  `.feed()` task and session renegotiation. No blinkpy import here — it goes
  through `blink_client.py`.
- **`exceptions.py`** defines one hierarchy:
  `BlinkCameraError` -> `TransientBlinkError` (retry) / `FatalConfigError`
  (never retry). Every `except` clause catches one of these, never bare
  `Exception` — the single exception being the top-level handler in `cli.py`.
- **No module-level mutable global state.** Config and session objects are
  constructed once in `cli.py` and passed explicitly. This is precisely what
  makes `stream_relay.py` testable against a real fake TCP server
  (see `40-testing.md`).

## 3. Mechanically enforced limits

In `pyproject.toml`:

```toml
[tool.ruff.lint]
extend-select = ["C90", "PLR0912", "PLR0915"]

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.pylint]
max-branches = 12     # Ruff default — do not raise it to make a function pass
max-statements = 50   # Ruff default
```

If a function exceeds a limit, split it. Raising a limit requires a one-line
justification comment next to the config key, in the same commit.

## 4. Naming

- Modules and files: `snake_case.py`.
- Exceptions: `PascalCase` ending in `Error`.
- Constants: `UPPER_SNAKE_CASE`, module level, typed `Final`. No magic numbers
  inline — the ~5-6 minute session expiry, ports and timeouts all get names.

## 5. Docstrings

- Every module opens with a one-line docstring stating its single
  responsibility. This doubles as the enforcement of rule 2: if you cannot
  write that one line, the module is doing too much.
- Every public function and class carries at least a one-line docstring.

## Checklist (verify before any PR)

- [ ] No module other than `blink_client.py` and `blinkpy_patches.py` imports
      `blinkpy` (`grep -rl "^from blinkpy\|^import blinkpy" src/` returns exactly
      those two files).
- [ ] Both patches live in `blinkpy_patches.py`, with upstream links and guards.
- [ ] No bare `except Exception` outside `cli.py`'s top-level handler.
- [ ] `uv run ruff check .` passes with C90/PLR0912/PLR0915 enabled.
- [ ] No module-level mutable globals.
- [ ] `tests/unit/` still mirrors `src/ha_blink_camera/` one-to-one.
