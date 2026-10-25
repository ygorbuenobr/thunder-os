"""Approval inputs include the exact commercial source, without CRM writes."""
from copy import deepcopy
import unittest
import test_thunder_os_new_journey as support
from thunder_os.new_journey import NewJourneyError


class Projection:
    def __init__(self):
        self.digest = 'a' * 64
        self.unavailable = False

    def project_many(self, rows, contexts, sheet=None):
        if self.unavailable:
            raise OSError('source unavailable')
        return {row['source_key']: {'status': 'available',
            'source': {'path': 'fixtures/account.md', 'sha256': self.digest,
                       'updated_at': '2026-10-23T10:00:00Z'},
            'sections': [{'key': key, 'items': [{'text': 'Synthetic review input.'}]}
                         for key in ('business', 'fit', 'people', 'thesis')], 'originals': {'source_text': 'Original synthetic research.',
                                         'tabs': {'context': []}}} for row in rows}


class CommercialJourneyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.NewJourneyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.reader = Projection()
        self.fixture.journey.commercial_accounts = self.reader

    def test_source_hash_change_invalidates_approval_and_export(self):
        f = self.fixture
        f.ready(); f.approve()
        self.assertEqual(f.row['steps'][0]['approval_status'], 'approved')
        self.reader.digest = 'b' * 64
        current = f.journey.get(f.row['id'], 'ygor')
        self.assertEqual(current['steps'][0]['approval_status'], 'invalidated')
        self.assertFalse(current['can_export'])
        with self.assertRaises(NewJourneyError):
            f.journey.export(f.row['id'], 'ygor', current['version'], current['review_hash'])
        f.assert_no_execution()

    def test_read_projection_is_not_persisted_or_mutated(self):
        f = self.fixture
        f.ready()
        before = deepcopy(f.repo.get_state('journey_draft', f.row['id']))
        current = f.journey.get(f.row['id'], 'ygor')
        self.assertIn('csv:1', current['commercial_accounts'])
        self.assertEqual(before, f.repo.get_state('journey_draft', f.row['id']))
        self.assertNotIn('commercial_accounts', before)
        f.assert_no_execution()

    def test_source_failure_invalidates_previous_approval(self):
        f = self.fixture
        f.ready(); f.approve()
        self.reader.unavailable = True
        current = f.journey.get(f.row['id'], 'ygor')
        self.assertEqual(current['commercial_accounts']['csv:1']['status'], 'unavailable')
        self.assertEqual(current['steps'][0]['approval_status'], 'invalidated')
        self.assertFalse(current['can_export'])

    def test_collection_time_is_displayed_but_not_an_approval_change(self):
        f = self.fixture
        stamp = ['2026-10-23T00:00:00Z']
        def checking(rows, campaign):
            results = support.passing(rows, campaign)
            for result in results:
                for check in result['checks']:
                    check['checked_at'] = stamp[0]
            return results
        f.journey.checker = checking
        f.ready(); f.approve()
        stamp[0] = '2026-10-23T00:01:00Z'
        current = f.journey.get(f.row['id'], 'ygor')
        self.assertEqual(current['rows'][0]['checks'][0]['checked_at'], '2026-10-23T00:00:00Z')
        exported = f.journey.export_preview(f.row['id'], 'ygor', current['version'], current['review_hash'])
        self.assertIsInstance(exported, dict)
        f.assert_no_execution()

    def test_unavailable_research_cannot_approve_or_export(self):
        f = self.fixture
        f.ready()
        self.reader.unavailable = True
        current = f.journey.get(f.row['id'], 'ygor')
        self.assertIn('journey_commercial_research_pending', current['blockers'])
        self.assertFalse(current['steps'][0]['can_approve'])
        with self.assertRaises(NewJourneyError) as error:
            f.journey.approve_step(f.row['id'], 'E1', 'ygor', current['version'], current['review_hash'])
        self.assertEqual(error.exception.code, 'journey_commercial_research_pending')
        f.assert_no_execution()

    def test_source_changed_during_approval_is_not_silently_approved(self):
        from unittest.mock import patch
        f = self.fixture
        f.ready()
        previous = deepcopy(f.repo.get_state('journey_draft', f.row['id']))
        original = f.journey._revalidate
        def change_after_preflight(*args):
            original(*args)
            self.reader.digest = 'b' * 64
        with patch.object(f.journey, '_revalidate', change_after_preflight):
            with self.assertRaises(NewJourneyError):
                f.approve()
        self.assertEqual(previous, f.repo.get_state('journey_draft', f.row['id']))
        self.assertEqual(f.journey.get(f.row['id'], 'ygor')['approvals'], {})

    def test_prompt_source_changed_during_approval_is_not_silently_approved(self):
        from unittest.mock import patch
        import test_thunder_os_new_journey_prompt_mode as prompts
        f = prompts.PromptModeTests()
        f.setUp(); self.addCleanup(f.doCleanups)
        f.journey.commercial_accounts = self.reader
        f.prompts()
        previous = deepcopy(f.repo.get_state('journey_draft', f.row['id']))
        original = f.journey._revalidate
        def change_after_preflight(*args):
            original(*args)
            self.reader.digest = 'b' * 64
        with patch.object(f.journey, '_revalidate', change_after_preflight):
            with self.assertRaises(NewJourneyError):
                f.approve(f.row['steps'][0]['id'])
        self.assertEqual(previous, f.repo.get_state('journey_draft', f.row['id']))
