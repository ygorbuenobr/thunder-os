from copy import deepcopy
from contextlib import closing
from dataclasses import asdict
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.cadence_readonly import project_snapshot
from thunder_os.cadence_readonly import CadenceReadonlyError
from thunder_os.campaign_scoreboard import CampaignScoreboard
from thunder_os.db import Repository
from thunder_os.learning import LearningEngine, LearningError, LearningProposal


def reply(ident, campaign='A', classification='positive', **extra):
    return {'id': ident, 'campaign_id': campaign, 'identity_confirmed': True,
            'human_reply': True, 'classification': classification, **extra}


class Reader:
    def __init__(self, snapshot):
        self.value, self.calls = snapshot, 0

    def snapshot(self):
        self.calls += 1
        return self.value


class ReadOnlyRepo:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def list_state(self, kind):
        assert kind == 'reply'
        self.calls.append(kind)
        return self.rows

    def __getattr__(self, name):
        raise AssertionError('unexpected repository access: ' + name)


class CampaignScoreboardTests(unittest.TestCase):
    def test_placar_sem_dado_nao_vira_zero(self):
        snapshot = project_snapshot([], collected_at='2026-10-21T12:00:00Z',
                                    registered_campaigns=[{'id': 'A'}])
        reader = Reader(snapshot)
        board = CampaignScoreboard(reader, ReadOnlyRepo())
        result = board.snapshot()
        metrics = result['campaigns'][0]['metrics']
        for name in ('contacts', 'tasks', 'replies', 'meetings', 'external_sent',
                     'completed_with_receipt', 'completed_status', 'pending'):
            self.assertIsNone(metrics[name], name)
        self.assertEqual(metrics['observed_tasks'], 0)
        self.assertEqual(metrics, snapshot['campaigns'][0]['metrics'])
        self.assertTrue(result['read_only'])
        self.assertEqual(reader.calls, 1)
        reader.value = {'campaigns': [{'id': 'A'}, {'id': 'B', 'metrics': None}]}
        for campaign in board.snapshot()['campaigns']:
            self.assertTrue(all(value is None for value in campaign['metrics'].values()))
            self.assertIsNone(campaign['learning']['observation']['positive_rate'])

    def test_preserves_observed_scope_and_reads_current_snapshot(self):
        reader = Reader({'campaigns': [{'id': 'A', 'metrics': {
            'tasks': None, 'observed_tasks': 7, 'replies': None,
            'observed_replies': 2, 'pending': 0, 'custom_metric': None}}]})
        board = CampaignScoreboard(reader, ReadOnlyRepo())
        before = deepcopy(reader.value)
        result = board.snapshot()['campaigns'][0]
        self.assertEqual(result['metrics']['observed_tasks'], 7)
        self.assertEqual(result['metrics']['observed_replies'], 2)
        self.assertEqual(result['metrics']['pending'], 0)
        result['metrics']['observed_tasks'] = 999
        self.assertEqual(reader.value, before)
        reader.value = {'campaigns': [{'id': 'B', 'metrics': {'tasks': 3}}]}
        self.assertEqual(board.snapshot()['campaigns'][0]['id'], 'B')
        self.assertEqual(reader.calls, 2)

    def test_default_minimum_sample_and_existing_lift(self):
        board = CampaignScoreboard(Reader({}), ReadOnlyRepo())
        rows = [reply(str(i)) for i in range(29)]
        self.assertEqual(board.analyze(rows)['observation']['reason'], 'insufficient_sample')
        rows.append(reply('29'))
        result = board.analyze(rows)
        self.assertEqual(result['proposal']['evidence'], {'positive': 30, 'sample': 30, 'rate': 1.0})
        conflicting = [reply(str(i), classification='positive' if i < 18 else 'negative')
                       for i in range(30)]
        self.assertEqual(board.analyze(conflicting)['observation']['reason'], 'conflicting_evidence')
        self.assertNotIn('proposal', board.analyze(conflicting))

    def test_aprendizado_so_com_aprovacao(self):
        board = CampaignScoreboard(Reader({}), ReadOnlyRepo())
        learning = board.analyze([reply(str(i)) for i in range(30)])
        self.assertEqual(learning['status'], 'proposal_pending_human_approval')
        self.assertIs(learning['requires_human_approval'], True)
        proposal = LearningProposal(**learning['proposal'])
        self.assertEqual(asdict(proposal), learning['proposal'])
        engine = LearningEngine(minimum_sample=30, minimum_lift=0.2)
        for actor, approved in [('agent', False), ('agent', True), ('ygor', False)]:
            with self.assertRaisesRegex(LearningError, 'human promotion required'):
                engine.promotion_command(proposal, actor, approved=approved)
        self.assertEqual(engine.promotion_command(proposal, 'ygor', approved=True)['status'],
                         'human_approved')

    def test_auto_unknown_unconfirmed_and_truthy_flags_are_excluded(self):
        valid = reply('valid', classification='negative')
        rows = [valid, reply('auto', human_reply=False), reply('unknown', classification='unknown'),
                reply('missing', classification=None), reply('unverified', identity_confirmed=False),
                reply('truthy-human', human_reply=1), reply('truthy-identity', identity_confirmed=1),
                reply('inbound', classification=None, direction='INCOMING_EMAIL', content='yes'),
                reply('outcome', classification=None, outcome='positive'),
                reply('', content='yes'), None, {}]
        board = CampaignScoreboard(Reader({}), ReadOnlyRepo(), minimum_sample=1)
        result = board.analyze(rows, campaign_id='A')
        self.assertEqual(result['observation']['sample'], 1)
        self.assertEqual(result['source_ids'], ['valid'])
        self.assertEqual(result['proposal']['evidence']['positive'], 0)

    def test_campaign_isolation_and_exact_id(self):
        rows = [reply(str(i), campaign='A' if i < 15 else 'A-extra') for i in range(30)]
        rows += [reply('unlinked', campaign=None)]
        board = CampaignScoreboard(Reader({'campaigns': [{'id': 'A'}, {'id': 'A-extra'}]}),
                                   ReadOnlyRepo(rows))
        results = board.snapshot()['campaigns']
        self.assertEqual([row['learning']['observation']['sample'] for row in results], [15, 15])
        self.assertTrue(all('proposal' not in row['learning'] for row in results))
        with self.assertRaisesRegex(ValueError, 'campaign_id_required'):
            board.analyze(rows)

    def test_source_deduplication_and_conflict_exclusion(self):
        rows = [reply('one', source_id='same'), reply('two', source_id='same'),
                reply('bad', classification='negative', source_id='same'), reply('valid')]
        board = CampaignScoreboard(Reader({}), ReadOnlyRepo(), minimum_sample=1)
        result = board.analyze(rows)
        self.assertEqual(result['source_ids'], ['valid'])
        self.assertEqual(result['observation']['sample'], 1)
        result = board.analyze([reply(str(i), source_id='same') for i in range(30)])
        self.assertEqual(result['source_ids'], ['same'])
        self.assertEqual(result['observation']['sample'], 1)

    def test_same_provider_message_is_one_source(self):
        source = {'provider': 'manual', 'account_id': 'test', 'provider_message_id': 'message'}
        board = CampaignScoreboard(Reader({}), ReadOnlyRepo(), minimum_sample=1)
        result = board.analyze([reply('one', source=source), reply('two', source=source)])
        self.assertEqual(result['observation']['sample'], 1)
        self.assertEqual(len(result['source_ids']), 1)

    def test_rate_denominator_is_classified_human_replies_not_sent(self):
        rows = [reply(str(i), classification='negative' if i == 29 else 'positive') for i in range(30)]
        board = CampaignScoreboard(Reader({'campaigns': [{'id': 'A', 'metrics': {
            'external_sent': 1000, 'replies': None}}]}), ReadOnlyRepo(rows))
        campaign = board.snapshot()['campaigns'][0]
        result = campaign['learning']
        self.assertEqual(result['observation']['metric'], 'positive_rate_among_classified_human_replies')
        self.assertEqual(result['observation']['positive_rate'], 29 / 30)
        self.assertIn('not_market_benchmark', result['observation']['comparison_scope'])
        self.assertIn('causalidade', result['proposal']['limitation'])
        self.assertIsNone(campaign['metrics']['replies'])

    def test_no_provider_repository_write_or_promotion(self):
        repo = ReadOnlyRepo([reply(str(i)) for i in range(30)])
        reader = Reader({'campaigns': [{'id': 'A'}]})
        before = deepcopy(repo.rows)
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch.object(LearningEngine, 'promotion_command', side_effect=AssertionError('promotion forbidden')), \
                patch.object(LearningEngine, 'start_experiment', side_effect=AssertionError('experiment forbidden')):
            self.assertIn('proposal', CampaignScoreboard(reader, repo).snapshot()['campaigns'][0]['learning'])
        self.assertEqual(repo.calls, ['reply'])
        self.assertEqual(repo.rows, before)

    def test_isolated_repository_sql_is_select_only(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(directory + '/test.db')
            repo.append_state('reply', 'one', reply('one'))
            with closing(sqlite3.connect(repo.path)) as connection:
                connection.row_factory = sqlite3.Row
                connection.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK
                                          if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ}
                                          else sqlite3.SQLITE_DENY)
                repo._local.connection = connection
                try:
                    result = CampaignScoreboard(Reader({'campaigns': [{'id': 'A'}]}), repo).snapshot()
                    self.assertEqual(result['campaigns'][0]['learning']['observation']['sample'], 1)
                    self.assertEqual(connection.total_changes, 0)
                finally:
                    repo._local.connection = None


class ManagementScoreboardTests(unittest.TestCase):
    """R7/R8 delegated proof checklist (only these two files are owned).

    C1 receipt/person dedupe: test_unique_people_with_bound_receipts_not_done_tasks
    C2 human/positive dedupe: test_human_replies_deduplicate_person_and_message
    C3 null/zero: test_zero_requires_complete_scoped_proof
    C4 partial 500: test_partial_500_keeps_observations_not_a_total
    C5 cohorts: test_cross_cohort_comparison_never_ranks_a_path
    C6 unavailable outcomes: test_outcomes_and_costs_require_sources
    C7 provenance/no effects: test_provenance_is_deterministic_detached_and_read_only
    C8 paths/batches: test_paths_and_batches_do_not_borrow_other_evidence

    Validation, retry/failure, idempotency, ordering and lifecycle are projection
    concerns covered below. Server authentication is unchanged. There are no
    mutations, provider calls, background work, new schemas or shared test ports.
    Independent verification belongs to the principal, over the final diff.
    """
    stamp = '2026-10-25T12:00:00Z'
    period = {'start': '2026-10-01T00:00:00Z', 'end': '2026-11-01T00:00:00Z'}

    def snapshot(self, **extra):
        return {'collected_at': self.stamp,
                'coverage': {'complete': True, 'partial': False, 'sources': {}, 'limitations': []},
                'campaigns': [{'id': 'A', 'registry': {'id': 'A', 'platform': 'thunder_os'},
                               'tasks': [], **extra}]}

    def task(self, ident, person='1', message=None, **extra):
        return {'id': ident, 'campaign': 'A', 'contact_ids': [person], 'link_status': 'linked',
                'completed': True, 'status': 'COMPLETED', 'outcome': 'sent_with_receipt',
                'receipt': {'provider': 'gmail', 'message_id': message or ident, 'contact_id': person,
                            'confirmed_at': self.stamp, 'verification': 'recorded_confirmation',
                            'source': 'hubspot_task_body'}, **extra}

    def human(self, ident, person='1', **extra):
        return reply(ident, **{'hubspot_contact_id': person, 'received_at': self.stamp,
                     'source': {'provider': 'manual', 'account_id': 'ygor', 'provider_message_id': ident}, **extra})

    def board(self, snapshot=None, replies=()):
        return CampaignScoreboard(Reader(snapshot if snapshot is not None else self.snapshot()), ReadOnlyRepo(replies))

    def management(self, snapshot=None, replies=()):
        return self.board(snapshot, replies).snapshot()['campaigns'][0]['management']

    def proof(self, **extra):
        return {'complete': True, 'source': 'synthetic_exhaustive_receipt_read',
                'campaign_id': 'A', 'period': self.period, 'population': 'test_population', **extra}

    def test_unique_people_with_bound_receipts_not_done_tasks(self):
        tasks = [self.task('E1'), self.task('E2'), self.task('E2-copy', message='E2'),
                 self.task('other', '2'), self.task('done', '3', receipt=None),
                 self.task('pending', '4', completed=False, status='NOT_STARTED', receipt=None)]
        reached = self.management(self.snapshot(tasks=tasks))['metrics']['reached']
        self.assertEqual(reached['value'], 2)
        self.assertEqual(len(reached['source_ids']), 3)
        self.assertFalse(reached['complete'])
        self.assertEqual(reached['reason'], 'observed_evidence_only')
        self.assertEqual(reached['source'], ['hubspot_task_body'])
        self.assertEqual(reached['person_ids'], ['1', '2'])

    def test_invalid_receipt_identity_campaign_or_status_never_reaches(self):
        good = self.task('valid')
        invalid = [self.task('unlinked', link_status='unverified'),
                   self.task('wrong-person', contact_ids=['99']),
                   self.task('many-people', contact_ids=['1', '2']),
                   self.task('wrong-campaign', campaign='A-extra'),
                   self.task('uncertain', outcome='uncertain_no_retry'),
                   self.task('crm-status', outcome='completed_without_receipt'),
                   self.task('foreign-receipt', receipt={**good['receipt'], 'campaign_id': 'B'}),
                   self.task('no-verification', receipt={**good['receipt'], 'verification': None}),
                   self.task('no-message', receipt={**good['receipt'], 'message_id': None})]
        for task in invalid:
            with self.subTest(task=task['id']):
                result = self.management(self.snapshot(tasks=[task]))
                self.assertIsNone(result['metrics']['reached']['value'])
                self.assertTrue(any(row['code'] == 'uncertain_evidence' for row in result['insights']))

    def test_same_receipt_for_different_people_is_a_conflict(self):
        tasks = [self.task('copy1', '1', message='same'), self.task('copy2', '2', message='same')]
        result = self.management(self.snapshot(tasks=tasks))
        self.assertIsNone(result['metrics']['reached']['value'])
        self.assertIn('receipt_identity_conflict', result['coverage']['limitations'])

    def test_human_replies_deduplicate_person_and_message(self):
        one = self.human('first')
        replies = [one, {**one, 'id': 'copy'}, self.human('second'),
                   self.human('other', '2', classification='negative'),
                   self.human('auto', '3', human_reply=False),
                   self.human('unverified', '4', identity_confirmed=False),
                   self.human('truthy', '5', human_reply=1),
                   self.human('foreign', '6', campaign_id='A-extra')]
        # Exact campaign_id field, rather than the helper's campaign argument.
        replies[-1]['campaign_id'] = 'A-extra'
        result = self.management(replies=replies)
        self.assertEqual(result['metrics']['responded']['value'], 2)
        self.assertEqual(result['metrics']['positive']['value'], 1)
        self.assertEqual(len(result['metrics']['responded']['source_ids']), 3)
        self.assertEqual(result['metrics']['responded']['source'], ['repository.reply'])

    def test_conflicting_reply_is_one_human_and_no_positive_vote(self):
        one = self.human('same')
        result = self.management(replies=[one, {**one, 'id': 'copy', 'classification': 'negative'}])
        self.assertEqual(result['metrics']['responded']['value'], 1)
        self.assertIsNone(result['metrics']['positive']['value'])
        self.assertIn('replies_awaiting_review', [row['code'] for row in result['insights']])
        conflict = self.management(replies=[one, {**one, 'hubspot_contact_id': '2'}])
        self.assertIsNone(conflict['metrics']['responded']['value'])

    def test_unknown_human_classification_still_counts_reply_and_requests_review(self):
        result = self.management(replies=[self.human('unknown', classification=None)])
        self.assertEqual(result['metrics']['responded']['value'], 1)
        self.assertIsNone(result['metrics']['positive']['value'])
        insight = next(row for row in result['insights'] if row['code'] == 'replies_awaiting_review')
        self.assertTrue(insight['source_ids'])
        self.assertTrue(insight['requires_human_approval'])
        self.assertTrue(insight['action'])

    def test_zero_requires_complete_scoped_proof(self):
        snapshot = self.snapshot(period=self.period)
        metric = self.management(snapshot)['metrics']['reached']
        self.assertIsNone(metric['value'])
        # A complete task read is still not an exhaustive receipt read.
        snapshot['campaigns'][0]['task_coverage_complete'] = True
        snapshot['coverage']['sources']['tasks'] = {'complete': True}
        self.assertIsNone(self.management(snapshot)['metrics']['reached']['value'])
        snapshot['coverage']['sources']['receipts'] = self.proof()
        metric = self.management(snapshot)['metrics']['reached']
        self.assertEqual(metric['value'], 0)
        self.assertTrue(metric['complete'])
        self.assertIsNone(metric['reason'])
        self.assertIn('synthetic_exhaustive_receipt_read', metric['source'])
        for field in ('source', 'campaign_id', 'period', 'population'):
            changed = deepcopy(snapshot)
            changed['coverage']['sources']['receipts'].pop(field)
            self.assertIsNone(self.management(changed)['metrics']['reached']['value'], field)
        snapshot['coverage']['sources']['receipts']['campaign_id'] = 'B'
        self.assertIsNone(self.management(snapshot)['metrics']['reached']['value'])

    def test_partial_500_keeps_observations_not_a_total(self):
        snapshot = self.snapshot(tasks=[self.task('one')], period=self.period)
        snapshot['coverage'].update(complete=False, partial=True,
            sources={'external_emails': {'complete': False, 'http_status': 500}, 'receipts': self.proof()},
            limitations=['external_emails:provider_http_500'])
        result = self.management(snapshot, [self.human('reply')])
        for key in ('reached', 'responded', 'positive'):
            self.assertIsNone(result['metrics'][key]['value'])
            self.assertEqual(result['metrics'][key]['observed_value'], 1)
            self.assertEqual(result['metrics'][key]['reason'], 'source_read_incomplete')
            self.assertFalse(result['metrics'][key]['complete'])
        self.assertEqual(result['coverage']['sources']['external_emails']['http_status'], 500)

    def test_reader_failure_preserves_local_reply_campaign(self):
        board = self.board(replies=[self.human('reply')])
        with patch.object(board.reader, 'snapshot', side_effect=CadenceReadonlyError('provider_http_error', 500)):
            result = board.snapshot()
        self.assertEqual(result['campaigns'][0]['id'], 'A')
        self.assertIsNone(result['campaigns'][0]['management']['metrics']['responded']['value'])
        self.assertEqual(result['campaigns'][0]['management']['metrics']['responded']['observed_value'], 1)

    def test_reply_store_failure_keeps_reader_campaigns(self):
        board = self.board(self.snapshot(tasks=[self.task('one')]))
        with patch.object(board.repo, 'list_state', side_effect=sqlite3.OperationalError('private detail')):
            result = board.snapshot()
        management = result['campaigns'][0]['management']
        self.assertIsNone(management['metrics']['responded']['value'])
        self.assertIn('local_reply_read_unavailable', management['coverage']['limitations'])
        self.assertNotIn('private detail', str(result))

    def test_outcomes_and_costs_require_sources(self):
        snapshot = self.snapshot(metrics={'meetings': 10, 'won': 5, 'cost_per_hot': 0},
            tasks=[{'id': 'scheduled-meeting', 'status': 'COMPLETED', 'type': 'meeting'}],
            activities=[{'type': 'meeting', 'status': 'COMPLETED', 'campaign': 'A',
                         'attribution': 'contact_current_campaign'}])
        result = self.management(snapshot, [self.human('agenda', classification='positive')])
        for key in ('meetings_scheduled', 'meetings_held', 'opportunities', 'won', 'cost_per_hot'):
            metric = result['metrics'][key]
            self.assertIsNone(metric['value'], key)
            self.assertFalse(metric['complete'], key)
            self.assertTrue(metric['reason'], key)
            self.assertEqual(metric['source'], [])

    def test_cross_cohort_comparison_never_ranks_a_path(self):
        snapshot = self.snapshot(period=self.period, offer='Offer A', segment='Textile',
                                 population={'id': 'group-A', 'size': 100})
        snapshot['campaigns'] += [
            {'id': 'B', 'registry': {'platform': 'apollo'}, 'offer': 'Offer B', 'segment': 'Food',
             'period': {'start': '2025-01-01T00:00:00Z', 'end': '2025-02-01T00:00:00Z'},
             'population': {'id': 'group-B', 'size': 5}},
            {'id': 'C', 'registry': {'platform': 'instantly'}}]
        result = self.board(snapshot).snapshot()
        self.assertEqual([row['cadence_path'] for row in result['comparisons']], ['thunder', 'apollo', 'instantly'])
        for comparison in result['comparisons']:
            self.assertIsNone(comparison['winner'])
            self.assertIsNone(comparison['ranking'])
            self.assertFalse(comparison['comparable'])
            self.assertTrue(comparison['reason'])
            for key in ('offer', 'segment', 'period', 'population', 'source'):
                self.assertIn(key, comparison)
        self.assertEqual(result['comparisons'][0]['population']['size'], 100)
        self.assertIsNone(result['comparisons'][2]['population'])
        self.assertIsNone(result['comparisons'][2]['offer'])
        self.assertEqual(result['comparisons'][2]['metadata_reasons']['offer'], 'offer_unavailable')

    def test_codex_local_registry_and_explicit_thunder_share_one_path(self):
        snapshot = self.snapshot(tasks=[self.task('one', '1', batch_id='batch-1'),
            self.task('two', '2', cadence_path='thunder', batch_id='batch-1')],
            registry={'id': 'A', 'platform': 'codex_local'})
        result = self.board(snapshot).snapshot()
        campaign = result['campaigns'][0]
        self.assertEqual(campaign['platform'], 'codex_local')
        self.assertEqual(campaign.get('cadence_path'), 'thunder')
        self.assertEqual(len(campaign['paths']), 1)
        path = campaign['paths'][0]
        self.assertEqual((path['cadence_path'], path['batch_id']), ('thunder', 'batch-1'))
        self.assertEqual(path['management']['metrics']['reached']['value'], 2)
        self.assertEqual(result['comparisons'][0]['cadence_path'], 'thunder')

    def test_paths_and_batches_do_not_borrow_other_evidence(self):
        tasks = [self.task('thunder', batch_id='batch-1'),
                 self.task('apollo', '2', cadence_path='apollo', batch_id='batch-2')]
        snapshot = self.snapshot(tasks=tasks)
        campaign = self.board(snapshot, [self.human('instant', '3',
                                   source={'provider': 'instantly', 'account_id': 'test', 'provider_message_id': 'reply'})]).snapshot()['campaigns'][0]
        paths = {(row['cadence_path'], row['batch_id']): row['management']['metrics'] for row in campaign['paths']}
        self.assertEqual(paths[('thunder', 'batch-1')]['reached']['value'], 1)
        self.assertEqual(paths[('apollo', 'batch-2')]['reached']['value'], 1)
        self.assertIsNone(paths[('instantly', None)]['reached']['value'])
        self.assertEqual(paths[('instantly', None)]['responded']['value'], 1)

    def test_a_receipt_with_conflicting_paths_cannot_fill_both_groups(self):
        snapshot = self.snapshot(tasks=[self.task('one', message='same'),
                                 self.task('two', message='same', cadence_path='apollo')],
                                 population={'id': 'whole-campaign', 'size': 50})
        result = self.board(snapshot).snapshot()
        for path in result['campaigns'][0]['paths']:
            self.assertIsNone(path['management']['metrics']['reached']['value'])
            self.assertIn('receipt_identity_or_scope_conflict', path['management']['coverage']['limitations'])
        for comparison in result['comparisons']:
            self.assertIsNone(comparison['population'])

    def test_period_filters_receipts_and_replies_without_inventing_dates(self):
        old = self.task('old', '2')
        old['receipt']['confirmed_at'] = '2025-10-01T00:00:00Z'
        missing = self.task('undated', '3')
        missing['receipt']['confirmed_at'] = None
        snapshot = self.snapshot(tasks=[self.task('now'), old, missing], period=self.period)
        result = self.management(snapshot, [self.human('now'),
                     self.human('old', '2', received_at='2025-01-01T00:00:00Z')])
        self.assertEqual(result['metrics']['reached']['value'], 1)
        self.assertEqual(result['metrics']['responded']['value'], 1)
        self.assertIn('evidence_time_unavailable', result['coverage']['limitations'])
        self.assertEqual(result['period'], self.period)
        unknown = self.management(self.snapshot())
        self.assertEqual(unknown['period'], {'start': None, 'end': None})
        self.assertEqual(unknown['period_reason'], 'period_unavailable')
        self.assertEqual(unknown['observed_at'], self.stamp)
        self.assertIsNone(unknown['source_observed_at']['repository.reply'])
        self.assertTrue(unknown['observed_at_reason'])

    def test_provenance_is_deterministic_detached_and_read_only(self):
        snapshot = self.snapshot(tasks=[self.task('one')], period=self.period)
        replies = [self.human('pending', classification=None)]
        before = deepcopy((snapshot, replies))
        board = self.board(snapshot, replies)
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch.object(LearningEngine, 'promotion_command', side_effect=AssertionError('promotion forbidden')), \
                patch.object(LearningEngine, 'start_experiment', side_effect=AssertionError('experiment forbidden')):
            one, two = board.snapshot(), board.snapshot()
        self.assertEqual(one, two)
        self.assertEqual((snapshot, replies), before)
        self.assertEqual(board.repo.calls, ['reply', 'reply'])
        self.assertTrue(one['requires_human_approval'])
        self.assertTrue(one['read_only'])
        management = one['campaigns'][0]['management']
        self.assertTrue(management['metrics']['reached']['source_ids'])
        self.assertEqual(management['source'], ['reader.snapshot', 'repository.reply'])
        for insight in management['insights']:
            self.assertTrue(insight['requires_human_approval'])
            self.assertTrue(insight['source_ids'])
            self.assertTrue(insight['action'])
        management['period']['start'] = 'mutated'
        management['coverage']['sources']['mutated'] = True
        self.assertEqual((snapshot, replies), before)


if __name__ == '__main__':
    unittest.main()
