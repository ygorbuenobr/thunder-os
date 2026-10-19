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


def _text(value, limit=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _config(value):
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise ApprovalError('campaign_registry_config_invalid')
    if not isinstance(value['campaign_id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',value['campaign_id']):
        raise ApprovalError('campaign_registry_id_invalid')
    if not _text(value['rules'], 65536):
        raise ApprovalError('campaign_registry_rules_invalid')
    configuration = value['configuration']
    if (not isinstance(configuration, Mapping)
            or set(configuration) != {'flow_version', 'provider_accounts'}
            or not _text(configuration['flow_version'])):
        raise ApprovalError('campaign_registry_configuration_invalid')
    accounts = configuration['provider_accounts']
    providers = value['providers']
    if (not isinstance(providers, Mapping) or not isinstance(accounts, Mapping)
            or set(accounts) != set(providers) or any(not _text(v) for v in accounts.values())):
        raise ApprovalError('campaign_registry_accounts_invalid')
    normalized = {**value, 'providers': dict(providers), 'configuration': {
        'flow_version': configuration['flow_version'], 'provider_accounts': dict(accounts)}}
    # Reuse the exact execution-context types and provider identifiers.
    _context(normalized, 1)
    return deepcopy(normalized)


def _context(config, revision):
    if type(revision) is not int or revision < 1:
        raise ApprovalError('campaign_registry_revision_invalid')
    try:
        rules_hash = hashlib.sha256(config['rules'].encode('utf-8')).hexdigest()
        configuration_hash = payload_hash({'configuration': config['configuration'],
                                          'providers': config['providers'], 'revision': revision})
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

    def binding(self, ident):
        with self.repo.transaction():
            row, context = self._read(ident)
            return {'providers': dict(context['providers']),
                    'provider_accounts': dict(row['config']['configuration']['provider_accounts'])}

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
