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
guide/setup/02-github-app.md records that this project's own App was made
this way.

Three ways to catch that redirect, depending on what's reachable from the
approving browser's device:

- Same machine (the common case): the default _run_server_flow, unchanged --
  a local callback server, browser auto-opened, fully automatic.
- Different device, same network (Tailscale, a LAN, a VPN -- e.g. approving
  from a phone or laptop while this runs on an SSH'd-into host they share a
  tailnet with): --bind-host, still _run_server_flow, just listening on an
  address the OTHER device can reach instead of localhost. Still no
  copy-pasting -- the operator opens one ordinary http:// URL on their own
  device and the rest is automatic, same as the local case. (The same result
  is reachable with zero code changes via SSH local port forwarding --
  `ssh -L 8765:localhost:8765 host` -- which makes the default flow's
  `localhost` correct again from the operator's side; --bind-host is for
  when setting that up isn't convenient.)
- No shared network at all (an isolated remote host, e.g. a bare cloud VM
  with only port 22 open): nothing above can work -- there is no path from
  the approving device to this one. --manual (_run_manual_flow) is the
  fallback for exactly this case: it writes the same auto-submitting form to
  a plain HTML file the operator copies to WHATEVER machine has a browser
  (scp, AirDrop, anything), and reads the resulting redirect URL back from
  them directly instead of catching it with a listener -- no network path
  required at all, just a file and a copy-paste.

A `data:` URL pasted straight into the browser's address bar was considered
as a zero-file-transfer alternative to --manual, but Chrome blocks top-level
navigation to a typed/pasted `data:` URL (anti-phishing, since ~2018) --
so it would silently fail for the majority of mobile users. Not used here
for that reason.

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
import tempfile
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

# --manual's redirect target. Deliberately never resolves -- same "obviously
# fake" convention as --base-url's own default -- so the operator's browser
# always fails to load it, but the code+state GitHub attaches to the redirect
# still show up in the address bar for them to copy. This is what makes
# --manual work with NO listener anywhere: the local-server flow needs the
# approving browser to reach localhost on the machine running this script,
# which fails over SSH/headless with no diagnosis beyond a 300s timeout.
_MANUAL_REDIRECT = "https://example.invalid/callback"

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
    self-install and have their events accepted (guide/setup/02-github-app.md).
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


def parse_redirect_code(pasted: str, expected_state: str) -> str:
    """Extract the one-time code from what --manual's operator pasted back:
    either the full URL GitHub's browser redirect landed on (recommended --
    it carries `state`, so this can run the same CSRF check the local-server
    flow's do_GET does), or just the bare code on its own.

    Raises SystemExit on anything unusable, mirroring exchange_code()'s own
    reporting convention: a clear, structural message, never a value dump.
    """
    pasted = pasted.strip()
    if not pasted:
        raise SystemExit("nothing pasted -- start over")
    if "://" not in pasted:
        return pasted
    params = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if params.get("state", [""])[0] != expected_state:
        raise SystemExit("state mismatch in the pasted URL -- start over")
    code = params.get("code", [""])[0]
    if not code:
        raise SystemExit("no code in the pasted URL")
    return code


def _render_manifest_form(manifest_json: str, state: str) -> str:
    """The auto-submitting page that POSTs the manifest to GitHub. Shared by
    the local-server flow (served over http://localhost) and --manual (written
    to a plain file, opened over file:// or copied elsewhere first) -- a form
    POST isn't subject to CORS, so which origin serves this page doesn't
    matter."""
    return (
        "<!doctype html><body onload='document.forms[0].submit()'>"
        f"<form action='{_NEW_APP_URL}?state={state}' method='post'>"
        f"<input type='hidden' name='manifest' value='{manifest_json}'>"
        "<button type='submit'>Create the GitHub App</button></form></body>"
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
        return _render_manifest_form(type(self).manifest_json, type(self).state)

    def _reply(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _run_server_flow(
    app_name: str, base_url: str, state: str, *, bind_host: str = "localhost"
) -> str | None:
    """The default, fully-automatic flow: a local server serves the
    auto-submitting form and catches GitHub's redirect. With the default
    bind_host="localhost", this needs the approving browser to be on THIS
    machine. Passing --bind-host (some other address of this machine's --
    a Tailscale IP/hostname, a LAN IP) instead extends the same automatic
    flow to a *different* device that shares a network with this one: the
    operator opens that address on their own device's browser, and
    everything else works exactly as it does in the same-machine case --
    still no copy-pasting. See --manual/_run_manual_flow for when there's no
    shared network at all (SSH to an isolated host, a headless box)."""
    redirect = f"http://{bind_host}:{_CALLBACK_PORT}/callback"
    manifest = build_manifest(app_name, base_url, redirect)
    _CallbackHandler.manifest_json = html.escape(json.dumps(manifest), quote=True)
    _CallbackHandler.state = state
    _CallbackHandler.code = None
    _CallbackHandler.state_mismatch = False
    _CallbackHandler.received.clear()

    try:
        server = http.server.HTTPServer((bind_host, _CALLBACK_PORT), _CallbackHandler)
    except OSError as exc:
        print(
            f"could not bind to {bind_host}:{_CALLBACK_PORT} ({type(exc).__name__}) -- "
            "close whatever else is using that port and try again",
            file=sys.stderr,
        )
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start = f"http://{bind_host}:{_CALLBACK_PORT}/"
    if bind_host == "localhost":
        print(f"opening {start} -- approve the App in your browser")
        opened = webbrowser.open(start)
        if not opened:
            print(
                "no browser could be opened automatically here -- if you're on a remote or "
                "headless machine (SSH, a container, a cloud VM), Ctrl+C and re-run with "
                "--manual instead, which needs no local browser or localhost access at all",
                file=sys.stderr,
            )
    else:
        # bind_host names a device other than this one's browser -- opening a
        # browser HERE would be opening it on the wrong machine, so don't.
        print(
            f"open {start} in a browser on a device that can reach this machine at "
            f"{bind_host} (e.g. over the same Tailscale/VPN/LAN) -- approve the App there"
        )
    try:
        # The handler sets this once GitHub's redirect arrives. A single Event
        # (created before the wait, not inside the loop) is what makes this a
        # real block rather than a spin.
        if not _CallbackHandler.received.wait(timeout=_CALLBACK_TIMEOUT):
            print(
                f"timed out after {_CALLBACK_TIMEOUT:.0f}s waiting for GitHub's redirect -- "
                + (
                    "if you're on a remote or headless machine, that's almost always why: the "
                    "approving browser can't reach localhost on this machine. Re-run with "
                    "--manual instead, or --bind-host if the other device shares a network "
                    "with this one."
                    if bind_host == "localhost"
                    else f"make sure the other device can actually reach {bind_host} on "
                    f"port {_CALLBACK_PORT} (firewall, same tailnet/VPN/LAN). If it can't, "
                    "re-run with --manual instead."
                ),
                file=sys.stderr,
            )
            return None
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return None
    finally:
        server.shutdown()

    if not _CallbackHandler.code:
        if _CallbackHandler.state_mismatch:
            print("state mismatch in GitHub's redirect -- start over", file=sys.stderr)
        else:
            print("no code in GitHub's redirect", file=sys.stderr)
        return None
    return _CallbackHandler.code


def _run_manual_flow(
    app_name: str, base_url: str, state: str, *, tmp_dir: Path | None = None
) -> str | None:
    """No local server, no localhost dependency at all: write the same
    auto-submitting form to a plain HTML file, let the operator open it on
    WHATEVER machine has a browser (copy it there first if this one has
    none), and read the resulting redirect URL back from them directly.
    GitHub's redirect still fires -- it just lands on a page that fails to
    load (_MANUAL_REDIRECT never resolves, deliberately). The code survives
    in the browser's address bar regardless of whether the page loads.

    `tmp_dir` lets tests redirect the written file into `tmp_path` instead of
    the real system temp dir; production calls leave it as the default.
    """
    manifest = build_manifest(app_name, base_url, _MANUAL_REDIRECT)
    manifest_json = html.escape(json.dumps(manifest), quote=True)
    page = _render_manifest_form(manifest_json, state)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", prefix="github-app-manifest-",
        delete=False, encoding="utf-8", dir=tmp_dir,
    ) as f:
        f.write(page)
        page_path = f.name

    print(f"wrote {page_path}")
    print(
        "Open that file in any browser -- this machine's, or copy it elsewhere first "
        "(e.g. `scp` it to your laptop) if this machine has none. It submits itself; "
        "approve the App on GitHub's page.\n"
        "GitHub then redirects to a page that will fail to load "
        f"({_MANUAL_REDIRECT}) -- that's expected. Copy the full URL from your "
        "browser's address bar at that point (it carries the code) and paste it below."
    )
    try:
        pasted = input("Paste the redirected URL (or just the code): ")
    except (EOFError, KeyboardInterrupt):
        print("cancelled", file=sys.stderr)
        return None
    return parse_redirect_code(pasted, state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create this project's GitHub App")
    parser.add_argument("--name", default="pr-review-engine",
                        help="App name (must be globally unique on GitHub)")
    parser.add_argument("--base-url", default="https://example.invalid",
                        help="placeholder public URL; scripts/deploy.py corrects it later")
    parser.add_argument("--env-path", default=".env", help="where to write the credentials")
    parser.add_argument("--overwrite", action="store_true",
                        help="replace an existing env file")
    parser.add_argument(
        "--manual", action="store_true",
        help="no local callback server or auto-opened browser -- write a manifest HTML "
             "file to open on any machine instead, then paste back the redirect URL "
             "yourself. Use this when there's no shared network at all between this "
             "machine and the approving browser's device.",
    )
    parser.add_argument(
        "--bind-host", default="localhost",
        help="address to listen on and to send GitHub's redirect to, instead of "
             "localhost -- e.g. this machine's Tailscale IP/hostname or LAN IP. Use "
             "this to approve from a different device (a phone, a laptop) that shares "
             "a network with this machine, still fully automatic. Leave unset for the "
             "default same-machine flow.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.manual and args.bind_host != "localhost":
        parser.error("--bind-host has no effect with --manual, which uses no listener at all")

    state = secrets.token_urlsafe(16)
    code = (
        _run_manual_flow(args.name, args.base_url, state)
        if args.manual
        else _run_server_flow(args.name, args.base_url, state, bind_host=args.bind_host)
    )
    if code is None:
        return 1

    creds = exchange_code(code)
    lengths = write_credentials(creds, Path(args.env_path), overwrite=args.overwrite)
    for name, length in lengths.items():
        print(f"wrote {name} (len {length})")
    print("next: uv run python -m scripts.doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
