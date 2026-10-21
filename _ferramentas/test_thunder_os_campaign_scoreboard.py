from copy import deepcopy
from contextlib import closing
from dataclasses import asdict
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.cadence_readonly import project_snapshot
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


if __name__ == '__main__':
    unittest.main()
