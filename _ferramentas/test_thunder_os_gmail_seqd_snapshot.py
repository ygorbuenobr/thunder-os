"""Offline checks for the opt-in Gmail snapshot bridge."""
import json
import unittest
from unittest.mock import Mock, patch

from thunder_os.gmail_seqd_snapshot import run_snapshot
from thunder_os.inbox_common import InboxError
from thunder_os.seqd_client import MAILBOX, SEQDClient, SEQDError


class SnapshotPortTests(unittest.TestCase):
    def test_disabled_and_missing_credential_do_not_call_network(self):
        service=Mock()
        request=Mock(side_effect=AssertionError('network forbidden'))
        client=SEQDClient(lambda: '',request=request)
        with self.assertRaisesRegex(InboxError,'gmail_snapshot_disabled'):
            run_snapshot(service,client=client)
        with self.assertRaisesRegex(InboxError,'gmail_read_failed'):
            run_snapshot(service,enabled=True,client=client)
        request.assert_not_called()

    def test_cursor_malformed_and_wrong_mailbox_fail_closed(self):
        request=Mock(return_value=(200,json.dumps({'mailbox':'wrong@example.invalid','status':'ready'}).encode()))
        client=SEQDClient(lambda:'a'*64,request=request)
        with self.assertRaisesRegex(SEQDError,'invalid_cursor'):
            client.snapshot_page('\x00')
        request.assert_not_called()
        with self.assertRaisesRegex(InboxError,'gmail_read_failed'):
            run_snapshot(Mock(),enabled=True,client=client)
        self.assertEqual(request.call_count,1)

    def test_snapshot_requires_explicit_inbox_coverage_and_valid_history(self):
        value={'mailbox':MAILBOX,'status':'ok','receipt_eligible':False,'messages':[],
            'nextPageToken':None,'coverage':{'scope':'inbox','window_days':7,'listing_complete':True}}
        request=Mock(side_effect=lambda *args:(200,json.dumps(value).encode()))
        client=SEQDClient(lambda:'a'*64,request=request)
        with self.assertRaisesRegex(SEQDError,'invalid_snapshot_page'):client.snapshot_page()
        value.clear();value.update(mailbox=MAILBOX,status='ok',receipt_eligible=False,
            thread={'id':'abc123','historyId':'not-a-number','messages':[]})
        with self.assertRaisesRegex(SEQDError,'invalid_snapshot_thread'):client.snapshot_thread('abc123')
