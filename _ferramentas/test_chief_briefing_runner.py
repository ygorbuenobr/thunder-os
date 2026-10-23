"""Offline runner integration: real private library + fake Telegram/model, no providers."""
import asyncio
from contextlib import closing
import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import chief_briefing as library
import chief_briefing_runner as runner

TEST_THREAD = "abc123"


def snapshot(count=1):
    return {"schema": "thunder_chief_readonly_v1", "email": {
        "state": "ok", "identity_verified": True, "listing_complete": True,
        "items": [{"threadId": "thread-" + str(i), "messages": [
            {"id": "message-" + str(i), "direction": "incoming", "from": "Synthetic",
             "date": "2026-10-20T12:00:00+00:00", "text": "Pode enviar detalhes?"}]}
            for i in range(count)]}}


def proposal(selection):
    return json.dumps({"priorities": [{
        "event_key": selection[0]["event_key"], "recommendation": "Preparar detalhes.",
        "decision": "Revisar rascunho.", "interpretation": "Pedido de detalhes.",
        "telegram": {"who": "Contato sintetico", "what": "Pediu detalhes.",
                     "recommendation": "Preparar rascunho.", "decision": "Revisar."}}],
        "dispositions": []})


class FakeSender:
    def __init__(self, store, result=None, error=None):
        self.store, self.result, self.error = store, result, error
        self.calls = 0
        self.closed = False

    async def verify(self):
        pass

    async def send(self, text):
        self.calls += 1
        row = self.store.db.execute("SELECT * FROM batches WHERE state='sending'").fetchone()
        assert row is not None and row["text"] == text
        assert json.loads(row["document"])["events"][0]["context"]["received"]["text"]
        if self.error:
            raise self.error
        return self.result

    async def close(self):
        self.closed = True


class TestRunner(unittest.TestCase):
    def test_collect_snapshot_assembly_fixed_credential_path_without_inherited_secrets(self):
        expected = "/var/lib/thunder-os-integrations/credentials/seqd_bridge"
        fake = Mock(returncode=0)
        fake.communicate.return_value = (json.dumps(snapshot()).encode(), b"")
        for inherited in ({}, {"THUNDER_OS_SEQD_CREDENTIAL_FILE": "/tmp/forged/seqd_bridge",
                               "CREDENTIALS_DIRECTORY": "/tmp/forged",
                               "TELEGRAM_BOT_TOKEN": "synthetic-private-value"}):
            with self.subTest(inherited=bool(inherited)), patch.dict(os.environ, inherited, clear=True), \
                    patch.object(runner.subprocess, "Popen", return_value=fake) as popen:
                observed = runner.collect_snapshot()
                self.assertEqual(observed["schema"], "thunder_chief_readonly_v1")
                self.assertEqual(popen.call_args.args[0], [str(runner.PYTHON), str(runner.SOURCE / "chief_read_snapshot.py")])
                env = popen.call_args.kwargs["env"]
                self.assertEqual(env["THUNDER_OS_SEQD_CREDENTIAL_FILE"], expected)
                self.assertNotIn("CREDENTIALS_DIRECTORY", env)
                self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
                self.assertNotIn("synthetic-private-value", str(env))
                self.assertNotIn("forged", str(env))
                self.assertNotIn("THUNDER_OS_SEQD_CREDENTIAL_FILE", runner.child_environment())

    def test_seqd_constructor_is_lazy_without_reading_credentials_or_network(self):
        from thunder_os.seqd_client import SEQDClient
        with patch("thunder_os.seqd_client.os.open", side_effect=AssertionError("credential read forbidden")) as opened, \
                patch("thunder_os.seqd_client._request", side_effect=AssertionError("network forbidden")) as request:
            SEQDClient()
            opened.assert_not_called()
            request.assert_not_called()

    def test_manual_missing_wrong_pin_and_missing_deliver_block_before_provider(self):
        state = self.directory / "manual"
        for pin, deliver in ((None, True), ("def456", True), ("", True),
                             ("ABC123", True), (TEST_THREAD, False)):
            env = {} if pin is None else {"THUNDER_BRIEFING_MANUAL_THREAD_ID": pin}
            with self.subTest(pin=pin, deliver=deliver), patch.dict(os.environ, env, clear=True), \
                    patch.object(runner, "MANUAL_STATE", state), \
                    patch.object(runner, "require_identity") as identity, \
                    patch("chief_read_snapshot.manual_snapshot") as collect, \
                    patch.object(runner, "generate") as generate, \
                    patch.object(runner, "official_sender", AsyncMock()) as sender, \
                    patch("sys.stdout", io.StringIO()):
                argv = ["--manual-test", "--thread-id", TEST_THREAD, "--state-dir", str(state)]
                if deliver:
                    argv.append("--deliver")
                self.assertEqual(runner.cli(argv), 1)
                identity.assert_not_called()
                collect.assert_not_called()
                generate.assert_not_called()
                sender.assert_not_awaited()
                self.assertFalse(state.exists())

    def test_child_rejects_changed_skill_before_provider_import(self):
        payload = json.dumps({"selection": [{"event_key": "synthetic"}], "skill_sha256": "0"*64}).encode()
        with patch.object(runner, "require_identity"), \
                patch.object(runner.sys, "stdin", NS(buffer=io.BytesIO(payload))):
            with self.assertRaisesRegex(runner.RunnerError, "skill_hash_mismatch"):
                runner.generation_child()

    def test_manual_state_rejects_git_or_symlink_and_both_modes_use_same_safe_sender(self):
        state = self.directory / "test-state"
        with patch.object(runner, "MANUAL_STATE", state):
            self.assertEqual(runner.validate_manual_request(TEST_THREAD, state, True), state)
            (self.directory / ".git").write_text("gitdir: synthetic")
            with self.assertRaisesRegex(runner.RunnerError, "manual_state_blocked"):
                runner.validate_manual_request(TEST_THREAD, state, True)
        self.assertEqual(runner.RECIPIENT, "6619531976")
        self.assertEqual(runner.BOT_ID, 8842126545)

    def test_manual_one_thread_no_cards_receipt_replay_and_skill_diagnostic(self):
        observed = snapshot()
        observed["email"]["items"][0]["threadId"] = TEST_THREAD
        state = self.directory / "manual"
        sender = NS(verify=AsyncMock(), close=AsyncMock(),
                    send=AsyncMock(return_value=NS(success=True, message_id="321")))
        generator = Mock(side_effect=lambda selection, **kwargs: proposal(selection))
        out = io.StringIO()
        with patch.object(runner, "MANUAL_STATE", state), patch.object(runner, "require_identity"), \
                patch("chief_read_snapshot.manual_snapshot", return_value=observed) as collect, \
                patch.object(runner, "generate", generator), \
                patch.object(runner, "official_sender", AsyncMock(return_value=sender)) as factory, \
                patch.object(runner, "project_cards", side_effect=AssertionError("cards forbidden")), \
                patch("chief_read_snapshot.crm", side_effect=AssertionError("CRM forbidden")), \
                patch("chief_read_snapshot.live_linkedin", side_effect=AssertionError("LinkedIn forbidden")), \
                patch("sys.stdout", out):
            self.assertEqual(runner.cli(["--manual-test", "--thread-id", TEST_THREAD,
                                        "--state-dir", str(state), "--deliver"]), 0)
            self.assertEqual(runner.manual_test(TEST_THREAD, str(state), deliver=True), 0)
        statuses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([s["state"] for s in statuses], ["delivered", "idle"])
        self.assertEqual(statuses[0]["skill_sha256"], runner.load_briefing_skill()["sha256"])
        self.assertEqual(generator.call_args.kwargs["skill_sha256"], statuses[0]["skill_sha256"])
        generator.assert_called_once()
        factory.assert_awaited_once()
        sender.send.assert_awaited_once()
        self.assertTrue(sender.send.call_args.args[0].startswith(library.MANUAL_TEST_HEADER + "\n\n"))
        self.assertLessEqual(len(sender.send.call_args.args[0].encode("utf-16-le")) // 2, 1200)
        collect.assert_called_with(TEST_THREAD)
        self.assertTrue((state / "receipts.sqlite").is_file())
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        for name in ("receipts.sqlite", "runner.lock", "last-run.json", "runs.jsonl"):
            self.assertEqual(stat.S_IMODE((state / name).stat().st_mode), 0o600)
        import hashlib
        import sqlite3
        with closing(sqlite3.connect((state / "receipts.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
            text, digest, receipt = db.execute("SELECT text,payload_sha256,receipt FROM batches").fetchone()
        self.assertEqual(text, sender.send.call_args.args[0])
        self.assertEqual(digest, hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(json.loads(receipt)["payload_sha256"], digest)
        self.assertNotIn("Pode enviar detalhes", out.getvalue())

    def test_manual_failure_is_safe_and_does_not_construct_sender(self):
        state = self.directory / "manual"
        out = io.StringIO()
        with patch.object(runner, "MANUAL_STATE", state), patch.object(runner, "require_identity"), \
                patch("chief_read_snapshot.manual_snapshot", side_effect=RuntimeError("private message")), \
                patch.object(runner, "official_sender", AsyncMock()) as factory, patch("sys.stdout", out):
            self.assertEqual(runner.manual_test(TEST_THREAD, state, deliver=True), 1)
        factory.assert_not_awaited()
        self.assertNotIn("private message", out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["code"], "unexpected_failure")

    def test_manual_validation_blocks_other_thread_state_and_missing_deliver(self):
        for args in (("aa", runner.MANUAL_STATE, True),
                     (TEST_THREAD, runner.STATE, True),
                     (TEST_THREAD, runner.MANUAL_STATE, False)):
            with self.subTest(args=args), self.assertRaises(runner.RunnerError):
                runner.validate_manual_request(*args)

    def test_skill_hash_consumed_by_model_and_change_rejected(self):
        skill = runner.load_briefing_skill()
        self.assertEqual(len(skill["sha256"]), 64)
        self.store.ingest(snapshot())
        selection = self.store.pending()
        value = json.loads(proposal(selection))
        value["priorities"][0]["event_key"] = "E1"
        fake = NS(tools=[], valid_tool_names=set(), close=Mock(), run_conversation=Mock(
            return_value={"final_response": json.dumps(value), "messages": []}))
        runner.infer(selection, Mock(return_value=fake), {}, "model", library.MODEL_SCHEMA, skill=skill)
        prompt = fake.run_conversation.call_args.kwargs["system_message"]
        self.assertIn(skill["text"], prompt)
        self.assertIn(skill["sha256"], prompt)
        with self.assertRaises(runner.RunnerError):
            runner.load_briefing_skill(expected_sha256="0"*64)

    def setUp(self):
        pin_patch = patch.dict(os.environ, {"THUNDER_BRIEFING_MANUAL_THREAD_ID": TEST_THREAD})
        pin_patch.start()
        self.addCleanup(pin_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name).resolve()
        self.skill_path = self.directory / "SKILL.md"
        self.skill_path.write_text("---\nname: briefing-diario\nversion: 1.3.0\n---\nSynthetic skill contract.\n")
        skill_patch = patch.object(runner, "SKILL", self.skill_path)
        skill_patch.start()
        self.addCleanup(skill_patch.stop)
        self.store = library.BriefingStore(self.directory / "private.sqlite")
        self.addCleanup(self.store.close)
        self.addCleanup(self.tmp.cleanup)
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.sender = FakeSender(self.store, NS(success=True, message_id="123"))

    def execute(self, generator=proposal, count=1):
        return asyncio.run(runner.run_once(self.store, lambda: snapshot(count),
                           generator, AsyncMock(return_value=self.sender)))

    def test_persist_before_send_receipt_and_replay(self):
        result = self.execute()
        self.assertEqual(result["state"], "delivered")
        batch = self.store.batch(result["batch_id"])
        receipt = json.loads(batch["receipt"])
        self.assertEqual(receipt["destination"], "telegram:6619531976")
        self.assertEqual(receipt["message_id"], "123")
        self.assertEqual(receipt["payload_sha256"], batch["payload_sha256"])
        self.assertEqual(self.execute()["state"], "idle")
        self.assertEqual(self.sender.calls, 1)

    def test_overflow_pending_and_unobserved_retained(self):
        self.assertEqual(self.execute(count=5)["state"], "delivered")
        self.assertEqual(len(self.store.pending()), 4)
        self.store.ingest(snapshot(0))
        self.assertEqual(len(self.store.pending()), 4)

    def test_generation_failure_preserves_pending(self):
        for fn in (Mock(side_effect=TimeoutError("secret")), lambda _: "invalid"):
            with self.assertRaises(Exception):
                self.execute(fn)
            self.assertEqual(len(self.store.pending()), 1)
            self.assertEqual(self.sender.calls, 0)

    def test_preparation_only_never_constructs_sender(self):
        factory = AsyncMock(side_effect=AssertionError("sender forbidden"))
        projection = Mock(side_effect=AssertionError("projection forbidden"))
        result = asyncio.run(runner.run_once(self.store, lambda: snapshot(), proposal,
                                             factory, prepare_only=True, projection=projection))
        self.assertEqual(result["state"], "prepared")
        self.assertFalse(result["delivery_attempted"])
        self.assertEqual(self.store.batch(result["batch_id"])["state"], "prepared")
        factory.assert_not_awaited()
        projection.assert_not_called()

    def test_state_is_fixed_outside_profile_and_source(self):
        self.assertEqual(runner.STATE, Path(
            "/var/lib/thunder-os-integrations/briefing-chief/thunder-mustard"))
        self.assertNotIn(runner.PROFILE, runner.STATE.parents)
        self.assertNotIn(runner.SOURCE, runner.STATE.parents)

    def test_main_normal_flow_keeps_all_state_outside_git_profile(self):
        profile = self.directory / "profile"
        (profile / ".git").mkdir(parents=True)
        evidence = profile / "briefing-chief" / "last-run.json"
        evidence.parent.mkdir()
        evidence.write_text('{"state":"failed","code":"state_must_be_outside_git"}')
        original_evidence = evidence.read_bytes()
        state = self.directory / "private" / "briefing-chief" / "thunder-mustard"
        out, err = io.StringIO(), io.StringIO()
        sender = NS(verify=AsyncMock(), close=AsyncMock(),
                    send=AsyncMock(return_value=NS(success=True, message_id="321")))
        factory = AsyncMock(return_value=sender)
        projection = Mock()
        with patch.object(runner, "STATE", state), patch.object(runner, "PROFILE", profile), \
                patch.object(runner, "require_identity"), \
                patch.object(runner, "generate", side_effect=lambda selection, **kwargs: proposal(selection)), \
                patch.object(runner, "official_sender", factory), \
                patch.object(runner, "project_cards", projection), \
                patch("sys.stdout", out), patch("sys.stderr", err):
            self.assertEqual(runner.main(snapshot_loader=lambda: snapshot()), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        factory.assert_awaited_once()
        sender.send.assert_awaited_once()
        projection.assert_called_once()
        self.assertEqual(evidence.read_bytes(), original_evidence)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        for name in ("runner.lock", "receipts.sqlite", "runs.jsonl", "last-run.json"):
            self.assertTrue((state / name).is_file(), name)
            self.assertEqual(stat.S_IMODE((state / name).stat().st_mode), 0o600)
        self.assertFalse((state / "last-run.tmp").exists())
        status = json.loads((state / "last-run.json").read_text())
        self.assertEqual(status["state"], "delivered")
        self.assertEqual(status["stage"], "receipt")
        self.assertEqual(status["mode"], "cron")
        self.assertEqual(json.loads((state / "runs.jsonl").read_text()), status)
        with closing(library.BriefingStore(state / "receipts.sqlite")) as store:
            receipt = json.loads(store.batch(status["batch_id"])["receipt"])
            self.assertEqual(receipt["message_id"], "321")

    def test_main_wires_cards_only_in_normal_mode(self):
        for prepare_only in (False, True):
            with patch.object(runner, "STATE", self.directory), patch.object(runner, "require_identity"), \
                    patch.object(runner, "project_cards") as projection, \
                    patch.object(runner, "run_once", AsyncMock(return_value={"state": "idle"})) as run:
                self.assertEqual(runner.main(prepare_only=prepare_only), 0)
                self.assertIs(run.call_args.kwargs["projection"], None if prepare_only else projection)
                projection.assert_not_called()

    def test_project_cards_uses_exact_existing_database_and_closes(self):
        connection = Mock()
        result = {"state": "projected", "cards": []}
        mapper = Mock(return_value=result)
        info = NS(st_mode=stat.S_IFREG | 0o600, st_dev=1, st_ino=2)
        document = {"events": []}
        with patch.object(runner, "require_identity"), \
                patch.dict(sys.modules, {"chief_briefing_cards": NS(project_document=mapper)}), \
                patch.object(Path, "resolve", return_value=runner.CARDS_DB), \
                patch.object(Path, "lstat", return_value=info), \
                patch("sqlite3.connect", return_value=connection) as connect:
            self.assertEqual(runner.project_cards(document), result)
        connect.assert_called_once_with(
            "file:///root/.hermes/profiles/ygor-personal/painel-dados/painel.db?mode=rw",
            uri=True, timeout=3, isolation_level=None)
        mapper.assert_called_once_with(connection, document)
        connection.close.assert_called_once()

    def test_project_cards_partial_or_error_has_safe_code(self):
        info = NS(st_mode=stat.S_IFREG | 0o600, st_dev=1, st_ino=2)
        for outcome in ({"state": "partial", "cards": [{"code": "private detail"}]},
                        None, RuntimeError("secret payload")):
            connection = Mock()
            mapper = Mock(side_effect=outcome if isinstance(outcome, Exception) else None,
                          return_value=outcome)
            with patch.object(runner, "require_identity"), \
                    patch.dict(sys.modules, {"chief_briefing_cards": NS(project_document=mapper)}), \
                    patch.object(Path, "resolve", return_value=runner.CARDS_DB), \
                    patch.object(Path, "lstat", return_value=info), \
                    patch("sqlite3.connect", return_value=connection):
                with self.assertRaisesRegex(runner.RunnerError, "^projection_failed$") as caught:
                    runner.project_cards({"events": []})
                self.assertEqual(runner.error_code(caught.exception), "projection_failed")
            connection.close.assert_called_once()

    def test_project_cards_rejects_missing_symlink_and_replaced_file(self):
        existing = self.directory / "existing.sqlite"
        existing.touch()
        link = self.directory / "link.sqlite"
        link.symlink_to(existing)
        mapper = Mock()
        for path in (link, self.directory / "missing.sqlite"):
            with patch.object(runner, "require_identity"), patch.object(runner, "CARDS_DB", path), \
                    patch.dict(sys.modules, {"chief_briefing_cards": NS(project_document=mapper)}), \
                    patch("sqlite3.connect") as connect:
                with self.assertRaisesRegex(runner.RunnerError, "^projection_failed$"):
                    runner.project_cards({"events": []})
                connect.assert_not_called()
        connection = Mock()
        before = NS(st_mode=stat.S_IFREG | 0o600, st_dev=1, st_ino=2)
        after = NS(st_mode=stat.S_IFREG | 0o600, st_dev=1, st_ino=3)
        with patch.object(runner, "require_identity"), \
                patch.dict(sys.modules, {"chief_briefing_cards": NS(project_document=mapper)}), \
                patch.object(Path, "resolve", return_value=runner.CARDS_DB), \
                patch.object(Path, "lstat", side_effect=[before, after]), \
                patch("sqlite3.connect", return_value=connection):
            with self.assertRaisesRegex(runner.RunnerError, "^projection_failed$"):
                runner.project_cards({"events": []})
        mapper.assert_not_called()
        connection.close.assert_called_once()

    def test_prepare_once_accepts_snapshot_without_collection_or_delivery(self):
        with patch.object(runner, "collect_snapshot", side_effect=AssertionError("live read forbidden")), \
                patch.object(runner, "official_sender", side_effect=AssertionError("sender forbidden")):
            batch = runner.prepare_once(self.store, lambda: snapshot(), proposal)
        self.assertEqual(batch["state"], "prepared")

    def test_optional_projection_runs_after_persistence_before_delivery(self):
        def project(document):
            self.assertEqual(self.sender.calls, 0)
            row = self.store.db.execute("SELECT document FROM batches WHERE state='prepared'").fetchone()
            self.assertEqual(json.loads(row[0]), document)
            self.assertTrue(document["events"][0]["thread_namespace"].startswith("thunder-mustard:"))
        projection = Mock(side_effect=project)
        output = asyncio.run(runner.run_once(self.store, lambda: snapshot(), proposal,
                                             AsyncMock(return_value=self.sender), projection=projection))
        self.assertEqual(output["state"], "delivered")
        projection.assert_called_once()

    def test_projection_failure_cancels_before_delivery(self):
        factory = AsyncMock(side_effect=AssertionError("sender forbidden"))
        with self.assertRaises(RuntimeError):
            asyncio.run(runner.run_once(self.store, lambda: snapshot(), proposal, factory,
                        projection=Mock(side_effect=RuntimeError("projection failed"))))
        factory.assert_not_awaited()
        self.assertEqual(len(self.store.pending()), 1)

    def test_safe_diagnostics_persist_code_stage_without_exception_payload(self):
        async def failed(*args, progress=None, **kwargs):
            progress["stage"] = "validation_persistence"
            raise library.BriefingError("schema_invalid")
        with patch.object(runner, "STATE", self.directory), patch.object(runner, "require_identity"), \
                patch.object(runner, "run_once", side_effect=failed):
            self.assertEqual(runner.main(prepare_only=True), 1)
        status = json.loads((self.directory / "last-run.json").read_text())
        self.assertEqual(status["code"], "schema_invalid")
        self.assertEqual(status["stage"], "validation_persistence")
        self.assertEqual(status["mode"], "prepare_only")
        self.assertIn("finished_at", status)
        history = [json.loads(row) for row in (self.directory / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(history[-1], status)
        self.assertEqual(runner.error_code(RuntimeError("secret payload")), "unexpected_failure")
        self.assertEqual(runner.error_code(library.BriefingError("token=private")), "unexpected_failure")

    def test_collector_uses_installed_sibling_not_mutable_checkout(self):
        installed = Path("/root/.hermes/profiles/thunder-mustard/scripts")
        with patch.object(runner, "SOURCE", installed), \
                patch.object(runner, "child_output", return_value=json.dumps(snapshot())) as child:
            runner.collect_snapshot()
        self.assertEqual(child.call_args.args[0][1], str(installed / "chief_read_snapshot.py"))

    def test_zero_priorities_is_analysis_no_delivery(self):
        def noncommercial(selection):
            return json.dumps({"priorities": [], "dispositions": [{
                "event_key": selection[0]["event_key"], "category": "newsletter",
                "reason": "Conteudo informativo sem pedido comercial."}]})
        result = self.execute(noncommercial)
        self.assertEqual(result["state"], "analyzed")
        self.assertEqual(self.sender.calls, 0)
        self.assertEqual(self.store.batch(result["batch_id"])["text"], "")

    def test_missing_receipt_uncertain(self):
        for index, result in enumerate((None, NS(success=True, message_id=None), NS(success=False, message_id="12"),
                       NS(success=True, message_id=True), NS(success=True, message_id="0"))):
            with self.subTest(result=result):
                self.store.close()
                self.store = library.BriefingStore(self.directory / ("case-" + str(index) + ".sqlite"))
                self.addCleanup(self.store.close)
                sender = FakeSender(self.store, result)
                self.sender = sender
                output = self.execute()
                self.assertEqual(output["state"], "uncertain")
                self.assertEqual(sender.calls, 1)
                with self.assertRaises(library.BriefingError):
                    self.execute()
                self.assertEqual(sender.calls, 1)

    def test_timeout_and_cancel_uncertain_no_retry(self):
        self.sender.error = TimeoutError("private provider error")
        result = self.execute()
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.sender.calls, 1)
        self.assertTrue(self.sender.closed)
        with self.assertRaises(library.BriefingError):
            self.execute()
        self.assertEqual(self.sender.calls, 1)

    def test_identity_failure_releases_prepared(self):
        self.sender.verify = AsyncMock(side_effect=runner.RunnerError("identity_blocked"))
        with self.assertRaises(runner.RunnerError):
            self.execute()
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.sender.calls, 0)

    def test_cancel_during_send_is_uncertain(self):
        self.sender.error = asyncio.CancelledError()
        result = self.execute()
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.sender.calls, 1)
        self.assertEqual(self.store.batch(result["batch_id"])["state"], "uncertain")

    def test_prepared_or_crashed_sending_requires_reconciliation(self):
        self.store.ingest(snapshot())
        selection = self.store.pending()
        batch = self.store.prepare(proposal(selection), selection, runner.DESTINATION)
        for state in ("prepared", "sending"):
            with self.assertRaisesRegex(library.BriefingError, "delivery_reconciliation_required"):
                self.execute()
            self.assertEqual(self.sender.calls, 0)
            if state == "prepared":
                self.store.begin_delivery(batch["batch_id"])

    def test_execution_lock_rejects_overlap_and_symlink(self):
        path = self.directory / "state"
        with runner.execution_lock(path):
            with self.assertRaises(runner.RunnerError):
                with runner.execution_lock(path):
                    self.fail("overlap")
        link = self.directory / "link"
        link.symlink_to(path)
        with self.assertRaises(runner.RunnerError):
            with runner.execution_lock(link):
                self.fail("symlink")

    def test_host_profile_fail_closed(self):
        with patch.object(runner.socket, "gethostname", return_value="another-host"):
            with self.assertRaises(runner.RunnerError):
                runner.require_identity()

    def test_parent_cannot_mask_failure_without_durable_status(self):
        with patch.object(runner, "STATE", self.directory), \
                patch.object(runner, "require_identity", side_effect=runner.RunnerError("identity_blocked")):
            self.assertEqual(runner.main(), 2)
        self.assertFalse((self.directory / "last-run.json").exists())
        with patch.dict(os.environ, {"HERMES_HOME": "/root/.hermes/profiles/ygor-personal"}):
            with self.assertRaises(runner.RunnerError):
                runner.require_identity()

    def test_no_inherited_delivery_credentials_or_tools(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "synthetic",
                                     "BOT_CHAT_ID": "999", "HERMES_KANBAN_TASK": "task"}):
            env = runner.child_environment()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("BOT_CHAT_ID", env)
        self.assertNotIn("HERMES_KANBAN_TASK", env)
        self.assertEqual(env["HERMES_HOME"], str(runner.PROFILE))

    def test_text_agent_contract_and_tool_rejection(self):
        self.store.ingest(snapshot())
        selection = self.store.pending()
        response = json.loads(proposal(selection))
        response["priorities"][0]["event_key"] = "E1"
        fake = NS(tools=[], valid_tool_names=set(), close=Mock(),
                  run_conversation=Mock(return_value={"final_response": json.dumps(response), "messages": []}))
        factory = Mock(return_value=fake)
        output = runner.infer(selection, factory, {}, "model", library.MODEL_SCHEMA)
        self.assertEqual(json.loads(output), json.loads(proposal(selection)))
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["enabled_toolsets"], [])
        self.assertEqual(kwargs["disabled_toolsets"], ["all"])
        self.assertTrue(kwargs["skip_context_files"])
        self.assertTrue(kwargs["skip_memory"])
        self.assertFalse(kwargs["load_soul_identity"])
        with self.assertRaises(runner.RunnerError):
            fake._execute_tool_calls(None)
        fake.tools = [{"function": {"name": "send_message"}}]
        with self.assertRaises(runner.RunnerError):
            runner.infer(selection, factory, {}, "model", library.MODEL_SCHEMA)
        self.assertEqual(fake.run_conversation.call_count, 1)

    def test_exact_alias_contract_maps_known_aliases_and_preserves_inputs(self):
        self.store.ingest(snapshot(2))
        selection = self.store.pending()
        original = json.dumps(selection, sort_keys=True)
        schema_before = json.dumps(library.MODEL_SCHEMA, sort_keys=True)
        projected, schema, aliases = runner.model_selection_contract(selection, library.MODEL_SCHEMA)
        self.assertEqual([item["event_key"] for item in projected], ["E1", "E2"])
        for section in ("priorities", "dispositions"):
            self.assertEqual(schema["properties"][section]["items"]["properties"]["event_key"],
                             {"type": "string", "enum": ["E1", "E2"]})
        value = json.loads(proposal(projected))
        value["dispositions"] = [{"event_key": "E2", "category": "insufficient_evidence",
                                   "reason": "Contexto insuficiente para recomendar abordagem."}]
        output = runner.bind_model_aliases(json.dumps(value), schema, aliases)
        decoded = json.loads(output)
        self.assertEqual(decoded["priorities"][0]["event_key"], selection[0]["event_key"])
        self.assertEqual(decoded["dispositions"][0]["event_key"], selection[1]["event_key"])
        batch = self.store.prepare(output, selection, runner.DESTINATION)
        self.assertEqual(batch["state"], "prepared")
        self.assertEqual(batch["document"]["events"][0]["context"], selection[0]["context"])
        self.assertEqual(json.dumps(selection, sort_keys=True), original)
        self.assertEqual(json.dumps(library.MODEL_SCHEMA, sort_keys=True), schema_before)

    def test_invented_or_normalized_aliases_are_rejected_without_repair(self):
        self.store.ingest(snapshot())
        selection = self.store.pending()
        projected, schema, aliases = runner.model_selection_contract(selection, library.MODEL_SCHEMA)
        for alias in ("E99", "e1", " E1", "E01", selection[0]["event_key"], "invented"):
            value = json.loads(proposal(projected))
            value["priorities"][0]["event_key"] = alias
            fake = NS(tools=[], valid_tool_names=set(), close=Mock(),
                      run_conversation=Mock(return_value={"final_response": json.dumps(value), "messages": []}))
            with self.subTest(alias=alias), self.assertRaisesRegex(runner.RunnerError, "model_alias_schema_invalid"):
                runner.infer(selection, Mock(return_value=fake), {}, "model", library.MODEL_SCHEMA)
            self.assertEqual(fake.run_conversation.call_count, 1)
            fake.close.assert_called_once()
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM batches").fetchone()[0], 0)

    def test_alias_repetition_across_or_within_sections_is_rejected(self):
        self.store.ingest(snapshot())
        projected, schema, aliases = runner.model_selection_contract(self.store.pending(), library.MODEL_SCHEMA)
        for across in (True, False):
            value = json.loads(proposal(projected))
            if across:
                value["dispositions"] = [{"event_key": "E1", "category": "non_commercial", "reason": "Teste."}]
            else:
                value["priorities"].append(dict(value["priorities"][0]))
            with self.assertRaisesRegex(runner.RunnerError, "model_event_alias_duplicate"):
                runner.bind_model_aliases(json.dumps(value), schema, aliases)
        with self.assertRaisesRegex(runner.RunnerError, "duplicate_json_key"):
            runner.bind_model_aliases('{"priorities":[],"priorities":[],"dispositions":[]}', schema, aliases)

    def test_infer_prompt_preserves_pauses_modalities_and_optout(self):
        self.store.ingest(snapshot())
        selection = self.store.pending()
        value = {"priorities": [], "dispositions": [{"event_key": "E1", "category": "insufficient_evidence",
                                                     "reason": "Sem contexto de campanha."}]}
        fake = NS(tools=[], valid_tool_names=set(), close=Mock(),
                  run_conversation=Mock(return_value={"final_response": json.dumps(value), "messages": []}))
        runner.infer(selection, Mock(return_value=fake), {}, "model", library.MODEL_SCHEMA)
        call = fake.run_conversation.call_args
        self.assertEqual(json.loads(call.args[0])["untrusted_observed_events"][0]["event_key"], "E1")
        prompt = call.kwargs["system_message"]
        for rule in ("pausas", "Mexico/networking/Loa", "Brasil/ofertas", "desconhecida permanece",
                     "Opt-out pede bloqueio de abordagem e revisao humana, nunca follow-up",
                     "nao autoriza escrita CRM", "nao prova zero global", "nao e nova resposta"):
            self.assertIn(rule, prompt)

    def test_commercial_relevance_policy_and_noncommercial_no_effects(self):
        observed = snapshot()
        observed["email"]["items"][0]["messages"][0]["text"] = (
            "Oferta de trabalho remoto remunerado para avaliacao de IA em portugues.")
        self.store.ingest(observed)
        selection = self.store.pending()
        output = {"priorities": [], "dispositions": [{
            "event_key": "E1", "category": "non_commercial",
            "reason": "Recrutamento, sem demanda de comprador da Thunder."}]}
        fake = NS(tools=[], valid_tool_names=set(), close=Mock(),
                  run_conversation=Mock(return_value={
                      "final_response": json.dumps(output), "messages": []}))
        response = runner.infer(selection, Mock(return_value=fake), {}, "model", library.MODEL_SCHEMA)
        prompt = fake.run_conversation.call_args.kwargs["system_message"]
        for rule in ("apenas comprador/cliente com evidencia",
                     "receita, demanda ou resposta concreta de negocio relacionada a Thunder",
                     "Oferta de emprego, recrutamento ou prestacao pessoal do Ygor sem demanda pelos servicos da Thunder",
                     "fornecedor vendendo ao Ygor", "dispositions non_commercial",
                     "insufficient_evidence (needs_context), nunca prioridade por padrao",
                     "Analise e classificacao somente leitura nao precisam de aprovacao",
                     "Nao confunda recrutamento pessoal com comprador contratando servicos da Thunder",
                     "pedido explicito de contratacao, consultoria ou reuniao comercial",
                     "pelo papel das partes e objeto da demanda, nao por palavra-chave isolada"):
            self.assertIn(rule, prompt)
        sender = AsyncMock(side_effect=AssertionError("sender forbidden"))
        projection = Mock(side_effect=AssertionError("projection forbidden"))
        result = asyncio.run(runner.run_once(
            self.store, lambda: observed, lambda _: response, sender, projection=projection))
        self.assertEqual(result["state"], "analyzed")
        document = self.store.batch(result["batch_id"])["document"]
        self.assertEqual(document["events"], [])
        self.assertEqual(document["analyzed"][0]["category"], "non_commercial")
        sender.assert_not_awaited()
        projection.assert_not_called()

    def test_child_timeout_kills_process_group(self):
        fake = Mock(pid=12345, returncode=-9)
        fake.communicate.side_effect = [__import__("subprocess").TimeoutExpired("child", 1), (b"", None)]
        with patch.object(runner.subprocess, "Popen", return_value=fake), patch.object(runner.os, "killpg") as kill:
            with self.assertRaises(runner.RunnerError):
                runner.child_output(["fixed"], timeout=1)
        kill.assert_called_once_with(12345, runner.signal.SIGKILL)

    def test_parent_stdout_empty_success_and_failure(self):
        for outcome in ({"state": "delivered"}, RuntimeError("token=private")):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(runner, "STATE", self.directory), patch.object(runner, "require_identity"), \
                    patch.object(runner, "run_once", AsyncMock(side_effect=outcome if isinstance(outcome, Exception) else None,
                                                            return_value=outcome)), \
                    patch("sys.stdout", out), patch("sys.stderr", err):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(err.getvalue(), "")
            status = json.loads((self.directory / "last-run.json").read_text())
            self.assertNotIn("private", json.dumps(status))
            self.assertIn(status["state"], ("failed", "delivered"))

    def test_official_adapter_fake_bot_single_call_and_verified_receipt(self):
        sys.path.insert(0, str(runner.RELEASE))
        with patch.dict(os.environ, {"HERMES_HOME": str(self.directory)}):
            from gateway.config import PlatformConfig
            from gateway.platforms.base import SendResult
            from plugins.platforms.telegram.adapter import TelegramAdapter
            from telegram.error import NetworkError
            for error in (None, TimeoutError("ambiguous"), NetworkError("connection lost")):
                bot = NS(
                    get_me=AsyncMock(return_value=NS(id=runner.BOT_ID, is_bot=True, username=runner.BOT_USERNAME)),
                    shutdown=AsyncMock(),
                    send_message=AsyncMock(side_effect=error, return_value=NS(
                        message_id=321, chat=NS(id=int(runner.RECIPIENT), type="private"),
                        from_user=NS(id=runner.BOT_ID), text="Mensagem curta.")))
                adapter = TelegramAdapter(PlatformConfig(typing_indicator=False, reply_to_mode="off"))
                proxy = runner.OneShotBot(bot)
                adapter._bot = proxy
                sender = runner.ProfileTelegram(adapter, bot, proxy, SendResult)
                async def exercise():
                    await sender.verify()
                    if error:
                        with self.assertRaises(runner.RunnerError):
                            await sender.send("Mensagem curta.")
                    else:
                        result = await sender.send("Mensagem curta.")
                        self.assertEqual(result.message_id, "321")
                    with self.assertRaises(runner.RunnerError):
                        await proxy.send_message(chat_id=runner.RECIPIENT)
                asyncio.run(exercise())
                self.assertEqual(bot.send_message.call_count, 1)
                self.assertIsNone(bot.send_message.call_args.kwargs["parse_mode"])

    def test_bot_identity_and_message_destination_rejected(self):
        bot = NS(get_me=AsyncMock(return_value=NS(id=1, is_bot=True, username=runner.BOT_USERNAME)))
        sender = runner.ProfileTelegram(None, bot, None, object)
        with self.assertRaises(runner.RunnerError):
            asyncio.run(sender.verify())
        proxy = runner.OneShotBot(bot)
        proxy.content = "Hello"
        with self.assertRaises(runner.RunnerError):
            asyncio.run(proxy.send_message(chat_id="999"))
        self.assertFalse(proxy.used)

    def test_sendresult_alone_or_wrong_message_is_not_receipt(self):
        class Result:
            success = True
            message_id = "12"
            continuation_message_ids = ()
        adapter = NS(send=AsyncMock(return_value=Result()))
        for message in (None, NS(message_id=12, chat=NS(id=999, type="private"),
                                from_user=NS(id=runner.BOT_ID), text="Valid text")):
            proxy = NS(content=None, message=message)
            sender = runner.ProfileTelegram(adapter, None, proxy, Result)
            with self.assertRaises(runner.RunnerError):
                asyncio.run(sender.send("Valid text"))

    def test_changed_hermes_module_origin_rejected(self):
        with patch.dict(sys.modules, {"fake_module": NS(__file__="/tmp/other/run_agent.py")}):
            with self.assertRaises(runner.RunnerError):
                runner.require_module_origin("fake_module", "run_agent.py")


if __name__ == "__main__":
    unittest.main()
