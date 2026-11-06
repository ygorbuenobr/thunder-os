"""One bounded, opt-in Apollo reply observation for previously bound contacts.

This worker never sends messages or changes Apollo. It reports incomplete reads
as unknown and creates only a conservative local stop and an operator alert.
It does not claim classification of a reply or a complete conversation history.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .apollo_linkedin_identity_binding import KIND, binding_key
from .apollo_linkedin_reply_coverage import ApolloContactReplyReader, ReplyCoverage
from .apollo_linkedin_runtime_safety import RuntimeSafety
from .models import payload_hash


class ApolloReplyMonitor:
    """Observe one tenant-scoped binding per call with existing reader authorities."""
    def __init__(self, repo, reader, *, company, team):
        if (not isinstance(company, str) or not company or not isinstance(team, str) or not team
                or not isinstance(reader, ApolloContactReplyReader) or reader.team_id != team):
            raise ValueError('monitor_configuration_invalid')
        self.repo, self.reader, self.company, self.team = repo, reader, company, team

    def _binding(self, row):
        if not isinstance(row, dict) or row.get('version') != 1:
            raise ValueError('binding_unavailable')
        fields = ('company', 'campaign_id', 'approval_id', 'campaign_version', 'scope_hash',
                  'team_id', 'sequence_id', 'task_id', 'action_type', 'contact_id', 'account_id',
                  'recipient_public_id', 'member_urn')
        if not all(isinstance(row.get(k), str) and row[k] for k in fields):
            raise ValueError('binding_invalid')
        if (row['company'] != self.company or row['team_id'] != self.team or row['id'] !=
                binding_key(row['company'], row['campaign_id'], row['task_id'], row['account_id'], row['approval_id'])):
            raise ValueError('binding_scope_mismatch')
        approval = self.repo.get_state('approval', row['approval_id'])
        snapshot = self.repo.get_state('campaign_version_snapshot', 'apollo-campaign-' + row['scope_hash'])
        scope = snapshot.get('scope') if isinstance(snapshot, dict) else None
        approved = scope.get('configuration', {}).get('apollo_linkedin') if isinstance(scope, dict) else None
        if (not isinstance(approval, dict) or type(approval.get('version')) is not int
                or approval['version'] < 1
                or approval.get('actor') != 'ygor' or approval.get('scope') != 'campaign_version'
                or approval.get('campaign_id') != row['campaign_id']
                or approval.get('campaign_version') != row['campaign_version']
                or approval.get('scope_hash') != row['scope_hash']
                or approval.get('snapshot_id') != 'apollo-campaign-' + row['scope_hash']
                or not isinstance(snapshot, dict) or snapshot.get('version') != 1
                or snapshot.get('scope_hash') != row['scope_hash']
                or payload_hash(scope) != row['scope_hash'] or not isinstance(approved, dict)
                or any(approved.get(k) != row[v] for k, v in
                       (('company_id', 'company'), ('team_id', 'team_id'),
                        ('sequence_id', 'sequence_id'), ('account_id', 'account_id')))):
            raise ValueError('approval_scope_unverified')
        reservation = self.repo.get_apollo_linkedin_reservation(row['company'], row['task_id'], row['action_type'])
        if (not isinstance(reservation, dict) or reservation.get('account_id') != row['account_id']
                or reservation.get('recipient') != row['recipient_public_id']
                or reservation.get('campaign_version') != str(row['campaign_version'])):
            raise ValueError('reservation_unverified')
        matches = [e for e in self.repo.events(row['task_id'])
                   if e.get('type') == 'apollo_linkedin_campaign_reservation'
                   and isinstance(e.get('payload'), dict) and e['payload'].get('binding_id') == row['id']]
        if len(matches) != 1 or any(matches[0]['payload'].get(k) != row[v] for k, v in
            (('company', 'company'), ('campaign_id', 'campaign_id'), ('approval_id', 'approval_id'),
             ('approval_scope_hash', 'scope_hash'), ('apollo_team_id', 'team_id'),
             ('apollo_sequence_id', 'sequence_id'), ('task_id', 'task_id'), ('action_type', 'action_type'),
             ('campaign_version', 'campaign_version'), ('apollo_contact_id', 'contact_id'),
             ('account_id', 'account_id'), ('member_urn', 'member_urn'))):
            raise ValueError('reservation_binding_unverified')
        return row

    def _record(self, binding):
        """Persist evidence of an unclassified reply and the existing T4 stop shape."""
        stop_id = RuntimeSafety._key({'company_id': binding['company']},
                                     {'recipient_public_id': binding['recipient_public_id']})
        observation_id = payload_hash({'company': binding['company'], 'campaign': binding['campaign_id'],
                                       'sequence': binding['sequence_id'], 'contact': binding['contact_id']})
        alert_id = 'apollo-reply:' + observation_id
        with self.repo.transaction():
            self._binding(self.repo.get_state(KIND, binding['id']))
            prior = self.repo.get_state('apollo_reply_observation', observation_id)
            if prior is None:
                self.repo.put_state('apollo_reply_observation', observation_id,
                    {'company': binding['company'], 'campaign_id': binding['campaign_id'],
                     'contact_id': binding['contact_id'], 'sequence_id': binding['sequence_id'],
                     'source': 'apollo', 'status': 'reply', 'classification': 'unknown',
                     'binding_id': binding['id']})
                self.repo.append_event(observation_id, 'apollo_reply_observed',
                                       {'company': binding['company'], 'classification': 'unknown'})
            elif (prior.get('company') != binding['company']
                  or prior.get('campaign_id') != binding['campaign_id']
                  or prior.get('contact_id') != binding['contact_id']
                  or prior.get('sequence_id') != binding['sequence_id']
                  or prior.get('source') != 'apollo' or prior.get('status') != 'reply'):
                raise ValueError('reply_observation_conflict')
            stop = self.repo.get_state('apollo_linkedin_reply_stop', stop_id)
            if stop is None:
                self.repo.put_state('apollo_linkedin_reply_stop', stop_id,
                    {'company': binding['company'], 'recipient': binding['recipient_public_id'].lower(),
                     'active': True, 'reason': 'reply_unclassified'})
            elif (stop.get('active') is not True or stop.get('company') != binding['company']
                  or stop.get('recipient') != binding['recipient_public_id'].lower()):
                raise ValueError('reply_stop_conflict')
            alert = self.repo.get_state('operator_alert', alert_id)
            if alert is None:
                self.repo.put_state('operator_alert', alert_id,
                    {'active': True, 'priority': 'high', 'reason': 'reply_unclassified',
                     'company_id': binding['company'], 'campaign_id': binding['campaign_id'],
                     'recipient_public_id': binding['recipient_public_id'],
                     'message': 'Resposta detectada no Apollo; confirmar identidade e interrupção dos canais.',
                     'external_pauses_enabled': False})
            elif (alert.get('company_id') != binding['company']
                  or alert.get('campaign_id') != binding['campaign_id']):
                raise ValueError('reply_alert_conflict')
        return observation_id

    def _next(self, previous):
        """Seek one scoped row without loading an unbounded table into memory."""
        deadline = time.monotonic() + 2
        query = ("SELECT entity_id, version, payload, updated_at FROM thunder_state "
                 "WHERE kind=? AND entity_id>? AND CASE WHEN json_valid(payload) "
                 "THEN json_extract(payload,'$.company')=? AND json_extract(payload,'$.team_id')=? "
                 "ELSE 1 END ORDER BY entity_id LIMIT 1")
        with self.repo._connect() as con:
            con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            try:
                args = (KIND, previous or '', self.company, self.team)
                row = con.execute(query, args).fetchone()
                if row is None and previous:
                    row = con.execute(query, (KIND, '', self.company, self.team)).fetchone()
                if row is None:
                    return None
                try:
                    value = json.loads(row['payload'])
                    if not isinstance(value, dict):
                        raise ValueError('binding_invalid')
                    return {**value, 'id': row['entity_id'], 'version': row['version'],
                            'updated_at': row['updated_at']}
                except (ValueError, TypeError):
                    return {'id': row['entity_id'], 'version': row['version']}
            finally:
                con.set_progress_handler(None, 0)

    def run_once(self):
        """Examine one binding per call. No execution authority or send is conferred."""
        cursor_key = payload_hash({'company': self.company, 'team': self.team})
        cursor = self.repo.get_state('apollo_reply_monitor_cursor', cursor_key)
        if cursor is not None and (not isinstance(cursor, dict) or cursor.get('company') != self.company
                or cursor.get('team') != self.team or not isinstance(cursor.get('last_id'), str)
                or type(cursor.get('version')) is not int or cursor['version'] < 1):
            return {'status': 'unknown', 'coverage': 'unknown', 'reason': 'cursor_invalid', 'observed': 0}
        previous = cursor.get('last_id') if isinstance(cursor, dict) else None
        try:
            selected = self._next(previous)
        except (ValueError, OSError, sqlite3.Error):
            return {'status': 'unknown', 'coverage': 'unknown', 'reason': 'scan_unavailable', 'observed': 0}
        if selected is None:
            return {'status': 'empty', 'coverage': 'complete', 'observed': 0}
        # One record per cycle never proves coverage of the whole population.
        result = {'status': 'unknown', 'coverage': 'partial', 'contact_coverage': 'unknown',
                  'observed': 1, 'binding_id': selected['id']}
        try:
            binding = self._binding(selected)
            target = {'apollo_contact_id': binding['contact_id'], 'sequence_id': binding['sequence_id'],
                      'email': None}
            status, reason = ReplyCoverage(self.reader, None)._apollo(target)
            result.update(status=status, reason=reason,
                          contact_coverage='complete' if status != 'unknown' else 'unknown')
            if status == 'reply':
                result['observation_id'] = self._record(binding)
        except Exception:
            result.update(status='unknown', contact_coverage='unknown', reason='observation_unavailable')
        try:
            version = cursor['version'] if isinstance(cursor, dict) else 0
            self.repo.put_states_atomic([('apollo_reply_monitor_cursor', cursor_key,
                {'last_id': selected['id'], 'company': self.company, 'team': self.team}, version)])
        except Exception:
            result.update(coverage='unknown', contact_coverage='unknown', reason='cursor_unavailable')
        return result


class ApolloReplyMonitorWorker:
    """Bound the polling rate so monitoring cannot starve other worker jobs."""
    def __init__(self, monitor, *, interval_seconds=60, clock=time.monotonic):
        if not callable(clock) or type(interval_seconds) is not int or not 60 <= interval_seconds <= 3600:
            raise ValueError('monitor_interval_invalid')
        self.monitor, self.interval_seconds, self.clock = monitor, interval_seconds, clock
        self._next_at = None
        self.degraded = False

    def _publish(self, result):
        """Persist source freshness and degraded coverage for operator visibility."""
        repo = self.monitor.repo
        company, team = self.monitor.company, self.monitor.team
        binding_id = result.get('binding_id', 'scan') if isinstance(result, dict) else 'scan'
        ident = payload_hash({'company': company, 'team': team, 'binding_id': binding_id})
        observed_at = datetime.now(timezone.utc).isoformat()
        unknown = (not isinstance(result, dict) or result.get('status') == 'unknown'
                   or result.get('coverage') == 'unknown')
        status = {'company': company, 'team': team, 'binding_id': binding_id,
                  'source': 'apollo', 'observed_at': observed_at,
                  'status': 'unknown' if unknown else result.get('status'),
                  'coverage': 'unknown' if unknown else result.get('coverage'),
                  'contact_coverage': 'unknown' if unknown else result.get('contact_coverage')}
        alert_id = 'apollo-reply-monitor:' + ident
        with repo.transaction():
            repo.put_state('apollo_reply_monitor_status', ident, status)
            alert = repo.get_state('operator_alert', alert_id)
            if unknown:
                repo.put_state('operator_alert', alert_id, {'active': True, 'priority': 'high',
                    'company_id': company, 'source': 'apollo', 'observed_at': observed_at,
                    'message': 'Leitura de respostas do Apollo indisponível ou incompleta; verificar a cobertura.',
                    'reason': 'apollo_reply_coverage_unknown'})
            elif alert is not None and alert.get('reason') == 'apollo_reply_coverage_unknown':
                value = {k: v for k, v in alert.items() if k not in ('id', 'version', 'updated_at')}
                value['active'] = False
                value['observed_at'] = observed_at
                repo.put_state('operator_alert', alert_id, value)
        # A different contact can recover while another one remains unknown.
        # Search for any unresolved scoped alert; do not equate one successful
        # cycle with complete population coverage, including after a restart.
        deadline = time.monotonic() + 2
        with repo._connect() as con:
            con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            try:
                row = con.execute("SELECT 1 FROM thunder_state WHERE kind='operator_alert' AND "
                    "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.company_id')=? "
                    "AND json_extract(payload,'$.reason')='apollo_reply_coverage_unknown' "
                    "AND json_extract(payload,'$.active')=1 ELSE 1 END LIMIT 1", (company,)).fetchone()
                self.degraded = row is not None
            finally:
                con.set_progress_handler(None, 0)

    def process_one(self):
        if os.environ.get('THUNDER_APOLLO_LI_REPLY_MONITOR_ENABLED') != 'true':
            return False
        now = self.clock()
        if self._next_at is not None and now < self._next_at:
            return False
        self._next_at = now + self.interval_seconds
        try:
            result = self.monitor.run_once()
        except Exception:
            result = {'status': 'unknown', 'coverage': 'unknown', 'reason': 'monitor_unavailable'}
        try:
            self._publish(result)
        except Exception:
            self.degraded = True
            raise
        return result.get('status') == 'reply'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--company', required=True)
    parser.add_argument('--team', required=True)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args(argv)
    if not args.run or os.environ.get('THUNDER_APOLLO_LI_REPLY_MONITOR_ENABLED') != 'true':
        print(json.dumps({'status': 'disabled', 'coverage': 'unknown'}))
        return 2
    try:
        from .db import Repository, _official_apollo_linkedin_db
        from .credential_paths import credential_path
        from .connectsafely_live import credential
        path = Path(os.environ.get('PAINEL_DB', ''))
        if not path.is_absolute() or not path.is_file() or path.resolve() != _official_apollo_linkedin_db().resolve():
            raise ValueError('official_store_unavailable')
        if (os.environ.get('THUNDER_APOLLO_LI_COMPANY') != args.company
                or os.environ.get('THUNDER_APOLLO_LI_TEAM') != args.team
                or os.environ.get('THUNDER_APOLLO_LI_API_ACTOR') != args.actor):
            raise ValueError('monitor_scope_unverified')
        repo = Repository(path)
        repo.apollo_linkedin_store_ready()
        loader = credential(credential_path('apollo_api_key', '/root/.config/thunder/credentials/apollo_api_key.txt'))
        result = ApolloReplyMonitor(repo, ApolloContactReplyReader(loader, team_id=args.team,
                                    api_actor_id=args.actor), company=args.company, team=args.team).run_once()
    except Exception:
        result = {'status': 'unknown', 'coverage': 'unknown', 'reason': 'monitor_unavailable'}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('ok', 'reply', 'empty') and result.get('reason') != 'cursor_unavailable' else 2


if __name__ == '__main__':
    raise SystemExit(main())
