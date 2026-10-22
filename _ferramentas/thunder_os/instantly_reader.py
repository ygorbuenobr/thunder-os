"""Bounded GET-only Instantly observation cache. No CRM or action ports."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
import re
import stat
import threading
from urllib.parse import urlencode

from .external_email_import import (ExternalEmailError, INSTANTLY_CAMPAIGN,
    INSTANTLY_WORKSPACE, email, require, timestamp)
from .instantly_webhook import _https_request, _uuid

KEY_FILE = '/etc/thunder-os/instantly_api_key'
ENABLED_FILE = '/etc/thunder-os/instantly_reader_enabled'
KIND = 'instantly_reader_snapshot'
CAMPAIGN = 'BR-INSTANTLY-01'
INTERVAL = 60


def private_text(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
                    and info.st_mode & 0o077 == 0 and info.st_size <= 4096,
                    'configuration_pending')
            raw = stream.read(4097)
        text = raw.decode('ascii').strip()
        require(0 < len(text) <= 4096 and text.isprintable(), 'configuration_pending')
        return text
    except Exception:
        raise ExternalEmailError('configuration_pending') from None


def enabled():
    try:
        return private_text(ENABLED_FILE) == 'read-only-v1'
    except ExternalEmailError:
        return False


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def observation(row):
    require(isinstance(row, dict), 'invalid_record')
    require(row.get('organization_id') == INSTANTLY_WORKSPACE
            and row.get('campaign_id') == INSTANTLY_CAMPAIGN, 'record_scope_mismatch')
    provider_id = row.get('id')
    require(_uuid(provider_id), 'invalid_email_id')
    lead, account = email(row.get('lead')), email(row.get('eaccount'))
    recipients = row.get('to_address_email_list')
    require(isinstance(recipients, str), 'invalid_recipient')
    recipients = {email(value) for value in recipients.split(',')}
    sender = email(row.get('from_address_email'))
    subject = row.get('subject')
    require(isinstance(subject, str) and len(subject) <= 1000, 'invalid_subject')
    ue_type = row.get('ue_type')
    require(type(ue_type) is int and ue_type in (1, 2, 3, 4), 'unknown_email_type')
    if ue_type == 4:
        return None
    classification = 'provider'
    if ue_type in (1, 3):
        require(sender == account and lead in recipients, 'sender_recipient_mismatch')
        kind = 'email_sent'
    else:
        require(sender == lead and account in recipients, 'sender_recipient_mismatch')
        auto = row.get('is_auto_reply')
        # Provider-null is common. A known automatic subject is conservative
        # evidence of automation, never evidence of human interest or a CRM reply.
        if re.match(r'^\s*(resposta autom[aá]tica|respuesta autom[aá]tica|automatic reply|auto[ -]?reply|out of office)\s*[:：-]', subject, re.I):
            kind, classification = 'auto_reply_received', 'subject'
        elif type(auto) is int and auto == 1:
            kind = 'auto_reply_received'
        elif type(auto) is int and auto == 0:
            kind = 'reply_received'
        else:
            kind, classification = 'unclassified_reply', 'unknown'
    body = row.get('body')
    require(isinstance(body, dict), 'invalid_body')
    text = body.get('text')
    if not isinstance(text, str):
        html = body.get('html')
        require(isinstance(html, str) and len(html) <= 200_000, 'invalid_body')
        parser = _Text()
        parser.feed(html)
        text = ''.join(parser.parts).strip()
    require(len(text) <= 200_000, 'invalid_body')
    return lead, {'provider_id': provider_id, 'event_type': kind,
        'occurred_at': timestamp(row.get('timestamp_email')), 'subject': subject,
        'text': text[:20000], 'content_complete': not row.get('body_fallback_reason') and len(text) <= 20000,
        'classification_source': classification}


class InstantlyCampaignReadOnly:
    def __init__(self, credential_loader=None, *, request=None):
        self.loader = credential_loader or (lambda: private_text(KEY_FILE))
        self.request = request or _https_request

    def _get(self, path):
        # No arbitrary paths, methods, hosts, redirects, or provider writes.
        require(path == '/api/v2/workspaces/current' or path.startswith('/api/v2/emails?campaign_id=' + INSTANTLY_CAMPAIGN + '&'),
                'instantly_endpoint_blocked')
        token = self.loader()
        require(isinstance(token, str) and len(token) <= 4096 and token and not any(c.isspace() for c in token),
                'credential_unavailable')
        try:
            status, raw = self.request('GET', path, None,
                {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, 15)
            require(status != 429, 'instantly_rate_limited')
            require(type(status) is int and status == 200, 'instantly_http_error')
            require(isinstance(raw, bytes) and len(raw) <= 1_000_000, 'invalid_response')
            result = json.loads(raw)
            require(isinstance(result, dict), 'invalid_response')
            return result
        except ExternalEmailError:
            raise
        except Exception:
            raise ExternalEmailError('instantly_read_unavailable') from None

    def collect(self):
        require(self._get('/api/v2/workspaces/current').get('id') == INSTANTLY_WORKSPACE,
                'instantly_workspace_mismatch')
        cursor, cursors, rows, invalid = None, set(), {}, 0
        complete, limit_reached = False, False
        for page in range(1, 11):
            query = {'campaign_id': INSTANTLY_CAMPAIGN, 'limit': 50,
                     'preview_only': 'false', 'mode': 'emode_all', 'sort_order': 'desc'}
            if cursor:
                query['starting_after'] = cursor
            result = self._get('/api/v2/emails?' + urlencode(query))
            items = result.get('items')
            require(isinstance(items, list) and len(items) <= 50, 'invalid_page')
            for row in items:
                try:
                    item = observation(row)
                    if item is not None:
                        lead, event = item
                        existing = rows.get(event['provider_id'])
                        require(existing is None or existing == (lead, event), 'conflicting_email')
                        rows[event['provider_id']] = (lead, event)
                except ExternalEmailError:
                    invalid += 1
            next_cursor = result.get('next_starting_after')
            if not items or not next_cursor:
                complete = True
                break
            require(_uuid(next_cursor), 'invalid_cursor')
            cursor = next_cursor
            require(cursor not in cursors, 'pagination_repeated')
            cursors.add(cursor)
        else:
            limit_reached = True
        counts, grouped = Counter(), {}
        for lead, event in rows.values():
            counts[event['event_type']] += 1
            grouped.setdefault(lead, []).append(event)
        sent_leads = sum(any(e['event_type'] == 'email_sent' for e in events)
                         for events in grouped.values())
        return {'coverage': {'complete': complete and invalid == 0, 'pages': page,
                    'invalid_records': invalid, 'limit_reached': limit_reached},
                'metrics': {'sent_messages': counts['email_sent'], 'leads_with_sent': sent_leads,
                    'human_replies': counts['reply_received'], 'automatic_replies': counts['auto_reply_received'],
                    'unclassified_replies': counts['unclassified_reply']},
                'leads': [{'email': lead, 'events': sorted(events, key=lambda e: (e['occurred_at'], e['provider_id']))}
                          for lead, events in sorted(grouped.items())]}


class InstantlyReader:
    def __init__(self, repo, provider=None, *, enable_check=enabled, clock=None):
        self.repo, self.provider = repo, provider or InstantlyCampaignReadOnly()
        self.enable_check = enable_check
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock, self.stop_event = threading.Lock(), threading.Event()
        self.thread = None

    def refresh(self):
        if not self.enable_check() or not self.lock.acquire(blocking=False):
            return
        try:
            previous = self.repo.get_state(KIND, CAMPAIGN) or {}
            now = self.clock().isoformat()
            previous_attempt = previous.get('last_attempt_at')
            if previous_attempt and (self.clock() - datetime.fromisoformat(previous_attempt)).total_seconds() < INTERVAL:
                return
            try:
                data = self.provider.collect()
                state = {**data, 'last_attempt_at': now, 'last_success_at': self.clock().isoformat(),
                    'error_code': None, 'status': 'ok' if data['coverage']['complete'] else 'partial'}
            except Exception as exc:
                code = exc.code if isinstance(exc, ExternalEmailError) else 'instantly_read_unavailable'
                # Never discard or refresh the date of the last successful collection.
                state = {key: previous[key] for key in ('coverage', 'metrics', 'leads', 'last_success_at') if key in previous}
                state.update(last_attempt_at=now, status='error', error_code=code)
            self.repo.put_state(KIND, CAMPAIGN, state)
        finally:
            self.lock.release()

    def snapshot(self):
        active = self.enable_check()
        state = self.repo.get_state(KIND, CAMPAIGN) if active else None
        result = {'schema_version': 1, 'read_only': True, 'enabled': active,
            'status': 'pending' if active else 'disabled', 'last_attempt_at': None,
            'last_success_at': None, 'poll_interval_seconds': INTERVAL,
            'campaign_id': CAMPAIGN, 'external_campaign_id': INSTANTLY_CAMPAIGN,
            'account_id': INSTANTLY_WORKSPACE, 'coverage': {'complete': False,
                'pages': 0, 'invalid_records': 0, 'limit_reached': False},
            'metrics': None, 'leads': [], 'error_code': None,
            'crm_write_enabled': False, 'external_actions_enabled': False}
        if state:
            for key in ('status', 'last_attempt_at', 'last_success_at', 'coverage', 'metrics', 'leads', 'error_code'):
                if key in state:
                    result[key] = deepcopy(state[key])
            if result['status'] in ('ok', 'partial') and result['last_success_at'] and (
                    self.clock() - datetime.fromisoformat(result['last_success_at'])).total_seconds() > 180:
                result['status'] = 'stale'
        return result

    def start(self):
        if self.thread is not None:
            return
        def run():
            while not self.stop_event.is_set():
                try:
                    self.refresh()
                except Exception:
                    pass  # A failed cache write leaves the previous snapshot visibly stale.
                self.stop_event.wait(INTERVAL)
        self.thread = threading.Thread(target=run, name='instantly-read-only', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
