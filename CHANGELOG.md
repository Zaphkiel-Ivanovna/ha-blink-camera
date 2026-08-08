# Changelog

All notable changes to this add-on are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Supervisor treats `version` in `config.yaml` as the sole update trigger, so every
release bumps it.

## [Unreleased]

## [0.2.0] - 2026-08-08

### Added

- A setup page in the add-on's **Web UI** tab. Enter your Blink email, password
  and the code Blink sends, and the add-on is configured — no CLI on another
  machine, no session file copied in over Samba. The password is never stored.
- A sidebar panel (`mdi:cctv`) and configuration labels that explain the fields
  can be left alone.

### Changed

- An unconfigured install now serves the setup page instead of idling with a
  message about options nobody should have to edit.
- Login and verification failures are classified and shown on the page. Only
  the two-factor challenge was handled before, so a network blip mid-login
  produced a bare HTTP 500 with no explanation.

### Fixed

- Setup banners are shown once rather than persisting, so an old failure no
  longer stays on screen after the flow has moved past it.

## [0.1.0] - 2026-08-08

First release. Installable as a custom add-on repository; authentication is
bootstrapped from a session file produced elsewhere.

### Added

- Relay one Blink camera liveview as MPEG-TS, copied byte for byte and never
  transcoded. Published on host port 9554.
- Open a Blink session only while a consumer is connected, and release it after
  a short linger once the last one leaves.
- Renegotiate the Blink session without dropping the downstream connection, so a
  bridge stays attached across the cloud's session expiry.
- Rate limit liveview commands: a minimum interval, a cooldown after each close,
  and a ceiling of 40 per hour.
- Classify failures as transient or fatal. Transient failures retry with
  exponential backoff; fatal ones log one actionable line and idle with a
  repeating warning. The add-on never exits for a condition a human must fix.
- Import a session dropped in the add-on's config folder, then delete the source
  file so a live token is not left in a folder other add-ons can read.
- Scrub the account email, password and every token from the log, including from
  tracebacks, and silence the `blinkpy` modules that dump whole HTTP responses.
- Store the session cache at mode `0600`, written atomically, password stripped.
- Discard a cached session that cannot prove it belongs to the configured
  account.
- Ship an AppArmor profile; security rating 6 with no elevated privileges.
- `tools/import_session.py`, converting a prototype credentials file into a
  session cache and refusing to produce one without a username.

### Fixed

- Two upstream `blinkpy` 0.25.9 bugs, patched at runtime and covered by tests
  that fail if the patch is reverted:
  [#1262](https://github.com/fronzbot/blinkpy/issues/1262), where `recv()` frames
  with `read(n)` and treats an ordinary short read as fatal; and `poll()`, which
  tears the session down on the first non-908 status.

### Known issues

- Each renegotiation costs roughly ten seconds of frozen picture. That is Blink's
  liveview startup latency; close-and-reopen is the only mechanism it offers.
- One camera per install, one consumer at a time.
- No two-factor login without an imported session file.

[Unreleased]: https://github.com/Zaphkiel-Ivanovna/ha-blink-camera/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Zaphkiel-Ivanovna/ha-blink-camera/releases/tag/v0.2.0
[0.1.0]: https://github.com/Zaphkiel-Ivanovna/ha-blink-camera/releases/tag/v0.1.0
