"""Read-only campaign metrics and evidence-bounded human-review proposals."""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import sqlite3

from .cadence_readonly import CadenceReadonlyError
from .learning import LearningEngine


METRICS = ('contacts', 'tasks', 'completed_with_receipt', 'completed_status',
           'pending', 'overdue', 'replies', 'meetings', 'unknown_status',
           'uncertain', 'blocked', 'completed_without_receipt', 'external_sent')


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _rows(value):
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _stamp(value):
    if not _text(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def _person(row):
    """Only confirmed IDs join people; names and email strings are not aliases."""
    if row.get('identity_confirmed') is not True:
        return None
    contact = row.get('hubspot_contact_id') or row.get('contact_id')
    if _text(contact):
        return 'contact:' + contact
    key = row.get('person_key')
    if _text(key):
        return 'contact:' + key[8:] if key.startswith('hubspot:') else 'person:' + key
    return None


def _source_id(row):
    source = _mapping(row.get('source'))
    values = [source.get(key) for key in ('provider', 'account_id', 'provider_message_id')]
    if all(_text(value) for value in values):
        # Tuple repr is unambiguous even when a provider ID contains a colon.
        return repr(tuple(values))
    ident = row.get('source_id') or row.get('id')
    return ident if _text(ident) else None


def _path(row, fallback=None):
    platform = row.get('cadence_path') or row.get('platform')
    if platform is None:
        provider = _mapping(row.get('source')).get('provider')
        platform = provider if provider in ('apollo', 'instantly') else fallback
    return {'apollo': 'apollo', 'instantly': 'instantly',
            'codex_local': 'thunder', 'thunder_os': 'thunder', 'thunder': 'thunder'}.get(platform) if _text(platform) else None


def _receipt_key(receipt):
    return repr((receipt.get('provider'), receipt.get('account_id'), receipt.get('message_id')))


def _management(row, snapshot, replies, *, path=None, batch=None, scoped=False):
    """Bounded observation, never a second CRM or an inferred provider result.

    Existing coverage.sources entries may certify an exhaustive source read.
    Task/schema completeness alone never certifies delivery/reply/outcome coverage.
    Unsupported source kinds remain explicitly unavailable.
    """
    campaign = row['id']
    registry = _mapping(row.get('registry'))
    fallback = registry.get('platform') or row.get('platform')
    coverage = deepcopy(_mapping(snapshot.get('coverage')))
    sources = _mapping(coverage.get('sources'))
    partial = (coverage.get('partial') is True or coverage.get('complete') is False
               or bool(snapshot.get('errors'))
               or any(_mapping(info).get('complete') is False for info in sources.values()))
    observed_at = snapshot.get('collected_at') if _stamp(snapshot.get('collected_at')) else None
    period = _mapping(row.get('period'))
    start, end = _stamp(period.get('start')), _stamp(period.get('end'))
    valid_period = start is not None and end is not None and start < end
    period = {'start': period['start'], 'end': period['end']} if valid_period else {'start': None, 'end': None}
    excluded, issue_sources = set(), set()

    def included(record, at):
        if scoped and (_path(record, fallback) != path or record.get('batch_id') != batch):
            return False
        if not valid_period:
            return True
        stamp = _stamp(at)
        if stamp is None:
            excluded.add('evidence_time_unavailable')
            issue_sources.add(_source_id(record) or 'reader.snapshot.coverage')
            return False
        return start <= stamp < end

    receipt_people, receipt_sources = {}, {}
    tasks = _rows(row.get('tasks'))
    bindings = {}
    for task in tasks:
        receipt = _mapping(task.get('receipt'))
        if all(_text(receipt.get(key)) for key in ('provider', 'message_id')):
            bindings.setdefault(_receipt_key(receipt), set()).add((
                str(receipt.get('contact_id')), _path(task, fallback),
                str(task.get('batch_id') or receipt.get('batch'))))
    disputed = {key for key, values in bindings.items() if len(values) > 1}
    observed_disputed = set()
    uncertain = []
    for task in tasks:
        receipt = _mapping(task.get('receipt'))
        if not receipt and task.get('outcome') not in ('uncertain_no_retry', 'unverified'):
            continue
        enriched = dict(task, batch_id=task.get('batch_id') or receipt.get('batch'))
        if not included(enriched, receipt.get('confirmed_at')):
            continue
        if task.get('outcome') in ('uncertain_no_retry', 'unverified'):
            if _text(task.get('id')):
                uncertain.append(task['id'])
        if not receipt:
            continue
        contact = receipt.get('contact_id')
        provider, message = receipt.get('provider'), receipt.get('message_id')
        if (task.get('link_status') != 'linked' or not _text(contact)
                or task.get('contact_ids') != [contact]
                or task.get('campaign', campaign) != campaign
                or receipt.get('campaign_id', campaign) != campaign
                or task.get('outcome') != 'sent_with_receipt'
                or receipt.get('verification') != 'recorded_confirmation'
                or not all(_text(value) for value in (provider, message))):
            excluded.add('receipt_identity_or_binding_unverified')
            issue_sources.add(_source_id(task) or 'reader.snapshot.tasks.receipt')
            continue
        key = _receipt_key(receipt)
        if key in disputed:
            excluded.add('receipt_identity_or_scope_conflict')
            issue_sources.add(key)
            observed_disputed.add(key)
            continue
        receipt_people.setdefault(key, set()).add(contact)
        receipt_sources[key] = receipt.get('source') if _text(receipt.get('source')) else 'reader.snapshot.tasks.receipt'
    conflicted_receipts = any(len(people) != 1 for people in receipt_people.values())
    if conflicted_receipts or observed_disputed:
        excluded.add('receipt_identity_conflict')
        issue_sources.update(key for key, people in receipt_people.items() if len(people) != 1)
    reached = {person for people in receipt_people.values() if len(people) == 1 for person in people}
    reached_ids = sorted(key for key, people in receipt_people.items() if len(people) == 1)

    reply_sources, awaiting, unresolved = {}, set(), set()
    for reply in replies:
        if reply.get('campaign_id') != campaign or not included(reply, reply.get('received_at')):
            continue
        if reply.get('human_reply') is False:
            continue
        ident = _source_id(reply)
        person = _person(reply)
        if not person or reply.get('human_reply') is not True or ident is None:
            if ident:
                unresolved.add(ident)
            continue
        entry = reply_sources.setdefault(ident, {'people': set(), 'classifications': set()})
        entry['people'].add(person)
        classification = reply.get('classification')
        entry['classifications'].add(classification if _text(classification) else None)
    responded, positives, responded_ids, positive_ids = set(), set(), [], []
    for ident, entry in reply_sources.items():
        if len(entry['people']) != 1:
            unresolved.add(ident)
            continue
        responded.update(entry['people'])
        responded_ids.append(ident)
        if entry['classifications'] == {'positive'}:
            positives.update(entry['people'])
            positive_ids.append(ident)
        if (len(entry['classifications']) != 1 or not entry['classifications'] <= {
                'positive', 'objection', 'negative', 'opt_out', 'risk', 'unrelated'}):
            awaiting.add(ident)

    def metric(count, ids, source, coverage_key, unavailable, *, invalid=False):
        info = _mapping(sources.get(coverage_key))
        # Certification must name its source, population and period. A generic
        # complete:true on the task reader cannot turn an absent outcome into 0.
        complete = (info.get('complete') is True and _text(info.get('source'))
                    and info.get('campaign_id') == campaign
                    and info.get('period') == period and valid_period
                    and _text(info.get('population')) and observed_at is not None
                    and not partial and not invalid and not excluded
                    and (not scoped or (info.get('cadence_path') == path and info.get('batch_id') == batch)))
        complete = bool(complete)
        if complete:
            source = [*source, info['source']]
        value = count if complete or (ids and not partial) else None
        reason = (None if complete else 'source_read_incomplete' if partial else
                  'evidence_conflict_or_unverified' if invalid or excluded else
                  'observed_evidence_only' if ids else unavailable)
        return {'value': value, 'observed_value': count if ids or complete else None,
                'source': sorted(set(source)), 'source_ids': sorted(ids),
                'complete': complete, 'reason': reason}

    metrics = {
        'reached': metric(len(reached), reached_ids, list(receipt_sources.values()), 'receipts',
                          'receipt_coverage_unavailable', invalid=conflicted_receipts or bool(uncertain)),
        'responded': metric(len(responded), responded_ids, ['repository.reply'], 'replies',
                            'human_reply_coverage_unavailable', invalid=bool(unresolved)),
        'positive': metric(len(positives), positive_ids, ['repository.reply'], 'replies',
                           'classified_reply_coverage_unavailable', invalid=bool(unresolved or awaiting)),
    }
    for name, people in (('reached', reached), ('responded', responded), ('positive', positives)):
        metrics[name]['person_ids'] = sorted(people)
    # These are missing reader contracts, not an observed empty result set.
    # Timeline meetings are contact context; follow-up deals only cover open
    # records due today. Neither provides historical campaign attribution.
    # A CRM field/enum existing does not establish its mapping to this registry.
    outcome_contracts = {
        'meetings_scheduled': (
            'meeting_campaign_attribution_unavailable',
            ['historical_meeting_campaign_binding', 'confirmed_scheduled_time_semantics',
             'meeting_read_population_period_coverage'],
            'Confirmar o vínculo histórico da reunião à campanha e a data que define reunião marcada.'),
        'meetings_held': (
            'meeting_campaign_attribution_unavailable',
            ['historical_meeting_campaign_binding', 'confirmed_meeting_outcome_semantics',
             'meeting_read_population_period_coverage'],
            'Confirmar o vínculo histórico da reunião à campanha e qual estado comprova sua realização.'),
        'opportunities': (
            'opportunity_definition_and_campaign_attribution_unverified',
            ['canonical_opportunity_pipeline_stages', 'deal_campaign_registry_mapping',
             'deal_read_population_period_coverage'],
            'Definir os pipelines/estágios que contam como oportunidade e validar o vínculo do negócio à campanha.'),
        'won': (
            'won_outcome_and_campaign_attribution_unverified',
            ['confirmed_closed_won_semantics', 'deal_campaign_registry_mapping',
             'confirmed_won_time_semantics', 'deal_read_population_period_coverage'],
            'Validar o estado de ganho, a data de fechamento e o vínculo do negócio à campanha; fechado não significa ganho.'),
        'cost_per_hot': (
            'cost_allocation_and_hot_population_unverified',
            ['attributed_cost_source_currency_and_period', 'cost_allocation_rule',
             'canonical_hot_population_with_campaign_and_time'],
            'Definir fonte/moeda e alocação dos custos e a população Hot por campanha e período; o rótulo Hot isolado não define a base.'),
    }
    for name, (reason, missing, decision) in outcome_contracts.items():
        metrics[name] = {'value': None, 'observed_value': None, 'source': [], 'source_ids': [],
                         'complete': False, 'availability': 'not_connected', 'reason': reason,
                         'missing_evidence': missing, 'required_decision': decision}

    insights = []

    def insight(code, message, ids, action):
        insights.append({'code': code, 'message': message, 'source_ids': sorted(ids),
                         'action': action, 'requires_human_approval': True, 'read_only': True})

    if not all(value['complete'] for value in metrics.values()):
        insight('coverage_incomplete', 'Cobertura insuficiente para um placar comercial completo.',
                ['reader.snapshot.coverage', 'repository.reply'],
                'Conferir as fontes, o período e a população antes de comparar resultados.')
    if uncertain or excluded:
        insight('uncertain_evidence', 'Há resultados ou vínculos sem confirmação.', set(uncertain) | issue_sources,
                'Reconciliar os recibos e as identidades manualmente antes de concluir o resultado.')
    if unresolved:
        insight('reply_identity_or_human_unverified', 'Há respostas com identidade ou natureza humana pendente.',
                unresolved, 'Conferir identidade e conversa original antes de classificar.')
    if awaiting:
        insight('replies_awaiting_review', 'Há respostas humanas aguardando revisão de classificação.',
                awaiting, 'Revisar a conversa e classificar a resposta; nenhuma mensagem será enviada.')
    return {'metrics': metrics, 'period': period, 'period_reason': None if valid_period else 'period_unavailable',
            'observed_at': observed_at, 'source': ['reader.snapshot', 'repository.reply'],
            'source_observed_at': {'reader.snapshot': observed_at, 'repository.reply': None},
            'observed_at_reason': 'local_reply_snapshot_time_unavailable',
            'coverage': {**coverage, 'complete': all(value['complete'] for value in metrics.values()),
                         'partial': not all(value['complete'] for value in metrics.values()),
                         'source_complete': coverage.get('complete'),
                         'scope': 'recorded_receipts_and_local_replies',
                         'limitations': sorted(set(coverage.get('limitations') or []) | excluded)},
            'insights': insights, 'requires_human_approval': True, 'read_only': True}


class CampaignScoreboard:
    def __init__(self, reader, repo, *, minimum_sample=30, minimum_lift=0.2, hunter=None):
        if type(minimum_sample) is not int or minimum_sample < 1:
            raise ValueError('invalid_minimum_sample')
        if (type(minimum_lift) not in (int, float)
                or not 0 <= minimum_lift <= 0.5):
            raise ValueError('invalid_minimum_lift')
        self.reader, self.repo = reader, repo
        self.hunter = hunter
        self.engine = LearningEngine(minimum_sample, minimum_lift)

    def snapshot(self, *, hunter_cursor=None):
        if hunter_cursor is not None:
            if self.hunter is None:
                raise ValueError('hunter_unavailable')
            return {'read_only':True,'hunter':self.hunter.continue_snapshot(hunter_cursor)}
        try:
            snapshot = self.reader.snapshot()
        except CadenceReadonlyError as exc:
            snapshot = {'coverage': {'complete': False, 'partial': True,
                        'sources': {'reader.snapshot': {'complete': False, 'error': exc.code,
                                                       'http_status': exc.status}},
                        'limitations': ['reader_snapshot_unavailable']}}
        try:
            replies = _rows(self.repo.list_state('reply'))
        except (sqlite3.Error, OSError):
            replies = []
            snapshot = deepcopy(snapshot)
            snapshot['coverage'] = dict(_mapping(snapshot.get('coverage')), complete=False, partial=True)
            snapshot['coverage'].setdefault('limitations', []).append('local_reply_read_unavailable')
        campaigns = []
        rows = list(snapshot.get('campaigns', []))
        known = {row.get('id') for row in rows}
        rows += [{'id': ident} for ident in sorted({r['campaign_id'] for r in replies
                  if _text(r.get('campaign_id'))} - known)]
        for row in rows:
            campaign = row.get('id')
            if not _text(campaign):
                raise ValueError('invalid_campaign_id')
            # Reader owns completeness and observed_* scope; never replace nulls.
            metrics = dict.fromkeys(METRICS)
            metrics.update(deepcopy(row.get('metrics') or {}))
            management = _management(row, snapshot, replies)
            result = {'id': campaign, 'metrics': metrics, 'management': management,
                      'learning': self.analyze(replies, campaign_id=campaign)}
            platform = row.get('platform') or _mapping(row.get('registry')).get('platform')
            if _text(platform):
                result['platform'] = platform
            path = _path(row, platform)
            if path is not None:
                result['cadence_path'] = path
            # Paths/batches are partitioned only by explicit IDs or registry
            # platform; provider names such as Gmail never imply a sales path.
            groups = set()
            for record in _rows(row.get('tasks')) + [r for r in replies if r.get('campaign_id') == campaign]:
                batch = record.get('batch_id') or _mapping(record.get('receipt')).get('batch')
                groups.add((_path(record, platform), batch if _text(batch) else None))
            if not groups:
                groups.add((path, None))
            result['paths'] = [{'cadence_path': p, 'batch_id': b,
                                'management': _management(row, snapshot, replies, path=p, batch=b, scoped=True)}
                               for p, b in sorted(groups, key=lambda pair: (pair[0] or '', pair[1] or ''))]
            campaigns.append(result)
        comparisons = []
        for row, campaign in zip(rows, campaigns):
            for path in campaign['paths']:
                population = _mapping(row.get('population'))
                population = {key: deepcopy(population[key]) for key in ('id', 'size', 'definition')
                              if key in population} or None
                if len(campaign['paths']) > 1 or path['batch_id'] is not None:
                    population = None
                missing = [key for key in ('offer', 'segment') if not _text(row.get(key))]
                if path['management']['period']['start'] is None:
                    missing.append('period')
                if population is None:
                    missing.append('population')
                comparisons.append({'campaign_id': campaign['id'], 'cadence_path': path['cadence_path'],
                    'batch_id': path['batch_id'], 'offer': row.get('offer') if _text(row.get('offer')) else None,
                    'segment': row.get('segment') if _text(row.get('segment')) else None,
                    'period': path['management']['period'],
                    'population': population,
                    'metadata_reasons': {key: key + '_unavailable' for key in missing},
                    'comparable': False, 'winner': None, 'ranking': None,
                    'reason': 'cohort_comparability_unverified',
                    'source': path['management']['source'], 'requires_human_approval': True, 'read_only': True})
        hunter = self.hunter.snapshot(snapshot, replies) if self.hunter is not None else None
        return {**({'hunter': hunter} if hunter is not None else {}), 'campaigns': campaigns, 'comparisons': comparisons, 'read_only': True,
                'requires_human_approval': True}

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
