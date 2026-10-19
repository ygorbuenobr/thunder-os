"""Provision a private Nginx-to-panel token without printing its value."""

from __future__ import annotations

import os
from pathlib import Path
import secrets


TOKEN = Path('/etc/thunder-os/agenttrail-ingress.token')
SNIPPET = Path('/etc/nginx/snippets/thunder-os-agenttrail-ingress.conf')


def main() -> None:
    if TOKEN.exists() or SNIPPET.exists():
        if not TOKEN.is_file() or not SNIPPET.is_file():
            raise RuntimeError('ingress files are incomplete')
        token = TOKEN.read_text(encoding='ascii').strip()
        if len(token) != 64 or not secrets.compare_digest(
            SNIPPET.read_text(encoding='ascii').strip(),
            f'proxy_set_header X-Thunder-Agenttrail-Ingress {token};',
        ):
            raise RuntimeError('ingress files do not match')
        return
    token = secrets.token_hex(32)
    descriptor = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='ascii') as output:
        output.write(token + '\n')
    descriptor = os.open(SNIPPET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='ascii') as output:
        output.write(f'proxy_set_header X-Thunder-Agenttrail-Ingress {token};\n')


if __name__ == '__main__':
    main()
