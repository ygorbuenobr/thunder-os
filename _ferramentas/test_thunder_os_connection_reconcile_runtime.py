"""Mocked ConnectSafely readbacks; no network, credentials, or panel writes."""

import copy
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from thunder_os.connection_reconcile_runtime import (
    audit_cards, audit_panel_snapshot, read_panel_cards,
)
from thunder_os.connectsafely_client import ConnectSafelyError
from thunder_os.connectsafely_social_client import ConnectSafelySocialClient
from thunder_os.connectsafely_social_transport import ConnectSafelySocialHTTPS


ACCOUNT = "a" * 24
OWNER = "urn:li:fsd_profile:ACoOWNER"
RECIPIENT = "urn:li:fsd_profile:ACoDIEGO"
PUBLIC = "diego-fixture"
PROFILE = "https://www.linkedin.com/in/" + PUBLIC + "/"
THREAD = "urn:li:msg_conversation:FIXTURE"
POST = "urn:li:activity:123456789"
POST_URL = "https://www.linkedin.com/feed/update/" + POST + "/"
CREATED = "2026-10-05T12:00:00+00:00"
ANCHOR = int(datetime.fromisoformat(CREATED).timestamp() * 1000)
AFTER = ANCHOR + 1000
BEFORE = ANCHOR - 1000
DM = {"id": "diego-card", "tipo": "dm", "status": "pendente",
      "pessoa": "Diego Fixture", "perfil_url": PROFILE,
      "texto": "Suggested fixture copy", "criado": CREATED}
COMMENT = {"id": "comment-card", "tipo": "comentario", "status": "pendente",
           "post_url": POST_URL, "texto": "Fixture comment", "criado": CREATED}
DM_BINDING = {"card_id": DM["id"], "channel": "linkedin_inbox",
              "account_id": ACCOUNT, "account_public_id": "owner-fixture",
              "owner_profile_urn": OWNER, "recipient_profile_urn": RECIPIENT,
              "recipient_public_id": PUBLIC, "recipient_profile_url": PROFILE}
COMMENT_BINDING = {"card_id": COMMENT["id"], "channel": "linkedin_comment",
                   "account_id": ACCOUNT, "account_public_id": "owner-fixture",
                   "owner_profile_urn": OWNER, "post_urn": POST}


def message(ref, text, stamp=AFTER, *, owner=True):
    return {"messageUrn": "urn:li:messagingMessage:" + ref,
            "senderProfileId": OWNER if owner else RECIPIENT,
            "isSentByOwner": owner, "text": text, "sentAt": stamp,
            "senderName": "PRIVATE", "cookies": "SECRET"}


def comment(ref="1", text=COMMENT["texto"]):
    return {"commentId": "urn:li:fsd_comment:(" + ref + ",urn:li:ugcPost:987654321)",
            "commenterUrn": OWNER, "commenterProfileId": "ACoOWNER",
            "commentText": text, "createdAt": AFTER, "authorName": "PRIVATE"}


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.dm_pages = [[message("ONE", DM["texto"])]]
        self.comment_pages = [[comment()]]
        self.has_conversation = True
        self.exists_account = ACCOUNT
        self.exists_profile = RECIPIENT
        self.exists_thread = THREAD
        self.participants = [{"profileId": OWNER}, {"profileId": RECIPIENT}]
        self.since_rows = []
        self.since_participants = None
        self.since_account = ACCOUNT
        self.since_has_more = False
        self.fail = False

    def __call__(self, method, path, query, body):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        if self.fail:
            raise RuntimeError("SECRET")
        if path == "/linkedin/accounts":
            return 200, [{"id": ACCOUNT, "publicId": "owner-fixture"}]
        if "/exists/" in path:
            return 200, {"success": True, "accountId": self.exists_account,
                         "profileUrn": self.exists_profile,
                         "hasConversation": self.has_conversation,
                         "conversationUrn": self.exists_thread if self.has_conversation else None}
        if path.endswith("/messages"):
            if "since" in query:
                return 200, {"success": True, "accountId": self.since_account,
                             "conversationUrn": THREAD,
                             "participants": copy.deepcopy(
                                 self.since_participants or self.participants),
                             "messages": copy.deepcopy(self.since_rows),
                             "hasMore": self.since_has_more,
                             "fetchingMore": False}
            index = 0 if "cursor" not in query else 1
            if index >= len(self.dm_pages):
                raise RuntimeError("SECRET")
            rows = self.dm_pages[index]
            has_more = index < len(self.dm_pages) - 1
            return 200, {"success": True, "accountId": ACCOUNT,
                         "conversationUrn": THREAD,
                         "participants": copy.deepcopy(self.participants),
                         "messages": copy.deepcopy(rows), "hasMore": has_more,
                         "fetchingMore": False,
                         "nextCursor": str(min(r["sentAt"] for r in rows)) if has_more else None}
        if path == "/linkedin/posts/comments":
            index = body["start"] // 10
            if index >= len(self.comment_pages):
                raise RuntimeError("SECRET")
            rows = self.comment_pages[index]
            has_more = index < len(self.comment_pages) - 1
            return 200, {"success": True, "accountId": ACCOUNT,
                         "postUrl": POST_URL,
                         "postDetails": {"activityUrn": POST,
                                         "ugcPostUrn": "urn:li:ugcPost:987654321"},
                         "comments": copy.deepcopy(rows),
                         "pagination": {"start": body["start"], "count": len(rows),
                                        "hasNextPage": has_more,
                                        "nextPaginationToken": "next" if has_more else None,
                                        "nextStart": body["start"] + len(rows) if has_more else None}}
        raise AssertionError("unexpected route")


class ConnectionReconcileRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.client = ConnectSafelySocialClient(
            ACCOUNT, "owner-fixture", self_profile_id=OWNER,
            request=self.provider)

    def audit(self, cards=None, bindings=None, **options):
        return audit_cards(cards if cards is not None else [DM],
                           bindings if bindings is not None else {DM["id"]: DM_BINDING},
                           self.client, **options)

    def test_exact_diego_outbound_closes_with_sanitized_receipt(self):
        cards = copy.deepcopy([DM])
        result = self.audit(cards)
        item = result["items"][0]
        self.assertEqual(item["decision"], "close")
        self.assertEqual(item["proposed_status"], "enviado")
        self.assertEqual(len(item["evidence"]["reference_sha256"]), 64)
        self.assertFalse(item["evidence"]["write_attribution_confirmed"])
        self.assertEqual(cards, [DM])
        serialized = json.dumps(result)
        for private in ("SECRET", "PRIVATE", DM["texto"], DM["pessoa"], "urn:li:"):
            self.assertNotIn(private, serialized)
        self.assertTrue(all(call[0] == "GET" for call in self.provider.calls))

    def test_recipient_resolves_from_exact_card_url_and_account_echo(self):
        minimal = {key: value for key, value in DM_BINDING.items()
                   if key not in ("recipient_profile_urn", "recipient_public_id",
                                  "recipient_profile_url")}
        item = self.audit(bindings={DM["id"]: minimal})["items"][0]
        self.assertEqual(item["decision"], "close")
        exists = [call for call in self.provider.calls if "/exists/" in call[1]]
        self.assertEqual(len(exists), 1)
        self.assertEqual(exists[0][0], "GET")
        self.assertEqual(exists[0][1].rsplit("/", 1)[-1], PUBLIC)
        self.assertEqual(exists[0][2], {"accountId": ACCOUNT})
        self.assertNotIn("urn:li:", json.dumps(item))

    def test_partial_single_participant_newer_outbound_only_supersedes(self):
        self.provider.participants = [{"profileId": RECIPIENT}]
        self.provider.dm_pages = [[message("OTHER", "Different fixture copy")],
                                  [message("OLD", "Older fixture", BEFORE)]]
        minimal = {key: value for key, value in DM_BINDING.items()
                   if key not in ("recipient_profile_urn", "recipient_public_id",
                                  "recipient_profile_url")}
        item = self.audit(bindings={DM["id"]: minimal})["items"][0]
        self.assertEqual(item["decision"], "superseded")
        self.assertFalse(item["suggested_copy_sent_confirmed"])
        self.assertNotIn("proposed_status", item)
        self.assertEqual(sum(path.endswith("/messages")
                             for _, path, _, _ in self.provider.calls), 1)
        self.assertNotIn("urn:li:", json.dumps(item))
        self.provider.dm_pages = [[message("ONE", DM["texto"])],
                                  [message("OLD", "Older fixture", BEFORE)]]
        self.assertEqual(self.audit(bindings={DM["id"]: minimal}, max_pages=1)["items"][0]["decision"],
                         "inconclusive")

    def test_complete_single_participant_exact_message_can_close(self):
        self.provider.participants = [{"profileId": RECIPIENT}]
        minimal = {key: value for key, value in DM_BINDING.items()
                   if key not in ("recipient_profile_urn", "recipient_public_id",
                                  "recipient_profile_url")}
        self.assertEqual(self.audit(bindings={DM["id"]: minimal})["items"][0]["decision"],
                         "close")

    def test_single_participant_exact_requires_verified_later_receipt(self):
        self.provider.participants = [{"profileId": RECIPIENT}]
        for stamp in (BEFORE, ANCHOR):
            self.provider.dm_pages = [[message("ONE", DM["texto"], stamp)]]
            item = self.audit()["items"][0]
            self.assertEqual(item["decision"], "inconclusive")
            self.assertNotIn("proposed_status", item)
        self.provider.dm_pages = [[message("ONE", DM["texto"], AFTER)]]
        for created in (None, "not-a-date", "2026-10-05T12:00:00"):
            item = self.audit([{**DM, "criado": created}])["items"][0]
            self.assertEqual(item["decision"], "inconclusive")
            self.assertNotIn("proposed_status", item)

    def test_since_canary_partial_page_supersedes_only_with_verified_sender(self):
        self.provider.dm_pages = [[message("OLD", "Older fixture", BEFORE)]]
        self.provider.since_rows = [message("NEW", "Different fixture copy")]
        self.provider.since_participants = [{"profileId": RECIPIENT}]
        self.provider.since_has_more = True
        minimal = {key: value for key, value in DM_BINDING.items()
                   if key not in ("recipient_profile_urn", "recipient_public_id",
                                  "recipient_profile_url")}
        item = self.audit(bindings={DM["id"]: minimal})["items"][0]
        self.assertEqual(item["decision"], "superseded")
        self.assertFalse(item["suggested_copy_sent_confirmed"])
        self.assertNotIn("proposed_status", item)
        since_calls = [query for method, path, query, _ in self.provider.calls
                       if method == "GET" and path.endswith("/messages")
                       and "since" in query]
        self.assertEqual(since_calls, [{"accountId": ACCOUNT, "count": 10,
                                        "since": ANCHOR}])
        self.assertNotIn("urn:li:", json.dumps(item))
        self.provider.since_rows[0]["senderProfileId"] = "urn:li:fsd_profile:ACoOTHER"
        self.assertEqual(self.audit(bindings={DM["id"]: minimal})["items"][0]["decision"],
                         "inconclusive")

    def test_resolved_identity_mismatches_fail_closed(self):
        minimal = {key: value for key, value in DM_BINDING.items()
                   if key not in ("recipient_profile_urn", "recipient_public_id",
                                  "recipient_profile_url")}
        for field, bad in (("exists_account", "b" * 24),
                           ("exists_profile", "urn:li:fsd_profile:ACoOTHER"),
                           ("exists_thread", "urn:li:msg_conversation:OTHER")):
            setattr(self.provider, field, bad)
            self.assertEqual(self.audit(bindings={DM["id"]: minimal})["items"][0]["decision"],
                             "inconclusive")
            setattr(self.provider, field, {"exists_account": ACCOUNT,
                                           "exists_profile": RECIPIENT,
                                           "exists_thread": THREAD}[field])
        self.provider.dm_pages = [[message("OTHER", "Different fixture copy")]]
        self.provider.dm_pages[0][0]["senderProfileId"] = "urn:li:fsd_profile:ACoOTHER"
        self.assertEqual(self.audit(bindings={DM["id"]: minimal})["items"][0]["decision"],
                         "inconclusive")
        changed = {**DM, "perfil_url": "https://www.linkedin.com/in/other-fixture/?q=1"}
        before = len(self.provider.calls)
        self.assertEqual(self.audit([changed], {DM["id"]: minimal})["items"][0]["decision"],
                         "inconclusive")
        self.assertEqual(len(self.provider.calls), before)

    def test_later_different_outbound_or_reply_supersedes_without_marking_sent(self):
        for row, kind in ((message("OTHER", "Different copy"), "outbound_other"),
                          (message("REPLY", "Interested", owner=False), "inbound_response")):
            self.provider.dm_pages = [[row]]
            item = self.audit()["items"][0]
            self.assertEqual(item["decision"], "superseded")
            self.assertFalse(item["suggested_copy_sent_confirmed"])
            self.assertNotIn("proposed_status", item)
            self.assertEqual(item["evidence"]["kind"], kind)
        self.provider.dm_pages = [[message("OLD", "Different copy", BEFORE)]]
        self.assertEqual(self.audit()["items"][0]["decision"], "inconclusive")
        card = {**DM, "criado": "unverified"}
        self.provider.dm_pages = [[message("OTHER", "Different copy")]]
        self.assertEqual(self.audit([card])["items"][0]["decision"], "inconclusive")

    def test_blank_legacy_draft_only_superseded_by_later_thread_event(self):
        card = {**DM, "texto": ""}
        self.provider.dm_pages = [[message("OTHER", "Actual sent copy")]]
        item = self.audit([card])["items"][0]
        self.assertEqual(item["decision"], "superseded")
        self.assertFalse(item["suggested_copy_sent_confirmed"])
        self.provider.dm_pages = [[message("OLD", "Old conversation", BEFORE)]]
        self.assertEqual(self.audit([card])["items"][0]["decision"], "inconclusive")

    def test_missing_first_page_and_incomplete_history_never_close(self):
        self.provider.has_conversation = False
        self.assertEqual(self.audit()["items"][0]["reason"], "conversation_unresolved")
        self.provider.has_conversation = True
        self.provider.dm_pages = [[message("ONE", DM["texto"])],
                                  [message("OLD", "Other", BEFORE)]]
        item = self.audit(max_pages=1)["items"][0]
        self.assertEqual(item["reason"], "readback_incomplete")
        self.assertNotIn("proposed_status", item)

    def test_comment_uses_only_documented_read_post_and_full_pages(self):
        self.provider.comment_pages = [[comment("1", "Other")], [comment("2")]]
        # The runtime requests 10 at a time, so the second page starts at 1.
        original = self.provider
        def paged(method, path, query, body):
            if path == "/linkedin/posts/comments" and body["start"] == 1:
                original.calls.append((method, path, query, copy.deepcopy(body)))
                return 200, {"success": True, "accountId": ACCOUNT,
                             "postUrl": POST_URL,
                             "postDetails": {"activityUrn": POST,
                                             "ugcPostUrn": "urn:li:ugcPost:987654321"},
                             "comments": [comment("2")],
                             "pagination": {"start": 1, "count": 1,
                                            "hasNextPage": False,
                                            "nextPaginationToken": None}}
            return original(method, path, query, body)
        self.client.request = paged
        item = self.audit([COMMENT], {COMMENT["id"]: COMMENT_BINDING})["items"][0]
        self.assertEqual(item["decision"], "close")
        posts = [(method, path) for method, path, _, _ in self.provider.calls if method == "POST"]
        self.assertEqual(posts, [("POST", "/linkedin/posts/comments")] * 2)
        self.assertFalse(any(path in ("/linkedin/posts/comment", "/linkedin/reply")
                             for _, path in posts))

    def test_baseline_41_items_and_no_name_inference(self):
        cards = ([{**DM, "id": "dm-" + str(i)} for i in range(21)]
                 + [{**COMMENT, "id": "comment-" + str(i)} for i in range(18)]
                 + [{"id": "wa-" + str(i), "tipo": "whatsapp",
                     "pessoa": "Diego Fixture", "texto": "PRIVATE"} for i in range(2)])
        result = self.audit(cards, {})
        self.assertTrue(result["baseline_complete"])
        self.assertEqual(result["observed"], {"dm": 21, "comentario": 18,
                                               "whatsapp": 2})
        self.assertEqual(len(result["items"]), 41)
        self.assertTrue(all(item["decision"] == "inconclusive"
                            for item in result["items"]))
        self.assertEqual(self.provider.calls, [])
        self.assertNotIn("Diego", json.dumps(result))

    def test_41_bound_cards_with_zero_exact_positives_remain_inconclusive(self):
        self.provider.dm_pages = [[message("OTHER", "Older unrelated copy", BEFORE)]]
        self.provider.comment_pages = [[comment("9", "Unrelated comment")]]
        cards = ([{**DM, "id": "dm-" + str(i), "texto": "Draft " + str(i)}
                  for i in range(21)]
                 + [{**COMMENT, "id": "comment-" + str(i),
                     "texto": "Comment draft " + str(i)} for i in range(18)]
                 + [{"id": "wa-" + str(i), "tipo": "whatsapp"}
                    for i in range(2)])
        bindings = {
            card["id"]: ({**DM_BINDING, "card_id": card["id"]}
                         if card["tipo"] == "dm"
                         else {**COMMENT_BINDING, "card_id": card["id"]})
            for card in cards if card["tipo"] != "whatsapp"
        }
        result = self.audit(cards, bindings)
        self.assertTrue(result["baseline_complete"])
        self.assertEqual(len(result["items"]), 41)
        self.assertTrue(all(item["decision"] == "inconclusive"
                            for item in result["items"]))
        self.assertEqual(sum(path == "/linkedin/posts/comments"
                             for _, path, _, _ in self.provider.calls), 1)
        self.assertEqual(sum(path.endswith("/messages")
                             for _, path, _, _ in self.provider.calls), 2)

    def test_shared_since_readback_keeps_card_specific_copy_decision(self):
        self.provider.dm_pages = [[message("OLD", "Older unrelated copy", BEFORE)]]
        self.provider.since_rows = [message("NEW", "Newer alternate copy")]
        other = {**DM, "id": "second-card", "texto": "Newer alternate copy"}
        bindings = {DM["id"]: DM_BINDING,
                    other["id"]: {**DM_BINDING, "card_id": other["id"]}}

        result = self.audit([DM, other], bindings)

        self.assertEqual([item["decision"] for item in result["items"]],
                         ["superseded", "inconclusive"])
        self.assertFalse(result["items"][0]["suggested_copy_sent_confirmed"])
        self.assertEqual(sum(path.endswith("/messages")
                             for _, path, _, _ in self.provider.calls), 2)

    def test_exact_binding_account_and_profile_required_before_read(self):
        for changed in ({"account_id": "b" * 24},
                        {"recipient_profile_urn": "urn:li:fsd_profile:OTHER"},
                        {"recipient_public_id": "wrong"},
                        {"card_id": "other-card"}):
            binding = {**DM_BINDING, **changed}
            item = self.audit(bindings={DM["id"]: binding})["items"][0]
            if changed.get("recipient_profile_urn"):
                self.assertEqual(item["reason"], "recipient_identity_mismatch")
            else:
                self.assertEqual(item["decision"], "inconclusive")
        self.assertFalse(any(method == "POST" for method, _, _, _ in self.provider.calls))

    def test_shared_receipt_does_not_close_two_cards(self):
        second = {**DM, "id": "second-card"}
        bindings = {DM["id"]: DM_BINDING,
                    second["id"]: {**DM_BINDING, "card_id": second["id"]}}
        result = self.audit([DM, second], bindings)
        self.assertEqual([item["reason"] for item in result["items"]],
                         ["shared_receipt_ambiguous"] * 2)

    def test_cached_target_rechecks_each_cards_optional_identity_pin(self):
        second = {**DM, "id": "second-card"}
        bindings = {DM["id"]: DM_BINDING,
                    second["id"]: {**DM_BINDING, "card_id": second["id"],
                                   "recipient_profile_urn": "urn:li:fsd_profile:ACoOTHER"}}
        result = self.audit([DM, second], bindings)
        self.assertEqual(result["items"][0]["decision"], "close")
        self.assertEqual(result["items"][1]["reason"], "recipient_identity_mismatch")
        self.assertEqual(sum("/exists/" in path for _, path, _, _ in self.provider.calls), 1)

    def test_provider_error_is_sanitized_and_repeat_is_deterministic(self):
        self.provider.fail = True
        result = self.audit()
        self.assertEqual(result["items"][0]["decision"], "inconclusive")
        self.assertNotIn("SECRET", json.dumps(result))
        second = {**DM, "id": "second-card"}
        bindings = {DM["id"]: DM_BINDING,
                    second["id"]: {**DM_BINDING, "card_id": second["id"]}}
        self.provider.calls.clear()
        repeated = self.audit([DM, second], bindings)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertTrue(all(item["decision"] == "inconclusive"
                            for item in repeated["items"]))
        self.provider.fail = False
        self.assertEqual(self.audit(), self.audit())

    def test_social_transport_allows_comment_read_without_write_capability(self):
        transport = ConnectSafelySocialHTTPS(ACCOUNT, lambda: "synthetic-token-only")
        transport._route("POST", "/linkedin/posts/comments", {}, {
            "accountId": ACCOUNT, "postUrl": POST_URL,
            "commentCount": 10, "start": 0})
        with self.assertRaisesRegex(ConnectSafelyError, "social_coordination_unverified"):
            transport._route("POST", "/linkedin/posts/comment", {}, {
                "accountId": ACCOUNT, "postUrl": POST_URL,
                "comment": "Fixture", "tagPostAuthor": False})

    def test_explicit_sqlite_snapshot_read_and_audit_do_not_write_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "panel.sqlite"
            with closing(sqlite3.connect(path)) as con:
                with con:
                    con.execute("CREATE TABLE itens (id TEXT PRIMARY KEY, dados JSON NOT NULL, atualizado TEXT NOT NULL)")
                    con.execute("INSERT INTO itens VALUES (?,?,?)",
                                (DM["id"], json.dumps({k: v for k, v in DM.items() if k != "id"}),
                                 CREATED))
            before = path.read_bytes()
            self.assertEqual(read_panel_cards(path, [DM["id"]]), [DM])
            result = audit_panel_snapshot(path, [DM["id"]],
                                          {DM["id"]: DM_BINDING}, self.client)
            self.assertEqual(result["items"][0]["decision"], "close")
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(str(path) + "-wal").exists())
            self.assertFalse(Path(str(path) + "-journal").exists())
            with self.assertRaisesRegex(ValueError, "card_snapshot_incomplete"):
                read_panel_cards(path, ["missing-card"])
            with self.assertRaisesRegex(ValueError, "card_ids_invalid"):
                read_panel_cards(path, [DM["id"], DM["id"]])


if __name__ == "__main__":
    unittest.main()
