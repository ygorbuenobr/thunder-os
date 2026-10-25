"""R4 reader checks, one delegated slice; synthetic fixtures, no credentials/network.

Sources: R4 in Brain cadencia-multicanal/prd-gtm-comercial.md and the task.
Landing: only two exact GET paths; reuse existing credentials, pins and transport.
No persistence, writer, enrollment, provider test or commit belongs to this slice.
Checks/proofs (unittest methods below):
C1 transport allowlist/bounds -> test_two_exact_gets_and_budget,
    test_allowlist_rejects_before_credentials_or_transport.
C2 identity isolation -> test_identity_mismatch_retains_pins_and_no_content.
C3 complete originals/order -> test_static_snapshot_preserves_all_variants_and_text.
C4 missing != false -> test_missing_and_invalid_metadata_stays_unknown.
C5 dynamic != approved -> test_dynamic_inputs_never_claim_exact_copy.
C6 deterministic semantic hash -> test_hash_stable_for_noise_and_changes_for_inputs.
C7 external failure/redaction -> test_transport_failures_no_retry_no_secrets,
    test_invalid_json_and_response_bounds.
C8 partial structure retains evidence -> test_malformed_steps_and_variants_are_partial.
Sweep: validation C2/C4/C8; failure/retry C7 (no retry); authorization C1/C2;
concurrency no mutable shared state; ordering C3; lifecycle no persistence;
observability source/reasons/time/budget C1/C4; provider failures C7.
Independent verification belongs to the principal over the integrated diff.
"""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from thunder_os import instantly_sequence_readonly as mod


KEY = 'synthetic-secret-only'
OWNER = '00000000-0000-4000-8000-000000000001'


def campaign():
    return {'id': mod.INSTANTLY_CAMPAIGN, 'organization': mod.INSTANTLY_WORKSPACE,
        'owned_by': OWNER, 'name': 'Synthetic R4', 'status': 2,
        'campaign_schedule': {'schedules': [{'timezone': 'America/Sao_Paulo',
            'timing': {'from': '09:00', 'to': '17:00'}, 'days': {'1': True}}]},
        'email_list': ['sender@example.invalid'], 'stop_on_reply': True,
        'stop_on_auto_reply': False, 'core_variables': {}, 'custom_variables': {},
        'text_only': False, 'insert_unsubscribe_header': False,
        'sequences': [{'steps': [
            {'type': 'email', 'delay': 2, 'delay_unit': 'days',
             'pre_delay': 0, 'pre_delay_unit': 'hours', 'variants': [
                 {'subject': 'Olá & mundo', 'body': '<p>Primeiro &amp; inteiro.</p>\n<p>Fim.</p>',
                  'v_disabled': False},
                 {'subject': 'Alternativa', 'body': ' Outro texto integral.\n\nFim. ',
                  'v_disabled': True}]},
            {'type': 'email', 'delay': 4, 'delay_unit': 'hours', 'variants': [
                {'subject': 'Continuação', 'body': 'Segunda mensagem.', 'v_disabled': False}]}]}]}


def fixture_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class Wire:
    def __init__(self, detail=None, workspace=None):
        self.detail = campaign() if detail is None else detail
        self.workspace = {'id': mod.INSTANTLY_WORKSPACE} if workspace is None else workspace
        self.calls = []

    def __call__(self, method, path, body, headers, timeout):
        self.calls.append((method, path, body, dict(headers), timeout))
        value = self.workspace if path == mod.WORKSPACE_PATH else self.detail
        return 200, json.dumps(value, ensure_ascii=False).encode()


class InstantlySequenceReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.net = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.credentials = patch.object(mod, 'private_text',
            side_effect=AssertionError('real credentials forbidden'))
        self.credentials.start()
        self.addCleanup(self.credentials.stop)

    def snapshot(self, detail=None, workspace=None):
        self.wire = Wire(detail, workspace)
        return mod.InstantlySequenceReadonly(lambda: KEY, request=self.wire).snapshot()

    def test_two_exact_gets_and_budget(self):
        result = self.snapshot()
        self.assertEqual([c[:3] for c in self.wire.calls], [
            ('GET', '/api/v2/workspaces/current', None),
            ('GET', '/api/v2/campaigns/' + mod.INSTANTLY_CAMPAIGN, None)])
        self.assertTrue(all(c[3] == {'Authorization': 'Bearer ' + KEY,
            'Content-Type': 'application/json'} and c[4] == 15 for c in self.wire.calls))
        self.assertEqual(result['budget'], {'requests': 2, 'max_requests': 2, 'credits': 0})
        self.assertTrue(result['read_only'])
        self.assertEqual(result['provider'], 'instantly')
        self.assertTrue(result['source']['identity_verified'])
        self.assertEqual(result['source']['api_version'], 'v2')
        self.assertIn('+00:00', result['observed_at'])

    def test_allowlist_rejects_before_credentials_or_transport(self):
        loader, request = Mock(), Mock()
        reader = mod.InstantlySequenceReadonly(loader, request=request)
        for path in ('/api/v2/campaigns', mod.CAMPAIGN_PATH + '?id=x',
                     mod.CAMPAIGN_PATH + '/activate', '/api/v2/campaigns/other',
                     '/api/v2/workspaces/current/', '/api/v2/emails',
                     'https://api.instantly.ai' + mod.CAMPAIGN_PATH,
                     '/api/v2/leads', '/api/v2/webhooks'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'endpoint_blocked'):
                reader._get(path)
        loader.assert_not_called()
        request.assert_not_called()
        self.assertFalse(any(hasattr(reader, name) for name in (
            'send', 'enroll', 'enrich', 'register', 'update', 'activate', 'collect')))

    def test_identity_mismatch_retains_pins_and_no_content(self):
        for key in ('id', 'organization'):
            for value in ('other', None, {}, mod.INSTANTLY_WORKSPACE.upper()):
                detail = campaign()
                detail[key] = value
                result = self.snapshot(detail)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['account_id'], mod.INSTANTLY_WORKSPACE)
                self.assertEqual(result['campaign_id'], mod.INSTANTLY_CAMPAIGN)
                self.assertEqual(result['steps'], [])
                self.assertIsNone(result['input_hash'])
                self.assertIsNone(result['sending_accounts'])
                self.assertFalse(result['source']['identity_verified'])
        for workspace in ({}, {'id': 'other'}, {'id': None},
                          {'id': mod.INSTANTLY_WORKSPACE.upper()}):
            result = self.snapshot(workspace=workspace)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(len(self.wire.calls), 1)
            self.assertEqual(result['steps'], [])

    def test_static_snapshot_preserves_all_variants_and_text(self):
        detail = campaign()
        detail['sequences'][0]['steps'][0]['variants'][0]['body'] += 'x' * 30000
        result = self.snapshot(detail)
        self.assertEqual((result['status'], result['complete']), ('ok', True))
        self.assertEqual(result['campaign_status'], 2)
        self.assertTrue(result['stop_on_reply'])
        self.assertIs(result['stop_on_auto_reply'], False)
        self.assertEqual(result['sending_accounts'], detail['email_list'])
        self.assertEqual(result['owner_id'], OWNER)
        self.assertEqual([s['order'] for s in result['steps']], [1, 2])
        self.assertEqual(len({s['id'] for s in result['steps']}), 2)
        for step, original in zip(result['steps'], detail['sequences'][0]['steps']):
            self.assertEqual(step['channel'], 'email')
            self.assertEqual(step['delay'], original['delay'])
            self.assertEqual(step['delay_unit'], original['delay_unit'])
            self.assertEqual(step['approval_kind'], 'exact_copy')
            self.assertFalse(step['approval_granted'])
            self.assertEqual(len(step['variants']), len(original['variants']))
            for variant, raw in zip(step['variants'], original['variants']):
                for key in ('subject', 'body', 'v_disabled'):
                    self.assertEqual(variant[key], raw[key])
                self.assertEqual(variant['body_html'], raw['body'])
            self.assertEqual(step['subject'], original['variants'][0]['subject'])
            self.assertEqual(step['body_html'], original['variants'][0]['body'])
        self.assertEqual(result['steps'][0]['pre_delay'], 0)
        self.assertEqual(result['steps'][0]['pre_delay_unit'], 'hours')
        self.assertIn('Primeiro & inteiro.', result['steps'][0]['content'])
        self.assertEqual(result['capabilities']['final_email'], 'unknown')
        self.assertFalse(result['approval_granted'])
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(result['input_hash'], fixture_hash(result['inputs']))

    def test_missing_and_invalid_metadata_stays_unknown(self):
        for key in ('status', 'stop_on_reply', 'stop_on_auto_reply', 'owned_by',
                    'email_list', 'campaign_schedule', 'core_variables', 'custom_variables'):
            for value in ('omitted', None, 'invalid'):
                detail = campaign()
                if value == 'omitted': detail.pop(key)
                else: detail[key] = value
                result = self.snapshot(detail)
                self.assertEqual(result['status'], 'partial', (key, value))
                self.assertFalse(result['complete'])
                self.assertEqual(len(result['steps']), 2)
                target = {'status': 'campaign_status', 'email_list': 'sending_accounts',
                          'owned_by': 'owner_id'}.get(key, key)
                self.assertIsNone(result[target], (key, value))
                self.assertIsNotNone(result['input_hash'])
        detail = campaign()
        detail.update(status=0, stop_on_reply=False)
        result = self.snapshot(detail)
        self.assertEqual(result['campaign_status'], 0)
        self.assertIs(result['stop_on_reply'], False)

    def test_dynamic_inputs_never_claim_exact_copy(self):
        cases = [({'subject': 'Hello {{firstName}}'}, None),
                 ({'body': '{Olá|Oi} {{companyName}}'}, None),
                 ({'body': '&#123;Olá|Oi&#125;'}, None),
                 ({'body': '[[AI intro]]'}, None),
                 ({'ai_prompt': 'write intro'}, None),
                 ({'generation_options': {'model': 'synthetic'}}, None),
                 ({'template_type': 'ai'}, None),
                 ({'hasAiVariables': True}, None),
                 ({'include_signature': True}, None),
                 ({}, {'ai_sdr_id': OWNER}),
                 ({}, {'core_variables': {'intro': {'type': 'ai', 'prompt': 'hello'}}}),
                 ({}, {'custom_variables': {'x': 'unresolved input'}}),
                 ({}, {'signature': '<p>Sender</p>'})]
        for variant, top in cases:
            detail = campaign()
            detail['sequences'][0]['steps'][0]['variants'][0].update(variant)
            detail.update(top or {})
            result = self.snapshot(detail)
            self.assertEqual(result['steps'][0]['approval_kind'], 'dynamic_unverified',
                             (variant, top))
            self.assertEqual(result['status'], 'partial')
            self.assertFalse(result['approval_granted'])
            self.assertEqual(result['capabilities']['dynamic_approval'], 'unknown')
            self.assertTrue(result['steps'][0]['reasons'])
        detail = campaign()
        detail['sequences'][0]['steps'][0]['variants'][1]['body'] = '{A|B}'
        result = self.snapshot(detail)
        self.assertTrue(result['steps'][0]['variants'][1]['v_disabled'])
        self.assertEqual(result['steps'][0]['approval_kind'], 'dynamic_unverified')

    def test_unknown_options_and_invalid_schedule_or_sender_are_not_complete(self):
        for key, value in (('email_list', ['not-an-email']), ('email_list', [None]),
                           ('campaign_schedule', {}),
                           ('campaign_schedule', {'schedules': []}),
                           ('campaign_schedule', {'schedules': ['invalid']})):
            detail = campaign()
            detail[key] = value
            result = self.snapshot(detail)
            self.assertEqual(result['status'], 'partial', (key, value))
        detail = campaign()
        detail['sequences'][0]['steps'][0]['unknown_generation'] = {'mode': 'new'}
        result = self.snapshot(detail)
        self.assertEqual(result['steps'][0]['approval_kind'], 'dynamic_unverified')
        self.assertEqual(result['inputs']['campaign']['sequences'], detail['sequences'])
        detail = campaign()
        detail['sequences'][0]['steps'][0]['variants'][0].pop('v_disabled')
        result = self.snapshot(detail)
        self.assertIsNone(result['steps'][0]['variants'][0]['v_disabled'])
        self.assertEqual(result['status'], 'partial')

    def test_hash_stable_for_noise_and_changes_for_inputs(self):
        original = self.snapshot()
        detail = campaign()
        detail.update(timestamp_created='then', timestamp_updated='now',
                      collected_at='noise', analytics={'sent': 42})
        noisy = self.snapshot(detail)
        self.assertEqual(original['input_hash'], noisy['input_hash'])
        self.assertEqual([s['id'] for s in original['steps']], [s['id'] for s in noisy['steps']])
        mutations = [lambda c: c.update(stop_on_reply=False),
            lambda c: c.update(status=1), lambda c: c.update(owned_by='other'),
            lambda c: c.update(email_list=['other@example.invalid']),
            lambda c: c['campaign_schedule'].update(start_date='2027-01-01'),
            lambda c: c.update(core_variables={'timestamp_updated': 'meaningful value'}),
            lambda c: c.update(custom_variables={'intro': 'raw input'}),
            lambda c: c.update(include_signature=False),
            lambda c: c.update(first_email_text_only=True),
            lambda c: c['sequences'][0]['steps'][0].update(delay=9),
            lambda c: c['sequences'][0]['steps'][0].update(pre_delay=3),
            lambda c: c['sequences'][0]['steps'][0]['variants'][1].update(v_disabled=False),
            lambda c: c['sequences'][0]['steps'][0]['variants'][1].update(body='Changed'),
            lambda c: c['sequences'][0]['steps'].reverse(),
            lambda c: c['sequences'].append({'steps': []})]
        for mutation in mutations:
            detail = campaign()
            mutation(detail)
            result = self.snapshot(detail)
            self.assertNotEqual(original['input_hash'], result['input_hash'])
        ordered = dict(reversed(list(campaign().items())))
        self.assertEqual(original['input_hash'], self.snapshot(ordered)['input_hash'])

    def test_transport_failures_no_retry_no_secrets(self):
        for status in (301, 302, 401, 403, 404, 429, 500, True):
            for failure_call in (1, 2):
                wire = Wire()
                def request(*args):
                    if len(wire.calls) + 1 == failure_call:
                        wire.calls.append(args)
                        return status, (KEY + ' private-provider-body').encode()
                    return wire(*args)
                result = mod.InstantlySequenceReadonly(lambda: KEY, request=request).snapshot()
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(len(wire.calls), failure_call)
                self.assertEqual(result['steps'], [])
                self.assertNotIn(KEY, json.dumps(result))
                self.assertNotIn('private-provider-body', json.dumps(result))
        for exception in (TimeoutError(KEY), RuntimeError(KEY), ValueError(KEY)):
            request = Mock(side_effect=exception)
            result = mod.InstantlySequenceReadonly(lambda: KEY, request=request).snapshot()
            self.assertEqual(result['status'], 'unavailable')
            request.assert_called_once()
            self.assertNotIn(KEY, json.dumps(result))
        for token in ('', 'with space', 'secret\r\nX: header', '\x00bad', 'x' * 4097, None):
            request = Mock()
            result = mod.InstantlySequenceReadonly(lambda: token, request=request).snapshot()
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['budget']['requests'], 0)
            request.assert_not_called()
        request = Mock()
        result = mod.InstantlySequenceReadonly(Mock(side_effect=RuntimeError(KEY)),
                                               request=request).snapshot()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(result['budget']['requests'], 0)
        request.assert_not_called()

    def test_invalid_json_and_response_bounds(self):
        for raw in (b'[]', b'null', b'broken', b'\xff', b'{"id":NaN}',
                    b'{"id":1,"id":2}', b' ' * 1_000_001, 'not bytes'):
            request = Mock(return_value=(200, raw))
            result = mod.InstantlySequenceReadonly(lambda: KEY, request=request).snapshot()
            self.assertEqual(result['status'], 'unavailable')
            self.assertIsNone(result['input_hash'])
        detail = campaign()
        detail['sequences'][0]['steps'][0]['variants'][0]['body'] = KEY
        result = self.snapshot(detail)
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn(KEY, json.dumps(result))

    def test_malformed_steps_and_variants_are_partial(self):
        for value in (None, [], {}, [{'steps': None}], [{'steps': []}]):
            detail = campaign()
            detail['sequences'] = value
            result = self.snapshot(detail)
            self.assertEqual(result['status'], 'partial')
            self.assertFalse(result['complete'])
        for step in (None, {}, {'type': 'sms', 'variants': []},
                     {'type': 'email', 'delay': True, 'variants': [None, {}]}):
            detail = campaign()
            detail['sequences'][0]['steps'].append(step)
            result = self.snapshot(detail)
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(len(result['steps']), 3)
            self.assertEqual(result['inputs']['campaign']['sequences'], detail['sequences'])
            self.assertEqual(result['steps'][2]['approval_kind'], 'dynamic_unverified')
        detail = campaign()
        detail['sequences'].append({'steps': [{'type': 'sms'}]})
        self.assertEqual(len(self.snapshot(detail)['steps']), 2)

    def test_default_loader_reuses_private_file_without_eager_access(self):
        with patch.object(mod, 'private_text', return_value=KEY) as private:
            wire = Wire()
            reader = mod.InstantlySequenceReadonly(request=wire)
            private.assert_not_called()
            result = reader.snapshot()
            self.assertEqual(result['status'], 'ok')
            self.assertTrue(all(c.args == (mod.KEY_FILE,) for c in private.call_args_list))


if __name__ == '__main__':
    unittest.main()
