"""Fresh, ID-scoped HubSpot opt-out observation; no CRM writes."""
from .new_journey_hubspot import HubSpotEligibilityRead, PORTAL_ID
from .delegated_suppression import SuppressionUnavailable


class HubSpotOptOutReader:
    def __init__(self, original, *, reader_factory=HubSpotEligibilityRead):
        self.original = original
        self.reader_factory = reader_factory

    def __call__(self, contact_id):
        if not isinstance(contact_id, str) or not contact_id.isdigit():
            raise SuppressionUnavailable('invalid_contact_scope')
        try:
            # A new instance per request avoids HubSpotEligibilityRead's cache.
            reader = self.reader_factory(self.original)
            portal = reader.port._call('GET', '/account-info/v3/details')
            if str(portal.get('portalId')) != PORTAL_ID:
                raise ValueError('portal_mismatch')
            reader._schema('contacts')
            if not reader._schema_complete.get('contacts') or not reader._schema_valid.get('contacts', {}).get('hs_email_optout'):
                raise ValueError('optout_schema_unverified')
            contact, _ = reader._record('contacts', contact_id)
            props = contact.get('properties')
            if contact.get('id') != contact_id or not isinstance(props, dict) or 'hs_email_optout' not in props:
                raise ValueError('optout_property_unavailable')
            value = props['hs_email_optout']
            if value is True or value == 'true':
                blocked = True
            elif value is False or value in ('false', '', None):
                blocked = False
            else:
                raise ValueError('optout_property_invalid')
            return {'target': contact_id, 'complete': True, 'suppressed': blocked}
        except Exception:
            raise SuppressionUnavailable('hubspot_optout_unavailable') from None
