"""Pedido 5, TLC light: local fixture proofs; independent review belongs to Main.

Binding source: delegated conversation; ownership is these two scripts only.
Checks/proofs (PanelResourceTests):
C1 identity/release/PID: test_identity_mismatch_never_reads_environment,
   test_release_manifest_and_process_must_agree, test_process_change_discards_snapshot.
C2 independent typed flags: test_independent_flags_use_process_not_runner_environment,
   test_missing_invalid_duplicate_flags_stay_unknown,
   test_missing_known_flags_are_effectively_off_without_becoming_observed_false,
   test_effective_rules_preserve_invalid_values_and_global_lower_semantics,
   test_activation_definitions_match_actual_product_predicates.
C3 config pins: test_config_missing_invalid_json_hash_and_boolean,
   test_paths_cannot_select_credentials_symlinks_or_commands.
C4 policy/drop-in presence: test_policy_and_dropins_are_observation_only.
C5 PGOPTIONS: test_pgoptions_explicit_readonly_and_overrides.
C6 no disclosure/side effects: test_canary_never_leaks_and_no_credentials_are_opened,
   test_subprocess_is_fixed_readonly_and_errors_are_redacted,
   test_cli_only_accepts_expected_sha_and_never_turns_flags_into_a_gate.
Swept: validation/failures C1-C6; concurrency C1; auth C1/C3; observability C2-C6;
no persistence, retries, provider operations or state transitions are in scope.
Landing: fixed host/unit, allowlisted process keys, JSON-only stdout with no raw
environment/config/exception strings; no user-supplied host/path/command options.
No extra checklist file under this delegated two-file ownership restriction.
"""
from contextlib import redirect_stderr, redirect_stdout
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import read_panel_resource_gates as reader


SHA = '9f9609ab2e0fd99adcb94f091bf73dd5240e9b65'
CANARY = 'SECRET_CANARY_NEVER_DISCLOSE_123'
CONFIG = '/etc/thunder-os/connectsafely.json'
POLICY = '/etc/thunder-os/assisted-policy.json'
WRITER = '/etc/systemd/system/painel-thunder.service.d/60-codex-assisted-writer.conf'


class PanelResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = reader.LocalFiles(self.root)
        self.opened = []
        self.original_read = self.files.read
        self.files.read = lambda path, limit: self.track_read(path, limit)
        self.env = {key: 'false' for key in reader.FLAGS}
        self.env.update(THUNDER_EXTERNAL_EFFECTS_ENABLED='true',
            THUNDER_OS_CODEX_ASSISTED_WRITER_ENABLED='true',
            THUNDER_OS_CONNECTSAFELY_CONFIG=CONFIG,
            PGOPTIONS='-c default_transaction_read_only=on',
            UNRELATED_PASSWORD=CANARY)
        self.put_json(CONFIG, {'version': 1, 'execution_enabled': True,
            'live': {'api_credential_file': '/etc/thunder-os/credentials/api.json',
                     'database_credential_file': '/etc/thunder-os/credentials/db.json',
                     'secret': CANARY}})
        self.pin(CONFIG, 'THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256')
        self.put_json('/opt/thunder-os/releases/' + SHA + '/thunder-os-manifest.json',
                      {'schema_version': 1, 'source_sha': SHA})
        self.link('/opt/thunder-os/current', '/opt/thunder-os/releases/' + SHA)
        self.link('/proc/123/cwd', '/opt/thunder-os/releases/' + SHA)
        self.put('/proc/123/stat', '123 (python process) S ' + ' '.join(['0'] * 18 + ['555']))
        self.put(WRITER, CANARY)  # Only presence may be observed, never contents.
        self.properties = {'MainPID': '123', 'ActiveState': 'active',
            'FragmentPath': '/etc/systemd/system/painel-thunder.service', 'DropInPaths': WRITER}
        self.run = patch.object(reader.subprocess, 'run', side_effect=self.systemctl).start()
        patch('socket.socket', side_effect=AssertionError('network forbidden')).start()
        self.addCleanup(patch.stopall)

    def track_read(self, path, limit):
        self.opened.append(path)
        return self.original_read(path, limit)

    def put(self, path, content):
        file = self.root / path.lstrip('/')
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content if isinstance(content, bytes) else content.encode())

    def put_json(self, path, content):
        self.put(path, json.dumps(content))

    def link(self, path, target):
        link = self.root / path.lstrip('/')
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)

    def pin(self, path, key):
        self.env[key] = hashlib.sha256((self.root / path.lstrip('/')).read_bytes()).hexdigest()

    def systemctl(self, command, **kwargs):
        self.assertEqual(command, reader.SYSTEMCTL)
        self.assertIs(kwargs['shell'], False)
        self.assertNotIn('Environment', command)
        self.assertEqual(kwargs['env'], {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        return subprocess.CompletedProcess(command, 0,
            '\n'.join(key + '=' + value for key, value in self.properties.items()), '')

    def observe(self, **kwargs):
        raw = b'\0'.join(key.encode() + b'=' + value.encode() for key, value in self.env.items()) + b'\0'
        self.put('/proc/123/environ', raw)
        return reader.observe(SHA, files=self.files, hostname=kwargs.get('hostname', reader.HOST),
                              system=kwargs.get('system', 'Linux'), uid=kwargs.get('uid', 0))

    def test_independent_flags_use_process_not_runner_environment(self):
        with patch.dict(os.environ, {key: 'true' for key in reader.FLAGS}):
            report = self.observe()
        self.assertEqual(report['status'], 'observed')
        flags = report['flags']
        for key in reader.FLAGS:
            self.assertIs(flags[key]['value'], self.env[key] == 'true')
            self.assertEqual(flags[key]['source'], '/proc/123/environ:' + key)
        self.assertIs(report['connectsafely']['execution_enabled']['value'], True)
        self.assertIs(flags['THUNDER_OS_CODEX_ASSISTED_DISPATCH']['value'], False)
        self.assertIs(flags['THUNDER_OS_CODEX_ASSISTED_WRITER_ENABLED']['value'], True)
        self.assertFalse(report['authorization_granted'])

    def test_missing_invalid_duplicate_flags_stay_unknown(self):
        for value in (None, '', '1', 'TRUE', CANARY):
            with self.subTest(value=value):
                key = 'THUNDER_OS_GMAIL_CRM_ENABLED'
                if value is None:
                    self.env.pop(key, None)
                else:
                    self.env[key] = value
                report = self.observe()
                self.assertIsNone(report['flags'][key]['value'])
                self.assertEqual(report['flags'][key]['status'], 'unknown')
        self.observe()
        file = '/proc/123/environ'
        self.put(file, self.original_read(file, 1048576) + b'THUNDER_EXTERNAL_EFFECTS_ENABLED=false\0')
        report = reader.observe(SHA, files=self.files, hostname=reader.HOST, system='Linux', uid=0)
        self.assertIsNone(report['flags']['THUNDER_EXTERNAL_EFFECTS_ENABLED']['value'])
        self.assertEqual(report['flags']['THUNDER_EXTERNAL_EFFECTS_ENABLED']['reason'], 'duplicate_key')

    def test_missing_known_flags_are_effectively_off_without_becoming_observed_false(self):
        for key in reader.FLAGS:
            self.env.pop(key)
        report = self.observe()
        for key in reader.FLAGS:
            with self.subTest(key=key):
                flag = report['flags'][key]
                self.assertIsNone(flag['value'])
                self.assertEqual(flag['status'], 'unknown')
                self.assertEqual(flag['reason'], 'missing')
                self.assertIs(flag['effective_enabled'], False)
                self.assertEqual(flag['activation_rule'], 'requires_lower_true' if key ==
                    'THUNDER_EXTERNAL_EFFECTS_ENABLED' else 'requires_true')
                self.assertIn('not authorization', flag['definition'])
        # Missing source artifacts have no corresponding environment default.
        self.env.pop('THUNDER_OS_CONNECTSAFELY_CONFIG')
        self.assertNotIn('effective_enabled', self.observe()['connectsafely']['execution_enabled'])

    def test_effective_rules_preserve_invalid_values_and_global_lower_semantics(self):
        for key in reader.FLAGS:
            for value in ('true', 'false', '', '1', CANARY, 'TRUE'):
                with self.subTest(key=key, value=value):
                    self.env[key] = value
                    flag = self.observe()['flags'][key]
                    valid = value in ('true', 'false') or (key == 'THUNDER_EXTERNAL_EFFECTS_ENABLED' and value == 'TRUE')
                    expected = value.lower() == 'true' if valid else None
                    self.assertIs(flag['effective_enabled'], expected)
                    self.assertIs(flag['value'], expected)
                    self.assertEqual(flag['status'], 'observed' if valid else 'unknown')
        self.observe()
        path = '/proc/123/environ'
        self.put(path, self.original_read(path, 1048576) + b'THUNDER_EXTERNAL_EFFECTS_ENABLED=false\0')
        report = reader.observe(SHA, files=self.files, hostname=reader.HOST, system='Linux', uid=0)
        self.assertIsNone(report['flags']['THUNDER_EXTERNAL_EFFECTS_ENABLED']['effective_enabled'])

    def test_activation_definitions_match_actual_product_predicates(self):
        # Source-only drift check: do not import product/provider modules.
        root = Path(__file__).resolve().parent.parent / '_ferramentas/thunder_os'
        for filename, function, keys, lower in (
            ('codex_assisted.py', 'configured_assisted',
             ('THUNDER_OS_CODEX_ASSISTED_DISPATCH', 'THUNDER_OS_CODEX_ASSISTED_WRITER_ENABLED'), False),
            ('thunder_email_runtime.py', 'configured_adapter', ('THUNDER_OS_GMAIL_CADENCE_ENABLED',), False),
            ('thunder_email_runtime.py', 'configured_cycle', ('THUNDER_OS_GMAIL_CRM_ENABLED',
             'THUNDER_OS_GMAIL_RECONCILE_ENABLED', 'THUNDER_OS_GMAIL_REPLY_INGEST_ENABLED'), False),
            ('worker.py', 'build_runtime', ('THUNDER_EXTERNAL_EFFECTS_ENABLED',), True),
        ):
            tree = ast.parse((root / filename).read_text())
            method = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function)
            comparisons = [node for node in ast.walk(method) if isinstance(node, ast.Compare)
                           and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq))
                           and len(node.comparators) == 1 and isinstance(node.comparators[0], ast.Constant)
                           and node.comparators[0].value == 'true']
            for key in keys:
                with self.subTest(key=key):
                    left = "os.environ.get('" + key + "'" + (", '').lower()" if lower else ')')
                    self.assertTrue(any(ast.unparse(node.left) == left for node in comparisons))

    def test_identity_mismatch_never_reads_environment(self):
        for kwargs in ({'hostname': 'different'}, {'system': 'Darwin'}, {'uid': 501}):
            with self.subTest(kwargs=kwargs):
                self.opened.clear()
                report = self.observe(**kwargs)
                self.assertEqual(report['status'], 'mismatch')
                self.assertNotIn('flags', report)
                self.assertEqual(self.opened, [])
        self.assertEqual(self.run.call_count, 0)

    def test_release_manifest_and_process_must_agree(self):
        for change in ('pid', 'inactive', 'cwd', 'current', 'manifest', 'json'):
            with self.subTest(change=change):
                props = self.properties.copy()
                if change == 'pid': self.properties['MainPID'] = '0'
                if change == 'inactive': self.properties['ActiveState'] = 'inactive'
                if change == 'cwd': self.link('/proc/123/cwd', '/opt/thunder-os/releases/' + 'b' * 40)
                if change == 'current': self.link('/opt/thunder-os/current', '/opt/thunder-os/releases/' + 'b' * 40)
                path = '/opt/thunder-os/releases/' + SHA + '/thunder-os-manifest.json'
                if change == 'manifest': self.put_json(path, {'schema_version': 1, 'source_sha': 'b' * 40})
                if change == 'json': self.put(path, '{' + CANARY)
                report = self.observe()
                self.assertNotEqual(report['status'], 'observed')
                self.assertNotIn('flags', report)
                self.assertNotIn(CANARY, json.dumps(report))
                self.properties = props
                self.link('/proc/123/cwd', '/opt/thunder-os/releases/' + SHA)
                self.link('/opt/thunder-os/current', '/opt/thunder-os/releases/' + SHA)
                self.put_json(path, {'schema_version': 1, 'source_sha': SHA})

    def test_config_missing_invalid_json_hash_and_boolean(self):
        cases = [({}, 'pin_missing'), ({'version': 1}, 'boolean_missing'),
                 ({'execution_enabled': 'true'}, 'boolean_invalid'),
                 ({'execution_enabled': 1}, 'boolean_invalid')]
        for data, reason in cases:
            with self.subTest(reason=reason):
                self.put_json(CONFIG, data)
                self.pin(CONFIG, 'THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256')
                if reason == 'pin_missing': self.env.pop('THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256')
                result = self.observe()['connectsafely']['execution_enabled']
                self.assertEqual(result['reason'], reason)
                self.assertIsNone(result['value'])
        for raw, reason in ((b'{', 'json_invalid'),
            (b'{"execution_enabled":true,"execution_enabled":false}', 'json_invalid')):
            self.put(CONFIG, raw); self.pin(CONFIG, 'THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256')
            self.assertEqual(self.observe()['connectsafely']['reason'], reason)
        self.put_json(CONFIG, {'execution_enabled': False})
        self.pin(CONFIG, 'THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256')
        self.put_json(CONFIG, {'execution_enabled': True})
        self.assertEqual(self.observe()['connectsafely']['reason'], 'hash_mismatch')
        (self.root / CONFIG.lstrip('/')).unlink()
        self.assertEqual(self.observe()['connectsafely']['reason'], 'unavailable')

    def test_paths_cannot_select_credentials_symlinks_or_commands(self):
        for path in ('relative.json', '/etc/thunder-os/../credentials/key.json',
                     '/etc/thunder-os/credentials/api.json', '/proc/1/environ',
                     '/etc/config.json;id', '/etc/$(id).json'):
            with self.subTest(path=path):
                self.env['THUNDER_OS_CONNECTSAFELY_CONFIG'] = path
                self.opened.clear()
                self.assertEqual(self.observe()['connectsafely']['reason'], 'path_invalid')
                self.assertNotIn(path, self.opened)
        self.env['THUNDER_OS_CONNECTSAFELY_CONFIG'] = CONFIG
        (self.root / CONFIG.lstrip('/')).unlink()
        self.link(CONFIG, '/etc/thunder-os/credentials/api.json')
        self.assertEqual(self.observe()['connectsafely']['reason'], 'unavailable')

    def test_policy_and_dropins_are_observation_only(self):
        self.env['THUNDER_OS_CODEX_ASSISTED_POLICY_FILE'] = POLICY
        self.put_json(POLICY, {'expires_at': 1, 'content': CANARY})
        self.pin(POLICY, 'THUNDER_OS_CODEX_ASSISTED_POLICY_SHA256')
        report = self.observe()
        self.assertTrue(report['policies']['codex_assisted']['present'])
        self.assertEqual(report['policies']['codex_assisted']['status'], 'verified')
        self.assertIsNone(report['policies']['gmail_cadence']['present'])
        self.assertTrue(report['dropins']['writer']['present'])
        self.assertTrue(report['dropins']['writer']['loaded'])
        self.assertNotIn(WRITER, self.opened)
        self.assertNotIn('expires_at', json.dumps(report))
        self.assertFalse(report['authorization_granted'])
        self.properties['DropInPaths'] = ''
        (self.root / WRITER.lstrip('/')).unlink()
        report = self.observe()
        self.assertFalse(report['dropins']['writer']['loaded'])
        self.assertFalse(report['dropins']['writer']['present'])
        self.assertTrue(report['flags']['THUNDER_OS_CODEX_ASSISTED_WRITER_ENABLED']['value'])

    def test_pgoptions_explicit_readonly_and_overrides(self):
        for raw, value in ((None, None), ('', None), ('-c search_path=public', None),
            ('-c default_transaction_read_only=on', True),
            ('-cdefault_transaction_read_only=off', False),
            ('--default-transaction-read-only=true', True),
            ('-c default_transaction_read_only=on -c default_transaction_read_only=off', False),
            ('-c default_transaction_read_only=off -c transaction_read_only=on', None),
            ('-c default_transaction_read_only=' + CANARY, None), ('"unterminated', None)):
            with self.subTest(raw=raw):
                if raw is None: self.env.pop('PGOPTIONS', None)
                else: self.env['PGOPTIONS'] = raw
                self.assertIs(self.observe()['pgoptions']['read_only'], value)

    def test_process_change_discards_snapshot(self):
        original = self.systemctl
        count = [0]
        def changed(*args, **kwargs):
            count[0] += 1
            if count[0] == 2: self.properties['MainPID'] = '124'
            return original(*args, **kwargs)
        self.run.side_effect = changed
        report = self.observe()
        self.assertEqual(report['status'], 'unknown')
        self.assertEqual(report['reason'], 'process_changed')
        self.assertNotIn('flags', report)

    def test_pid_reuse_selected_environment_or_current_change_discards_snapshot(self):
        for change in ('starttime', 'environment', 'current'):
            with self.subTest(change=change):
                self.link('/opt/thunder-os/current', 'releases/' + SHA)
                def changed(*args, **kwargs):
                    result = self.systemctl(*args, **kwargs)
                    if self.run.call_count % 2 == 0:
                        if change == 'starttime':
                            self.put('/proc/123/stat', '123 (python process) S ' + ' '.join(['0'] * 18 + ['556']))
                        elif change == 'environment':
                            self.put('/proc/123/environ', b'THUNDER_EXTERNAL_EFFECTS_ENABLED=false\0')
                        else:
                            self.link('/opt/thunder-os/current', '/opt/thunder-os/releases/' + 'b' * 40)
                    return result
                self.run.reset_mock()
                self.run.side_effect = changed
                self.put('/proc/123/stat', '123 (python process) S ' + ' '.join(['0'] * 18 + ['555']))
                report = self.observe()
                self.assertEqual(report['reason'], 'process_changed')
                self.assertNotIn('flags', report)

    def test_unreadable_or_oversized_environment_is_unknown(self):
        original = self.files.read
        def unavailable(path, limit):
            if path.endswith('/environ'):
                raise PermissionError(CANARY)
            return original(path, limit)
        self.files.read = unavailable
        report = self.observe()
        self.assertEqual(report['reason'], 'local_read_unavailable')
        self.assertNotIn(CANARY, json.dumps(report))
        self.files.read = original
        self.observe()
        self.put('/proc/123/environ', b'x' * 1048577)
        report = reader.observe(SHA, files=self.files, hostname=reader.HOST, system='Linux', uid=0)
        self.assertEqual(report['reason'], 'oversized')
        self.assertNotIn('flags', report)

    def test_canary_never_leaks_and_no_credentials_are_opened(self):
        self.put('/etc/thunder-os/credentials/api.json', CANARY)
        report = self.observe()
        self.assertNotIn(CANARY, json.dumps(report))
        self.assertFalse(any('/credentials/' in path for path in self.opened))
        self.assertEqual(set(report['flags']), set(reader.FLAGS))
        self.assertNotIn('UNRELATED_PASSWORD', json.dumps(report))

    def test_subprocess_is_fixed_readonly_and_errors_are_redacted(self):
        self.run.side_effect = subprocess.CalledProcessError(1, CANARY, output=CANARY, stderr=CANARY)
        report = self.observe()
        self.assertEqual(report['reason'], 'service_unavailable')
        self.assertNotIn(CANARY, json.dumps(report))
        self.assertNotIn('flags', report)

    def test_cli_only_accepts_expected_sha_and_never_turns_flags_into_a_gate(self):
        for args in (['--expected-sha', SHA, '--host', 'different'],
                     ['--expected-sha', '../' + CANARY], ['--expected-sha', SHA, '--path', CONFIG]):
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                self.assertEqual(reader.main(args), 2)
            self.assertNotIn(CANARY, output.getvalue())
        report = self.observe()
        with patch.object(reader, 'observe', return_value=report), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(reader.main(['--expected-sha', SHA]), 0)
        self.assertEqual(json.loads(output.getvalue()), report)


if __name__ == '__main__':
    unittest.main()
