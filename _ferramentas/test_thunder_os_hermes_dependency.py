import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from thunder_os import hermes_dependency as subject

class DependencyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'copy'; self.root.mkdir()
        self.binary = self.base / 'python-real'; self.binary.write_bytes(b'synthetic-binary')
        self.venv = self.base / 'venv'; (self.venv / 'bin').mkdir(parents=True)
        self.python = self.venv / 'bin/python'; self.python.symlink_to(self.binary)
        self.cfg = self.venv / 'pyvenv.cfg'; self.cfg.write_text('synthetic')
        self.site = self.venv / 'lib' / ('python' + '.'.join(map(str,sys.version_info[:2]))) / 'site-packages'; self.site.mkdir(parents=True)
        self.hook = self.site / 'synthetic.pth'; self.hook.write_text('# inert')
        self.code = self.root / 'module.py'; self.code.write_text('VALUE=1')
        self.manifest = self.root / 'thunder-hermes-ro-manifest.json'
        self.manifest.write_text(json.dumps({'upstream_sha':'abc','files':[{**self.identity(self.code),'path':'module.py'}]}))
        self.lock = self.base / 'lock.json'
        self.lock.write_text(json.dumps({'schema_version':1,'upstream_sha':'abc','dependency_path':str(self.root),'dependency_manifest':self.identity(self.manifest),'interpreter_path':str(self.python),'interpreter_realpath':str(self.binary),'interpreter_binary':self.identity(self.binary),'pyvenv_config':self.identity(self.cfg),'startup_files':[self.identity(self.hook)]}))
        p=patch.object(subject,'LOCK_PATH',self.lock); p.start(); self.addCleanup(p.stop)
    def identity(self,p):
        raw=p.read_bytes(); return {'path':str(p),'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    def verify(self): subject.verify_dependency(self.root, self.python)
    def assert_blocked(self):
        with self.assertRaisesRegex(RuntimeError,'^hermes_dependency_identity_mismatch$'): self.verify()
    def test_valid(self): self.verify()
    def test_modified_source(self): self.code.write_text('PRIVATE_SENTINEL'); self.assert_blocked()
    def test_added_source(self): (self.root/'extra.py').write_text('x'); self.assert_blocked()
    def test_changed_binary(self): self.binary.write_text('changed'); self.assert_blocked()
    def test_added_startup_hook(self): (self.site/'evil.pth').write_text('x'); self.assert_blocked()
    def test_changed_config(self): self.cfg.write_text('x'); self.assert_blocked()
    def test_manifest_drift(self): self.manifest.write_text('{}'); self.assert_blocked()
    def test_source_symlink(self): self.code.unlink(); self.code.symlink_to(self.binary); self.assert_blocked()
    def test_wrong_interpreter(self):
        with self.assertRaisesRegex(RuntimeError,'^hermes_dependency_identity_mismatch$'): subject.verify_dependency(self.root, self.binary)
if __name__ == '__main__': unittest.main()
