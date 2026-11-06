#!/usr/bin/env python3
"""Fail-stop, bounded Mac observer. Existing sync invokes it; it never syncs/sends.

All operational paths/accounts are supplied by a secret-free local config.
Every failure latches BLOCKED until a human reviews and explicitly rearms it.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

MAX_FILE = 524288
MAX_OUTPUT = 65536


class Stopped(Exception):
    pass


def atomic(path, data):
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if len(raw.encode()) > MAX_OUTPUT: raise Stopped('output_too_large')
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(temp, path)


def bounded_text(path):
    if path.stat().st_size > MAX_FILE: raise Stopped('input_too_large')
    with path.open('rb') as stream: raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE: raise Stopped('input_too_large')
    return raw.decode()


class Reader:
    def __init__(self, account, token):
        self.account, self.token, self.calls = account, token, 0

    def get(self, path):
        allowed = {'/linkedin/account/' + self.account + '/status',
                   '/linkedin/account/' + self.account + '/quota',
                   '/linkedin/invitations/sent?accountId=' + self.account + '&startIndex=0'}
        relationship = '/linkedin/relationship/' + self.account + '/'
        if path not in allowed and not (path.startswith(relationship) and re.fullmatch(r'[A-Za-z0-9_%.-]+', path[len(relationship):])):
            raise Stopped('route_not_allowed')
        if self.calls >= 6: raise Stopped('call_budget_exceeded')
        self.calls += 1
        request = urllib.request.Request('https://api.connectsafely.ai' + path,
            headers={'Authorization': 'Bearer ' + self.token})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = response.read(MAX_FILE + 1)
                if len(raw) > MAX_FILE: raise Stopped('response_too_large')
                data = json.loads(raw)
                if not isinstance(data, dict): raise Stopped('invalid_response')
                if data.get('success') is False or data.get('error'): raise Stopped('provider_error')
                return data
        except urllib.error.HTTPError as error:
            raise Stopped('http_' + str(error.code)) from None
        except (OSError, ValueError):
            raise Stopped('network_or_schema_error') from None


def collect(config):
    mcp = tomllib.loads(bounded_text(Path(config['mcp_config'])))['mcp_servers'][config['connection']]
    url = urllib.parse.urlsplit(mcp['url']); query = urllib.parse.parse_qs(url.query)
    account = config['account_id']
    if url.scheme != 'https' or url.hostname != 'mcp.connectsafely.ai' or query.get('accountId') != [account]:
        raise Stopped('connection_identity_mismatch')
    token = query['apiKey'][0]
    reader = Reader(account, token)
    status = reader.get('/linkedin/account/' + account + '/status')
    if status.get('id') != account or status.get('publicId') != config['public_id'] or status.get('status') != 'AVAILABLE':
        raise Stopped('account_not_available_or_mismatch')
    quotas = reader.get('/linkedin/account/' + account + '/quota')
    if not isinstance(quotas.get('quotas'), list): raise Stopped('quota_schema')
    if any(q.get('limitReached') for q in quotas['quotas']): raise Stopped('quota_limit_reached')
    receipts, expected = [], {}
    for spec in config['ledgers']:
        path = Path(spec['path']); raw = bounded_text(path)
        rows = json.loads(raw) if path.suffix == '.json' else [json.loads(line) for line in raw.splitlines() if line.strip()]
        if not isinstance(rows, list) or len(rows) > 200: raise Stopped('ledger_schema_or_size')
        for row in rows:
            batch = row.get('batch', spec.get('batch'))
            if batch not in config['batches']: raise Stopped('ledger_batch_mismatch')
            task = str(row.get('task_id', row.get('task', '')))
            receipt = row.get('message_id', row.get('id'))
            if not re.fullmatch(r'\d{1,20}', task): raise Stopped('ledger_task_invalid')
            item = {'batch': batch, 'task_id': task, 'state': 'receipt_reference'}
            if receipt is not None:
                if not re.fullmatch(r'[a-fA-F0-9]{10,40}', str(receipt)): raise Stopped('receipt_id_invalid')
                item['receipt_id'] = str(receipt)
            receipts.append(item)
            if row.get('accountId') == account:
                urn = row.get('profileUrn', '')
                if not re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]+', urn): raise Stopped('ledger_profile_invalid')
                expected[urn] = task
    if len(expected) != 3: raise Stopped('loa_scope_changed_review_required')
    invitations = reader.get('/linkedin/invitations/sent?accountId=' + account + '&startIndex=0')
    if invitations.get('accountId') != account or not isinstance(invitations.get('invitations'), list):
        raise Stopped('invitations_identity_or_schema')
    states = {}
    for row in invitations['invitations']:
        urn = row.get('memberUrn')
        if urn in expected:
            iid = str(row.get('invitationId', ''))
            if not re.fullmatch(r'\d{1,30}', iid): raise Stopped('invitation_id_invalid')
            states[expected[urn]] = {'task_id': expected[urn], 'state': 'sent_pending', 'invitation_id': iid}
    # Missing from the first page never means accepted; only scoped relationship reads.
    for urn, task in expected.items():
        if task in states: continue
        relation = reader.get('/linkedin/relationship/' + account + '/' + urllib.parse.quote(urn, safe=''))
        if relation.get('accountId') != account: raise Stopped('relationship_identity_unverified')
        if relation.get('connected') is True or relation.get('isConnected') is True:
            state = 'accepted'
        elif relation.get('invitationSent') is True:
            state = 'sent_pending'
        else:
            state = 'uncertain'
        states[task] = {'task_id': task, 'state': state}
    return {'schema': 2, 'state': 'OK', 'observed_at': time.time(), 'account_id': account,
            'campaign': config['campaign'], 'batches': config['batches'],
            'receipt_references': receipts, 'invitations': list(states.values()),
            'call_count': reader.calls, 'gmail_coverage': 'not_collected'}


def run(config, collect_fn=collect, clock=time.time):
    root = Path(config['state_dir']); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = Path(config['output']); output.parent.mkdir(parents=True, exist_ok=True)
    with (root / 'lock').open('w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 'LOCKED'
        blocked = root / 'blocked.json'; last = root / 'last.json'
        if blocked.exists():
            # Reproject a lost error marker without any network retry.
            if not output.exists(): atomic(output, json.loads(bounded_text(blocked)))
            return 'BLOCKED'
        if last.exists() and clock() - json.loads(bounded_text(last))['observed_at'] < 590:
            return 'THROTTLED'
        started = clock()
        try:
            data = collect_fn(config)
            data['duration_ms'] = round((clock() - started) * 1000)
            atomic(output, data); atomic(last, {'observed_at': data['observed_at']})
            return 'OK'
        except Exception as exc:
            code = str(exc) if isinstance(exc, Stopped) else 'local_input_or_runtime_error'
            data = {'schema': 2, 'state': 'BLOCKED', 'observed_at': clock(),
                    'error_code': code, 'campaign': config['campaign'], 'account_id': config['account_id'],
                    'batches': config['batches'], 'automatic_retry': False}
            atomic(blocked, data); atomic(output, data)
            return 'BLOCKED'


def deadline(signum, frame):
    raise Stopped('deadline_45s')


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--config', required=True); args = parser.parse_args()
    config = json.loads(bounded_text(Path(args.config)))
    signal.signal(signal.SIGALRM, deadline); signal.alarm(45)
    try: result = run(config)
    finally: signal.alarm(0)
    print('mexico_observer=' + result)


if __name__ == '__main__': main()
