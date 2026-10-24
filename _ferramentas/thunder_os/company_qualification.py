"""S27 company-only human review, using the existing preview/attempt journal.

preview(ident, source_key, facts, actor, expected_version, review_hash) is read
only at HubSpot. facts are qualification.review's nine fields plus literal
not_customer=True and not_excluded=True. execute accepts exactly preview_id,
expected_version (the preview version), preview_hash and confirm=True.
reconcile accepts the same fields except confirm, and performs only reads.

Only the scoped transport can prepare these properties. The global record
allowlist stays unchanged. No draft approval, export or outreach is granted.
"""
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
import json
import math
import re

from .hubspot_executor import HubSpotHTTP, HubSpotExecutor
from .integration_hubspot import PORTAL_ID, _require as contract_require, plan_record
from .models import payload_hash
from .new_journey import NewJourneyError
from .new_journey_hubspot import HubSpotEligibilityRead
from .new_journey_qualification import QualificationError, candidate, decision, review


PURPOSE = 'company_qualification_v1'
QUALIFICATION_FIELDS = frozenset({'thunder_icp_status', 'thunder_is_customer',
    'thunder_icp_exclusion_reason', 'country', 'hs_country_code', 'thunder_icp_sector',
    'thunder_annual_revenue_estimate', 'thunder_last_enriched_at', 'thunder_enrichment_source'})
_IDENTITY_FIELDS = frozenset({'name', 'domain'})
_HASH_FIELDS = ('kind', 'portal_id', 'journey_id', 'journey_version', 'journey_review_hash',
    'source_key', 'row_hash', 'sheet_hash', 'company_identity', 'facts', 'qualification',
    'plan', 'before', 'after', 'created_by', 'expires_at')
_META = {'id', 'version', 'updated_at'}
_ID = re.compile(r'[1-9][0-9]{0,29}\Z')


def _require(ok, code, status=409):
    if not ok:
        raise NewJourneyError(code, status)


class CompanyQualificationHTTP(HubSpotHTTP):
    """Per-call qualification preparer; never mutate the shared HTTP transport."""
    def __init__(self, original):
        self.original = original
        self.creation_mode = original.creation_mode
        self.unique_properties = original.unique_properties

    def _call(self, method, path, body=None):
        return self.original._call(method, path, body)

    def prepare(self, kind, values, effect_id, record_id=None):
        contract_require(kind == 'companies' and isinstance(record_id, str)
            and _ID.fullmatch(record_id) is not None, 'company_existing_target_required')
        contract_require(isinstance(values, Mapping)
            and set(values) == QUALIFICATION_FIELDS | _IDENTITY_FIELDS,
            'company_qualification_properties_invalid')
        contract_require(all(isinstance(v, str) and len(v) <= 10000
            and (v.strip() or k == 'thunder_icp_exclusion_reason')
            for k, v in values.items()), 'company_qualification_values_invalid')
        contract_require(values['thunder_icp_status'] in {'qualified', 'disqualified'}
            and values['thunder_is_customer'] == 'false'
            and values['thunder_icp_exclusion_reason'] == ''
            and values['country'] == values['hs_country_code'] == 'BR',
            'company_qualification_values_invalid')
        self.portal()
        metadata = self.metadata(kind)
        schemas = metadata['properties']
        types = {'thunder_icp_status': 'enumeration', 'thunder_is_customer': 'bool',
            'thunder_icp_exclusion_reason': 'enumeration', 'thunder_icp_sector': 'enumeration',
            'thunder_annual_revenue_estimate': 'number', 'thunder_last_enriched_at': 'date',
            'thunder_enrichment_source': 'string'}
        for key, value in values.items():
            schema = schemas.get(key, {})
            contract_require(schema.get('readOnlyValue') is False,
                             'verified_writable_properties_required')
            if key in types:
                contract_require(schema.get('type') == types[key], 'company_qualification_schema_changed')
            if schema.get('type') == 'enumeration' and key != 'thunder_icp_exclusion_reason':
                contract_require(value in schema.get('options', []), 'unknown_property_option')
        # Enum clearing is confined to this explicit human-reviewed operation.
        reads = self.dedupe(kind, values, metadata, record_id)
        plan = plan_record(kind, {k: values[k] for k in _IDENTITY_FIELDS},
            metadata=metadata, dedupe=reads, effect_id=effect_id, record_id=record_id)
        contract_require(plan['operation'] == 'update' and not plan['changes'],
                         'company_identity_mismatch')
        contract_require(set(values) <= set(plan['before']['properties']),
                         'company_qualification_before_incomplete')
        plan.update(purpose=PURPOSE, properties=dict(values), changes={k: values[k]
            for k in QUALIFICATION_FIELDS if plan['before']['properties'][k] != values[k]})
        plan['plan_hash'] = payload_hash({k: v for k, v in plan.items() if k != 'plan_hash'})
        return plan


class CompanyQualification:
    def __init__(self, journey, console):
        self.journey, self.console, self.repo = journey, console, journey.repo
        _require(self.repo is console.repo, 'company_qualification_repository_mismatch')

    def _port(self):
        _require(isinstance(self.console.port, HubSpotHTTP), 'company_qualification_transport_required', 503)
        return CompanyQualificationHTTP(self.console.port)

    def _actor(self, actor):
        self.journey._actor(actor)
        _require(self.console.read_enabled is True, 'hubspot_not_configured', 503)

    def _now(self):
        value = self.console.clock()
        _require(type(value) in (int, float) and math.isfinite(value), 'company_qualification_clock_invalid', 503)
        return value

    def _binding(self, ident, source_key, version, review_hash):
        self.journey._running()
        row = self.journey._read(ident, version)
        context, sheet = self.journey._campaign(row['campaign_id'])
        _require(review_hash == self.journey._review_hash(row, context, sheet), 'journey_review_conflict')
        _require(sheet.get('status') == 'complete' and context == row['campaign_context']
            and self.journey._sheet_hash(sheet) == row['sheet_hash'], 'journey_context_changed')
        leads = [lead for lead in row['rows'] if lead['source_key'] == source_key]
        checks = [check for check in row['checks'] if check['source_key'] == source_key]
        bound = checks[0].get('qualification_context') if len(checks) == 1 else None
        _require(len(leads) == 1 and isinstance(bound, Mapping)
            and isinstance(bound.get('binding_hash'), str)
            and re.fullmatch(r'[a-f0-9]{64}', bound['binding_hash']) is not None
            and all(isinstance(bound.get(k), str) and _ID.fullmatch(bound[k])
                    for k in ('company_id', 'contact_id')), 'company_qualification_identity_required')
        return row, leads[0], dict(bound)

    def _fresh_identity(self, lead, bound):
        evidence = HubSpotEligibilityRead(self.console.port).lookup(lead['email'],
            contact_id=bound['contact_id'], profile=lead.get('linkedin_url') or None)
        fresh = candidate(evidence)
        _require(fresh is not None and fresh['binding_hash'] == bound['binding_hash'],
                 'company_qualification_identity_changed')

    def _view(self, saved):
        return {**deepcopy(saved), 'preview_id': saved['id'], 'expected_version': saved['version'],
                'write_enabled': self.console._write_allowed(), 'retry_write': False}

    def preview(self, ident, source_key, facts, actor, expected_version, review_hash):
        self._actor(actor)
        _require(isinstance(facts, Mapping) and facts.get('not_customer') is True
            and facts.get('not_excluded') is True, 'company_qualification_explicit_review_required', 400)
        with self.repo.transaction():
            row, lead, bound = self._binding(ident, source_key, expected_version, review_hash)
        try:
            qualification = review({k: v for k, v in facts.items() if k not in {'not_customer', 'not_excluded'}},
                bound, row['sheet_hash'], self.journey._now())
        except OverflowError:
            raise QualificationError('journey_qualification_invalid') from None
        _require(facts['country'] == 'BR', 'company_qualification_country_unsupported', 400)
        _require(facts['decision'] != 'qualified'
            or decision(qualification, bound, row['sheet_hash'], self.journey._now())['fits'],
            'company_qualification_icp_facts_conflict', 400)
        qualification.update(not_customer=True, not_excluded=True, source_kind='human_company_review',
            company_id=bound['company_id'], contact_id=bound['contact_id'])
        self._fresh_identity(lead, bound)
        provenance = {'kind': PURPOSE, 'source_kind': 'human_company_review', 'actor': actor,
            'company_id': bound['company_id'], 'binding_hash': bound['binding_hash'],
            'company_identity_hash': payload_hash({key: bound[key] for key in ('company_id', 'company_name', 'domain')}),
            'sheet_hash': row['sheet_hash'], 'not_customer': True, 'not_excluded': True,
            'source_url': facts['source_url'], 'source_date': facts['source_date'],
            'reviewed_at': qualification['reviewed_at'], 'rationale': facts['rationale'],
            'facts_hash': payload_hash(facts)}
        values = {'name': bound['company_name'], 'domain': bound['domain'],
            'thunder_icp_status': facts['decision'], 'thunder_is_customer': 'false',
            'thunder_icp_exclusion_reason': '', 'country': 'BR', 'hs_country_code': 'BR',
            'thunder_icp_sector': facts['sector'],
            'thunder_annual_revenue_estimate': format(Decimal(str(facts['annual_revenue_brl'])), 'f'),
            'thunder_last_enriched_at': self.journey._now().date().isoformat(),
            'thunder_enrichment_source': json.dumps(provenance, ensure_ascii=False, sort_keys=True)}
        effect = 'company-qualification-' + payload_hash({'journey': ident, 'version': expected_version,
            'source_key': source_key, 'review_hash': review_hash, 'facts': facts})
        port = self._port()
        plan = port.prepare('companies', values, effect, bound['company_id'])
        key = effect
        unsigned = {'kind': PURPOSE, 'portal_id': PORTAL_ID, 'journey_id': ident,
            'journey_version': expected_version, 'journey_review_hash': review_hash,
            'source_key': source_key, 'row_hash': payload_hash(lead), 'sheet_hash': row['sheet_hash'],
            'company_identity': bound, 'facts': deepcopy(dict(facts)), 'qualification': qualification,
            'plan': plan, 'before': plan['before'], 'after': {**plan['before'],
                'properties': {**plan['before']['properties'], **values}},
            'created_by': actor, 'expires_at': self._now() + 300}
        with self.repo.transaction():
            self._binding(ident, source_key, expected_version, review_hash)
            current = self.repo.get_state('hubspot_preview', key)
            if current:
                # A new review never resets an expired/attempted one-shot effect.
                _require(current.get('kind') == PURPOSE, 'company_qualification_preview_conflict')
                return self._view(current)
            saved = self.repo.append_state('hubspot_preview', key, {**unsigned,
                'preview_hash': payload_hash(unsigned), 'state': 'review_required', 'external_writes': 0})
        return self._view(saved)

    def _current_binding(self, preview):
        row, lead, bound = self._binding(preview['journey_id'], preview['source_key'],
            preview['journey_version'], preview['journey_review_hash'])
        _require(payload_hash(lead) == preview['row_hash'] and bound == preview['company_identity']
            and row['sheet_hash'] == preview['sheet_hash'], 'company_qualification_binding_changed')
        return lead, bound

    def _authorize(self, approved):
        current = self.repo.get_state('hubspot_preview', approved['id'])
        if not (self.console._write_allowed() and current and current['state'] == 'executing'
                and current.get('approved_by') == 'ygor' and current['version'] == approved['version']
                and current['expires_at'] > self._now() and current['preview_hash'] == approved['preview_hash']
                and current['preview_hash'] == payload_hash({k: current.get(k) for k in _HASH_FIELDS})):
            return False
        try:
            self._current_binding(current)
            return True
        except NewJourneyError:
            return False

    def _load(self, body, actor, execute=False):
        self._actor(actor)
        fields = {'preview_id', 'expected_version', 'preview_hash'} | ({'confirm'} if execute else set())
        _require(isinstance(body, Mapping) and set(body) == fields
            and (not execute or body.get('confirm') is True) and type(body.get('expected_version')) is int
            and isinstance(body.get('preview_id'), str), 'company_qualification_exact_review_required', 400)
        preview = self.repo.get_state('hubspot_preview', body['preview_id'])
        _require(preview is not None and preview.get('kind') == PURPOSE, 'company_qualification_preview_missing', 404)
        _require(preview['version'] == body['expected_version']
            and preview['preview_hash'] == body['preview_hash'], 'company_qualification_preview_conflict')
        _require(preview['preview_hash'] == payload_hash({k: preview.get(k) for k in _HASH_FIELDS}),
                 'company_qualification_preview_integrity_mismatch')
        return preview

    def execute(self, body, actor):
        with self.repo.transaction():
            preview = self._load(body, actor, execute=True)
            _require(self.console._write_allowed(), 'hubspot_writes_disabled_or_paused')
            _require(preview['state'] == 'review_required' and preview['expires_at'] > self._now(),
                     'company_qualification_preview_not_current')
            self._current_binding(preview)
            approved = self.repo.put_states_atomic([('hubspot_preview', preview['id'],
                {**{k: v for k, v in preview.items() if k not in _META},
                 'state': 'executing', 'approved_by': actor}, preview['version'])])[0]
        outer, port, writes = self, self._port(), 0

        class Guarded:
            def __getattr__(self, name):
                return getattr(port, name)

            def write(self, request):
                nonlocal writes
                plan = approved['plan']
                _require(request == {'method': 'PATCH', 'path': '/crm/v3/objects/companies/' + plan['record_id'],
                    'body': {'properties': plan['changes']}}
                    and set(plan['changes']) <= QUALIFICATION_FIELDS, 'company_qualification_write_blocked')
                # Existing repository fence serializes local pause/draft changes;
                # attempted intent was committed by HubSpotExecutor beforehand.
                with outer.repo.transaction():
                    _require(outer._authorize(approved), 'hubspot_writes_disabled_or_paused')
                    lead, bound = outer._current_binding(approved)
                    outer._fresh_identity(lead, bound)
                    fresh = port.prepare('companies', plan['properties'], plan['effect_id'], plan['record_id'])
                    _require(fresh == plan, 'company_qualification_target_changed')
                    _require(outer._authorize(approved), 'hubspot_writes_disabled_or_paused')
                    writes += 1
                    response = port.write(request)
                    _require(isinstance(response, Mapping) and response.get('id') == plan['record_id'],
                             'company_qualification_receipt_mismatch')
                    return response

        executor = HubSpotExecutor(self.repo, Guarded(), enabled=True,
            authorize=lambda plan: plan == approved['plan'] and self._authorize(approved))
        try:
            lead, bound = self._current_binding(approved)
            self._fresh_identity(lead, bound)
            result = executor.execute(approved['plan'])
            result.setdefault('retry_write', False)
        except Exception as exc:
            result = {'status': 'needs_human', 'retry_write': False,
                'error_code': exc.code if isinstance(exc, NewJourneyError)
                              else 'company_qualification_preflight_failed'}
        return self._save_result(approved, result, writes)

    def _save_result(self, preview, result, writes=None):
        with self.repo.transaction():
            current = self.repo.get_state('hubspot_preview', preview['id'])
            _require(current is not None and current['version'] == preview['version'],
                     'company_qualification_preview_conflict')
            payload = {**{k: v for k, v in current.items() if k not in _META},
                       'state': 'processed', 'result': result}
            if writes is not None:
                payload['external_writes'] = writes
            updates = []
            if result.get('status') == 'verified_state' and not current.get('qualification_saved'):
                try:
                    self._current_binding(current)
                    draft = self.journey._read(current['journey_id'], current['journey_version'])
                except NewJourneyError as exc:
                    # The remote readback remains true; preserve a concurrently
                    # edited draft and expose the local conflict separately.
                    payload.update(qualification_saved=False, qualification_error_code=exc.code)
                else:
                    draft = {k: v for k, v in draft.items() if k not in _META}
                    draft.setdefault('qualifications', {})[current['source_key']] = deepcopy(current['qualification'])
                    draft.update(checks=[], approvals={}, export_snapshot=None, reconciliation=None)
                    updates.append(('journey_draft', current['journey_id'], draft, current['journey_version']))
                    payload.update(qualification_saved=True, qualification_error_code=None,
                                   journey_version_after_review=current['journey_version'] + 1)
            updates.append(('hubspot_preview', current['id'], payload, current['version']))
            saved = self.repo.put_states_atomic(updates)[-1]
        return self._view(saved)

    def reconcile(self, body, actor):
        """Read-only recovery, even while paused/expired; never reset an attempt."""
        preview = self._load(body, actor)
        _require(preview['state'] in {'executing', 'processed'}, 'company_qualification_attempt_required')
        result = HubSpotExecutor(self.repo, self._port()).reconcile(preview['plan'])
        return self._save_result(preview, result)
