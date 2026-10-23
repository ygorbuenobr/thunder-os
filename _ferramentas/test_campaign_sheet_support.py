"""Synthetic Brain sources for tests exercising approval, never live metadata."""
import hashlib
from pathlib import Path
from thunder_os.campaign_sheet import FIELDS
from thunder_os.knowledge import KnowledgeStore


def make_sheet(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    raw = '\n'.join('## ' + key + '\nSynthetic ' + label + '\n' for key, label in FIELDS.items()).encode()
    (root/'campaign.md').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return KnowledgeStore(root), {'version': '1', 'fields': {
        key: {'path': 'campaign.md', 'sha256': digest, 'section': key} for key in FIELDS}}
