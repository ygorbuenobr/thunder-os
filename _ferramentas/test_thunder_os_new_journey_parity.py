"""No approved-copy claim without current Apollo content and export cohort."""
from copy import deepcopy
import csv
import io
from datetime import timedelta
import unittest

import test_thunder_os_new_journey as fixture
from thunder_os.new_journey import NewJourneyError


class JourneyParityTests(fixture.NewJourneyTests):
    def test_imported_identifiers_cannot_replace_confirmed_export_binding(self):
        self.source('email,apollo_contact_id,apollo_person_id,hubspot_id\n'
                    'ana@example.invalid,foreign-contact,foreign-person,foreign-crm\n')
        self.check(); self.steps(); self.approve()
        output = self.export()
        record = next(csv.DictReader(io.StringIO(output['csv_text'].lstrip('\ufeff'))))
        self.assertEqual(record['apollo_contact_id'], 'contact-1')
        self.assertEqual(record['apollo_person_id'], '')
        self.assertEqual(record['hubspot_id'], 'hubspot-1')
        snapshot = self.repo.get_state('journey_draft', self.row['id'])['export_snapshot']
        binding = snapshot['recipient_bindings'][self.row['rows'][0]['source_key']]
        self.assertEqual(binding['contact_id'], 'contact-1')
        self.assertEqual(binding['email'], 'ana@example.invalid')

    def test_changed_apollo_body_after_approval_blocks_export(self):
        self.ready(); self.approve()
        self.templates.items[0]['content'] = 'Different provider content'
        with self.assertRaisesRegex(NewJourneyError, 'sequence_changed'):
            self.export()
        self.assertIsNone(self.repo.get_state('journey_draft', self.row['id'])['export_snapshot'])

    def test_unavailable_dynamic_extra_or_mismatching_templates_never_approve(self):
        self.ready()
        snapshot = deepcopy(self.row['sequence']['template_snapshot'])
        for kind in ('unavailable', 'dynamic', 'extra', 'mismatch'):
            with self.subTest(kind=kind):
                value = deepcopy(snapshot)
                if kind == 'unavailable':
                    value['complete'] = False
                elif kind == 'dynamic':
                    value['items'][0]['flags']['static_safe'] = False
                elif kind == 'extra':
                    value['items'].append(deepcopy(value['items'][0]))
                else:
                    value['items'][0]['subject'] = 'Other subject'
                stored = self.repo.get_state('journey_draft', self.row['id'])
                stored['sequence']['template_snapshot'] = value
                self.repo.put_state('journey_draft', self.row['id'], stored)
                observed = self.journey.get(self.row['id'], 'ygor')
                self.assertFalse(observed['steps'][0]['can_approve'])
                self.assertFalse(observed['can_export'])
                self.assertNotEqual(observed['apollo_text_parity']['status'], 'verified')
        self.assert_no_execution()

    def test_export_cohort_is_exact_persisted_and_reconciles_entire_batch(self):
        self.source('email\n' + ''.join(f'person{i}@example.invalid\n' for i in range(30))
                    + 'not-an-address\n')
        self.check(); self.steps(); self.approve()
        first = self.export()
        stored = self.repo.get_state('journey_draft', self.row['id'])
        self.assertEqual(first['rows'], 30)
        self.assertEqual(len(stored['export_snapshot']['source_keys']), 30)
        seen = []

        def lookup(**kwargs):
            address = kwargs['query']; seen.append(address)
            return self.apollo.response([{'id': 'contact-fixture', 'email': address,
                'contact_campaign_statuses_complete': True,
                'contact_campaign_statuses': [{'emailer_campaign_id': 'sequence-2', 'status': 'active'}]}])

        self.apollo.contacts_search = lookup
        for index in range(3):
            self.row = self.journey.reconcile(self.row['id'], 'ygor', self.row['version'])
            self.assertEqual(self.row['reconciliation']['request_count'], 10)
            self.assertEqual(self.row['reconciliation']['pagination']['processed_count'], (index + 1) * 10)
        self.assertEqual(len(set(seen)), 30)
        self.assertTrue(self.row['reconciliation']['pagination']['complete'])
        self.assertTrue(all(r['status'] == 'enrolled' for r in self.row['reconciliation']['rows']))
        self.assert_no_execution()

    def test_reconciliation_without_export_has_no_provider_queries(self):
        self.ready()
        before = list(self.apollo.calls)
        self.row = self.journey.reconcile(self.row['id'], 'ygor', self.row['version'])
        self.assertEqual(self.apollo.calls, before)
        self.assertEqual(self.row['reconciliation']['request_count'], 0)
        self.assertEqual(self.row['reconciliation']['reason'], 'export_snapshot_missing')

    def test_original_24_hour_expiry_is_exposed_not_extended(self):
        self.ready(); self.approve()
        self.assertEqual(self.row['approval_validity_hours'], 24)
        self.now += timedelta(hours=25)
        self.assertFalse(self.journey.get(self.row['id'], 'ygor')['can_export'])


if __name__ == '__main__':
    unittest.main()
