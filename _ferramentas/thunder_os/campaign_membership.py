"""Single-contact membership proposals. HubSpot is the membership authority.

No route, scheduler, schema creation or provider activation. The independent
mutation switch defaults OFF, regardless of legacy external-effects settings.
Repository CAS covers cooperating proposals, never external platform enrollment.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import re

from .approvals import ApprovalError
from .campaign_registry import CampaignRegistry
from .db import RepositoryConflict
from .integration_hubspot import PORTAL_ID


PROPOSAL_KIND = 'campaign_membership_proposal'
PROPERTIES = ('campanha_ativa', 'executor')


class CampaignMembershipError(ValueError):
    """Stable sanitized code; adapters may map it to their existing error shape."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise CampaignMembershipError(code)


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,29}', value)


def _campaign(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value)


def _version(value, minimum=0):
    _require(type(value) is int and value >= minimum, 'membership_version_invalid')


class CampaignMembership:
    """Reader + internal proposal workflow; writer is an explicit injected port.

    reader: portal(), metadata('contacts'), read('contacts', id, properties), as
    implemented by HubSpotHTTP. writer: write_membership(command), read_note(id).
    authorize(command) must use existing trusted server authority, not a browser
    boolean. Production composition must retain mutation_enabled=False today.
    """

    def __init__(self, repository, reader, *, writer=None, mutation_enabled=False,
                 authorize=None):
        self.repo, self.reader, self.writer = repository, reader, writer
        self.mutation_enabled, self.authorize = mutation_enabled, authorize
        self.registry = CampaignRegistry(repository)

    def _enabled(self):
        return (self.mutation_enabled is True and self.writer is not None
                and callable(self.authorize))

    @staticmethod
    def _validate(contact_id, campaign_id, action, reply_ref):
        _require(_identifier(contact_id), 'membership_contact_id_invalid')
        _require(_campaign(campaign_id), 'membership_campaign_id_invalid')
        _require(action in ('assign', 'release'), 'membership_action_invalid')
        if action == 'release':
            _require(isinstance(reply_ref, str) and bool(re.fullmatch(
                r'[A-Za-z0-9_.:/-]{1,200}', reply_ref)), 'invalid_reply_ref')
        else:
            _require(reply_ref is None, 'unexpected_reply_ref')

    def _read(self, contact_id):
        _require(self.reader.portal() == PORTAL_ID, 'membership_portal_mismatch')
        schema = self.reader.metadata('contacts')
        _require(isinstance(schema, Mapping) and schema.get('portal_id') == PORTAL_ID
                 and schema.get('object_type') == 'contacts', 'membership_schema_invalid')
        fields = schema.get('properties')
        _require(isinstance(fields, Mapping) and all(
            isinstance(fields.get(name), Mapping) and fields[name].get('type') == 'string'
            and fields[name].get('readOnlyValue') is False for name in PROPERTIES),
            'membership_configuration_pending')
        record = self.reader.read('contacts', contact_id, PROPERTIES)
        _require(isinstance(record, Mapping) and record.get('id') == contact_id
                 and record.get('archived') is False, 'membership_contact_unavailable')
        props = record.get('properties')
        _require(isinstance(props, Mapping) and all(name in props for name in PROPERTIES),
                 'membership_properties_unknown')
        campaign, executor = props['campanha_ativa'], props['executor']
        _require(campaign is None or campaign == '' or _campaign(campaign),
                 'membership_campaign_value_invalid')
        _require(executor is None or executor == '' or _campaign(executor),
                 'membership_executor_value_invalid')
        revision = record.get('updatedAt')
        _require(isinstance(revision, str) and 0 < len(revision) <= 100,
                 'membership_revision_unavailable')
        return {'campanha_ativa': campaign or '', 'executor': executor or '',
                'updated_at': revision}

    def inspect(self, contact_id, campaign_id, *, action='assign', reply_ref=None):
        """Fresh direct read, no writes, including when a conflict is detected."""
        self._validate(contact_id, campaign_id, action, reply_ref)
        result = {'contact_id': contact_id, 'campaign_id': campaign_id, 'action': action,
                  'reply_ref': reply_ref, 'status': 'blocked', 'reason': None,
                  'membership': None, 'expected': None, 'desired': None,
                  'registry_context': None, 'note': None, 'contact_conflicts': [],
                  'source': {'provider': 'hubspot', 'portal_id': PORTAL_ID,
                             'status': 'unavailable',
                             'observed_at': datetime.now(timezone.utc).isoformat()},
                  'mutation_enabled': self._enabled(), 'readback_verified': False,
                  'external_platforms_stopped': False}
        try:
            context = self.registry.load(campaign_id)
            result['registry_context'] = context
        except ApprovalError:
            result['reason'] = 'membership_registry_unavailable'
            return result
        try:
            snapshot = self._read(contact_id)
        except CampaignMembershipError as exc:
            result['reason'] = exc.code
            if exc.code == 'membership_configuration_pending':
                result['source']['status'] = 'configuration_pending'
            return result
        except Exception:
            result['reason'] = 'membership_source_unavailable'
            return result
        result['source']['status'] = 'verified'
        result['expected'] = snapshot
        current, executor = snapshot['campanha_ativa'], snapshot['executor']
        result['membership'] = {'campaign': current or None, 'executor': executor or None}
        reason = None
        if action == 'release' and current != campaign_id:
            reason = 'release_campaign_mismatch'
        elif action == 'assign' and current and current != campaign_id:
            reason = 'lead_occupied'
        elif not current and executor:
            reason = 'executor_without_campaign'
        elif current and not executor:
            reason = 'executor_missing'
        elif current and executor != context['platform']:
            reason = 'executor_differs_from_registry_platform'
        if reason:
            result['reason'] = reason
            result['contact_conflicts'] = [{
                'contact_id': contact_id, 'campaign': current or None,
                'requested_campaign': campaign_id, 'executor': executor or None,
                'platform': context['platform'], 'reason': reason}]
            return result
        result['desired'] = {'campanha_ativa': campaign_id if action == 'assign' else '',
                             'executor': context['platform'] if action == 'assign' else ''}
        result['status'] = 'unchanged' if action == 'assign' and current == campaign_id else 'ready'
        if action == 'release':
            result['note'] = (f'Thunder OS: respondeu; liberar campanha {campaign_id}; '
                              f'contato {contact_id}; referencia {reply_ref}. '
                              'Parada nas plataformas externas nao verificada.')
        return result

    def propose(self, contact_id, campaign_id, *, action='assign', reply_ref=None,
                expected_version=0):
        """Persist only a proposal/diagnostic in the existing Repository."""
        _version(expected_version)
        result = self.inspect(contact_id, campaign_id, action=action, reply_ref=reply_ref)
        try:
            with self.repo.transaction():
                previous = self.repo.get_state(PROPOSAL_KIND, contact_id)
                _require(not previous or previous['status'] not in ('attempted', 'uncertain'),
                         'membership_reconciliation_required')
                return self.repo.put_states_atomic([
                    (PROPOSAL_KIND, contact_id, result, expected_version)])[0]
        except RepositoryConflict:
            raise CampaignMembershipError('membership_version_conflict') from None

    def _authority(self, command):
        _require(self.mutation_enabled is True, 'membership_mutations_disabled')
        _require(self.writer is not None, 'membership_writer_unavailable')
        control = self.repo.get_state('control', 'global')
        _require(not control or not control.get('paused'), 'global_paused')
        try:
            allowed = callable(self.authorize) and self.authorize(deepcopy(command)) is True
        except Exception:
            allowed = False
        _require(allowed, 'membership_not_authorized')

    def execute(self, contact_id, *, expected_version):
        """One synchronous attempt; HTTP acceptance alone never confirms success.

        A committed attempted/uncertain proposal cannot be reproposed/retried.
        No HTTP route should expose this in the current read-only composition.
        """
        _require(_identifier(contact_id), 'membership_contact_id_invalid')
        _version(expected_version, 1)
        _require(self.mutation_enabled is True, 'membership_mutations_disabled')
        _require(getattr(self.repo._local, 'connection', None) is None,
                 'membership_outer_transaction_blocked')
        try:
            with self.repo.transaction():
                row = self.repo.get_state(PROPOSAL_KIND, contact_id)
                _require(row is not None, 'membership_proposal_missing')
                _require(row['version'] == expected_version, 'membership_version_conflict')
                self._authority(row)
                if row['status'] in ('confirmed', 'attempted', 'uncertain'):
                    return row
                _require(row['status'] == 'ready', 'proposal_not_ready')
                current = self.inspect(contact_id, row['campaign_id'], action=row['action'],
                                       reply_ref=row['reply_ref'])
                _require(current['status'] == 'ready' and all(current[key] == row[key]
                    for key in ('expected', 'desired', 'registry_context', 'note')),
                    'membership_proposal_stale')
                self._authority(row)
                command = {key: deepcopy(value) for key, value in row.items()
                           if key not in ('id', 'version', 'updated_at')}
                row = self.repo.put_states_atomic([(PROPOSAL_KIND, contact_id,
                    {**command, 'status': 'attempted'}, expected_version)])[0]
        except RepositoryConflict:
            raise CampaignMembershipError('membership_version_conflict') from None
        # Commit intent before any provider effect. A crash retains attempted.
        outcome = {**command, 'status': 'uncertain', 'reason': 'membership_effect_uncertain',
                   'readback_verified': False, 'note_id': None}
        try:
            self._authority(command)
            receipt = self.writer.write_membership(deepcopy(command))
            _require(isinstance(receipt, Mapping) and receipt.get('contact_id') == contact_id,
                     'membership_receipt_invalid')
            actual = self._read(contact_id)
            _require(all(actual[name] == command['desired'][name] for name in PROPERTIES),
                     'membership_readback_mismatch')
            if command['note'] is not None:
                note_id = receipt.get('note_id')
                _require(_identifier(note_id), 'membership_note_unverified')
                note = self.writer.read_note(note_id)
                _require(isinstance(note, Mapping) and note.get('id') == note_id
                         and note.get('portal_id') == PORTAL_ID
                         and note.get('contact_id') == contact_id
                         and note.get('body') == command['note'], 'membership_note_unverified')
                outcome['note_id'] = note_id
            outcome.update(status='confirmed', reason=None, readback_verified=True,
                           membership={'campaign': actual['campanha_ativa'] or None,
                                       'executor': actual['executor'] or None})
            outcome['source'] = {**command['source'],
                'observed_at': datetime.now(timezone.utc).isoformat()}
        except Exception:
            # Provider bodies, reply text and transport exceptions never escape.
            pass
        try:
            return self.repo.put_states_atomic([
                (PROPOSAL_KIND, contact_id, outcome, row['version'])])[0]
        except RepositoryConflict:
            raise CampaignMembershipError('membership_reconciliation_required') from None
