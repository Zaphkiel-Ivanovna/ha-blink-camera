# Blink Camera Streamer for Home Assistant

Opens a liveview on one Blink camera and re-broadcasts it as raw MPEG-TS on your
LAN, so Home Assistant, go2rtc or Frigate can consume it as a normal camera.

The stream is passed through untouched — H.264 High 1920x1080 at ~14 fps plus
AAC 16 kHz mono, about 2 Mbit/s. The add-on remuxes nothing and transcodes
nothing.

> **"Local" here means locally *re-broadcast*, never locally *captured*.** Blink
> cameras expose no service on your network; the video always travels through
> Blink's cloud. This add-on does not change that. It gives you a LAN endpoint
> for a cloud stream.

## What this add-on does not do

It relays live video, and nothing else. Arming, motion detection, snapshots,
clips, battery and temperature all come from Home Assistant's built-in
[Blink integration](https://www.home-assistant.io/integrations/blink/), which
covers everything except live viewing — its documentation says so explicitly.
Install both; they are complementary.

An add-on cannot register entities in Home Assistant, so camera control could
only be added here by reimplementing a first-party integration over MQTT
discovery. That is not a trade worth making.

## Before you start

**One camera, and only one.** Supervisor identifies an add-on by its slug and
installs exactly one copy of it, and the relay's host port is fixed — so a
second instance is not something you can install, not merely something that is
untested. Multiple cameras need either separate repositories with distinct
slugs, or a future version that multiplexes them. Neither exists yet.

## First run

Open the add-on's **Web UI** tab, enter your Blink email and password, then the
code Blink sends you. That is the whole setup — the session is stored, your
password is not, and you will not be asked again.

Blink keeps its login state in memory for the duration of the exchange, which is
why the code has to be typed into a page served by the running add-on rather
than pasted into an option or a file.

<details>
<summary>Importing a session instead of signing in</summary>

## Importing a session by hand

### 1. Produce a session file

On a computer with Python, using the prototype CLI in this project's sibling
repository:

```bash
python blink_live.py list          # asks for your 2FA code the first time
```

That writes `~/.blink_creds.json`. **It contains your password in plaintext**,
because that is what `blinkpy`'s own `save()` does. The importer below strips it.

### 2. Import it

```bash
uv run python tools/import_session.py ~/.blink_creds.json ./session.json
```

The importer keeps only the fields needed to resume a session, drops the
password, and writes the file with mode `0600`. It refuses to produce a file
with no username, because the add-on cannot verify which account such a session
belongs to and would discard it.

### 3. Drop it in the add-on's config folder

Copy `session.json` into `/addon_configs/local_blink_camera/`. That folder is
reachable from the Samba add-on, the Terminal & SSH add-on and the File editor.
The add-on's *data* volume is private to the add-on, so this config folder is
the only place a human can hand it a file.

On the next start the add-on imports the session into its own storage at mode
`0600` and **deletes the file you dropped** — a live `refresh_token` should not
be left sitting in a folder every other add-on can read. You will see:

```
Imported the session from /config/session.json and removed it
```

</details>

## Options

| Option | Meaning |
|---|---|
| `username` | Filled in for you when you sign in from the Web UI. Set it by hand only when importing a session; it must match, or the session is discarded as belonging to someone else. |
| `password` | Not needed and not stored. Sign in from the Web UI instead. |
| `camera_name` | The camera's name exactly as shown in the Blink app. Leave empty if the account has one camera. |

The log should show `Authenticated`, then `Relaying camera '<name>' on demand`,
then `Relay listening on tcp://0.0.0.0:8554` (published on the host as 9554). No Blink session is opened until
something actually connects.

## Connecting it to Home Assistant

The add-on speaks raw MPEG-TS over TCP, and it needs an ffmpeg bridge in front
of it.

> **The go2rtc that Home Assistant bundles will not do.** It generates its
> configuration from a hardcoded template and reads no user file
> (`homeassistant/components/go2rtc/server.py`), so it cannot be given the
> command below. Pointing a Generic Camera straight at `tcp://…` gets further
> than you would expect — Home Assistant accepts the source — but the bundled
> go2rtc then fails with `AAC with no global headers is currently not
> supported`, because Blink's audio is ADTS-framed and go2rtc's own ffmpeg
> invocation has no `-bsf:a aac_adtstoasc`. Verified on Home Assistant on
> 2026-08-08.
>
> Use the **standalone go2rtc add-on**, whose configuration you control, and
> point the go2rtc integration at it with its `url` option.

Install it from `https://github.com/AlexxIT/hassio-addons` — the repository of
go2rtc's own author — picking the plain **go2rtc** add-on, not the `-master`,
`-dev`, `-hardware` or `-rockchip` variants. It has no add-on options; it reads
`/config/go2rtc.yaml`.

Add this to that file:

```yaml
streams:
  blink_bureau:
    - "exec:ffmpeg -hide_banner -fflags +genpts -f mpegts -i tcp://HOST:9554 -c copy -bsf:a aac_adtstoasc -rtsp_transport tcp -f rtsp {output}"
```

Replace `HOST` with the address of the machine running this add-on — its Home
Assistant host IP, not `127.0.0.1`.

The port is **9554**, and it dodges two occupied ones. The standalone go2rtc
add-on runs with host networking and needs 8554 for RTSP — publishing 8554 from
this add-on disables go2rtc's RTSP module outright, and every `exec:` stream
then fails because `{output}` has nowhere to point. 18554 is taken too: the
go2rtc bundled with Home Assistant offsets its ports by 10000.

**On a low-powered box, drop the audio instead**, with `-an -c:v copy` in place
of `-c copy -bsf:a aac_adtstoasc`. WebRTC cannot carry AAC, so go2rtc transcodes
it to Opus — the only transcode in the entire chain, and it lands on whichever
machine runs go2rtc. For a security camera microphone that is rarely a good
trade.

Then add a Generic Camera pointing at go2rtc, or let the go2rtc integration
discover it.

Three details in that command are not optional, each learned the hard way:

- **`-bsf:a aac_adtstoasc`** — Blink sends AAC in ADTS framing, and ffmpeg's
  RTSP muxer rejects it outright with `AAC with no global headers is currently
  not supported`. Without this filter the bridge fails to start at all.
- **`-fflags +genpts`** — every time the Blink session is renegotiated the
  timestamps restart, and ffmpeg logs a discontinuity. This regenerates them.
- **`-c copy`** — never transcode. The stream is already in a standard container,
  and transcoding 1080p on a Home Assistant box is a good way to melt it.

If `127.0.0.1` does not reach the add-on from where go2rtc runs, use your Home
Assistant machine's LAN IP instead. The port is published on the host, so the
host's own address always works.

## Things that will cost you

The add-on opens a Blink liveview **only while something is connected**, and
closes it shortly after the last consumer leaves. That is deliberate: every
liveview is a command against an undocumented cloud API with no published quota.

Two common setups defeat that entirely and can generate *more* cloud traffic
than an always-on design:

- **A camera card that polls for thumbnails.** Each poll connects, which opens a
  session. Use a card that only streams when you open it.
- **Frigate with `record: always`.** That holds the connection permanently, so
  the add-on renegotiates around the clock — roughly 24 liveview commands an
  hour, every hour.

There is a ceiling of 40 sessions per hour. Reaching it is not normal; if the
log says `Hourly session limit reached`, something is reconnecting in a loop.

## What it does when things go wrong

The add-on never exits because of a problem you have to fix. Under Supervisor an
exit is an immediate restart, and a restart loop against Blink's login endpoint
is how an account gets locked out. It logs one actionable line and idles instead,
repeating it every fifteen minutes so it does not look healthy when it is not.

| Situation | Behaviour |
|---|---|
| Nothing connected | No Blink call at all |
| A consumer connects | Session opens on demand |
| Session reaches its deadline | Renegotiates; your connection is not dropped |
| Blink ends the session early | Reopens; your connection is not dropped |
| Last consumer leaves | Session released after a short linger |
| Consumer reconnects quickly | Same session reused, no new Blink call |
| Network drops | Retries with growing backoff, never fatal |
| Session delivers no video | Gives up on it and backs off |
| Hourly ceiling reached | Waits, and says so |
| Wrong credentials | One line, then idle — no further Blink calls |
| Wrong camera name | One line naming the cameras it did find, then idle |
| Not configured yet | One line, then idle |
| Add-on stopped | Clean shutdown, even mid-stream |

Expect a gap of roughly ten seconds of frozen picture whenever the session is
renegotiated, which happens every few minutes. That is Blink's own startup
latency for a new liveview, not add-on overhead, and closing-and-reopening is
the only mechanism Blink offers.

## Security

The add-on requests **no elevated privileges**: no `privileged`, no
`full_access`, no `docker_api`, no `host_network`, no `host_pid`, and the default
`hassio_role`. It makes outbound HTTPS connections to Blink and serves one local
TCP port. Nothing else.

It ships a custom AppArmor profile, which takes the Supervisor security rating
from the baseline 5 to **6**. There are no deviations to justify.

Your credentials are handled as follows:

- The password is never written to disk. The session cache holds tokens only,
  with mode `0600`, written atomically so a live token never sits in a
  world-readable file.
- The account email, the password and every token are scrubbed from the log
  before it is written, including from tracebacks. `blinkpy`'s own modules that
  dump whole HTTP responses at ERROR are silenced, because add-on logs end up
  verbatim in Home Assistant support bundles.
- A session belonging to a different account is discarded rather than reused.

## Known limitations

- One camera per install.
- One consumer at a time is what has been tested. More may work; nothing checks.
- Recordings, clips, snapshots and motion events are out of scope — this relays
  live video and nothing else.
- No `armv7`: Home Assistant publishes no `base-python` image for it.
