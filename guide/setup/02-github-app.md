# Step 2: Create the GitHub App

The GitHub App is the bot's identity: it's what lets the service read a
pull request's diff and post (and later edit) a review comment.

## The one-command path

```bash
uv run python -m scripts.create_github_app --base-url https://your-host
```

Run this yourself — it writes real credentials to `.env`, so it must never
be run by an agent.

This drives GitHub's **App Manifest flow**: it opens a browser form that
POSTs a manifest to `github.com/settings/apps/new`, you approve it, GitHub
redirects back with a one-time code, and the script exchanges that code for
the App ID, private key, and webhook secret — all in one round trip. That
replaces creating the App by hand, generating a private key by hand, and
base64-encoding it by hand.

The App is created with:

- **Permissions**: `pull_requests: write`, `contents: read`, `issues: write`,
  `metadata: read`.
- **Event**: `pull_request`.
- **Webhook URL**: a placeholder at creation time — the tunnel or Render URL
  doesn't exist yet. Step 7 (`scripts/deploy.py`) corrects it once your
  public URL is known.

!!! warning "Keep the App private"
    Do not publish the App to the GitHub Marketplace. Leaving
    `GITHUB_TARGET_REPO` unset (step 3) makes the bot act on *every* repo the
    installation covers, and that's only a safe default because only
    accounts *you* choose can install a private App in the first place. A
    public App would let any third party self-install and have their events
    accepted in that same track-all mode.

## The manual fallback

If you'd rather create the App by hand in GitHub's UI (**Settings → Developer
settings → GitHub Apps → New GitHub App**), give it the same permissions and
event listed above, then collect three IDs from the App's settings page —
only two of which this project uses:

- **App ID** → `GITHUB_APP_ID`. A short integer, near the top of the App's
  **General** settings page.
- **Installation ID** → `GITHUB_APP_INSTALLATION_ID`. **Optional but
  recommended** — the app auto-discovers it at boot when unset, but that
  auto-discovery reads `GITHUB_APP_PRIVATE_KEY` and crashes the whole
  service at startup if that credential is ever missing or wrong. Pinning
  the Installation ID removes that read from the unconditional boot path
  entirely, so a bad private key only breaks webhook handling later instead
  of the whole service at boot.
- **Client ID** — sits on the same settings page, and is easy to grab by
  mistake, but this project **does not use it at all**.

Download the private key as a `.pem` file, then base64-encode it with this
project's own script — never a raw file path:

```bash
uv run python -m scripts.encode_credential github-app-private-key.pem
```

Paste the output into `GITHUB_APP_PRIVATE_KEY` in `.env` (verbatim, the
whole base64 string — never a file path).

## Next

Continue to [Step 3: install the App](03-install-app.md) — GitHub does not
let an App install itself, so that part is always a browser step.
