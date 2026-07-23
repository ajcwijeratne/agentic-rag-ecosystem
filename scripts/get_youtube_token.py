"""
Get a YouTube refresh token for the publisher.

You only run this once. It turns an OAuth client (client_id + client_secret,
created in Google Cloud) into the long-lived refresh token publishers/youtube.py
uses to upload videos and read their stats.

Prerequisites (done once in Google Cloud Console, in your browser):
  1. Create / pick a project, enable "YouTube Data API v3".
  2. OAuth consent screen: External, add yourself as a Test user.
  3. Credentials -> Create OAuth client ID -> type "Desktop app".
  4. Copy the client ID and client secret.

Then run (from the repo root, in the main .venv):
  set YOUTUBE_CLIENT_ID=...        (PowerShell: $env:YOUTUBE_CLIENT_ID="...")
  set YOUTUBE_CLIENT_SECRET=...
  python -m scripts.get_youtube_token

A browser opens, you approve, and the refresh token prints. The script offers
to append all four YOUTUBE_ values to your .env.
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

PORT = int(os.getenv("YOUTUBE_OAUTH_PORT", "8081"))
REDIRECT_URI = f"http://localhost:{PORT}"
# upload = publish; readonly = read your own video statistics for the measure loop.
SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
          "https://www.googleapis.com/auth/youtube.readonly")
_code_holder: dict[str, str] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (params.get("code") or [""])[0]
        _code_holder["code"] = code
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorised. You can close this tab and return to the terminal."
        if not code:
            msg = "No code received. Check the consent screen and try again."
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *args) -> None:  # silence the default request logging
        return


def _capture_code(auth_url: str) -> str:
    with socketserver.TCPServer(("localhost", PORT), _Handler) as httpd:
        threading.Thread(target=httpd.handle_request, daemon=True).start()
        print(f"Opening browser for consent. If it does not open, visit:\n{auth_url}\n")
        webbrowser.open(auth_url)
        while "code" not in _code_holder:
            pass
    return _code_holder.get("code", "")


def _append_env(values: dict[str, str]) -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    lines = [f"{k}={v}" for k, v in values.items()]
    block = "\n# YouTube publisher (Phase 2/3)\n" + "\n".join(lines) + "\n"
    with env_path.open("a", encoding="utf-8") as f:
        f.write(block)
    print(f"Appended {len(values)} YOUTUBE_ values to {env_path}")


def main() -> int:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip().strip('"').strip("'")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip().strip('"').strip("'")
    if not client_id or not client_secret:
        print("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first (see the docstring).")
        return 1
    print(f"Using client_id: {client_id[:14]}...{client_id[-30:]}")
    if not client_id.endswith(".apps.googleusercontent.com"):
        print("\nThat does not look like an OAuth client ID. It must end in "
              "'.apps.googleusercontent.com'.\nCopy it from Google Cloud -> APIs & Services "
              "-> Credentials -> 'OAuth 2.0 Client IDs' (NOT an API key).")
        return 1
    if not client_secret.startswith("GOCSPX-"):
        print("Warning: client secrets usually start with 'GOCSPX-'. "
              "Make sure you copied the secret, not the ID.")

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })
    code = _capture_code(auth_url)
    if not code:
        print("No authorization code captured.")
        return 1

    resp = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    resp.raise_for_status()
    token = resp.json()
    refresh = token.get("refresh_token")
    if not refresh:
        print("No refresh token returned. Revoke prior access at "
              "https://myaccount.google.com/permissions and re-run (prompt=consent).")
        return 1

    print("\nRefresh token acquired.\n")
    print(f"YOUTUBE_REFRESH_TOKEN={refresh}\n")
    if input("Append all four YOUTUBE_ values to .env now? [y/N] ").strip().lower() == "y":
        _append_env({
            "YOUTUBE_CLIENT_ID": client_id,
            "YOUTUBE_CLIENT_SECRET": client_secret,
            "YOUTUBE_REFRESH_TOKEN": refresh,
            "YOUTUBE_PRIVACY_STATUS": os.getenv("YOUTUBE_PRIVACY_STATUS", "private"),
        })
    else:
        print("Not written. Add the four YOUTUBE_ lines to .env yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
