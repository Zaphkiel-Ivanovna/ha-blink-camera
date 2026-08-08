"""The Ingress setup page: the only way a two-factor code can reach the add-on."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ha_blink_camera.setup_ui import SetupState, Stage, build_app


class Recorder:
    """Captures what the page hands back, and decides what happens next."""

    def __init__(self, state: SetupState) -> None:
        """Watch `state` and start out accepting everything."""
        self.state = state
        self.logins: list[tuple[str, str]] = []
        self.codes: list[str] = []
        self.needs_code = False
        self.code_ok = True

    async def on_login(self, username: str, password: str) -> None:
        """Record a sign-in attempt and advance the page."""
        self.logins.append((username, password))
        self.state.stage = Stage.TWO_FACTOR if self.needs_code else Stage.READY

    async def on_code(self, code: str) -> None:
        """Record a submitted code and advance, or report a rejection."""
        self.codes.append(code)
        if self.code_ok:
            self.state.stage = Stage.READY
        else:
            self.state.error = "That code was not accepted. Try the newest one."


@pytest.fixture
async def page() -> tuple[TestClient, SetupState, Recorder]:
    """A running setup page, its state, and the recorder behind it."""
    state = SetupState()
    recorder = Recorder(state)
    app = build_app(state, recorder.on_login, recorder.on_code)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, state, recorder


async def test_a_fresh_install_asks_for_credentials(page: tuple) -> None:
    """The whole point: no CLI, no file drop, just a form."""
    client, _, _ = page

    body = await (await client.get("/")).text()

    assert "Connect your Blink account" in body
    assert 'name="username"' in body and 'name="password"' in body


async def test_the_password_field_is_masked(page: tuple) -> None:
    """A password typed into a browser must not be shown in the clear."""
    client, _, _ = page

    body = await (await client.get("/")).text()

    assert 'type="password"' in body


async def test_signing_in_reaches_the_handler(page: tuple) -> None:
    """The credentials go to the live process, which is the entire design."""
    client, _, recorder = page

    await client.post("/login", data={"username": "me@example.com", "password": "pw"})

    assert recorder.logins == [("me@example.com", "pw")]


async def test_a_two_factor_prompt_appears_when_blink_asks(page: tuple) -> None:
    """Blink's code has to be collectable, or the add-on cannot be set up at all."""
    client, _, recorder = page
    recorder.needs_code = True

    await client.post("/login", data={"username": "me@example.com", "password": "pw"})
    body = await (await client.get("/")).text()

    assert "Enter the code Blink sent" in body
    assert 'autocomplete="one-time-code"' in body


async def test_the_code_reaches_the_handler(page: tuple) -> None:
    """This is the step no restart, option or dropped file can perform."""
    client, state, recorder = page
    state.stage = Stage.TWO_FACTOR

    await client.post("/verify", data={"code": "123456"})

    assert recorder.codes == ["123456"]


async def test_a_rejected_code_is_reported_and_retryable(page: tuple) -> None:
    """A typo must not dead-end the setup."""
    client, state, recorder = page
    state.stage, recorder.code_ok = Stage.TWO_FACTOR, False

    await client.post("/verify", data={"code": "000000"})
    body = await (await client.get("/")).text()

    assert "not accepted" in body
    assert 'name="code"' in body, "the form is still there to try again"


def test_the_code_is_never_echoed_back(page: tuple) -> None:
    """Codes and passwords must not survive into the rendered page."""
    state = SetupState()
    state.stage = Stage.TWO_FACTOR
    state.error = "That code was not accepted. Try the newest one."

    from ha_blink_camera.setup_ui import _two_factor_form

    assert "000000" not in _two_factor_form(state)


async def test_empty_fields_do_not_reach_the_handler(page: tuple) -> None:
    """Blink should not be called with nothing."""
    client, _, recorder = page

    await client.post("/login", data={"username": "", "password": ""})

    assert recorder.logins == []


async def test_state_is_escaped_into_the_page(page: tuple) -> None:
    """Camera names come from Blink; they are data, not markup."""
    client, state, _ = page
    state.stage = Stage.READY
    state.cameras = ["<script>alert(1)</script>"]

    body = await (await client.get("/")).text()

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


async def test_the_ready_page_says_what_to_do_next(page: tuple) -> None:
    """Finishing the sign-in is not finishing the setup."""
    client, state, _ = page
    state.stage, state.camera, state.cameras = Stage.READY, "Office", ["Office"]

    body = await (await client.get("/")).text()

    assert "Connected" in body
    assert "9554" in body and "go2rtc" in body


async def test_redirects_respect_the_ingress_path_prefix(page: tuple) -> None:
    """Ingress serves the add-on under a random prefix; a bare / would 404."""
    client, _, _ = page
    prefix = "/api/hassio_ingress/abc123"

    response = await client.post(
        "/login",
        data={"username": "me@example.com", "password": "pw"},
        headers={"X-Ingress-Path": prefix},
        allow_redirects=False,
    )

    assert response.headers["Location"] == f"{prefix}/"


async def test_overlong_input_is_truncated(page: tuple) -> None:
    """A browser can post anything; nothing unbounded reaches Blink."""
    client, _, recorder = page

    await client.post("/login", data={"username": "a" * 5000, "password": "pw"})

    assert len(recorder.logins[0][0]) <= 256


def test_the_app_exposes_only_three_routes() -> None:
    """A first-run page is not a place to grow an API."""
    state = SetupState()

    async def noop_login(_u: str, _p: str) -> None: ...

    async def noop_code(_c: str) -> None: ...

    app = build_app(state, noop_login, noop_code)
    routes = {
        (r.method, r.resource.canonical)
        for r in app.router.routes()
        if isinstance(r.resource, web.Resource)
    }

    assert routes - {("HEAD", "/")} == {
        ("GET", "/"),
        ("POST", "/login"),
        ("POST", "/verify"),
    }
