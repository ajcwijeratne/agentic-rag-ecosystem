"""
Agentic Tool-Calling Executor
=============================
Lets a WijerCo agent call tools mid-conversation.

The model is given local Command Centre tools plus any n8n MCP tools as callable
functions. When it decides to use one, the orchestrator executes it, feeds the
result back, and the model continues — looping until it produces a final answer.

Supports two provider styles:
  • OpenAI-compatible (openai, deepseek, google-via-compat) — function calling
  • Anthropic (Claude) — tool_use blocks

Tool calls require a cloud, tool-capable model, so the executor forces tier >= 1
(Ollama is skipped for tool turns). Emits an event stream so the UI can show
"⚙ ran workflow X" as it happens.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, AsyncGenerator

from .llm_registry import MODELS
from .token_optimizer import pick_model, rough_token_count

MAX_TOOL_ITERS = 12         # rounds the model may spend CALLING tools.
# The n8n Workflow SDK build path is a gated chain (plan -> sdk reference ->
# write -> validate -> create -> publish), ~8-10 sequential calls, so the budget
# must comfortably exceed it or builds get cut off mid-pipeline.
_TOOLS_CACHE: dict[str, Any] = {"tools": None, "fetched_at": 0.0}
_TOOLS_TTL = 300.0   # seconds

_LOCAL_TOOLS = [
    {
        "name": "create_content_production",
        "description": (
            "Create a Content Studio production record so the agent team can "
            "advance it from idea through brief, research, script, asset plan, "
            "render, review, publish, and measure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short production title."},
                "project": {"type": "string", "description": "Project or client name.", "default": "WijerCo"},
                "format": {
                    "type": "string",
                    "enum": [
                        "linkedin_short",
                        "explainer_carousel",
                        "talking_head_clip",
                        "policy_briefing",
                        "course_teaser",
                        "proposal_walkthrough",
                    ],
                    "description": "Production format.",
                    "default": "talking_head_clip",
                },
                "owner": {"type": "string", "description": "Owner name.", "default": "Aaron"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "advance_content_production",
        "description": "Advance one Content Studio production by exactly one state and run the mapped agent stage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "production_id": {"type": "string", "description": "Production ID to advance."},
                "actor": {"type": "string", "description": "Who initiated the advance.", "default": "chat"},
            },
            "required": ["production_id"],
        },
    },
    {
        "name": "advance_content_production_until_blocked",
        "description": (
            "Keep advancing a Content Studio production until it reaches review/publish/measure, "
            "a governance gate blocks it, or max_steps is reached."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "production_id": {"type": "string", "description": "Production ID to advance."},
                "actor": {"type": "string", "description": "Who initiated the advance.", "default": "chat"},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 9, "default": 9},
                "stop_at": {
                    "type": "string",
                    "enum": ["review", "publish", "measure", "done"],
                    "default": "review",
                    "description": "State at which the automation should stop.",
                },
            },
            "required": ["production_id"],
        },
    },
    {
        "name": "get_content_production",
        "description": "Fetch a Content Studio production record, including events and generated stage outputs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "production_id": {"type": "string", "description": "Production ID to inspect."},
            },
            "required": ["production_id"],
        },
    },
    {
        "name": "list_content_productions",
        "description": "List recent Content Studio productions, optionally filtered by state or project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "Optional production state filter."},
                "project": {"type": "string", "description": "Optional project filter."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
        },
    },
    {
        "name": "list_media_tools",
        "description": "List configured local multimedia tools and whether they are enabled and available.",
        "input_schema": {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "enum": ["video", "image", "voice", "avatar", "animation", "transcribe", "visual_embed"],
                    "description": "Optional capability filter.",
                },
                "enabled_only": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "generate_media_for_production",
        "description": (
            "Run one local multimedia generation job for a Content Studio production. "
            "Use this after a production has a script or asset plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "production_id": {"type": "string", "description": "Production ID to attach generated media to."},
                "capability": {
                    "type": "string",
                    "enum": ["video", "image", "voice", "avatar", "animation"],
                    "description": "Kind of media to generate.",
                },
                "brief": {"type": "object", "description": "Generation brief. For video, template/props are filled from the production when omitted."},
                "tool": {"type": "string", "description": "Optional registered tool name override."},
                "rights": {"type": "string", "default": "owned"},
            },
            "required": ["production_id", "capability"],
        },
    },
    {
        "name": "generate_planned_media_for_production",
        "description": (
            "Extract generation briefs from a Content Studio production asset plan, "
            "run the matching local media jobs, and attach produced assets to the production."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "production_id": {"type": "string", "description": "Production ID whose asset plan should be generated."},
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["video", "image", "voice", "avatar", "animation"]},
                    "description": "Optional capability filter.",
                },
                "include_video": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
                "max_jobs": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "required": ["production_id"],
        },
    },
    {
        "name": "create_operating_plan",
        "description": "Create a Phase 4 operating plan with persistent task state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "project": {"type": "string"},
                "goal": {"type": "string"},
                "owner": {"type": "string", "default": "Aaron"},
                "tasks": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["title"],
        },
    },
    {
        "name": "generate_operating_plan",
        "description": "Turn a goal into a sequenced operating plan with dependencies, risk flags, and a recommended next action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "title": {"type": "string"},
                "project": {"type": "string"},
                "owner": {"type": "string", "default": "Aaron"},
                "workflow": {"type": "string", "enum": ["content_studio", "deployment", "incident", "general"]},
                "create": {"type": "boolean", "default": True},
                "context": {"type": "object"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "add_operating_task",
        "description": "Add a task to an operating plan or standalone operating queue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "title": {"type": "string"},
                "type": {"type": "string", "enum": ["agent", "approval", "production", "memory", "manual"], "default": "manual"},
                "status": {"type": "string", "enum": ["todo", "doing", "blocked", "waiting_approval", "done", "cancelled"], "default": "todo"},
                "assignee": {"type": "string"},
                "priority": {"type": "integer", "default": 3},
                "target_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "get_operating_daily_brief",
        "description": "Return the current daily operating brief: priorities, pending approvals, productions, and project memory.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remember_project_fact",
        "description": "Store a durable project memory for future planning and daily briefs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "content": {"type": "string"},
                "source": {"type": "string", "default": "agent"},
            },
            "required": ["project", "content"],
        },
    },
    {
        "name": "sync_operating_plan_to_obsidian",
        "description": "Write an operating plan to the configured Obsidian Projects folder as a Markdown project note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "overwrite": {"type": "boolean", "default": True},
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "import_obsidian_project_memory",
        "description": "Import Context, Decisions, Risks, and Client Preferences sections from Obsidian project notes into planner memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["project"],
        },
    },
]
_LOCAL_TOOL_NAMES = {tool["name"] for tool in _LOCAL_TOOLS}

# Appended to the system prompt on the agentic path so the model walks the
# n8n build pipeline in order instead of looping on discovery tools.
_TOOL_USE_GUIDANCE = (
    "\n\n---\n\n## Using tools\n"
    "These tools can run real Command Centre actions, including Content Studio productions, "
    "and may also build or run n8n workflows. Use them deliberately and move forward.\n"
    "- For Content Studio production requests, prefer the local tools: create_content_production, "
    "advance_content_production, advance_content_production_until_blocked, get_content_production, "
    "and list_content_productions.\n"
    "- If the user asks to automate stages between Content Studio agents, create or locate the "
    "production, then advance it until the requested stopping point, normally review.\n"
    "- Do NOT repeat a search or reference call you have already made — reuse the result you have.\n"
    "- Honour the ordering stated in the tool descriptions. To build a workflow the required path is:\n"
    "  plan nodes (get_suggested_nodes / search_nodes) -> read get_sdk_reference -> write SDK code ->\n"
    "  validate (validate_node_config, then validate_workflow) -> create_workflow_from_code -> publish_workflow.\n"
    "- Advance through these steps; never loop back to discovery once a step has what it needs.\n"
    "- The moment you can take the next concrete action, take it instead of searching more.\n"
    "- Never end a turn having only searched. If the tool budget runs out, output the validated SDK "
    "code and the remaining steps so the work isn't lost."
)


# ─────────────────────────────────────────────────────────────────────────────
# n8n tool discovery + schema conversion
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_")
    return (s or "tool")[:64]


async def get_available_tools() -> tuple[list[dict], dict[str, str]]:
    """
    Return (tools, name_map) where tools includes local Command Centre tools and
    any raw n8n tool list. name_map maps sanitized function name to display/original
    tool name. n8n discovery is cached for the TTL.
    """
    now = time.time()
    if _TOOLS_CACHE["tools"] is not None and now - _TOOLS_CACHE["fetched_at"] < _TOOLS_TTL:
        n8n_tools = _TOOLS_CACHE["tools"]
    else:
        from .n8n_client import list_tools
        try:
            n8n_tools = await list_tools()
        except Exception:
            n8n_tools = []
        _TOOLS_CACHE["tools"] = n8n_tools
        _TOOLS_CACHE["fetched_at"] = now

    tools = [*_LOCAL_TOOLS, *n8n_tools]
    name_map = {_sanitize(t["name"]): t["name"] for t in tools}
    return tools, name_map


async def get_n8n_tools() -> tuple[list[dict], dict[str, str]]:
    """Backward-compatible alias for older imports."""
    return await get_available_tools()


def _openai_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        schema = t.get("input_schema") or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name":        _sanitize(t["name"]),
                "description": (t.get("description") or t["name"])[:1024],
                "parameters":  schema,
            },
        })
    return out


def _anthropic_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        schema = t.get("input_schema") or {"type": "object", "properties": {}}
        out.append({
            "name":        _sanitize(t["name"]),
            "description": (t.get("description") or t["name"])[:1024],
            "input_schema": schema,
        })
    return out


async def _execute_local_tool(func_name: str, args: dict) -> str:
    """Run a local Command Centre tool and return JSON text."""
    from . import production as production_store
    from . import operating as operating_store

    args = args or {}
    if func_name == "create_content_production":
        pid = production_store.create_production(
            title=str(args.get("title") or "Untitled production"),
            project=args.get("project") or "WijerCo",
            format=args.get("format") or "talking_head_clip",
            owner=args.get("owner") or "Aaron",
        )
        return json.dumps(production_store.get_production(pid), ensure_ascii=False)

    if func_name == "advance_content_production":
        result = await production_store.advance(
            str(args.get("production_id") or ""),
            actor=args.get("actor") or "chat",
        )
        return json.dumps(result, ensure_ascii=False)

    if func_name == "advance_content_production_until_blocked":
        production_id = str(args.get("production_id") or "")
        actor = args.get("actor") or "chat"
        max_steps = max(1, min(9, int(args.get("max_steps") or 9)))
        stop_at = args.get("stop_at") or "review"
        steps = []
        result: dict[str, Any] = {}
        for _ in range(max_steps):
            result = await production_store.advance(production_id, actor=actor)
            steps.append(result)
            prod = result.get("production") or {}
            state = prod.get("state")
            if result.get("blocked") or result.get("done") or state == stop_at:
                break
        return json.dumps({"steps": steps, "final": result}, ensure_ascii=False)

    if func_name == "get_content_production":
        production = production_store.get_production(str(args.get("production_id") or ""))
        return json.dumps(production or {"error": "production not found"}, ensure_ascii=False)

    if func_name == "list_content_productions":
        items = production_store.list_productions(
            state=args.get("state") or None,
            project=args.get("project") or None,
            limit=max(1, min(50, int(args.get("limit") or 10))),
        )
        return json.dumps({"items": items}, ensure_ascii=False)

    if func_name == "list_media_tools":
        from media import tool_registry

        items = tool_registry.list_tools(
            capability=args.get("capability") or None,
            enabled_only=bool(args.get("enabled_only", False)),
        )
        return json.dumps({"items": items}, ensure_ascii=False)

    if func_name == "generate_media_for_production":
        from . import production_media

        production_id = str(args.get("production_id") or "")
        try:
            result = production_media.generate_one_for_production(
                production_id,
                capability=str(args.get("capability") or ""),
                brief=dict(args.get("brief") or {}),
                tool=args.get("tool") or None,
                rights=args.get("rights") or "owned",
                meta={"actor": "chat"},
            )
        except KeyError:
            result = {"error": "production not found"}
        return json.dumps(result, ensure_ascii=False)

    if func_name == "generate_planned_media_for_production":
        from . import production_media

        try:
            result = production_media.generate_plan_for_production(
                str(args.get("production_id") or ""),
                capabilities=args.get("capabilities") or None,
                include_video=bool(args.get("include_video", False)),
                dry_run=bool(args.get("dry_run", False)),
                max_jobs=max(1, min(50, int(args.get("max_jobs") or 20))),
                actor="chat",
            )
        except KeyError:
            result = {"error": "production not found"}
        return json.dumps(result, ensure_ascii=False)

    if func_name == "create_operating_plan":
        plan_id = operating_store.create_plan(
            str(args.get("title") or "Untitled operating plan"),
            project=args.get("project") or None,
            goal=args.get("goal") or None,
            owner=args.get("owner") or "Aaron",
            tasks=args.get("tasks") or [],
        )
        return json.dumps(operating_store.get_plan(plan_id), ensure_ascii=False)

    if func_name == "generate_operating_plan":
        result = operating_store.generate_plan_from_goal(
            str(args.get("goal") or ""),
            title=args.get("title") or None,
            project=args.get("project") or None,
            owner=args.get("owner") or "Aaron",
            workflow=args.get("workflow") or None,
            context=args.get("context") or {},
            create=bool(args.get("create", True)),
        )
        return json.dumps(result, ensure_ascii=False)

    if func_name == "add_operating_task":
        try:
            task_id = operating_store.add_task(
                args.get("plan_id") or None,
                str(args.get("title") or "Untitled task"),
                type=args.get("type") or "manual",
                status=args.get("status") or "todo",
                assignee=args.get("assignee") or None,
                priority=int(args.get("priority") or 3),
                target_id=args.get("target_id") or None,
                note=args.get("note") or None,
            )
            result = {"task_id": task_id, "overview": operating_store.overview()}
        except KeyError:
            result = {"error": "plan not found"}
        return json.dumps(result, ensure_ascii=False)

    if func_name == "get_operating_daily_brief":
        return json.dumps(operating_store.daily_brief(), ensure_ascii=False)

    if func_name == "remember_project_fact":
        memory_id = operating_store.add_project_memory(
            str(args.get("project") or "WijerCo"),
            str(args.get("content") or ""),
            source=args.get("source") or "agent",
        )
        return json.dumps({"memory_id": memory_id}, ensure_ascii=False)

    if func_name == "sync_operating_plan_to_obsidian":
        from . import obsidian_projects as obsidian_projects_store

        result = obsidian_projects_store.sync_plan(
            str(args.get("plan_id") or ""),
            overwrite=bool(args.get("overwrite", True)),
        )
        return json.dumps(result, ensure_ascii=False)

    if func_name == "import_obsidian_project_memory":
        from . import obsidian_projects as obsidian_projects_store

        result = obsidian_projects_store.import_project_notes(
            str(args.get("project") or ""),
            limit=int(args.get("limit") or 20),
        )
        return json.dumps(result, ensure_ascii=False)

    return json.dumps({"error": f"unknown local tool {func_name}"})


async def _execute(func_name: str, args: dict, name_map: dict[str, str]) -> str:
    """Run a local or n8n tool and return its result as a string."""
    if func_name in _LOCAL_TOOL_NAMES:
        try:
            return (await _execute_local_tool(func_name, args or {}))[:6000]
        except Exception as exc:
            return f"[tool error: {exc}]"

    from .n8n_client import call_tool
    original = name_map.get(func_name, func_name)
    try:
        result = await call_tool(original, args or {})
        content = result.get("content", "")
        return content if isinstance(content, str) else json.dumps(content)[:4000]
    except Exception as exc:
        return f"[tool error: {exc}]"


# ─────────────────────────────────────────────────────────────────────────────
# Model selection
# ─────────────────────────────────────────────────────────────────────────────

# Lowest tier a tool turn may use. Default 1 keeps tool turns in the budget
# cloud tier; raise to 2 (env MIN_TOOL_TIER=2) to force mid-tier for tool work.
_MIN_TOOL_TIER = int(os.getenv("MIN_TOOL_TIER", "1"))


def _eligible_tool_models(max_tier: int):
    """Funded, cloud, function-calling-capable models within max_tier."""
    from .llm_registry import available_models
    return {
        k: v for k, v in available_models().items()
        if v.provider != "ollama"
        and getattr(v, "supports_tools", True)
        and v.tier <= max_tier
    }


def _pick_tool_model(
    query: str,
    system_prompt: str,
    max_tier: int,
    force_model_key: str | None = None,
) -> str:
    """
    Choose the model for a tool (agentic) turn.

    Order of precedence:
      1. An explicit, tool-capable model (the UI dropdown's force_model_key, or
         the TOOL_MODEL_KEY env pin) — honoured as long as it can call tools.
      2. Policy default: stay in the lowest funded tier (>= MIN_TOOL_TIER) and,
         within it, pick the "smartest" model — the one with the broadest
         capability set — preferring code-capable models. This upgrades the Auto
         pick from the cheapest tool model to the strongest one in the same tier.
      3. Graceful fallbacks if no funded cloud tool model exists.
    """
    eligible = _eligible_tool_models(max_tier)

    # ── 1. Explicit override (dropdown or env pin) ───────────────────────────
    override = (force_model_key or os.getenv("TOOL_MODEL_KEY", "").strip()) or None
    if override and override in eligible:
        return override
    if override and override in MODELS and not getattr(MODELS[override], "supports_tools", True):
        # User asked for a model that can't call tools — ignore it and fall
        # through to the policy default rather than silently failing every call.
        pass

    # ── 3 (early): nothing funded that can call tools ────────────────────────
    if not eligible:
        # Defer to the optimizer; the loop will surface an error if it returns
        # an unusable (e.g. ollama) model, which is better than crashing here.
        return pick_model(query, system_prompt, max_tier=max(1, max_tier)).model_key

    # ── 2. Policy default: lowest tier at/above the floor, smartest within ────
    floor = max(_MIN_TOOL_TIER, min(v.tier for v in eligible.values()))
    tiers_at_or_above = sorted({v.tier for v in eligible.values() if v.tier >= floor})
    target_tier = tiers_at_or_above[0] if tiers_at_or_above else min(v.tier for v in eligible.values())

    in_tier = [(k, v) for k, v in eligible.items() if v.tier == target_tier]
    # "Smartest" proxy: code-capable first, then broadest capability set,
    # then higher input price (stronger within a tier), then key for determinism.
    in_tier.sort(key=lambda kv: (
        0 if "code" in kv[1].capabilities else 1,
        -len(kv[1].capabilities),
        -kv[1].cost_input_per_m,
        kv[0],
    ))
    return in_tier[0][0]


# ─────────────────────────────────────────────────────────────────────────────
# Agentic turn (event generator)
# ─────────────────────────────────────────────────────────────────────────────

async def run_agentic_turn(
    user_message:    str,
    system_prompt:   str,
    history:         list[dict] | None = None,
    max_tier:        int = 3,
    force_model_key: str | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Yields events:
      {"type":"tool_call","tool": "...", "args": {...}}
      {"type":"tool_result","tool": "...", "ok": bool}
      {"type":"token","token": "...", "done": False}
      {"type":"end","done": True, "model_label","provider","cost_usd",...}
    """
    history = history or []
    tools, name_map = await get_available_tools()

    # No tools available — defer to the normal non-tool streaming path.
    if not tools:
        from .fallback_chain import stream_with_fallback
        async for ev in stream_with_fallback(user_message, system_prompt, history, max_tier=max_tier):
            yield ev
        return

    model_key = _pick_tool_model(user_message, system_prompt, max_tier, force_model_key)
    spec      = MODELS[model_key]

    # Bias the model toward acting rather than looping on discovery tools.
    system_prompt = (system_prompt or "") + _TOOL_USE_GUIDANCE

    try:
        if spec.provider == "anthropic":
            async for ev in _anthropic_loop(spec, system_prompt, user_message, history, tools, name_map):
                yield ev
        else:
            async for ev in _openai_loop(spec, system_prompt, user_message, history, tools, name_map):
                yield ev
    except Exception as exc:
        yield {"type": "token", "token": f"[tool-agent error: {exc}]", "done": False}
        yield {"type": "end", "done": True, "model_key": model_key,
               "model_label": spec.label, "provider": spec.provider, "cost_usd": 0.0}


# ── OpenAI-compatible loop (openai / deepseek / google) ──────────────────────

async def _openai_loop(spec, system, user, history, tools, name_map):
    from openai import AsyncOpenAI
    from .multi_llm import GOOGLE_OPENAI_BASE

    if spec.provider == "deepseek":
        client = AsyncOpenAI(api_key=os.getenv("DEEPSEEK_API_KEY", ""), base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"))
    elif spec.provider == "google":
        client = AsyncOpenAI(api_key=os.getenv("GOOGLE_API_KEY", ""), base_url=os.getenv("GOOGLE_OPENAI_BASE", GOOGLE_OPENAI_BASE))
    else:
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

    messages = ([{"role": "system", "content": system}] if system else []) + list(history) + [{"role": "user", "content": user}]
    oai_tools = _openai_tools(tools)
    in_tok = out_tok = 0

    # MAX_TOOL_ITERS rounds may call tools; the final extra round forces a
    # text answer (tools off) so the turn can never dead-end after only searching.
    for i in range(MAX_TOOL_ITERS + 1):
        final_turn = i == MAX_TOOL_ITERS
        resp = await client.chat.completions.create(
            model=spec.model_id, messages=messages, tools=oai_tools,
            tool_choice="none" if final_turn else "auto", max_tokens=spec.max_output,
        )
        if resp.usage:
            in_tok += resp.usage.prompt_tokens or 0
            out_tok += resp.usage.completion_tokens or 0
        msg = resp.choices[0].message

        if msg.tool_calls and not final_turn:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                yield {"type": "tool_call", "tool": name_map.get(tc.function.name, tc.function.name), "args": args}
                result = await _execute(tc.function.name, args, name_map)
                yield {"type": "tool_result", "tool": name_map.get(tc.function.name, tc.function.name), "ok": "[tool error" not in result}
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result[:6000]})
            continue

        # Final answer
        content = msg.content or ""
        for chunk in _chunk_text(content):
            yield {"type": "token", "token": chunk, "done": False}
        yield {"type": "end", "done": True, "model_key": f"{spec.provider}/{spec.model_id}",
               "model_label": spec.label, "provider": spec.provider,
               "cost_usd": spec.estimated_cost_usd(in_tok, out_tok),
               "input_tokens": in_tok, "output_tokens": out_tok}
        return

    yield {"type": "token", "token": "[stopped after max tool iterations]", "done": False}
    yield {"type": "end", "done": True, "model_label": spec.label, "provider": spec.provider, "cost_usd": spec.estimated_cost_usd(in_tok, out_tok)}


# ── Anthropic loop ───────────────────────────────────────────────────────────

async def _anthropic_loop(spec, system, user, history, tools, name_map):
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    messages = list(history) + [{"role": "user", "content": user}]
    ant_tools = _anthropic_tools(tools)
    in_tok = out_tok = 0

    # MAX_TOOL_ITERS rounds may call tools; the final extra round drops the tools
    # so the model is forced to synthesise a text answer instead of dead-ending.
    for i in range(MAX_TOOL_ITERS + 1):
        final_turn = i == MAX_TOOL_ITERS
        resp = await client.messages.create(
            model=spec.model_id, max_tokens=spec.max_output,
            system=system or anthropic.NOT_GIVEN, messages=messages,
            tools=anthropic.NOT_GIVEN if final_turn else ant_tools,
        )
        in_tok += resp.usage.input_tokens
        out_tok += resp.usage.output_tokens

        if resp.stop_reason == "tool_use" and not final_turn:
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    yield {"type": "tool_call", "tool": name_map.get(block.name, block.name), "args": block.input}
                    result = await _execute(block.name, block.input, name_map)
                    yield {"type": "tool_result", "tool": name_map.get(block.name, block.name), "ok": "[tool error" not in result}
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result[:6000]})
            messages.append({"role": "user", "content": tool_results})
            continue

        text = "".join(b.text for b in resp.content if b.type == "text")
        for chunk in _chunk_text(text):
            yield {"type": "token", "token": chunk, "done": False}
        yield {"type": "end", "done": True, "model_key": f"anthropic/{spec.model_id}",
               "model_label": spec.label, "provider": "anthropic",
               "cost_usd": spec.estimated_cost_usd(in_tok, out_tok),
               "input_tokens": in_tok, "output_tokens": out_tok}
        return

    yield {"type": "token", "token": "[stopped after max tool iterations]", "done": False}
    yield {"type": "end", "done": True, "model_label": spec.label, "provider": "anthropic", "cost_usd": spec.estimated_cost_usd(in_tok, out_tok)}


def _chunk_text(text: str, size: int = 24):
    """Yield text in small chunks so the UI renders it progressively."""
    for i in range(0, len(text), size):
        yield text[i:i + size]
