"""Create this project's GitHub App in one browser round-trip.

    uv run python -m scripts.create_github_app --base-url https://your-host

HUMAN-RUN ONLY. It writes real credentials to .env. An agent must never
invoke it -- same rule, and same reason, as scripts/encode_credential.py's
own docstring: doing so would put secret-derived bytes into a tool result.

GitHub's App Manifest flow: a browser form POSTs a manifest to
github.com/settings/apps/new, the operator approves, GitHub redirects back
with a one-time code, and POST /app-manifests/{code}/conversions returns the
App ID, PEM, and webhook secret together. That replaces creating the App by
hand, generating a private key by hand, and base64-encoding it by hand.
SETUP.md section 1 records that this project's own App was made this way.

The webhook URL is a placeholder at creation time -- the tunnel or Render URL
does not exist yet. scripts/deploy.py's github-app check corrects it later
("points here -- set only if wrong"), which is why an ephemeral tunnel URL is
fine (design spec 2026-08-18 section 4c).
"""

from __future__ import annotations

import argparse
import base64
import html
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import NamedTuple

import httpx

_CONVERSIONS_API = "https://api.github.com/app-manifests/{code}/conversions"
_NEW_APP_URL = "https://github.com/settings/apps/new"
_HTTP_TIMEOUT = 15.0
_CALLBACK_PORT = 8765
# Long enough for a human to read GitHub's approval screen, short enough that a
# closed browser tab does not hang the terminal forever.
_CALLBACK_TIMEOUT = 300.0

# Exactly what the bot needs, and nothing more. pull_requests+issues write
# because a PR review comment is an issue comment on GitHub's API; contents
# read to fetch the diff; metadata read is mandatory for any App.
MANIFEST_PERMISSIONS = {
    "pull_requests": "write",
    "contents": "read",
    "issues": "write",
    "metadata": "read",
}
MANIFEST_EVENTS = ("pull_request",)


class AppCredentials(NamedTuple):
    app_id: int
    private_key_pem: str
    webhook_secret: str


def build_manifest(app_name: str, base_url: str, redirect_url: str) -> dict:
    """The manifest GitHub is asked to create an App from.

    public=False is a security boundary, not a preference: leaving
    GITHUB_TARGET_REPO unset makes the bot act on every repo its installation
    covers, which is only safe because a private App can only be installed by
    accounts the owner chooses. A public App would let any third party
    self-install and have their events accepted (SETUP.md section 1).
    """
    return {
        "name": app_name,
        "url": base_url,
        "public": False,
        "hook_attributes": {"url": f"{base_url.rstrip('/')}/webhook", "active": True},
        "redirect_url": redirect_url,
        "default_events": list(MANIFEST_EVENTS),
        "default_permissions": dict(MANIFEST_PERMISSIONS),
    }


def exchange_code(code: str) -> AppCredentials:
    """Trade the one-time redirect code for the App's credentials.

    A failure is reported by STATUS ONLY. GitHub's error bodies can echo parts
    of the submitted manifest, and this response carries the PEM and webhook
    secret -- so nothing from it is ever printed (CLAUDE.md).
    """
    response = httpx.post(
        _CONVERSIONS_API.format(code=code),
        headers={"Accept": "application/vnd.github+json"},
        timeout=_HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"GitHub refused the manifest conversion (HTTP {response.status_code}). "
            "The code is single-use and expires quickly -- re-run to start over."
        )
    body = response.json()
    return AppCredentials(
        app_id=int(body["id"]),
        private_key_pem=body["pem"],
        webhook_secret=body["webhook_secret"],
    )


def write_credentials(
    creds: AppCredentials, path: Path, overwrite: bool = False
) -> dict[str, int]:
    """Write the three values to `path`; return name -> length.

    `path` is REQUIRED with no default: a default of Path(".env") would let a
    test or a mis-run clobber a real credential file. Refuses an existing file
    unless overwrite=True for the same reason.

    Returns lengths, never values -- the caller prints this, mirroring
    scripts/deploy.py::sync_env()'s `pushed {key} (len {n})` convention.
    """
    if path.exists() and not overwrite:
        raise SystemExit(
            f"{path} already exists; refusing to overwrite it. Move it aside "
            "first, or pass --overwrite if you are sure."
        )
    encoded_pem = base64.b64encode(creds.private_key_pem.encode()).decode()
    values = {
        "GITHUB_APP_ID": str(creds.app_id),
        "GITHUB_APP_PRIVATE_KEY": encoded_pem,
        "GITHUB_WEBHOOK_SECRET": creds.webhook_secret,
    }
    body = "".join(f"{name}={value}\n" for name, value in values.items())
    path.write_text(body, encoding="utf-8", newline="\n")
    return {name: len(value) for name, value in values.items()}


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Serves the auto-submitting manifest form, then catches the redirect."""

    manifest_json = ""
    state = ""
    code: str | None = None
    state_mismatch: bool = False
    received = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 -- stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/callback":
            if params.get("state", [""])[0] != type(self).state:
                self._reply(400, "<h1>State mismatch -- start over.</h1>")
                type(self).state_mismatch = True
                type(self).received.set()  # unblock main(); code stays None
                return
            type(self).code = params.get("code", [""])[0]
            self._reply(200, "<h1>Done. Return to your terminal.</h1>")
            type(self).received.set()
            return
        self._reply(200, self._form())

    def log_message(self, *args) -> None:
        """Silence the default request logging: the query string carries the
        one-time code, and stdout is not where that belongs."""

    def _form(self) -> str:
        return (
            "<!doctype html><body onload='document.forms[0].submit()'>"
            f"<form action='{_NEW_APP_URL}?state={type(self).state}' method='post'>"
            f"<input type='hidden' name='manifest' value='{type(self).manifest_json}'>"
            "<button type='submit'>Create the GitHub App</button></form></body>"
        )

    def _reply(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create this project's GitHub App")
    parser.add_argument("--name", default="pr-review-engine",
                        help="App name (must be globally unique on GitHub)")
    parser.add_argument("--base-url", default="https://example.invalid",
                        help="placeholder public URL; scripts/deploy.py corrects it later")
    parser.add_argument("--env-path", default=".env", help="where to write the credentials")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing env file")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    redirect = f"http://localhost:{_CALLBACK_PORT}/callback"
    manifest = build_manifest(args.name, args.base_url, redirect)
    _CallbackHandler.manifest_json = html.escape(json.dumps(manifest), quote=True)
    _CallbackHandler.state = secrets.token_urlsafe(16)
    _CallbackHandler.code = None
    _CallbackHandler.state_mismatch = False
    _CallbackHandler.received.clear()

    try:
        server = http.server.HTTPServer(("localhost", _CALLBACK_PORT), _CallbackHandler)
    except OSError as exc:
        print(
            f"could not bind to localhost:{_CALLBACK_PORT} ({type(exc).__name__}) -- "
            "close whatever else is using that port and try again",
            file=sys.stderr,
        )
        return 1
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start = f"http://localhost:{_CALLBACK_PORT}/"
    print(f"opening {start} -- approve the App in your browser")
    webbrowser.open(start)
    try:
        # The handler sets this once GitHub's redirect arrives. A single Event
        # (created before the wait, not inside the loop) is what makes this a
        # real block rather than a spin.
        if not _CallbackHandler.received.wait(timeout=_CALLBACK_TIMEOUT):
            print(
                f"timed out after {_CALLBACK_TIMEOUT:.0f}s waiting for GitHub's redirect",
                file=sys.stderr,
            )
            return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 1
    finally:
        server.shutdown()

    if not _CallbackHandler.code:
        if _CallbackHandler.state_mismatch:
            print("state mismatch in GitHub's redirect -- start over", file=sys.stderr)
        else:
            print("no code in GitHub's redirect", file=sys.stderr)
        return 1

    creds = exchange_code(_CallbackHandler.code)
    lengths = write_credentials(creds, Path(args.env_path), overwrite=args.overwrite)
    for name, length in lengths.items():
        print(f"wrote {name} (len {length})")
    print("next: uv run python -m scripts.doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
