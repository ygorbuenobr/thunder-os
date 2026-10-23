"""Read-only campaign facts referenced by exact Brain path, section and hash."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import PurePosixPath
import re
import stat
from collections.abc import Mapping

from .approvals import ApprovalError
from .models import payload_hash

FIELDS = {'offer': 'Oferta', 'icp': 'ICP', 'proofs': 'Provas',
          'objective': 'Objetivo', 'restrictions': 'Restrições', 'cta': 'CTA',
          'language': 'Idioma', 'country': 'País'}
MAX_BYTES = 65536


def _read_source(store, relative):
    """Walk directory descriptors without following any Brain symlink."""
    descriptor = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=descriptor)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError('source size or type')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('source size')
        return raw, info.st_mtime
    finally:
        os.close(descriptor)


def validate_sheet(value):
    if (not isinstance(value, Mapping) or set(value) != {'version', 'fields'}
            or not isinstance(value['version'], str) or not value['version'].strip()
            or len(value['version']) > 100 or not isinstance(value['fields'], Mapping)
            or set(value['fields']) - set(FIELDS)):
        raise ApprovalError('campaign_sheet_invalid')
    fields = {}
    for key, ref in value['fields'].items():
        if isinstance(ref, list):
            if not 1 <= len(ref) <= 6 or any(not isinstance(source, Mapping) for source in ref):
                raise ApprovalError('campaign_sheet_reference_invalid')
            fields[key] = [validate_sheet({'version': value['version'], 'fields': {key: source}})
                           ['fields'][key] for source in ref]
            if any(isinstance(source, list) for source in fields[key]):
                raise ApprovalError('campaign_sheet_reference_invalid')
            continue
        if not isinstance(ref, Mapping) or set(ref) != {'path', 'sha256', 'section'}:
            raise ApprovalError('campaign_sheet_reference_invalid')
        path, digest, section = ref['path'], ref['sha256'], ref['section']
        if (not isinstance(path, str) or not path or len(path) > 500
                or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or PurePosixPath(path).is_absolute()
                or any(p in {'', '.', '..'} or p.startswith('.') for p in path.split('/'))
                or not path.endswith('.md')
                or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
                or not isinstance(section, str) or not section.strip() or len(section) > 160
                or '\n' in section or '\r' in section):
            raise ApprovalError('campaign_sheet_reference_invalid')
        fields[key] = dict(ref)
    return {'version': value['version'], 'fields': fields}


def _section(content, heading):
    lines = content.splitlines()
    headings = [(i, len(m[1]), m[2].strip().rstrip('#').strip())
                for i, line in enumerate(lines)
                if (m := re.match(r'^(#{1,6})[ \t]+(.+?)\s*$', line))]
    matches = [(i, level) for i, level, title in headings if title == heading]
    if len(matches) != 1:
        return None
    start, level = matches[0]
    end = next((i for i, depth, _ in headings if i > start and depth <= level), len(lines))
    return '\n'.join(lines[start + 1:end]).strip()


def sheet_view(campaign_id, value, store=None):
    sheet = validate_sheet(value) if value is not None else {'version': None, 'fields': {}}
    rows, cache = [], {}
    for key, label in FIELDS.items():
        ref = sheet['fields'].get(key)
        if isinstance(ref, list):
            sources = [sheet_view(campaign_id, {'version': sheet['version'],
                       'fields': {key: source}}, store)['fields'][list(FIELDS).index(key)]
                       for source in ref]
            row = dict(sources[0], sources=sources)
            failed = next((source for source in sources if source['status'] == 'hash_mismatch'),
                          next((source for source in sources if source['status'] != 'ok'), None))
            row['status'] = failed['status'] if failed else 'ok'
            row['content'] = '\n\n'.join(source['content'] for source in sources
                                         if source['content']) if not failed else None
            rows.append(row)
            continue
        row = {'key': key, 'label': label, 'status': 'missing', 'path': None,
               'expected_sha256': None, 'observed_sha256': None,
               'source_updated_at': None, 'content': None}
        if ref:
            row.update(path=ref['path'], expected_sha256=ref['sha256'])
            row['status'] = 'unavailable'
            if store is not None:
                try:
                    if ref['path'] not in cache:
                        cache[ref['path']] = _read_source(store, ref['path'])
                    raw, mtime = cache[ref['path']]
                    row['observed_sha256'] = hashlib.sha256(raw).hexdigest()
                    row['source_updated_at'] = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
                    if row['observed_sha256'] != ref['sha256']:
                        row['status'] = 'hash_mismatch'
                    else:
                        content = _section(raw.decode('utf-8'), ref['section'])
                        row['status'] = 'section_missing' if content is None else 'empty' if not content else 'ok'
                        row['content'] = content
                except FileNotFoundError:
                    row['status'] = 'missing_file'
                except (OSError, ValueError, UnicodeError):
                    row['status'] = 'unavailable'
        rows.append(row)
    missing = [row['key'] for row in rows if row['status'] != 'ok']
    return {'campaign_id': campaign_id, 'status': 'incomplete' if missing else 'complete',
            'version': sheet['version'], 'fingerprint': payload_hash(sheet),
            'missing_fields': missing, 'fields': rows}


def require_sheet(campaign_id, value, store=None):
    result = sheet_view(campaign_id, value, store)
    if result['status'] != 'complete':
        changed = any(row['status'] == 'hash_mismatch' for row in result['fields'])
        raise ApprovalError('campaign_sheet_changed' if changed else 'campaign_sheet_incomplete')
    return result
