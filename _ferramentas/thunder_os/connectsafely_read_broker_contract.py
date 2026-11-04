"""Offline protocol prototype, not an isolated or deployable broker.

No sockets, credential loading or network are provided. Process, operating-system,
credential and network isolation must be implemented and verified separately.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone


class ReadBrokerError(ValueError):
    """Static error codes only; never return provider output or credentials."""


def _identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', value) is not None


class AccountReadBrokerContract:
    """Accept one operation for one explicitly bound company and account."""

    _FIELDS = frozenset({'version', 'operation', 'company_id', 'account_id', 'request_id'})

    def __init__(self, company_id: str, account_id: str, public_id: str, read_get: Callable,
                 *, clock: Callable = lambda: datetime.now(timezone.utc)):
        if (not _identifier(company_id) or not isinstance(account_id, str)
                or not re.fullmatch(r'[a-f0-9]{24}', account_id)
                or not isinstance(public_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', public_id)
                or not callable(read_get) or not callable(clock)):
            raise ReadBrokerError('invalid_configuration')
        self._company_id = company_id
        self._account_id = account_id
        self._public_id = public_id
        self._read_get = read_get
        self._clock = clock

    def _get(self, path: str, query: dict):
        try:
            status, payload = self._read_get(path, query)
        except Exception:
            raise ReadBrokerError('provider_unavailable') from None
        if type(status) is not int or status != 200:
            raise ReadBrokerError('provider_unavailable')
        return payload

    def dispatch(self, envelope: dict) -> dict:
        """Return minimal account observation, not proof of execution binding."""
        if (not isinstance(envelope, dict) or set(envelope) != self._FIELDS
                or type(envelope.get('version')) is not int or envelope['version'] != 1
                or envelope.get('operation') != 'account_status'
                or not _identifier(envelope.get('request_id'))):
            raise ReadBrokerError('invalid_request')
        if envelope.get('company_id') != self._company_id or envelope.get('account_id') != self._account_id:
            raise ReadBrokerError('scope_mismatch')

        rows = self._get('/linkedin/accounts', {'q': self._account_id})
        if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
            raise ReadBrokerError('invalid_response')
        matches = [row for row in rows if row.get('id') == self._account_id]
        if (len(matches) != 1 or matches[0].get('publicId') != self._public_id
                or sum(row.get('publicId') == self._public_id for row in rows) != 1):
            raise ReadBrokerError('identity_mismatch')

        payload = self._get('/linkedin/account/' + self._account_id + '/status', {})
        if not isinstance(payload, dict):
            raise ReadBrokerError('invalid_response')
        if payload.get('id') != self._account_id or payload.get('publicId') != self._public_id:
            raise ReadBrokerError('identity_mismatch')
        if (payload.get('status') not in ('AVAILABLE', 'IN_USE', 'ERROR', 'WARMUP', 'UNKNOWN')
                or type(payload.get('enabled')) is not bool or type(payload.get('hasTokens')) is not bool):
            raise ReadBrokerError('invalid_response')
        try:
            observed = self._clock()
            if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError('invalid clock')
            observed_at = observed.astimezone(timezone.utc).isoformat()
        except Exception:
            raise ReadBrokerError('observation_time_invalid') from None
        return {'version': 1, 'operation': 'account_status', 'request_id': envelope['request_id'],
                'company_id': self._company_id, 'account_id': self._account_id,
                'public_id': self._public_id, 'status': payload['status'],
                'source': 'connectsafely', 'observed_at': observed_at,
                'available': payload['status'] == 'AVAILABLE' and payload['enabled'] and payload['hasTokens']}
