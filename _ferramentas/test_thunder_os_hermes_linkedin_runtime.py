import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from thunder_os.db import Repository
from thunder_os.hermes_linkedin_cadence import HermesLinkedInCadence, STEP_CONTRACT
from thunder_os.hermes_linkedin_runtime import SyntheticCadenceApp, SyntheticTasks, build, installed_revision, load_config, main, run_once


STAMP = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc).timestamp()


def plan(marker):
    action=STEP_CONTRACT[marker][1]
    return {"account_id":"cs-account","account_public_id":"ygorbuenobr","action":action,
        "recipient_public_id":"lead-public","recipient_profile_urn":"urn:li:fsd_profile:lead123",
        "channel":"linkedin","messaging_channel":"linkedin_inbox","content":"copy "+marker,
        "conversation_urn":None if action=="invite" else "urn:li:msg_conversation:(lead123,owner123)"}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.db=self.root/"state.sqlite"
        self.revision=self.root/"REVISION";self.revision.write_text("fixture-revision\n")
        self.repo=Repository(self.db)
        self.repo.put_state('agent_control', 'global', {'enabled': True, 'paused': False})
        self.fixture={"clock":STAMP,"relationship_status":"CONNECTED",
            "provider_result":{"status":"sent","receipt_ref":"connectsafely:synthetic-1","retry_write":False},
            "task":{"id":"118775080824","status":"NOT_STARTED","due_at":"2026-10-19T14:00:00+00:00",
                    "subject":"LinkedIn: msg 1","body":"cad_li_1","contact_ids":["255570819657"],
                    "contact_status":"em_cadencia","cadence_id":"316669709"}}
        self.config={"version":1,"mode":"synthetic","execution_enabled":True,"repository":str(self.db),
            "source_revision":"fixture-revision","poll_minutes":30,"max_tasks_per_run":5,"timezone":"America/Sao_Paulo",
            "source_revision_file":str(self.revision),
            "business_hours":{"start":"08:00","end":"19:00","weekdays":[0,1,2,3,4]},"daily_cap":30,
            "action_spacing_seconds":{"min":30,"max":90},
            "kill_switch":"thunder_state:delegation/hermes.enabled",
            "synthetic":self.fixture}
        self.path=self.root/"runtime.json";self.path.write_text(json.dumps(self.config))
        self.repo.put_state("delegation","hermes",{"enabled":True,"scopes":["linkedin_cadence"],
            "daily_limit":30,"actor":"ygor","audit_actor":"hermes (delegado)"})
        app=SyntheticCadenceApp(self.repo,self.fixture);tasks=SyntheticTasks(self.fixture)
        runner=HermesLinkedInCadence(self.repo,tasks,app,clock=lambda:STAMP)
        approved=runner.approve({"id":"approval-1","hubspot_contact_id":"255570819657","cadence_id":"316669709",
            "expires_at":"2026-10-20T15:00:00+00:00","steps":[
                {"marker":marker,"subject":value[0],"plan":plan(marker)} for marker,value in STEP_CONTRACT.items()]},"ygor")
        self.repo.append_state('linkedin_cadence_task_binding', '118775080824', {
            'task_id':'118775080824','approval_id':approved['id'],'approval_version':approved['version'],
            'review_hash':approved['review_hash'],'marker':'cad_li_1','contact_id':'255570819657',
            'cadence_id':'316669709','actor':'ygor'})

    def tearDown(self):self.temp.cleanup()

    def test_runtime_config_is_explicit_bounded_and_secret_free(self):
        self.assertEqual("synthetic",load_config(self.path)["mode"])
        mutations=(
            {"repository":"relative.sqlite"},{"poll_minutes":1},{"max_tasks_per_run":31},{"daily_cap":31},
            {"source_revision_file":"relative"},
            {"action_spacing_seconds":{"min":29,"max":90}},
            {"mode":"disabled","execution_enabled":True},{"api_key":"secret"},
            {"mode":"live","synthetic":None,"hubspot":{"portal_id":"51412240","owner_id":"92901049","token":"secret"},
             "connectsafely_config":"relative.json"},
        )
        for index,mutation in enumerate(mutations):
            value={**self.config,**mutation};target=self.root/f"bad-{index}.json";target.write_text(json.dumps(value))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):load_config(target)
        example=Path("runtime/hermes-linkedin-cadence.example.json")
        self.assertEqual("disabled",load_config(example.resolve())["mode"])
        live=load_config(Path("runtime/hermes-linkedin-cadence.live.example.json").resolve())
        self.assertEqual("51412240",live["hubspot"]["portal_id"])
        self.assertFalse(live["execution_enabled"])

    def test_synthetic_once_waits_for_human_without_network(self):
        output=io.StringIO()
        def synthetic_runner(*args, **kwargs):
            runner = HermesLinkedInCadence(*args, **kwargs)
            runner.identity_verifier = lambda contact, plan: {
                'contact': {'id': contact, 'linkedin_url': 'https://www.linkedin.com/in/' + plan['recipient_public_id'],
                            'name': 'Synthetic Lead', 'company': 'Synthetic Company'},
                'profile': {'account_id': plan['account_id'], 'profile_urn': plan['recipient_profile_urn'],
                            'linkedin_url': 'https://www.linkedin.com/in/' + plan['recipient_public_id'],
                            'name': 'Synthetic Lead', 'company': 'Synthetic Company'}}
            return runner
        with patch("socket.create_connection",side_effect=AssertionError("network forbidden")), \
             patch('thunder_os.hermes_linkedin_runtime.HermesLinkedInCadence', side_effect=synthetic_runner), \
             redirect_stdout(output):
            code=main(["--config",str(self.path),"--once"])
        self.assertEqual(0,code);summary=json.loads(output.getvalue())
        self.assertEqual("human_confirmation_required",summary["processed"][0]["status"])
        self.assertEqual("fixture-revision",summary["source_revision"])
        self.assertNotIn("secret",output.getvalue().lower())
        stored=Repository(self.db).get_state("linkedin_cadence_task","118775080824")
        self.assertIsNone(stored)

    def test_live_runtime_rejects_account_identity_mismatch_without_provider_io(self):
        from thunder_os.hermes_linkedin_runtime import build
        configured = {'account_id':'fixture-account', 'public_id':'fixture-owner'}
        cfg = {**self.config, 'mode':'live', 'connectsafely_config':str(self.path),
               'connectsafely_identity':configured,
               'hubspot':{'portal_id':'51412240', 'owner_id':'92901049', 'token_env':'HUBSPOT_ACCESS_TOKEN'}}
        for changed in ({'account_id':'other-account'}, {'public_id':'other-owner'}):
            provider = SimpleNamespace(account_id=configured['account_id'],
                                       expected_public_id=configured['public_id'])
            for key,value in changed.items():
                setattr(provider, 'expected_public_id' if key == 'public_id' else key, value)
            app = SimpleNamespace(provider=provider)
            with self.subTest(changed=changed), \
                 patch.dict(os.environ, {'THUNDER_EXTERNAL_EFFECTS_ENABLED':'true'}, clear=True), \
                 patch('socket.create_connection', side_effect=AssertionError('network forbidden')), \
                 patch('thunder_os.hermes_linkedin_runtime.load_cs_config', return_value={'live':configured}), \
                 patch('thunder_os.hermes_linkedin_runtime.build_connectsafely', return_value=SimpleNamespace(application=app)), \
                 patch('thunder_os.hermes_linkedin_runtime.HubSpotTasks') as tasks:
                with self.assertRaisesRegex(ValueError, 'connectsafely_identity_mismatch'):
                    build(cfg)
                tasks.assert_not_called()
        self.assertEqual([], self.repo.list_state('connectsafely_job'))

    def test_live_runtime_attaches_campaign_sheet_store_to_direct_console(self):
        from thunder_os.hermes_linkedin_runtime import build
        from thunder_os.knowledge import KnowledgeStore
        configured = {'account_id':'fixture-account', 'public_id':'fixture-owner'}
        root = Path(self.temp.name) / 'brain'; root.mkdir()
        base = {**self.config, 'mode':'live', 'connectsafely_config':str(self.path),
                'connectsafely_identity':configured,
                'hubspot':{'portal_id':'51412240', 'owner_id':'92901049', 'token_env':'HUBSPOT_ACCESS_TOKEN'}}
        for cfg, expected in (({**base, 'campaign_sheet_root': str(root)}, True), (base, False)):
            direct = SimpleNamespace(campaign_sheet_store=None)
            def compose(_cfg, repository):
                return SimpleNamespace(application=SimpleNamespace(
                    provider=SimpleNamespace(account_id='fixture-account', expected_public_id='fixture-owner'),
                    direct=direct, repo=repository, delegated_task_guard=None))
            with self.subTest(expected=expected), \
                 patch.dict(os.environ, {'THUNDER_EXTERNAL_EFFECTS_ENABLED':'true'}, clear=True), \
                 patch('thunder_os.hermes_linkedin_runtime.load_cs_config', return_value={'live':configured}), \
                 patch('thunder_os.hermes_linkedin_runtime.build_connectsafely', side_effect=compose), \
                 patch('thunder_os.hermes_linkedin_runtime.HubSpotTasks'):
                build(cfg)
            self.assertEqual(expected, isinstance(direct.campaign_sheet_store, KnowledgeStore))

    def test_synthetic_runtime_without_identity_source_fails_closed(self):
        output=io.StringIO()
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')), redirect_stdout(output):
            self.assertEqual(0, main(['--config', str(self.path), '--once']))
        outcome=json.loads(output.getvalue())['processed'][0]
        self.assertEqual('blocked', outcome['status'])
        self.assertEqual('contact_profile_identity_unverified', outcome['error_code'])

    def test_runtime_passes_only_persisted_human_confirmation_to_runner(self):
        cfg=load_config(self.path)
        self.repo.append_state("linkedin_cadence_human_confirmation", "task-1", {
            "task_id":"task-1", "preview_id":"preview-1", "campaign_approval_id":"approval-1", "actor":"ygor"})
        runner=Mock();runner.repository=self.repo
        tasks=Mock();tasks.due.return_value=["task-1", "task-2"]
        with patch("thunder_os.hermes_linkedin_runtime.build",return_value=(runner,tasks,STAMP)), \
                patch("thunder_os.hermes_linkedin_runtime.time.sleep"):
            run_once(cfg)
        self.assertEqual([unittest.mock.call("task-1", preview_id="preview-1", campaign_approval_id="approval-1"),
                          unittest.mock.call("task-2")], runner.run_task.call_args_list)

    def test_revision_comes_from_release_manifest(self):
        manifest=self.root/"thunder-os-manifest.json"
        manifest.write_text(json.dumps({"schema_version":1,"source_sha":"fixture-revision","files":{}}))
        self.assertEqual("fixture-revision",installed_revision(str(manifest)))
        manifest.write_text(json.dumps({"schema_version":1,"source_sha":"other"}))
        cfg=load_config(self.path);cfg["source_revision_file"]=str(manifest)
        with self.assertRaisesRegex(ValueError,"source_revision_mismatch"):build(cfg)
        for broken in ('{"schema_version":1}','not json','{"source_sha":""}'):
            manifest.write_text(broken)
            with self.assertRaisesRegex(ValueError,"source_revision_unavailable"):installed_revision(str(manifest))
        with self.assertRaisesRegex(ValueError,"source_revision_unavailable"):installed_revision(str(self.root/"missing.json"))

    def test_runtime_binds_revision_and_spaces_multiple_tasks(self):
        cfg=load_config(self.path)
        self.revision.write_text("different\n")
        output=io.StringIO()
        with redirect_stdout(output):self.assertEqual(2,main(["--config",str(self.path),"--once"]))
        self.assertEqual("source_revision_mismatch",json.loads(output.getvalue())["error_code"])
        runner=Mock();runner.repository=self.repo;runner.run_task.side_effect=lambda task_id:{"task_id":task_id,"status":"completed"}
        tasks=Mock();tasks.due.return_value=["task-1","task-2"]
        with patch("thunder_os.hermes_linkedin_runtime.build",return_value=(runner,tasks,STAMP)), \
                patch("thunder_os.hermes_linkedin_runtime.secrets.randbelow",return_value=7), \
                patch("thunder_os.hermes_linkedin_runtime.time.sleep") as sleep:
            result=run_once(cfg)
        self.assertEqual(2,len(result["processed"]));sleep.assert_called_once_with(37)

    def test_pending_rotation_is_fair_to_due_and_confirmed_with_one_slot(self):
        cfg={**load_config(self.path), 'max_tasks_per_run': 1}
        for index, status in enumerate(('uncertain', 'uncertain', 'provider_confirmed')):
            self.repo.put_state('linkedin_cadence_task', f'pending-{index}', {'status':status})
        runner=Mock(); runner.repository=self.repo
        runner.run_task.side_effect=lambda task_id, **kwargs: {'task_id':task_id, 'status':'pending'}
        tasks=Mock(); tasks.due.return_value=['due-1']
        with patch('thunder_os.hermes_linkedin_runtime.build', return_value=(runner,tasks,STAMP)):
            rounds=[run_once(cfg)['processed'][0]['task_id'] for _ in range(6)]
        self.assertEqual(['pending-0', 'due-1', 'pending-1', 'due-1', 'pending-2', 'due-1'], rounds)
        self.assertEqual(3, tasks.due.call_count)

    def test_pending_due_search_advances_past_full_page_of_pending(self):
        cfg={**load_config(self.path), 'max_tasks_per_run': 1}
        self.repo.put_state('linkedin_cadence_task','pending-0',{'status':'uncertain'})
        runner=Mock();runner.repository=self.repo
        runner.run_task.side_effect=lambda task_id, **kwargs: {'task_id':task_id,'status':'pending'}
        class PagedTasks:
            def __init__(self): self.calls=[]
            def due_page(self, *, limit, after):
                self.calls.append((limit,after))
                if after is None: return {'ids':['pending-0'],'next_after':'page-2'}
                return {'ids':['due-1'],'next_after':None}
        tasks=PagedTasks()
        with patch('thunder_os.hermes_linkedin_runtime.build',return_value=(runner,tasks,STAMP)):
            rounds=[run_once(cfg)['processed'][0]['task_id'] for _ in range(3)]
        self.assertEqual(['pending-0','pending-0','due-1'], rounds)
        self.assertEqual([(1,None),(1,'page-2')], tasks.calls)

    def test_pending_due_pages_do_not_skip_unselected_ids_with_two_slots(self):
        cfg={**load_config(self.path), 'max_tasks_per_run': 2}
        self.repo.put_state('linkedin_cadence_task','pending-0',{'status':'uncertain'})
        runner=Mock();runner.repository=self.repo
        runner.run_task.side_effect=lambda task_id, **kwargs: {'task_id':task_id,'status':'pending'}
        class PagedTasks:
            def __init__(self): self.calls=[]
            def due_page(self, *, limit, after):
                self.calls.append((limit,after))
                ids=['due-1','due-2','due-3','due-4']
                start=int(after or 0); end=min(start+limit,len(ids))
                return {'ids':ids[start:end], 'next_after':str(end) if end < len(ids) else None}
        tasks=PagedTasks()
        with patch('thunder_os.hermes_linkedin_runtime.build',return_value=(runner,tasks,STAMP)), \
             patch('thunder_os.hermes_linkedin_runtime.time.sleep'):
            rounds=[run_once(cfg)['processed'] for _ in range(4)]
        self.assertEqual(['due-1','due-2','due-3','due-4'],
                         [row[1]['task_id'] for row in rounds])
        self.assertEqual([(1,None),(1,'1'),(1,'2'),(1,'3')],tasks.calls)

    def test_pending_completed_hubspot_task_retries_after_patch_readback_failure(self):
        cfg=load_config(self.path)
        task_id='118775080824'
        contract={'subject':'LinkedIn: msg 1','body':'cad_li_1','contact_ids':['255570819657'],
                  'content':'Mensagem','sent_at':'nao_comprovado'}
        self.repo.put_state('linkedin_cadence_task', task_id, {'task_id':task_id,'status':'provider_confirmed',
            'job_id':'job-1','receipt_ref':'connectsafely:fixture','task_contract':contract})
        tasks=Mock(); tasks.due.return_value=[]
        calls=[]
        def complete(_task, _receipt, _contract):
            calls.append('already_applied')
            if len(calls) == 1: raise RuntimeError('readback unavailable')
        tasks.complete.side_effect=complete
        app=Mock(); app.repo=self.repo
        runner=HermesLinkedInCadence(self.repo,tasks,app,clock=lambda:STAMP)
        with patch('thunder_os.hermes_linkedin_runtime.build', return_value=(runner,tasks,STAMP)):
            first=run_once(cfg)['processed'][0]
            second=run_once(cfg)['processed'][0]
        self.assertEqual('blocked', first['status'])
        self.assertEqual('completed', second['status'])
        self.assertEqual(2,len(calls))
        self.assertEqual(1,len(self.repo.list_state('linkedin_cadence_audit')))
        app.process_one.assert_not_called()


if __name__=="__main__":unittest.main()
