import importlib.util
import os
import time
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("backstop_core", Path(__file__).resolve().parent.parent / "backstop_core.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("const a = 1\nconst b: number = 'x'\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    return tmp_path


def rec(proj, **kw):
    base = {"v": 1, "id": "abc", "ts": time.time(), "cwd": str(proj), "command": "npm run build",
            "exit": 2, "output": "src/app.ts(2,7): error TS2322: Type 'string' is not assignable", "shell": "pwsh"}
    base.update(kw)
    return base


@pytest.mark.parametrize("code,ok", [(0, False), (130, False), (-1073741510, False), (3221225786, False),
                                     (1, True), (2, True), (-1, True)])
def test_exit_semantics(proj, code, ok):
    assert core.classify(rec(proj, exit=code))[0] is ok


def test_empty_output_and_bad_cwd_skipped(proj):
    assert core.classify(rec(proj, output="  "))[1] == "no output captured"
    assert core.classify(rec(proj, cwd="relative/path"))[1] == "cwd missing"


def test_signature_ignores_timings_and_ansi(proj):
    a = rec(proj, output="\x1b[31mFAIL\x1b[0m test.js in 1.23s at 10:42:01")
    b = rec(proj, output="FAIL test.js in 9.80s at 11:02:59")
    assert core.signature(a) == core.signature(b)
    assert core.signature(a) != core.signature(rec(proj, output="FAIL other.js in 1s"))


def test_referenced_files_inside_project_only(proj):
    out = "src/app.ts(2,7): error\nsrc\\app.ts:2:3 again\n/etc/passwd:1\nC:\\Windows\\win.ini:3\nmissing.ts:4"
    refs = core.referenced_files(out, str(proj))
    assert refs == [{"path": "src/app.ts", "line": 2}]


def test_python_traceback_reference(proj):
    (proj / "m.py").write_text("x\n", encoding="utf-8")
    refs = core.referenced_files('Traceback:\n  File "m.py", line 1, in <module>', str(proj))
    assert {"path": "m.py", "line": 1} in refs


def test_fingerprint_detects_edit_and_skips_deps(proj):
    before = core.fingerprint(str(proj))
    assert "src/app.ts" in before and not any(k.startswith("node_modules") for k in before)
    (proj / "node_modules" / "dep.js").write_text("changed", encoding="utf-8")
    assert core.changed_files(before, core.fingerprint(str(proj))) == []
    time.sleep(0.02)
    (proj / "src" / "app.ts").write_text("const b: number = 1\n", encoding="utf-8")
    assert core.changed_files(before, core.fingerprint(str(proj))) == ["src/app.ts"]


def test_tools_are_confined_and_read_only(proj):
    r = core.tool_read_file(str(proj), "src/app.ts", 2, 2)
    assert "2| const b: number = 'x'" in r["content"]
    with pytest.raises(ValueError):
        core.tool_read_file(str(proj), "../outside.txt")
    with pytest.raises(ValueError):
        core.tool_list(str(proj), os.path.abspath(os.sep))
    hits = core.tool_search(str(proj), r"number")["matches"]
    assert hits and all(not h.startswith("node_modules") for h in hits)


def test_prompt_redacts_secrets_and_keeps_tail(proj):
    big = "noise\n" * 10000 + "API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\nerror TS2322 at src/app.ts:2"
    p = core.build_prompt(rec(proj, output=big), [{"path": "src/app.ts", "line": 2}])
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in p
    assert "error TS2322" in p and "earlier output trimmed" in p
    assert len(p) < core.MAX_OUTPUT_CHARS + 3000


def test_spool_roundtrip(tmp_path, proj):
    import json
    f = tmp_path / "1.json"
    f.write_text(json.dumps(rec(proj, id="../../evil")), encoding="utf-8")
    r = core.read_spool_file(f)
    assert r["id"] == "evil"
    f.write_text(json.dumps(rec(proj, v=99)), encoding="utf-8")
    assert core.read_spool_file(f) is None


def test_prompt_includes_source_excerpt(proj):
    p = core.build_prompt(rec(proj), core.referenced_files(rec(proj)["output"], str(proj)))
    assert "2| const b: number = 'x'" in p and "without calling any tool" in p


def test_tool_guard_and_arg_filter(monkeypatch, proj):
    import importlib.util, json, sys, types
    spec = importlib.util.spec_from_file_location("bs_init", Path(core.__file__).with_name("__init__.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    handler = mod._wrap(core.tool_read_file)
    monkeypatch.setattr(mod, "_cwd", lambda: str(proj))
    monkeypatch.setattr(mod, "_is_backstop_session", lambda: False)
    assert "only work inside" in json.loads(handler({"path": "src/app.ts"}))["error"]
    monkeypatch.setattr(mod, "_is_backstop_session", lambda: True)
    out = json.loads(handler({"path": "src/app.ts", "reason": "model-added field"}))
    assert out["total_lines"] == 2
