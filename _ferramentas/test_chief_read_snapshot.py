import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import chief_read_snapshot as m


class Fake:
    def __init__(self, pages, portal=m.PORTAL):
        self.pages = iter(pages)
        self.portal = portal
    def get(self, path):
        if path == "/account-info/v3/details":
            return {"portalId": self.portal}
        return {}
    def tasks_search(self, body):
        return next(self.pages)


class TestSnapshot(unittest.TestCase):
    def test_gmail_fixed_account_no_body_no_send(self):
        with patch.object(m.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps({"state":"ok","identity_verified":True,
                "mailbox":"ygor.bueno@thundermustard.com","items":[{"id":"1","body":"private"}],
                "listing_complete":False})
            value = m.email()
            args = run.call_args.args[0]
            self.assertTrue(args[1].endswith("/seqd_mail_readonly.py"))
            self.assertEqual(len(args), 2)
            self.assertNotIn("body", value["items"][0])
            self.assertFalse(value["listing_complete"])

    def test_gmail_auth_failure_never_zero(self):
        with patch.object(m.subprocess, "run") as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "secret"
            value = m.email()
            self.assertEqual(value["state"], "unavailable")
            self.assertNotIn("secret", str(value))

    def test_seqd_invalid_grant_is_not_empty_inbox(self):
        with patch.object(m.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"state":"unavailable","auth_status":"invalid_grant"}'
            value = m.email()
            self.assertEqual(value["error_code"], "gmail_reauthentication_required")
            self.assertIsNone(value["response_count"])

    def test_seqd_wrong_mailbox_rejected(self):
        with patch.object(m.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"state":"ok","identity_verified":true,"mailbox":"other","items":[]}'
            with self.assertRaises(m.SafeError):
                m.email()

    def test_linkedin_partial_keeps_unknown(self):
        from unittest.mock import Mock
        client, inbox = Mock(), Mock()
        inbox._list.return_value = ([{"thread": "1", "display": []}], None, True, False)
        inbox.last_from_cache = True
        inbox._messages.return_value = ([{"outgoing": False, "message_urn": "m",
            "sent_at_ms": 1, "sender_profile_id": "p", "content": "private"}], False)
        value = m.live_linkedin(dt.datetime.now(dt.timezone.utc), client, inbox)
        self.assertIsNone(value["response_count"])
        self.assertTrue(value["list_from_cache"])
        self.assertEqual(value["items"][0]["messages"][0]["text"], "private")
        self.assertEqual(value["items"][0]["messages"][0]["direction"], "incoming")
        self.assertTrue(value["items"][0]["context_truncated"])
        inbox.poll.assert_not_called()

    def test_context_projection_bounds(self):
        item = m.mail_context({"subject": "x"*500, "raw": "forbidden", "messages": [
            {"text": "senha=secret https://example.test/?token=abc " + "x"*5000,
             "direction": "incoming", "raw": "forbidden"} for _ in range(8)]})
        self.assertEqual(len(item["subject"]), 300)
        self.assertEqual(len(item["messages"]), 4)
        self.assertTrue(item["context_truncated"])
        self.assertLessEqual(len(item["messages"][0]["text"]), 2000)
        self.assertNotIn("secret", str(item))
        self.assertNotIn("token=abc", str(item))
        self.assertNotIn("forbidden", str(item))
        self.assertEqual(item["messages"][0]["direction"], "incoming")

    def test_email_direction_aliases_fail_closed(self):
        from chief_briefing import snapshot_events
        cases = [("inbound", "incoming"), ("incoming", "incoming"),
                 ("outbound", "outgoing"), ("outgoing", "outgoing"), ("own", "outgoing"),
                 ("unknown", "unknown"), (None, "unknown"), ("", "unknown"),
                 ("INBOUND", "unknown"), (" incoming", "unknown"),
                 ([], "unknown"), ({}, "unknown"), (1, "unknown")]
        for direction, expected in cases:
            with self.subTest(direction=direction):
                message = {"id": "a1", "text": "Synthetic context"}
                if direction is not None:
                    message["direction"] = direction
                item = m.mail_context({"threadId": "b1", "messages": [message]})
                self.assertEqual(item["messages"][0]["direction"], expected)
                events, _ = snapshot_events({"schema": "thunder_chief_readonly_v1",
                    "email": {"state": "ok", "identity_verified": True,
                              "listing_complete": True, "items": [item]}})
                self.assertEqual(len(events), int(expected == "incoming"))

    def test_seqd_producer_snapshot_library_selection(self):
        from chief_briefing import BriefingStore, digest
        producer = Path(m.__file__).with_name("seqd_mail_readonly.cjs")
        script = r"""
const deny = () => { throw new Error('network forbidden'); };
require('node:http').request = deny;
require('node:https').request = deny;
require('node:net').Socket.prototype.connect = deny;
const {parseMessage, LIMITS} = require(process.argv[1]);
const owner = 'ygor.bueno@thundermustard.com';
function raw(id, labels, from) {
  return {id, threadId:'b1', labelIds:labels, internalDate:'1792497600000',
    payload:{mimeType:'text/plain', headers:[
      {name:'From',value:from}, {name:'To',value:owner},
      {name:'Auto-Submitted',value:'auto-replied'}],
      body:{data:Buffer.from('Synthetic context only.').toString('base64url')}}};
}
const budget = {remaining:LIMITS.total_text_bytes};
const messages = [raw('a1',['INBOX'],'Buyer <buyer@example.test>'),
  raw('a2',['SENT'],owner), raw('a3',['INBOX'],owner),
  raw('a4',['DRAFT','INBOX'],'Buyer <buyer@example.test>')]
  .map(value => parseMessage(value,budget));
process.stdout.write(JSON.stringify({state:'ok', identity_verified:true,
  mailbox:owner, listing_complete:true, items:[{threadId:'b1',messages}]}));
"""
        completed = subprocess.run(["node", "-e", script, str(producer)],
                                   capture_output=True, text=True, check=True, timeout=10,
                                   env={"PATH": os.defpath})
        produced = json.loads(completed.stdout)
        self.assertEqual([v["direction"] for v in produced["items"][0]["messages"]],
                         ["inbound", "outbound", "own", "unknown"])
        unavailable = {"state": "unavailable"}
        with patch.object(m.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=completed.stdout)), \
                patch.object(m, "Reader", return_value=SimpleNamespace(calls=0)), \
                patch.object(m, "crm", return_value=unavailable), \
                patch.object(m, "live_linkedin", return_value=unavailable):
            observed = m.snapshot()
        messages = observed["email"]["items"][0]["messages"]
        self.assertEqual([v["direction"] for v in messages],
                         ["incoming", "outgoing", "outgoing", "unknown"])
        self.assertEqual([v["id"] for v in messages], ["a1", "a2", "a3", "a4"])
        self.assertTrue(all(v["is_autoreply"] for v in messages))
        with tempfile.TemporaryDirectory() as directory:
            store = BriefingStore(Path(directory) / "receipts.sqlite")
            try:
                store.ingest(observed)
                selected = store.pending()
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["source_event_id"], "a1")
                self.assertEqual(selected[0]["event_key"], digest(["email", "thunder-mustard", "b1", "a1"]))
                self.assertEqual(selected[0]["context"]["thread"]["messages"], messages)
                store.ingest(observed)
                self.assertEqual(store.pending(), selected)
            finally:
                store.close()

    def test_context_retains_automation_and_prior_outbound(self):
        item = m.mail_context({"thread_id":"t", "messages":[
            {"id":"1", "text":"Proposta anterior", "direction":"outbound"},
            {"id":"2", "text":"Recebimento automatico", "direction":"inbound", "is_autoreply":True}]})
        self.assertEqual(item["messages"][0]["direction"], "outgoing")
        self.assertEqual(item["messages"][1]["direction"], "incoming")
        self.assertTrue(item["messages"][1]["is_autoreply"])
        self.assertTrue(item["context_available"])
        self.assertNotIn("secretvalue", m.safe_text('token="secretvalue with space"'))
        self.assertNotIn("privatevalue", m.safe_text('-----BEGIN PRIVATE KEY-----privatevalue'))
        self.assertNotIn("jsonsecret", m.safe_text('{"access_token":"jsonsecret", "password":"jsonsecret"}'))
        self.assertTrue(m.mail_context({"messages":[{"text":"x"*2001}]})["messages"][0]["text_truncated"])

    def test_linkedin_identity_failure_stops_all_reads(self):
        from unittest.mock import Mock
        client, inbox = Mock(), Mock()
        client.verify_identity.side_effect = m.SafeError("account_identity_mismatch")
        with self.assertRaises(m.SafeError):
            m.live_linkedin(dt.datetime.now(dt.timezone.utc), client, inbox)
        inbox._list.assert_not_called()
        inbox._messages.assert_not_called()
        inbox.poll.assert_not_called()

    def test_route_boundary(self):
        for path in ["/v1/send", "/crm/v3/objects/deals?limit=1",
                     "/crm/v3/objects/tasks/1", "https://evil.test", "/crm/v3/objects/tasks?\r\nx"]:
            self.assertFalse(m.allowed(path))
        self.assertTrue(m.allowed("/crm/v3/objects/tasks?limit=100"))

    def test_real_get_only_transport(self):
        with patch.object(m.http.client, "HTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{}'
            reader = m.Reader(lambda: "synthetic-token")
            reader.get("/account-info/v3/details")
            self.assertEqual(connection.return_value.request.call_args.args[0], "GET")
            with self.assertRaises(m.SafeError):
                reader.get("/v1/send")
            self.assertEqual(connection.return_value.request.call_count, 1)

    def test_no_error_leak(self):
        def fail():
            raise RuntimeError("secret or response body")
        self.assertEqual(m.safe_call(fail)["error_code"], "read_failed")

    def test_global_deadline_not_swallowed(self):
        def fail():
            raise m.DeadlineExceeded()
        with self.assertRaises(m.DeadlineExceeded):
            m.safe_call(fail)

    def test_wrong_portal(self):
        with self.assertRaises(m.SafeError):
            m.crm(Fake([], "wrong"), dt.datetime.now(dt.timezone.utc))

    def test_due_not_sent(self):
        row = {"id": "1", "properties": {"hs_task_status": "NOT_STARTED",
                "hs_timestamp": "2026-01-01T00:00:00Z", "hs_task_subject": "Follow up"}}
        value = m.crm(Fake([{"results": [row]}]), dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc))
        self.assertEqual(value["overdue_count_in_scanned_set"], 1)
        self.assertFalse(value["items"][0]["send_authorized"])
        self.assertEqual(value["items"][0]["reply_state"], "unknown")

    def test_pagination_loop(self):
        page = {"results": [], "paging": {"next": {"after": "1"}}}
        with self.assertRaises(m.SafeError):
            m.crm(Fake([page, page]), dt.datetime.now(dt.timezone.utc))

    def test_malformed_pagination(self):
        with self.assertRaises(m.SafeError):
            m.crm(Fake([{"results": [], "paging": {"next": {"link": "ignored"}}}]), dt.datetime.now(dt.timezone.utc))

    def test_invalid_date_reported(self):
        row = {"id": "1", "properties": {"hs_task_status": "NOT_STARTED", "hs_timestamp": "bad"}}
        value = m.crm(Fake([{"results": [row]}]), dt.datetime.now(dt.timezone.utc))
        self.assertEqual(value["invalid_due_dates"], 1)
        self.assertEqual(value["state"], "degraded")

    def test_only_search_post_allowed(self):
        reader = m.Reader(lambda: "fake")
        for path in ["/crm/v3/objects/tasks", "/crm/v3/objects/contacts", "/v1/send"]:
            with self.assertRaises(m.SafeError):
                reader._read("POST", path, {})

    def test_stale_linkedin(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "db"
            conn = sqlite3.connect(path)
            conn.execute("create table thunder_state(kind,entity_id,payload)")
            conn.execute("insert into thunder_state values(?,?,?)",
                         ("connectsafely_account_monitor", "global",
                          json.dumps({"state": "ok", "last_finished_at": 1})))
            conn.commit()
            conn.close()
            value = m.linkedin(dt.datetime.now(dt.timezone.utc), str(path))
            self.assertFalse(value["fresh"])
            self.assertIsNone(value["response_count"])


if __name__ == "__main__":
    unittest.main()
