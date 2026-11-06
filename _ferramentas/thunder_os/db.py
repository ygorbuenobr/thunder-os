"""Additive SQLite repository for workflow state and durable outbox."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
from threading import local
from typing import Any, Iterator
from uuid import uuid4

from .models import WorkItem, as_payload, parse_work_item, payload_hash


OFFICIAL_PANEL_DB = Path('/root/.hermes/profiles/ygor-personal/painel-dados/painel.db')
APOLLO_LINKEDIN_STORE_ENV = 'THUNDER_APOLLO_LI_OFFICIAL_DB'
APOLLO_LINKEDIN_LOCK_TTL = timedelta(minutes=5)


def _official_apollo_linkedin_db():
    """Return the configured installation store; invalid config never falls back."""
    value = os.environ.get(APOLLO_LINKEDIN_STORE_ENV)
    if value is None:
        return OFFICIAL_PANEL_DB
    if not value or value != value.strip() or not Path(value).is_absolute():
        raise RepositoryConflict('official Apollo LinkedIn store configuration invalid')
    return Path(value)


def _configured_apollo_linkedin_db():
    """Startup/migration view: invalid config disables T3, not the whole repository."""
    try:
        return _official_apollo_linkedin_db()
    except RepositoryConflict:
        return None


class RepositoryConflict(RuntimeError):
    pass


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = local()
        official = _configured_apollo_linkedin_db()
        if (official is not None and Path(self.path).is_absolute() and Path(self.path).resolve() == official.resolve()
                and not Path(self.path).is_file()):
            raise RepositoryConflict('official Apollo LinkedIn store unavailable')
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        active = getattr(self._local, "connection", None)
        if active is not None:
            return _BorrowedConnection(active)
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None, factory=_OwnedConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def migrate(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS thunder_schema (
                    version INTEGER NOT NULL
                );
                INSERT INTO thunder_schema(version)
                    SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM thunder_schema);
                CREATE TABLE IF NOT EXISTS thunder_work_items (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thunder_events (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thunder_outbox (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    lease_owner TEXT,
                    lease_until TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thunder_state (
                    kind TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(kind, entity_id)
                );
                CREATE TABLE IF NOT EXISTS thunder_effect_jobs (
                    id TEXT PRIMARY KEY,
                    effect_key TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    lease_owner TEXT,
                    lease_until TEXT,
                    receipt_ref TEXT,
                    error_code TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

        # T3 schema is provisioned only in the canonical panel database.
        official = _configured_apollo_linkedin_db()
        if official is not None and Path(self.path).is_absolute() and Path(self.path).resolve() == official.resolve():
            with self._connect() as con:
                con.executescript("""
                CREATE TABLE IF NOT EXISTS apollo_linkedin_reservations (
                    company TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','confirmed','rejected','uncertain')),
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(company, task_id, action_type)
                );
                CREATE TABLE IF NOT EXISTS apollo_linkedin_account_locks (
                    account_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    lease_until TEXT
                );
                """)
                columns = {row[1] for row in con.execute('PRAGMA table_info(apollo_linkedin_account_locks)')}
                if 'lease_until' not in columns:
                    con.execute('ALTER TABLE apollo_linkedin_account_locks ADD COLUMN lease_until TEXT')
            # Serialize additive column checks across process startups. Existing
            # reservations stay intact and retain their original CHECK constraint.
            with self.transaction() as con:
                columns = {row[1] for row in con.execute('PRAGMA table_info(apollo_linkedin_reservations)')}
                for column in ('text_hash', 'campaign_version', 'recipient', 'receipt', 'apollo_status'):
                    if column not in columns:
                        con.execute('ALTER TABLE apollo_linkedin_reservations ADD COLUMN ' + column + ' TEXT')

    def apollo_linkedin_store_ready(self):
        """Require an existing, accessible installation DB with both migrated tables."""
        path = Path(self.path)
        if (self.path == ':memory:' or not path.is_absolute() or not path.is_file()
                or path.resolve() != _official_apollo_linkedin_db().resolve()):
            raise RepositoryConflict('official Apollo LinkedIn store unavailable')
        try:
            with self._connect() as con:
                tables = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                    "('apollo_linkedin_reservations','apollo_linkedin_account_locks')")}
                if len(tables) != 2:
                    raise RepositoryConflict('official Apollo LinkedIn store not migrated')
                con.execute('SELECT text_hash,campaign_version,recipient,receipt,apollo_status FROM apollo_linkedin_reservations LIMIT 1').fetchone()
                con.execute('SELECT lease_until FROM apollo_linkedin_account_locks LIMIT 1').fetchone()
        except sqlite3.Error as exc:
            raise RepositoryConflict('official Apollo LinkedIn store unavailable') from exc
        return True

    def reserve_apollo_linkedin(self, company, task_id, action_type, account_id, *, text_hash=None,
                                campaign_version=None, recipient=None):
        for value in (company, task_id, action_type, account_id):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError('invalid Apollo LinkedIn reservation identity')
        if action_type not in {'linkedin_step_connect', 'linkedin_step_message'}:
            raise ValueError('unsupported Apollo LinkedIn action')
        supplied = (text_hash, campaign_version, recipient)
        if any(value is not None for value in supplied):
            if (not isinstance(text_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', text_hash)
                    or isinstance(campaign_version, bool)
                    or not isinstance(campaign_version, (str, int))
                    or (isinstance(campaign_version, int) and campaign_version < 1)
                    or not str(campaign_version).strip() or str(campaign_version) != str(campaign_version).strip()
                    or len(str(campaign_version)) > 128
                    or not isinstance(recipient, str) or not recipient.strip()
                    or recipient != recipient.strip() or len(recipient) > 256):
                raise ValueError('invalid Apollo LinkedIn reservation binding')
            campaign_version = str(campaign_version)
        self.apollo_linkedin_store_ready()
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            try:
                con.execute('INSERT INTO apollo_linkedin_reservations '
                            '(company,task_id,action_type,account_id,state,reason,created_at,updated_at,text_hash,campaign_version,recipient) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            (company, task_id, action_type, account_id, 'reserved', None, now, now,
                             text_hash, campaign_version, recipient))
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict('Apollo LinkedIn task already reserved') from exc
        return {'company': company, 'task_id': task_id, 'action_type': action_type,
                'account_id': account_id, 'state': 'reserved', 'reason': None,
                'created_at': now, 'updated_at': now, 'text_hash': text_hash,
                'campaign_version': campaign_version, 'recipient': recipient,
                'receipt': None, 'apollo_status': None}

    def reserve_apollo_linkedin_for_campaign(self, campaign_id, approval_id, campaign_version,
                                            task, *, observed_team_id, expected_text_hash, binding_id):
        """Atomically reserve an observed task against a persisted campaign approval.

        The caller must obtain ``task`` and ``observed_team_id`` from its
        authenticated Apollo reader. This method checks their bindings but does
        not itself authenticate the observation or authorize provider dispatch.
        The executor must re-read the task and all gates at the send boundary.
        """
        from .apollo_linkedin_authority import CampaignApprovalAuthority
        from .apollo_linkedin_identity_binding import KIND, binding_key
        from .apollo_linkedin_preview import _slug, validated_task_text

        self.apollo_linkedin_store_ready()
        if not isinstance(task, dict):
            raise ValueError('task_observation_invalid')
        task_id, action = task.get('id'), task.get('type')
        action_name = {'linkedin_step_connect': 'invite', 'linkedin_step_message': 'message'}.get(action)
        if not action_name or task.get('status') != 'scheduled':
            raise ValueError('task_observation_invalid')
        contact = task.get('contact')
        if (not isinstance(contact, dict) or not isinstance(contact.get('id'), str)
                or not contact['id'] or task.get('contact_id') != contact['id']):
            raise ValueError('contact_identity_invalid')
        recipient = _slug(contact.get('linkedin_url'))
        _, digest, blocker = validated_task_text(task, action_name)
        if blocker or not recipient or digest != expected_text_hash:
            raise ValueError('task_text_or_recipient_invalid')
        self._t4_canary_fields('placeholder', task_id, 'placeholder', recipient, digest)
        with self.transaction() as con:
            # Nested callers may catch an exception without rolling back their
            # outer transaction; a savepoint preserves reservation+audit atomicity.
            con.execute('SAVEPOINT campaign_reservation')
            try:
                approved = CampaignApprovalAuthority(self).require(campaign_id, approval_id, campaign_version)
                binding = approved['binding']
                if (binding.get('team_id') != observed_team_id
                        or task.get('emailer_campaign_id') != binding.get('sequence_id')
                        or task.get('user_id') not in binding.get('owner_ids', ())
                        or recipient == binding.get('public_id', '').lower()):
                    raise ValueError('task_campaign_binding_invalid')
                company, account = binding['company_id'], binding['account_id']
                self._t4_canary_fields(company, task_id, account, recipient, digest)
                identity = self.get_state(KIND, binding_id) if isinstance(binding_id, str) else None
                expected_id = binding_key(company, campaign_id, task_id, account, approval_id)
                if (binding_id != expected_id or not isinstance(identity, dict) or identity.get('version') != 1
                        or identity.get('id') != expected_id
                        or identity.get('company') != company or identity.get('campaign_id') != campaign_id
                        or identity.get('approval_id') != approval_id
                        or identity.get('campaign_version') != campaign_version
                        or identity.get('scope_hash') != approved['scope_hash']
                        or identity.get('team_id') != observed_team_id
                        or identity.get('sequence_id') != binding['sequence_id']
                        or identity.get('task_id') != task_id or identity.get('action_type') != action
                        or identity.get('contact_id') != contact['id']
                        or identity.get('account_id') != account or identity.get('recipient_public_id') != recipient
                        or not isinstance(identity.get('member_urn'), str)
                        or not re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}', identity['member_urn'])):
                    raise ValueError('identity_binding_unverified')
                result = self.reserve_apollo_linkedin(company, task_id, action, account,
                    text_hash=digest, campaign_version=campaign_version, recipient=recipient)
                self.append_event(task_id, 'apollo_linkedin_campaign_reservation', {
                    'company': company, 'task_id': task_id, 'action_type': action,
                    'account_id': account, 'campaign_id': campaign_id, 'approval_id': approval_id,
                    'campaign_version': str(campaign_version), 'approval_scope_hash': approved['scope_hash'],
                    'actor': 'ygor', 'text_hash': digest, 'apollo_contact_id': contact['id'],
                    'binding_id': binding_id, 'member_urn': identity['member_urn'],
                    'apollo_owner_id': task['user_id'], 'apollo_sequence_id': binding['sequence_id'],
                    'apollo_team_id': observed_team_id})
                con.execute('RELEASE SAVEPOINT campaign_reservation')
                return result
            except Exception:
                con.execute('ROLLBACK TO SAVEPOINT campaign_reservation')
                con.execute('RELEASE SAVEPOINT campaign_reservation')
                raise

    @staticmethod
    def _t4_canary_fields(company, task_id, account_id, recipient, text_hash):
        # A canary is bound to exact identifiers, never an inferred default.
        for value in (company, task_id, account_id, recipient):
            if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value)):
                raise ValueError('invalid canary identity')
        from .apollo_linkedin_attention import EXCLUDED_TASK
        if task_id == EXCLUDED_TASK:
            raise ValueError('protected fixture')
        if not isinstance(text_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', text_hash):
            raise ValueError('invalid canary text hash')

    @staticmethod
    def _t4_message_binding(member_urn, campaign_version, approval_id):
        if (not isinstance(member_urn, str)
                or not re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}', member_urn)
                or isinstance(campaign_version, bool) or not isinstance(campaign_version, (str, int))
                or (isinstance(campaign_version, int) and campaign_version < 1)
                or not str(campaign_version).strip() or str(campaign_version) != str(campaign_version).strip()
                or len(str(campaign_version)) > 128
                or not isinstance(approval_id, str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', approval_id)):
            raise ValueError('invalid canary campaign or recipient binding')
        return str(campaign_version)

    def issue_apollo_linkedin_canary(self, company, task_id, account_id, recipient, text_hash, actor,
                                     *, action_type='linkedin_step_connect', member_urn=None,
                                     campaign_version=None, approval_id=None):
        """Persist a bounded one-shot canary; caller must authenticate actor/role."""
        self._t4_canary_fields(company, task_id, account_id, recipient, text_hash)
        if not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', actor):
            raise ValueError('invalid canary actor')
        if action_type not in ('linkedin_step_connect', 'linkedin_step_message'):
            raise ValueError('invalid canary action')
        extended = action_type == 'linkedin_step_message' or any(
            value is not None for value in (member_urn, campaign_version, approval_id))
        if extended:
            campaign_version = self._t4_message_binding(member_urn, campaign_version, approval_id)
        self.apollo_linkedin_store_ready()
        now = datetime.now(timezone.utc)
        key = uuid4().hex
        payload = {'company': company, 'task_id': task_id, 'account_id': account_id,
                   'recipient': recipient, 'text_hash': text_hash, 'actor': actor,
                   'issued_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=30)).isoformat(),
                   'state': 'issued'}
        if extended:
            payload.update(action_type=action_type, member_urn=member_urn,
                           campaign_version=campaign_version, approval_id=approval_id)
        with self.transaction() as con:
            try:
                con.execute('INSERT INTO thunder_state VALUES (?,?,?,?,?)',
                            ('apollo_linkedin_canary', key, 1,
                             json.dumps(payload, ensure_ascii=False, sort_keys=True), now.isoformat()))
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict('canary identity conflict') from exc
            self.append_event(key, 'apollo_linkedin_canary_issued',
                              {'company': company, 'task_id': task_id, 'actor': actor})
        return {**payload, 'id': key, 'version': 1, 'updated_at': now.isoformat()}

    def reserve_apollo_linkedin_with_canary(self, canary_id, company, task_id, action_type,
                                            account_id, recipient, text_hash, campaign_version,
                                            *, member_urn=None, approval_id=None):
        """Consume canary and reserve atomically; any failure rolls back both."""
        self._t4_canary_fields(company, task_id, account_id, recipient, text_hash)
        if (not isinstance(canary_id, str) or not re.fullmatch(r'[a-f0-9]{32}', canary_id)
                or action_type not in ('linkedin_step_connect', 'linkedin_step_message')
                or isinstance(campaign_version, bool) or not isinstance(campaign_version, (str, int))
                or not str(campaign_version).strip() or str(campaign_version) != str(campaign_version).strip()
                or len(str(campaign_version)) > 128
                or (isinstance(campaign_version, int) and campaign_version < 1)):
            raise ValueError('invalid canary reservation')
        extended = action_type == 'linkedin_step_message' or member_urn is not None or approval_id is not None
        if extended:
            self._t4_message_binding(member_urn, campaign_version, approval_id)
        self.apollo_linkedin_store_ready()
        now = datetime.now(timezone.utc)
        with self.transaction() as con:
            row = con.execute('SELECT version,payload FROM thunder_state WHERE kind=? AND entity_id=?',
                              ('apollo_linkedin_canary', canary_id)).fetchone()
            try:
                if row is None:
                    raise ValueError('missing')
                auth = json.loads(row['payload'])
                legacy_fields = {'company', 'task_id', 'account_id', 'recipient',
                                 'text_hash', 'actor', 'issued_at', 'expires_at', 'state'}
                extended_fields = legacy_fields | {'action_type', 'member_urn', 'campaign_version', 'approval_id'}
                if (not isinstance(auth, dict) or
                        (set(auth) != (extended_fields if extended else legacy_fields))):
                    raise ValueError('invalid')
                if extended and (auth['action_type'], auth['member_urn'], auth['campaign_version'], auth['approval_id']) != (
                        action_type, member_urn, str(campaign_version), approval_id):
                    raise ValueError('binding mismatch')
                issued = datetime.fromisoformat(auth['issued_at'])
                expires = datetime.fromisoformat(auth['expires_at'])
                if (type(row['version']) is not int or row['version'] < 1
                        or not isinstance(auth['actor'], str)
                        or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', auth['actor'])
                        or issued.tzinfo is None or expires.tzinfo is None or issued.utcoffset() is None
                        or expires.utcoffset() is None or issued > now or expires <= now
                        or not timedelta(0) < expires - issued <= timedelta(minutes=30)
                        or auth['state'] != 'issued'
                        or (auth['company'], auth['task_id'], auth['account_id'], auth['recipient'], auth['text_hash'])
                        != (company, task_id, account_id, recipient, text_hash)):
                    raise ValueError('invalid')
            except (KeyError, ValueError, TypeError, OverflowError):
                raise RepositoryConflict('canary unavailable or mismatched') from None
            previous = con.execute('SELECT * FROM apollo_linkedin_reservations '
                                   'WHERE company=? AND task_id=? AND action_type=?',
                                   (company, task_id, action_type)).fetchone()
            if previous is None:
                try:
                    con.execute('INSERT INTO apollo_linkedin_reservations '
                                '(company,task_id,action_type,account_id,state,reason,created_at,updated_at,text_hash,campaign_version,recipient) '
                                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                                (company, task_id, action_type, account_id, 'reserved', None, now.isoformat(),
                                 now.isoformat(), text_hash, str(campaign_version), recipient))
                except sqlite3.IntegrityError as exc:
                    raise RepositoryConflict('Apollo LinkedIn task already reserved') from exc
            else:
                # An operator's decision is audit evidence, not proof that an
                # ambiguous provider effect did not happen. Neither a new
                # canary nor a release may dispatch the same action again.
                if action_type == 'linkedin_step_message':
                    raise RepositoryConflict('uncertain message cannot be retried')
                raise RepositoryConflict('Apollo LinkedIn task already reserved')
            self._put_state_in(con, 'apollo_linkedin_canary', canary_id,
                               {**auth, 'state': 'consumed'}, now.isoformat(), row['version'])
        return {'company': company, 'task_id': task_id, 'action_type': action_type,
                'account_id': account_id, 'state': 'reserved', 'text_hash': text_hash,
                'campaign_version': str(campaign_version), 'recipient': recipient}

    def resolve_apollo_linkedin_uncertain(self, company, task_id, action_type, decision, justification, actor):
        """Record a human decision atomically; never perform or authorize an effect.

        The caller is responsible for trusted-proxy authentication and RBAC.
        A release decision records the review but never authorizes another send;
        the original uncertain reservation remains authoritative.
        """
        if (decision not in ('release', 'close') or not isinstance(justification, str)
                or not justification.strip() or len(justification) > 2000
                or not isinstance(actor, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', actor)
                or not all(isinstance(v, str) and v.strip() == v and v for v in (company, task_id, action_type))):
            raise ValueError('invalid uncertainty resolution')
        from .apollo_linkedin_attention import EXCLUDED_TASK
        if task_id == EXCLUDED_TASK:
            raise ValueError('protected fixture')
        self.apollo_linkedin_store_ready()
        with self.transaction() as con:
            row = con.execute('SELECT state,updated_at FROM apollo_linkedin_reservations '
                              'WHERE company=? AND task_id=? AND action_type=?',
                              (company, task_id, action_type)).fetchone()
            if row is None or row['state'] != 'uncertain':
                raise RepositoryConflict('reservation not uncertain')
            key = payload_hash({'company': company, 'task_id': task_id, 'action_type': action_type})
            previous = self.get_state('apollo_linkedin_uncertain_resolution', key)
            if previous is not None and previous.get('reservation_updated_at') == row['updated_at']:
                raise RepositoryConflict('uncertainty already resolved')
            record = {'company': company, 'task_id': task_id, 'action_type': action_type,
                      'decision': decision, 'justification': justification.strip(), 'actor': actor,
                      'reservation_updated_at': row['updated_at'], 'consumed': False}
            if previous is None:
                self.append_state('apollo_linkedin_uncertain_resolution', key, record)
            else:
                self._put_state_in(con, 'apollo_linkedin_uncertain_resolution', key, record,
                                   datetime.now(timezone.utc).isoformat(), previous['version'])
            self.append_event(key, 'apollo_linkedin_uncertain_resolved', record)
        return {'state': 'uncertain', 'decision': decision, 'execution_enabled': False}

    def finish_apollo_linkedin(self, company, task_id, action_type, state, reason=None, *,
                               receipt=None, apollo_status=None):
        if state not in {'confirmed', 'rejected', 'uncertain'}:
            raise ValueError('invalid Apollo LinkedIn terminal state')
        self.apollo_linkedin_store_ready()
        with self.transaction() as con:
            row = con.execute('SELECT text_hash,campaign_version,recipient FROM apollo_linkedin_reservations '
                              'WHERE company=? AND task_id=? AND action_type=? AND state=\'reserved\'',
                              (company, task_id, action_type)).fetchone()
            if row is None:
                raise RepositoryConflict('Apollo LinkedIn reservation is not reserved')
            t4 = any(row[key] is not None for key in ('text_hash', 'campaign_version', 'recipient'))
            if t4 and not all(row[key] is not None for key in ('text_hash', 'campaign_version', 'recipient')):
                raise RepositoryConflict('Apollo LinkedIn reservation binding incomplete')
            if receipt is not None:
                if not isinstance(receipt, dict) or not receipt:
                    raise ValueError('invalid Apollo LinkedIn receipt')
                if t4:
                    if set(receipt) - {'provider_id', 'publicIdentifier'}:
                        raise ValueError('invalid Apollo LinkedIn receipt')
                    provider_id = receipt.get('provider_id')
                    public_id = receipt.get('publicIdentifier')
                    if provider_id is not None and (not isinstance(provider_id, str) or not provider_id.strip()
                                                    or len(provider_id) > 256):
                        raise ValueError('invalid Apollo LinkedIn receipt')
                    if public_id is not None and public_id != row['recipient']:
                        raise ValueError('invalid Apollo LinkedIn receipt')
                    if not ((isinstance(provider_id, str) and bool(provider_id.strip()))
                            or (isinstance(public_id, str) and public_id == row['recipient'])):
                        raise ValueError('invalid Apollo LinkedIn receipt')
                try:
                    receipt = json.dumps(receipt, allow_nan=False, ensure_ascii=False, separators=(',', ':'))
                except (TypeError, ValueError):
                    raise ValueError('invalid Apollo LinkedIn receipt') from None
                if len(receipt.encode('utf-8')) > 8192:
                    raise ValueError('invalid Apollo LinkedIn receipt')
            if t4 and state == 'confirmed' and (receipt is None or apollo_status != 'pending'):
                raise ValueError('confirmed Apollo LinkedIn action requires receipt and pending reconciliation')
            if state != 'confirmed' and (receipt is not None or apollo_status is not None):
                raise ValueError('receipt requires confirmed Apollo LinkedIn action')
            if apollo_status not in (None, 'pending'):
                raise ValueError('invalid Apollo reconciliation status')
            changed = con.execute(
                "UPDATE apollo_linkedin_reservations SET state=?,reason=?,updated_at=?,receipt=?,apollo_status=? "
                "WHERE company=? AND task_id=? AND action_type=? AND state='reserved'",
                (state, reason, datetime.now(timezone.utc).isoformat(), receipt, apollo_status,
                 company, task_id, action_type)).rowcount
            if changed != 1:
                raise RepositoryConflict('Apollo LinkedIn reservation is not reserved')

    @staticmethod
    def _apollo_linkedin_reservation(row):
        result = dict(row)
        if result.get('receipt') is not None:
            try:
                result['receipt'] = json.loads(result['receipt'])
                if not isinstance(result['receipt'], dict):
                    raise ValueError('invalid receipt')
            except (TypeError, ValueError):
                raise RepositoryConflict('Apollo LinkedIn receipt invalid') from None
        return result

    def get_apollo_linkedin_reservation(self, company, task_id, action_type):
        self.apollo_linkedin_store_ready()
        with self._connect() as con:
            row = con.execute('SELECT * FROM apollo_linkedin_reservations WHERE company=? AND task_id=? AND action_type=?',
                              (company, task_id, action_type)).fetchone()
            return None if row is None else self._apollo_linkedin_reservation(row)

    def claim_apollo_linkedin_completion(self, company, task_id, action_type):
        """Durably claim the one Apollo POST before it can cross the network."""
        self.apollo_linkedin_store_ready()
        with self.transaction() as con:
            changed = con.execute("UPDATE apollo_linkedin_reservations SET apollo_status='attempted',updated_at=? "
                                  "WHERE company=? AND task_id=? AND action_type=? AND state='confirmed' "
                                  "AND apollo_status='pending' AND receipt IS NOT NULL",
                                  (datetime.now(timezone.utc).isoformat(), company, task_id, action_type)).rowcount
            if changed != 1:
                raise RepositoryConflict('Apollo LinkedIn completion already attempted or not pending')

    def reconcile_apollo_linkedin(self, company, task_id, action_type):
        self.apollo_linkedin_store_ready()
        with self.transaction() as con:
            changed = con.execute("UPDATE apollo_linkedin_reservations SET apollo_status='confirmed',updated_at=? "
                                  "WHERE company=? AND task_id=? AND action_type=? AND state='confirmed' "
                                  "AND apollo_status='attempted' AND receipt IS NOT NULL",
                                  (datetime.now(timezone.utc).isoformat(), company, task_id, action_type)).rowcount
            if changed != 1:
                raise RepositoryConflict('Apollo LinkedIn reconciliation not pending')

    def list_apollo_linkedin_reservations(self):
        self.apollo_linkedin_store_ready()
        with self._connect() as con:
            return [self._apollo_linkedin_reservation(row) for row in con.execute('SELECT * FROM apollo_linkedin_reservations ORDER BY created_at')]

    def list_apollo_linkedin_account_locks(self):
        self.apollo_linkedin_store_ready()
        with self._connect() as con:
            return [dict(row) for row in con.execute(
                'SELECT account_id, created_at, lease_until FROM apollo_linkedin_account_locks ORDER BY created_at')]

    @contextmanager
    def apollo_linkedin_account_lock(self, account_id):
        if not isinstance(account_id, str) or not account_id.strip() or account_id != account_id.strip():
            raise ValueError('invalid LinkedIn account')
        self.apollo_linkedin_store_ready()
        # An expiring DB row alone can overlap a slow sender. A per-account OS
        # lock stays held until process exit, fencing lease takeover on this host.
        lock_dir = Path(self.path).resolve().parent / '.apollo-linkedin-locks'
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        lock_name = hashlib.sha256(account_id.encode('utf-8')).hexdigest() + '.lock'
        fd = os.open(lock_dir / lock_name, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RepositoryConflict('LinkedIn account locked') from exc
            owner = uuid4().hex
            now = datetime.now(timezone.utc)
            with self.transaction() as con:
                existing = con.execute('SELECT lease_until FROM apollo_linkedin_account_locks WHERE account_id=?',
                                       (account_id,)).fetchone()
                if existing:
                    # Legacy rows have no expiry: fail closed instead of guessing.
                    try:
                        expiry = datetime.fromisoformat(existing['lease_until'])
                        if expiry.tzinfo is None or expiry.utcoffset() is None:
                            raise ValueError('naive lease expiry')
                    except (TypeError, ValueError):
                        raise RepositoryConflict('LinkedIn account lock expiry invalid') from None
                    if expiry > now:
                        raise RepositoryConflict('LinkedIn account locked')
                    con.execute('DELETE FROM apollo_linkedin_account_locks WHERE account_id=?', (account_id,))
                con.execute('INSERT INTO apollo_linkedin_account_locks '
                            '(account_id,owner,created_at,lease_until) VALUES (?,?,?,?)',
                            (account_id, owner, now.isoformat(), (now + APOLLO_LINKEDIN_LOCK_TTL).isoformat()))
            try:
                yield
            finally:
                primary_error = sys.exc_info()[0] is not None
                try:
                    with self.transaction() as con:
                        changed = con.execute('DELETE FROM apollo_linkedin_account_locks WHERE account_id=? AND owner=?',
                                              (account_id, owner)).rowcount
                        if changed != 1:
                            raise RepositoryConflict('LinkedIn account lock lost')
                except Exception:
                    if not primary_error:
                        raise
                    logging.getLogger(__name__).exception('Apollo LinkedIn lock release failed')
        finally:
            os.close(fd)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._local.connection = con
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            self._local.connection = None
            con.close()

    def save_with_event(
        self,
        item: WorkItem,
        expected_version: int,
        event_type: str,
        outbox_kind: str | None = None,
    ) -> WorkItem:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(as_payload(item), ensure_ascii=False, sort_keys=True)
        event_payload = json.dumps({
            "id": item.id, "status": item.status.value, "version": item.version,
            "actor": item.actor, "next_action": item.next_action,
            "result_present": item.result is not None,
            "result_hash": payload_hash(item.result) if item.result is not None else None,
        }, ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            row = con.execute("SELECT version FROM thunder_work_items WHERE id=?", (item.id,)).fetchone()
            current = row["version"] if row else 0
            if current != expected_version or item.version != expected_version + 1:
                raise RepositoryConflict("stale writer")
            if row:
                changed = con.execute(
                    "UPDATE thunder_work_items SET version=?, payload=?, updated_at=? WHERE id=? AND version=?",
                    (item.version, payload, now, item.id, expected_version),
                ).rowcount
                if changed != 1:
                    raise RepositoryConflict("concurrent writer")
            else:
                try:
                    con.execute(
                        "INSERT INTO thunder_work_items VALUES (?, ?, ?, ?, ?)",
                        (item.id, item.idempotency_key, item.version, payload, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise RepositoryConflict("duplicate work or idempotency key") from exc
            event_id = uuid4().hex
            con.execute(
                "INSERT INTO thunder_events VALUES (?, ?, ?, ?, ?)",
                (event_id, item.id, event_type, event_payload, now),
            )
            if outbox_kind:
                con.execute(
                    "INSERT INTO thunder_outbox(id, entity_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (uuid4().hex, item.id, outbox_kind, payload, now),
                )
        return item

    def get_work(self, item_id: str) -> WorkItem | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM thunder_work_items WHERE id=?", (item_id,)).fetchone()
        return parse_work_item(json.loads(row["payload"])) if row else None

    def list_work(self, limit: int = 100) -> list[WorkItem]:
        """Read newest requests without migrating or mutating workflow state."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("work limit must be between 1 and 1000")
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM thunder_work_items ORDER BY updated_at DESC,rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [parse_work_item(json.loads(row["payload"])) for row in rows]

    def get_by_idempotency_key(self, key: str) -> WorkItem | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM thunder_work_items WHERE idempotency_key=?", (key,)).fetchone()
        return parse_work_item(json.loads(row["payload"])) if row else None

    def events(self, entity_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT id, type, payload, created_at FROM thunder_events WHERE entity_id=? ORDER BY rowid",
                (entity_id,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def outbox(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM thunder_outbox ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]

    def claim_outbox(self, worker_id: str, now: datetime, lease_for: timedelta, *, kind: str | None = None) -> dict[str, Any] | None:
        now = now.astimezone(timezone.utc)
        with self.transaction() as con:
            row = con.execute(
                """SELECT * FROM thunder_outbox
                   WHERE state='pending' AND (lease_until IS NULL OR lease_until <= ?) AND (? IS NULL OR kind=?)
                   ORDER BY rowid LIMIT 1""",
                (now.isoformat(), kind, kind),
            ).fetchone()
            if not row:
                return None
            lease_until = (now + lease_for).isoformat()
            claim_owner = f"{worker_id}:{uuid4().hex}"
            changed = con.execute(
                """UPDATE thunder_outbox SET lease_owner=?, lease_until=?
                   WHERE id=? AND (lease_until IS NULL OR lease_until <= ?)""",
                (claim_owner, lease_until, row["id"], now.isoformat()),
            ).rowcount
            if changed != 1:
                return None
            result = dict(row)
            result.update(lease_owner=claim_owner, lease_until=lease_until)
            return result

    def put_state(self, kind: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            row = con.execute(
                "SELECT version FROM thunder_state WHERE kind=? AND entity_id=?", (kind, entity_id)
            ).fetchone()
            version = (row["version"] if row else 0) + 1
            con.execute(
                """INSERT INTO thunder_state(kind,entity_id,version,payload,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(kind,entity_id) DO UPDATE SET
                   version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at""",
                (kind, entity_id, version, encoded, now),
            )
        return {**payload, "id": entity_id, "version": version, "updated_at": now}

    @staticmethod
    def _put_state_in(con, kind, entity_id, payload, now, expected_version=None):
        row = con.execute("SELECT version FROM thunder_state WHERE kind=? AND entity_id=?", (kind, entity_id)).fetchone()
        version = row["version"] if row else 0
        if expected_version is not None and version != expected_version:
            raise RepositoryConflict("stale state version")
        version += 1
        con.execute("""INSERT INTO thunder_state VALUES (?,?,?,?,?) ON CONFLICT(kind,entity_id)
                    DO UPDATE SET version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at""",
                    (kind, entity_id, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), now))
        return {**payload, "id": entity_id, "version": version, "updated_at": now}

    def put_states_atomic(self, updates):
        """Compare versions and save all states, or roll back the entire transition."""
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            return [self._put_state_in(con, kind, key, payload, now, expected)
                    for kind, key, payload, expected in updates]

    def append_state(self, kind: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an immutable state record; IDs are caller-stable and never overwritten."""
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            try:
                con.execute("INSERT INTO thunder_state VALUES (?,?,?,?,?)",
                            (kind, entity_id, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), now))
            except sqlite3.IntegrityError as exc:
                raise RepositoryConflict("immutable state already exists") from exc
        return {**payload, "id": entity_id, "version": 1, "updated_at": now}

    def get_state(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT version,payload,updated_at FROM thunder_state WHERE kind=? AND entity_id=?",
                (kind, entity_id),
            ).fetchone()
        if not row:
            return None
        return {**json.loads(row["payload"]), "id": entity_id, "version": row["version"], "updated_at": row["updated_at"]}

    def list_state(self, kind: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT entity_id,version,payload,updated_at FROM thunder_state WHERE kind=? ORDER BY rowid",
                (kind,),
            ).fetchall()
        return [
            {**json.loads(row["payload"]), "id": row["entity_id"], "version": row["version"], "updated_at": row["updated_at"]}
            for row in rows
        ]

    def reserve_delegated_action(self, actor: str, action: str, day: str) -> int:
        """Atomically enforce the persisted kill switch, scope and daily ceiling."""
        key = f"{actor}:{day}"
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            row = con.execute(
                "SELECT payload FROM thunder_state WHERE kind='delegation' AND entity_id=?", (actor,)
            ).fetchone()
            delegation = json.loads(row["payload"]) if row else None
            if (not delegation or not delegation.get("enabled")
                    or action not in delegation.get("scopes", [])):
                raise RepositoryConflict("delegation is disabled or out of scope")
            usage_row = con.execute(
                "SELECT version,payload FROM thunder_state WHERE kind='delegation_usage' AND entity_id=?", (key,)
            ).fetchone()
            usage = json.loads(usage_row["payload"]) if usage_row else {"count": 0}
            count = int(usage.get("count", 0))
            if count >= int(delegation["daily_limit"]):
                raise RepositoryConflict("daily delegation limit reached")
            payload = json.dumps({"count": count + 1, "daily_limit_date": day}, sort_keys=True)
            version = (usage_row["version"] if usage_row else 0) + 1
            con.execute(
                """INSERT INTO thunder_state(kind,entity_id,version,payload,updated_at)
                   VALUES ('delegation_usage',?,?,?,?) ON CONFLICT(kind,entity_id) DO UPDATE SET
                   version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at""",
                (key, version, payload, now),
            )
        return count + 1

    def enqueue_effect(self, effect_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        job_id = f"effect-{uuid4().hex}"
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            existing = con.execute(
                "SELECT * FROM thunder_effect_jobs WHERE effect_key=?", (effect_key,)
            ).fetchone()
            if existing:
                if existing["payload"] != encoded:
                    raise RepositoryConflict("effect key payload mismatch")
                return dict(existing)
            con.execute(
                "INSERT INTO thunder_effect_jobs(id,effect_key,payload,updated_at) VALUES (?,?,?,?)",
                (job_id, effect_key, encoded, now),
            )
        return {"id": job_id, "effect_key": effect_key, "payload": payload, "state": "pending"}

    def claim_effect(self, worker_id: str, now: datetime, lease_for: timedelta) -> dict[str, Any] | None:
        now = now.astimezone(timezone.utc)
        with self.transaction() as con:
            row = con.execute(
                """SELECT * FROM thunder_effect_jobs WHERE state='pending'
                   AND (lease_until IS NULL OR lease_until<=?) ORDER BY rowid LIMIT 1""",
                (now.isoformat(),),
            ).fetchone()
            if not row:
                return None
            lease_until = (now + lease_for).isoformat()
            claim_owner = f"{worker_id}:{uuid4().hex}"
            changed = con.execute(
                """UPDATE thunder_effect_jobs SET lease_owner=?,lease_until=?,updated_at=?
                   WHERE id=? AND state='pending' AND (lease_until IS NULL OR lease_until<=?)""",
                (claim_owner, lease_until, now.isoformat(), row["id"], now.isoformat()),
            ).rowcount
            if changed != 1:
                return None
            result = dict(row)
            result.update(payload=json.loads(row["payload"]), lease_owner=claim_owner,
                          lease_until=lease_until, updated_at=now.isoformat())
            return result

    @property
    def dispatch_fence_active(self) -> bool:
        return getattr(self._local, "dispatch_fence", None) is not None

    @contextmanager
    def effect_dispatch_fence(self, job_id, lease_owner):
        """Linearize config/approval changes and one dispatch in this database.

        BEGIN IMMEDIATE prevents any other writer/claimant until receipt or
        uncertainty is durably recorded. A revocation waiting behind a dispatch
        takes effect afterwards; it cannot undo an already-started remote call.
        """
        if not isinstance(lease_owner,str) or not lease_owner.strip():
            raise RepositoryConflict("dispatch owner required")
        if self.dispatch_fence_active:
            raise RepositoryConflict("nested dispatch fence")
        with self.transaction() as con:
            row = con.execute(
                "SELECT state,lease_owner,lease_until FROM thunder_effect_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            now = datetime.now(timezone.utc).isoformat()
            if (not row or row["state"] != "pending" or row["lease_owner"] != lease_owner
                    or not row["lease_until"] or row["lease_until"] <= now):
                raise RepositoryConflict("dispatch lease lost")
            self._local.dispatch_fence = (job_id, lease_owner)
            try:
                yield
            finally:
                self._local.dispatch_fence = None

    def finish_effect(self, job_id: str, state: str, receipt_ref: str | None, error_code: str | None, *, lease_owner: str | None = None) -> None:
        if state not in {"sent", "failed", "uncertain", "needs_reconciliation"}:
            raise ValueError("invalid effect terminal state")
        fenced = getattr(self._local, "dispatch_fence", None) == (job_id, lease_owner)
        with self.transaction() as con:
            changed = con.execute(
                """UPDATE thunder_effect_jobs SET state=?,receipt_ref=?,error_code=?,lease_owner=NULL,
                   lease_until=NULL,updated_at=? WHERE id=? AND state='pending' AND (lease_owner=? OR (lease_owner IS NULL AND ? IS NULL)) AND (lease_owner IS NULL OR lease_until>? OR ?)""",
                (state, receipt_ref, error_code, datetime.now(timezone.utc).isoformat(), job_id, lease_owner, lease_owner, datetime.now(timezone.utc).isoformat(), fenced),
            ).rowcount
            if changed != 1:
                raise RepositoryConflict("effect is not pending")

    def effect(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM thunder_effect_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        if result["state"] == "sent":
            result["sent_at"] = result["updated_at"]
        return result

    def effects(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM thunder_effect_jobs ORDER BY rowid").fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]), **({"sent_at": row["updated_at"]} if row["state"] == "sent" else {})} for row in rows]

    def reconciliation_effects(self, *, after_rowid: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Page registered effects, including consumed jobs, without loading all history."""
        if type(after_rowid) is not int or after_rowid < 0:
            raise ValueError("invalid effect cursor")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("effect limit must be between 1 and 100")
        with self._connect() as con:
            rows = con.execute(
                "SELECT rowid AS reconciliation_cursor,* FROM thunder_effect_jobs "
                "WHERE rowid>? ORDER BY rowid LIMIT ?", (after_rowid, limit),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def mark_effect_reconciliation(self, snapshot: dict[str, Any], evidence: dict[str, Any]) -> bool:
        """CAS one observed effect and append its incident atomically, retaining receipts."""
        touches = evidence.get("touches", [])
        if (len({touch.get("touch_id") for touch in touches}) < 2
                or any(touch.get("status") != "sent" or not touch.get("touch_id") for touch in touches)):
            raise ValueError("two confirmed touches required")
        alert_id = "duplicate-effect:" + snapshot["id"]
        code = "duplicate_consumed_effect"
        with self.transaction() as con:
            current = self.effect(snapshot["id"])
            fields = ("id", "effect_key", "payload", "state", "updated_at", "lease_owner",
                      "lease_until", "receipt_ref", "error_code")
            if not current or any(current[field] != snapshot[field] for field in fields):
                raise RepositoryConflict("effect changed during reconciliation")
            alert = self.get_state("operator_alert", alert_id)
            if current["state"] == "needs_reconciliation" and current["error_code"] == code and alert:
                return False
            changed = con.execute(
                "UPDATE thunder_effect_jobs SET state='needs_reconciliation',error_code=?,"
                "lease_owner=NULL,lease_until=NULL,updated_at=? "
                "WHERE id=? AND effect_key=? AND state=? AND updated_at=? "
                "AND lease_owner IS ? AND lease_until IS ?",
                (code, datetime.now(timezone.utc).isoformat(), current["id"], current["effect_key"],
                 current["state"], current["updated_at"], current["lease_owner"], current["lease_until"]),
            ).rowcount
            if changed != 1:
                raise RepositoryConflict("effect changed during reconciliation")
            if not alert:
                self.append_state("operator_alert", alert_id, {
                    "active": True, "priority": "high", "owner": "ygor", "code": code,
                    "reason": code, "effect_id": current["id"], "effect_key": current["effect_key"],
                    "cadence_id": current["payload"].get("cadence_id"),
                    "step_id": current["payload"].get("step", {}).get("id"),
                    "title": "Reconciliar execucoes duplicadas",
                    "message": "Dois ou mais toques confirmados no mesmo escopo. Historico preservado.",
                    "next_action": "Ygor: conferir os recibos e reconciliar o incidente sem novo envio.",
                    "duplicate_evidence": evidence,
                })
        return True

    def append_event(self, entity_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Append internal evidence using the existing event journal."""
        with self.transaction() as con:
            con.execute('INSERT INTO thunder_events VALUES (?,?,?,?,?)',
                        (uuid4().hex, entity_id, event_type,
                         json.dumps(payload, ensure_ascii=False, sort_keys=True),
                         datetime.now(timezone.utc).isoformat()))

    def reconcile_proven_not_sent(self, snapshot, assisted_snapshot, record):
        """CAS a service-validated pre-tool failure; never make it retryable."""
        with self.transaction() as con:
            current = self.effect(snapshot['id'])
            row = self.get_state('codex_assisted', snapshot['id'])
            if (current != snapshot or row != assisted_snapshot or not current
                    or current['state'] != 'uncertain' or current.get('receipt_ref')
                    or current.get('lease_owner') or current.get('lease_until')
                    or current.get('error_code') != 'assisted_receipt_pending'
                    or not row or row.get('phase') != 'dispatch_issued'
                    or row.get('manual_reconciliation') or row.get('receipt')):
                raise RepositoryConflict('proven not sent snapshot changed')
            if (record.get('schema') != 'internal_operator_receipt_v1'
                    or record.get('proof', {}).get('job_id') != current['id']
                    or record.get('job_payload_hash') != payload_hash(current['payload'])
                    or record.get('dispatch_authorized') is not False
                    or record.get('retry_write') is not False):
                raise RepositoryConflict('invalid operator reconciliation record')
            changed = con.execute("UPDATE thunder_effect_jobs SET state='failed',error_code='proved_not_sent',updated_at=? "
                                  "WHERE id=? AND state='uncertain' AND updated_at=? AND receipt_ref IS NULL "
                                  "AND lease_owner IS NULL AND lease_until IS NULL",
                                  (datetime.now(timezone.utc).isoformat(), current['id'], current['updated_at'])).rowcount
            if changed != 1:
                raise RepositoryConflict('proven not sent snapshot changed')
            self.put_state('codex_assisted', current['id'],
                           {**row, 'phase': 'proved_not_sent', 'manual_reconciliation': record})
            self.append_event(current['id'], 'assisted_proved_not_sent',
                              {'record_hash': payload_hash(record), 'record': record,
                               'previous_effect': current})
            return self.effect(current['id'])

    def validate_operator_receipt(self, snapshot, receipt):
        """Dedicated manual readback; ordinary failed jobs remain ineligible."""
        if getattr(self._local, 'connection', None) is None:
            raise RepositoryConflict('reconciliation transaction required')
        current = self.effect(snapshot['id'])
        row = self.get_state('codex_assisted', snapshot['id']) or {}
        record = row.get('manual_reconciliation', {})
        if (current != snapshot or not current or current.get('lease_owner') or current.get('lease_until')
                or not ((current['state'] == 'failed' and current.get('error_code') == 'proved_not_sent'
                         and not current.get('receipt_ref') and row.get('phase') == 'proved_not_sent')
                        or (current['state'] == 'sent' and current.get('receipt_ref') == receipt
                            and row.get('phase') == 'confirmed' and row.get('receipt') == receipt))):
            raise RepositoryConflict('operator receipt snapshot changed')
        if (record.get('schema') != 'internal_operator_receipt_v1'
                or record.get('job_payload_hash') != payload_hash(current['payload'])
                or record.get('proof', {}).get('job_id') != current['id']
                or not any(event['type'] == 'assisted_proved_not_sent'
                           and event['payload'].get('record_hash') == payload_hash(record)
                           for event in self.events(current['id']))):
            raise RepositoryConflict('operator reconciliation proof missing')
        if not isinstance(receipt, str) or not re.fullmatch(r'gmail:[A-Za-z0-9_-]{1,200}', receipt):
            raise RepositoryConflict('invalid operator receipt')
        if self._local.connection.execute('SELECT id FROM thunder_effect_jobs WHERE receipt_ref IN (?,?) AND id<>?',
                (receipt, 'email:'+receipt, current['id'])).fetchone():
            raise RepositoryConflict('receipt already bound')
        return current

    def confirm_operator_receipt(self, snapshot, receipt):
        """Observe the operator's sent copy, retaining the terminal failure audit."""
        with self.transaction() as con:
            current = self.validate_operator_receipt(snapshot, receipt)
            if current['state'] == 'sent':
                return current
            changed = con.execute("UPDATE thunder_effect_jobs SET state='sent',receipt_ref=?,error_code=NULL,updated_at=? "
                                  "WHERE id=? AND state='failed' AND error_code='proved_not_sent' AND updated_at=? "
                                  "AND receipt_ref IS NULL AND lease_owner IS NULL AND lease_until IS NULL",
                                  (receipt, datetime.now(timezone.utc).isoformat(), current['id'], current['updated_at'])).rowcount
            if changed != 1:
                raise RepositoryConflict('operator receipt snapshot changed')
            self.append_event(current['id'], 'assisted_operator_receipt_confirmed',
                              {'receipt': receipt, 'attempt_attribution': 'operator_reported',
                               'manual_record_hash': payload_hash(self.get_state('codex_assisted', current['id'])['manual_reconciliation'])})
            return self.effect(current['id'])

    def validate_uncertain_receipt(self, snapshot, receipt):
        """Preflight under the caller's write transaction before a ledger commit."""
        if getattr(self._local,'connection',None) is None:
            raise RepositoryConflict('reconciliation transaction required')
        current = self.effect(snapshot['id'])
        if current != snapshot or current['state'] not in {'uncertain','needs_reconciliation','sent'}:
            raise RepositoryConflict('reconciliation snapshot changed')
        if (current.get('error_code') == 'duplicate_consumed_effect'
                or current.get('lease_owner') or current.get('receipt_ref') not in (None, receipt)):
            raise RepositoryConflict('reconciliation receipt conflict')
        if self._local.connection.execute('SELECT id FROM thunder_effect_jobs WHERE receipt_ref IN (?,?) AND id<>?',
                (receipt,'email:'+receipt,current['id'])).fetchone():
            raise RepositoryConflict('receipt already bound')
        return current

    def confirm_uncertain_receipt(self, snapshot, receipt):
        """CAS historical confirmation. Never queue work or renew approval."""
        with self.transaction() as con:
            current = self.validate_uncertain_receipt(snapshot,receipt)
            con.execute("UPDATE thunder_effect_jobs SET state='sent',receipt_ref=?,error_code=NULL,updated_at=? WHERE id=?",
                        (receipt,datetime.now(timezone.utc).isoformat(),current['id']))
            return self.effect(current['id'])

    def requeue_confirmed_effect(self, snapshot: dict[str, Any], confirmation_hash: str) -> dict[str, Any]:
        """CAS a safely failed effect after the caller persists fresh campaign authority."""
        if not isinstance(confirmation_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", confirmation_hash):
            raise ValueError("invalid execution confirmation hash")
        with self.transaction() as con:
            current = self.effect(snapshot["id"])
            fields = ("id", "effect_key", "payload", "state", "updated_at", "lease_owner",
                      "lease_until", "receipt_ref", "error_code")
            if not current or any(current[field] != snapshot[field] for field in fields):
                raise RepositoryConflict("effect changed during confirmed retry")
            if current["state"] != "failed" or current["error_code"] not in {
                "provider_rejected", "email_rejected", "hubspot_rejected", "channel_not_configured"
            }:
                raise RepositoryConflict("effect is not safely retryable")
            if confirmation_hash == current["payload"].get("execution_confirmation_hash"):
                raise RepositoryConflict("fresh campaign confirmation required")
            payload = {**current["payload"], "execution_confirmation_hash": confirmation_hash}
            changed = con.execute(
                "UPDATE thunder_effect_jobs SET payload=?,state='pending',lease_owner=NULL,"
                "lease_until=NULL,error_code=NULL,receipt_ref=NULL,updated_at=? "
                "WHERE id=? AND effect_key=? AND state='failed' AND updated_at=? "
                "AND error_code=? AND lease_owner IS ? AND lease_until IS ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 datetime.now(timezone.utc).isoformat(), current["id"], current["effect_key"],
                 current["updated_at"], current["error_code"], current["lease_owner"], current["lease_until"]),
            ).rowcount
            if changed != 1:
                raise RepositoryConflict("effect changed during confirmed retry")
            return self.effect(current["id"])

    def retry_effect(self, job_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as con:
            row = con.execute("SELECT * FROM thunder_effect_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] != "failed" or row["error_code"] not in {
                "provider_rejected", "email_rejected", "hubspot_rejected", "channel_not_configured"
            }:
                raise RepositoryConflict("effect is not safely retryable")
            con.execute(
                "UPDATE thunder_effect_jobs SET state='pending',lease_owner=NULL,lease_until=NULL,error_code=NULL,updated_at=? WHERE id=?",
                (now, job_id),
            )
        return self.effect(job_id)  # type: ignore[return-value]

    def renew_lease(self, table, job_id, lease_owner, lease_for=timedelta(seconds=30)):
        if table not in {"thunder_effect_jobs", "thunder_outbox"}:
            raise ValueError("invalid queue")
        now = datetime.now(timezone.utc)
        with self.transaction() as con:
            changed = con.execute(f"UPDATE {table} SET lease_until=? WHERE id=? AND state='pending' AND lease_owner=? AND lease_until>?",
                ((now + lease_for).isoformat(), job_id, lease_owner, now.isoformat())).rowcount
            if changed != 1:
                raise RepositoryConflict("lease lost")

    def defer_effect(self, job_id, lease_owner, reason, delay=timedelta(seconds=5)):
        with self.transaction() as con:
            changed = con.execute("UPDATE thunder_effect_jobs SET lease_owner=NULL,lease_until=?,error_code=? WHERE id=? AND state='pending' AND lease_owner=?",
                ((datetime.now(timezone.utc) + delay).isoformat(), reason, job_id, lease_owner)).rowcount
            if changed != 1:
                raise RepositoryConflict("lease lost")

    def finish_outbox(self, job_id, lease_owner, *, state="done"):
        if state not in {"done", "failed"}:
            raise ValueError("invalid outbox state")
        with self.transaction() as con:
            changed = con.execute("UPDATE thunder_outbox SET state=?,lease_owner=NULL,lease_until=NULL WHERE id=? AND state='pending' AND lease_owner=? AND lease_until>?",
                                  (state, job_id, lease_owner, datetime.now(timezone.utc).isoformat())).rowcount
            if changed != 1:
                raise RepositoryConflict("outbox lease lost")

    @contextmanager
    def outbox_completion(self, job_id, lease_owner):
        """All repository writes inside this scope and the acknowledgement commit together."""
        with self.transaction() as con:
            row = con.execute("SELECT id FROM thunder_outbox WHERE id=? AND state='pending' AND lease_owner=? AND lease_until>?",
                              (job_id, lease_owner, datetime.now(timezone.utc).isoformat())).fetchone()
            if not row:
                raise RepositoryConflict("outbox lease lost")
            yield self
            self.finish_outbox(job_id, lease_owner)

    def approve_cadence_atomic(self, cadence_id, expected_version, review_hash, approval_payload, cadence_payload, *, review_hasher, research_version=None):
        with self.transaction() as con:
            current = self.get_state("cadence", cadence_id)
            if not current or current["version"] != expected_version or review_hasher(current) != review_hash:
                raise RepositoryConflict("stale cadence review")
            if research_version is not None:
                research = self.get_state("research", current["account_id"])
                if not research or research["version"] != research_version:
                    raise RepositoryConflict("stale research review")
            now = datetime.now(timezone.utc).isoformat()
            approval = self._put_state_in(con, "approval", cadence_id, approval_payload, now)
            self._put_state_in(con, "cadence", cadence_id, cadence_payload, now, expected_version)
            return approval


class _BorrowedConnection:
    """Prevent nested read contexts from committing their caller's transaction."""
    def __init__(self, connection):
        self.connection = connection
    def __getattr__(self, name):
        return getattr(self.connection, name)
    def __enter__(self):
        return self.connection
    def __exit__(self, *_args):
        return False
    def close(self):
        pass


class _OwnedConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()
