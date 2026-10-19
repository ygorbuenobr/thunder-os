#!/usr/bin/env python3
"""Publish a bounded Mac Agenttrail summary to the Thunder OS VPS."""

import json
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_ferramentas"))
from thunder_os.agenttrail_observer import read_kitchen  # noqa: E402


REMOTE_WRITE = """
import os, pathlib, sys, tempfile
folder = pathlib.Path('/var/lib/thunder-os-agenttrail')
folder.mkdir(mode=0o700, exist_ok=True)
data = sys.stdin.buffer.read(16385)
if len(data) > 16384: raise SystemExit('snapshot too large')
fd, name = tempfile.mkstemp(prefix='.mac-', dir=folder)
try:
    with os.fdopen(fd, 'wb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(name, 0o600)
    os.replace(name, folder / 'mac-summary.json')
finally:
    if os.path.exists(name): os.unlink(name)
"""


def main() -> None:
    summary = read_kitchen("mac")
    payload = json.dumps(summary, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(payload) > 16_384:
        raise ValueError("Agenttrail summary exceeds limit")
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "minha-vps",
               "python3 -c " + shlex.quote(REMOTE_WRITE)]
    subprocess.run(command, input=payload, check=True, timeout=12,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
