"""Receipt contract for the external Codex Mexico runner, not an OS executor."""
from datetime import datetime
import hashlib
import json
import re

MARKER = 'mexico_gmail_receipt_v1'
BINDING_KEYS = ('campaign_id', 'batch', 'hubspot_task_id', 'contact_id',
                'touch', 'sender', 'recipient', 'payload_sha256')


def binding(value):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k] for k in BINDING_KEYS):
        raise ValueError('BINDING_INVALID')
    result = {k: value[k] for k in BINDING_KEYS}
    if (any(not re.fullmatch(r'[1-9][0-9]{0,29}', result[k]) for k in ('hubspot_task_id', 'contact_id'))
            or not re.fullmatch(r'E[1-9][0-9]?', result['touch'])
            or not re.fullmatch(r'[a-f0-9]{64}', result['payload_sha256'])
            or any(not re.fullmatch(r'[^\s@;<>,]+@[^\s@;<>,]+\.[^\s@;<>,]+', result[k]) for k in ('sender', 'recipient'))
            or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', result[k]) for k in ('campaign_id', 'batch'))):
        raise ValueError('BINDING_INVALID')
    return result


def receipt_key(value):
    raw = json.dumps(binding(value), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_receipt(value, expected):
    expected = binding(expected)
    if not isinstance(value, dict) or binding(value) != expected or value.get('marker') != MARKER:
        raise ValueError('RECEIPT_BINDING_MISMATCH')
    if (value.get('key') != receipt_key(expected) or value.get('provider') != 'gmail'
            or value.get('verification') != 'full_readback'
            or value.get('state') != 'PROVIDER_CONFIRMED'
            or not isinstance(value.get('message_id'), str)
            or not re.fullmatch(r'[a-f0-9]{6,64}', value['message_id'])):
        raise ValueError('RECEIPT_INVALID')
    try:
        if datetime.fromisoformat(value['confirmed_at'].replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ValueError('RECEIPT_DATE_INVALID') from None
    return value
