"""Phase 1: isolated SQLite and read-port doubles; no network, model or writes."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from thunder_os.db import Repository
from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.journey_readonly import JourneyReadonly, MISSING
from thunder_os.seqd_client import MAILBOX
from thunder_os.service import ThunderService
from thunder_os.today_readonly import TodayError, TodayReadonly, ReadonlyRepository
from thunder_os.models import payload_hash


NOW = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)
BR = 'BR-FIXTURE-01'
OTHER = 'BR-FIXTURE-02'
PEER = 'buyer@example.invalid'


def message(ident, hour, outgoing, text, **extra):
    return {'id': ident, 'date': f'2026-10-23T{hour:02}:00:00Z', 'outgoing': outgoing,
        'text': text, 'sender': MAILBOX if outgoing else PEER, 'subject': 'Fixture subject',
        'body_complete': True, 'human_reply': not outgoing, 'canonical_id': 'gmail:' + ident, **extra}


def receipt(provider, ident):
    return {'provider': provider, 'message_id': ident, 'verification': 'provider_read'}


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name) / 'fixture.db')
        with closing(sqlite3.connect(self.repo.path)) as con, con:
            con.execute('CREATE TABLE itens(id TEXT PRIMARY KEY,dados JSON,atualizado TEXT)')
        self.service = ThunderService(self.repo)
        self.register(BR)
        self.register(OTHER)
        self.runner = Mock(side_effect=AssertionError('model forbidden'))
        self.today = TodayReadonly(self.service, clock=lambda: NOW.timestamp(), monotonic=lambda: 0,
                                   runner=self.runner)
        self.rows = []
        self.today._gmail = self.mail_threads
        for method in ('_tasks', '_legacy', '_emails', '_instantly', '_linkedin', '_seqd', '_deals', '_approvals'):
            setattr(self.today, method, Mock(return_value={'status': 'ok', 'respond': [], 'do': []}))
        self.cadence = Mock()
        self.cadence.snapshot.return_value = {'coverage': {'complete': False},
            'collected_at': NOW.isoformat(), 'campaigns': []}
        self.cadence.timeline.return_value = {'events': [], 'coverage': {'complete': False}}
        self.adapter = JourneyReadonly(self.today, cadence_reader=self.cadence)
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.getaddrinfo'):
            p = patch(target, side_effect=AssertionError('network forbidden'))
            port = p.start()
            self.addCleanup(p.stop)
            self.addCleanup(port.assert_not_called)

    def add_thread(self, messages, *, ident='fixture', campaign=BR, contact='700', **extra):
        self.rows.append({'ident': ident, 'campaign': campaign, 'contact_id': contact,
            'source': 'gmail', 'title': 'Buyer / Fixture company', 'messages': messages,
            'complete': True, 'person_keys': [PEER],
            'action': {'label': 'Abrir fonte', 'url': 'https://app.hubspot.com/contacts/51412240/record/0-1/' + str(contact)},
            **extra})

    def register(self, campaign):
        CampaignRegistry(self.repo).register({'campaign_id': campaign, 'campaign_version': '1',
            'rules_version': '1', 'run_id': 'fixture', 'platform': 'thunder_os',
            'providers': {'email': 'fake-email'}, 'rules': 'Synthetic isolated fixture only.',
            'configuration': {'flow_version': '1', 'provider_accounts': {'email': 'fixture'}}}, 'ygor', 0)

    def mail_threads(self):
        result = {'status': 'ok', 'respond': [], 'do': []}
        for row in self.rows:
            data = self.today._thread(**row)
            for key in ('respond', 'do'):
                result[key].extend(data.get(key, []))
        return result

    def refresh(self):
        self.today._cache = None

    def stage(self, snapshot, key):
        return next(s for s in snapshot['stages'] if s['id'] == key)

    def database(self):
        with closing(sqlite3.connect(self.repo.path)) as con:
            return {table: con.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('thunder_state', 'thunder_effect_jobs', 'thunder_events', 'itens')}

    def instant(self, *, status='ok', complete=True, unclassified=0):
        self.register('BR-INSTANTLY-01')
        self.today.instantly = SimpleNamespace(enable_check=lambda: True, clock=lambda: NOW)
        self.repo.put_state('instantly_reader_snapshot', 'BR-INSTANTLY-01', {
            'status': status, 'last_success_at': NOW.isoformat(), 'coverage': {'complete': complete},
            'metrics': {'unclassified_replies': unclassified, 'human_replies': 0, 'leads_with_sent': 100,
                        'sent_messages': 104},
            'leads': [{'email': PEER, 'events': [{'provider_id': 'provider-first', 'event_type': 'email_sent',
                'occurred_at': '2026-10-23T07:00:00Z', 'text': 'Original provider body',
                'subject': 'Original provider subject', 'content_complete': True}]}]})

    def test_crm_unknown_identity_coverage_never_reports_empty_total(self):
        self.today._deals.return_value = {'status': 'ok', 'do': [],
            'limitations': ['unknown_identity_cannot_guarantee_mx_exclusion']}
        value = self.stage(self.adapter.snapshot(), 'crm')
        self.assertIsNone(value['count'])
        self.assertFalse(value['coverage']['complete'])
        self.assertIn('total desconhecido', value['reason'])

    def test_hubspot_incoming_original_is_readable_without_human_claim(self):
        self.add_thread([message('crm-in', 10, False, 'Original integral CRM', human_reply=None)],
                        source='hubspot_emails', complete=False)
        snap = self.adapter.snapshot()
        rows = self.stage(snap, 'resposta')['items']
        self.assertEqual(len(rows), 1)
        self.assertIn('classificacao pendentes', rows[0]['summary'])
        detail = self.adapter.detail(rows[0]['id'])
        self.assertIn('Original integral CRM', [b['text'] for b in detail['blocks']])
        self.assertNotEqual(snap['metrics']['replied']['value'], 1)
        self.assertFalse(detail['approval_available'])
        self.rows[0]['messages'].append(message('crm-out', 11, True, 'Retorno real CRM'))
        self.refresh()
        self.assertEqual(self.stage(self.adapter.snapshot(), 'resposta')['items'], [])

    def test_seven_stages_exact_contract_unknown_missing_sources(self):
        self.today._gmail = Mock(side_effect=RuntimeError('secret error'))
        self.today._deals = Mock(side_effect=RuntimeError('secret error'))
        snap = self.adapter.snapshot()
        self.assertEqual([s['id'] for s in snap['stages']],
            ['icp', 'abordagem', 'resposta', 'crm', 'linkedin', 'followup', 'preparacao'])
        self.assertTrue(snap['read_only'])
        self.assertEqual(set(snap['metrics']), {'reached', 'replied', 'hot', 'cost_per_hot'})
        for metric in snap['metrics'].values():
            self.assertEqual(set(metric), {'value', 'source', 'reason', 'coverage'})
            self.assertIsNone(metric['value'])
        self.assertIsNone(self.stage(snap, 'crm')['count'])
        self.assertNotIn('secret error', json.dumps(snap))
        self.assertEqual(snap['timeline']['events'], [])

    def test_default_brazil_excludes_internal_test_even_with_brazil_country(self):
        internal = 'BR-INTERNAL-SYNTHETIC'
        self.register(internal)
        self.repo.put_state('internal_canary', internal, {'actor': 'ygor', 'scope': {}})
        self.add_thread([message('internal-synthetic', 9, True, 'Synthetic internal original')], campaign=internal)
        self.canonical(execution_context=CampaignRegistry(self.repo).load(internal))
        before = self.database()
        snap = self.adapter.snapshot()
        self.assertFalse(any(c['id'] == internal for c in snap['campaigns']))
        self.assertFalse(any(i['campaign_id'] == internal for s in snap['stages'] for i in s['items']))
        self.assertEqual(snap['timeline']['events'], [])
        self.assertIsNone(snap['metrics']['reached']['value'])
        self.assertEqual(before, self.database())

    def test_internal_prefix_alone_never_makes_an_option_or_projection(self):
        internal = 'internal-canary-20261025-ygor-01'
        self.register(internal)
        self.canonical(execution_context=CampaignRegistry(self.repo).load(internal))
        for selected in (None, internal):
            snap = self.adapter.snapshot(campaign=selected)
            self.assertNotIn(internal, [c['id'] for c in snap['campaigns']])
            self.assertFalse(any(s['items'] for s in snap['stages']))
            self.assertTrue(all(m['value'] is None for m in snap['metrics'].values()))

    def internal_fixture(self, sent=False):
        from thunder_os.codex_assisted import MANUAL_JOB_ID, MANUAL_RECIPIENT
        cid = 'internal-canary-20261025-ygor-01'
        scope = {'kind': 'internal_canary', 'from': MAILBOX, 'to': MANUAL_RECIPIENT,
                 'subject': 'Synthetic internal subject', 'content': 'Synthetic internal original\nExact last line.',
                 'max_attempts': 1, 'no_followup': True}
        self.repo.put_state('internal_canary', cid, {'actor': 'ygor', 'scope': scope, 'scope_hash': payload_hash(scope)})
        CampaignRegistry(self.repo).register({'campaign_id': cid, 'campaign_version': '1', 'rules_version': '1',
            'run_id': cid, 'platform': 'thunder_os', 'providers': {'email': 'gmail'}, 'rules': payload_hash(scope),
            'configuration': {'flow_version': 'internal-canary-v1', 'provider_accounts': {'email': MAILBOX}}}, 'ygor', 0)
        context = CampaignRegistry(self.repo).load(cid)
        step = {'id': 'e1', 'channel': 'email', 'order': 1, 'recipient': scope['to'],
                'subject': scope['subject'], 'content': scope['content'], 'status': 'sent' if sent else 'approved'}
        cadence = self.repo.put_state('cadence', cid, {'account_id': cid, 'person_key': scope['to'],
            'execution_context': context, 'status': 'paused_reply' if sent else 'approved',
            'reply_id': 'gmail:synthetic-reply' if sent else None, 'steps': [step]})
        payload = {'cadence_id': cid, 'step': step}
        job = self.repo.enqueue_effect('synthetic-internal-effect', payload)
        with closing(sqlite3.connect(self.repo.path)) as con, con:
            con.execute('UPDATE thunder_effect_jobs SET id=? WHERE id=?', (MANUAL_JOB_ID, job['id']))
        envelope = {'from_address': MAILBOX, 'to': scope['to'], 'subject': scope['subject'],
            'payload': {'mime_type': 'text/plain', 'charset': 'UTF-8', 'body': {'content': scope['content']}},
            'response_fields': ['id', 'thread_id', 'label_ids']}
        ticket = {'cadence_id': cid, 'step_id': 'e1', 'campaign': context, 'payload': envelope}
        ticket['review_hash'] = payload_hash({**ticket, 'dispatch_authorized': False})
        ticket['ticket_hash'] = payload_hash(ticket)
        grant = {'ticket_hash': ticket['ticket_hash'], 'packet_hash': ticket['review_hash']}
        manual = {'schema': 'internal_operator_receipt_v1', 'job_payload_hash': payload_hash(payload),
            'envelope_hash': payload_hash(envelope), 'internal_scope_hash': payload_hash(scope),
            'dispatch_authorized': False, 'retry_write': False,
            'proof': {'job_id': MANUAL_JOB_ID, 'ticket_hash': ticket['ticket_hash'],
                      'packet_hash': ticket['review_hash'], 'grant_hash': payload_hash(grant)}}
        self.repo.append_event(MANUAL_JOB_ID, 'assisted_proved_not_sent', {'record_hash': payload_hash(manual), 'record': manual})
        receipt_ref = 'gmail:synthetic-sent'
        row = {'ticket': ticket, 'grant': grant, 'manual_reconciliation': manual,
               'phase': 'confirmed' if sent else 'proved_not_sent'}
        if sent:
            self.repo.append_event(MANUAL_JOB_ID, 'assisted_operator_receipt_confirmed', {
                'receipt': receipt_ref, 'manual_record_hash': payload_hash(manual),
                'attempt_attribution': 'operator_reported'})
            row.update(receipt=receipt_ref, verification='provider_sent_copy', attempt_attribution='operator_reported',
                operator_receipt_event={'event_type': 'email_sent', 'source': {'provider': 'gmail',
                    'account_id': MAILBOX, 'provider_id': 'synthetic-sent'}, 'lead_email': scope['to'],
                    'subject': scope['subject'], 'text': scope['content'], 'occurred_at': NOW.isoformat(),
                    'receipt_ref': receipt_ref, 'scope': 'sent_copy_not_delivery'})
            self.repo.put_state('reply', 'gmail:synthetic-reply', {'campaign_id': cid,
                'cadence_id': cid, 'provider_receipt': receipt_ref,
                'person_key': scope['to'], 'content': 'Synthetic registered reply\nReply last line.',
                'identity_confirmed': True, 'human_reply': True, 'paused_cadence_ids': [cid],
                'received_at': NOW.isoformat(), 'source': {'provider': 'gmail', 'account_id': MAILBOX,
                                                       'provider_message_id': 'synthetic-reply'}})
        self.repo.finish_effect(MANUAL_JOB_ID, 'sent' if sent else 'failed', receipt_ref if sent else None,
                                None if sent else 'proved_not_sent')
        self.repo.put_state('codex_assisted', MANUAL_JOB_ID, row)
        return cid, MANUAL_JOB_ID, cadence

    def test_verified_internal_manual_failure_visible_only_when_selected(self):
        cid, job, cadence = self.internal_fixture()
        before = self.database()
        default = self.adapter.snapshot()
        option = next(c for c in default['campaigns'] if c['id'] == cid)
        self.assertEqual(option['label'], 'Teste interno')
        self.assertTrue(option['internal_test'])
        self.assertFalse(any(s['items'] for s in default['stages']))
        snap = self.adapter.snapshot(cid)
        item, = self.stage(snap, 'abordagem')['items']
        detail = self.adapter.detail(item['id'], cid)
        self.assertEqual(detail['step_status'], 'proved_not_sent')
        reconciled_at = next(e['created_at'] for e in self.repo.events(job)
                             if e['type'] == 'assisted_proved_not_sent')
        self.assertEqual(snap['timeline']['events'][0]['date'], reconciled_at)
        self.assertEqual(item['date'], reconciled_at)
        self.assertNotEqual(reconciled_at, cadence['updated_at'])
        self.assertEqual(detail['canonical_cadence_id'], cid)
        self.assertFalse(detail['editable'])
        self.assertEqual(self.stage(snap, 'icp')['items'], [])
        self.assertTrue(all(m['value'] is None and m['reason'] == 'internal_test_excluded' for m in snap['metrics'].values()))
        self.assertEqual(before, self.database())

    def test_verified_internal_sent_copy_registered_reply_and_internal_stop(self):
        cid, job, cadence = self.internal_fixture(sent=True)
        before = self.database()
        snap = self.adapter.snapshot(cid)
        item, = self.stage(snap, 'abordagem')['items']
        detail = self.adapter.detail(item['id'], cid)
        self.assertEqual(detail['step_status'], 'sent')
        self.assertIn(cadence['steps'][0]['content'], [b['text'] for b in detail['blocks']])
        self.assertIn('Synthetic registered reply\nReply last line.', [b['text'] for b in detail['blocks']])
        self.assertEqual([e['label'] for e in snap['timeline']['events']], ['Enviado · teste interno', 'Resposta · teste interno', 'Parada interna'])
        self.assertTrue(self.stage(snap, 'resposta')['items'])
        self.assertTrue(all(m['value'] is None for m in snap['metrics'].values()))
        self.assertEqual(before, self.database())

    def test_internal_normalized_receipt_revalidates_body_and_preserves_full_original(self):
        cid, job, cadence = self.internal_fixture(sent=True)
        row = self.repo.get_state('codex_assisted', job)
        event = row['operator_receipt_event']
        approved = cadence['steps'][0]['content']
        original = approved.replace('Synthetic internal', 'Synthetic\ninternal') + '\n\nYgor\nThunder'
        evidence = {'mime_type': 'multipart/alternative', 'html_body': '<div>Approved body</div><div class="gmail_signature">Ygor<br>Thunder</div>',
                    'mime_complete': True, 'attachment_count': 0}
        event.update(text=original, approved_text=approved, signature_excluded=True, content_evidence=evidence)
        self.repo.put_state('codex_assisted', job, row)
        snap = self.adapter.snapshot(cid)
        item, = self.stage(snap, 'abordagem')['items']
        detail = self.adapter.detail(item['id'], cid)
        self.assertIn(original, [b['text'] for b in detail['blocks']])
        self.assertTrue(self.stage(snap, 'resposta')['items'])
        self.assertEqual(snap['timeline']['events'][-1]['label'], 'Parada interna')
        for key, value in [('text', original.replace('Synthetic', 'Changed', 1)),
                           ('approved_text', approved + ' changed'), ('content_evidence', {}),
                           ('content_evidence', {**evidence, 'html_body': '<div class="gmail_signature">Different signature</div>'})]:
            mutated = deepcopy(row);mutated['operator_receipt_event'][key] = value
            self.repo.put_state('codex_assisted', job, mutated)
            hidden = self.adapter.snapshot(cid)
            self.assertNotIn(cid, [c['id'] for c in hidden['campaigns']], key)
            self.assertFalse(any(s['items'] for s in hidden['stages']), key)
        self.repo.put_state('codex_assisted', job, row)

    def test_internal_scope_manual_hash_and_provider_conflicts_fail_closed(self):
        cid, job, _ = self.internal_fixture(sent=True)
        for kind, ident, key, value in [('internal_canary', cid, 'scope_hash', '0'*64),
                ('codex_assisted', job, 'phase', 'dispatch_issued'),
                ('codex_assisted', job, 'receipt', 'gmail:wrong'),
                ('codex_assisted', job, 'operator_receipt_event', {})]:
            original = self.repo.get_state(kind, ident)
            with self.subTest(key=key):
                self.repo.put_state(kind, ident, {**original, key: value})
                snap = self.adapter.snapshot(cid)
                self.assertNotIn(cid, [c['id'] for c in snap['campaigns']])
                self.assertFalse(any(s['items'] for s in snap['stages']))
            self.repo.put_state(kind, ident, original)

    def test_internal_security_scope_and_reply_conflicts_fail_closed(self):
        cid, job, _ = self.internal_fixture(sent=True)
        for kind, ident, path, value in [
                ('internal_canary', cid, ['scope', 'from'], 'wrong@example.invalid'),
                ('internal_canary', cid, ['scope', 'no_followup'], False),
                ('internal_canary', cid, ['scope', 'max_attempts'], True),
                ('codex_assisted', job, ['manual_reconciliation', 'job_payload_hash'], '0'*64),
                ('codex_assisted', job, ['manual_reconciliation', 'envelope_hash'], '0'*64),
                ('codex_assisted', job, ['operator_receipt_event', 'text'], 'Different synthetic copy'),
                ('cadence', cid, ['steps'], []),
                ('cadence', cid, ['execution_context', 'campaign_id'], BR)]:
            original = self.repo.get_state(kind, ident)
            mutated = deepcopy(original)
            current = mutated
            for part in path[:-1]:
                current = current[part]
            current[path[-1]] = value
            with self.subTest(path=path):
                self.repo.put_state(kind, ident, mutated)
                snap = self.adapter.snapshot(cid)
                self.assertNotIn(cid, [c['id'] for c in snap['campaigns']])
                self.assertFalse(any(s['items'] for s in snap['stages']))
            self.repo.put_state(kind, ident, original)
        original = self.repo.get_state('reply', 'gmail:synthetic-reply')
        self.repo.put_state('reply', original['id'], {**original, 'provider_receipt': 'gmail:wrong'})
        snap = self.adapter.snapshot(cid)
        self.assertFalse(self.stage(snap, 'resposta')['items'])
        self.assertEqual(len(snap['timeline']['events']), 1)

    def test_real_backend_manual_record_contract_with_fake_gmail(self):
        from test_thunder_os_codex_assisted import ManualReconciliationTests
        fixture = ManualReconciliationTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.reconcile()
        view = object.__new__(JourneyReadonly)
        view.repo = ReadonlyRepository(fixture.repo.path)
        view.service = ThunderService(view.repo)
        def project():
            states = {k: view.repo.list_state(k) for k in
                      ('cadence', 'internal_canary', 'codex_assisted', 'campaign_config')}
            with view.repo.transaction():
                return view._internal_canary(states, view._effects())
        with patch('thunder_os.journey_readonly.INTERNAL_CANARY', fixture.data['id']):
            self.assertEqual(project()['status'], 'proved_not_sent')
            fixture.now += 2
            preview = fixture.assisted.receipt_preview(fixture.job['id'], fixture.receipt)
            fixture.assisted.receipt_confirm(fixture.job['id'], fixture.receipt, preview['review_hash'])
            result = project()
            self.assertEqual(result['status'], 'sent')
            self.assertEqual(result['event']['text'], fixture.data['scope']['content'])
        self.assertEqual(fixture.mail.sends, [])

    def test_internal_audit_read_failure_does_not_break_default_brazil(self):
        from thunder_os.db import RepositoryConflict
        cid, _, _ = self.internal_fixture(sent=True)
        self.canonical()
        for error in (sqlite3.OperationalError('synthetic offline'), RepositoryConflict('synthetic conflict')):
            with self.subTest(error=type(error).__name__), patch.object(self.adapter.repo, 'events', side_effect=error):
                snap = self.adapter.snapshot()
                self.assertIn(BR, [c['id'] for c in snap['campaigns']])
                self.assertNotIn(cid, [c['id'] for c in snap['campaigns']])
                self.assertTrue(self.stage(snap, 'abordagem')['items'])
                source = next(s for s in snap['sources'] if s['id'] == 'local:internal_canary_projection')
                self.assertEqual(source['status'], 'offline')
                selected = self.adapter.snapshot(cid)
                self.assertFalse(any(s['items'] for s in selected['stages']))
                self.assertTrue(all(m['value'] is None for m in selected['metrics'].values()))

    def test_crm_unknown_when_healthy_hubspot_has_deals_without_verified_campaign(self):
        self.today._deals.return_value = {'status': 'ok', 'do': [{
            'id': 'hubspot:deal:' + ident, 'title': title, 'campaign_id': cid,
            'date': '2026-10-23T11:00:00Z'} for ident, title, cid in (
                ('900', 'Known deal', BR), ('901', 'Unbound deal', None),
                ('902', 'Other campaign deal', OTHER), ('903', 'Excluded deal', 'MX27-FIXTURE'))]}
        snapshot = self.adapter.snapshot(campaign=BR)
        stage = self.stage(snapshot, 'crm')
        self.assertEqual(next(s for s in snapshot['sources'] if s['id'] == 'hubspot_deals')['status'], 'ok')
        self.assertEqual([i['name'] for i in stage['items']], ['Known deal'])
        self.assertIsNone(stage['count'])
        self.assertEqual(stage['status'], 'partial')
        self.assertEqual(stage['coverage']['omitted_without_verified_brazil_campaign'], 1)
        self.assertFalse(stage['coverage']['complete'])
        self.assertIn('total desconhecido', stage['reason'])
        self.today._deals.return_value['do'] = self.today._deals.return_value['do'][1:]
        self.refresh()
        stage = self.stage(self.adapter.snapshot(campaign=BR), 'crm')
        self.assertEqual(stage['items'], [])
        self.assertIsNone(stage['count'])
        self.assertEqual(stage['status'], 'unavailable')

    def test_crm_intentional_other_campaign_and_mexico_exclusion_do_not_invent_unknown(self):
        self.today._deals.return_value = {'status': 'ok', 'do': [{
            'id': 'hubspot:deal:' + ident, 'title': 'Synthetic deal', 'campaign_id': cid,
            'date': '2026-10-23T11:00:00Z'} for ident, cid in (
                ('902', OTHER), ('903', 'MX27-FIXTURE'))]}
        stage = self.stage(self.adapter.snapshot(campaign=BR), 'crm')
        self.assertEqual(stage['items'], [])
        self.assertEqual(stage['count'], 0)
        self.assertEqual(stage['status'], 'ok')
        self.assertTrue(stage['coverage']['complete'])
        self.assertEqual(stage['coverage']['scope'], 'observed_campaign_bound_deals')
        self.assertEqual(stage['coverage']['omitted_without_verified_brazil_campaign'], 0)

    def test_campaign_filter_and_mexico_never_enter_items_or_details(self):
        for campaign in (BR, OTHER, 'MX27-MTY', 'CDMX-fixture', 'old-mexico'):
            self.add_thread([message(campaign, 10, False, campaign)], ident=campaign, campaign=campaign)
        snap = self.adapter.snapshot()
        self.assertEqual({c['id'] for c in snap['campaigns']}, {BR, OTHER})
        other = next(i for i in self.stage(snap, 'resposta')['items'] if i['campaign_id'] == OTHER)
        with self.assertRaises(TodayError) as exc:
            self.adapter.detail(other['id'], campaign=BR)
        self.assertEqual(exc.exception.status, 404)
        filtered = self.adapter.snapshot(campaign=BR)
        self.assertTrue(all(i['campaign_id'] == BR for s in filtered['stages'] for i in s['items']))
        self.assertNotIn('MX27', json.dumps(filtered))
        self.assertEqual(self.adapter.snapshot(campaign='MX27-MTY')['timeline']['events'], [])

    def test_registry_campaigns_are_filters_and_sheets_never_icp_leads(self):
        with patch.object(self.adapter.service, 'campaign_sheet_view', return_value={
                'status': 'complete', 'fields': [{'key': 'country', 'label': 'Pais',
                    'status': 'ok', 'content': 'BR'}]}) as sheets:
            snapshot = self.adapter.snapshot()
        self.assertEqual({c['id'] for c in snapshot['campaigns']}, {BR, OTHER})
        self.assertEqual({call.args[0] for call in sheets.call_args_list}, {BR, OTHER})
        self.assertEqual(self.stage(snapshot, 'icp')['items'], [])
        self.assertIsNone(self.stage(snapshot, 'icp')['count'])
        self.assertEqual(self.stage(snapshot, 'icp')['status'], 'unavailable')

    def test_icp_account_date_fallback_survives_real_reader_and_campaign_filter(self):
        from thunder_os.account_cadence_readonly import AccountCadenceReadonly
        import os
        brain = Path(self.tmp.name) / 'brain'
        path = brain / 'contas/source-2020-01-01/cadencia.md'
        path.parent.mkdir(parents=True)
        path.write_text('account_id: dated-account\ncampaign_id: ' + BR +
                        '\n# Cadencia sintetica\n', encoding='utf-8')
        timestamp = datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc).timestamp()
        os.utime(path, (timestamp, timestamp))
        accounts = AccountCadenceReadonly(brain, clock=lambda: NOW)
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=accounts)
        snapshot = adapter.snapshot(campaign=BR)
        items = self.stage(snapshot, 'icp')['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['action'], {'label': 'Abrir', 'type': 'account', 'id': 'dated-account'})
        self.assertEqual(items[0]['date'], '2024-02-03T04:05:06+00:00')
        self.assertEqual(items[0]['date_basis'], 'source_file')
        self.assertEqual(items[0]['date_sourcepath'], 'contas/source-2020-01-01/cadencia.md')
        detail = adapter.account_detail('dated-account', campaign=BR)
        for key in ('date', 'date_basis', 'date_sourcepath'):
            self.assertEqual(items[0][key], detail[key])
        self.assertEqual(self.stage(adapter.snapshot(campaign=OTHER), 'icp')['items'], [])

    def test_all_observed_full_originals_survive_latest_outgoing_pending_unchanged(self):
        bodies = [' First contact\n\nExact <literal> & text.\n', 'Human original\nSecond paragraph.',
                  'Reply from owner', 'Followup after reply\n' + 'Full source line\n' * 2500]
        self.add_thread([message('latest', 13, True, bodies[3]), message('first', 7, True, bodies[0]),
                         message('human', 8, False, bodies[1]), message('owner', 9, True, bodies[2])])
        today = self.today.snapshot()
        self.assertEqual(today['blocks']['respond']['items'], [])
        self.assertEqual(today['blocks']['do']['items'], [])
        thread = today['observed_threads'][0]
        self.assertEqual([m['text'] for m in thread['thread']['messages']], bodies)
        self.assertEqual(thread['source']['id'], 'gmail')
        snap = self.adapter.snapshot()
        self.assertEqual(len(snap['timeline']['events']), 4)
        item = self.stage(snap, 'followup')['items'][0]
        detail = self.adapter.detail(item['id'])
        originals = [b['text'] for b in detail['blocks'] if b['body_complete']]
        self.assertEqual(originals, bodies)
        self.assertFalse(detail['editable'])
        self.assertFalse(detail['approval_available'])
        self.runner.assert_not_called()

    def test_prose_country_marker_matches_real_sheet_without_broad_inference(self):
        self.register('CHATTIE-01')
        self.register('MX27-CDMX')
        for literal in ('BR.', 'Brasil.', 'Brazil.', 'BR'):
            with patch.object(self.adapter.service, 'campaign_sheet_view', return_value={
                    'status': 'complete', 'fields': [{'key': 'country', 'label': 'Pais',
                        'status': 'ok', 'content': literal}]}):
                snapshot = self.adapter.snapshot()
            self.assertEqual({r['id'] for r in snapshot['campaigns']}, {BR, OTHER, 'CHATTIE-01'})
        for literal in ('MX.', 'BR. texto livre', 'BR..', 'desconhecido'):
            with patch.object(self.adapter.service, 'campaign_sheet_view', return_value={
                    'status': 'complete', 'fields': [{'key': 'country', 'label': 'Pais',
                        'status': 'ok', 'content': literal}]}):
                self.assertEqual(self.adapter.snapshot()['campaigns'], [])

    def test_source_log_uses_read_status_labels_not_internal_identifiers(self):
        self.today._gmail = Mock(side_effect=RuntimeError('isolated source'))
        snapshot = self.adapter.snapshot()
        messages = [row['message'] for row in snapshot['log']]
        self.assertIn('Historico local do Gmail: leitura indisponivel.', messages)
        self.assertIn('Cadencias HubSpot: leitura parcial.', messages)
        self.assertTrue(all('local:' not in text and 'hubspot_' not in text for text in messages))
        self.assertFalse(any('envio' in text.lower() or 'executou' in text.lower() for text in messages))

    def test_sent_only_thread_is_observed_and_not_pending(self):
        self.add_thread([message('out-only', 12, True, 'Original sent-only body')])
        today = self.today.snapshot()
        self.assertEqual(len(today['observed_threads']), 1)
        self.assertEqual(today['blocks']['respond']['items'], [])
        snap = self.adapter.snapshot()
        self.assertEqual(snap['timeline']['events'][0]['label'], 'Voce')

    def test_seqd_all_sent_and_sent_after_reply_reach_observed_collection(self):
        self.repo.put_state('identity', 'fixture', {'status': 'canonical', 'actor': 'ygor',
            'canonical_person': PEER, 'records': [{'domain': 'example.invalid', 'person_key': PEER,
                'hubspot_contact_id': '700'} for _ in range(2)]})
        port = Mock()
        port.messages.return_value = {'mailbox': MAILBOX, 'messages': [{'thread_id': 'abc'}]}
        port.conversation.return_value = {'mailbox': MAILBOX, 'thread': {'complete': True, 'truncated': False,
            'messages': [{'id': 'one', 'from': PEER, 'to': MAILBOX, 'labelIds': ['INBOX'],
                'internal_date': '1792742400000', 'complete': True, 'subject': 'Fixture',
                'bodies': {'text': 'Exact received'}},
                {'id': 'two', 'from': MAILBOX, 'to': PEER, 'labelIds': ['SENT'],
                'internal_date': '1792746000000', 'complete': True, 'subject': 'Fixture',
                'bodies': {'text': 'Exact latest sent'}}]}}
        self.today.seqd = SimpleNamespace(read_enabled=True, port=port)
        self.today._seqd = TodayReadonly._seqd.__get__(self.today)
        snap = self.today.snapshot()
        self.assertEqual(snap['blocks']['respond']['items'], [])
        self.assertEqual([m['text'] for m in snap['observed_threads'][0]['thread']['messages']],
                         ['Exact received', 'Exact latest sent'])
        port.conversation.return_value['thread']['messages'].pop(0)
        self.refresh()
        self.assertEqual(len(self.today.snapshot()['observed_threads']), 1)

    def test_provider_receipts_dedupe_person_across_tools_not_48_plus_100(self):
        self.add_thread([message('a', 7, True, 'First original', receipt=receipt('apollo', 'a'))],
                        ident='apollo', source='apollo')
        self.add_thread([message('b', 8, True, 'Second original', receipt=receipt('instantly', 'b'))],
                        ident='instantly', source='instantly', campaign=OTHER)
        snap = self.adapter.snapshot()
        self.assertEqual(snap['metrics']['reached']['value'], 1)
        self.assertFalse(snap['metrics']['reached']['coverage']['complete'])
        self.assertEqual(self.adapter.snapshot(campaign=OTHER)['metrics']['reached']['value'], 1)

    def test_partial_reply_metrics_and_no_fake_zero_cost_or_hot(self):
        self.instant(status='partial', complete=False, unclassified=2)
        snap = self.adapter.snapshot(campaign='BR-INSTANTLY-01')
        self.assertEqual(snap['metrics']['reached']['value'], 1)
        self.assertIsNone(snap['metrics']['replied']['value'])
        self.assertIsNone(snap['metrics']['hot']['value'])
        self.assertIsNone(snap['metrics']['cost_per_hot']['value'])
        self.assertNotEqual(snap['metrics']['reached']['value'], 100)

    def test_complete_classified_instantly_proves_zero_humans_only_for_its_scope(self):
        self.instant()
        snap = self.adapter.snapshot(campaign='BR-INSTANTLY-01')
        self.assertEqual(snap['metrics']['replied']['value'], 0)
        self.assertTrue(snap['metrics']['replied']['coverage']['complete'])
        self.add_thread([message('other', 10, False, 'Human body')])
        self.refresh()
        self.assertIsNone(self.adapter.snapshot()['metrics']['replied']['value'])

    def test_no_false_receipt_from_crm_sent_or_task_completion_or_draft(self):
        self.cadence.snapshot.return_value['campaigns'] = [{'id': BR, 'activities': [{
            'id': 'crm-email', 'type': 'email', 'direction': 'outgoing', 'status': 'SENT',
            'sent_evidence': True, 'crm_recorded_sent': True, 'occurred_at': '2026-10-23T10:00:00Z',
            'contact_ids': ['700'], 'text': 'CRM original'}], 'tasks': []}]
        self.add_thread([message('draft', 12, True, 'Draft', unsent=True, receipt=receipt('gmail', 'draft'))])
        snap = self.adapter.snapshot()
        self.assertIsNone(snap['metrics']['reached']['value'])
        self.assertFalse(any('Primeiro toque' in i['summary'] for i in self.stage(snap, 'abordagem')['items']))

    def test_hot_requires_explicit_classification_bound_to_exact_original(self):
        self.add_thread([message('human', 8, False, 'Human exact original')])
        self.repo.put_state('reply', 'unbound', {'classification': 'positive', 'state': 'classified',
            'content': 'Signal from a job posting', 'classification_history': [{'classification': 'positive'}]})
        self.assertIsNone(self.adapter.snapshot()['metrics']['hot']['value'])
        self.repo.put_state('reply', 'gmail:human', {'classification': 'positive', 'state': 'classified',
            'content': 'Human exact original', 'classification_history': [{'classification': 'positive'}]})
        self.assertEqual(self.adapter.snapshot()['metrics']['hot']['value'], 1)
        self.assertIsNone(self.adapter.snapshot()['metrics']['replied']['value'])

    def test_unknown_ids_out_of_filter_stale_and_stable_hashes(self):
        self.add_thread([message('human', 8, False, 'Original')])
        snap = self.adapter.snapshot()
        ident = self.stage(snap, 'resposta')['items'][0]['id']
        self.assertRegex(ident, r'^j_[a-f0-9]{64}$')
        self.assertEqual(ident, self.stage(self.adapter.snapshot(), 'resposta')['items'][0]['id'])
        for unknown in ('700', 'gmail:human', 'j_' + '0' * 64):
            with self.assertRaises(TodayError) as exc:
                self.adapter.detail(unknown)
            self.assertEqual(exc.exception.status, 404)
        self.rows[0]['messages'][0]['text'] = 'Changed source'
        self.refresh()
        with self.assertRaises(TodayError) as exc:
            self.adapter.detail(ident)
        self.assertEqual(exc.exception.code, 'journey_detail_stale')

    def test_partial_body_never_labelled_complete_and_empty_not_summary_original(self):
        self.add_thread([message('partial', 8, False, 'Truncated...', body_complete=False)])
        snap = self.adapter.snapshot()
        detail = self.adapter.detail(self.stage(snap, 'resposta')['items'][0]['id'])
        self.assertFalse(detail['blocks'][0]['body_complete'])
        self.assertEqual(detail['blocks'][0]['text'], 'Truncated...')
        self.assertNotEqual(detail['blocks'][0]['label'], 'Texto original')
        self.assertFalse(self.today.snapshot()['observed_threads'][0]['thread']['complete'])

    def test_detail_uses_only_observed_contact_for_crm_full_history(self):
        self.add_thread([message('old', 8, False, 'Original received')])
        self.cadence.timeline.return_value = {'coverage': {'complete': False}, 'events': [{
            'id': 'email:late', 'occurred_at': '2026-10-23T12:58:00Z', 'campaign': BR,
            'text': 'Exact late outgoing body\n\nNo rewrite.', 'direction': 'outgoing'}]}
        snap = self.adapter.snapshot(campaign=BR)
        detail = self.adapter.detail(self.stage(snap, 'resposta')['items'][0]['id'], campaign=BR)
        self.cadence.timeline.assert_called_once_with('700', campaign=BR)
        self.assertIn('Exact late outgoing body\n\nNo rewrite.', [b['text'] for b in detail['blocks']])
        with self.assertRaises(TodayError):
            self.adapter.detail('235718647634', campaign=BR)
        self.assertEqual(self.cadence.timeline.call_count, 1)

    def test_urls_allowlist_no_approval_and_no_db_mutation(self):
        self.add_thread([message('human', 8, False, 'Original')],
            action={'label': 'Injected', 'url': 'https://app.hubspot.com.evil.invalid/anything'})
        before = self.database()
        snap = self.adapter.snapshot()
        for stage in snap['stages']:
            for item in stage['items']:
                self.assertEqual(item['action'], {'label': 'Abrir', 'type': 'detail', 'id': item['id']})
                detail = self.adapter.detail(item['id'])
                self.assertFalse(detail['approval_available'])
                self.assertFalse(detail['editable'])
                self.assertTrue(detail['blocks'])
                self.assertEqual(detail['source_links'], [])
        self.assertEqual(before, self.database())
        self.runner.assert_not_called()
        with self.assertRaises(sqlite3.OperationalError):
            self.adapter.repo.put_state('reply', 'forbidden', {})

    def test_invalid_campaign_is_rejected_before_readers(self):
        for value in ('../MX', '', ['BR'], 'x' * 101):
            with self.subTest(value=value), self.assertRaises(TodayError) as exc:
                self.adapter.snapshot(value)
            self.assertEqual(exc.exception.status, 400)
        self.cadence.snapshot.assert_not_called()

    def test_brazil_requires_valid_registry_not_name_and_account_wrappers_filter(self):
        self.add_thread([message('unknown', 8, False, 'Unregistered BR name')],
                        ident='unregistered', campaign='BR-UNREGISTERED')
        self.add_thread([message('unlinked', 9, False, 'No region')], ident='unlinked', campaign=None)
        self.register('MX27-FIXTURE')
        self.register('GENERIC-FIXTURE')
        accounts = Mock()
        accounts.list_accounts.return_value = {'accounts': [
            {'id': 'a', 'campaign_id': BR, 'name': 'Known account', 'source': {'sha256': 'abc'}},
            {'id': 'b', 'campaign_id': 'MX27-FIXTURE'},
            {'id': 'c', 'campaign_id': None},
            {'id': 'd', 'campaign_id': 'BR-UNREGISTERED'},
            {'id': 'e', 'campaign_id': 'GENERIC-FIXTURE'}], 'limitations': []}
        accounts.account.return_value = {'id': 'a', 'campaign_id': BR, 'source': {'sha256': 'abc'},
                                        'source_text': 'Full actual account text', 'tabs': {}}
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=accounts)
        snap = adapter.snapshot()
        self.assertEqual({c['id'] for c in snap['campaigns']}, {BR, OTHER})
        self.assertEqual(self.stage(snap, 'resposta')['items'], [])
        scope = next(s for s in snap['sources'] if s['id'] == 'brazil_scope')
        self.assertEqual(scope['omitted']['threads'], 2)
        self.assertEqual(scope['omitted']['accounts'], 4)
        catalog = adapter.account_list()
        self.assertEqual([r['id'] for r in catalog['accounts']], ['a'])
        self.assertEqual(catalog['omitted_count'], 4)
        self.assertEqual(adapter.account_detail('a')['source_text'], 'Full actual account text')
        accounts.account.assert_called_once_with('a', campaign=BR)
        with self.assertRaises(TodayError):
            adapter.account_detail('b')
        self.assertEqual(accounts.account.call_count, 1)

    def test_mailbox_sender_does_not_merge_unrelated_buyers(self):
        self.add_thread([message('first', 7, True, 'One', receipt=receipt('gmail', 'first'))],
                        contact='700', person_keys=[MAILBOX, PEER])
        self.add_thread([message('second', 8, True, 'Two', receipt=receipt('gmail', 'second'))],
                        ident='second', contact='701', person_keys=[MAILBOX, 'second@example.invalid'])
        snap = self.adapter.snapshot()
        self.assertEqual(snap['metrics']['reached']['value'], 2)
        self.assertNotIn(MAILBOX, self.today.snapshot()['observed_threads'][0]['person_keys'])

    def test_fuller_canonical_message_observation_survives_deduplication(self):
        self.add_thread([message('same', 8, False, 'Truncated...', body_complete=False)])
        self.add_thread([message('same', 8, False, 'Full exact original\nLast marker')],
                        ident='second-reader', source='seqd')
        snap = self.adapter.snapshot()
        self.assertEqual(len(snap['timeline']['events']), 1)
        detail = self.adapter.detail(snap['timeline']['events'][0]['detail_id'])
        self.assertEqual(detail['blocks'][0]['text'], 'Full exact original\nLast marker')
        self.assertTrue(detail['blocks'][0]['body_complete'])

    def test_later_crm_outgoing_removes_pending_projection_without_delivery_receipt(self):
        self.add_thread([message('old-inbound', 8, False, 'Exact original inbound')])
        self.cadence.snapshot.return_value['campaigns'] = [{'id': BR, 'tasks': [], 'activities': [{
            'id': 'email:latest', 'contact_ids': ['700'], 'crm_recorded_sent': True,
            'occurred_at': '2026-10-23T12:58:00Z', 'text': 'Exact last outbound original',
            'subject': 'Latest CRM outgoing', 'type': 'email', 'receipt': None}]}]
        snap = self.adapter.snapshot()
        self.assertEqual(self.stage(snap, 'resposta')['items'], [])
        self.assertEqual(len(self.stage(snap, 'followup')['items']), 1)
        self.assertEqual(snap['timeline']['events'][-1]['label'], 'Voce')
        self.assertIsNone(snap['metrics']['reached']['value'])

    def test_unavailable_sqlite_dims_sources_instead_of_fabricating_zero(self):
        with patch.object(self.adapter.repo, 'transaction', side_effect=sqlite3.OperationalError('private')):
            snap = self.adapter.snapshot()
        self.assertIsNone(snap['metrics']['reached']['value'])
        self.assertIsNone(self.stage(snap, 'icp')['count'])
        self.assertNotIn('private', json.dumps(snap))

    def test_each_of_seven_stages_has_source_detail_or_explicit_missing_body(self):
        accounts = Mock()
        accounts.list_accounts.return_value = {'accounts': [{
            'id': 'fixture-account', 'campaign_id': BR, 'name': 'Recorded account'}], 'limitations': []}
        self.adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=accounts)
        self.add_thread([message('human', 8, False, 'Original inbound')])
        self.add_thread([message('sent', 9, True, 'Original sent', receipt=receipt('gmail', 'sent'))],
                        ident='outgoing')
        self.add_thread([message('linkedin', 10, False, 'Original LinkedIn')], ident='linkedin', source='linkedin')
        self.today._deals.return_value = {'status': 'ok', 'do': [{
            'id': 'hubspot:deal:900', 'title': 'Recorded deal', 'campaign_id': BR,
            'date': '2026-10-23T11:00:00Z', 'last_message': 'CRM next step, not original'}]}
        self.repo.put_state('meeting', 'fixture', {'account_id': 'fixture-account', 'campaign_id': BR,
            'person_key': PEER, 'occurred_at': '2026-10-24T12:00:00Z'})
        self.repo.put_state('research', 'fixture-account', {'facts': [{'text': 'Recorded account research'}]})
        snap = self.adapter.snapshot()
        for stage in snap['stages']:
            with self.subTest(stage=stage['id']):
                self.assertTrue(stage['items'])
                detail = self.adapter.detail(stage['items'][0]['id'])
                self.assertTrue(all(isinstance(b['text'], str) and b['text'] for b in detail['blocks']))
                self.assertFalse(detail['approval_available'])
        crm = self.adapter.detail(self.stage(snap, 'crm')['items'][0]['id'])
        self.assertEqual(crm['blocks'][0]['text'], MISSING)
        self.assertFalse(crm['blocks'][1]['body_complete'])

    def test_later_inbound_invalidates_old_positive_and_stale_provider_leaves_unknown(self):
        self.add_thread([message('positive', 8, False, 'Exact positive'),
                         message('later', 10, False, 'New unclassified original')])
        self.repo.put_state('reply', 'gmail:positive', {'classification': 'positive', 'state': 'classified',
            'content': 'Exact positive', 'classification_history': [{'classification': 'positive'}]})
        self.assertIsNone(self.adapter.snapshot()['metrics']['hot']['value'])
        self.instant(status='stale')
        self.assertIsNone(self.adapter.snapshot(campaign='BR-INSTANTLY-01')['metrics']['replied']['value'])

    def test_registry_failure_or_unregistered_campaign_cannot_turn_partial_into_zero(self):
        self.instant()
        with patch.object(self.adapter.service.campaign_registry, 'list_campaigns', side_effect=ValueError('bad')):
            snap = self.adapter.snapshot(campaign='BR-INSTANTLY-01')
        self.assertEqual(snap['campaigns'], [])
        self.assertIsNone(snap['metrics']['replied']['value'])
        self.assertIsNone(snap['metrics']['reached']['value'])

    def test_explicit_indexed_visitor_group_is_draft_not_campaign_and_last_four_status_only(self):
        from thunder_os.account_cadence_readonly import AccountCadenceReadonly
        reader = AccountCadenceReadonly(self.tmp.name)
        ready = ['vicunha', 'kyb', 'mcassab', 'temp-log', 'swarovski', 'ponto-da-lingerie']
        status_only = {'movida': 'warm_review', 'sansul': 'warm_review',
                       'hidrosina': 'blocked_mexico', 'fajo': 'discarded'}
        rows = [{'id': slug, 'name': 'Synthetic ' + slug, 'campaign_id': None,
            'source_group': 'visitantes-2026-10-22', 'readiness': status_only.get(slug, 'ready_for_review'),
            'source_index': {'path': '_entrada/claude/cadencias-visitantes-2026-10-22/README.md', 'sha256': 'b' * 64},
            'reviewable': slug in ready, 'source': {'sha256': 'a' * 64,
                'path': '_entrada/claude/cadencias-visitantes-2026-10-22/' + slug + '/cadencia.md'}}
            for slug in ready + list(status_only)]
        older = [{'id': 'old-' + str(i), 'campaign_id': None, 'source': {'path': 'contas/old-' + str(i) + '/cadencia.md'}}
                 for i in range(59)]
        details = {row['id']: {**deepcopy(row), 'source_text': 'Synthetic exact original body: ' + row['id'],
            'tabs': {'research': [], 'dossier': [], 'context': [],
                     'cadence': [{'text': 'Synthetic exact touch: ' + row['id']}], 'before_approval': []}}
            for row in rows}
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=reader)
        before = self.database()
        with patch.object(reader, 'list_accounts', return_value={'accounts': rows + older, 'limitations': []}), \
                patch.object(reader, 'account', side_effect=lambda id, campaign=None: deepcopy(details[id])):
            snap = adapter.snapshot()
            self.assertEqual({r['id'] for r in snap['campaigns']}, {BR, OTHER})
            group_items = [i for i in self.stage(snap, 'icp')['items'] if i.get('source_group')]
            self.assertEqual(len(group_items), 10)
            self.assertTrue(all(i['campaign_id'] is None and i['approval_available'] is False for i in group_items))
            self.assertTrue(all('nao cadastrada' in i['campaign_label'] for i in group_items))
            catalog = adapter.account_list()
            self.assertEqual(len(catalog['accounts']), 10)
            self.assertEqual(catalog['omitted_count'], 59)
            for slug in ready:
                detail = adapter.account_detail(slug)
                self.assertEqual(detail['source_text'], 'Synthetic exact original body: ' + slug)
                self.assertEqual(detail['tabs']['cadence'][0]['text'], 'Synthetic exact touch: ' + slug)
                self.assertTrue(detail['reviewable'])
                self.assertFalse(detail['approval_available'])
                self.assertFalse(detail['editable'])
                self.assertNotIn('canonical_cadence_id', detail)
            for slug in status_only:
                detail = adapter.account_detail(slug)
                self.assertTrue(detail['status_only'])
                self.assertEqual(detail['source_text'], '')
                self.assertEqual(detail['tabs']['cadence'], [])
                self.assertFalse(detail['approval_available'])
                self.assertNotIn('Synthetic exact touch', json.dumps(detail))
            self.assertIsNone(snap['metrics']['reached']['value'])
            self.assertEqual(snap['timeline']['events'], [])
            self.assertEqual(adapter.account_list(campaign=BR)['accounts'], [])
            with self.assertRaises(TodayError):
                adapter.account_detail('hidrosina', campaign=BR)
        self.assertEqual(before, self.database())

    def test_source_group_claim_from_generic_reader_is_not_authority(self):
        reader = Mock()
        reader.list_accounts.return_value = {'accounts': [{'id': 'pretend', 'campaign_id': None,
            'source_group': 'visitantes-2026-10-22', 'readiness': 'ready_for_review', 'reviewable': True,
            'source': {'path': '_entrada/claude/cadencias-visitantes-2026-10-22/kyb/cadencia.md'}}], 'limitations': []}
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=reader)
        self.assertEqual(adapter.account_list()['accounts'], [])
        with self.assertRaises(TodayError):
            adapter.account_detail('pretend')
        reader.account.assert_not_called()

    def canonical(self, **changes):
        return self.repo.put_state('cadence', 'actual-cadence', {'account_id': 'fixture-account',
            'person_key': PEER, 'status': 'ready_for_review',
            'execution_context': CampaignRegistry(self.repo).load(BR),
            'steps': [{'id': 'E1', 'recipient': PEER, 'channel': 'email',
                       'content': ' Exact canonical draft\n\nLast line.\n', 'subject': 'Exact subject', 'status': 'draft'}],
            **changes})

    def test_canonical_steps_actual_ids_exact_draft_full_context_no_get_authority(self):
        row = self.canonical()
        self.add_thread([message('incoming', 8, False, 'Full original lead message\nFinal marker')])
        before = self.database()
        with patch.object(ThunderService, 'review_step', side_effect=AssertionError('GET review forbidden')) as review, \
                patch.object(ThunderService, 'dispatch_step', side_effect=AssertionError('GET dispatch forbidden')) as dispatch:
            snap = self.adapter.snapshot()
            item = next(i for i in self.stage(snap, 'abordagem')['items'] if i['summary'].startswith('Passo canonico'))
            detail = self.adapter.detail(item['id'])
            self.assertEqual(detail['canonical_cadence_id'], 'actual-cadence')
            self.assertEqual(detail['canonical_step_id'], 'E1')
            self.assertEqual(detail['canonical_cadence_version'], row['version'])
            self.assertEqual(detail['draft']['text'], row['steps'][0]['content'])
            self.assertEqual(detail['draft']['subject'], 'Exact subject')
            self.assertEqual(detail['blocks'][0]['text'], 'Full original lead message\nFinal marker')
            self.assertTrue(detail['editable'])
            self.assertFalse(detail['approval_available'])
            review.assert_not_called()
            dispatch.assert_not_called()
        self.assertEqual(before, self.database())
        self.assertIsNone(self.repo.get_state('approval', 'actual-cadence'))
        self.assertIsNone(snap['metrics']['reached']['value'])

    def test_canonical_sent_discarded_pending_effect_and_missing_effect_source_not_editable(self):
        for changes in ({'status': 'sent'}, {'discarded': True}):
            step = {'id': 'E1', 'recipient': PEER, 'channel': 'email', 'content': 'Canonical body',
                    'status': 'draft', **changes}
            self.canonical(steps=[step])
            snap = self.adapter.snapshot()
            item = next(i for i in self.stage(snap, 'abordagem')['items'] if i['summary'].startswith('Passo canonico'))
            self.assertFalse(self.adapter.detail(item['id'])['editable'])

        self.canonical()
        self.repo.enqueue_effect('fixture-pending', {'cadence_id': 'actual-cadence', 'step': {'id': 'E1'}})
        snap = self.adapter.snapshot()
        item = next(i for i in self.stage(snap, 'abordagem')['items'] if i['summary'].startswith('Passo canonico'))
        self.assertFalse(self.adapter.detail(item['id'])['editable'])
        with patch.object(self.adapter, '_effects', side_effect=ValueError('unavailable')):
            snap = self.adapter.snapshot()
            item = next(i for i in self.stage(snap, 'abordagem')['items'] if i['summary'].startswith('Passo canonico'))
            self.assertFalse(self.adapter.detail(item['id'])['editable'])

    def test_confirmed_effect_projects_sent_without_mutating_approved_cadence(self):
        row=self.canonical()
        job=self.repo.enqueue_effect('fixture-confirmed',{'cadence_id':'actual-cadence','step':{'id':'E1'}})
        self.repo.finish_effect(job['id'],'sent','gmail:synthetic-confirmed',None)
        before=self.database()
        snap=self.adapter.snapshot()
        item=next(i for i in self.stage(snap,'abordagem')['items'] if i['summary'].startswith('Passo canonico'))
        detail=self.adapter.detail(item['id'])
        self.assertEqual(detail['step_status'],'sent')
        self.assertIn(': sent',item['summary'])
        self.assertFalse(detail['editable'])
        self.assertEqual(self.repo.get_state('cadence','actual-cadence'),row)
        self.assertEqual(before,self.database())

    def test_namespaced_canonical_ids_survive_projection_without_get_authority(self):
        original = self.canonical()
        payload = {k: v for k, v in original.items() if k not in {'id', 'version', 'updated_at'}}
        payload['steps'] = [{**payload['steps'][0], 'id': 'D0:email'}]
        self.repo.put_state('cadence', 'cadence:company.example.invalid', payload)
        self.repo.put_state('cadence', 'invalid/path', payload)
        before = self.database()
        snapshot = self.adapter.snapshot()
        details = [self.adapter.detail(row['id']) for row in self.stage(snapshot, 'abordagem')['items']
                   if row['summary'].startswith('Passo canonico')]
        matching = [row for row in details if row['canonical_cadence_id'] == 'cadence:company.example.invalid']
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['canonical_step_id'], 'D0:email')
        self.assertTrue(matching[0]['editable'])
        self.assertFalse(matching[0]['approval_available'])
        self.assertFalse(any(row['canonical_cadence_id'] == 'invalid/path' for row in details))
        self.assertEqual(before, self.database())

    def test_cold_readers_overlap_join_and_preserve_other_source_on_failure(self):
        import threading
        today_entered, cadence_entered = threading.Event(), threading.Event()
        original_today = self.today.snapshot
        original_cadence = self.cadence.snapshot.return_value
        def today():
            today_entered.set()
            if not cadence_entered.wait(2):
                raise AssertionError('cadence reader must start before Today finishes')
            return original_today()
        def cadence():
            cadence_entered.set()
            if not today_entered.wait(2):
                raise AssertionError('Today reader must start before cadence finishes')
            return original_cadence
        before = self.database()
        with patch.object(self.today, 'snapshot', side_effect=today):
            self.cadence.snapshot.side_effect = cadence
            snapshot = self.adapter.snapshot()
        source = {row['id']: row for row in snapshot['sources']}
        self.assertEqual(source['today']['status'], 'ok')
        self.assertEqual(source['cadence']['status'], 'partial')
        self.assertTrue(today_entered.is_set() and cadence_entered.is_set())
        self.assertFalse(any(t.is_alive() and t.name.startswith('thunder-journey-read')
                             for t in threading.enumerate()))
        self.cadence.snapshot.side_effect = RuntimeError('isolated source failure')
        degraded = self.adapter.snapshot()
        source = {row['id']: row for row in degraded['sources']}
        self.assertEqual(source['today']['status'], 'ok')
        self.assertEqual(source['cadence']['status'], 'offline')
        self.assertEqual(before, self.database())

    def test_account_adds_canonical_steps_by_account_id_never_markdown_touch_name(self):
        self.canonical()
        accounts = Mock()
        account = {'id': 'fixture-account', 'campaign_id': BR, 'source': {'path': None, 'sha256': None}}
        accounts.list_accounts.return_value = {'accounts': [account], 'limitations': []}
        accounts.account.return_value = {**account, 'tabs': {'cadence': [{'id': 'E1', 'text': 'Different Markdown original'}]}}
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=accounts)
        detail = adapter.account_detail('fixture-account', campaign=BR)
        touches = detail['tabs']['cadence']
        self.assertEqual(touches[0]['text'], 'Different Markdown original')
        self.assertNotIn('canonical_cadence_id', touches[0])
        self.assertEqual(touches[1]['canonical_cadence_id'], 'actual-cadence')
        self.assertEqual(touches[1]['canonical_step_id'], 'E1')
        self.assertEqual(touches[1]['source'], 'canonical_cadence_state')
        self.assertFalse(touches[1]['approval_available'])

    def account_document(self):
        from thunder_os.account_cadence_readonly import AccountCadenceReadonly, VISITOR_DIRECTORY, VISITOR_INDEX
        root = Path(self.tmp.name)
        index = root / VISITOR_INDEX
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text('# Cadências dos visitantes da página (Brasil)\n\n'
            '| Pasta | Conta e pessoa | Método | Status |\n|---|---|---|---|\n'
            '| fixture | Empresa, Pessoa | BAY | pronta para revisão |\n')
        path = root / VISITOR_DIRECTORY / 'fixture/cadencia.md'
        path.parent.mkdir()
        path.write_text('# Empresa | Pessoa, Cargo | Método BAY\n\n'
            '## Tese (1 linha)\nTese original.\n\n'
            '## Contexto usado\nthunder/provas.md SHA256 ' + 'a' * 64 + '\n')
        context = root / 'thunder/provas.md'
        context.parent.mkdir()
        context.write_text('Prova atual.')
        reader = AccountCadenceReadonly(root, repo=ReadonlyRepository(self.repo.path))
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=reader)
        ident = reader.list_accounts()['accounts'][0]['id']
        return adapter, reader, ident, path, index, context

    def test_account_documents_do_not_read_provider_catalogs_or_replace_journey_details(self):
        adapter, _, ident, *_ = self.account_document()
        adapter._details = {'sentinel': {'original': True}}
        before = self.database()
        with patch.object(self.today, 'snapshot', side_effect=RuntimeError('provider offline')) as today:
            self.cadence.snapshot.side_effect = RuntimeError('provider partial/failure')
            self.assertEqual([r['id'] for r in adapter.account_list()['accounts']], [ident])
            detail = adapter.account_detail(ident)
            self.assertIn('Tese original.', detail['source_text'])
            self.assertFalse(detail['editable'])
            self.assertFalse(detail['approval_available'])
            today.assert_not_called()
            self.cadence.snapshot.assert_not_called()
        self.assertEqual(adapter._details, {'sentinel': {'original': True}})
        self.assertEqual(before, self.database())

    def test_account_documents_recheck_source_index_and_context_without_ttl(self):
        adapter, _, ident, path, index, context = self.account_document()
        before = adapter.account_detail(ident)
        path.write_text(path.read_text().replace('Tese original.', 'Tese modificada.'))
        context.write_text('Prova modificada.')
        after = adapter.account_detail(ident)
        self.assertNotEqual(before['source']['sha256'], after['source']['sha256'])
        self.assertIn('Tese modificada.', after['source_text'])
        old = next(r for r in before['tabs']['context'] if r['path'] == 'thunder/provas.md')
        new = next(r for r in after['tabs']['context'] if r['path'] == 'thunder/provas.md')
        self.assertNotEqual(old['sha256'], new['sha256'])
        self.assertFalse(after['approval_available'])
        index.write_text(index.read_text().replace('pronta para revisão', 'bloqueada: já está em MX27-EXAMPLE (México)'))
        held = adapter.account_detail(ident)
        self.assertNotEqual(before['source_index']['sha256'], held['source_index']['sha256'])
        self.assertTrue(held['status_only'])
        self.assertEqual(held['source_text'], '')
        self.assertEqual(held['tabs']['cadence'], [])

    def test_account_documents_source_race_fails_closed(self):
        adapter, reader, ident, path, *_ = self.account_document()
        original = reader.account
        def changed(*args, **kwargs):
            path.write_text(path.read_text() + '\nChanged between list and detail.\n')
            return original(*args, **kwargs)
        with patch.object(reader, 'account', side_effect=changed):
            with self.assertRaises(TodayError) as raised:
                adapter.account_detail(ident)
        self.assertEqual((raised.exception.code, raised.exception.status), ('journey_account_stale', 409))

    def test_account_documents_index_race_cannot_reuse_previous_reviewability(self):
        adapter, reader, ident, _, index, *_ = self.account_document()
        original = reader.account
        def held(*args, **kwargs):
            index.write_text(index.read_text().replace('pronta para revisão', 'descartada: não prospectável'))
            return original(*args, **kwargs)
        with patch.object(reader, 'account', side_effect=held):
            with self.assertRaises(TodayError) as raised:
                adapter.account_detail(ident)
        self.assertEqual((raised.exception.code, raised.exception.status), ('journey_account_stale', 409))

    def test_account_document_does_not_wait_for_unrelated_journey_snapshot_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        adapter, _, ident, *_ = self.account_document()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            with adapter._lock:
                future = pool.submit(adapter.account_detail, ident)
                value = future.result(timeout=2)
                self.assertEqual(value['id'], ident)
                self.assertFalse(value['approval_available'])
        finally:
            pool.shutdown(wait=True)

    def test_account_catalog_rechecks_registry_and_partial_source_without_provider_reads(self):
        accounts = Mock()
        account = {'id': 'fixture-account', 'campaign_id': BR, 'source': {'path': None, 'sha256': None}}
        accounts.list_accounts.return_value = {'accounts': [account], 'limitations': ['source_file_limit']}
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=accounts)
        with patch.object(self.today, 'snapshot') as today:
            value = adapter.account_list()
            self.assertEqual([r['id'] for r in value['accounts']], ['fixture-account'])
            self.assertIn('source_file_limit', value['limitations'])
            self.service.campaign_registry.revoke(BR, 'ygor', 1)
            self.assertEqual(adapter.account_list()['accounts'], [])
            accounts.list_accounts.side_effect = RuntimeError('source offline')
            value = adapter.account_list()
            self.assertEqual(value['accounts'], [])
            self.assertIsNone(value['omitted_count'])
            self.assertIn('account_source_unavailable', value['limitations'])
            today.assert_not_called()
            self.cadence.snapshot.assert_not_called()

    def test_canonical_account_detail_refreshes_current_state_and_keeps_provider_guards(self):
        self.canonical()
        accounts = Mock()
        account = {'id': 'fixture-account', 'campaign_id': BR, 'source': {'path': None, 'sha256': None}}
        accounts.list_accounts.return_value = {'accounts': [account], 'limitations': []}
        accounts.account.side_effect = lambda *a, **k: {**account, 'tabs': {'cadence': []}}
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=accounts)
        before = adapter.account_detail('fixture-account', campaign=BR)['tabs']['cadence'][0]
        self.assertTrue(before['editable'])
        self.canonical(steps=[{'id': 'E1', 'channel': 'email', 'order': 1, 'content': 'New exact text',
                              'status': 'sent'}])
        after = adapter.account_detail('fixture-account', campaign=BR)['tabs']['cadence'][0]
        self.assertFalse(after['editable'])
        self.assertFalse(after['approval_available'])
        self.assertEqual(after['status'], 'sent')
        self.assertEqual(after['text'], 'New exact text')
        self.assertGreaterEqual(self.cadence.snapshot.call_count, 2)

    def test_canonical_account_campaign_revoked_during_snapshot_is_not_served(self):
        self.canonical()
        accounts = Mock()
        account = {'id': 'fixture-account', 'campaign_id': BR, 'source': {'path': None, 'sha256': None}}
        accounts.list_accounts.return_value = {'accounts': [account], 'limitations': []}
        adapter = JourneyReadonly(self.today, cadence_reader=self.cadence, accounts=accounts)
        snapshot = adapter.snapshot
        def revoke(*args, **kwargs):
            value = snapshot(*args, **kwargs)
            self.service.campaign_registry.revoke(BR, 'ygor', 1)
            return value
        with patch.object(adapter, 'snapshot', side_effect=revoke):
            with self.assertRaises(TodayError) as raised:
                adapter.account_detail('fixture-account', campaign=BR)
        self.assertEqual(raised.exception.status, 404)
        accounts.account.assert_not_called()


if __name__ == '__main__':
    unittest.main()
