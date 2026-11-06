#!/usr/bin/env python3
"""Display a conservative canary checklist without credentials, IO, or writes.

A task ID and recipient alone cannot establish the approved campaign, account,
quota, text, response coverage, or existing invitations. The command therefore
reports those items as unverified rather than manufacturing readiness.
"""
import argparse
import json
from pathlib import Path
import re
import os
import stat
import sqlite3
from contextlib import contextmanager
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_ferramentas'))
from thunder_os.apollo_linkedin_live_executor import execution_status, valid_id
from thunder_os.apollo_linkedin_authority import CampaignApprovalAuthority
from thunder_os.approvals import ApprovalError
from thunder_os.apollo_linkedin_preview import validated_task_text, _slug
from thunder_os.apollo_linkedin_reader import ApolloLinkedInReader

_SLUG = re.compile(r'[A-Za-z0-9_.-]{1,160}\Z')


def credential(path):
    """Lazy read of an explicitly configured private, regular token file."""
    def load():
        try:
            candidate = Path(path)
            if not candidate.is_absolute() or candidate.is_symlink():
                raise ValueError('credential_unavailable')
            fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                        or info.st_mode & 0o077 or not 0 < info.st_size <= 4096):
                    raise ValueError('credential_unavailable')
                token = stream.read(4097).decode('utf-8').strip()
            if not token or any(c.isspace() for c in token):
                raise ValueError('credential_unavailable')
            return token
        except (OSError, UnicodeError, ValueError):
            raise ValueError('credential_unavailable') from None
    return load


class ReadOnlyStates:
    """Read an existing SQLite state store without running migrations or WAL setup."""
    def __init__(self, path):
        db = Path(path)
        if not db.is_absolute() or not db.is_file():
            raise ValueError('database_unavailable')
        self.connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA query_only=ON')
        self.depth = 0

    @contextmanager
    def transaction(self):
        outer = self.depth == 0
        if outer:
            self.connection.execute('BEGIN')
        self.depth += 1
        try:
            yield self.connection
        finally:
            self.depth -= 1
            if outer:
                self.connection.rollback()

    def get_state(self, kind, entity_id):
        row = self.connection.execute(
            'SELECT version,payload,updated_at FROM thunder_state WHERE kind=? AND entity_id=?',
            (kind, entity_id)).fetchone()
        if row is None:
            return None
        payload = json.loads(row['payload'])
        if not isinstance(payload, dict):
            raise ValueError('state_invalid')
        return {**payload, 'id': entity_id, 'version': row['version'], 'updated_at': row['updated_at']}

    def close(self):
        self.connection.close()


def inspect_observations(result, *, db=None, campaign=None, cadence=None, version=None, reader=None, apollo_actor=None, apollo_credential_file=None):
    """Read configured authorities and the allowlisted Apollo task reader only.

    Supplying a reader is an integration seam: observations never authorize
    dispatch, and callers must construct ApolloLinkedInReader with the verified
    team and API actor. Missing sources remain unknown.
    """
    if result['execution']['reason'] == 'fixture_protegida':
        return result
    binding = None
    if db and campaign and cadence and version:
        store = None
        try:
            store = ReadOnlyStates(db)
            authority = CampaignApprovalAuthority(store).require(campaign, cadence, version)
            binding = authority['binding']
            result['checks']['campaign_approval'] = 'verified'
            result['account'] = {'id': binding['account_id'], 'public_id': binding['public_id'],
                                 'status': 'unverified'}
        except (ApprovalError, ValueError, OSError, sqlite3.Error):
            result['checks']['campaign_approval'] = 'unverified'
        finally:
            if store:
                store.close()
    if reader is None and binding is not None and apollo_actor and apollo_credential_file:
        try:
            reader = ApolloLinkedInReader(credential(apollo_credential_file), team_id=binding['team_id'],
                api_actor_id=apollo_actor, owner_ids=binding['owner_ids'])
        except (ValueError, TypeError, OSError):
            reader = None
    if reader is not None and binding is not None:
        try:
            if reader.team_id != binding['team_id'] or reader.owner_ids != frozenset(binding['owner_ids']):
                raise ValueError('reader_binding_mismatch')
            task = reader.get(result['task'])
            action = {'linkedin_step_connect': 'invite', 'linkedin_step_message': 'message'}.get(task.get('type'))
            if action is None:
                raise ValueError('unsupported_action')
            text, digest, error = validated_task_text(task, action)
            contact = task.get('contact')
            if (error
                    or task.get('status') != 'scheduled'
                    or task.get('emailer_campaign_id') != binding['sequence_id']
                    or task.get('user_id') not in binding['owner_ids']
                    or not isinstance(contact, dict)
                    or _slug(contact.get('linkedin_url')) != result['recipient'].lower()):
                raise ValueError('task_binding_or_text_invalid')
            result['checks']['text'] = 'observed_not_approved'
            result['text'] = text
            result['text_hash'] = digest
        except (ValueError, TypeError, KeyError, OSError):
            result['checks']['text'] = 'unverified'
    return result


def prepare(task, recipient, authorization):
    if (not valid_id(task) or not valid_id(authorization) or
            not isinstance(recipient, str) or not _SLUG.fullmatch(recipient)):
        raise ValueError('configuration_invalid')
    result = execution_status(task, authorization)
    command = ['env', 'PYTHONPATH=_ferramentas', 'python3', '-m',
               'thunder_os.apollo_linkedin_t4', '--task', task,
               '--authorization', authorization, '--execute']
    return {'task': task, 'recipient': recipient, 'status': 'blocked',
            'execution': result,
            'checks': {key: 'unverified' for key in
                       ('campaign_approval', 'account', 'quota', 'reply_coverage',
                        'relationship', 'text')},
            'command': shlex.join(command),
            'command_status': 'blocked; does not send'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--recipient', required=True, help='LinkedIn publicIdentifier')
    parser.add_argument('--authorization', required=True)
    parser.add_argument('--db', help='existing absolute path to official SQLite database')
    parser.add_argument('--campaign')
    parser.add_argument('--cadence')
    parser.add_argument('--campaign-version')
    parser.add_argument('--apollo-actor', help='verified Apollo API actor ID for optional GET')
    parser.add_argument('--apollo-credential-file', help='absolute path to Apollo read credential')
    args = parser.parse_args(argv)
    try:
        result = prepare(args.task, args.recipient, args.authorization)
        result = inspect_observations(result, db=args.db, campaign=args.campaign,
                                      cadence=args.cadence, version=args.campaign_version,
                                      apollo_actor=args.apollo_actor,
                                      apollo_credential_file=args.apollo_credential_file)
    except ValueError:
        parser.error('invalid task, recipient or authorization')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
