"""Campaign authority for an exact legacy preview, using existing approval state.

Call execution_guard only after the operation's durable one-shot journal commits.
The caller retains provider-specific identity, attempt ownership and retry guards.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import math
import time
from collections.abc import Mapping

from .approvals import ApprovalError, execution_request, confirm_execution, validate_execution
from .campaign_registry import CampaignRegistry
from .models import payload_hash


class OperationCampaign:
    def __init__(self, repo, kind, provider, account_id, channel, clock=time.time):
        if not isinstance(kind, str) or kind not in {'seqd', 'hubspot', 'connectsafely'}:
            raise ApprovalError('operation_kind_invalid')
        if any(not isinstance(v, str) or not v.strip() for v in (provider, account_id, channel)):
            raise ApprovalError('operation_identity_invalid')
        self.repo, self.kind, self.provider = repo, kind, provider
        self.account_id, self.channel, self.clock = account_id, channel, clock
        self.registry = CampaignRegistry(repo)

    def _preview(self, ident):
        if not isinstance(ident, str) or not ident:
            raise ApprovalError('operation_preview_required')
        preview = self.repo.get_state(self.kind + '_preview', ident)
        if (not preview or not isinstance(preview.get('plan'), dict)
                or type(preview.get('version')) is not int):
            raise ApprovalError('operation_preview_required')
        expiry = preview.get('expires_at')
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= self.clock():
            raise ApprovalError('operation_preview_expired')
        existing_hash = (preview['plan'].get('plan_hash') if self.kind == 'hubspot'
                         else preview.get('content_hash' if self.kind == 'seqd' else 'review_hash'))
        if not isinstance(existing_hash, str) or not existing_hash:
            raise ApprovalError('operation_preview_hash_required')
        fingerprint = payload_hash({'kind': self.kind, 'id': ident,
                                    'plan': preview['plan'], 'existing_hash': existing_hash})
        return preview, fingerprint

    def _request(self, preview, fingerprint, campaign_id, original_version):
        context = self.registry.load(campaign_id)
        binding = self.registry.binding(campaign_id)
        if (context['platform'] != 'thunder_os' or binding['providers'].get(self.channel) != self.provider
                or binding['provider_accounts'].get(self.channel) != self.account_id):
            raise ApprovalError('operation_campaign_identity_mismatch')
        control = self.repo.get_state('control', 'global') or {}
        if control.get('paused'):
            raise ApprovalError('global_pause')
        return execution_request(context, self.kind + ':' + preview['id'], original_version,
                                 {preview['id']: fingerprint}, control.get('version', 0))

    def review(self, preview_id, campaign_id):
        with self.repo.transaction():
            preview, fingerprint = self._preview(preview_id)
            if preview.get('state') != 'review_required':
                raise ApprovalError('operation_preview_not_reviewable')
            return self._request(preview, fingerprint, campaign_id, preview['version'])

    def approve(self, body, actor):
        if (not isinstance(body, dict)
                or not {'preview_id', 'expected_version', 'execution_confirmation'} <= set(body)
                or type(body.get('expected_version')) is not int
                or not isinstance(body.get('execution_confirmation'), dict)
                or not isinstance(body['execution_confirmation'].get('context'), dict)):
            raise ApprovalError('campaign_confirmation_required')
        if actor != 'ygor':
            raise ApprovalError('campaign_confirmation_required')
        with self.repo.transaction():
            preview, fingerprint = self._preview(body['preview_id'])
            if preview['version'] != body['expected_version'] or preview.get('state') != 'review_required':
                raise ApprovalError('operation_preview_changed')
            campaign_id = body['execution_confirmation']['context'].get('campaign_id')
            request = self._request(preview, fingerprint, campaign_id, preview['version'])
            confirmation = confirm_execution(body['execution_confirmation'], request, actor,
                datetime.fromtimestamp(preview['expires_at'], timezone.utc),
                datetime.fromtimestamp(self.clock(), timezone.utc))
            key = payload_hash({'kind': self.kind, 'request': request})
            existing = self.repo.get_state('operation_approval', key)
            if existing is not None:
                self.validate(existing)
                return existing
            return self.repo.append_state('operation_approval', key, {
                'kind': self.kind, 'preview_id': preview['id'], 'preview_version': preview['version'],
                'preview_fingerprint': fingerprint, 'preview_expires_at': preview['expires_at'],
                'provider': self.provider, 'account_id': self.account_id, 'channel': self.channel,
                'execution_confirmation': confirmation})

    def validate(self, record):
        with self.repo.transaction():
            if not isinstance(record, Mapping) or not isinstance(record.get('id'), str):
                raise ApprovalError('operation_approval_required')
            persisted = self.repo.get_state('operation_approval', record['id'])
            if not persisted or persisted != record or record.get('kind') != self.kind:
                raise ApprovalError('operation_approval_changed')
            if (record.get('provider'), record.get('account_id'), record.get('channel')) != (self.provider, self.account_id, self.channel):
                raise ApprovalError('operation_campaign_identity_mismatch')
            preview, fingerprint = self._preview(record['preview_id'])
            # Known lifecycle transitions must not silently permit arbitrary edits.
            offset = {'review_required': 0, 'approved': 1, 'attempted': 1, 'processed': 2}.get(preview.get('state'))
            if (offset is None or preview['version'] != record['preview_version'] + offset
                    or fingerprint != record['preview_fingerprint']
                    or preview['expires_at'] != record['preview_expires_at']):
                raise ApprovalError('operation_preview_changed')
            confirmation = record['execution_confirmation']
            campaign_id = confirmation['request']['context']['campaign_id']
            expected = self._request(preview, fingerprint, campaign_id, record['preview_version'])
            if record['id'] != payload_hash({'kind': self.kind, 'request': expected}):
                raise ApprovalError('operation_approval_changed')
            validate_execution(confirmation, expected, datetime.fromtimestamp(self.clock(), timezone.utc))
            return expected

    @contextmanager
    def execution_guard(self, record, attempt_kind, attempt_id):
        # An outer transaction would make the pre-send intent non-durable.
        if getattr(self.repo._local, 'connection', None) is not None or self.repo.dispatch_fence_active:
            raise ApprovalError('operation_requires_committed_attempt')
        if attempt_kind != self.kind + '_attempt' or not isinstance(attempt_id, str) or not attempt_id:
            raise ApprovalError('operation_attempt_invalid')
        attempt = self.repo.get_state(attempt_kind, attempt_id)
        if not attempt:
            raise ApprovalError('operation_durable_attempt_required')
        # Snapshot is checked again after waiting for the write lock.
        with self.repo.transaction():
            if self.repo.get_state(attempt_kind, attempt_id) != attempt:
                raise ApprovalError('operation_attempt_changed')
            self.validate(record)
            preview, _ = self._preview(record['preview_id'])
            if self.kind == 'hubspot':
                associated = attempt.get('plan') == preview['plan']
            else:
                associated = attempt.get('preview_id') == record['preview_id']
            if not associated:
                raise ApprovalError('operation_attempt_scope_changed')
            # Shared registry mutation guard; deliberately not an effect-job owner.
            self.repo._local.dispatch_fence = ('operation:' + attempt_kind + ':' + attempt_id, record['id'])
            try:
                yield
            finally:
                self.repo._local.dispatch_fence = None
