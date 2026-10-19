"""Read-only, bounded projection of local Agenttrail Kitchen activity."""

from __future__ import annotations

import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen


KITCHEN_URLS = {
    "mac": "http://127.0.0.1:4780/api/state",
    "vps": "http://127.0.0.1:4791/api/state",
}
MAC_SNAPSHOT = Path("/var/lib/thunder-os-agenttrail/mac-summary.json")
MAX_BYTES = 1_000_000
MAX_AGE_SECONDS = 180
PROVIDERS = {"codex", "claude", "cursor"}
STATES = {"working", "complete", "waiting", "idle", "blocked", "unknown"}


def _label(value: object, limit: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def project_state(raw: object, host: str, observed_at: float | None = None) -> dict:
    if not isinstance(raw, dict) or raw.get("app") != "agenttrail-kitchen":
        raise ValueError("Agenttrail state unavailable")
    entries = raw.get("executors")
    if not isinstance(entries, list):
        raise ValueError("Agenttrail sessions unavailable")
    sessions = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("provider") not in PROVIDERS:
            continue
        state = entry.get("state")
        project = entry.get("project")
        sessions.append({
            "provider": entry["provider"],
            "state": state if state in STATES else "unknown",
            "project": _label(Path(project).name if isinstance(project, str) else "", 60),
            "last_event_at": entry.get("lastEventAt") if type(entry.get("lastEventAt")) in (int, float) else None,
        })
        if len(sessions) == 24:
            break
    return {
        "host": host,
        "status": "available",
        "observed_at": observed_at if observed_at is not None else time.time(),
        "sessions": sessions,
        "limited": raw.get("discoveryLimited") is True,
    }


def read_kitchen(host: str = "vps") -> dict:
    request = Request(KITCHEN_URLS[host], headers={"Accept": "application/json"})
    with urlopen(request, timeout=1) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Agenttrail state exceeds limit")
    return project_state(json.loads(data), host)


def read_mac_snapshot(path: Path = MAC_SNAPSHOT, now: float | None = None) -> dict:
    data = path.read_bytes()
    if len(data) > 16_384:
        raise ValueError("Mac snapshot exceeds limit")
    snapshot = json.loads(data)
    if not isinstance(snapshot, dict) or snapshot.get("host") != "mac" or snapshot.get("status") != "available":
        raise ValueError("Mac snapshot invalid")
    observed_at = snapshot.get("observed_at")
    current = now if now is not None else time.time()
    if type(observed_at) not in (float, int) or not 0 <= current - observed_at <= MAX_AGE_SECONDS:
        raise ValueError("Mac snapshot stale")
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list) or len(sessions) > 24:
        raise ValueError("Mac snapshot invalid")
    clean = []
    for entry in sessions:
        if not isinstance(entry, dict) or entry.get("provider") not in PROVIDERS:
            raise ValueError("Mac snapshot invalid")
        state = entry.get("state")
        clean.append({
            "provider": entry["provider"],
            "state": state if state in STATES else "unknown",
            "project": _label(Path(entry.get("project", "")).name, 60),
            "last_event_at": entry.get("last_event_at") if type(entry.get("last_event_at")) in (int, float) else None,
        })
    return {"host": "mac", "status": "available", "observed_at": observed_at,
            "sessions": clean, "limited": snapshot.get("limited") is True}


def panel_state() -> dict:
    hosts = {}
    for host, reader in (("vps", read_kitchen), ("mac", read_mac_snapshot)):
        try:
            hosts[host] = reader()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            hosts[host] = {"host": host, "status": "unavailable", "sessions": []}
    return {"hosts": hosts, "source": "Agenttrail Kitchen local metadata", "read_only": True}
