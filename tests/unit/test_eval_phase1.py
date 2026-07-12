from __future__ import annotations

import importlib


def test_eval_store_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "evals.db"))
    import orchestrator.eval_store as store

    store = importlib.reload(store)
    run_id = store.create_run("routing", "offline")
    store.add_result(
        run_id,
        case_id="case-1",
        suite="routing",
        target="research_intelligence",
        passed=True,
        score=1.0,
        detail={"predicted": "research_intelligence"},
    )
    summary = store.finish_run(run_id)

    assert summary["total"] == 1
    assert summary["pass_rate"] == 1.0
    detail = store.get_run(run_id)
    assert detail is not None
    assert detail["results"][0]["case_id"] == "case-1"


async def test_offline_routing_eval_persists_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "evals.db"))
    import orchestrator.eval_store as store
    import orchestrator.eval_runner as runner

    importlib.reload(store)
    runner = importlib.reload(runner)

    result = await runner.run_eval("routing", live=False, limit=3)

    assert result["status"] == "complete"
    assert result["summary"]["total"] == 3
    assert 0.0 <= result["summary"]["pass_rate"] <= 1.0
    assert store.get_run(result["run_id"])["summary"]["total"] == 3


async def test_verify_routing_case_promotes_passing_case(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "evals.db"))
    import orchestrator.eval_store as store
    import orchestrator.eval_runner as runner

    importlib.reload(store)
    runner = importlib.reload(runner)
    store.update_case_state("routing", "ld-1", status="fixed", note="Adjusted learning design signals.")

    result = await runner.verify_case("routing", "ld-1")

    assert result["passed"] is True
    assert result["case_state"]["status"] == "verified"
    assert store.get_case_state("routing", "ld-1")["status"] == "verified"


def test_quality_overview_recommends_eval_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "evals.db"))
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "traces.jsonl"))
    import orchestrator.eval_store as store
    import orchestrator.trace as trace
    import orchestrator.quality as quality

    importlib.reload(store)
    importlib.reload(trace)
    quality = importlib.reload(quality)

    overview = quality.overview()

    assert overview["traces"]["total"] == 0
    assert any("routing eval" in r for r in overview["recommendations"])


def test_eval_case_state_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "evals.db"))
    import orchestrator.eval_store as store

    store = importlib.reload(store)
    run_id = store.create_run("routing", "offline")
    store.add_result(
        run_id,
        case_id="ri-1",
        suite="routing",
        target="research_intelligence",
        passed=False,
        score=0.0,
        issues=["expected research_intelligence, got hybrid"],
    )
    store.finish_run(run_id)

    before = store.get_run(run_id)["results"][0]
    assert before["case_status"] == "new"
    assert before["case_note"] == ""

    updated = store.update_case_state(
        "routing",
        "ri-1",
        status="triaged",
        note="Router needs stronger research signal weighting.",
    )
    assert updated["status"] == "triaged"

    after = store.get_run(run_id)["results"][0]
    assert after["case_status"] == "triaged"
    assert "stronger research" in after["case_note"]
    assert store.list_case_states(status="triaged")[0]["case_id"] == "ri-1"


def test_eval_work_queue_includes_fresh_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_DB_PATH", str(tmp_path / "evals.db"))
    import orchestrator.eval_store as store

    store = importlib.reload(store)
    run_id = store.create_run("routing", "offline")
    store.add_result(
        run_id,
        case_id="ri-1",
        suite="routing",
        target="research_intelligence",
        passed=False,
        score=0.0,
        issues=["expected research_intelligence, got rag"],
        detail={"prompt": "Benchmark a policy change"},
    )
    store.add_result(
        run_id,
        case_id="ld-1",
        suite="routing",
        target="learning_design",
        passed=True,
        score=1.0,
    )
    store.finish_run(run_id)

    queue = store.list_case_work_items()
    assert [q["case_id"] for q in queue] == ["ri-1"]
    assert queue[0]["status"] == "new"
    assert queue[0]["issues"] == ["expected research_intelligence, got rag"]

    store.update_case_state("routing", "ri-1", status="triaged")
    assert store.list_case_work_items(status="triaged")[0]["case_id"] == "ri-1"

    store.update_case_state("routing", "ri-1", status="verified")
    assert store.list_case_work_items() == []
    assert store.list_case_work_items(include_verified=True)[0]["status"] == "verified"
