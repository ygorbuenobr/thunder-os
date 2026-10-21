"""Read-only campaign metrics and evidence-bounded human-review proposals."""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict

from .learning import LearningEngine


METRICS = ('contacts', 'tasks', 'completed_with_receipt', 'completed_status',
           'pending', 'overdue', 'replies', 'meetings', 'unknown_status',
           'uncertain', 'blocked', 'completed_without_receipt', 'external_sent')


def _text(value):
    return isinstance(value, str) and bool(value.strip())


class CampaignScoreboard:
    def __init__(self, reader, repo, *, minimum_sample=30, minimum_lift=0.2):
        if type(minimum_sample) is not int or minimum_sample < 1:
            raise ValueError('invalid_minimum_sample')
        if (type(minimum_lift) not in (int, float)
                or not 0 <= minimum_lift <= 0.5):
            raise ValueError('invalid_minimum_lift')
        self.reader, self.repo = reader, repo
        self.engine = LearningEngine(minimum_sample, minimum_lift)

    def snapshot(self):
        snapshot = self.reader.snapshot()
        replies = self.repo.list_state('reply')
        campaigns = []
        for row in snapshot.get('campaigns', []):
            campaign = row.get('id')
            if not _text(campaign):
                raise ValueError('invalid_campaign_id')
            # Reader owns completeness and observed_* scope; never replace nulls.
            metrics = dict.fromkeys(METRICS)
            metrics.update(deepcopy(row.get('metrics') or {}))
            campaigns.append({'id': campaign, 'metrics': metrics,
                              'learning': self.analyze(replies, campaign_id=campaign)})
        return {'campaigns': campaigns, 'read_only': True}

    def analyze(self, records, *, campaign_id=None):
        records = [row for row in records if isinstance(row, Mapping)]
        if campaign_id is None:
            campaigns = {row['campaign_id'] for row in records if _text(row.get('campaign_id'))}
            if len(campaigns) > 1:
                raise ValueError('campaign_id_required')
            campaign_id = next(iter(campaigns), None)
        if campaign_id is not None and not _text(campaign_id):
            raise ValueError('invalid_campaign_id')
        sources = {}
        for row in records:
            if (campaign_id is None or row.get('campaign_id') != campaign_id
                    or row.get('identity_confirmed') is not True
                    or row.get('human_reply') is not True
                    or row.get('classification') not in ('positive', 'negative')):
                continue
            ident = row.get('source_id') or row.get('id')
            if not _text(ident):
                continue
            source = row.get('source')
            key = ('id', ident)
            if isinstance(source, Mapping):
                values = tuple(source.get(k) for k in ('provider', 'account_id', 'provider_message_id'))
                if all(_text(value) for value in values):
                    key = ('provider', *values)
            entry = sources.setdefault(key, {'ids': set(), 'outcomes': set()})
            entry['ids'].add(ident)
            entry['outcomes'].add(row['classification'])
        # Conflicting classifications of one source cannot become two votes.
        evidence = [{'source_id': min(entry['ids']), 'outcome': next(iter(entry['outcomes']))}
                    for entry in sources.values() if len(entry['outcomes']) == 1]
        result = self.engine.analyze(
            evidence, hypothesis='Avaliar associacao das respostas classificadas da campanha',
            population=f'respostas humanas classificadas da campanha {campaign_id}',
            owner='ygor', review_at='revisao humana pendente')
        sample = len(evidence)
        positive = sum(row['outcome'] == 'positive' for row in evidence)
        learning = {
            'status': 'proposal_pending_human_approval' if result.recommendation else 'observation_only',
            'observation': {**result.observation,
                            'metric': 'positive_rate_among_classified_human_replies',
                            'positive_rate': positive / sample if sample else None,
                            'comparison_scope': 'existing_engine_criterion_not_market_benchmark; association_only'},
            'source_ids': sorted(row['source_id'] for row in evidence),
            'requires_human_approval': True,
        }
        if result.recommendation is not None:
            learning['proposal'] = asdict(result.recommendation)
        return learning
