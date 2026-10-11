"""Sign-in for the command line: browser hand-off, storage and refresh.

``login_via_browser`` opens ``<site>/mcp/auth?cli_port=N&state=S`` and
waits on a one-shot HTTP listener bound to 127.0.0.1:N. After the user
signs in, that page POSTs the Supabase session (access token, refresh
token, expiry) to ``/callback``; the ``state`` must match or the request
is refused. The session is saved to ``credentials.json`` (mode 0600) in
the user's config directory with the site's public Supabase URL and anon
key, which is all ``refresh`` needs to renew the access token when it is
about to expire.

``RHYLTHYME_TOKEN`` overrides the stored session with a raw access token
(no refresh), for CI or a headless box.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional

DEFAULT_SITE_URL = "https://www.rhylthyme.com"
REFRESH_MARGIN_SECONDS = 120
LOGIN_TIMEOUT_SECONDS = 300


class AuthError(Exception):
    """Sign-in is missing, expired beyond refresh, or failed."""


def site_url() -> str:
    return os.environ.get("RHYLTHYME_SITE_URL", DEFAULT_SITE_URL).rstrip("/")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "rhylthyme"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def load_credentials(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = path or credentials_path()
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_credentials(creds: Dict[str, Any], path: Optional[Path] = None) -> Path:
    path = path or credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        pass
    # Create with 0600 before writing so the token is never world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(path, 0o600)
    return path


def clear_credentials(path: Optional[Path] = None) -> bool:
    path = path or credentials_path()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _http_json(
    url: str,
    *,
    data: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 20,
) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        method="POST" if data is not None else "GET",
        headers=dict({"Content-Type": "application/json"}, **(headers or {})),
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_public_config(site: Optional[str] = None) -> Dict[str, str]:
    """The site's public Supabase URL and anon key (same values the web app uses)."""
    cfg = _http_json(f"{site or site_url()}/api/config")
    if not cfg.get("supabase_url") or not cfg.get("supabase_anon_key"):
        raise AuthError("The site did not return its Supabase configuration.")
    return {
        "supabase_url": cfg["supabase_url"],
        "supabase_anon_key": cfg["supabase_anon_key"],
    }


def refresh(
    creds: Dict[str, Any], http: Optional[Callable[..., dict]] = None
) -> Dict[str, Any]:
    """Exchange the refresh token for a new session. Returns updated creds."""
    http = http or _http_json
    if not creds.get("refresh_token"):
        raise AuthError(
            "Session expired and there is no refresh token. Run `rhylthyme login` again."
        )
    url = creds["supabase_url"].rstrip("/") + "/auth/v1/token?grant_type=refresh_token"
    try:
        data = http(
            url,
            data={"refresh_token": creds["refresh_token"]},
            headers={"apikey": creds["supabase_anon_key"]},
        )
    except urllib.error.HTTPError as e:
        raise AuthError(
            f"Could not refresh the session (HTTP {e.code}). Run `rhylthyme login` again."
        ) from e
    except urllib.error.URLError as e:
        raise AuthError(f"Could not reach the sign-in service: {e.reason}") from e
    new = dict(creds)
    new["access_token"] = data["access_token"]
    new["refresh_token"] = data.get("refresh_token") or creds["refresh_token"]
    new["expires_at"] = int(
        data.get("expires_at") or (time.time() + int(data.get("expires_in") or 3600))
    )
    return new


def access_token(now: Optional[float] = None, path: Optional[Path] = None) -> str:
    """A usable access token, refreshing and re-saving the session if needed."""
    env = os.environ.get("RHYLTHYME_TOKEN")
    if env:
        return env.strip()
    creds = load_credentials(path)
    if not creds or not creds.get("access_token"):
        raise AuthError("Not signed in. Run `rhylthyme login` first.")
    now = time.time() if now is None else now
    expires_at = float(creds.get("expires_at") or 0)
    if expires_at and expires_at - REFRESH_MARGIN_SECONDS > now:
        return creds["access_token"]
    if not expires_at and not creds.get("refresh_token"):
        return creds[
            "access_token"
        ]  # pasted token of unknown age; let the server judge
    creds = refresh(creds)
    save_credentials(creds, path)
    return creds["access_token"]


# ---------------------------------------------------------------------------
# Browser hand-off
# ---------------------------------------------------------------------------

_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>Rhylthyme CLI</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;">
<h2 style="color:#6B9E7D">&#10003; Signed in</h2>
<p>The Rhylthyme command-line tool has your sign-in. You can close this tab.</p></body>"""


def _make_handler(expected_state: str, sink: Dict[str, Any], done: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _reply(self, code: int, body: str):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/callback":
                return self._reply(404, "not found")
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            get = lambda k: (form.get(k) or [""])[0]
            if not secrets.compare_digest(get("state"), expected_state):
                return self._reply(
                    400,
                    "State mismatch. Start again with <code>rhylthyme login</code>.",
                )
            if not get("access_token"):
                return self._reply(400, "No session received.")
            sink.update(
                access_token=get("access_token"),
                refresh_token=get("refresh_token") or None,
                expires_at=(
                    int(get("expires_at")) if get("expires_at").isdigit() else None
                ),
                email=get("email") or None,
            )
            self._reply(200, _DONE_PAGE)
            done.set()

        def do_GET(self):
            self._reply(200, "Waiting for sign-in from the Rhylthyme site…")

    return Handler


def login_via_browser(
    *,
    site: Optional[str] = None,
    open_browser: bool = True,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    echo: Callable[[str], None] = print,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    site = site or site_url()
    public = fetch_public_config(site)
    state = secrets.token_urlsafe(24)
    sink: Dict[str, Any] = {}
    done = threading.Event()
    server = HTTPServer(("127.0.0.1", 0), _make_handler(state, sink, done))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"{site}/mcp/auth?" + urllib.parse.urlencode(
        {"cli_port": port, "state": state}
    )
    try:
        echo(f"Opening {url}")
        echo("Sign in, then click “Send to Rhylthyme CLI”. Waiting…")
        if open_browser:
            webbrowser.open(url)
        if not done.wait(timeout):
            raise AuthError("Timed out waiting for the browser sign-in.")
    finally:
        server.shutdown()
        server.server_close()
    creds = dict(public, **sink)
    save_credentials(creds, path)
    return creds


def login_with_token(
    token: str, *, site: Optional[str] = None, path: Optional[Path] = None
) -> Dict[str, Any]:
    """Store a pasted access token (no refresh; expires in about an hour)."""
    creds = dict(
        fetch_public_config(site),
        access_token=token.strip(),
        refresh_token=None,
        expires_at=None,
    )
    save_credentials(creds, path)
    return creds
