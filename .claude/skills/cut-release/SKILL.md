---
name: cut-release
description: >
  Cut a new release: bump config.yaml's version, move the CHANGELOG Unreleased
  section under the new version, and tag the commit. Use when the user
  explicitly asks to release, cut a release, bump the version or publish.
argument-hint: <new-version> [--breaking]
allowed-tools: Read, Edit, Bash(git tag:*), Bash(git log:*), Bash(git status:*)
disable-model-invocation: true
---

# Cut a release

Releasing is a deliberate human action. This skill never auto-triggers, and it
never pushes anything — the last step is a command for the user to run.

## Steps

1. **Validate** that the argument is a semantic version (`MAJOR.MINOR.PATCH`).
   Refuse anything else, including a leading `v`.

2. **Never release on a red build.** Run the `check` skill first. If it fails,
   stop and report — do not bump anything.

3. **`config.yaml`** — set `version:` to the new value. `version` is Supervisor's
   only update trigger, so this bump *is* the release. If `--breaking` was
   passed, append the version to `breaking_versions:` so Supervisor forces a
   manual update.

4. **`CHANGELOG.md`** — move everything under `## [Unreleased]` into a new
   `## [<version>] - <YYYY-MM-DD>` section, and leave `[Unreleased]` empty with
   its usual subsections. Use today's date; ask if you cannot determine it.

5. **Reminder** — if `image:` is set in `config.yaml`, the pushed Docker tag must
   equal this version exactly, or Supervisor will pull the wrong image.

6. **Tag**: `git tag -a v<version> -m "Release <version>"`.

7. **Stop there.** Print the resulting diff and the exact command for the user to
   run themselves:
   ```
   git push && git push --tags
   ```
   Pushing is the user's call, per `.claude/rules/50-git-hygiene.md`.

## Checklist

- [ ] `check` skill passed.
- [ ] `config.yaml` version bumped; `breaking_versions:` updated if needed.
- [ ] `CHANGELOG.md` section moved and dated.
- [ ] Annotated tag created locally.
- [ ] Nothing pushed.
