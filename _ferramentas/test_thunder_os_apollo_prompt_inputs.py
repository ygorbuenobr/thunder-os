"""S27C5: synthetic inputs only; no approvals, generation or provider access."""
import copy
import unittest
from unittest.mock import patch

from thunder_os.apollo_prompt_inputs import prompt_inputs
from thunder_os.apollo_sequence_readonly import ApolloSequenceReadonly
from test_thunder_os_apollo_sequence_readonly import KEY, Wire, detail


def snapshot():
    data = detail()
    data['emailer_touches'][0].update(template_type='prompt_template',
        generation_options=[{'field_name': 'research', 'fallback': 'Sem prova'}],
        generation_tone='formal', include_signature=True)
    data['emailer_templates'][0].update(has_ai_variables=True,
        body_text='Olá {{first_name}}\n{{research}}', body_html=None,
        prompt_template={'id': 'prompt-1', 'team_id': 'team-1',
            'prompt_template': 'Use {{research}} exatamente.',
            'system_instructions': 'Só provas.', 'instructions': 'Sem invenção.',
            'raw_variables': {'research': {'value': 'Texto Unicode é', 'fallback': 'vazio'}},
            'tone': 'professional'})
    return ApolloSequenceReadonly(lambda: KEY, request=Wire(show=data)).sequence_detail(
        'seq-1', 'team-1', 'owner-1', recipients=[])


class PromptInputsTests(unittest.TestCase):
    def setUp(self):
        net = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        net.start()
        self.addCleanup(net.stop)

    def changed(self, data, change):
        value = copy.deepcopy(data)
        change(value['items'])
        value['snapshot']['steps'] = copy.deepcopy(value['items'])
        return prompt_inputs(value)

    def test_apollo_prompt_hash_approval_invalidates_on_input_change(self):
        data = snapshot()
        baseline = prompt_inputs(data)
        self.assertEqual(baseline['status'], 'verified')
        self.assertRegex(baseline['input_hash'], r'^[a-f0-9]{64}$')
        self.assertFalse(data['source']['static_safe'])
        changes = [
            lambda s: s[0]['variants'][0]['prompt_template'].update(instructions='Nova instrução'),
            lambda s: s[0]['variants'][0]['prompt_template'].update(system_instructions='Novo sistema'),
            lambda s: s[0]['variants'][0]['prompt_template'].update(prompt_template='Novo prompt'),
            lambda s: s[0]['variants'][0]['prompt_template']['raw_variables']['research'].update(value='Outra prova'),
            lambda s: s[0]['variants'][0]['prompt_template']['raw_variables']['research'].update(fallback='Outro fallback'),
            lambda s: s[0]['variants'][0]['prompt_template'].update(tone='casual'),
            lambda s: s[0]['variants'][0].update(generation_tone='casual'),
            lambda s: s[0]['variants'][0]['generation_options'][0].update(fallback='Novo fallback'),
            lambda s: s[0]['variants'][0].update(include_signature=False),
            lambda s: s[0]['variants'][0].update(body_text='Olá {{research}} '),
            lambda s: s[0]['variants'][0].update(body_html='<p>Olá</p>'),
            lambda s: s[0]['variants'][0].update(subject='Outro assunto'),
            lambda s: s[0]['variants'][0].update(template_id='template-2'),
            lambda s: s[0]['variants'][0].update(attachments=[['file-1'], [], [], []]),
            lambda s: s[0].update(interval_hours=49),
            lambda s: s[0]['flags'].update(raw_step_type='manual_email'),
            lambda s: s[0]['variants'][0]['prompt_template']['raw_variables']['research'].update(value='Texto Unicode e\u0301'),
        ]
        for change in changes:
            result = self.changed(data, change)
            self.assertEqual(result['status'], 'verified', result)
            self.assertNotEqual(result['input_hash'], baseline['input_hash'])
        self.assertEqual(data, snapshot(), 'helper must not mutate caller inputs')

    def test_order_variants_and_top_level_attachments_affect_hash(self):
        data = snapshot()
        second = copy.deepcopy(data['items'][0])
        second.update(id='step-2', order=2)
        second['variants'][0].update(touch_id='touch-2', template_id='template-2')
        data['items'].append(second)
        data['snapshot']['steps'] = copy.deepcopy(data['items'])
        first = prompt_inputs(data)['input_hash']
        swapped = self.changed(data, lambda s: (s[0].update(order=2), s[1].update(order=1)))
        self.assertEqual(swapped['status'], 'verified')
        self.assertNotEqual(first, swapped['input_hash'])
        data['snapshot']['attachments'] = [{'id': 'file-2', 'filename': 'prova.pdf'}]
        self.assertNotEqual(first, prompt_inputs(data)['input_hash'])
        variant = copy.deepcopy(data['items'][0]['variants'][0])
        variant.update(touch_id='touch-b', template_id='template-b')
        data['items'][0]['variants'].append(variant)
        data['snapshot']['steps'] = copy.deepcopy(data['items'])
        before = prompt_inputs(data)['input_hash']
        result = self.changed(data, lambda s: s[0]['variants'].reverse())
        self.assertEqual(result['status'], 'verified')
        self.assertNotEqual(before, result['input_hash'])

    def test_timestamps_provider_stats_and_mapping_key_order_are_not_inputs(self):
        data = snapshot()
        first = prompt_inputs(data)['input_hash']
        data.update(collected_at='2099-01-01', snapshot_hash='obsolete-reader-checksum',
                    stats={'sent': 100})
        data['source'].update(request_count=500, collected_at='2099', static_safe=True)
        data['snapshot'].update(collected_at='2099', stats={'opens': 3})
        data['items'][0].update(stats={'opens': 9}, collected_at='tomorrow')
        data['items'][0]['variants'][0].update(stats={'replies': 8}, collected_at='tomorrow')
        data['items'][0]['variants'][0]['prompt_template'].update(
            stats={'uses': 20}, collected_at='tomorrow')
        self.assertEqual(first, prompt_inputs(data)['input_hash'])
        data['items'][0]['variants'][0]['prompt_template'] = dict(reversed(list(
            data['items'][0]['variants'][0]['prompt_template'].items())))
        self.assertEqual(first, prompt_inputs(data)['input_hash'])

    def test_malformed_scope_incomplete_and_unknown_step_are_unverified(self):
        mutations = [
            lambda d: d.update(status='error'), lambda d: d.update(complete=1),
            lambda d: d['source'].update(identity_verified=False),
            lambda d: d['source'].update(contact_overrides_verified=False),
            lambda d: d['source'].update(status='error'),
            lambda d: d['source'].update(complete=False),
            lambda d: d['source'].update(provider='instantly'),
            lambda d: d['source'].update(account_id=''),
            lambda d: d['source'].update(sequence_id='bad/id'),
            lambda d: d['source'].update(observed_profile_account_id='another-team'),
            lambda d: d['snapshot'].update(sequence_id='another-seq'),
            lambda d: d['snapshot'].update(owner_id='another-owner'),
            lambda d: d['items'][0]['variants'][0]['prompt_template'].update(team_id='another-team'),
            lambda d: d['items'][0].update(team_id='another-team'),
            lambda d: d['items'][0]['variants'][0].update(sequence_id='another-seq'),
            lambda d: d['items'][0]['flags'].update(raw_step_type='linkedin_message'),
            lambda d: d['items'][0].update(interval_hours=None),
            lambda d: d['items'][0].update(order=True),
            lambda d: d['items'][0].update(variants=[]),
            lambda d: d['items'][0]['variants'][0].update(include_signature=None),
            lambda d: d['items'][0]['variants'][0].update(body_text=None, body_html=None),
            lambda d: d['items'][0]['variants'][0].update(instructions='bad\ud800'),
            lambda d: d['items'][0]['variants'][0].update(generation_options=float('nan')),
            lambda d: d['items'][0]['variants'][0].update(prompt_template={'nested': object()}),
        ]
        for mutate in mutations:
            data = snapshot()
            mutate(data)
            result = prompt_inputs(data)
            self.assertEqual(result['status'], 'unverified', result)
            self.assertIsNone(result['input_hash'])
            self.assertEqual(result['steps'], [])
        for malformed in (None, [], {}, 'snapshot'):
            self.assertEqual(prompt_inputs(malformed)['status'], 'unverified')

    def test_scope_changes_hash_and_output_is_detached(self):
        data = snapshot()
        before = prompt_inputs(data)
        data['source']['sequence_id'] = data['snapshot']['sequence_id'] = 'seq-2'
        after = prompt_inputs(data)
        self.assertEqual(after['status'], 'verified')
        self.assertNotEqual(before['input_hash'], after['input_hash'])
        original = copy.deepcopy(data)
        after['steps'][0]['variants'][0]['prompt_template']['instructions'] = 'Changed'
        self.assertEqual(data, original)

    def test_raw_variables_keep_literal_metadata_names_and_direct_prompt_fields(self):
        data = snapshot()
        variant = data['items'][0]['variants'][0]
        variant.update(instructions='Literal instructions', system_instructions='System',
            tone='formal', raw_variables={'stats': 1, 'collected_at': 'source date'})
        data['snapshot']['steps'] = copy.deepcopy(data['items'])
        first = prompt_inputs(data)['input_hash']
        for key, value in [('instructions', 'Other'), ('system_instructions', 'Other'),
                           ('tone', 'casual'), ('raw_variables', {'stats': 2})]:
            changed = self.changed(data, lambda s: s[0]['variants'][0].update({key: value}))
            self.assertEqual(changed['status'], 'verified')
            self.assertNotEqual(changed['input_hash'], first)

    def test_recipient_binding_and_override_changes_are_hashed(self):
        data = snapshot()
        data['snapshot']['recipients'] = [{'email': 'fake@example.invalid',
            'contact_id': None, 'account_id': 'team-1', 'lookup_complete': True}]
        data['snapshot']['recipient_overrides'] = [{'email': 'fake@example.invalid',
            'contact_id': None, 'steps': [{'step_id': 'step-1', 'state': 'no_override',
                                         'subject': None, 'content': None}]}]
        first = prompt_inputs(data)
        self.assertEqual(first['status'], 'verified')
        data['snapshot']['recipient_overrides'][0]['steps'][0].update(content='Observed override')
        self.assertNotEqual(first['input_hash'], prompt_inputs(data)['input_hash'])
        data['snapshot']['recipients'][0]['account_id'] = 'another-team'
        self.assertEqual(prompt_inputs(data)['status'], 'unverified')

    def test_limits_cycles_duplicate_steps_and_variants_fail_closed(self):
        data = snapshot()
        data['items'].append(copy.deepcopy(data['items'][0]))
        self.assertEqual(prompt_inputs(data)['status'], 'unverified')
        data = snapshot()
        data['items'][0]['variants'].append(copy.deepcopy(data['items'][0]['variants'][0]))
        self.assertEqual(prompt_inputs(data)['status'], 'unverified')
        for value in ('é' * 600_000, {'value': list(range(2001))}):
            data = snapshot()
            data['items'][0]['variants'][0]['prompt_template']['instructions'] = value
            self.assertEqual(prompt_inputs(data)['status'], 'unverified')
        data = snapshot()
        recursive = []
        recursive.append(recursive)
        data['items'][0]['variants'][0]['prompt_template']['variables'] = recursive
        self.assertEqual(prompt_inputs(data)['status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
