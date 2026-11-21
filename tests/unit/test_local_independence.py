"""Local independence: the local model is configurable and can be the only one."""
import importlib

import pytest

_VARS = ("OLLAMA_MODEL", "OLLAMA_NUM_CTX", "OLLAMA_CAPABILITIES", "LLM_LOCAL_ONLY")
_DEFAULT_CAPS = {"classification", "retrieval", "summary", "fast"}


def _reload_registry():
    import orchestrator.llm_registry as reg
    return importlib.reload(reg)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    _reload_registry()


def test_default_local_model_is_unchanged():
    spec = _reload_registry().MODELS["ollama/llama3"]
    assert spec.model_id == "llama3"
    assert spec.label == "Llama 3 8B (local)"
    assert spec.context_window == 8192
    assert spec.capabilities == _DEFAULT_CAPS


def test_local_model_follows_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    monkeypatch.setenv("OLLAMA_CAPABILITIES", "summary, advisory, creative, not-a-tag")
    spec = _reload_registry().MODELS["ollama/llama3"]
    assert spec.model_id == "qwen3:8b"
    assert spec.label == "qwen3:8b (local)"
    assert spec.context_window == 16384
    assert spec.capabilities == {"summary", "advisory", "creative"}


def test_bad_env_values_fall_back(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "lots")
    monkeypatch.setenv("OLLAMA_CAPABILITIES", "nonsense")
    spec = _reload_registry().MODELS["ollama/llama3"]
    assert spec.context_window == 8192
    assert spec.capabilities == _DEFAULT_CAPS


def test_local_only_offers_no_cloud_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a-real-looking-key-000000")
    monkeypatch.setenv("LLM_LOCAL_ONLY", "1")
    reg = _reload_registry()
    assert {m.provider for m in reg.available_models().values()} == {"ollama"}
    assert set(reg.models_by_tier(3)) == {"ollama/llama3"}


def test_local_only_off_keeps_cloud(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-a-real-looking-key-000000")
    monkeypatch.setenv("LLM_LOCAL_ONLY", "0")
    reg = _reload_registry()
    assert "anthropic" in {m.provider for m in reg.available_models().values()}


def test_ollama_calls_request_a_context_window(monkeypatch):
    import orchestrator.multi_llm as ml
    monkeypatch.setenv("OLLAMA_NUM_CTX", "12000")
    assert ml._ollama_options() == {"num_ctx": 12000}
    monkeypatch.setenv("OLLAMA_NUM_CTX", "junk")
    assert ml._ollama_options() == {"num_ctx": 8192}
    monkeypatch.setenv("OLLAMA_TIMEOUT_S", "600")
    assert ml._ollama_timeout() == 600.0


# ── Reasoning off for local models ───────────────────────────────────────────

import httpx
import respx

_CHAT = "http://ollama.test/api/chat"


def test_think_setting_parses(monkeypatch):
    import orchestrator.multi_llm as ml
    monkeypatch.delenv("OLLAMA_THINK", raising=False)
    assert ml._ollama_think() is False
    monkeypatch.setenv("OLLAMA_THINK", "1")
    assert ml._ollama_think() is True
    monkeypatch.setenv("OLLAMA_THINK", "auto")
    assert ml._ollama_think() is None


def test_payload_carries_think_only_when_set():
    import orchestrator.multi_llm as ml
    assert ml._ollama_payload("m", [], False, False)["think"] is False
    assert "think" not in ml._ollama_payload("m", [], False, None)


def test_strip_reasoning():
    import orchestrator.multi_llm as ml
    assert ml._strip_reasoning("plain answer") == "plain answer"
    assert ml._strip_reasoning("<think>\nhmm\n</think>\n\nAnswer.") == "Answer."
    # reasoning-only models open the block in the template, so only the close shows
    assert ml._strip_reasoning("Okay, the user wants...\n</think>\nDear client") == "Dear client"
    assert ml._strip_reasoning("") == ""


@respx.mock
async def test_call_ollama_sends_think_false_and_strips(monkeypatch):
    import json as _json
    import orchestrator.multi_llm as ml
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test")
    monkeypatch.delenv("OLLAMA_THINK", raising=False)
    route = respx.post(_CHAT).mock(return_value=httpx.Response(200, json={
        "message": {"content": "reasoning here</think>\nThe answer."},
        "prompt_eval_count": 12, "eval_count": 5}))
    content, tin, tout = await ml._call_ollama("qwen3:4b", "sys", "hi", [])
    assert content == "The answer."
    assert (tin, tout) == (12, 5)
    assert _json.loads(route.calls[0].request.content)["think"] is False


@respx.mock
async def test_call_ollama_retries_without_think_when_refused(monkeypatch):
    import json as _json
    import orchestrator.multi_llm as ml
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test")
    monkeypatch.delenv("OLLAMA_THINK", raising=False)
    route = respx.post(_CHAT).mock(side_effect=[
        httpx.Response(400, json={"error": "invalid field: think"}),
        httpx.Response(200, json={"message": {"content": "ok"}}),
    ])
    content, _, _ = await ml._call_ollama("llama3", "", "hi", [])
    assert content == "ok"
    assert len(route.calls) == 2
    assert "think" not in _json.loads(route.calls[1].request.content)


def _ndjson(*chunks):
    import json as _json
    lines = [_json.dumps({"message": {"content": c}, "done": False}) for c in chunks]
    lines.append(_json.dumps({"message": {"content": ""}, "done": True}))
    return "\n".join(lines) + "\n"


@respx.mock
async def test_stream_ollama_holds_back_inline_reasoning(monkeypatch):
    import orchestrator.multi_llm as ml
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test")
    respx.post(_CHAT).mock(return_value=httpx.Response(
        200, text=_ndjson("<thi", "nk>\nlet me", " think</thi", "nk>\n\nHello", " there")))
    out = [c async for c in ml._stream_ollama("m", "", "hi", [])]
    assert "".join(out) == "Hello there"


@respx.mock
async def test_stream_ollama_passes_plain_text_through(monkeypatch):
    import orchestrator.multi_llm as ml
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test")
    respx.post(_CHAT).mock(return_value=httpx.Response(200, text=_ndjson("Hel", "lo", " world")))
    out = [c async for c in ml._stream_ollama("m", "", "hi", [])]
    assert "".join(out) == "Hello world"
    assert out[0] == "Hel"     # streaming starts at once, nothing buffered


@respx.mock
async def test_stream_ollama_retries_without_think_when_refused(monkeypatch):
    import json as _json
    import orchestrator.multi_llm as ml
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test")
    monkeypatch.delenv("OLLAMA_THINK", raising=False)
    route = respx.post(_CHAT).mock(side_effect=[
        httpx.Response(400, json={"error": "think not supported"}),
        httpx.Response(200, text=_ndjson("ok")),
    ])
    out = [c async for c in ml._stream_ollama("m", "", "hi", [])]
    assert "".join(out) == "ok"
    assert "think" not in _json.loads(route.calls[1].request.content)


def test_health_probes_the_ollama_address_the_models_use(monkeypatch):
    import importlib
    import common.health as health
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/")
    assert importlib.reload(health).ollama_check()["url"] == "http://127.0.0.1:11434/api/tags"
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.test:11434")
    assert importlib.reload(health).ollama_check()["url"] == "http://ollama.test:11434/api/tags"
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert importlib.reload(health).ollama_check()["url"] == "http://localhost:11434/api/tags"
