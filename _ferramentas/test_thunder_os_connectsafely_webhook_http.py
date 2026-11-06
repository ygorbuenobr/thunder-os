"""HTTP boundary with HMAC and local SQLite; no provider network."""
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest

import painel_servidor as panel
from thunder_os.connectsafely_webhook import ConnectSafelyWebhook
from thunder_os.db import Repository

PATH = '/api/os/v1/integrations/connectsafely/webhook'
ACCOUNT = 'a' * 24
SECRET = b's' * 32


class WebhookHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Repository(Path(self.temp.name) / 'local.db')
        self.service = SimpleNamespace(repository=self.repo, record_reply=Mock(), campaign_registry=Mock())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.banco = SimpleNamespace()
        self.server.thunder_service = self.service
        self.server.thunder_connectsafely_webhook = ConnectSafelyWebhook(
            self.repo, secret=SECRET, account_id=ACCOUNT)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def payload(self):
        now = int(time.time())
        raw = json.dumps({'object':'event','event':'message.received','data':{
            'provider':'LINKEDIN','account_id':ACCOUNT,
            'message_urn':'urn:li:messagingMessage:synthetic',
            'conversation_id':'urn:li:messagingThread:synthetic',
            'sender':{'id':'urn:li:fsd_profile:synthetic'},
            'timestamp':datetime.fromtimestamp(now, timezone.utc).isoformat(),
            'body':'Synthetic message'}}).encode()
        return raw, now

    def request(self, raw, now, *, signature=None, path=PATH, method='POST'):
        sig = signature or 'sha256=' + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=raw, headers={'Content-Type':'application/json',
                'X-Webhook-Signature':sig,'X-Webhook-Timestamp':str(now)})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_signed_raw_body_processed_once_without_human_nonce(self):
        raw, now = self.payload()
        status, value = self.request(raw, now)
        self.assertEqual((status, value['status']), (200, 'stored'))
        self.assertEqual(len(self.repo.list_state('connectsafely_event')), 1)
        self.service.record_reply.assert_called_once()
        status, value = self.request(raw, now)
        self.assertEqual((status, value['status']), (200, 'duplicate'))
        self.service.record_reply.assert_called_once()

    def test_invalid_signature_and_wrong_account_never_persist(self):
        raw, now = self.payload()
        self.assertEqual(self.request(raw, now, signature='sha256='+'0'*64)[0], 401)
        obj = json.loads(raw); obj['data']['account_id'] = 'b' * 24
        self.assertEqual(self.request(json.dumps(obj).encode(), now)[0], 400)
        self.assertEqual(self.repo.list_state('connectsafely_event'), [])
        self.service.record_reply.assert_not_called()

    def test_disabled_and_wrong_route_method_fail_closed(self):
        raw, now = self.payload()
        self.assertEqual(self.request(raw, now, path=PATH+'?x=1')[0], 405)
        self.assertEqual(self.request(raw, now, method='GET')[0], 405)
        self.server.thunder_connectsafely_webhook = None
        self.assertEqual(self.request(raw, now)[0], 503)
        self.assertEqual(self.repo.list_state('connectsafely_event'), [])

    def test_duplicate_signature_headers_are_rejected_before_persistence(self):
        raw, now = self.payload()
        signature = 'sha256=' + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            conn.putrequest('POST', PATH)
            conn.putheader('Content-Type', 'application/json')
            conn.putheader('Content-Length', str(len(raw)))
            conn.putheader('X-Webhook-Timestamp', str(now))
            conn.putheader('X-Webhook-Signature', signature)
            conn.putheader('X-Webhook-Signature', signature)
            conn.endheaders(raw)
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            conn.close()
        self.assertEqual(self.repo.list_state('connectsafely_event'), [])

    def test_incomplete_slow_body_times_out_without_persisting(self):
        raw, now = self.payload()
        signature = 'sha256=' + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            with patch.object(panel, 'CONNECTSAFELY_WEBHOOK_BODY_TIMEOUT_SECONDS', 0.1):
                conn.putrequest('POST', PATH)
                conn.putheader('Content-Type', 'application/json')
                conn.putheader('Content-Length', str(len(raw)))
                conn.putheader('X-Webhook-Timestamp', str(now))
                conn.putheader('X-Webhook-Signature', signature)
                conn.endheaders(raw[:1])
                response = conn.getresponse()
                self.assertEqual(response.status, 408)
                response.read()
        finally:
            conn.close()
        self.assertEqual(self.repo.list_state('connectsafely_event'), [])

    def test_slow_trickle_cannot_renew_the_total_body_deadline(self):
        raw, now = self.payload()
        signature = 'sha256=' + hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            with patch.object(panel, 'CONNECTSAFELY_WEBHOOK_BODY_TIMEOUT_SECONDS', 0.12):
                conn.putrequest('POST', PATH)
                conn.putheader('Content-Type', 'application/json')
                conn.putheader('Content-Length', str(len(raw)))
                conn.putheader('X-Webhook-Timestamp', str(now))
                conn.putheader('X-Webhook-Signature', signature)
                conn.endheaders()
                stopped = threading.Event()
                def trickle():
                    try:
                        for byte in raw[:20]:
                            conn.send(bytes([byte]))
                            if stopped.wait(0.04):
                                break
                    except OSError:
                        pass
                sender = threading.Thread(target=trickle, daemon=True)
                sender.start()
                response = conn.getresponse()
                self.assertEqual(response.status, 408)
                response.read()
                stopped.set(); sender.join(2)
        finally:
            conn.close()
        self.assertEqual(self.repo.list_state('connectsafely_event'), [])

    def test_processing_error_returns_retryable_and_duplicate_recovers(self):
        raw, now = self.payload()
        self.service.record_reply.side_effect = RuntimeError('temporary')
        self.assertEqual(self.request(raw, now)[0], 503)
        self.assertEqual(self.repo.list_state('connectsafely_event')[0]['state'], 'received')
        self.service.record_reply.side_effect = None
        self.assertEqual(self.request(raw, now)[0], 200)
        self.assertEqual(self.repo.list_state('connectsafely_event')[0]['state'], 'processed')

    def test_bootstrap_is_opt_in_and_refuses_invalid_config(self):
        server = SimpleNamespace()
        panel.configure_connectsafely_webhook(server, self.repo, {})
        self.assertIsNone(server.thunder_connectsafely_webhook)
        panel.configure_connectsafely_webhook(server, self.repo, {
            'THUNDER_OS_CONNECTSAFELY_WEBHOOK_ENABLED':'true',
            'THUNDER_OS_CONNECTSAFELY_WEBHOOK_ACCOUNT':ACCOUNT,
            'THUNDER_OS_CONNECTSAFELY_WEBHOOK_SECRET_FILE':'/missing/secret'})
        self.assertIsNone(server.thunder_connectsafely_webhook)
        self.assertEqual(server.thunder_connectsafely_webhook_error, 'configuration_invalid')
        secret = Path(self.temp.name) / 'synthetic-secret'
        secret.write_bytes(SECRET)
        secret.chmod(0o600)
        with patch.dict('os.environ', {}, clear=True):
            panel.configure_connectsafely_webhook(server, self.repo, {
                'THUNDER_OS_CONNECTSAFELY_WEBHOOK_ENABLED':'true',
                'THUNDER_OS_CONNECTSAFELY_WEBHOOK_ACCOUNT':ACCOUNT,
                'THUNDER_OS_CONNECTSAFELY_WEBHOOK_SECRET_FILE':str(secret)})
        self.assertIsInstance(server.thunder_connectsafely_webhook, ConnectSafelyWebhook)
        self.assertEqual(server.thunder_connectsafely_webhook.account, ACCOUNT)


if __name__ == '__main__':
    unittest.main()
