"""Bounded C checks (TLC light, delegated build; principal owns fresh review).

Binding source: campaign-sheet-prd1/apollo-input-approval-contract.md and the
bounded delegation. Only this test and apollo_research_input.py are owned here.
C1 exact Brain bytes/path/hash -> test_exact_research_is_readable_and_unmodified
C2 actor/hash/version CAS -> test_approval_is_local_exact_and_versioned,
    test_stale_version_hash_and_actor_rejected, test_concurrent_approval_has_one_winner
C3 contact/team/field binding -> test_bad_field_definitions_fail_closed,
    test_wrong_contact_bindings_fail_closed
C4 changed input invalidates -> test_reprepare_invalidates_approval,
    test_brain_and_definition_changes_invalidate_approval
C5 exact readback/absence -> test_readback_classifies_exact_missing_and_mismatch
C6 no external writer/legacy changes -> test_apply_is_always_disabled,
    test_existing_approval_pack_is_untouched
Swept: validation C1/C3; failures C3/C5; idempotency C2; authorization C2;
concurrency C2; lifecycle C4; dependency failures C5; transitions C2/C4;
observability explicit codes/readback C3/C5. No UI, migration or effect dispatch.
Landing: reuse Repository CAS and KnowledgeStore safe paths; new discriminated
JSON state only. Apply disabled: cadence effect workers are not field PATCH ports.
One bounded slice, two files, no build handoff. No network/credits/provider tests.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunder_os.db import Repository
from thunder_os.knowledge import KnowledgeStore
from thunder_os.models import payload_hash
from thunder_os.apollo_research_input import ApolloResearchInput, ApolloResearchInputError


class Reads:
    def __init__(self):
        self.definition = {'id': 'field-1', 'label': 'thunder_research',
            'type': 'textarea', 'modality': 'contact', 'source': 'custom', 'read_only': False}
        self.contact = {'id': 'contact-1', 'email': 'ana@example.invalid',
            'account_id': 'company-1', 'typed_custom_fields': {}}
        self.team = 'team-1'
        self.complete = True
        self.fields_override = None
        self.error = False
        self.calls = []

    def envelope(self, items, endpoint):
        if self.error:
            raise TimeoutError('sensitive provider response')
        return {'status': 'ok', 'complete': self.complete, 'items': deepcopy(items),
            'source': {'provider': 'apollo', 'account_id': self.team,
                'identity_verified': True, 'endpoint': endpoint}}

    def fields(self, team_id):
        self.calls.append(('fields', team_id))
        items = self.fields_override if self.fields_override is not None else [self.definition]
        return self.envelope(items, '/api/v1/fields?source=custom')

    def contact_read(self, contact_id, team_id):
        self.calls.append(('contact', contact_id, team_id))
        return self.envelope([self.contact], '/api/v1/contacts/' + contact_id)


class ApolloResearchInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.brain = self.root / 'brain'
        self.file = self.brain / 'contas/acme/dossie.md'
        self.file.parent.mkdir(parents=True)
        self.value = '\r\n# Pesquisa Acme\r\n  São Paulo — contexto, hipótese e fontes.\n' + 'x' * 2100 + '  \n'
        self.file.write_bytes(self.value.encode('utf-8'))
        self.repo = Repository(self.root / 'state.sqlite')
        self.reads = Reads()
        self.now = datetime(2026, 10, 24, 18, tzinfo=timezone.utc)
        self.api = ApolloResearchInput(self.repo, KnowledgeStore(self.brain),
            field_reader=self.reads.fields, contact_reader=self.reads.contact_read,
            clock=lambda: self.now)
        self.data = {
            'binding': {'journey_id': 'journey-1', 'journey_version': 3,
                'campaign_id': 'BR-NEW-1', 'apollo_team_id': 'team-1',
                'apollo_contact_id': 'contact-1', 'apollo_company_account_id': 'company-1',
                'source_key': 'lead-1', 'email': 'ana@example.invalid', 'account_slug': 'acme'},
            'source': {'path': 'contas/acme/dossie.md',
                'content_hash': hashlib.sha256(self.value.encode()).hexdigest()},
            'prompt_proposal': {'target': 'new_sequence_only', 'proposal_id': 'new-1',
                'variable_token': '{{contact.thunder_research}}',
                'prompt_template': 'Use {{contact.thunder_research}} como pesquisa.',
                'instructions': 'Não invente fatos.', 'fallback': 'Revisão humana.',
                'generation_options': [], 'generation_tone': 'professional'}}
        self.network = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def prepare(self, version=0):
        return self.api.prepare('input-1', self.data, 'ygor', expected_version=version)

    def approve(self, row):
        return self.api.approve('input-1', 'ygor', expected_version=row['version'],
            input_set_hash=row['input_set_hash'], expires_at=self.now + timedelta(hours=1))

    def test_exact_research_is_readable_and_unmodified(self):
        row = self.prepare()
        self.assertEqual(row['research']['value_exact'], self.value)
        self.assertEqual(row['research']['value_hash'], payload_hash({'value_exact': self.value}))
        self.assertEqual(row['source'], self.data['source'])
        self.assertEqual(self.api.get('input-1', 'ygor'), row)
        self.assertEqual(row['kind'], 'apollo_inputs_v1')
        self.assertFalse(row['apply_enabled'])
        self.assertEqual(row['capability'], 'prepare_only')
        self.assertFalse(row['executable'])
        self.assertFalse(row['final_email_parity'])
        self.assertEqual(row['pilot'], {'max_leads': 10, 'e1_execution_mode': 'manual_email',
            'human_review_location': 'apollo'})

    def test_approval_is_local_exact_and_versioned(self):
        row = self.prepare()
        approved = self.approve(row)
        self.assertEqual(approved['approval']['approved_by'], 'ygor')
        self.assertEqual(approved['approval']['input_set_hash'], row['input_set_hash'])
        self.assertEqual(approved['approval']['input_version'], row['version'])
        self.assertEqual(approved['version'], row['version'] + 1)
        again = self.approve(approved)
        self.assertEqual(again, approved)
        self.assertEqual(self.repo.effects(), [])

    def test_stale_version_hash_and_actor_rejected(self):
        row = self.prepare()
        for actor, version, digest, code in [('agent', row['version'], row['input_set_hash'], 'human_authority_required'),
                ('ygor', 0, row['input_set_hash'], 'version_conflict'),
                ('ygor', row['version'], '0' * 64, 'input_hash_conflict')]:
            with self.subTest(code=code), self.assertRaisesRegex(ApolloResearchInputError, code):
                self.api.approve('input-1', actor, expected_version=version,
                    input_set_hash=digest, expires_at=self.now + timedelta(hours=1))
        self.assertEqual(self.api.get('input-1', 'ygor'), row)

    def test_concurrent_approval_has_one_winner(self):
        row = self.prepare()
        def attempt(_):
            try:
                return self.approve(row)['approval']['approved_by']
            except ApolloResearchInputError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, range(2)))
        self.assertCountEqual(outcomes, ['ygor', 'version_conflict'])

    def test_bad_field_definitions_fail_closed(self):
        cases = [[], [{**self.reads.definition, 'label': 'QualifyContact', 'read_only': True}],
            [self.reads.definition, self.reads.definition],
            [{**self.reads.definition, 'type': 'string'}],
            [{**self.reads.definition, 'modality': 'account'}],
            [{**self.reads.definition, 'read_only': True}],
            [{**self.reads.definition, 'source': 'system'}],
            [{**self.reads.definition, 'team_id': 'other'}],
            [{k: v for k, v in self.reads.definition.items() if k != 'read_only'}],
            [self.reads.definition, {**self.reads.definition, 'label': 'other'}]]
        for index, fields in enumerate(cases):
            self.reads.fields_override = fields
            with self.subTest(index=index):
                old = self.repo.get_state('apollo_research_input', 'input-1')
                row = self.prepare(old['version'] if old else 0)
                self.assertEqual(row['field_status'], 'unavailable')
                self.assertIsNone(row['field_definition'])
                with self.assertRaisesRegex(ApolloResearchInputError, 'field_definition_unavailable'):
                    self.approve(row)

    def test_wrong_contact_bindings_fail_closed(self):
        row = self.prepare()
        for key, value in [('id', 'other'), ('email', 'other@example.invalid'), ('account_id', 'other')]:
            original = deepcopy(self.reads.contact)
            self.reads.contact[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ApolloResearchInputError, 'contact_binding_unavailable'):
                self.approve(row)
            self.reads.contact = original
        self.reads.team = 'other-team'
        with self.assertRaisesRegex(ApolloResearchInputError, 'field_definition_unavailable'):
            self.approve(row)

    def test_reprepare_invalidates_approval(self):
        approved = self.approve(self.prepare())
        self.data['prompt_proposal']['instructions'] += ' Use fontes.'
        updated = self.prepare(approved['version'])
        self.assertNotEqual(updated['input_set_hash'], approved['input_set_hash'])
        self.assertIsNone(updated['approval'])

    def test_brain_and_definition_changes_invalidate_approval(self):
        approved = self.approve(self.prepare())
        self.file.write_bytes(b'changed')
        with self.assertRaisesRegex(ApolloResearchInputError, 'source_hash_conflict'):
            self.approve(approved)
        result = self.api.readback('input-1', 'ygor')
        self.assertEqual(result['approval_status'], 'stale')
        self.assertEqual(result['status'], 'unavailable')
        self.file.write_bytes(self.value.encode())
        self.reads.definition['id'] = 'field-2'
        with self.assertRaisesRegex(ApolloResearchInputError, 'field_definition_changed'):
            self.approve(approved)
        self.assertEqual(self.api.readback('input-1', 'ygor')['approval_status'], 'stale')

    def test_readback_classifies_exact_missing_and_mismatch(self):
        self.approve(self.prepare())
        for value, expected in [(None, 'unavailable'), (self.value.strip(), 'mismatch'),
                (self.value, 'verified'), ({'value': self.value}, 'unavailable'), ('', 'mismatch')]:
            self.reads.contact['typed_custom_fields'] = {'field-1': value}
            with self.subTest(value_type=type(value).__name__, expected=expected):
                view = self.api.readback('input-1', 'ygor')
                self.assertEqual(view['status'], expected)
                self.assertEqual(view['approval_status'], 'approved')
                self.assertEqual(view['delivery_verified'], False)
        self.reads.contact.pop('typed_custom_fields')
        self.assertEqual(self.api.readback('input-1', 'ygor')['status'], 'unavailable')

    def test_provider_failures_are_unavailable_without_sensitive_text(self):
        self.prepare()
        self.reads.error = True
        result = self.api.readback('input-1', 'ygor')
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('sensitive', str(result))

    def test_partial_and_wrong_endpoint_reads_are_not_evidence(self):
        self.prepare()
        self.reads.complete = False
        self.assertEqual(self.api.readback('input-1', 'ygor')['status'], 'unavailable')
        self.reads.complete = True
        with patch.object(self.api, 'contact_reader', lambda *_: self.reads.envelope(
                [self.reads.contact], '/api/v1/contacts/search')):
            self.assertEqual(self.api.readback('input-1', 'ygor')['status'], 'unavailable')

    def test_apply_is_always_disabled(self):
        approved = self.approve(self.prepare())
        self.reads.contact['typed_custom_fields'] = {'field-1': self.value}
        before = deepcopy(self.api.get('input-1', 'ygor'))
        calls = list(self.reads.calls)
        for gate in (False, True):
            result = self.api.apply('input-1', 'ygor', expected_version=approved['version'],
                input_set_hash=approved['input_set_hash'], explicit_gate=gate)
            self.assertEqual(result['status'], 'disabled')
            self.assertEqual(result['reason'], 'existing_one_shot_patch_integration_required')
            self.assertEqual(result['writes'], 0)
            self.assertEqual(result['capability'], 'prepare_only')
            self.assertFalse(result['executable'])
        self.assertEqual(self.reads.calls, calls)
        self.assertEqual(self.api.get('input-1', 'ygor'), before)
        self.assertEqual(self.repo.effects(), [])

    def test_existing_approval_pack_is_untouched(self):
        old = self.repo.put_state('approval', 'legacy', {'kind': 'exact_copy_v1', 'content': 'unchanged'})
        self.approve(self.prepare())
        self.assertEqual(self.repo.get_state('approval', 'legacy'), old)
        self.assertEqual(self.repo.list_state('journey_draft'), [])
        self.assertEqual(self.repo.list_state('cadence'), [])

    def test_paths_hashes_and_new_sequence_scope_are_validated(self):
        for path in ('../outside.md', '/tmp/outside.md', 'contas/other/dossie.md'):
            self.data['source']['path'] = path
            with self.subTest(path=path), self.assertRaises(ApolloResearchInputError):
                self.prepare()
        self.data['source']['path'] = 'contas/acme/dossie.md'
        self.data['source']['content_hash'] = '0' * 64
        with self.assertRaisesRegex(ApolloResearchInputError, 'source_hash_conflict'):
            self.prepare()
        self.data['source']['content_hash'] = hashlib.sha256(self.value.encode()).hexdigest()
        self.data['prompt_proposal']['target'] = 'existing_sequence'
        with self.assertRaisesRegex(ApolloResearchInputError, 'new_sequence_only'):
            self.prepare()
        self.assertEqual(self.repo.list_state('apollo_research_input'), [])

    def test_expired_approval_is_not_current(self):
        self.approve(self.prepare())
        self.now += timedelta(hours=2)
        result = self.api.readback('input-1', 'ygor')
        self.assertEqual(result['approval_status'], 'expired')

    def test_all_public_operations_require_authenticated_ygor(self):
        row = self.prepare()
        for call in [lambda: self.api.prepare('input-1', self.data, 'agent', expected_version=row['version']),
                lambda: self.api.get('input-1', 'agent'),
                lambda: self.api.readback('input-1', 'agent'),
                lambda: self.api.apply('input-1', 'agent', expected_version=row['version'],
                    input_set_hash=row['input_set_hash'], explicit_gate=True)]:
            with self.assertRaisesRegex(ApolloResearchInputError, 'human_authority_required'):
                call()
        self.assertEqual(self.api.get('input-1', 'ygor'), row)

    def test_each_prompt_and_identity_input_is_hashed(self):
        baseline = self.prepare()
        prompt_changes = {'proposal_id': 'new-2', 'prompt_template': 'Different {{contact.thunder_research}}',
            'instructions': 'Different rules', 'fallback': 'Different fallback',
            'generation_tone': 'formal', 'generation_options': [{'field_name': 'other'}]}
        binding_changes = {'journey_id': 'journey-2', 'journey_version': 4, 'campaign_id': 'BR-OTHER',
            'apollo_team_id': 'team-2', 'apollo_contact_id': 'contact-2',
            'apollo_company_account_id': 'company-2', 'source_key': 'lead-2', 'email': 'other@example.invalid'}
        for section, changes in [('prompt_proposal', prompt_changes), ('binding', binding_changes)]:
            for key, value in changes.items():
                data = deepcopy(self.data)
                data[section][key] = value
                with self.subTest(section=section, key=key):
                    row = self.api.prepare('check-' + key, data, 'ygor')
                    self.assertNotEqual(row['input_set_hash'], baseline['input_set_hash'])
        data = deepcopy(self.data)
        data['prompt_proposal']['variable_token'] = '{{thunder_research}}'
        data['prompt_proposal']['prompt_template'] += ' {{thunder_research}}'
        self.assertNotEqual(self.api.prepare('new-token', data, 'ygor')['input_set_hash'], baseline['input_set_hash'])

    def test_observation_metadata_is_not_input_authority(self):
        baseline = self.prepare()
        self.reads.definition['observed_at'] = 'later'
        self.reads.definition['statistics'] = {'usage': 10}
        self.now += timedelta(minutes=1)
        again = self.prepare(baseline['version'])
        self.assertEqual(again['input_set_hash'], baseline['input_set_hash'])
        self.reads.definition['meta'] = {'max_length': 9999}
        changed = self.prepare(again['version'])
        self.assertNotEqual(changed['input_set_hash'], baseline['input_set_hash'])

    def test_changed_source_and_value_are_not_approved_as_old_input(self):
        approved = self.approve(self.prepare())
        self.value += 'New source evidence\n'
        self.file.write_bytes(self.value.encode())
        self.data['source']['content_hash'] = hashlib.sha256(self.value.encode()).hexdigest()
        row = self.prepare(approved['version'])
        self.assertNotEqual(row['research']['value_hash'], approved['research']['value_hash'])
        self.assertNotEqual(row['input_set_hash'], approved['input_set_hash'])
        self.assertIsNone(row['approval'])
        self.assertEqual(row['research']['value_exact'], self.value)

    def test_readback_requires_contact_identity_even_if_value_matches(self):
        self.approve(self.prepare())
        self.reads.contact['typed_custom_fields'] = {'field-1': self.value}
        self.reads.contact['team_id'] = 'wrong-team'
        view = self.api.readback('input-1', 'ygor')
        self.assertEqual(view['status'], 'unavailable')
        self.assertFalse(view['readback_verified'])
        self.assertEqual(view['reason'], 'contact_binding_unavailable')

    def test_no_read_ports_means_explicit_unavailable(self):
        api = ApolloResearchInput(self.repo, KnowledgeStore(self.brain), clock=lambda: self.now)
        row = api.prepare('offline', self.data, 'ygor')
        self.assertEqual(row['field_status'], 'unavailable')
        self.assertEqual(row['contact_binding_status'], 'unavailable')
        with self.assertRaisesRegex(ApolloResearchInputError, 'field_definition_unavailable'):
            api.approve('offline', 'ygor', expected_version=row['version'],
                input_set_hash=row['input_set_hash'], expires_at=self.now + timedelta(hours=1))

    def test_invalid_or_expired_authority_cannot_mutate_state(self):
        row = self.prepare()
        for expiry in (None, self.now, self.now - timedelta(seconds=1), self.now.replace(tzinfo=None)):
            with self.subTest(expiry=expiry), self.assertRaisesRegex(ApolloResearchInputError, 'approval_expiry_invalid'):
                self.api.approve('input-1', 'ygor', expected_version=row['version'],
                    input_set_hash=row['input_set_hash'], expires_at=expiry)
        self.data['prompt_proposal']['generation_options'] = [{'bad': '\ud800'}]
        with self.assertRaisesRegex(ApolloResearchInputError, 'prompt_invalid'):
            self.prepare(row['version'])
        self.assertEqual(self.api.get('input-1', 'ygor'), row)

    def test_local_edit_during_provider_read_invalidates_old_review(self):
        approved = self.approve(self.prepare())
        original_reader = self.api.contact_reader
        def concurrent_edit(*args):
            current = self.repo.get_state('apollo_research_input', 'input-1')
            self.repo.put_state('apollo_research_input', 'input-1',
                {k: v for k, v in current.items() if k not in {'id', 'version', 'updated_at'}})
            return original_reader(*args)
        self.api.contact_reader = concurrent_edit
        with self.assertRaisesRegex(ApolloResearchInputError, 'version_conflict'):
            self.approve(approved)
        view = self.api.readback('input-1', 'ygor')
        self.assertEqual(view['status'], 'unavailable')
        self.assertEqual(view['reason'], 'version_conflict')
        self.assertEqual(view['approval_status'], 'stale')

    def test_invalid_provider_metadata_never_becomes_a_definition(self):
        for meta in ({'bad': '\ud800'}, {'bad': float('nan')}):
            self.reads.definition['meta'] = meta
            old = self.repo.get_state('apollo_research_input', 'input-1')
            row = self.prepare(old['version'] if old else 0)
            self.assertEqual(row['field_status'], 'unavailable')

    def test_authorized_visitor_dossier_root_preserves_exact_content(self):
        self.data['binding']['account_slug'] = 'vicunha'
        relative = '_entrada/claude/cadencias-visitantes-2026-10-22/vicunha/cadencia.md'
        dossier = self.brain / relative
        dossier.parent.mkdir(parents=True)
        dossier.write_bytes(self.value.encode())
        self.data['source']['path'] = relative
        row = self.prepare()
        self.assertEqual(row['source']['path'], relative)
        self.assertEqual(row['research']['value_exact'], self.value)
        self.assertEqual(row['capability'], 'prepare_only')
        self.assertFalse(row['executable'])

    def test_visitor_root_does_not_grant_other_dates_accounts_or_files(self):
        root = '_entrada/claude/cadencias-visitantes-2026-10-22/'
        for relative in [root + 'other/cadencia.md', root + 'acme/other.md',
                root.replace('2026-10-22', '2026-10-23') + 'acme/cadencia.md',
                '_entrada/claude/other/acme/cadencia.md', root + 'acme/../acme/cadencia.md']:
            self.data['source']['path'] = relative
            with self.subTest(path=relative), self.assertRaisesRegex(ApolloResearchInputError, 'source_path_invalid'):
                self.prepare()

    def test_intermediate_symlink_swap_cannot_redirect_source_read(self):
        original = os.open
        target = self.brain / 'contas/acme'
        outside = self.root / 'foreign'
        outside.mkdir()
        (outside / 'dossie.md').write_bytes(self.value.encode())
        def swap(path, flags, *args, **kwargs):
            if path == 'acme':
                target.rename(target.with_name('preserved'))
                target.symlink_to(outside, target_is_directory=True)
            return original(path, flags, *args, **kwargs)
        with patch('thunder_os.apollo_research_input.os.open', side_effect=swap):
            with self.assertRaisesRegex(ApolloResearchInputError, 'source_unavailable'):
                self.prepare()

    def test_symlink_escape_and_oversize_are_rejected_without_truncation(self):
        self.file.unlink()
        outside = self.root / 'outside.md'
        outside.write_text('private')
        self.file.symlink_to(outside)
        with self.assertRaisesRegex(ApolloResearchInputError, 'source_unavailable'):
            self.prepare()
        self.file.unlink()
        self.file.write_bytes(b'x' * (256 * 1024 + 1))
        with self.assertRaisesRegex(ApolloResearchInputError, 'source_too_large'):
            self.prepare()


if __name__ == '__main__':
    unittest.main()
