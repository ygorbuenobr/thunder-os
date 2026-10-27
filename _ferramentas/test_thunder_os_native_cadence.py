"""S51 named proofs: real temporary Repository/Service, synthetic data, no network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_campaign_sheet_support import make_sheet
from test_thunder_os_research import base_artifact
from thunder_os.approvals import ApprovalError, ApprovalService, _step_hash
from thunder_os.cadence import changed_future_steps
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.models import ContractError, effect_payload_hash, payload_hash
from thunder_os.native_cadence import ACTION_FIELDS, DEFAULT_CALENDAR
from thunder_os.native_contract import (validate_native_context, validate_native_step,
    native_step_hash, evaluate_native_schedule)
from thunder_os.service import ThunderService
from thunder_os.touch_ledger import TouchLedger


ZONE = ZoneInfo('America/Mexico_City')
CATALOG = {'version': 'synthetic-native-1', 'status': 'validado',
           'capabilities': ['cap-1'], 'proofs': ['proof-1']}


class NativeLedger:
    """No production ledger import/configuration/connection is exercised."""
    def __init__(self):
        self.rows = {}

    def validate_identity(self, step):
        return TouchLedger.identity_destination(step['recipient'])

    def preflight(self, *args, **kwargs):
        return {}


class NativePreparationTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket.connect', 'socket.getaddrinfo'):
            guard = patch(name, side_effect=AssertionError('network denied'))
            mocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mocked.assert_not_called)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = Repository(self.root / 'workflow.db')
        self.now = datetime(2026, 10, 27, 9, tzinfo=ZONE)
        self.dossier = base_artifact()
        self.ledger = NativeLedger()
        self.readers = {'source': lambda ref: self.raw,
                        'research': lambda ref, selection: deepcopy(self.dossier),
                        'crm': lambda selection: deepcopy(self.crm)}
        self.service = ThunderService(self.repo, self.ledger, approved_catalog=CATALOG,
            native_readers=self.readers, native_clock=lambda: self.now)
        store, sheet = make_sheet(self.root / 'brain')
        self.service.campaign_sheet_store = store
        self.config = {'campaign_id': 'synthetic-native', 'campaign_version': '1', 'rules_version': '1',
            'run_id': 'synthetic-run', 'platform': 'thunder_os',
            'providers': {'email': 'fake-email', 'linkedin': 'fake-linkedin'},
            'rules': 'Synthetic isolated native cadence only.', 'briefing': sheet,
            'configuration': {'flow_version': 'mexico_native_v1',
                'provider_accounts': {'email': 'synthetic-mailbox', 'linkedin': 'synthetic-ygor'}}}
        self.context = self.service.configure_campaign(self.config, 'ygor', expected_version=0)['context']
        self.route = 'ygor'
        self.install_package()

    def install_package(self, route='ygor'):
        self.route = route
        plan = [('E1', 'EMAIL'), ('L1', 'CONNECTION_REQUEST'), ('E2', 'EMAIL'), ('L2', 'MESSAGE'), ('E3', 'EMAIL')]
        if route == 'loa':
            plan = [('L1', 'CONNECTION_REQUEST'), ('L2', 'MESSAGE'), ('L3', 'MESSAGE'), ('L4', 'MESSAGE')]
        actions = []
        for label, action in plan:
            email = action == 'EMAIL'
            actions.append({'key': f'synthetic-{label}', 'person_id': 'synthetic-person', 'step': label,
                'channel': 'email' if email else 'linkedin', 'action': action, 'sender': route,
                'account_id': 'synthetic-mailbox' if email else f'synthetic-{route}',
                'recipient': 'person@acme.example' if email else 'https://www.linkedin.com/in/synthetic-person',
                'subject': '  Synthetic subject  ' if email else '',
                'text': '' if action == 'CONNECTION_REQUEST' else f'  Synthetic {label}\n\nLiteral trailing space  ',
                'contact_id': '', 'task_id': '', 'editorial_status': 'PREPARED_FOR_EXACT_REVIEW',
                'campaign': 'synthetic-source', 'deal_id': ''})
        self.package = {'people': [{'id': 'synthetic-person', 'route': route, 'holds': []}],
            'actions': actions, 'approval': {'state': 'APPROVED_EXTERNALLY', 'package_sha256': ''},
            'approval_payload': {'actions': []}}
        self.selection = [action['key'] for action in actions]
        records = []
        for person in sorted({action['recipient'] for action in actions}):
            records.extend({'domain': 'acme.example', 'person_key': person,
                'hubspot_contact_id': 'synthetic-contact', 'source': source}
                for source in ('synthetic:crm', 'synthetic:dossier'))
        self.crm = {'portal_id': 'synthetic-portal', 'company_id': 'synthetic-company',
                    'contact_id': 'synthetic-contact', 'deal_id': 'synthetic-deal',
                    'task_ids': {key: 'task-' + key for key in self.selection}, 'identity_records': records}
        if route == 'loa':
            self.config['configuration']['provider_accounts']['linkedin'] = 'synthetic-loa'
            current = self.repo.get_state('campaign_config', 'synthetic-native')
            self.context = self.service.configure_campaign(self.config, 'ygor', expected_version=current['version'])['context']
        self.encode_package()

    def encode_package(self):
        self.package['approval_payload']['actions'] = [
            {key: action[key] for key in ACTION_FIELDS} for action in self.package['actions']]
        self.package['approval']['package_sha256'] = payload_hash(self.package['approval_payload'])
        self.raw = json.dumps(self.package, ensure_ascii=False).encode()
        self.ref = {'path': 'synthetic/package.json', 'sha256': hashlib.sha256(self.raw).hexdigest()}

    def prepare(self, **overrides):
        args = dict(route=self.route, target_context=self.context, expected_version=0, actor='ygor')
        args.update(overrides)
        self.result = self.service.prepare_native_cadence(self.ref, self.selection, **args)
        return self.result

    def cadence(self):
        return self.repo.get_state('cadence', self.result['cadence_id'])

    def approve(self):
        cadence = self.cadence()
        return self.service.approve_cadence(cadence['id'], 'ygor', expected_version=cadence['version'],
            review_hash=self.service.cadence_review_hash(cadence),
            execution_confirmation=self.service.campaign_execution_review(cadence))

    def sent(self, cadence, index, at, **changes):
        value = {'state': 'sent', 'receipt_ref': f'synthetic-receipt-{index}', 'sent_at': at.isoformat(),
            'payload': {'cadence_id': cadence['id'], 'step': deepcopy(cadence['steps'][index]),
                'native_context': deepcopy(cadence['native_context']),
                'campaign_id': cadence['execution_context']['campaign_id']}}
        value.update(changes)
        return value

    def test_prepare_persists_existing_crm_binding_and_draft(self):
        for route in ('ygor', 'loa'):
            with self.subTest(route=route):
                if route == 'loa':
                    self.install_package(route)
                result = self.prepare()
                cadence = self.cadence()
                self.assertEqual(result['status'], 'ready_for_review')
                self.assertEqual(cadence['status'], 'ready_for_review')
                self.assertEqual(cadence['steps'][0]['status'], 'draft')
                self.assertEqual(cadence['native_context']['crm_binding']['task_ids'], self.crm['task_ids'])
                self.assertEqual(cadence['native_context']['route'], route)
                self.assertEqual(cadence['native_context']['source_campaign'], 'synthetic-source')
                self.assertNotIn('context_sources', cadence)
                self.assertEqual(self.repo.get_state('research', 'acme.example')['status'], 'validated')
                self.assertIsNone(self.service.native_execution_error(cadence))
                self.assertIsNone(self.service.identity_execution_error(cadence))
        self.assertEqual(self.repo.list_state('approval'), [])
        self.assertEqual(self.repo.effects(), [])

    def test_prepare_same_scope_is_idempotent(self):
        first = self.prepare()
        snapshots = {kind: self.repo.list_state(kind) for kind in ('research', 'research_history', 'identity', 'cadence')}
        second = self.prepare()
        self.assertTrue(second['replayed'])
        self.assertEqual({k: v for k, v in first.items() if k != 'replayed'},
                         {k: v for k, v in second.items() if k != 'replayed'})
        for kind, before in snapshots.items():
            self.assertEqual(self.repo.list_state(kind), before)

    def test_source_or_target_change_conflicts(self):
        self.prepare()
        original = deepcopy(self.package)
        self.package['actions'][0]['text'] += 'altered'
        self.encode_package()
        with self.assertRaises(RepositoryConflict):
            self.prepare()
        with self.assertRaises((ContractError, RepositoryConflict)):
            self.prepare(expected_version=7)
        other = deepcopy(self.context); other['run_id'] = 'other-run'
        with self.assertRaises(ContractError):
            self.prepare(target_context=other)
        self.package = original; self.encode_package()
        self.crm['contact_id'] = 'changed-crm-contact'
        for record in self.crm['identity_records']:
            record['hubspot_contact_id'] = 'changed-crm-contact'
        with self.assertRaises(RepositoryConflict):
            self.prepare()
        self.assertEqual(len(self.repo.list_state('cadence')), 1)

    def test_source_approval_never_becomes_os_approval(self):
        self.prepare()
        self.assertEqual(self.repo.list_state('approval'), [])
        with self.assertRaisesRegex(ContractError, 'active approval'):
            self.service.dispatch_step(self.result['cadence_id'], self.selection[0], 'ygor')
        self.assertEqual(self.repo.effects(), [])

    def test_invalid_target_registry_sheet_route_is_blocked(self):
        for changes in ({'platform': 'codex_local'}, {'campaign_id': 'missing'}, {'configuration_hash': 'f'*64}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.prepare(target_context={**self.context, **changes})
        with self.assertRaises(ContractError):
            self.prepare(target_context=None)
        with self.assertRaisesRegex(ContractError, 'route_mismatch'):
            self.prepare(route='loa')
        self.config['briefing']['fields'].pop('cta')
        self.context = self.service.configure_campaign(self.config, 'ygor', expected_version=1)['context']
        with self.assertRaisesRegex(ApprovalError, 'sheet_incomplete'):
            self.prepare()
        self.assertEqual(self.repo.list_state('cadence'), [])
        self.assertEqual(self.repo.list_state('research'), [])

    def test_insufficient_research_creates_no_ready_cadence(self):
        self.dossier['recommendation'] = 'pesquisar mais'
        for _ in range(2):
            result = self.prepare()
            self.assertEqual(result['status'], 'held')
            self.assertIsNone(result['cadence_id'])
            self.assertIn('native_research_insufficient', result['blockers'])
        self.assertTrue(result['replayed'])
        self.assertEqual(self.repo.get_state('research', 'acme.example')['status'], 'insufficient')
        self.assertEqual(len(self.repo.list_state('research_history')), 1)
        for kind in ('cadence', 'identity', 'approval'):
            self.assertEqual(self.repo.list_state(kind), [])

    def test_native_metadata_tamper_invalidates_review(self):
        self.prepare(); cadence = self.cadence()
        original_hash = self.service.cadence_review_hash(cadence)
        for field, value in [('action', 'invite'), ('sender_account', 'other'), ('requires_connection', True),
                             ('thread_parent_step_id', 'other'), ('source_task_id', 'other'), ('context_hash', 'f'*64)]:
            with self.subTest(field=field):
                changed = deepcopy(cadence); changed['steps'][0]['native'][field] = value
                self.assertNotEqual(self.service.cadence_review_hash(changed), original_hash)
                self.assertIsNotNone(self.service.native_execution_error(changed))
        for field in ('route', 'source_campaign', 'calendar', 'crm_binding', 'source_ref', 'selection_hash'):
            with self.subTest(root_field=field):
                changed = deepcopy(cadence)
                if field == 'route': changed['native_context'][field] = 'loa'
                elif field == 'source_campaign': changed['native_context'][field] = 'other-logical-campaign'
                elif field == 'selection_hash': changed['native_context'][field] = 'f'*64
                elif field == 'calendar': changed['native_context'][field]['holidays'] = ['2026-10-28']
                elif field == 'crm_binding': changed['native_context'][field]['contact_id'] = 'other'
                else: changed['native_context'][field]['sha256'] = 'f'*64
                self.assertNotEqual(self.service.cadence_review_hash(changed), original_hash)
                self.assertIsNotNone(self.service.native_execution_error(changed))
        # Consumer proof: persisted tampering cannot reach the actual effect queue.
        self.approve()
        changed = self.cadence()
        changed['native_context']['calendar']['holidays'] = ['2026-10-28']
        self.repo.put_state('cadence', changed['id'], changed)
        with self.assertRaises(ContractError):
            self.service.dispatch_step(changed['id'], self.selection[0], 'ygor')
        self.assertEqual(self.repo.effects(), [])

    def test_legacy_step_hash_is_unchanged(self):
        for channel in ('email', 'linkedin'):
            value = {'id': 'legacy', 'recipient': 'synthetic-person', 'channel': channel,
                     'content': '  legacy literal \n', 'order': 1, 'interval_hours': 24}
            if channel == 'email': value['subject'] = '  subject  '
            self.assertEqual(_step_hash(value), effect_payload_hash(target=value['recipient'], channel=channel,
                content=value['content'], order=1, interval_hours=24, subject=value.get('subject')))

    def test_empty_invite_and_literal_content(self):
        self.prepare(); cadence = self.cadence()
        invitation = self.service._approval_step(cadence['steps'][1])
        self.assertEqual(invitation['content'], '')
        old_hash = native_step_hash(invitation)
        for content in (' ', '  Note\n', 'Note'):
            self.assertNotEqual(native_step_hash({**invitation, 'content': content}), old_hash)
        email = self.service._approval_step(cadence['steps'][0])
        self.assertNotEqual(native_step_hash(email), native_step_hash({**email, 'content': email['content'].strip()}))
        for index in (0, 3):
            step = self.service._approval_step(cadence['steps'][index])
            for empty in ('', '  '):
                with self.assertRaises(ContractError): native_step_hash({**step, 'content': empty})
        legacy_invite = {key: value for key, value in invitation.items() if key != 'native'}
        with self.assertRaises(ContractError): _step_hash(legacy_invite)
        with self.assertRaises(ContractError): native_step_hash({**invitation, 'extra': True})
        revised = deepcopy(cadence['steps']); revised[1]['content'] = ' '
        self.assertEqual(changed_future_steps(cadence['steps'], revised), (invitation['id'],))

    def test_mexico_schedule_matrix(self):
        self.prepare(); cadence = self.cadence()
        self.approve()
        initial_now = self.now
        cadence['steps'][0]['due_at'] = '2026-10-20T09:00:00-06:00'
        cases = [('2026-10-27T08:59:00-06:00', 'approved'), ('2026-10-27T09:00:00-06:00', 'due'),
                 ('2026-10-27T17:59:00-06:00', 'due'), ('2026-10-27T18:00:00-06:00', 'approved'),
                 ('2026-10-24T12:00:00-06:00', 'approved'), ('2026-10-25T12:00:00-06:00', 'approved'),
                 ('2026-10-30T18:00:00-06:00', 'blocked')]
        for stamp, state in cases:
            with self.subTest(stamp=stamp):
                decision = evaluate_native_schedule(cadence, cadence['steps'][0], [], datetime.fromisoformat(stamp))
                self.assertEqual(set(decision), {'state', 'due_at', 'reason'})
                self.assertEqual(decision['state'], state)
                if decision['due_at']: self.assertIsNotNone(datetime.fromisoformat(decision['due_at']).tzinfo)
                # Same table through the production Service dispatch consumer.
                self.now = datetime.fromisoformat(stamp)
                if state == 'due':
                    self.assertTrue(self.service.dispatch_step(cadence['id'], self.selection[0], 'ygor')['id'])
                else:
                    with self.assertRaises(ContractError):
                        self.service.dispatch_step(cadence['id'], self.selection[0], 'ygor')
        self.now = initial_now
        cadence['native_context']['calendar']['holidays'] = ['2026-10-27']
        for step in cadence['steps']: step['native']['context_hash'] = payload_hash(cadence['native_context'])
        decision = evaluate_native_schedule(cadence, cadence['steps'][0], [], self.now)
        self.assertEqual(decision['state'], 'approved')
        self.assertTrue(decision['due_at'].startswith('2026-10-28T09:00'))

    def test_same_day_e1_l1_exception_and_one_touch_after_d0(self):
        self.prepare(); cadence = self.cadence()
        first = self.sent(cadence, 0, self.now)
        for effects in ([], [{**first, 'state': 'uncertain'}],
                        [{**first, 'payload': {**first['payload'], 'cadence_id': 'other-person'}}]):
            self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][1], effects, self.now)['state'], 'blocked')
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][1], [first], self.now)['state'], 'due')
        invite = self.sent(cadence, 1, self.now + timedelta(minutes=1))
        decision = evaluate_native_schedule(cadence, cadence['steps'][2], [first, invite], self.now + timedelta(hours=1))
        self.assertEqual(decision['state'], 'approved')
        next_day = self.now + timedelta(days=1)
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][2], [first, invite], next_day)['state'], 'due')
        e2 = self.sent(cadence, 2, next_day)
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][3], [first, invite, e2], next_day)['state'], 'approved')
        # A delayed invite loses the D0 exception; the next contact day is free.
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][1], [first], next_day)['state'], 'due')

    def test_native_readers_strict_and_default_disabled(self):
        for readers in ({}, {'source': lambda ref: b'{}'}, {**self.readers, 'authority': True},
                        {**self.readers, 'source': 'from-payload'}):
            with self.subTest(readers=tuple(readers)), self.assertRaises(ContractError):
                ThunderService(self.repo, native_readers=readers)
        disabled = ThunderService(self.repo, approved_catalog=CATALOG)
        with self.assertRaisesRegex(ContractError, 'readers_unavailable'):
            disabled.prepare_native_cadence(self.ref, self.selection, route='ygor',
                target_context=self.context, expected_version=0, actor='ygor')

    def test_canonical_catalog_and_sheet_changes_block_native_gate(self):
        self.prepare(); cadence = self.cadence()
        self.service._approved_catalog = {**CATALOG, 'version': 'changed-catalog'}
        self.assertIn('catalog changed', self.service.native_execution_error(cadence))
        self.service._approved_catalog = deepcopy(CATALOG)
        path = self.service.campaign_sheet_store.root / 'campaign.md'
        path.write_text(path.read_text() + '\nChanged source.\n')
        self.assertIsNotNone(self.service.native_execution_error(cadence))

    def test_package_projection_invalid_types_and_holds_are_not_authority(self):
        original = deepcopy(self.package)
        for mutate in (
            lambda p: p['actions'][0].update(text='changed but hash untouched'),
            lambda p: p['actions'][0].update(key=[]),
            lambda p: p['people'][0].update(holds=['unresolved identity']),
            lambda p: p['actions'][0].update(editorial_status='CONDITIONAL_IDENTITY'),
        ):
            self.package = deepcopy(original)
            mutate(self.package)
            self.raw = json.dumps(self.package).encode()
            self.ref['sha256'] = hashlib.sha256(self.raw).hexdigest()
            with self.subTest(package=self.package['people'][0]['holds']), self.assertRaises(ContractError):
                self.prepare()
        self.assertEqual(self.repo.list_state('cadence'), [])

    def test_calendar_and_native_schema_are_strict(self):
        self.prepare(); cadence = self.cadence()
        root = cadence['native_context']
        for key in root:
            invalid = deepcopy(root); invalid.pop(key)
            with self.subTest(missing=key), self.assertRaises(ContractError): validate_native_context(invalid)
        for key, value in [('timezone', 'America/Sao_Paulo'), ('start_hour', True), ('end_hour', 19),
                           ('expires_at', '2026-10-30T18:00:00'), ('holidays', ['not-a-date'])]:
            invalid = deepcopy(root); invalid['calendar'][key] = value
            with self.subTest(field=key), self.assertRaises(ContractError): validate_native_context(invalid)
        for step in cadence['steps']:
            native = step['native']
            for key in native:
                invalid = deepcopy(native); invalid.pop(key)
                with self.subTest(step=step['id'], missing=key), self.assertRaises(ContractError):
                    validate_native_step(invalid, root)
        invalid = deepcopy(root); invalid['approval'] = True
        with self.assertRaises(ContractError): validate_native_context(invalid)

    def test_cross_route_occupancy_is_bound_to_exact_crm_person(self):
        self.prepare(); cadence = self.cadence()
        other = self.sent(cadence, 0, self.now)
        other['payload']['cadence_id'] = 'other-route'
        other['payload']['campaign_id'] = 'different-destination-registry'
        result = evaluate_native_schedule(cadence, cadence['steps'][0], [other], self.now)
        self.assertEqual(result['state'], 'approved')
        self.assertTrue(result['due_at'].startswith('2026-10-28T09:00'))
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][0], [other],
                                                self.now + timedelta(days=1))['state'], 'due')
        other['payload']['native_context']['crm_binding']['contact_id'] = 'different-person'
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][0], [other], self.now)['state'], 'due')
        other['payload']['native_context']['crm_binding']['contact_id'] = self.crm['contact_id']
        other['payload']['native_context']['crm_binding']['portal_id'] = 'different-portal'
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][0], [other], self.now)['state'], 'due')
        other['payload']['native_context']['crm_binding']['portal_id'] = self.crm['portal_id']
        other['payload']['native_context']['source_campaign'] = 'independent-logical-campaign'
        # Even an identical destination ID must not merge independent campaigns.
        other['payload']['campaign_id'] = cadence['execution_context']['campaign_id']
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][0], [other], self.now)['state'], 'due')

    def test_cross_route_uncertainty_retains_same_source_campaign(self):
        self.prepare(); cadence = self.cadence()
        other = self.sent(cadence, 0, self.now)
        other['payload']['cadence_id'] = 'other-route'
        other['payload']['campaign_id'] = 'other-registry'
        for state in ('uncertain', 'sending', 'reserved', 'needs_reconciliation'):
            with self.subTest(state=state):
                other['state'] = state
                other['receipt_ref'] = None
                decision = evaluate_native_schedule(cadence, cadence['steps'][0], [other], self.now)
                self.assertEqual(decision['state'], 'blocked')
                self.assertEqual(decision['reason'], 'native_effect_uncertain')
        other['payload']['native_context']['source_campaign'] = 'independent-logical-campaign'
        self.assertEqual(evaluate_native_schedule(cadence, cadence['steps'][0], [other], self.now)['state'], 'due')

    def test_source_campaign_is_strict_and_bound_to_source(self):
        self.prepare(); cadence = self.cadence()
        root = cadence['native_context']
        self.assertEqual(root['source_campaign'], self.package['actions'][0]['campaign'])
        for value in (None, True, 123, [], {}, '', '   '):
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_native_context({**root, 'source_campaign': value})
        changed = deepcopy(cadence)
        changed['native_context']['source_campaign'] = 'another-source'
        for step in changed['steps']:
            step['native']['context_hash'] = payload_hash(changed['native_context'])
        self.assertNotEqual(self.service.cadence_review_hash(changed), self.service.cadence_review_hash(cadence))
        self.assertIsNotNone(self.service.native_execution_error(changed))

    def test_empty_invite_uses_same_approval_pack_and_full_scope(self):
        self.prepare(); cadence = self.cadence()
        invite = self.service._approval_step(cadence['steps'][1])
        service = ApprovalService(lambda: False)
        pack = service.approve_pack(cadence['id'], cadence['version'], [invite], 'ygor',
                                    self.now + timedelta(hours=1), self.now)
        self.assertEqual(service.authorize_step(invite, pack, self.now).effect_hash, native_step_hash(invite))
        for field, value in [('content', ' '), ('native', {**invite['native'], 'sender_account': 'other'})]:
            with self.subTest(field=field), self.assertRaises(ApprovalError):
                service.authorize_step({**invite, field: value}, pack, self.now)

    def test_source_hash_projection_and_identity_revalidated(self):
        self.prepare(); cadence = self.cadence()
        original = self.raw
        self.raw += b' '
        self.assertEqual(self.service.native_execution_error(cadence), 'native_source_hash_changed')
        self.raw = original
        self.crm['contact_id'] = 'other'
        self.assertIsNotNone(self.service.native_execution_error(cadence))
        self.crm['contact_id'] = 'synthetic-contact'
        self.dossier['recommendation'] = 'pesquisar mais'
        self.assertEqual(self.service.native_execution_error(cadence), 'native_research_changed')

    def test_prepare_after_workflow_version_change_conflicts(self):
        self.prepare(); self.approve()
        with self.assertRaises(RepositoryConflict): self.prepare()
        self.assertEqual(len(self.repo.list_state('cadence')), 1)
        self.assertEqual(len(self.repo.list_state('approval')), 1)

    def test_atomic_failure_rolls_back_research_identity_and_cadence(self):
        with patch.object(self.service, 'create_cadence', side_effect=RuntimeError('synthetic write fault')):
            with self.assertRaisesRegex(RuntimeError, 'write fault'): self.prepare()
        for kind in ('research', 'research_history', 'identity', 'cadence', 'approval'):
            self.assertEqual(self.repo.list_state(kind), [])

    def test_concurrent_prepare_has_one_persisted_result(self):
        def prepare(_):
            return self.service.prepare_native_cadence(self.ref, self.selection, route=self.route,
                target_context=self.context, expected_version=0, actor='ygor')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, range(2)))
        self.assertEqual(len({result['cadence_id'] for result in results}), 1)
        self.assertEqual(sum(result['replayed'] for result in results), 1)
        self.assertEqual(len(self.repo.list_state('research_history')), 1)

    def test_real_service_review_approval_and_queue_bind_native_scope(self):
        self.prepare(); cadence = self.cadence()
        review = self.service.review_step(cadence['id'], self.selection[0], 'ygor')
        self.assertTrue(review['can_approve'], review['blockers'])
        for text in ('email', 'synthetic-mailbox', 'America/Mexico_City', 'synthetic-contact', 'task-synthetic-E1'):
            self.assertIn(text, review['execution_question'])
        self.approve()
        job = self.service.dispatch_step(cadence['id'], self.selection[0], 'ygor')
        self.assertEqual(job['payload']['step']['content'], self.package['actions'][0]['text'])
        self.assertEqual(job['payload']['native_context'], cadence['native_context'])
        self.assertEqual(job['payload']['approval_hash'], native_step_hash(self.service._approval_step(cadence['steps'][0])))
        self.assertEqual(len(self.repo.effects()), 1)


if __name__ == '__main__':
    unittest.main()
