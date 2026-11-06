"""Read-only, fail-closed reply observations for Apollo + LinkedIn.

Composes existing canary readers and, when configured, fresh HubSpot eligibility
and history readers. It never pauses a cadence, authorizes an action, or writes
externally. The caller must persist a reply stop and recheck freshness at the
execution boundary; an observation is not an approval or a durable stop.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re

MAX_AGE = timedelta(minutes=15)
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def _now(clock):
    value = clock()
    if type(value) in (int, float) and math.isfinite(value):
        value = datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('clock_unavailable')
    return value.astimezone(timezone.utc)


def _stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('observation_time_invalid')
    return value.astimezone(timezone.utc)


def _observation(status, now, reason):
    return {'status': status, 'observed_at': now.isoformat().replace('+00:00', 'Z'), 'reason': reason}


def coverage_current(result, now, *, hubspot_configured):
    """Validate each required observation again immediately before use."""
    try:
        current = _stamp(now)
        if type(hubspot_configured) is not bool or not isinstance(result, dict) or result.get('status') != 'ok':
            return False
        sources = result.get('sources')
        required = result.get('required_sources')
        if (not isinstance(sources, dict) or not isinstance(required, list)
                or set(required) != ({'apollo', 'linkedin', 'hubspot'} if hubspot_configured else {'apollo', 'linkedin'})
                or set(required) != set(sources) or len(required) != len(sources)):
            return False
        for name in required:
            item = sources[name]
            if not isinstance(item, dict) or item.get('status') != 'ok':
                return False
            elapsed = current - _stamp(item['observed_at'])
            if elapsed < timedelta(0) or elapsed > MAX_AGE:
                return False
        return True
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


class HubSpotReplySource:
    """Compose the existing fresh eligibility and contact-history readers.

    ``eligibility_factory`` must make a new HubSpotEligibilityRead per request:
    its cache is scoped to that request. ``history`` is ContactHistoryReadonly.
    Inbound activity without explicit human/automatic classification is unknown,
    not evidence of a human reply and not evidence of no reply.
    """
    def __init__(self, eligibility_factory, history, *, clock=None):
        self._eligibility_factory, self._history = eligibility_factory, history
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, target):
        email, cid, company = target['email'], target['hubspot_contact_id'], target['hubspot_company_id']
        evidence = self._eligibility_factory().lookup(email, contact_id=cid, profile=target['linkedin_url'])
        if not isinstance(evidence, dict):
            return 'unknown', 'hubspot_evidence_unavailable'
        identity, coverage = evidence.get('identity'), evidence.get('coverage')
        if (not isinstance(identity, dict) or identity.get('verified') is not True
                or str(identity.get('contact_id')) != cid or str(identity.get('company_id')) != company
                or not isinstance(coverage, dict)
                or any(coverage.get(k) is not True for k in ('identity', 'company', 'deals'))):
            return 'unknown', 'hubspot_identity_or_deals_incomplete'
        deals = evidence.get('deals')
        if not isinstance(deals, list):
            return 'unknown', 'hubspot_deals_incomplete'
        for deal in deals:
            props = deal.get('properties') if isinstance(deal, dict) else None
            if not isinstance(props, dict) or not (props.get('hs_is_closed') is True or props.get('hs_is_closed') == 'true'):
                return 'unknown', 'hubspot_deal_open_or_unverified'
        context = {'binding_hash': target['hubspot_binding_hash'], 'contact_id': cid,
                   'company_id': company, 'verified_linkedin_urls': [target['linkedin_url']]}
        history = self._history.read({'email': email, 'linkedin_url': target['linkedin_url']}, context)
        if (not isinstance(history, dict) or str(history.get('contact_id')) != cid
                or str(history.get('company_id')) != company):
            return 'unknown', 'hubspot_history_identity_unverified'
        try:
            age = _now(self._clock) - _stamp(history.get('observed_at'))
            if not timedelta(0) <= age <= MAX_AGE:
                return 'unknown', 'hubspot_history_stale'
        except (ValueError, TypeError, OverflowError):
            return 'unknown', 'hubspot_history_stale'
        hubspot_sources = [x for x in history.get('sources', []) if isinstance(x, dict)
                           and x.get('provider') == 'hubspot']
        if len(hubspot_sources) != 1 or hubspot_sources[0].get('complete') is not True:
            return 'unknown', 'hubspot_history_incomplete'
        events = history.get('events')
        if not isinstance(events, list):
            return 'unknown', 'hubspot_history_incomplete'
        unclear = False
        for event in events:
            if not isinstance(event, dict):
                return 'unknown', 'hubspot_history_invalid'
            if event.get('provider') not in ('hubspot', 'gmail'):
                return 'unknown', 'hubspot_history_invalid'
            if event.get('provider') != 'hubspot':
                continue
            direction = event.get('direction')
            if direction == 'incoming':
                if event.get('classification') == 'human':
                    return 'reply', 'hubspot_human_reply'
                unclear = True
            elif direction not in ('outgoing',):
                unclear = True
        if unclear:
            return 'unknown', 'hubspot_inbound_classification_unverified'
        return 'ok', 'hubspot_contact_and_deals_observed'


class ReplyCoverage:
    """Compose scoped read-only readers; never infer no reply from partial data."""
    def __init__(self, apollo, linkedin, *, hubspot=None, hubspot_configured=False, clock=None):
        if type(hubspot_configured) is not bool:
            raise ValueError('hubspot_configuration_invalid')
        self.apollo, self.linkedin, self.hubspot = apollo, linkedin, hubspot
        self.hubspot_configured = hubspot_configured
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _apollo(self, target):
        rows = self.apollo.contacts(target['email'])
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return 'unknown', 'apollo_contact_unavailable'
        row = rows[0]
        if row.get('id') != target['apollo_contact_id']:
            return 'unknown', 'apollo_contact_mismatch'
        memberships = row.get('contact_campaign_statuses')
        if not isinstance(memberships, list):
            return 'unknown', 'apollo_membership_unavailable'
        if row.get('email_replied') is True or any(
                isinstance(m, dict) and (m.get('replied') is True
                    or m.get('in_response_to_emailer_message_id')
                    or m.get('inactive_reason') in ('replied', 'contact_replied'))
                for m in memberships):
            return 'reply', 'apollo_reply_observed'
        if (row.get('email_unsubscribed') is True or row.get('email_status') in ('unsubscribed', 'bounced')
                or any(isinstance(m, dict) and m.get('inactive_reason') in ('unsubscribed', 'bounced')
                       for m in memberships)):
            return 'unknown', 'apollo_suppression_observed'
        if (type(row.get('email_replied')) is not bool
                or type(row.get('email_unsubscribed')) is not bool
                or not isinstance(row.get('email_status'), str) or not row['email_status']
                or any(not isinstance(m, dict) or type(m.get('replied')) is not bool
                       or not isinstance(m.get('emailer_campaign_id'), str)
                       or not m['emailer_campaign_id'] for m in memberships)):
            return 'unknown', 'apollo_coverage_incomplete'
        matches = [m for m in memberships if isinstance(m, dict)
                   and m.get('emailer_campaign_id') == target['sequence_id']]
        if len(matches) != 1:
            return 'unknown', 'apollo_sequence_unverified'
        m = matches[0]
        if (m.get('status') != 'active' or type(m.get('replied')) is not bool
                or m.get('replied') is not False
                or ('email_replied' in row and type(row['email_replied']) is not bool)
                or m.get('inactive_reason') not in (None, '')):
            return 'unknown', 'apollo_reply_or_sequence_state_unverified'
        return 'ok', 'apollo_active_sequence_no_reply'

    def _linkedin(self, target):
        evidence = self.linkedin(target['linkedin_profile_urn'])
        if (not isinstance(evidence, dict) or evidence.get('target') != target['linkedin_profile_urn']
                or evidence.get('complete') is not True or type(evidence.get('suppressed')) is not bool
                or evidence.get('coverage') != 'exact_profile_conversation'):
            return 'unknown', 'linkedin_conversation_incomplete'
        return ('reply', 'linkedin_reply_observed') if evidence['suppressed'] else ('ok', 'linkedin_conversation_observed')

    def observe(self, target):
        required = ['apollo', 'linkedin'] + (['hubspot'] if self.hubspot_configured else [])
        now = _now(self.clock)
        result = {'status': 'unknown', 'observed_at': now.isoformat().replace('+00:00', 'Z'),
                  'required_sources': required, 'sources': {}}
        if (not isinstance(target, dict)
                or not all(isinstance(target.get(k), str) and target[k] for k in
                           ('email', 'apollo_contact_id', 'sequence_id', 'linkedin_profile_urn'))
                or not _ID.fullmatch(target['apollo_contact_id']) or not _ID.fullmatch(target['sequence_id'])
                or not re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}', target['linkedin_profile_urn'])):
            for name in required:
                result['sources'][name] = _observation('unknown', now, 'target_unverified')
            return result
        for name in required:
            started = _now(self.clock)
            try:
                if name == 'apollo':
                    status, reason = self._apollo(target)
                elif name == 'linkedin':
                    status, reason = self._linkedin(target)
                elif self.hubspot is None:
                    status, reason = 'unknown', 'hubspot_unavailable'
                else:
                    status, reason = self.hubspot(target)
                if status not in ('ok', 'reply', 'unknown') or not isinstance(reason, str):
                    status, reason = 'unknown', 'source_response_invalid'
            except Exception:
                status, reason = 'unknown', name + '_unavailable'
            finished = _now(self.clock)
            if finished < started or finished - started > MAX_AGE:
                if status == 'reply':
                    reason = 'reply_observed_stale_requires_stop'
                else:
                    status, reason = 'unknown', 'observation_stale_or_clock_changed'
            result['sources'][name] = _observation(status, started, reason)
        now = _now(self.clock)
        result['observed_at'] = now.isoformat().replace('+00:00', 'Z')
        for item in result['sources'].values():
            if not timedelta(0) <= now - _stamp(item['observed_at']) <= MAX_AGE:
                if item['status'] == 'reply':
                    item['reason'] = 'reply_observed_stale_requires_stop'
                else:
                    item['status'], item['reason'] = 'unknown', 'observation_stale_or_clock_changed'
        statuses = [item['status'] for item in result['sources'].values()]
        result['status'] = 'reply' if 'reply' in statuses else 'unknown' if 'unknown' in statuses else 'ok'
        return result
