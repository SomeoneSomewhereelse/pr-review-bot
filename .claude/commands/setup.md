---
description: Walk through setting up this project from a fresh clone to a first review
---

Run the setup doctor and report where things stand:

```bash
uv run python -m scripts.doctor --json
```

Read the JSON. It carries `track` (`local` or `hosted`), `step` (the current
`number`, `title`, and exact `command`), and one `checks` entry per row with a
`status` of `PASS`, `WARN`, `FAIL`, or `SKIPPED`.

Then, in a loop until `step` is `null`:

1. Show the user the current step number, its title, and the exact `command`.
2. Explain the first `FAIL` row in plain language, using its own `detail` —
   which already names what is wrong and what to do.
3. Treat `SKIPPED` as normal, not as a problem. A skipped row means its
   precondition does not exist yet (no Render service, no `RENDER_API_KEY`),
   which is the expected state early in setup.
4. Run the next command **only if it neither writes a credential, nor opens a
   browser, nor stays running in the foreground** (a tunnel, a server) — see
   the handoff rule below. A foreground process never returns control, so
   running one yourself would hang this session indefinitely; hand it to the
   user instead, the same way as a credential-writing command, e.g.: "Run
   this yourself in a separate terminal: `cloudflared tunnel --url
   http://localhost:8000`". Otherwise hand it to the user.
5. Re-run the doctor and repeat.

## Credential handoff — not optional

`CLAUDE.md` forbids you from opening `.env` for any reason, including a
single-line read. Two of this project's setup tools write real credentials to
it:

- `uv run python -m scripts.init_env`
- `uv run python -m scripts.create_github_app`

**Never run either of these yourself.** Ask the user to run it in this session
with the `!` prefix, so its output lands in the conversation without you
invoking it:

> Run this yourself: `! uv run python -m scripts.create_github_app`

Both print names and lengths only, never values, so their output is safe to
read and reason about afterwards. The doctor is safe for you to run as often as
you like — it is read-only, and reports names, lengths, and booleans only.

If the user asks you to check or fix a value inside `.env`, decline and ask
them to do it themselves. That is the rule working, not an obstacle to route
around.

## For reference

Full explanations of each row live in the setup guide; `scripts/deploy.py`
covers the deployment-verification rows specifically. This command holds no
setup logic of its own — `scripts/doctor.py` is the tool, and it works identically for people who do not use Claude Code.
