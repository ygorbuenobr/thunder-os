"""Pinned loopback SEQD port. No proxy, redirect, retry or credential logging."""
import hashlib
import hmac
import http.client
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote, urlencode

MAILBOX = 'ygor.bueno@thundermustard.com'
MAILBOX_ID = '452c6677-5ead-41d8-b036-6ef63945752d'
WORKSPACE_ID = '80d9245b-bc72-420e-b48b-f72e63b8660b'

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


class SEQDError(ValueError):
    pass


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


def _request(method, path, body, headers, timeout):
    connection = http.client.HTTPConnection('127.0.0.1', 8792, timeout=timeout)
    try:
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(1_000_001)
    finally:
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
                raise SEQDError('seqd_http_error')
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

    def messages(self, limit=10, cursor=None):
        if type(limit) is not int or not 1<=limit<=10:
            raise SEQDError('invalid_limit')
        query = {'limit': limit}
        if cursor is not None:
            if not isinstance(cursor,str) or len(cursor)>512 or any(ord(c)<32 for c in cursor): raise SEQDError('invalid_cursor')
            query['cursor']=cursor
        return self._call('GET', '/v1/messages?'+urlencode(query))

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
