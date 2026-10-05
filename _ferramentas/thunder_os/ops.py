"""Bounded local backups and internal operational alerts. Never invokes providers."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from uuid import uuid4


class OpsError(RuntimeError):
    pass


def read_db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)


def backup(database, destination, *, timeout=120, max_bytes=3*1024**3, reserve_bytes=2*1024**3):
    """Online snapshot, integrity check and test restoration; existing backups are never deleted."""
    started = time.monotonic()
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    def bounded(*_args):
        if len(_args) == 3 and _args[2] * page_size > max_bytes:
            raise OpsError('backup_size_limit')
        if time.monotonic() - started > timeout:
            raise OpsError('backup_timeout')
    with closing(read_db(database)) as source:
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        size = source.execute('PRAGMA page_count').fetchone()[0] * page_size
        if size > max_bytes:
            raise OpsError('backup_size_limit')
        if shutil.disk_usage(destination).free < size*3 + reserve_bytes:
            raise OpsError('backup_insufficient_disk')
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex + '.sqlite3'
        # Only our uncommitted temporary files may be cleaned up on failure.
        with tempfile.TemporaryDirectory(prefix='.backup-', dir=destination) as temporary:
            snapshot = Path(temporary)/'snapshot.sqlite3'
            restored = Path(temporary)/'restore.sqlite3'
            with closing(sqlite3.connect(snapshot)) as target:
                source.backup(target, pages=128, progress=bounded, sleep=.05)
                target.set_progress_handler(lambda: int(time.monotonic()-started > timeout), 1000)
                if target.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise OpsError('backup_integrity_failed')
                with closing(sqlite3.connect(restored)) as restore:
                    target.backup(restore, pages=128, progress=bounded, sleep=.05)
                    restore.set_progress_handler(lambda: int(time.monotonic()-started > timeout), 1000)
                    if restore.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                        raise OpsError('restore_integrity_failed')
                    original_schema = target.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
                    if restore.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall() != original_schema:
                        raise OpsError('restore_schema_mismatch')
            bounded()
            if snapshot.stat().st_size > max_bytes:
                raise OpsError('backup_size_limit')
            with snapshot.open('rb') as stream:
                os.fsync(stream.fileno())
            final = destination/name
            snapshot.rename(final)
    directory_fd = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {'status': 'verified', 'file': final.name, 'bytes': final.stat().st_size,
            'integrity': 'ok', 'restore': 'ok', 'completed_at': datetime.now(timezone.utc).isoformat()}


def inspect(database, heartbeats, *, now=None, stale_seconds=120, queue_age_seconds=900, usage_path=None, backup_destination=None):
    now = now or datetime.now(timezone.utc)
    alerts = []
    metrics = {}
    def alert(code, severity='warning', **counts):
        alerts.append({'code': code, 'severity': severity, **counts})
    with closing(read_db(database)) as con:
        con.set_progress_handler(lambda: int(time.monotonic()-started > 10), 1000)
        started = time.monotonic()
        for table, date in [('thunder_outbox', 'created_at'), ('thunder_effect_jobs', 'updated_at')]:
            try:
                rows = con.execute(f'SELECT state,COUNT(*),MIN({date}) FROM {table} GROUP BY state').fetchall()
                metrics[table] = {state: count for state, count, _ in rows}
                for state, count, oldest in rows:
                    if state in {'uncertain', 'needs_reconciliation', 'failed'} and count:
                        alert(f'{table}_{state}', count=count)
                    if state == 'pending' and oldest:
                        try:
                            age = (now-datetime.fromisoformat(oldest)).total_seconds()
                            if age > queue_age_seconds:
                                alert(f'{table}_backlog', count=count)
                        except (ValueError, TypeError):
                            alert(f'{table}_invalid_timestamp')
            except sqlite3.Error:
                alert(f'{table}_unavailable', 'error')
    for role, path in heartbeats.items():
        try:
            path = Path(path)
            if path.stat().st_size > 65536:
                raise ValueError()
            value = json.loads(path.read_text())
            age = (now-datetime.fromisoformat(value['heartbeat_at'].replace('Z', '+00:00'))).total_seconds()
            if age < -5 or age > stale_seconds:
                alert(f'{role}_heartbeat_stale', 'error')
            if value.get('status') == 'disabled' or (value.get('status') == 'paused' and value.get('claims_enabled') is False):
                metrics[role] = value['status']
            elif value.get('status') != 'ok' or value.get('readiness') is not True:
                alert(f'{role}_not_ready', 'error')
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            alert(f'{role}_heartbeat_unavailable', 'error')
    with closing(read_db(database)) as con:
        try:
            rows = con.execute("SELECT payload FROM thunder_state WHERE kind='agent_usage' ORDER BY updated_at DESC LIMIT 1000").fetchall()
            counts = {'records': len(rows), 'errors': 0, 'attempts': 0}
            for (raw,) in rows:
                value = json.loads(raw)
                counts['errors'] += int(bool(value.get('error_code') or value.get('status') in {'failed', 'needs_human'}))
                attempts = (int(value['attempted']) if type(value.get('attempted')) is bool
                            else value.get('attempts', value.get('attempt', 0)))
                if type(attempts) is int and attempts >= 0:
                    counts['attempts'] += attempts
            metrics['model_usage'] = counts
            if not rows:
                metrics['model_usage']['state'] = 'not_started'
                if metrics.get('agent') not in {'paused', 'disabled'}:
                    alert('model_usage_unavailable')
            elif counts['errors']:
                alert('model_usage_errors', count=counts['errors'])
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            alert('model_usage_unavailable')
    with closing(read_db(database)) as con:
        row = con.execute("SELECT payload FROM thunder_state WHERE kind='operator_alert' AND entity_id='backup'").fetchone()
        try:
            last = json.loads(row[0]) if row else {}
            # Failure snapshots retain the most recent verified backup timestamp.
            completed = last.get('last_success_at') or (last.get('completed_at') if last.get('status') == 'verified' else None)
            age = (now-datetime.fromisoformat(completed)).total_seconds() if completed else None
            metrics['backup'] = {'last_success_at': completed, 'age_seconds': age}
            if age is None or age < 0 or age > 8*3600:
                alert('backup_overdue', 'error')
        except (TypeError, ValueError):
            alert('backup_status_invalid', 'error')
    if backup_destination:
        try:
            free = shutil.disk_usage(backup_destination).free
            metrics['backup_disk_free_bytes'] = free
            if free < 2*1024**3:
                alert('backup_disk_low', 'error')
        except OSError:
            alert('backup_destination_unavailable', 'error')
    return {'checked_at': now.isoformat(), 'alerts': alerts, 'metrics': metrics}


def record(database, report, kind='monitor'):
    """Write one bounded internal snapshot and a sanitized event to existing OS tables."""
    now = datetime.now(timezone.utc).isoformat()
    # No Repository construction: operational checks must never run schema migrations.
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=rw', uri=True, timeout=2)) as con:
        with con:
            if kind == 'backup':
                previous = con.execute("SELECT payload FROM thunder_state WHERE kind='operator_alert' AND entity_id='backup'").fetchone()
                old = json.loads(previous[0]) if previous else {}
                success = report.get('completed_at') if report.get('status') == 'verified' else old.get('last_success_at', old.get('completed_at') if old.get('status') == 'verified' else None)
                report = {**report, 'last_success_at': success}
            con.execute('''INSERT INTO thunder_state(kind,entity_id,version,payload,updated_at)
                VALUES ('operator_alert',?,1,?,?) ON CONFLICT(kind,entity_id) DO UPDATE SET
                version=version+1,payload=excluded.payload,updated_at=excluded.updated_at''',
                (kind, json.dumps(report, sort_keys=True), now))
            con.execute('INSERT INTO thunder_events VALUES (?,?,?,?,?)',
                (uuid4().hex, 'operator:'+kind, 'operator_'+kind, json.dumps(report, sort_keys=True), now))


def append_log(path, kind, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': kind, 'at': datetime.now(timezone.utc).isoformat(), **report}, sort_keys=True)+'\n')
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'monitor'])
    parser.add_argument('--database', required=True)
    parser.add_argument('--destination')
    parser.add_argument('--worker-health')
    parser.add_argument('--agent-health')
    parser.add_argument('--usage-metrics')
    parser.add_argument('--events-log')
    args = parser.parse_args(argv)
    try:
        if args.action == 'backup':
            if not args.destination:
                parser.error('--destination required')
            report = backup(args.database, args.destination)
        else:
            report = inspect(args.database, {key: path for key, path in
                [('worker', args.worker_health), ('agent', args.agent_health)] if path}, usage_path=args.usage_metrics, backup_destination=args.destination)
        record(args.database, report, args.action)
        if args.events_log:
            append_log(args.events_log, args.action, report)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (OpsError, sqlite3.Error, OSError):
        report = {'alerts': [{'code': args.action+'_failed', 'severity': 'error'}]}
        try:
            record(args.database, report, args.action)
            if args.events_log:
                append_log(args.events_log, args.action, report)
        except (sqlite3.Error, OSError):
            pass
        print(json.dumps(report))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
