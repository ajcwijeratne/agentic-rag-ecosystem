"""
Voice control of the Command Centre
===================================
Lets the conversation drive the whole cockpit, not just the chat box: move
between pages, hear what is on them, and run the operations that already have
buttons.

Matching is deterministic and local — a table of phrasings, no model call. That
is a deliberate latency choice: "show me deliverables" has to feel like pressing
the button, and a three-second round trip to an LLM to decide which page you
meant would make voice navigation worse than the mouse. Anything this table does
not recognise falls straight through to the normal agent, so nothing is lost by
keeping the table small and certain.

Three kinds of command:

  navigate  switch page. Free, instant, nothing else happens.
  readout   fetch what is on a page and say it in a sentence or two.
  action    run something. Anything that spends money or changes state needs
            confirmation first — the assistant says what it is about to do and
            waits for "yes".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import httpx

SELF_URL: str = os.getenv("SELF_URL", "http://localhost:8000")
READOUT_TIMEOUT: float = float(os.getenv("VOICE_READOUT_TIMEOUT_S", "20"))


def _headers() -> dict:
    key = os.getenv("API_KEY", "").strip()
    return {"X-API-Key": key} if key else {}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

# Page key -> what someone might call it out loud. Keys match the UI's NAV.
PAGES: dict = {
    "overview":     ["overview", "home", "dashboard", "apex", "the front page"],
    "chat":         ["chat", "conversation", "the chat"],
    "productivity": ["productivity", "my tasks", "tasks", "to do", "todo"],
    "admin":        ["usage", "spend", "costs", "cost", "billing", "admin"],
    "quality":      ["quality", "evals", "evaluation"],
    "library":      ["deliverables", "library", "documents", "the library"],
    "content":      ["content", "content pipeline", "pipeline", "posts"],
    "production":   ["production"],
    "media":        ["media", "media library", "assets"],
    "engagements":  ["engagements", "clients", "projects"],
    "kb":           ["knowledge base", "knowledge", "the kb", "vault"],
    "intel":        ["sector intel", "intel", "sector intelligence", "news"],
    "n8n":          ["automations", "automation", "workflows", "n8n"],
    "operating":    ["operating", "operating layer"],
    "schedule":     ["scheduled", "schedule", "scheduled runs", "cron"],
    "trace":        ["routing", "routing inspector", "traces", "trace"],
    "harness":      ["self improve", "self-improve", "harness", "improvements"],
}

_NAV_VERB = re.compile(
    r"\b(show|open|go\s+to|take\s+me\s+to|switch\s+to|bring\s+up|display|jump\s+to|"
    r"pull\s+up|navigate\s+to|let'?s\s+see)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Read-outs: page -> endpoint + how to say it
# ---------------------------------------------------------------------------

# Endpoints in this codebase name the human-readable field differently per
# source: title, head, name, t, q, text. Guessing one and hard-coding it is how
# read-outs ended up saying "untitled" four times, so try them in order instead.
_NAME_KEYS = ("title", "name", "head", "t", "text", "q", "label")


def _name_of(item, prefer: str = "") -> str:
    if not isinstance(item, dict):
        return str(item)
    for key in ((prefer,) if prefer else ()) + _NAME_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "untitled"


def _say_items(label: str, items: list, name_key: str = "", limit: int = 4) -> str:
    if not items:
        return f"There is nothing in {label} right now."
    names = [_name_of(it, name_key) for it in items[:limit]]
    more = f", and {len(items) - limit} more" if len(items) > limit else ""
    return f"{len(items)} in {label}. " + "; ".join(names) + more + "."


def _say_productivity(d) -> str:
    """
    The productivity page is several sections, not one list, so summarise the
    counts that matter rather than reading one arbitrary section aloud.
    """
    if not isinstance(d, dict):
        return _say_items("productivity", _items(d))
    parts = []
    for key, noun in (("tasks", "task"), ("inbox", "inbox item"), ("goals", "goal")):
        section = d.get(key)
        if isinstance(section, list) and section:
            parts.append(f"{len(section)} {noun}{'s' if len(section) != 1 else ''}")
    if not parts:
        return "Nothing in productivity right now."
    first = next((s for s in (d.get("tasks"), d.get("inbox")) if isinstance(s, list) and s), None)
    lead = f" First up: {_name_of(first[0])}." if first else ""
    return "You have " + ", ".join(parts) + "." + lead


def _say_cost(d: dict) -> str:
    total = d.get("total_cost_usd", d.get("total", 0)) or 0
    calls = d.get("total_calls", d.get("calls", 0)) or 0
    return f"Spend so far is {total:.2f} US dollars across {calls} calls."


def _say_pipeline(d) -> str:
    """
    Summarise the content board by stage.

    The endpoint returns a flat mapping of column name to its items —
    {"Ideas": [...], "Drafts": [...]} — with no "columns" key to look under.
    Reading for that key reported a board of 22 pieces as empty. The wrapped
    shapes are still accepted in case the endpoint changes.
    """
    cols: dict = {}
    if isinstance(d, dict):
        inner = d.get("columns") or d.get("cols")
        source = inner if isinstance(inner, dict) else d
        cols = {k: v for k, v in source.items() if isinstance(v, list)}
    elif isinstance(d, list) and d and isinstance(d[0], dict):
        cols = {c.get("title", c.get("col", "items")): c.get("items", []) for c in d}

    cols = {k: v for k, v in cols.items() if v}
    if not cols:
        return "The content pipeline is empty."

    total = sum(len(v) for v in cols.values())
    parts = [f"{len(v)} in {str(k).lower()}" for k, v in cols.items()]
    return f"{total} pieces in the content pipeline: " + ", ".join(parts) + "."


READOUTS: dict = {
    "library":      ("/deliverables", lambda d: _say_items("deliverables", _items(d))),
    "content":      ("/content/pipeline", _say_pipeline),
    "engagements":  ("/engagements", lambda d: _say_items("engagements", _items(d))),
    "intel":        ("/intel/feed", lambda d: _say_items("sector intel", _items(d), "head")),
    "schedule":     ("/schedule/list", lambda d: _say_items("scheduled runs", _items(d), "name")),
    "admin":        ("/cost", _say_cost),
    "trace":        ("/trace/recent", lambda d: _say_items("recent routing", _items(d), "q")),
    "productivity": ("/productivity/overview", _say_productivity),
}


def _items(d) -> list:
    """Endpoints return either a bare list or {items: [...]} — accept both."""
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for key in ("items", "rows", "groups", "metrics"):
            if isinstance(d.get(key), list):
                return d[key]
    return []


_READ_VERB = re.compile(
    r"\b(what'?s?\s+(in|on|my)|read( me)?|tell me( about)?|summari[sz]e|catch me up|"
    r"how many|what do i have|run\s+through|brief\s+me)\b",
    re.IGNORECASE,
)

# Spend *we* incurred, as opposed to the price of something in the business.
_OWN_SPEND = re.compile(
    r"\b(my|our|today'?s|this month'?s)\s+(spend|spending|costs?|budget|bill)\b"
    r"|\b(what|how much)\b[^.?]*\b(i|we|this|it|you)\b[^.?]*\b(spend|spent|spending|cost|costing)\b"
    r"|\bhow much (have|has|did|do)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

@dataclass
class Action:
    key:      str
    phrases:  list
    method:   str
    path:     str
    body:     dict = field(default_factory=dict)
    confirm:  bool = True          # anything that costs or mutates
    describe: str = ""


ACTIONS: list = [
    Action("reindex", ["reindex the vault", "re index the vault", "index the vault",
                       "refresh the knowledge base", "reindex"],
           "POST", "/index/vault", confirm=True,
           describe="re-index the Obsidian vault"),
    Action("harness", ["run the harness", "run self improve", "start self improvement",
                       "improve yourself"],
           "POST", "/harness/run", body={}, confirm=True,
           describe="run the self-improvement harness"),
    Action("new_chat", ["new chat", "start a new chat", "new conversation",
                        "clear the conversation"],
           "POST", "/sessions", confirm=False,
           describe="start a new conversation"),
]


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------

@dataclass
class Command:
    kind:    str            # navigate | readout | action
    target:  str
    spoken:  str = ""
    events:  list = field(default_factory=list)
    confirm: str = ""       # non-empty means: ask before doing it


def _match_page(text: str) -> str | None:
    """Longest alias first, so "sector intel" beats "intel"."""
    lowered = f" {re.sub(r'[^a-z0-9 ]+', ' ', text.lower())} "
    best, best_len = None, 0
    for page, aliases in PAGES.items():
        for alias in aliases:
            if f" {alias} " in lowered and len(alias) > best_len:
                best, best_len = page, len(alias)
    return best


def interpret(text: str) -> Command | None:
    """
    Map an utterance to a Command, or None to let the normal agent handle it.

    Order matters: a read verb wins over a navigation verb, because "what's in
    my deliverables" should answer rather than merely open the page.
    """
    if not text or not text.strip():
        return None
    clean = text.strip()

    for action in ACTIONS:
        if any(p in clean.lower() for p in action.phrases):
            return Command(
                kind="action", target=action.key,
                confirm=(f"Do you want me to {action.describe}?" if action.confirm else ""),
            )

    page = _match_page(clean)
    if page:
        if _READ_VERB.search(clean) and page in READOUTS:
            return Command(kind="readout", target=page)
        if _NAV_VERB.search(clean):
            return Command(kind="navigate", target=page)

    # Money questions name no page but mean the usage one. Matched separately
    # because they are phrased too many ways for the read-verb table ("what did
    # I spend", "how much has this cost me").
    #
    # Scoped to *our own* spend on purpose. A bare mention of "cost" would drag
    # in business questions like "what is the cost of a diagnostic sprint
    # engagement", which belongs to the knowledge base, not the usage page.
    if _OWN_SPEND.search(clean):
        return Command(kind="readout", target="admin")
    return None


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

# Commands awaiting a yes, per session. A confirmation is only meaningful for
# the turn straight after the question, so this is deliberately not persisted:
# a "yes" said ten minutes later should not fire a re-index.
_pending: dict = {}

_YES = re.compile(r"^\s*(yes|yeah|yep|yup|go ahead|do it|confirm|please do|ok|okay|sure)\b", re.I)
_NO = re.compile(r"^\s*(no|nope|cancel|stop|don'?t|never ?mind|forget it)\b", re.I)


def set_pending(session_id: str, cmd: "Command") -> None:
    _pending[session_id] = cmd


def resolve_pending(session_id: str, text: str):
    """
    Interpret an utterance as an answer to a pending confirmation.

    Returns (command_to_run, spoken_reply). Either may be None: a command means
    go ahead, a reply alone means it was declined or was not an answer at all.
    """
    cmd = _pending.get(session_id)
    if cmd is None:
        return None, None
    if _YES.search(text or ""):
        _pending.pop(session_id, None)
        return cmd, None
    if _NO.search(text or ""):
        _pending.pop(session_id, None)
        return None, "Cancelled."
    # Anything else: they moved on. Drop it rather than leaving it armed.
    _pending.pop(session_id, None)
    return None, None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _page_label(page: str) -> str:
    return PAGES.get(page, [page])[0]


async def execute(cmd: Command) -> dict:
    """Run a command and return an answer payload shaped like the others."""
    if cmd.kind == "navigate":
        return {
            "answer": f"Opening {_page_label(cmd.target)}.",
            "route":  "ui",
            "model":  "", "cost_usd": 0.0,
            "ui":     {"navigate": cmd.target},
        }

    if cmd.kind == "readout":
        path, say = READOUTS[cmd.target]
        try:
            async with httpx.AsyncClient(timeout=READOUT_TIMEOUT) as client:
                r = await client.get(f"{SELF_URL}{path}", headers=_headers())
                r.raise_for_status()
                spoken = say(r.json())
        except Exception as exc:  # noqa: BLE001
            spoken = f"I could not read {_page_label(cmd.target)}. {type(exc).__name__}."
        return {
            "answer": spoken, "route": "ui", "model": "", "cost_usd": 0.0,
            # Show the page being talked about, so the screen follows the voice.
            "ui": {"navigate": cmd.target},
        }

    action = next((a for a in ACTIONS if a.key == cmd.target), None)
    if action is None:
        return {"answer": "I do not know that command.", "route": "ui",
                "model": "", "cost_usd": 0.0}

    try:
        async with httpx.AsyncClient(timeout=READOUT_TIMEOUT) as client:
            r = await client.request(
                action.method, f"{SELF_URL}{action.path}",
                json=action.body or None, headers=_headers(),
            )
        ok = r.status_code < 400
        spoken = (f"Done. I started {action.describe}." if ok
                  else f"That failed with status {r.status_code}.")
    except Exception as exc:  # noqa: BLE001
        spoken = f"I could not {action.describe}. {type(exc).__name__}."
    return {"answer": spoken, "route": "ui", "model": "", "cost_usd": 0.0,
            "ui": {"action": action.key}}
