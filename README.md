<p align="center">
  <img src="logo.png" alt="Blink Camera Streamer for Home Assistant" width="250">
</p>

# Blink Camera Streamer for Home Assistant

[![Home Assistant add-on](https://img.shields.io/badge/Home%20Assistant-Add--on-41BDF5.svg?style=for-the-badge)](https://www.home-assistant.io/getting-started/concepts-terminology/#add-ons)
[![Validate](https://img.shields.io/github/actions/workflow/status/Zaphkiel-Ivanovna/ha-blink-camera/validate.yml?branch=main&style=for-the-badge&label=validate)](https://github.com/Zaphkiel-Ivanovna/ha-blink-camera/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/Zaphkiel-Ivanovna/ha-blink-camera?style=for-the-badge)](LICENSE)

A Home Assistant add-on that opens a liveview on a Blink camera and
re-broadcasts it as MPEG-TS on your LAN, so Home Assistant, go2rtc or Frigate
can consume it as an ordinary camera.

The stream is copied byte for byte — H.264 1920x1080 plus AAC 16 kHz mono, about
2 Mbit/s. Nothing is transcoded.

> **"Local" here means locally *re-broadcast*, never locally *captured*.** Blink
> cameras expose no service on your network; the video always travels through
> Blink's cloud. This add-on gives you a LAN endpoint for a cloud stream, not a
> way around the cloud.

## Use it alongside the official Blink integration

This add-on does one thing: live video. For everything else — arming, motion
detection, snapshots, clips, battery and temperature — install Home Assistant's
built-in [Blink integration](https://www.home-assistant.io/integrations/blink/).
The two are complementary by design, and its own documentation states that it
"does NOT allow for live viewing of your Blink camera within Home Assistant".

| You want | Use |
|---|---|
| Live video | **this add-on** |
| Arm / disarm the system | Blink integration — alarm control panel |
| Motion detection on/off per camera | Blink integration — switch |
| Motion, armed and battery state | Blink integration — binary sensors |
| Temperature, Wi-Fi strength | Blink integration — sensors |
| Snapshots and recorded clips | Blink integration — `blink.trigger_camera`, `blink.record`, `blink.save_video` |

Camera control deliberately stays out of this add-on. An add-on is a container:
it cannot register entities in Home Assistant at all, so exposing switches and
sensors from here would mean reimplementing a first-party integration on top of
MQTT discovery — more moving parts, for something you already have.

## Two pieces, and you need both

| | What it is | How you install it |
|---|---|---|
| **Add-on** | Talks to Blink, re-broadcasts the liveview on your LAN | Add-on store (button below) |
| **Integration** | Turns that stream into a `camera.*` entity with its own device | HACS |

The split is not a preference. An add-on is a container: it has no access to
Home Assistant's entity registry and *cannot* create entities. Something inside
Home Assistant has to do that, which means an integration. Without it you can
still use a Generic Camera pointed at the stream — you just get an entity filed
under "Generic Camera" with no connection to this project.

## Installation

### The add-on, in one click

[![Add this repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository=https%3A%2F%2Fgithub.com%2FZaphkiel-Ivanovna%2Fha-blink-camera)

The button adds this repository to the add-on store on **your** instance. Then
open **Settings → Add-ons → Add-on store**, find *Blink Camera Streamer* and
install it.

> HACS does not distribute add-ons — Supervisor's own add-on store does, and the
> button above is its equivalent of the HACS one. The companion integration
> below *is* distributed by HACS.

<details>
<summary>If the button does not work</summary>

**Settings → Add-ons → Add-on store → ⋮ → Repositories**, paste
`https://github.com/Zaphkiel-Ivanovna/ha-blink-camera`, then **Add**.

</details>

### The integration, through HACS

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Zaphkiel-Ivanovna&repository=ha-blink-camera&category=integration)

Same repository, added to HACS as a custom repository of category
**Integration**. Then **Settings → Devices & services → Add integration →
Blink Camera Streamer**, and give it the stream URL the add-on's documentation
tells you to use.

The two live in one repository on purpose: they are versioned and released
together, and HACS and Supervisor each read only the files that concern them.

### Requirements

- Home Assistant OS or Supervised — the add-on store needs Supervisor.
- `aarch64` or `amd64`. There is no `armv7` build, because Home Assistant
  publishes no `armv7` Python base image.
- A bridge that turns MPEG-TS into RTSP. The standalone **go2rtc** add-on is the
  tested one; see [DOCS.md](DOCS.md).

## Getting started

[DOCS.md](DOCS.md) is the user guide and covers the whole path: the one-time
session import, the go2rtc bridge, what quietly defeats on-demand streaming, and
every failure state.

The short version:

1. Authenticate once elsewhere and drop the resulting session file into
   `/addon_configs/<slug>/session.json`. This version cannot answer a two-factor
   prompt on its own — Blink keeps that state in memory, so the code has to
   reach a process that is already running.
2. Fill in `username`, and `camera_name` if the account has more than one camera.
3. Bridge the relay into go2rtc and add a camera entity.

## How it behaves

The add-on opens a Blink liveview **only while something is connected**, and
releases it shortly after the last consumer leaves. Every liveview is a command
against an undocumented cloud API with no published quota, so it rate-limits
itself: a minimum interval, a cooldown after each close, and a ceiling of 40
sessions per hour.

It never exits because of a problem a human has to fix. Under Supervisor an exit
is an immediate restart, and a restart loop against Blink's login endpoint is how
an account gets locked out. It logs one actionable line and idles instead.

| Situation | Behaviour |
|---|---|
| Nothing connected | No Blink call at all |
| A consumer connects | Session opens on demand |
| Session ends, for any reason | Reopens without dropping your connection |
| Last consumer leaves | Session released after a short linger |
| Consumer reconnects quickly | Same session reused, no new Blink call |
| Network drops | Retries with growing backoff, never fatal |
| Wrong credentials or camera name | One line, then idle |
| Add-on stopped | Clean shutdown, even mid-stream |

One camera per installation: Supervisor installs one copy per slug, and the
relay's host port is fixed, so a second instance cannot be installed.

## Security

The add-on requests **no elevated privileges** — no `privileged`, no
`full_access`, no `docker_api`, no `host_network`, no `host_pid`, and the default
`hassio_role`. It ships an AppArmor profile, which gives it a Supervisor
security rating of **6**.

Your password is never written to disk. The session cache holds tokens only, at
mode `0600`, written atomically. The account email, the password and every token
are scrubbed from the log before it is written, including from tracebacks,
because add-on logs end up verbatim in Home Assistant support bundles.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

No test ever calls the real Blink cloud: there are no credentials in CI, and the
socket layer is exercised against a real `asyncio` server rather than a mock.

Run it outside a container with the three development-only environment
variables, none of which are set in the add-on:

```bash
mkdir -p /tmp/blinkdata
uv run python tools/import_session.py ~/.blink_creds.json /tmp/blinkdata/session.json
printf '{"username":"you@example.com","password":"","camera_name":""}' > /tmp/blinkdata/options.json

ADDON_DATA_DIR=/tmp/blinkdata ADDON_RELAY_PORT=18554 uv run python -m ha_blink_camera.cli
ffplay -f mpegts -fflags nobuffer -flags low_delay tcp://127.0.0.1:18554
```

Inside the container the relay always listens on 8554; the add-on publishes it
on the host as 9554, to stay clear of go2rtc.

### Layout

```
src/ha_blink_camera/
  config.py            options.json -> typed Config
  blink_client.py      the only module that calls blinkpy
  blinkpy_patches.py   two upstream bug patches, isolated
  stream_relay.py      TCP server, fan-out, session lifecycle
  exceptions.py        BlinkCameraError -> Transient / Fatal
  logging_setup.py     stdout logging and secret redaction
  cli.py               entrypoint
rootfs/                S6 service definition
tools/                 one-shot session importer
```

`CLAUDE.md` records the architecture and the facts that are settled;
[PLAN.md](PLAN.md) holds the development plan and the reasoning behind it.

## Acknowledgements

Built on [blinkpy](https://github.com/fronzbot/blinkpy). The IMMI framing was
first documented by
[blink-liveview-middleware](https://github.com/amattu2/blink-liveview-middleware).

## License

MIT — see [LICENSE](LICENSE).
