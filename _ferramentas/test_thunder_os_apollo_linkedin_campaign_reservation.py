"""Campaign approval and reservation integration on disposable SQLite only."""
from copy import deepcopy
from hashlib import sha256
import json
from multiprocessing import get_context
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.approvals import ApprovalError
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.apollo_linkedin_identity_binding import KIND, binding_key
from thunder_os.service import ThunderService
from test_thunder_os_apollo_linkedin_authority import config


def task():
    return {'id': 'task1', 'type': 'linkedin_step_message', 'status': 'scheduled',
            'emailer_campaign_id': 'seq', 'user_id': 'owner', 'contact_id': 'contact1',
            'contact': {'id': 'contact1', 'linkedin_url': 'https://www.linkedin.com/in/person'},
            'linkedin_emailer_template': {'body_text': 'Ola'}}


def contend(path, approval, queue, start):
    with patch('thunder_os.db.OFFICIAL_PANEL_DB', Path(path)):
        repo = Repository(path)
        start.wait()
        try:
            repo.reserve_apollo_linkedin_for_campaign('campaign', approval, 'v1', task(),
                observed_team_id='team', expected_text_hash=sha256(b'Ola').hexdigest(),
                binding_id=binding_key('company', 'campaign', 'task1', 'a' * 24, approval))
            queue.put('reserved')
        except RepositoryConflict:
            queue.put('duplicate')


class CampaignReservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'official.sqlite'; self.path.touch()
        self.env = patch.dict('os.environ', {'THUNDER_APOLLO_LI_OFFICIAL_DB': str(self.path)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.repo = Repository(self.path)
        self.service = ThunderService(self.repo)
        self.service.configure_campaign(config(), 'ygor', expected_version=0)
        self.repo.put_state('control', 'global', {'paused': False, 'actor': 'ygor'})
        review = self.service.apollo_campaign_approval_review('campaign')
        self.approval = self.service.approve_apollo_campaign_version('campaign', 'ygor',
            execution_confirmation=review['execution_confirmation'])['id']
        self.digest = sha256(b'Ola').hexdigest()
        self.identity_id = binding_key('company', 'campaign', 'task1', 'a' * 24, self.approval)
        approval = self.repo.get_state('approval', self.approval)
        self.identity = {'company': 'company', 'campaign_id': 'campaign', 'approval_id': self.approval,
            'campaign_version': 'v1', 'scope_hash': approval['scope_hash'], 'team_id': 'team',
            'sequence_id': 'seq', 'task_id': 'task1', 'action_type': 'linkedin_step_message',
            'contact_id': 'contact1', 'account_id': 'a' * 24,
            'recipient_public_id': 'person', 'member_urn': 'urn:li:fsd_profile:member'}
        self.repo.append_state(KIND, self.identity_id, self.identity)

    def reserve(self, observed=None, approval=None, team='team', digest=None):
        return self.repo.reserve_apollo_linkedin_for_campaign('campaign', approval or self.approval,
            'v1', observed or task(), observed_team_id=team, expected_text_hash=digest or self.digest,
            binding_id=self.identity_id)

    def test_approved_campaign_reserves_once_and_records_audit(self):
        result = self.reserve()
        self.assertEqual((result['company'], result['account_id'], result['state']), ('company', 'a' * 24, 'reserved'))
        with self.assertRaises(RepositoryConflict): self.reserve()
        with self.repo._connect() as con:
            events = con.execute("SELECT payload FROM thunder_events WHERE type='apollo_linkedin_campaign_reservation'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn(self.approval, events[0]['payload'])
        self.assertIn(self.identity_id, events[0]['payload'])
        self.assertIn('urn:li:fsd_profile:member', events[0]['payload'])

    def test_missing_approval_and_all_binding_changes_fail_before_reservation(self):
        with self.assertRaises(ApprovalError): self.reserve(approval='missing')
        cases = [('emailer_campaign_id', 'other'), ('user_id', 'other'), ('contact_id', 'other'),
                 ('status', 'completed'), ('type', 'unsupported')]
        for key, value in cases:
            modified = deepcopy(task()); modified[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.reserve(modified)
        with self.assertRaises(ValueError): self.reserve(team='other')
        with self.assertRaises(ValueError): self.reserve(digest='a' * 64)
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])

    def test_revoked_or_changed_approval_and_paused_control_fail(self):
        self.repo.put_state('control', 'global', {'paused': True, 'actor': 'ygor'})
        with self.assertRaises(ApprovalError): self.reserve()
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])

    def test_expired_or_revoked_approval_never_reserves(self):
        from datetime import datetime, timedelta, timezone
        class Future(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(timezone.utc) + timedelta(days=15)
        with patch('thunder_os.apollo_linkedin_authority.datetime', Future):
            with self.assertRaises(ApprovalError): self.reserve()
        approval = self.repo.get_state('approval', self.approval)
        approval['status'] = 'revoked'
        self.repo.put_state('approval', self.approval, approval)
        with self.assertRaises(ApprovalError): self.reserve()
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])

    def test_invitation_is_supported_but_protected_fixture_is_never_reserved(self):
        invite = task(); invite['type'] = 'linkedin_step_connect'
        self.identity['action_type'] = 'linkedin_step_connect'
        with self.repo.transaction() as con:
            con.execute('UPDATE thunder_state SET payload=? WHERE kind=? AND entity_id=?',
                        (json.dumps(self.identity), KIND, self.identity_id))
        self.assertEqual(self.reserve(invite)['action_type'], 'linkedin_step_connect')
        from thunder_os.apollo_linkedin_attention import EXCLUDED_TASK
        protected = task(); protected['id'] = EXCLUDED_TASK
        with self.assertRaises(ValueError): self.reserve(protected)

    def test_changed_identity_binding_never_reserves(self):
        for key, value in [('member_urn', 'invalid'), ('contact_id', 'other'),
                           ('action_type', 'linkedin_step_connect'),
                           ('account_id', 'b' * 24), ('recipient_public_id', 'other'),
                           ('scope_hash', 'different'), ('approval_id', 'different')]:
            with self.subTest(field=key):
                with self.repo.transaction() as con:
                    con.execute('UPDATE thunder_state SET payload=? WHERE kind=? AND entity_id=?',
                                (json.dumps({**self.identity, key: value}), KIND, self.identity_id))
                with self.assertRaises(ValueError): self.reserve()
                with self.repo.transaction() as con:
                    con.execute('UPDATE thunder_state SET payload=? WHERE kind=? AND entity_id=?',
                                (json.dumps(self.identity), KIND, self.identity_id))
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])

    def test_missing_or_wrong_binding_id_never_reserves(self):
        for binding_id in ('missing', None):
            with self.subTest(binding_id=binding_id), self.assertRaises(ValueError):
                self.repo.reserve_apollo_linkedin_for_campaign('campaign', self.approval, 'v1', task(),
                    observed_team_id='team', expected_text_hash=self.digest, binding_id=binding_id)
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])

    def test_modified_campaign_version_and_unresolved_text_are_rejected(self):
        updated = deepcopy(config()); updated['campaign_version'] = 'v2'
        self.service.configure_campaign(updated, 'ygor', expected_version=1)
        with self.assertRaises(ApprovalError): self.reserve()
        with self.assertRaises(ValueError):
            modified = task(); modified['linkedin_emailer_template']['body_text'] = 'Oi {{name}}'
            self.reserve(modified)
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])

    def test_audit_failure_rolls_back_even_if_outer_transaction_catches(self):
        with self.repo.transaction():
            with patch.object(self.repo, 'append_event', side_effect=RuntimeError('failure')):
                with self.assertRaises(RuntimeError): self.reserve()
        self.assertEqual(self.repo.list_apollo_linkedin_reservations(), [])

    def test_two_processes_cannot_reserve_same_task(self):
        ctx = get_context('spawn'); queue = ctx.Queue(); start = ctx.Event()
        procs = [ctx.Process(target=contend, args=(str(self.path), self.approval, queue, start)) for _ in range(2)]
        for proc in procs: proc.start()
        start.set()
        result = sorted(queue.get(timeout=15) for _ in procs)
        for proc in procs:
            proc.join(15); self.assertEqual(proc.exitcode, 0)
        self.assertEqual(result, ['duplicate', 'reserved'])


if __name__ == '__main__': unittest.main()
