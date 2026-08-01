"""Deterministic, CI-safe tests for app/github_app.py.

PyGithub makes its HTTP calls via the `requests` library (not `httpx`), so
`respx` — which only intercepts `httpx` traffic — cannot see them. To still
get respx-style deterministic mocking with zero real network calls and zero
new dependencies, these tests patch `requests.adapters.HTTPAdapter.send`
(the actual transport boundary PyGithub's Requester sends through) with a
tiny in-memory router keyed on method + URL substring.
"""

import json

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import github_app
from app.config import settings

REPO_FULL_NAME = "SomeoneSomewhereelse/pr-review-bot-testbed"
PR_NUMBER = 1
REPO_API_URL = f"https://api.github.com/repos/{REPO_FULL_NAME}"
PR_API_URL = f"{REPO_API_URL}/pulls/{PR_NUMBER}"
ISSUE_API_URL = f"{REPO_API_URL}/issues/{PR_NUMBER}"


def _repo_json():
    return {"id": 1, "name": "pr-review-bot-testbed", "full_name": REPO_FULL_NAME, "url": REPO_API_URL}


def _pull_json():
    # PyGithub's PullRequest.get_issue_comments()/create_issue_comment() need
    # `issue_url`, and get_files()/get_pull() completion needs `url` — both
    # are lazily fetched from this response, so both must be present.
    return {
        "number": PR_NUMBER,
        "id": 1,
        "title": "test PR",
        "state": "open",
        "url": PR_API_URL,
        "issue_url": ISSUE_API_URL,
    }


@pytest.fixture(autouse=True)
def _throwaway_app_credentials(tmp_path, monkeypatch):
    """Point settings at a freshly generated, throwaway RSA key.

    Keeps these tests independent of the real (gitignored) App credentials —
    only JWT *signing* happens locally with this key; every HTTP call is
    mocked below, so nothing is ever sent anywhere with it.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_path = tmp_path / "throwaway-key.pem"
    pem_path.write_bytes(pem)

    monkeypatch.setattr(settings, "github_app_id", 999999)
    monkeypatch.setattr(settings, "github_app_installation_id", 123456)
    monkeypatch.setattr(settings, "github_app_private_key_path", str(pem_path))


class FakeGithubTransport:
    """Routes requests by (method, url-substring) to canned JSON responses."""

    def __init__(self):
        self.routes: list[tuple[str, str, dict, int]] = []
        self.requests: list[requests.PreparedRequest] = []

    def route(self, method: str, url_substring: str, json_body, status_code: int = 200):
        self.routes.append((method.upper(), url_substring, json_body, status_code))

    def send(self, request: requests.PreparedRequest, **kwargs) -> requests.Response:
        self.requests.append(request)
        # Longest url_substring first, so e.g. ".../pulls/1/files" doesn't
        # get shadowed by an earlier, shorter ".../pulls/1" registration.
        for method, url_substring, json_body, status_code in sorted(
            self.routes, key=lambda r: -len(r[1])
        ):
            if request.method == method and url_substring in request.url:
                return self._build_response(request, json_body, status_code)
        raise AssertionError(f"Unmocked request: {request.method} {request.url}")

    @staticmethod
    def _build_response(request, json_body, status_code) -> requests.Response:
        resp = requests.Response()
        resp.status_code = status_code
        resp.headers["Content-Type"] = "application/json"
        resp._content = json.dumps(json_body).encode("utf-8")
        resp.encoding = "utf-8"
        resp.url = request.url
        resp.reason = "OK"
        resp.request = request
        return resp


@pytest.fixture
def fake_transport(monkeypatch):
    transport = FakeGithubTransport()
    transport.route(
        "POST",
        f"/app/installations/{123456}/access_tokens",
        {"token": "fake-installation-token", "expires_at": "2099-01-01T00:00:00Z"},
        201,
    )
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", transport.send)
    return transport


def test_fetch_pr_diff_concatenates_file_patches(fake_transport):
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}/files",
        [
            {
                "sha": "abc",
                "filename": "app.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "@@ -1 +1 @@\n-old\n+new",
            }
        ],
    )

    diff = github_app.fetch_pr_diff(REPO_FULL_NAME, PR_NUMBER)

    assert "diff --git a/app.py b/app.py" in diff
    assert "-old" in diff
    assert "+new" in diff


def test_upsert_comment_creates_when_no_marker_comment_exists(fake_transport, monkeypatch):
    created = {}

    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [
            {
                "id": 111,
                "body": "an unrelated human comment, no marker here",
                "user": {"login": "someone", "type": "User"},
            }
        ],
    )

    def send_with_create_capture(request, **kwargs):
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            body = json.loads(request.body)
            created["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 222, "body": body["body"], "user": {"login": "bot"}}, 201
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_create_capture))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nfindings here")

    assert github_app.COMMENT_MARKER in created["body"]
    assert "findings here" in created["body"]
    assert result.id == 222


def test_upsert_comment_edits_existing_marker_comment_in_place(fake_transport, monkeypatch):
    edited = {}
    existing_body = f"{github_app.COMMENT_MARKER}\n## Review\nold findings"

    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [
            {
                "id": 333,
                "body": existing_body,
                "user": {"login": "bot", "type": "Bot"},
                "url": f"{REPO_API_URL}/issues/comments/333",
            }
        ],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            raise AssertionError("should not create a new comment when marker already exists")
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew findings")

    assert github_app.COMMENT_MARKER in edited["body"]
    assert "new findings" in edited["body"]
    assert "old findings" not in edited["body"]
    assert result.id == 333


def test_append_review_footnote_edits_marker_and_replaces_prior_footnote(fake_transport, monkeypatch):
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\ngood findings\n\n"
        f"{github_app.FAIL_NOTE_START}\n> old failure note\n{github_app.FAIL_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture))

    footnote = f"{github_app.FAIL_NOTE_START}\n> new failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    assert "good findings" in edited["body"]                 # review preserved
    assert "new failure note" in edited["body"]
    assert "old failure note" not in edited["body"]          # prior footnote replaced
    assert edited["body"].count(github_app.FAIL_NOTE_START) == 1  # no stacking


def test_append_review_footnote_preserves_stray_marker_in_real_content(fake_transport, monkeypatch):
    """A stray, unmatched FAIL_NOTE_START inside real finding text (e.g. a
    specialist quoting this very file's source) must NOT be treated as the
    start of a footnote block to strip -- only a WELL-FORMED TRAILING block
    (one that actually ends the body) should be replaced. Regression test for
    the content-loss bug where a first-START-to-next-END regex spanned from
    the stray marker all the way to the real trailing footnote's END,
    deleting genuine review content in between.
    """
    edited = {}
    existing_body = (
        f"{github_app.COMMENT_MARKER}\n## Review\n"
        f"Finding: the string `{github_app.FAIL_NOTE_START}` appears in app/github_app.py "
        f"and should be reviewed for clarity.\n\n"
        f"Other real finding: consider renaming this variable.\n\n"
        f"{github_app.FAIL_NOTE_START}\n> old failure note\n{github_app.FAIL_NOTE_END}"
    )
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": existing_body, "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send_with_patch_capture(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_patch_capture))

    footnote = f"{github_app.FAIL_NOTE_START}\n> new failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    # The stray marker's surrounding real content must survive.
    assert "appears in app/github_app.py" in edited["body"]
    assert "consider renaming this variable" in edited["body"]
    # Only the real trailing footnote was replaced.
    assert "new failure note" in edited["body"]
    assert "old failure note" not in edited["body"]
    assert edited["body"].count(github_app.FAIL_NOTE_START) == 2  # stray + new trailing one


def test_append_review_footnote_creates_marker_comment_when_none_exists(fake_transport, monkeypatch):
    created = {}
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 111, "body": "human comment, no marker", "user": {"login": "someone", "type": "User"}}],
    )

    def send_with_create_capture(request, **kwargs):
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            body = json.loads(request.body)
            created["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 222, "body": body["body"], "user": {"login": "bot"}}, 201
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send_with_create_capture))

    footnote = f"{github_app.FAIL_NOTE_START}\n> failure note\n{github_app.FAIL_NOTE_END}"
    github_app.append_review_footnote(REPO_FULL_NAME, PR_NUMBER, footnote)

    assert github_app.COMMENT_MARKER in created["body"]
    assert "failure note" in created["body"]


def test_upsert_comment_skips_human_comment_containing_the_marker(fake_transport, monkeypatch):
    created = {}
    # A human comment that quotes the marker must NOT be edited.
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 501, "body": f"quoting the bot: {github_app.COMMENT_MARKER}",
          "user": {"login": "a-human", "type": "User"},
          "url": f"{REPO_API_URL}/issues/comments/501"}],
    )

    def send(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/501" in request.url:
            raise AssertionError("must not edit a human comment that merely quotes the marker")
        if request.method == "POST" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            body = json.loads(request.body)
            created["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 777, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 201
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nfresh")
    assert result.id == 777                       # created a new bot comment
    assert github_app.COMMENT_MARKER in created["body"]


def test_upsert_comment_edits_by_id_when_comment_id_given(fake_transport, monkeypatch):
    edited = {}
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/comments/333",
        {"id": 333, "body": f"{github_app.COMMENT_MARKER}\nold", "user": {"login": "bot", "type": "Bot"},
         "url": f"{REPO_API_URL}/issues/comments/333"},
    )

    def send(request, **kwargs):
        if request.method == "GET" and request.url.endswith(f"/issues/{PR_NUMBER}/comments"):
            raise AssertionError("must not scan the thread when a comment_id is known")
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            body = json.loads(request.body)
            edited["body"] = body["body"]
            return fake_transport._build_response(
                request, {"id": 333, "body": body["body"], "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew", comment_id=333)
    assert result.id == 333
    assert "new" in edited["body"]


def test_upsert_comment_falls_back_to_scan_when_comment_id_deleted(fake_transport, monkeypatch):
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}", _repo_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/pulls/{PR_NUMBER}", _pull_json())
    fake_transport.route("GET", f"/repos/{REPO_FULL_NAME}/issues/comments/999",
                         {"message": "Not Found"}, 404)
    fake_transport.route(
        "GET",
        f"/repos/{REPO_FULL_NAME}/issues/{PR_NUMBER}/comments",
        [{"id": 333, "body": f"{github_app.COMMENT_MARKER}\nold", "user": {"login": "bot", "type": "Bot"},
          "url": f"{REPO_API_URL}/issues/comments/333"}],
    )

    def send(request, **kwargs):
        if request.method == "PATCH" and "/issues/comments/333" in request.url:
            return fake_transport._build_response(
                request, {"id": 333, "body": json.loads(request.body)["body"],
                          "user": {"login": "bot", "type": "Bot"}}, 200
            )
        return fake_transport.send(request, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", staticmethod(send))

    result = github_app.upsert_comment(REPO_FULL_NAME, PR_NUMBER, "## Review\nnew", comment_id=999)
    assert result.id == 333   # deleted id -> fell back to the author-filtered scan
