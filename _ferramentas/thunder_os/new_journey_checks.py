"""Bounded cold-journey checks. Positive blockers are evidence; absence is unknown.

Current structured HubSpot qualification and bounded provider reads can prove
individual checks. Freeform research and CSV fields cannot attest eligibility.
Stable source references exclude consultation timestamps for review comparison.
"""
from collections import Counter
from copy import copy
from datetime import date, datetime, timezone
import math
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit

from .cadence_readonly import PORTAL_ID, _id
from .hubspot_executor import HubSpotHTTP
from .models import payload_hash
from .today_readonly import ReadonlyRepository, _safe_url
from .new_journey_hubspot import ENUMS, HubSpotEligibilityRead
from .new_journey_membership import CrossCampaignReadOnly
from .new_journey_refusal import RegisteredRefusalRead
from .new_journey_qualification import candidate, decision as qualification_decision

KEYS = ('icp', 'exclusions', 'client', 'open_deal', 'refusal', 'membership')
PROPERTIES = {'email', 'hs_additional_emails', 'company', 'lifecyclestage',
              'hs_email_optout', 'tipo_do_lead', 'campanha_ativa', 'executor'}
DEAL_PROPERTIES = {'hs_is_closed', 'dealname'}
KINDS = ('identity', 'research', 'reply', 'cadence')
MEMBERSHIP_PROVIDERS = ('apollo', 'instantly')
POLICY = 'br-canonical-qualification-registered-refusal-apollo-instantly-v1'
SUPPORTED_ICP = '''
- Empresas B2B brasileiras, especialmente industria e distribuicao.
- Faturamento acima de R$ 10 milhoes/ano; potencial de consultoria a partir de R$ 15 mil.
- Qualificacao no HubSpot (tipo_do_lead): MQL, LEAD, SPAM, CONCORRENTE, NAO E ICP.
- Sinal nao e intencao: vaga, post ou noticia pode ter outra explicacao. Registrar a hipotese alternativa.
'''


def _complete(evidence, key):
    value = evidence.get('coverage', {}).get(key)
    return value is True or isinstance(value, dict) and value.get('complete') is True


def _property(evidence, kind, key):
    # The reader calculates these flags from current schema AND literal values.
    flat = evidence.get('property_coverage', {}).get(kind, {})
    if key in flat:
        return flat[key] is True
    section = 'identity' if kind == 'contacts' else 'company'
    flags = evidence.get('coverage', {}).get(section)
    return isinstance(flags, dict) and flags.get('properties', {}).get(key) is True


def _fresh_company(evidence, now):
    props = (evidence.get('company') or {}).get('properties', {})
    if not all(_property(evidence, 'companies', key) for key in
               ('thunder_last_enriched_at', 'thunder_enrichment_source')):
        return False
    value = props.get('thunder_last_enriched_at')
    try:
        if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            observed = date.fromisoformat(value)
        elif type(value) is int or isinstance(value, str) and value.isdigit():
            observed = datetime.fromtimestamp(int(value) / 1000, timezone.utc).date()
        else:
            return False
        return 0 <= (now.date() - observed).days <= 14
    except (ValueError, OverflowError, OSError):
        return False


def _br_icp(sheet):
    fields = [field for field in sheet.get('fields', []) if field.get('key') == 'icp']
    if sheet.get('status') != 'complete' or len(fields) != 1:
        return False
    text = fields[0].get('content', '')
    if not isinstance(text, str):
        return False
    def normalized(value):
        folded = ''.join(c for c in unicodedata.normalize('NFKD', value.lower()) if not unicodedata.combining(c))
        return ' '.join(folded.split())
    # New criteria need a supported interpretation, not an ignored extra clause.
    return normalized(text) == normalized(SUPPORTED_ICP)


class NewJourneyChecksError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _email(value):
    return value.strip().lower() if isinstance(value, str) and len(value) <= 254 and re.fullmatch(
        r'[^\s@<>*]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', value.strip()) else None


def _token(value):
    if not isinstance(value, str):
        return None
    if _email(value):
        return _email(value)
    if value.startswith('hubspot:') and _id(value[8:]):
        return value
    try:
        return value.rstrip('/').lower() if _safe_url(value) else None
    except ValueError:
        return None


def _ref(kind, rows):
    return kind + ':' + payload_hash(rows)


class _HubSpotRead:
    def __init__(self, original):
        self.deadline, self.count, self.requests = time.monotonic() + 15, 0, 0
        self.cache, self.verified = {}, False
        self.port = HubSpotHTTP(original._loader, request=self.request, timeout=min(4, original.timeout))
        self.transport = original._request

    def request(self, method, path, body, headers, timeout):
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        allowed = method == 'GET' and path == '/account-info/v3/details'
        if method == 'POST' and path == '/crm/v3/objects/contacts/search':
            try:
                filters = body['filterGroups'][0]['filters']
                allowed = (set(body) == {'filterGroups', 'properties', 'limit'} and body['limit'] == 2
                    and len(body['filterGroups']) == 1 and len(filters) == 1
                    and set(filters[0]) == {'propertyName', 'operator', 'value'}
                    and filters[0]['propertyName'] == 'email' and filters[0]['operator'] == 'EQ'
                    and bool(_email(filters[0]['value'])) and set(body['properties']) == PROPERTIES)
            except (KeyError, TypeError, IndexError):
                allowed = False
        match = re.fullmatch(r'/crm/v3/objects/(contacts|deals)/[1-9][0-9]{0,29}', parsed.path)
        if method == 'GET' and match and not parsed.netloc and not parsed.fragment:
            wanted = PROPERTIES if match[1] == 'contacts' else DEAL_PROPERTIES
            allowed = (set(query) == ({'properties', 'associations'} if match[1] == 'contacts' else {'properties'})
                and query.get('properties') == [','.join(sorted(wanted))]
                and (match[1] != 'contacts' or query.get('associations') == ['deals']))
        remaining = self.deadline - time.monotonic()
        if not allowed or remaining <= 0 or self.requests >= 41:
            raise NewJourneyChecksError('checks_hubspot_read_blocked')
        self.requests += 1
        return self.transport(method, path, body, headers, min(timeout, 4, remaining))

    def contact(self, email, ident):
        key = email, ident
        if key in self.cache:
            return self.cache[key]
        if self.count >= 10 or time.monotonic() >= self.deadline:
            return None, [], 'checks_hubspot_budget_exhausted'
        self.count += 1
        try:
            if not self.verified:
                self.port.portal()
                self.verified = True
            if not ident:
                result = self.port._call('POST', '/crm/v3/objects/contacts/search', {
                    'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'EQ', 'value': email}]}],
                    'properties': sorted(PROPERTIES), 'limit': 2})
                rows = result.get('results')
                if (not isinstance(rows, list) or len(rows) != 1 or result.get('total') != 1
                        or result.get('paging', {}).get('next')):
                    raise NewJourneyChecksError('checks_hubspot_identity_unresolved')
                ident = _id(rows[0].get('id'))
            if not ident:
                raise NewJourneyChecksError('checks_hubspot_identity_unresolved')
            contact = self.port._call('GET', '/crm/v3/objects/contacts/' + ident + '?' + urlencode({
                'properties': ','.join(sorted(PROPERTIES)), 'associations': 'deals'}))
            props = contact.get('properties', {})
            aliases = {_email(v) for v in [props.get('email'), *(props.get('hs_additional_emails') or '').split(';')]} - {None}
            if contact.get('id') != ident or contact.get('archived') is not False or email and email not in aliases:
                raise NewJourneyChecksError('checks_hubspot_identity_mismatch')
            deals = []
            associations = contact.get('associations', {}).get('deals', {})
            for row in associations.get('results', [])[:3]:
                did = _id(row.get('id'))
                if did:
                    value = self.port._call('GET', '/crm/v3/objects/deals/' + did + '?' + urlencode({
                        'properties': ','.join(sorted(DEAL_PROPERTIES))}))
                    if value.get('id') == did and value.get('archived') is False:
                        deals.append(value)
            answer = contact, deals, None
        except Exception as exc:
            code = exc.code if isinstance(exc, NewJourneyChecksError) else 'checks_hubspot_read_unavailable'
            answer = None, [], code
        self.cache[key] = answer
        return answer


class NewJourneyChecks:
    def __init__(self, service, hubspot=None, clock=None, *, membership=None, refusals=None):
        self.repo = ReadonlyRepository(service.repository.path)
        self.service = copy(service)
        self.service.repository = self.repo
        from .campaign_registry import CampaignRegistry
        self.service.campaign_registry = CampaignRegistry(self.repo)
        self.hubspot, self.clock = hubspot, clock or (lambda: datetime.now(timezone.utc))
        self.membership, self.refusals = membership, refusals

    def _canonical_checks(self, checks, evidence, sheet, now, deadline, source_keys, reviewed=None):
        """Only canonical server reads may turn an unknown check into a pass."""
        identity = evidence.get('identity', {})
        if identity.get('verified') is not True or not _complete(evidence, 'identity'):
            return
        cp = (evidence.get('contact') or {}).get('properties', {})
        company = evidence.get('company') or {}
        props = company.get('properties', {})
        source = _ref('canonical:' + POLICY, {'hubspot': evidence.get('source_ref'),
            'sheet': {'fingerprint': sheet.get('fingerprint'), 'status': sheet.get('status'),
                'fields': [{key: field.get(key) for key in ('key', 'content', 'status', 'observed_sha256', 'expected_sha256')}
                           for field in sheet.get('fields', [])]}})

        def result(key, status, reason, ref=source, scope='canonical_hubspot_qualification'):
            if checks[key]['status'] != 'blocked':
                checks[key].update(status=status, reason=reason, source=ref,
                    coverage={'complete': True, 'identity_verified': True, 'scope': scope})

        qualified = (_complete(evidence, 'company') and _fresh_company(evidence, now)
            and _property(evidence, 'companies', 'thunder_icp_status')
            and props.get('thunder_icp_status') == 'qualified')
        from .new_journey import NewJourney
        human = qualification_decision(reviewed, candidate(evidence), NewJourney._sheet_hash(sheet), now)
        human_qualified = bool(human and human['fits'] and _br_icp(sheet))
        qualified = qualified or human_qualified
        if props.get('thunder_icp_status') == 'disqualified':
            result('icp', 'blocked', 'canonical_company_disqualified')
        if human and _br_icp(sheet):
            result('icp', 'pass' if human['fits'] else 'blocked',
                   'human_reviewed_icp_fit' if human['fits'] else 'human_reviewed_icp_disqualified', human['source'])
        if props.get('thunder_is_customer') in (True, 'true') or props.get('lifecyclestage') == 'customer':
            result('client', 'blocked', 'canonical_company_is_customer')
        if props.get('thunder_suppressed') in (True, 'true') or cp.get('opt_out') in (True, 'true'):
            result('refusal', 'blocked', 'canonical_person_or_account_suppressed')
        if cp.get('thunder_outreach_risk') in ('high', 'blocked'):
            result('refusal', 'blocked', 'canonical_outreach_risk_requires_review')
        exclusion = props.get('thunder_icp_exclusion_reason')
        if isinstance(exclusion, str) and exclusion.strip():
            result('exclusions', 'blocked', 'canonical_company_excluded')
        literal = ' '.join(str(props.get(key) or '') for key in ('name', 'domain'))
        folded = ''.join(c for c in unicodedata.normalize('NFKD', literal.casefold()) if not unicodedata.combining(c))
        if re.search(r'\b(?:coca[ -]?cola|femsa|bebidas|beverages|cervejaria|yamaha)\b', folded):
            result('exclusions', 'blocked', 'canonical_fixed_exclusion')
        if re.search(r'\b(?:kawasaki|pta\s+rental|bimbo|cuetara|gonac)\b', folded):
            result('client', 'blocked', 'canonical_protected_account_requires_review')
        if qualified and _br_icp(sheet):
            required = ('hs_country_code', 'thunder_icp_sector', 'thunder_annual_revenue_estimate')
            if all(_property(evidence, 'companies', key) for key in required):
                revenue = float(props['thunder_annual_revenue_estimate'])
                fits = (props['hs_country_code'] == 'BR'
                    and props['thunder_icp_sector'] in ('industria_b2b', 'distribuicao_b2b')
                    and math.isfinite(revenue) and revenue > 10_000_000)
                result('icp', 'pass' if fits else 'blocked',
                       'canonical_company_icp_confirmed' if fits else 'canonical_company_outside_campaign_icp')
        non_customer = (qualified and _property(evidence, 'companies', 'name')
            and _property(evidence, 'companies', 'thunder_is_customer')
            and props.get('thunder_is_customer') in (False, 'false')
            and _property(evidence, 'companies', 'lifecyclestage')
            and props.get('lifecyclestage') != 'customer')
        if non_customer:
            result('client', 'pass', 'canonical_company_explicit_non_customer')
        exclusion_schema = evidence.get('schema', {}).get('companies', {}).get('thunder_icp_exclusion_reason', {})
        if (qualified and non_customer and checks['icp']['status'] == 'pass'
                and cp.get('tipo_do_lead') in ('MQL', 'LEAD')
                and _property(evidence, 'contacts', 'tipo_do_lead')
                and exclusion_schema.get('type') == 'enumeration'
                and ENUMS['thunder_icp_exclusion_reason'] <= set(exclusion_schema.get('options', []))
                and 'thunder_icp_exclusion_reason' in props and exclusion in ('', None)):
            result('exclusions', 'pass', 'canonical_qualification_no_registered_exclusion')
        deals = evidence.get('deals', [])
        for deal in deals:
            if deal.get('properties', {}).get('hs_is_closed') in (False, 'false'):
                result('open_deal', 'blocked', 'canonical_account_open_deal')
        if (_complete(evidence, 'company') and _complete(evidence, 'deals')
                and all(deal.get('properties', {}).get('hs_is_closed') in (True, 'true') for deal in deals)):
            result('open_deal', 'pass', 'canonical_contact_and_company_deals_closed', scope='all_associated_contact_and_company_deals')
        emails, profiles = identity.get('emails', []), identity.get('linkedin_urls', [])
        if (isinstance(self.refusals, RegisteredRefusalRead)
                and all(_property(evidence, 'contacts', key) and cp.get(key) in (False, 'false')
                        for key in ('opt_out', 'hs_email_optout'))
                and _property(evidence, 'companies', 'thunder_suppressed')
                and props.get('thunder_suppressed') in (False, 'false')
                and _property(evidence, 'contacts', 'thunder_outreach_risk')
                and cp.get('thunder_outreach_risk') in ('low', 'medium')):
            observation = self.refusals.lookup(emails, profiles, deadline=deadline)
            if observation['status'] == 'blocked':
                result('refusal', 'blocked', observation['reason'], observation['source_ref'])
            elif observation['status'] == 'clear' and observation['coverage']['complete']:
                result('refusal', 'pass', 'canonical_registered_refusals_clear',
                    _ref('registered_refusal', {'hubspot': source, 'ledger': observation['source_ref'],
                                               'local_reply_state': source_keys}),
                    scope='registered_hubspot_toques_and_local_reply_state_not_global_inbox')
        if ('campanha_ativa' in cp and 'executor' in cp
                and all(_property(evidence, 'contacts', key) for key in ('campanha_ativa', 'executor'))
                and cp['campanha_ativa'] in ('', None) and cp['executor'] in ('', None)
                and isinstance(self.membership, CrossCampaignReadOnly) and emails):
            observations = []
            for email in emails:
                observations.append(self.membership.lookup(email, deadline=deadline))
            occupied = next((item for item in observations if item['status'] == 'occupied'), None)
            ref = _ref('campaign_membership:' + POLICY, {'hubspot': source, 'providers': observations})
            if occupied:
                result('membership', 'blocked', occupied['reason'], ref)
            elif all(item['status'] == 'clear' and item['coverage']['complete'] for item in observations):
                result('membership', 'pass', 'canonical_apollo_instantly_no_membership', ref,
                       scope='hubspot_apollo_instantly_all_verified_email_aliases')
                bindings = []
                for observation in observations:
                    apollo = [r for r in observation['coverage'].get('results', []) if r.get('provider') == 'apollo']
                    if len(apollo) == 1 and apollo[0]['coverage']['complete']:
                        records = apollo[0].get('records', [])
                        if len(records) <= 1:
                            bindings.append({'email': observation.get('email') or (
                                records[0]['email'] if records else None),
                                'contact_id': records[0]['id'] if records else None,
                                'account_id': apollo[0]['coverage']['identity'], 'lookup_complete': True})
                checks['membership']['coverage']['apollo_recipients'] = bindings

    def __call__(self, rows, campaign_id, *, qualifications=None):
        if (not isinstance(rows, list) or len(rows) > 200 or any(not isinstance(r, dict) for r in rows)
                or not isinstance(campaign_id, str) or not re.fullmatch(r'BR-[A-Za-z0-9_-]{1,97}', campaign_id)):
            raise NewJourneyChecksError('checks_input_invalid')
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise NewJourneyChecksError('checks_clock_invalid', 503)
        now = now.astimezone(timezone.utc)
        deadline = time.monotonic() + 15
        state, unavailable = {}, []
        try:
            with self.repo.transaction():
                for kind in KINDS:
                    try:
                        state[kind] = self.repo.list_state(kind)
                    except Exception:
                        state[kind] = []
                        unavailable.append(kind)
        except Exception:
            state, unavailable = {k: [] for k in KINDS}, list(KINDS)
        sheet = {}
        try:
            sheet = self.service.campaign_sheet_view(campaign_id)
            sheet_ref = _ref('campaign_sheet', {'fingerprint': sheet.get('fingerprint'), 'status': sheet.get('status'),
                'fields': [{k: f.get(k) for k in ('key', 'status', 'expected_sha256', 'observed_sha256', 'content')}
                           for f in sheet.get('fields', [])]})
            sheet_reason = 'structured_account_icp_proof_unavailable' if sheet.get('status') == 'complete' else 'current_campaign_sheet_incomplete'
        except Exception:
            sheet_ref, sheet_reason = 'campaign_sheet:unavailable', 'current_campaign_sheet_unavailable'
        groups = []
        for identity in state['identity']:
            records = identity.get('records', [])
            if (identity.get('status') != 'canonical' or identity.get('actor') != 'ygor'
                    or not self.service._identity_records_match(records)
                    or not _token(identity.get('canonical_person'))
                    or any(_token(r.get('person_key')) != _token(identity['canonical_person'])
                           or r.get('domain') != identity.get('canonical_account') for r in records)):
                continue
            ids = {str(r['hubspot_contact_id']) for r in records if _id(r.get('hubspot_contact_id'))}
            keys = {_token(identity.get('canonical_person')), *(_token(r.get('person_key')) for r in records)} - {None}
            groups.append((keys, ids, identity))
        port = HubSpotEligibilityRead(self.hubspot, deadline=deadline) if isinstance(self.hubspot, HubSpotHTTP) else None
        duplicates = Counter(r.get('source_key') for r in rows if isinstance(r.get('source_key'), str))
        output, observed_aliases, proof_aliases = [], {}, {}
        for row in rows:
            source_key = row.get('source_key')
            if (not isinstance(source_key, str) or not 1 <= len(source_key) <= 200
                    or any(ord(c) < 32 or ord(c) == 127 for c in source_key)):
                raise NewJourneyChecksError('checks_source_key_invalid')
            email = _email(row.get('email'))
            profile = _token(row.get('linkedin_url') or row.get('linkedin'))
            hinted_id = _id(row.get('hubspot_id') or row.get('hubspot_contact_id'))
            keys = {email, profile} - {None}
            matched = [g for g in groups if keys & g[0] or not keys and hinted_id in g[1]]
            ids = set().union(*(g[1] for g in matched)) if matched else set()
            identity_reason = 'identity_unlinked'
            if len(ids) > 1 or hinted_id and ids and hinted_id not in ids or duplicates[source_key] > 1:
                identity_reason = 'identity_ambiguous'
            elif len(ids) == 1:
                identity_reason = None
                keys.add('hubspot:' + next(iter(ids)))
                for g in groups:
                    if g[1] == ids:
                        keys.update(g[0])
            identity_ref = _ref('identity', [g[2] for g in matched])
            checks = {k: {'key': k, 'status': 'unknown', 'reason': {
                'icp': sheet_reason, 'exclusions': 'exclusions_coverage_unavailable',
                'client': 'client_coverage_unavailable', 'open_deal': 'account_open_deal_coverage_unavailable',
                'refusal': 'cross_channel_reply_coverage_unavailable',
                'membership': 'apollo_instantly_membership_coverage_unavailable'}[k],
                'source': 'checks:' + k + ':coverage_unavailable',
                'checked_at': now.isoformat(), 'coverage': {'complete': False, 'identity_verified': identity_reason is None}}
                for k in KEYS}

            def block(key, reason, source):
                checks[key].update(status='blocked', reason=reason, source=source)

            company = row.get('company') if isinstance(row.get('company'), str) else ''
            hint = company[:2000] + ' ' + (email.split('@')[1] if email else '')
            folded = ''.join(c for c in unicodedata.normalize('NFKD', hint.casefold()) if not unicodedata.combining(c))
            for key, pattern, reason in (
                ('client', r'\b(?:kawasaki|pta\s+rental)\b', 'protected_client_literal_requires_review'),
                ('client', r'\b(?:bimbo|cuetara|gonac)\b', 'loa_approval_required'),
                ('exclusions', r'\b(?:coca[ -]?cola|femsa|bebidas|beverages|cervejaria|yamaha)\b', 'fixed_exclusion_literal')):
                if re.search(pattern, folded):
                    block(key, reason, _ref('policy:fixed_exclusions_v1:untrusted_hint', folded))
            contact, deals, error = None, [], 'checks_hubspot_unavailable'
            evidence = None
            if port and identity_reason != 'identity_ambiguous' and (email or hinted_id):
                evidence = port.lookup(email, contact_id=hinted_id, profile=profile)
                contact, deals, error = evidence.get('contact'), evidence.get('deals', []), evidence.get('reason')
            if contact:
                ident = str(contact['id'])
                if ids and ids != {ident}:
                    identity_reason = 'identity_ambiguous'
                else:
                    keys.add('hubspot:' + ident)
                    for g in groups:
                        if g[1] == {ident}:
                            keys.update(g[0])
                    identity_reason = None
                props, ref = contact['properties'], _ref('hubspot:' + PORTAL_ID + ':contact:' + ident, contact)
                if props.get('lifecyclestage') == 'customer':
                    block('client', 'hubspot_current_customer', ref)
                if props.get('hs_email_optout') in (True, 'true'):
                    block('refusal', 'hubspot_email_optout_person_requires_review', ref)
                if props.get('tipo_do_lead') in {'SPAM', 'CONCORRENTE', 'NAO E ICP'}:
                    block('icp', 'hubspot_disqualified_contact', ref)
                if isinstance(props.get('campanha_ativa'), str) and props['campanha_ativa'].strip():
                    block('membership', 'hubspot_current_campaign_membership', ref)
                elif 'campanha_ativa' not in props or 'executor' not in props:
                    checks['membership']['reason'] = 'hubspot_membership_property_missing'
                for deal in deals:
                    if deal.get('properties', {}).get('hs_is_closed') in (False, 'false'):
                        block('open_deal', 'hubspot_open_deal', _ref('hubspot:deal', deal))
            identity = evidence.get('identity', {}) if evidence else {}
            if identity.get('contact_id') and email in identity.get('emails', []):
                aliases = set(identity.get('emails', []) + identity.get('linkedin_urls', []))
                proof_aliases[source_key] = aliases
                for alias in aliases:
                    observed_aliases.setdefault(alias, set()).add(identity['contact_id'])
            if evidence and identity.get('verified') is True and identity_reason != 'identity_ambiguous':
                keys.update(identity.get('emails', []))
                keys.update(identity.get('linkedin_urls', []))
                keys.add('hubspot:' + str(identity.get('contact_id')))
                relevant_state = {kind: [r for r in state[kind] if _token(r.get('person_key')) in keys
                    or 'hubspot:' + str(r.get('hubspot_contact_id')) in keys] for kind in ('reply', 'cadence')}
                self._canonical_checks(checks, evidence, sheet, now, deadline, relevant_state,
                    (qualifications or {}).get(source_key))
            if identity_reason != 'identity_ambiguous':
                for reply in state['reply']:
                    linked = _token(reply.get('person_key')) in keys or 'hubspot:' + str(reply.get('hubspot_contact_id')) in keys
                    proven = reply.get('state') == 'classified' or reply.get('identity_confirmed') is True
                    if linked and proven:
                        if reply.get('classification') in {'negative', 'opt_out', 'risk'}:
                            block('refusal', 'canonical_refusal_all_channels', _ref('repository:reply', reply))
                        elif (reply.get('human_reply') is True or reply.get('state') == 'classified') and checks['refusal']['status'] != 'blocked':
                            block('refusal', 'human_reply_requires_conversation_review', _ref('repository:reply', reply))
                for cadence in state['cadence']:
                    if (_token(cadence.get('person_key')) in keys or 'hubspot:' + str(cadence.get('hubspot_contact_id')) in keys):
                        if cadence.get('status') == 'paused_reply' and checks['refusal']['status'] != 'blocked':
                            block('refusal', 'canonical_cadence_paused_reply', _ref('repository:cadence', cadence))
                        elif cadence.get('status') in {'ready_for_review', 'approved', 'active'}:
                            block('membership', 'existing_canonical_cadence', _ref('repository:cadence', cadence))
            if identity_reason:
                for check in checks.values():
                    if check['status'] == 'unknown':
                        check['reason'] = identity_reason
            accounts = {g[2].get('canonical_account') for g in matched} - {None}
            research = [r for r in state['research'] if r['id'] in accounts]
            if checks['icp']['status'] == 'unknown':
                checks['icp']['source'] = _ref('checks:icp', {'identity': identity_ref, 'research': research, 'sheet': sheet_ref})
                for r in research:
                    for fact in r.get('facts', []):
                        try:
                            age = (now.date() - datetime.fromisoformat(fact['observed_at']).date()).days
                            if age < 0 or age > 14:
                                checks['icp']['reason'] = 'canonical_research_stale'
                        except (ValueError, TypeError, KeyError):
                            continue
            for check in checks.values():
                check['coverage']['identity_verified'] = identity_reason is None and (
                    bool(evidence and evidence.get('identity', {}).get('verified')) or len(ids) == 1)
                if check['status'] == 'pass' and (unavailable and check['key'] in ('refusal', 'membership')
                        or 'identity' in unavailable):
                    check.update(status='unknown', reason='canonical_state_read_incomplete')
                    check['coverage']['complete'] = False
            output.append({'source_key': source_key, 'qualification_context': candidate(evidence) if evidence else None,
                'rowstablekey': row.get('rowstablekey') or source_key,
                'checks': list(checks.values()), 'checked_at': now.isoformat(),
                'retained': not all(c['status'] == 'pass' for c in checks.values()),
                'coverage': {'complete': all(c['status'] == 'pass' for c in checks.values()),
                             'unavailable': unavailable, 'hubspot_reason': error,
                             'membership_required': list(MEMBERSHIP_PROVIDERS)}})
        ambiguous = {alias for alias, owners in observed_aliases.items() if len(owners) > 1}
        for result in output:
            if proof_aliases.get(result['source_key'], set()) & ambiguous:
                result['retained'], result['coverage']['complete'] = True, False
                for check in result['checks']:
                    check['coverage']['identity_verified'] = False
                    if check['status'] == 'pass':
                        check.update(status='unknown', reason='identity_ambiguous')
                        check['coverage']['complete'] = False
        return output
