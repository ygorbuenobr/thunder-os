"""Synthetic provider tests. No real credential, network, database or CRM write."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from test_thunder_os_campaign_registry import config

from thunder_os.cadence_readonly import (
    ACTIVITY_PROPERTIES, CAMPAIGN_ID, OWNER_ID, PORTAL_ID, SEQUENCE_ID,
    TASK_PROPERTIES, CadenceReadonlyConsole, CadenceReadonlyError,
    HubSpotReadonlyAdapter, plain_text, project_activity, project_snapshot, project_task,
)


def task(identifier="1", title="LinkedIn: convite", contact="100", **changes):
    props = {"hubspot_owner_id": OWNER_ID, "hs_task_subject": title,
             "hs_task_body": "<p>Texto integral</p>", "hs_task_status": "NOT_STARTED",
             "hs_timestamp": "2026-10-21T10:00:00Z", "hs_task_sequence_id": SEQUENCE_ID,
             "hs_task_sequence_step_enrollment_id": "200",
             "hs_task_sequence_step_enrollment_contact_id": contact}
    props.update(changes)
    return {"id": identifier, "properties": props}


class Provider:
    def __init__(self):
        self.calls = []
        self.tasks = [task()]
        self.portal = PORTAL_ID
        self.owner = OWNER_ID
        self.schema = list(TASK_PROPERTIES)
        self.pages = None
        self.task_links = {}
        self.activity = {"id": "300", "properties": {
            "hs_timestamp": "2026-10-21T12:00:00Z", "hs_email_subject": "Resposta",
            "hs_email_text": "<p>Obrigado</p>", "hs_email_direction": "INCOMING_EMAIL"}}

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, deepcopy(body)))
        assert 0 < timeout <= 8
        if path == "/account-info/v3/details":
            result = {"portalId": self.portal}
        elif path.startswith("/crm/v3/owners/"):
            result = {"id": self.owner, "archived": False}
        elif path.startswith("/crm/v3/properties/"):
            kind = path.rsplit("/", 1)[-1]
            result = {"results": [{"name": name} for name in (self.schema if kind == "tasks" else ACTIVITY_PROPERTIES[kind])]}
        elif path.endswith("tasks/search"):
            if self.pages is not None:
                result = self.pages[str(body.get("after", ""))]
            else:
                result = {"results": self.tasks, "total": len(self.tasks)}
        elif path == "/crm/v4/associations/tasks/contacts/batch/read":
            result = {"status": "COMPLETE", "results": [{"from": {"id": item["id"]},
                "to": [{"toObjectId": cid} for cid in self.task_links.get(item["id"], [])]} for item in body["inputs"]]}
        elif path == "/crm/v3/objects/contacts/batch/read":
            result = {"status": "COMPLETE", "results": [{"id": item["id"], "properties": {
                "firstname": "Pessoa", "lastname": "Sintetica"}} for item in body["inputs"]]}
        elif "/associations/" in path:
            result = {"results": [{"toObjectId": 300}] if "/emails?" in path else []}
        elif path.endswith("emails/batch/read"):
            result = {"status": "COMPLETE", "results": [self.activity]}
        elif path.endswith('meetings/search'):
            result = {'results': [], 'total': 0}
        else:
            raise AssertionError("unexpected endpoint")
        return 200, json.dumps(result).encode()


class ProjectionTests(unittest.TestCase):
    def test_actor_bracket_preserves_exact_touch_without_establishing_identity(self):
        for actor in ('YGOR','LOA'):
            record=task(title=f'[MX27-MTY][{actor}][E2] Empresa | EMAIL',
                        hs_task_sequence_id=None,hs_task_sequence_step_enrollment_id=None,
                        hs_task_sequence_step_enrollment_contact_id=None)
            row=project_task(record,associated_contact_ids=[],association_complete=True)
            self.assertEqual((row['campaign'],row['touch'],row['company'],row['channel']),
                             ('MX27-MTY','E2','Empresa','email'))
            self.assertEqual(row['contact_ids'],[])
            self.assertEqual(row['link_status'],'sem_vinculo')
            linked=project_task(record,associated_contact_ids=['123'],association_complete=True)
            self.assertEqual(linked['contact_ids'],['123'])
        row=project_task(task(title='[MX27-MTY][YGOR] Empresa | EMAIL',
            hs_task_sequence_id=None,hs_task_sequence_step_enrollment_id=None))
        self.assertEqual(row['touch'],'YGOR')

    def test_sequence_membership_survives_renamed_subject(self):
        row = project_task(task(title="BLOQUEADA - contato de teste - convite nao enviado"))
        self.assertEqual(row["campaign"], CAMPAIGN_ID)
        self.assertIsNone(row["touch"])
        self.assertIsNone(row["channel"])
        self.assertIsNone(row["receipt"])
        self.assertEqual(row["approval_status"], "unknown")
        self.assertEqual(row["contact_ids"], ["100"])

    def test_each_known_touch(self):
        for title, touch in [("LinkedIn: convite", "LI0"), ("LinkedIn: msg 1", "LI1"),
                             ("LinkedIn: msg 2", "LI2"), ("LinkedIn: msg 3", "LI3"), ("WhatsApp 1", "WA1")]:
            self.assertEqual(project_task(task(title=title))["touch"], touch)

    def test_missing_contact_never_resolved_from_name_or_generic_association(self):
        row = task(title="[OUTRA][LI1] Empresa | LINKEDIN", contact=None)
        row["associations"] = {"contacts": {"results": [{"id": "999"}]}}
        result = project_task(row)
        self.assertEqual(result["campaign"], CAMPAIGN_ID)
        self.assertEqual(result["contact_ids"], [])
        self.assertEqual(result["link_status"], "sem_vinculo")

    def test_title_alone_never_proves_br_enrollment(self):
        for title in ("LinkedIn: convite", "WhatsApp 1"):
            self.assertIsNone(project_task(task(title=title, hs_task_sequence_step_enrollment_id=None)))
            self.assertIsNone(project_task(task(title=title, hs_task_sequence_id="999")))
        manual = project_task(task(title="[BR-CADENCIA-V1][D0] Empresa | LINKEDIN",
                                   hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None))
        self.assertEqual(manual["campaign"], CAMPAIGN_ID)
        self.assertEqual(manual["source_kind"], "title")
        self.assertIsNone(manual["sequence_id"])
        self.assertIsNone(manual["enrollment_id"])
        self.assertEqual(manual["contact_ids"], [])
        self.assertEqual(manual["link_status"], "unverified")

    def test_bracket_campaign_and_owner_boundary(self):
        record = task(title="[MEXICO][LI1] Empresa | LINKEDIN", hs_task_sequence_id=None,
                      hs_task_sequence_step_enrollment_id=None)
        self.assertEqual(project_task(record)["campaign"], "MEXICO")
        self.assertEqual(project_task(record)["contact_ids"], [])
        record["properties"]["hubspot_owner_id"] = "999"
        self.assertIsNone(project_task(record))

    def test_completion_receipt_approval_and_metrics_are_separate(self):
        rows = [task(hs_task_status="COMPLETED"), task("2", contact=None, hs_task_status="WAITING")]
        result = project_snapshot(rows, collected_at="2026-10-21T13:00:00Z", coverage={
            'complete': True, 'partial': False, 'limitations': [],
            'sources': {'tasks': {'complete': True}, 'task_schema': {'complete': True}}})
        metrics = result["campaigns"][0]["metrics"]
        self.assertEqual(metrics["completed_status"], 1)
        self.assertIsNone(metrics["completed_with_receipt"])
        self.assertEqual(metrics["overdue"], 1)
        self.assertEqual(metrics["contacts"], 1)
        self.assertIsNone(metrics["replies"])
        self.assertIsNone(metrics["meetings"])
        self.assertIsNone(result["today"]["awaiting_approval"])
        self.assertIsNone(result["today"]["unreplied"])
        self.assertEqual(len(result["unlinked"]), 1)

    def test_html_controls_and_credentials_are_removed_without_truncating_text(self):
        long_text = "conteudo " * 4000
        result = plain_text('<p onclick="bad()">' + long_text + '</p><script>evil()</script>\x00Bearer secret')
        self.assertIn(long_text.strip(), result)
        for forbidden in ("onclick", "script", "evil", "\x00", "secret"):
            self.assertNotIn(forbidden, result)
        self.assertIn("[REDACTED]", result)

    def test_logged_sent_not_delivery_and_incoming_is_only_reply_evidence(self):
        for direction, expected in [("EMAIL", False), ("FORWARDED_EMAIL", False), ("INCOMING_EMAIL", True)]:
            event = project_activity("emails", {"id": "1", "properties": {
                "hs_email_direction": direction, "hs_email_status": "SENT"}})
            self.assertEqual(event["response_evidence"], expected)
            self.assertIsNone(event["receipt"])
            self.assertIsNone(event["campaign"])


class AdapterTests(unittest.TestCase):
    def make_console(self, provider=None, **kwargs):
        kwargs.setdefault("sleep", lambda seconds: None)
        return CadenceReadonlyConsole(lambda: "synthetic-test-token", request=provider or Provider(), **kwargs)

    def registry(self, *ids):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        registry = CampaignRegistry(Repository(Path(temp.name) / 'registry.db'))
        for ident in ids:
            registry.register({**config(), 'campaign_id': ident, 'platform': 'codex_local'}, 'ygor', 0)
        return registry

    def test_registered_empty_complete_tasks_has_zero_tasks_but_unknown_outcomes(self):
        provider = Provider()
        provider.tasks = [task(title='Unrecognized task', hs_task_sequence_id=None,
                               hs_task_sequence_step_enrollment_id=None)]
        registry = self.registry('REGISTERED')
        before = registry.repo.list_state('campaign_config')
        result = self.make_console(provider, campaign_registry=registry).snapshot()
        self.assertEqual(len(result['campaigns']), 1)
        group = result['campaigns'][0]
        self.assertEqual(group['id'], 'REGISTERED')
        self.assertEqual(group['registry'], {'id': 'REGISTERED', 'platform': 'codex_local', 'active': True})
        self.assertEqual(group['task_state'], 'empty')
        self.assertTrue(group['task_coverage_complete'])
        self.assertEqual(group['tasks'], [])
        self.assertEqual(group['metrics']['tasks'], 0)
        for field in ('completed_with_receipt', 'replies', 'meetings'):
            self.assertIsNone(group['metrics'][field])
        self.assertNotIn('synthetic-account', json.dumps(result))
        self.assertNotIn('provider_accounts', json.dumps(result))
        self.assertEqual(registry.repo.list_state('campaign_config'), before)

    def test_registered_campaigns_merge_exact_ids_only_and_preserve_task_groups(self):
        provider = Provider()
        provider.tasks.append(task('2', title='[REGISTERED-RUN][E1] Example | EMAIL',
                                   hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None))
        registry = self.registry(CAMPAIGN_ID, 'REGISTERED')
        registry.revoke('REGISTERED', 'ygor', 1)
        result = self.make_console(provider, campaign_registry=registry).snapshot()
        groups = {g['id']: g for g in result['campaigns']}
        self.assertEqual(set(groups), {CAMPAIGN_ID, 'REGISTERED', 'REGISTERED-RUN'})
        self.assertEqual(groups[CAMPAIGN_ID]['metrics']['tasks'], 1)
        self.assertEqual(groups[CAMPAIGN_ID]['task_state'], 'observed')
        self.assertEqual(groups['REGISTERED']['tasks'], [])
        self.assertFalse(groups['REGISTERED']['registry']['active'])
        self.assertIsNone(groups['REGISTERED-RUN']['registry'])

    def test_registered_empty_partial_tasks_or_schema_remains_unknown(self):
        for partial in ('pagination', 'schema'):
            provider = Provider()
            provider.tasks = []
            if partial == 'pagination':
                provider.pages = {'': {'results': [], 'paging': {'next': {'after': 'more'}}}}
            else:
                provider.schema.remove('hs_task_subject')
            with self.subTest(partial=partial):
                result = self.make_console(provider, campaign_registry=self.registry('REGISTERED'), max_pages=1).snapshot()
                group = result['campaigns'][0]
                self.assertEqual(group['task_state'], 'unknown')
                self.assertFalse(group['task_coverage_complete'])
                for field in ('tasks', 'contacts', 'pending', 'overdue', 'completed_status',
                              'completed_with_receipt', 'replies', 'meetings'):
                    self.assertIsNone(group['metrics'][field])
                self.assertEqual(group['metrics']['observed_tasks'], 0)

    def test_partial_nonempty_completed_page_never_implies_zero_pending_or_overdue(self):
        for partial in ('pagination', 'schema'):
            provider = Provider()
            provider.tasks = [task(hs_task_status='COMPLETED')]
            if partial == 'pagination':
                provider.pages = {'': {'results': provider.tasks, 'total': 2,
                                      'paging': {'next': {'after': 'remaining-unknown'}}}}
            else:
                provider.schema.remove('hs_task_subject')
            with self.subTest(partial=partial):
                result = self.make_console(provider, max_pages=1).snapshot()
                group = result['campaigns'][0]
                self.assertEqual(group['task_state'], 'observed')
                self.assertFalse(group['task_coverage_complete'])
                self.assertEqual(len(group['tasks']), 1)
                self.assertEqual(group['tasks'][0]['status'], 'COMPLETED')
                for field, count in {'contacts': 1, 'tasks': 1, 'completed_status': 1,
                                     'pending': 0, 'overdue': 0, 'unknown_status': 0}.items():
                    self.assertEqual(group['metrics']['observed_' + field], count)
                    self.assertIsNone(group['metrics'][field])
                for field in ('completed_with_receipt', 'replies', 'meetings'):
                    self.assertIsNone(group['metrics'][field])

    def test_registered_campaign_survives_provider_failure_without_false_empty(self):
        calls = []
        def forbidden(*args):
            calls.append(args)
            return 403, b'{"secret":"do not expose"}'
        result = self.make_console(forbidden, campaign_registry=self.registry('REGISTERED')).snapshot()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['campaigns'][0]['task_state'], 'unknown')
        self.assertTrue(result['coverage']['partial'])
        self.assertNotIn('do not expose', json.dumps(result))

    def test_corrupt_registry_is_visible_partial_but_preserves_task_campaigns(self):
        registry = self.registry('REGISTERED')
        with registry.repo.transaction() as con:
            con.execute("UPDATE thunder_state SET payload='invalid-json' WHERE kind='campaign_config'")
        result = self.make_console(campaign_registry=registry).snapshot()
        self.assertEqual([g['id'] for g in result['campaigns']], [CAMPAIGN_ID])
        self.assertFalse(result['coverage']['sources']['campaign_registry']['complete'])
        self.assertIn('campaign_registry:unavailable', result['coverage']['limitations'])

    def test_endpoint_enforcement_precedes_credentials(self):
        def no_credential():
            self.fail("credential called for blocked endpoint")
        session = HubSpotReadonlyAdapter(no_credential).session()
        for method, path in [("POST", "/crm/v3/objects/tasks"), ("PATCH", "/crm/v3/objects/tasks/1"),
                             ("DELETE", "/crm/v3/objects/contacts/1"),
                             ("POST", "/automation/v4/sequences/enrollments"),
                             ("GET", "https://evil.test/"), ("POST", "/crm/v3/objects/emails/batch/create")]:
            with self.assertRaisesRegex(CadenceReadonlyError, "readonly_endpoint_blocked"):
                session.call(method, path)

    def test_wrong_portal_and_owner_fail_closed(self):
        for attribute in ("portal", "owner"):
            provider = Provider()
            setattr(provider, attribute, "999")
            with self.assertRaisesRegex(CadenceReadonlyError, attribute + "_mismatch"):
                self.make_console(provider).snapshot()
            self.assertFalse(any(path.endswith("search") for _, path, _ in provider.calls))

    def test_complete_pagination_dedupe_and_hostile_link_ignored(self):
        provider = Provider()
        provider.pages = {"": {"results": [task()], "total": 2, "paging": {"next": {
            "after": "next", "link": "https://evil.test/steal"}}},
            "next": {"results": [task(), task("2")], "total": 2}}
        result = self.make_console(provider).snapshot()
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["campaigns"][0]["metrics"]["tasks"], 2)
        self.assertFalse(any("evil" in path for _, path, _ in provider.calls))

    def test_page_cap_and_cycle_are_visible_partial(self):
        provider = Provider()
        page = {"results": [task()], "total": 2, "paging": {"next": {"after": "next"}}}
        provider.pages = {"": page, "next": page}
        for max_pages in (1, 3):
            result = self.make_console(provider, max_pages=max_pages).snapshot()
            self.assertTrue(result["coverage"]["partial"])
            self.assertEqual(result["campaigns"][0]["metrics"]["observed_tasks"], 1)
            self.assertIsNone(result["campaigns"][0]["metrics"]["tasks"])

    def test_missing_schema_and_total_mismatch_do_not_claim_complete(self):
        provider = Provider()
        provider.schema.remove("hs_task_sequence_id")
        result = self.make_console(provider).snapshot()
        self.assertFalse(result["coverage"]["complete"])
        provider = Provider()
        provider.pages = {"": {"results": [task()], "total": 10}}
        result = self.make_console(provider).snapshot()
        self.assertIn("tasks:provider_total_mismatch", result["coverage"]["limitations"])

    def test_missing_membership_schema_never_proves_contact_absence(self):
        for field in ("hs_task_sequence_id", "hs_task_sequence_step_enrollment_contact_id",
                      "hs_task_sequence_step_enrollment_id", "hs_task_subject"):
            with self.subTest(field=field):
                provider = Provider()
                provider.schema.remove(field)
                provider.tasks[0]["properties"].pop(field)
                console = self.make_console(provider)
                snapshot = console.snapshot()
                self.assertFalse(snapshot["coverage"]["sources"]["task_schema"]["complete"])
                if field == "hs_task_sequence_step_enrollment_contact_id":
                    self.assertEqual(snapshot["unlinked"], [])
                    self.assertEqual(len(snapshot["unverified"]), 1)
                    self.assertIsNone(snapshot["campaigns"][0]["metrics"]["contacts"])
                with self.assertRaisesRegex(CadenceReadonlyError, "contact_membership_unverified"):
                    console.timeline("999")

    def test_budget_and_provider_errors_are_sanitized(self):
        def failing(*args):
            raise RuntimeError("synthetic-test-token private-provider-body")
        with self.assertRaises(CadenceReadonlyError) as caught:
            self.make_console(failing).snapshot()
        self.assertNotIn("synthetic-test-token", str(caught.exception))
        self.assertNotIn("private-provider-body", str(caught.exception))
        with self.assertRaisesRegex(CadenceReadonlyError, "read_budget_exhausted"):
            self.make_console(max_requests=1).snapshot()

    def test_forbidden_and_rate_limited_reads_are_not_retried(self):
        for status in (403, 429, 500, 302):
            calls = []
            def http_error(*args):
                calls.append(args[0])
                return status, b'{"message":"synthetic-test-token"}'
            with self.assertRaisesRegex(CadenceReadonlyError, "provider_http_error") as caught:
                self.make_console(http_error).snapshot()
            self.assertEqual(len(calls), 1)
            self.assertNotIn("synthetic-test-token", str(caught.exception))

    def test_cache_lock_ttl_and_mutation_isolation(self):
        provider, monotonic = Provider(), [0]
        console = self.make_console(provider, monotonic=lambda: monotonic[0])
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: console.snapshot(), range(8)))
        self.assertEqual(sum(path.endswith("search") for _, path, _ in provider.calls), 1)
        results[0]["campaigns"].clear()
        self.assertTrue(console.snapshot()["campaigns"])
        monotonic[0] = 60
        console.snapshot()
        self.assertEqual(sum(path.endswith("search") for _, path, _ in provider.calls), 2)

    def test_cache_expires_from_request_start_after_slow_read(self):
        provider, tick = Provider(), [0.0]
        def slow_request(*args):
            if args[1].endswith("tasks/search"):
                tick[0] += 14
            return provider(*args)
        console = self.make_console(slow_request, monotonic=lambda: tick[0], clock=lambda: 1792587600 + tick[0])
        first = console.snapshot()
        self.assertEqual(tick[0], 14)
        tick[0] = 59
        cached = console.snapshot()
        self.assertEqual(cached["collected_at"], first["collected_at"])
        self.assertEqual(sum(path.endswith("search") for _, path, _ in provider.calls), 1)
        tick[0] = 60
        refreshed = console.snapshot()
        self.assertNotEqual(refreshed["collected_at"], first["collected_at"])
        self.assertEqual(sum(path.endswith("search") for _, path, _ in provider.calls), 2)

    def test_timeline_real_association_contract_and_context_attribution(self):
        provider = Provider()
        result = self.make_console(provider).timeline("100", CAMPAIGN_ID)
        self.assertTrue(result["ok"])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(len(result["events"]), 2)
        email = next(event for event in result["events"] if event["type"] == "email")
        self.assertEqual(email["text"], "Obrigado")
        self.assertIsNone(email["campaign"])
        self.assertEqual(result["metrics"]["replies"], 1)
        body = next(body for _, path, body in provider.calls if path.endswith("emails/batch/read"))
        self.assertEqual(body["inputs"], [{"id": "300"}])

    def test_timeline_does_not_expose_arbitrary_contact(self):
        provider = Provider()
        with self.assertRaises(LookupError):
            self.make_console(provider).timeline("999")
        self.assertFalse(any("associations" in path for _, path, _ in provider.calls))

    def test_batch_with_missing_or_unassociated_activity_is_partial(self):
        provider = Provider()
        provider.activity["id"] = "999"
        result = self.make_console(provider).timeline("100")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(len(result["events"]), 1)

    def test_invalid_input_rejected_before_io(self):
        provider = Provider()
        console = self.make_console(provider)
        for contact in ("../100", "", "0", True, "100?token=foo"):
            with self.assertRaises(CadenceReadonlyError):
                console.timeline(contact)
        with self.assertRaises(CadenceReadonlyError):
            console.timeline("100", "<script>")
        self.assertEqual(provider.calls, [])

    def test_bracket_associations_and_names_work_in_snapshot_and_timeline(self):
        provider = Provider()
        provider.tasks = [task(title="[MEXICO][LI1] Empresa | LINKEDIN", contact=None,
                               hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None)]
        provider.task_links = {"1": ["700"]}
        console = self.make_console(provider)
        snapshot = console.snapshot()
        row = snapshot["campaigns"][0]["tasks"][0]
        self.assertEqual(row["contact_ids"], ["700"])
        self.assertEqual(row["contact_name"], "Pessoa Sintetica")
        self.assertEqual(row["contact_names"], {"700": "Pessoa Sintetica"})
        self.assertEqual(row["contacts"], [{"id": "700", "name": "Pessoa Sintetica"}])
        result = console.timeline("700", "MEXICO")
        self.assertTrue(result["ok"])
        self.assertEqual(result["contact_name"], "Pessoa Sintetica")
        self.assertTrue(any(event["type"] == "task" for event in result["events"]))

    def test_manual_br_campaign_uses_associations_not_enrollment_guess(self):
        provider = Provider()
        provider.tasks = [task(title="[BR-CADENCIA-V1][D0] Empresa | EMAIL", contact="999",
                               hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None)]
        provider.task_links = {"1": ["700"]}
        console = self.make_console(provider)
        row = console.snapshot()["campaigns"][0]["tasks"][0]
        self.assertEqual(row["campaign"], CAMPAIGN_ID)
        self.assertEqual(row["source_kind"], "title")
        self.assertEqual(row["contact_ids"], ["700"])
        self.assertIsNone(row["sequence_id"])
        self.assertEqual(row["touch"], "D0")
        self.assertEqual(row["channel"], "email")
        self.assertTrue(console.timeline("700", CAMPAIGN_ID)["ok"])
        with self.assertRaises(LookupError):
            console.timeline("999", CAMPAIGN_ID)

    def test_all_2845_tasks_not_first_page_and_batch_associations(self):
        provider = Provider()
        all_tasks = [task(str(i + 1), title="unrelated", hs_task_sequence_id=None,
                          hs_task_sequence_step_enrollment_id=None) for i in range(2845)]
        for i in range(2500, 2845):
            all_tasks[i]["properties"]["hs_task_subject"] = "[MEXICO][LI1] Empresa | LINKEDIN"
            provider.task_links[str(i + 1)] = ["700"]
        provider.pages = {}
        for offset in range(0, 2845, 200):
            page = {"results": all_tasks[offset:offset + 200], "total": 2845}
            if offset + 200 < 2845:
                page["paging"] = {"next": {"after": str(offset + 200)}}
            provider.pages[str(offset) if offset else ""] = page
        result = self.make_console(provider).snapshot()
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["coverage"]["sources"]["tasks"]["records"], 2845)
        self.assertEqual(result["campaigns"][0]["metrics"]["tasks"], 345)
        self.assertEqual(len(result["unlinked"]), 0)
        batches = [body for _, path, body in provider.calls if path == "/crm/v4/associations/tasks/contacts/batch/read"]
        self.assertEqual([len(body["inputs"]) for body in batches], [100, 100, 100, 45])
        self.assertTrue(all(body["limit"] == 200 for _, path, body in provider.calls if path.endswith("tasks/search")))
        self.assertFalse(any("/objects/contacts/" in path and "/associations/" in path for _, path, _ in provider.calls))

    def test_association_per_result_pagination_and_missing_results(self):
        provider = Provider()
        provider.tasks = [task(title="[MEXICO][LI1] Empresa | LINKEDIN", hs_task_sequence_id=None,
                               hs_task_sequence_step_enrollment_id=None)]
        seen = []
        def paginated(method, path, body, headers, timeout):
            if path == "/crm/v4/associations/tasks/contacts/batch/read":
                seen.append(body)
                row = {"from": {"id": "1"}, "to": [{"toObjectId": "700"}]}
                if "after" not in body["inputs"][0]:
                    row["paging"] = {"next": {"after": "next", "link": "https://evil.test"}}
                else:
                    row["to"] = [{"toObjectId": "701"}]
                return 200, json.dumps({"status": "COMPLETE", "results": [row]}).encode()
            return provider(method, path, body, headers, timeout)
        result = self.make_console(paginated).snapshot()
        row = result["campaigns"][0]["tasks"][0]
        self.assertEqual(row["contact_ids"], ["700", "701"])
        self.assertIsNone(row["contact_name"])
        self.assertEqual(len(row["contacts"]), 2)
        self.assertEqual(seen[1]["inputs"], [{"id": "1", "after": "next"}])
        self.assertTrue(result["coverage"]["complete"])

    def test_missing_association_batch_result_degrades_not_false_notfound(self):
        provider = Provider()
        provider.tasks = [task(title="[MEXICO][LI1] Empresa | LINKEDIN", hs_task_sequence_id=None,
                               hs_task_sequence_step_enrollment_id=None)]
        def missing(method, path, body, headers, timeout):
            if path == "/crm/v4/associations/tasks/contacts/batch/read":
                return 200, b'{"status":"COMPLETE","results":[]}'
            return provider(method, path, body, headers, timeout)
        console = self.make_console(missing)
        snapshot = console.snapshot()
        self.assertTrue(snapshot["coverage"]["partial"])
        self.assertEqual(snapshot["unlinked"], [])
        self.assertEqual(snapshot["unverified"][0]["link_status"], "unverified")
        self.assertIsNone(snapshot["campaigns"][0]["metrics"]["contacts"])
        with self.assertRaisesRegex(CadenceReadonlyError, "contact_membership_unverified"):
            console.timeline("700")

    def test_proven_empty_association_is_unlinked(self):
        provider = Provider()
        provider.tasks = [task(title="[MEXICO][LI1] Empresa | LINKEDIN", hs_task_sequence_id=None,
                               hs_task_sequence_step_enrollment_id=None)]
        result = self.make_console(provider).snapshot()
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["unverified"], [])
        self.assertEqual(result["unlinked"][0]["link_status"], "sem_vinculo")

    def test_association_budget_failure_never_mislabels_unlinked(self):
        provider = Provider()
        provider.tasks = [task(str(i + 1), title="[MEXICO][LI1] Empresa | LINKEDIN",
                               hs_task_sequence_id=None, hs_task_sequence_step_enrollment_id=None) for i in range(350)]
        provider.task_links = {str(i + 1): ["700"] for i in range(350)}
        result = self.make_console(provider, max_requests=5).snapshot()
        self.assertEqual(result["unlinked"], [])
        self.assertEqual(len(result["unverified"]), 250)
        self.assertLessEqual(len(result["coverage"]["limitations"]), 10)

    def test_pending_association_batch_empty_result_is_unverified(self):
        provider = Provider()
        provider.tasks = [task(title="[MEXICO][LI1] Empresa | LINKEDIN", hs_task_sequence_id=None,
                               hs_task_sequence_step_enrollment_id=None)]
        def pending(method, path, body, headers, timeout):
            status, raw = provider(method, path, body, headers, timeout)
            if path == "/crm/v4/associations/tasks/contacts/batch/read":
                result = json.loads(raw)
                result["status"] = "PENDING"
                return status, json.dumps(result).encode()
            return status, raw
        result = self.make_console(pending).snapshot()
        self.assertEqual(result["unlinked"], [])
        self.assertEqual(len(result["unverified"]), 1)

    def test_contact_names_permission_failure_preserves_identity(self):
        provider = Provider()
        def forbidden(method, path, body, headers, timeout):
            if path == "/crm/v3/objects/contacts/batch/read":
                return 403, b'{}'
            return provider(method, path, body, headers, timeout)
        result = self.make_console(forbidden).snapshot()
        row = result["campaigns"][0]["tasks"][0]
        self.assertEqual(row["contact_ids"], ["100"])
        self.assertIsNone(row["contact_name"])
        self.assertEqual(row["link_status"], "linked")
        self.assertTrue(result["coverage"]["partial"])

    def test_missing_activity_permission_is_unknown_not_zero(self):
        provider = Provider()
        def forbidden(method, path, body, headers, timeout):
            if path in {"/crm/v3/properties/emails", "/crm/v3/properties/meetings"}:
                return 403, b'{"message":"not for display"}'
            return provider(method, path, body, headers, timeout)
        result = self.make_console(forbidden).timeline("100")
        self.assertIsNone(result["metrics"]["replies"])
        self.assertIsNone(result["metrics"]["meetings"])
        self.assertTrue(result["coverage"]["partial"])

    def test_search_pacing(self):
        provider, tick, starts = Provider(), [0.0], []
        provider.pages = {"": {"results": [task()], "total": 2, "paging": {"next": {"after": "next"}}},
                          "next": {"results": [task("2")], "total": 2}}
        def request(*args):
            if args[1].endswith("tasks/search"):
                starts.append(tick[0])
            return provider(*args)
        def sleep(seconds):
            tick[0] += seconds
        self.make_console(request, monotonic=lambda: tick[0], sleep=sleep).snapshot()
        self.assertGreaterEqual(starts[1] - starts[0], .21)


if __name__ == "__main__":
    unittest.main()
