"""Ravenwatch Ops dashboard plugin backend.

Read-only operator telemetry mounted at /api/plugins/ravenwatch-ops/.
No secrets, no side effects, no auth bypass beyond the dashboard plugin route.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Query

router = APIRouter()

_CACHE: Dict[str, tuple[float, Any]] = {}
CACHE_TTL = 5.0
MAX_LINE_CHARS = 280
MAX_LOG_BYTES = 512_000

SASS_OK = [
    "Everything's behaving. Suspicious.",
    "Green across the board. I’ll allow it.",
    "The agent gremlins are quiet. For now.",
]
SASS_WARN = [
    "A few things need supervision.",
    "Mostly fine. I still have an eyebrow raised.",
    "Not on fire, but I smell smoke.",
]
SASS_CRIT = [
    "Something escalated. Professionally.",
    "Red lights are not decoration. Go look.",
    "The dashboard is yelling for a reason.",
]

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*)['\"]?[^\s'\"]+"),
    re.compile(r"sk-[A-Za-z0-9]{12,}"),
    re.compile(r"(?i)(x-api-key:\s*)[^\s]+"),
]

NOISE_PATTERNS = [
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"\b[0-9a-f]{16,}\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
    re.compile(r"\b\d+\.\d+\.\d+\.\d+\b"),
    re.compile(r"\b\d{3,}\b"),
    re.compile(r"/[^\s:]+(?:/[^\s:]+)+"),
]


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return Path.home() / ".hermes"


def _cache_get(key: str):
    item = _CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any):
    _CACHE[key] = (time.time(), value)
    return value


def _redact(text: str) -> str:
    if not text:
        return ""
    out = text.replace("\x00", "")
    for pat in SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + "[REDACTED]" if m.groups() else "[REDACTED]", out)
    if len(out) > MAX_LINE_CHARS:
        out = out[: MAX_LINE_CHARS - 1] + "…"
    return out


def _tail_lines(path: Path, max_bytes: int = MAX_LOG_BYTES) -> List[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, 2)
            data = f.read()
        return data.decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def _severity(line: str) -> Optional[str]:
    # Ignore normal Hermes meta-notifications that can contain scary words
    # inside quoted skill/memory diffs (e.g. "CRITICAL" in a skill document).
    if "Skill '" in line and "patched:" in line:
        return None
    if "Memory updated" in line or "Entry replaced" in line:
        return None
    upper = line.upper()
    # Multi-line Python stack traces produce generic "Traceback" / frame lines;
    # the actual logger ERROR line carries the incident. Counting every stack
    # frame floods the operator board with useless duplicates.
    if "CRITICAL" in upper or "FATAL" in upper:
        return "critical"
    if "ERROR" in upper or "EXCEPTION" in upper or "FAILED" in upper:
        return "error"
    if "WARNING" in upper or "WARN" in upper or "TIMEOUT" in upper:
        return "warning"
    return None


def _normalise_incident(line: str) -> str:
    text = _redact(line)
    # Strip common log prelude; keep the human-relevant part.
    text = re.sub(r"^\s*\d{4}-\d{2}-\d{2}[^|\]]*(?:\||\]|\s-\s)?\s*", "", text)
    text = re.sub(r"^\s*(ERROR|WARNING|WARN|CRITICAL|INFO)\s+", "", text, flags=re.I)
    for pat in NOISE_PATTERNS:
        text = pat.sub("‹var›", text)
    text = re.sub(r"\s+", " ", text).strip(" -:|[]")
    return text[:180] or "Unclassified runtime incident"


def _collect_incidents(limit: int = 8) -> List[Dict[str, Any]]:
    cached = _cache_get(f"incidents:{limit}")
    if cached is not None:
        return cached

    logs_dir = _home() / "logs"
    files = ["errors.log", "gateway.log", "agent.log"]
    groups: Dict[str, Dict[str, Any]] = {}
    order = 0
    for fname in files:
        for line in _tail_lines(logs_dir / fname):
            sev = _severity(line)
            if not sev:
                continue
            msg = _normalise_incident(line)
            key = f"{sev}:{msg.lower()}"
            order += 1
            item = groups.setdefault(key, {
                "severity": sev,
                "title": msg,
                "count": 0,
                "sources": set(),
                "last_seen_rank": order,
                "sample": _redact(line),
            })
            item["count"] += 1
            item["sources"].add(fname)
            item["last_seen_rank"] = order
            item["sample"] = _redact(line)

    severity_weight = {"critical": 3, "error": 2, "warning": 1}
    incidents = sorted(
        groups.values(),
        key=lambda x: (severity_weight.get(x["severity"], 0), x["count"], x["last_seen_rank"]),
        reverse=True,
    )[:limit]
    for item in incidents:
        item["sources"] = sorted(item["sources"])
        # Current dashboard Logs page does not deep-link to a specific line yet,
        # so the best actionable target is the Logs tab. Keeping href on each
        # incident lets the frontend make every attention item clickable today
        # and upgrade to line/file anchors later without UI changes.
        item["href"] = "/logs"
    return _cache_set(f"incidents:{limit}", incidents)


def _sessions(limit: int = 50) -> List[Dict[str, Any]]:
    cached = _cache_get(f"sessions:{limit}")
    if cached is not None:
        return cached
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        try:
            rows = db.list_sessions_rich(limit=limit, include_children=True)
            return _cache_set(f"sessions:{limit}", rows)
        finally:
            db.close()
    except Exception:
        return []


def _cron_jobs() -> List[Dict[str, Any]]:
    cached = _cache_get("cron")
    if cached is not None:
        return cached
    try:
        from cron.jobs import list_jobs
        jobs = list_jobs(include_disabled=True)
        if isinstance(jobs, dict) and "jobs" in jobs:
            jobs = jobs["jobs"]
        if not isinstance(jobs, list):
            jobs = []
        return _cache_set("cron", jobs)
    except Exception:
        return []


def _status_from_runtime_file() -> Dict[str, Any]:
    # Best-effort, read-only: dashboard's own /api/status is richer, but importing
    # web_server from a plugin would recurse. The runtime file gives us enough.
    candidates = [
        _home() / "gateway" / "runtime_status.json",
        _home() / "runtime_status.json",
        _home() / "gateway_status.json",
    ]
    for path in candidates:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def _health_summary() -> Dict[str, Any]:
    cached = _cache_get("summary")
    if cached is not None:
        return cached

    now = time.time()
    sessions = _sessions(80)
    incidents = _collect_incidents(8)
    jobs = _cron_jobs()
    runtime = _status_from_runtime_file()

    sessions_24h = [s for s in sessions if (now - float(s.get("started_at") or 0)) < 86400]
    active_sessions = [
        s for s in sessions
        if s.get("ended_at") is None and (now - float(s.get("last_active") or s.get("started_at") or 0)) < 300
    ]
    tool_calls_24h = sum(int(s.get("tool_call_count") or 0) for s in sessions_24h)
    messages_24h = sum(int(s.get("message_count") or 0) for s in sessions_24h)
    cost_24h = sum(float(s.get("estimated_cost_usd") or s.get("actual_cost_usd") or 0) for s in sessions_24h)

    error_count = sum(i["count"] for i in incidents if i["severity"] in ("critical", "error"))
    warning_count = sum(i["count"] for i in incidents if i["severity"] == "warning")
    critical_count = sum(i["count"] for i in incidents if i["severity"] == "critical")
    # Score by incident *classes*, not raw repeated log-line volume. A noisy
    # log loop should hurt, but it should not pin the console to zero forever.
    error_groups = sum(1 for i in incidents if i["severity"] == "error")
    warning_groups = sum(1 for i in incidents if i["severity"] == "warning")
    critical_groups = sum(1 for i in incidents if i["severity"] == "critical")
    volume_penalty = min(12, int(math.log10(max(error_count + warning_count, 1))) * 3)

    score = 100
    score -= min(42, critical_groups * 16)
    score -= min(32, error_groups * 8)
    score -= min(16, warning_groups * 3)
    score -= volume_penalty
    if runtime.get("gateway_state") in ("stopped", "startup_failed"):
        score -= 35
    score = max(0, min(100, score))

    if score >= 90:
        level, label, sass = "ok", "Operational", SASS_OK[int(now) % len(SASS_OK)]
    elif score >= 70:
        level, label, sass = "watch", "Watch", SASS_WARN[int(now) % len(SASS_WARN)]
    else:
        level, label, sass = "critical", "Attention", SASS_CRIT[int(now) % len(SASS_CRIT)]

    top_models = Counter((s.get("model") or "unknown") for s in sessions_24h)
    platforms = runtime.get("platforms") if isinstance(runtime.get("platforms"), dict) else {}

    summary = {
        "generated_at": now,
        "mode": "live",
        "health_score": score,
        "level": level,
        "label": label,
        "sass": sass,
        "gateway_state": runtime.get("gateway_state") or "unknown",
        "gateway_updated_at": runtime.get("updated_at"),
        "metrics": {
            "active_sessions": len(active_sessions),
            "sessions_24h": len(sessions_24h),
            "messages_24h": messages_24h,
            "tool_calls_24h": tool_calls_24h,
            "errors": error_count,
            "warnings": warning_count,
            "cron_jobs": len(jobs),
            "cron_enabled": sum(1 for j in jobs if j.get("enabled", True)),
            "estimated_cost_24h": round(cost_24h, 4),
            "platforms": len(platforms),
        },
        "top_models": [{"name": k, "count": v} for k, v in top_models.most_common(4)],
        "incidents": incidents,
        "recommendations": _recommendations(score, incidents, jobs),
    }
    return _cache_set("summary", summary)


def _recommendations(score: int, incidents: List[Dict[str, Any]], jobs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    recs = []
    if incidents:
        recs.append({"title": "Inspect Logs", "detail": "Clustered warnings/errors detected in recent logs.", "href": "/logs"})
    if any(i["severity"] in ("critical", "error") for i in incidents):
        recs.append({"title": "Triage top incident", "detail": incidents[0]["title"], "href": "/logs"})
    disabled = [j for j in jobs if not j.get("enabled", True)]
    if disabled:
        recs.append({"title": "Review paused jobs", "detail": f"{len(disabled)} cron job(s) disabled or paused.", "href": "/cron"})
    if score >= 90 and not recs:
        recs.append({"title": "Keep shipping", "detail": "Runtime looks healthy. Screenshot it before it learns shame.", "href": "/sessions"})
    return recs[:4]


def _timeline() -> List[Dict[str, Any]]:
    cached = _cache_get("timeline")
    if cached is not None:
        return cached
    items: List[Dict[str, Any]] = []
    for s in _sessions(12)[:8]:
        title = s.get("title") or s.get("preview") or "Hermes session"
        items.append({
            "kind": "session",
            "severity": "info",
            "timestamp": s.get("last_active") or s.get("started_at"),
            "title": title,
            "detail": f"{s.get('source') or 'session'} · {s.get('message_count') or 0} messages · {s.get('tool_call_count') or 0} tools",
            "href": f"/sessions/{s.get('id')}",
        })
    now = time.time()
    for inc in _collect_incidents(5):
        items.append({
            "kind": "incident",
            "severity": inc["severity"],
            "timestamp": now,
            "title": inc["title"],
            "detail": f"{inc['count']}× · {', '.join(inc['sources'])}",
            "href": "/logs",
        })
    items.sort(key=lambda x: float(x.get("timestamp") or 0), reverse=True)
    return _cache_set("timeline", items[:12])


def _briefing(data: Optional[Dict[str, Any]] = None) -> str:
    data = data or _health_summary()
    lines = [
        "Ravenwatch Operator Briefing",
        f"Status: {data['label']} ({data['health_score']}/100)",
        f"Amy says: {data['sass']}",
        "",
        "Metrics:",
    ]
    for key, value in data.get("metrics", {}).items():
        lines.append(f"- {key.replace('_', ' ')}: {value}")
    incidents = data.get("incidents") or []
    lines.append("")
    lines.append("Top incidents:" if incidents else "Top incidents: none detected")
    for inc in incidents[:5]:
        lines.append(f"- [{inc['severity']}] {inc['title']} ({inc['count']}×; {', '.join(inc['sources'])})")
    recs = data.get("recommendations") or []
    if recs:
        lines.append("")
        lines.append("Recommended actions:")
        for rec in recs:
            lines.append(f"- {rec['title']}: {rec['detail']}")
    return "\n".join(lines)


def _demo_summary() -> Dict[str, Any]:
    now = time.time()
    incidents = [
        {"severity": "error", "title": "Gateway platform reconnect loop detected", "count": 3, "sources": ["gateway.log"], "sample": "ERROR gateway.platforms: reconnect loop detected", "href": "/logs"},
        {"severity": "warning", "title": "Long-running agent crossed 10 minute operator-watch threshold", "count": 2, "sources": ["agent.log"], "sample": "WARNING agent: long running task still active", "href": "/logs"},
        {"severity": "warning", "title": "Cron job missed one scheduled tick", "count": 1, "sources": ["errors.log"], "sample": "WARNING cron: missed tick", "href": "/cron"},
    ]
    data = {
        "generated_at": now,
        "mode": "demo",
        "health_score": 76,
        "level": "watch",
        "label": "Watch",
        "sass": "A few things need supervision.",
        "gateway_state": "running",
        "gateway_updated_at": now,
        "metrics": {
            "active_sessions": 2,
            "sessions_24h": 18,
            "messages_24h": 742,
            "tool_calls_24h": 319,
            "errors": 3,
            "warnings": 3,
            "cron_jobs": 9,
            "cron_enabled": 8,
            "estimated_cost_24h": 4.2069,
            "platforms": 5,
        },
        "top_models": [
            {"name": "gpt-5.5", "count": 11},
            {"name": "claude-opus-4.6", "count": 4},
            {"name": "gemini-3.1-pro", "count": 3},
        ],
        "incidents": incidents,
        "recommendations": _recommendations(76, incidents, [{"enabled": False}]),
    }
    return data


@router.get("/summary")
async def summary(demo: bool = Query(False, description="Return polished fixture data for screenshots")):
    return _demo_summary() if demo else _health_summary()


@router.get("/incidents")
async def incidents(limit: int = Query(8, ge=1, le=20), demo: bool = False):
    return {"incidents": _demo_summary()["incidents"][:limit] if demo else _collect_incidents(limit)}


@router.get("/timeline")
async def timeline(demo: bool = False):
    if demo:
        now = time.time()
        return {"timeline": [
            {"kind": "incident", "severity": "error", "timestamp": now - 90, "title": "Gateway reconnect loop detected", "detail": "3× gateway.log", "href": "/logs"},
            {"kind": "session", "severity": "info", "timestamp": now - 240, "title": "Dashboard plugin hackathon build", "detail": "telegram · 96 messages · 54 tools", "href": "/sessions"},
            {"kind": "job", "severity": "warning", "timestamp": now - 720, "title": "MemPalace sync skipped duplicate batch", "detail": "cron · recovered", "href": "/cron"},
        ]}
    return {"timeline": _timeline()}


@router.get("/briefing")
async def briefing(demo: bool = False):
    data = _demo_summary() if demo else _health_summary()
    return {"briefing": _briefing(data), "generated_at": data["generated_at"], "mode": data["mode"]}


@router.get("/diagnostics")
async def diagnostics():
    home = _home()
    return {
        "ok": True,
        "hermes_home": str(home),
        "state_db_exists": (home / "state.db").exists(),
        "logs_dir_exists": (home / "logs").exists(),
        "cache_entries": len(_CACHE),
    }
