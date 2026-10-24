"""Bounded, read-only observation of the exact manually exported cohort.

The caller persists the returned dictionary as row['reconciliation'] under its
existing optimistic transaction/revalidation. This function does not mutate row.
The injected synchronous provider owns timeouts, as in NewJourney._provider;
HTTP callers must keep these reads outside their event loop.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime


REQUEST_BUDGET = 10
_IDENTITY_POLICY = 'unique_email_snapshot_binding_v1'
_PROVIDER_ERRORS = {'permission_denied', 'credential_unavailable', 'credential_rejected',
                    'disabled', 'configuration_pending', 'rate_limited', 'timeout'}


def _observe(lead, snapshot, provider, observed_at):
    # Deferred reuse allows NewJourney to import observe_batch during integration.
    from .new_journey import _ACCOUNT_PROOFS, _ID, NewJourneyError

    observation = {'source_key': lead['source_key'], 'status': 'unknown',
                   'reason': 'membership_unverified', 'observed_at': observed_at}
    try:
        response = provider('contacts_search', query=lead['email'], page=1, per_page=100)
        if isinstance(response, Mapping) and response.get('status') == 'error':
            reason = response.get('reason')
            observation['reason'] = ('journey_apollo_' + reason if isinstance(reason, str)
                                     and reason in _PROVIDER_ERRORS else 'journey_apollo_unavailable')
            return observation
        if (not isinstance(response, Mapping) or response.get('status') not in ('ok', 'partial')
                or response.get('credit_cost') != 0 or not isinstance(response.get('items'), list)):
            observation['reason'] = 'journey_apollo_unavailable'
            return observation
        source = response.get('source')
        if (not isinstance(source, Mapping) or source.get('account_id') != snapshot['account_id']
                or source.get('identity_source') not in _ACCOUNT_PROOFS):
            observation['reason'] = 'journey_apollo_account_mismatch'
            return observation
        contacts = [item for item in response['items'] if isinstance(item, Mapping)
                    and isinstance(item.get('email'), str)
                    and item['email'].lower() == lead['email'].lower()]
        if len(contacts) != 1 or response.get('complete') is not True or response['status'] != 'ok':
            return observation
        contact = contacts[0]
        if not isinstance(contact.get('id'), str) or not _ID.fullmatch(contact['id']):
            return observation
        if 'recipient_bindings' in snapshot:
            bound_id = snapshot['recipient_bindings'][lead['source_key']]['contact_id']
            # None proves absence at export, allowing a new contact after manual
            # enrollment. A preexisting contact must retain its canonical ID.
            if bound_id is not None and contact['id'] != bound_id:
                return observation
        statuses = contact.get('contact_campaign_statuses')
        if not isinstance(statuses, list) or contact.get('contact_campaign_statuses_complete') is not True:
            return observation
        matching = [s for s in statuses if isinstance(s, Mapping)
                    and s.get('emailer_campaign_id') == snapshot['sequence_id']]
        if len(matching) == 1:
            membership = matching[0]
            observation.update(sequence_id=snapshot['sequence_id'], contact_id=contact.get('id'),
                               provider_state=membership.get('state'), provider_status=membership.get('status'))
            signals = [membership[key] for key in ('state', 'status') if key in membership]
            if signals and all(signal in ('active', 'in_progress') for signal in signals):
                observation.update(status='enrolled', reason='sequence_membership_observed')
    except NewJourneyError as exc:
        # Only adapter error codes from the existing read contract are exposed.
        allowed = {'journey_apollo_' + reason for reason in _PROVIDER_ERRORS}
        observation['reason'] = exc.code if exc.code in allowed else 'journey_apollo_unavailable'
    except Exception:
        observation['reason'] = 'journey_apollo_unavailable'
    return observation


def observe_batch(row, provider, now):
    """Return persistable observations/pagination for row['export_snapshot'] only.

    provider('contacts_search', query=email, page=1, per_page=100) returns the
    existing read envelope. At most ten calls are made, once per exported key in
    a round. Save this entire return value as row['reconciliation'] to resume.
    Snapshot changes reset both observations and cursor. Duplicate snapshot keys
    are deduplicated in order; ambiguous/missing source rows stay unknown.
    Exact-email uniqueness is checked before the canonical snapshot ID; CSV IDs
    are never identity evidence. If recipient_bindings is present, every queried
    key needs a complete binding with the exported email/account. A bound None ID
    permits a new unique provider contact after manual enrollment.

    pagination.complete describes traversal (including failed reads), never
    enrollment success. Per-lead observed_at shows freshness across rounds;
    current remains False because observations are not execution authority.
    After a full traversal the cursor wraps for subsequent read-only refreshes.
    """
    from .new_journey import _ID, _text, _valid_email, NewJourneyError

    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise NewJourneyError('journey_clock_invalid', 503)
    observed_at = now.isoformat()
    raw = row.get('export_snapshot')
    valid = (isinstance(raw, Mapping) and _text(raw.get('scope_hash'))
             and _text(raw.get('account_id')) and isinstance(raw.get('sequence_id'), str)
             and bool(_ID.fullmatch(raw['sequence_id']))
             and isinstance(raw.get('source_keys'), list)
             and all(_text(key) for key in raw['source_keys']))
    result = {'observed_at': observed_at, 'current': False, 'status': 'unknown',
              'identity_policy': _IDENTITY_POLICY,
              'sequence_id': None, 'account_id': None, 'scope_hash': None,
              'export_snapshot': None, 'snapshot_changed': False, 'cursor': 0,
              'request_count': 0, 'request_budget': REQUEST_BUDGET, 'rows': [],
              'pagination': {'next_cursor': 0, 'total': 0, 'processed_count': 0,
                             'remaining_count': 0, 'complete': False},
              'external_writes': 0, 'credits': 0}
    if not valid:
        result['reason'] = 'export_snapshot_missing' if raw is None else 'export_snapshot_invalid'
        return result

    snapshot = {key: raw[key] for key in ('scope_hash', 'sequence_id', 'account_id')}
    keys = list(dict.fromkeys(raw['source_keys']))
    snapshot['source_keys'] = keys
    if 'recipient_bindings' in raw:
        snapshot['recipient_bindings'] = deepcopy(raw['recipient_bindings'])
    previous = row.get('reconciliation')
    previous = previous if isinstance(previous, Mapping) else {}
    # Discard cached positives produced before exact-email uniqueness was enforced.
    same_snapshot = (previous.get('export_snapshot') == snapshot
                     and previous.get('identity_policy') == _IDENTITY_POLICY)
    result.update(export_snapshot=deepcopy(snapshot), sequence_id=snapshot['sequence_id'],
                  account_id=snapshot['account_id'], scope_hash=snapshot['scope_hash'],
                  snapshot_changed=bool(previous) and not same_snapshot,
                  status='observed' if callable(provider) else 'configuration_pending')
    pagination = previous.get('pagination') if same_snapshot else None
    cursor = pagination.get('next_cursor', 0) if isinstance(pagination, Mapping) else 0
    if type(cursor) is not int or not 0 <= cursor < len(keys):
        cursor = 0

    by_key = {}
    for lead in row.get('rows', []):
        if isinstance(lead, Mapping) and isinstance(lead.get('source_key'), str):
            by_key.setdefault(lead['source_key'], []).append(lead)
    cached = {}
    if same_snapshot and callable(provider) and isinstance(previous.get('rows'), list):
        for item in previous['rows']:
            if isinstance(item, Mapping) and isinstance(item.get('source_key'), str):
                cached[item['source_key']] = item
    observations = {}
    for key in keys:
        prior = cached.get(key)
        observations[key] = (deepcopy(dict(prior)) if prior is not None else
                             {'source_key': key, 'status': 'unknown',
                              'reason': 'request_budget_exhausted' if callable(provider)
                              else 'provider_unavailable'})

    scanned = 0
    while callable(provider) and scanned < len(keys) and result['request_count'] < REQUEST_BUDGET:
        key = keys[cursor]
        matches = by_key.get(key, [])
        bindings = snapshot.get('recipient_bindings')
        binding = bindings.get(key) if isinstance(bindings, Mapping) else None
        observation = {'source_key': key, 'status': 'unknown', 'observed_at': observed_at}
        if len(matches) != 1:
            observation['reason'] = 'exported_source_ambiguous' if matches else 'exported_source_missing'
        elif not _valid_email(matches[0].get('email')):
            observation['reason'] = 'exported_email_invalid'
        elif 'recipient_bindings' in snapshot and (
                not isinstance(binding, Mapping) or binding.get('lookup_complete') is not True
                or binding.get('account_id') != snapshot['account_id']
                or not _valid_email(binding.get('email'))
                or binding['email'].lower() != matches[0]['email'].lower()
                or 'contact_id' not in binding
                or (binding['contact_id'] is not None and (
                    not isinstance(binding['contact_id'], str)
                    or not _ID.fullmatch(binding['contact_id'])))):
            observation['reason'] = 'exported_recipient_binding_unverified'
        else:
            result['request_count'] += 1
            observation = _observe(matches[0], snapshot, provider, observed_at)
        observations[key] = observation
        cursor = (cursor + 1) % len(keys)
        scanned += 1

    result['rows'] = [observations[key] for key in keys]
    processed = sum('observed_at' in item for item in result['rows'])
    result['cursor'] = cursor
    result['pagination'] = {'next_cursor': cursor, 'total': len(keys),
                            'processed_count': processed, 'remaining_count': len(keys) - processed,
                            'complete': processed == len(keys)}
    return result
