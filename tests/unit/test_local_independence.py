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
