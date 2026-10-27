"""Native preparation helpers. Readers are supplied only by trusted composition."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from collections.abc import Mapping

from .models import ContractError, payload_hash
from .native_contract import validate_native_context, validate_source_ref, native_step_hash
from .research import validate_artifact


DEFAULT_CALENDAR = {'timezone': 'America/Mexico_City', 'start_hour': 9, 'end_hour': 18,
                    'expires_at': '2026-10-30T18:00:00-06:00', 'holidays': []}
ACTION_FIELDS = {'key', 'step', 'channel', 'action', 'sender', 'account_id', 'recipient',
                 'subject', 'text', 'contact_id', 'task_id', 'editorial_status'}
ACTION_NAMES = {'EMAIL': 'email', 'CONNECTION_REQUEST': 'invite', 'MESSAGE': 'dm'}


def validate_readers(value):
    if value is None:
        return {}
    if (not isinstance(value, Mapping) or set(value) != {'source', 'research', 'crm'}
            or not all(callable(reader) for reader in value.values())):
        raise ContractError('native_readers_invalid')
    return dict(value)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ContractError('native_source_duplicate_key')
        value[key] = item
    return value


def read_inputs(readers, source_ref, selection, route, calendar, catalog):
    """Return validated data, never approval/consent imported from the package."""
    if not readers:
        raise ContractError('native_readers_unavailable')
    ref = validate_source_ref(source_ref)
    if (not isinstance(selection, list) or not selection
            or any(not isinstance(key, str) or not key.strip() for key in selection)
            or len(set(selection)) != len(selection)):
        raise ContractError('native_selection_invalid')
    if route not in ('ygor', 'loa'):
        raise ContractError('native_route_invalid')
    try:
        raw = readers['source'](deepcopy(ref))
        if not isinstance(raw, bytes) or len(raw) > 20_000_000:
            raise ContractError('native_source_invalid')
        if hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise ContractError('native_source_hash_changed')
        package = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ContractError('native_source_invalid')))
    except ContractError:
        raise
    except Exception as exc:
        raise ContractError('native_source_unavailable') from exc
    if not isinstance(package, Mapping):
        raise ContractError('native_package_invalid')
    approved_payload = package.get('approval_payload')
    claimed = package.get('approval', {}).get('package_sha256') if isinstance(package.get('approval'), Mapping) else None
    if not isinstance(approved_payload, Mapping) or payload_hash(approved_payload) != claimed:
        raise ContractError('native_package_hash_changed')
    projected = approved_payload.get('actions')
    actions = package.get('actions')
    if not isinstance(actions, list) or not isinstance(projected, list) or not actions:
        raise ContractError('native_package_actions_invalid')
    if any(not isinstance(action, Mapping) or not ACTION_FIELDS <= set(action)
           or any(not isinstance(action[key], str) for key in ACTION_FIELDS) for action in actions):
        raise ContractError('native_package_actions_invalid')
    projection = [{key: action[key] for key in ACTION_FIELDS} for action in actions]
    if projection != projected or len({action['key'] for action in actions}) != len(actions):
        raise ContractError('native_package_projection_changed')
    by_key = {action['key']: action for action in actions}
    if any(key not in by_key for key in selection):
        raise ContractError('native_selection_missing')
    selected = [deepcopy(by_key[key]) for key in selection]
    # A cadence belongs to exactly one person and source campaign, not a batch of
    # similarly named people. Conditions in prose never grant eligibility.
    if (any(not isinstance(action.get('person_id'), str) or not action['person_id']
            or not isinstance(action.get('campaign'), str) or not action['campaign'] for action in selected)
            or len({action.get('person_id') for action in selected}) != 1
            or not selected[0].get('person_id')
            or len({action.get('campaign') for action in selected}) != 1
            or any(action['editorial_status'] != 'PREPARED_FOR_EXACT_REVIEW' for action in selected)):
        raise ContractError('native_selection_held')
    people = package.get('people')
    if not isinstance(people, list):
        raise ContractError('native_person_missing')
    people = [p for p in people if isinstance(p, Mapping) and p.get('id') == selected[0]['person_id']]
    if len(people) != 1 or not isinstance(people[0].get('holds'), list) or people[0]['holds']:
        raise ContractError('native_person_held')
    if people[0].get('route') != route:
        raise ContractError('native_route_mismatch')
    try:
        dossier = readers['research'](deepcopy(ref), deepcopy(selection))
        crm = readers['crm'](deepcopy(selection))
    except Exception as exc:
        raise ContractError('native_binding_unavailable') from exc
    if (not isinstance(crm, Mapping)
            or set(crm) != {'portal_id', 'company_id', 'contact_id', 'deal_id', 'task_ids', 'identity_records'}
            or not isinstance(crm['identity_records'], list)):
        raise ContractError('native_crm_invalid')
    root = validate_native_context({'schema': 'mexico_native_v1', 'route': route, 'source_ref': ref,
        'source_campaign': selected[0]['campaign'],
        'selection_hash': payload_hash(selection),
        'crm_binding': {key: deepcopy(crm[key]) for key in crm if key != 'identity_records'},
        'calendar': deepcopy(calendar)})
    if set(root['crm_binding']['task_ids']) != set(selection):
        raise ContractError('native_task_mismatch')
    try:
        artifact = validate_artifact(dossier, set(catalog['capabilities']), set(catalog['proofs']))
    except (ValueError, TypeError, KeyError) as exc:
        raise ContractError('native_research_invalid') from exc
    domain = artifact.account['domain'].lower().strip()
    records = crm['identity_records']
    if (len(records) < 2 or any(not isinstance(record, Mapping)
            or str(record.get('domain', '')).lower().strip() != domain
            or str(record.get('hubspot_contact_id', '')) != crm['contact_id'] for record in records)):
        raise ContractError('native_identity_mismatch')
    # Match each selected destination to at least two corroborating identity
    # records. Grouping permits email and LinkedIn identities of the same CRM ID.
    identities = {}
    for record in records:
        key = record.get('person_key')
        if not isinstance(key, str) or not key:
            raise ContractError('native_identity_mismatch')
        identities.setdefault(key, []).append(dict(record))
    for action in selected:
        if (action['action'] not in ACTION_NAMES or action['channel'] != ('email' if action['action'] == 'EMAIL' else 'linkedin')
                or action['recipient'] not in identities or len(identities[action['recipient']]) < 2):
            raise ContractError('native_identity_mismatch')
        for field, expected in (('contact_id', crm['contact_id']), ('task_id', crm['task_ids'][action['key']])):
            if action.get(field) and action[field] != expected:
                raise ContractError('native_crm_mismatch')
        if action.get('deal_id') and action['deal_id'] != crm['deal_id']:
            raise ContractError('native_crm_mismatch')
    canonical_artifact = asdict(artifact)
    # submit_research binds the catalog source, regardless of the dossier label.
    canonical_artifact['knowledge_source'] = 'thunder/catalog.json'
    return {'root': root, 'dossier': deepcopy(dossier), 'artifact': canonical_artifact,
            'actions': selected, 'identities': identities, 'domain': domain}


def build_steps(inputs, binding, now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ContractError('native_time_invalid')
    root = inputs['root']
    if root['route'] == 'ygor':
        plan = [('E1', 'email'), ('L1', 'invite'), ('E2', 'email'), ('L2', 'dm'), ('E3', 'email')]
    else:
        plan = [('L1', 'invite'), ('L2', 'dm'), ('L3', 'dm'), ('L4', 'dm')]
    if len(inputs['actions']) > len(plan):
        raise ContractError('native_route_plan_invalid')
    steps = []
    email_parent = None
    for index, action in enumerate(inputs['actions']):
        native_action = ACTION_NAMES[action['action']]
        if (action['step'], native_action) != plan[index]:
            raise ContractError('native_route_plan_invalid')
        if binding['provider_accounts'].get(action['channel']) != action['account_id']:
            raise ContractError('native_sender_mismatch')
        step = {'id': action['key'], 'recipient': action['recipient'], 'channel': action['channel'],
                'content': action['text'], 'order': index + 1, 'interval_hours': 0,
                'native': {'schema': 'mexico_native_step_v1', 'action': native_action,
                    'sender_account': action['account_id'], 'requires_connection': native_action == 'dm',
                    'thread_parent_step_id': email_parent if native_action == 'email' else None,
                    'source_task_id': root['crm_binding']['task_ids'][action['key']],
                    'context_hash': payload_hash(root)}}
        if native_action == 'email':
            step['subject'] = action['subject']
            email_parent = step['id']
        native_step_hash(step)
        steps.append(step)
    return steps
