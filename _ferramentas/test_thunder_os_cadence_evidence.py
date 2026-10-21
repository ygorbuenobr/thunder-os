"""Synthetic Task2 evidence; no customer records, network or production writes."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
import os

from thunder_os.cadence_readonly import project_task, project_snapshot, CadenceReadonlyConsole
from thunder_os.seqd_client import MAILBOX
from test_thunder_os_cadence_readonly import Provider, task

BATCH = 'MX27-CDMX-D0-20261021'
STAMP = '2026-10-21T14:03:03.618Z'
HASH = 'a' * 64


def mexico(identifier='1', contact='101', outcome='sent'):
    metadata = {'campaign': 'Mexico2027-networking', 'batch': BATCH,
                'task_key': f'{BATCH}:{contact}:E1', 'contact_id': contact,
                'recipient': 'synthetic@example.test', 'provider': 'Gmail',
                'account': MAILBOX,
                'sha256_subject_newline_text': HASH}
    messages = {
        'sent': f'E1 CONFIRMADO Gmail abc123 em {STAMP}; destinatario synthetic@example.test; SHA256 {HASH}.',
        'reconciled': f'E1 CONFIRMADO POR RECONCILIACAO SOMENTE LEITURA: Gmail abc123; destinatario synthetic@example.test; SHA256 {HASH}. Nenhum reenvio.',
        'uncertain': 'RESULTADO E1 21/10/2026: Tentativa Gmail retornou HTTP403; sem recibo. Busca posterior em Enviados nao encontrou mensagem. Resultado nao confirmado; sem retry.',
        'blocked': 'RESULTADO E1 21/10/2026: Historico LinkedIn incompleto: somente convite de saida visivel; paginacao retorna hasMore=true sem cursor. Nao classificar como resposta. E1 nao enviado.',
        'reply': 'RESULTADO E1 21/10/2026: Resposta humana no LinkedIn. Suspender cadencia fria nos dois canais.',
        'none': '',
    }
    body = '<pre>' + json.dumps(metadata) + '</pre><p>' + messages[outcome] + '</p>'
    return task(identifier, title=f'{BATCH} | E1 | Pessoa teste | Empresa teste', contact=None,
                hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None,
                hs_task_body=body, hs_task_status='COMPLETED' if outcome in {'sent', 'reconciled', 'none'} else 'WAITING')


def project(record):
    return project_task(record, associated_contact_ids=['101'], association_complete=True)


def coverage(complete=True):
    return {'complete': complete, 'partial': not complete, 'limitations': [],
            'sources': {'tasks': {'complete': complete}, 'task_schema': {'complete': complete}}}


class EvidenceTests(unittest.TestCase):
    def test_recibo_executor_mexico(self):
        # Acceptance population is synthetic; production code has no fixed totals.
        outcomes = ['sent'] * 81 + ['reconciled'] * 2 + ['uncertain'] * 4 + ['blocked'] * 12 + ['reply']
        rows = [mexico(str(index + 1), outcome=outcome) for index, outcome in enumerate(outcomes)]
        for index, row in enumerate(rows):
            row['properties']['hs_task_body'] = row['properties']['hs_task_body'].replace('abc123', f'abc{index:03x}')
        result = project_snapshot(rows, collected_at=STAMP, coverage=coverage(),
            associations={row['id']: ['101'] for row in rows})
        group = result['campaigns'][0]
        self.assertEqual(len(group['tasks']), 100)
        self.assertEqual(group['metrics']['completed_with_receipt'], 83)
        self.assertEqual(group['metrics']['uncertain'], 4)
        self.assertEqual(group['metrics']['blocked'], 12)
        self.assertEqual(sum(row['outcome'] == 'paused_reply' for row in group['tasks']), 1)

    def test_receipt_requires_complete_exact_contact_link_and_mailbox(self):
        for contacts, complete in [([], True), ([], False), (['101'], False), (['101', '102'], True)]:
            result = project_task(mexico(), associated_contact_ids=contacts, association_complete=complete)
            self.assertIsNone(result['receipt'])
            self.assertNotEqual(result['outcome'], 'sent_with_receipt')
        row = mexico()
        row['properties']['hs_task_body'] = row['properties']['hs_task_body'].replace(MAILBOX, 'other@example.test')
        self.assertIsNone(project(row)['receipt'])
        blocked = project_task(mexico(outcome='blocked'), associated_contact_ids=[], association_complete=False)
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['reason_code'], 'LINKEDIN_HISTORY_INCOMPLETE')

    def test_malformed_metadata_degrades_without_exception(self):
        for field in ['contact_id', 'task_key', 'recipient', 'account', 'provider', 'sha256_subject_newline_text']:
            for value in [None, True, 99, [], {}, '']:
                row = mexico()
                body = row['properties']['hs_task_body']
                metadata = json.loads(body.split('<pre>')[1].split('</pre>')[0])
                metadata[field] = value
                row['properties']['hs_task_body'] = '<pre>' + json.dumps(metadata) + '</pre>' + body.split('</pre>', 1)[1]
                self.assertIsNone(project(row)['receipt'], (field, value))

    def test_exact_batch_mapping_and_no_prefix_alias(self):
        self.assertEqual(project(mexico())['campaign'], 'MX27-CDMX')
        row = mexico()
        row['properties']['hs_task_subject'] = row['properties']['hs_task_subject'].replace(BATCH, BATCH + '-OTHER')
        self.assertIsNone(project(row))

    def test_tarefa_sem_recibo(self):
        result = project(mexico(outcome='none'))
        self.assertEqual(result['outcome'], 'completed_without_receipt')
        self.assertIsNone(result['receipt'])

    def test_recibo_executor_mexico_and_later_reconciliation(self):
        for outcome in ['sent', 'reconciled']:
            row = mexico(outcome=outcome)
            row['properties']['hs_task_body'] = '<p>Resultado antigo: sem recibo.</p>' + row['properties']['hs_task_body']
            result = project(row)
            self.assertEqual(result['outcome'], 'sent_with_receipt')
            self.assertEqual(result['receipt']['message_id'], 'abc123')
            self.assertEqual(result['receipt']['task_id'], '1')
            self.assertEqual(result['receipt']['verification'], 'recorded_confirmation')
            self.assertFalse(result['retry_allowed'])

    def test_uncertain_distinct_from_history_block(self):
        result = project(mexico(outcome='uncertain'))
        self.assertEqual(result['outcome'], 'uncertain_no_retry')
        self.assertFalse(result['retry_allowed'])
        self.assertIsNone(result['receipt'])
        blocked = project(mexico(outcome='blocked'))
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['reason_code'], 'LINKEDIN_HISTORY_INCOMPLETE')

    def test_receipt_mismatch_and_quoted_marker_not_accepted(self):
        for old, new in [(HASH, 'b' * 64), ('synthetic@example.test; SHA256', 'other@example.test; SHA256')]:
            row = mexico()
            body = row['properties']['hs_task_body']
            start = body.index('</pre>')
            row['properties']['hs_task_body'] = body[:start] + body[start:].replace(old, new)
            self.assertIsNone(project(row)['receipt'])
        row = mexico()
        row['properties']['hs_task_body'] = row['properties']['hs_task_body'].replace('<p>E1 CONFIRMADO', '<p>Texto citado: E1 CONFIRMADO')
        self.assertIsNone(project(row)['receipt'])
        self.assertIsNone(project_task(mexico(), associated_contact_ids=['999'], association_complete=True)['receipt'])

    def test_observed_counts_not_constants_and_partial_totals_null(self):
        for count in [2, 5]:
            rows = [mexico(str(i + 1)) for i in range(count)]
            # Distinct synthetic provider receipts; one receipt cannot count twice.
            for i, row in enumerate(rows):
                row['properties']['hs_task_body'] = row['properties']['hs_task_body'].replace('abc123', f'abc{i + 1:03x}')
            rows += [mexico('80', outcome='uncertain'), mexico('81', outcome='blocked')]
            for complete in [True, False]:
                result = project_snapshot(rows, collected_at=STAMP, coverage=coverage(complete),
                    associations={row['id']: ['101'] for row in rows})
                metrics = result['campaigns'][0]['metrics']
                self.assertEqual(metrics['observed_completed_with_receipt'], count)
                self.assertEqual(metrics['observed_uncertain'], 1)
                self.assertEqual(metrics['observed_blocked'], 1)
                self.assertEqual(metrics['completed_with_receipt'], count if complete else None)
                self.assertIsNone(metrics['replies'])
                self.assertIsNone(metrics['meetings'])

    def test_provider_receipt_deduplication(self):
        rows = [mexico('1'), mexico('2')]
        metrics = project_snapshot(rows, collected_at=STAMP, coverage=coverage(),
            associations={'1': ['101'], '2': ['101']})['campaigns'][0]['metrics']
        self.assertEqual(metrics['observed_completed_with_receipt'], 1)

    def test_one_unverified_association_keeps_receipt_total_unknown(self):
        result = project_snapshot([mexico('1'), mexico('2')], collected_at=STAMP, coverage=coverage(),
            associations={'1': ['101']}, associations_complete={'1'})
        self.assertEqual(result['campaigns'][0]['metrics']['observed_completed_with_receipt'], 1)
        self.assertIsNone(result['campaigns'][0]['metrics']['completed_with_receipt'])

    def test_later_uncertain_does_not_inherit_receipt(self):
        row = mexico()
        row['properties']['hs_task_body'] += mexico(outcome='uncertain')['properties']['hs_task_body'].split('</pre>')[1]
        self.assertIsNone(project(row)['receipt'])
        self.assertEqual(project(row)['outcome'], 'uncertain_no_retry')


class ExternalActivityTests(unittest.TestCase):
    # The user's Task 3 decision supersedes GUID/source attribution in Task 2.
    def test_event_pair_does_not_attribute_incoming_or_meetings(self):
        provider = Provider()
        provider.tasks = [task(title='[APOLLO-TEST][E1] Test | EMAIL', hs_task_sequence_id=None,
                               hs_task_sequence_step_enrollment_id=None)]
        common = {'hs_campaign_guid': 'guid', 'hs_engagement_source_id': 'app', 'hs_timestamp': STAMP}
        email = {'id': '901', 'properties': {**common, 'hs_email_direction': 'INCOMING_EMAIL'}}
        meeting = {'id': '902', 'properties': {**common, 'hs_meeting_outcome': 'SCHEDULED'}}
        def request(method, path, body, headers, timeout):
            if path.endswith('/emails/search'):
                return 200, json.dumps({'results': [email], 'total': 1}).encode()
            if path.endswith('/meetings/search'):
                return 200, json.dumps({'results': [meeting], 'total': 1}).encode()
            return provider(method, path, body, headers, timeout)
        result = CadenceReadonlyConsole(lambda: 'synthetic-token', request=request, sleep=lambda _: None,
            activity_bindings=[{'campaign_id': 'APOLLO-TEST', 'provider': 'apollo',
                               'campaign_guid': 'guid', 'source_id': 'app'}]).snapshot()
        group = result['campaigns'][0]
        self.assertIsNone(group['metrics']['replies'])
        self.assertIsNone(group['metrics']['meetings'])
        self.assertIsNone(group['metrics']['external_sent'])
        self.assertEqual(group['activities'], [])

    def test_wrong_portal_stops_all_provider_reads_after_identity(self):
        class Registry:
            def list_campaigns(self):
                return [{'id': 'APOLLO-TEST', 'platform': 'apollo', 'active': True}]
        provider = Provider()
        provider.portal = '999'
        result = CadenceReadonlyConsole(lambda: 'synthetic-token', request=provider, sleep=lambda _: None,
            campaign_registry=Registry(), activity_bindings=[{'campaign_id': 'APOLLO-TEST', 'provider': 'apollo',
                'campaign_guid': 'guid', 'source_id': 'app'}]).snapshot()
        self.assertEqual([call[1] for call in provider.calls], ['/account-info/v3/details'])
        self.assertEqual(result['campaigns'][0]['id'], 'APOLLO-TEST')
        self.assertIsNone(result['campaigns'][0]['metrics']['external_sent'])
        self.assertFalse(result['coverage']['complete'])

    def test_missing_activity_classification_is_unknown_not_zero(self):
        from test_thunder_os_cadence_contact_campaign import ContactProvider, Registry, CAMPAIGN
        for missing in ['hs_email_status', 'hs_email_direction']:
            provider = ContactProvider()
            provider.emails[0]['properties'].pop(missing)
            result = CadenceReadonlyConsole(lambda: 'synthetic-token', request=provider, sleep=lambda _: None,
                                             campaign_registry=Registry()).snapshot()
            group = next(row for row in result['campaigns'] if row['id'] == CAMPAIGN)
            self.assertIsNone(group['metrics']['external_sent'], missing)
            self.assertEqual(group['metrics']['observed_external_sent'], 0)
            self.assertFalse(result['coverage']['complete'])

    def test_runtime_binding_input_is_unused_for_contact_attribution(self):
        from thunder_os.cadence_readonly import environment_activity_bindings
        from test_thunder_os_cadence_contact_campaign import ContactProvider, Registry, CAMPAIGN
        for raw in ['not-json', '{}', '[{"campaign_id":true}]']:
            with patch.dict(os.environ, {'THUNDER_OS_CADENCE_ACTIVITY_BINDINGS': raw}):
                result = CadenceReadonlyConsole(lambda: 'synthetic-token', request=ContactProvider(),
                    sleep=lambda _: None, campaign_registry=Registry(),
                    activity_bindings_loader=environment_activity_bindings).snapshot()
            group = next(row for row in result['campaigns'] if row['id'] == CAMPAIGN)
            self.assertEqual(group['metrics']['external_sent'], 1)
            self.assertEqual(group['activities'][0]['attribution'], 'contact_current_campaign')
            self.assertNotIn('external_emails:bindings_invalid', result['coverage']['limitations'])

    def test_contact_context_alone_never_attributes_activity(self):
        result = CadenceReadonlyConsole(lambda: 'synthetic-token', request=Provider(), sleep=lambda _: None).timeline('100')
        email = next(row for row in result['events'] if row['type'] == 'email')
        self.assertIsNone(email['campaign'])
        self.assertIsNone(email['receipt'])

    def test_instantly_pagination_or_schema_gap_keeps_total_unknown(self):
        from test_thunder_os_cadence_contact_campaign import ContactProvider, Registry, OTHER, contact
        for schema_missing in [True, False]:
            provider = ContactProvider()
            provider.contacts['100'] = contact(campaign=OTHER, executor='instantly')
            provider.email_pages = {'': {'results': provider.emails, 'paging': {'next': {'after': 'next'}}}}
            def request(method, path, body, headers, timeout):
                if path.endswith('/properties/emails') and schema_missing:
                    return 200, json.dumps({'results': []}).encode()
                return provider(method, path, body, headers, timeout)
            result = CadenceReadonlyConsole(lambda: 'synthetic-token', request=request, sleep=lambda _: None,
                                             campaign_registry=Registry(), max_pages=1).snapshot()
            group = next(row for row in result['campaigns'] if row['id'] == OTHER)
            self.assertIsNone(group['metrics']['external_sent'])
            self.assertEqual(group['metrics']['observed_external_sent'], 0 if schema_missing else 1)
            self.assertFalse(result['coverage']['complete'])

    def test_email_apollo_conta_como_enviado_not_delivery_receipt(self):
        from test_thunder_os_cadence_contact_campaign import ContactProvider, Registry, CAMPAIGN
        provider = ContactProvider()
        console = CadenceReadonlyConsole(lambda: 'synthetic-token', request=provider, sleep=lambda _: None,
                                         campaign_registry=Registry())
        group = next(row for row in console.snapshot()['campaigns'] if row['id'] == CAMPAIGN)
        self.assertEqual(group['metrics']['external_sent'], 1)
        self.assertEqual(group['activities'][0]['provider'], 'apollo')
        self.assertEqual(group['activities'][0]['provider_semantics'], 'registry_platform_not_origin')
        self.assertIsNone(group['activities'][0]['origin_provider'])
        self.assertIsNone(group['activities'][0]['receipt'])
        provider.contacts['100']['properties']['campanha_ativa'] = 'unknown'
        console._cache.clear()
        group = next(row for row in console.snapshot()['campaigns'] if row['id'] == CAMPAIGN)
        self.assertIsNone(group['metrics']['external_sent'])
        self.assertEqual(group['activities'], [])


if __name__ == '__main__':
    unittest.main()
