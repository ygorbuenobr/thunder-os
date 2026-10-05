"""Explicit provider selection. Legacy Unipile is history-only, never a fallback."""
from .connectsafely import ConnectSafelyAdapter
from ..connectsafely_client import require,select


def linkedin_provider_name(value=None):
    # No environment selector can silently switch an approved write to Unipile.
    provider='connectsafely' if value is None else value
    require(provider=='connectsafely','legacy_provider_writes_disabled')
    return provider


def select_linkedin_adapter(provider=None,*,client=None,social_client=None):
    linkedin_provider_name(provider)
    return ConnectSafelyAdapter(client,social_client=social_client)


class LegacyLinkedInHistory:
    """Caller explicitly supplies a read-only migration reader; no send method."""
    def __init__(self,reader):
        require(callable(reader),'history_reader_unconfigured');self.reader=reader
    def history(self,*,account_id,limit=10):
        require(isinstance(account_id,str) and 0<len(account_id)<=128 and type(limit) is int and 1<=limit<=10,'invalid_history_request')
        try:rows=self.reader(account_id=account_id,limit=limit)
        except Exception:
            from ..connectsafely_client import ConnectSafelyError
            raise ConnectSafelyError('history_read_unavailable') from None
        require(isinstance(rows,list) and len(rows)<=limit,'provider_schema_invalid')
        fields={'id':str,'status':str,'receipt_ref':str,'action':str,'timestamp':str}
        return {'provider':'unipile','history_only':True,'records':[select(row,fields) for row in rows]}


def select_linkedin_history(provider,*,reader):
    require(provider=='unipile','unsupported_history_provider')
    return LegacyLinkedInHistory(reader)
