"""
Google link: one OAuth sign-in that lets the brain read Aaron's personal
Google account and, after approval, write drafts and tentative calendar holds.
The account address is read from Google at run time, never stored in code.

Decided 23 Nov 2026:
  * Personal Gmail, Google Calendar and Drive are personal/WijerCo sources, so
    their content may go to cloud models. Work sources (Outlook, Teams) do not
    come through here.
  * Access is read, plus Gmail drafts and tentative calendar holds, each one
    behind an approval. Nothing is ever sent. There is no gmail.send scope.

Setup (once, on wijerco):
  1. Create a Desktop OAuth client in Google Cloud with the Gmail, Calendar
     and Drive APIs enabled, and set the app to "In production". An app left in
     "Testing" loses its sign-in every 7 days.
  2. Save the client JSON as data/google_client_secret.json.
  3. python -m orchestrator.google_link connect
     A browser opens on wijerco; sign in and allow. The token is written to
     data/google_token.json (backed up nightly with data/).

Google libraries are imported lazily, so the orchestrator still imports on a
machine that has not installed them yet.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET_PATH = Path(os.getenv("GOOGLE_CLIENT_SECRET_PATH", str(_ROOT / "data" / "google_client_secret.json")))
TOKEN_PATH = Path(os.getenv("GOOGLE_TOKEN_PATH", str(_ROOT / "data" / "google_token.json")))

# Least privilege for the agreed access. gmail.compose allows drafts; it also
# technically allows sending, so this module never calls drafts.send or
# messages.send (tests/unit/test_google_link.py checks the source for that).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Data class for everything that arrives through this link. The work-data rule
# keys off this value: anything marked "work" must stay on local models.
DATA_CLASS = "personal"


class GoogleNotConnected(RuntimeError):
    pass


def libraries_installed() -> bool:
    try:
        import google.oauth2.credentials  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
        return True
    except Exception:
        return False


def credentials() -> Any:
    """Load the saved token, refreshing it when it has expired.

    Raises GoogleNotConnected when there is no usable token."""
    if not libraries_installed():
        raise GoogleNotConnected("Google libraries are not installed")
    if not TOKEN_PATH.exists():
        raise GoogleNotConnected(f"no token at {TOKEN_PATH}; run: python -m orchestrator.google_link connect")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GoogleNotConnected(f"token refresh failed ({exc}); run connect again") from exc
        _save(creds)
        return creds
    raise GoogleNotConnected("token is not valid; run connect again")


def _save(creds: Any) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")


def service(api: str, version: str) -> Any:
    from googleapiclient.discovery import build

    return build(api, version, credentials=credentials(), cache_discovery=False)


def status() -> dict[str, Any]:
    """Connection state for the UI and verify scripts. Never raises."""
    out: dict[str, Any] = {
        "libraries": libraries_installed(),
        "client_secret": CLIENT_SECRET_PATH.exists(),
        "token": TOKEN_PATH.exists(),
        "connected": False,
        "account": None,
        "scopes_missing": [],
        "error": None,
    }
    try:
        creds = credentials()
        granted = set(getattr(creds, "scopes", None) or [])
        out["scopes_missing"] = [s for s in SCOPES if granted and s not in granted]
        prof = service("gmail", "v1").users().getProfile(userId="me").execute()
        out["account"] = prof.get("emailAddress")
        out["connected"] = True
    except Exception as exc:  # noqa: BLE001 - status must not raise
        out["error"] = str(exc)[:300]
    return out


def connect() -> dict[str, Any]:
    """Run the one-time browser sign-in on this machine and save the token."""
    if not CLIENT_SECRET_PATH.exists():
        raise SystemExit(f"Missing {CLIENT_SECRET_PATH}. Download the Desktop OAuth client JSON from Google Cloud and save it there.")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    # access_type=offline + prompt=consent guarantee a refresh token.
    creds = flow.run_local_server(port=0, open_browser=True, access_type="offline", prompt="consent")
    _save(creds)
    return status()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "connect":
        print(json.dumps(connect(), indent=2))
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    else:
        raise SystemExit("usage: python -m orchestrator.google_link [connect|status]")
