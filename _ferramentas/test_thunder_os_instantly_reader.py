import http.client
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import painel_servidor as panel
from thunder_os.db import Repository
from thunder_os.external_email_import import ExternalEmailError
from thunder_os import instantly_reader as mod


def row(number=1, **changes):
    return {'id': f'00000000-0000-4000-8000-{number:012d}',
        'organization_id': mod.INSTANTLY_WORKSPACE, 'campaign_id': mod.INSTANTLY_CAMPAIGN,
        'lead': 'lead@example.invalid', 'eaccount': 'sender@example.invalid',
        'from_address_email': 'sender@example.invalid', 'to_address_email_list': 'lead@example.invalid',
        'ue_type': 1, 'is_auto_reply': None, 'timestamp_email': '2026-10-22T12:00:00Z',
        'subject': 'Synthetic', 'body': {'text': 'Synthetic content'}, **changes}


def incoming(**changes):
    return row(2, ue_type=2, from_address_email='lead@example.invalid',
               to_address_email_list='sender@example.invalid', **changes)


class ObservationTests(unittest.TestCase):
    def test_sent_scheduled_auto_human_unknown(self):
        self.assertEqual(mod.observation(row())[1]['event_type'], 'email_sent')
        self.assertIsNone(mod.observation(row(ue_type=4)))
        cases = [(incoming(is_auto_reply=1), 'auto_reply_received', 'provider'),
                 (incoming(is_auto_reply=0), 'reply_received', 'provider'),
                 (incoming(), 'unclassified_reply', 'unknown'),
                 (incoming(is_auto_reply=False), 'unclassified_reply', 'unknown'),
                 (incoming(subject='Resposta automática: Hello'), 'auto_reply_received', 'subject')]
        for value, kind, source in cases:
            with self.subTest(kind=kind, value=value['is_auto_reply']):
                event = mod.observation(value)[1]
                self.assertEqual((event['event_type'], event['classification_source']), (kind, source))

    def test_html_is_plain_text_no_script_and_excerpt_is_explicit(self):
        event = mod.observation(row(body={'html': '<p>Hello<br>World</p><script>unsafe()</script>'}))[1]
        self.assertIn('Hello\nWorld', event['text'])
        self.assertNotIn('unsafe', event['text'])
        self.assertTrue(event['content_complete'])
        for value in (row(body_fallback_reason='too_large'), row(body={'text': 'x' * 21000})):
            event = mod.observation(value)[1]
            self.assertFalse(event['content_complete'])
            self.assertLessEqual(len(event['text']), 20000)

    def test_pin_identity_and_direction(self):
        for changes in ({'organization_id': 'wrong'}, {'campaign_id': 'wrong'},
                        {'lead': 'other@example.invalid'}, {'from_address_email': 'other@example.invalid'},
                        {'id': 'not-a-uuid'}, {'timestamp_email': 'yesterday'}, {'ue_type': True}):
            with self.subTest(changes=changes), self.assertRaises(ExternalEmailError):
                mod.observation(row(**changes))


class ProviderTests(unittest.TestCase):
    def provider(self, pages, workspace=mod.INSTANTLY_WORKSPACE):
        self.calls = []
        def request(method, path, body, headers, timeout):
            self.calls.append((method, path, body))
            value = {'id': workspace} if path.endswith('/current') else pages.pop(0)
            return 200, json.dumps(value).encode()
        return mod.InstantlyCampaignReadOnly(lambda: 'synthetic-secret', request=request)

    def test_complete_pagination_deduplicates_and_uses_only_pinned_get(self):
        provider = self.provider([{'items': [row(), incoming(subject='Automatic reply: Synthetic')],
            'next_starting_after': row(2)['id']}, {'items': [row()], 'next_starting_after': None}])
        result = provider.collect()
        self.assertEqual(result['metrics'], {'sent_messages': 1, 'leads_with_sent': 1,
            'human_replies': 0, 'automatic_replies': 1, 'unclassified_replies': 0})
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['coverage']['pages'], 2)
        for method, path, body in self.calls:
            self.assertEqual(method, 'GET')
            self.assertIsNone(body)
            self.assertTrue(path.endswith('/current') or ('campaign_id=' + mod.INSTANTLY_CAMPAIGN) in path)

    def test_bad_workspace_stops_before_emails(self):
        provider = self.provider([], workspace='other')
        with self.assertRaisesRegex(ExternalEmailError, 'workspace_mismatch'):
            provider.collect()
        self.assertEqual(len(self.calls), 1)

    def test_invalid_record_marks_partial_without_counting_other_campaign(self):
        provider = self.provider([{'items': [row(), row(2, campaign_id='other')]}])
        result = provider.collect()
        self.assertEqual(result['metrics']['sent_messages'], 1)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['coverage']['invalid_records'], 1)

    def test_repeated_cursor_and_page_cap(self):
        pages = [{'items': [row()], 'next_starting_after': row()['id']}] * 2
        with self.assertRaisesRegex(ExternalEmailError, 'pagination_repeated'):
            self.provider(pages).collect()
        pages = [{'items': [row(i)], 'next_starting_after': row(i)['id']} for i in range(1, 11)]
        result = self.provider(pages).collect()
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(result['coverage']['limit_reached'])
        self.assertEqual(len(self.calls), 11)  # 10 list reads < 20/min; no inline retry.

    def test_errors_redacted_and_endpoint_allowlist(self):
        for status in (401, 403, 429, 500):
            provider = mod.InstantlyCampaignReadOnly(lambda: 'synthetic-secret',
                request=lambda *args: (status, b'private-provider-body'))
            with self.assertRaises(ExternalEmailError) as caught:
                provider.collect()
            self.assertNotIn('private', str(caught.exception))
        provider = self.provider([])
        for path in ('/api/v2/emails/send', '/api/v2/campaigns', 'https://other.invalid'):
            with self.assertRaisesRegex(ExternalEmailError, 'endpoint_blocked'):
                provider._get(path)
        self.assertFalse(self.calls)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repository(Path(self.tmp.name) / 'test.db')
        self.now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        provider = mod.InstantlyCampaignReadOnly(lambda: 'synthetic', request=lambda *args:
            (200, json.dumps({'id': mod.INSTANTLY_WORKSPACE} if args[1].endswith('/current') else {'items': [row()]}).encode()))
        self.provider = Mock()
        self.provider.collect.return_value = provider.collect()
        self.reader = mod.InstantlyReader(self.repo, self.provider, enable_check=lambda: True, clock=lambda: self.now)

    def test_disabled_no_network_no_db_and_no_old_data_exposed(self):
        self.reader.enable_check = lambda: False
        self.reader.refresh()
        self.provider.collect.assert_not_called()
        self.assertEqual(self.reader.snapshot()['status'], 'disabled')
        self.assertEqual(self.repo.list_state(mod.KIND), [])

    def test_persist_restart_throttle_and_cache_only_get(self):
        self.reader.refresh()
        self.reader.refresh()
        self.provider.collect.assert_called_once()
        new_reader = mod.InstantlyReader(self.repo, self.provider, enable_check=lambda: True, clock=lambda: self.now)
        self.assertEqual(new_reader.snapshot()['metrics']['sent_messages'], 1)
        self.provider.collect.assert_called_once()
        with self.repo._connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM thunder_outbox').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM thunder_events').fetchone()[0], 0)

    def test_failure_keeps_lastgood_date_and_never_becomes_zero(self):
        self.reader.refresh()
        good = self.reader.snapshot()
        self.now += timedelta(seconds=61)
        self.provider.collect.side_effect = RuntimeError('private-token-and-provider-body')
        self.reader.refresh()
        observed = self.reader.snapshot()
        self.assertEqual(observed['status'], 'error')
        self.assertEqual(observed['last_success_at'], good['last_success_at'])
        self.assertEqual(observed['metrics'], good['metrics'])
        self.assertNotIn('private', json.dumps(observed))

    def test_first_failure_is_unknown_and_age_becomes_stale(self):
        self.provider.collect.side_effect = ExternalEmailError('instantly_rate_limited')
        self.reader.refresh()
        self.assertIsNone(self.reader.snapshot()['metrics'])
        self.provider.collect.side_effect = None
        self.now += timedelta(seconds=61)
        self.reader.refresh()
        self.now += timedelta(seconds=181)
        self.assertEqual(self.reader.snapshot()['status'], 'stale')

    def test_private_secret_file_permissions_symlink_and_enable(self):
        path = Path(self.tmp.name) / 'key'
        path.write_text('read-only-v1')
        path.chmod(0o600)
        self.assertEqual(mod.private_text(path), 'read-only-v1')
        with patch.object(mod, 'ENABLED_FILE', str(path)):
            self.assertTrue(mod.enabled())
            path.chmod(0o644)
            self.assertFalse(mod.enabled())
        link = path.with_name('link')
        link.symlink_to(path)
        with self.assertRaises(ExternalEmailError):
            mod.private_text(link)


class HTTPTests(unittest.TestCase):
    def test_authenticated_get_cache_only_rejects_query_and_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            server = panel.ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
            server.banco = panel.Banco(Path(directory) / 'brain', Path(directory) / 'panel.db')
            server.thunder_service = SimpleNamespace()
            server.thunder_instantly_reader = Mock()
            server.thunder_instantly_reader.snapshot.return_value = {'status': 'ok', 'read_only': True}
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, suffix=''):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                try:
                    conn.request(method, '/api/os/v1/integrations/instantly/reader' + suffix)
                    response = conn.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    conn.close()
            try:
                with patch.object(panel.Handler, 'thunder_proxy_actor', return_value=None):
                    self.assertEqual(request('GET')[0], 403)
                server.thunder_instantly_reader.snapshot.assert_not_called()
                with patch.object(panel.Handler, 'thunder_proxy_actor', return_value='ygor'):
                    self.assertEqual(request('GET'), (200, {'status': 'ok', 'read_only': True}))
                    for method, suffix in (('POST', ''), ('DELETE', ''), ('GET', '?refresh=true')):
                        self.assertIn(request(method, suffix)[0], (405, 501))
                server.thunder_instantly_reader.snapshot.assert_called_once()
                server.thunder_instantly_reader.refresh.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
