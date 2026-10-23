"""Bounded HubSpot evidence reads for New Journey, without policy or writes.

Create one reader per checker invocation. ``deadline`` is an absolute monotonic
deadline, capped at 15 seconds. Missing/partial evidence is returned explicitly;
the policy composer owns eligibility, freshness and approval decisions.
``status=ok`` describes the read, not eligibility. Boolean coverage covers record
scope; ``property_coverage`` separately identifies verified schema/value pairs.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
import math
import re
from threading import RLock
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .hubspot_executor import HubSpotHTTP
from .integration_hubspot import PORTAL_ID
from .models import payload_hash


CONTACT_PROPERTIES = frozenset({'email', 'hs_additional_emails', 'lifecyclestage',
    'opt_out', 'hs_email_optout', 'hs_linkedin_url', 'linkedin_url', 'tipo_do_lead',
    'thunder_outreach_risk', 'campanha_ativa', 'executor'})
COMPANY_PROPERTIES = frozenset({'name', 'domain', 'country', 'hs_country_code',
    'lifecyclestage', 'thunder_icp_status', 'thunder_icp_sector',
    'thunder_icp_exclusion_reason', 'thunder_annual_revenue_estimate',
    'thunder_last_enriched_at', 'thunder_enrichment_source', 'thunder_is_customer',
    'thunder_suppressed'})
DEAL_PROPERTIES = frozenset({'hs_is_closed', 'dealname'})
PROPERTIES = {'contacts': CONTACT_PROPERTIES, 'companies': COMPANY_PROPERTIES,
              'deals': DEAL_PROPERTIES}
ENUMS = {
    'thunder_outreach_risk': {'low', 'medium', 'high', 'blocked'},
    'thunder_icp_status': {'qualified', 'disqualified', 'pending_review', 'research_required'},
    'thunder_icp_sector': {'distribuicao_b2b', 'industria_b2b', 'locacao_equipamentos',
        'facilities', 'saude_b2b', 'logistica', 'servicos_tecnicos', 'alimentacao_b2b', 'outro'},
    'thunder_icp_exclusion_reason': {'simples_nacional', 'franquia', 'b2c', 'setor_publico',
        'startup', 'sem_decisor', 'fora_porte', 'fora_setor'},
}
BOOLS = {'opt_out', 'hs_email_optout', 'thunder_is_customer', 'thunder_suppressed', 'hs_is_closed'}
_ID = re.compile(r'[1-9][0-9]{0,29}\Z')
_EMAIL = re.compile(r'[^\s@<>*]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z')


class HubSpotEligibilityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise HubSpotEligibilityError(code)


def _text(value, limit=4000):
    return (isinstance(value, str) and len(value) <= limit
        and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _email(value):
    return value.strip().lower() if _text(value, 254) and _EMAIL.fullmatch(value.strip()) else None


def _profile(value):
    if not _text(value, 500):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in ('linkedin.com', 'www.linkedin.com')
                or url.username or url.password or url.port is not None
                or url.query or url.fragment or not re.fullmatch(r'/in/[A-Za-z0-9][A-Za-z0-9_.-]{0,199}/?', url.path)):
            return None
        return 'https://www.linkedin.com' + url.path.rstrip('/').lower()
    except ValueError:
        return None


def _domain(value):
    if not _text(value, 253):
        return None
    value = value.strip().lower().rstrip('.').removeprefix('www.')
    return value if (re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}', value)
        and '..' not in value and all(0 < len(part) <= 63 and not part.startswith('-')
                                     and not part.endswith('-') for part in value.split('.'))) else None


class _ReadPort(HubSpotHTTP):
    def __init__(self, original, owner):
        self.owner = owner
        super().__init__(original._loader, request=owner.request, timeout=min(4, original.timeout))

    def _call(self, method, path, body=None):
        self.owner._validate_request(method, path, body)
        return super()._call(method, path, body)


class HubSpotEligibilityRead:
    def __init__(self, original, deadline=None, max_requests=50):
        _require(isinstance(original, HubSpotHTTP), 'checks_hubspot_configuration_invalid')
        now = time.monotonic()
        _require(deadline is None or type(deadline) in (int, float) and math.isfinite(deadline),
                 'checks_hubspot_configuration_invalid')
        _require(type(max_requests) is int and 1 <= max_requests <= 50,
                 'checks_hubspot_configuration_invalid')
        _require(type(original.timeout) in (int, float)
            and math.isfinite(original.timeout) and original.timeout > 0,
            'checks_hubspot_configuration_invalid')
        self.deadline = min(now + 15, deadline) if deadline is not None else now + 15
        self.maxrequests, self.requests, self.count = max_requests, 0, 0
        self._transport = original._request
        self._lock = RLock()
        self.port = _ReadPort(original, self)
        self._portal_verified = False
        self._schemas, self._schema_complete, self._schema_valid = {}, {}, {}
        self._cache, self._owners = {}, {}

    def _validate_request(self, method, path, body):
        _require(isinstance(path, str) and len(path) <= 4000, 'checks_hubspot_read_blocked')
        try:
            parsed = urlsplit(path)
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise HubSpotEligibilityError('checks_hubspot_read_blocked') from None
        allowed = False
        if not parsed.scheme and not parsed.netloc and not parsed.fragment:
            if method == 'GET' and body is None:
                allowed = path == '/account-info/v3/details' or path in {
                    '/crm/v3/properties/' + kind for kind in PROPERTIES}
                match = re.fullmatch(r'/crm/v3/objects/(contacts|companies|deals)/[1-9][0-9]{0,29}', parsed.path)
                if match:
                    kind = match[1]
                    fields = query.get('properties', [])
                    requested = fields[0].split(',') if len(fields) == 1 else []
                    expected = {'properties'} if kind == 'deals' else {'properties', 'associations'}
                    associations = 'companies,deals' if kind == 'contacts' else 'deals'
                    allowed = (set(query) == expected and bool(requested)
                        and requested == sorted(set(requested)) and set(requested) <= PROPERTIES[kind]
                        and (kind == 'deals' or query.get('associations') == [associations]))
            elif method == 'POST' and path == '/crm/v3/objects/contacts/search':
                try:
                    groups = body['filterGroups']
                    filters = groups[0]['filters']
                    fields = body['properties']
                    allowed = (isinstance(body, dict) and set(body) == {'filterGroups', 'properties', 'limit'}
                        and type(body['limit']) is int and body['limit'] == 2
                        and isinstance(groups, list) and len(groups) == 1
                        and set(groups[0]) == {'filters'} and isinstance(filters, list) and len(filters) == 1
                        and set(filters[0]) == {'propertyName', 'operator', 'value'}
                        and filters[0]['propertyName'] == 'email' and filters[0]['operator'] == 'EQ'
                        and _email(filters[0]['value']) is not None
                        and _email(filters[0]['value']) == filters[0]['value']
                        and isinstance(fields, list) and bool(fields)
                        and all(isinstance(f, str) for f in fields)
                        and fields == sorted(set(fields)) and set(fields) <= CONTACT_PROPERTIES)
                except (KeyError, IndexError, TypeError):
                    allowed = False
        _require(allowed, 'checks_hubspot_read_blocked')

    def request(self, method, path, body, headers, timeout):
        with self._lock:
            self._validate_request(method, path, body)
            remaining = self.deadline - time.monotonic()
            _require(remaining > 0 and self.requests < self.maxrequests, 'checks_hubspot_budget_exhausted')
            self.requests += 1
            result = self._transport(method, path, body, headers, min(timeout, 4, remaining))
            _require(time.monotonic() <= self.deadline, 'checks_hubspot_budget_exhausted')
            return result

    def _clean(self, value, limit=4000):
        return _text(value, limit) and (not self.port._token or self.port._token not in value)

    @staticmethod
    def _terminal(value):
        paging = value.get('paging', {})
        return (isinstance(paging, Mapping)
                and ('next' not in paging or isinstance(paging['next'], Mapping))
                and not paging.get('next'))

    def _schema(self, kind):
        if kind in self._schemas:
            return self._schemas[kind]
        result, valid, complete = {}, {}, False
        try:
            raw = self.port._call('GET', '/crm/v3/properties/' + kind)
            rows = raw.get('results')
            _require(isinstance(rows, list) and len(rows) <= 5000 and self._terminal(raw),
                     'checks_hubspot_schema_incomplete')
            complete = True
            seen = set()
            for item in rows:
                if not isinstance(item, Mapping):
                    continue
                name = item.get('name')
                if not self._clean(name, 200) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,199}', name):
                    continue
                if name in seen:
                    valid[name] = False
                    result.pop(name, None)
                    continue
                seen.add(name)
                if item.get('archived') is True:
                    continue
                typ = item.get('type')
                if typ not in {'string', 'bool', 'enumeration', 'number', 'date', 'datetime'}:
                    valid[name] = False
                    continue
                metadata = {'name': name, 'type': typ, 'options': [], 'description': None}
                options = item.get('options', [])
                good = True
                if typ == 'enumeration':
                    good = isinstance(options, list) and len(options) <= 2000
                    if good:
                        for option in options:
                            if (not isinstance(option, Mapping) or not self._clean(option.get('value'), 200)):
                                good = False
                                break
                            if option.get('hidden') is not True:
                                metadata['options'].append(option['value'])
                        good &= len(set(metadata['options'])) == len(metadata['options'])
                if self._clean(item.get('description')):
                    metadata['description'] = item['description']
                if name in BOOLS:
                    good &= typ == 'bool'
                elif name in ENUMS:
                    good &= typ == 'enumeration' and ENUMS[name] <= set(metadata['options'])
                elif name == 'lifecyclestage':
                    good &= typ == 'enumeration' and 'customer' in metadata['options']
                elif name == 'thunder_annual_revenue_estimate':
                    description = metadata['description'] or ''
                    good &= (typ == 'number' and bool(re.search(r'\b(?:anual|annual)\b', description, re.I))
                             and bool(re.search(r'R\$|\bBRL\b|\breais\b', description, re.I)))
                elif name == 'thunder_last_enriched_at':
                    good &= typ == 'date'
                elif name != 'tipo_do_lead':
                    good &= typ == 'string'
                result[name], valid[name] = metadata, bool(good)
        except Exception:
            complete = False
        self._schemas[kind], self._schema_complete[kind], self._schema_valid[kind] = result, complete, valid
        return result

    def _property_complete(self, kind, key, value):
        if not self._schema_complete.get(kind) or not self._schema_valid.get(kind, {}).get(key):
            return False
        schema = self._schemas[kind][key]
        if (kind == 'contacts' and key in ('hs_additional_emails', 'campanha_ativa', 'executor')
                and schema['type'] == 'string' and value in ('', None)):
            return True
        if key in BOOLS:
            return type(value) is bool or type(value) is str and value in ('true', 'false')
        if value is None or value == '' or type(value) is bool:
            return False
        if schema['type'] == 'number':
            try:
                return type(value) in (str, int, float) and math.isfinite(float(value))
            except (ValueError, OverflowError):
                return False
        if schema['type'] == 'date':
            try:
                if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    date.fromisoformat(value)
                    return True
                return (type(value) is int and value >= 0 or isinstance(value, str)
                        and bool(re.fullmatch(r'[0-9]{1,15}', value)))
            except ValueError:
                return False
        if not self._clean(value):
            return False
        if schema['type'] == 'enumeration':
            return value in schema['options']
        return bool(value.strip())

    def _record(self, kind, ident):
        # Read positive evidence even if schema is unavailable. Its property
        # completeness stays false; no missing metadata becomes a clean bill.
        query = {'properties': ','.join(sorted(PROPERTIES[kind]))}
        if kind != 'deals':
            query['associations'] = 'companies,deals' if kind == 'contacts' else 'deals'
        raw = self.port._call('GET', '/crm/v3/objects/' + kind + '/' + ident + '?' + urlencode(query))
        _require(raw.get('id') == ident and raw.get('archived') is False
            and isinstance(raw.get('properties'), Mapping), 'checks_hubspot_record_invalid')
        props = {}
        for key in PROPERTIES[kind] & set(raw['properties']):
            value = raw['properties'][key]
            if value is None or type(value) in (bool, int) or type(value) is float and math.isfinite(value):
                props[key] = value
            elif self._clean(value):
                props[key] = value
        associations = raw.get('associations')
        return {'id': ident, 'archived': False, 'properties': props}, associations

    @staticmethod
    def _associations(associations, kind):
        if not isinstance(associations, Mapping) or not isinstance(associations.get(kind), Mapping):
            return [], False
        group = associations[kind]
        rows = group.get('results')
        if not isinstance(rows, list) or len(rows) > 1000:
            return [], False
        ids, complete = [], HubSpotEligibilityRead._terminal(group)
        for row in rows:
            ident = row.get('id') if isinstance(row, Mapping) else None
            if not isinstance(ident, str) or not _ID.fullmatch(ident):
                complete = False
            elif ident in ids:
                complete = False
            else:
                ids.append(ident)
        total = group.get('total')
        if total is not None and (type(total) is not int or total != len(ids)):
            complete = False
        return ids, complete

    @staticmethod
    def _empty():
        return {'status': 'unknown', 'reason': 'checks_hubspot_identity_unresolved',
            'contact': None, 'company': None, 'deals': [],
            'identity': {'verified': False, 'emails': [], 'linkedin_urls': [],
                         'contact_id': None, 'company_id': None, 'domain': None},
            'schema': {kind: {} for kind in PROPERTIES},
            'coverage': {'identity': {'complete': False, 'properties': {}},
                'company': {'complete': False, 'properties': {}},
                'deals': {'complete': False, 'properties': {}, 'expected_ids': [], 'resolved_ids': []},
                'schema': {kind: False for kind in PROPERTIES}},
            'source_ref': None}

    def _finish(self, result):
        result = deepcopy(result)
        result['schema'] = deepcopy(self._schemas)
        for kind in PROPERTIES:
            result['schema'].setdefault(kind, {})
            result['coverage']['schema'][kind] = self._schema_complete.get(kind, False)
        details = result['coverage']
        result['coverage'] = {key: details[key]['complete'] for key in ('identity', 'company', 'deals')}
        result['property_coverage'] = {'contacts': details['identity']['properties'],
            'companies': details['company']['properties'], 'deals': details['deals']['properties']}
        result['schema_coverage'] = details['schema']
        result['association_coverage'] = {key: details['deals'][key] for key in ('expected_ids', 'resolved_ids')}
        result['source_ref'] = 'hubspot:' + PORTAL_ID + ':eligibility:' + payload_hash({
            key: value for key, value in result.items() if key != 'source_ref'})
        return result

    def lookup(self, email, contact_id=None, profile=None):
        with self._lock:
            address = _email(email) if email else None
            linkedin = _profile(profile) if profile else None
            result = self._empty()
            if ((email and address is None) or (profile and linkedin is None)
                    or contact_id is not None and (not isinstance(contact_id, str) or not _ID.fullmatch(contact_id))
                    or address is None and contact_id is None):
                result['reason'] = 'checks_hubspot_input_invalid'
                return self._finish(result)
            key = address, contact_id, linkedin
            if key in self._cache:
                cached = deepcopy(self._cache[key])
                if any(len(self._owners.get(alias, set())) > 1 for alias in
                       cached['identity']['emails'] + cached['identity']['linkedin_urls']):
                    cached['identity']['verified'] = cached['coverage']['identity']['complete'] = False
                    cached.update(status='unknown', reason='checks_hubspot_identity_ambiguous')
                return self._finish(cached)
            if self.count >= 10 or self.requests >= self.maxrequests or time.monotonic() >= self.deadline:
                result['reason'] = 'checks_hubspot_budget_exhausted'
                return self._finish(result)
            self.count += 1
            try:
                if not self._portal_verified:
                    portal = self.port._call('GET', '/account-info/v3/details')
                    _require(str(portal.get('portalId')) == PORTAL_ID, 'checks_hubspot_portal_mismatch')
                    self._portal_verified = True
                for kind in PROPERTIES:
                    self._schema(kind)
                ident = contact_id
                if ident is None:
                    found = self.port._call('POST', '/crm/v3/objects/contacts/search', {
                        'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'EQ', 'value': address}]}],
                        'properties': ['email'], 'limit': 2})
                    rows = found.get('results')
                    if isinstance(rows, list) and not rows and found.get('total') == 0 and self._terminal(found):
                        raise HubSpotEligibilityError('checks_hubspot_contact_not_found')
                    _require(isinstance(rows, list) and len(rows) == 1
                        and type(found.get('total')) is int and found['total'] == 1 and self._terminal(found)
                        and isinstance(rows[0], Mapping) and isinstance(rows[0].get('id'), str)
                        and _ID.fullmatch(rows[0]['id']), 'checks_hubspot_identity_ambiguous')
                    ident = rows[0]['id']
                contact, associations = self._record('contacts', ident)
                result['contact'] = contact
                properties = contact['properties']
                property_coverage = {name: name in properties and self._property_complete('contacts', name, properties.get(name))
                                     for name in CONTACT_PROPERTIES}
                result['coverage']['identity']['properties'] = property_coverage
                emails, profiles, valid = [], [], True
                primary = _email(properties.get('email'))
                if primary and property_coverage['email']:
                    emails.append(primary)
                aliases = properties.get('hs_additional_emails')
                valid &= property_coverage['hs_additional_emails']
                if aliases:
                    if not property_coverage['hs_additional_emails'] or len(aliases.split(';')) > 50:
                        valid = False
                    else:
                        for alias in aliases.split(';'):
                            parsed = _email(alias)
                            if parsed is None:
                                valid = False
                            elif parsed not in emails:
                                emails.append(parsed)
                for field in ('hs_linkedin_url', 'linkedin_url'):
                    value = properties.get(field)
                    if value:
                        parsed = _profile(value)
                        if parsed is None or not property_coverage[field]:
                            valid = False
                        elif parsed not in profiles:
                            profiles.append(parsed)
                valid &= (len(profiles) <= 1 and (address is None or address in emails)
                          and (linkedin is None or linkedin in profiles))
                for alias in emails + profiles:
                    self._owners.setdefault(alias, set()).add(ident)
                    valid &= len(self._owners[alias]) == 1
                result['identity'].update(contact_id=ident, emails=sorted(emails), linkedin_urls=sorted(profiles),
                                          verified=bool(valid))
                result['coverage']['identity']['complete'] = bool(valid)
                company_ids, company_complete = self._associations(associations, 'companies')
                contact_deals, contact_deals_complete = self._associations(associations, 'deals')
                result['coverage']['deals']['expected_ids'] = sorted(contact_deals)
                result.update(status='partial' if valid else 'unknown',
                    reason='checks_hubspot_company_unresolved' if valid else 'checks_hubspot_identity_mismatch')
                company_deals, company_deals_complete = [], False
                # A directly observed contact can expose a positive deal blocker
                # despite absent identity metadata. It cannot prove absence.
                if valid and company_complete and len(company_ids) == 1:
                    try:
                        company, company_associations = self._record('companies', company_ids[0])
                        result['company'] = company
                        domain = _domain(company['properties'].get('domain'))
                        property_flags = {name: self._property_complete('companies', name, company['properties'].get(name))
                                          for name in COMPANY_PROPERTIES}
                        property_flags['domain'] &= domain is not None
                        result['coverage']['company'].update(complete=bool(domain and property_flags['domain']),
                                                             properties=property_flags)
                        if result['coverage']['company']['complete']:
                            result['identity'].update(company_id=company['id'], domain=domain)
                        company_deals, company_deals_complete = self._associations(company_associations, 'deals')
                    except Exception:
                        result['reason'] = 'checks_hubspot_company_unavailable'
                elif len(company_ids) > 1:
                    result['identity']['verified'] = result['coverage']['identity']['complete'] = False
                    result.update(status='unknown', reason='checks_hubspot_company_ambiguous')
                expected = sorted(set(contact_deals) | set(company_deals))
                result['coverage']['deals']['expected_ids'] = expected
                complete = (contact_deals_complete and company_deals_complete
                            and result['coverage']['company']['complete']
                            and result['identity']['verified'])
                for did in expected:
                    try:
                        deal, _ = self._record('deals', did)
                        result['deals'].append(deal)
                        flags = {name: self._property_complete('deals', name, deal['properties'].get(name))
                                 for name in DEAL_PROPERTIES}
                        result['coverage']['deals']['properties'][did] = flags
                        complete &= flags['hs_is_closed']
                    except Exception:
                        complete = False
                result['coverage']['deals']['resolved_ids'] = [row['id'] for row in result['deals']]
                result['coverage']['deals']['complete'] = bool(complete)
                if result['coverage']['company']['complete'] and complete:
                    result.update(status='ok', reason='checks_hubspot_evidence_observed')
                elif result['coverage']['company']['complete']:
                    result.update(status='partial', reason='checks_hubspot_deals_incomplete')
            except Exception as exc:
                code = exc.code if isinstance(exc, HubSpotEligibilityError) else None
                result['reason'] = code or 'checks_hubspot_read_unavailable'
                if result['contact'] is not None:
                    result['status'] = 'partial' if result['identity']['verified'] else 'unknown'
            self._cache[key] = deepcopy(result)
            return self._finish(result)
