"""Loopback-only transport checks; no provider or credentials involved."""
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from thunder_os import seqd_client


class DeadlineTests(unittest.TestCase):
    def run_server(self, slow_part):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        requests = []

        def serve():
            conn, _ = listener.accept()
            try:
                conn.settimeout(2)
                request = b''
                while b'\r\n\r\n' not in request:
                    request += conn.recv(4096)
                requests.append(request)
                payload = json.dumps({'mailbox': seqd_client.MAILBOX}).encode()
                header = b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n'
                prefix, trickle = (b'', header + payload) if slow_part == 'header' else (header, payload)
                conn.sendall(prefix)
                for byte in trickle:
                    conn.sendall(bytes([byte]))
                    time.sleep(.03)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                conn.close()
                listener.close()

        worker = threading.Thread(target=serve)
        worker.start()
        original_connect = socket.create_connection

        def connect(_address, *args, **kwargs):
            return original_connect(('127.0.0.1', listener.getsockname()[1]), *args, **kwargs)

        return worker, requests, connect

    def test_trickle_has_absolute_deadline_for_reads_and_send(self):
        for method, path, part in [('GET', '/v1/status', 'header'),
                                   ('POST', '/v1/reconcile', 'body'),
                                   ('POST', '/v1/send', 'body')]:
            with self.subTest(method=method, path=path):
                worker, requests, connect = self.run_server(part)
                client = seqd_client.SEQDClient(lambda: 'x' * 32,
                    request=lambda m, p, b, h, _: seqd_client._request(m, p, b, h, .25))
                started = time.monotonic()
                try:
                    with patch('socket.create_connection', side_effect=connect):
                        with self.assertRaisesRegex(seqd_client.SEQDError, '^seqd_request_uncertain$'):
                            client._call(method, path, {} if method == 'POST' else None)
                    self.assertLess(time.monotonic() - started, .8)
                finally:
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(requests), 1)

    def test_normal_response_and_connection_close(self):
        worker, requests, connect = self.run_server('body')
        try:
            with patch('socket.create_connection', side_effect=connect):
                status, body = seqd_client._request('GET', '/v1/status', None, {}, 4)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['mailbox'], seqd_client.MAILBOX)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(requests), 1)

    def test_blocked_send_returns_uncertain_without_retry(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        accepted = []

        def refuse_to_read():
            conn, _ = listener.accept()
            accepted.append(True)
            try:
                # Leave the receive buffer full longer than the client's deadline.
                time.sleep(.7)
            finally:
                conn.close()
                listener.close()

        worker = threading.Thread(target=refuse_to_read)
        worker.start()
        original_connect = socket.create_connection
        calls = []

        def connect(_address, *args, **kwargs):
            calls.append(True)
            sock = original_connect(('127.0.0.1', port), *args, **kwargs)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            return sock

        client = seqd_client.SEQDClient(lambda: 'x' * 32,
            request=lambda m, p, b, h, _: seqd_client._request(m, p, b, h, .25))
        started = time.monotonic()
        try:
            with patch('socket.create_connection', side_effect=connect):
                with self.assertRaisesRegex(seqd_client.SEQDError, '^seqd_request_uncertain$'):
                    client.send({'body': 'x' * 4_000_000})
            self.assertLess(time.monotonic() - started, .8)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(calls), 1)

    def test_connect_receives_remaining_deadline_and_does_not_retry(self):
        observed = []

        def slow_connect(_address, timeout, *args, **kwargs):
            observed.append(timeout)
            time.sleep(timeout)
            raise TimeoutError('synthetic connect timeout')

        client = seqd_client.SEQDClient(lambda: 'x' * 32,
            request=lambda m, p, b, h, _: seqd_client._request(m, p, b, h, .15))
        started = time.monotonic()
        with patch('socket.create_connection', side_effect=slow_connect):
            with self.assertRaisesRegex(seqd_client.SEQDError, '^seqd_request_uncertain$'):
                client.status()
        self.assertLess(time.monotonic() - started, .6)
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], .15)


if __name__ == '__main__':
    unittest.main()
