---
name: debug-liveview
description: >
  Diagnose a broken, frozen or endlessly reconnecting Blink liveview stream.
  Use when the RTSP or MPEG-TS feed is stuck, the camera shows no image in Home
  Assistant, or logs show repeated reconnects, "Insufficient data for payload",
  session expiry, or a dead go2rtc/MediaMTX bridge.
allowed-tools: Read, Grep, Glob, Bash(docker logs:*), Bash(ffprobe:*)
---

# Debug a liveview stream

Work down this list in order — it is sorted by how often each cause is the real
one. Report findings as: symptom -> cause -> the exact line to check -> fix.

## 1. Distinguish expiry from failure

Blink sessions expire after ~5-6 minutes. Reconnects at that cadence are
**normal**. Reconnects every few seconds are a bug. Get the actual cadence from
the logs before theorising.

## 2. Are the blinkpy patches applied?

The add-on logs a startup line confirming patch application. If it is missing,
or if the installed blinkpy version changed, the guards in `blinkpy_patches.py`
may have skipped patching.

- `Insufficient data for payload: N bytes, expected 1316` means the `recv()`
  patch is **not** active. Upstream frames with `read(n)`, which returns short
  reads; the loop then breaks and the stream dies. Must be `readexactly(n)`.
- A stream that dies right after a single failed poll means the `poll()` patch
  is not active: upstream exits the loop on the first non-908 status and its
  `finally` sends `request_command_done`, which asks Blink to close the session.

## 3. Is the relay port stable?

`.claude/rules/30-python-async.md` requires a constant `host:port` across
renegotiations. If the port moves, the downstream bridge silently stops
reconnecting even though the add-on looks healthy.

## 4. Is the remux bridge alive?

- go2rtc: check the `exec:` entry's ffmpeg command, and that
  `-f mpegts -i tcp://<host>:<PORT>` matches the relay's real port.
- MediaMTX: check `runOnInitRestart: yes`, otherwise it never retries.
- Either way the command must be `-c copy`. If someone added a transcode, CPU
  will be saturated and latency will grow without bound.

## 5. Is a fatal error being retried as transient?

Bad credentials must surface, not loop forever. Check that the `except*` handler
routes `FatalConfigError` to exit and `TransientBlinkError` to retry
(`exceptions.py`).

## 6. Is the stream itself sane?

Record ten seconds and measure it:

```
ffprobe -hide_banner <file>
```

Expected: H.264 High 1920x1080, ~14 fps, AAC 16 kHz mono, ~1.15 Mbit/s total
(~0.039 bit/pixel). A much lower bitrate points at Wi-Fi signal, not at this
add-on. Artifacts only in the first seconds mean the client attached mid-GOP —
a relay pre-buffer issue, not a camera issue.

## What is not worth investigating

The Blink Mini exposes no LAN service and the stream always transits Blink's
cloud relay. "The camera is unreachable locally" is expected, not a bug. Do not
chase a local capture path.
