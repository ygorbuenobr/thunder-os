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
            or set(configuration) - {'flow_version', 'provider_accounts', 'linkedin_policy'}
            or not _text(configuration['flow_version'])):
        raise ApprovalError('campaign_registry_configuration_invalid')
    accounts = configuration['provider_accounts']
    providers = value['providers']
    if (not isinstance(providers, Mapping) or not isinstance(accounts, Mapping)
            or set(accounts) != set(providers) or any(not _text(v) for v in accounts.values())):
        raise ApprovalError('campaign_registry_accounts_invalid')
    normalized = {**value, 'providers': dict(providers), 'configuration': {
        'flow_version': configuration['flow_version'], 'provider_accounts': dict(accounts)}}
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
