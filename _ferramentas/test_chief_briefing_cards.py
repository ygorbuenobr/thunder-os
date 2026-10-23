"""Offline projection tests; all ledgers and panel data are synthetic/temp."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import chief_briefing as producer
import chief_briefing_cards as projection
from thunder_os.briefing_cards import BriefingError, put_briefing


def source_snapshot():
    return {"schema": "thunder_chief_readonly_v1", "email": {
        "state": "ok", "identity_verified": True, "listing_complete": True,
        "items": [{"threadId": "synthetic-thread", "subject": "Synthetic subject",
                   "context_truncated": True, "messages": [
                       {"id": "synthetic-out", "direction": "outgoing", "text": "Earlier context",
                        "from": "Equipe", "date": "2026-10-20T11:00:00Z"},
                       {"id": "synthetic-in", "direction": "incoming", "text": "More details please",
                        "from": "Contato", "date": "2026-10-20T12:00:00Z"}]}]},
        "linkedin": {"state": "unavailable"}, "hubspot": {"state": "unavailable"}}


def document():
    events, maintenance = producer.snapshot_events(source_snapshot())
    event = events[0]
    event.update(interpretation="Interpretation, not a fact", recommendation="Prepare a draft",
                 decision="Review before sending", telegram={"who": "Synthetic Contact | email",
                 "what": "Requested details", "recommendation": "Prepare details",
                 "decision": "Review"})
    return {"schema": "thunder_briefing_v1", "events": [event], "analyzed": [],
            "maintenance": maintenance, "pending_count": 0,
            "coverage": "bounded_observed_sample_not_global"}


def later_document():
    doc = document()
    event = doc["events"][0]
    event["source_event_id"] = "synthetic-in-later"
    event["event_key"] = "synthetic-event-later"
    event["evidence"][0]["message_id"] = "synthetic-in-later"
    received = dict(event["context"]["received"], id="synthetic-in-later",
                    date="2026-10-20T13:00:00Z", text="Newer incoming message")
    event["context"]["thread"]["messages"].append(received)
    event["context"]["received"] = received
    event["recommendation"] = "Updated recommendation"
    return doc


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chief-projection-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "panel.sqlite"
        self.con = sqlite3.connect(self.path)
        self.addCleanup(self.con.close)
        self.con.execute("CREATE TABLE itens (id TEXT PRIMARY KEY, dados JSON NOT NULL, atualizado TEXT NOT NULL)")

    def rows(self):
        return self.con.execute("SELECT id,dados,atualizado FROM itens").fetchall()

    def test_exact_identity_full_context_and_evidence_no_actions(self):
        doc = document()
        original = deepcopy(doc)
        before = self.con.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        result = projection.project_document(self.con, doc)
        event = doc["events"][0]
        self.assertEqual(result, {"state": "projected", "cards": [
            {"id": event["event_id"], "state": "persisted", "version": 1}],
            "analyzed_count": 0, "pending_count": 0})
        data = json.loads(self.rows()[0][1])
        self.assertEqual(self.rows()[0][0], event["event_id"])
        self.assertEqual(data["titulo"], event["telegram"]["who"])
        self.assertIn(event["telegram"]["what"], data["porque"])
        self.assertIn(event["telegram"]["recommendation"], data["porque"])
        self.assertEqual([r["texto"] for r in data["contexto"][:2]],
                         ["Earlier context", "More details please"])
        self.assertEqual([r["quem"] for r in data["contexto"][:2]],
                         ["Enviada - Equipe", "Recebida - Contato"])
        self.assertEqual([r["quando"] for r in data["contexto"][:2]],
                         ["2026-10-20T11:00:00+00:00", "2026-10-20T12:00:00+00:00"])
        self.assertEqual([r["texto"] for r in data["contexto"][2:5]],
                         [event["interpretation"], event["recommendation"], event["decision"]])
        self.assertEqual(data["fatos"], ["Fonte: email | Conversa: synthetic-thread | Mensagem: synthetic-in"])
        self.assertIn("Amostra limitada", data["contexto"][-1]["texto"])
        self.assertIn("parcial da origem", data["contexto"][-1]["texto"])
        self.assertEqual(len(data["contexto"]), 6)
        for entry in data["contexto"]:
            with self.assertRaises(json.JSONDecodeError):
                json.loads(entry["texto"])
            for technical in ("synthetic-thread", "synthetic-in", "synthetic-out",
                              "thread_namespace", "untrusted_data", "context_truncated"):
                self.assertNotIn(technical, entry["texto"])
        self.assertEqual((data["origem"], data["tipo"], data["status"], data["rascunho"]),
                         ("briefing", "pedido", "pendente", ""))
        self.assertFalse({"acoes", "actions", "fila_ref", "aprovacao"}.intersection(data))
        self.assertEqual(doc, original)
        self.assertEqual(before, self.con.execute("SELECT type,name,sql FROM sqlite_master").fetchall())

    def test_replay_and_new_message_same_conversation_update_one_card(self):
        doc = document()
        projection.project_document(self.con, doc)
        before, changes = self.rows(), self.con.total_changes
        projection.project_document(self.con, doc)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.con.total_changes, changes)
        result = projection.project_document(self.con, later_document())
        self.assertEqual(result["cards"][0]["version"], 2)
        self.assertEqual(len(self.rows()), 1)

    def test_a_then_b_then_a_replay_cannot_overwrite_newer_message(self):
        projection.project_document(self.con, document())
        projection.project_document(self.con, later_document())
        before, changes = self.rows(), self.con.total_changes
        result = projection.project_document(self.con, document())
        self.assertEqual(result["cards"][0]["state"], "conflict")
        self.assertEqual(result["cards"][0]["code"], "incoming_not_newer")
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.con.total_changes, changes)
        replay = projection.project_document(self.con, later_document())
        self.assertEqual(replay["cards"][0]["version"], 2)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.con.total_changes, changes)

    def test_new_message_equal_date_is_conflict_without_write(self):
        projection.project_document(self.con, document())
        doc = later_document()
        doc["events"][0]["context"]["received"]["date"] = "2026-10-20T09:00:00-03:00"
        before, changes = self.rows(), self.con.total_changes
        result = projection.project_document(self.con, doc)
        self.assertEqual(result["cards"][0]["code"], "incoming_not_newer")
        self.assertEqual(result["state"], "partial")
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.con.total_changes, changes)

    def test_same_message_changed_analysis_requires_reconciliation(self):
        projection.project_document(self.con, document())
        before, changes = self.rows(), self.con.total_changes
        for later_time in (False, True):
            doc = document()
            doc["events"][0]["recommendation"] = "Reanalysis without producer revision"
            if later_time:
                doc["events"][0]["context"]["received"]["date"] = "2026-10-20T14:00:00Z"
            result = projection.project_document(self.con, doc)
            self.assertEqual(result["cards"][0]["state"], "conflict")
            self.assertEqual(result["cards"][0]["code"], "same_message_requires_reconciliation")
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.con.total_changes, changes)

    def test_order_uses_maximum_prior_message_date_not_persistence_clock(self):
        doc = document()
        doc["events"][0]["context"]["thread"]["messages"][0]["date"] = "2026-10-20T14:00:00Z"
        projection.project_document(self.con, doc)
        before = self.rows()
        result = projection.project_document(self.con, later_document())
        self.assertEqual(result["cards"][0]["code"], "incoming_not_newer")
        self.assertEqual(self.rows(), before)

    def test_unknown_prior_date_requires_reconciliation(self):
        projection.project_document(self.con, document())
        data = json.loads(self.rows()[0][1])
        data["contexto"][0]["quando"] = "not a trusted date"
        self.con.execute("UPDATE itens SET dados=?", (json.dumps(data),))
        self.con.commit()
        before = self.rows()
        result = projection.project_document(self.con, later_document())
        self.assertEqual(result["cards"][0]["code"], "previous_order_unknown")
        self.assertEqual(self.rows(), before)

    def test_incoming_evidence_must_match_message_identity_and_direction(self):
        for field, value in (("id", "wrong-message"), ("direction", "outgoing")):
            doc = document()
            doc["events"][0]["context"]["received"][field] = value
            result = projection.project_document(self.con, doc)
            self.assertEqual(result["cards"][0]["code"], "incoming_evidence_mismatch")
            self.assertEqual(self.rows(), [])

    def test_terminal_skip_preserves_discard_audit_byte_for_byte(self):
        doc = document()
        projection.project_document(self.con, doc)
        data = json.loads(self.rows()[0][1])
        data.update(status="descartado", _descarte={"ator": "ygor"}, decisao={"acao": "descartar"})
        self.con.execute("UPDATE itens SET dados=?", (json.dumps(data),))
        self.con.commit()
        before, changes = self.rows(), self.con.total_changes
        doc["events"][0]["recommendation"] = "Must not reopen"
        result = projection.project_document(self.con, doc)
        self.assertEqual(result["cards"][0]["state"], "skipped_terminal")
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.con.total_changes, changes)

    def test_analyzed_only_has_no_false_priority(self):
        doc = document()
        event = doc["events"].pop()
        doc["analyzed"] = [{"event_key": event["event_key"], "category": "non_commercial",
                            "reason": "Synthetic reason", "source_event": event}]
        result = projection.project_document(self.con, doc)
        self.assertEqual(result["state"], "no_priorities")
        self.assertEqual(result["analyzed_count"], 1)
        self.assertEqual(result["cards"], [])
        self.assertEqual(self.rows(), [])

    def test_identity_duplicates_and_evidence_fail_before_any_write(self):
        for mutation in ("id", "namespace", "evidence", "duplicate"):
            doc = document()
            event = doc["events"][0]
            if mutation == "id":
                event["event_id"] = "briefing-" + "0" * 64
            elif mutation == "namespace":
                event["thread_namespace"] = "other:synthetic-thread"
            elif mutation == "evidence":
                event["evidence"][0]["message_id"] = "other-message"
            else:
                doc["events"].append(deepcopy(event))
            with self.subTest(mutation=mutation), self.assertRaises(BriefingError):
                projection.project_document(self.con, doc)
            self.assertEqual(self.rows(), [])

    def test_over_budget_or_missing_date_reports_error_without_truncation(self):
        for mutation in ("date", "title", "context"):
            doc = document()
            event = doc["events"][0]
            if mutation == "date":
                del event["context"]["received"]["date"]
            elif mutation == "title":
                event["telegram"]["who"] = "x" * 241
            else:
                event["context"]["thread"]["messages"][0]["text"] = "x" * 70000
            result = projection.project_document(self.con, doc)
            self.assertEqual(result["state"], "partial")
            self.assertEqual(result["cards"][0]["state"], "error")
            self.assertEqual(self.rows(), [])

    def test_large_message_is_losslessly_chunked_without_raw_metadata(self):
        doc = document()
        text = "synthetic " * 1500
        doc["events"][0]["context"]["thread"]["messages"][0]["text"] = text
        doc["events"][0]["context"]["extra"] = {"private_metadata": "kept only in ledger"}
        result = projection.project_document(self.con, doc)
        self.assertEqual(result["state"], "projected")
        context = json.loads(self.rows()[0][1])["contexto"]
        self.assertEqual("".join(r["texto"] for r in context if r["quem"].startswith("Enviada")), text)
        self.assertNotIn("private_metadata", json.dumps(context))

    def test_millisecond_and_mail_header_dates_are_supported(self):
        for source in ({"sent_at_ms": 1792497600000}, {"internal_date": "1792497600000"},
                       {"date": "Tue, 20 Oct 2026 12:00:00 +0000"}):
            self.con.execute("DELETE FROM itens")
            self.con.commit()
            doc = document()
            context = doc["events"][0]["context"]
            context["received"].pop("date")
            context["received"].update(source)
            context["thread"]["messages"][-1] = deepcopy(context["received"])
            self.assertEqual(projection.project_document(self.con, doc)["state"], "projected")

    def test_received_outside_thread_is_included_once_and_source_limits_visible(self):
        doc = document()
        context = doc["events"][0]["context"]
        context["thread"]["messages"] = context["thread"]["messages"][:1]
        context["received"].pop("from")
        context["received"]["sender_profile_id"] = "technical-profile-id"
        context["received"]["text_truncated"] = True
        self.assertEqual(projection.project_document(self.con, doc)["state"], "projected")
        entries = json.loads(self.rows()[0][1])["contexto"]
        self.assertEqual(entries[1]["quem"], "Recebida - Autoria nao informada")
        self.assertEqual(entries[1]["texto"], "More details please\n[Texto parcial na leitura de origem.]")
        self.assertEqual(len(entries), 6)
        self.assertNotIn("technical-profile-id", json.dumps(entries))

    def test_missing_message_date_is_explicit_error_not_invented_timestamp(self):
        doc = document()
        del doc["events"][0]["context"]["thread"]["messages"][0]["date"]
        result = projection.project_document(self.con, doc)
        self.assertEqual(result["cards"][0]["code"], "missing_observed_time")
        self.assertEqual(self.rows(), [])

    def test_cas_conflict_with_real_interleaved_write_is_not_retried(self):
        doc = document()
        projection.project_document(self.con, doc)
        doc = later_document()

        def concurrent_write(conn, item, expected_version):
            put_briefing(conn, dict(item, titulo="Concurrent winner"), expected_version)
            return put_briefing(conn, item, expected_version)

        with patch.object(projection, "put_briefing", side_effect=concurrent_write) as intercepted:
            result = projection.project_document(self.con, doc)
        self.assertEqual(intercepted.call_count, 1)
        self.assertEqual(result["cards"][0]["state"], "conflict")
        self.assertEqual(json.loads(self.rows()[0][1])["titulo"], "Concurrent winner")

    def test_readback_detects_post_commit_change(self):
        def change_after_commit(conn, item, expected_version):
            saved = put_briefing(conn, item, expected_version)
            put_briefing(conn, dict(item, titulo="Later change"), saved["version"])
            return saved

        with patch.object(projection, "put_briefing", side_effect=change_after_commit):
            result = projection.project_document(self.con, document())
        self.assertEqual(result["cards"][0], {"id": document()["events"][0]["event_id"],
                         "state": "conflict", "code": "readback_changed"})

    def test_terminal_arriving_after_read_is_skipped(self):
        doc = document()
        projection.project_document(self.con, doc)

        def discard_before_write(conn, item, expected_version):
            data = json.loads(self.rows()[0][1])
            data.update(status="descartado", _descarte={"ator": "ygor"})
            conn.execute("UPDATE itens SET dados=?", (json.dumps(data),))
            conn.commit()
            return put_briefing(conn, item, expected_version)

        with patch.object(projection, "put_briefing", side_effect=discard_before_write):
            result = projection.project_document(self.con, doc)
        self.assertEqual(result["cards"][0]["state"], "skipped_terminal")
        self.assertEqual(json.loads(self.rows()[0][1])["status"], "descartado")

    def test_real_briefing_store_batch_document_projects_without_delivery(self):
        store = producer.BriefingStore(Path(self.tmp.name) / "briefing.sqlite")
        self.addCleanup(store.close)
        store.ingest(source_snapshot())
        selection = store.pending()
        event = document()["events"][0]
        response = {"priorities": [{key: event[key] for key in (
            "event_key", "recommendation", "decision", "interpretation", "telegram")}],
            "dispositions": []}
        batch = store.prepare(json.dumps(response), selection, producer.DESTINATION)
        before = store.batch(batch["batch_id"])
        self.assertEqual(projection.project_document(self.con, batch["document"])["state"], "projected")
        self.assertEqual(store.batch(batch["batch_id"]), before)
        self.assertEqual(before["state"], "prepared")
        self.assertIsNone(before["receipt"])

    def test_origin_collision_and_invalid_document_do_not_mutate(self):
        self.con.execute("INSERT INTO itens VALUES (?,?,?)", (
            document()["events"][0]["event_id"], '{"origem":"other"}', "original"))
        self.con.commit()
        before = self.rows()
        result = projection.project_document(self.con, document())
        self.assertEqual(result["cards"][0]["code"], "origin_collision")
        self.assertEqual(self.rows(), before)
        with self.assertRaises(BriefingError):
            projection.project_document(self.con, {"events": []})


if __name__ == "__main__":
    unittest.main()
