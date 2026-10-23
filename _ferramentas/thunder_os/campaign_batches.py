"""Campaign-owned batches and a pure projection; never rewrite task evidence."""
from copy import deepcopy
import re
from .approvals import ApprovalError
from .models import payload_hash


def validate_batches(value):
    if not isinstance(value, list) or len(value) > 500:
        raise ApprovalError('campaign_batches_invalid')
    result, ids, codes = [], set(), set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {'batch_id', 'source_codes'}
                or not isinstance(row['batch_id'], str)
                or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', row['batch_id'])
                or row['batch_id'] in ids or not isinstance(row['source_codes'], list)
                or not row['source_codes'] or len(row['source_codes']) > 50):
            raise ApprovalError('campaign_batches_invalid')
        ids.add(row['batch_id'])
        for code in row['source_codes']:
            if (not isinstance(code, str) or not code.strip() or len(code) > 200
                    or code in codes or any(not c.isprintable() for c in code)):
                raise ApprovalError('campaign_batches_invalid')
            codes.add(code)
        result.append(deepcopy(row))
    return result


def preview_batches(snapshot, mapping):
    """Only explicit exact code mappings from an operator-reviewed proposal."""
    from .cadence_evidence import pipe_title
    from .cadence_readonly import TITLE_PATTERN
    before = payload_hash(snapshot)
    result = []
    for target in mapping.values():
        if (not isinstance(target, dict) or set(target) != {'campaign_id', 'batch_id'}
                or any(not isinstance(v, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', v)
                       for v in target.values())):
            raise ApprovalError('campaign_batch_mapping_invalid')
    for group in snapshot.get('campaigns', []):
        code = group['id']
        target = mapping.get(code)
        bindings = []
        for task in group.get('tasks', []):
            title = task.get('title') or ''
            pipe = pipe_title(title)
            bracket = TITLE_PATTERN.fullmatch(title)
            source = pipe['batch'] if pipe else bracket[1] if bracket else code
            scoped = mapping.get(source)
            bindings.append({'task_id': task['id'], 'source_code': source,
                'campaign_id': scoped['campaign_id'] if scoped else code,
                'batch_id': scoped['batch_id'] if scoped else None})
        result.append({'source_code': code, 'campaign_id': target['campaign_id'] if target else code,
                       'batch_id': target['batch_id'] if target else None,
                       'status': 'proposed' if target else 'unchanged',
                       'tasks': deepcopy(group.get('tasks', [])),
                       'task_bindings': bindings,
                       'metrics': deepcopy(group.get('metrics', {})),
                       'evidence_sha256': payload_hash(group)})
    assert payload_hash(snapshot) == before
    return {'read_only': True, 'writes': 0, 'source_sha256': before, 'groups': result}
