---
description: Walk through setting up this project from a fresh clone to a first review
---

Run the setup doctor and report where things stand:

```bash
uv run python -m bot.scripts.doctor --json
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
single-line read. Step 2 (the GitHub App) and Step 4 (the LLM provider) are
the two places real credentials reach it, and they work differently:

- **Step 2** is done entirely by hand in GitHub's UI — the operator collects
  the App ID, webhook secret, and base64-encoded private key themselves and
  pastes them into `.env` directly. There is no script call to hand off
  here; the handoff is the same as any other `.env` edit — you never open,
  read, or write to it, full stop, and a `PreToolUse` hook enforces this
  independently of your own judgment.
- **Step 4** writes credentials via a script: `uv run python -m
  bot.scripts.init_env`. **Never run this yourself.** Ask the user to run it in
  this session with the `!` prefix, so its output lands in the conversation
  without you invoking it:

  > Run this yourself: `! uv run python -m bot.scripts.init_env`

  It prints names and lengths only, never values, so its output is safe to
  read and reason about afterwards. The doctor is safe for you to run as
  often as you like — it is read-only, and reports names, lengths, and
  booleans only.

`bot.scripts.create_github_app` also still exists — an optional, still-tested
automated alternative to Step 2's by-hand process, not the documented
default. If the user chooses to use it anyway, it writes credentials the
same way `init_env` does and must never be run by you either; hand it off
the same way: `! uv run python -m bot.scripts.create_github_app`.

If the user asks you to check or fix a value inside `.env`, decline and ask
them to do it themselves. That is the rule working, not an obstacle to route
around.

## For reference

Full explanations of each row live in the setup guide; `bot/scripts/deploy.py`
covers the deployment-verification rows specifically. This command holds no
setup logic of its own — `bot/scripts/doctor.py` is the tool, and it works identically for people who do not use Claude Code.
