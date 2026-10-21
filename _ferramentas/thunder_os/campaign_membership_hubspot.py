"""Concrete membership HTTP port, deliberately unconnected to mutation routes.

Fixed api.hubapi.com transport and canonical credential loader. No schema, email,
sequence, enrollment, batch or external-platform writes. Both this port and the
workflow must be explicitly enabled with trusted exact-command authorization.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re
from threading import local

from .campaign_membership import (CampaignMembership, CampaignMembershipError,
                                  PROPERTIES, _identifier, _require)
from .hubspot_console import systemd_hubspot_credential
from .hubspot_executor import HubSpotHTTP, HubSpotHTTPError, _https_request
from .integration_hubspot import HubSpotContractError, PORTAL_ID


class CampaignMembershipHubSpot:
    def __init__(self, *, credential_loader=systemd_hubspot_credential, request=None,
                 timeout=20, mutation_enabled=False, authorize=None):
        _require(type(timeout) in (int, float) and math.isfinite(timeout)
                 and 0 < timeout <= 30, 'membership_timeout_invalid')
        self.mutation_enabled, self.authorize = mutation_enabled, authorize
        self._loader, self._request = credential_loader, request or _https_request
        self.timeout, self._token, self._local = timeout, None, local()
        self._http = HubSpotHTTP(self._credential, request=self._transport, timeout=timeout)

    def _credential(self):
        if self._token is None:
            try:
                value = self._loader()
            except Exception:
                raise HubSpotHTTPError('hubspot_credential_unavailable') from None
            if not isinstance(value, str) or not value or any(c.isspace() for c in value):
                raise HubSpotHTTPError('hubspot_credential_unavailable')
            self._token = value
        return self._token

    def _authority(self, command):
        _require(self.mutation_enabled is True, 'membership_mutations_disabled')
        try:
            authorized = callable(self.authorize) and self.authorize(deepcopy(command)) is True
        except Exception:
            authorized = False
        _require(authorized, 'membership_not_authorized')

    def _transport(self, method, path, body, headers, timeout):
        allowed_read = method == 'GET' and (
            path in ('/account-info/v3/details', '/crm/v3/properties/contacts',
                     '/crm/v4/associations/notes/contacts/labels')
            or bool(re.fullmatch(r'/crm/v3/objects/contacts/[1-9][0-9]{0,29}\?properties=[A-Za-z0-9_%.,-]+', path))
            or bool(re.fullmatch(r'/crm/v3/objects/notes/[1-9][0-9]{0,29}\?properties=hs_note_body&associations=contacts', path)))
        allowed_write = ((method == 'PATCH' and bool(re.fullmatch(
            r'/crm/v3/objects/contacts/[1-9][0-9]{0,29}', path)))
            or (method == 'POST' and path == '/crm/v3/objects/notes'))
        _require(allowed_read or allowed_write, 'membership_endpoint_blocked')
        if allowed_write:
            command = getattr(self._local, 'command', None)
            _require(command is not None, 'membership_not_authorized')
            self._authority(command)
        return self._request(method, path, body, headers, timeout)

    def portal(self):
        try:
            return self._http.portal()
        except HubSpotContractError:
            raise CampaignMembershipError('membership_portal_mismatch') from None

    def metadata(self, kind):
        _require(kind == 'contacts', 'membership_object_invalid')
        return self._http.metadata(kind)

    def read(self, kind, identifier, properties):
        _require(kind == 'contacts' and _identifier(identifier)
                 and set(properties) == set(PROPERTIES), 'membership_target_invalid')
        return self._http.read(kind, identifier, properties)

    def _notes_call(self, method, path, body=None):
        try:
            status, raw = self._transport(method, path, body, {
                'Authorization': 'Bearer ' + self._credential(),
                'Content-Type': 'application/json'}, self.timeout)
            if type(status) is not int or not 200 <= status < 300:
                raise HubSpotHTTPError('hubspot_http_error', status)
            if not isinstance(raw, (bytes, bytearray)) or len(raw) > 1_000_000:
                raise HubSpotHTTPError('hubspot_invalid_response')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise HubSpotHTTPError('hubspot_invalid_response')
            return result
        except (HubSpotHTTPError, CampaignMembershipError):
            raise
        except Exception:
            raise HubSpotHTTPError('hubspot_request_uncertain') from None

    def read_note(self, note_id):
        _require(_identifier(note_id), 'membership_note_id_invalid')
        self.portal()
        row = self._notes_call('GET', '/crm/v3/objects/notes/' + note_id
                               + '?properties=hs_note_body&associations=contacts')
        _require(row.get('id') == note_id and row.get('archived') is False,
                 'membership_note_unverified')
        props, associations = row.get('properties'), row.get('associations')
        _require(isinstance(props, Mapping) and isinstance(props.get('hs_note_body'), str)
                 and isinstance(associations, Mapping), 'membership_note_unverified')
        contacts = associations.get('contacts')
        _require(isinstance(contacts, Mapping) and not contacts.get('paging'),
                 'membership_note_association_incomplete')
        rows = contacts.get('results')
        _require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], Mapping)
                 and _identifier(rows[0].get('id')), 'membership_note_association_invalid')
        return {'id': note_id, 'portal_id': PORTAL_ID, 'contact_id': rows[0]['id'],
                'body': props['hs_note_body']}

    def _preflight(self, command):
        self.portal()
        schema = self.metadata('contacts').get('properties', {})
        _require(all(isinstance(schema.get(name), Mapping)
                     and schema[name].get('type') == 'string'
                     and schema[name].get('readOnlyValue') is False for name in PROPERTIES),
                 'membership_configuration_pending')
        row = self.read('contacts', command['contact_id'], PROPERTIES)
        _require(isinstance(row, Mapping) and row.get('id') == command['contact_id']
                 and row.get('archived') is False and isinstance(row.get('properties'), Mapping)
                 and all(name in row['properties'] and (row['properties'][name] is None
                         or isinstance(row['properties'][name], str)) for name in PROPERTIES),
                 'membership_contact_unavailable')
        actual = {name: row['properties'][name] or '' for name in PROPERTIES}
        actual['updated_at'] = row.get('updatedAt')
        _require(actual == command['expected'], 'membership_proposal_stale')

    def write_membership(self, command):
        """Note first on release, then exact PATCH; not a distributed transaction.

        The core persists attempted before calling this port and owns retry/crash
        handling. A direct caller must use that core, not retry this method.
        """
        self._authority(command)  # Before credentials, reads or any HTTP mutation.
        _require(isinstance(command, Mapping), 'membership_command_invalid')
        command = deepcopy(command)
        CampaignMembership._validate(command.get('contact_id'), command.get('campaign_id'),
                                     command.get('action'), command.get('reply_ref'))
        desired, expected = command.get('desired'), command.get('expected')
        _require(isinstance(desired, Mapping) and set(desired) == set(PROPERTIES)
                 and isinstance(expected, Mapping)
                 and set(expected) == {*PROPERTIES, 'updated_at'}, 'membership_command_invalid')
        context = command.get('registry_context')
        _require(isinstance(context, Mapping) and context.get('campaign_id') == command['campaign_id']
                 and context.get('platform') in ('thunder_os', 'apollo', 'instantly',
                     'hubspot_seq', 'chattie', 'codex_local'), 'membership_command_invalid')
        release = command['action'] == 'release'
        target = {'campanha_ativa': '' if release else command['campaign_id'],
                  'executor': '' if release else context['platform']}
        _require(desired == target, 'membership_command_invalid')
        _require(isinstance(expected['updated_at'], str) and bool(expected['updated_at']),
                 'membership_command_invalid')
        _require((release and expected['campanha_ativa'] == command['campaign_id']
                  and expected['executor'] == context['platform']
                  and isinstance(command.get('note'), str) and 0 < len(command['note']) <= 1000)
                 or (not release and expected['campanha_ativa'] == expected['executor'] == ''
                     and command.get('note') is None), 'membership_command_invalid')
        self._preflight(command)
        note_id = None
        self._local.command = command
        try:
            if release:
                labels = self._notes_call('GET', '/crm/v4/associations/notes/contacts/labels')
                entries = labels.get('results')
                _require(isinstance(entries, list) and not labels.get('paging'),
                         'membership_association_metadata_invalid')
                types = [row['typeId'] for row in entries if isinstance(row, Mapping)
                         and row.get('category') == 'HUBSPOT_DEFINED'
                         and 'label' in row and row['label'] is None
                         and type(row.get('typeId')) is int and row['typeId'] > 0]
                _require(len(types) == 1, 'membership_association_metadata_invalid')
                note = self._notes_call('POST', '/crm/v3/objects/notes', {
                    'properties': {'hs_timestamp': datetime.now(timezone.utc).isoformat(),
                                   'hs_note_body': command['note']},
                    'associations': [{'to': {'id': command['contact_id']}, 'types': [{
                        'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': types[0]}]}]})
                note_id = note.get('id')
                _require(_identifier(note_id), 'membership_note_unverified')
                confirmed = self.read_note(note_id)
                _require(confirmed['body'] == command['note']
                         and confirmed['contact_id'] == command['contact_id'],
                         'membership_note_unverified')
                # Another writer may have changed the lead while the note was saved.
                self._preflight(command)
            result = self._http.write({'method': 'PATCH',
                'path': '/crm/v3/objects/contacts/' + command['contact_id'],
                'body': {'properties': dict(desired)}})
            _require(result.get('id') == command['contact_id'], 'membership_receipt_invalid')
            return {'contact_id': command['contact_id'], 'note_id': note_id}
        finally:
            self._local.command = None
