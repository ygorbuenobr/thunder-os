#!/usr/bin/env python3
"""Run bounded acceptance tests without loading the external production ledger."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='thunder-campaign-test-') as directory:
    Path(directory, 'toques.py').write_text('class ErroBanco(Exception): pass\n')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=os.pathsep.join((str(root / '_ferramentas'), directory)))
    result = subprocess.run([sys.executable, '-m', 'unittest',
        '_ferramentas/test_thunder_os_campaign_confirmation.py',
        '_ferramentas/test_thunder_os_legacy_campaign_gate.py',
        '_ferramentas/test_thunder_os_approvals.py'], cwd=root, env=env)
    raise SystemExit(result.returncode)
