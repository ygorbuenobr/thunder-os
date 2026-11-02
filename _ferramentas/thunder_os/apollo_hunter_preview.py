"""Pure Apollo-to-Hunter planning; no provider or persistence access."""
from copy import deepcopy
import re
from urllib.parse import urlsplit, parse_qs, unquote, quote

_DOMAIN = re.compile(r'^(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}$')
_ID = re.compile(r'^[0-9]+$')
_PERSONAL = frozenset({'gmail.com', 'googlemail.com', 'hotmail.com', 'outlook.com',
                       'live.com', 'outlook.com.br', 'hotmail.com.br', 'yahoo.com', 'yahoo.com.br', 'icloud.com', 'aol.com'})
_EXCLUDED = frozenset({'embalatec.com', 'embalatec.com.br'})
_PIPELINE = '903417452'
_NEW_DEAL = {'pipeline': _PIPELINE, 'dealstage': '1366237352',
             'hubspot_owner_id': '92901049', 'origem_do_lead': 'Outbound GTM'}


def _domain(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _DOMAIN.fullmatch(value) and '..' not in value else None


def _id(value):
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and _ID.fullmatch(str(value)) else None


def _email(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value.count('@') != 1:
        return None
    local, domain = value.split('@')
    return value if local and not any(c.isspace() for c in local) and _domain(domain) else None


def plan_company(company, contacts, crm_evidence):
    """Return one company/deal plan and per-contact actions from explicit evidence."""
    out = {'company_action': 'hold', 'company_id': None, 'deal_action': 'hold',
           'deal_id': None, 'deal_properties': None, 'contacts': [], 'reasons': []}
    if not isinstance(company, dict) or not isinstance(contacts, list) or not isinstance(crm_evidence, dict):
        out['reasons'].append('invalid_evidence')
        return out
    domain = _domain(company.get('domain'))
    if (not domain or domain in _PERSONAL or any(domain == x or domain.endswith('.' + x) for x in _EXCLUDED)
            or company.get('domain_classification') != 'corporate_verified'):
        out['reasons'].append('domain_unavailable_personal_or_excluded')
        return out
    coverage = crm_evidence.get('coverage')
    if not isinstance(coverage, dict) or any(coverage.get(k) is not True for k in ('contacts','companies','deals')):
        out['reasons'].append('coverage_incomplete')
        return out
    companies = crm_evidence.get('company_matches')
    matches = crm_evidence.get('contact_matches')
    deals = crm_evidence.get('open_deals')
    if not isinstance(companies, list) or not isinstance(matches, dict) or not isinstance(deals, list):
        out['reasons'].append('invalid_evidence')
        return out
    ids = []
    for row in companies:
        if not isinstance(row, dict) or _domain(row.get('domain')) != domain or not _id(row.get('id')):
            out['reasons'].append('company_identity_conflict')
            return out
        ids.append(_id(row['id']))
    if len(set(ids)) != len(ids) or len(ids) > 1:
        out['reasons'].append('company_ambiguous')
        return out
    out['company_action'] = 'reuse' if ids else 'create'
    out['company_id'] = ids[0] if ids else None
    open_ids = []
    for deal in deals:
        if (not isinstance(deal, dict) or not _id(deal.get('id'))
                or deal.get('is_closed') is not False
                or not ids or _id(deal.get('company_id')) != ids[0]
                or not isinstance(deal.get('pipeline'), str)):
            out.update(company_action='hold', company_id=None)
            out['reasons'].append('deal_evidence_conflict')
            return out
        if deal['pipeline'] != _PIPELINE:
            out.update(company_action='hold', company_id=None)
            out['reasons'].append('open_deal_other_pipeline')
            return out
        open_ids.append(_id(deal['id']))
    if len(open_ids) != len(set(open_ids)) or len(open_ids) > 1:
        out['reasons'].append('multiple_open_deals')
        return out
    out['deal_action'] = 'reuse' if open_ids else 'create'
    out['deal_id'] = open_ids[0] if open_ids else None
    out['deal_properties'] = None if open_ids else deepcopy(_NEW_DEAL)
    seen = set()
    for row in contacts:
        ident = row.get('id') if isinstance(row, dict) else None
        address = _email(row.get('email')) if isinstance(row, dict) else None
        item = {'source_id': ident, 'action': 'hold', 'contact_id': None, 'reason': None}
        if not isinstance(ident, str) or not ident or ident in seen or not address or address.split('@')[1] != domain:
            item['reason'] = 'contact_identity_unverified'
        else:
            seen.add(ident)
            found = matches.get(address)
            if not isinstance(found, list) or any(not isinstance(v, dict) or not _id(v.get('id')) for v in found):
                item['reason'] = 'contact_lookup_incomplete'
            elif len(found) > 1:
                item['reason'] = 'contact_ambiguous'
            elif found:
                candidate = found[0]
                linked = candidate.get('company_ids')
                if (candidate.get('company_associations_complete') is not True
                        or not isinstance(linked, list) or any(not _id(v) for v in linked)
                        or any(_id(v) != out['company_id'] for v in linked)):
                    item['reason'] = 'contact_company_unverified_or_conflicting'
                else:
                    item.update(action='reuse', contact_id=_id(candidate['id']))
            else:
                item['action'] = 'create'
        out['contacts'].append(item)
    crm_ids = [item['contact_id'] for item in out['contacts'] if item['contact_id']]
    source_ids = [row.get('id') if isinstance(row, dict) and isinstance(row.get('id'), str) else None for row in contacts]
    addresses = [_email(row.get('email')) if isinstance(row, dict) else None for row in contacts]
    if (len(crm_ids) != len(set(crm_ids)) or len(source_ids) != len(set(source_ids))
            or len(addresses) != len(set(addresses))
            or not any(item['action'] != 'hold' for item in out['contacts'])):
        out.update(company_action='hold', company_id=None, deal_action='hold', deal_id=None, deal_properties=None)
        out['reasons'].append('contacts_ambiguous_or_unavailable')
        for item in out['contacts']:
            item.update(action='hold', contact_id=None, reason='contacts_ambiguous_or_unavailable')
    return out


def plan_snapshot(snapshot, crm_evidence):
    """Summarize observed rows without treating incomplete snapshots as a cohort."""
    rows = snapshot.get('leads') if isinstance(snapshot, dict) else None
    observed = len(rows) if isinstance(rows, list) else None
    result = {'status': 'held', 'total_leads': None, 'observed_leads': observed,
              'held_leads': observed, 'reason': 'apollo_incomplete'}
    if not isinstance(snapshot, dict):
        return result
    if snapshot.get('campaign_id') != 'BR-APOLLO-01':
        result.update(reason='source_scope_mismatch', observed_leads=None, held_leads=None)
        return result
    from .external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE
    if (snapshot.get('account_id'), snapshot.get('external_campaign_id')) != (APOLLO_TEAM, APOLLO_SEQUENCE):
        result.update(reason='apollo_scope_unverified', observed_leads=None, held_leads=None)
        return result
    coverage = snapshot.get('coverage')
    if (snapshot.get('status') != 'ok' or not isinstance(coverage, dict)
            or coverage.get('complete') is not True):
        result['provenance_status'] = 'unverified'
        retained = coverage.get('retained_observed_at') if isinstance(coverage, dict) else None
        try:
            from .external_email_import import timestamp
            result['retained_observed_at'] = timestamp(retained) if retained else None
        except Exception:
            result['retained_observed_at'] = None
        from .external_email_import import timestamp
        def source_time(value):
            try:
                return timestamp(value) if isinstance(value, str) else None
            except Exception:
                return None
        cov = coverage if isinstance(coverage, dict) else {}
        result.update(enumeration_finished=cov.get('contacts_complete') if type(cov.get('contacts_complete')) is bool else None,
                      messages_enumerated=cov.get('messages_complete') if type(cov.get('messages_complete')) is bool else None,
                      atomic_snapshot=cov.get('atomic_snapshot') if type(cov.get('atomic_snapshot')) is bool else None,
                      contacts_observed_at=source_time(cov.get('contacts_observed_at')),
                      messages_observed_at=source_time(cov.get('messages_observed_at')),
                      limitations=[x for x in cov.get('reasons', []) if isinstance(x, str)]
                          if isinstance(cov.get('reasons'), list) else [],
                      stale=snapshot.get('stale') if type(snapshot.get('stale')) is bool else None, rows=[])
        for row in rows if isinstance(rows, list) else []:
            row = row if isinstance(row, dict) else {}
            member = row.get('membership')
            member_time = source_time(row.get('membership_observed_at'))
            snapshot_time = source_time(snapshot.get('observed_at'))
            member_observed = (isinstance(member, dict)
                and member.get('emailer_campaign_id') == APOLLO_SEQUENCE
                and member_time is not None and snapshot_time is not None and member_time <= snapshot_time)
            result['rows'].append({
                'source_id': row.get('id') if isinstance(row.get('id'), str) else None,
                'name': row.get('name') if isinstance(row.get('name'), str) else None,
                'company': row.get('company') if isinstance(row.get('company'), str) else None,
                'email': _email(row.get('email')), 'action': 'hold',
                'reason': 'apollo_incomplete', 'membership_status': 'observed' if member_observed else 'unverified',
                'membership_observed_at': member_time, 'crm_binding': 'unverified'})
        return result
    try:
        from .apollo_hubspot_sync import ApolloHubSpotSync
        leads = ApolloHubSpotSync._scope(snapshot)
    except Exception:
        result.update(reason='apollo_scope_unverified', observed_leads=None, held_leads=None)
        return result
    from .external_email_import import timestamp
    def source_time(value):
        try:
            return timestamp(value) if isinstance(value, str) else None
        except Exception:
            return None
    def source_row(lead):
        member = lead.get('membership')
        member_time = source_time(lead.get('membership_observed_at'))
        snapshot_time = source_time(snapshot.get('observed_at'))
        member_observed = (isinstance(member, dict) and
            member.get('emailer_campaign_id') == APOLLO_SEQUENCE and member_time is not None
            and snapshot_time is not None and member_time <= snapshot_time)
        return {'source_id':lead['id'], 'action':'hold', 'reason':'company_uniqueness_unverified',
                'membership_status':'observed' if member_observed else 'unverified',
                'membership_observed_at':member_time, 'crm_binding':'unverified'}
    # Search results cannot prove global uniqueness during indexing delay.
    # No candidate or CRM absence is interpreted as permission to create.
    result.update(enumeration_finished=coverage.get('contacts_complete'),
                  messages_enumerated=coverage.get('messages_complete'),
                  contacts_observed_at=source_time(coverage.get('contacts_observed_at')),
                  messages_observed_at=source_time(coverage.get('messages_observed_at')),
                  total_leads=len(leads), observed_leads=len(leads), held_leads=len(leads),
                  reason='company_uniqueness_unverified', snapshot_observed_at=snapshot.get('observed_at'),
                  provenance_status='unverified', atomic_snapshot=(coverage.get('atomic_snapshot') if type(coverage.get('atomic_snapshot')) is bool else None),
                  rows=[source_row(lead) for lead in leads])
    return result


def read_identity_candidates(candidate_read, domain, addresses):
    """Read existing bounded candidates; no assertion on associations or deals."""
    result = {'coverage_complete': False, 'companies': None, 'contacts': None,
              'reason': 'identity_read_unavailable'}
    domain = _domain(domain)
    if (not domain or domain in _PERSONAL or
            any(domain == x or domain.endswith('.' + x) for x in _EXCLUDED) or
            not isinstance(addresses, list) or not addresses or len(addresses) > 10):
        return result
    normalized = [_email(value) for value in addresses]
    if any(not value or value.split('@')[1] != domain for value in normalized):
        return result
    if len(normalized) != len(set(normalized)) or not callable(candidate_read):
        return result
    try:
        companies = candidate_read('companies', {'domain': domain})
        contacts = {email: candidate_read('contacts', {'email': email}) for email in normalized}
        if not isinstance(companies, list) or any(not isinstance(rows, list) for rows in contacts.values()):
            return result
    except Exception:
        return result
    return {'coverage_complete': False, 'companies': companies, 'contacts': contacts,
            'reason': 'candidate_coverage_unverified'}


def readonly_hubspot_request(request):
    """Constrain the existing HubSpot transport to known read-only endpoints."""
    if not callable(request):
        raise ValueError('read_transport_required')

    def read(method, path, body, headers, timeout):
        get_allowed = isinstance(path, str) and (
            path in {'/account-info/v3/details', '/crm/v3/pipelines/deals'}
            or re.fullmatch(r'/crm/v3/properties/(?:contacts|companies|deals)', path)
            or re.fullmatch(r'/crm/v3/objects/(?:contacts|companies|deals)/[1-9][0-9]*(?:\?[^#]*)?', path)
            or re.fullmatch(r'/crm/v4/objects/(?:contacts|companies)/[1-9][0-9]*/associations/(?:companies|deals)\?limit=500', path))
        if method == 'GET' and isinstance(path, str):
            parsed = urlsplit(path)
            prefix = '/crm/v3/objects/contacts/'
            if not parsed.scheme and not parsed.netloc and not parsed.fragment and parsed.path.startswith(prefix):
                segment = parsed.path[len(prefix):]
                address = unquote(segment)
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                except ValueError:
                    query = {}
                if (_email(address) == address and quote(address, safe='') == segment
                        and set(query) == {'properties', 'idProperty'}
                        and query.get('idProperty') == ['email']
                        and len(query.get('properties', [])) == 1
                        and re.fullmatch(r'[a-zA-Z0-9_,]+', query['properties'][0])):
                    get_allowed = True
        post_allowed = path in {'/crm/v3/objects/contacts/search', '/crm/v3/objects/companies/search'}
        if not ((method == 'GET' and body is None and get_allowed)
                or (method == 'POST' and post_allowed and isinstance(body, dict))):
            raise ValueError('hubspot_read_only')
        return request(method, path, body, headers, timeout)
    return read


def readonly_candidate_reader(original):
    """Bind existing candidate searches to an independently guarded transport."""
    from .hubspot_executor import HubSpotHTTP
    from .new_journey_import import JourneyImportHTTP
    if not isinstance(original, HubSpotHTTP):
        raise ValueError('read_transport_required')
    guarded = HubSpotHTTP(original._loader, request=readonly_hubspot_request(original._request),
                         timeout=original.timeout, unique_properties=original.unique_properties,
                         creation_mode=original.creation_mode)
    return JourneyImportHTTP(guarded).candidates


def readonly_association_evidence(original, email):
    """Observe one verified contact's associations; never infer Hunter pipeline."""
    from .hubspot_executor import HubSpotHTTP
    from .new_journey_hubspot import HubSpotEligibilityRead
    empty = {'status': 'held', 'coverage': {'identity': False, 'company': False, 'deals': False},
             'hunter_pipeline_verified': False, 'evidence': None}
    if not isinstance(original, HubSpotHTTP):
        return empty
    try:
        guarded = HubSpotHTTP(original._loader, request=readonly_hubspot_request(original._request),
                             timeout=original.timeout, unique_properties=original.unique_properties,
                             creation_mode=original.creation_mode)
        evidence = HubSpotEligibilityRead(guarded).lookup(email)
        flags = evidence.get('coverage', {})
        if not isinstance(flags, dict):
            return empty
        coverage = {key: flags.get(key) is True for key in ('identity', 'company', 'deals')}
        return {'status': 'observed' if all(coverage.values()) else 'held',
                'coverage': coverage, 'hunter_pipeline_verified': False, 'evidence': evidence}
    except Exception:
        return empty


def readonly_deal_pipeline_evidence(original, association):
    """Read deal status by observed IDs; this does not prove company-deal binding."""
    from .hubspot_executor import HubSpotHTTP
    empty = {'status': 'held', 'deals': None, 'company_binding_complete': False}
    if (not isinstance(original, HubSpotHTTP) or not isinstance(association, dict)
            or association.get('status') != 'observed'
            or not isinstance(association.get('coverage'), dict)
            or any(association['coverage'].get(k) is not True for k in ('identity', 'company', 'deals'))):
        return empty
    evidence = association.get('evidence')
    if not isinstance(evidence, dict) or not isinstance(evidence.get('deals'), list):
        return empty
    ids = [_id(row.get('id')) if isinstance(row, dict) else None for row in evidence['deals']]
    if any(not ident for ident in ids) or len(set(ids)) != len(ids):
        return empty
    try:
        guarded = HubSpotHTTP(original._loader, request=readonly_hubspot_request(original._request),
                             timeout=original.timeout, unique_properties=original.unique_properties,
                             creation_mode=original.creation_mode)
        deals = []
        for ident in ids:
            row = guarded.read('deals', ident, {'pipeline', 'dealstage', 'hs_is_closed'})
            if not isinstance(row, dict) or row.get('id') != ident or row.get('archived') is not False:
                return empty
            props = row.get('properties')
            if not isinstance(props, dict) or not all(isinstance(props.get(k), str) and props[k]
                        for k in ('pipeline', 'dealstage', 'hs_is_closed')):
                return empty
            if props['hs_is_closed'] not in ('true', 'false'):
                return empty
            deals.append({'id': ident, 'pipeline': props['pipeline'], 'stage': props['dealstage'],
                          'is_closed': props['hs_is_closed'] == 'true'})
        return {'status': 'observed', 'deals': deals, 'company_binding_complete': False}
    except Exception:
        return empty


def readonly_company_deals(original, company_id, expected_domain):
    """Observe one company's complete first association page or retain as incomplete."""
    from .hubspot_executor import HubSpotHTTP
    from .new_journey_hubspot import HubSpotEligibilityRead
    from .new_journey_import import _identities
    empty = {'status': 'held', 'company_id': None, 'deals': None}
    ident = _id(company_id)
    domain = _domain(expected_domain)
    if not ident or not domain or not isinstance(original, HubSpotHTTP):
        return empty
    try:
        guarded = HubSpotHTTP(original._loader, request=readonly_hubspot_request(original._request),
                             timeout=original.timeout, unique_properties=original.unique_properties,
                             creation_mode=original.creation_mode)
        guarded.portal()
        company = guarded.read('companies', ident, {'domain','hs_additional_domains'})
        if not isinstance(company, dict) or company.get('id') != ident or domain not in _identities('companies', company):
            return empty
        raw = guarded._call('GET', '/crm/v4/objects/companies/' + ident + '/associations/deals?limit=500')
        ids, complete = HubSpotEligibilityRead._associations({'deals': raw}, 'deals', v4=True)
        if not complete:
            return empty
        observation = {'status':'observed', 'coverage':{'identity':True,'company':True,'deals':True},
                       'evidence':{'deals':[{'id': value} for value in ids]}}
        read = readonly_deal_pipeline_evidence(guarded, observation)
        if read['status'] != 'observed':
            return empty
        return {'status':'observed','company_id':ident,'deals':read['deals']}
    except Exception:
        return empty



def readonly_pipeline_catalog(original):
    """Observe a complete Hunter stage catalog; do not imply company identity."""
    from .hubspot_executor import HubSpotHTTP
    from .new_journey_hubspot import HubSpotEligibilityRead
    empty = {'status': 'held', 'pipeline_id': None, 'stages': None}
    if not isinstance(original, HubSpotHTTP):
        return empty
    try:
        port = HubSpotHTTP(original._loader, request=readonly_hubspot_request(original._request),
                           timeout=original.timeout, unique_properties=original.unique_properties,
                           creation_mode=original.creation_mode)
        port.portal()
        raw = port._call('GET', '/crm/v3/pipelines/deals')
        if not HubSpotEligibilityRead._terminal(raw) or not isinstance(raw.get('results'), list):
            return empty
        rows = raw['results']
        hunters = [row for row in rows if isinstance(row, dict) and row.get('id') == _PIPELINE]
        if len(hunters) != 1 or hunters[0].get('archived') is not False or not isinstance(hunters[0].get('stages'), list):
            return empty
        stages = hunters[0]['stages']
        if any(not isinstance(row, dict) or not _id(row.get('id')) or type(row.get('archived')) is not bool
               for row in stages):
            return empty
        all_ids = [_id(row['id']) for row in stages]
        if len(all_ids) != len(set(all_ids)):
            return empty
        active = [_id(row['id']) for row in stages if row['archived'] is False]
        if _NEW_DEAL['dealstage'] not in active:
            return empty
        return {'status': 'observed', 'pipeline_id': _PIPELINE, 'stages': sorted(active)}
    except Exception:
        return empty


def validate_company_deals(company, catalog):
    """Validate observed stages without claiming global company uniqueness."""
    held = {'status':'held', 'company_uniqueness_verified':False}
    if (not isinstance(company, dict) or company.get('status') != 'observed'
            or not _id(company.get('company_id')) or not isinstance(company.get('deals'), list)
            or not isinstance(catalog, dict) or catalog.get('status') != 'observed'
            or catalog.get('pipeline_id') != _PIPELINE or not isinstance(catalog.get('stages'), list)):
        return held
    stages = catalog['stages']
    if not stages or any(not _id(s) for s in stages) or len(set(stages)) != len(stages):
        return held
    ids = []
    for deal in company['deals']:
        if (not isinstance(deal, dict) or not _id(deal.get('id'))
                or deal.get('pipeline') != _PIPELINE or deal.get('stage') not in stages
                or type(deal.get('is_closed')) is not bool):
            return held
        ids.append(_id(deal['id']))
    if len(ids) != len(set(ids)):
        return held
    return {'status':'observed', 'company_uniqueness_verified':False}
