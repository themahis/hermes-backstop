"""Backstop core: spool records, exit semantics, dedupe, project fingerprints, prompts.

Pure standard library so the agent half, the dashboard backend and the tests share one copy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

PLUGIN_ID = "backstop"
TOOLSET = "backstop"
SCHEMA_VERSION = 1

MAX_OUTPUT_CHARS = 24_000      # what goes into the prompt (tail of the captured output)
MAX_RECORD_BYTES = 512_000     # a spool file larger than this is refused
DEDUPE_WINDOW_S = 600          # same failure (cwd + command + output) within 10 min -> skipped
PENDING_TTL_S = 1800           # an unclaimed failure older than 30 min expires
FINGERPRINT_FILE_CAP = 20_000

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", "out", "target", ".next", ".nuxt",
    ".turbo", ".cache", ".parcel-cache", "coverage", ".idea", ".vscode", ".gradle", "bin", "obj",
    ".hermes",
})
SKIP_SUFFIXES = (".tsbuildinfo", ".pyc", ".log")

# Exit codes that are not failures worth diagnosing.
#   130 / 0xC000013A (-1073741510): Ctrl-C on POSIX / Windows.  -1073741510 & 0xFFFFFFFF = 3221225786.
INTERRUPT_CODES = frozenset({130, -1073741510, 3221225786, 143, -2})


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env)
        if os.name == "nt":
            return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes"
        return Path.home() / ".hermes"


def data_dir() -> Path:
    return hermes_home() / "plugin-data" / PLUGIN_ID


def spool_dir() -> Path:
    return data_dir() / "spool"


# ── classification ──────────────────────────────────────────────────────────


def classify(record: dict) -> tuple[bool, str]:
    """(diagnose?, reason). Mirrors the shell side so a hand-written spool file is held to it too."""
    code = record.get("exit")
    if not isinstance(code, int) or isinstance(code, bool):
        return False, "no exit code"
    if code == 0:
        return False, "succeeded"
    if code in INTERRUPT_CODES:
        return False, "interrupted"
    if not str(record.get("output") or "").strip():
        return False, "no output captured"
    cwd = record.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return False, "cwd missing"
    unsafe = unsafe_project_dir(cwd)
    if unsafe:
        return False, unsafe
    return True, "failed"


def unsafe_project_dir(cwd: str) -> str | None:
    """Why ``cwd`` must not become a diagnosis root, or None.

    The diagnosis tools may read anything under the project directory, and the failing command's
    output (which an attacker can influence) steers what they read. So a failure run from the home
    directory, an ancestor of it, a filesystem root, anywhere under HERMES_HOME, or inside any
    dot-directory (~/.ssh, ~/.config, ...) is never diagnosed.
    """
    real = os.path.normcase(os.path.realpath(cwd))
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    hh = os.path.normcase(os.path.realpath(str(hermes_home())))
    if real == home or _inside(home, real):
        return "cwd is the home directory or contains it"
    if os.path.dirname(real) == real:
        return "cwd is a filesystem root"
    if real == hh or _inside(real, hh):
        return "cwd is inside HERMES_HOME"
    parts = [p for p in re.split(r"[\\/]+", os.path.realpath(cwd)) if p]
    if any(p.startswith(".") and p not in (".", "..") for p in parts):
        return "cwd is inside a dot-directory"
    return None


_VOLATILE = [
    (re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]"), ""),                       # ANSI escapes
    (re.compile(r"\b\d+(\.\d+)?\s?(ms|s|sec|seconds|m|min)\b"), "<t>"),   # durations
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\b"), "<clock>"),       # wall clock
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?\b"), "<date>"),        # timestamps
    (re.compile(r"0x[0-9a-fA-F]{6,}"), "<addr>"),                          # pointers
    (re.compile(r"\bpid[ =:]?\d+\b", re.I), "<pid>"),
]


def normalize_output(text: str) -> str:
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    for rx, sub in _VOLATILE:
        out = rx.sub(sub, out)
    return "\n".join(line.rstrip() for line in out.split("\n")).strip()


def signature(record: dict) -> str:
    """Identity of a failure for dedupe: same project, same command, same (normalized) output."""
    key = json.dumps([os.path.normcase(str(record.get("cwd") or "")), str(record.get("command") or ""),
                      int(record.get("exit") or 0), normalize_output(str(record.get("output") or ""))])
    return hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:20]


def strip_ansi(text: str) -> str:
    return _VOLATILE[0][0].sub("", text)


def tail(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = strip_ansi(text.replace("\r\n", "\n"))
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    nl = cut.find("\n")
    return "[... earlier output trimmed ...]\n" + (cut[nl + 1:] if 0 <= nl < 400 else cut)


def redact(text: str, file_read: bool = False) -> str:
    """Hermes' redaction (``agent.redact.redact_sensitive_text``, forced on: everything here goes to a
    model) when available; a conservative fallback otherwise. ``file_read`` for file content."""
    try:
        from agent.redact import redact_sensitive_text  # type: ignore
        return redact_sensitive_text(text, force=True, file_read=file_read)
    except Exception:
        pass
    text = re.sub(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b(\s*[:=]\s*)(\S+)",
                  r"\1\2[redacted]", text)
    text = re.sub(r"\b(sk|ghp|gho|xox[abp]|AKIA)[A-Za-z0-9_\-]{12,}\b", "[redacted]", text)
    return text


# ── file references ─────────────────────────────────────────────────────────

_REF_RX = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[\w.\-\\/]*[\w\-]\.[A-Za-z]{1,6})"
    r"(?:[:(](?P<line>\d{1,6})(?:[:,](?P<col>\d{1,5}))?\)?)"
)


def referenced_files(output: str, cwd: str, limit: int = 12) -> list[dict]:
    """Project files the output points at (``src/app.ts:12``, ``src\\app.ts(12,5)``, ``File "x.py", line 3``)."""
    found: list[dict] = []
    seen: set[str] = set()
    candidates: list[tuple[str, int | None]] = []
    for m in _REF_RX.finditer(output):
        candidates.append((m.group("path"), int(m.group("line")) if m.group("line") else None))
    for m in re.finditer(r'File "([^"]+)", line (\d+)', output):
        candidates.append((m.group(1), int(m.group(2))))
    root = os.path.realpath(cwd)
    for raw, line in candidates:
        p = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
        try:
            real = os.path.realpath(p)
        except (OSError, ValueError):
            continue
        if not _inside(real, root) or not os.path.isfile(real) or read_block_error(root, real):
            continue
        rel = os.path.relpath(real, root).replace("\\", "/")
        if rel in seen:
            continue
        seen.add(rel)
        found.append({"path": rel, "line": line})
        if len(found) >= limit:
            break
    return found


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:
        return False


# ── project fingerprint (stale detection) ───────────────────────────────────


def fingerprint(cwd: str, cap: int = FINGERPRINT_FILE_CAP) -> dict[str, list[int]]:
    """relpath -> [size, mtime_ns] for the project's source files (build/dependency dirs skipped)."""
    out: dict[str, list[int]] = {}
    root = os.path.realpath(cwd)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(SKIP_SUFFIXES) or name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out[os.path.relpath(full, root).replace("\\", "/")] = [st.st_size, st.st_mtime_ns]
            if len(out) >= cap:
                return out
    return out


def changed_files(before: dict[str, list[int]], after: dict[str, list[int]]) -> list[str]:
    changed = [p for p, meta in after.items() if before.get(p) != meta]
    removed = [p for p in before if p not in after]
    return sorted(changed + removed)


# ── prompt ──────────────────────────────────────────────────────────────────


def excerpts(cwd: str, refs: list[dict], files: int = 3, radius: int = 12, budget: int = 6000) -> str:
    """Numbered source around each referenced line, so most diagnoses need no tool round trip."""
    parts: list[str] = []
    used = 0
    for ref in [r for r in refs if r.get("line")][:files]:
        try:
            whole = tool_read_file(cwd, ref["path"], 1, 150)
            small = "content" in whole and whole["total_lines"] <= 150
            got = whole if small else tool_read_file(cwd, ref["path"], max(1, ref["line"] - radius), ref["line"] + radius)
        except (OSError, ValueError):
            continue
        if "content" not in got:
            continue
        block = f"{ref['path']} (lines {got['start_line']}-{got['end_line']} of {got['total_lines']}):\n{got['content']}"
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def build_prompt(record: dict, refs: list[dict]) -> str:
    output = redact(tail(str(record.get("output") or "")))
    secs = (record.get("duration_ms") or 0) / 1000
    ref_lines = "\n".join(f"- {r['path']}" + (f" (line {r['line']})" if r.get("line") else "") for r in refs)
    source = excerpts(str(record.get("cwd") or ""), refs)
    files = sorted(fingerprint(str(record.get("cwd") or ""), cap=400))
    listing = ", ".join(files[:60]) + (f" (+{len(files) - 60} more)" if len(files) > 60 else "")
    return (
        "[Backstop] A command just failed in this project. Find the root cause.\n\n"
        "You can only read. Call tools directly through tool_call, no discovery needed:\n"
        '  tool_call {"name": "backstop_read_file", "arguments": {"path": "src/x.ts", "start_line": 1, "end_line": 80}}\n'
        '  tool_call {"name": "backstop_search", "arguments": {"pattern": "regex", "glob": "*.ts"}}\n'
        "You cannot edit files or run commands. The source below is usually enough: answer directly if "
        "it is, and make at most two tool calls if it is not. Never guess a file you have not seen.\n\n"
        f"Command: `{record.get('command')}`\n"
        f"Exit code: {record.get('exit')}  ·  Duration: {secs:.1f}s  ·  Shell: {record.get('shell', 'unknown')}\n"
        f"Working directory: {record.get('cwd')}\n"
        f"Project files: {listing}\n"
        + (f"Files the output points at:\n{ref_lines}\n" if ref_lines else "")
        + "\nCaptured output (tail; secrets redacted):\n```\n" + output + "\n```\n\n"
        + ("Source at the referenced lines (read just now):\n```\n" + source + "\n```\n"
           "If this is enough, answer without calling any tool.\n\n" if source else "")
        + "Answer in exactly this shape, short:\n"
        "**Cause:** one or two sentences.\n"
        "**Evidence:** the exact error line from the output, and the exact source line (path:line) you "
        "saw that proves it. Quote both verbatim.\n"
        "**Where:** path:line\n"
        "**Fix:** the smallest change, as a code snippet (or the one command to run).\n"
        "**Status:** `confirmed` only if you read the source line that causes the error; otherwise "
        "`hypothesis`, followed by what to check.\n"
    )


def session_title(record: dict) -> str:
    cmd = str(record.get("command") or "command")
    cmd = cmd if len(cmd) <= 48 else cmd[:47] + "…"
    stamp = time.strftime("%H:%M:%S", time.localtime(float(record.get("ts") or time.time())))
    return f"Backstop · {cmd} · exit {record.get('exit')} · {stamp} · {str(record.get('id', ''))[:6]}"


# ── spool io ────────────────────────────────────────────────────────────────


def read_spool_file(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != SCHEMA_VERSION:
        return None
    data["id"] = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("id") or path.stem))[:40] or path.stem
    return data


def iter_spool(directory: Path | None = None) -> Iterable[Path]:
    d = directory or spool_dir()
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob("*.json") if p.is_file()), key=lambda p: p.name)


# ── read-only tools (the session's only capabilities) ───────────────────────

MAX_READ_BYTES = 1_000_000
MAX_READ_LINES = 400


def read_block_error(root: str, target: str) -> str | None:
    """Why ``target`` (an absolute, resolved path under ``root``) must not be read, or None.

    Applies Hermes' own read denylist (``agent.file_safety.get_read_block_error``: credential stores,
    project ``.env`` files, internal caches) and, independently of it, refuses any dot-prefixed path
    segment below the project root (``.env*``, ``.ssh``, ``.git``, ``.aws``, ``.npmrc`` ...)."""
    rel = os.path.relpath(target, root)
    if rel != "." and any(seg.startswith(".") and seg not in (".", "..") for seg in re.split(r"[\\/]+", rel)):
        return f"Access denied: {rel.replace(os.sep, '/')} is a hidden (dot) path and cannot be read"
    try:
        from agent.file_safety import get_read_block_error  # type: ignore
    except Exception:
        return None
    try:
        return get_read_block_error(target)
    except Exception:
        return "Access denied: the read could not be safety-checked"


def _resolve_in(cwd: str, rel: str) -> str:
    if not cwd or not os.path.isdir(cwd):
        raise ValueError("this session has no project directory")
    unsafe = unsafe_project_dir(cwd)
    if unsafe:
        raise ValueError(f"this project directory cannot be read: {unsafe}")
    root = os.path.realpath(cwd)
    target = os.path.realpath(os.path.join(root, rel or "."))
    if not _inside(target, root):
        raise ValueError("path is outside the project directory")
    blocked = read_block_error(root, target)
    if blocked:
        raise ValueError(blocked)
    return target


def tool_read_file(cwd: str, path: str, start_line: int | None = None, end_line: int | None = None) -> dict:
    target = _resolve_in(cwd, path)
    if not os.path.isfile(target):
        return {"error": f"not a file: {path}"}
    if os.path.getsize(target) > MAX_READ_BYTES:
        return {"error": "file too large to read (over 1 MB)"}
    with open(target, "rb") as fh:
        raw = fh.read()
    if b"\x00" in raw[:4096]:
        return {"error": "binary file"}
    lines = raw.decode("utf-8", "replace").splitlines()
    start = max(1, int(start_line or 1))
    end = min(len(lines), int(end_line or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1)
    body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
    return {"path": path, "total_lines": len(lines), "start_line": start, "end_line": end, "content": redact(body, file_read=True)}


def tool_list(cwd: str, path: str = ".") -> dict:
    target = _resolve_in(cwd, path)
    if not os.path.isdir(target):
        return {"error": f"not a directory: {path}"}
    entries = []
    root = os.path.realpath(cwd)
    for name in sorted(os.listdir(target)):
        full = os.path.join(target, name)
        if read_block_error(root, os.path.realpath(full)):
            continue
        entries.append(name + ("/" if os.path.isdir(full) else ""))
        if len(entries) >= 300:
            break
    return {"path": path, "entries": entries}


def tool_search(cwd: str, pattern: str, glob: str | None = None, max_results: int = 50) -> dict:
    import fnmatch
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"error": f"bad regex: {e}"}
    root = _resolve_in(cwd, ".")
    hits: list[str] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
            if glob and not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(dirpath, name)
            if name.startswith(".") or read_block_error(root, os.path.realpath(full)):
                continue
            try:
                if os.path.getsize(full) > MAX_READ_BYTES:
                    continue
                with open(full, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue
            scanned += 1
            for i, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                    if len(hits) >= max_results:
                        return {"matches": [redact(h, file_read=True) for h in hits], "truncated": True}
            if scanned > 5000:
                return {"matches": [redact(h, file_read=True) for h in hits], "truncated": True}
    return {"matches": [redact(h, file_read=True) for h in hits], "truncated": False}


def now() -> float:
    return time.time()


def jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))
