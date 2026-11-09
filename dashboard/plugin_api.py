"""Backstop backend (runs inside the gateway process).

Watches the shell spool, turns each real failure into one pending item, pushes it to the Desktop
half (``plugin.backstop.failure.new``), and judges results: a diagnosis whose project changed while
it ran comes back ``stale`` with the files that moved.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)


def _load_core():
    # Fresh on every import of this backend: a plugin update/reload must not keep running old code.
    name = "backstop_plugin_core"
    path = Path(__file__).resolve().parent.parent / "backstop_core.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load_core()
router = APIRouter()

_LOCK = threading.RLock()
_ITEMS: dict[str, dict] = {}        # id -> public item
_BASELINES: dict[str, dict] = {}    # id -> fingerprint at failure time
_RECENT_SIGS: dict[str, float] = {}  # signature -> last seen
_WATCHER: threading.Thread | None = None
_STOP = threading.Event()
HISTORY_LIMIT = 50


def _settings() -> dict:
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
        cfg = load_config_readonly() or {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(core.PLUGIN_ID) or {}
        return dict(entry.get("settings") or {})
    except Exception:
        return {}


def _history_path() -> Path:
    return core.data_dir() / "history.jsonl"


def _log(item: dict) -> None:
    try:
        core.data_dir().mkdir(parents=True, exist_ok=True)
        with open(_history_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: item.get(k) for k in (
                "id", "status", "reason", "command", "exit", "cwd", "session_id", "stale_files", "tools",
                "last_event", "last_event_via", "seconds", "resolved", "minutes_to_fix",
                "created", "updated")}, ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("backstop: history write failed", exc_info=True)


def _emit(event: str, item: dict) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(core.PLUGIN_ID, event, _public(item))
    except Exception:
        logger.debug("backstop: broadcast failed", exc_info=True)


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in ("output", "prompt", "sig")}


def _set(item: dict, **changes: Any) -> dict:
    item.update(changes, updated=core.now())
    _log(item)
    return item


def ingest_once() -> list[dict]:
    """Process every spool file once. Returns the new pending items (also used by tests)."""
    new: list[dict] = []
    for path in core.iter_spool():
        record = core.read_spool_file(path)
        try:
            path.unlink()
        except OSError:
            continue  # still being written / locked: next tick
        if record is None:
            continue
        if record.get("kind") == "resolved":
            _resolve(record)
            continue
        ok, reason = core.classify(record)
        item = {
            "id": record["id"], "command": str(record.get("command") or "")[:300], "exit": record.get("exit"),
            "cwd": record.get("cwd"), "shell": record.get("shell"), "duration_ms": record.get("duration_ms"),
            "failed_at": record.get("ts"), "created": core.now(), "updated": core.now(),
            "status": "pending" if ok else "skipped", "reason": reason,
        }
        if ok:
            sig = core.signature(record)
            item["sig"] = sig
            with _LOCK:
                last = _RECENT_SIGS.get(sig)
                _RECENT_SIGS[sig] = core.now()
            if last is not None and core.now() - last < core.DEDUPE_WINDOW_S:
                item.update(status="skipped", reason="duplicate of a failure seen in the last 10 minutes")
            else:
                refs = core.referenced_files(str(record.get("output") or ""), record["cwd"])
                item["refs"] = refs
                item["output_tail"] = core.redact(core.tail(str(record.get("output") or ""), 1500))
                item["prompt"] = core.build_prompt(record, refs)
                with _LOCK:
                    _BASELINES[item["id"]] = core.fingerprint(record["cwd"])
        with _LOCK:
            _ITEMS[item["id"]] = item
            _trim()
        _log(item)
        if item["status"] == "pending":
            new.append(item)
            _emit("failure.new", item)
    _expire()
    return new


def _resolve(record: dict) -> None:
    """The same command later exited 0 in the same directory: close the incident."""
    key = (os.path.normcase(str(record.get("cwd") or "")), str(record.get("command") or ""))
    ts = record.get("ts") or core.now()
    with _LOCK:
        matching = [i for i in _ITEMS.values() if not i.get("resolved") and (
            i["id"] == record.get("resolves")
            or (os.path.normcase(str(i.get("cwd") or "")), i.get("command")) == key)]
        if not matching:
            return
        for item in matching:  # every open run of this command here is fixed now
            _RECENT_SIGS.pop(item.get("sig") or "", None)  # a recurrence is a new incident again
            _set(item, resolved=True, resolved_at=ts,
                 minutes_to_fix=round((ts - (item.get("failed_at") or ts)) / 60, 1))
        # announce the incident the user actually saw: the diagnosed one, else the newest
        target = max(matching, key=lambda i: (i["status"] != "skipped", i["created"]))
    _emit("failure.resolved", target)


def _trim() -> None:
    if len(_ITEMS) <= HISTORY_LIMIT:
        return
    for key in sorted(_ITEMS, key=lambda k: _ITEMS[k]["created"])[: len(_ITEMS) - HISTORY_LIMIT]:
        _ITEMS.pop(key, None)
        _BASELINES.pop(key, None)


def _expire() -> None:
    with _LOCK:
        for item in _ITEMS.values():
            if item["status"] == "pending" and core.now() - item["created"] > core.PENDING_TTL_S:
                _set(item, status="expired", reason="no Desktop window picked it up within 30 minutes")
            if item["status"] == "diagnosing" and core.now() - item["updated"] > 600:
                _set(item, status="failed", reason="no answer within 10 minutes")


def _watch() -> None:
    core.spool_dir().mkdir(parents=True, exist_ok=True)
    while not _STOP.wait(1.0):
        try:
            ingest_once()
        except Exception:
            logger.warning("backstop: ingest failed", exc_info=True)


def ensure_watcher() -> None:
    global _WATCHER
    with _LOCK:
        # One watcher per process: a re-imported backend stops the previous module's thread.
        holder = sys.modules.setdefault("backstop_plugin_watchers", type(sys)("backstop_plugin_watchers"))
        previous = getattr(holder, "stop", None)
        if previous is not None and previous is not _STOP:
            previous.set()
        holder.stop = _STOP
        if _WATCHER is None or not _WATCHER.is_alive():
            _WATCHER = threading.Thread(target=_watch, name="backstop-spool", daemon=True)
            _WATCHER.start()


def _get(item_id: str) -> dict:
    item = _ITEMS.get(item_id)
    if item is None:
        raise HTTPException(404, "unknown failure id")
    return item


# ── routes ──────────────────────────────────────────────────────────────────


@router.get("/status")
async def status():
    ensure_watcher()
    with _LOCK:
        items = sorted((_public(i) for i in _ITEMS.values()), key=lambda i: i["created"], reverse=True)
    return {"watching": True, "spool": str(core.spool_dir()), "items": items[:20],
            "model": _settings().get("diagnosis_model") or None}


@router.post("/claim")
async def claim(body: dict):
    """One Desktop window takes a pending failure; returns what session.create / prompt.submit need."""
    ensure_watcher()
    with _LOCK:
        item = _get(str(body.get("id")))
        if item["status"] != "pending":
            return {"ok": False, "status": item["status"]}
        _set(item, status="diagnosing", claimed_by=str(body.get("client") or "desktop"))
        settings = _settings()
        out = {
            "ok": True, "id": item["id"], "cwd": item["cwd"], "prompt": item["prompt"],
            "title": core.session_title(item | {"ts": item.get("failed_at")}),
            "toolsets": [core.TOOLSET], "model": settings.get("diagnosis_model") or None,
            "max_turns": int(settings.get("max_turns") or 8),
        }
    _emit("failure.claimed", item)
    return out


@router.post("/started")
async def started(body: dict):
    with _LOCK:
        item = _get(str(body.get("id")))
        _set(item, session_id=body.get("session_id"), stored_session_id=body.get("stored_session_id"),
             tools=body.get("tools") if isinstance(body.get("tools"), dict) else None)
    return {"ok": True}


@router.post("/event")
async def desktop_event(body: dict):
    """Desktop milestones (notified / opened) — recorded in history.jsonl as evidence of delivery."""
    with _LOCK:
        item = _get(str(body.get("id")))
        event = str(body.get("event") or "")[:32]
        _set(item, last_event=event, last_event_via=str(body.get("via") or "")[:32])
    return {"ok": True}


@router.post("/result")
async def result(body: dict):
    """Judge an answer: if any project file changed since the failure, the diagnosis is stale."""
    with _LOCK:
        item = _get(str(body.get("id")))
        baseline = _BASELINES.get(item["id"])
    after = core.fingerprint(item["cwd"]) if baseline is not None else None
    changed = core.changed_files(baseline, after) if baseline is not None else []
    text = str(body.get("text") or "")
    with _LOCK:
        if body.get("error"):
            _set(item, status="failed", reason=str(body["error"])[:300])
        else:
            _set(item, status="stale" if changed else "ready", stale_files=changed[:20],
                 answer=text[:6000], session_id=body.get("session_id") or item.get("session_id"),
                 stored_session_id=body.get("stored_session_id") or item.get("stored_session_id"),
                 seconds=round(core.now() - item["created"], 1))
    _emit("failure.done", item)
    return _public(item)


@router.post("/dismiss")
async def dismiss(body: dict):
    with _LOCK:
        item = _get(str(body.get("id")))
        _set(item, status="dismissed")
    return {"ok": True}
