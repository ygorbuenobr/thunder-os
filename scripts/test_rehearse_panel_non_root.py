"""Offline checks for the rehearsal script; never run a service or copy data."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/rehearse_panel_non_root.sh'


class RehearsalSafetyTests(unittest.TestCase):
    def test_shell_syntax(self):
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True, capture_output=True)

    def test_no_args_fails_before_work(self):
        result = subprocess.run(['bash', str(SCRIPT)], capture_output=True, text=True, timeout=5)
        self.assertEqual(2, result.returncode)
        self.assertTrue('Linux' in result.stderr or 'Uso:' in result.stderr)

    def test_isolation_and_cleanup_are_explicit(self):
        text = SCRIPT.read_text()
        for required in ('--property=PrivateNetwork=yes', '--property=ProtectHome=yes',
                         '--property=ProtectSystem=strict', '--property=PrivateTmp=yes',
                         '--property=NoNewPrivileges=yes', '--property=RestrictNamespaces=yes',
                         '--property=RestrictAddressFamilies=AF_INET AF_INET6',
                         'THUNDER_EXTERNAL_EFFECTS_ENABLED=false', 'THUNDER_OS_SEQD_SEND_ENABLED=false',
                         'THUNDER_OS_HUBSPOT_WRITE_ENABLED=false', 'sqlite3 -init /dev/null -readonly',
                         'LoadCredential=rehearsal_probe:', 'work_inode=', 'created_user=',
                         'mkdir -m 0700 -- "$lockdir"', 'ReadOnlyPaths=${db%/*}',
                         'ReadWritePaths=$work/db', '$work/root-db/painel.db',
                         'nsenter --net="/proc/self/fd/$network_fd" curl --disable',
                         "SELECT dados FROM itens WHERE id=?"):
            self.assertIn(required, text)
        self.assertNotIn('LoadCredential=connectsafely', text)
        self.assertNotIn('exec 9>', text)
        self.assertNotIn('python3 - "$work/db/painel.db"', text)

    def test_cleanup_preserves_user_files_and_lock_when_process_check_not_clear(self):
        text = SCRIPT.read_text()
        cleanup = text[text.index('cleanup() {'):text.index('\ntrap cleanup EXIT')]
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / 'destructive-call'
            for process_code in (0, 2):  # live process; inspection error
                program = f'''set -u
created_user=1; uid=123; user=rehearsal; created_dir=1
launched_backup=0; launched_writer=0; launched_http=0
backup_unit=b; writer_unit=w; unit=u; work=/tmp/never; lockdir=/tmp/never-lock
id() {{ printf '123\\n'; }}
pgrep() {{ return {process_code}; }}
userdel() {{ printf called > "$MARKER"; }}
rm() {{ printf called > "$MARKER"; }}
rmdir() {{ printf called > "$MARKER"; }}
{cleanup}
cleanup
'''
                result = subprocess.run(['bash', '-c', program], env={'MARKER': str(marker)},
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(1, result.returncode)
                self.assertIn('preservando', result.stderr)
                self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
