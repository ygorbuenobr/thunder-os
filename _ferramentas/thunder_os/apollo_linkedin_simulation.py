"""Offline Apollo-to-LinkedIn domain exercise; no runtime or network adapters.

FakeStore provides a single explicit local repository for an installation.
Separate processes must open that same repository; this does not establish a
production storage or provider configuration.
Simulation accepts only the concrete local fakes below. Its outputs are test
evidence and cannot represent provider receipts.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

from .db import Repository

_KINDS = {'linkedin_step_connect': 'invite', 'linkedin_step_message': 'message'}
_VARIABLE = re.compile(r'\{\{|\}\}|\{%|%\}|\$\{|\[\[|\]\]')


class FakeApollo:
    """In-memory task reader and completion double; no external I/O."""
    def __init__(self, tasks):
        self.tasks = {task['id']: copy.deepcopy(task) for task in tasks}
        self.complete_outcome = 'completed'
        self.completions = 0
        self.change_on_get = None
        self.gets = 0
        self._lock = Lock()
        self._store = None

    def read(self):
        with self._lock:
            return copy.deepcopy(list(self.tasks.values()))

    def get(self, task_id):
        with self._lock:
            self.gets += 1
            if self.gets == self.change_on_get and task_id in self.tasks:
                self.tasks[task_id]['status'] = 'skipped'
            return copy.deepcopy(self.tasks.get(task_id))

    def complete(self, task_id):
        with self._lock:
            self.completions += 1
            if self.complete_outcome == 'timeout':
                raise TimeoutError('simulated Apollo timeout')
            if self.complete_outcome == 'completed':
                self.tasks[task_id]['status'] = 'completed'
                return {'status': 'completed', 'deleted': False}
            if self.complete_outcome == 'deleted':
                return {'status': 'completed', 'deleted': True}
            return {'status': 'uncertain'}


class FakeLinkedIn:
    """In-memory provider double; receipts have no external meaning."""
    def __init__(self):
        self.outcome = 'confirmed'
        self.attempts = 0
        self._lock = Lock()
        self._store = None

    def dispatch(self, action, account_id, recipient, body):
        with self._lock:
            self.attempts += 1
            if self.outcome == 'timeout':
                raise TimeoutError('simulated LinkedIn timeout')
            if self.outcome == 'confirmed':
                return {'status': 'confirmed', 'receipt': f'fake-receipt-{self.attempts}'}
            return {'status': self.outcome}


class FakeAuthority:
    """Mutable source of test-only campaign approval and reply observations."""
    def __init__(self, policy):
        self.policy = copy.deepcopy(policy)
        self.gets = 0
        self.change_on_get = None
        self.change = None
        self._lock = Lock()

    def get(self):
        with self._lock:
            self.gets += 1
            if self.gets == self.change_on_get and self.change is not None:
                self.policy.update(copy.deepcopy(self.change))
            return copy.deepcopy(self.policy)


class FakeStore:
    """A test installation owns one temporary store; other processes reopen it."""
    @classmethod
    def provision_test_store(cls):
        temporary = tempfile.TemporaryDirectory(prefix='thunder-apollo-simulation-')
        path = Path(temporary.name) / 'simulation.sqlite'
        repository = Repository(path)
        repository.append_state('apollo_simulation_installation', 'official', {'format': 1})
        store = cls(path)
        store._temp = temporary
        return store

    def __init__(self, official_path=None):
        self._pid = os.getpid()
        self._temp = None
        if official_path is None:
            raise RuntimeError('official_store_missing')
        self.path = Path(official_path).resolve()
        if not self.path.is_file():
            raise RuntimeError('official_store_missing')
        try:
            with sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True) as con:
                row = con.execute("SELECT payload FROM thunder_state WHERE kind=? AND entity_id=?",
                                  ('apollo_simulation_installation', 'official')).fetchone()
                if not row or json.loads(row[0]).get('format') != 1:
                    raise RuntimeError('official_store_invalid')
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError('official_store_invalid') from exc
        self.repo = Repository(self.path)

    def assert_owner(self):
        if os.getpid() != self._pid:
            raise RuntimeError('simulation_store_wrong_process')

    def close(self):
        if self._temp is not None:
            self._temp.cleanup()

    @contextmanager
    def account_lock(self, account_id):
        self.assert_owner()
        lock_name = hashlib.sha256(account_id.encode()).hexdigest()
        with open(self.path.parent / ('account-' + lock_name + '.lock'), 'a+') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _key(policy, task_id, action):
    identity = [policy.get('company_id'), task_id, action]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


def _task_fingerprint(task):
    value = {key: value for key, value in task.items() if key != 'status'}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _valid(task, p):
    if not isinstance(task, dict) or not isinstance(p, dict):
        return False
    ids = ('company_id', 'apollo_org_id', 'sequence_id', 'campaign_id', 'account_id')
    if any(not isinstance(p.get(k), str) or not p[k].strip() for k in ids):
        return False
    if type(p.get('campaign_version')) is not int or p['campaign_version'] < 1:
        return False
    if task.get('company_id') != p['company_id'] or task.get('apollo_org_id') != p['apollo_org_id']:
        return False
    if not isinstance(p.get('allowed_account_ids'), list) or p['account_id'] not in p['allowed_account_ids']:
        return False
    if p.get('authorized') is not True or p.get('reply_checked') is not True or p.get('has_reply') is not False:
        return False
    age = p.get('reply_age_seconds')
    if type(age) not in (int, float) or not 0 <= age <= 60:
        return False
    kind = task.get('type')
    if kind not in _KINDS or not isinstance(p.get('allowed_types'), list) or kind not in p['allowed_types']:
        return False
    sequence, contact = task.get('sequence'), task.get('contact')
    if not isinstance(sequence, dict) or sequence.get('id') != p['sequence_id']:
        return False
    if type(sequence.get('step_position')) is not int or sequence['step_position'] < 1:
        return False
    if not isinstance(contact, dict) or not isinstance(contact.get('id'), str) or not contact['id']:
        return False
    url = contact.get('linkedin_url')
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('linkedin.com', 'www.linkedin.com') or not parsed.path.startswith('/in/'):
        return False
    if task.get('status') != 'scheduled' or not isinstance(task.get('id'), str) or not task['id']:
        return False
    if not isinstance(task.get('user_id'), str) or not task['user_id'] or task['user_id'] != p.get('owner_id'):
        return False
    body = task.get('body')
    if not isinstance(body, str) or body != p.get('approved_body') or _VARIABLE.search(body):
        return False
    if kind == 'linkedin_step_message' and not body.strip():
        return False
    try:
        due = datetime.fromisoformat(task['due_at'].replace('Z', '+00:00'))
        if due.tzinfo is None or due > datetime.now(timezone.utc):
            return False
    except (KeyError, ValueError, TypeError, AttributeError):
        return False
    return True


class Simulation:
    """Execute a fully offline flow using the existing transactional repository."""
    def __init__(self, apollo, linkedin, authority, store):
        if store is None:
            raise RuntimeError('official_store_missing')
        if (type(apollo) is not FakeApollo or type(linkedin) is not FakeLinkedIn
                or type(authority) is not FakeAuthority or type(store) is not FakeStore):
            raise TypeError('simulation_requires_local_fakes')
        store.assert_owner()
        # A fake provider represents one simulated external system. Binding it
        # to two stores would silently discard reservations and replay effects.
        # Fixed lock ordering makes concurrent simulator creation deterministic.
        with apollo._lock, linkedin._lock:
            if (apollo._store is not None and apollo._store is not store
                    or linkedin._store is not None and linkedin._store is not store):
                raise ValueError('simulation_store_mismatch')
            apollo._store = store
            linkedin._store = store
        self.apollo = apollo
        self.linkedin = linkedin
        self.authority = authority
        self.repo = store.repo
        self.store = store

    def read_tasks(self):
        return self.apollo.read()

    def get_task(self, task_id):
        return self.apollo.get(task_id)

    def execute(self, task_id, policy):
        self.store.assert_owner()
        if not isinstance(policy, dict) or not isinstance(policy.get('account_id'), str) or not policy['account_id']:
            return {'state': 'blocked'}
        with self.store.account_lock(policy['account_id']) as acquired:
            if not acquired:
                return {'state': 'blocked', 'reason': 'account_locked'}
            return self._execute_locked(task_id, policy)

    def _execute_locked(self, task_id, policy):
        initial = self.apollo.get(task_id)
        if not initial or initial.get('type') not in _KINDS:
            return {'state': 'blocked', 'reason': 'eligibility_unverified'}
        key = _key(policy, task_id, _KINDS[initial['type']])
        approval = {k: v for k, v in policy.items() if k not in ('reply_age_seconds', 'reply_checked', 'has_reply')}
        fingerprint = hashlib.sha256(json.dumps(approval, sort_keys=True, separators=(',', ':'),
                                                  ensure_ascii=False).encode()).hexdigest()
        with self.repo.transaction():
            previous = self.repo.get_state('apollo_linkedin_simulation', key)
            if previous:
                if previous.get('fingerprint') != fingerprint:
                    return {'state': 'blocked', 'reason': 'authorization_changed'}
                observed = self.apollo.get(task_id)
                if not observed or _task_fingerprint(observed) != previous.get('task_fingerprint'):
                    return {'state': 'blocked', 'reason': 'task_changed'}
                if previous['state'] in ('provider_confirmed', 'apollo_uncertain', 'apollo_unconfirmed'):
                    if observed and observed.get('status') == 'completed':
                        previous['state'] = 'apollo_completed'
                        self.repo.put_state('apollo_linkedin_simulation', key, previous)
                return previous
            task = self.apollo.get(task_id)
            if task != initial:
                return {'state': 'blocked', 'reason': 'task_changed'}
            if self.authority.get() != policy or not _valid(task, policy):
                return {'state': 'blocked', 'reason': 'eligibility_unverified'}
            task_fingerprint = _task_fingerprint(task)
            self.repo.put_state('apollo_linkedin_simulation', key, {'state': 'reserved', 'fingerprint': fingerprint,
                                                                     'task_fingerprint': task_fingerprint})

        latest_policy = self.authority.get()
        if latest_policy != policy or not _valid(task, latest_policy):
            record = {'state': 'blocked', 'reason': 'authority_changed', 'fingerprint': fingerprint,
                      'task_fingerprint': task_fingerprint}
            self.repo.put_state('apollo_linkedin_simulation', key, record)
            return record
        if self.apollo.get(task_id) != task:
            record = {'state': 'blocked', 'reason': 'task_changed', 'fingerprint': fingerprint,
                      'task_fingerprint': task_fingerprint}
            self.repo.put_state('apollo_linkedin_simulation', key, record)
            return record
        try:
            result = self.linkedin.dispatch(_KINDS[task['type']], policy['account_id'],
                                            task['contact']['linkedin_url'], task['body'])
        except TimeoutError:
            result = {'status': 'uncertain'}
        if result.get('status') != 'confirmed' or not result.get('receipt'):
            state = 'provider_rejected' if result.get('status') == 'rejected' else 'provider_uncertain'
            record = {'state': state, 'fingerprint': fingerprint, 'task_fingerprint': task_fingerprint}
            self.repo.put_state('apollo_linkedin_simulation', key, record)
            return record
        record = {'state': 'provider_confirmed', 'receipt': result['receipt'], 'fingerprint': fingerprint,
                  'task_fingerprint': task_fingerprint}
        self.repo.put_state('apollo_linkedin_simulation', key, record)
        current = self.apollo.get(task_id)
        if current != task:
            record['state'] = 'apollo_unconfirmed'
            self.repo.put_state('apollo_linkedin_simulation', key, record)
            return record
        latest_policy = self.authority.get()
        if latest_policy != policy or not _valid(current, latest_policy):
            record['state'] = 'apollo_unconfirmed'
            record['reason'] = 'authority_changed_after_dispatch'
            self.repo.put_state('apollo_linkedin_simulation', key, record)
            return record
        try:
            outcome = self.apollo.complete(task_id)
        except TimeoutError:
            outcome = {'status': 'uncertain'}
        reread = self.apollo.get(task_id)
        if outcome.get('status') == 'completed' and outcome.get('deleted') is False and reread and reread.get('status') == 'completed':
            record['state'] = 'apollo_completed'
        elif outcome.get('status') == 'uncertain':
            record['state'] = 'apollo_uncertain'
        else:
            record['state'] = 'apollo_unconfirmed'
        self.repo.put_state('apollo_linkedin_simulation', key, record)
        return record
