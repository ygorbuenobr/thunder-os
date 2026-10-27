"""Pure native cadence contracts; no services, providers or authorization stores."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone, date
from pathlib import PurePosixPath
import re
from collections.abc import Mapping
from zoneinfo import ZoneInfo

from .models import ContractError, payload_hash


CONTEXT_FIELDS = {'schema', 'route', 'source_campaign', 'source_ref', 'selection_hash', 'crm_binding', 'calendar'}
NATIVE_FIELDS = {'schema', 'action', 'sender_account', 'requires_connection',
                 'thread_parent_step_id', 'source_task_id', 'context_hash'}
STEP_FIELDS = {'id', 'recipient', 'channel', 'content', 'order', 'interval_hours', 'native'}


def _strict(value, fields, code):
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(code)


def _text(value, code, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(code)
    return value


def _hash(value):
    if not isinstance(value, str) or re.fullmatch('[a-f0-9]{64}', value) is None:
        raise ContractError('native_hash_invalid')


def _stamp(value):
    if not isinstance(value, str):
        raise ContractError('native_time_invalid')
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContractError('native_time_invalid') from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContractError('native_time_invalid')
    return result


def validate_source_ref(value):
    _strict(value, {'path', 'sha256'}, 'native_source_ref_invalid')
    path = _text(value['path'], 'native_source_ref_invalid', 2000)
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or '..' in parsed.parts or '\\' in path or str(parsed) != path:
        raise ContractError('native_source_ref_invalid')
    _hash(value['sha256'])
    return dict(value)


def validate_native_context(value):
    _strict(value, CONTEXT_FIELDS, 'native_context_invalid')
    if value['schema'] != 'mexico_native_v1' or value['route'] not in ('ygor', 'loa'):
        raise ContractError('native_context_invalid')
    _text(value['source_campaign'], 'native_source_campaign_invalid')
    validate_source_ref(value['source_ref'])
    _hash(value['selection_hash'])
    crm = value['crm_binding']
    _strict(crm, {'portal_id', 'company_id', 'contact_id', 'deal_id', 'task_ids'}, 'native_crm_invalid')
    for key in ('portal_id', 'company_id', 'contact_id', 'deal_id'):
        _text(crm[key], 'native_crm_invalid')
    if not isinstance(crm['task_ids'], Mapping) or not crm['task_ids']:
        raise ContractError('native_crm_invalid')
    for key, task in crm['task_ids'].items():
        _text(key, 'native_crm_invalid'); _text(task, 'native_crm_invalid')
    if len(set(crm['task_ids'].values())) != len(crm['task_ids']):
        raise ContractError('native_crm_invalid')
    calendar = value['calendar']
    _strict(calendar, {'timezone', 'start_hour', 'end_hour', 'expires_at', 'holidays'}, 'native_calendar_invalid')
    if (calendar['timezone'] != 'America/Mexico_City'
            or type(calendar['start_hour']) is not int or calendar['start_hour'] != 9
            or type(calendar['end_hour']) is not int or calendar['end_hour'] != 18):
        raise ContractError('native_calendar_invalid')
    cutoff = _stamp(calendar['expires_at']).astimezone(ZoneInfo(calendar['timezone']))
    if cutoff != datetime(2026, 10, 30, 18, tzinfo=ZoneInfo(calendar['timezone'])):
        raise ContractError('native_calendar_invalid')
    holidays = calendar['holidays']
    if not isinstance(holidays, list) or any(not isinstance(day, str) for day in holidays):
        raise ContractError('native_calendar_invalid')
    try:
        if any(date.fromisoformat(day).isoformat() != day for day in holidays) or len(set(holidays)) != len(holidays):
            raise ValueError()
    except ValueError as exc:
        raise ContractError('native_calendar_invalid') from exc
    return deepcopy(dict(value))


def validate_native_step(native, context=None):
    _strict(native, NATIVE_FIELDS, 'native_step_invalid')
    if (native['schema'] != 'mexico_native_step_v1' or native['action'] not in ('email', 'invite', 'dm')
            or type(native['requires_connection']) is not bool
            or native['requires_connection'] != (native['action'] == 'dm')):
        raise ContractError('native_step_invalid')
    _text(native['sender_account'], 'native_sender_invalid')
    _text(native['source_task_id'], 'native_task_invalid')
    parent = native['thread_parent_step_id']
    if parent is not None:
        _text(parent, 'native_thread_invalid')
        if native['action'] != 'email':
            raise ContractError('native_thread_invalid')
    _hash(native['context_hash'])
    if context is not None:
        root = validate_native_context(context)
        if native['context_hash'] != payload_hash(root):
            raise ContractError('native_context_changed')
        if native['source_task_id'] not in root['crm_binding']['task_ids'].values():
            raise ContractError('native_task_mismatch')
    return deepcopy(dict(native))


def native_step_hash(step):
    allowed = STEP_FIELDS | ({'subject'} if isinstance(step, Mapping) and step.get('channel') == 'email' else set())
    _strict(step, allowed, 'native_step_scope_invalid')
    native = validate_native_step(step['native'])
    _text(step['id'], 'native_step_id_invalid')
    _text(step['recipient'], 'native_recipient_invalid')
    if (type(step['order']) is not int or step['order'] < 1
            or type(step['interval_hours']) is not int or not 0 <= step['interval_hours'] <= 87600
            or step['channel'] != ('email' if native['action'] == 'email' else 'linkedin')):
        raise ContractError('native_step_scope_invalid')
    content = step['content']
    if not isinstance(content, str) or len(content) > 20000:
        raise ContractError('native_content_invalid')
    if native['action'] != 'invite' and not content.strip():
        raise ContractError('native_content_invalid')
    if step['channel'] == 'email':
        _text(step['subject'], 'native_subject_invalid', 998)
    # Do not call effect_payload_hash: its legacy _text strips the literal body.
    return payload_hash(dict(step))


def _scope(step):
    return {key: value for key, value in step.items() if key in STEP_FIELDS | {'subject'}}


def evaluate_native_schedule(cadence, step, effects, now):
    """Calendar/dependency decision only; a due result is never authorization."""
    def result(state, due=None, reason=None):
        return {'state': state, 'due_at': due.isoformat() if due else None, 'reason': reason}

    try:
        root = validate_native_context(cadence['native_context'])
        validate_native_step(step['native'], root)
        native_step_hash(_scope(step))
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ContractError('native_time_invalid')
        zone = ZoneInfo(root['calendar']['timezone'])
        current = now.astimezone(zone)
        expiry = _stamp(root['calendar']['expires_at'])
        if current >= expiry:
            return result('blocked', reason='native_expired')
        if step.get('dependency_status') in ('failed', 'uncertain', 'blocked'):
            return result('blocked', reason='native_dependency_blocked')
        stored = next((s for s in cadence['steps'] if s['id'] == step['id']), None)
        if stored is None or native_step_hash(_scope(stored)) != native_step_hash(_scope(step)):
            return result('blocked', reason='native_step_scope_changed')
        ordered = sorted(cadence['steps'], key=lambda s: s['order'])
        index = next(i for i, s in enumerate(ordered) if s['id'] == step['id'])
        # Only exact persisted effects with a receipt and matching literal scope
        # establish predecessor time; another person/cadence cannot establish D0.
        sent = {}
        for effect in effects:
            payload = effect.get('payload', {})
            if payload.get('cadence_id') != cadence['id']:
                continue
            candidate = payload.get('step', {})
            original = next((s for s in ordered if s['id'] == candidate.get('id')), None)
            if original is None:
                continue
            if native_step_hash(_scope(candidate)) != native_step_hash(_scope(original)):
                return result('blocked', reason='native_receipt_scope_changed')
            if effect.get('state') in ('uncertain', 'sending', 'reserved', 'needs_reconciliation'):
                return result('blocked', reason='native_effect_uncertain')
            if effect.get('state') == 'sent':
                if not effect.get('receipt_ref') or not effect.get('sent_at'):
                    return result('blocked', reason='native_receipt_missing')
                sent[candidate['id']] = _stamp(effect['sent_at']).astimezone(zone)
                if sent[candidate['id']] > current:
                    return result('blocked', reason='native_receipt_time_invalid')
        if step['id'] in sent:
            return result('blocked', reason='native_already_sent')
        if any(s['id'] not in sent for s in ordered[:index]):
            return result('blocked', reason='native_predecessor_not_confirmed')
        due = _stamp(step['due_at']).astimezone(zone)
        if index:
            previous = sent[ordered[index - 1]['id']]
            d0 = sent[ordered[0]['id']]
            exception = (root['route'] == 'ygor' and index == 1
                         and ordered[0]['native']['action'] == 'email'
                         and step['native']['action'] == 'invite'
                         and previous.date() == d0.date() == current.date())
            due = max(due, previous if exception else
                      datetime.combine(previous.date() + timedelta(days=1), datetime.min.time(), zone).replace(hour=9))
            if not exception and any(stamp.date() == current.date() for stamp in sent.values()):
                due = max(due, datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), zone).replace(hour=9))
        # Logical source campaign spans destination Registries (one sender per
        # channel). Exact portal/contact IDs identify the person across routes.
        for effect in effects:
            payload = effect.get('payload', {})
            other = payload.get('native_context', {})
            if (payload.get('cadence_id') != cadence['id']
                    and other.get('source_campaign') == root['source_campaign']
                    and other.get('crm_binding', {}).get('contact_id') == root['crm_binding']['contact_id']
                    and other.get('crm_binding', {}).get('portal_id') == root['crm_binding']['portal_id']):
                validate_native_context(other)
                # No absence of a receipt proves absence of an attempted effect.
                # Unknown reconciliation states retain the other route safely.
                if effect.get('state') in ('uncertain', 'sending', 'reserved', 'needs_reconciliation'):
                    return result('blocked', reason='native_effect_uncertain')
                if effect.get('state') == 'sent':
                    if not effect.get('receipt_ref') or not effect.get('sent_at'):
                        return result('blocked', reason='native_receipt_missing')
                    occupied = _stamp(effect['sent_at']).astimezone(zone)
                    due = max(due, datetime.combine(occupied.date() + timedelta(days=1), datetime.min.time(), zone).replace(hour=9))
        candidate = max(due, current)
        holidays = set(root['calendar']['holidays'])
        while True:
            if candidate.weekday() >= 5 or candidate.date().isoformat() in holidays or candidate.hour >= 18:
                candidate = (candidate + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            elif candidate.hour < 9:
                candidate = candidate.replace(hour=9, minute=0, second=0, microsecond=0)
            else:
                break
        if candidate >= expiry:
            return result('blocked', reason='native_expired')
        if candidate > current:
            return result('approved', candidate, 'native_calendar_wait')
        return result('due', candidate)
    except (ContractError, KeyError, TypeError, ValueError, AttributeError, OverflowError, StopIteration):
        return result('blocked', reason='native_schedule_invalid')
