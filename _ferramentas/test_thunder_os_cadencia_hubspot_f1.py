import ast
from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from thunder_os import cadencia_hubspot_f1 as f1


class F1SimulationTests(unittest.TestCase):
    def snapshot(self):
        return {"synthetic": True, "portal_id": f1.PORTAL_ID, "owner_id": f1.OWNER_ID,
                "object_type": "contacts", "complete": True, "includes_archived": True,
                "groups": [], "properties": []}

    def fixture(self):
        return {"synthetic": True, "portal_id": f1.PORTAL_ID,
                "owner": {"id": f1.OWNER_ID, "userId": "123456", "archived": False},
                "pages": {
                    "": {"status": 200, "body": {"results": [{"id": "fixture-1", "name": "Synthetic 1", "userId": "123456"}],
                         "paging": {"next": {"after": "cursor/2=", "link": "https://untrusted.invalid"}}}},
                    "cursor/2=": {"status": 200, "body": {"results": [{"id": "fixture-2", "name": "Synthetic 2", "userId": "123456"}]}}}}

    def test_exact_section_4_contract(self):
        props = {p["name"]: p for p in f1.properties()}
        expected_multiline = {"cad_email_1", "cad_wa_1", "cad_email_2", "cad_email_3", "cad_email_4",
                              "cad_email_4b", "cad_li_0", "cad_li_1", "cad_li_2", "cad_li_3", "cad_tese", "cad_fato_fonte"}
        self.assertEqual(set(props), expected_multiline | {"cad_braco", "cad_email_4_versao", "cad_status", "cad_email_1_assunto", "cad_teste_id"})
        for name in expected_multiline:
            self.assertEqual((props[name]["type"], props[name]["fieldType"]), ("string", "textarea"))
        for name in ("cad_email_1_assunto", "cad_teste_id"):
            self.assertEqual((props[name]["type"], props[name]["fieldType"]), ("string", "text"))
        for name, values in {"cad_braco": ["A", "B"], "cad_email_4_versao": ["A", "B"],
                             "cad_status": ["rascunho", "aprovado", "em_cadencia", "respondeu", "encerrado", "incerto"]}.items():
            self.assertEqual((props[name]["type"], props[name]["fieldType"]), ("enumeration", "select"))
            self.assertEqual([o["value"] for o in props[name]["options"]], values)
        self.assertTrue(all(p["groupName"] == "cadencia_thunder" for p in props.values()))
        self.assertEqual(f1.GROUP["label"], "Cadência Thunder")

    def test_empty_snapshot_plan_does_not_modify_input(self):
        snapshot = self.snapshot()
        before = deepcopy(snapshot)
        plan = f1.plan_schema(snapshot)
        self.assertEqual(snapshot, before)
        self.assertEqual(plan["status"], "planned_only")
        self.assertEqual(len(plan["proposals"]), 18)
        self.assertEqual(plan["external_effects"], 0)
        self.assertTrue(all(p["path"].startswith("/crm/v3/properties/contacts") for p in plan["proposals"]))

    def test_idempotent_plan_from_synthetic_readback(self):
        snapshot = self.snapshot()
        for proposal in f1.plan_schema(snapshot)["proposals"]:
            key = "groups" if proposal["path"].endswith("groups") else "properties"
            snapshot[key].append(deepcopy(proposal["body"]))
        plan = f1.plan_schema(snapshot)
        self.assertEqual(plan["proposals"], [])
        self.assertEqual(len(plan["unchanged"]), 17)

    def test_schema_conflicts_block_all_proposals(self):
        for change in ({"type": "number"}, {"fieldType": "text"}, {"groupName": "other"},
                       {"archived": True}, {"calculated": True}, {"hasUniqueValue": True},
                       {"modificationMetadata": {"readOnlyValue": True}}):
            with self.subTest(change=change):
                snapshot = self.snapshot()
                snapshot["properties"] = [{**f1.properties()[0], **change}]
                plan = f1.plan_schema(snapshot)
                self.assertEqual(plan["status"], "blocked")
                self.assertEqual(plan["proposals"], [])

    def test_enum_changes_block(self):
        prop = next(p for p in f1.properties() if p["name"] == "cad_braco")
        for options in ([prop["options"][0]], prop["options"] + [{"value": "C"}],
                        [{**o, "hidden": True} for o in prop["options"]]):
            snapshot = self.snapshot()
            snapshot["properties"] = [{**prop, "options": options}]
            self.assertEqual(f1.plan_schema(snapshot)["status"], "blocked")

    def test_group_alias_and_archive_block(self):
        for group in ({**f1.GROUP, "name": "already_exists"}, {**f1.GROUP, "archived": True}):
            snapshot = self.snapshot()
            snapshot["groups"] = [group]
            self.assertEqual(f1.plan_schema(snapshot)["proposals"], [])
            self.assertEqual(f1.plan_schema(snapshot)["status"], "blocked")

    def test_incomplete_wrong_scope_and_real_mode_rejected(self):
        for change in ({"portal_id": "other"}, {"owner_id": "other"}, {"object_type": "deals"},
                       {"complete": False}, {"includes_archived": False}, {"synthetic": False}):
            with self.assertRaises(f1.SimulationError):
                f1.plan_schema({**self.snapshot(), **change})
        for fn, value in ((f1.plan_schema, self.snapshot()), (f1.list_sequences_fixture, self.fixture())):
            with self.assertRaisesRegex(f1.SimulationError, "real_mode_unavailable"):
                fn(value, modo_simulacao=False)

    def test_duplicate_schema_rejected(self):
        snapshot = self.snapshot()
        snapshot["properties"] = [f1.properties()[0]] * 2
        with self.assertRaisesRegex(f1.SimulationError, "duplicate_schema_name"):
            f1.plan_schema(snapshot)

    def test_paginated_fixture_uses_user_id_not_owner_or_link(self):
        result = f1.list_sequences_fixture(self.fixture())
        self.assertEqual([r["id"] for r in result["results"]], ["fixture-1", "fixture-2"])
        self.assertEqual(result["requests"][1]["query"], {"userId": "123456", "limit": 100, "after": "cursor/2="})
        self.assertEqual(result["requests"][1]["path"], "/automation/v4/sequences")
        self.assertEqual(result["external_effects"], 0)

    def test_numeric_owner_user_id_matches_owners_api(self):
        fixture = self.fixture()
        fixture["owner"]["userId"] = 123456
        result = f1.list_sequences_fixture(fixture)
        self.assertEqual(result["requests"][0]["query"]["userId"], "123456")
        for invalid in (True, False, 0, -1, "0", "１２３"):
            fixture["owner"]["userId"] = invalid
            with self.assertRaisesRegex(f1.SimulationError, "resolved_user_id_required"):
                f1.list_sequences_fixture(fixture)

    def test_failed_or_missing_page_never_returns_partial_success(self):
        for status in (401, 403, 429, 500):
            fixture = self.fixture()
            fixture["pages"]["cursor/2="]["status"] = status
            with self.assertRaises(f1.SimulationError):
                f1.list_sequences_fixture(fixture)
        fixture = self.fixture()
        del fixture["pages"]["cursor/2="]
        with self.assertRaises(f1.SimulationError):
            f1.list_sequences_fixture(fixture)

    def test_user_resolution_and_pagination_integrity(self):
        fixture = self.fixture()
        del fixture["owner"]["userId"]
        with self.assertRaisesRegex(f1.SimulationError, "resolved_user_id_required"):
            f1.list_sequences_fixture(fixture)
        fixture = self.fixture()
        fixture["pages"]["cursor/2="]["body"]["paging"] = {"next": {"after": "cursor/2="}}
        with self.assertRaisesRegex(f1.SimulationError, "pagination_cycle"):
            f1.list_sequences_fixture(fixture)
        fixture = self.fixture()
        fixture["pages"][""]["body"]["results"][0]["userId"] = f1.OWNER_ID
        with self.assertRaisesRegex(f1.SimulationError, "invalid_sequence_identity"):
            f1.list_sequences_fixture(fixture)

    def test_calendar_tokens_threads_and_unimplemented_dependencies(self):
        bp = f1.sequence_blueprint()
        self.assertEqual(bp["timezone"], "America/Sao_Paulo")
        self.assertTrue(bp["business_days_only"])
        self.assertEqual([s["day"] for s in bp["steps"]], [0, 1, 3, 4, 5, 8, 10, 13, 15])
        self.assertEqual([s["delay_business_days"] for s in bp["steps"]], [0, 1, 2, 1, 1, 3, 2, 3, 2])
        emails = [s for s in bp["steps"] if s["kind"] == "email"]
        self.assertEqual([s["body_token"] for s in emails], ["{{ contact.cad_email_" + str(n) + " }}" for n in range(1, 5)])
        self.assertEqual([s["thread"] for s in emails], ["new"] + ["reply_to_email_1"] * 3)
        self.assertEqual([s["title"] for s in bp["steps"] if s["kind"] == "task"],
                         ["LinkedIn: convite", "WhatsApp 1", "LinkedIn: msg 1", "LinkedIn: msg 2", "LinkedIn: msg 3"])
        self.assertEqual(len(bp["unimplemented_dependencies"]), 2)
        self.assertEqual(bp["linkedin_property"], "linkedin_url")
        self.assertEqual(bp["steps"][3]["body_token"], "{{ contact.cad_wa_1 }}")
        self.assertNotIn("cad_linkedin_url", {p["name"] for p in f1.properties()})
        self.assertNotIn("linkedin_url", {p["name"] for p in f1.properties()})

    def test_no_network_and_no_credential_imports(self):
        tree = ast.parse(inspect.getsource(f1))
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual(imports, {"__future__", "argparse", "copy", "json"})
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            self.assertEqual(f1.demo()["schema"]["external_effects"], 0)

    def test_cli_emits_only_synthetic_demo_and_rejects_live_flag(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        command = [sys.executable, str(Path(f1.__file__)), "--demo"]
        result = subprocess.run(command, capture_output=True, text=True, check=True, env=env)
        self.assertEqual(json.loads(result.stdout)["sequence_read"]["status"], "fixture_read_only")
        rejected = subprocess.run(command + ["--live"], capture_output=True, text=True, env=env)
        self.assertNotEqual(rejected.returncode, 0)


if __name__ == "__main__":
    unittest.main()
