"""Draft-child read contract, no provider writes or production data."""
from copy import deepcopy
import unittest
from unittest.mock import Mock
import test_thunder_os_new_journey as support
from thunder_os.new_journey import NewJourneyError

class HistoryJourneyTests(unittest.TestCase):
    def setUp(self):
        self.f = support.NewJourneyTests()
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.source(); self.f.check()
        stored = self.f.repo.get_state('journey_draft', self.f.row['id'])
        stored['checks'][0]['qualification_context'] = {
            'contact_id': '123', 'company_id': '456', 'binding_hash': 'a'*64,
            'domain':'example.invalid','verified_linkedin_urls':['https://www.linkedin.com/in/ana']}
        self.f.row = self.f.journey._save(stored, stored['version'])
        self.reader = Mock()
        self.reader.read.return_value = {'read_only':True,'events':[{'text':'Original inbound'},
            {'text':'Latest original outbound'}], 'complete':True,'sources':[]}
        self.f.journey.history_reader = self.reader

    def test_history_reads_exact_bound_person_without_persisting(self):
        before = deepcopy(self.f.repo.get_state('journey_draft', self.f.row['id']))
        result = self.f.journey.history(self.f.row['id'], 'csv:1', 'ygor')
        self.assertEqual(result['events'][-1]['text'], 'Latest original outbound')
        self.assertEqual(result['external_writes'],0)
        self.reader.read.assert_called_once_with(before['rows'][0],before['checks'][0]['qualification_context'])
        self.assertEqual(before,self.f.repo.get_state('journey_draft',self.f.row['id']))
        self.f.assert_no_execution()

    def test_unknown_person_and_other_actor_never_reach_reader(self):
        for key,actor in [('csv:999','ygor'),('csv:1','codex')]:
            with self.assertRaises(NewJourneyError):
                self.f.journey.history(self.f.row['id'],key,actor)
        self.reader.read.assert_not_called()

    def test_missing_verified_binding_does_not_read_provider(self):
        row=self.f.repo.get_state('journey_draft', self.f.row['id'])
        row['checks'][0]['qualification_context']={'contact_id':'123'}
        self.f.journey._save(row,row['version'])
        with self.assertRaisesRegex(NewJourneyError,'identity_unverified'):
            self.f.journey.history(row['id'],'csv:1','ygor')
        self.reader.read.assert_not_called()
