"""Compare scoped CRM and provider identity observations without network access.

This module does not authorize dispatch. Its result must be bound to a specific
human-reviewed preview and re-observed by the dispatch guard.
"""
import hashlib
import json
import unicodedata

from .connection_intake import profile_key


class IdentityObservationError(ValueError):
    pass


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise IdentityObservationError('identity_observation_incomplete')
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def compare_identity(contact_id, contact, profile, plan):
    """Return exact observation hash and mismatch fields; incomplete reads fail.

    Callers must acquire these observations from authenticated, scoped sources;
    user-supplied fields and arbitrary cached records are not source evidence.
    """
    if not isinstance(contact_id, str) or not contact_id.isdigit():
        raise IdentityObservationError('identity_contact_invalid')
    if not all(isinstance(value, dict) for value in (contact, profile, plan)):
        raise IdentityObservationError('identity_observation_incomplete')
    if contact.get('id') != contact_id or profile.get('account_id') != plan.get('account_id'):
        raise IdentityObservationError('identity_scope_mismatch')
    if profile.get('profile_urn') != plan.get('recipient_profile_urn'):
        raise IdentityObservationError('identity_scope_mismatch')
    try:
        crm_url = profile_key(contact.get('linkedin_url'))
        provider_url = profile_key(profile.get('linkedin_url'))
        plan_url = profile_key('https://www.linkedin.com/in/' + plan['recipient_public_id'])
    except (ValueError, TypeError, KeyError):
        raise IdentityObservationError('identity_observation_incomplete') from None
    if not crm_url or not provider_url or not plan_url or provider_url != plan_url:
        raise IdentityObservationError('identity_scope_mismatch')
    crm_name, provider_name = _text(contact.get('name')), _text(profile.get('name'))
    crm_company, provider_company = _text(contact.get('company')), _text(profile.get('company'))
    mismatches = [key for key, left, right in (
        ('linkedin_url', crm_url, provider_url), ('name', crm_name, provider_name),
        ('company', crm_company, provider_company)) if left != right]
    observation = {'contact_id': contact_id, 'account_id': plan['account_id'],
        'profile_urn': profile['profile_urn'], 'crm_url': crm_url,
        'provider_url': provider_url, 'crm_name': crm_name, 'provider_name': provider_name,
        'crm_company': crm_company, 'provider_company': provider_company}
    digest = hashlib.sha256(json.dumps(observation, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()
    return {'matches': not mismatches, 'mismatches': mismatches, 'observation_hash': digest}
