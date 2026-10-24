"""One explicitly reviewed journey contact/company import, without enrichment.

API (all methods return the server-held preview, including id/version):
    NewJourneyImport(journey: NewJourney, console: HubSpotConsole)
    preview(journey_id, data, actor, expected_version, review_hash)
    execute({preview_id, expected_version, preview_hash, confirm: True}, actor)
    reconcile({preview_id, expected_version, preview_hash}, actor)

``data`` has exactly source_key, contact, company, source_refs. The two property
maps accept only CONTACT/COMPANY below. Each nonempty value has a source_refs
entry, e.g. ``contact.email: {ref: 'https://...', verified: True}``. These are
Ygor's explicit attestations of already verified sources, NOT API verification
or ICP qualification. The email must already belong to the selected draft row;
missing fields produce a blocked preview before any provider reads. Never split
a name, guess a domain/email, enrich, or mutate qualifications/checks/campaigns.

The initial expected_version/review_hash belong to the journey. execute and
reconcile use the PREVIEW version/hash. One confirmation covers three explicit
operations, dispatched serially; stop on uncertainty and reconcile read-only.
Creation uses HubSpotExecutor; association uses its existing attempt/identity
journal namespaces with a separate, exact-ID plan. No extra tables, approval
service, daemon or agent activation. Reconciliation can verify current state but
never attributes causality, releases an ambiguous create, or resumes writes.

Official contracts: CRM v3 contacts/companies, contact-company association type
279 (unlabeled); v4 label metadata. Search includes hs_additional_emails. Sources:
https://developers.hubspot.com/docs/api-reference/legacy/crm/objects/contacts/guide
https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm
https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide
Scope/tenant access must still be verified by the real provider; fake tests do
not establish that access. No retry, including HTTP 429; caller may retry reads.
"""
from collections.abc import Mapping
from copy import deepcopy
import json
import math
import re
from threading import RLock
import time
from urllib.parse import urlsplit

from .hubspot_executor import HubSpotExecutor, HubSpotHTTP, HubSpotHTTPError
from .integration_hubspot import PORTAL_ID, _name
from .models import payload_hash
from .new_journey import NewJourneyError, _text, _valid_email
from .new_journey_hubspot import _domain


CONTACT = frozenset({'email', 'firstname', 'lastname', 'jobtitle'})
COMPANY = frozenset({'name', 'domain', 'website'})
ALIASES = {'contacts': 'hs_additional_emails', 'companies': 'hs_additional_domains'}
_ID = re.compile(r'[1-9][0-9]{0,29}\Z')
_META = {'id', 'version', 'updated_at'}
_HASH_FIELDS = ('kind', 'portal_id', 'journey_id', 'journey_version', 'journey_review_hash',
                'source_key', 'row_hash', 'contact', 'company', 'association', 'source_refs',
                'blockers', 'expires_at', 'created_by')


def _require(ok, code, status=409):
    if not ok:
        raise NewJourneyError(code, status)


def _record(row):
    _require(isinstance(row, Mapping) and isinstance(row.get('id'), str)
        and _ID.fullmatch(row['id']) and row.get('archived') is False
        and isinstance(row.get('properties'), Mapping), 'import_invalid_readback')
    return row


def _identities(kind, row):
    props = _record(row)['properties']
    primary = 'email' if kind == 'contacts' else 'domain'
    _require(primary in props and ALIASES[kind] in props, 'import_alias_coverage_unavailable')
    alias = props[ALIASES[kind]]
    _require(alias is None or isinstance(alias, str) and len(alias) <= 10000,
             'import_alias_coverage_unavailable')
    values = [props[primary], *(alias.split(';') if alias else [])]
    normalize = (lambda v: v.strip().lower() if isinstance(v, str) and _valid_email(v.strip()) else None) if kind == 'contacts' else _domain
    normalized = [normalize(value) for value in values]
    _require(all(normalized), 'import_identity_invalid')
    return set(normalized)


class JourneyImportHTTP(HubSpotHTTP):
    """Bounded extension of the existing fixed-origin, one-attempt HTTP port.

    Construct per operation from the console port, never mutate its credential,
    timeout or endpoint allowlist. Only the exact contact/company label read and
    association PUT are added. The inherited transport still owns fixed HTTPS,
    TLS/redirect rules and the 1 MB response bound. Per-request timeout <= 4s.
    """
    _search_lock = RLock()
    _last_search_at = 0.0

    def __init__(self, original):
        _require(isinstance(original, HubSpotHTTP), 'import_transport_unavailable', 503)
        _require(type(original.timeout) in (int, float) and math.isfinite(original.timeout)
                 and original.timeout > 0, 'import_transport_unavailable', 503)
        super().__init__(original._loader, request=original._request,
            timeout=min(4, original.timeout), unique_properties=original.unique_properties,
            creation_mode=original.creation_mode)

    def _pace_search(self):
        # HubSpot documents five searches/sec/account. Serialize this process at
        # four/sec; external writers can still cause 429, which fails closed.
        with JourneyImportHTTP._search_lock:
            wait = max(0, JourneyImportHTTP._last_search_at + 0.26 - time.monotonic())
            if wait:
                time.sleep(wait)
            JourneyImportHTTP._last_search_at = time.monotonic()

    def _call(self, method, path, body=None):
        if method == 'POST' and path in {'/crm/v3/objects/contacts/search', '/crm/v3/objects/companies/search'}:
            self._pace_search()
        return super()._call(method, path, body)

    def _association_call(self, method, path):
        allowed = (method == 'GET' and path == '/crm/v4/associations/contacts/companies/labels'
            or method == 'GET' and re.fullmatch(
                r'/crm/v4/objects/contacts/[1-9][0-9]{0,29}/associations/companies\?limit=500', path)
            or method == 'PUT' and re.fullmatch(
                r'/crm/v3/objects/contacts/[1-9][0-9]{0,29}/associations/companies/[1-9][0-9]{0,29}/279', path))
        _require(allowed, 'import_endpoint_blocked')
        try:
            # Use the same loader and fixed-origin request function as HubSpotHTTP.
            if self._token is None:
                self._token = self._loader()
            _require(isinstance(self._token, str) and self._token
                     and not any(c.isspace() for c in self._token), 'import_credential_unavailable')
            status, raw = self._request(method, path, None,
                {'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json'}, self.timeout)
            if type(status) is not int or not 200 <= status < 300:
                raise HubSpotHTTPError('hubspot_http_error', status)
            _require(isinstance(raw, (bytes, bytearray)) and len(raw) <= 1_000_000,
                     'import_invalid_response')
            value = json.loads(raw)
            _require(isinstance(value, dict), 'import_invalid_response')
            return value
        except HubSpotHTTPError:
            raise
        except Exception:
            raise HubSpotHTTPError('hubspot_request_uncertain') from None

    def association_schema(self):
        result = self._association_call('GET', '/crm/v4/associations/contacts/companies/labels')
        rows = result.get('results')
        _require(isinstance(rows, list) and sum(isinstance(r, Mapping)
            and r.get('typeId') == 279 and r.get('category') == 'HUBSPOT_DEFINED'
            and r.get('label') is None for r in rows) == 1, 'import_association_schema_unavailable')
        return {'category': 'HUBSPOT_DEFINED', 'type_id': 279, 'label': None}

    def association_ids(self, contact_id):
        _require(isinstance(contact_id, str) and _ID.fullmatch(contact_id), 'import_invalid_contact_id')
        # Dedicated endpoint has an explicit empty results list. Missing v3
        # associations on a newly created contact is not fabricated as emptiness.
        value = self._association_call('GET', '/crm/v4/objects/contacts/' + contact_id
                                       + '/associations/companies?limit=500')
        _require(isinstance(value.get('results'), list)
                 and not value.get('paging', {}).get('next'), 'import_association_read_incomplete')
        ids = []
        for row in value['results']:
            raw = row.get('toObjectId') if isinstance(row, Mapping) else None
            _require(type(raw) in (str, int) and _ID.fullmatch(str(raw)), 'import_association_read_invalid')
            ids.append(str(raw))
        return sorted(set(ids))

    def associate(self, contact_id, company_id):
        _require(all(isinstance(v, str) and _ID.fullmatch(v) for v in (contact_id, company_id)),
                 'import_association_id_required')
        return self._association_call('PUT', '/crm/v3/objects/contacts/' + contact_id
            + '/associations/companies/' + company_id + '/279')

    def candidates(self, kind, properties, record_id=None):
        """Complete bounded union: name, primary/secondary identity, direct ID.

        Search absence does not remove indexing/concurrent-external-writer races.
        Missing alias schema, truncated pages, malformed totals => fail closed.
        """
        field = 'email' if kind == 'contacts' else 'domain'
        alias = ALIASES[kind]
        metadata = self.metadata(kind)
        _require(alias in metadata['properties'], 'import_alias_schema_unavailable')
        props = set(properties) | {alias, field}
        rows = {}
        name_filters = [{'propertyName': key, 'operator': 'EQ', 'value': properties[key]}
                        for key in (('firstname', 'lastname') if kind == 'contacts' else ('name',))
                        if properties.get(key)]
        for row in self._search(kind, name_filters, props):
            rows[_record(row)['id']] = row
        # Both primary and secondary emails are documented searchable text.
        # Explicit additional-domain token search complements primary domain.
        query = ({'query': properties[field]} if kind == 'contacts' else
                 {'filterGroups': [{'filters': [{'propertyName': field, 'operator': 'EQ', 'value': properties[field]}]},
                                   {'filters': [{'propertyName': alias, 'operator': 'CONTAINS_TOKEN', 'value': properties[field]}]}]})
        after, found = None, []
        for _ in range(10):
            body = {**query, 'properties': sorted(props), 'limit': 100}
            if after is not None:
                body['after'] = after
            result = self._call('POST', '/crm/v3/objects/' + kind + '/search', body)
            page = result.get('results')
            _require(isinstance(page, list), 'import_dedupe_incomplete')
            found.extend(page)
            next_after = result.get('paging', {}).get('next', {}).get('after')
            if next_after is None:
                _require(type(result.get('total')) is int and result['total'] == len(found)
                         and len({_record(r)['id'] for r in found}) == len(found), 'import_dedupe_incomplete')
                break
            _require(next_after != after, 'import_dedupe_incomplete')
            after = next_after
        else:
            raise NewJourneyError('import_dedupe_incomplete', 409)
        for row in found:
            rows[_record(row)['id']] = row
        direct = self.read(kind, properties[field], props, id_property=field) if kind == 'contacts' else None
        if direct:
            rows[_record(direct)['id']] = direct
        if record_id:
            pinned = self.read(kind, record_id, props)
            _require(pinned is not None and _record(pinned)['id'] == record_id, 'import_pinned_id_unavailable')
            rows[record_id] = pinned
        # Exact comparison removes broad query matches, but retains name conflicts.
        return [row for row in rows.values() if properties[field] in _identities(kind, row)
                or _name(row['properties'], kind) == _name(properties, kind) or row['id'] == record_id]


class NewJourneyImport:
    def __init__(self, journey, hubspot_console):
        self.journey, self.console = journey, hubspot_console
        self.repo = journey.repo
        _require(self.repo is hubspot_console.repo, 'import_repository_mismatch')

    def _port(self):
        port = self.console.port
        return JourneyImportHTTP(port) if isinstance(port, HubSpotHTTP) else port

    def _now(self):
        value = self.console.clock()
        _require(type(value) in (int, float) and math.isfinite(value), 'import_clock_invalid', 503)
        return value

    def _actor(self, actor):
        self.journey._actor(actor)
        _require(self.console.read_enabled is True, 'hubspot_not_configured', 503)

    def _allowed(self):
        return bool(self.console._write_allowed() and not self.journey.service.global_paused())

    def _binding(self, ident, expected_version, review_hash):
        row = self.journey._read(ident, expected_version)
        context, sheet = self.journey._campaign(row['campaign_id'])
        _require(isinstance(review_hash, str) and review_hash == self.journey._review_hash(row, context, sheet),
                 'journey_review_conflict')
        _require(context == row['campaign_context'] and self.journey._sheet_hash(sheet) == row['sheet_hash'],
                 'journey_context_changed')
        return row

    @staticmethod
    def _input(data, row):
        _require(isinstance(data, Mapping) and set(data) == {'source_key', 'contact', 'company', 'source_refs'},
                 'import_input_invalid', 400)
        matches = [r for r in row['rows'] if r['source_key'] == data['source_key']]
        _require(len(matches) == 1, 'import_single_row_required', 400)
        lead, blockers = matches[0], []
        properties = {}
        for label, allowed in (('contact', CONTACT), ('company', COMPANY)):
            values = data[label]
            _require(isinstance(values, Mapping) and set(values) <= allowed
                     and all(_text(v, 2000, empty=True) for v in values.values()), 'import_properties_invalid', 400)
            properties[label] = {k: v.strip() for k, v in values.items() if v.strip()}
        contact, company = properties['contact'], properties['company']
        if not contact.get('email'):
            blockers.append('contact_email_required')
        elif not _valid_email(contact['email']):
            blockers.append('contact_email_invalid')
        else:
            contact['email'] = contact['email'].lower()
            _require(_valid_email(lead.get('email')) and contact['email'] == lead['email'].strip().lower(),
                     'import_email_not_in_journey_row', 400)
        if not contact.get('firstname') and not contact.get('lastname'):
            blockers.append('contact_name_required')
        if not company.get('name'):
            blockers.append('company_name_required')
        if not company.get('domain'):
            blockers.append('company_domain_required')
        elif not _domain(company['domain']):
            blockers.append('company_domain_invalid')
        else:
            company['domain'] = _domain(company['domain'])
        if company.get('website'):
            try:
                url = urlsplit(company['website'])
                _require(url.scheme in {'https', 'http'} and url.hostname and not url.username
                    and not url.password and not url.port and _domain(url.hostname) == company.get('domain'),
                    'import_website_identity_mismatch', 400)
            except ValueError:
                raise NewJourneyError('import_website_identity_mismatch', 400) from None
        refs = data['source_refs']
        supplied = {label + '.' + key for label, props in properties.items() for key in props}
        _require(isinstance(refs, Mapping) and set(refs) <= supplied, 'import_source_refs_invalid', 400)
        for field in supplied:
            ref = refs.get(field)
            if not (isinstance(ref, Mapping) and set(ref) == {'ref', 'verified'}
                    and ref.get('verified') is True and _text(ref.get('ref'), 2000)):
                blockers.append('source_unverified:' + field)
        # Existing source blockers cannot be removed by CRM import.
        blockers.extend('journey_row:' + value for value in lead.get('blockers', [])
                        if value not in {'email_required', 'email_invalid'})
        return lead, contact, company, sorted(set(blockers))

    @staticmethod
    def _effect(kind, properties):
        field = 'email' if kind == 'contacts' else 'domain'
        return 'journey-import:' + kind + ':' + payload_hash({'portal': PORTAL_ID, field: properties[field]})

    def _object(self, port, kind, props, pinned=None):
        rows = port.candidates(kind, props, pinned)
        _require(isinstance(rows, list), 'import_dedupe_incomplete')
        field = 'email' if kind == 'contacts' else 'domain'
        _require(len({_record(r)['id'] for r in rows}) == len(rows), 'import_dedupe_incomplete')
        _require(len(rows) <= 1, 'import_identity_conflict')
        if rows:
            target = rows[0]
            _require(props[field] in _identities(kind, target), 'import_name_identity_conflict')
            _require(not pinned or target['id'] == pinned, 'import_pinned_identity_conflict')
            return {'properties': props, 'existing_id': target['id'], 'action': 'reuse_existing',
                    'dedupe_status': 'verified', 'plan': None}
        _require(not pinned, 'import_pinned_id_unavailable')
        plan = port.prepare(kind, props, self._effect(kind, props))
        _require(plan['operation'] == 'create' and plan['record_id'] is None
                 and plan['properties'] == props, 'import_create_plan_changed')
        return {'properties': props, 'existing_id': None, 'action': 'propose_create',
                'dedupe_status': 'verified_absence', 'plan': plan}

    def _view(self, preview):
        result = deepcopy(preview)
        allowed = self._allowed()
        result.update(write_enabled=allowed, can_execute=bool(allowed and not preview['blockers']
            and preview['state'] == 'review_required' and preview['expires_at'] > self._now()),
            external_writes=preview.get('external_writes', 0))
        return result

    def preview(self, journey_id, data, actor, expected_version, review_hash):
        self._actor(actor)
        with self.repo.transaction():
            row = self._binding(journey_id, expected_version, review_hash)
            lead, contact, company, blockers = self._input(data, row)
        binding = {'journey_id': journey_id, 'journey_version': expected_version,
                   'journey_review_hash': review_hash, 'source_key': lead['source_key'],
                   'row_hash': payload_hash(lead)}
        request_hash = payload_hash({'binding': binding, 'input': data})
        key = 'journey-import-' + request_hash
        existing = self.repo.get_state('hubspot_preview', key)
        if existing and (existing['state'] not in {'review_required', 'blocked'}
                         or existing['expires_at'] > self._now()):
            return self._view(existing)
        objects = {label: {'properties': props, 'existing_id': None, 'action': 'manual_review',
                   'dedupe_status': 'unverified', 'plan': None}
                   for label, props in (('contact', contact), ('company', company))}
        association = {'action': 'associate_after_exact_id_readback', 'type_id': 279,
                       'contact_id': None, 'company_id': None, 'status': 'unverified'}
        if not blockers:
            try:
                port = self._port()
                _require(port.portal() == PORTAL_ID, 'import_portal_mismatch')
                _require(port.association_schema() == {'category': 'HUBSPOT_DEFINED', 'type_id': 279, 'label': None},
                         'import_association_schema_unavailable')
                objects['company'] = self._object(port, 'companies', company)
                objects['contact'] = self._object(port, 'contacts', contact, lead.get('hubspot_id') or None)
                cid, oid = objects['contact']['existing_id'], objects['company']['existing_id']
                ids = port.association_ids(cid) if cid else []
                _require(not ids or oid is not None and ids == [oid], 'import_company_association_conflict')
                association.update(contact_id=cid, company_id=oid,
                    status='verified' if ids else 'planned',
                    action='reuse_existing' if ids else 'associate_after_exact_id_readback')
            except Exception as exc:
                blockers.append(exc.code if isinstance(exc, NewJourneyError) else 'import_provider_read_unavailable')
        unsigned = {'kind': 'journey_import_v1', 'portal_id': PORTAL_ID, **binding,
                    **objects, 'association': association, 'source_refs': deepcopy(data['source_refs']),
                    'blockers': sorted(set(blockers)), 'expires_at': self._now() + 300, 'created_by': actor}
        preview = {**unsigned, 'preview_hash': payload_hash(unsigned),
            'state': 'blocked' if blockers else 'review_required', 'result': None, 'external_writes': 0}
        with self.repo.transaction():
            self._binding(journey_id, expected_version, review_hash)
            current = self.repo.get_state('hubspot_preview', key)
            if current is None:
                saved = self.repo.append_state('hubspot_preview', key, preview)
            elif (existing and current['version'] == existing['version']
                  and current['state'] in {'review_required', 'blocked'}
                  and current['expires_at'] <= self._now()):
                saved = self.repo.put_states_atomic([
                    ('hubspot_preview', key, preview, current['version'])])[0]
            else:
                # Another caller may have refreshed or claimed execution while
                # this caller performed reads. Never reset that newer state.
                saved = current
        return self._view(saved)

    def _load(self, body, actor, execute=False):
        self._actor(actor)
        fields = {'preview_id', 'expected_version', 'preview_hash'} | ({'confirm'} if execute else set())
        _require(isinstance(body, Mapping) and set(body) == fields
            and isinstance(body.get('preview_id'), str) and type(body.get('expected_version')) is int
            and (not execute or body.get('confirm') is True), 'import_exact_review_required', 400)
        preview = self.repo.get_state('hubspot_preview', body['preview_id'])
        _require(preview is not None and preview.get('kind') == 'journey_import_v1', 'import_preview_missing', 404)
        _require(preview['version'] == body['expected_version'] and preview['preview_hash'] == body['preview_hash'],
                 'import_preview_conflict')
        _require(preview['preview_hash'] == payload_hash({k: preview.get(k) for k in _HASH_FIELDS}),
                 'import_preview_integrity_mismatch')
        return preview

    def _authorize(self, approved):
        current = self.repo.get_state('hubspot_preview', approved['id'])
        if not (self._allowed() and current and current['state'] == 'executing'
                and current['version'] == approved['version'] and current['preview_hash'] == approved['preview_hash']
                and current['preview_hash'] == payload_hash({k: current.get(k) for k in _HASH_FIELDS})
                and current['expires_at'] > self._now()):
            return False
        try:
            self._binding(current['journey_id'], current['journey_version'], current['journey_review_hash'])
            return True
        except NewJourneyError:
            return False

    def _save_result(self, approved, result, writes):
        with self.repo.transaction():
            current = self.repo.get_state('hubspot_preview', approved['id'])
            _require(current['version'] == approved['version'], 'import_preview_conflict')
            return self._view(self.repo.put_state('hubspot_preview', current['id'],
                {**{k: v for k, v in current.items() if k not in _META}, 'state': 'processed',
                 'result': result, 'external_writes': writes}))

    def _read_id(self, port, kind, props, ident):
        _require(isinstance(ident, str) and _ID.fullmatch(ident), 'import_record_id_required')
        row = port.read(kind, ident, set(props) | {ALIASES[kind]})
        _require(row is not None and _record(row)['id'] == ident, 'import_exact_id_readback_required')
        field = 'email' if kind == 'contacts' else 'domain'
        _require(props[field] in _identities(kind, row), 'import_readback_identity_mismatch')
        return ident

    def execute(self, body, actor):
        with self.repo.transaction():
            preview = self._load(body, actor, True)
            _require(self._allowed(), 'hubspot_writes_disabled_or_paused')
            _require(preview['state'] == 'review_required' and not preview['blockers']
                     and preview['expires_at'] > self._now(), 'import_preview_not_current')
            self._binding(preview['journey_id'], preview['journey_version'], preview['journey_review_hash'])
            approved = self.repo.put_state('hubspot_preview', preview['id'],
                {**{k: v for k, v in preview.items() if k not in _META}, 'state': 'executing', 'approved_by': actor})
        port, ids, writes = self._port(), {}, 0
        result = {'status': 'pending_reconciliation', 'retry_write': False, 'operations': {}}
        outer = self
        class Guarded:
            def __getattr__(self, name):
                return getattr(port, name)
            def write(self, request):
                nonlocal writes
                _require(request.get('method') == 'POST' and request.get('path') in {
                    '/crm/v3/objects/contacts', '/crm/v3/objects/companies'}, 'import_write_blocked')
                kind = request['path'].rsplit('/', 1)[-1]
                label = 'contact' if kind == 'contacts' else 'company'
                with outer.repo.transaction():
                    _require(outer._authorize(approved), 'hubspot_writes_disabled_or_paused')
                    target = approved[label]
                    _require(outer._object(port, kind, target['properties']) == target, 'import_target_changed')
                    _require(request.get('body') == {'properties': target['properties']}, 'import_write_blocked')
                    writes += 1
                    return port.write(request)
        executor = HubSpotExecutor(self.repo, Guarded(), enabled=True,
            authorize=lambda plan: self._authorize(approved) and any(
                approved[label]['plan'] == plan for label in ('company', 'contact')),
            human_review=lambda plan: self._authorize(approved))
        try:
            _require(port.portal() == PORTAL_ID, 'import_portal_mismatch')
            # Revalidate BOTH targets before the first effect; then again per effect.
            for label, kind in (('company', 'companies'), ('contact', 'contacts')):
                target = approved[label]
                fresh = self._object(port, kind, target['properties'], target['existing_id'])
                _require(fresh == target, 'import_target_changed')
            existing_contact = approved['contact']['existing_id']
            existing_company = approved['company']['existing_id']
            linked = port.association_ids(existing_contact) if existing_contact else []
            _require(not linked or existing_company and linked == [existing_company],
                     'import_company_association_conflict')
            for label, kind in (('company', 'companies'), ('contact', 'contacts')):
                target = approved[label]
                _require(self._authorize(approved), 'hubspot_writes_disabled_or_paused')
                fresh = self._object(port, kind, target['properties'], target['existing_id'])
                _require(fresh == target, 'import_target_changed')
                if target['action'] == 'reuse_existing':
                    ident = self._read_id(port, kind, target['properties'], target['existing_id'])
                    outcome = {'status': 'verified_state', 'record_id': ident, 'retry_write': False}
                else:
                    outcome = executor.execute(target['plan'])
                result['operations'][label] = outcome
                if outcome.get('status') != 'verified_state' or outcome.get('identity_lock_retained'):
                    return self._save_result(approved, result, writes)
                ids[label] = self._read_id(port, kind, target['properties'], outcome['record_id'])
            result['operations']['association'] = self._association(port, approved, ids)
            if result['operations']['association']['status'] == 'verified_state':
                result.update(status='verified_state', contact_id=ids['contact'], company_id=ids['company'],
                              write_attribution_confirmed=False)
            writes += result['operations']['association'].get('write_attempts', 0)
        except Exception as exc:
            result['error_code'] = exc.code if isinstance(exc, NewJourneyError) else 'import_execution_requires_reconciliation'
        return self._save_result(approved, result, writes)

    def _association(self, port, approved, ids):
        """Separate exact-ID operation, using the existing one-shot journal."""
        cid, oid = ids['contact'], ids['company']
        for label, kind in (('contact', 'contacts'), ('company', 'companies')):
            self._read_id(port, kind, approved[label]['properties'], ids[label])
        _require(port.association_schema() == {'category': 'HUBSPOT_DEFINED', 'type_id': 279, 'label': None},
                 'import_association_schema_unavailable')
        found = port.association_ids(cid)
        _require(not found or found == [oid], 'import_company_association_conflict')
        if found == [oid]:
            return {'status': 'verified_state', 'contact_id': cid, 'company_id': oid, 'write_attempts': 0}
        plan = {'portal_id': PORTAL_ID, 'operation': 'associate', 'contact_id': cid, 'company_id': oid,
                'association_type_id': 279, 'preview_hash': approved['preview_hash'], 'automatic_retry_allowed': False}
        plan['plan_hash'] = payload_hash(plan)
        effect = payload_hash({'portal': PORTAL_ID, 'effect': 'journey-import:association:' + cid + ':' + oid})
        lock_key = payload_hash({'portal': PORTAL_ID, 'import_association_contact': cid})
        with self.repo.transaction():
            _require(self._authorize(approved), 'hubspot_writes_disabled_or_paused')
            previous = self.repo.get_state('hubspot_attempt', effect)
            if previous:
                return {'status': 'pending_reconciliation', 'retry_write': False, 'write_attempts': 0}
            lock = self.repo.get_state('hubspot_identity', lock_key)
            _require(not lock or lock.get('status') == 'released', 'import_association_pending')
            self.repo.append_state('hubspot_attempt', effect,
                {'plan_hash': plan['plan_hash'], 'plan': plan, 'status': 'attempted', 'returned_id': None})
            self.repo.put_state('hubspot_identity', lock_key, {'effect': effect, 'status': 'attempted'})
        try:
            _require(self._authorize(approved), 'hubspot_writes_disabled_or_paused')
            port.associate(cid, oid)
            self.repo.put_state('hubspot_attempt', effect,
                {'plan_hash': plan['plan_hash'], 'plan': plan, 'status': 'response_received', 'returned_id': None})
            found = port.association_ids(cid)
        except Exception:
            return {'status': 'pending_reconciliation', 'retry_write': False, 'write_attempts': 1}
        if found != [oid]:
            return {'status': 'pending_reconciliation', 'retry_write': False, 'write_attempts': 1}
        self.repo.put_state('hubspot_attempt', effect,
            {'plan_hash': plan['plan_hash'], 'plan': plan, 'status': 'verified_state', 'returned_id': None})
        self.repo.put_state('hubspot_identity', lock_key, {'effect': effect, 'status': 'released'})
        return {'status': 'verified_state', 'contact_id': cid, 'company_id': oid, 'write_attempts': 1}

    def reconcile(self, body, actor):
        preview = self._load(body, actor)
        _require(preview['state'] in {'executing', 'processed'}, 'import_no_execution_to_reconcile')
        port, ids = self._port(), {}
        result = {'status': 'pending_reconciliation', 'retry_write': False, 'operations': {}}
        try:
            _require(port.portal() == PORTAL_ID, 'import_portal_mismatch')
            executor = HubSpotExecutor(self.repo, port)
            for label, kind in (('company', 'companies'), ('contact', 'contacts')):
                target = preview[label]
                if target['plan']:
                    effect, _ = executor._keys(target['plan'])
                    if not self.repo.get_state('hubspot_attempt', effect):
                        result['operations'][label] = {'status': 'not_attempted', 'retry_write': False}
                        return self._save_result(preview, result, preview.get('external_writes', 0))
                    outcome = executor.reconcile(target['plan'])
                    result['operations'][label] = outcome
                    if outcome['status'] != 'verified_state' or outcome.get('identity_lock_retained'):
                        return self._save_result(preview, result, preview.get('external_writes', 0))
                    ident = outcome['record_id']
                else:
                    ident = target['existing_id']
                ids[label] = self._read_id(port, kind, target['properties'], ident)
            linked = port.association_ids(ids['contact'])
            if linked == [ids['company']]:
                result.update(status='verified_state', contact_id=ids['contact'], company_id=ids['company'],
                              write_attribution_confirmed=False)
                result['operations']['association'] = {'status': 'verified_state', **ids}
            else:
                result['operations']['association'] = {'status': 'pending_reconciliation', 'retry_write': False}
        except Exception:
            result['error_code'] = 'import_readback_unavailable'
        return self._save_result(preview, result, preview.get('external_writes', 0))
