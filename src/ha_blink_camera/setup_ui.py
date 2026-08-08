"""Serves the Ingress first-run page, and nothing else."""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from typing import Any, Final

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

INGRESS_PORT: Final = 8099
_MAX_FIELD_LENGTH: Final = 256

_ACCENT: Final = "#41bdf5"


class Stage(Enum):
    """What the setup page should be asking for right now."""

    CREDENTIALS = "credentials"
    TWO_FACTOR = "two_factor"
    READY = "ready"


class SetupState:
    """What the page shows, kept by the add-on and read by the page."""

    def __init__(self) -> None:
        """Start out asking for credentials."""
        self.stage = Stage.CREDENTIALS
        self.message = ""
        self.error = ""
        self.cameras: list[str] = []
        self.camera: str | None = None
        self.streaming = False


LoginHandler = Callable[[str, str], Awaitable[None]]
CodeHandler = Callable[[str], Awaitable[None]]


def _page(body: str, *, title: str) -> web.Response:
    """Wrap page content in the add-on's shell."""
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: transparent; color: inherit;
    display: flex; justify-content: center;
  }}
  .card {{
    width: 100%; max-width: 30rem;
    background: rgba(127, 127, 127, 0.08);
    border: 1px solid rgba(127, 127, 127, 0.22);
    border-radius: 14px; padding: 24px 24px 28px;
  }}
  h1 {{ font-size: 1.3rem; margin: 0 0 4px; }}
  p.sub {{ margin: 0 0 20px; opacity: 0.72; }}
  label {{ display: block; font-weight: 600; margin: 16px 0 6px; }}
  input, select {{
    width: 100%; padding: 11px 12px; font: inherit;
    border-radius: 9px; border: 1px solid rgba(127, 127, 127, 0.4);
    background: rgba(127, 127, 127, 0.08); color: inherit;
  }}
  input:focus, select:focus {{ outline: 2px solid {_ACCENT}; border-color: {_ACCENT}; }}
  button {{
    width: 100%; margin-top: 22px; padding: 12px;
    font: inherit; font-weight: 600; color: #10171f;
    background: {_ACCENT}; border: 0; border-radius: 9px; cursor: pointer;
  }}
  button:hover {{ filter: brightness(1.08); }}
  .banner {{ padding: 11px 13px; border-radius: 9px; margin-bottom: 18px; }}
  .error {{ background: rgba(229, 62, 62, 0.16); border: 1px solid rgba(229, 62, 62, 0.5); }}
  .ok {{ background: rgba(67, 160, 71, 0.16); border: 1px solid rgba(67, 160, 71, 0.5); }}
  .hint {{ font-size: 0.86rem; opacity: 0.7; margin-top: 8px; }}
  dl {{ margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 8px 16px; }}
  dt {{ opacity: 0.7; }} dd {{ margin: 0; font-weight: 600; }}
  code {{
    background: rgba(127, 127, 127, 0.18); padding: 1px 6px; border-radius: 5px;
    font-size: 0.9em;
  }}
</style>
</head>
<body><div class="card">{body}</div></body>
</html>"""
    return web.Response(text=document, content_type="text/html")


def _banners(state: SetupState) -> str:
    """Render the error and status banners, then consume them.

    They are shown once, on the page the redirect lands on. Leaving them set
    would keep an old failure on screen after the flow has already moved past
    it, which reads as though the last thing you did failed.
    """
    out = ""
    if state.error:
        out += f'<div class="banner error">{html.escape(state.error)}</div>'
    if state.message:
        out += f'<div class="banner ok">{html.escape(state.message)}</div>'
    state.error = state.message = ""
    return out


def _credentials_form(state: SetupState) -> str:
    """The first screen: Blink account details."""
    return f"""
<h1>Connect your Blink account</h1>
<p class="sub">Used once, to obtain a session. Your password is never stored.</p>
{_banners(state)}
<form method="post" action="login">
  <label for="username">Email</label>
  <input id="username" name="username" type="email" required autocomplete="username"
         placeholder="you@example.com">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" required
         autocomplete="current-password">
  <button type="submit">Sign in</button>
  <p class="hint">Blink will almost certainly email or text you a code next.</p>
</form>"""


def _two_factor_form(state: SetupState) -> str:
    """The second screen: the code Blink just sent."""
    return f"""
<h1>Enter the code Blink sent</h1>
<p class="sub">Check your email or messages. The code is usually six digits.</p>
{_banners(state)}
<form method="post" action="verify">
  <label for="code">Verification code</label>
  <input id="code" name="code" inputmode="numeric" autocomplete="one-time-code"
         required autofocus placeholder="123456">
  <button type="submit">Verify</button>
  <p class="hint">Codes expire quickly. Reload this page to start over.</p>
</form>"""


def _ready_page(state: SetupState) -> str:
    """The final screen: what to do next."""
    cameras = ", ".join(html.escape(c) for c in state.cameras) or "none found"
    streaming = "streaming now" if state.streaming else "idle, waiting for a viewer"
    return f"""
<h1>Connected</h1>
<p class="sub">The session is stored. You will not need to do this again.</p>
{_banners(state)}
<dl>
  <dt>Cameras</dt><dd>{cameras}</dd>
  <dt>Relaying</dt><dd>{html.escape(state.camera or "not selected")}</dd>
  <dt>State</dt><dd>{streaming}</dd>
</dl>
<p class="hint">Point a bridge at <code>tcp://&lt;host&gt;:9554</code>, then add a
camera entity. The Documentation tab has a copy-pasteable go2rtc snippet.</p>
<p class="hint">To relay a different camera, set <code>camera_name</code> in the
Configuration tab and restart.</p>"""


def _field(data: Mapping[str, Any], name: str) -> str:
    """Read one posted field, trimmed and length-capped."""
    value = data.get(name)
    return str(value).strip()[:_MAX_FIELD_LENGTH] if value else ""


def build_app(
    state: SetupState, on_login: LoginHandler, on_code: CodeHandler
) -> web.Application:
    """Build the one-page setup application."""

    async def index(_: web.Request) -> web.Response:
        renderers = {
            Stage.CREDENTIALS: _credentials_form,
            Stage.TWO_FACTOR: _two_factor_form,
            Stage.READY: _ready_page,
        }
        return _page(renderers[state.stage](state), title="Blink Camera Streamer")

    async def login(request: web.Request) -> web.Response:
        data = await request.post()
        username, password = _field(data, "username"), _field(data, "password")
        if not username or not password:
            state.error = "Both fields are required."
        else:
            await on_login(username, password)
        raise web.HTTPFound(_self(request))

    async def verify(request: web.Request) -> web.Response:
        data = await request.post()
        code = _field(data, "code")
        if not code:
            state.error = "Enter the code Blink sent you."
        else:
            await on_code(code)
        raise web.HTTPFound(_self(request))

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/login", login)
    app.router.add_post("/verify", verify)
    return app


def _self(request: web.Request) -> str:
    """The page's own URL as the browser sees it, behind Ingress's path prefix."""
    return request.headers.get("X-Ingress-Path", "") + "/"


async def serve(app: web.Application, port: int = INGRESS_PORT) -> web.AppRunner:
    """Start the setup page and return its runner so the caller can stop it."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    _LOGGER.info("Setup page available on the add-on's Web UI tab")
    return runner
