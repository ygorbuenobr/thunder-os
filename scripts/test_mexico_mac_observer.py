import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import mexico_mac_observer as m


class ObserverTests(unittest.TestCase):
    def config(self, tmp):
        return {'state_dir': str(Path(tmp) / 'state'), 'output': str(Path(tmp) / 'output.json'),
                'campaign': 'test', 'account_id': '0' * 24, 'batches': ['test']}

    def test_failure_stops_future_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp); call = Mock(side_effect=m.Stopped('http_429'))
            self.assertEqual(m.run(cfg, call), 'BLOCKED')
            self.assertEqual(m.run(cfg, call), 'BLOCKED')
            call.assert_called_once()
            self.assertIn('http_429', Path(cfg['output']).read_text())

    def test_success_throttle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(tmp); call = Mock(return_value={'observed_at': 100, 'state': 'OK'})
            self.assertEqual(m.run(cfg, call, lambda: 100), 'OK')
            self.assertEqual(m.run(cfg, call, lambda: 200), 'THROTTLED')
            call.assert_called_once()

    def test_no_profile_write_wrong_account(self):
        reader = m.Reader('0' * 24, 'fake')
        with patch('urllib.request.urlopen') as network:
            for path in ['/linkedin/profile', '/linkedin/connect', '/linkedin/account/other/status']:
                with self.assertRaises(m.Stopped): reader.get(path)
            network.assert_not_called()

    def test_budget(self):
        reader = m.Reader('0' * 24, 'fake'); reader.calls = 6
        with self.assertRaises(m.Stopped): reader.get('/linkedin/account/' + '0' * 24 + '/status')

    def test_file_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'file'; p.write_bytes(b'x' * (m.MAX_FILE + 1))
            with self.assertRaises(m.Stopped): m.bounded_text(p)


if __name__ == '__main__': unittest.main()
