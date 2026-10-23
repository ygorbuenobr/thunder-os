"""Phase 1 Brazil journey projection over existing read ports; no action port.

snapshot(campaign=None) and detail(id, campaign=None) return plain text for an
escaping consumer. IDs are opaque, stable hashes of source identity. A detail is
served only while its source fingerprint still matches the current projection.
Counts describe observed rows, not an inferred population or provider totals.
"""
from copy import copy, deepcopy
import json
import re
import threading
from urllib.parse import urlsplit

from .cadence_readonly import PORTAL_ID, _date, _id, plain_text
from .campaign_registry import CampaignRegistry
from .external_email_import import _validate_plan
from .instantly_reader import InstantlyReader
from .models import payload_hash
from .seqd_client import MAILBOX
from .today_readonly import MX_CODE, ReadonlyRepository, TodayError, _safe_url


STAGES = (('icp', 'ICP'), ('abordagem', 'Abordagem'), ('resposta', 'Resposta'),
          ('crm', 'CRM'), ('linkedin', 'LinkedIn'), ('followup', 'Follow-up'),
          ('preparacao', 'Prepara\u00e7\u00e3o'))
MISSING = 'Texto original indisponivel nesta fonte.'
APPROVAL_REASON = 'Sem cadence e passo canonicos vinculados; a autorizacao exige identidade e toque exatos no backend.'
SOURCE_GROUP = 'visitantes-2026-10-22'
SOURCE_GROUP_LABEL = 'Visitantes 22/10/2026: rascunhos, campanha nao cadastrada'


def _opaque(kind, key):
    return 'j_' + payload_hash({'kind': kind, 'key': key})


def _text(value):
    """Reader text is already plain: retain whitespace and literal angle brackets."""
    if not isinstance(value, str):
        return ''
    value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', value)
    value = re.sub(r'\bpat-[A-Za-z0-9_-]+', '[REDACTED]', value)
    return re.sub(r'(?i)((?:access_token|api_key|token|password)\s*[=:]\s*)[^\s&]+',
                  r'\1[REDACTED]', value)


def _links(action):
    url = action.get('url') if isinstance(action, dict) else None
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
        return []
    try:
        parsed = urlsplit(url)
    except ValueError:
        return []
    allowed = _safe_url(url) is not None
    if parsed.scheme == 'https' and not parsed.query:
        if parsed.netloc == 'app.hubspot.com':
            allowed = bool(re.fullmatch('/contacts/' + PORTAL_ID + r'/record/0-(?:1|3|27|49)/[1-9][0-9]{0,29}', parsed.path)) and not parsed.fragment
        elif parsed.netloc == 'mail.google.com':
            allowed = bool(re.fullmatch(r'/mail/u/[A-Za-z0-9@%._+-]+/', parsed.path)
                           and re.fullmatch(r'all/[A-Za-z0-9_-]{1,200}', parsed.fragment))
        elif parsed.netloc == 'app.instantly.ai':
            allowed = parsed.path == '/app/unibox' and not parsed.fragment
        elif parsed.netloc == 'app.apollo.io':
            allowed = parsed.path in {'', '/', '/#/emails'} and not parsed.fragment
    return [{'label': plain_text(action.get('label')) or 'Abrir fonte', 'url': url}] if allowed else []


def _block(text=None, *, label=None, date=None, sender=None, direction=None, complete=False):
    text = _text(text)
    return {'label': label or ('Texto original' if complete and text.strip() else 'Texto parcial / indisponivel'),
            'text': text if text.strip() else MISSING, 'date': date,
            'sender': sender, 'direction': direction, 'body_complete': bool(complete and text.strip())}


class JourneyReadonly:
    def __init__(self, today, *, cadence_reader=None, accounts=None):
        self.today, self.cadence_reader, self.accounts = today, cadence_reader, accounts
        self.repo = ReadonlyRepository(today.repo.path)
        self.service = copy(today.service)
        self.service.repository = self.repo
        self.service.campaign_registry = CampaignRegistry(self.repo)
        self._lock = threading.RLock()
        self._details = {}

    def snapshot(self, campaign=None):
        self._validate_campaign(campaign)
        with self._lock:
            result, details, _ = self._build(campaign)
            self._details = details
            return deepcopy(result)

    def detail(self, id, campaign=None):
        self._validate_campaign(campaign)
        if not isinstance(id, str) or not re.fullmatch(r'j_[a-f0-9]{64}', id):
            raise TodayError('journey_detail_not_found', 404)
        with self._lock:
            previous = self._details.get(id)
            if previous is None:
                raise TodayError('journey_detail_not_found', 404)
            _, current, contacts = self._build(campaign)
            entry = current.get(id)
            if entry is None:
                raise TodayError('journey_detail_not_found', 404)
            if payload_hash(previous) != payload_hash(entry):
                raise TodayError('journey_detail_stale', 409)
            entry = deepcopy(entry)
            contact = contacts.get(id)
            if contact and self.cadence_reader is not None:
                try:
                    history = self.cadence_reader.timeline(contact, campaign=campaign)
                    for event in history.get('events', []):
                        if event.get('campaign') and campaign and event['campaign'] != campaign:
                            continue
                        entry['blocks'].append(_block(event.get('text'),
                            label='Historico CRM do contato / cobertura e vinculo historico nao garantidos',
                            date=event.get('occurred_at'), direction=event.get('direction'),
                            complete=event.get('body_complete') is True))
                    entry['blocks'].sort(key=lambda b: (b.get('date') or '~', b['label']))
                    if history.get('coverage', {}).get('complete') is not True:
                        entry['blocks'].append(_block(label='Cobertura parcial do historico CRM'))
                except Exception:
                    entry['blocks'].append(_block(label='Historico CRM indisponivel'))
            return entry

    def account_list(self, campaign=None):
        """Jornada's Brazil boundary; the generic account reader retains its scope."""
        self._validate_campaign(campaign)
        snap = self.snapshot(campaign=campaign)
        allowed = {row['id'] for row in snap['campaigns']}
        if self.accounts is None:
            return {'accounts': [], 'limitations': ['account_reader_unavailable'],
                    'omitted_count': None, 'read_only': True}
        try:
            catalog = self.accounts.list_accounts(campaign=campaign)
        except Exception:
            return {'accounts': [], 'limitations': ['account_source_unavailable'],
                    'omitted_count': None, 'read_only': True}
        rows = catalog.get('accounts', [])
        kept = [self._account_projection(row) for row in rows if (
            row.get('campaign_id') in allowed and (campaign is None or row['campaign_id'] == campaign))
            or (campaign is None and self._indexed_draft(row))]
        return {'accounts': deepcopy(kept), 'limitations': sorted({v.split(':', 1)[0]
                for v in catalog.get('limitations', []) if isinstance(v, str)}) +
                (['omitted_accounts_without_verified_brazil_campaign'] if len(kept) != len(rows) else []),
                'omitted_count': len(rows) - len(kept), 'read_only': True}

    def account_detail(self, id, campaign=None):
        with self._lock:
            return self._account_detail(id, campaign)

    def _account_detail(self, id, campaign):
        self._validate_campaign(campaign)
        if not isinstance(id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}', id):
            raise TodayError('journey_account_not_found', 404)
        matches = [r for r in self.account_list(campaign)['accounts'] if r['id'] == id]
        if not matches:
            raise TodayError('journey_account_not_found', 404)
        if len(matches) != 1:
            raise TodayError('journey_account_ambiguous', 409)
        row = matches[0]
        value = self.accounts.account(id, campaign=row['campaign_id'])
        if (value.get('id') != id or value.get('campaign_id') != row['campaign_id']
                or value.get('source') != row.get('source')):
            raise TodayError('journey_account_stale', 409)
        value = deepcopy(value)
        if self._indexed_draft(row):
            value.update({k: v for k, v in self._account_projection(row).items()
                          if k in {'source_group', 'source_group_label', 'campaign_label', 'readiness',
                                   'reviewable', 'status_only', 'approval_available', 'approval_reason', 'editable'}})
            if row['reviewable'] is False:
                # The explicit exception authorizes status only, including the
                # Mexico exclusion. Never expose original messages through raw text.
                value['source_text'] = ''
                context = [{k: v for k, v in source.items() if k in {'path', 'sha256', 'status', 'usage'}}
                           for source in value.get('tabs', {}).get('context', []) if isinstance(source, dict)]
                value['tabs'] = {key: [] for key in ('research', 'dossier', 'context', 'cadence', 'before_approval')}
                value['tabs']['context'] = context
                value['tabs']['before_approval'] = [{'text': str(row['readiness']), 'blocking': True}]
                value['thesis'] = None
            for touch in value.get('tabs', {}).get('cadence', []):
                for key in ('canonical_cadence_id', 'canonical_step_id'):
                    touch.pop(key, None)
                touch.update(approval_available=False, editable=False)
        else:
            touches = value.setdefault('tabs', {}).setdefault('cadence', [])
            for detail in self._details.values():
                if (detail.get('canonical_account_id') == id and detail.get('campaign_id') == row['campaign_id']
                        and detail.get('canonical_step_id')):
                    touches.append({'id': detail['id'], 'day': detail['canonical_step_id'],
                        'date': detail.get('date'), 'text': detail['draft']['text'],
                        'subject': detail['draft'].get('subject'), 'channel': detail.get('channel'),
                        'status': detail.get('step_status'), 'source': 'canonical_cadence_state',
                        'canonical_cadence_id': detail['canonical_cadence_id'],
                        'canonical_step_id': detail['canonical_step_id'], 'editable': detail['editable'],
                        'approval_available': False, 'approval_reason': detail['approval_reason'],
                        'blocks': deepcopy(detail['blocks'])})
        return value

    def _indexed_draft(self, row):
        from .account_cadence_readonly import AccountCadenceReadonly
        path = (row.get('source') or {}).get('path')
        index = row.get('source_index') or {}
        return (type(self.accounts) is AccountCadenceReadonly
            and row.get('source_group') == SOURCE_GROUP and row.get('campaign_id') is None
            and type(row.get('reviewable')) is bool and isinstance(row.get('readiness'), str)
            and index.get('path') == '_entrada/claude/cadencias-visitantes-2026-10-22/README.md'
            and isinstance(index.get('sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', index['sha256']) is not None
            and isinstance(path, str) and re.fullmatch(
                r'_entrada/claude/cadencias-visitantes-2026-10-22/[a-z0-9-]+/cadencia\.md', path) is not None)

    def _account_projection(self, row):
        result = deepcopy(row)
        if self._indexed_draft(row):
            for key in ('canonical_cadence_id', 'canonical_step_id'):
                result.pop(key, None)
            if not row['reviewable']:
                result['thesis'] = None
            result.update(source_group_label=SOURCE_GROUP_LABEL, campaign_label='Campanha nao cadastrada',
                          status_only=not row['reviewable'], editable=False, approval_available=False,
                          approval_reason='Rascunho documental sem cadence/step canonicos; execucao indisponivel.')
        return result

    def _effects(self):
        with self.repo._connect() as con:
            rows = con.execute('SELECT payload,state,receipt_ref FROM thunder_effect_jobs ORDER BY id LIMIT 10001').fetchall()
        if len(rows) > 10000:
            raise TodayError('journey_effect_read_limit', 503)
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]

    @staticmethod
    def _validate_campaign(campaign):
        if campaign is not None and (not isinstance(campaign, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', campaign)):
            raise TodayError('invalid_campaign', 400)

    def _build(self, campaign):
        sources, details, contacts = {}, {}, {}
        stages = {key: {'id': key, 'label': label, 'count': None, 'status': 'unavailable',
                       'reason': 'Fonte indisponivel; total desconhecido.', 'items': []}
                  for key, label in STAGES}

        def read(name, reader, fallback):
            try:
                value = reader()
                sources[name] = {'id': name, 'status': 'ok', 'collected_at': None}
                return value
            except Exception:
                sources[name] = {'id': name, 'status': 'offline', 'collected_at': None,
                                 'reason': 'source_unavailable'}
                return deepcopy(fallback)

        observed = read('today', self.today.snapshot, {})
        collected = observed.get('collected_at')
        sources['today']['collected_at'] = collected
        for block in observed.get('blocks', {}).values():
            for source in block.get('sources', []):
                sources[source['id']] = deepcopy(source)
        cadence = read('cadence', self.cadence_reader.snapshot, {}) if self.cadence_reader is not None else {}
        if not cadence:
            sources['cadence'] = {'id': 'cadence', 'status': 'offline', 'collected_at': None}
        else:
            sources['cadence'].update(collected_at=cadence.get('collected_at'),
                status='ok' if cadence.get('coverage', {}).get('complete') is True else 'partial',
                coverage=deepcopy(cadence.get('coverage', {})))

        # One read-only SQLite snapshot; provider ports are never called in a DB transaction.
        kinds = ('campaign_config', 'reply', 'cadence', 'meeting', 'research', 'hubspot_attempt')
        instant = None
        effects = None
        try:
            with self.repo.transaction():
                registry = read('campaign_registry', self.service.campaign_registry.list_campaigns, [])
                states = {kind: read('local:' + kind, lambda kind=kind: self.repo.list_state(kind), []) for kind in kinds}
                effects = read('local:effects', self._effects, None)
                if self.today.instantly is not None:
                    instant = read('instantly_cache', lambda: InstantlyReader(self.repo,
                        enable_check=self.today.instantly.enable_check, clock=self.today.instantly.clock).snapshot(), None)
        except Exception:
            registry, states = [], {kind: [] for kind in kinds}
            for name in ['campaign_registry', *('local:' + kind for kind in kinds)]:
                sources[name] = {'id': name, 'status': 'offline', 'reason': 'source_unavailable', 'collected_at': None}

        mx = set(getattr(self.today, '_mx_codes', ()))
        mx_contacts = set(getattr(self.today, '_mx_contacts', ()))
        mx_people = set(getattr(self.today, '_mx_people', ()))
        for row in states['campaign_config']:
            cfg = row.get('config', {})
            if MX_CODE.match(cfg.get('campaign_id', '')):
                mx.add(cfg['campaign_id'])
                mx.update(code for batch in cfg.get('batches', []) for code in batch.get('source_codes', []))

        def excluded(row):
            codes = [row.get(key) for key in ('campaign_id', 'campaign', 'source_code')]
            title = row.get('title') or ''
            codes.append(title.lstrip('[').split(']')[0].split('|')[0].strip())
            return (any(isinstance(code, str) and (MX_CODE.match(code) or code in mx) for code in codes)
                or bool(({row.get('contact_id')} | set(row.get('contact_ids') or [])) & mx_contacts)
                or bool(set(row.get('person_keys') or []) & mx_people))

        def selected(row):
            return not excluded(row) and (campaign is None or (row.get('campaign_id') or row.get('campaign')) == campaign)

        campaigns, sheets, omitted_campaigns = {}, {}, 0
        for row in registry:
            cid = row['id']
            if not row.get('active') or excluded({'campaign_id': cid}):
                omitted_campaigns += 1
                continue
            view = read('sheet:' + cid, lambda cid=cid: self.service.campaign_sheet_view(cid), {})
            if view.get('status') != 'complete' and sources['sheet:' + cid]['status'] == 'ok':
                sources['sheet:' + cid]['status'] = 'partial'
            country = next((f.get('content') for f in view.get('fields', [])
                            if f.get('key') == 'country' and f.get('status') == 'ok'), None)
            country = country.strip().casefold() if isinstance(country, str) else None
            if country not in {'br', 'brasil', 'brazil'} and not (country is None and cid.startswith('BR-')):
                omitted_campaigns += 1
                sources.pop('sheet:' + cid, None)
                continue
            campaigns[cid], sheets[cid] = {'id': cid, 'label': cid}, view
        omitted = {'campaigns': omitted_campaigns if sources['campaign_registry']['status'] == 'ok' else None,
                   'threads': 0, 'accounts': 0, 'activities': 0, 'items': 0}
        in_scope = selected

        def selected(row):
            cid = row.get('campaign_id') or row.get('campaign')
            return cid in campaigns and in_scope(row)

        all_threads = observed.get('observed_threads', [])
        threads = [t for t in all_threads if selected(t)]
        omitted['threads'] = sum(not selected(t) for t in all_threads)
        groups = [g for g in cadence.get('campaigns', []) if selected({'campaign_id': g['id']})]
        omitted['activities'] = sum(len(g.get('activities', [])) for g in cadence.get('campaigns', [])
                                    if not selected({'campaign_id': g['id']}))
        omitted['items'] = sum(not selected(row) for b in observed.get('blocks', {}).values()
                               for row in b.get('items', []))
        account_rows = []
        if self.accounts is not None:
            catalog = read('accounts', lambda: self.accounts.list_accounts(campaign=campaign), {})
            account_rows = [self._account_projection(r) for r in catalog.get('accounts', [])
                            if selected(r) or (campaign is None and self._indexed_draft(r))]
            omitted['accounts'] = len(catalog.get('accounts', [])) - len(account_rows)
            sources['accounts'].update(status='partial' if catalog.get('limitations') else sources['accounts']['status'],
                limitations=sorted({v.split(':', 1)[0] for v in catalog.get('limitations', []) if isinstance(v, str)}))

        contact_display = {}
        for group in groups:
            for task in group.get('tasks', []):
                for row in task.get('contacts', []):
                    if _id(row.get('id')):
                        contact_display[str(row['id'])] = {'name': row.get('name'), 'company': task.get('company')}

        def put(stage, key, row, blocks, *, draft=None, summary=None):
            ident = _opaque('detail', key)
            cid = row.get('campaign_id') or row.get('campaign')
            name = plain_text(row.get('name') or row.get('contact_name') or row.get('title')) or 'Identidade nao informada'
            company = plain_text(row.get('company')) or None
            date = row.get('date') or row.get('due_at') or row.get('occurred_at')
            detail = {'id': ident, 'title': name, 'meta': ' \u00b7 '.join(str(v) for v in (company, cid, date) if v),
                'blocks': blocks or [_block()], 'source_links': _links(row.get('action')),
                'draft': deepcopy(draft), 'editable': False, 'approval_available': False,
                'approval_reason': APPROVAL_REASON}
            details[ident] = detail
            contact = _id(row.get('contact_id'))
            if not contact and len(row.get('contact_ids') or []) == 1:
                contact = _id(row['contact_ids'][0])
            if contact:
                contacts[ident] = contact
            item = {'id': ident, 'name': name, 'company': company, 'campaign_id': cid,
                'date': date, 'owner': row.get('owner') or 'ygor',
                'summary': plain_text(summary or row.get('summary') or row.get('title')) or MISSING,
                'action': {'label': 'Abrir', 'type': 'detail', 'id': ident}}
            if stage and not any(i['id'] == ident for i in stages[stage]['items']):
                stages[stage]['items'].append(item)
            return ident

        for cid in sorted(campaigns):
            if campaign is not None and cid != campaign:
                continue
            view = sheets[cid]
            fields = view.get('fields', [])
            put('icp', 'campaign:' + cid, {'title': cid, 'campaign_id': cid}, [
                _block(f.get('content'), label=f['label'] if f.get('status') == 'ok' else f['label'] + ' / indisponivel',
                       date=f.get('source_updated_at'), complete=f.get('status') == 'ok') for f in fields],
                summary='Ficha da campanha: ' + ('completa' if view.get('status') == 'complete' else 'incompleta'))

        for row in account_rows:
            put('icp', ['account', row['id'], row.get('campaign_id')],
                {**row, 'title': row.get('name') or row.get('company') or row['id']},
                [_block(label='Ficha individual disponivel no leitor de contas')],
                summary=('DRAFT / campanha nao cadastrada. ' + row['readiness']) if self._indexed_draft(row) else
                        'Ficha individual da conta; cadastro nao comprova envio ou aprovacao')
            item = stages['icp']['items'][-1]
            item['action'] = {'label': 'Abrir', 'type': 'account', 'id': row['id']}
            if self._indexed_draft(row):
                item.update({k: row[k] for k in ('source_group', 'source_group_label', 'campaign_label',
                                                 'readiness', 'reviewable', 'status_only', 'approval_available')})

        pending = {item['id']: item for b in observed.get('blocks', {}).values() for item in b.get('items', [])}
        events, partial_stages = [], set()
        for thread in threads:
            if not selected(thread):
                continue
            thread = {**thread, **{k: v for k, v in contact_display.get(str(thread.get('contact_id')), {}).items() if v}}
            if not thread.get('name'):
                thread['name'] = next((p for p in thread.get('person_keys', []) if p != MAILBOX), None)
            messages = thread.get('thread', {}).get('messages', [])
            blocks = [_block(m.get('text'), date=m.get('date'), sender=m.get('sender'),
                direction=m.get('direction'), complete=m.get('body_complete') is True) for m in messages]
            latest = next((m for m in reversed(messages) if not m.get('unsent')), {})
            last_incoming = max((m.get('date') or '' for m in messages if m.get('direction') == 'incoming'), default='')
            # Exact contact + campaign join only. A later CRM outgoing observation
            # removes a pending assertion, but does not become a delivery receipt.
            later_outgoing = bool(thread.get('contact_id') and any(
                str(thread['contact_id']) in event.get('contact_ids', [])
                and event.get('crm_recorded_sent') is True
                and (event.get('occurred_at') or '') > last_incoming
                for group in groups if group['id'] == thread.get('campaign_id')
                for event in group.get('activities', [])))
            stage = 'linkedin' if 'linkedin' in thread.get('source_ids', []) else (
                'resposta' if thread['id'] in pending and not later_outgoing else 'followup')
            if thread.get('thread', {}).get('complete') is not True:
                partial_stages.add(stage)
            ident = put(stage, 'thread:' + thread['id'], thread, blocks,
                draft=pending.get(thread['id'], {}).get('draft'),
                summary='Saida posterior registrada no CRM; pendencia nao confirmada.' if later_outgoing else
                    'Ultima mensagem observada: ' + (latest.get('subject') or latest.get('direction', 'desconhecida')))
            for m in messages:
                events.append({**deepcopy(m), 'campaign_id': thread.get('campaign_id'),
                    'contact_id': thread.get('contact_id'), 'person_keys': thread.get('person_keys', []),
                    'thread_id': ident, 'name': thread.get('name') or thread.get('title'),
                    'company': thread.get('company'), 'action': thread.get('action')})

        # Provider-read observations in verified importer state retain their exact body.
        for attempt in states['hubspot_attempt']:
            plan = attempt.get('plan', {})
            if attempt.get('status') != 'verified_state' or plan.get('object_type') != 'emails':
                continue
            try:
                _validate_plan(plan)
            except ValueError:
                continue
            source = plan['source']
            row = {'campaign_id': source['campaign_id'], 'contact_id': plan['contact_id'],
                   'person_keys': [plan['lead_email']]}
            if not selected(row):
                continue
            reference = ':'.join((source['provider'], source['account_id'], source['provider_id']))
            body = plan['properties']['hs_email_text'].removesuffix('\n\n[Thunder OS source: ' + reference + ']')
            outgoing = plan['event_type'] == 'email_sent'
            events.append({**row, 'id': source['provider_id'], 'source_id': reference,
                'message_keys': [source['provider'] + ':' + source['provider_id']],
                'date': _date(plan['properties']['hs_timestamp']), 'text': body, 'body_complete': False,
                'direction': 'outgoing' if outgoing else 'incoming', 'unsent': False,
                'human_reply': plan.get('human_reply'), 'automatic': False,
                'subject': plan['properties']['hs_email_subject'], 'name': plan['lead_email'],
                'sender': None if outgoing else plan['lead_email'],
                'receipt': {'provider': source['provider'], 'message_id': source['provider_id'],
                            'verification': 'provider_read'} if outgoing else None})

        if instant and instant.get('status') in {'ok', 'partial', 'stale'} and selected(instant):
            sources['instantly_cache'].update(status=instant['status'],
                collected_at=instant.get('last_success_at'), coverage=deepcopy(instant.get('coverage', {})))
            for lead in instant.get('leads', []):
                row = {'campaign_id': instant['campaign_id'], 'person_keys': [lead['email']]}
                if not selected(row):
                    continue
                for e in lead.get('events', []):
                    outgoing = e.get('event_type') == 'email_sent'
                    events.append({**row, 'id': e['provider_id'], 'source_id': 'instantly:' + e['provider_id'],
                        'message_keys': ['instantly:' + e['provider_id']], 'date': _date(e.get('occurred_at')),
                        'text': e.get('text'), 'subject': e.get('subject'), 'body_complete': e.get('content_complete') is True,
                        'name': lead['email'], 'sender': None if outgoing else lead['email'],
                        'direction': 'outgoing' if outgoing else 'incoming', 'unsent': False,
                        'automatic': e.get('event_type') == 'auto_reply_received',
                        'human_reply': True if e.get('event_type') == 'reply_received' else None,
                        'receipt': {'provider': 'instantly', 'message_id': e['provider_id'],
                                    'verification': 'provider_read'} if outgoing else None})

        # Cadence activity rows remain CRM observations unless an actual provider
        # receipt is present. SENT, completed tasks and registry platform do not qualify.
        for group in groups:
            if campaign is not None and group['id'] != campaign:
                continue
            for row in group.get('tasks', []):
                row = {**row, 'campaign_id': group['id']}
                if not selected(row):
                    continue
                stage = 'linkedin' if row.get('channel') == 'linkedin' else 'followup'
                put(stage, 'task:' + row['id'], row, [_block(row.get('body'),
                    label='Corpo da tarefa (registro CRM)', date=row.get('due_at'))])
            for event in group.get('activities', []):
                row = {**event, 'campaign_id': group['id']}
                if not selected(row):
                    continue
                put('abordagem', 'activity:' + event['id'], row, [_block(event.get('text'),
                    label='Atividade registrada no CRM', date=event.get('occurred_at'))],
                    summary=event.get('subject') or 'Atividade CRM; envio externo nao comprovado')
                direction = event.get('direction') or ('incoming' if event.get('response_evidence') is True else
                    'outgoing' if event.get('crm_recorded_sent') is True else 'unknown')
                display = contact_display.get(str(row.get('contact_ids', [None])[0]), {}) if len(row.get('contact_ids', [])) == 1 else {}
                events.append({**row, **display, 'date': event.get('occurred_at'), 'direction': direction,
                    'body_complete': event.get('body_complete') is True})

        for block_name, block in observed.get('blocks', {}).items():
            for row in block.get('items', []):
                if not selected(row) or row['id'] in {t['id'] for t in threads}:
                    continue
                stage = ('abordagem' if block_name == 'approve' else 'crm' if row['id'].startswith('hubspot:deal:')
                         else 'linkedin' if 'linkedin' in row['id'] else 'followup')
                blocks = [_block(c.get('texto'), label='Contexto registrado / original nao verificado',
                                 date=c.get('quando'), sender=c.get('quem')) for c in row.get('context', [])]
                if not blocks:
                    blocks = [_block(label='Texto original indisponivel'),
                              _block(row.get('last_message'), label='Resumo / registro da fonte', date=row.get('date'))]
                put(stage, 'today:' + row['id'], row, blocks, draft=row.get('draft'))

        # Canonical DB rows are visible by their actual IDs. GET neither enters
        # the ledger preflight nor creates an approval/review/dispatch command.
        for cadence_row in states['cadence']:
            cid = (cadence_row.get('execution_context') or {}).get('campaign_id')
            canonical_id = cadence_row['id']
            row = {**cadence_row, 'campaign_id': cid, 'person_keys': [cadence_row.get('person_key')]}
            if (not selected(row) or not isinstance(canonical_id, str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', canonical_id)):
                continue
            steps = cadence_row.get('steps', [])
            step_ids = [s.get('id') for s in steps if isinstance(s, dict)]
            if (len(step_ids) != len(steps) or any(not isinstance(sid, str) for sid in step_ids)
                    or len(set(step_ids)) != len(step_ids)):
                continue
            for step in steps:
                sid = step.get('id')
                if (not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', sid)
                        or not isinstance(step.get('content'), str)):
                    continue
                peers = {p for p in (cadence_row.get('person_key'), step.get('recipient')) if isinstance(p, str)}
                blocks, links = [], []
                for thread in threads:
                    if thread.get('campaign_id') != cid or not peers.intersection(thread.get('person_keys', [])):
                        continue
                    links.extend(_links(thread.get('action')))
                    blocks.extend(_block(m.get('text'), date=m.get('date'), sender=m.get('sender'),
                        direction=m.get('direction'), complete=m.get('body_complete') is True)
                        for m in thread.get('thread', {}).get('messages', []))
                blocks.sort(key=lambda b: b.get('date') or '~')
                related = [e for e in effects or [] if e['payload'].get('cadence_id') == canonical_id
                           and (e['payload'].get('step') or {}).get('id') == sid]
                editable = (effects is not None and cadence_row.get('status') in {'ready_for_review', 'approved'}
                    and step.get('status') in {'draft', 'approved'} and not step.get('discarded')
                    and not any(e.get('receipt_ref') or e.get('state') != 'failed' for e in related))
                ident = put('abordagem', ['canonical_step', canonical_id, sid],
                    {**row, 'name': cadence_row.get('person_key'), 'company': cadence_row.get('account_id'),
                     'date': cadence_row.get('updated_at')},
                    blocks or [_block(label='Conversa original nao observada para este passo')],
                    draft={'text': _text(step['content']), 'subject': _text(step.get('subject')) or None,
                           'origin': 'canonical_cadence_state'},
                    summary='Passo canonico ' + sid + ': ' + str(step.get('status') or 'status desconhecido'))
                details[ident].update(canonical_cadence_id=canonical_id, canonical_step_id=sid,
                    canonical_account_id=cadence_row.get('account_id'), campaign_id=cid,
                    canonical_cadence_version=cadence_row.get('version'), channel=step.get('channel'),
                    step_status=step.get('status'), date=cadence_row.get('updated_at'), editable=bool(editable),
                    approval_reason='Revisao obrigatoria no backend para o toque exato; este GET nao autoriza execucao.',
                    source_links=sorted([dict(pair) for pair in {tuple(sorted(link.items())) for link in links}],
                                        key=lambda link: (link['url'], link['label'])))

        # Meeting records are preparation context, never evidence that a call happened.
        research = {r['id']: r for r in states['research']}
        for row in states['meeting']:
            cid = row.get('campaign_id')
            if not cid:
                matched = {c.get('execution_context', {}).get('campaign_id') for c in states['cadence']
                           if c.get('account_id') == row.get('account_id') and c.get('person_key') == row.get('person_key')}
                cid = next(iter(matched)) if len(matched) == 1 else None
            row = {**row, 'campaign_id': cid, 'person_keys': [row.get('person_key')],
                   'title': row.get('account_id'), 'date': row.get('occurred_at')}
            if selected(row):
                dossier = research.get(row.get('account_id'), {})
                put('preparacao', 'meeting:' + row['id'], row, [_block(f.get('text') or f.get('fact'),
                    label='Pesquisa registrada', date=f.get('occurred_at')) for f in dossier.get('facts', [])],
                    summary='Contexto registrado para reuniao; realizacao nao verificada')

        # Exact provider/message aliases deduplicate observations; no body/name joins.
        unique, aliases = [], {}
        for event in sorted(events, key=lambda e: (e.get('date') or '~', str(e.get('source_id') or e['id']))):
            keys = event.get('message_keys') or [event.get('source_id') or event['id']]
            found = next((aliases[k] for k in keys if k in aliases), None)
            if found is None:
                found = len(unique)
                unique.append(event)
            else:
                prior = unique[found]
                if event.get('body_complete') is True and prior.get('body_complete') is not True:
                    for key in ('text', 'body_complete', 'sender', 'subject', 'date'):
                        prior[key] = event.get(key)
                for key in ('receipt', 'contact_id', 'thread_id'):
                    if key == 'contact_id' and prior.get(key) and event.get(key) and prior[key] != event[key]:
                        prior['contact_ids'] = sorted(set(prior.get('contact_ids', [])) | {prior[key], event[key]})
                    if not prior.get(key) and event.get(key):
                        prior[key] = event[key]
                prior['person_keys'] = sorted(set(prior.get('person_keys', [])) | set(event.get('person_keys', [])))
                prior['message_keys'] = sorted(set(prior.get('message_keys') or [prior.get('source_id') or prior['id']]) | set(keys))
            for key in keys:
                aliases[key] = found

        person_contacts = {}
        for e in unique:
            for key in e.get('person_keys', []):
                if isinstance(key, str) and key.lower() != MAILBOX and _id(e.get('contact_id')):
                    person_contacts.setdefault(key.lower(), set()).add(str(e['contact_id']))

        def person(e):
            ids = set(e.get('contact_ids') or []) | ({str(e['contact_id'])} if _id(e.get('contact_id')) else set())
            for key in e.get('person_keys', []):
                if str(key).lower() != MAILBOX:
                    ids.update(person_contacts.get(str(key).lower(), ()))
            if len(ids) == 1:
                return 'crm:' + next(iter(ids))
            if ids:
                return None
            keys = sorted(str(k).lower() for k in e.get('person_keys', []) if isinstance(k, str)
                          and k.lower() != MAILBOX and (re.fullmatch(r'[^\s@<>]+@[^\s@<>]+', k) or _safe_url(k)))
            return keys[0] if len(keys) == 1 else None

        reached, humans, positives = set(), set(), set()
        first_touches, missing_identity = {}, False
        for e in unique:
            who = person(e)
            receipt = e.get('receipt') or {}
            proven = (e.get('direction') == 'outgoing' and not e.get('unsent') and e.get('date')
                and receipt.get('verification') == 'provider_read'
                and receipt.get('provider') in {'gmail', 'instantly', 'apollo', 'connectsafely'}
                and isinstance(receipt.get('message_id'), str) and bool(receipt['message_id']))
            if proven:
                if who:
                    reached.add(who)
                    first_touches.setdefault((who, e.get('campaign_id')), e)
                else:
                    missing_identity = True
            if e.get('direction') == 'incoming' and not e.get('automatic') and e.get('human_reply') is True and who:
                humans.add(who)
            e['person'] = who

        for key, e in first_touches.items():
            put('abordagem', ['first_touch', *key], {**e, 'title': e.get('name') or e.get('subject')},
                [_block(e.get('text'), date=e.get('date'), sender=e.get('sender'),
                        direction='outgoing', complete=e.get('body_complete') is True)],
                summary='Primeiro toque observado com recibo do provider')

        for reply in states['reply']:
            if (reply.get('classification') != 'positive' or reply.get('state') != 'classified'
                    or not reply.get('classification_history')
                    or reply['classification_history'][-1].get('classification') != 'positive'):
                continue
            for e in unique:
                if (reply['id'] in (e.get('message_keys') or [e.get('source_id')])
                        and e.get('direction') == 'incoming' and e.get('body_complete') is True
                        and not e.get('automatic') and e.get('person')
                        and reply.get('content') == e.get('text') and not any(
                            later.get('person') == e['person'] and later.get('direction') == 'incoming'
                            and not later.get('automatic') and (later.get('date') or '') > (e.get('date') or '')
                            for later in unique)):
                    positives.add(e['person'])

        scope = ({campaign} if campaign in campaigns else set()) if campaign else set(campaigns)
        reply_complete = bool(scope and instant and scope == {instant.get('campaign_id')}
            and instant.get('status') == 'ok' and instant.get('coverage', {}).get('complete') is True
            and not (instant.get('metrics') or {}).get('unclassified_replies')
            and not missing_identity)
        # Complete Instantly classification can prove zero; other bounded sources
        # cannot prove a global absence of human replies or positive interest.
        metric_sources = sorted(k for k in sources if k in {'gmail', 'seqd', 'instantly', 'cadence',
                                                          'instantly_cache', 'local:hubspot_attempt', 'local:reply'})

        def metric(value, reason, complete=False, observed_count=None):
            return {'value': value, 'source': metric_sources, 'reason': reason,
                    'coverage': {'complete': complete, 'scope': 'observed_brazil_people',
                                 'observed_count': observed_count}}

        metrics = {
            'reached': metric(len(reached) if (reached or reply_complete) and not missing_identity else None,
                'person_identity_unverified' if missing_identity else
                'unique_people_first_observed_touch_provider_receipt' if reached else 'provider_receipt_unavailable',
                complete=reply_complete, observed_count=len(reached)),
            'replied': metric(len(humans) if reply_complete else None,
                'provider_classified_humans' if reply_complete else 'partial_human_reply_coverage', reply_complete, len(humans)),
            'hot': metric(len(positives) if positives else None,
                'explicit_positive_classification_bound_to_original' if positives else 'positive_classification_unavailable',
                observed_count=len(positives)),
            'cost_per_hot': metric(None, 'verified_cost_source_unavailable')}

        dependencies = {'icp': ['campaign_registry'], 'abordagem': ['cadence', 'instantly'],
            'resposta': ['gmail', 'seqd', 'instantly'], 'crm': ['hubspot_deals'],
            'linkedin': ['linkedin'], 'followup': ['hubspot_tasks', 'gmail', 'seqd'],
            'preparacao': ['local:meeting', 'local:research']}
        for key, stage in stages.items():
            deps = [sources.get(name, {'status': 'offline'}) for name in dependencies[key]]
            complete = key not in partial_stages and all(s.get('status') == 'ok' for s in deps)
            if key == 'icp':
                complete = complete and all(view.get('status') == 'complete' for cid, view in sheets.items()
                                             if campaign is None or campaign == cid)
                if self.accounts is not None:
                    complete = complete and sources.get('accounts', {}).get('status') == 'ok'
            stage['items'].sort(key=lambda i: (i['date'] or '~', i['id']))
            stage.update(count=len(stage['items']) if complete else None,
                status='ok' if complete else 'partial' if stage['items'] else 'unavailable',
                reason='Registros observados nas fontes declaradas.' if complete else
                       'Cobertura parcial ou indisponivel; total desconhecido.')
            if key == 'preparacao' and not stage['items']:
                stage.update(count=None, status='unavailable', reason='Sem preparacao vinculada a reuniao comprovada.')

        # Pick a real observed person with a full chronology, including sent-after-reply.
        candidates = {}
        for e in unique:
            key = e.get('person') or e.get('thread_id')
            if key:
                candidates.setdefault(key, []).append(e)
        chosen = max(candidates.values(), key=lambda rows: (
            max(e.get('date') or '' for e in rows), len(rows)), default=[])
        timeline = {'name': next((e['name'] for e in reversed(chosen) if e.get('name')), None),
                    'company': next((e['company'] for e in reversed(chosen) if e.get('company')), None), 'events': []}
        for e in chosen:
            did = put(None, ['event', e.get('source_id') or e['id']], {**e, 'title': e.get('subject') or e.get('name')},
                [_block(e.get('text'), date=e.get('date'), sender=e.get('sender'),
                        direction=e.get('direction'), complete=e.get('body_complete') is True)])
            timeline['events'].append({'id': _opaque('event', e.get('source_id') or e['id']),
                'date': e.get('date'), 'label': 'Voce' if e.get('direction') == 'outgoing' else 'Resposta'
                    if e.get('direction') == 'incoming' else 'Registro',
                'summary': plain_text(e.get('subject')) or 'Mensagem observada', 'detail_id': did})
        timeline['events'].sort(key=lambda e: (e['date'] or '~', e['id']))
        log = [{'date': s.get('collected_at'), 'message': s['id'] + ': ' + s.get('status', 'unknown')}
               for s in sources.values()]
        sources['brazil_scope'] = {'id': 'brazil_scope', 'status': 'partial' if any(omitted.values()) else 'ok',
            'collected_at': collected, 'omitted': omitted,
            'reason': 'Validated Brazil campaigns; explicit indexed visitor drafts are account documents only. Unknown regions omitted.',
            'draft_source_group': SOURCE_GROUP if any(self._indexed_draft(r) for r in account_rows) else None}
        return {'read_only': True, 'collected_at': collected, 'campaigns': [campaigns[k] for k in sorted(campaigns)],
                'metrics': metrics, 'stages': list(stages.values()), 'timeline': timeline,
                'log': log, 'sources': list(sources.values())}, details, contacts
