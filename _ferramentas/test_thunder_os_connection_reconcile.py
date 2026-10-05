"""Local synthetic contract checks; no provider or panel access."""

import copy
import unittest

from thunder_os.connection_reconcile import evaluate_card


ACCOUNT = "a" * 24
OWNER = "urn:li:fsd_profile:ACoOWNER"
DIEGO = "urn:li:fsd_profile:ACoDIEGO"
PROFILE = "https://www.linkedin.com/in/diego-fixture/"
THREAD = "urn:li:msg_conversation:FIXTURE"
POST = "urn:li:activity:123456789"
POST_URL = "https://www.linkedin.com/feed/update/" + POST + "/"
DM_CARD = {"id": "card-diego-fixture", "tipo": "dm", "status": "pendente",
           "pessoa": "Diego", "perfil_url": PROFILE, "texto": "Mensagem sintética exata",
           "criado": "2026-10-05T12:00:00+00:00"}
DM_READBACK = {
    "source": "connectsafely", "accountId": ACCOUNT, "complete": True,
    "conversationUrn": THREAD,
    "participants": [{"profileId": OWNER}, {"profileId": DIEGO}],
    "messages": [{"messageUrn": "urn:li:messagingMessage:FIXTURE1",
                  "senderProfileId": OWNER, "isSentByOwner": True,
                  "text": DM_CARD["texto"], "sentAt": 1_800_000_000_000}],
}
COMMENT_CARD = {"id": "card-comment-fixture", "tipo": "comentario", "status": "pendente",
                "post_url": POST_URL, "texto": "Comentário sintético exato",
                "criado": "2026-10-05T12:00:00+00:00"}
COMMENT_READBACK = {
    "source": "connectsafely", "accountId": ACCOUNT, "complete": True,
    "postDetails": {"activityUrn": POST, "ugcPostUrn": "urn:li:ugcPost:987654321"},
    "comments": [{"commentId": "urn:li:fsd_comment:(123,urn:li:ugcPost:987654321)",
                  "commenterUrn": OWNER, "commentText": COMMENT_CARD["texto"],
                  "createdAt": 1_800_000_000_000}],
}


class ConnectionReconcileTests(unittest.TestCase):
    def dm(self, card=None, observation=None, **overrides):
        options = {"channel": "linkedin_inbox", "account_id": ACCOUNT,
                   "owner_profile_urn": OWNER, "recipient_profile_urn": DIEGO,
                   "recipient_profile_url": PROFILE,
                   "observation": observation if observation is not None else DM_READBACK}
        options.update(overrides)
        return evaluate_card(card if card is not None else DM_CARD, **options)

    def comment(self, card=None, observation=None, **overrides):
        options = {"channel": "linkedin_comment", "account_id": ACCOUNT,
                   "owner_profile_urn": OWNER,
                   "observation": observation if observation is not None else COMMENT_READBACK}
        options.update(overrides)
        return evaluate_card(card if card is not None else COMMENT_CARD, **options)

    def test_diego_already_sent_exact_readback_can_close_without_attributing_write(self):
        card = copy.deepcopy(DM_CARD)
        readback = copy.deepcopy(DM_READBACK)
        result = self.dm(card, readback)
        self.assertEqual(result["decision"], "close")
        self.assertEqual(result["proposed_status"], "enviado")
        self.assertEqual(result["card_id"], card["id"])
        self.assertEqual(result["evidence"]["reference"], readback["messages"][0]["messageUrn"])
        self.assertFalse(result["evidence"]["write_attribution_confirmed"])
        self.assertEqual(card, DM_CARD)
        self.assertEqual(readback, DM_READBACK)
        self.assertNotIn(card["texto"], str(result))

    def test_old_identical_message_or_comment_does_not_close_new_card(self):
        readback = copy.deepcopy(DM_READBACK)
        readback["messages"][0]["sentAt"] = 1_000_000_000_000
        self.assertEqual(self.dm(observation=readback)["decision"], "inconclusive")
        readback = copy.deepcopy(COMMENT_READBACK)
        readback["comments"][0]["createdAt"] = 1_000_000_000_000
        self.assertEqual(self.comment(observation=readback)["decision"], "inconclusive")
        self.assertEqual(self.dm(card={k: v for k, v in DM_CARD.items() if k != "criado"})["reason"],
                         "card_creation_unverified")

    def test_incoming_response_does_not_close_outbound_card(self):
        readback = copy.deepcopy(DM_READBACK)
        readback["messages"] = [{"messageUrn": "urn:li:messagingMessage:RESPONSE",
                                 "senderProfileId": DIEGO, "isSentByOwner": False,
                                 "text": DM_CARD["texto"], "sentAt": 1_800_000_000_001}]
        self.assertEqual(self.dm(observation=readback)["decision"], "inconclusive")

    def test_incomplete_history_is_inconclusive_even_with_matching_row(self):
        readback = copy.deepcopy(DM_READBACK)
        readback["complete"] = False
        result = self.dm(observation=readback)
        self.assertEqual(result["reason"], "readback_incomplete")
        self.assertNotIn("proposed_status", result)
        readback = copy.deepcopy(COMMENT_READBACK)
        readback.pop("complete")
        self.assertEqual(self.comment(observation=readback)["reason"], "readback_incomplete")

    def test_malformed_or_duplicate_readback_is_inconclusive(self):
        readback = copy.deepcopy(DM_READBACK)
        readback["messages"].append({"messageUrn": "urn:li:messagingMessage:OTHER",
                                     "senderProfileId": DIEGO, "isSentByOwner": False,
                                     "sentAt": 1_800_000_000_001})
        self.assertEqual(self.dm(observation=readback)["reason"], "readback_incomplete")
        readback = copy.deepcopy(DM_READBACK)
        readback["messages"].append(copy.deepcopy(readback["messages"][0]))
        self.assertEqual(self.dm(observation=readback)["reason"], "readback_incomplete")
        readback = copy.deepcopy(DM_READBACK)
        readback["messages"][0]["isSentByOwner"] = False
        self.assertEqual(self.dm(observation=readback)["reason"], "readback_incomplete")
        readback = copy.deepcopy(COMMENT_READBACK)
        readback["comments"][0]["commenterProfileId"] = "ACoOTHER"
        self.assertEqual(self.comment(observation=readback)["reason"], "readback_incomplete")

    def test_ambiguous_matching_rows_do_not_close(self):
        readback = copy.deepcopy(DM_READBACK)
        duplicate = copy.deepcopy(readback["messages"][0])
        duplicate["messageUrn"] = "urn:li:messagingMessage:FIXTURE2"
        readback["messages"].append(duplicate)
        self.assertEqual(self.dm(observation=readback)["reason"], "receipt_missing_or_ambiguous")
        readback = copy.deepcopy(COMMENT_READBACK)
        duplicate = copy.deepcopy(readback["comments"][0])
        duplicate["commentId"] = "urn:li:fsd_comment:(124,urn:li:ugcPost:987654321)"
        readback["comments"].append(duplicate)
        self.assertEqual(self.comment(observation=readback)["reason"], "receipt_missing_or_ambiguous")

    def test_idempotent_pure_evaluation_and_closed_card_noop(self):
        first = self.dm()
        self.assertEqual(self.dm(), first)
        closed = {**DM_CARD, "status": "enviado"}
        self.assertEqual(self.dm(card=closed), {
            "card_id": closed["id"], "decision": "unchanged", "reason": "already_closed"})

    def test_comment_exact_owner_content_and_post_can_close(self):
        result = self.comment()
        self.assertEqual(result["decision"], "close")
        self.assertEqual(result["evidence"]["kind"], "comment")
        self.assertEqual(result["evidence"]["target"], POST)
        changed = copy.deepcopy(COMMENT_READBACK)
        changed["comments"][0]["commenterUrn"] = DIEGO
        self.assertEqual(self.comment(observation=changed)["decision"], "inconclusive")
        changed = copy.deepcopy(COMMENT_READBACK)
        changed["postUrl"] = "https://www.linkedin.com/feed/update/urn:li:activity:999/"
        self.assertEqual(self.comment(observation=changed)["reason"], "post_identity_unverified")

    def test_wrong_account_or_name_only_never_matches(self):
        self.assertEqual(self.dm(account_id="b" * 24)["reason"], "account_mismatch")
        self.assertEqual(self.dm(recipient_profile_urn=None)["reason"], "recipient_identity_unverified")
        self.assertEqual(self.dm(recipient_profile_url=None)["reason"], "recipient_identity_unverified")
        readback = copy.deepcopy(DM_READBACK)
        readback["participants"][1] = {"name": "Diego"}
        self.assertEqual(self.dm(observation=readback)["decision"], "inconclusive")

    def test_whatsapp_always_inconclusive(self):
        card = {"id": "card-whatsapp", "tipo": "whatsapp", "status": "pendente",
                "texto": "Mensagem sintética"}
        result = evaluate_card(card, channel="whatsapp", account_id=ACCOUNT,
                               owner_profile_urn=OWNER, observation=DM_READBACK)
        self.assertEqual(result, {"card_id": card["id"], "decision": "inconclusive",
                                  "reason": "whatsapp_readback_unavailable"})


if __name__ == "__main__":
    unittest.main()
