"""Mission A fixtures: no scheduler, provider or model is contacted."""
import tempfile
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from thunder_os.db import Repository
from thunder_os.mission_a import MissionA, MissionAError

class Port:
    def __init__(self): self.created=[]; self.resumed=[]; self.records={}
    def create_paused(self, request_id, context):
        self.created.append((request_id,context)); return 'abc123def456'
    def resume(self, job_id): self.resumed.append(job_id)
    def inspect(self,job_id): return self.records.get(job_id, {'executions':[], 'outputs':[]})

class MissionATests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo=Repository(Path(self.tmp.name)/'test.db'); self.port=Port()
        self.app=MissionA(self.repo,self.port,lambda: {'campaign_id':'BR-APOLLO-01','apollo':{'coverage':'partial'},'hubspot':{'status':'unverified'}})
    def test_request_is_explicit_idempotent_and_scoped(self):
        with self.assertRaises(MissionAError): self.app.request('fixture-1', False)
        self.assertEqual(len(self.port.created),0)
        a=self.app.request('fixture-1',True); b=self.app.request('fixture-1',True)
        self.assertEqual(a['job_id'],b['job_id']); self.assertEqual(len(self.port.created),1)
        self.assertEqual(self.port.resumed,['abc123def456'])
        self.assertEqual(self.port.created[0][1]['hubspot']['status'],'unverified')
    def test_concurrent_duplicate_creates_only_one_job(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.app.request('fixture-race',True), range(2)))
        self.assertEqual(len(self.port.created),1)
        self.assertEqual(len(self.port.resumed),1)
        self.assertTrue(all(x['status'] in {'creating','starting','scheduled'} for x in results))

    def test_uncertain_creation_never_retries(self):
        def fail(*args): raise TimeoutError()
        self.port.create_paused=fail
        self.assertEqual(self.app.request('fixture-2',True)['status'],'uncertain')
        self.assertEqual(self.app.request('fixture-2',True)['status'],'uncertain')
    def test_cached_sources_preserves_partial_and_unverified_hubspot(self):
        from thunder_os.mission_a import cached_sources
        from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE
        snapshot={'campaign_id':'BR-APOLLO-01','account_id':APOLLO_TEAM,'external_campaign_id':APOLLO_SEQUENCE,
                  'status':'partial','enabled':True,'observed_at':'2026-11-03T01:00:00Z',
                  'cohort_observation':{'count':8,'observed_at':'2026-11-02T12:00:00Z'},
                  'coverage':{'complete':False,'contacts_observed_at':'2026-11-02T12:00:00Z','atomic_snapshot':False},
                  'leads':[{'id':'fixture'}]}
        result=cached_sources(SimpleNamespace(snapshot=lambda:snapshot),self.repo)
        self.assertEqual(result['apollo']['observed_contacts'],8)
        self.assertEqual(result['apollo']['contacts_observed_at'],'2026-11-02T12:00:00Z')
        self.assertEqual(result['apollo']['cache_records_including_historical'],1)
        snapshot['cohort_observation']={'count':None,'observed_at':None}
        self.assertIsNone(cached_sources(SimpleNamespace(snapshot=lambda:snapshot),self.repo)['apollo']['observed_contacts'])
        self.assertIsNone(result['apollo']['total_contacts'])
        self.assertEqual(result['hubspot']['status'],'unverified')
        self.assertFalse(result['apollo']['atomic_snapshot'])
        self.assertNotIn('leads',str(result))

    def test_native_port_uses_restricted_profile_and_paused_one_shot(self):
        from thunder_os.mission_a import NativeScheduler, PROFILE
        with patch('thunder_os.mission_a.subprocess.run',return_value=SimpleNamespace(stdout='{ "id":"abc123def456" }')) as run:
            self.assertEqual(NativeScheduler().create_paused('fixture-4',{'safe':'fixture'}),'abc123def456')
            args,kw=run.call_args
            self.assertIn('paused=True',args[0][2]); self.assertIn('repeat=1',args[0][2])
            self.assertIn('["delegation","no_mcp"]',args[0][2]); self.assertEqual(kw['env']['HERMES_HOME'],PROFILE)
            self.assertNotIn('shell',kw); self.assertEqual(len(run.mock_calls),1)

    def test_native_inspect_reads_only_requested_job_directory(self):
        from thunder_os.mission_a import NativeScheduler
        root=Path(self.tmp.name); cron=root/'cron'; (cron/'output'/'abc123def456').mkdir(parents=True)
        (cron/'output'/'abc123def456'/'fixture.md').write_text('job output')
        (cron/'output'/'999999999999').mkdir(); (cron/'output'/'999999999999'/'old.md').write_text('other job')
        with sqlite3.connect(cron/'executions.db') as con:
            con.execute('CREATE TABLE executions(id TEXT,job_id TEXT,status TEXT)')
            con.execute('INSERT INTO executions VALUES (?,?,?)',('e1','abc123def456','completed'))
            con.execute('INSERT INTO executions VALUES (?,?,?)',('e2','999999999999','completed'))
        with patch('thunder_os.mission_a.PROFILE',str(root)):
            result=NativeScheduler().inspect('abc123def456')
            self.assertEqual(result,{'executions':[{'id':'e1','status':'completed'}],'outputs':['job output']})
            (cron/'output'/'abc123def456'/'second.md').write_text('ambiguous')
            self.assertEqual(len(NativeScheduler().inspect('abc123def456')['outputs']),2)

    def test_result_requires_single_completed_execution_and_single_output(self):
        self.app.request('fixture-3',True)
        self.port.records['abc123def456']={'executions':[{'id':'e1','status':'completed'}], 'outputs':['fixture report']}
        result=self.app.result('fixture-3'); self.assertEqual(result['execution_id'],'e1'); self.assertEqual(result['output'],'fixture report')
        self.assertEqual(self.repo.get_state('mission_a_request','fixture-3')['execution_id'],'e1')
        self.port.records['abc123def456']['outputs'].append('other output')
        self.assertEqual(self.app.result('fixture-3')['status'],'ambiguous')
        self.port.records['abc123def456']={'executions':[{'id':'e1','status':'completed'},{'id':'e2','status':'completed'}], 'outputs':['fixture report']}
        self.assertEqual(self.app.result('fixture-3')['status'],'ambiguous')

if __name__=='__main__': unittest.main()
