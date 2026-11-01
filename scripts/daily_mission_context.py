#!/usr/bin/env python3
"""Prepare a candidate and collect local signals; never execute a mission."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid arguments")


def _coverage(signals, schema):
    """Classify the documented collector fields, not optional event content."""
    if not isinstance(signals, dict):
        return "unknown"

    def entries(key, identity, expected):
        items = signals.get(key)
        if not isinstance(items, list) or len(items) != len(expected):
            return None
        if any(not isinstance(item, dict) for item in items):
            return None
        names = [item.get(identity) for item in items]
        if any(not isinstance(name, str) for name in names) or set(names) != set(expected):
            return None
        return items

    units = entries("units", "name", schema.UNITS)
    releases = entries("releases", "name", [Path(path).name for path in schema.RELEASE_LINKS])
    history = entries("history", "unit", schema.JOURNAL_UNITS)
    if units is None or releases is None or history is None:
        return "unknown"
    if any(not isinstance(item.get("state"), str) or item["state"] not in schema._STATES
           or not isinstance(item.get("result"), str) or item["result"] not in schema._RESULTS
           for item in units):
        return "unknown"
    if any(not isinstance(item.get("release"), str)
           or not schema._RELEASE.fullmatch(item["release"]) for item in releases):
        return "unknown"
    if any(item.get("status") != "ok" or item.get("coverage") not in ("complete", "partial")
           or type(item.get("truncated")) is not bool or not isinstance(item.get("events"), list)
           for item in history):
        return "unknown"
    if any(item["coverage"] == "partial" or item["truncated"] for item in history):
        return "partial"
    return "collected"


def main(argv=None):
    stage = "arguments"
    try:
        parser = ArgumentParser(add_help=False)
        for name in ("repo", "expected-commit", "candidate", "toques", "validated-toques"):
            parser.add_argument("--" + name, required=True)
        args = parser.parse_args(argv)
        stage = "preparation"
        # The invoked helpers own their checks; their output is not this JSON protocol.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            import daily_mission_prepare
            candidate = daily_mission_prepare.prepare(
                args.repo, args.expected_commit, args.candidate, args.toques, args.validated_toques
            )
            stage = "signals"
            import daily_mission_signals
            signals = daily_mission_signals.collect_signals()
        stage = "serialization"
        result = {
            "status": "ok",
            "state": _coverage(signals, daily_mission_signals),
            "sha": args.expected_commit.lower(),
            "candidate": str(candidate),
            "validated_dependency": str(Path(candidate).parent / Path(args.validated_toques).name),
            "signals": signals,
        }
        output = json.dumps(result, ensure_ascii=True, allow_nan=False)
    except Exception:
        output = json.dumps({"status": "error", "state": "error", "stage": stage})
        print(output)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
