"""Validate persisted human approval of the exact Apollo campaign revision."""
from datetime import datetime, timezone

from .approvals import (ApprovalError, ApprovalService, CampaignVersionApprovalPack,
                        execution_request, validate_execution)
from .campaign_registry import CampaignRegistry
from .models import payload_hash


class CampaignApprovalAuthority:
    def __init__(self, repository):
        self.repository = repository
        self.registry = CampaignRegistry(repository)

    def require(self, campaign_id, approval_id, expected_campaign_version):
        """Fail closed when approval, snapshot, control or current revision differs.

        A historical snapshot alone is insufficient evidence of an enrollment
        started under that version; historical execution stays blocked.
        """
        try:
            with self.repository.transaction():
                approval = self.repository.get_state('approval', approval_id)
                if (not isinstance(approval, dict) or approval.get('version') != 1
                        or approval.get('scope') != 'campaign_version'
                        or approval.get('campaign_id') != campaign_id
                        or approval.get('campaign_version') != expected_campaign_version
                        or approval.get('actor') != 'ygor' or approval.get('status') != 'active'):
                    raise ApprovalError('campaign_approval_missing_or_invalid')
                digest = approval.get('scope_hash')
                if not isinstance(digest, str) or approval.get('snapshot_id') != 'apollo-campaign-' + digest:
                    raise ApprovalError('campaign_snapshot_invalid')
                snapshot = self.repository.get_state('campaign_version_snapshot', approval['snapshot_id'])
                if not isinstance(snapshot, dict) or snapshot.get('version') != 1:
                    raise ApprovalError('campaign_snapshot_invalid')
                scope = snapshot.get('scope')
                if (not isinstance(scope, dict) or set(scope) != {'context', 'configuration', 'campaign_config'}
                        or payload_hash(scope) != digest or snapshot.get('scope_hash') != digest):
                    raise ApprovalError('campaign_snapshot_changed')
                row, context = self.registry._read(campaign_id)
                config = row['config']['configuration']
                if (context.get('platform') != 'apollo' or context.get('campaign_version') != expected_campaign_version
                        or scope != {'context': context, 'configuration': config, 'campaign_config': row['config']}
                        or approval.get('campaign_revision') != row['version']
                        or not isinstance(config.get('apollo_linkedin'), dict)
                        or not isinstance(config.get('linkedin_policy'), dict)):
                    raise ApprovalError('campaign_version_or_binding_changed')
                control = self.repository.get_state('control', 'global')
                if not isinstance(control, dict) or control.get('paused') is not False:
                    raise ApprovalError('global_control_unverified')
                confirmation = approval.get('execution_confirmation')
                if (not isinstance(confirmation, dict) or approval.get('expires_at') != confirmation.get('expires_at')
                        or approval.get('approved_at') != confirmation.get('confirmed_at')):
                    raise ApprovalError('campaign_approval_invalid')
                expected = execution_request(context, campaign_id, row['version'],
                                             {'campaign_version': digest}, control['version'])
                now = datetime.now(timezone.utc)
                validate_execution(confirmation, expected, now, platform='apollo')
                pack = CampaignVersionApprovalPack(id=approval_id, cadence_id=campaign_id,
                    cadence_version=row['version'], actor=approval['actor'],
                    approved_at=datetime.fromisoformat(approval['approved_at']),
                    expires_at=datetime.fromisoformat(approval['expires_at']), step_hashes=(),
                    status=approval['status'], campaign_scope_hash=digest)
                ApprovalService(lambda: False).authorize_campaign_version(pack, digest, now)
                return {'context': context, 'configuration': config,
                        'binding': dict(config['apollo_linkedin']), 'approval_id': approval_id,
                        'scope_hash': digest}
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalError('campaign_approval_unavailable') from None
