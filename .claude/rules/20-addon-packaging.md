---
paths:
  - "config.yaml"
  - "Dockerfile"
  - "rootfs/**"
  - "apparmor.txt"
  - "repository.yaml"
  - "build.yaml"
  - ".github/workflows/**"
---

# Add-on Packaging

Terminology note: Home Assistant renamed "add-ons" to "apps" in the UI and docs
(2026.2), but **every file name and `config.yaml` key below is unchanged** —
that is what Supervisor reads. Say "app" in prose if you like; never rename a
key to match.

## Repository layout

```
ha-blink-camera/              (git root)
  repository.yaml             # required at git root; `name:` is its only required key
  config.yaml
  Dockerfile
  DOCS.md
  README.md
  CHANGELOG.md                # keepachangelog.com format
  icon.png                    # 1:1, ~128x128
  logo.png                    # ~250x100
  apparmor.txt
  translations/en.yaml
  rootfs/etc/s6-overlay/s6-rc.d/
    blink-camera/{run,type,dependencies.d/base}
    user/contents.d/blink-camera
```

## config.yaml

Required keys: `name`, `version`, `slug`, `description`, `arch`.

```yaml
name: Blink Camera Streamer
version: "0.1.0"
slug: blink_camera
description: Re-broadcasts a Blink camera liveview as an RTSP/MPEG-TS stream
arch: [aarch64, amd64]
startup: services
boot: auto
init: false                 # REQUIRED with S6 base images, or the add-on will not start
map:
  - type: data
    read_only: false
options:
  username: ""
  password: ""
  camera_name: ""
schema:
  username: str?
  password: password?
  camera_name: str?
ports:
  8554/tcp: 9554
```

No `relay_port` option: it could not change the container-side literal in
`ports:`, so it would be a trap rather than a setting. Pick the **host** port
with care — 8554 belongs to the standalone go2rtc add-on (host networking,
RTSP) and 18554 to the go2rtc Home Assistant bundles, which offsets its ports by
10000. Claiming either one silently breaks go2rtc rather than failing loudly.

Make every option optional. A required option whose default is empty makes
Supervisor refuse to start the add-on, which replaces your own actionable error
message with a schema failure nobody can act on.

Schema validator syntax: `str`, `str(min,max)`, `int(min,max)`, `float`, `bool`,
`email`, `url`, `password`, `port`, `match(REGEX)`, `list(a|b|c)`, `device`,
and a trailing `?` to mark a key optional.

**Keys that do not exist — never invent them:** there is no `permissions:` key
(privileges are the discrete booleans in `10-security-secrets.md`), no
`documentation:`, no `issue_tracker:`, no `codenotary:`, and no add-on-level
`healthcheck:` (a `HEALTHCHECK` belongs in the Dockerfile if used at all). A
single `url:` key covers the repository/homepage link.

## Dockerfile — and no build.yaml

This is a new repository, so skip `build.yaml` entirely: it was deprecated by
the 2026-04-02 Docker BuildKit migration. Supervisor still reads it for legacy
compatibility, but the current scaffold does not use it.

**The base image does NOT ship `uv`** — verified 2026-08-08 against
`3.13-alpine3.24-2026.06.1`, which has Python 3.13.14, pip, bashio and s6, but
no uv. Copy it in from the official image, or `uv sync` fails at the first line.
The image publishes **only linux/amd64 and linux/arm64**, which is why `arch:`
lists aarch64 and amd64 and nothing else — there is no armv7 base to build on.

```dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.06.1
FROM ${BUILD_FROM}

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /bin/

ARG TARGETARCH
RUN if [ -z "${TARGETARCH}" ]; then \
      echo "TARGETARCH not set, build with Docker BuildKit" && exit 1; \
    fi

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-install-project

COPY src /app/src
RUN uv sync --locked

COPY rootfs /

LABEL \
    org.opencontainers.image.title="Blink Camera Streamer" \
    org.opencontainers.image.description="Re-broadcasts Blink liveview as RTSP" \
    org.opencontainers.image.licenses="Apache-2.0"
```

Do not override `ENTRYPOINT` — `/init` (the S6 entrypoint) is baked into the
base image. Do not add a `USER` directive: S6 needs PID 1, and no
HA-maintained Dockerfile uses one.

## S6 service

Native v3 `s6-rc.d` layout:

`rootfs/etc/s6-overlay/s6-rc.d/blink-camera/run`:

```sh
#!/command/with-contenv bashio
bashio::log.info "Starting Blink camera streamer"
exec /app/.venv/bin/python -m ha_blink_camera.cli
```

Note the interpreter path: bare `python3` is the *system* interpreter and does
not see the project's dependencies, so the service dies immediately.

`.../blink-camera/type` contains the single word `longrun`. Register the
service by creating the empty file
`rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/blink-camera`.

The official example scaffold still teaches the older `services.d`/`cont-init.d`
layout; both coexist in production. This repo picks v3 because actively
maintained add-ons use it. Judgment call, documented here on purpose.

## bashio

Use the **dot** form: `bashio::log.info`, `bashio::log.warning`,
`bashio::config`, `bashio::config.true`, `bashio::exit.nok`.
`bashio::log::info` and `bashio::addon::option` (double colon after the
namespace) do not exist — they are a common fabrication.

Reading options in shell is `bashio::config 'key'`; the Python side reads
`/data/options.json` directly through `config.py`.

## AppArmor

Ship `apparmor.txt` with the profile named after the slug (`blink_camera`),
copied from the template at developers.home-assistant.io — **copied, not
reconstructed from memory.** The template opens with

```
  # Capabilities
  file,
  signal (send) set=(kill,term,int,hup,cont),
```

and omitting that `file,` rule means only explicitly listed paths are permitted:
the container then dies at boot with `/bin/sh: can't open '/init': Permission
denied`. Learned the hard way on 2026-08-08.

Two more things that cost an hour each:

- `ha apps rebuild` does **not** reload the AppArmor profile. Only an
  uninstall/reinstall does.
- `ha addons reload` does **not** discover a new local add-on; it only refreshes
  remote repositories. Use `ha store reload`.

It raises the security rating by one and costs nothing.

## CI

Use `home-assistant/builder`'s composite actions
(`prepare-multi-arch-matrix`, `build-image`, `publish-multi-arch-manifest`) —
not the retired monolithic `home-assistant/builder@master`. Pin every
third-party action to a full SHA with a trailing version comment.

Consider `hassio-addons/workflows`' reusable CI workflow as the lint+build
gate: it already runs the add-on linter, hadolint, shellcheck, yamllint,
prettier and zizmor.

**Verify the exact action versions and SHAs when wiring CI** — they were only
checked once, on 2026-07-31.
