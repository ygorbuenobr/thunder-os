"""Fail-closed, per-contact suppression contract for delegated dispatch.

Reader implementations must perform a fresh, complete provider read on each
call. This contract does not turn cached campaign snapshots into readbacks.
"""
from dataclasses import dataclass
import time


class SuppressionUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ContactScope:
    hubspot_contact_id: str
    linkedin_profile_urn: str
    email: str
    email_provider: str


@dataclass(frozen=True)
class SuppressionProof:
    scope: ContactScope
    completed_at: float

    def valid_for(self, scope, *, now=None, max_age=30):
        current = time.monotonic() if now is None else now
        age = current - self.completed_at
        return self.scope == scope and 0 <= age <= max_age


class DelegatedSuppression:
    """Reader responses explicitly bind their target, completeness and status.

    Each reader returns {'target': <exact requested ID>, 'complete': True,
    'suppressed': False}; missing or ambiguous flags never mean a clean result.
    """
    def __init__(self, *, linkedin, email_readers, hubspot, clock=None):
        self.linkedin = linkedin
        self.email_readers = email_readers
        self.hubspot = hubspot
        self.clock = clock or time.monotonic

    def check(self, scope):
        started = self.clock()
        if (not isinstance(scope, ContactScope)
                or any(not isinstance(v, str) or not v.strip() for v in
                       (scope.hubspot_contact_id, scope.linkedin_profile_urn,
                        scope.email, scope.email_provider))
                or scope.email_provider not in ('apollo', 'instantly')):
            raise SuppressionUnavailable('invalid_contact_scope')
        if not isinstance(self.email_readers, dict):
            raise SuppressionUnavailable('reader_unavailable')
        checks = ((self.linkedin, scope.linkedin_profile_urn),
                  (self.email_readers.get(scope.email_provider), scope.email),
                  (self.hubspot, scope.hubspot_contact_id))
        for reader, target in checks:
            if not callable(reader):
                raise SuppressionUnavailable('reader_unavailable')
            try:
                result = reader(target)
            except Exception:
                raise SuppressionUnavailable('provider_read_failed') from None
            if (not isinstance(result, dict) or result.get('target') != target
                    or result.get('complete') is not True
                    or result.get('suppressed') is not False):
                raise SuppressionUnavailable('suppression_not_cleared')
        # Age all evidence from the beginning, including slow provider calls.
        return SuppressionProof(scope, started)
