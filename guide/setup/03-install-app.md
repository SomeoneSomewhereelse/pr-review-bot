# Step 3: Install the App on your repo(s)

This step only happens in a browser — GitHub does not permit an App to
install itself, so there's no CLI or script for it.

1. Go to `https://github.com/settings/apps/<your-app-slug>`.
2. Click **Install App**.
3. Choose **All repositories**, or select specific repos (e.g. a throwaway
   test repo while you're getting set up).

Whichever GitHub account this browser session installs the App on is the
account that matters from here on — including for Step 8's demo PR, which
needs `gh` (Step 1) authenticated as *that same* account. Step 1's warning
about this covers what goes wrong when they don't match and how `doctor`
catches it if they don't.

## Set `GITHUB_APP_INSTALLATION_ID`

**Required** — never auto-discovered or guessed on your behalf; the service
refuses to start without it, and re-verifies it against the App's actual
installation on every boot, so a value that's gone stale (e.g. the App was
uninstalled and reinstalled) fails loudly rather than silently drifting.

You don't have to hunt it down by hand: with `GITHUB_APP_INSTALLATION_ID`
still blank, run

```bash
uv run python -m scripts.doctor
```

not `scripts.deploy` — this early, before a public URL exists (that's Step
6), `deploy` refuses to run at all (`a public base URL
(PUBLIC_BASE_URL/RENDER_EXTERNAL_URL) is required`, exit 2) before it ever
reaches an installation-discovery check. `doctor` needs no public URL for
this: its `github-install` row calls the same GitHub API discovery and
prints `installation=<id>` on success, using only the App credentials you
already set in Step 2. Copy that id into `.env` as
`GITHUB_APP_INSTALLATION_ID`.

## `GITHUB_TARGET_REPO` is a separate, optional narrowing

Choosing "All repositories" above decides which repos the *installation*
covers — which repos the App can see at all. `GITHUB_TARGET_REPO` is a
different, optional setting on top of that: an allowlist that further
narrows which of the *installed* repos the bot actually acts on. It doesn't
change what the App can see, only what it responds to.

Leaving `GITHUB_TARGET_REPO` unset means the bot acts on **every** repo the
installation covers. That's only a safe default because the App is
private (step 2) — only accounts you chose could install it in the first
place. If the App were public, an unset `GITHUB_TARGET_REPO` would mean
accepting events from any repo any third party chose to install it on.

## Next

Continue to [Step 4: configure an LLM provider](04-llm-provider.md).
