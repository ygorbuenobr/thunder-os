"""Validate the pinned legacy research profile plus current safety contracts.

New research/deadline tests belong to the panel profile, not this compatibility
branch. No test file is deleted or weakened; immutable legacy suites are loaded
from Git, and the shared safety fixture is checked for exact AST equivalence.
"""
import ast
import http.client
import importlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASE='d562788c59853980d9a6091df7069393611f69de'


def blob(name):
    return subprocess.check_output(['git','-C',str(ROOT),'show',BASE+':_ferramentas/'+name])


def main():
    sys.path.insert(0,str(ROOT/'_ferramentas'))
    for name in ['hermes_text_adapter.py','hermes_text_child.py','research_producer.py']:
        assert (ROOT/'_ferramentas/thunder_os'/name).read_bytes()==blob('thunder_os/'+name),name
    previous=blob('thunder_os/continuous_consumer.py').decode()
    replacements=[
        ('from .service import ThunderService\n','from .service import ThunderService\nfrom .email_readback import configured_email_readers\n'),
        ('            knowledge_store=service.knowledge_store, approved_catalog=service._approved_catalog)\n',
         '            knowledge_store=service.knowledge_store, approved_catalog=service._approved_catalog,\n            effect_readers=service._effect_readers)\n'),
        ('    service = ThunderService(repository, knowledge_store=KnowledgeStore(args.brain))\n',
         '    service = ThunderService(repository, knowledge_store=KnowledgeStore(args.brain),\n        effect_readers=configured_email_readers(repository))\n'),
    ]
    for before,after in replacements:
        assert previous.count(before)==1
        previous=previous.replace(before,after,1)
    assert (ROOT/'_ferramentas/thunder_os/continuous_consumer.py').read_text()==previous
    old_sources={name:blob(name) for name in ['test_thunder_os_research.py',
        'test_thunder_os_connection_worker.py','test_thunder_os_research_producer.py']}
    def helper(tree):
        return ast.dump(next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='base_artifact'))
    assert helper(ast.parse(old_sources['test_thunder_os_research.py']))==helper(
        ast.parse((ROOT/'_ferramentas/test_thunder_os_research.py').read_text()))

    def blocked(*args,**kwargs):
        raise AssertionError('network forbidden in compatibility tests')
    socket.socket=blocked
    socket.create_connection=blocked
    socket.getaddrinfo=blocked
    legacy=[]
    for filename,source in old_sources.items():
        name=filename[:-3] if filename=='test_thunder_os_research.py' else 'compat_'+filename[:-3]
        module=types.ModuleType(name)
        module.__file__=str(ROOT/'_ferramentas'/filename)
        sys.modules[name]=module
        exec(compile(source,module.__file__,'exec'),module.__dict__)
        legacy.append(module)
    from thunder_os.hermes_text_adapter import single_attempt_runner
    from thunder_os.continuous_consumer import GuardedRunner
    assert single_attempt_runner(lambda job, timeout: '').timeout_seconds==120
    assert GuardedRunner.timeout_seconds==120
    names='test_thunder_os_approvals test_thunder_os_cadence test_thunder_os_cadence_readonly test_thunder_os_reply_control test_thunder_os_models test_thunder_os_db_integracao test_thunder_os_campaign_registry test_thunder_os_campaign_confirmation test_thunder_os_operation_campaign test_thunder_os_knowledge test_thunder_os_cadence_contact_campaign test_thunder_os_cadence_evidence test_thunder_os_learning test_thunder_os_effect_reconciliation test_thunder_os_connection_panel test_thunder_os_email_readback test_thunder_os_migrated_operations test_thunder_os_connectsafely_panel_wiring test_thunder_os_email_context test_thunder_os_eval_trajectory'.split()
    suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    for module in legacy:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    for method in ['test_internal_worker_preserves_server_owned_receipt_readers',
                   'test_internal_worker_without_receipt_readers_remains_disconnected']:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(
            'test_thunder_os_connection_worker.ConnectionWorkerTests.'+method))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    trajectory=importlib.import_module('test_thunder_os_eval_trajectory')
    rows=trajectory.CASE_RUNS
    ids=[row['case_id'] for row in rows]
    passed=all(row['outcome']=='PASS' for row in rows)
    complete=set(ids)=={f'os-eval-{number:03}' for number in range(1,37)} and len(ids)==36
    print(json.dumps({'profile':'legacy-research-current-safety','tests_run':result.testsRun,
                      'successful':result.wasSuccessful(),'case_count':len(ids),
                      'cases_complete':complete,'all_cases_passed':passed,
                      'research_timeout':120,'shared_fixture_ast_equal':True}))
    return 0 if result.wasSuccessful() and complete and passed else 1


if __name__=='__main__':
    raise SystemExit(main())
