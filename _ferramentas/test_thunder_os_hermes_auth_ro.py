"""Offline stubs only; no installed Hermes auth or inference."""
import os, sys, shutil, tempfile, types, unittest
from pathlib import Path
from unittest.mock import Mock, patch
from thunder_os.hermes_auth_ro import *

class ReadOnlyAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.runtime=dict(provider='openai-codex',source='hermes-auth-store',api_key='synthetic-token',base_url=ENDPOINT)
        self.resolve=Mock(return_value=self.runtime);self.expiring=Mock(return_value=False)
        self.modules={'hermes_cli':types.ModuleType('hermes_cli'),'hermes_cli.auth_codex':types.SimpleNamespace(resolve_codex_runtime_credentials=self.resolve,_codex_access_token_is_expiring=self.expiring)}
    def call(self):
        with patch.dict(sys.modules,self.modules):return resolve_operation_runtime(requested='openai-codex',target_model='existing-model')
    def code(self,code,fn):
        with self.assertRaises(ReadOnlyAuthError) as ctx:fn()
        self.assertEqual(code,ctx.exception.code);self.assertNotIn('synthetic-token',str(ctx.exception))
    def test_valid_exact_flags(self):
        self.assertEqual('codex_responses',self.call()['api_mode'])
        self.resolve.assert_called_once_with(read_only=True,force_refresh=False,refresh_if_expiring=False)
        self.expiring.assert_called_once_with('synthetic-token',0)
    def test_expired_no_retry(self):
        self.expiring.return_value=True;self.code('auth_expired',self.call);self.resolve.assert_called_once()
    def test_pool_rejected(self):
        self.runtime['source']='credential_pool';self.code('auth_unavailable',self.call)
    def test_runtime_endpoint_rejected(self):
        self.runtime['base_url']='https://elsewhere.invalid';self.code('auth_endpoint_blocked',self.call)
    def test_swallowed_save_corrupt_backup_and_probe_blocked(self):
        auth=self.root/'auth-fixture';auth.write_text('original');backup=self.root/'corrupt-fixture'
        actions=[lambda:auth.write_text('changed'),lambda:shutil.copy2(auth,backup),lambda:sys.audit('socket.connect',None,('invalid',443))]
        for action in actions:
            def resolver(**kwargs):
                try:action()
                except Exception:pass
                return self.runtime
            self.resolve.side_effect=resolver;self.code('auth_readonly_violation',self.call)
            self.assertEqual('original',auth.read_text());self.assertFalse(backup.exists())
    def test_config_projection_no_backup(self):
        config=self.root/'config.yaml';config.write_text('model:\n  provider: openai-codex\n  default: existing-model\nother: synthetic-private\n')
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual({'model':{'provider':'openai-codex','default':'existing-model','base_url':ENDPOINT}},load_operation_config(self.root))
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
    def test_config_rejects_endpoint_and_unsafe_yaml(self):
        config=self.root/'config.yaml';config.write_text('model:\n  provider: openai-codex\n  default: existing-model\n  base_url: https://elsewhere.invalid\n')
        self.code('auth_endpoint_blocked',lambda:load_operation_config(self.root))
        config.write_text('!!python/object/apply:os.system [false]');self.code('auth_config_invalid',lambda:load_operation_config(self.root))
    def test_endpoint_exact(self):
        for url in ['http://chatgpt.com/backend-api/codex','https://chatgpt.com.evil/backend-api/codex',ENDPOINT+'?q=x',ENDPOINT+'/other','https://user@chatgpt.com/backend-api/codex','https://chatgpt.com:444/backend-api/codex']:
            self.code('auth_endpoint_blocked',lambda:require_endpoint(url))
        require_endpoint(ENDPOINT+'/responses',request=True)
    def test_sdk_tls_no_proxy_redirect_retry_and_actual_url_pinned(self):
        http=Mock();factory=Mock(return_value=http);sdk=Mock();headers=Mock(return_value={})
        modules={'httpx':types.SimpleNamespace(Client=factory),'openai':types.SimpleNamespace(OpenAI=sdk),'agent':types.ModuleType('agent'),'agent.codex_headers':types.SimpleNamespace(codex_cloudflare_headers=headers)}
        with patch.dict(sys.modules,modules),patch.dict(os.environ,{'HTTPS_PROXY':'http://invalid','SSL_CERT_FILE':'invalid'}):create_operation_client(api_key='synthetic',base_url=ENDPOINT)
        kw=factory.call_args.kwargs;self.assertIs(False,kw['trust_env']);self.assertIs(False,kw['follow_redirects']);self.assertIs(True,kw['verify']);self.assertEqual(0,sdk.call_args.kwargs['max_retries'])
        hook=kw['event_hooks']['request'][0];hook(types.SimpleNamespace(url=ENDPOINT+'/responses'))
        self.code('auth_endpoint_blocked',lambda:hook(types.SimpleNamespace(url='https://elsewhere.invalid/responses')))
        headers.assert_called_once_with('synthetic',base_url=ENDPOINT)
    def test_guard_restores_fixture_writes(self):
        with readonly_resolution():self.assertTrue(self.root.exists())
        (self.root/'after').write_text('fixture')

if __name__=='__main__':unittest.main()

class OriginGuardTests(unittest.TestCase):
    def test_fresh_missing_module_cannot_fall_back_to_editable_install(self):
        import subprocess
        code='''
import sys,tempfile,types,importlib
from pathlib import Path
from thunder_os.hermes_text_child import install_hermes_origin_guard
with tempfile.TemporaryDirectory() as d:
    root=Path(d)/'copy';root.mkdir();original=Path(d)/'original';original.mkdir()
    (original/'hermes_missing_fixture.py').write_text('raise AssertionError("fallback")')
    (root/'hermes_present_fixture.py').write_text('VALUE=42')
    sys.path.insert(0,str(original))
    sys.modules['__editable__synthetic_finder']=types.SimpleNamespace(MAPPING={'hermes_missing_fixture':str(original)},NAMESPACES={})
    validate=install_hermes_origin_guard(root)
    assert importlib.import_module('hermes_present_fixture').VALUE==42
    validate()
    try:importlib.import_module('hermes_missing_fixture')
    except ModuleNotFoundError:pass
    else:raise AssertionError('fallback allowed')
'''
        result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parent,capture_output=True,text=True,timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)
