"""Human-reviewed ICP facts, confined to the existing internal journey draft."""
from collections.abc import Mapping
from datetime import date, datetime, timezone
import ipaddress
import math
from urllib.parse import urlsplit

from .models import payload_hash


class QualificationError(ValueError):
    status = 409

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def candidate(evidence):
    identity = evidence.get('identity', {})
    company = evidence.get('company') or {}
    props = company.get('properties', {})
    flags = evidence.get('property_coverage', {}).get('companies', {})
    if (identity.get('verified') is not True or not identity.get('company_id')
            or flags.get('name') is not True or not identity.get('domain')):
        return None
    bound = {key: identity.get(key) for key in
             ('contact_id', 'company_id', 'domain', 'emails', 'linkedin_urls')}
    bound['company_name'] = props['name']
    return {'binding_hash': payload_hash(bound), 'company_id': identity['company_id'],
            'contact_id': identity['contact_id'], 'company_name': props['name'],
            'domain': identity['domain'], 'source': evidence.get('source_ref'),
            'observed': {key: props.get(key) for key in
                         ('hs_country_code', 'thunder_icp_sector', 'thunder_annual_revenue_estimate')}}


def _text(value, limit):
    return (isinstance(value, str) and 0 < len(value.strip()) <= limit
            and not any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in value))


def _source(value):
    if not _text(value, 2000):
        return False
    try:
        u = urlsplit(value)
        if (u.scheme != 'https' or not u.hostname or '.' not in u.hostname
                or u.username or u.password or u.port or u.hostname.endswith(('.local', '.internal'))):
            return False
        try:
            return ipaddress.ip_address(u.hostname).is_global
        except ValueError:
            return u.hostname not in ('localhost',)
    except ValueError:
        return False


def review(data, context, sheet_hash, now):
    fields = {'decision', 'is_b2b', 'country', 'sector', 'annual_revenue_brl',
              'source_url', 'source_date', 'rationale'}
    if not isinstance(data, Mapping) or set(data) != fields or not context:
        raise QualificationError('journey_qualification_invalid')
    try:
        observed = date.fromisoformat(data['source_date'])
        revenue = data['annual_revenue_brl']
        valid = (data['decision'] in ('qualified', 'disqualified')
            and type(data['is_b2b']) is bool and data['country'] in ('BR', 'OTHER')
            and data['sector'] in ('industria_b2b', 'distribuicao_b2b', 'outro')
            and type(revenue) in (float, int) and math.isfinite(revenue) and revenue >= 0
            and _source(data['source_url']) and 0 <= (now.date() - observed).days <= 366
            and _text(data['rationale'], 3000))
    except (TypeError, ValueError, KeyError):
        valid = False
    if not valid:
        raise QualificationError('journey_qualification_invalid')
    return {'actor': 'ygor', 'reviewed_at': now.isoformat(), 'binding_hash': context['binding_hash'],
            'sheet_hash': sheet_hash, 'facts': dict(data), 'source_kind': 'human_review_not_crm_write'}


def decision(record, context, sheet_hash, now):
    if not isinstance(record, Mapping) or not context:
        return None
    try:
        when = datetime.fromisoformat(record['reviewed_at'])
        if (record.get('actor') != 'ygor' or when.tzinfo is None
                or not 0 <= (now - when.astimezone(timezone.utc)).total_seconds() <= 14 * 86400
                or record.get('binding_hash') != context['binding_hash']
                or record.get('sheet_hash') != sheet_hash):
            return None
        facts = record['facts']
        review(facts, context, sheet_hash, now)
        fits = (facts['decision'] == 'qualified' and facts['is_b2b'] is True
                and facts['country'] == 'BR' and facts['sector'] in ('industria_b2b', 'distribuicao_b2b')
                and facts['annual_revenue_brl'] > 10_000_000)
        return {'fits': fits, 'source': 'human_icp_review:' + payload_hash(record)}
    except (KeyError, TypeError, ValueError):
        return None
