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
