"""Internal journey drafts and exact human review; manual CSV handoff only.

Uses the existing repository and ApprovalPack. Injected synchronous read adapters
own their timeouts; HTTP composition must run this service outside its event loop.
No cadence, outbox, external writer, paid lookup or enrollment port exists here.
"""
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import csv
import io
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from .approvals import ApprovalError, ApprovalPack, ApprovalService
from .db import RepositoryConflict
from .models import payload_hash


CHECK_KEYS = ('icp', 'exclusions', 'client', 'open_deal', 'refusal', 'membership')
MAX_ROWS = 200
MAX_BYTES = 100 * 1024
_META = {'id', 'version', 'updated_at'}
_UNSET = object()
_ACCOUNT_PROOFS = {'injected', 'verified', 'verified_profile',
                   'provider_contact_team', 'provider_response_team'}
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\Z")
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class NewJourneyError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _require(condition, code, status=400):
    if not condition:
        raise NewJourneyError(code, status)


def _text(value, limit=500, *, empty=False, multiline=False):
    return (isinstance(value, str) and len(value) <= limit
            and (empty or bool(value.strip()))
            and not any((ord(c) < 32 and not (multiline and c in '\n\r\t'))
                        or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _valid_email(value):
    return (isinstance(value, str) and len(value) <= 254 and bool(_EMAIL.fullmatch(value))
            and len(value.split('@')[0]) <= 64 and '..' not in value
            and not value.startswith('.') and '.@' not in value
            and value.lower() != 'email_not_unlocked@domain.com')


def _linkedin(value):
    if not value:
        return ''
    try:
        url = urlsplit(value if '://' in value else 'https://' + value)
        if (url.scheme != 'https' or url.hostname not in ('linkedin.com', 'www.linkedin.com')
                or url.username or url.password or url.port or url.query or url.fragment
                or not re.fullmatch(r'/in/[A-Za-z0-9_%.-]+/?', url.path)):
            return None
        return 'https://www.linkedin.com' + url.path.rstrip('/').lower()
    except ValueError:
        return None


def _safe_cell(value):
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value


def _require_exact_copy(step):
    _require(all(_safe_cell(step.get(field)) == (step.get(field) or '')
                 for field in ('subject', 'content')), 'journey_csv_copy_unsafe')


class NewJourney:
    def __init__(self, service, apollo=None, checker=None, clock=None, *, template_reader=None, commercial_accounts=None, history_reader=None):
        self.service, self.repo = service, service.repository
        self.apollo, self.checker = apollo, checker
        self.template_reader = template_reader
        self.commercial_accounts = commercial_accounts
        self.history_reader = history_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.approval_service = ApprovalService(service.global_paused)

    def _now(self):
        now = self.clock()
        _require(isinstance(now, datetime) and now.tzinfo is not None,
                 'journey_clock_invalid', 503)
        return now

    @staticmethod
    def _actor(actor):
        _require(actor == 'ygor', 'journey_human_authority_required', 403)

    def _running(self):
        _require(not self.service.global_paused(), 'journey_global_paused', 409)

    def _read(self, ident, expected_version=_UNSET):
        _require(isinstance(ident, str) and bool(_ID.fullmatch(ident)), 'journey_id_invalid')
        row = self.repo.get_state('journey_draft', ident)
        _require(row is not None, 'journey_missing', 404)
        if expected_version is not _UNSET:
            _require(type(expected_version) is int and expected_version >= 1,
                     'journey_version_invalid')
            _require(row['version'] == expected_version, 'journey_version_conflict', 409)
        return row

    def _campaign(self, campaign_id):
        _require(isinstance(campaign_id, str) and campaign_id.startswith('BR-'),
                 'journey_campaign_invalid')
        try:
            context = self.service.campaign_registry.load(campaign_id)
            _require(context['platform'] == 'apollo', 'journey_campaign_invalid')
            sheet = self.service.campaign_sheet_view(campaign_id)
        except ApprovalError:
            raise NewJourneyError('journey_campaign_unavailable', 409) from None
        return context, sheet

    @staticmethod
    def _sheet_hash(sheet):
        def stable(value):
            if isinstance(value, Mapping):
                return {k: stable(v) for k, v in value.items() if k != 'source_updated_at'}
            if isinstance(value, list):
                return [stable(v) for v in value]
            return value
        return payload_hash(stable(sheet))

    def catalog(self):
        campaigns = []
        for item in self.service.campaign_registry.list_campaigns():
            if item['active'] and item['platform'] == 'apollo' and item['id'].startswith('BR-'):
                _, sheet = self._campaign(item['id'])
                campaigns.append({**item, 'campaign_id': item['id'], 'sheet': sheet})
        drafts = sorted(self.repo.list_state('journey_draft'),
                        key=lambda row: row['updated_at'], reverse=True)[:20]
        return {'campaigns': campaigns, 'drafts': [{key: row[key] for key in
                ('id', 'name', 'campaign_id', 'version', 'updated_at')} for row in drafts],
                'enrollment_enabled': False,
                'external_writes': 0, 'credits': 0, 'check_keys': list(CHECK_KEYS)}

    def _save(self, row, expected_version):
        try:
            return self.repo.put_states_atomic([('journey_draft', row['id'],
                {key: value for key, value in row.items() if key not in _META}, expected_version)])[0]
        except RepositoryConflict:
            raise NewJourneyError('journey_version_conflict', 409) from None

    def create(self, data, actor):
        self._actor(actor)
        _require(isinstance(data, Mapping) and set(data) == {'campaign_id', 'name'}
                 and _text(data['name'], 160), 'journey_input_invalid')
        with self.repo.transaction():
            self._running()
            context, sheet = self._campaign(data['campaign_id'])
            row = self._save({'id': 'journey-' + uuid4().hex, 'name': data['name'],
                'campaign_id': data['campaign_id'], 'actor': actor, 'source': None,
                'rows': [], 'checks': [], 'steps': [], 'approvals': {},
                'campaign_context': context, 'sheet_hash': self._sheet_hash(sheet),
                'sequence': None, 'reconciliation': None, 'qualifications': {}, 'export_snapshot': None}, 0)
            return self._view(row)

    def get(self, ident, actor):
        self._actor(actor)
        with self.repo.transaction():
            return self._view(self._read(ident))

    def history(self, ident, source_key, actor):
        """Read the bound person's originals without changing the draft or CRM."""
        self._actor(actor)
        _require(_text(source_key, 200), 'journey_recipient_required', 404)
        with self.repo.transaction():
            row = self._read(ident)
            leads = [lead for lead in row['rows'] if lead['source_key'] == source_key]
            contexts = [item.get('qualification_context') for item in row['checks']
                        if item['source_key'] == source_key]
        _require(len(leads) == 1, 'journey_recipient_required', 404)
        _require(len(contexts) == 1 and isinstance(contexts[0], Mapping)
                 and _text(contexts[0].get('contact_id'), 128)
                 and _text(contexts[0].get('company_id'), 128)
                 and isinstance(contexts[0].get('binding_hash'), str)
                 and re.fullmatch(r'[a-f0-9]{64}', contexts[0]['binding_hash']) is not None,
                 'journey_identity_unverified', 409)
        _require(self.history_reader is not None, 'journey_history_unavailable', 503)
        result = self.history_reader.read(deepcopy(leads[0]), deepcopy(contexts[0]))
        _require(isinstance(result, Mapping) and result.get('read_only') is True,
                 'journey_history_unavailable', 503)
        return {**deepcopy(result), 'journey_id': row['id'], 'source_key': source_key,
                'journey_version': row['version'], 'external_writes': 0}

    def _commercial(self, row, sheet=None):
        if self.commercial_accounts is None:
            return {}
        contexts = {item['source_key']: item.get('qualification_context')
                    for item in row.get('checks', [])}
        try:
            result = self.commercial_accounts.project_many(
                deepcopy(row.get('rows', [])), deepcopy(contexts), sheet=sheet)
            return result if isinstance(result, dict) else {}
        except Exception:
            return {item['source_key']: {'status': 'unavailable',
                    'limitations': ['commercial_source_unavailable']}
                    for item in row.get('rows', [])}

    def _commercial_ready(self, row):
        if self.commercial_accounts is None:
            return True  # Optional port in pre-existing isolated consumers.
        observed = self._commercial(row)
        for lead in self._eligible(row):
            item = observed.get(lead['source_key'], {})
            pending = item.get('limitations') or []
            sections = {section.get('key'): section.get('items') or []
                        for section in item.get('sections', []) if isinstance(section, Mapping)}
            required = ('business', 'fit', 'people', 'thesis')
            if item.get('status') != 'available' or any(
                    'commercial_section_pending:' + key in pending
                    or not any(isinstance(part, Mapping) and _text(part.get('text'), 524288, multiline=True)
                               for part in sections.get(key, [])) for key in required):
                return False
        return True

    @staticmethod
    def _stable_checks(checks):
        # Collection time is display provenance, not a change in evidence.
        return [{**item, 'checks': [{key: value for key, value in check.items()
                                    if key not in ('checked_at', 'observed_at')}
                                   for check in item.get('checks', [])]}
                for item in checks]

    def _scope(self, row):
        values = {key: row.get(key) for key in ('name', 'campaign_id', 'source',
            'rows', 'checks', 'steps', 'sequence', 'campaign_context', 'sheet_hash', 'qualifications')}
        values['checks'] = self._stable_checks(row.get('checks', []))
        if self.commercial_accounts is not None:
            # The source used in a commercial review is approval input. A fresh
            # content hash invalidates the existing pack; no second ledger/state.
            values['commercial_sources'] = {
                key: {'status': item.get('status'), 'source': {
                    field: (item.get('source') or {}).get(field)
                    for field in ('path', 'sha256')},
                    'context': (item.get('originals') or {}).get('tabs', {}).get('context', [])}
                for key, item in self._commercial(row).items()}
        if 'approval_mode' in row:
            values['approval_mode'] = row['approval_mode']
        return payload_hash(values)

    @staticmethod
    def _text_parity(row):
        snapshot = (row.get('sequence') or {}).get('template_snapshot') or {}
        if row.get('approval_mode') == 'apollo_prompts_v1':
            from .apollo_prompt_inputs import prompt_inputs
            observed = prompt_inputs(snapshot)
            return {**observed, 'fingerprint': observed.get('input_hash'),
                    'steps': snapshot.get('items', []), 'approval_kind': 'prompts',
                    'final_email_parity': False}
        result = {'status': 'unverified', 'reason': 'apollo_template_read_unavailable',
                  'fingerprint': snapshot.get('fingerprint'), 'steps': snapshot.get('items', [])}
        if snapshot.get('status') != 'ok' or snapshot.get('complete') is not True:
            return result
        if snapshot.get('source', {}).get('contact_overrides_verified') is not True:
            return {**result, 'reason': 'apollo_contact_overrides_unverified'}
        if snapshot.get('source', {}).get('static_safe') is not True:
            return {**result, 'reason': 'apollo_dynamic_or_extra_content'}
        steps = sorted(row.get('steps', []), key=lambda step: step['order'])
        actual = snapshot.get('items', [])
        if not steps or len(steps) != len(actual):
            return {**result, 'status': 'mismatch', 'reason': 'apollo_step_count_mismatch'}
        for approved, observed in zip(steps, actual):
            if observed.get('flags', {}).get('static_safe') is not True:
                return {**result, 'status': 'unverified', 'reason': 'apollo_dynamic_or_extra_content'}
            if any((approved.get(key) or '') != (observed.get(key) or '')
                   for key in ('channel', 'subject', 'content')):
                return {**result, 'status': 'mismatch', 'reason': 'apollo_template_text_mismatch'}
        return {**result, 'status': 'verified', 'reason': 'apollo_static_text_exact_match'}

    def _recipient_bindings(self, row):
        checks = {entry['source_key']: entry['checks'] for entry in row['checks']}
        bindings = []
        for lead in self._eligible(row):
            membership = next((c for c in checks[lead['source_key']] if c['key'] == 'membership'), {})
            matches = [b for b in membership.get('coverage', {}).get('apollo_recipients', [])
                       if b.get('email') == lead['email'] and b.get('lookup_complete') is True]
            if len(matches) != 1:
                return None
            bindings.append(deepcopy(matches[0]))
        return bindings

    def _review_hash(self, row, context, sheet, *, scope_hash=None):
        return payload_hash({'id': row['id'], 'version': row['version'],
            'scope_hash': self._scope(row) if scope_hash is None else scope_hash, 'context': context, 'sheet_hash': self._sheet_hash(sheet),
            'approvals': row['approvals']})

    def _eligible(self, row):
        checks = {item['source_key']: item['checks'] for item in row['checks']}
        return [item for item in row['rows'] if not item['blockers']
                and _valid_email(item['email']) and len(checks.get(item['source_key'], [])) == 6
                and all(c['status'] == 'pass' for c in checks[item['source_key']])]

    def _view(self, row):
        try:
            context, sheet = self._campaign(row['campaign_id'])
        except NewJourneyError:
            context, sheet = None, self.service.campaign_sheet_view(row['campaign_id'])
        blockers = []
        if context != row['campaign_context']:
            blockers.append('journey_campaign_changed')
        if sheet.get('status') != 'complete':
            blockers.append('journey_sheet_incomplete')
        if self._sheet_hash(sheet) != row['sheet_hash']:
            blockers.append('journey_sheet_changed')
        if self.service.global_paused():
            blockers.append('journey_global_paused')
        if self.checker is None:
            blockers.append('journey_checker_unavailable')
        eligible = self._eligible(row)
        if not eligible:
            blockers.append('journey_no_eligible_rows')
        elif not self._commercial_ready(row):
            blockers.append('journey_commercial_research_pending')
        if not any(step['channel'] == 'email' for step in row['steps']):
            blockers.append('journey_email_steps_required')
        if not row.get('sequence'):
            blockers.append('journey_sequence_required')
        parity = self._text_parity(row)
        if parity['status'] != 'verified':
            blockers.append('journey_apollo_text_unverified')
        steps = []
        for step in row['steps']:
            status = 'pending'
            if step['id'] in row['approvals']:
                try:
                    self._authorize(row, step, eligible)
                    status = 'approved'
                except (NewJourneyError, ApprovalError):
                    status = 'invalidated'
            if status != 'approved':
                blockers.append('journey_step_approval_required')
            step_blockers = [b for b in blockers if b != 'journey_step_approval_required']
            if step['channel'] == 'linkedin' and any(not lead['linkedin_url'] for lead in eligible):
                step_blockers.append('journey_recipient_required')
            steps.append({**step, 'approval_status': status,
                'can_approve': not step_blockers, 'blockers': sorted(set(step_blockers))})
        checks_by_key = {item['source_key']: item['checks'] for item in row['checks']}
        rows = [{**item, 'checks': checks_by_key.get(item['source_key'], self._unknown()),
                 'eligible': item['source_key'] in {lead['source_key'] for lead in eligible}}
                for item in row['rows']]
        return {**deepcopy(row), 'rows': rows, 'steps': steps, 'sheet': sheet,
            'review_hash': self._review_hash(row, context, sheet),
            'expected_version': row['version'], 'blockers': sorted(set(blockers)),
            'counts': {'total': len(rows), 'eligible': len(eligible),
                       'retained': len(rows) - len(eligible)},
            'cost': {'credits': 0, 'paid_lookup_enabled': False}, 'credits': 0,
            'enrollment_enabled': False, 'external_writes': 0, 'manual_export': True,
            'can_export': not blockers, 'check_keys': list(CHECK_KEYS),
            'apollo_text_parity': parity, 'approval_validity_hours': 24,
            'approval_mode': row.get('approval_mode', 'exact_copy_v1'),
            'commercial_accounts': self._commercial(row, sheet),
            'cadence_paths': [
                {'id': 'apollo', 'label': 'Apollo', 'approval': 'prompt_hash_and_sent_audit'},
                {'id': 'instantly', 'label': 'Instantly', 'approval': 'provider_sequence_and_exact_inputs'},
                {'id': 'thunder', 'label': 'Cadência Thunder', 'approval': 'exact_touch_text'}]}

    @staticmethod
    def _unknown(reason='check_not_observed'):
        return [{'key': key, 'status': 'unknown', 'reason': reason, 'source': None}
                for key in CHECK_KEYS]

    @staticmethod
    def _rows(raw, mode):
        _require(isinstance(raw, list) and len(raw) <= MAX_ROWS, 'journey_rows_invalid')
        result, keys = [], set()
        for index, item in enumerate(raw):
            _require(isinstance(item, Mapping), 'journey_row_invalid')
            def value(*names):
                v = next((item[name] for name in names if item.get(name) is not None), '')
                _require(_text(v, 2000, empty=True, multiline=True), 'journey_row_invalid')
                return v
            person_id = value('apollo_person_id', 'person_id')
            contact_id = value('apollo_contact_id')
            if mode == 'apollo_people':
                person_id, contact_id = value('id'), ''
            elif mode == 'apollo_contacts':
                contact_id = value('id')
            source_key = (mode + ':' + (person_id if mode == 'apollo_people' else contact_id)
                          if mode != 'csv' else 'csv:' + str(index + 1))
            _require(source_key not in keys and (mode == 'csv' or bool(_ID.fullmatch(
                person_id if mode == 'apollo_people' else contact_id))), 'journey_source_id_invalid')
            keys.add(source_key)
            address = value('email', 'email_address').strip()
            if mode == 'apollo_people':
                address = ''
            profile = value('linkedin_url', 'linkedin')
            normalized_profile = _linkedin(profile)
            name = value('name')
            # Do not reconstruct an obfuscated surname or infer an identity.
            company = value('company', 'organization_name', 'company_name')
            if not company and isinstance(item.get('organization'), Mapping):
                company = item['organization'].get('name', '')
                _require(_text(company, 2000, empty=True), 'journey_row_invalid')
            variables = item.get('variables', {})
            _require(isinstance(variables, Mapping) and len(variables) <= 30
                and all(isinstance(k, str) and bool(_ID.fullmatch(k))
                        and _text(v, 2000, empty=True, multiline=True) for k, v in variables.items()),
                'journey_variables_invalid')
            blockers = []
            if not _valid_email(address):
                blockers.append('email_required' if not address else 'email_invalid')
            elif address.startswith(('=', '+', '-', '@')):
                blockers.append('email_csv_unsafe')
            if normalized_profile is None:
                blockers.append('linkedin_invalid')
            if item.get('email_unsubscribed') is True:
                blockers.append('email_unsubscribed')
            result.append({'source_key': source_key, 'name': name,
                'first_name': value('first_name'), 'last_name': value('last_name'),
                'company': company, 'email': address, 'linkedin_url': normalized_profile or profile,
                'apollo_person_id': person_id, 'apollo_contact_id': contact_id,
                'hubspot_id': value('hubspot_id', 'hubspot_contact_id'),
                'variables': dict(variables), 'blockers': blockers,
                'source_record_hash': payload_hash(dict(item))})
        for field in ('email', 'linkedin_url'):
            duplicates = Counter(row[field].lower() for row in result if row[field])
            for row in result:
                if row[field] and duplicates[row[field].lower()] > 1:
                    row['blockers'].append('duplicate_' + field)
        return result

    @staticmethod
    def _csv(text):
        _require(isinstance(text, str), 'journey_csv_invalid')
        try:
            _require(len(text.encode('utf-8')) <= MAX_BYTES, 'journey_source_too_large')
            text = text.removeprefix('\ufeff')
            first = next(csv.reader(io.StringIO(text, newline=''), delimiter=';'), [])
            delimiter = ';' if len(first) > 1 else ','
            reader = csv.DictReader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True)
            fields = reader.fieldnames
            _require(fields and len(fields) <= 60, 'journey_csv_headers_invalid')
            fields = [field.strip().lower().replace(' ', '_') for field in fields]
            _require(len(set(fields)) == len(fields) and all(fields)
                and ('email' in fields or 'email_address' in fields), 'journey_csv_email_required')
            reader.fieldnames = fields
            result = []
            for item in reader:
                _require(None not in item and all(v is not None for v in item.values()),
                         'journey_csv_row_invalid')
                variables = {k[4:]: v for k, v in item.items() if k.startswith('var_')}
                result.append({**item, 'variables': variables})
                _require(len(result) <= MAX_ROWS, 'journey_source_too_large')
            return result
        except (csv.Error, UnicodeError):
            raise NewJourneyError('journey_csv_invalid') from None

    def _provider(self, method, **kwargs):
        _require(self.apollo is not None and callable(getattr(self.apollo, method, None)),
                 'journey_apollo_unavailable', 503)
        try:
            response = getattr(self.apollo, method)(**kwargs)
        except Exception:
            raise NewJourneyError('journey_apollo_unavailable', 503) from None
        if isinstance(response, Mapping) and response.get('status') == 'error':
            reason = response.get('reason')
            if reason in {'permission_denied', 'credential_unavailable', 'credential_rejected',
                          'disabled', 'configuration_pending', 'rate_limited', 'timeout'}:
                raise NewJourneyError('journey_apollo_' + reason, 503)
        _require(isinstance(response, Mapping) and response.get('status') in ('ok', 'partial')
            and response.get('credit_cost') == 0 and isinstance(response.get('items'), list),
            'journey_apollo_unavailable', 503)
        return response

    def source(self, ident, data, actor, expected_version):
        self._actor(actor)
        _require(isinstance(data, Mapping), 'journey_source_invalid')
        mode = data.get('mode')
        fields = {'csv': {'mode', 'csv_text'}, 'apollo_people': {'mode', 'filters', 'page'},
                  'apollo_contacts': {'mode', 'query', 'page'}}
        _require(isinstance(mode, str) and mode in fields and set(data) == fields[mode],
                 'journey_source_invalid')
        with self.repo.transaction():
            self._running()
            self._read(ident, expected_version)
        if mode == 'csv':
            raw = self._csv(data['csv_text'])
            descriptor = {'mode': mode, 'content_hash': payload_hash({'text': data['csv_text']})}
        else:
            _require(type(data['page']) is int and 1 <= data['page'] <= 500,
                     'journey_page_invalid')
            key, method = ('filters', 'people_search') if mode == 'apollo_people' else ('query', 'contacts_search')
            _require((isinstance(data[key], Mapping) if key == 'filters' else _text(data[key])),
                     'journey_source_invalid')
            if mode == 'apollo_people':
                filters = data['filters']
                def explicit_list(name):
                    values = filters.get(name)
                    return (isinstance(values, list) and bool(values)
                            and all(_text(value, 200) for value in values))
                _require(any(explicit_list(name) for name in
                             ('person_locations', 'organization_locations')),
                         'journey_geography_required')
                _require(any(explicit_list(name) for name in
                             ('person_titles', 'organization_num_employees_ranges'))
                         or _text(filters.get('q_keywords')), 'journey_business_filter_required')
            response = self._provider(method, **{key: data[key], 'page': data['page'], 'per_page': 100})
            raw = response['items']
            descriptor = {**deepcopy(dict(data)), 'provider_source': response.get('source'),
                'complete': response.get('complete') is True, 'reason': response.get('reason')}
        rows = self._rows(raw, mode)
        _require(len(json.dumps(rows).encode('utf-8')) <= MAX_BYTES, 'journey_source_too_large')
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
            context, sheet = self._campaign(row['campaign_id'])
            row.update(source=descriptor, rows=rows, checks=[], approvals={}, reconciliation=None,
                       qualifications={}, export_snapshot=None,
                       campaign_context=context, sheet_hash=self._sheet_hash(sheet))
            return self._view(self._save(row, expected_version))

    def _run_checks(self, row):
        observed = []
        if self.checker is not None:
            try:
                from .new_journey_checks import NewJourneyChecks
                kwargs = ({'qualifications': row.get('qualifications', {})}
                          if isinstance(self.checker, NewJourneyChecks) else {})
                observed = self.checker(deepcopy(row['rows']), row['campaign_id'], **kwargs)
            except Exception:
                observed = []
        if not isinstance(observed, list):
            observed = []
        by_key = {}
        for item in observed:
            if isinstance(item, Mapping) and isinstance(item.get('source_key'), str):
                by_key.setdefault(item['source_key'], []).append(item)
        result = []
        for lead in row['rows']:
            matches = by_key.get(lead['source_key'], [])
            context = matches[0].get('qualification_context') if len(matches) == 1 else None
            evidence = matches[0].get('checks', []) if len(matches) == 1 else []
            if not isinstance(evidence, list):
                evidence = []
            normalized = self._unknown()
            for target in normalized:
                matches = [c for c in evidence if isinstance(c, Mapping) and c.get('key') == target['key']]
                if len(matches) == 1:
                    check = matches[0]
                    if (check.get('status') in ('pass', 'blocked', 'unknown')
                            and _text(check.get('reason'), 2000)
                            and _text(check.get('source'), 2000)):
                        target.update({key: check[key] for key in ('status', 'reason', 'source')})
                        if _text(check.get('checked_at'), 100):
                            target['checked_at'] = check['checked_at']
                        if target['key'] == 'membership' and isinstance(check.get('coverage'), Mapping):
                            target['coverage'] = deepcopy(check['coverage'])
                        if target['key'] == 'refusal' and isinstance(check.get('observation'), Mapping):
                            target['observation'] = deepcopy(check['observation'])
                        # Stable coverage/revision fingerprints are review inputs;
                        # consultation times do not make an unchanged check stale.
                        stable = {k: v for k, v in check.items() if k not in ('checked_at', 'observed_at')}
                        try:
                            encoded = json.dumps(stable, allow_nan=False)
                            if len(encoded) > 16384:
                                raise ValueError('evidence too large')
                            target['evidence_hash'] = payload_hash(stable)
                        except (TypeError, ValueError):
                            target.update(status='unknown', reason='check_evidence_invalid', source=None)
            result.append({'source_key': lead['source_key'], 'checks': normalized,
                           **({'qualification_context': deepcopy(context)} if isinstance(context, Mapping) else {})})
        return result

    def qualify(self, ident, source_key, facts, actor, expected_version, review_hash):
        """Review ICP in the draft, never write qualifications into the CRM."""
        self._actor(actor)
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
            context, sheet = self._campaign(row['campaign_id'])
            _require(review_hash == self._review_hash(row, context, sheet), 'journey_review_conflict', 409)
            _require(any(lead['source_key'] == source_key for lead in row['rows']), 'journey_recipient_required', 409)
        checks = self._run_checks(row)
        observed = next((item.get('qualification_context') for item in checks if item['source_key'] == source_key), None)
        presented = next((item.get('qualification_context') for item in row['checks']
                          if item['source_key'] == source_key), None)
        _require(isinstance(presented, Mapping) and isinstance(observed, Mapping)
                 and isinstance(presented.get('binding_hash'), str)
                 and re.fullmatch(r'[a-f0-9]{64}', presented['binding_hash']) is not None
                 and presented.get('binding_hash') == observed.get('binding_hash'),
                 'journey_checks_changed', 409)
        from .new_journey_qualification import review
        record = review(facts, observed, self._sheet_hash(sheet), self._now())
        row.setdefault('qualifications', {})[source_key] = record
        updated_checks = self._run_checks(row)
        with self.repo.transaction():
            self._revalidate(row, context, sheet)
            row.update(checks=updated_checks, approvals={}, export_snapshot=None, reconciliation=None)
            return self._view(self._save(row, expected_version))

    def check(self, ident, actor, expected_version):
        self._actor(actor)
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
            context, sheet = self._campaign(row['campaign_id'])
        checks = self._run_checks(row)
        with self.repo.transaction():
            self._running()
            self._read(ident, expected_version)
            _require(self._campaign(row['campaign_id']) == (context, sheet),
                     'journey_context_changed', 409)
            row.update(checks=checks, approvals={}, campaign_context=context,
                       sheet_hash=self._sheet_hash(sheet))
            return self._view(self._save(row, expected_version))

    def sequences(self, ident, query, actor, expected_version):
        self._actor(actor)
        _require(_text(query, empty=True), 'journey_sequence_query_invalid')
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
        response = self._provider('sequences_search', query=query, page=1, per_page=100)
        with self.repo.transaction():
            self._running()
            self._read(ident, expected_version)
        return {**deepcopy(response), 'journey_id': row['id'], 'version': row['version'],
                'enrollment_enabled': False, 'external_writes': 0, 'credits': 0}

    def _sequence(self, campaign_id, ident, query, recipients=None):
        _require(isinstance(ident, str) and bool(_ID.fullmatch(ident)) and _text(query, empty=True),
                 'journey_sequence_invalid')
        if callable(getattr(self.apollo, 'profile', None)):
            profile = self._provider('profile')
            _require(profile.get('status') == 'ok' and profile.get('complete') is True,
                     'journey_apollo_configuration_pending', 503)
        response = self._provider('sequences_search', query=query, page=1, per_page=100)
        matches = [item for item in response['items'] if isinstance(item, Mapping) and item.get('id') == ident]
        _require(len(matches) == 1, 'journey_sequence_unverified', 409)
        item, source = matches[0], response.get('source') or {}
        account_id = source.get('account_id')
        _require(_text(account_id) and source.get('identity_source') in _ACCOUNT_PROOFS
            and _text(item.get('user_id')), 'journey_sequence_owner_unverified', 409)
        from .external_email_import import SOURCES
        known = next((account for account, _, campaign in SOURCES.values() if campaign == campaign_id), None)
        _require(known is None or known == account_id, 'journey_apollo_account_mismatch', 409)
        _require(item.get('archived') is False, 'journey_sequence_archived', 409)
        activity = ('active' if item.get('active') is True else
                    'inactive' if item.get('active') is False else 'unknown')
        snapshot = {'status': 'error', 'complete': False, 'items': [],
                    'reason': 'apollo_template_read_unavailable'}
        if self.template_reader is not None:
            try:
                _require(recipients is not None and all(
                    b.get('account_id') == account_id and b.get('lookup_complete') is True
                    and _valid_email(b.get('email')) for b in recipients),
                    'journey_apollo_recipient_unverified', 409)
                raw = self.template_reader.sequence_detail(ident, account_id, item['user_id'],
                                                           query=query, recipients=recipients)
                if isinstance(raw, Mapping) and raw.get('credit_cost') == 0:
                    snapshot = {key: deepcopy(raw.get(key)) for key in
                                ('status', 'complete', 'items', 'fingerprint', 'reason', 'source', 'snapshot')}
                    snapshot['fingerprint'] = raw.get('snapshot_hash') or raw.get('fingerprint')
            except Exception:
                pass
        return {'apollo_sequence_id': ident, 'query': query, 'account_id': account_id,
            'owner_id': item['user_id'], 'item': dict(item), 'activity_status': activity,
            'template_snapshot': snapshot,
            'source': {key: source.get(key) for key in
                       ('provider', 'operation', 'endpoint', 'account_id', 'identity_source')}}

    def revise_sequence(self, ident, steps, actor, expected_version,
                        apollo_sequence_id=None, apollo_sequence_query=None):
        self._actor(actor)
        _require(isinstance(steps, list) and 1 <= len(steps) <= 20, 'journey_steps_invalid')
        seen, orders = set(), set()
        for step in steps:
            _require(isinstance(step, Mapping) and set(step) == {
                'id', 'channel', 'subject', 'content', 'order', 'interval_hours'}, 'journey_step_invalid')
            _require(isinstance(step['id'], str) and bool(_ID.fullmatch(step['id']))
                and step['id'] not in seen and step['channel'] in ('email', 'linkedin')
                and _text(step['content'], 20000, multiline=True)
                and type(step['order']) is int and 1 <= step['order'] <= 20
                and step['order'] not in orders and type(step['interval_hours']) is int
                and 0 <= step['interval_hours'] <= 8760, 'journey_step_invalid')
            _require((_text(step['subject'], 998) if step['channel'] == 'email'
                     else step['subject'] in ('', None)), 'journey_subject_invalid')
            _require(not any('{{' in (step.get(field) or '') or '}}' in (step.get(field) or '')
                             for field in ('subject', 'content')), 'journey_unresolved_variables')
            _require_exact_copy(step)
            seen.add(step['id']); orders.add(step['order'])
        _require(orders == set(range(1, len(steps) + 1)), 'journey_step_order_invalid')
        _require((apollo_sequence_id is None) == (apollo_sequence_query is None),
                 'journey_sequence_pair_required')
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
        selected = (self._sequence(row['campaign_id'], apollo_sequence_id, apollo_sequence_query,
                                   self._recipient_bindings(row))
                    if apollo_sequence_id is not None else row.get('sequence'))
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
            row.update(steps=deepcopy(sorted(steps, key=lambda step: step['order'])),
                       sequence=selected, approvals={}, export_snapshot=None, reconciliation=None)
            row.pop('approval_mode', None)  # Static edits must re-enter exact-copy validation.
            return self._view(self._save(row, expected_version))

    def select_apollo_prompts(self, ident, sequence_id, query, actor, expected_version):
        """Select observed Apollo inputs without editing or generating provider content."""
        self._actor(actor)
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
            context, sheet = self._campaign(row['campaign_id'])
        selected = self._sequence(row['campaign_id'], sequence_id, query,
                                  self._recipient_bindings(row))
        proposed = {**row, 'sequence': selected, 'approval_mode': 'apollo_prompts_v1'}
        _require(self._text_parity(proposed)['status'] == 'verified',
                 'journey_apollo_inputs_unverified', 409)
        steps = []
        for item in selected['template_snapshot']['items']:
            _require(item.get('channel') == 'email' and type(item.get('interval_hours')) in (int, float)
                     and item['interval_hours'] == int(item['interval_hours']),
                     'journey_apollo_inputs_unverified', 409)
            steps.append({key: deepcopy(item[key]) for key in
                          ('id', 'channel', 'subject', 'content', 'order', 'interval_hours')})
        _require(bool(steps), 'journey_email_steps_required', 409)
        with self.repo.transaction():
            self._revalidate(row, context, sheet)
            row.update(sequence=selected, steps=steps, approval_mode='apollo_prompts_v1',
                       approvals={}, export_snapshot=None, reconciliation=None)
            return self._view(self._save(row, expected_version))

    def _preflight(self, ident, actor, expected_version, review_hash):
        self._actor(actor)
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
            context, sheet = self._campaign(row['campaign_id'])
            _require(sheet['status'] == 'complete', 'journey_sheet_incomplete', 409)
            presented_scope = self._scope(row)
            _require(isinstance(review_hash, str) and review_hash == self._review_hash(
                row, context, sheet, scope_hash=presented_scope), 'journey_review_conflict', 409)
            _require(context == row['campaign_context'], 'journey_campaign_changed', 409)
            _require(self._sheet_hash(sheet) == row['sheet_hash'], 'journey_sheet_changed', 409)
        checks = self._run_checks(row)
        _require(self.checker is not None and self._stable_checks(checks) == self._stable_checks(row['checks']), 'journey_checks_changed', 409)
        _require(bool(self._eligible(row)), 'journey_no_eligible_rows', 409)
        _require(self._commercial_ready(row), 'journey_commercial_research_pending', 409)
        _require(bool(row.get('sequence')), 'journey_sequence_required', 409)
        selected = row['sequence']
        observed = self._sequence(row['campaign_id'], selected['apollo_sequence_id'], selected['query'],
                                  self._recipient_bindings(row))
        if row.get('approval_mode') == 'apollo_prompts_v1':
            current = self._text_parity({**row, 'sequence': observed})
            saved = self._text_parity(row)
            _require(current['status'] == 'verified' and saved['status'] == 'verified'
                     and current.get('input_hash') == saved.get('input_hash')
                     and all(observed.get(k) == selected.get(k) for k in
                             ('apollo_sequence_id', 'account_id', 'owner_id')),
                     'journey_sequence_changed', 409)
        else:
            _require(observed == selected, 'journey_sequence_changed', 409)
        _require(self._text_parity(row)['status'] == 'verified', 'journey_apollo_text_unverified', 409)
        return row, context, sheet, presented_scope

    def _revalidate(self, row, context, sheet):
        self._running()
        self._read(row['id'], row['version'])
        _require(self._campaign(row['campaign_id']) == (context, sheet),
                 'journey_context_changed', 409)

    @staticmethod
    def _recipient_step(step, lead):
        _require_exact_copy(step)
        recipient = lead['email'] if step['channel'] == 'email' else lead['linkedin_url']
        _require(bool(recipient), 'journey_recipient_required', 409)
        return {'id': step['id'] + ':' + lead['source_key'], 'recipient': recipient,
            **{key: step[key] for key in ('channel', 'content', 'order', 'interval_hours')},
            **({'subject': step['subject']} if step['channel'] == 'email' else {})}

    def approve_step(self, ident, step_id, actor, expected_version, review_hash):
        row, context, sheet, presented_scope = self._preflight(ident, actor, expected_version, review_hash)
        step = next((s for s in row['steps'] if s['id'] == step_id), None)
        _require(step is not None, 'journey_step_missing', 404)
        if row.get('approval_mode') == 'apollo_prompts_v1':
            with self.repo.transaction():
                self._revalidate(row, context, sheet)
                now = self._now()
                _require(self._scope(row) == presented_scope, 'journey_context_changed', 409)
                row['approvals'][step_id] = {
                    'kind': 'apollo_prompts_v1', 'scope_hash': presented_scope,
                    'input_hash': self._text_parity(row)['input_hash'], 'approved_by': actor,
                    'approved_at': now.isoformat(),
                    'expires_at': (now + timedelta(hours=24)).isoformat(),
                    'dispatch_authorized': False}
                return self._view(self._save(row, expected_version))
        targets = [self._recipient_step(step, lead) for lead in self._eligible(row)]
        with self.repo.transaction():
            self._revalidate(row, context, sheet)
            now = self._now()
            try:
                pack = self.approval_service.approve_pack(row['id'], row['version'], targets,
                    actor, now + timedelta(hours=24), now)
            except ApprovalError:
                raise NewJourneyError('journey_approval_denied', 409) from None
            payload = asdict(pack)
            payload.update(approved_at=pack.approved_at.isoformat(), expires_at=pack.expires_at.isoformat())
            _require(self._scope(row) == presented_scope, 'journey_context_changed', 409)
            row['approvals'][step_id] = {'scope_hash': presented_scope, 'pack': payload}
            return self._view(self._save(row, expected_version))

    def _authorize(self, row, step, eligible):
        record = row['approvals'].get(step['id'])
        _require(isinstance(record, Mapping) and record.get('scope_hash') == self._scope(row),
                 'journey_approval_scope_changed', 409)
        if row.get('approval_mode') == 'apollo_prompts_v1':
            try:
                valid = (record.get('kind') == 'apollo_prompts_v1'
                    and record.get('approved_by') == 'ygor'
                    and record.get('dispatch_authorized') is False
                    and bool(eligible)
                    and datetime.fromisoformat(record['approved_at']) <= self._now()
                    < datetime.fromisoformat(record['expires_at'])
                    and record.get('input_hash') == self._text_parity(row).get('input_hash'))
            except (KeyError, TypeError, ValueError):
                valid = False
            _require(valid, 'journey_approval_invalid', 409)
            return
        try:
            data = dict(record['pack'])
            data['approved_at'] = datetime.fromisoformat(data['approved_at'])
            data['expires_at'] = datetime.fromisoformat(data['expires_at'])
            data['step_hashes'] = tuple(tuple(item) for item in data['step_hashes'])
            data['invalidated_step_ids'] = tuple(data['invalidated_step_ids'])
            pack = ApprovalPack(**data)
            targets = [self._recipient_step(step, lead) for lead in eligible]
            _require(pack.actor == 'ygor' and pack.cadence_id == row['id']
                and pack.approved_at <= self._now() and set(pack.step_ids) == {s['id'] for s in targets},
                'journey_approval_scope_changed', 409)
            for target in targets:
                self.approval_service.authorize_step(target, pack, self._now())
        except (KeyError, ValueError, TypeError, ApprovalError):
            raise NewJourneyError('journey_approval_invalid', 409) from None

    def export_preview(self, ident, actor, expected_version, review_hash):
        return self.export(ident, actor, expected_version, review_hash, preview_only=True)

    def export(self, ident, actor, expected_version, review_hash, *, preview_only=False):
        row, context, sheet, presented_scope = self._preflight(ident, actor, expected_version, review_hash)
        with self.repo.transaction():
            self._revalidate(row, context, sheet)
            eligible = self._eligible(row)
            bindings = self._recipient_bindings(row)
            _require(bindings is not None and len(bindings) == len(eligible),
                     'journey_apollo_recipient_unverified', 409)
            bound = {lead['source_key']: deepcopy(binding) for lead, binding in zip(eligible, bindings)}
            canonical = {entry['source_key']: entry.get('qualification_context') or {}
                         for entry in row['checks']}
            emails = [s for s in row['steps'] if s['channel'] == 'email']
            _require(bool(emails), 'journey_email_steps_required', 409)
            for step in row['steps']:
                self._authorize(row, step, eligible)
            columns = ['name', 'first_name', 'last_name', 'company', 'email', 'linkedin_url',
                'apollo_person_id', 'apollo_contact_id', 'hubspot_id', 'source_key',
                'campaign_id', 'journey_id', 'scope_hash', 'apollo_sequence_id']
            prompts = row.get('approval_mode') == 'apollo_prompts_v1'
            if prompts:
                columns += ['approval_kind', 'approved_prompt_hash']
            variables = sorted({key for lead in eligible for key in lead['variables']})
            columns += ['var_' + key for key in variables]
            for step in row['steps']:
                columns += [step['id'] + '_' + field for field in
                            (('channel', 'subject_template', 'body_template', 'order', 'interval_hours')
                             if prompts else ('channel', 'subject', 'content', 'order', 'interval_hours'))]
            exact_columns = {'email', 'linkedin_url'} | {
                step['id'] + '_' + field for step in row['steps'] for field in ('subject', 'content')}
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=columns, lineterminator='\r\n')
            writer.writerow({key: _safe_cell(key) for key in columns})
            for lead in eligible:
                values = {key: lead.get(key, '') for key in columns}
                # Imported identifiers are hints, never export authority.
                values.update(apollo_person_id='',
                              apollo_contact_id=bound[lead['source_key']].get('contact_id') or '',
                              hubspot_id=canonical[lead['source_key']].get('contact_id') or '')
                values.update(campaign_id=row['campaign_id'], journey_id=ident, scope_hash=presented_scope)
                values['apollo_sequence_id'] = row['sequence']['apollo_sequence_id']
                if prompts:
                    values.update(approval_kind='apollo_prompts_v1',
                                  approved_prompt_hash=self._text_parity(row)['input_hash'])
                values.update({'var_' + key: lead['variables'].get(key, '') for key in variables})
                for step in row['steps']:
                    values.update({step['id'] + '_' + field: step[field] for field in
                                   ('channel', 'subject', 'content', 'order', 'interval_hours')})
                    if prompts:
                        values[step['id'] + '_subject_template'] = values.pop(step['id'] + '_subject')
                        values[step['id'] + '_body_template'] = values.pop(step['id'] + '_content')
                writer.writerow({key: value if key in exact_columns else _safe_cell(value)
                                 for key, value in values.items()})
            _require(self._scope(row) == presented_scope, 'journey_context_changed', 409)
            snapshot = {'scope_hash': presented_scope, 'source_keys': [lead['source_key'] for lead in eligible],
                        'sequence_id': row['sequence']['apollo_sequence_id'],
                        'account_id': row['sequence']['account_id'], 'recipient_bindings': bound}
            if not preview_only and row.get('export_snapshot') != snapshot:
                row.update(export_snapshot=snapshot, reconciliation=None)
                row = self._save(row, expected_version)
            return {'filename': ident + '.csv', 'csv_text': '\ufeff' + stream.getvalue(),
                'rows': len(eligible), 'credits': 0, 'external_writes': 0,
                'preview_only': preview_only,
                'approval_kind': row.get('approval_mode', 'exact_copy_v1'),
                'final_email_generated': False if prompts else None,
                'enrollment_enabled': False, 'manual_export': True, 'version': row['version'],
                'review_hash': self._review_hash(row, context, sheet, scope_hash=presented_scope), 'scope_hash': presented_scope,
                'csv_formula_escaped': True, 'reconciliation_required': True}

    def reconcile(self, ident, actor, expected_version):
        """Bounded observation after manual enrollment; never registers contacts."""
        self._actor(actor)
        with self.repo.transaction():
            self._running()
            row = self._read(ident, expected_version)
            context, sheet = self._campaign(row['campaign_id'])
        from .new_journey_reconciliation import observe_batch
        snapshot = row.get('export_snapshot')
        selected = row.get('sequence') or {}
        if snapshot and (snapshot.get('scope_hash') != self._scope(row)
                or snapshot.get('sequence_id') != selected.get('apollo_sequence_id')
                or snapshot.get('account_id') != selected.get('account_id')
                or self._sheet_hash(sheet) != row['sheet_hash'] or context != row['campaign_context']):
            row['export_snapshot'] = None
        reconciliation = observe_batch(row, self._provider if self.apollo is not None else None, self._now())
        with self.repo.transaction():
            self._revalidate(row, context, sheet)
            row['reconciliation'] = reconciliation
            return self._view(self._save(row, expected_version))
