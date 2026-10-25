"""Read existing preparation inputs without enrolling or changing a provider."""
from collections.abc import Mapping
from copy import copy, deepcopy
from datetime import datetime, timezone
import re

from .campaign_registry import CampaignRegistry
from .today_readonly import ReadonlyRepository


class CadencePreparationReadonly:
    def __init__(self, service, *, journeys=None, accounts=None, instantly=None):
        self.repo = ReadonlyRepository(service.repository.path)
        self.service = copy(service)
        self.service.repository = self.repo
        self.registry = CampaignRegistry(self.repo)
        self.journeys, self.accounts, self.instantly = journeys, accounts, instantly

    def snapshot(self, campaign):
        if not isinstance(campaign, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', campaign):
            raise ValueError('campaign_id_invalid')
        context = self.registry.load(campaign)
        platform = context['platform']
        kind, label = ({'apollo': ('apollo_prompts_v1', 'Versão dos prompts e instruções; e-mail final auditado após o envio.'),
                       'instantly': ('instantly_exact_copy_or_hold_dynamic', 'Texto original da sequência; conteúdo dinâmico permanece retido sem contrato comprovado.')}
                      .get(platform, ('thunder_exact_touch' if platform == 'codex_local' else 'registered_external_provider',
                                      'Texto exato por toque; aprovação e executor seguem a campanha registrada.')))
        result = {'id': campaign, 'platform': platform, 'read_only': True,
                  'enrollment_enabled': False, 'execution_authorized': False, 'external_writes': 0, 'credits': 0,
                  'observed_at': datetime.now(timezone.utc).isoformat(),
                  'approval_contract': {'kind': kind, 'label': label},
                  'drafts': [], 'sequence': None, 'accounts': [], 'limitations': []}
        if platform == 'apollo':
            if self.journeys is None:
                result['limitations'].append('journey_reader_unavailable')
            else:
                drafts = [d for d in self.repo.list_state('journey_draft') if d.get('campaign_id') == campaign]
                for draft in sorted(drafts, key=lambda d: d.get('updated_at', ''), reverse=True)[:20]:
                    try:
                        view = self.journeys.get(draft['id'], 'ygor')
                        result['drafts'].append({key: deepcopy(view.get(key)) for key in
                            ('id', 'name', 'approval_mode', 'steps', 'counts', 'can_export', 'blockers',
                             'apollo_text_parity', 'review_hash', 'version')})
                        result['drafts'][-1]['review_url'] = '#jornada/nova/' + draft['id'] + '/4'
                    except Exception:
                        result['limitations'].append('journey_draft_unavailable')
                if len(drafts) > 20:
                    result['limitations'].append('draft_listing_bounded')
                if not drafts:
                    result['limitations'].append('no_existing_apollo_draft')
        elif platform == 'instantly':
            # The existing reader has exactly one verified external campaign.
            from .instantly_reader import CAMPAIGN
            if campaign != CAMPAIGN or self.instantly is None:
                result['limitations'].append('provider_campaign_binding_unavailable')
            else:
                try:
                    sequence = self.instantly.snapshot()
                    if not isinstance(sequence, Mapping) or sequence.get('read_only') is not True:
                        raise ValueError('sequence_read_contract_invalid')
                    result['sequence'] = deepcopy(sequence)
                    if sequence.get('complete') is not True:
                        result['limitations'].append('provider_sequence_incomplete')
                except Exception:
                    result['limitations'].append('provider_sequence_unavailable')
        if self.accounts is not None:
            try:
                records = self.accounts.list_accounts(campaign=campaign)
                for account in records.get('accounts', [])[:100]:
                    # Explicit campaign binding only: never assign visitor drafts by name.
                    if account.get('campaign_id') != campaign:
                        continue
                    detail = self.accounts.account(account['id'], campaign=campaign)
                    if detail.get('source') != account.get('source'):
                        result['limitations'].append('account_source_changed')
                        continue
                    result['accounts'].append({'id': detail['id'], 'name': detail.get('name'),
                        'company': detail.get('company'), 'source': detail.get('source'),
                        'touches': deepcopy(detail.get('tabs', {}).get('cadence', [])),
                        'limitations': deepcopy(detail.get('limitations', []))})
                if len(records.get('accounts', [])) > 100:
                    result['limitations'].append('account_listing_bounded')
            except Exception:
                result['limitations'].append('account_reader_unavailable')
        elif platform == 'codex_local':
            result['limitations'].append('account_reader_unavailable')
        if platform not in ('apollo', 'instantly') and not result['accounts']:
            result['limitations'].append('no_campaign_bound_cadence_source')
        result['limitations'] = sorted(set(result['limitations']))
        return result
