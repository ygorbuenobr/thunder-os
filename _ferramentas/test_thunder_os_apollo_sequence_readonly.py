"""Isolated contract tests: synthetic MCP data, never a provider or real key."""
import copy
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from thunder_os.apollo_sequence_readonly import (ApolloSequenceReadonly, _https_request,
                                               _headers, _rpc_response, _validate_wire)


KEY = 'synthetic-private-key'
PROFILE = 'apollo_users_api_profile'
SEARCH = 'apollo_emailer_campaigns_search'
SHOW = 'apollo_emailer_campaigns_show'
STEP_CONTENT = 'apollo_emailer_campaigns_step_content'


def detail():
    return {
        'emailer_campaign': {'id': 'seq-1', 'user_id': 'owner-1', 'num_steps': 1,
                             'active': False, 'sharing_permission': {
                                 'visibility': 'restricted', 'is_owner': True,
                                 'owner_id': 'owner-1', 'object_id': 'seq-1',
                                 'object_type': 'EmailerCampaign'}},
        'emailer_steps': [{'id': 'step-1', 'emailer_campaign_id': 'seq-1',
                           'position': 1, 'type': 'auto_email', 'wait_time': 2,
                           'wait_mode': 'days'}],
        'emailer_touches': [{'id': 'touch-1', 'emailer_step_id': 'step-1',
                             'emailer_template_id': 'template-1', 'status': 'approved',
                             'template_type': 'email_template', 'include_signature': False}],
        'emailer_templates': [{'id': 'template-1', 'user_id': 'owner-1',
                               'subject': 'Hello', 'body_text': 'First paragraph.\n\nSecond & last.',
                               'body_html': '<p>First paragraph.</p><p>Second &amp; last.</p>',
                               'has_ai_variables': False}],
        'attachments': [],
    }


def search():
    return {'pagination': {'page': 1, 'per_page': 100, 'total_entries': 1, 'total_pages': 1},
            'emailer_campaigns': [copy.deepcopy(detail()['emailer_campaign'])]}


def tool_result(data):
    return {'isError': False, 'content': [{'type': 'text', 'text': json.dumps(data)}]}


class Wire:
    def __init__(self, show=None, found=None, profile=None, sse=False, overrides=None):
        self.calls = []
        self.show = detail() if show is None else show
        self.found = search() if found is None else found
        self.profile = {'id': 'owner-1', 'team_id': 'team-1'} if profile is None else profile
        self.sse = sse
        self.responses = []
        self.overrides = {'step_contact_contents': []} if overrides is None else overrides

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, copy.deepcopy(body), dict(headers), timeout))
        if self.responses:
            return self.responses.pop(0)
        request = body
        if request['method'] == 'notifications/initialized':
            return 202, b'', {}
        if request['method'] == 'initialize':
            value = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                     'serverInfo': {'name': 'synthetic', 'version': '1'}}
        else:
            value = tool_result({PROFILE: self.profile, SEARCH: self.found, SHOW: self.show,
                                 STEP_CONTENT: self.overrides}[
                request['params']['name']])
        response = {'jsonrpc': '2.0', 'id': request['id'], 'result': value}
        raw = json.dumps(response).encode()
        if self.sse:
            raw = b': heartbeat\n\nevent: message\ndata: ' + raw + b'\n\n'
        return 200, raw, {'Content-Type': 'text/event-stream' if self.sse else 'application/json',
                          'Mcp-Session-Id': 'private-session'}


class ApolloSequenceReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.net = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.net.start()
        self.addCleanup(self.net.stop)

    def run_reader(self, wire=None, *, recipients=None, **kwargs):
        self.wire = wire or Wire()
        return ApolloSequenceReadonly(lambda: KEY, request=self.wire, **kwargs).sequence_detail(
            'seq-1', 'team-1', 'owner-1', recipients=recipients)

    def test_static_detail_normalization_identity_and_hash(self):
        result = self.run_reader()
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['complete'])
        self.assertEqual(result['credit_cost'], 0)
        step = result['items'][0]
        self.assertEqual({key: step[key] for key in (
            'id', 'order', 'channel', 'subject', 'content', 'interval_hours')},
            {'id': 'step-1', 'order': 1, 'channel': 'email', 'subject': 'Hello',
             'content': 'First paragraph.\n\nSecond & last.', 'interval_hours': 48})
        self.assertTrue(step['flags']['static_safe'])
        self.assertTrue(result['source']['static_safe'])
        self.assertEqual(result['source']['account_id'], 'team-1')
        self.assertEqual(result['source']['owner_id'], 'owner-1')
        self.assertEqual(result['source']['sequence_id'], 'seq-1')
        self.assertTrue(result['source']['identity_verified'])
        canonical = json.dumps(result['snapshot'], sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False).encode()
        self.assertEqual(result['snapshot_hash'], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(len(result['snapshot_hash']), 64)
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn('private-session', json.dumps(result))

    def test_five_fixed_posts_exact_tool_whitelist_and_no_paid_capabilities(self):
        self.run_reader()
        calls = self.wire.calls
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(c[0:2] == ('POST', '/mcp') for c in calls))
        self.assertEqual(calls[0][2]['method'], 'initialize')
        self.assertEqual(calls[0][2]['params']['capabilities'], {})
        self.assertEqual(calls[1][2], {'jsonrpc': '2.0',
                                        'method': 'notifications/initialized'})
        requests = [calls[2][2], calls[3][2], calls[4][2]]
        self.assertEqual([r['params']['name'] for r in requests], [PROFILE, SEARCH, SHOW])
        self.assertEqual(requests[0]['params']['arguments'], {'include_credit_usage': False})
        self.assertEqual(requests[1]['params']['arguments'],
                         {'q_name': '', 'page': '1', 'per_page': '100'})
        self.assertEqual(requests[2]['params']['arguments'], {'id': 'seq-1'})
        self.assertTrue(all(c[3]['X-Api-Key'] == KEY for c in calls))
        self.assertEqual(calls[1][3]['Mcp-Session-Id'], 'private-session')
        self.assertEqual(calls[0][2]['params']['protocolVersion'], '2025-11-25')
        self.assertTrue(all(c[3]['MCP-Protocol-Version'] == '2025-11-25' for c in calls[1:]))
        self.assertTrue(all(isinstance(c[2], dict) for c in calls))
        self.assertTrue(all(0 < c[4] <= 15 for c in calls))

    def test_profile_mismatch_stops_before_search_or_show(self):
        for profile in ({'id': 'bad/id', 'team_id': 'team-1'},
                        {'id': KEY, 'team_id': 'team-1'},
                        {'id': 'owner-1', 'team_id': 'wrong'}, {},
                        {'id': 'owner-1', 'team_id': 'team-1', 'error_details': {}}):
            result = self.run_reader(Wire(profile=profile))
            self.assertEqual(result['status'], 'error')
            self.assertFalse(result['complete'])
            self.assertEqual(len(self.wire.calls), 3)
            self.assertEqual(result['items'], [])
            if profile.get('team_id') == 'wrong':
                self.assertEqual(result['source']['observed_profile_account_id'], 'wrong')

    def test_same_team_different_authenticated_actor_can_read_configured_sequence_owner(self):
        show, found = detail(), search()
        for campaign in (show['emailer_campaign'], found['emailer_campaigns'][0]):
            campaign['sharing_permission']['is_owner'] = False
        result = self.run_reader(Wire(show=show, found=found,
            profile={'id': 'admin-actor', 'team_id': 'team-1'}))
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['complete'])
        self.assertTrue(result['source']['identity_verified'])
        self.assertEqual(result['source']['authenticated_user_id'], 'admin-actor')
        self.assertEqual(result['source']['observed_profile_owner_id'], 'admin-actor')
        self.assertEqual(result['source']['owner_id'], 'owner-1')
        self.assertEqual(result['snapshot']['owner_id'], 'owner-1')
        self.assertEqual(len(self.wire.calls), 5)

    def test_different_actor_never_substitutes_for_sequence_owner_or_team(self):
        for mutation in ('profile_team', 'search_owner', 'show_owner', 'show_team'):
            show, found = detail(), search()
            profile = {'id': 'admin-actor', 'team_id': 'team-1'}
            if mutation == 'profile_team': profile['team_id'] = 'other-team'
            if mutation == 'search_owner': found['emailer_campaigns'][0]['user_id'] = 'admin-actor'
            if mutation == 'show_owner': show['emailer_campaign']['user_id'] = 'admin-actor'
            if mutation == 'show_team': show['emailer_campaign']['team_id'] = 'other-team'
            result = self.run_reader(Wire(profile=profile, show=show, found=found))
            self.assertEqual(result['status'], 'error', mutation)
            self.assertFalse(result['complete'])
            self.assertEqual(result['items'], [])
            self.assertEqual(result['source']['owner_id'], 'owner-1')
            self.assertEqual(len(self.wire.calls), 3 if mutation == 'profile_team' else
                             4 if mutation == 'search_owner' else 5)

    def test_search_missing_duplicate_partial_or_permission_unknown_blocks_show(self):
        cases = []
        for mutation in ('missing', 'duplicate', 'partial', 'owner', 'permission', 'object'):
            found = search()
            if mutation == 'missing': found['emailer_campaigns'] = []
            if mutation == 'duplicate': found['emailer_campaigns'] *= 2
            if mutation == 'partial': found['pagination']['total_pages'] = 2
            if mutation == 'owner': found['emailer_campaigns'][0]['user_id'] = 'other'
            if mutation == 'permission': found['emailer_campaigns'][0].pop('sharing_permission')
            if mutation == 'object': found['emailer_campaigns'][0]['sharing_permission']['object_id'] = 'other'
            cases.append(found)
        for found in cases:
            result = self.run_reader(Wire(found=found))
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(self.wire.calls), 4)

    def test_show_identity_and_broken_joins_never_return_partial_steps(self):
        for mutation in ('campaign', 'owner', 'permission', 'team', 'step', 'touch',
                         'template', 'duplicate', 'position', 'count', 'no_touch'):
            show = detail()
            if mutation == 'campaign': show['emailer_campaign']['id'] = 'other'
            if mutation == 'owner': show['emailer_campaign']['user_id'] = 'other'
            if mutation == 'permission': show['emailer_campaign']['sharing_permission'] = {}
            if mutation == 'team': show['emailer_templates'][0]['team_id'] = 'other'
            if mutation == 'step': show['emailer_steps'][0]['emailer_campaign_id'] = 'other'
            if mutation == 'touch': show['emailer_touches'][0]['emailer_step_id'] = 'other'
            if mutation == 'template': show['emailer_touches'][0]['emailer_template_id'] = 'other'
            if mutation == 'duplicate': show['emailer_templates'] *= 2
            if mutation == 'position': show['emailer_steps'][0]['position'] = True
            if mutation == 'count': show['emailer_campaign']['num_steps'] = 2
            if mutation == 'no_touch': show['emailer_touches'] = []
            result = self.run_reader(Wire(show=show))
            self.assertEqual(result['status'], 'error', mutation)
            self.assertFalse(result['complete'])
            self.assertEqual(result['items'], [])

    def test_safety_flags_fail_closed_without_rewriting_ai_as_static(self):
        for mutation, flag in (('ai', 'ai'), ('prompt', 'dynamic'), ('signature', 'include_signature'),
                               ('variables', 'unresolved_variables'), ('attachment', 'attachments'),
                               ('multi', 'multiple_touches'), ('unknown', 'unknown_safety'),
                               ('draft', 'unapproved'), ('mismatch', 'body_mismatch')):
            show = detail()
            if mutation == 'ai': show['emailer_templates'][0]['has_ai_variables'] = True
            if mutation == 'prompt': show['emailer_touches'][0]['template_type'] = 'prompt_template'
            if mutation == 'signature': show['emailer_touches'][0]['include_signature'] = True
            if mutation == 'variables': show['emailer_templates'][0]['subject'] = 'Hi {{first_name}}'
            if mutation == 'attachment': show['attachments'] = [{'id': 'file-1'}]
            if mutation == 'multi':
                touch = copy.deepcopy(show['emailer_touches'][0]); touch['id'] = 'touch-2'
                show['emailer_touches'].append(touch)
            if mutation == 'unknown': show['emailer_templates'][0].pop('has_ai_variables')
            if mutation == 'draft': show['emailer_touches'][0]['status'] = 'draft'
            if mutation == 'mismatch': show['emailer_templates'][0]['body_text'] = 'Different'
            result = self.run_reader(Wire(show=show))
            self.assertEqual(result['status'], 'ok', mutation)
            self.assertTrue(result['complete'])
            self.assertTrue(result['items'][0]['flags'][flag], mutation)
            self.assertFalse(result['items'][0]['flags']['static_safe'], mutation)
            self.assertFalse(result['source']['static_safe'], mutation)
            if mutation == 'multi': self.assertEqual(result['items'][0]['content'], '')

    def test_unknown_wait_mode_or_non_email_step_is_not_static_safe(self):
        for key, value in (('wait_mode', 'business_days'), ('type', 'call')):
            show = detail(); show['emailer_steps'][0][key] = value
            result = self.run_reader(Wire(show=show))
            self.assertEqual(result['status'], 'ok')
            self.assertFalse(result['source']['static_safe'])
            if key == 'wait_mode': self.assertIsNone(result['items'][0]['interval_hours'])
            else: self.assertEqual(result['items'][0]['flags']['raw_step_type'], 'call')

    def test_sse_parses_json_data_and_structured_content_without_regex_extraction(self):
        self.assertEqual(self.run_reader(Wire(sse=True))['status'], 'ok')
        wire = Wire()
        original = wire.__call__
        def structured(*args):
            status, raw, headers = original(*args)
            if status == 202: return status, raw, headers
            value = json.loads(raw)
            rows = value if isinstance(value, list) else [value]
            for row in rows:
                result = row['result']
                if 'content' in result:
                    result['structuredContent'] = json.loads(result['content'][0]['text'])
            return status, json.dumps(value).encode(), headers
        self.assertEqual(self.run_reader(structured)['status'], 'ok')

    def test_protocol_errors_redirects_and_oversized_payloads_are_redacted(self):
        invalid = [
            (302, b'', {'Location': 'https://evil.invalid'}),
            (403, KEY.encode(), {'Content-Type': 'application/json'}),
            (429, b'{}', {'Content-Type': 'application/json'}),
            (200, b'x' * 1_000_001, {'Content-Type': 'application/json'}),
            (200, b'{"jsonrpc":"2.0","id":1,"error":{"message":"' + KEY.encode() + b'"}}', {'Content-Type': 'application/json'}),
            (200, b'{"id":1,"id":1}', {'Content-Type': 'application/json'}),
            (200, b'{"jsonrpc":"2.0","id":99,"result":{}}', {'Content-Type': 'application/json'}),
            (200, b'data: [not json]\n\n', {'Content-Type': 'text/event-stream'}),
            (200, b'{}', {'Content-Type': 'text/html'}),
        ]
        for response in invalid:
            wire = Wire(); wire.responses = [response]
            result = self.run_reader(wire)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(wire.calls), 1)
            self.assertNotIn(KEY, json.dumps(result))

    def test_unsupported_negotiation_tool_errors_and_unknown_notifications_stop(self):
        for result in (
            {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}},
            {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}},
            {'protocolVersion': '2099-01-01', 'capabilities': {'tools': {}}},
            {'protocolVersion': '2025-11-25', 'capabilities': {}},
        ):
            wire = Wire(); wire.responses = [(200, json.dumps({'jsonrpc': '2.0', 'id': 1,
                'result': result}).encode(), {'Content-Type': 'application/json'})]
            self.assertEqual(self.run_reader(wire)['status'], 'error')
            self.assertEqual(len(wire.calls), 1)
        for result in ({'isError': True, 'content': [{'type': 'text', 'text': KEY}]},
                       {'content': [{'type': 'text', 'text': '{}'}], 'structuredContent': {'x': 1}}):
            wire = Wire()
            init = wire('POST', '/mcp', {'id': 1, 'method': 'initialize'}, {}, 15)
            wire.calls = []; wire.responses = [init, (202, b'', {}), (200, json.dumps(
                {'jsonrpc': '2.0', 'id': 2, 'result': result}).encode(),
                {'Content-Type': 'application/json'})]
            result = self.run_reader(wire)
            self.assertEqual(result['status'], 'error')
            self.assertNotIn(KEY, json.dumps(result))

    def test_only_initialized_notification_accepts_empty_202_ack(self):
        good = Wire()
        result = self.run_reader(good)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(good.calls), 5)
        for status, raw in ((200, b''), (204, b''), (202, b'{}'), (202, '')):
            wire = Wire()
            init = wire('POST', '/mcp', good.calls[0][2], {}, 15)
            wire.calls = []
            wire.responses = [init, (status, raw, {})]
            result = self.run_reader(wire)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(wire.calls), 2)
            self.assertEqual(result['items'], [])
        for target in ('initialize', 'profile'):
            wire = Wire()
            init = wire('POST', '/mcp', good.calls[0][2], {}, 15)
            wire.calls = []
            wire.responses = ([(202, b'', {})] if target == 'initialize' else
                              [init, (202, b'', {}), (202, b'', {})])
            result = self.run_reader(wire)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(wire.calls), 1 if target == 'initialize' else 3)

    def test_wire_rejects_batches_other_notifications_and_notification_with_id(self):
        initialized = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        _validate_wire('POST', '/mcp', initialized)
        for body in ([initialized], {**initialized, 'id': 2},
                     {**initialized, 'params': {}},
                     {'jsonrpc': '2.0', 'method': 'notifications/cancelled'}):
            with self.assertRaises(ValueError):
                _validate_wire('POST', '/mcp', body)

    def test_default_tls_transport_completes_current_protocol_with_separate_202(self):
        wire, connections = Wire(), []
        def factory(host, port, *, timeout, context):
            self.assertEqual((host, port), ('mcp.apollo.io', 443))
            connection, response = Mock(), Mock()
            connections.append(connection)
            def request(method, path, *, body, headers):
                status, raw, response_headers = wire(method, path, json.loads(body), headers, timeout)
                response.status = status
                response.getheaders.return_value = list(response_headers.items())
                response.read1.side_effect = [raw, b'']
            connection.request.side_effect = request
            connection.getresponse.return_value = response
            return connection
        with patch('thunder_os.apollo_sequence_readonly.http.client.HTTPSConnection',
                   side_effect=factory), patch(
                   'thunder_os.apollo_sequence_readonly.ssl.create_default_context'):
            result = ApolloSequenceReadonly(lambda: KEY).sequence_detail(
                'seq-1', 'team-1', 'owner-1')
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['source']['identity_verified'])
        self.assertEqual(result['source']['request_count'], 5)
        self.assertEqual(wire.calls[1][2], {'jsonrpc': '2.0',
                                         'method': 'notifications/initialized'})
        for connection in connections:
            connection.close.assert_called_once()

    def test_sse_empty_priming_event_is_not_a_tool_response(self):
        data = b'id: prime\ndata:\n\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
        self.assertEqual(_rpc_response(data, 'text/event-stream', 1), {})

    def test_shared_deadline_and_exception_have_no_retry(self):
        wire = Wire()
        with patch('thunder_os.apollo_sequence_readonly.time.monotonic', side_effect=[0, 0, 16]):
            result = self.run_reader(wire)
        self.assertEqual(result['reason'], 'timeout')
        self.assertEqual(len(wire.calls), 1)
        wire = Mock(side_effect=RuntimeError(KEY))
        result = self.run_reader(wire)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(wire.call_count, 1)
        self.assertNotIn(KEY, json.dumps(result))

    def test_invalid_input_and_loader_do_not_reach_transport(self):
        for values in (('bad/id', 'team-1', 'owner-1'), ('seq-1', '', 'owner-1')):
            wire = Mock()
            result = ApolloSequenceReadonly(lambda: KEY, request=wire).sequence_detail(*values)
            self.assertEqual(result['status'], 'error'); wire.assert_not_called()
        wire = Mock()
        result = ApolloSequenceReadonly(lambda: '\ninvalid', request=wire).sequence_detail(
            'seq-1', 'team-1', 'owner-1')
        self.assertEqual(result['status'], 'error'); wire.assert_not_called()

    def test_snapshot_changes_when_template_or_safety_state_changes(self):
        first = self.run_reader()['snapshot_hash']
        show = detail(); show['emailer_templates'][0]['subject'] = 'Changed'
        self.assertNotEqual(first, self.run_reader(Wire(show=show))['snapshot_hash'])
        show = detail(); show['emailer_touches'][0]['include_signature'] = True
        self.assertNotEqual(first, self.run_reader(Wire(show=show))['snapshot_hash'])

    def test_only_exact_whitelisted_requests_can_reach_default_transport(self):
        self.run_reader()
        initialize = copy.deepcopy(self.wire.calls[0][2])
        for name in ('apollo_sequences_update', 'apollo_people_match', 'tools/list',
                     'apollo_emailer_campaigns_step_content'):
            body = {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                    'params': {'name': name, 'arguments': {'id': 'seq-1'}}}
            with self.assertRaises(ValueError): _validate_wire('POST', '/mcp', body)
        for method, path in (('GET', '/mcp'), ('POST', '/mcp?url=evil'),
                             ('POST', 'https://evil.invalid/mcp'), ('POST', '/other')):
            with self.assertRaises(ValueError): _validate_wire(method, path, initialize)
        initialize['params']['capabilities'] = {'sampling': {}}
        with self.assertRaises(ValueError): _validate_wire('POST', '/mcp', initialize)

    def test_real_observed_day_mode_and_html_only_paragraphs(self):
        show = detail(); show['emailer_steps'][0]['wait_mode'] = 'day'
        show['emailer_templates'][0].pop('body_text')
        result = self.run_reader(Wire(show=show))
        self.assertTrue(result['source']['static_safe'])
        self.assertEqual(result['items'][0]['interval_hours'], 48)
        self.assertEqual(result['items'][0]['content'], 'First paragraph.\n\nSecond & last.')

    def test_secret_redaction_even_when_json_escapes_key(self):
        key = 'synthetic-"private\\key'
        show = detail(); show['emailer_templates'][0]['subject'] = key
        result = ApolloSequenceReadonly(lambda: key, request=Wire(show=show)).sequence_detail(
            'seq-1', 'team-1', 'owner-1')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['reason'], 'invalid_snapshot')
        self.assertEqual(result['items'], [])
        self.assertNotIn(key, str(result))

    def test_session_change_and_header_injection_stop_without_retry(self):
        for session in ('different-session', 'bad\nsession'):
            wire = Wire()
            original = wire.__call__
            def request(*args):
                status, raw, headers = original(*args)
                if len(wire.calls) == 2: headers['Mcp-Session-Id'] = session
                return status, raw, headers
            result = self.run_reader(request)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(wire.calls), 2)

    def test_sse_multiline_data_duplicate_response_and_server_request(self):
        raw = b'event: message\r\ndata: {"jsonrpc":"2.0",\r\ndata: "id":1,"result":{}}\r\n\r\n'
        self.assertEqual(_rpc_response(raw, 'text/event-stream', 1), {})
        for messages in (
            [{'jsonrpc': '2.0', 'id': 1, 'result': {}},
             {'jsonrpc': '2.0', 'id': 1, 'result': {}}],
            [{'jsonrpc': '2.0', 'id': 22, 'method': 'sampling/createMessage', 'params': {}}],
            [{'jsonrpc': '2.0', 'method': 'notifications/unknown'}],
        ):
            with self.assertRaises(ValueError):
                _rpc_response(json.dumps(messages).encode(), 'application/json', 1)
        with self.assertRaises(ValueError):
            _rpc_response(b'data: {"jsonrpc":"2.0","id":1,"result":{}}',
                          'text/event-stream', 1)

    def test_html_semantics_and_unclosed_markup_never_pass_static_parity(self):
        for body in ('<a href="https://example.invalid">First paragraph.</a>',
                     '<p>First paragraph.', '<p style="display:none">Hidden</p>',
                     '<script>hidden()</script>', '<p>Hi &lt;%= name %&gt;</p>'):
            show = detail(); show['emailer_templates'][0]['body_html'] = body
            result = self.run_reader(Wire(show=show))
            self.assertEqual(result['status'], 'ok')
            self.assertFalse(result['source']['static_safe'])

    def test_snapshot_raw_html_and_prompt_changes_affect_hash(self):
        show = detail(); show['emailer_templates'][0]['prompt_template'] = {
            'id': 'prompt-1', 'team_id': 'team-1', 'instructions': 'one'}
        first = self.run_reader(Wire(show=show))['snapshot_hash']
        show['emailer_templates'][0]['prompt_template']['instructions'] = 'two'
        result = self.run_reader(Wire(show=show))
        self.assertNotEqual(first, result['snapshot_hash'])
        self.assertFalse(result['source']['static_safe'])

    def test_default_transport_fixed_tls_host_bounds_and_closes(self):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheaders.return_value = [('Content-Type', 'application/json')]
        response.read1.side_effect = [b'{}', b'']
        with patch('thunder_os.apollo_sequence_readonly.http.client.HTTPSConnection',
                   return_value=connection) as factory, patch(
                   'thunder_os.apollo_sequence_readonly.ssl.create_default_context',
                   return_value='verified-tls'):
            self.run_reader()
            initialize = self.wire.calls[0][2]
            status, raw, headers = _https_request('POST', '/mcp', initialize, {}, 2)
        factory.assert_called_once_with('mcp.apollo.io', 443, timeout=2, context='verified-tls')
        self.assertEqual((status, raw), (200, b'{}'))
        connection.close.assert_called_once()
        self.assertEqual(connection.request.call_args.args[:2], ('POST', '/mcp'))
        with self.assertRaises(ValueError): _https_request('GET', '/mcp', {}, {}, 2)

    def test_default_transport_stops_sse_at_response_and_enforces_byte_bound(self):
        self.run_reader(); initialize = self.wire.calls[0][2]
        for oversized in (False, True):
            connection = Mock(); response = connection.getresponse.return_value
            response.status = 200
            response.getheaders.return_value = [('Content-Type', 'text/event-stream')]
            raw = b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
            response.read1.side_effect = ([b'x' * 1_000_001] if oversized else
                                         [raw, AssertionError('stream must not wait for EOF')])
            with patch('thunder_os.apollo_sequence_readonly.http.client.HTTPSConnection',
                       return_value=connection), patch(
                       'thunder_os.apollo_sequence_readonly.ssl.create_default_context'):
                if oversized:
                    with self.assertRaisesRegex(ValueError, 'response_too_large'):
                        _https_request('POST', '/mcp', initialize, {}, 2)
                else:
                    self.assertEqual(_https_request('POST', '/mcp', initialize, {}, 2)[1], raw)
            self.assertEqual(response.read1.call_count, 1)
            connection.close.assert_called_once()

    def test_recipient_none_empty_and_proven_new_contact_have_distinct_coverage(self):
        missing = self.run_reader()
        self.assertFalse(missing['source']['contact_overrides_verified'])
        self.assertIsNone(missing['source']['recipient_binding_hash'])
        empty = self.run_reader(recipients=[])
        self.assertTrue(empty['source']['contact_overrides_verified'])
        self.assertEqual(empty['source']['contact_overrides_reason'], 'empty_cohort')
        self.assertEqual(empty['source']['recipient_binding_hash'],
                         hashlib.sha256(b'[]').hexdigest())
        new = self.run_reader(recipients=[{'email': 'new@example.invalid', 'contact_id': None,
                                        'account_id': 'team-1', 'lookup_complete': True}])
        self.assertEqual(len(self.wire.calls), 5)
        self.assertTrue(new['source']['contact_overrides_verified'])
        self.assertEqual(new['recipient_overrides'][0]['steps'], [
            {'step_id': 'step-1', 'state': 'no_override', 'subject': None, 'content': None}])

    def test_existing_contact_bulk_is_sixth_read_and_unknown_shape_blocks_coverage(self):
        recipients = [{'email': 'owner@example.invalid', 'contact_id': 'contact-1',
                       'account_id': 'team-1', 'lookup_complete': True},
                      {'email': 'new@example.invalid', 'contact_id': None,
                       'account_id': 'team-1', 'lookup_complete': True}]
        cases = ({}, {'step_content': []}, {'overrides': []},
                 {'contacts': [{'id': 'contact-1', 'steps': []}]},
                 {'step_contact_contents': None}, {'step_contact_contents': {}},
                 {'step_contact_contents': ''}, {'step_contact_contents': False},
                 {'step_contact_contents': [], 'partial': True},
                 {'step_contact_contents': [{}]},
                 {'step_contact_contents': [{'contact_id': 'contact-1',
                    'emailer_step_id': 'step-1', 'subject': 'unknown schema'}]})
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.run_reader(Wire(overrides=payload), recipients=recipients)
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['reason'], 'contact_override_shape_unverified')
                self.assertFalse(result['complete'])
                self.assertEqual(result['items'], [])
                self.assertEqual(len(self.wire.calls), 6)
                last = self.wire.calls[-1][2]
                self.assertEqual(last['params'], {'name': STEP_CONTENT,
                    'arguments': {'id': 'seq-1', 'contact_ids': ['contact-1']}})
                self.assertFalse(result['source']['contact_overrides_verified'])
                self.assertEqual(result['source']['contact_overrides_reason'], 'provider_shape_unverified')
                self.assertFalse(result['source']['static_safe'])

    def test_exact_scoped_empty_list_covers_every_requested_contact_and_step(self):
        recipients = [{'email': f'person{i}@example.invalid', 'contact_id': f'contact-{i}',
                       'account_id': 'team-1', 'lookup_complete': True} for i in range(2)]
        recipients.append({'email': 'new@example.invalid', 'contact_id': None,
                           'account_id': 'team-1', 'lookup_complete': True})
        show = detail()
        show['emailer_campaign']['num_steps'] = 2
        step = copy.deepcopy(show['emailer_steps'][0]); step.update(id='step-2', position=2)
        show['emailer_steps'].append(step)
        touch = copy.deepcopy(show['emailer_touches'][0])
        touch.update(id='touch-2', emailer_step_id='step-2', emailer_template_id='template-2')
        show['emailer_touches'].append(touch)
        template = copy.deepcopy(show['emailer_templates'][0]); template['id'] = 'template-2'
        show['emailer_templates'].append(template)
        result = self.run_reader(Wire(show=show, overrides={'step_contact_contents': []}),
                                 recipients=recipients)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['complete'])
        self.assertTrue(result['source']['contact_overrides_verified'])
        self.assertEqual(result['source']['contact_overrides_reason'], 'scoped_empty_step_contact_contents')
        self.assertEqual(self.wire.calls[-1][2]['params']['arguments'],
                         {'id': 'seq-1', 'contact_ids': ['contact-0', 'contact-1']})
        self.assertEqual(len(self.wire.calls), 6)
        coverage = {(row['contact_id'], step['step_id'], step['state'])
                    for row in result['recipient_overrides'] for step in row['steps']}
        self.assertEqual(coverage, {(contact, step, 'no_override')
            for contact in (None, 'contact-0', 'contact-1') for step in ('step-1', 'step-2')})
        self.assertEqual(result['snapshot']['recipient_overrides'], result['recipient_overrides'])
        self.assertTrue(result['snapshot']['contact_overrides_verified'])
        self.assertIsNotNone(result['source']['recipient_binding_hash'])

    def test_empty_overrides_do_not_clear_shared_ai_or_signature_flags(self):
        show = detail()
        show['emailer_touches'][0].update(template_type='prompt_template', include_signature=True)
        show['emailer_templates'][0]['has_ai_variables'] = True
        result = self.run_reader(Wire(show=show), recipients=[{
            'email': 'owner@example.invalid', 'contact_id': 'contact-1',
            'account_id': 'team-1', 'lookup_complete': True}])
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['source']['contact_overrides_verified'])
        self.assertFalse(result['source']['static_safe'])
        self.assertTrue(result['items'][0]['flags']['ai'])
        self.assertTrue(result['items'][0]['flags']['include_signature'])
        self.assertFalse(result['items'][0]['flags']['static_safe'])

    def test_real_case_duplicate_cookies_are_ignored_but_protocol_headers_are_unique(self):
        headers = [('Set-Cookie', 'synthetic-one'), ('set-cookie', 'synthetic-two'),
                   ('Set-Cookie', 'synthetic-three'), ('Content-Type', 'application/json'),
                   ('MCP-Protocol-Version', '2025-11-25')]
        self.assertEqual(_headers(headers), {'content-type': 'application/json',
                                            'mcp-protocol-version': '2025-11-25'})
        for key in ('Content-Type', 'Mcp-Session-Id', 'MCP-Protocol-Version'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'invalid_headers'):
                _headers([(key, 'one'), (key.lower(), 'two')])
        with self.assertRaisesRegex(ValueError, 'invalid_headers'):
            _headers([('ignored', 'x' * 8193)])

    def test_real_initialize_headers_allow_negotiation_then_fail_on_invalid_notification_ack(self):
        self.run_reader(); initialize = self.wire.calls[0][2]
        connection = Mock(); response = connection.getresponse.return_value
        response.status = 200
        response.getheaders.return_value = [('Set-Cookie', 'synthetic-one'),
            ('Set-Cookie', 'synthetic-two'), ('Set-Cookie', 'synthetic-three'),
            ('Content-Type', 'application/json'), ('MCP-Protocol-Version', '2025-11-25')]
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {
            'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
            'serverInfo': {'name': 'synthetic', 'version': '1'}}}).encode()
        response.read1.side_effect = [raw, b'', b'', b'']
        with patch('thunder_os.apollo_sequence_readonly.http.client.HTTPSConnection',
                   return_value=connection) as factory, patch(
                   'thunder_os.apollo_sequence_readonly.ssl.create_default_context'):
            result = ApolloSequenceReadonly(lambda: KEY).sequence_detail(
                'seq-1', 'team-1', 'owner-1')
        self.assertEqual(result['reason'], 'provider_http_error')
        self.assertEqual(result['source']['request_count'], 2)
        self.assertFalse(result['complete'])
        self.assertEqual(result['items'], [])
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(connection.close.call_count, 2)
        self.assertNotIn('synthetic-one', json.dumps(result))

    def test_recipient_provenance_identity_duplicates_and_200_limit(self):
        valid = {'email': 'owner@example.invalid', 'contact_id': 'contact-1',
                 'account_id': 'team-1', 'lookup_complete': True}
        cases = [[{**valid, 'lookup_complete': False}], [{**valid, 'account_id': 'other'}],
                 [{'email': 'owner@example.invalid', 'contact_id': 'contact-1'}],
                 [{**valid, 'contact_id': 'bad/id'}], [valid, valid],
                 [valid, {**valid, 'email': 'second@example.invalid'}],
                 [{**valid, 'email': 'bad email'}], [{**valid, 'lookup_complete': 1}]]
        for recipients in cases:
            wire = Wire()
            result = self.run_reader(wire, recipients=recipients)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(wire.calls), 0)
        maximum = [{**valid, 'email': f'person{i}@example.invalid', 'contact_id': f'contact-{i}'}
                   for i in range(200)]
        result = self.run_reader(recipients=maximum)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(self.wire.calls[-1][2]['params']['arguments']['contact_ids']), 200)
        wire = Wire()
        self.assertEqual(self.run_reader(wire, recipients=maximum + [valid])['status'], 'error')
        self.assertEqual(wire.calls, [])

    def test_recipient_binding_hash_is_order_stable_and_changes_with_identity(self):
        rows = [{'email': 'SECOND@example.invalid', 'contact_id': 'contact-2',
                 'account_id': 'team-1', 'lookup_complete': True},
                {'email': 'first@example.invalid', 'contact_id': 'contact-1',
                 'account_id': 'team-1', 'lookup_complete': True}]
        first = self.run_reader(recipients=rows)['source']['recipient_binding_hash']
        self.assertEqual(first, self.run_reader(recipients=list(reversed(rows)))[
            'source']['recipient_binding_hash'])
        changed = copy.deepcopy(rows); changed[0]['contact_id'] = None
        self.assertNotEqual(first, self.run_reader(recipients=changed)[
            'source']['recipient_binding_hash'])


if __name__ == '__main__':
    unittest.main()
