"""Process-local read admission for the readers' pinned account 51412240.

All participating readers use one volatile registry. At most 64 credentials and
16 schema responses per credential (8 MB total) are retained. No record, search,
association, membership or eligibility response is cached. A token digest is the
namespace; raw credentials are never retained here. Other processes and readers
bypassing these hooks in the same portal still require their own coordination.

Admission is account-wide even before identity verification (an unexpected token
can only over-throttle; the caller still must reject a portal mismatch). Cache
keys remain credential-scoped. CRM Search has a separate limit; .21s between
starts keeps below 5/s, while .11s for all reads stays below the smallest
documented 100/10s app burst.
https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines
https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm
No retries. 429 closes admission immediately, including fresh schema cache hits.
"""
from collections import OrderedDict
from contextlib import contextmanager
from hashlib import sha256
import json
import math
import re
import threading
import time


class HubSpotReadBudgetError(RuntimeError):
    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


class HubSpotReadResponse(tuple):
    """Keep the existing (status, bytes) transport contract, with header metadata.

    Raw transport consumers still unpack two values. Admission alone reads the
    optional headers; injected transports may alternatively return three values.
    """
    def __new__(cls, status, raw, headers):
        response = super().__new__(cls, (status, raw))
        response.headers = dict(headers)
        return response


def is_read(method, path):
    """Classification only; callers retain their existing endpoint allowlists."""
    return method == 'GET' or (method == 'POST' and path.endswith(('/search', '/batch/read')))


def _schema_response(raw):
    try:
        if not isinstance(raw, bytes) or len(raw) > 2_000_000:
            return False
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get('results'), list):
            return False
        paging = value.get('paging', {})
        if (not isinstance(paging, dict) or paging.get('next')
                or 'next' in paging and not isinstance(paging['next'], dict)
                or value.get('errors') or value.get('status', 'COMPLETE') != 'COMPLETE'
                or len(value['results']) > 5000):
            return False
        names = [row.get('name') if isinstance(row, dict) else None for row in value['results']]
        return (all(isinstance(name, str) and bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,199}', name))
                    for name in names) and len(set(names)) == len(names))
    except (ValueError, TypeError, UnicodeError):
        return False


class _CredentialState:
    def __init__(self):
        self.users = 0
        self.schemas = OrderedDict()


class HubSpotReadBudget:
    """One attempt with bounded lock/pacing wait and schema singleflight.

    Inject a *shared* instance with an advancing monotonic/sleep pair in tests.
    Defaults are conservative local ceilings, not a claim about portal quotas.
    DAILY 429 latches admission closed for this instance: no automatic timed
    retry, since the account timezone/reset and remaining daily quota are unknown.
    Schema TTL is 60s from request start; expiry/failure never serves stale data.
    Only visibility readers opt into cache_schema. Critical HubSpotHTTP reads
    explicitly disable it, including eligibility and write-preparation schemas.
    Optional transport headers accompany the pair or are a third tuple member.
    """
    def __init__(self, *, monotonic=time.monotonic, sleep=time.sleep,
                 schema_ttl=60, max_credentials=64, max_schemas=16):
        if (not math.isfinite(schema_ttl) or not 0 <= schema_ttl <= 60
                or type(max_credentials) is not int or not 1 <= max_credentials <= 64
                or type(max_schemas) is not int or not 1 <= max_schemas <= 16):
            raise ValueError('invalid_read_budget_configuration')
        self.monotonic, self.sleep = monotonic, sleep
        self.schema_ttl, self.max_credentials, self.max_schemas = schema_ttl, max_credentials, max_schemas
        self._lock, self._states = threading.Lock(), OrderedDict()
        self._admission = threading.Lock()
        self._next_read = self._next_search = self._cooldown = 0.0
        self._daily_limited = False

    @contextmanager
    def _state(self, token):
        key = sha256(token.encode()).digest()
        with self._lock:
            if key not in self._states:
                if len(self._states) >= self.max_credentials:
                    victim = next((k for k, s in self._states.items() if s.users == 0), None)
                    if victim is None:
                        raise HubSpotReadBudgetError('read_budget_exhausted')
                    del self._states[victim]
                self._states[key] = _CredentialState()
            state = self._states[key]
            self._states.move_to_end(key)
            state.users += 1
        try:
            yield state
        finally:
            with self._lock:
                state.users -= 1

    def call(self, token, method, path, body, headers, timeout, request, *, cache_schema=True):
        if not is_read(method, path):
            return request(method, path, body, headers, timeout)[:2]
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise HubSpotReadBudgetError('read_budget_exhausted')
        deadline = self.monotonic() + timeout
        schema = cache_schema is True and method == 'GET' and body is None and bool(re.fullmatch(
            r'/crm/v3/properties/[A-Za-z0-9_-]+', path))
        search = method == 'POST' and path.endswith('/search')
        with self._state(token) as state:
            if not self._admission.acquire(timeout=max(0, deadline - self.monotonic())):
                raise HubSpotReadBudgetError('read_budget_exhausted')
            try:
                now = self.monotonic()
                if now >= deadline:
                    raise HubSpotReadBudgetError('read_budget_exhausted')
                if self._daily_limited:
                    raise HubSpotReadBudgetError('read_daily_limit', 429)
                if now < self._cooldown:
                    raise HubSpotReadBudgetError('read_cooldown', 429)
                state.schemas = OrderedDict((k, v) for k, v in state.schemas.items() if v[0] > now)
                if schema and path in state.schemas:
                    return 200, state.schemas[path][1]
                delay = max(0, self._next_read - now, self._next_search - now if search else 0)
                if now + delay >= deadline:
                    raise HubSpotReadBudgetError('read_budget_exhausted')
                if delay:
                    self.sleep(delay)
                started = self.monotonic()
                # A stalled fake clock or early wake must never bypass pacing.
                if started < now + delay or started >= deadline:
                    raise HubSpotReadBudgetError('read_budget_exhausted')
                self._next_read = started + .11
                if search:
                    self._next_search = started + .21
                response = request(method, path, body, headers, deadline - started)
                status, raw = response[:2]
                if type(status) is int and status == 429:
                    try:
                        detail = json.loads(raw) if isinstance(raw, bytes) and len(raw) <= 2_000_000 else {}
                        self._daily_limited = isinstance(detail, dict) and detail.get('policyName') == 'DAILY'
                    except (ValueError, UnicodeError):
                        pass
                    cooldown = 10.0
                    response_headers = response[2] if len(response) == 3 else getattr(response, 'headers', {})
                    if isinstance(response_headers, dict):
                        value = next((v for k, v in response_headers.items()
                                      if isinstance(k, str) and k.lower() == 'retry-after'), None)
                        try:
                            seconds = float(value)
                            if math.isfinite(seconds):
                                cooldown = max(cooldown, seconds)
                        except (ValueError, TypeError):
                            pass
                    self._cooldown = self.monotonic() + cooldown
                    with self._lock:
                        for cached in self._states.values():
                            cached.schemas.clear()
                if self.monotonic() >= deadline:
                    raise HubSpotReadBudgetError('read_budget_exhausted')
                if (schema and type(status) is int and status == 200 and _schema_response(raw)
                        and token.encode() not in raw and started + self.schema_ttl > self.monotonic()):
                    with self._lock:
                        if len(state.schemas) >= self.max_schemas:
                            state.schemas.popitem(last=False)
                        state.schemas[path] = (started + self.schema_ttl, raw)
                        while sum(len(v[1]) for s in self._states.values() for v in s.schemas.values()) > 8_000_000:
                            victim = next(s for s in self._states.values() if s.schemas)
                            victim.schemas.popitem(last=False)
                return status, raw
            finally:
                self._admission.release()


SHARED_READ_BUDGET = HubSpotReadBudget()
