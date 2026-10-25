"""R4: bounded observation of the pinned Instantly campaign's current sequence.

Only GET /api/v2/workspaces/current and GET /api/v2/campaigns/{pinned id}.
Wire: request(method, path, body, headers, timeout) -> (status, bytes).
The existing transport fixes api.instantly.ai, TLS, no redirects and byte bounds.
Two requests maximum, 15s socket timeout each, no retries, cache or persistence.
Injected transports must enforce the same constraints. This is not a wall deadline.

status/complete describe this read contract. Missing/malformed inputs and dynamic
content produce partial, never an approval. exact_copy names review of the stored
static source only: no recipient substitution, account signature or final rendered
email is verified. body/body_html retain originals; content is a text projection.
Step/variant IDs are campaign + array position (the official schema has no IDs).
SHA-256 binds inputs, including raw variables/options, not collection timestamps;
it is an integrity checksum, not a signature or authorization.

Official contract: https://developer.instantly.ai/api-reference/groups/campaign
and https://developer.instantly.ai/api-reference/schemas/campaign (API v2).
Zero credits is the capability budget, not a measured provider balance. No real
provider/credential verification is implied by local fake-transport tests.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from html import unescape
import json
import math
import re

from .external_email_import import ExternalEmailError, email, require
from .instantly_reader import (KEY_FILE, INSTANTLY_CAMPAIGN, INSTANTLY_WORKSPACE,
                              _Text, _https_request, private_text)
from .instantly_webhook import _uuid


WORKSPACE_PATH = '/api/v2/workspaces/current'
CAMPAIGN_PATH = '/api/v2/campaigns/' + INSTANTLY_CAMPAIGN
_PATHS = frozenset((WORKSPACE_PATH, CAMPAIGN_PATH))
_MAX_BYTES = 1_000_000
_STATUS = frozenset((-99, -2, -1, 0, 1, 2, 3, 4))
_DYNAMIC_KEY = re.compile(r'(^|_)(ai|sdr|spintax|spin|dynamic|prompt|generation|generated)(_|$)')
_OPTIONS = (
    'id', 'name', 'organization', 'owned_by', 'created_by', 'status', 'sequences',
    'campaign_schedule', 'email_list', 'email_tag_list', 'stop_on_reply',
    'stop_on_auto_reply', 'core_variables', 'custom_variables', 'text_only',
    'first_email_text_only', 'email_gap', 'random_wait_max', 'daily_limit',
    'daily_max_leads', 'prioritize_new_leads', 'auto_variant_select', 'match_lead_esp',
    'not_sending_status', 'stop_for_company', 'insert_unsubscribe_header',
    'allow_risky_contacts', 'disable_bounce_protect', 'limit_emails_per_company_override',
    'cc_list', 'bcc_list', 'ai_sdr_id', 'provider_routing_rules', 'is_evergreen')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'instantly_invalid_response')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ExternalEmailError('instantly_invalid_response')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _contains(value, secret):
    if isinstance(value, str):
        return secret in value
    if isinstance(value, list):
        return any(_contains(item, secret) for item in value)
    if isinstance(value, dict):
        return any(_contains(key, secret) or _contains(item, secret)
                   for key, item in value.items())
    return False


def _text(value):
    return isinstance(value, str) and not any(
        ord(c) < 32 and c not in '\r\n\t' or ord(c) == 127
        or 0xD800 <= ord(c) <= 0xDFFF for c in value)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _option_key(key):
    normalized = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', key).lower()
    return 'signature' in normalized or bool(_DYNAMIC_KEY.search(normalized))


def _accounts(value):
    if not isinstance(value, list):
        return False
    try:
        for item in value:
            email(item)
        return True
    except ExternalEmailError:
        return False


def _schedule(value):
    return (isinstance(value, dict) and isinstance(value.get('schedules'), list)
            and bool(value['schedules']) and all(isinstance(item, dict) and item
                                               for item in value['schedules']))


def _dynamic(value):
    """Conservative indicators, never a claim that unknown provider AI is off."""
    if isinstance(value, str):
        decoded = unescape(value)
        return bool(re.search(r'[{}]|\[\[|\]\]|<%|%>', decoded))
    if isinstance(value, list):
        return any(_dynamic(item) for item in value)
    if isinstance(value, dict):
        return any((_option_key(key) and item not in (None, False, '', {}, []))
                   or _dynamic(item) for key, item in value.items())
    return False


def _variant(raw, ident, inherited):
    reasons = list(inherited)
    data = raw if isinstance(raw, dict) else {}
    subject, body, disabled = data.get('subject'), data.get('body'), data.get('v_disabled')
    if not _text(subject) or not _text(body) or not body:
        reasons.append('variant_content_unknown')
    if subject == '':
        reasons.append('subject_inheritance_unverified')
    if type(disabled) is not bool:
        reasons.append('variant_disabled_unknown')
    if set(data) - {'subject', 'body', 'v_disabled'}:
        reasons.append('variant_options_unverified')
    if _dynamic(data):
        reasons.append('dynamic_content_unverified')
    # No transformations are represented as a final provider-rendered message.
    content = None
    if _text(body):
        parser = _Text()
        parser.feed(body)
        parser.close()
        content = ''.join(parser.parts)
        if re.search(r'<\s*(script|style|iframe|object)\b', body, re.I):
            reasons.append('html_rendering_unverified')
    return {'id': ident, 'subject': subject if _text(subject) else None,
        'body': body if _text(body) else None, 'body_html': body if _text(body) else None,
        'content': content, 'v_disabled': disabled if type(disabled) is bool else None,
        'approval_kind': 'dynamic_unverified' if reasons else 'exact_copy',
        'approval_granted': False, 'reasons': sorted(set(reasons))}


def _step(raw, position, inherited):
    reasons = list(inherited)
    data = raw if isinstance(raw, dict) else {}
    ident = INSTANTLY_CAMPAIGN + ':sequence:0:step:' + str(position)
    kind, delay, unit = data.get('type'), data.get('delay'), data.get('delay_unit')
    if kind != 'email':
        reasons.append('step_type_unverified')
    if set(data) - {'type', 'delay', 'delay_unit', 'pre_delay', 'pre_delay_unit', 'variants'}:
        reasons.append('step_options_unverified')
    if not _number(delay) or unit not in ('days', 'hours', 'minutes'):
        reasons.append('step_delay_unknown')
    if ('pre_delay' in data and not _number(data['pre_delay'])
            or 'pre_delay_unit' in data and data['pre_delay_unit'] not in ('days', 'hours', 'minutes')):
        reasons.append('step_pre_delay_unknown')
    if ('pre_delay' in data) != ('pre_delay_unit' in data):
        reasons.append('step_pre_delay_unknown')
    options = {key: value for key, value in data.items() if key != 'variants'}
    if _dynamic(options):
        reasons.append('dynamic_content_unverified')
    raw_variants = data.get('variants')
    if not isinstance(raw_variants, list) or not raw_variants:
        reasons.append('step_variants_unknown')
    variants = [_variant(value, ident + ':variant:' + str(i), reasons)
                for i, value in enumerate(raw_variants if isinstance(raw_variants, list) else [], 1)]
    for value in variants:
        reasons.extend(value['reasons'])
    first = variants[0] if variants else {}
    return {'id': ident, 'order': position, 'channel': kind if _text(kind) else None,
        'subject': first.get('subject'), 'content': first.get('content'),
        'body_html': first.get('body_html'), 'delay': delay if _number(delay) else None,
        'delay_unit': unit if _text(unit) else None,
        'pre_delay': data.get('pre_delay'), 'pre_delay_unit': data.get('pre_delay_unit'),
        'variants': variants, 'approval_kind': 'dynamic_unverified' if reasons else 'exact_copy',
        'approval_granted': False, 'reasons': sorted(set(reasons))}


class InstantlySequenceReadonly:
    def __init__(self, credential_loader=None, request=None):
        self.loader = credential_loader or (lambda: private_text(KEY_FILE))
        self.request = request or _https_request

    def _get(self, path, *, budget=None):
        require(path in _PATHS, 'instantly_endpoint_blocked')
        try:
            token = self.loader()
        except Exception:
            raise ExternalEmailError('credential_unavailable') from None
        require(isinstance(token, str) and 0 < len(token) <= 4096
                and all(33 <= ord(c) <= 126 for c in token), 'credential_unavailable')
        try:
            if budget is not None:
                budget['requests'] += 1
            status, raw = self.request('GET', path, None,
                {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, 15)
        except TimeoutError:
            raise ExternalEmailError('instantly_timeout') from None
        except Exception:
            raise ExternalEmailError('instantly_read_unavailable') from None
        require(status != 429, 'instantly_rate_limited')
        require(type(status) is int and status == 200, 'instantly_http_error')
        require(isinstance(raw, bytes) and len(raw) <= _MAX_BYTES, 'instantly_invalid_response')
        try:
            result = _json(raw)
            require(isinstance(result, dict) and not _contains(result, token), 'instantly_invalid_response')
            # Validate encodability before retaining originals or calculating hashes.
            _canonical(result)
            return result
        except Exception:
            raise ExternalEmailError('instantly_invalid_response') from None

    def snapshot(self):
        result = {'read_only': True, 'provider': 'instantly',
            'account_id': INSTANTLY_WORKSPACE, 'campaign_id': INSTANTLY_CAMPAIGN,
            'owner_id': None, 'status': 'unavailable', 'complete': False,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'input_hash': None,
            'inputs': None, 'steps': [], 'sending_accounts': None,
            'campaign_status': None, 'stop_on_reply': None, 'stop_on_auto_reply': None,
            'campaign_schedule': None, 'core_variables': None, 'custom_variables': None,
            'approval_granted': False,
            'capabilities': {'dynamic_approval': 'unknown', 'final_email': 'unknown',
                             'sending_account_signatures': 'unknown'},
            'source': {'api_version': 'v2', 'method': 'GET', 'paths': [],
                'identity_verified': False, 'active_sequence_index': 0,
                'step_id_kind': 'campaign_and_position'},
            'budget': {'requests': 0, 'max_requests': 2, 'credits': 0}, 'reasons': []}
        try:
            for path in (WORKSPACE_PATH, CAMPAIGN_PATH):
                result['source']['paths'].append(path)
                data = self._get(path, budget=result['budget'])
                if path == WORKSPACE_PATH:
                    require(data.get('id') == INSTANTLY_WORKSPACE, 'instantly_workspace_mismatch')
            require(data.get('id') == INSTANTLY_CAMPAIGN, 'instantly_campaign_mismatch')
            require(data.get('organization') == INSTANTLY_WORKSPACE, 'instantly_organization_mismatch')
            result['source']['identity_verified'] = True
            options = {key: deepcopy(value) for key, value in data.items()
                       if key in _OPTIONS or _option_key(key)}
            inputs = {'schema_version': 1, 'provider': 'instantly',
                'account_id': INSTANTLY_WORKSPACE, 'campaign_id': INSTANTLY_CAMPAIGN,
                'campaign': options}
            reasons, inherited = [], []
            validations = {
                'campaign_status': ('status', lambda v: type(v) is int and v in _STATUS),
                'owner_id': ('owned_by', _uuid),
                'sending_accounts': ('email_list', _accounts),
                'stop_on_reply': ('stop_on_reply', lambda v: type(v) is bool),
                'stop_on_auto_reply': ('stop_on_auto_reply', lambda v: type(v) is bool),
                'campaign_schedule': ('campaign_schedule', _schedule),
                'core_variables': ('core_variables', lambda v: isinstance(v, dict)),
                'custom_variables': ('custom_variables', lambda v: isinstance(v, dict))}
            for target, (key, valid) in validations.items():
                value = data.get(key)
                if valid(value):
                    result[target] = deepcopy(value)
                else:
                    reasons.append(key + '_unknown')
                    if key in ('core_variables', 'custom_variables'):
                        inherited.append('variables_unknown')
            if data.get('core_variables') or data.get('custom_variables'):
                inherited.append('variable_resolution_unverified')
            dynamic_options = {key: value for key, value in options.items()
                               if key != 'sequences'}
            if _dynamic(dynamic_options):
                inherited.append('dynamic_content_unverified')
            sequences = data.get('sequences')
            steps = (sequences[0].get('steps') if isinstance(sequences, list) and sequences
                     and isinstance(sequences[0], dict) else None)
            if not isinstance(steps, list) or not steps:
                reasons.append('sequence_steps_unknown')
                steps = []
            elif _dynamic({k: v for k, v in sequences[0].items() if k != 'steps'}):
                inherited.append('dynamic_content_unverified')
            items = [_step(raw, i, inherited) for i, raw in enumerate(steps, 1)]
            for step in items:
                reasons.extend(step['reasons'])
            result.update(inputs=inputs, input_hash=hashlib.sha256(_canonical(inputs)).hexdigest(),
                steps=items, reasons=sorted(set(reasons)), complete=not reasons,
                status='partial' if reasons else 'ok')
        except ExternalEmailError as error:
            result['reasons'] = [error.code]
        except Exception:
            # Never expose exceptions, provider error payloads, headers or credentials.
            result['reasons'] = ['instantly_read_unavailable']
        return result
