"""Synthetic F3 obligations. No credentials, provider clients or operational DB."""
import copy
import socket
import unittest
from datetime import datetime
from unittest.mock import patch

from thunder_os.cadencia_hermes_f3 import simulate, message_dates


def fixture():
    return {
        'synthetic': True, 'complete': True, 'owner_id': 'fixture-owner',
        'sequence_id': 'fixture-sequence', 'test_id': 'fixture-test',
        'quota': {'daily_remaining': 10, 'weekly_invites_remaining': 10},
        'contacts': [{
            'id': 'c1', 'owner_id': 'fixture-owner', 'sequence_id': 'fixture-sequence',
            'test_id': 'fixture-test', 'status': 'em_cadencia',
            'linkedin_url': 'https://www.linkedin.com/in/fixture-one',
            'start_date': '2026-10-01', 'accepted_at': None, 'relation_known': True,
            'history_complete': True, 'email_dates': [], 'linkedin_dates': [],
            'reply': None, 'suppressed': False, 'version': None,
            'texts': {'cad_li_0': 'SYNTHETIC invite', 'cad_li_1': 'SYNTHETIC one',
                      'cad_li_2': 'SYNTHETIC two', 'cad_li_3': 'SYNTHETIC three',
                      'cad_email_4b': 'SYNTHETIC email B'},
            'approved_texts': {'cad_li_0': 'SYNTHETIC invite', 'cad_li_1': 'SYNTHETIC one',
                               'cad_li_2': 'SYNTHETIC two', 'cad_li_3': 'SYNTHETIC three',
                               'cad_email_4b': 'SYNTHETIC email B'},
            'ledger': {}, 'posts': [], 'confirmed_messages': {},
        }],
        'tasks': [{'id': 't1', 'contact_id': 'c1', 'owner_id': 'fixture-owner',
                   'sequence_id': 'fixture-sequence', 'test_id': 'fixture-test',
                   'title': 'LinkedIn: convite', 'status': 'open',
                   'due_at': '2026-10-02T08:00:00-03:00'}],
        'provider_outcomes': {},
    }


class F3Tests(unittest.TestCase):
    def run_fixture(self, f=None, now='2026-10-02T10:00:00-03:00', **kwargs):
        return simulate(f or fixture(), datetime.fromisoformat(now), **kwargs)

    def actions(self, report, kind):
        return [a for a in report['intentions'] if a['kind'] == kind]

    def test_simulation_boundary(self):
        f = fixture()
        before = copy.deepcopy(f)
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
            r = self.run_fixture(f)
        self.assertEqual(f, before)
        self.assertEqual(r['mode'], 'MODO_SIMULACAO')
        self.assertEqual(r['external_effects'], 0)
        self.assertFalse(self.actions(r, 'would_complete_task'))
        with self.assertRaises(ValueError):
            self.run_fixture(mode=False)

    def test_overdue_and_scope(self):
        f = fixture()
        f['tasks'] += [{**f['tasks'][0], 'id': 'other', 'owner_id': 'other'},
                       {**f['tasks'][0], 'id': 'other-test', 'test_id': 'other'},
                       {**f['tasks'][0], 'id': 'future', 'due_at': '2026-11-01T08:00:00-03:00'}]
        actions = self.actions(self.run_fixture(f), 'would_send')
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['text'], 'SYNTHETIC invite')
        self.assertEqual(actions[0]['task_id'], 't1')
        self.assertTrue(30 <= actions[0]['delay_seconds'] <= 90)

    def test_acceptance_schedule(self):
        c = fixture()['contacts'][0]
        c['accepted_at'] = '2026-10-02T10:00:00-03:00'
        self.assertEqual(message_dates(c), ['2026-10-08', '2026-10-15', '2026-10-22'])
        c['accepted_at'] = '2026-10-09T10:00:00-03:00'
        c['email_dates'] = ['2026-10-12']
        self.assertEqual(message_dates(c), ['2026-10-13', '2026-10-20', '2026-10-27'])
        c['confirmed_messages'] = {'1': '2026-10-14'}
        self.assertEqual(message_dates(c)[1:], ['2026-10-21', '2026-10-28'])

    def test_d10_b(self):
        f = fixture()
        f['tasks'][0]['title'] = 'LinkedIn: msg 2'
        f['contacts'][0]['posts'] = [{'url': 'https://www.linkedin.com/feed/update/urn:li:activity:fixture', 'published_at': '2026-09-01T10:00:00-03:00'}]
        r = self.run_fixture(f, '2026-10-15T10:00:00-03:00')
        self.assertEqual(self.actions(r, 'would_update_contact')[0]['properties'],
                         {'cad_email_4_versao': 'B', 'cad_email_4': 'SYNTHETIC email B'})
        self.assertEqual(self.actions(r, 'would_send')[0]['action'], 'like')
        self.assertEqual(self.actions(r, 'would_cancel_step')[0]['step'], 3)
        f['contacts'][0]['posts'][0]['published_at'] = '2026-01-01T10:00:00-03:00'
        self.assertFalse(self.actions(self.run_fixture(f, '2026-10-15T10:00:00-03:00'), 'would_send'))

    def test_reply_channels(self):
        for channel in ('email', 'linkedin'):
            with self.subTest(channel=channel):
                f = fixture()
                f['contacts'][0]['reply'] = {'id': 'reply1', 'channel': channel, 'kind': 'human', 'text': 'SYNTHETIC reply'}
                r = self.run_fixture(f)
                self.assertFalse(self.actions(r, 'would_send'))
                self.assertEqual(self.actions(r, 'would_update_contact')[0]['properties'], {'cad_status': 'respondeu'})
                urgent = self.actions(r, 'would_create_urgent_task')[0]
                self.assertEqual(urgent['due_date'], '2026-10-02')
                self.assertEqual(urgent['reply_text'], 'SYNTHETIC reply')
                self.assertEqual(urgent['sequence_stop'], 'manual_required_unverified_api')
                self.assertEqual(self.actions(r, 'would_run_skill')[0]['skill'], 'acompanhar-respostas')

    def test_opt_out(self):
        f = fixture()
        f['contacts'][0]['reply'] = {'id': 'r1', 'channel': 'email', 'kind': 'opt_out', 'text': 'STOP'}
        r = self.run_fixture(f)
        self.assertEqual(self.actions(r, 'would_suppress')[0]['channels'], ['email', 'linkedin', 'whatsapp'])
        self.assertFalse(self.actions(r, 'would_send'))

    def test_provider_outcomes(self):
        for outcome in ('failed', 'uncertain', 'confirmed'):
            with self.subTest(outcome=outcome):
                f = fixture()
                f['provider_outcomes']['t1'] = {'status': outcome, 'receipt_id': 'fixture-receipt'}
                r = self.run_fixture(f)
                self.assertEqual(bool(self.actions(r, 'would_complete_task')), outcome == 'confirmed')
                self.assertEqual(bool(self.actions(r, 'would_mark_uncertain')), outcome == 'uncertain')
                self.assertEqual(self.actions(r, 'would_record_receipt')[0]['synthetic'], True)
        f['provider_outcomes']['t1'] = {'status': 'confirmed'}
        r = self.run_fixture(f)
        self.assertFalse(self.actions(r, 'would_complete_task'))
        self.assertTrue(self.actions(r, 'would_mark_uncertain'))

    def test_quota(self):
        for field in ('daily_remaining', 'weekly_invites_remaining'):
            f = fixture()
            f['quota'][field] = 0
            self.assertFalse(self.actions(self.run_fixture(f), 'would_send'))
        f = fixture()
        f['quota']['daily_remaining'] = 1
        c = copy.deepcopy(f['contacts'][0])
        c.update(id='c2', linkedin_url='https://www.linkedin.com/in/fixture-two')
        f['contacts'].append(c)
        f['tasks'].append({**f['tasks'][0], 'id': 't2', 'contact_id': 'c2'})
        self.assertEqual(len(self.actions(self.run_fixture(f), 'would_send')), 1)

    def test_business_window(self):
        for now in ('2026-10-03T10:00:00-03:00', '2026-10-02T07:59:00-03:00', '2026-10-02T19:00:00-03:00'):
            self.assertFalse(self.actions(self.run_fixture(now=now), 'would_send'))
        self.assertTrue(self.actions(self.run_fixture(now='2026-10-02T11:00:00+00:00'), 'would_send'))

    def test_ledger_and_retry(self):
        f = fixture()
        f['contacts'][0]['accepted_at'] = '2026-10-02T10:00:00-03:00'
        f['contacts'][0]['confirmed_messages'] = {'1': '2026-10-08'}
        f['tasks'][0]['title'] = 'LinkedIn: msg 1'
        self.assertFalse(self.actions(self.run_fixture(f, '2026-10-09T10:00:00-03:00'), 'would_send'))
        for state in ('reserved', 'sent', 'uncertain', 'failed', 'suppressed'):
            f = fixture()
            f['contacts'][0]['ledger']['invite'] = state
            r = self.run_fixture(f)
            self.assertFalse(self.actions(r, 'would_send'))
            self.assertEqual(r, self.run_fixture(f))

    def test_fail_closed(self):
        f = fixture()
        del f['contacts'][0]['reply']
        self.assertFalse(self.actions(self.run_fixture(f), 'would_send'))
        for field, value in [('history_complete', False), ('relation_known', False),
                             ('suppressed', True), ('status', 'incerto')]:
            f = fixture()
            f['contacts'][0][field] = value
            self.assertFalse(self.actions(self.run_fixture(f), 'would_send'))
        f = fixture()
        f['contacts'][0]['texts']['cad_li_0'] = 'edited without approval'
        self.assertFalse(self.actions(self.run_fixture(f), 'would_send'))
        f = fixture()
        f['complete'] = False
        self.assertFalse(self.actions(self.run_fixture(f), 'would_send'))
        f = fixture()
        del f['quota']
        self.assertFalse(self.actions(self.run_fixture(f), 'would_send'))

    def test_same_day(self):
        # The fixed email calendar blocks even if the observed history is empty.
        self.assertFalse(self.actions(self.run_fixture(now='2026-10-06T10:00:00-03:00'), 'would_send'))
        for field in ('email_dates', 'linkedin_dates'):
            f = fixture()
            f['contacts'][0][field] = ['2026-10-02']
            self.assertFalse(self.actions(self.run_fixture(f), 'would_send'))
        f = fixture()
        f['tasks'].append({**f['tasks'][0], 'id': 'duplicate-task'})
        self.assertEqual(len(self.actions(self.run_fixture(f), 'would_send')), 1)


if __name__ == '__main__':
    unittest.main()
