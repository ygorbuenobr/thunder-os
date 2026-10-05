"""Explicit internal continuous operation. Never consumes legacy/effect queues.

Only Ygor's post-activation requests and canonical connection research qualify. Attempt records are
committed before inference; any interrupted attempt requires a NEW human request.
No automatic retry, LLM fallback, tools, approvals or external-action adapters.
"""
from __future__ import annotations
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import threading
from typing import Any
from uuid import uuid4

from .agent_consumer import atomic_json, enforce_external_gate, source_revision
from .agent_worker import AgentWorker
from .agents import AgentResult, AgentRunner
from .connection_backfill import DAILY_RESEARCH_LIMIT
from .connection_intake import PROVIDER_CALENDAR
from .connection_work import CONNECTION_ACTOR, WORK_LINK_KIND, canonical_connection_request
from .db import Repository, RepositoryConflict
from .hermes_text_adapter import LIMIT, bounded_process_transport, load_catalog_context
from .knowledge import KnowledgeStore
from .service import ThunderService
from .models import ContractError, WorkStatus
from .research import COVERAGE_STATUSES, SOURCE_AREAS
from .research_producer import PublicResearchProducer, ResearchCollectionError


def utcnow():
    return datetime.now(timezone.utc)


class InternalExecutionError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def bounded_json(path):
    with path.open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise InternalExecutionError('trusted_context_too_large')
    return json.loads(raw)


class TrustedEvidence:
    """Canonical repository facts, approved bundles, or fresh public research."""
    def __init__(self, service, brain, bundle_root, *, research_producer=None):
        self.service, self.brain, self.bundle_root = service, Path(brain).resolve(), Path(bundle_root).resolve()
        self.research_producer = research_producer or PublicResearchProducer()

    def __call__(self, job):
        domain = job.payload.get('account_id')
        if not isinstance(domain, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]', domain):
            raise InternalExecutionError('canonical_account_required')
        canonical = load_catalog_context(self.brain)
        if canonical['catalog_hash'] != job.payload['catalog_hash']:
            raise InternalExecutionError('catalog_changed')
        if job.type == 'research':
            try:
                packet = self.research_producer.collect_from_brain(str(self.brain), domain)
            except ResearchCollectionError as exc:
                raise InternalExecutionError(str(exc)) from None
            evidence = packet.get('research_evidence')
            coverage = packet.get('source_coverage')
            account = packet.get('account')
            if not isinstance(evidence, list) or not isinstance(coverage, list) or not isinstance(account, dict):
                raise InternalExecutionError('public_research_packet_invalid')
            for index, fact in enumerate(evidence, 1):
                if (not isinstance(fact, dict) or not isinstance(fact.get('statement'), str) or not fact['statement'].strip()
                        or not isinstance(fact.get('source'), str) or not re.fullmatch(r'https://\S+', fact['source'])
                        or not isinstance(fact.get('observed_at'), str)
                        or fact.get('source_area') not in SOURCE_AREAS):
                    raise InternalExecutionError('trusted_evidence_invalid')
                fact['fact_id'] = f'F{index}'
                date.fromisoformat(fact['observed_at'])
                if fact.get('published_at') is not None:
                    date.fromisoformat(fact['published_at'])
            seen_areas = set()
            for row in coverage:
                fields = {'area', 'status', 'query', 'checked_at', 'planned_urls', 'attempted_urls',
                          'discovered_urls', 'source_urls', 'details'}
                if (not isinstance(row, dict) or set(row) != fields or row.get('area') not in SOURCE_AREAS
                        or row['area'] in seen_areas or row.get('status') not in COVERAGE_STATUSES
                        or not isinstance(row.get('query'), str) or not row['query'].strip()
                        or not isinstance(row.get('details'), str) or not row['details'].strip()):
                    raise InternalExecutionError('trusted_source_coverage_invalid')
                seen_areas.add(row['area'])
                for key in ('planned_urls', 'attempted_urls', 'discovered_urls', 'source_urls'):
                    urls = row.get(key)
                    if (not isinstance(urls, list)
                            or any(not isinstance(url, str) or not re.fullmatch(r'https://\S+', url) for url in urls)
                            or len(urls) != len(set(urls))):
                        raise InternalExecutionError('trusted_source_coverage_invalid')
                checked_at = row.get('checked_at')
                if checked_at is not None:
                    date.fromisoformat(checked_at)
                area_facts = [fact for fact in evidence if fact['source_area'] == row['area']]
                row['fact_ids'] = [fact['fact_id'] for fact in area_facts]
                area_sources = {fact['source'] for fact in area_facts}
                if row['status'] == 'checked_with_evidence' and (
                        checked_at is None or not area_sources or set(row['source_urls']) != area_sources
                        or not area_sources <= set(row['attempted_urls'])):
                    raise InternalExecutionError('trusted_source_coverage_invalid')
                if row['status'] != 'checked_with_evidence' and (area_facts or row['source_urls']):
                    raise InternalExecutionError('trusted_source_coverage_invalid')
                if row['status'] in {'checked_no_result', 'not_accessible', 'attempted'} and (
                        checked_at is None or not row['attempted_urls']):
                    raise InternalExecutionError('trusted_source_coverage_invalid')
                if row['status'] == 'planned' and (checked_at is not None or row['attempted_urls'] or not row['planned_urls']):
                    raise InternalExecutionError('trusted_source_coverage_invalid')
                if row['status'] == 'not_provided' and (
                        checked_at is not None or row['planned_urls'] or row['attempted_urls']):
                    raise InternalExecutionError('trusted_source_coverage_invalid')
            if seen_areas != SOURCE_AREAS:
                raise InternalExecutionError('trusted_source_coverage_incomplete')
            account = {key: account[key] for key in ('name', 'domain', 'sector')}
            return {'account_id': domain, 'research_evidence': evidence, 'source_coverage': coverage,
                'research_account': account, 'research_producer': packet.get('producer', {}), 'angle_ids': []}
        bundle = {}
        path = self.bundle_root / (domain + '.json')
        if path.exists():
            if not path.resolve().is_relative_to(self.bundle_root) or path.is_symlink():
                raise InternalExecutionError('unsafe_evidence_path')
            bundle = bounded_json(path)
            if (not isinstance(bundle, dict) or bundle.get('status') != 'validado'
                    or bundle.get('account_id') != domain or bundle.get('catalog_hash') != canonical['catalog_hash']
                    or not isinstance(bundle.get('source_files'), dict) or not bundle['source_files']):
                raise InternalExecutionError('unapproved_evidence_bundle')
            for relative, expected in bundle['source_files'].items():
                source = (self.brain / relative).resolve()
                if not source.is_relative_to(self.brain) or not source.is_file():
                    raise InternalExecutionError('unsafe_evidence_source')
                with source.open('rb') as stream:
                    raw = stream.read(LIMIT + 1)
                if len(raw) > LIMIT or hashlib.sha256(raw).hexdigest() != expected:
                    raise InternalExecutionError('evidence_source_changed')
        research = self.service.repository.get_state('research', domain)
        facts = []
        if (research and research.get('catalog_hash') == canonical['catalog_hash']
                and research.get('status') == 'validated' and isinstance(research.get('facts'), list)):
            facts = research['facts']
        evidence = bundle.get('research_evidence') or facts
        if not isinstance(evidence, list) or (not evidence and job.type != 'research'):
            raise InternalExecutionError('trusted_evidence_missing')
        for fact in evidence:
            if (not isinstance(fact, dict) or not isinstance(fact.get('statement'), str) or not fact['statement'].strip()
                    or not isinstance(fact.get('source'), str) or not re.fullmatch(r'https?://\S+', fact['source'])
                    or not isinstance(fact.get('observed_at'), str)):
                raise InternalExecutionError('trusted_evidence_invalid')
            date.fromisoformat(fact['observed_at'])
            if 'published_at' in fact and fact['published_at'] is not None:
                date.fromisoformat(fact['published_at'])
            if 'source_area' in fact and fact['source_area'] not in SOURCE_AREAS:
                raise InternalExecutionError('trusted_evidence_invalid')
        if job.type == 'copy':
            self.service.current_research(domain)
            angle_ids = bundle.get('angle_ids', [])
            if not isinstance(angle_ids, list) or not angle_ids or any(not isinstance(v, str) or not v.strip() for v in angle_ids):
                raise InternalExecutionError('trusted_copy_angles_missing')
            style_rules = {}
            for relative in ('eu/estilo-e-regras.md', 'eu/aprendizados.md'):
                source = (self.brain / relative).resolve()
                if not source.is_relative_to(self.brain):
                    raise InternalExecutionError('unsafe_style_source')
                with source.open('rb') as stream:
                    raw = stream.read(LIMIT + 1)
                if not raw.strip() or len(raw) > LIMIT:
                    raise InternalExecutionError('trusted_style_missing')
                style_rules[relative] = raw.decode('utf-8')
            return {'account_id': domain, 'research_evidence': evidence, 'angle_ids': angle_ids,
                    'style_rules': style_rules}
        coverage = bundle.get('source_coverage', research.get('source_coverage', []) if research else [])
        if not isinstance(coverage, list):
            raise InternalExecutionError('trusted_source_coverage_invalid')
        seen_areas = set()
        for row in coverage:
            legacy_fields = {'area', 'status', 'query', 'checked_at', 'source_urls', 'details'}
            current_fields = {'area', 'status', 'query', 'checked_at', 'planned_urls', 'attempted_urls', 'discovered_urls',
                              'source_urls', 'fact_ids', 'details'}
            if (not isinstance(row, dict) or frozenset(row) not in {frozenset(legacy_fields), frozenset(current_fields)}
                    or row.get('area') not in SOURCE_AREAS or row['area'] in seen_areas
                    or row.get('status') not in COVERAGE_STATUSES):
                raise InternalExecutionError('trusted_source_coverage_invalid')
            if set(row) == legacy_fields:
                # Legacy package rows do not identify the request URL for a
                # negative result. Preserve positive source provenance only;
                # downgrade unsupported search claims to visible gaps.
                if row['status'] == 'checked_with_evidence':
                    row = {**row, 'planned_urls': list(row['source_urls']),
                        'attempted_urls': list(row['source_urls']),
                        'discovered_urls': [],
                        'fact_ids': [f'F{index + 1}' for index, fact in enumerate(evidence)
                                     if fact.get('source') in row['source_urls']],
                    }
                else:
                    row = {**row, 'status': 'not_provided', 'checked_at': None,
                        'planned_urls': [], 'attempted_urls': [], 'discovered_urls': [], 'source_urls': [], 'fact_ids': [],
                        'details': 'Legacy coverage had no request URL; prior search status was not independently verifiable.'}
            elif not isinstance(row.get('fact_ids'), list):
                row['fact_ids'] = []
            seen_areas.add(row['area'])
            if (not isinstance(row.get('query'), str) or not row['query'].strip()
                    or not isinstance(row.get('details'), str) or not row['details'].strip()
                    or any(not isinstance(row.get(field), list) for field in ('planned_urls', 'attempted_urls', 'discovered_urls', 'source_urls'))
                    or any(not isinstance(url, str) or not re.fullmatch(r'https://\S+', url)
                           for field in ('planned_urls', 'attempted_urls', 'discovered_urls', 'source_urls') for url in row[field])
                    or any(len(row[field]) != len(set(row[field]))
                           for field in ('planned_urls', 'attempted_urls', 'discovered_urls', 'source_urls'))):
                raise InternalExecutionError('trusted_source_coverage_invalid')
            checked_at = row.get('checked_at')
            if checked_at is not None:
                date.fromisoformat(checked_at)
            if row['status'] == 'not_provided' and (checked_at is not None or row['source_urls'] or row['planned_urls'] or row['attempted_urls']):
                raise InternalExecutionError('trusted_source_coverage_invalid')
            if row['status'] == 'checked_with_evidence' and (checked_at is None or not row['source_urls']):
                raise InternalExecutionError('trusted_source_coverage_invalid')
            if row['status'] in {'checked_no_result', 'not_accessible', 'attempted'} and (
                    checked_at is None or row['source_urls'] or not row.get('attempted_urls')):
                raise InternalExecutionError('trusted_source_coverage_invalid')
            if row['status'] == 'planned' and (checked_at is not None or row['attempted_urls'] or not row['planned_urls']):
                raise InternalExecutionError('trusted_source_coverage_invalid')
            area_sources = {fact['source'] for fact in evidence
                if fact.get('source_area', 'other_public_source') == row['area']}
            if row['status'] == 'checked_with_evidence' and set(row['source_urls']) != area_sources:
                raise InternalExecutionError('trusted_source_coverage_invalid')
            if row['status'] in {'checked_no_result', 'not_accessible'} and area_sources:
                raise InternalExecutionError('trusted_source_coverage_invalid')
        if seen_areas and seen_areas != SOURCE_AREAS:
            raise InternalExecutionError('trusted_source_coverage_incomplete')
        result = {'account_id': domain, 'research_evidence': evidence, 'source_coverage': coverage,
            'angle_ids': bundle.get('angle_ids', [])}
        if research and isinstance(research.get('account'), dict):
            result['research_account'] = research['account']
        if job.type == 'copy':
            self.service.current_research(domain)
            if not isinstance(result['angle_ids'], list) or not result['angle_ids'] or any(not isinstance(v, str) or not v.strip() for v in result['angle_ids']):
                raise InternalExecutionError('trusted_copy_angles_missing')
            result['style_rules'] = {}
            for relative in ('eu/estilo-e-regras.md', 'eu/aprendizados.md'):
                source = (self.brain / relative).resolve()
                if not source.is_relative_to(self.brain):
                    raise InternalExecutionError('unsafe_style_source')
                with source.open('rb') as stream:
                    raw = stream.read(LIMIT + 1)
                if not raw.strip() or len(raw) > LIMIT:
                    raise InternalExecutionError('trusted_style_missing')
                result['style_rules'][relative] = raw.decode('utf-8')
        return result


class EligibleRepository:
    """Delegate all persistence, overriding only the fenced claim selection."""
    def __init__(self, repository, owner):
        self.repository, self.owner = repository, owner

    def __getattr__(self, name):
        return getattr(self.repository, name)

    def claim_outbox(self, worker_id, now, lease_for, *, kind=None):
        if kind != 'agent_job' or self.owner.block_reason():
            return None
        with self.repository.transaction() as con:
            # Gate and cutoff must be re-read under the same write lock as claim.
            if self.owner.block_reason():
                return None
            cutoff = self.repository.get_state('agent_control', 'global')['activation_at']
            rows = con.execute("SELECT * FROM thunder_outbox WHERE kind='agent_job' AND state='pending' AND (lease_until IS NULL OR lease_until<=?) ORDER BY rowid", (now.isoformat(),)).fetchall()
            for row in rows:
                try:
                    connection_claim = False
                    original = json.loads(row['payload'])
                    created = datetime.fromisoformat(original['created_at'])
                    item = self.repository.get_work(row['entity_id'])
                    context = original.get('context', {})
                    if (created < datetime.fromisoformat(cutoff)
                            or item is None or original.get('id') != item.id
                            or original.get('actor') != item.actor
                            or original.get('logical_owner') != item.logical_owner
                            or context != item.context):
                        continue
                    if original['actor'] == CONNECTION_ACTOR:
                        if (original['logical_owner'] != CONNECTION_ACTOR
                                or context.get('capability') != 'research'):
                            continue
                        link = self.repository.get_state(WORK_LINK_KIND, item.id)
                        if (link is None
                                or link.get('connectsafely_account_id') != context.get('connectsafely_account_id')
                                or link.get('profile') != context.get('connection_profile')
                                or link.get('request_version') != context.get('connection_request_version')):
                            continue
                        request = canonical_connection_request(
                            self.repository, context.get('connectsafely_account_id'),
                            context.get('connection_profile'))
                        if (request['account_id'] != context.get('account_id')
                                or request['version'] != context.get('connection_request_version')):
                            continue
                        connection_claim = True
                    elif (original['actor'] != 'ygor' or original['logical_owner'] != 'ygor'
                          or context.get('capability') not in {'research', 'copy'}
                          or not context.get('account_id')):
                        continue
                except ContractError:
                    continue
                except (ValueError, KeyError, TypeError):
                    continue
                budget_day = None
                claimed_ids = None
                if (connection_claim and item.status in {WorkStatus.REQUESTED, WorkStatus.IN_PROGRESS}
                        and self.repository.get_state('agent_attempt', item.id) is None):
                    budget_day = now.astimezone(PROVIDER_CALENDAR).date().isoformat()
                    budget = self.repository.get_state('connection_research_execution_day', budget_day)
                    claimed_ids = budget.get('work_ids', []) if budget else []
                    if (not isinstance(claimed_ids, list)
                            or any(not isinstance(value, str) for value in claimed_ids)
                            or len(claimed_ids) != len(set(claimed_ids))
                            or len(claimed_ids) > DAILY_RESEARCH_LIMIT):
                        continue
                    if item.id not in claimed_ids and len(claimed_ids) >= DAILY_RESEARCH_LIMIT:
                        continue
                token, until = f'{worker_id}:{uuid4().hex}', (now + lease_for).isoformat()
                con.execute('UPDATE thunder_outbox SET lease_owner=?,lease_until=? WHERE id=?', (token, until, row['id']))
                if budget_day is not None and item.id not in claimed_ids:
                    self.repository.put_state('connection_research_execution_day', budget_day,
                                              {'work_ids': [*claimed_ids, item.id]})
                return {**dict(row), 'lease_owner': token, 'lease_until': until}
        return None


class GuardedRunner:
    timeout_seconds = 120
    max_attempts = 1

    def __init__(self, owner):
        self.owner = owner

    def run(self, job):
        owner, repo = self.owner, self.owner.repository
        if repo.get_state('agent_attempt', job.id):
            owner.usage(job.id, 'needs_human', 'interrupted_attempt', attempted=True)
            return AgentResult('needs_human', error_code='interrupted_attempt')
        reason = owner.job_block(job.id)
        if reason:
            owner.usage(job.id, 'needs_human', reason)
            return AgentResult('needs_human', error_code=reason)
        try:
            evidence = owner.evidence_loader(job)
        except Exception as exc:
            code = exc.code if isinstance(exc, InternalExecutionError) else 'trusted_evidence_invalid'
            owner.usage(job.id, 'needs_human', code)
            return AgentResult('needs_human', error_code=code)
        # Fail before marking/spending; recheck gates atomically with attempt claim.
        try:
            with repo.transaction():
                reason = owner.job_block(job.id)
                if reason:
                    return AgentResult('needs_human', error_code=reason)
                repo.append_state('agent_attempt', job.id, {'started_at': owner.clock().isoformat(),
                    'status': 'consumed', 'max_attempts': 1, 'timeout_seconds': 120, 'external_effects_enabled': False})
                owner.usage(job.id, 'running', None, attempted=True)
        except RepositoryConflict:
            return AgentResult('needs_human', error_code='interrupted_attempt')
        try:
            transport = owner.transport_factory(job, evidence, lambda: bool(owner.job_block(job.id)))
            result = AgentRunner(transport, timeout_seconds=120, max_attempts=1).run(job)
        except InternalExecutionError as exc:
            result = AgentResult('needs_human', error_code=exc.code)
        except InterruptedError:
            result = AgentResult('needs_human', error_code=owner.job_block(job.id) or 'execution_cancelled')
        except Exception:
            result = AgentResult('needs_human', error_code='agent_transport_failed')
        reason = owner.job_block(job.id)
        if reason:
            result = AgentResult('needs_human', error_code=reason)
        # Copy angle must come from the approved bundle, never invented by a model.
        if (result.status == 'succeeded' and job.type == 'copy'
                and result.output.get('angle_id') not in evidence.get('angle_ids', [])):
            result = AgentResult('needs_human', error_code='unapproved_copy_angle')
        owner.usage(job.id, result.status, result.error_code, attempted=True)
        return result


class GatedService(ThunderService):
    def record_job_result(self, item_id, expected_version, result, actor):
        if not result.get('dependency_failure') and self.repository.owner.job_block(item_id):
            raise ContractError('internal operation paused or cancelled')
        return super().record_job_result(item_id, expected_version, result, actor)


class ContinuousConsumer:
    def __init__(self, service, *, evidence_loader, transport_factory, clock=utcnow, stop=None):
        self.repository, self.service = service.repository, service
        self.evidence_loader, self.transport_factory, self.clock = evidence_loader, transport_factory, clock
        self.stop = stop or threading.Event()
        with self.repository.transaction():
            control = self.repository.get_state('agent_control', 'global')
            if not control:
                self.repository.put_state('agent_control', 'global', {'enabled': False, 'paused': True,
                    'activation_at': self.clock().isoformat(), 'circuit_reset_version': 0})
            elif not control.get('activation_at'):
                # Older control records must not authorize the existing backlog.
                self.repository.put_state('agent_control', 'global', {
                    **{k: v for k, v in control.items() if k not in {'id', 'version', 'updated_at'}},
                    'activation_at': self.clock().isoformat(), 'enabled': False, 'paused': True})
        self.worker_service = GatedService(EligibleRepository(self.repository, self),
            knowledge_store=service.knowledge_store, approved_catalog=service._approved_catalog)
        self.worker = AgentWorker(self.worker_service, runner=GuardedRunner(self), actor='hermes', clock=clock)

    def block_reason(self):
        enforce_external_gate()
        if self.stop.is_set():
            return 'consumer_stopping'
        control = self.repository.get_state('agent_control', 'global') or {}
        if control.get('enabled') is not True or control.get('paused') is not False or self.service.global_paused():
            return 'agent_paused'
        circuit = self.repository.get_state('agent_circuit', 'global') or {}
        if circuit.get('reset_version') != control.get('circuit_reset_version', 0):
            return None  # explicit Ygor reset generation, applied on next result
        if circuit.get('open'):
            return 'circuit_open'
        if circuit.get('blocked_until') and self.clock() < datetime.fromisoformat(circuit['blocked_until']):
            return 'rate_backoff'
        return None

    def job_block(self, item_id):
        reason = self.block_reason()
        if reason:
            return reason
        cancel = self.repository.get_state('agent_cancel', item_id)
        if cancel and cancel.get('actor') == 'ygor' and cancel.get('cancelled') is True:
            return 'execution_cancelled'
        return None

    def usage(self, item_id, status, error, *, attempted=False):
        previous = self.repository.get_state('agent_usage', item_id) or {}
        self.repository.put_state('agent_usage', item_id, {'status': status, 'error_code': error,
            'attempted': attempted or previous.get('attempted', False),
            'started_at': previous.get('started_at') or self.clock().isoformat(),
            'updated_at': self.clock().isoformat(), 'provider': 'openai-codex',
            'model': previous.get('model', 'existing_configured_model'), 'external_effects_enabled': False})

    def update_circuit(self, result):
        control = self.repository.get_state('agent_control', 'global') or {}
        reset = control.get('circuit_reset_version', 0)
        previous = self.repository.get_state('agent_circuit', 'global') or {}
        count = previous.get('failures', 0) if previous.get('reset_version') == reset else 0
        code = result.error_code
        if result.status == 'succeeded':
            state = {'failures': 0, 'open': False, 'blocked_until': None}
        elif code in {'execution_cancelled', 'agent_paused', 'consumer_stopping'}:
            return
        else:
            count += 1
            seconds = 300 if code == 'rate_limited' else min(3600, 60 * 2 ** min(count - 3, 5)) if count >= 3 else 0
            state = {'failures': count, 'open': code in {'auth_unauthorized', 'auth_forbidden', 'auth_expired', 'auth_unavailable', 'auth_readonly_violation', 'auth_config_invalid', 'auth_endpoint_blocked'},
                'blocked_until': (self.clock() + timedelta(seconds=seconds)).isoformat() if seconds else None}
        self.repository.put_state('agent_circuit', 'global', {**state, 'reset_version': reset, 'last_error': code})

    def run_once(self):
        reason = self.block_reason()
        if reason:
            return {'status': 'paused', 'reason': reason}
        result = self.worker.run_once()
        if result.get('item_id'):
            item = self.repository.get_work(result['item_id'])
            if item:
                before = self.repository.get_state('agent_usage', item.id) or {}
                code = (item.result or {}).get('dependency_failure')
                # Only the persisted domain outcome may reset/fail the circuit.
                # A valid JSON envelope alone is not successful research/copy.
                if before.get('attempted') and item.status.value in {'succeeded', 'needs_human', 'failed'}:
                    self.update_circuit(AgentResult(item.status.value, error_code=code))
                self.usage(item.id, item.status.value, code)
        return result


def process_factory(*, brain, hermes_root, profile, python, runtime_root, repository):
    """Trusted operator paths only. No executable/provider fields come from work."""
    source_root = Path(__file__).resolve().parents[2]
    def factory(job, evidence, cancelled):
        directory = Path(runtime_root) / 'attempts' / job.id
        directory.mkdir(parents=True, exist_ok=False)
        snapshot, diagnostic = directory / 'evidence.json', directory / 'diagnostic.json'
        atomic_json(snapshot, evidence)
        inherited = {key: os.environ[key] for key in ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TZ') if key in os.environ}
        env = {**inherited, 'HERMES_AUTH_READ_ONLY': '1', 'HERMES_HOME': str(Path(profile).resolve()), 'PYTHONPATH': str(source_root),
            'PYTHONDONTWRITEBYTECODE': '1', 'THUNDER_EXTERNAL_EFFECTS_ENABLED': 'false', 'THUNDER_OS_EXTERNAL_EFFECTS_ENABLED': 'false'}
        command = [str(Path(python).absolute()), '-m', '_ferramentas.thunder_os.hermes_text_child', '--mode', 'operation',
            '--brain', str(Path(brain).resolve()), '--evidence', str(snapshot.resolve()), '--diagnostic', str(diagnostic.resolve()),
            '--hermes-root', str(Path(hermes_root).resolve()), '--profile', str(Path(profile).resolve())]
        transport = bounded_process_transport(command, cwd=str(source_root), env=env, cancel_check=cancelled)
        def execute(current_job, timeout):
            try:
                output = transport(current_job, timeout)
            except InterruptedError:
                raise InternalExecutionError('execution_cancelled') from None
            except (TimeoutError,):
                raise InternalExecutionError('agent_timeout') from None
            except Exception:
                code = 'agent_transport_failed'
                if diagnostic.is_file():
                    try:
                        value = bounded_json(diagnostic).get('error_code')
                        if value in {'auth_unauthorized', 'auth_forbidden', 'auth_expired', 'auth_unavailable', 'auth_readonly_violation', 'auth_config_invalid', 'auth_endpoint_blocked', 'rate_limited', 'request_timeout',
                                     'invalid_output_schema', 'text_contract_rejected', 'bootstrap_failed', 'inference_failed', 'tool_output_blocked'}:
                            code = value
                    except Exception:
                        pass
                raise InternalExecutionError(code) from None
            if diagnostic.is_file():
                value = bounded_json(diagnostic).get('model')
                if isinstance(value, str) and 0 < len(value) <= 120 and all(c.isprintable() for c in value):
                    usage = repository.get_state('agent_usage', job.id) or {}
                    repository.put_state('agent_usage', job.id, {**{k: v for k, v in usage.items() if k not in {'id', 'version', 'updated_at'}}, 'model': value})
            return output
        return execute
    return factory


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('operation',), required=True)
    for option in ('db', 'brain', 'bundles', 'hermes-root', 'profile', 'python', 'runtime-root', 'health'):
        parser.add_argument('--' + option, type=Path, required=True)
    args = parser.parse_args(argv)
    enforce_external_gate()
    repository = Repository(args.db)
    service = ThunderService(repository, knowledge_store=KnowledgeStore(args.brain))
    stop = threading.Event()
    consumer = ContinuousConsumer(service, evidence_loader=TrustedEvidence(service, args.brain, args.bundles),
        transport_factory=process_factory(brain=args.brain, hermes_root=args.hermes_root, profile=args.profile,
            python=args.python, runtime_root=args.runtime_root, repository=repository), stop=stop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    def health():
        reason = consumer.block_reason()
        atomic_json(args.health, {'status': 'paused' if reason else 'ok', 'readiness': not bool(reason), 'reason': reason,
            'heartbeat_at': utcnow().isoformat(), 'source_sha': source_revision(), 'pid': os.getpid(),
            'claims_enabled': not bool(reason), 'external_effects_enabled': False})
    def reporter():
        while not stop.is_set():
            try:
                health()
            except Exception:
                stop.set()
                return
            stop.wait(5)
    thread = threading.Thread(target=reporter, daemon=True)
    thread.start()
    try:
        while not stop.is_set():
            consumer.run_once()
            stop.wait(1)
    finally:
        stop.set()
        thread.join(timeout=6)
        atomic_json(args.health, {'status': 'stopped', 'readiness': False, 'heartbeat_at': utcnow().isoformat(),
            'source_sha': source_revision(), 'claims_enabled': False, 'external_effects_enabled': False})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
