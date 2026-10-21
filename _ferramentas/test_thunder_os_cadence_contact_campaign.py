"""Current-contact commercial attribution; isolated HubSpot transport fixtures."""
from copy import deepcopy
import json
import unittest

from test_thunder_os_cadence_readonly import Provider, task
from thunder_os.cadence_readonly import ACTIVITY_PROPERTIES, CadenceReadonlyConsole


CAMPAIGN = 'BR-APOLLO-01'
OTHER = 'MX27-INSTANTLY'


def email(ident='900', **properties):
    return {'id': ident, 'properties': {
        'hs_timestamp': '2025-01-01T13:00:00Z', 'hs_email_direction': 'EMAIL',
        'hs_email_status': 'SENT', 'hs_email_subject': 'Synthetic email', **properties}}


def contact(ident='100', campaign=CAMPAIGN, executor='apollo'):
    return {'id': ident, 'properties': {'campanha_ativa': campaign, 'executor': executor}}


class Registry:
    def list_campaigns(self):
        return [{'id': CAMPAIGN, 'platform': 'apollo', 'active': True},
                {'id': OTHER, 'platform': 'instantly', 'active': True}]


class ContactProvider(Provider):
    def __init__(self):
        super().__init__()
        self.tasks = []
        self.emails = [email()]
        self.email_pages = None
        self.contact_schema = {'campanha_ativa', 'executor'}
        self.contacts = {'100': contact()}
        self.email_links = {'900': ['100']}
        self.association_pages = {}
        self.batch_status = 'COMPLETE'

    def __call__(self, method, path, body, headers, timeout):
        result = None
        if path == '/crm/v3/properties/contacts':
            result = {'results': [{'name': field} for field in sorted(self.contact_schema)]}
        elif path == '/crm/v3/properties/emails':
            result = {'results': [{'name': field} for field in ACTIVITY_PROPERTIES['emails']]}
        elif path == '/crm/v3/objects/emails/search':
            result = (self.email_pages[str(body.get('after', ''))] if self.email_pages is not None
                      else {'results': deepcopy(self.emails), 'total': len(self.emails)})
        elif path == '/crm/v4/associations/emails/contacts/batch/read':
            rows = []
            for item in body['inputs']:
                page = self.association_pages.get((item['id'], str(item.get('after', ''))))
                rows.append(deepcopy(page) if page else {'from': {'id': item['id']},
                    'to': [{'toObjectId': cid} for cid in self.email_links.get(item['id'], [])]})
            result = {'status': 'COMPLETE', 'results': rows}
        elif path == '/crm/v3/objects/contacts/batch/read' and 'campanha_ativa' in body['properties']:
            result = {'status': self.batch_status, 'results': [deepcopy(self.contacts[item['id']])
                for item in body['inputs'] if item['id'] in self.contacts]}
        if result is None:
            return super().__call__(method, path, body, headers, timeout)
        self.calls.append((method, path, deepcopy(body)))
        return 200, json.dumps(result).encode()


class ContactCampaignTests(unittest.TestCase):
    def console(self, provider=None, **kwargs):
        kwargs.setdefault('campaign_registry', Registry())
        return CadenceReadonlyConsole(lambda: 'synthetic-token', request=provider or ContactProvider(),
                                     sleep=lambda _: None, **kwargs)

    def group(self, result, ident=CAMPAIGN):
        return next(group for group in result['campaigns'] if group['id'] == ident)

    def test_current_campaign_exact_registry_not_event_fields(self):
        for ident, executor in [(CAMPAIGN, 'apollo'), (OTHER, 'instantly')]:
            provider = ContactProvider()
            provider.contacts['100'] = contact(campaign=ident, executor=executor)
            provider.emails[0]['properties'].update(hs_campaign_guid='wrong-campaign',
                                                   hs_engagement_source_id='unmapped-source')
            result = self.console(provider, activity_bindings=[{'campaign_id': CAMPAIGN,
                'provider': 'apollo', 'campaign_guid': 'wrong-campaign', 'source_id': 'unmapped-source'}]).snapshot()
            group = self.group(result, ident)
            self.assertEqual(group['metrics']['external_sent'], 1)
            event = group['activities'][0]
            self.assertEqual(event['campaign'], ident)
            self.assertEqual(event['attribution'], 'contact_current_campaign')
            self.assertEqual(event['contact_ids'], ['100'])
            self.assertFalse(event['historical_membership_verified'])
            self.assertIsNone(event['origin_provider'])
            self.assertEqual(event['origin_identifiers']['hs_engagement_source_id'], 'unmapped-source')
            queries = [body for _, path, body in provider.calls if path.endswith('/emails/search')]
            self.assertEqual(queries[0]['filterGroups'], [])
            self.assertEqual(self.group(result, OTHER if ident == CAMPAIGN else CAMPAIGN)['activities'], [])

    def test_missing_unknown_multiple_and_conflicting_contacts(self):
        for value in ['unknown', CAMPAIGN.lower(), CAMPAIGN + ';' + OTHER,
                      [CAMPAIGN, OTHER], CAMPAIGN + ' ']:
            with self.subTest(value=value):
                provider = ContactProvider()
                provider.contacts['100']['properties']['campanha_ativa'] = value
                result = self.console(provider).snapshot()
                self.assertEqual(self.group(result)['activities'], [])
                self.assertIsNone(self.group(result)['metrics']['external_sent'])
                self.assertEqual(len(result['unattributed_activities']), 1)
        provider = ContactProvider()
        provider.contacts['101'] = contact('101', OTHER, 'instantly')
        provider.email_links['900'].append('101')
        result = self.console(provider).snapshot()
        self.assertEqual(result['unattributed_activities'][0]['attribution_reason'], 'contact_campaign_conflict')
        self.assertTrue(all(not group['activities'] for group in result['campaigns']))
        self.assertTrue(all(group['metrics']['external_sent'] is None for group in result['campaigns']))

    def test_control_explicit_empty_other_platform_and_omitted(self):
        class WithOtherPlatform(Registry):
            def list_campaigns(self):
                return super().list_campaigns() + [
                    {'id': 'BR-HUBSPOT', 'platform': 'hubspot_seq', 'active': True}]
        for campaign, executor in [(CAMPAIGN, 'apollo'), (OTHER, 'instantly')]:
            for case in ['control', 'explicit_null', 'explicit_empty', 'other_platform', 'omitted']:
                with self.subTest(provider=executor, case=case):
                    provider = ContactProvider()
                    provider.contacts['100'] = contact(campaign=campaign, executor=executor)
                    if case != 'control':
                        provider.emails.append(email('901'))
                        provider.email_links['901'] = ['101']
                        provider.contacts['101'] = contact('101', campaign=None, executor=None)
                        if case == 'explicit_empty':
                            provider.contacts['101']['properties']['campanha_ativa'] = ''
                        elif case == 'other_platform':
                            provider.contacts['101'] = contact('101', 'BR-HUBSPOT', 'hubspot_seq')
                        elif case == 'omitted':
                            provider.contacts['101']['properties'].pop('campanha_ativa')
                    result = self.console(provider, campaign_registry=WithOtherPlatform()).snapshot()
                    group = self.group(result, campaign)
                    self.assertEqual(group['metrics']['observed_external_sent'], 1)
                    self.assertEqual(group['metrics']['external_sent'], None if case == 'omitted' else 1)
                    other_campaign = OTHER if campaign == CAMPAIGN else CAMPAIGN
                    self.assertEqual(self.group(result, other_campaign)['metrics']['external_sent'],
                                     None if case == 'omitted' else 0)
                    self.assertEqual(result['coverage']['partial'], case == 'omitted')
                    self.assertEqual(group['external_activity_state'], 'partial' if case == 'omitted' else 'complete')
                    self.assertEqual([event['id'] for event in group['activities']], ['emails:900'])
                    if case != 'control':
                        expected = ('contact_campaign_unavailable' if case == 'omitted' else
                            'contact_campaign_out_of_scope' if case == 'other_platform' else 'contact_without_campaign')
                        self.assertEqual(result['unattributed_activities'][0]['attribution_reason'], expected)
                        member = next(row for row in result['contact_memberships'] if row['id'] == '101')
                        self.assertEqual(member['status'], 'campaign_missing_or_unknown' if case == 'omitted' else
                                         'linked' if case == 'other_platform' else 'without_campaign')

    def test_excluded_email_classification_does_not_degrade_totals(self):
        class WithOtherPlatform(Registry):
            def list_campaigns(self):
                return super().list_campaigns() + [
                    {'id': 'BR-HUBSPOT', 'platform': 'hubspot_seq', 'active': True}]
        for campaign, executor in [(CAMPAIGN, 'apollo'), (OTHER, 'instantly')]:
            for excluded in [None, '', 'BR-HUBSPOT']:
                with self.subTest(provider=executor, excluded=excluded):
                    provider = ContactProvider()
                    provider.contacts['100'] = contact(campaign=campaign, executor=executor)
                    provider.emails.append(email('901', hs_timestamp='invalid', hs_email_direction=None))
                    provider.email_links['901'] = ['101']
                    provider.contacts['101'] = contact('101', excluded, None)
                    result = self.console(provider, campaign_registry=WithOtherPlatform()).snapshot()
                    self.assertEqual(self.group(result, campaign)['metrics']['external_sent'], 1)
                    self.assertTrue(result['coverage']['complete'])

    def test_explicit_empty_only_does_not_become_unknown_but_mixed_contact_remains_conflict(self):
        for value in [None, '']:
            provider = ContactProvider()
            provider.contacts['100']['properties']['campanha_ativa'] = value
            result = self.console(provider).snapshot()
            self.assertEqual(self.group(result)['metrics']['external_sent'], 0)
            self.assertEqual(self.group(result)['activities'], [])
            self.assertTrue(result['coverage']['complete'])
            provider.contacts['101'] = contact('101')
            provider.email_links['900'].append('101')
            result = self.console(provider).snapshot()
            self.assertIsNone(self.group(result)['metrics']['external_sent'])
            self.assertEqual(result['unattributed_activities'][0]['attribution_reason'], 'contact_campaign_conflict')

    def test_recorded_sent_not_delivery_or_reply_inference(self):
        provider = ContactProvider()
        provider.emails = [email(), email('901', hs_email_direction='INCOMING_EMAIL'),
                           email('902', hs_email_status='FAILED')]
        provider.email_links = {row['id']: ['100'] for row in provider.emails}
        result = self.console(provider).snapshot()
        group = self.group(result)
        self.assertEqual(group['metrics']['external_sent'], 1)
        self.assertIsNone(group['metrics']['replies'])
        self.assertIsNone(group['metrics']['meetings'])
        self.assertEqual(group['metric_availability']['external_sent'], 'crm_recorded_sent_not_delivery')
        self.assertTrue(all(event['receipt'] is None for event in group['activities']))
        self.assertTrue(all(event['delivery_verified'] is False for event in group['activities']))
        self.assertTrue(all(event['response_evidence'] is False for event in group['activities']))
        self.assertFalse(any(path.endswith('/meetings/search') for _, path, _ in provider.calls))

    def test_pagination_and_partial_coverage(self):
        provider = ContactProvider()
        provider.email_pages = {'': {'results': [email()], 'total': 2,
            'paging': {'next': {'after': 'next', 'link': 'https://evil.invalid/'}}},
            'next': {'results': [email(), email('901')], 'total': 2}}
        provider.email_links['901'] = ['100']
        result = self.console(provider).snapshot()
        self.assertEqual(self.group(result)['metrics']['external_sent'], 2)
        self.assertTrue(result['coverage']['sources']['external_emails']['complete'])
        result = self.console(provider, max_pages=1).snapshot()
        self.assertIsNone(self.group(result)['metrics']['external_sent'])
        self.assertEqual(self.group(result)['metrics']['observed_external_sent'], 1)
        self.assertTrue(result['coverage']['partial'])
        self.assertFalse(any('evil.invalid' in path for _, path, _ in provider.calls))

    def test_contact_schema_batch_and_budget_failures(self):
        for fault in ['schema', 'missing_record', 'pending_batch', 'budget']:
            with self.subTest(fault=fault):
                provider = ContactProvider()
                if fault == 'schema':
                    provider.contact_schema.clear()
                elif fault == 'missing_record':
                    provider.contacts.clear()
                elif fault == 'pending_batch':
                    provider.batch_status = 'PENDING'
                result = self.console(provider, **({'max_requests': 7} if fault == 'budget' else {})).snapshot()
                self.assertIsNone(self.group(result)['metrics']['external_sent'])
                self.assertEqual(self.group(result)['activities'], [])
                self.assertTrue(result['coverage']['partial'])
                self.assertFalse(any(method in {'PATCH', 'DELETE', 'PUT'} for method, _, _ in provider.calls))
                self.assertTrue(all(method == 'GET' or path.endswith(('/search', '/batch/read'))
                                    for method, path, _ in provider.calls))

    def test_association_pagination_same_campaign_and_unverified_ids(self):
        provider = ContactProvider()
        provider.contacts['101'] = contact('101')
        provider.association_pages = {('900', ''): {'from': {'id': '900'},
            'to': [{'toObjectId': '100'}], 'paging': {'next': {'after': 'next'}}},
            ('900', 'next'): {'from': {'id': '900'}, 'to': [{'toObjectId': '101'}]}}
        result = self.console(provider).snapshot()
        self.assertEqual(self.group(result)['metrics']['external_sent'], 1)
        self.assertEqual(self.group(result)['activities'][0]['contact_ids'], ['100', '101'])
        result = self.console(provider, max_pages=1).snapshot()
        self.assertEqual(self.group(result)['activities'], [])
        self.assertIsNone(self.group(result)['metrics']['external_sent'])
        provider = ContactProvider()
        provider.email_links.clear()
        result = self.console(provider).snapshot()
        self.assertEqual(result['unattributed_activities'][0]['attribution_reason'], 'contact_id_unavailable')

    def test_email_only_timeline_and_contact_isolation(self):
        provider = ContactProvider()
        provider.emails.append(email('901'))
        provider.contacts['101'] = contact('101')
        provider.email_links['901'] = ['101']
        # Contact activity context also contains email 300 in the inherited fixture.
        result = self.console(provider).timeline('100', CAMPAIGN)
        attributed = [event for event in result['events'] if event['attribution'] == 'contact_current_campaign']
        self.assertEqual([event['id'] for event in attributed], ['emails:900'])
        with self.assertRaises(LookupError):
            self.console(provider).timeline('999', CAMPAIGN)

    def test_contacts_union_distinct_tasks_and_attributed_emails(self):
        for campaign, executor in [(CAMPAIGN, 'apollo'), (OTHER, 'instantly')]:
            with self.subTest(provider=executor):
                provider = ContactProvider()
                provider.contacts['100'] = contact(campaign=campaign, executor=executor)
                result = self.console(provider).snapshot()
                group = self.group(result, campaign)
                self.assertEqual(group['metrics']['external_sent'], 1)
                self.assertEqual(group['metrics']['contacts'], 1)
                self.assertEqual(group['metrics']['observed_contacts'], 1)
                provider.tasks = [task(title=f'[{campaign}][E1] Test | EMAIL',
                    hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None)]
                provider.task_links = {'1': ['100', '102']}
                provider.contacts['102'] = contact('102', campaign, executor)
                provider.contacts['101'] = contact('101', campaign, executor)
                provider.emails.extend([email('901'), email('902')])
                provider.email_links.update({'901': ['100'], '902': ['101']})
                result = self.console(provider).snapshot()
                group = self.group(result, campaign)
                self.assertEqual(group['metrics']['external_sent'], 3)
                self.assertEqual(group['metrics']['contacts'], 3)
                self.assertEqual(group['metrics']['observed_contacts'], 3)
                self.assertEqual(group['metric_availability']['contacts'], 'observed_task_and_email_contacts')

    def test_contacts_union_preserves_unknown_coverage(self):
        for campaign, executor in [(CAMPAIGN, 'apollo'), (OTHER, 'instantly')]:
            for fault in ['email_partial', 'task_partial', 'schema_missing']:
                with self.subTest(provider=executor, fault=fault):
                    provider = ContactProvider()
                    provider.contacts['100'] = contact(campaign=campaign, executor=executor)
                    provider.tasks = [task(title=f'[{campaign}][E1] Test | EMAIL',
                        hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None)]
                    provider.task_links = {'1': ['100']}
                    if fault == 'email_partial':
                        provider.email_pages = {'': {'results': [email()], 'paging': {'next': {'after': 'next'}}}}
                    elif fault == 'task_partial':
                        provider.pages = {'': {'results': provider.tasks, 'total': 2}}
                    else:
                        provider.contact_schema.clear()
                    result = self.console(provider, max_pages=1).snapshot()
                    group = self.group(result, campaign)
                    self.assertIsNone(group['metrics']['contacts'])
                    self.assertEqual(group['metrics']['observed_contacts'], 1)
                    self.assertTrue(result['coverage']['partial'])

    def test_displays_conflict(self):
        provider = ContactProvider()
        provider.tasks = [task(title=f'[{OTHER}][E1] Test | EMAIL',
                               hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None)]
        provider.task_links = {'1': ['100']}
        provider.contacts['100']['properties']['executor'] = 'instantly'
        result = self.console(provider).snapshot()
        conflicts = result['contact_conflicts']
        self.assertTrue(any(row['reason'] == 'task_campaign_differs_from_current_campaign' for row in conflicts))
        self.assertTrue(any(row['reason'] == 'executor_differs_from_registry_platform' for row in conflicts))
        self.assertEqual(self.group(result)['activities'][0]['campaign'], CAMPAIGN)
        self.assertFalse(any(path.endswith(('/batch/update', '/batch/create')) for _, path, _ in provider.calls))

    def test_registry_inactive_wrong_platform_and_absent_cannot_attribute(self):
        for entry in [[], [{'id': CAMPAIGN, 'platform': 'apollo', 'active': False}],
                      [{'id': CAMPAIGN, 'platform': 'codex_local', 'active': True}]]:
            class OtherRegistry:
                def list_campaigns(self):
                    return entry
            provider = ContactProvider()
            result = self.console(provider, campaign_registry=OtherRegistry()).snapshot()
            self.assertFalse(any(group['activities'] for group in result['campaigns']))
            self.assertFalse(any(path.endswith('/emails/search') for _, path, _ in provider.calls))

    def test_optional_event_fields_and_legacy_loader_are_not_gates(self):
        provider = ContactProvider()
        def request(method, path, body, headers, timeout):
            if path == '/crm/v3/properties/emails':
                provider.calls.append((method, path, body))
                return 200, json.dumps({'results': [{'name': field} for field in ACTIVITY_PROPERTIES['emails']
                    if field not in {'hs_campaign_guid', 'hs_engagement_source_id'}]}).encode()
            return provider(method, path, body, headers, timeout)
        def forbidden_loader():
            self.fail('legacy attribution loader was called')
        result = self.console(request, activity_bindings_loader=forbidden_loader).snapshot()
        self.assertEqual(self.group(result)['metrics']['external_sent'], 1)
        self.assertTrue(result['coverage']['complete'])

    def test_missing_executor_does_not_invent_value_or_hide_current_association(self):
        provider = ContactProvider()
        provider.contact_schema.remove('executor')
        provider.contacts['100']['properties'].pop('executor')
        result = self.console(provider).snapshot()
        self.assertEqual(self.group(result)['activities'][0]['attribution'], 'contact_current_campaign')
        self.assertIsNone(result['contact_memberships'][0]['executor'])
        self.assertIsNone(self.group(result)['metrics']['external_sent'])
        self.assertEqual(self.group(result)['metrics']['observed_external_sent'], 1)
        self.assertIn('contact_campaigns:configuration_pending', result['coverage']['limitations'])
        self.assertEqual(self.group(result)['external_activity_state'], 'configuration_pending')

    def test_invalid_batches_never_prove_contact_identity(self):
        for fault in ['duplicate_contact', 'unexpected_contact', 'archived_contact', 'pending_association']:
            with self.subTest(fault=fault):
                provider = ContactProvider()
                def request(method, path, body, headers, timeout):
                    status, raw = provider(method, path, body, headers, timeout)
                    result = json.loads(raw)
                    if path == '/crm/v3/objects/contacts/batch/read':
                        if fault == 'duplicate_contact':
                            result['results'].append(contact(campaign=OTHER, executor='instantly'))
                        elif fault == 'unexpected_contact':
                            result['results'][0]['id'] = '999'
                        elif fault == 'archived_contact':
                            result['results'][0]['archived'] = True
                    if fault == 'pending_association' and path == '/crm/v4/associations/emails/contacts/batch/read':
                        result['status'] = 'PENDING'
                    return status, json.dumps(result).encode()
                result = self.console(request).snapshot()
                self.assertEqual(self.group(result)['activities'], [])
                self.assertIsNone(self.group(result)['metrics']['external_sent'])
                self.assertTrue(result['coverage']['partial'])

    def test_schema_permission_failure_is_sanitized_and_no_retry(self):
        provider = ContactProvider()
        calls = []
        def request(method, path, body, headers, timeout):
            if path == '/crm/v3/properties/contacts':
                calls.append(path)
                return 403, b'{"message":"synthetic-token private provider body"}'
            return provider(method, path, body, headers, timeout)
        result = self.console(request).snapshot()
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.group(result)['metrics']['external_sent'])
        self.assertNotIn('private provider body', json.dumps(result))
        self.assertNotIn('synthetic-token', json.dumps(result))

    def test_205_contact_and_association_inputs_are_batched(self):
        provider = ContactProvider()
        provider.emails = [email(str(900 + i)) for i in range(205)]
        provider.email_pages = {}
        for offset in range(0, 205, 100):
            page = {'results': provider.emails[offset:offset + 100], 'total': 205}
            if offset + 100 < 205:
                page['paging'] = {'next': {'after': str(offset + 100)}}
            provider.email_pages[str(offset) if offset else ''] = page
        provider.contacts = {str(100 + i): contact(str(100 + i)) for i in range(205)}
        provider.email_links = {str(900 + i): [str(100 + i)] for i in range(205)}
        result = self.console(provider).snapshot()
        self.assertEqual(self.group(result)['metrics']['external_sent'], 205)
        for path in ['/crm/v4/associations/emails/contacts/batch/read', '/crm/v3/objects/contacts/batch/read']:
            batches = [body for _, called, body in provider.calls if path == called]
            self.assertEqual([len(body['inputs']) for body in batches], [100, 100, 5])

    def test_complete_empty_read_can_prove_zero_but_missing_schema_cannot(self):
        provider = ContactProvider()
        provider.emails.clear()
        result = self.console(provider).snapshot()
        self.assertEqual(self.group(result)['metrics']['external_sent'], 0)
        self.assertTrue(result['coverage']['complete'])
        provider.contact_schema.clear()
        result = self.console(provider).snapshot()
        self.assertIsNone(self.group(result)['metrics']['external_sent'])
        self.assertEqual(self.group(result)['external_activity_state'], 'configuration_pending')

    def test_malformed_classification_is_unknown_instead_of_crashing(self):
        for props in [{'hs_email_status': []}, {'hs_email_direction': {}}, {'hs_timestamp': 'bad'}]:
            provider = ContactProvider()
            provider.emails = [email(**props)]
            result = self.console(provider).snapshot()
            self.assertIsNone(self.group(result)['metrics']['external_sent'])
            self.assertEqual(self.group(result)['metrics']['observed_external_sent'], 0)

    def test_partial_association_page_cannot_be_repaired_by_later_complete_page(self):
        provider = ContactProvider()
        provider.association_pages = {('900', ''): {'from': {'id': '900'},
            'to': [{'toObjectId': '100'}], 'paging': {'next': {'after': 'next'}}},
            ('900', 'next'): {'from': {'id': '900'}, 'to': []}}
        def request(method, path, body, headers, timeout):
            status, raw = provider(method, path, body, headers, timeout)
            result = json.loads(raw)
            if path == '/crm/v4/associations/emails/contacts/batch/read' and 'after' not in body['inputs'][0]:
                result['status'] = 'PENDING'
            return status, json.dumps(result).encode()
        result = self.console(request).snapshot()
        self.assertEqual(self.group(result)['activities'], [])
        self.assertIsNone(self.group(result)['metrics']['external_sent'])

    def test_association_cursor_cycle_never_proves_link(self):
        provider = ContactProvider()
        page = {'from': {'id': '900'}, 'to': [{'toObjectId': '100'}],
                'paging': {'next': {'after': 'loop'}}}
        provider.association_pages = {('900', ''): page, ('900', 'loop'): page}
        result = self.console(provider).snapshot()
        self.assertEqual(self.group(result)['activities'], [])
        self.assertIsNone(self.group(result)['metrics']['external_sent'])
        self.assertIn('email_contacts:pagination_cycle_or_invalid_cursor', result['coverage']['limitations'])


if __name__ == '__main__':
    unittest.main()
