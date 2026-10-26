"""C46-4/5 isolated sync proofs: synthetic contacts, real SQLite, blocked sockets."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from thunder_os.db import Repository
from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE, PORTAL_ID, HubSpotExternalEmail
from thunder_os.apollo_hubspot_sync import ApolloHubSpotSync, BINDING_KIND, CAMPAIGN_ID
from thunder_os.seqd_client import MAILBOX


def lead(ident='contact-a', address='person@example.invalid'):
    return {'id': ident, 'name': 'Person Example', 'first_name': 'Person', 'last_name': 'Example',
            'email': address, 'contact_emails': [{'email': 'alias+' + ident + '@example.invalid'}],
            'company': 'Synthetic Company', 'title': 'Synthetic Operations Director', 'linkedin_url': None,
            'membership_status': 'active', 'events': [sent(ident)]}


def sent(ident='contact-a'):
    return {'provider_id': 'message-' + ident, 'event_type': 'email_sent',
            'occurred_at': '2026-10-26T12:00:00Z', 'text': 'Synthetic complete message',
            'subject': 'Synthetic subject', 'content_complete': True}


def reply(address='person@example.invalid'):
    return {'provider': 'gmail', 'account_id': MAILBOX, 'provider_id': 'aabb1122',
            'provider_message_id': 'aabb1122', 'provider_thread_id': 'ccdd3344',
            'event_type': 'reply_received', 'human_reply': True, 'sender': address,
            'occurred_at': '2026-10-26T12:02:00Z', 'text': 'Synthetic human reply',
            'subject': 'Re: Synthetic subject', 'content_complete': True}


def snapshot(leads=None):
    return {'campaign_id': CAMPAIGN_ID, 'account_id': APOLLO_TEAM,
            'external_campaign_id': APOLLO_SEQUENCE, 'observed_at': '2026-10-26T12:05:00Z',
            'coverage': {'complete': True, 'contacts_complete': True, 'messages_complete': True},
            'leads': [lead()] if leads is None else leads}


class FakeHubSpot:
    def __init__(self):
        self.contacts, self.emails = {}, {}
        self.writes, self.snapshots = [], []
        self.fault = None
        self.legacy = False
        self.company_links = {'100': ['900']}

    def portal(self):
        return PORTAL_ID

    def existing(self, ident='100', address='person@example.invalid', aliases=''):
        self.contacts[ident] = {'id': ident, 'archived': False, 'properties':
            {'email': address, 'hs_additional_emails': aliases, 'firstname': 'Existing',
             'lastname': 'Owner', 'jobtitle': 'Preserve', 'hubspot_owner_id': '777'}}

    def contact_snapshot(self, addresses):
        self.snapshots.append(list(addresses))
        return {'portal_id': PORTAL_ID, 'complete': True, 'aliases_complete': True,
                'checked_emails': addresses, 'records': deepcopy(list(self.contacts.values()))}

    def read_object(self, kind, ident, properties):
        return deepcopy((self.contacts if kind == 'contacts' else self.emails).get(ident))

    def email_dedupe(self, plan):
        suffix = '\n\n[Thunder OS source: ' + plan['effect_id'] + ']'
        rows = [deepcopy(r) for r in self.emails.values() if r['properties']['hs_email_text'].endswith(suffix)]
        return {'complete': True, 'records': rows, 'unattributed_candidates': int(self.legacy)}

    def write(self, plan):
        self.writes.append(deepcopy(plan))
        kind = plan['object_type']
        rows = self.contacts if kind == 'contacts' else self.emails
        ident = str((100 if kind == 'contacts' else 500) + len(rows))
        properties = deepcopy(plan['properties'])
        if kind == 'contacts':
            properties['hs_additional_emails'] = ''
        row = {'id': ident, 'archived': False, 'properties': properties}
        if kind == 'emails':
            row['associations'] = {'contacts': {'results': [{'id': plan['contact_id']}]}}
        if self.fault != 'accepted_only':
            rows[ident] = row
        if self.fault == 'wrong_association' and kind == 'emails':
            row['associations']['contacts']['results'] = [{'id': '999'}]
        if self.fault == 'timeout' or self.fault == 'contact_timeout' and kind == 'contacts':
            raise TimeoutError('PRIVATE_TOKEN must never escape')
        return {'id': ident}


class ApolloHubSpotSyncTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.scratch' / 'apollo-hubspot-sync'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'isolated.db')
        self.crm = FakeHubSpot()
        self.sync = ApolloHubSpotSync(self.repo, self.crm, enabled=True, authorize=lambda p: True,
                                     permission_check=lambda: 'AVAILABLE')
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_default_dry_run_prepares_but_never_persists_or_writes(self):
        sync = ApolloHubSpotSync(self.repo, self.crm)
        out = sync.run(snapshot())
        self.assertEqual(out['status'], 'dry_run')
        self.assertEqual(out['writes'], 0)
        self.assertEqual(out['rows'][0]['contact_status'], 'would_create')
        self.assertEqual(out['rows'][0]['events'][0]['status'], 'awaiting_contact_readback')
        self.assertFalse(out['readiness']['ready'])
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])
        self.assertEqual(self.repo.list_state(BINDING_KIND), [])
        self.assertEqual(self.crm.writes, [])

    def test_permission_and_disabled_gate_before_crm_or_state(self):
        for enabled, check, auth in [(False, lambda: 'AVAILABLE', lambda p: True),
                (True, None, lambda p: True),
                (True, lambda: 'REQUIRES_PERMISSION_MODIFICATION', lambda p: True),
                (True, lambda: {'status': 'AVAILABLE', 'portal_id': '1'}, lambda p: True),
                (True, lambda: 'AVAILABLE', None)]:
            with self.subTest(enabled=enabled, check=check):
                crm = Mock()
                sync = ApolloHubSpotSync(self.repo, crm, enabled=enabled, authorize=auth, permission_check=check)
                out = sync.run(snapshot(), dry_run=False)
                self.assertEqual(out['status'], 'blocked')
                self.assertEqual(out['writes'], 0)
                self.assertEqual(crm.mock_calls, [])
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])

    def test_two_runs_reuse_original_create_plan_and_stable_ids(self):
        first = self.sync.run(snapshot(), dry_run=False)
        second = self.sync.run(snapshot(), dry_run=False)
        self.assertEqual(first['status'], 'processed')
        self.assertEqual(second['status'], 'processed')
        self.assertEqual(first['writes'], 2)
        self.assertEqual(second['writes'], 0)
        self.assertEqual(len(self.crm.contacts), 1)
        self.assertEqual(len(self.crm.emails), 1)
        self.assertEqual(first['rows'][0]['hubspot_contact_id'], second['rows'][0]['hubspot_contact_id'])
        self.assertEqual(len(self.repo.list_state(BINDING_KIND)), 1)
        self.assertEqual(second['rows'][0]['contact_plan']['action'], 'create')
        self.assertEqual(second['rows'][0]['events'][0]['status'], 'verified_state')
        preview = self.sync.run(snapshot())
        self.assertEqual(preview['rows'][0]['contact_status'], 'existing')
        self.assertEqual(preview['writes'], 0)
        binding = self.repo.list_state(BINDING_KIND)[0]
        self.assertEqual(binding['source']['company'], 'Synthetic Company')
        self.assertEqual(binding['source']['title'], 'Synthetic Operations Director')
        self.assertEqual(self.crm.contacts['100']['properties']['company'], 'Synthetic Company')
        self.assertEqual(self.crm.contacts['100']['properties']['jobtitle'], 'Synthetic Operations Director')
        self.assertEqual(binding['company_link_status'], 'company_link_pending')
        self.assertEqual(len(self.repo.list_state('cadence')), 0)

    def test_alias_existing_preserves_contact_properties_and_company_binding(self):
        self.crm.existing(address='other@example.invalid', aliases='person@example.invalid')
        before = deepcopy(self.crm.contacts)
        out = self.sync.run(snapshot(), dry_run=False)
        self.assertEqual(out['rows'][0]['hubspot_contact_id'], '100')
        self.assertEqual(out['writes'], 1)
        self.assertEqual(self.crm.contacts, before)
        self.assertEqual(self.crm.company_links, {'100': ['900']})
        self.assertEqual(out['rows'][0]['company_link_status'], 'company_link_pending')
        self.assertEqual(self.crm.writes[0]['object_type'], 'emails')

    def test_scope_partial_stale_and_duplicate_ids_block_before_crm(self):
        cases = [{'account_id': 'other'}, {'external_campaign_id': 'other'}, {'campaign_id': 'other'},
                 {'portal_id': '1'}, {'observed_at': 'bad'}, {'status': 'stale'},
                 {'coverage': {'complete': True, 'contacts_complete': False, 'messages_complete': True}},
                 {'leads': [lead(), lead()]}]
        for change in cases:
            with self.subTest(change=change):
                crm = Mock()
                out = ApolloHubSpotSync(self.repo, crm).run({**snapshot(), **change})
                self.assertEqual(out['status'], 'blocked')
                self.assertEqual(crm.mock_calls, [])

    def test_reply_only_partial_logs_proven_sent_and_holds_missing_reply(self):
        reasons = ['reply_original_unavailable', 'gmail_reply_read_unavailable',
                   'gmail_reply_discovery_unavailable', 'gmail_discovery_page_limit',
                   'gmail_reply_body_partial']
        for i, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                row = lead('partial-' + str(i), 'partial' + str(i) + '@example.invalid')
                self.crm.existing(str(200 + i), row['email'])
                row['events'].append({'provider_id': 'missing-reply-' + str(i),
                    'event_type': 'unclassified_reply', 'content_complete': False, 'human_reply': None})
                data = snapshot([row])
                data.update(status='partial')
                data['coverage'].update(complete=False, contents_complete=True, reasons=[reason])
                before = deepcopy(data)
                out = self.sync.run(data, dry_run=False)
                self.assertEqual(out['status'], 'partial')
                self.assertEqual(out['rows'][0]['contact_status'], 'verified_state')
                self.assertEqual(out['rows'][0]['events'][0]['status'], 'verified_state')
                self.assertEqual(out['rows'][0]['events'][1]['status'], 'held')
                self.assertEqual(out['writes'], 1)
                self.assertEqual(self.sync.run(data, dry_run=False)['writes'], 0)
                self.assertEqual(data, before)  # Never relabel source coverage as complete.
        self.assertEqual(len(self.crm.emails), 5)
        self.assertTrue(all(p['event_type'] == 'email_sent' for p in self.crm.writes))

    def test_reply_partial_exception_rejects_missing_flags_unknown_or_mixed_reasons(self):
        base = snapshot()
        base.update(status='partial')
        base['coverage'].update(complete=False, contents_complete=True,
                                reasons=['reply_original_unavailable'])
        changes = [
            {'reasons': []}, {'reasons': None}, {'reasons': 'reply_original_unavailable'},
            {'reasons': ['unknown']}, {'reasons': ['reply_original_unavailable', 'gmail_rate_limited']},
            {'reasons': ['completed_content_unverified']}, {'reasons': ['gmail_reply_reader_unavailable']},
            {'reasons': ['gmail_thread_partial']}, {'reasons': ['gmail_discovered_thread_context_only']},
            {'reasons': [['reply_original_unavailable']]},
            {'contacts_complete': False}, {'messages_complete': False}, {'contents_complete': False},
            {'complete': None}, {'complete': True}, {'contacts_complete': 1}, {'contents_complete': 1},
        ]
        for change in changes:
            with self.subTest(change=change):
                data = deepcopy(base)
                data['coverage'].update(change)
                crm = Mock()
                out = ApolloHubSpotSync(self.repo, crm).run(data)
                self.assertEqual(out['error_code'], 'incomplete_source')
                self.assertEqual(crm.mock_calls, [])
        for missing in ['complete', 'contacts_complete', 'messages_complete', 'contents_complete', 'reasons']:
            data = deepcopy(base)
            del data['coverage'][missing]
            self.assertEqual(self.sync.run(data)['error_code'], 'incomplete_source')
        for change in [{'status': 'ok'}, {'status': None}, {'status': 'stale'},
                       {'stale': True}, {'enabled': False}]:
            self.assertEqual(self.sync.run({**base, **change})['error_code'], 'incomplete_source')
        data = deepcopy(base)
        data['account_id'] = 'other'
        self.assertEqual(self.sync.run(data)['error_code'], 'source_identity_mismatch')
        self.assertEqual(self.crm.writes, [])
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])

    def test_reply_only_partial_never_changes_email_permission_gate(self):
        data = snapshot()
        data.update(status='partial')
        data['coverage'].update(complete=False, contents_complete=True,
                                reasons=['reply_original_unavailable'])
        crm = Mock()
        sync = ApolloHubSpotSync(self.repo, crm, enabled=True, authorize=lambda p: True,
                                permission_check=lambda: 'REQUIRES_PERMISSION_MODIFICATION')
        out = sync.run(data, dry_run=False)
        self.assertEqual(out['status'], 'blocked')
        self.assertEqual(out['error_code'], 'email_write_permission_unavailable')
        self.assertEqual(out['writes'], 0)
        self.assertEqual(crm.mock_calls, [])
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])

    def test_ambiguous_contacts_held_no_email_untouched(self):
        self.crm.existing('100')
        self.crm.existing('101')
        noemail = lead('no-email', None)
        noemail['contact_emails'] = []
        out = self.sync.run(snapshot([lead(), noemail]), dry_run=False)
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(out['rows'][0]['error_code'], 'ambiguous_contact_identity')
        self.assertEqual(out['rows'][1]['contact_status'], 'no_email')
        self.assertEqual(self.crm.writes, [])

    def test_batch_dedupe_once_for_preparation_and_explicit_exact_snapshot_cursor(self):
        leads = [lead('contact-' + str(i), 'person' + str(i) + '@example.invalid') for i in range(12)]
        out = self.sync.run(snapshot(leads))
        self.assertEqual(len(out['rows']), 10)
        self.assertEqual(len(self.crm.snapshots), 1)
        self.assertEqual(len(self.crm.snapshots[0]), 20)
        more = self.sync.run(snapshot(leads), cursor=out['next_cursor'])
        self.assertEqual(len(more['rows']), 2)
        self.assertIsNone(more['next_cursor'])
        changed = snapshot(leads)
        changed['observed_at'] = '2026-10-26T13:00:00Z'
        self.assertEqual(self.sync.run(changed, cursor=out['next_cursor'])['error_code'], 'invalid_cursor')
        self.assertEqual(self.sync.run(snapshot(), max_contacts=11)['error_code'], 'invalid_contact_limit')
        selected = self.sync.run(snapshot(leads), exact_emails=['alias+contact-11@example.invalid'])
        self.assertEqual([r['source_contact_id'] for r in selected['rows']], ['contact-11'])

    def test_timeout_and_restart_never_retry_post_and_recover_by_canonical_id(self):
        self.crm.existing()
        self.crm.fault = 'timeout'
        out = self.sync.run(snapshot(), dry_run=False)
        self.assertEqual(out['rows'][0]['events'][0]['status'], 'pending_reconciliation')
        restarted = ApolloHubSpotSync(Repository(self.repo.path), self.crm, enabled=True,
                authorize=lambda p: True, permission_check=lambda: 'AVAILABLE')
        again = restarted.run(snapshot(), dry_run=False)
        self.assertEqual(again['writes'], 0)
        self.assertEqual(len(self.crm.writes), 1)
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(again))
        self.assertEqual(restarted.run(snapshot())['rows'][0]['events'][0]['status'], 'pending_reconciliation')
        from thunder_os.external_email_import import ExternalEmailImport
        from thunder_os.apollo_hubspot_sync import validate_sync_plan
        attempt = next(a for a in self.repo.list_state('hubspot_attempt') if a['plan']['object_type'] == 'emails')
        importer = ExternalEmailImport(self.repo, self.crm, enabled=True, authorize=lambda p: True,
                                       plan_validator=validate_sync_plan)
        self.assertEqual(importer.reconcile(attempt['plan'], recovered_id='500')['status'], 'verified_state')
        self.assertEqual(restarted.run(snapshot(), dry_run=False)['rows'][0]['events'][0]['status'], 'verified_state')
        self.assertEqual(len(self.crm.writes), 1)

    def test_contact_timeout_stops_events_and_later_runs_do_not_retry(self):
        self.crm.fault = 'contact_timeout'
        first = self.sync.run(snapshot(), dry_run=False)
        second = self.sync.run(snapshot(), dry_run=False)
        self.assertEqual(first['rows'][0]['contact_status'], 'pending_reconciliation')
        self.assertEqual(second['rows'][0]['contact_status'], 'pending_reconciliation')
        self.assertEqual(len(self.crm.writes), 1)
        self.assertEqual(self.crm.emails, {})
        self.assertEqual(self.repo.list_state(BINDING_KIND), [])

    def test_changed_contact_or_original_body_id_metadata_is_held(self):
        self.crm.existing()
        self.sync.run(snapshot(), dry_run=False)
        changed = snapshot()
        changed['leads'][0]['first_name'] = 'Changed'
        self.assertEqual(self.sync.run(changed, dry_run=False)['rows'][0]['error_code'], 'sync_source_changed')
        changed = snapshot()
        changed['leads'][0]['events'][0]['text'] = 'Changed original body'
        out = self.sync.run(changed, dry_run=False)
        self.assertEqual(out['rows'][0]['events'][0]['error_code'], 'sync_source_changed')
        changed = snapshot()
        changed['leads'][0]['events'][0]['provider_thread_id'] = 'changed-thread'
        self.assertEqual(self.sync.run(changed, dry_run=False)['rows'][0]['events'][0]['error_code'], 'sync_source_changed')
        self.assertEqual(len(self.crm.writes), 1)

    def test_changed_title_invalidates_saved_contact_plan_without_patch(self):
        data = snapshot()
        self.sync.run(data, dry_run=False)
        before = deepcopy(self.crm.contacts)
        data['leads'][0]['title'] = 'Changed title'
        out = self.sync.run(data, dry_run=False)
        self.assertEqual(out['rows'][0]['error_code'], 'sync_source_changed')
        self.assertEqual(out['writes'], 0)
        self.assertEqual(self.crm.contacts, before)

    def test_human_original_gmail_reply_honest_source_and_skip_signals_automatic_thirdparty(self):
        self.crm.existing()
        row = lead()
        row['events'] = [reply(), {**reply(), 'provider_id': 'bad-auto', 'event_type': 'auto_reply_received'},
            {**reply(), 'provider_id': 'bad-unknown', 'event_type': 'unclassified_reply', 'human_reply': None},
            {**reply(), 'provider_id': 'bad-third', 'sender': 'thirdparty@example.invalid'},
            {**reply(), 'provider_id': 'bad-signal', 'provider': 'apollo'},
            {**reply(), 'provider_id': 'bad-unverified', 'human_reply': None},
            {**reply(), 'provider_id': 'bad-outgoing', 'event_type': 'email_sent'}]
        out = self.sync.run(snapshot([row]), dry_run=False)
        self.assertEqual(out['rows'][0]['events'][0]['status'], 'verified_state')
        self.assertTrue(all(e['status'] == 'held' for e in out['rows'][0]['events'][1:]))
        self.assertEqual(len(self.crm.writes), 1)
        plan = self.crm.writes[0]
        self.assertEqual(plan['source']['provider'], 'gmail')
        self.assertEqual(plan['source']['account_id'], MAILBOX)
        self.assertEqual(plan['source']['provider_id'], 'aabb1122')
        self.assertEqual(plan['properties']['hs_email_direction'], 'INCOMING_EMAIL')
        self.assertNotIn('hs_email_message_id', plan['properties'])
        self.assertIn('[Thunder OS source: gmail:', plan['properties']['hs_email_text'])

    def test_legacy_or_partial_activity_dedupe_never_creates(self):
        self.crm.existing()
        self.crm.legacy = True
        out = self.sync.run(snapshot(), dry_run=False)
        self.assertEqual(out['rows'][0]['events'][0]['status'], 'needs_human')
        self.assertEqual(self.crm.writes, [])
        self.assertEqual(self.sync.run(snapshot(), dry_run=False)['writes'], 0)

    def test_incomplete_content_and_event_limit_preserve_no_activity(self):
        self.crm.existing()
        row = lead()
        row['events'][0]['content_complete'] = False
        out = self.sync.run(snapshot([row]), dry_run=False)
        self.assertEqual(out['rows'][0]['events'][0]['error_code'], 'incomplete_event_content')
        row['events'] = [sent(str(i)) for i in range(101)]
        out = self.sync.run(snapshot([row]), dry_run=False)
        self.assertEqual(out['rows'][0]['error_code'], 'event_limit')
        self.assertEqual(self.crm.writes, [])

    def test_revoked_permission_during_preflight_stops_email_post(self):
        self.crm.existing()
        original = self.crm.email_dedupe
        allowed = [True]
        self.sync.permission_check = lambda: 'AVAILABLE' if allowed[0] else 'REQUIRES_PERMISSION_MODIFICATION'
        def dedupe(plan):
            allowed[0] = False
            return original(plan)
        self.crm.email_dedupe = dedupe
        out = self.sync.run(snapshot(), dry_run=False)
        self.assertEqual(out['rows'][0]['events'][0]['status'], 'needs_human')
        self.assertEqual(self.crm.writes, [])

    def test_wrong_association_or_acceptance_not_readback_is_pending(self):
        for fault in ['wrong_association', 'accepted_only']:
            with self.subTest(fault=fault):
                row = lead('contact-' + fault, fault + '@example.invalid')
                self.crm.existing(str(200 + len(self.crm.contacts)), row['email'])
                self.crm.fault = fault
                out = self.sync.run(snapshot([row]), dry_run=False)
                self.assertEqual(out['rows'][0]['events'][0]['status'], 'pending_reconciliation')
                self.assertFalse(out['rows'][0]['events'][0]['email_logged'])

    def test_concurrent_runs_shared_repository_never_duplicate(self):
        def run(_):
            return ApolloHubSpotSync(Repository(self.repo.path), self.crm, enabled=True,
                authorize=lambda p: True, permission_check=lambda: 'AVAILABLE').run(snapshot(), dry_run=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, range(2)))
        final = run(None)
        self.assertEqual(len(self.crm.contacts), 1)
        self.assertEqual(len(self.crm.emails), 1)
        self.assertEqual(final['rows'][0]['events'][0]['status'], 'verified_state')

    def test_reader_nested_source_reply_unknown_and_third_party_never_imported(self):
        self.crm.existing()
        human = reply()
        human.pop('account_id')
        human['source'] = {'provider': 'gmail', 'account_id': MAILBOX,
            'external_campaign_id': APOLLO_SEQUENCE, 'campaign_id': CAMPAIGN_ID,
            'provider_id': human['provider_id']}
        human.update(source_association='canonical_contact', receipt=True)
        row = lead()
        row['events'] = [human, {**human, 'human_reply': None, 'event_type': 'unclassified_reply'},
                         {**human, 'sender': 'fernanda@example.invalid', 'event_type': 'third_party_reply'},
                         {**human, 'source_association': 'thread_context'}]
        out = self.sync.run(snapshot([row]), dry_run=False)
        self.assertEqual(out['rows'][0]['events'][0]['status'], 'verified_state')
        self.assertTrue(all(e['status'] == 'held' for e in out['rows'][0]['events'][1:]))
        self.assertEqual(len(self.crm.writes), 1)
        self.assertEqual(self.crm.writes[0]['sync_provenance']['provider_thread_id'], 'ccdd3344')

    def test_out_of_order_and_repeated_events_do_not_duplicate(self):
        self.crm.existing()
        row = lead()
        row['events'] = [reply(), sent(), reply(), sent()]
        self.sync.run(snapshot([row]), dry_run=False)
        second = self.sync.run(snapshot([row]), dry_run=False)
        self.assertEqual(len(self.crm.emails), 2)
        self.assertEqual(second['writes'], 0)
        self.assertTrue(all(e['status'] == 'verified_state' for e in second['rows'][0]['events']))

    def test_scheduled_no_receipt_conflicting_account_and_partial_dedupe_are_held(self):
        self.crm.existing()
        row = lead()
        row['events'] = [{**sent(), 'provider_status': 'scheduled'}, {**sent(), 'receipt': False},
                         {**sent(), 'account_id': 'wrong'},
                         {**reply(), 'source': {'provider': 'apollo'}}]
        out = self.sync.run(snapshot([row]), dry_run=False)
        self.assertTrue(all(e['status'] == 'held' for e in out['rows'][0]['events']))
        self.assertEqual(self.crm.writes, [])
        self.crm.email_dedupe = lambda p: {'complete': False, 'records': [], 'unattributed_candidates': 0}
        out = self.sync.run(snapshot(), dry_run=False)
        self.assertEqual(out['rows'][0]['events'][0]['status'], 'needs_human')
        self.assertEqual(self.crm.writes, [])

    def test_authorization_false_and_underlying_writer_off_never_reserve(self):
        self.sync.authorize = lambda p: False
        out = self.sync.run(snapshot(), dry_run=False)
        self.assertEqual(out['rows'][0]['error_code'], 'email_write_not_authorized')
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])
        port = HubSpotExternalEmail(Mock(), enabled=False)
        sync = ApolloHubSpotSync(self.repo, port, enabled=True, authorize=lambda p: True,
                                permission_check=lambda: 'AVAILABLE')
        self.assertFalse(sync.readiness()['ready'])
        self.assertEqual(sync.run(snapshot(), dry_run=False)['status'], 'blocked')
        self.assertEqual(self.repo.list_state('hubspot_attempt'), [])

    def test_same_instance_busy_fails_without_crm_calls(self):
        with self.sync._run_lock:
            self.assertEqual(self.sync.run(snapshot(), dry_run=False)['error_code'], 'sync_busy')
        self.assertEqual(self.crm.snapshots, [])

    def test_narrower_transport_validator_is_never_overridden(self):
        from thunder_os.apollo_hubspot_sync import _GuardedTransport
        from thunder_os.external_email_import import ExternalEmailError
        request = Mock(side_effect=AssertionError('must not dispatch'))
        def restricted(plan):
            raise ExternalEmailError('restricted_runtime_policy')
        port = HubSpotExternalEmail(lambda: 'synthetic-token', request=request, enabled=True,
                                    authorize=lambda p: True, plan_validator=restricted)
        sync = ApolloHubSpotSync(self.repo, port, enabled=True, authorize=lambda p: True,
                                permission_check=lambda: 'AVAILABLE')
        with self.assertRaisesRegex(ExternalEmailError, 'restricted_runtime_policy'):
            _GuardedTransport(sync).write(sync._event_plan(reply(), lead(), '100'))
        request.assert_not_called()
        self.assertIs(port.validate_plan, restricted)

    def test_gmail_wire_uses_extension_without_mutating_shared_hubspot_validator(self):
        from thunder_os.apollo_hubspot_sync import _GuardedTransport
        from thunder_os.external_email_import import _validate_plan
        calls = []
        def request(method, path, body, headers, timeout):
            calls.append((method, path, body))
            return 201, b'{"id":"500"}'
        port = HubSpotExternalEmail(lambda: 'synthetic-token', request=request,
                                    enabled=True, authorize=lambda p: True)
        sync = ApolloHubSpotSync(self.repo, port, enabled=True, authorize=lambda p: True,
                                permission_check=lambda: 'AVAILABLE')
        plan = sync._event_plan(reply(), lead(), '100')
        _GuardedTransport(sync).write(plan)
        self.assertIs(port.validate_plan, _validate_plan)
        self.assertEqual(calls[0][0:2], ('POST', '/crm/v3/objects/emails'))
        self.assertEqual(calls[0][2]['associations'][0]['to']['id'], '100')


if __name__ == '__main__':
    unittest.main()
