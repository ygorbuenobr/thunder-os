"""Read-only attention projection over a non-dispatching task preparation."""

from datetime import datetime, timezone

EXCLUDED_TASK = '6aea584a2b8c4b0020f93935'


class ApolloLinkedInAttention:
    def __init__(self, service, reader, executor, *, company_id, team_id):
        self.service, self.reader, self.executor = service, reader, executor
        self.company_id, self.team_id = company_id, team_id
        self._first_observed = {}

    def snapshot(self):
        items = []
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
            for task in result['tasks']:
                if task.get('id') == EXCLUDED_TASK:
                    continue
                try:
                    detail = self.reader.get(task['id'])
                    keys = ('id', 'type', 'status', 'emailer_campaign_id', 'user_id')
                    if any(task.get(key) != detail.get(key) for key in keys):
                        prepared = {'state': 'blocked', 'reason': 'task_changed'}
                    else:
                        prepared = self.executor.prepare(detail, company_id=self.company_id, team_id=self.team_id, read_only=True)
                except Exception:
                    prepared = {'state': 'blocked', 'reason': 'preparation_unavailable'}
                if prepared.get('state') == 'blocked':
                    observed = self._first_observed.setdefault(task.get('id'), datetime.now(timezone.utc).isoformat())
                    items.append({'task_id': task.get('id'), 'reason': prepared.get('reason', 'blocked'),
                                  'stopped_at': task.get('updated_at') or task.get('created_at') or observed,
                                  'age_basis': 'provider' if task.get('updated_at') or task.get('created_at') else 'first_observed',
                                  'next_action': 'Verificar os dados e a conta padrão antes de qualquer ação.'})
            return {'status': 'ok' if result.get('coverage') == 'complete' else 'partial',
                    'coverage': result.get('coverage', 'unknown'), 'items': items}
        except Exception:
            return {'status': 'partial', 'coverage': 'unknown', 'items': items}
