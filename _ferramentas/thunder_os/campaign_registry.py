"""Local canonical campaign registry in existing versioned workflow state.

No external campaign registration or activation. Caller composition owns authority.
"""
from copy import deepcopy
import hashlib
import re
from collections.abc import Mapping

from .approvals import ApprovalError, execution_context
from .db import RepositoryConflict
from .models import payload_hash


_FIELDS = {'campaign_id', 'campaign_version', 'rules_version', 'run_id',
           'platform', 'providers', 'rules', 'configuration'}
_OPTIONAL = {'briefing', 'batches'}


def _text(value, limit=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def sequence_allowed(binding, sequence_id):
    """Match an actual task sequence against the exact, non-ambiguous approved scope."""
    ident = re.compile(r'[A-Za-z0-9_.-]{1,160}\Z')
    if (not isinstance(binding, Mapping) or not isinstance(sequence_id, str)
            or not ident.fullmatch(sequence_id)
            or ('sequence_id' in binding) == ('sequence_ids' in binding)):
        return False
    if 'sequence_id' in binding:
        value = binding['sequence_id']
        return isinstance(value, str) and bool(ident.fullmatch(value)) and sequence_id == value
    values = binding['sequence_ids']
    return (isinstance(values, list) and 0 < len(values) <= 100
            and all(isinstance(value, str) and ident.fullmatch(value) for value in values)
            and len(set(values)) == len(values) and sequence_id in values)


def _config(value):
    if (not isinstance(value, Mapping) or not _FIELDS <= set(value)
            or set(value) - _FIELDS - _OPTIONAL):
        raise ApprovalError('campaign_registry_config_invalid')
    if not isinstance(value['campaign_id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',value['campaign_id']):
        raise ApprovalError('campaign_registry_id_invalid')
    if not _text(value['rules'], 65536):
        raise ApprovalError('campaign_registry_rules_invalid')
    configuration = value['configuration']
    if (not isinstance(configuration, Mapping)
            or not {'flow_version', 'provider_accounts'} <= set(configuration)
            or set(configuration) - {'flow_version', 'provider_accounts', 'linkedin_policy', 'apollo_linkedin', 'required_custom_field_id', 'required_custom_field_ids'}
            or not _text(configuration['flow_version'])):
        raise ApprovalError('campaign_registry_configuration_invalid')
    accounts = configuration['provider_accounts']
    providers = value['providers']
    if (not isinstance(providers, Mapping) or not isinstance(accounts, Mapping)
            or set(accounts) != set(providers) or any(not _text(v) for v in accounts.values())):
        raise ApprovalError('campaign_registry_accounts_invalid')
    normalized = {**value, 'providers': dict(providers), 'configuration': {
        'flow_version': configuration['flow_version'], 'provider_accounts': dict(accounts)}}
    if 'required_custom_field_id' in configuration:
        field_id = configuration['required_custom_field_id']
        if (not isinstance(field_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', field_id)
                or 'apollo_linkedin' not in configuration):
            raise ApprovalError('campaign_registry_custom_field_invalid')
        normalized['configuration']['required_custom_field_id'] = field_id
    if 'required_custom_field_ids' in configuration:
        fields_map = configuration['required_custom_field_ids']
        if (not isinstance(fields_map, Mapping)
                or set(fields_map) != {'linkedin_step_connect', 'linkedin_step_message'}
                or 'required_custom_field_id' in configuration or 'apollo_linkedin' not in configuration
                or any(not isinstance(field, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', field)
                       for field in fields_map.values())):
            raise ApprovalError('campaign_registry_custom_field_invalid')
        normalized['configuration']['required_custom_field_ids'] = dict(fields_map)
    if 'apollo_linkedin' in configuration:
        # Routing data only: a registered configuration is NOT an approval.
        # All bindings are covered by the existing configuration hash/revision.
        binding = configuration['apollo_linkedin']
        fields = {'company_id', 'team_id', 'owner_ids', 'account_id', 'public_id'}
        ident = re.compile(r'[A-Za-z0-9_.-]{1,160}\Z')
        if (not isinstance(binding, Mapping)
                or set(binding) not in (fields | {'sequence_id'}, fields | {'sequence_ids'})
                or any(not isinstance(binding[k], str) or not ident.fullmatch(binding[k])
                       for k in fields - {'owner_ids'})
                or ('sequence_id' in binding and (not isinstance(binding['sequence_id'], str)
                    or not ident.fullmatch(binding['sequence_id'])))
                or ('sequence_ids' in binding and (not isinstance(binding['sequence_ids'], list)
                    or not binding['sequence_ids'] or len(binding['sequence_ids']) > 100
                    or any(not isinstance(seq, str) or not ident.fullmatch(seq) for seq in binding['sequence_ids'])
                    or len(set(binding['sequence_ids'])) != len(binding['sequence_ids'])))
                or not re.fullmatch(r'[a-f0-9]{24}', binding['account_id'])
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', binding['public_id'])
                or not isinstance(binding['owner_ids'], list) or not binding['owner_ids']
                or len(binding['owner_ids']) > 100
                or any(not isinstance(owner, str) or not ident.fullmatch(owner) for owner in binding['owner_ids'])
                or len(set(binding['owner_ids'])) != len(binding['owner_ids'])
                or providers.get('linkedin') != 'connectsafely'
                or accounts.get('linkedin') != binding['account_id']):
            raise ApprovalError('campaign_registry_apollo_linkedin_binding_invalid')
        normalized['configuration']['apollo_linkedin'] = {
            **dict(binding), 'owner_ids': list(binding['owner_ids'])}
        if 'sequence_ids' in binding:
            normalized['configuration']['apollo_linkedin']['sequence_ids'] = list(binding['sequence_ids'])
    if 'linkedin_policy' in configuration:
        from .linkedin_preparation import validate_policy
        try:
            normalized['configuration']['linkedin_policy']=validate_policy(configuration['linkedin_policy'])
        except ValueError:
            raise ApprovalError('campaign_registry_linkedin_policy_invalid') from None
    if 'briefing' in value:
        from .campaign_sheet import validate_sheet
        normalized['briefing'] = validate_sheet(value['briefing'])
    if 'batches' in value:
        from .campaign_batches import validate_batches
        normalized['batches'] = validate_batches(value['batches'])
    # Reuse context validation; accepting an external platform does not grant OS execution.
    _context(normalized, 1)
    return deepcopy(normalized)


def _context(config, revision):
    if type(revision) is not int or revision < 1:
        raise ApprovalError('campaign_registry_revision_invalid')
    try:
        rules_hash = hashlib.sha256(config['rules'].encode('utf-8')).hexdigest()
        configuration_hash = payload_hash({'configuration': config['configuration'],
            'providers': config['providers'], 'revision': revision,
            **{key: config[key] for key in _OPTIONAL if key in config}})
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ApprovalError('campaign_registry_config_invalid') from exc
    return execution_context({key: config[key] for key in (
        'platform', 'campaign_id', 'campaign_version', 'rules_version', 'run_id', 'providers')}
        | {'rules_hash': rules_hash, 'configuration_hash': configuration_hash})


class CampaignRegistry:
    def __init__(self, repo):
        self.repo = repo

    @staticmethod
    def _authority(actor, expected_version):
        if actor != 'ygor':
            raise ApprovalError('campaign_registry_human_authority_required')
        if type(expected_version) is not int or expected_version < 0:
            raise ApprovalError('campaign_registry_version_invalid')

    def register(self, config, actor, expected_version):
        self._authority(actor, expected_version)
        if getattr(self.repo, 'dispatch_fence_active', False):
            raise ApprovalError('campaign_dispatch_in_progress')
        config = _config(config)
        context = _context(config, expected_version + 1)
        try:
            with self.repo.transaction():
                codes = {code for batch in config.get('batches', []) for code in batch['source_codes']}
                if codes:
                    for existing in self.repo.list_state('campaign_config'):
                        if existing['id'] == config['campaign_id'] or not existing.get('active'):
                            continue
                        other, _ = self._read(existing['id'])
                        claimed = {code for batch in other['config'].get('batches', [])
                                   for code in batch['source_codes']}
                        if codes & claimed:
                            raise ApprovalError('campaign_batch_parent_conflict')
                return self.repo.put_states_atomic([('campaign_config', config['campaign_id'], {
                    'config': config, 'context': context, 'active': True,
                    'authorized_by': 'ygor'}, expected_version)])[0]
        except RepositoryConflict as exc:
            raise ApprovalError('campaign_registry_version_conflict') from exc

    def _read(self, ident, *, allow_inactive=False):
        if not _text(ident):
            raise ApprovalError('campaign_registry_id_invalid')
        try:
            row = self.repo.get_state('campaign_config', ident)
        except (ValueError, TypeError) as exc:
            raise ApprovalError('campaign_registry_source_invalid') from exc
        if row is None:
            raise ApprovalError('campaign_registry_missing')
        if (set(row) != {'config', 'context', 'active', 'authorized_by', 'id', 'version', 'updated_at'}
                or row['authorized_by'] != 'ygor' or type(row['active']) is not bool):
            raise ApprovalError('campaign_registry_source_invalid')
        config = _config(row['config'])
        context = _context(config, row['version'])
        if config['campaign_id'] != ident or row['context'] != context:
            raise ApprovalError('campaign_registry_integrity_invalid')
        if not allow_inactive and row['active'] is not True:
            raise ApprovalError('campaign_registry_revoked')
        return row, context

    def load(self, ident):
        with self.repo.transaction():
            _, context = self._read(ident)
            return deepcopy(context)

    def list_campaigns(self):
        """Validated display metadata, including revoked entries; never a binding."""
        with self.repo.transaction():
            try:
                ids = [row['id'] for row in self.repo.list_state('campaign_config')]
            except (ValueError, TypeError, KeyError) as exc:
                raise ApprovalError('campaign_registry_source_invalid') from exc
            result = []
            for ident in sorted(ids):
                row, context = self._read(ident, allow_inactive=True)
                result.append({'id': ident, 'platform': context['platform'], 'active': row['active']})
            return result

    def binding(self, ident):
        with self.repo.transaction():
            row, context = self._read(ident)
            return {'providers': dict(context['providers']),
                    'provider_accounts': dict(row['config']['configuration']['provider_accounts'])}

    def configuration(self, ident):
        """Validated configuration only; it never grants execution authority."""
        with self.repo.transaction():
            row,_=self._read(ident)
            return deepcopy(row['config']['configuration'])

    def sheet(self, ident):
        with self.repo.transaction():
            row, _ = self._read(ident)
            return deepcopy(row['config'].get('briefing'))

    def revoke(self, ident, actor, expected_version):
        self._authority(actor, expected_version)
        if getattr(self.repo, 'dispatch_fence_active', False):
            raise ApprovalError('campaign_dispatch_in_progress')
        try:
            with self.repo.transaction():
                row, _ = self._read(ident, allow_inactive=True)
                return self.repo.put_states_atomic([('campaign_config', ident, {
                    'config': row['config'], 'context': _context(row['config'], expected_version + 1),
                    'active': False, 'authorized_by': 'ygor'}, expected_version)])[0]
        except RepositoryConflict as exc:
            raise ApprovalError('campaign_registry_version_conflict') from exc
