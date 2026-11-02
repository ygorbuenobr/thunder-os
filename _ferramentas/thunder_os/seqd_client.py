"""Pinned loopback SEQD port. No proxy, redirect, retry or credential logging."""
import hashlib
import hmac
import http.client
import io
import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import quote, urlencode

MAILBOX = 'ygor.bueno@thundermustard.com'
MAILBOX_ID = '452c6677-5ead-41d8-b036-6ef63945752d'
WORKSPACE_ID = '80d9245b-bc72-420e-b48b-f72e63b8660b'

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


class SEQDError(ValueError):
    def __init__(self, code, status=None):
        self.status = status
        super().__init__(code)


def credential_file():
    explicit = os.environ.get('THUNDER_OS_SEQD_CREDENTIAL_FILE', '')
    directory = os.environ.get('CREDENTIALS_DIRECTORY', '')
    path = Path(explicit) if explicit else Path(directory)/'seqd_bridge'
    if not path.is_absolute() or path.name != 'seqd_bridge':
        raise SEQDError('seqd_credential_unavailable')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'r') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 4096
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid not in {0, os.geteuid()}):
                raise ValueError()
            token = stream.read(4097).strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,4096}', token):
            raise ValueError()
        return token
    except Exception:
        raise SEQDError('seqd_credential_unavailable') from None


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline):
        self._sock, self._deadline = sock, deadline
        self._raw = sock.makefile('rb', buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        self._sock.settimeout(_remaining(self._deadline))
        return self._raw.readinto(buffer)

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('seqd deadline exceeded')
    return remaining


class _DeadlineSocket:
    def __init__(self, sock, deadline):
        self._sock, self._deadline = sock, deadline

    def sendall(self, data):
        self._sock.settimeout(_remaining(self._deadline))
        self._sock.sendall(data)

    def makefile(self, mode, buffering=None):
        if mode != 'rb':
            raise ValueError('unsupported socket mode')
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline))

    def close(self):
        self._sock.close()


class _DeadlineConnection(http.client.HTTPConnection):
    def __init__(self, deadline):
        self._deadline = deadline
        super().__init__('127.0.0.1', 8792, timeout=_remaining(deadline))

    def connect(self):
        self.timeout = _remaining(self._deadline)
        super().connect()
        self.sock = _DeadlineSocket(self.sock, self._deadline)


def _request(method, path, body, headers, timeout):
    deadline = time.monotonic() + timeout
    connection = _DeadlineConnection(deadline)
    response = None
    try:
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(1_000_001)
    finally:
        if response is not None:
            response.close()
        connection.close()


class SEQDClient:
    def __init__(self, credential_loader=credential_file, *, request=None):
        self._loader = credential_loader
        self._request = request or _request

    def _call(self, method, path, body=None):
        try:
            token = self._loader()
            if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,4096}', token):
                raise SEQDError('seqd_credential_unavailable')
            if method == 'POST' and path == '/v1/send':
                body = dict(body)
                body['approval_signature'] = hmac.new(token.encode(), b'seqd-approval-v1\n'+canonical(body), hashlib.sha256).hexdigest()
            status, raw = self._request(method, path, body, {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'}, 40)
            if type(status) is not int or not 200 <= status < 300:
                raise SEQDError('seqd_http_error', status if type(status) is int else None)
            if not isinstance(raw, bytes) or len(raw)>1_000_000:
                raise SEQDError('seqd_invalid_response')
            data=json.loads(raw)
            if not isinstance(data, dict) or data.get('mailbox') != MAILBOX:
                raise SEQDError('seqd_mailbox_mismatch')
            return data
        except SEQDError:
            raise
        except Exception:
            raise SEQDError('seqd_request_uncertain') from None

    def status(self):
        return self._call('GET', '/v1/status')

    def contact_search(self, email, cursor=None):
        if (not isinstance(email, str) or len(email) > 254 or not re.fullmatch(
                r'[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', email)):
            raise SEQDError('invalid_contact_email')
        query = {'email': email}
        if cursor is not None:
            if not isinstance(cursor, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,4096}\.[a-f0-9]{64}', cursor):
                raise SEQDError('invalid_search_cursor')
            query['cursor'] = cursor
        data = self._call('GET', '/v1/contact-search?' + urlencode(query))
        coverage = data.get('coverage')
        expected_query = f'in:anywhere {{from:{email} to:{email} cc:{email} bcc:{email}}} -in:drafts'
        if (data.get('status') != 'ok' or data.get('email') != email or data.get('query') != expected_query
                or data.get('context_only') is not True or data.get('receipt_eligible') is not False
                or not isinstance(data.get('messages'), list) or len(data['messages']) > 10
                or not all(isinstance(row, dict) and all(isinstance(row.get(key), str)
                    and re.fullmatch(r'[a-f0-9]{1,64}', row[key]) for key in ('id', 'thread_id')) for row in data['messages'])
                or not isinstance(coverage, dict) or coverage.get('scope') != 'mailbox_verified_alias'
                or coverage.get('include_spam_trash') is not True or coverage.get('window_days') is not None
                or type(coverage.get('listing_complete')) is not bool
                or coverage['listing_complete'] != (data.get('next_cursor') is None)
                or data.get('next_cursor') is not None and (not isinstance(data['next_cursor'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,4096}\.[a-f0-9]{64}', data['next_cursor']))):
            raise SEQDError('invalid_contact_search')
        return data

    def messages(self, limit=10, cursor=None):
        if type(limit) is not int or not 1<=limit<=10:
            raise SEQDError('invalid_limit')
        query = {'limit': limit}
        if cursor is not None:
            if not isinstance(cursor,str) or len(cursor)>512 or any(ord(c)<32 for c in cursor): raise SEQDError('invalid_cursor')
            query['cursor']=cursor
        return self._call('GET', '/v1/messages?'+urlencode(query))

    def snapshot_page(self, cursor=None):
        if cursor is not None and (not isinstance(cursor,str) or not re.fullmatch(r'[\x21-\x7e]{1,512}',cursor)):
            raise SEQDError('invalid_cursor')
        query={'limit':10}
        if cursor is not None: query['cursor']=cursor
        data=self._call('GET','/v1/snapshot/messages?'+urlencode(query))
        coverage=data.get('coverage'); next_token=data.get('nextPageToken')
        if (data.get('status')!='ok' or data.get('receipt_eligible') is not False or not isinstance(coverage,dict)
                or coverage.get('scope')!='inbox' or coverage.get('window_days') is not None
                or type(coverage.get('listing_complete')) is not bool or coverage['listing_complete'] != (next_token is None)
                or not isinstance(data.get('messages'),list) or len(data['messages'])>10
                or not all(isinstance(row,dict) and all(isinstance(row.get(key),str) and re.fullmatch(r'[a-f0-9]{1,64}',row[key]) for key in ('id','threadId')) for row in data['messages'])
                or next_token is not None and (not isinstance(next_token,str) or not re.fullmatch(r'[\x21-\x7e]{1,512}',next_token) or next_token==cursor)):
            raise SEQDError('invalid_snapshot_page')
        return data

    def snapshot_thread(self, identifier):
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{1,64}',identifier):
            raise SEQDError('invalid_thread_identifier')
        data=self._call('GET','/v1/snapshot/threads/'+identifier)
        thread=data.get('thread')
        if (data.get('status')!='ok' or data.get('receipt_eligible') is not False or not isinstance(thread,dict)
                or thread.get('id')!=identifier or not isinstance(thread.get('historyId'),str)
                or not re.fullmatch(r'\d{1,30}',thread['historyId']) or not isinstance(thread.get('messages'),list)):
            raise SEQDError('invalid_snapshot_thread')
        return thread

    @staticmethod
    def _id(identifier):
        if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', identifier):
            raise SEQDError('invalid_identifier')
        return quote(identifier, safe='')

    def read(self, identifier):
        return self._call('GET', '/v1/messages/'+self._id(identifier))

    def conversation(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{1,64}', identifier):
            raise SEQDError('invalid_thread_identifier')
        result = self._call('GET', '/v1/threads/'+identifier)
        thread = result.get('thread')
        if (result.get('status') != 'ok' or not isinstance(thread, dict)
                or thread.get('id') != identifier or thread.get('context_only') is not True
                or thread.get('receipt_eligible') is not False
                or not isinstance(thread.get('messages'), list) or len(thread['messages']) > 100
                or type(thread.get('complete')) is not bool
                or type(thread.get('truncated')) is not bool
                or not isinstance(thread.get('coverage'), dict)):
            raise SEQDError('invalid_thread_context')
        return result

    def reconcile(self, key):
        self._id(key)
        return self._call('POST', '/v1/reconcile', {'idempotency_key':key})

    def send(self, envelope):
        if not isinstance(envelope,dict) or 'approval_signature' in envelope or 'mailbox' in envelope:
            raise SEQDError('invalid_mailbox')
        return self._call('POST', '/v1/send', envelope)
