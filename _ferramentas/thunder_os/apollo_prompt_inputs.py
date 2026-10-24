"""Pure S27 Apollo semantic-input fingerprint; no approval or effect authority.

prompt_inputs accepts the *whole* ApolloSequenceReadonly.sequence_detail result,
not its inner `snapshot` alone. It requires status=ok, complete=True, a verified
Apollo team/owner/sequence and verified recipient override coverage. AI, dynamic
variables, signatures and multiple variants are valid inputs; static_safe is
deliberately irrelevant. This does not verify freshness, recipients' eligibility,
approval, generated copy, provider permissions or authorization to send.

The hash is SHA-256 of UTF-8 canonical JSON (sorted object keys, compact separators,
ensure_ascii=False, no NaN). Strings retain exact Unicode, whitespace and line
endings; lists retain order. Known semantic step/variant fields, raw prompts and
variables, signatures, attachments and recipient bindings/overrides are included.
Provider statistics and collection metadata are excluded, including in prompt
metadata; keys *inside variable values* remain literal, even if named `stats`.
The reader's broader snapshot_hash is not reused as a semantic fingerprint.

Returns {status: verified|unverified, reason, input_hash: str|None, steps: list}.
Verified results also expose scope/attachments for inspection. Invalid inputs
return no partial steps or hash. Limits: 1 MB canonical input, 100 steps, 100
variants per step, 20,000 JSON nodes and depth 32. All work is synchronous CPU
work; no I/O, retries, model calls or persisted state.
"""
import hashlib
import json
import math
import re


_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_MAX_BYTES = 1_000_000
_VOLATILE = frozenset({'stats', 'statistics', 'provider_stats', 'collected_at',
    'collected_timestamp', 'collected_timestamps', 'fetched_at', 'retrieved_at',
    'request_count'})
_VARIABLES = frozenset({'raw_variables', 'variables', 'variable_values'})
_COMMON = ('team_id', 'account_id', 'owner_id', 'sequence_id', 'emailer_campaign_id',
    'prompt_template', 'prompt_template_id', 'prompt', 'system_instructions',
    'instructions', 'generation_options', 'generation_tone', 'tone', 'fallback',
    'raw_variables', 'variables', 'variable_values', 'signature_policy', 'signature',
    'attachments', 'attachment_ids')
_STEP = ('id', 'order', 'channel', 'subject', 'content', 'interval_hours', *_COMMON)
_VARIANT = ('touch_id', 'template_id', 'subject', 'content', 'body_text', 'body_html',
    'status', 'template_type', 'include_signature', 'has_ai_variables', *_COMMON)


class _Invalid(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise _Invalid(reason)


def _ident(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _semantic(value, budget, scope, *, depth=0, literal=False):
    """Copy only JSON values, validating scope and bounding hostile structures."""
    budget[0] -= 1
    _require(budget[0] >= 0 and depth <= 32, 'input_limit')
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        _require(math.isfinite(value), 'invalid_number')
        return value
    if isinstance(value, str):
        _require(len(value) <= _MAX_BYTES and not any(
            0xD800 <= ord(c) <= 0xDFFF or ord(c) == 127
            or ord(c) < 32 and c not in '\n\r\t' for c in value), 'invalid_text')
        budget[1] -= len(value.encode('utf-8'))
        _require(budget[1] >= 0, 'input_limit')
        return value
    if isinstance(value, list):
        _require(len(value) <= 2000, 'input_limit')
        return [_semantic(item, budget, scope, depth=depth + 1, literal=literal)
                for item in value]
    _require(isinstance(value, dict) and len(value) <= 2000, 'invalid_json_input')
    result = {}
    for key, item in value.items():
        _require(isinstance(key, str), 'invalid_json_input')
        _semantic(key, budget, scope, depth=depth + 1, literal=True)
        if not literal and key in _VOLATILE:
            continue
        if not literal:
            expected = {'team_id': scope['account_id'], 'account_id': scope['account_id'],
                'sequence_id': scope['sequence_id'], 'emailer_campaign_id': scope['sequence_id'],
                'owner_id': scope['owner_id']}.get(key)
            if expected is not None:
                _require(item == expected, 'input_scope_mismatch')
        result[key] = _semantic(item, budget, scope, depth=depth + 1,
                                literal=literal or key in _VARIABLES)
    return result


def _steps(items, scope):
    _require(isinstance(items, list) and 1 <= len(items) <= 100, 'steps_unverified')
    result, positions, ids, touches = [], set(), set(), set()
    budget = [20_000, _MAX_BYTES]
    for step in items:
        _require(isinstance(step, dict) and _ident(step.get('id'))
                 and step['id'] not in ids, 'step_identity_unverified')
        ids.add(step['id'])
        order, interval = step.get('order'), step.get('interval_hours')
        _require(type(order) is int and 1 <= order <= len(items)
                 and order not in positions, 'step_order_unverified')
        positions.add(order)
        flags = step.get('flags')
        _require(isinstance(flags, dict) and flags.get('raw_step_type') in (
            'auto_email', 'manual_email') and step.get('channel') == 'email',
            'step_kind_unverified')
        _require(type(interval) in (int, float) and math.isfinite(interval)
                 and 0 <= interval <= 876000, 'interval_unverified')
        variants = step.get('variants')
        _require(isinstance(variants, list) and 1 <= len(variants) <= 100,
                 'variants_unverified')
        normalized = {key: step[key] for key in _STEP if key in step}
        normalized['raw_step_type'] = flags['raw_step_type']
        if 'raw_wait_mode' in flags:
            normalized['raw_wait_mode'] = flags['raw_wait_mode']
        normalized['variants'] = []
        for variant in variants:
            _require(isinstance(variant, dict) and _ident(variant.get('touch_id'))
                     and _ident(variant.get('template_id'))
                     and variant['touch_id'] not in touches, 'variant_identity_unverified')
            touches.add(variant['touch_id'])
            _require(variant.get('template_type') in ('email_template', 'prompt_template')
                     and type(variant.get('include_signature')) is bool
                     and type(variant.get('has_ai_variables')) is bool,
                     'variant_policy_unverified')
            _require(isinstance(variant.get('subject'), str)
                     and all(variant.get(key) is None or isinstance(variant[key], str)
                             for key in ('body_text', 'body_html'))
                     and any(isinstance(variant.get(key), str)
                             for key in ('body_text', 'body_html')), 'body_unverified')
            normalized['variants'].append({key: variant[key] for key in _VARIANT
                                            if key in variant})
        result.append(_semantic(normalized, budget, scope))
    return result


def prompt_inputs(snapshot):
    """Fingerprint complete, identity-bound reader inputs; never approve them."""
    try:
        _require(isinstance(snapshot, dict) and snapshot.get('status') == 'ok'
                 and snapshot.get('complete') is True, 'snapshot_incomplete')
        source = snapshot.get('source')
        _require(isinstance(source, dict) and source.get('provider') == 'apollo'
                 and source.get('identity_verified') is True, 'source_unverified')
        _require(('status' not in source or source['status'] == 'ok')
                 and ('complete' not in source or source['complete'] is True),
                 'source_incomplete')
        scope = {key: source.get(key) for key in ('account_id', 'owner_id', 'sequence_id')}
        _require(all(_ident(value) for value in scope.values()), 'scope_unverified')
        for key in ('team_id', 'observed_profile_account_id'):
            _require(key not in source or source[key] == scope['account_id'],
                     'source_scope_mismatch')
        _require(source.get('contact_overrides_verified') is True,
                 'contact_overrides_unverified')
        raw = snapshot.get('snapshot')
        _require(isinstance(raw, dict) and raw.get('provider') == 'apollo'
                 and all(raw.get(key) == value for key, value in scope.items()),
                 'snapshot_scope_mismatch')
        _require(raw.get('contact_overrides_verified') is True
                 and isinstance(raw.get('recipients'), list)
                 and isinstance(raw.get('recipient_overrides'), list),
                 'contact_overrides_unverified')
        _require(isinstance(raw.get('attachments'), list), 'attachments_unverified')
        steps = _steps(snapshot.get('items'), scope)
        _require(steps == _steps(raw.get('steps'), scope), 'snapshot_steps_mismatch')
        rest = _semantic({key: raw[key] for key in (
            'attachments', 'recipients', 'recipient_overrides')}, [20_000, _MAX_BYTES], scope)
        payload = {'schema_version': 'apollo_prompt_inputs_v1', 'scope': scope,
                   'steps': steps, **rest}
        encoded = _canonical(payload)
        _require(len(encoded) <= _MAX_BYTES, 'input_limit')
        return {'status': 'verified', 'reason': 'complete_prompt_inputs',
                'input_hash': hashlib.sha256(encoded).hexdigest(), 'steps': steps,
                'scope': scope, 'attachments': rest['attachments']}
    except _Invalid as error:
        reason = str(error)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        reason = 'invalid_input'
    return {'status': 'unverified', 'reason': reason, 'input_hash': None, 'steps': []}
