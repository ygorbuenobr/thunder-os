import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch
import mexico_supervisor as m


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        m.BATCHES = ('MX27-MTY-L01',)
        m.ACCOUNT = '0' * 24

    def task(self, status='COMPLETED', body='', title='[MX27-MTY-L01][T01] Company | EMAIL'):
        return {'id': '123', 'properties': {'hs_task_subject': title, 'hs_task_body': body, 'hs_task_status': status}}

    def test_completed_does_not_prove_sent(self):
        alerts, _ = m.classify([self.task(body='Conferir recibo antes de agir')], 0)
        self.assertEqual(alerts[0]['key'], 'receipt:123')

    def test_disposed_not_touch(self):
        alerts, summary = m.classify([self.task(title='[DISPENSADA SEM ENVIO] [MX27-MTY-L01][T02] Risoul')], 0)
        self.assertEqual(alerts, []); self.assertEqual(summary['completed'], 0)

    def test_waiting_does_not_mean_overdue(self):
        self.assertEqual(m.classify([self.task(status='WAITING')], 9999999999)[0], [])

    def test_receipt_reference(self):
        self.assertEqual(m.classify([self.task(body='ENVIO CONFIRMADO Gmail SENT id=1a155dc9eac44989')], 0)[0], [])

    def test_dedupe_and_material_change(self):
        item = m.incident('a', 'HTTP404', 'Revisar')
        state = {'acknowledged': [m.fingerprint(item)]}
        self.assertEqual(m.select_alerts({'incidents': [item]}, state), [])
        self.assertEqual(len(m.select_alerts({'incidents': [dict(item, detail='HTTP429')]}, state)), 1)

    def test_writes_profile_views_wrong_identity_fail_closed(self):
        with patch('urllib.request.urlopen') as network:
            for host, path, data in [('api.hubapi.com', '/crm/v3/objects/tasks/123', {}),
                                     ('api.connectsafely.ai', '/linkedin/connect', {}),
                                     ('api.connectsafely.ai', '/linkedin/profile', None),
                                     ('api.connectsafely.ai', '/linkedin/account/LOA/status', None)]:
                with self.assertRaises(ValueError): m.fetch(host, path, 'unused', data)
            network.assert_not_called()

    def test_mac_stale_blocked_and_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'observation.json'
            cfg = {'mac_observation': str(p), 'campaign': 'test', 'loa_account_id': 'loa', 'batches': ['test']}
            self.assertEqual(m.mac_coverage(cfg, 100)[1]['state'], 'UNAVAILABLE')
            data = {'schema': 2, 'campaign': 'test', 'account_id': 'loa', 'batches': ['test'],
                    'state': 'BLOCKED', 'error_code': 'http_429'}
            p.write_text(json.dumps(data))
            self.assertEqual(m.mac_coverage(cfg, 100)[1]['state'], 'BLOCKED')
            data.update(state='OK', observed_at=100, invitations=[{'task_id': str(i), 'state': 'sent_pending'} for i in range(3)], receipt_references=[])
            p.write_text(json.dumps(data))
            self.assertEqual(m.mac_coverage(cfg, 200)[0], [])
            self.assertEqual(m.mac_coverage(cfg, 1500)[1]['state'], 'UNAVAILABLE')


if __name__ == '__main__': unittest.main()
