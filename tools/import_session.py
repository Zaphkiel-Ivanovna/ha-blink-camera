#!/usr/bin/env python3
"""One-shot: turn a prototype credentials file into the add-on's session cache.

v0.1 cannot ask for a two-factor code on its own (blinkpy keeps the OAuth PKCE
state in memory, so the code has to reach a *live* process — see PLAN.md M5).
Until the Ingress form exists, authenticate once with the prototype CLI and
import the result:

    uv run python tools/import_session.py ~/.blink_creds.json /data/session.json

The password is dropped on the way through — the prototype's file contains it in
plaintext because `blink.save()` serialises `Auth.data` verbatim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ha_blink_camera.blink_client import session_payload, write_session_file


def _mask(email: str) -> str:
    """Show enough of an address to recognise the account, not enough to leak it."""
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def _load(path: Path) -> dict[str, object]:
    """Read the source credentials file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path} does not contain a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    """Convert `source` into `destination`, password stripped, mode 0600."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="prototype credentials JSON")
    parser.add_argument("destination", type=Path, help="add-on session.json to write")
    args = parser.parse_args(argv)

    try:
        source = _load(args.source)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as err:
        print(f"error: cannot read {args.source}: {err}", file=sys.stderr)
        return 1

    payload = session_payload(source)
    if not payload.get("refresh_token"):
        print(
            "error: no refresh_token in the source file — that session cannot be "
            "resumed. Re-authenticate with the prototype first.",
            file=sys.stderr,
        )
        return 1
    if not str(payload.get("username") or "").strip():
        print(
            "error: no username in the source file. The add-on cannot verify "
            "which account that session belongs to, and will discard it.",
            file=sys.stderr,
        )
        return 1

    try:
        write_session_file(args.destination, payload)
    except (OSError, ValueError) as err:
        print(f"error: cannot write {args.destination}: {err}", file=sys.stderr)
        return 1

    username = payload.get("username")
    dropped = sorted(set(source) - set(payload))
    print(f"Wrote {args.destination} (mode 0600) for {_mask(str(username or ''))}")
    print(f"Kept {len(payload)} field(s); dropped {len(dropped)}: {', '.join(dropped)}")
    print("Set the same username in the add-on options, or the cache is discarded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
