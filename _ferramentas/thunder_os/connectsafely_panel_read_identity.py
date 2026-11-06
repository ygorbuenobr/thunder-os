"""In-process account-status facade with no public send interface.

This is an API boundary, not a security boundary. The bound read method keeps a
transitive reference to its write-capable owner and Python introspection can
recover it. Only use after explicit acceptance of that same-process risk.
"""
import re


class PanelReadIdentity:
    __slots__ = ('_account_id', '_public_id', '_read_status')

    def __init__(self, direct_client, *, account_id, public_id):
        if (not isinstance(account_id, str) or not re.fullmatch(r'[a-f0-9]{24}', account_id)
                or not isinstance(public_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', public_id)
                or getattr(direct_client, 'account_id', None) != account_id
                or getattr(direct_client, 'expected_public_id', None) != public_id):
            raise ValueError('account_identity_mismatch')
        read = getattr(direct_client, 'account_status', None)
        if not callable(read):
            raise ValueError('account_status_unavailable')
        self._account_id, self._public_id, self._read_status = account_id, public_id, read

    @property
    def account_id(self):
        return self._account_id

    @property
    def public_id(self):
        return self._public_id

    def account_status(self):
        try:
            result = self._read_status()
            if (not isinstance(result, dict) or result.get('id') != self.account_id
                    or result.get('publicId') != self.public_id
                    or type(result.get('available')) is not bool):
                raise ValueError
            return {'id': self.account_id, 'publicId': self.public_id, 'available': result['available'],
                    'write_authorized': False}
        except Exception:
            raise ValueError('account_status_unavailable') from None
