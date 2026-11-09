"""Backend flow without a gateway: spool -> pending -> claim -> result (ready/stale) -> resolved."""
import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location("bs_api", ROOT / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.core, "hermes_home", lambda: tmp_path / "home")
    monkeypatch.setattr(mod, "_emit", lambda event, item: events.append((event, item["id"])))
    monkeypatch.setattr(mod, "_settings", lambda: {"diagnosis_model": "m1", "max_turns": 5})
    events = []
    mod.events = events
    return mod


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    (p / "src").mkdir(parents=True)
    (p / "src" / "app.ts").write_text("let a: number = 'x'\n", encoding="utf-8")
    return p


def spool(api, **rec):
    d = api.core.spool_dir()
    d.mkdir(parents=True, exist_ok=True)
    base = {"v": 1, "ts": time.time(), "shell": "pwsh", "command": "npm run build", "exit": 2,
            "output": "src/app.ts(1,5): error TS2322"}
    base.update(rec)
    (d / f"{base['id']}.json").write_text(json.dumps(base), encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


def test_full_flow_ready_then_resolved(api, proj):
    spool(api, id="f1", cwd=str(proj))
    new = api.ingest_once()
    assert [i["id"] for i in new] == ["f1"] and ("failure.new", "f1") in api.events
    job = run(api.claim({"id": "f1"}))
    assert job["toolsets"] == ["backstop"] and job["model"] == "m1" and job["max_turns"] == 5
    assert "src/app.ts" in job["prompt"] and "**Status:**" in job["prompt"]
    assert run(api.claim({"id": "f1"}))["ok"] is False  # a second window cannot take it
    res = run(api.result({"id": "f1", "session_id": "s", "stored_session_id": "s", "text": "**Cause:** x"}))
    assert res["status"] == "ready" and res["stale_files"] == []
    spool(api, id="ok1", kind="resolved", resolves="f1", cwd=str(proj), exit=0, output="")
    api.ingest_once()
    assert api._ITEMS["f1"]["resolved"] is True and ("failure.resolved", "f1") in api.events


def test_stale_when_project_changes(api, proj):
    spool(api, id="f2", cwd=str(proj))
    api.ingest_once()
    run(api.claim({"id": "f2"}))
    time.sleep(0.02)
    (proj / "src" / "app.ts").write_text("let a: number = 1\n", encoding="utf-8")
    res = run(api.result({"id": "f2", "text": "**Cause:** x"}))
    assert res["status"] == "stale" and res["stale_files"] == ["src/app.ts"]


def test_dedupe_and_recurrence_after_fix(api, proj):
    spool(api, id="a", cwd=str(proj))
    spool(api, id="b", cwd=str(proj))
    api.ingest_once()
    assert api._ITEMS["a"]["status"] == "pending" and api._ITEMS["b"]["status"] == "skipped"
    spool(api, id="ok", kind="resolved", cwd=str(proj), exit=0, output="")  # matched by cwd+command
    api.ingest_once()
    assert api._ITEMS["a"]["resolved"] is True
    spool(api, id="c", cwd=str(proj))  # it broke again: a new incident, not a duplicate
    api.ingest_once()
    assert api._ITEMS["c"]["status"] == "pending"


def test_skips_interrupt_and_garbage(api, proj):
    spool(api, id="i", cwd=str(proj), exit=130)
    d = api.core.spool_dir()
    (d / "junk.json").write_text("{not json", encoding="utf-8")
    assert api.ingest_once() == []
    assert api._ITEMS["i"]["status"] == "skipped" and not list(d.glob("*.json"))


def test_failed_answer_is_recorded(api, proj):
    spool(api, id="e", cwd=str(proj))
    api.ingest_once()
    run(api.claim({"id": "e"}))
    res = run(api.result({"id": "e", "error": "no answer within 5 minutes"}))
    assert res["status"] == "failed"
