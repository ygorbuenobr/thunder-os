"""Bounded Apollo MCP sequence reader; no REST guesses or write capabilities.

Wire contract: request(method, path, body, headers, remaining_seconds) returns
(status, UTF-8 bytes, response_headers). Injected transports must enforce the
same fixed host, TLS, byte bound, deadline and no-redirect rules as _https_request.
One invocation uses at most six HTTP requests, without retries or cached IDs.
MCP 2025-11-25 uses a separate initialized notification with an empty HTTP 202
acknowledgement. Unsupported negotiated versions fail closed; no batches are sent.
Only the four read tool names observed in Apollo connector metadata are allowed.

Docs: docs.apollo.io/docs/apollo-mcp and
modelcontextprotocol.io/specification/2025-11-25/basic/{lifecycle,transports}.
Actual master-key compatibility and tool-specific credits require provider proof;
credit_cost=0 describes this reader's read-only contract, not a measured balance.
A snapshot hash is an unkeyed integrity checksum, not a cryptographic signature.
"""
import hashlib
from html.parser import HTMLParser
import http.client
import json
import math
import os
import re
import ssl
import time


_HOST = 'mcp.apollo.io'
_PATH = '/mcp'
_PROTOCOL = '2025-11-25'
_MAX_BYTES = 1_000_000
_PROFILE = 'apollo_users_api_profile'
_SEARCH = 'apollo_emailer_campaigns_search'
_SHOW = 'apollo_emailer_campaigns_show'
_STEP_CONTENT = 'apollo_emailer_campaigns_step_content'
_TOOLS = frozenset({_PROFILE, _SEARCH, _SHOW, _STEP_CONTENT})
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_VARIABLE = re.compile(r'\{\{|\}\}|\{%|%\}|\$\{|<%|%>|\[\[|\]\]')


class _ReadError(ValueError):
    pass


def _check(condition, reason):
    if not condition:
        raise _ReadError(reason)


def _id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _text(value, limit=200_000):
    _check(isinstance(value, str) and len(value) <= limit
           and not any(ord(c) < 32 and c not in '\n\r\t' or ord(c) == 127
                       or 0xD800 <= ord(c) <= 0xDFFF for c in value), 'invalid_content')
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _contains(value, secret):
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_contains(key, secret) or _contains(item, secret)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains(item, secret) for item in value)
    return False


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result, 'invalid_json')
        result[key] = value
    return result


def _json(raw):
    def invalid_constant(value):
        raise _ReadError('invalid_json')
    return json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=invalid_constant)


def _validate_wire(method, path, body):
    """Final outbound allowlist, including public transport calls before auth."""
    _check(method == 'POST' and path == _PATH, 'endpoint_blocked')
    message = body
    _check(isinstance(message, dict), 'invalid_request')
    if message == {'jsonrpc': '2.0', 'method': 'notifications/initialized'}:
        return
    _check(isinstance(message, dict) and message.get('jsonrpc') == '2.0'
           and type(message.get('id')) is int, 'invalid_request')
    if message.get('method') == 'initialize':
        _check(not isinstance(body, list) and message == {
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': _PROTOCOL, 'capabilities': {},
                'clientInfo': {'name': 'thunder-os-sequence-readonly', 'version': '1.0'}}},
            'method_blocked')
        return
    _check(message.get('method') == 'tools/call' and set(message) == {
        'jsonrpc', 'id', 'method', 'params'}, 'method_blocked')
    params = message.get('params')
    _check(isinstance(params, dict) and set(params) == {'name', 'arguments'}
           and params.get('name') in _TOOLS, 'tool_blocked')
    name, args = params['name'], params['arguments']
    _check(isinstance(args, dict), 'invalid_request')
    if name == _PROFILE:
        _check(message['id'] == 2
               and args == {'include_credit_usage': False}, 'invalid_request')
    elif name == _SEARCH:
        _check(not isinstance(body, list) and message['id'] == 3
               and set(args) == {'q_name', 'page', 'per_page'}
               and isinstance(args['q_name'], str) and len(args['q_name']) <= 500
               and args['page'] == '1' and args['per_page'] == '100', 'invalid_request')
    elif name == _SHOW:
        _check(not isinstance(body, list) and message['id'] == 4
               and set(args) == {'id'} and _id(args['id']), 'invalid_request')
    else:
        ids = args.get('contact_ids')
        _check(not isinstance(body, list) and message['id'] == 5
               and set(args) == {'id', 'contact_ids'} and _id(args['id'])
               and isinstance(ids, list) and 1 <= len(ids) <= 200
               and all(_id(value) for value in ids) and len(set(ids)) == len(ids),
               'invalid_request')


def _https_request(method, path, body, headers, timeout):
    _validate_wire(method, path, body)
    _check(not any(os.environ.get(key) for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR')),
           'tls_override_blocked')
    _check(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30,
           'timeout')
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPSConnection(_HOST, 443, timeout=timeout,
                                            context=ssl.create_default_context())
    try:
        connection.request(method, path, body=_canonical(body), headers=headers)
        remaining = deadline - time.monotonic()
        _check(remaining > 0, 'timeout')
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        _check(200 <= response.status < 300, {
            401: 'credential_rejected', 403: 'permission_denied', 429: 'rate_limited'
        }.get(response.status, 'redirect_blocked' if 300 <= response.status < 400
              else 'provider_http_error'))
        pairs = response.getheaders()
        response_headers = _headers(pairs)
        content_type = response_headers.get('content-type', '')
        message = body
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            _check(remaining > 0, 'timeout')
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, _MAX_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            _check(size <= _MAX_BYTES, 'response_too_large')
            chunks.append(chunk)
            if content_type.split(';')[0].strip().lower() == 'text/event-stream':
                accumulated = b''.join(chunks)
                # Stop at the matching response event, even on an open stream.
                ends = [accumulated.rfind(separator) + len(separator)
                        for separator in (b'\n\n', b'\r\n\r\n', b'\r\r')
                        if separator in accumulated]
                if ends:
                    prefix = accumulated[:max(ends)]
                    if _rpc_response(prefix, content_type, message['id'], allow_pending=True) is not None:
                        return response.status, prefix, response_headers
        return response.status, b''.join(chunks), response_headers
    finally:
        connection.close()


def _headers(values):
    _check(isinstance(values, (dict, list, tuple)) and len(values) <= 100, 'invalid_headers')
    pairs = values.items() if isinstance(values, dict) else values
    result, size = {}, 0
    for pair in pairs:
        _check(isinstance(pair, (list, tuple)) and len(pair) == 2, 'invalid_headers')
        key, value = pair
        _check(isinstance(key, str) and isinstance(value, str)
               and len(key) + len(value) <= 8192, 'invalid_headers')
        size += len(key) + len(value)
        _check(size <= 16384, 'invalid_headers')
        name = key.lower()
        # Cookies and other non-protocol headers may legally repeat. Never store
        # or replay them. Ambiguous protocol headers still fail closed.
        if name in ('content-type', 'mcp-session-id', 'mcp-protocol-version'):
            _check(name not in result, 'invalid_headers')
            result[name] = value
    return result


def _rpc_response(raw, content_type, expected_id, *, allow_pending=False):
    _check(isinstance(raw, bytes) and len(raw) <= _MAX_BYTES, 'response_too_large')
    decoded = raw.decode('utf-8-sig')
    mime = content_type.split(';')[0].strip().lower()
    messages = []
    if mime == 'application/json':
        value = _json(decoded)
        _check(isinstance(value, dict), 'invalid_rpc')
        messages.append(value)
    elif mime == 'text/event-stream':
        # SSE data fields form a JSON value. Never extract JSON with regex or eval.
        lines, event = [], ''
        for line in decoded.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
            if not line:
                if lines and any(lines):
                    _check(event in ('', 'message'), 'invalid_sse')
                    value = _json('\n'.join(lines))
                    _check(isinstance(value, dict), 'invalid_rpc')
                    messages.append(value)
                lines, event = [], ''
            elif not line.startswith(':'):
                field, separator, value = line.partition(':')
                value = value[1:] if value.startswith(' ') else value
                if field == 'data': lines.append(value)
                elif field == 'event': event = value
                elif field not in ('id', 'retry'): raise _ReadError('invalid_sse')
            _check(len(messages) <= 20, 'invalid_sse')
        _check(not lines, 'incomplete_sse')
    else:
        raise _ReadError('content_type_blocked')
    results = []
    for message in messages:
        _check(isinstance(message, dict) and message.get('jsonrpc') == '2.0', 'invalid_rpc')
        if 'method' in message:
            _check('id' not in message and message['method'] == 'notifications/message',
                   'server_request_blocked')
            continue
        _check(type(message.get('id')) is int and message['id'] == expected_id
               and 'error' not in message and 'result' in message, 'invalid_rpc')
        results.append(message['result'])
    if allow_pending and not results:
        return None
    _check(len(results) == 1 and isinstance(results[0], dict), 'invalid_rpc')
    return results[0]


def _tool_payload(result):
    _check(result.get('isError', False) is False, 'tool_error')
    structured = result.get('structuredContent')
    texts = []
    for block in result.get('content', []):
        _check(isinstance(block, dict) and block.get('type') == 'text'
               and isinstance(block.get('text'), str), 'invalid_tool_result')
        texts.append(_json(block['text']))
    _check(len(texts) <= 1 and (structured is not None or len(texts) == 1),
           'invalid_tool_result')
    if structured is not None and texts:
        _check(structured == texts[0], 'ambiguous_tool_result')
    payload = structured if structured is not None else texts[0]
    _check(isinstance(payload, dict) and not set(payload) & {
        'error', 'errors', 'error_code', 'error_details'}, 'invalid_tool_result')
    # A credit report has no role in a zero-credit content snapshot. Unknown costs
    # are not silently interpreted as a free result.
    _check('mcp_credits' not in payload and 'mcp_credits' not in result, 'credit_report_unexpected')
    return payload


def _paragraphs(value):
    value = _text(value).replace('\r\n', '\n').replace('\r', '\n').replace('\xa0', ' ')
    lines = [re.sub(r'[^\S\n]+', ' ', line).strip() for line in value.split('\n')]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.complex = False
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in ('p', 'div', 'br', 'b', 'strong', 'i', 'em', 'u', 'span') or attrs:
            self.complex = True
        if tag in ('p', 'div'): self.parts.append('\n\n')
        elif tag == 'br': self.parts.append('\n')
        if tag not in ('br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.complex = True
        else:
            self.stack.pop()
        if tag in ('p', 'div'): self.parts.append('\n\n')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_data(self, data):
        self.parts.append(data)

    def handle_comment(self, data):
        self.complex = True

    def unknown_decl(self, data):
        self.complex = True


def _html_text(value):
    parser = _HTMLText()
    parser.feed(_text(value))
    parser.close()
    return _paragraphs(''.join(parser.parts)), parser.complex or bool(parser.stack)


def _permission(campaign, sequence_id, owner_id):
    _check(isinstance(campaign, dict) and campaign.get('id') == sequence_id
           and campaign.get('user_id') == owner_id, 'sequence_identity_mismatch')
    permission = campaign.get('sharing_permission')
    # is_owner describes the authenticated actor, not whether the server permits
    # this read. A successful scoped read may legitimately belong to another user.
    _check(isinstance(permission, dict) and type(permission.get('is_owner')) is bool
           and permission.get('owner_id') == owner_id
           and permission.get('object_id') == sequence_id
           and permission.get('object_type') == 'EmailerCampaign'
           and permission.get('visibility') in ('restricted', 'everyone'),
           'sequence_permission_unverified')


def _index(rows):
    _check(isinstance(rows, list) and len(rows) <= 100, 'invalid_detail')
    result = {}
    for row in rows:
        _check(isinstance(row, dict) and _id(row.get('id')) and row['id'] not in result,
               'invalid_detail')
        result[row['id']] = row
    return result


def _normalize(data, sequence_id, account_id, owner_id):
    campaign = data.get('emailer_campaign')
    _permission(campaign, sequence_id, owner_id)
    steps = _index(data.get('emailer_steps'))
    touches = _index(data.get('emailer_touches'))
    templates = _index(data.get('emailer_templates'))
    _check(steps and type(campaign.get('num_steps')) is int
           and campaign['num_steps'] == len(steps), 'invalid_step_count')
    _check(isinstance(data.get('attachments'), list), 'attachments_unverified')
    for row in [campaign, *steps.values(), *touches.values(), *templates.values()]:
        if 'team_id' in row:
            _check(row['team_id'] == account_id, 'team_mismatch')
    positions, used_templates, grouped = set(), set(), {key: [] for key in steps}
    for step in steps.values():
        position = step.get('position')
        _check(step.get('emailer_campaign_id') == sequence_id
               and type(position) is int and 1 <= position <= 100 and position not in positions,
               'invalid_step')
        positions.add(position)
    _check(positions == set(range(1, len(steps) + 1)), 'invalid_step_order')
    for touch in touches.values():
        step_id, template_id = touch.get('emailer_step_id'), touch.get('emailer_template_id')
        _check(step_id in steps and template_id in templates, 'invalid_join')
        grouped[step_id].append(touch)
        used_templates.add(template_id)
    _check(used_templates == set(templates), 'orphan_template')
    result = []
    for step in sorted(steps.values(), key=lambda row: row['position']):
        variants = grouped[step['id']]
        _check(variants, 'missing_touch')
        raw_type = _text(step.get('type'), 80)
        wait_mode, wait = step.get('wait_mode'), step.get('wait_time')
        _check(isinstance(wait_mode, str) and len(wait_mode) <= 80
               and type(wait) in (int, float) and math.isfinite(wait) and 0 <= wait <= 36500,
               'invalid_interval')
        factor = {'day': 24, 'days': 24, 'hour': 1, 'hours': 1}.get(wait_mode)
        flags = {'raw_step_type': raw_type, 'raw_wait_mode': wait_mode,
                 'dynamic': False, 'include_signature': False, 'ai': False,
                 'unresolved_variables': False, 'multiple_touches': len(variants) != 1,
                 'attachments': bool(data['attachments']), 'unknown_safety': False,
                 'unapproved': False, 'body_mismatch': False, 'complex_html': False,
                 'unsupported_step': raw_type not in ('auto_email', 'manual_email'),
                 'unknown_interval': factor is None}
        snapshots = []
        for touch in sorted(variants, key=lambda row: row['id']):
            template = templates[touch['emailer_template_id']]
            if 'user_id' in template:
                _check(template['user_id'] == owner_id, 'template_owner_mismatch')
            prompt = template.get('prompt_template')
            if prompt is not None:
                _check(isinstance(prompt, dict), 'invalid_prompt')
                if 'team_id' in prompt:
                    _check(prompt['team_id'] == account_id, 'team_mismatch')
            subject = _text(template.get('subject'), 10000)
            text, html = template.get('body_text'), template.get('body_html')
            _check(text is None or isinstance(text, str), 'invalid_content')
            _check(html is None or isinstance(html, str), 'invalid_content')
            _check(text is not None or html is not None, 'missing_body')
            plain = _paragraphs(text) if text is not None else None
            rendered, complex_html = _html_text(html) if html is not None else (None, False)
            content = plain if plain is not None else rendered
            flags['body_mismatch'] |= plain is not None and rendered is not None and plain != rendered
            flags['complex_html'] |= complex_html
            flags['unknown_safety'] |= (type(touch.get('include_signature')) is not bool
                or type(template.get('has_ai_variables')) is not bool
                or touch.get('template_type') not in ('email_template', 'prompt_template')
                or not content)
            flags['include_signature'] |= touch.get('include_signature') is not False
            flags['ai'] |= (template.get('has_ai_variables') is True or prompt is not None
                            or bool(template.get('prompt_template_id'))
                            or touch.get('template_type') == 'prompt_template'
                            or bool(touch.get('generation_options')))
            flags['unresolved_variables'] |= bool(_VARIABLE.search(subject + '\n' + (text or '')
                                                                   + '\n' + (html or '')))
            flags['dynamic'] |= flags['ai'] or flags['unresolved_variables']
            flags['unapproved'] |= touch.get('status') != 'approved'
            for row in (touch, template):
                for key in ('attachments', 'attachment_ids'):
                    if key in row:
                        _check(isinstance(row[key], list), 'invalid_attachments')
                        flags['attachments'] |= bool(row[key])
            # Retain raw source strings in the hash input; normalized text never
            # substitutes for prompts, variants, original HTML or signature policy.
            snapshots.append({'touch_id': touch['id'], 'template_id': template['id'],
                'subject': subject, 'content': content, 'body_text': text, 'body_html': html,
                'status': touch.get('status'), 'template_type': touch.get('template_type'),
                'include_signature': touch.get('include_signature'),
                'has_ai_variables': template.get('has_ai_variables'),
                'prompt_template': prompt, 'generation_options': touch.get('generation_options'),
                'generation_tone': touch.get('generation_tone'),
                'system_instructions': {'touch': touch.get('system_instructions'), 'template': template.get('system_instructions')},
                'instructions': {'touch': touch.get('instructions'), 'template': template.get('instructions')},
                'tone': {'touch': touch.get('tone'), 'template': template.get('tone')},
                'attachments': [row.get(key, []) for row in (touch, template)
                                for key in ('attachments', 'attachment_ids')]})
        flags['static_safe'] = not any(value for key, value in flags.items()
                                      if key not in ('raw_step_type', 'raw_wait_mode'))
        result.append({'id': step['id'], 'order': step['position'],
            'channel': 'email' if raw_type in ('auto_email', 'manual_email') else raw_type,
            'subject': snapshots[0]['subject'] if len(snapshots) == 1 else '',
            'content': snapshots[0]['content'] if len(snapshots) == 1 else '',
            'interval_hours': wait * factor if factor is not None else None,
            'flags': flags, 'variants': snapshots})
    return result


def _recipients(rows, account_id):
    """Internal preflight proof from main's fresh REST reads, never HTTP input.

    null contact_id is main's proof that a complete literal-email lookup found
    no saved contact. It does not mean a failed or incomplete search was empty.
    Hash schema: canonical JSON of this normalized list, sorted by email.
    """
    if rows is None:
        return None
    _check(isinstance(rows, list) and len(rows) <= 200, 'invalid_recipients')
    normalized, emails, ids = [], set(), set()
    for row in rows:
        _check(isinstance(row, dict) and set(row) == {
            'email', 'contact_id', 'account_id', 'lookup_complete'}
            and row.get('account_id') == account_id and row.get('lookup_complete') is True,
            'recipient_provenance_unverified')
        address, contact_id = row.get('email'), row.get('contact_id')
        _check(isinstance(address, str) and len(address) <= 254
               and re.fullmatch(r'[^@\s*<>]+@[^@\s*<>]+\.[^@\s*<>]+', address)
               and (contact_id is None or _id(contact_id)), 'invalid_recipients')
        address = address.lower()
        _check(address not in emails and (contact_id is None or contact_id not in ids),
               'duplicate_recipient_binding')
        emails.add(address)
        if contact_id is not None:
            ids.add(contact_id)
        normalized.append({'email': address, 'contact_id': contact_id,
                           'account_id': account_id, 'lookup_complete': True})
    return sorted(normalized, key=lambda row: row['email'])


def _override_coverage(recipients, steps, existing_verified=False):
    """Only a successful scoped empty step_contact_contents list proves absence.

    The tool contract says an absent contact/step pair uses its shared template.
    Nonempty response schemas are deliberately unsupported; no fields are guessed.
    """
    return [{'email': row['email'], 'contact_id': row['contact_id'], 'steps': [
        {'step_id': step['id'], 'state': 'no_override'
         if row['contact_id'] is None or existing_verified else 'unknown',
         'subject': None, 'content': None}
        for step in steps]} for row in (recipients or [])]


class ApolloSequenceReadonly:
    """No key discovery, no inferred master privileges, no remote instructions.

    sequence_detail returns status/complete/credit_cost/items/source, plus a
    canonical snapshot and SHA-256. complete proves structural read coverage;
    source.static_safe and every step.flags.static_safe must ALSO be true for
    static shared-template parity. Main alone supplies fresh validated recipient
    bindings. The authenticated profile actor may differ from the configured
    sequence owner; the exact team and sequence owner remain independently bound.
    Only the exact empty override-list shape is supported for existing
    contacts. Nonempty/unknown shapes fail closed. This reader never asserts final parity.
    """
    def __init__(self, loader, *, request=None, timeout=15):
        _check(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 30,
               'invalid_timeout')
        self._loader, self._request, self.timeout = loader, request or _https_request, timeout

    def sequence_detail(self, sequence_id, account_id, owner_id, query='', recipients=None):
        source = {'provider': 'apollo', 'transport': 'mcp', 'endpoint': 'https://mcp.apollo.io/mcp',
                  'protocol_version': _PROTOCOL, 'account_id': None, 'owner_id': None,
                  'sequence_id': None, 'identity_verified': False, 'request_count': 0,
                  'static_safe': False, 'contact_overrides_verified': False,
                  'contact_overrides_reason': 'recipients_not_supplied',
                  'recipient_binding_hash': None,
                  'credit_cost_basis': 'read_tool_allowlist_not_measured'}
        try:
            _check(all(_id(value) for value in (sequence_id, account_id, owner_id)), 'invalid_identity')
            _text(query, 500)
            bindings = _recipients(recipients, account_id)
            if bindings is not None:
                source['recipient_binding_hash'] = hashlib.sha256(_canonical(bindings)).hexdigest()
            _check(callable(self._loader), 'credential_unavailable')
            deadline = time.monotonic() + self.timeout
            try:
                token = self._loader()
            except Exception:
                raise _ReadError('credential_unavailable') from None
            _check(isinstance(token, str) and 0 < len(token) <= 4096 and token.isascii()
                   and all(33 <= ord(c) <= 126 for c in token), 'credential_unavailable')
            _check(not any(token in value for value in (sequence_id, account_id, owner_id, query)),
                   'invalid_identity')
            source.update(account_id=account_id, owner_id=owner_id, sequence_id=sequence_id)
            session = None

            def call(message, expected_id):
                nonlocal session
                _validate_wire('POST', _PATH, message)
                _check(source['request_count'] < 6, 'request_limit')
                remaining = deadline - time.monotonic()
                _check(remaining > 0, 'timeout')
                headers = {'X-Api-Key': token, 'Content-Type': 'application/json',
                           'Accept': 'application/json, text/event-stream'}
                if expected_id != 1:
                    headers['MCP-Protocol-Version'] = _PROTOCOL
                if session is not None:
                    headers['Mcp-Session-Id'] = session
                source['request_count'] += 1
                status, raw, response_headers = self._request('POST', _PATH, message, headers, remaining)
                _check(time.monotonic() < deadline, 'timeout')
                _check(type(status) is int and status == (202 if expected_id is None else 200), {
                    401: 'credential_rejected', 403: 'permission_denied', 429: 'rate_limited'
                }.get(status, 'redirect_blocked' if type(status) is int and 300 <= status < 400
                      else 'provider_http_error'))
                response_headers = _headers(response_headers)
                if 'mcp-session-id' in response_headers:
                    observed = response_headers['mcp-session-id']
                    _check(0 < len(observed) <= 256 and observed.isascii()
                           and all(33 <= ord(c) <= 126 for c in observed)
                           and (session is None or session == observed), 'invalid_session')
                    session = observed
                if expected_id is None:
                    _check(isinstance(raw, bytes) and raw == b'', 'invalid_notification_ack')
                    return None
                result = _rpc_response(raw, response_headers.get('content-type', ''), expected_id)
                return result

            initialized = call({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': _PROTOCOL, 'capabilities': {},
                'clientInfo': {'name': 'thunder-os-sequence-readonly', 'version': '1.0'}}}, 1)
            _check(initialized.get('protocolVersion') == _PROTOCOL
                   and isinstance(initialized.get('capabilities'), dict)
                   and isinstance(initialized['capabilities'].get('tools'), dict),
                   'protocol_unsupported')
            call({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, None)

            def tool(name, arguments, number):
                _check(name in _TOOLS, 'tool_blocked')
                message = {'jsonrpc': '2.0', 'id': number, 'method': 'tools/call',
                           'params': {'name': name, 'arguments': arguments}}
                return _tool_payload(call(message, number))

            profile = tool(_PROFILE, {'include_credit_usage': False}, 2)
            for field, key in (('observed_profile_owner_id', 'id'),
                               ('observed_profile_account_id', 'team_id')):
                observed = profile.get(key)
                if _id(observed) and token not in observed:
                    source[field] = observed
            _check(_id(profile.get('id')) and token not in profile['id']
                   and profile.get('team_id') == account_id,
                   'profile_identity_mismatch')
            source['identity_verified'] = True
            source['authenticated_user_id'] = profile['id']
            found = tool(_SEARCH, {'q_name': query, 'page': '1', 'per_page': '100'}, 3)
            rows, pagination = found.get('emailer_campaigns'), found.get('pagination')
            _check(isinstance(rows, list) and len(rows) <= 100 and isinstance(pagination, dict)
                   and type(pagination.get('total_entries')) is int
                   and pagination['total_entries'] == len(rows)
                   and type(pagination.get('page')) is int and pagination['page'] == 1
                   and type(pagination.get('per_page')) is int and pagination['per_page'] == 100
                   and type(pagination.get('total_pages')) is int
                   and pagination['total_pages'] == (1 if rows else 0), 'search_incomplete')
            _index(rows)
            matches = [row for row in rows if row.get('id') == sequence_id]
            _check(len(matches) == 1, 'sequence_not_in_current_search')
            _permission(matches[0], sequence_id, owner_id)
            if 'team_id' in matches[0]:
                _check(matches[0]['team_id'] == account_id, 'team_mismatch')
            data = tool(_SHOW, {'id': matches[0]['id']}, 4)
            items = _normalize(data, sequence_id, account_id, owner_id)
            ids = [row['contact_id'] for row in (bindings or []) if row['contact_id'] is not None]
            override_payload = None
            if ids:
                source['contact_overrides_reason'] = 'provider_shape_unverified'
                override_payload = tool(_STEP_CONTENT, {'id': sequence_id, 'contact_ids': ids}, 5)
                _check(not _contains(override_payload, token), 'invalid_snapshot')
                _check(set(override_payload) == {'step_contact_contents'}
                       and type(override_payload['step_contact_contents']) is list
                       and not override_payload['step_contact_contents'],
                       'contact_override_shape_unverified')
            coverage = _override_coverage(bindings, items, existing_verified=bool(ids))
            overrides_verified = bindings is not None
            override_reason = ('scoped_empty_step_contact_contents' if ids else
                               'complete_lookup_absence' if bindings else
                               'empty_cohort' if bindings == [] else 'recipients_not_supplied')
            snapshot = {'schema_version': 1, 'provider': 'apollo', 'account_id': account_id,
                        'owner_id': owner_id, 'sequence_id': sequence_id,
                        'active': data['emailer_campaign'].get('active'),
                        'sharing_permission': data['emailer_campaign']['sharing_permission'],
                        'attachments': data['attachments'], 'steps': items,
                        'recipients': bindings, 'recipient_overrides': coverage,
                        'override_response_hash': hashlib.sha256(_canonical(override_payload)).hexdigest()
                        if override_payload is not None else None,
                        'contact_overrides_verified': overrides_verified}
            encoded = _canonical(snapshot)
            _check(not _contains(snapshot, token) and len(encoded) <= _MAX_BYTES, 'invalid_snapshot')
            source['static_safe'] = all(step['flags']['static_safe'] for step in items)
            source.update(contact_overrides_verified=overrides_verified,
                          contact_overrides_reason=override_reason)
            _check(time.monotonic() < deadline, 'timeout')
            return {'status': 'ok', 'complete': True, 'credit_cost': 0, 'items': items,
                    'source': source, 'snapshot': snapshot,
                    'recipient_overrides': coverage,
                    'snapshot_hash': hashlib.sha256(encoded).hexdigest(),
                    'snapshot_hash_algorithm': 'sha256'}
        except _ReadError as error:
            reason = str(error)
        except TimeoutError:
            reason = 'timeout'
        except Exception:
            reason = 'provider_read_unavailable'
        source['static_safe'] = False
        source['contact_overrides_verified'] = False
        return {'status': 'error', 'complete': False, 'credit_cost': 0,
                'items': [], 'source': source, 'reason': reason}
