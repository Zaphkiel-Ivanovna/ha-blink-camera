---
name: scaffold-addon-option
description: >
  Add, change or remove a Home Assistant add-on configuration option end to end:
  config.yaml options and schema, translations/en.yaml, DOCS.md, and the typed
  field in config.py. Use when adding a user-facing setting such as a refresh
  interval, RTSP port or camera selection.
argument-hint: <option-name> <schema-type> ["description text"]
allowed-tools: Read, Write, Edit, Grep, Glob, Bash(uv run mypy:*)
---

# Scaffold an add-on option

An option is only "added" when all four surfaces agree: the schema, the
translation, the docs and the typed config object. Partial additions are the
usual source of "option silently ignored" bugs.

## Steps

1. **Parse the arguments** into option name, schema type and description. If the
   schema type is missing, infer it from the name and say which you chose.

2. **`config.yaml`** — add the key in *both* blocks:
   - under `options:` with a sane default (this is what a fresh install gets),
   - under `schema:` with a validator: `str`, `str(min,max)`, `int(min,max)`,
     `float`, `bool`, `email`, `url`, `password`, `port`, `match(REGEX)`,
     `list(a|b|c)`. Append `?` to make it optional.
   A key present in `schema:` but absent from `options:` is valid only when
   marked optional.

3. **`translations/en.yaml`** — add `configuration.<key>.name` and
   `configuration.<key>.description`. Without this the UI shows the raw key.

4. **`src/ha_blink_camera/config.py`** — add the field to the typed `Config`
   object with a type matching the validator (`password` -> `str`,
   `int(a,b)` -> `int`, `list(...)` -> a `Literal` or `Enum`). Nothing outside
   `config.py` reads `/data/options.json`.

5. **`DOCS.md`** — append a row or bullet to the configuration section: key,
   type, default, what it does.

6. **Verify**: `uv run mypy src/ha_blink_camera/config.py`.

## Removing an option

Delete it from all four surfaces, and note in `CHANGELOG.md` that users with the
old key set will need a manual update. If the removal breaks existing installs,
add the new version to `breaking_versions:` in `config.yaml`.

## Checklist

- [ ] Key present under both `options:` and `schema:` (or optional with `?`).
- [ ] Validator syntax is one of the documented forms.
- [ ] `translations/en.yaml` has name and description.
- [ ] `Config` field added with a matching type; mypy clean.
- [ ] `DOCS.md` updated.
- [ ] Secret-bearing options use the `password` validator and are never logged
      (`.claude/rules/10-security-secrets.md`).
