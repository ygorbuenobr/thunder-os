#!/usr/bin/env python3
"""Bounded read-only Mexico supervision, invoked by the existing Hermes scheduler.

Only HTTP GET and HubSpot search POST are implemented. No commercial writes,
profile views, model calls, credential copying or outbound-message transport.
Hermes owns alert delivery; unknown delivery is never blindly repeated.
"""
import argparse
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

BATCHES = ()
ACCOUNT = ''


def fingerprint(incident):
    return hashlib.sha256(json.dumps(incident, sort_keys=True).encode()).hexdigest()


def incident(key, detail, action):
    return {'key': key, 'detail': detail, 'action': action}


def fetch(host, path, token, payload=None):
    allowed = (host == 'api.hubapi.com' and path == '/crm/v3/objects/tasks/search' and payload is not None)
    allowed |= (host == 'api.connectsafely.ai' and payload is None and path in (
        '/linkedin/account/' + ACCOUNT + '/status', '/linkedin/account/' + ACCOUNT + '/quota',
        '/linkedin/invitations/sent?accountId=' + ACCOUNT + '&startIndex=0'))
    if not allowed:
        raise ValueError('route_not_read_only')
    req = urllib.request.Request('https://' + host + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.loads(response.read(3000000))
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (OSError, ValueError):
        return 0, {}


def classify(tasks, now):
    alerts, keys = [], {}
    summary = {'tasks': len(tasks), 'completed': 0, 'administrative': 0, 'waiting': 0}
    for task in tasks:
        p = task.get('properties', {}); title = p.get('hs_task_subject') or ''
        body = html.unescape(re.sub('<[^>]+>', '\n', p.get('hs_task_body') or ''))
        if not any(batch in title for batch in BATCHES):
            continue
        tid = str(task['id']); status = p.get('hs_task_status')
        if 'DISPENSADA SEM ENVIO' in title or '[T0' not in title:
            summary['administrative'] += 1; continue
        if status == 'DEFERRED':
            continue
        summary['completed'] += status == 'COMPLETED'
        summary['waiting'] += status == 'WAITING'
        key = re.search(r'"task_key"\s*:\s*"([^"]+)"', body)
        if key:
            if key[1] in keys:
                alerts.append(incident('duplicate:' + key[1], 'Duas tarefas ativas para a mesma chave.', 'Reconciliar IDs ' + keys[key[1]] + ' e ' + tid + '; não executar em paralelo.'))
            keys[key[1]] = tid
        if status == 'COMPLETED':
            receipt = re.search(r'(Gmail.{0,100}(?:id|message).{0,12}[a-f0-9]{16}|SENT.{0,30}id=[a-f0-9]{16}|ConnectSafely success=true|invitationSent=true)', body, re.I | re.S)
            if not receipt:
                alerts.append(incident('receipt:' + tid, 'Tarefa comercial concluída sem referência reconhecível de recibo.', 'Reconciliar tarefa ' + tid + ' com provedor; não reenviar.'))
        # Nominal WAITING dates are not delivery obligations: conditional stages stay silent.
        if status == 'NOT_STARTED' and ('outreach_authorized=true' in body or '"outreach_authorized": true' in body):
            from datetime import datetime
            try: due = datetime.fromisoformat(p['hs_timestamp'].replace('Z', '+00:00')).timestamp()
            except (KeyError, ValueError, TypeError): continue
            if due < now - 3600:
                alerts.append(incident('due-review:' + tid, 'Tarefa autorizada NOT_STARTED com data nominal vencida há mais de uma hora.', 'Executor deve revisar elegibilidade/gatilhos da tarefa ' + tid + '; vencimento sozinho não autoriza envio.'))
    return alerts, summary


def collect(config):
    alerts, tasks, observations = [], [], {}
    env = {}
    for line in Path(config['env_file']).read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1); env[key.strip()] = value.strip().strip(chr(34)).strip(chr(39))
    for batch in BATCHES:
        after = None
        for page in range(6):
            body = {'filterGroups': [{'filters': [
                {'propertyName': 'hubspot_owner_id', 'operator': 'EQ', 'value': config['owner_id']},
                {'propertyName': 'hs_task_subject', 'operator': 'CONTAINS_TOKEN', 'value': batch}]}],
                'properties': ['hs_task_subject', 'hs_task_body', 'hs_task_status', 'hs_timestamp'], 'limit': 100}
            if after is not None: body['after'] = after
            code, data = fetch('api.hubapi.com', '/crm/v3/objects/tasks/search', env.get('HUBSPOT_ACCESS_TOKEN', ''), body)
            if code != 200:
                alerts.append(incident('hubspot-read', 'HubSpot leitura HTTP' + str(code), 'Restaurar a leitura existente; nenhuma conclusão de ausência de resposta ou recibo.')); break
            tasks.extend(data.get('results', [])); after = data.get('paging', {}).get('next', {}).get('after')
            if after is None: break
        if after is not None:
            alerts.append(incident('hubspot-pagination', 'Consulta de tarefas excedeu orçamento de paginação.', 'Revisar cobertura do lote sem extrapolar escopo.'))
        if code != 200: break
    found, observations['crm'] = classify(tasks, time.time()); alerts += found
    token = env.get('CONNECTSAFELY_API_KEY', '')
    code, status = fetch('api.connectsafely.ai', '/linkedin/account/' + ACCOUNT + '/status', token)
    observations['ygor_status_http'] = code
    if code != 200 or status.get('publicId') != config['public_id'] or status.get('id') != ACCOUNT or status.get('status') != 'AVAILABLE':
        alerts.append(incident('ygor-channel', 'Conta Ygor indisponível, identidade divergente ou leitura HTTP' + str(code), 'Revisar canal existente; não reconectar Waalaxy nem trocar conta.'))
    else:
        code, quotas = fetch('api.connectsafely.ai', '/linkedin/account/' + ACCOUNT + '/quota', token)
        if code != 200:
            alerts.append(incident('ygor-quota-read', 'Leitura de cotas HTTP' + str(code), 'Não inferir capacidade nem contornar limites.'))
        else:
            observations['quotas'] = [{k: row.get(k) for k in ('feature', 'used', 'limit', 'limitReached')} for row in quotas.get('quotas', [])]
            for row in quotas.get('quotas', []):
                if row.get('limitReached'):
                    alerts.append(incident('quota:' + str(row.get('feature')), 'Limite ConnectSafely atingido.', 'Aguardar renovação; não contornar nem repetir ações.'))
            # Do not issue additional reads after 429 or another quota-read failure.
            code, invites = fetch('api.connectsafely.ai', '/linkedin/invitations/sent?accountId=' + ACCOUNT + '&startIndex=0', token)
            observations['invitation_page_http'] = code
            observations['invitation_page_complete'] = invites.get('endOfList') is True
            observations['invitation_page_count'] = len(invites.get('invitations', []))
            # This page is diagnostic, never proof of absence/acceptance.
            if code != 200:
                alerts.append(incident('ygor-invitations-read', 'Leitura de convites HTTP' + str(code), 'Reconciliar convites pelo recibo e estado remoto; não reenviar.'))
    result = subprocess.run(['journalctl', '-u', 'thunder-os-inbound-monitor.service', '-n', '15', '-o', 'cat', '--no-pager'], capture_output=True, text=True, timeout=10)
    last = None
    for line in result.stdout.splitlines():
        try: row = json.loads(line)
        except ValueError: continue
        if isinstance(row, dict) and 'healthy' in row: last = row
    observations['inbox'] = last
    if not last or not last.get('healthy') or last.get('stale') or last.get('from_cache') or not last.get('window_complete'):
        alerts.append(incident('inbox-degraded', 'Inbox VPS sem cobertura atual completa: ' + str((last or {}).get('error_code') or 'unknown'), 'Reconciliar leitor ConnectSafely. Exitcode zero não prova saúde; respostas/aceites não estão integralmente cobertos.'))
    alerts.append(incident('coverage-loa', 'Cobertura parcial: credencial Hermes não acessa Loa (HTTP404 comprovado); MCP Loa separado funciona.', 'Usar revisão manual pela rota Loa autorizada. Não transferir credenciais nem ampliar acesso.'))
    alerts.append(incident('coverage-receipts-mail', 'Cobertura parcial: recibos atuais do executor no Mac e Gmail não têm coleta contínua verificada neste hook VPS.', 'Executor deve conferir Gmail e recibos antes de cada toque. Este supervisor não afirma ausência de respostas nem envio apenas por COMPLETED.'))
    return {'observed_at': time.time(), 'coverage': 'DEGRADED', 'observations': observations, 'incidents': alerts}


def delivery_status(config, since):
    base = Path(config['cron_dir']); statuses = []
    with sqlite3.connect('file:' + str(base / 'executions.db') + '?mode=ro', uri=True) as ex, sqlite3.connect('file:' + str(base / 'deliveries.db') + '?mode=ro', uri=True) as de:
        rows = ex.execute('select id from executions where job_id=? and julianday(claimed_at)>=julianday(?)', (config['job_id'], since)).fetchall()
        for (eid,) in rows:
            row = de.execute('select status from deliveries where execution_id=?', (eid,)).fetchone()
            if row: statuses.append(row[0])
    if 'delivered' in statuses: return 'delivered'
    if 'unknown' in statuses or 'delivering' in statuses: return 'unknown'
    return 'pending'


def select_alerts(snapshot, state):
    return [i for i in snapshot['incidents'] if fingerprint(i) not in state.get('acknowledged', [])]


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--config', required=True); parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(); config = json.loads(Path(args.config).read_text()); root = Path(config['state_dir'])
    global BATCHES, ACCOUNT
    BATCHES = tuple(config['batches']); ACCOUNT = config['account_id']
    if not re.fullmatch(r'[a-f0-9]{24}', ACCOUNT): raise ValueError('invalid_account')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'lock').open('w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        snapshot = collect(config)
        if args.dry_run:
            print(json.dumps(snapshot, ensure_ascii=False)); return
        path = root / 'state.json'; state = json.loads(path.read_text()) if path.exists() else {'acknowledged': []}
        pending = state.get('pending')
        if pending:
            status = delivery_status(config, pending['since'])
            if status == 'delivered':
                state['acknowledged'] += pending['fingerprints']; state.pop('pending')
            else:
                snapshot['delivery'] = status
                (root / 'latest.json').write_text(json.dumps(snapshot, ensure_ascii=False)); return
        # Reopening after a fully observed resolution is a new incident.
        current = {fingerprint(i) for i in snapshot['incidents']}
        state['acknowledged'] = [f for f in state['acknowledged'] if f in current]
        fresh = select_alerts(snapshot, state)
        if fresh:
            from datetime import datetime, timezone
            state['pending'] = {'since': datetime.fromtimestamp(time.time() - 300, timezone.utc).isoformat(), 'fingerprints': [fingerprint(i) for i in fresh]}
        temp = root / 'state.tmp'; temp.write_text(json.dumps(state)); os.replace(temp, path)
        (root / 'latest.json').write_text(json.dumps(snapshot, ensure_ascii=False))
        if fresh:
            print('Supervisão México — atenção (somente leitura; cobertura parcial)')
            for item in fresh: print('- ' + item['detail'] + ' ' + item['action'])


if __name__ == '__main__':
    main()
