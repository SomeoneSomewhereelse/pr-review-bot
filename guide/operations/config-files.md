# Config files: `.env` and `.env.config`

Every setting this service reads comes from one of two files. `Settings`
(`bot/config.py`) reads both, in the order `env_file=(".env", ".env.config")`
— the **last** file wins on a key present in both, so `.env.config` is the
designated home and always outranks a stale duplicate left behind in `.env`.

- **`.env`** holds credentials and identity — API keys, the GitHub App's
  private key, `DATABASE_URL` (the password lives inside the connection
  string itself). It is gitignored and never committed.
- **`.env.config`** holds operational settings — provider, model, GCP
  project/location, timeouts, dispatcher retry/backoff tuning, usage caps,
  cooldown tuning. Nothing but a credential belongs in `.env`, and nothing
  but operational config belongs in `.env.config`.

See [Configuration reference](../reference/config.md) for the field-by-field
table of every setting, its type, default, and which file it lives in —
generated from the code, so it isn't repeated here.

## `.env.config` is safe to open; `.env` is not

`.env.config` never mixes in credential material, so it is safe for anyone —
including an agent — to open and edit directly. `.env` is not: it mixes
secrets with everything else, so it must never be opened by an agent for any
reason, full stop (see CLAUDE.md's "Secret handling" section).

## `OPERATIONAL_KEYS` is exhaustive, not a pattern

`OPERATIONAL_KEYS` (`bot/config.py`) is the exhaustive, hand-maintained list
of which env-var names are operational (provider, model, usage caps,
cooldown tuning, etc.) rather than credentials. It is a literal list of
names, enumerated one by one — never a prefix or glob — because a pattern
would silently classify future keys that happen to match. **Everything not
on that list is a secret by default.**

Adding a setting to `OPERATIONAL_KEYS` is a deliberate classification
decision, not a formality: `tests/test_config.py` enforces the split in both
directions — it fails, naming every misplaced key, if a listed key is found
in `.env`, or if a key that is *not* listed is found in `.env.config`.

## Settings that are operational but never a Render env var

A handful of `OPERATIONAL_KEYS` entries — the cooldown trio and the usage-cap
settings — are edited in `.env.config` like any other operational setting,
but are **never** pushed to Render by `--sync-env` and never declared in
`render.yaml`. They live only in the `runtime_config` database table
instead, because the dispatcher needs to change them with no redeploy. See
[Tuning cooldowns and usage caps](tuning.md) for the full explanation and the
`--sync-config-db` push path.

## Migrating an existing `.env`

If you have operational values still sitting in `.env` from before this
split existed:

1. Copy `.env.config.example` to `.env.config` and fill in the values
   currently sitting in `.env` for every name on `OPERATIONAL_KEYS`.
2. Remove those same keys from `.env`.

This order matters: `.env.config` wins by precedence, so creating it first
and removing the old keys second means there is never a window where a
setting reads as unset. Doing it in the other order would create one.
Re-run `tests/test_config.py::test_no_operational_key_lives_in_the_secrets_file`
to confirm the migration is complete.

## Next

- [Deploying and verifying](deploy.md)
- [Tuning cooldowns and usage caps](tuning.md)
