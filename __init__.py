"""Backstop agent half: the read-only toolset a diagnosis session is created with.

The tools are confined to the calling session's recorded project directory. They never write,
never execute, and never leave the machine on their own; what they return goes to whichever
model the diagnosis session uses.
"""
from __future__ import annotations

import json
import logging

try:
    from . import backstop_core as core
except ImportError:  # imported outside a package (pytest collection)
    import importlib.util as _ilu
    from pathlib import Path as _P
    _spec = _ilu.spec_from_file_location("backstop_core", _P(__file__).with_name("backstop_core.py"))
    core = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(core)

logger = logging.getLogger(__name__)


def _cwd() -> str:
    from hermes_cli.plugin_session import session_cwd
    return session_cwd()


def _is_backstop_session() -> bool:
    """True only inside a session created with exactly ``toolsets: ["backstop"]`` (its frozen policy).
    Plugin toolsets are on by default everywhere, so the tools refuse to act in any other chat."""
    try:
        from gateway.session_policy import active_policy
        policy = active_policy()
    except Exception:
        return False
    return policy is not None and tuple(getattr(policy, "toolsets", ()) or ()) == (core.TOOLSET,)


def _wrap(fn):
    import inspect
    accepted = set(inspect.signature(fn).parameters) - {"cwd"}

    def handler(args: dict, **_kw) -> str:
        # Models add fields like "reason"; pass only what the tool takes.
        args = {k: v for k, v in (args or {}).items() if k in accepted}
        if not _is_backstop_session():
            return json.dumps({"error": "backstop tools only work inside a Backstop diagnosis session"})
        try:
            return json.dumps(fn(_cwd(), **(args or {})), ensure_ascii=False)
        except TypeError as e:
            return json.dumps({"error": f"bad arguments: {e}"})
        except ValueError as e:
            return json.dumps({"error": str(e)})
        except OSError as e:
            return json.dumps({"error": f"cannot read: {e.strerror or e}"})
    return handler


_TOOLS = [
    ("backstop_read_file", core.tool_read_file, "📄", {
        "name": "backstop_read_file",
        "description": "Read a text file inside the project (read-only). Returns numbered lines; at most 400 per call.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the project root"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1}},
            "required": ["path"]},
    }),
    ("backstop_search", core.tool_search, "🔎", {
        "name": "backstop_search",
        "description": "Regex search across the project's text files (read-only). Skips dependency and build dirs.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Python regular expression"},
            "glob": {"type": "string", "description": "Optional filename glob, e.g. '*.ts' or 'src/**'"}},
            "required": ["pattern"]},
    }),
    ("backstop_list", core.tool_list, "📁", {
        "name": "backstop_list",
        "description": "List a directory inside the project (read-only).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory relative to the project root; default '.'"}}},
    }),
]


def register(ctx) -> None:
    for name, fn, emoji, schema in _TOOLS:
        ctx.register_tool(name=name, toolset=core.TOOLSET, schema=schema, handler=_wrap(fn),
                          description=schema["description"], emoji=emoji)
