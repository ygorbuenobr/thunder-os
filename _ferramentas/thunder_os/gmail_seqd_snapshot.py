"""Opt-in composition of the existing Gmail inbox and read-only SEQD port."""
from .inbox_common import InboxError
from .inbox_gmail import sync_snapshot
from .seqd_client import MAILBOX, SEQDClient


def run_snapshot(service, *, enabled=False, client=None, max_pages=5):
    if enabled is not True:
        raise InboxError('gmail_snapshot_disabled')
    port = client if client is not None else SEQDClient()
    def get(path, query):
        if path == '/users/me/profile' and query == {}:
            status=port.status()
            if status.get('status') != 'ready' or status.get('mailbox') != MAILBOX:
                raise InboxError('gmail_account_mismatch')
            return {'emailAddress':status['mailbox']}
        if path == '/users/me/messages' and query.get('q') == 'in:inbox':
            return port.snapshot_page(query.get('pageToken'))
        prefix='/users/me/threads/'
        if path.startswith(prefix) and query == {'format':'full'}:
            return port.snapshot_thread(path[len(prefix):])
        raise InboxError('gmail_snapshot_route_denied')
    return sync_snapshot(service,mailbox=MAILBOX,get=get,max_pages=max_pages,max_threads=50)


def main(argv=None, *, environ=None, service_factory=None, client=None):
    """Explicit one-shot entrypoint; no scheduler or implicit activation."""
    import argparse
    from contextlib import closing
    import os
    import sqlite3
    from pathlib import Path
    parser=argparse.ArgumentParser(description='Collect an opt-in Gmail inbox snapshot')
    parser.add_argument('--db',required=True)
    parser.add_argument('--max-pages',type=int,default=5)
    args=parser.parse_args(argv)
    env=os.environ if environ is None else environ
    if env.get('THUNDER_OS_GMAIL_SNAPSHOT_ENABLED') != 'true':
        raise InboxError('gmail_snapshot_disabled')
    if not 1 <= args.max_pages <= 20:
        raise InboxError('invalid_sync_bound')
    path=Path(args.db)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise InboxError('gmail_snapshot_database_unavailable')
    # Check the existing OS schema read-only before Repository can migrate it.
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as con:
            names={row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required={'thunder_schema','thunder_work_items','thunder_events','thunder_outbox','thunder_state','thunder_effect_jobs'}
            if not required <= names:
                raise ValueError('schema')
            columns={row[1] for row in con.execute('PRAGMA table_info(thunder_state)')}
            if not {'kind','entity_id','version','payload','updated_at'} <= columns:
                raise ValueError('schema')
    except (sqlite3.Error, ValueError):
        raise InboxError('gmail_snapshot_database_invalid') from None
    if service_factory is None:
        from .db import Repository
        from .service import ThunderService
        service_factory=lambda value: ThunderService(Repository(value))
    return run_snapshot(service_factory(path),enabled=True,client=client,max_pages=args.max_pages)


if __name__ == '__main__':
    import json
    print(json.dumps(main(),ensure_ascii=False))
