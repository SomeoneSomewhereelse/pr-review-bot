# Step 3: Install the App on your repo(s)

This step only happens in a browser — GitHub does not permit an App to
install itself, so there's no CLI or script for it.

1. Go to `https://github.com/settings/apps/<your-app-slug>`.
2. Click **Install App**.
3. Choose **All repositories**, or select specific repos (e.g. a throwaway
   test repo while you're getting set up).

## Set `GITHUB_APP_INSTALLATION_ID`

**Required** — never auto-discovered or guessed on your behalf; the service
refuses to start without it, and re-verifies it against the App's actual
installation on every boot, so a value that's gone stale (e.g. the App was
uninstalled and reinstalled) fails loudly rather than silently drifting.

You don't have to hunt it down by hand: with `GITHUB_APP_INSTALLATION_ID`
still blank, run

```bash
uv run python -m scripts.deploy
```

Its `github-app` check discovers the real installation and names it in the
failure detail even while the var is unset. Copy that value into `.env` as
`GITHUB_APP_INSTALLATION_ID`, then re-run to confirm the check now passes.

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
