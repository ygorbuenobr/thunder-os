import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from thunder_os.credential_paths import credential_path

class CredentialPathTests(unittest.TestCase):
    def test_named_credential_and_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': tmp}):
            self.assertEqual('/legacy', credential_path('apollo_api_key', '/legacy'))
            path = Path(tmp) / 'apollo_api_key'
            path.write_text('fake')
            self.assertEqual(str(path), credential_path('apollo_api_key', '/legacy'))
            path.unlink()
            path.symlink_to(Path(tmp) / 'missing')
            self.assertEqual(str(path), credential_path('apollo_api_key', '/legacy'))
    def test_legacy_paths_resolve_only_when_credential_is_loaded(self):
        from thunder_os.credential_paths import resolve_legacy, LEGACY_CREDENTIAL_NAMES
        hub = '/var/lib/thunder-os-integrations/credentials/hubspot_access'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': tmp}):
            self.assertEqual(hub, resolve_legacy(hub))
            (Path(tmp) / 'hubspot_access').write_text('fake')
            self.assertEqual(str(Path(tmp) / 'hubspot_access'), resolve_legacy(hub))
            self.assertEqual('/other/path', resolve_legacy('/other/path'))
        self.assertIn('/etc/thunder-os/instantly_api_key', LEGACY_CREDENTIAL_NAMES)

    def test_canary_readers_use_systemd_credential_for_legacy_path(self):
        from thunder_os import canary_readers
        hub = '/var/lib/thunder-os-integrations/credentials/hubspot_access'
        seen = {}
        class FakeHTTP:
            def __init__(self, loader): seen['loader'] = loader
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': tmp}), \
                patch.object(canary_readers, 'HubSpotHTTP', FakeHTTP), \
                patch.object(canary_readers, 'CanaryScope', lambda config, owner_public_id: type('S', (), {'by_id': {}})()), \
                patch.object(canary_readers, 'HubSpotCanaryReader', lambda scope, http: None):
            stored = Path(tmp) / 'hubspot_access'
            stored.write_text('fake-hub')
            stored.chmod(0o400)
            try:
                canary_readers.build_canary_readers({'hubspot': {'credential_file': hub}}, owner_public_id='x')
            except Exception:
                pass
            self.assertIn('loader', seen)
            self.assertEqual('fake-hub', seen['loader']())

    def test_bad_directory_or_name_fails_closed(self):
        with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': 'relative'}):
            with self.assertRaises(ValueError): credential_path('key', '/legacy')
        with self.assertRaises(ValueError): credential_path('../key', '/legacy')
        with self.assertRaises(ValueError): credential_path('..', '/legacy')

class ConnectSafelyCredentialTests(unittest.TestCase):
    def test_private_credential_is_lazy_and_rejects_insecure_or_symlink(self):
        from thunder_os.connectsafely_live import credential
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'secret'
            read = credential(str(path))
            with self.assertRaises(ValueError): read()
            path.write_text('fake-token')
            path.chmod(0o600)
            self.assertEqual('fake-token', read())
            path.chmod(0o400)
            self.assertEqual('fake-token', read())
            path.chmod(0o640)
            with self.assertRaises(ValueError): read()
            path.unlink()
            path.symlink_to(Path(tmp) / 'missing')
            with self.assertRaises(ValueError): read()

    def test_config_referenced_legacy_paths_use_systemd_credentials(self):
        from thunder_os.connectsafely_live import credential
        legacy = {'/root/.config/thunder/credentials/apollo_api_key.txt': 'apollo_api_key',
                  '/var/lib/thunder-os-integrations/credentials/hubspot_access': 'hubspot_access',
                  '/etc/thunder-os/connectsafely.token': 'connectsafely_token',
                  '/etc/thunder-os/social-db.dsn': 'social_db_dsn'}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': tmp}):
            for path, name in legacy.items():
                stored = Path(tmp) / name
                stored.write_text('fake-' + name)
                stored.chmod(0o400)
                self.assertEqual('fake-' + name, credential(path)())
