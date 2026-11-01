"""Read-only, bounded systemd and release-link metadata for daily missions."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import selectors
import subprocess
import time

UNITS = (
    "thunder-os-connections-live.service",
    "thunder-os-connections-backfill.service",
    "painel-thunder.service",
    "thunder-os-agent.service",
)
RELEASE_LINKS = ("/opt/thunder-os/current", "/opt/thunder-os/agent-current")
TIMEOUT_SECONDS = 3
UNKNOWN = "unknown"
_STATES = frozenset(("active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance", "refreshing"))
_RESULTS = frozenset(("success", "resources", "protocol", "timeout", "exit-code", "signal", "core-dump", "watchdog", "start-limit-hit", "oom-kill", "exec-condition", "assert", "dependency", "skipped"))
_RELEASE = re.compile(r"[0-9a-f]{40}\Z")
RELEASE_ROOT = "/opt/thunder-os/releases"
SYSTEMCTL_MAX_BYTES = 16384


def _unit_signal(name: str) -> dict:
    signal = {"name": name, "state": UNKNOWN, "result": UNKNOWN}
    command = ["/usr/bin/systemctl", "show", name, "--no-pager",
               "--property=LoadState,ActiveState,Result"]
    try:
        output = _read_bounded(command, TIMEOUT_SECONDS, SYSTEMCTL_MAX_BYTES).decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return signal
    fields = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("LoadState", "ActiveState", "Result"):
            fields[key] = value
    if fields.get("LoadState") != "loaded":
        return signal
    for key, field, allowed in (("ActiveState", "state", _STATES), ("Result", "result", _RESULTS)):
        value = fields.get(key, "")
        if value in allowed:
            signal[field] = value
    return signal


def _release_signal(path: str) -> dict:
    signal = {"name": os.path.basename(path), "release": UNKNOWN}
    try:
        target = os.readlink(path)
    except OSError:
        return signal
    resolved = os.path.realpath(os.path.join(os.path.dirname(path), target))
    name = os.path.basename(resolved)
    if (os.path.dirname(resolved) == RELEASE_ROOT and _RELEASE.fullmatch(name)
            and os.path.isdir(resolved)):
        signal["release"] = name
    return signal


# Only literal values emitted by the connection runtime can leave this boundary.
JOURNAL_UNITS = UNITS[:2]
JOURNAL_MAX_ENTRIES = 500
JOURNAL_MAX_BYTES = 1024 * 1024
JOURNAL_TIMEOUT_SECONDS = 3
_ALLOWED = {
    "status": frozenset(("ok", "blocked")),
    "mode": frozenset(("live", "backfill")),
    "reason": frozenset(("duplicate_profile", "date_out_of_order")),
    "error_code": frozenset((
        "protected_file_unavailable", "protected_file_invalid",
        "connection_config_invalid", "connection_database_unavailable",
        "credential_unavailable", "read_only_transport_required",
        "connection_runtime_invalid", "provider_timeout", "provider_outcome_unknown",
        "provider_schema_invalid", "connection_order_invalid", "connection_page_invalid",
        "connection_row_invalid", "connection_window_incomplete",
        "connection_account_ambiguous", "backfill_window_incomplete",
        "connection_cursor_invalid", "connection_profile_invalid",
        "connection_date_invalid", "connection_window_invalid",
        "activation_date_invalid", "backfill_configuration_invalid",
        "backfill_identity_invalid", "backfill_anchor_missing",
        "provider_unauthorized", "provider_forbidden", "provider_rate_limited",
        "provider_unavailable", "account_catalog_unavailable",
        "account_configuration_invalid", "transport_configuration_invalid",
        "account_identity_mismatch", "response_too_large", "invalid_pagination",
        "pagination_unverified", "profile_identity_invalid", "profile_company_invalid",
        "company_name_invalid", "company_observation_invalid",
        "connection_transition_invalid", "connection_transition_origin_invalid",
        "connection_fit_evidence_invalid", "connection_request_conflict",
        "connection_observation_invalid", "connection_activation_changed",
        "connection_poll_stale", "connection_poll_conflict",
    )),
}
_COUNT_FIELDS = ("observed", "research", "jobs_ensured")


def _read_bounded(command: list[str], timeout: float, max_bytes: int) -> bytes:
    """Read at most max_bytes, enforcing a deadline and reaping the child."""
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(command, timeout)
                chunk = os.read(proc.stdout.fileno(), min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("volume_exceeded")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, timeout)
        if proc.wait(timeout=remaining) != 0:
            raise ValueError("unavailable")
        return bytes(data)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()


def _read_journal(name: str) -> bytes:
    command = ["/usr/bin/journalctl", "--unit", name, "--since=-24 hours",
               "--no-pager", "--output=json", "--output-fields=MESSAGE",
               "--lines=" + str(JOURNAL_MAX_ENTRIES + 1)]
    return _read_bounded(command, JOURNAL_TIMEOUT_SECONDS, JOURNAL_MAX_BYTES)


def _journal_signal(name: str) -> dict:
    signal = {"source": "journalctl", "unit": name, "window_hours": 24,
              "status": UNKNOWN, "coverage": "unknown", "events": None}
    try:
        data = _read_journal(name)
        lines = data.splitlines()
        if len(lines) > JOURNAL_MAX_ENTRIES + 1:
            raise ValueError("volume_exceeded")
        truncated = len(lines) > JOURNAL_MAX_ENTRIES
        events = []
        for line in lines[-JOURNAL_MAX_ENTRIES:]:
            try:
                entry = json.loads(line)
                message = entry.get("MESSAGE") if isinstance(entry, dict) else None
                payload = json.loads(message) if isinstance(message, str) else None
            except (ValueError, UnicodeError, RecursionError):
                continue
            if (not isinstance(payload, dict) or not isinstance(payload.get("status"), str)
                    or payload["status"] not in _ALLOWED["status"]):
                continue
            event = {}
            for field, allowed in _ALLOWED.items():
                value = payload.get(field)
                event[field] = value if isinstance(value, str) and value in allowed else UNKNOWN
            for field in _COUNT_FIELDS:
                value = payload.get(field)
                event[field] = value if type(value) is int and 0 <= value <= 1000000 else UNKNOWN
            events.append(event)
        signal.update(status="ok", coverage="partial", events=events,
                      entries_scanned=min(len(lines), JOURNAL_MAX_ENTRIES), truncated=truncated)
    except subprocess.TimeoutExpired:
        signal["error_code"] = "journal_timeout"
    except ValueError as exc:
        signal["error_code"] = ("journal_volume_exceeded" if str(exc) == "volume_exceeded"
                                else "journal_unavailable")
    except (OSError, UnicodeError):
        signal["error_code"] = "journal_unavailable"
    return signal


def collect_signals() -> dict:
    """Return metadata only; individual failures are represented as unknown."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "units": [_unit_signal(name) for name in UNITS],
        "releases": [_release_signal(path) for path in RELEASE_LINKS],
        "history": [_journal_signal(name) for name in JOURNAL_UNITS],
    }


def main() -> None:
    print(json.dumps(collect_signals(), ensure_ascii=True))


if __name__ == "__main__":
    main()
