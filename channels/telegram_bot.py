"""
Telegram Channel
================
Two-way bridge between Aaron's phone and the orchestrator. Long-polls the
Telegram Bot API with httpx (no SDK dependency), forwards messages to
POST /inbox, and renders governance approvals as inline buttons.

Security: only TELEGRAM_ALLOWED_CHAT_ID is answered. Every other chat gets
silence and a log line. Approval callbacks re-check the chat ID before acting.

Env:
  TELEGRAM_BOT_TOKEN        falls back to APPRISE_TELEGRAM_TOKEN
  TELEGRAM_ALLOWED_CHAT_ID  falls back to APPRISE_TELEGRAM_CHAT_ID
  ORCHESTRATOR_URL          default http://localhost:8000
  ORCH_API_KEY              sent as X-API-Key when set (loopback needs none)

Commands:
  /brief    the operating daily brief
  /pending  pending governance gates with approve / reject buttons
  /status   daemon status, budget, next action
  /pause    pause the daemon      /resume  resume it
  plan: ... generate a full operating plan
  anything else is classified by the inbox: question or task

Run: python -m channels.telegram_bot
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("telegram_bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("APPRISE_TELEGRAM_TOKEN", "")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_ALLOWED_CHAT_ID") or os.getenv("APPRISE_TELEGRAM_CHAT_ID", "")
ORCH_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("ORCH_API_KEY", os.getenv("API_KEY", ""))

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
POLL_TIMEOUT = 50


def _orch_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


async def _orch(client: httpx.AsyncClient, method: str, path: str, payload: dict | None = None) -> dict:
    r = await client.request(method, f"{ORCH_URL}{path}", json=payload, headers=_orch_headers(), timeout=180)
    r.raise_for_status()
    return r.json()


async def _send(client: httpx.AsyncClient, chat_id: str, text: str,
                reply_markup: dict | None = None) -> None:
    # Telegram caps messages at 4096 chars; split on paragraph boundaries.
    chunks: list[str] = []
    while text:
        if len(text) <= 4000:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, 4000)
        cut = cut if cut > 2000 else 4000
        chunks.append(text[:cut])
        text = text[cut:]
    for i, chunk in enumerate(chunks):
        payload: dict = {"chat_id": chat_id, "text": chunk}
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            await client.post(f"{TG}/sendMessage", data=payload, timeout=30)
        except Exception:
            logger.exception("sendMessage failed")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _cmd_brief(client: httpx.AsyncClient, chat_id: str) -> None:
    brief = await _orch(client, "GET", "/operating/daily-brief")
    lines = ["Daily brief"]
    for key in ("headline", "summary"):
        if brief.get(key):
            lines.append(str(brief[key]))
    for section in ("priorities", "approvals", "productions", "project_memory"):
        items = brief.get(section) or []
        if items:
            lines.append(f"\n{section.replace('_', ' ').title()}:")
            for item in items[:6]:
                if isinstance(item, dict):
                    lines.append("- " + (item.get("title") or item.get("content") or json.dumps(item)[:120]))
                else:
                    lines.append(f"- {item}")
    if len(lines) == 1:
        lines.append(json.dumps(brief, indent=2)[:3500])
    await _send(client, chat_id, "\n".join(lines))


async def _cmd_pending(client: httpx.AsyncClient, chat_id: str) -> None:
    pending = await _orch(client, "GET", "/governance/pending")
    items = pending.get("items") or []
    if not items:
        await _send(client, chat_id, "No pending approvals.")
        return
    for item in items[:10]:
        gate = item.get("gate", "?")
        target = item.get("target_id") or item.get("production_id") or "?"
        title = item.get("title") or item.get("reason") or ""
        markup = {"inline_keyboard": [[
            {"text": "Approve", "callback_data": f"approve|{gate}|{target}"},
            {"text": "Reject", "callback_data": f"reject|{gate}|{target}"},
        ]]}
        await _send(client, chat_id, f"Gate: {gate}\nTarget: {target}\n{title}", reply_markup=markup)


async def _cmd_status(client: httpx.AsyncClient, chat_id: str) -> None:
    try:
        st = await _orch(client, "GET", "/operating/daemon/status")
    except Exception as exc:
        await _send(client, chat_id, f"Daemon status unavailable: {exc}")
        return
    budget = st.get("budget") or {}
    lines = [
        f"Daemon: {'paused' if st.get('paused') else 'running'}",
        f"Cycles: {st.get('cycles')}  Last heartbeat: {st.get('last_heartbeat')}",
        f"Last result: {json.dumps(st.get('last_result') or {})[:300]}",
    ]
    if budget.get("enabled"):
        lines.append(f"Budget: ${budget.get('spent_usd')} / ${budget.get('budget_usd')} ({budget.get('level')})")
    else:
        lines.append("Budget breaker: disabled (set MONTHLY_BUDGET_USD)")
    try:
        nxt = await _orch(client, "GET", "/operating/next-action")
        task = nxt.get("task")
        lines.append("Next action: " + (task.get("title") if task else nxt.get("reason", "none")))
    except Exception:
        pass
    await _send(client, chat_id, "\n".join(lines))


async def _handle_text(client: httpx.AsyncClient, chat_id: str, text: str) -> None:
    stripped = text.strip()
    lower = stripped.lower()
    if lower in ("/start", "/help"):
        await _send(client, chat_id,
                    "Commands: /brief /pending /status /pause /resume\n"
                    "plan: <goal> creates a plan. approve <gate> <id> approves. "
                    "Anything else becomes a question or a task.")
        return
    if lower == "/brief":
        await _cmd_brief(client, chat_id); return
    if lower == "/pending":
        await _cmd_pending(client, chat_id); return
    if lower == "/status":
        await _cmd_status(client, chat_id); return
    if lower == "/pause":
        await _orch(client, "POST", "/operating/daemon/pause", {"actor": "telegram"})
        await _send(client, chat_id, "Daemon paused."); return
    if lower == "/resume":
        await _orch(client, "POST", "/operating/daemon/resume", {"actor": "telegram"})
        await _send(client, chat_id, "Daemon resumed."); return

    result = await _orch(client, "POST", "/inbox", {
        "channel": "telegram", "sender": chat_id, "text": stripped,
    })
    kind = result.get("kind")
    if kind == "answer":
        await _send(client, chat_id, result.get("answer") or "(no answer)")
    elif kind == "task":
        await _send(client, chat_id, f"Queued as task {result.get('task_id')}. "
                                     "The daemon picks it up within a minute.")
    elif kind == "plan":
        await _send(client, chat_id,
                    f"Plan {result.get('plan_id')} created "
                    f"({result.get('workflow')}, {result.get('task_count')} tasks).")
    elif kind == "approval":
        await _send(client, chat_id, f"{result.get('status')}: {result.get('gate')} "
                                     f"for {result.get('target_id')}")
    else:
        await _send(client, chat_id, json.dumps(result)[:1000])


async def _handle_callback(client: httpx.AsyncClient, callback: dict) -> None:
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    data = callback.get("data", "")
    if chat_id != str(ALLOWED_CHAT_ID):
        logger.warning("callback from unauthorised chat %s ignored", chat_id)
        return
    try:
        verb, gate, target = data.split("|", 2)
    except ValueError:
        return
    result = await _orch(client, "POST", "/inbox", {
        "channel": "telegram", "sender": chat_id,
        "text": f"{verb} {gate} {target}", "mode": "auto",
    })
    await client.post(f"{TG}/answerCallbackQuery",
                      data={"callback_query_id": callback.get("id"), "text": result.get("status", "done")},
                      timeout=30)
    await _send(client, chat_id, f"{result.get('status', 'done')}: {gate} for {target}")


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN (or APPRISE_TELEGRAM_TOKEN) is required")
    if not ALLOWED_CHAT_ID:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID (or APPRISE_TELEGRAM_CHAT_ID) is required")
    logger.info("telegram bot starting; allowed chat %s", ALLOWED_CHAT_ID)
    offset = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get(f"{TG}/getUpdates",
                                     params={"timeout": POLL_TIMEOUT, "offset": offset},
                                     timeout=POLL_TIMEOUT + 10)
                updates = r.json().get("result", [])
            except Exception:
                logger.exception("getUpdates failed; backing off")
                await asyncio.sleep(10)
                continue
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                try:
                    if "callback_query" in upd:
                        await _handle_callback(client, upd["callback_query"])
                        continue
                    msg = upd.get("message") or {}
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text") or ""
                    if not text:
                        continue
                    if chat_id != str(ALLOWED_CHAT_ID):
                        logger.warning("message from unauthorised chat %s ignored", chat_id)
                        continue
                    await _handle_text(client, chat_id, text)
                except Exception:
                    logger.exception("update handling failed")


if __name__ == "__main__":
    asyncio.run(main())
