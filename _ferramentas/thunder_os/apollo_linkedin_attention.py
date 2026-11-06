"""Read-only attention projection over a non-dispatching task preparation."""

from datetime import datetime, timezone
from contextlib import nullcontext
import threading

EXCLUDED_TASK = '6aea584a2b8c4b0020f93935'


class ApolloLinkedInAttention:
    def __init__(self, service, reader, executor, *, company_id, team_id, budget_seconds=5, max_pages=3, read_isolation=None):
        if read_isolation not in (None, 'in_process_accepted', 'separate_broker'):
            raise ValueError('configuration_invalid')
        self.read_isolation = read_isolation
        self.service, self.reader, self.executor = service, reader, executor
        self.company_id, self.team_id = company_id, team_id
        self._first_observed = {}
        self.budget_seconds, self.max_pages = budget_seconds, max_pages
        self._inflight = threading.BoundedSemaphore(1)
        self._partial_items = []

    def snapshot(self):
        result = self._snapshot_bounded()
        result['read_isolation'] = self.read_isolation
        return result

    def _snapshot_bounded(self):
        if not self._inflight.acquire(blocking=False):
            return {'status': 'partial', 'coverage': 'unknown', 'reason': 'read_in_progress', 'items': list(self._partial_items)}
        finished = threading.Event()
        result = []
        partial_items = []
        self._partial_items = partial_items
        def run():
            try:
                factory = getattr(self.reader, 'attention_budget', None) if callable(getattr(type(self.reader), 'attention_budget', None)) else None
                context = factory(seconds=self.budget_seconds, max_pages=self.max_pages) if callable(factory) else nullcontext()
                with context as budget:
                    result.append(self._snapshot(budget, partial_items))
            except Exception:
                result.append({'status': 'partial', 'coverage': 'unknown', 'reason': 'read_unavailable', 'items': []})
            finally:
                self._inflight.release()
                finished.set()
        threading.Thread(target=run, daemon=True).start()
        if not finished.wait(self.budget_seconds):
            return {'status': 'partial', 'coverage': 'unknown', 'reason': 'time_budget_exhausted', 'items': list(partial_items)}
        return result[0]

    def _snapshot(self, budget=None, items=None):
        items = [] if items is None else items
        try:
            for row in self.service.list_apollo_linkedin_reservations():
                if row['state'] in ('uncertain', 'reserved'):
                    items.append({'task_id': row['task_id'], 'reason': row.get('reason') or
                                  ('Resultado incerto' if row['state'] == 'uncertain' else 'Reserva sem resultado'),
                                  'stopped_at': row.get('updated_at') or row.get('created_at'),
                                  'next_action': 'Verificar o resultado no provedor antes de qualquer ação; não repetir.'})
            for lock in self.service.list_apollo_linkedin_account_locks():
                items.append({'task_id': None, 'reason': 'Lock de conta presente: ' + lock['account_id'],
                              'stopped_at': lock['created_at'],
                              'next_action': 'Verificar o processo responsável e reconciliar o lock antes de agir.'})
        except Exception:
            return {'status': 'unavailable', 'items': [], 'reason': 'official_store_unavailable'}
        try:
            result = self.reader.list_open_tasks()
            exhausted = False
            for task in result['tasks']:
                if budget is not None and budget.expired():
                    exhausted = True
                    break
                if task.get('id') == EXCLUDED_TASK:
                    items.append({'task_id': task.get('id'), 'reason': 'fixture_protegida',
                                  'stopped_at': task.get('updated_at') or task.get('created_at'),
                                  'next_action': 'Fixture protegida: não executar nem concluir.',
                                  'execution_enabled': False})
                    continue
                try:
                    detail = self.reader.get(task['id'])
                    keys = ('id', 'type', 'status', 'emailer_campaign_id', 'user_id')
                    if any(task.get(key) != detail.get(key) for key in keys):
                        prepared = {'state': 'blocked', 'reason': 'task_changed'}
                    else:
                        prepared = self.executor.prepare(detail, company_id=self.company_id, team_id=self.team_id, read_only=True)
                except Exception as exc:
                    reason = 'task_removed' if isinstance(exc, ValueError) and str(exc) == 'task_removed' else 'preparation_unavailable'
                    prepared = {'state': 'blocked', 'reason': reason}
                if prepared.get('state') == 'blocked':
                    observed = self._first_observed.setdefault(task.get('id'), datetime.now(timezone.utc).isoformat())
                    items.append({'task_id': task.get('id'), 'reason': prepared.get('reason', 'blocked'),
                                  'stopped_at': task.get('updated_at') or task.get('created_at') or observed,
                                  'age_basis': 'provider' if task.get('updated_at') or task.get('created_at') else 'first_observed',
                                  'account_observation': prepared.get('account_observation'),
                                  'text_hash': prepared.get('text_hash'),
                                  'execution_enabled': False,
                                  'next_action': ('Tarefa removida no Apollo: não executar nem marcar como concluída.'
                                                  if prepared.get('reason') == 'task_removed' else
                                                  'Verificar os dados e a conta padrão antes de qualquer ação.')})
            if budget is not None and budget.expired():
                exhausted = True
            coverage = 'partial' if exhausted else result.get('coverage', 'unknown')
            return {'status': 'ok' if coverage == 'complete' else 'partial',
                    'coverage': coverage, 'reason': 'time_budget_exhausted' if exhausted else result.get('reason'), 'items': items}
        except Exception:
            return {'status': 'partial', 'coverage': 'unknown', 'items': items}
