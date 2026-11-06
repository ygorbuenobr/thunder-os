"""Read-only attention projection over a non-dispatching task preparation."""

from datetime import datetime, timezone
from contextlib import nullcontext
import threading
from .models import payload_hash

EXCLUDED_TASK = '6aea584a2b8c4b0020f93935'


def reservation_resolution(service, row, authorized_company):
    """Read a matching decision; absence or corrupted evidence never hides risk."""
    if (row.get('state') != 'uncertain' or not isinstance(authorized_company, str)
            or not authorized_company or row.get('company') != authorized_company):
        return None
    if not all(isinstance(row.get(k), str) and row[k] for k in ('task_id', 'action_type', 'updated_at')):
        return None
    repo = getattr(service, 'repository', None)
    if not callable(getattr(repo, 'get_state', None)):
        return None
    key = payload_hash({'company': authorized_company, 'task_id': row['task_id'], 'action_type': row['action_type']})
    try:
        decision = repo.get_state('apollo_linkedin_uncertain_resolution', key)
    except Exception:
        return None
    if (not isinstance(decision, dict) or decision.get('company') != authorized_company
            or decision.get('task_id') != row['task_id'] or decision.get('action_type') != row['action_type']
            or decision.get('reservation_updated_at') != row['updated_at']
            or not isinstance(decision.get('actor'), str) or not decision['actor'].strip()
            or not isinstance(decision.get('justification'), str) or not decision['justification'].strip()
            or decision.get('consumed') is not False or decision.get('decision') not in ('close', 'release')):
        return None
    return decision['decision']


def reservation_attention_item(row, resolution=None):
    """Present durable states without interpreting a reservation as a receipt."""
    state = row.get('state')
    if state == 'confirmed' and row.get('apollo_status') in ('pending', 'attempted'):
        label = 'apollo_reconcile_pending'
        next_action = ('Consultar a tarefa no Apollo antes de qualquer nova conclusão; não reenviar no LinkedIn.'
                       if row.get('apollo_status') == 'attempted' else
                       'Reconciliar a conclusão no Apollo; não reenviar no LinkedIn.')
    elif state == 'uncertain':
        if resolution == 'close':
            label = 'uncertain_closed'
            next_action = 'Decisão registrada como encerrada; consultar o histórico. Nenhum envio será repetido.'
        elif resolution == 'release':
            label = 'uncertain_release_recorded'
            next_action = ('Liberação registrada; convite com resultado incerto não pode ser repetido. Verificar no provedor.'
                           if row.get('action_type') == 'linkedin_step_connect' else
                           'Liberação registrada; um novo canário e todas as verificações serão necessários. Nenhum envio foi feito.')
        else:
            label = row.get('reason') or 'Resultado incerto'
            next_action = 'Verificar o resultado no provedor; não repetir o envio.'
    elif state == 'reserved':
        label = row.get('reason') or 'Reserva sem resultado'
        next_action = 'Verificar o resultado no provedor antes de qualquer ação; não repetir.'
    else:
        return None
    return {'task_id': row['task_id'], 'state': state, 'reason': label,
            'stopped_at': row.get('updated_at') or row.get('created_at'),
            **({'attention_level': 'resolved'} if resolution == 'close' else {}),
            'next_action': next_action, 'execution_enabled': False}


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
                if row.get('company') != self.company_id:
                    continue
                item = reservation_attention_item(row, reservation_resolution(self.service, row, self.company_id))
                if item:
                    items.append(item)
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
