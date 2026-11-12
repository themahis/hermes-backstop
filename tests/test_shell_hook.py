"""bash / zsh hook end to end: fail -> spool record the backend accepts -> pass -> resolved record."""
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = (ROOT / "shell" / "backstop.sh").as_posix()

spec = importlib.util.spec_from_file_location("bs_core_shell", ROOT / "backstop_core.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def _shells():
    found = []
    for name in ("bash", "zsh"):
        exe = shutil.which(name)
        if exe and "system32" not in exe.lower():  # skip the WSL launcher on Windows
            found.append(pytest.param(exe, id=name))
    return found or [pytest.param(None, marks=pytest.mark.skip(reason="no bash/zsh"))]


FAKE = """#!/bin/sh
printf '\\033[31msrc/app.ts(1,5): error TS2322: "x" \\\\ not number\\033[0m\\n'
printf 'tab\\there\\n' >&2
exit "${FAKE_EXIT:-2}"
"""


def run(shell, home, proj, script):
    env = dict(os.environ, HERMES_HOME=str(home), BACKSTOP_COMMANDS="fakebuild")
    # a Windows "C:/..." entry would split PATH at its colon: convert it under Git Bash
    pre = (f'P="{proj.as_posix()}"; command -v cygpath >/dev/null && P="$(cygpath -u "$P")"; '
           f'export PATH="$P/bin:$PATH"; source "{HOOK}"; __backstop_tty() {{ return 0; }}; cd "{proj.as_posix()}"; ')
    args = [shell, "--norc", "-i", "-c"] if Path(shell).stem.lower() == "bash" else [shell, "-f", "-i", "-c"]
    return subprocess.run(args + [pre + script], env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("shell", _shells())
def test_hook_records_failure_then_resolution(shell, tmp_path):
    home, proj = tmp_path / "home", tmp_path / "proj"
    (proj / "bin").mkdir(parents=True)
    (proj / "bin" / "fakebuild").write_text(FAKE, encoding="utf-8", newline="\n")
    (proj / "bin" / "fakebuild").chmod(0o755)
    spool = home / "plugin-data" / "backstop" / "spool"

    r = run(shell, home, proj, 'fakebuild run build; echo "rc=$?"; FAKE_EXIT=130 fakebuild run build; __backstop_tty() { [ -t 1 ]; }; fakebuild run build | cat >/dev/null')
    assert "rc=2" in r.stdout and "sent to Hermes" in r.stderr, r.stderr
    files = list(spool.glob("*.json"))
    assert len(files) == 1, "Ctrl-C (130) and a piped run must not be recorded"
    rec = core.read_spool_file(files[0])
    assert rec and rec["exit"] == 2 and rec["command"] == "fakebuild run build"
    assert os.path.isdir(rec["cwd"]) and core.classify(rec)[0]
    assert 'error TS2322: "x" \\ not number' in rec["output"] and "\033" not in rec["output"]
    assert "tab\there" in rec["output"]
    if os.name != "nt":  # spool records carry command output: owner-only
        assert files[0].stat().st_mode & 0o077 == 0
    files[0].unlink()

    r = run(shell, home, proj, 'FAKE_EXIT=0 fakebuild run build; FAKE_EXIT=0 fakebuild run build')
    assert r.stderr.count("passes now") == 1, r.stderr  # closed once, not twice
    (done,) = spool.glob("*.json")
    res = json.loads(done.read_text(encoding="utf-8"))
    assert res["kind"] == "resolved" and res["resolves"] == rec["id"] and res["exit"] == 0
