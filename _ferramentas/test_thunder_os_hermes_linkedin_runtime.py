import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.db import Repository
from thunder_os.hermes_linkedin_cadence import HermesLinkedInCadence, STEP_CONTRACT
from thunder_os.hermes_linkedin_runtime import SyntheticCadenceApp, SyntheticTasks, load_config, main


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
        self.repo=Repository(self.db)
        self.fixture={"clock":STAMP,"relationship_status":"CONNECTED",
            "provider_result":{"status":"sent","receipt_ref":"connectsafely:synthetic-1","retry_write":False},
            "task":{"id":"118775080824","status":"NOT_STARTED","due_at":"2026-10-19T14:00:00+00:00",
                    "subject":"LinkedIn: msg 1","body":"cad_li_1","contact_ids":["255570819657"]}}
        self.config={"version":1,"mode":"synthetic","execution_enabled":True,"repository":str(self.db),
            "source_revision":"fixture-revision","poll_minutes":30,"max_tasks_per_run":5,"timezone":"America/Sao_Paulo",
            "business_hours":{"start":"08:00","end":"18:00","weekdays":[0,1,2,3,4]},"daily_cap":30,
            "kill_switch":"thunder_state:delegation/hermes.enabled",
            "synthetic":self.fixture}
        self.path=self.root/"runtime.json";self.path.write_text(json.dumps(self.config))
        self.repo.put_state("delegation","hermes",{"enabled":True,"scopes":["linkedin_cadence"],
            "daily_limit":30,"actor":"ygor","audit_actor":"hermes (delegado)"})
        app=SyntheticCadenceApp(self.repo,self.fixture);tasks=SyntheticTasks(self.fixture)
        runner=HermesLinkedInCadence(self.repo,tasks,app,clock=lambda:STAMP)
        runner.approve({"id":"approval-1","hubspot_contact_id":"255570819657","cadence_id":"316669709",
            "expires_at":"2026-10-20T15:00:00+00:00","steps":[
                {"marker":marker,"subject":value[0],"plan":plan(marker)} for marker,value in STEP_CONTRACT.items()]},"ygor")

    def tearDown(self):self.temp.cleanup()

    def test_runtime_config_is_explicit_bounded_and_secret_free(self):
        self.assertEqual("synthetic",load_config(self.path)["mode"])
        mutations=(
            {"repository":"relative.sqlite"},{"poll_minutes":1},{"max_tasks_per_run":31},{"daily_cap":31},
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

    def test_synthetic_once_runs_end_to_end_without_network(self):
        output=io.StringIO()
        with patch("socket.create_connection",side_effect=AssertionError("network forbidden")),redirect_stdout(output):
            code=main(["--config",str(self.path),"--once"])
        self.assertEqual(0,code);summary=json.loads(output.getvalue())
        self.assertEqual("completed",summary["processed"][0]["status"])
        self.assertEqual("fixture-revision",summary["source_revision"])
        self.assertNotIn("secret",output.getvalue().lower())
        stored=Repository(self.db).get_state("linkedin_cadence_task","118775080824")
        self.assertEqual("connectsafely:synthetic-1",stored["receipt_ref"])


if __name__=="__main__":unittest.main()
