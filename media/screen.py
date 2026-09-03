"""
Screen awareness — capture
==========================
Lets the assistant see what is on the display, so "what am I looking at?" and
"summarise this page" have an answer.

Read-only by design. This module captures and describes; it never clicks, types,
or moves anything. Nothing here can act on the desktop.

Privacy matters more here than anywhere else in the voice stack. Everything
before this point runs locally — VAD, VOSK, Whisper, the wake word — and no
audio leaves the machine. A screenshot does leave, if it is described by a cloud
vision model, and a screenshot of your desktop is about the most sensitive thing
this system could transmit: open email, credentials, client documents. So:

  * It is off unless SCREEN_AWARENESS_ENABLED is set.
  * Nothing is ever captured automatically. Every capture answers a specific
    request made at that moment.
  * Every capture is written to the audit log with what triggered it.
  * A local vision model is preferred when one is available, in which case the
    image never leaves the machine at all.
  * Windows whose titles match SCREEN_BLOCKLIST are refused outright.

Capture uses Pillow's ImageGrab, which needs no extra dependency on Windows.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCREEN_ENABLED: bool = os.getenv("SCREEN_AWARENESS_ENABLED", "false").lower() in ("1", "true", "yes")
# Long edge in pixels. Vision models gain nothing from a 4K screenshot and it
# costs tokens roughly with area, so downscale hard before sending.
SCREEN_MAX_EDGE: int = int(os.getenv("SCREEN_MAX_EDGE", "1400"))
SCREEN_JPEG_QUALITY: int = int(os.getenv("SCREEN_JPEG_QUALITY", "70"))

# Windows that must never be captured, matched case-insensitively against the
# foreground window title. Password managers and banking by default.
SCREEN_BLOCKLIST: list = [
    w.strip().lower()
    for w in os.getenv(
        "SCREEN_BLOCKLIST",
        "1password,bitwarden,lastpass,keepass,dashlane,banking,internet banking",
    ).split(",")
    if w.strip()
]


class ScreenCaptureError(RuntimeError):
    """Capture was refused or impossible. The message is safe to show a user."""


@dataclass
class Capture:
    """One screenshot plus what was on screen when it was taken."""

    image_bytes: bytes
    width:       int
    height:      int
    window_title: str = ""
    app:         str = ""
    monitor:     str = "all"
    taken_at:    str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def size_kb(self) -> int:
        return len(self.image_bytes) // 1024

    def meta(self) -> dict:
        """Everything except the pixels — safe to log."""
        return {
            "width": self.width, "height": self.height,
            "window_title": self.window_title, "app": self.app,
            "monitor": self.monitor, "taken_at": self.taken_at,
            "size_kb": self.size_kb,
        }


# ---------------------------------------------------------------------------
# What is in front
# ---------------------------------------------------------------------------

def active_window() -> dict:
    """
    Title and owning process of the foreground window.

    Cheap and text-only: often enough on its own to know what someone is working
    on, without capturing any pixels at all.
    """
    info = {"title": "", "app": "", "pid": None}
    try:
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return info
        info["title"] = win32gui.GetWindowText(hwnd) or ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            info["pid"] = pid
            import psutil

            info["app"] = psutil.Process(pid).name()
        except Exception:
            pass
    except Exception:
        pass
    return info


def is_blocked(title: str) -> str | None:
    """Return the blocklist entry that forbids capturing this window, if any."""
    lowered = (title or "").lower()
    for blocked in SCREEN_BLOCKLIST:
        if blocked and blocked in lowered:
            return blocked
    return None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def _encode(image, max_edge: int = SCREEN_MAX_EDGE) -> tuple[bytes, int, int]:
    """Downscale and JPEG-encode. Returns (bytes, width, height)."""
    from PIL import Image

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    longest = max(image.size)
    if longest > max_edge:
        scale = max_edge / longest
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=SCREEN_JPEG_QUALITY, optimize=True)
    return buf.getvalue(), image.width, image.height


def capture(
    region: str = "screen",
    enabled: bool | None = None,
    reason: str = "",
) -> Capture:
    """
    Take a screenshot.

    `region` is "screen" (everything) or "window" (just the foreground window).
    Raises ScreenCaptureError when disabled, blocked, or unsupported — callers
    surface the message rather than failing silently, because a user who asked
    "what's on my screen?" needs to know *why* nothing came back.
    """
    if enabled is None:
        enabled = SCREEN_ENABLED
    if not enabled:
        raise ScreenCaptureError(
            "Screen awareness is off. Set SCREEN_AWARENESS_ENABLED=true to allow it."
        )

    window = active_window()
    blocked = is_blocked(window["title"])
    if blocked:
        raise ScreenCaptureError(
            f"That window is on the screen-capture blocklist ({blocked}), so I did not look."
        )

    try:
        from PIL import ImageGrab
    except Exception as exc:
        raise ScreenCaptureError(f"Screen capture needs Pillow: {exc}") from exc

    try:
        if region == "window":
            bbox = _foreground_bbox()
            image = ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab(all_screens=True)
        else:
            image = ImageGrab.grab(all_screens=True)
    except Exception as exc:
        raise ScreenCaptureError(f"Could not capture the screen: {exc}") from exc

    data, w, h = _encode(image)
    shot = Capture(
        image_bytes=data, width=w, height=h,
        window_title=window["title"], app=window["app"], monitor=region,
    )

    # Auditable: a screenshot is the most sensitive thing this system handles, so
    # every one is recorded with what asked for it. Never the pixels.
    try:
        from common.security import audit_log

        audit_log("screen.capture", {"reason": reason[:200], **shot.meta()})
    except Exception:
        pass

    return shot


def _foreground_bbox():
    """Bounding box of the foreground window, or None to fall back to full screen."""
    try:
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 50 or bottom - top < 50:
            return None
        return (left, top, right, bottom)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------

# Spoken phrasings that mean "look at my screen" rather than "search the
# knowledge base". Matched before retrieval so the assistant does not answer a
# question about the screen out of the vault.
_SCREEN_INTENT = re.compile(
    r"\b("
    r"(what|whats|what's)\s+(is\s+)?(on|in)\s+(my|the)\s+screen"
    r"|what\s+am\s+i\s+(looking\s+at|seeing|reading)"
    r"|(read|describe|summarise|summarize|explain|look\s+at)\s+(this|my|the)\s+"
    r"(screen|page|window|document|error|code|chart|diagram)"
    r"|what\s+does\s+(this|the)\s+(say|show|mean)"
    r"|(can\s+you\s+)?see\s+my\s+screen"
    r")\b",
    re.IGNORECASE,
)


def wants_screen(text: str) -> bool:
    """Is this question about what is on screen rather than about the vault?"""
    return bool(_SCREEN_INTENT.search(text or ""))


def region_for(text: str) -> str:
    """Whether the question is about the whole screen or just the active window."""
    lowered = (text or "").lower()
    if any(w in lowered for w in ("this window", "the window", "this app", "active window")):
        return "window"
    return "screen"
