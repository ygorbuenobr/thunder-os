"""S28 acceptance checks, synthetic data only; no network or operational state.

Proofs: profile/CRM binding, exclusive source, freshness, literal content,
historical CRM isolation, missing data, publication dates and plain text.
Independent feature verification belongs to the coordinating agent.
"""
from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

from thunder_os.commercial_account import CommercialAccountReadonly


PROFILE = 'https://www.linkedin.com/in/synthetic-person'
TEXT = ('# Synthetic company | Synthetic person\n' + PROFILE + '/\n'
        '## Empresa\nVende componentes.\n')


def document(ident='source-a'):
    return {'id': ident, 'company': 'Synthetic company', 'contact': 'Synthetic person, CMO',
        'source': {'path': 'contas/synthetic/cadencia.md',
                   'sha256': hashlib.sha256(TEXT.encode()).hexdigest(),
                   'updated_at': '2026-10-25T12:00:00Z'},
        'source_status': 'observed', 'source_text': TEXT, 'thesis': 'Uma tese literal?',
        'tabs': {'research': [
            {'text': '**Empresa**'},
            {'text': '- Tecelagem: vende componentes para fabricantes.', 'source_url': 'https://example.test/news',
             'published_on': '2000-01-01', 'label': 'contexto'},
            {'text': '**Histórico no HubSpot**'},
            {'text': '- Busca 22/10/2026: nenhum registro. HubSpot está vazio.'},
            {'text': '**Fit e oferta**'}, {'text': '- Fit: indústria B2B.'},
            {'text': '- E-mail corporativo: não levantado.'}],
            'dossier': [
                {'key': 'M1', 'text': 'Expansão anunciada.', 'classification': 'nao_classificado', 'sources': []},
                {'key': 'M2', 'text': 'Preferência pode ajudar?', 'classification': 'hipotese', 'sources': []},
                {'key': 'M4', 'text': 'Prova literal. Limite: outro setor.', 'classification': 'nao_classificado', 'sources': ['https://example.test/proof']},
                {'key': 'M5', 'text': 'Perguntas em reserva.', 'classification': 'reserva', 'sources': []}],
            'context': [], 'cadence': [{'text': '<script>alert(1)</script>', 'status': 'draft'}],
            'before_approval': [{'text': 'Confirmar responsável.', 'blocking': True},
                                {'text': 'Obter e-mail; HubSpot está vazio.', 'blocking': True}]},
        'limitations': ['source_claims_not_independently_verified']}


class Accounts:
    def __init__(self, docs=None):
        self.docs = docs if docs is not None else [document()]

    def list_accounts(self):
        return {'accounts': deepcopy(self.docs), 'limitations': []}

    def account(self, ident):
        return deepcopy(next(d for d in self.docs if d['id'] == ident))


class CommercialAccountTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            mock = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(mock.assert_not_called)
        self.row = {'name': 'Synthetic person', 'company': 'Synthetic company',
                    'linkedin_url': PROFILE + '/', 'hubspot_id': 'contact-1'}
        self.context = {'company_id': 'company-1', 'contact_id': 'contact-1',
                        'domain': 'synthetic.test', 'verified_linkedin_urls': [PROFILE]}
        self.accounts = Accounts()

    def project(self):
        return CommercialAccountReadonly(self.accounts).project(self.row, self.context)

    def test_available_literal_sections_and_originals(self):
        result = self.project()
        self.assertEqual(result['status'], 'available')
        self.assertEqual([s['key'] for s in result['sections']],
                         ['business', 'fit', 'moment', 'people', 'thesis', 'next_action'])
        self.assertEqual(result['originals']['tabs'], self.accounts.docs[0]['tabs'])
        self.assertEqual(result['originals']['source_text'], TEXT)
        items = [i for s in result['sections'] for i in s['items']]
        self.assertIn('Preferência pode ajudar?', [i['text'] for i in items if i['classification'] == 'hipotese'])
        self.assertIn('Perguntas em reserva.', [i['text'] for i in items if i['classification'] == 'reserva'])
        self.assertIn('Prova literal. Limite: outro setor.', [i['text'] for i in items])
        self.assertIn('binding_via_verified_profile_and_crm_not_markdown_domain', result['limitations'])

    def test_no_identity_and_contact_id_mismatch(self):
        for field in ('company_id', 'contact_id', 'domain', 'verified_linkedin_urls'):
            context = {k: v for k, v in self.context.items() if k != field}
            self.assertEqual(CommercialAccountReadonly(self.accounts).project(self.row, context)['status'], 'unlinked')
        self.row['hubspot_id'] = 'different'
        self.assertEqual(self.project()['status'], 'unlinked')

    def test_profile_mismatch_never_falls_back_to_name(self):
        self.row['linkedin_url'] = 'https://www.linkedin.com/in/other'
        self.assertEqual(self.project()['status'], 'unlinked')
        self.context['verified_linkedin_urls'] = [self.row['linkedin_url']]
        self.assertEqual(self.project()['status'], 'unlinked')

    def set_source(self, text):
        self.accounts.docs[0]['source_text'] = text
        self.accounts.docs[0]['source']['sha256'] = hashlib.sha256(text.encode()).hexdigest()

    def test_alternative_profile_never_inherits_primary_person_dossier(self):
        alternate = 'https://www.linkedin.com/in/synthetic-alternative'
        self.set_source('# Cadência\n## Pessoa e empresa\n**Pessoa abordada: Primary**\n'
                        '- Cargo atual: ' + PROFILE + '\n\n**Por que ele e não a visitante**\n'
                        '- Outros nomes: ' + alternate + '\n## Dossiê\nM7: ' + alternate)
        self.assertEqual(self.project()['status'], 'available')
        self.row['linkedin_url'] = alternate
        self.context['verified_linkedin_urls'] = [alternate]
        self.assertEqual(self.project()['status'], 'unlinked')

    def test_ambiguous_primary_profiles_are_retained(self):
        alternate = 'https://www.linkedin.com/in/synthetic-alternative'
        self.set_source('# Cadência\n## Pessoa\n**Pessoa abordada: Primary**\n' + PROFILE +
                        '\n' + alternate + '\n**Empresa**\nLiteral.')
        self.assertEqual(self.project()['status'], 'ambiguous')

    def test_late_url_without_primary_metadata_never_falls_back(self):
        self.set_source('# Cadência\n## Pesquisa\nPerfil relacionado: ' + PROFILE)
        self.assertEqual(self.project()['status'], 'unlinked')

    def test_primary_metadata_and_section_boundary(self):
        self.set_source('linkedin_url: ' + PROFILE + '\n# Cadência\n## Dossiê\n'
                        'M7: https://www.linkedin.com/in/synthetic-alternative')
        self.assertEqual(self.project()['status'], 'available')
        self.set_source('# Cadência\n## Pessoa\n**Pessoa abordada: Primary**\n' + PROFILE +
                        '\n## Outros\nhttps://www.linkedin.com/in/synthetic-alternative')
        self.assertEqual(self.project()['status'], 'available')

    def test_profile_host_and_path_cannot_be_spoofed(self):
        for bad in ('https://linkedin.com.evil.test/in/synthetic-person',
                    'https://www.linkedin.com/company/synthetic-person',
                    'https://www.linkedin.com:444/in/synthetic-person',
                    'https://user@www.linkedin.com/in/synthetic-person'):
            with self.subTest(bad=bad):
                self.row['linkedin_url'] = bad
                self.context['verified_linkedin_urls'] = [bad]
                self.assertEqual(self.project()['status'], 'unlinked')

    def test_multiple_documents_are_ambiguous(self):
        self.accounts.docs.append(document('source-b'))
        result = self.project()
        self.assertEqual(result['status'], 'ambiguous')
        self.assertIsNone(result['originals']['account_id'])

    def test_normalized_profile_and_missing_row_id(self):
        self.row.pop('hubspot_id')
        self.row['linkedin_url'] = 'https://linkedin.com/in/SYNTHETIC-PERSON/?trk=test'
        self.assertEqual(self.project()['status'], 'available')

    def test_unique_canonical_profile_without_csv_and_conflicts_retained(self):
        self.row.pop('linkedin_url')
        self.assertEqual(self.project()['status'], 'available')
        self.context['verified_linkedin_urls'].append('https://www.linkedin.com/in/another-primary')
        self.assertEqual(self.project()['status'], 'ambiguous')
        self.row['linkedin_url'] = 'https://www.linkedin.com/in/conflicting-csv'
        self.assertEqual(self.project()['status'], 'unlinked')

    def test_historical_block_descendants_never_enter_business(self):
        self.accounts.docs[0]['tabs']['research'] = [
            {'text': '**Empresa**'}, {'text': 'Vende componentes.'},
            {'text': '**Histórico no HubSpot**'},
            {'text': '- Empresa sem negócio aberto em 2020.'},
            {'text': '**Outros registros antigos**'}, {'text': '- Empresa antiga sem contato.'},
            {'text': '**Fit e oferta**'}, {'text': '- Fit: indústria B2B.'}]
        result = self.project()
        primary = ' '.join(i['text'] for s in result['sections'] for i in s['items'])
        self.assertNotIn('sem negócio aberto', primary)
        self.assertNotIn('Empresa antiga', primary)
        self.assertIn('- Fit: indústria B2B.', primary)
        self.assertEqual(result['originals']['tabs'], self.accounts.docs[0]['tabs'])

    def test_real_reader_paragraph_headings_preserve_content_and_history_boundary(self):
        from pathlib import Path
        import tempfile
        from thunder_os.account_cadence_readonly import AccountCadenceReadonly
        text = ('# Cadência · Synthetic · Person\n' + PROFILE + '\n\n## Pesquisa\n\n'
                '**Empresa**\nVende componentes industriais.\n\n'
                '**Histórico no HubSpot**\n- Empresa sem negócio aberto em 2020.\n\n'
                '**Fit e oferta**\n- Fit: indústria B2B.\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contas/synthetic/cadencia.md'
            path.parent.mkdir(parents=True)
            path.write_text(text, encoding='utf-8')
            result = CommercialAccountReadonly(AccountCadenceReadonly(directory)).project(self.row, self.context)
            self.assertEqual(result['status'], 'available')
            sections = {s['key']: s['items'] for s in result['sections']}
            self.assertEqual(sections['business'][0]['text'], 'Vende componentes industriais.')
            self.assertEqual(sections['fit'][0]['text'], '- Fit: indústria B2B.')
            self.assertNotIn('sem negócio aberto', repr(result['sections']))
            self.assertEqual(result['originals']['source_text'], text)

    def test_stale_crm_and_email_not_promoted(self):
        result = self.project()
        primary = ' '.join(i['text'] for s in result['sections'] for i in s['items'])
        self.assertNotIn('HubSpot está vazio', primary)
        self.assertNotIn('não levantado', primary)
        self.assertNotIn('Obter e-mail', primary)
        self.assertIn('historical_crm_and_email_claims_only_in_originals', result['limitations'])

    def test_thesis_is_hypothesis_and_pending_actions_precede_reserve(self):
        result = self.project()
        sections = {s['key']: s['items'] for s in result['sections']}
        self.assertEqual(sections['thesis'][0]['classification'], 'hipotese')
        self.assertEqual(sections['thesis'][0]['text'], 'Uma tese literal?')
        self.assertEqual(sections['next_action'][0]['text'], 'Confirmar responsável.')
        self.assertEqual(sections['next_action'][-1]['text'], 'Perguntas em reserva.')
        self.assertIn('manual_next_action_owner_unknown', result['limitations'])
        self.assertIn('manual_next_action_date_unknown', result['limitations'])

    def test_catalog_foreign_warnings_not_in_selected_document(self):
        warnings = ['source_directory_missing:contas', 'dossier_missing:M7',
                    'campaign_unassigned:contas/other/cadencia.md',
                    'campaign_unassigned:contas/synthetic/cadencia.md']
        self.accounts.list_accounts = lambda: {'accounts': deepcopy(self.accounts.docs), 'limitations': warnings}
        self.accounts.docs[0]['limitations'] += warnings + ['touch_stop_condition_unknown:D3:2']
        result = self.project()
        self.assertNotIn('dossier_missing:M7', result['limitations'])
        self.assertNotIn('campaign_unassigned:contas/other/cadencia.md', result['limitations'])
        self.assertIn('source_directory_missing:contas', result['limitations'])
        self.assertIn('campaign_unassigned:contas/synthetic/cadencia.md', result['limitations'])
        self.assertIn('touch_stop_condition_unknown:D3:2', result['limitations'])

    def test_real_reader_document_warnings_do_not_leak_across_details(self):
        from pathlib import Path
        import tempfile
        from thunder_os.account_cadence_readonly import AccountCadenceReadonly
        with tempfile.TemporaryDirectory() as directory:
            for name, content in [('a', '# Cadência · Other · Other\n'),
                                  ('b', 'linkedin_url: ' + PROFILE + '\n# Cadência · Synthetic · Person\n\n## Pesquisa\n' + PROFILE +
                                   '\n\n## Dossiê\n' + '\n'.join('M' + str(n) + ': Literal.' for n in range(1, 8)))]:
                path = Path(directory) / 'contas' / name / 'cadencia.md'
                path.parent.mkdir(parents=True)
                path.write_text(content, encoding='utf-8')
            result = CommercialAccountReadonly(AccountCadenceReadonly(directory)).project(self.row, self.context)
            self.assertEqual(result['status'], 'available')
            self.assertFalse(any(v.startswith('dossier_missing:') for v in result['limitations']))
            self.assertFalse(any('contas/a/' in v for v in result['limitations']))

    def test_old_date_context_and_mtime_not_publication(self):
        result = self.project()
        business = result['sections'][0]['items'][0]
        self.assertTrue(business['context_only'])
        self.assertEqual(business['published_on'], '2000-01-01')
        self.assertTrue(all(i['published_on'] != result['source']['updated_at']
                            for s in result['sections'] for i in s['items']))

    def test_xss_stays_plain_text_and_inputs_are_unchanged(self):
        self.accounts.docs[0]['tabs']['research'][1]['text'] = '<script>alert(1)</script>'
        before = deepcopy(self.accounts.docs)
        result = self.project()
        self.assertEqual(result['sections'][0]['items'][0]['text'], '<script>alert(1)</script>')
        self.assertEqual(self.accounts.docs, before)
        result['originals']['tabs']['cadence'].clear()
        self.assertEqual(self.accounts.docs, before)

    def test_source_hash_mismatch_unavailable(self):
        self.accounts.docs[0]['source']['sha256'] = '0' * 64
        self.assertEqual(self.project()['status'], 'unavailable')

    def test_catalog_limit_or_incomplete_discovery_is_unavailable(self):
        self.accounts.list_accounts = lambda: {'accounts': [document()] * 501, 'limitations': []}
        self.assertEqual(self.project()['status'], 'unavailable')
        self.accounts.list_accounts = lambda: {'accounts': [document()], 'limitations': ['source_file_limit']}
        self.assertEqual(self.project()['status'], 'unavailable')

    def test_unreadable_source_cannot_prove_exclusive_match(self):
        broken = document('broken')
        broken.pop('source_text')
        broken['source_status'] = 'incomplete'
        self.accounts.docs.append(broken)
        self.assertEqual(self.project()['status'], 'unavailable')

    def test_changed_source_between_list_and_detail_unavailable(self):
        original = self.accounts.account
        def changed(ident):
            d = original(ident)
            d['source']['updated_at'] = '2026-10-26T00:00:00Z'
            return d
        self.accounts.account = changed
        self.assertEqual(self.project()['status'], 'unavailable')

    def test_missing_sections_visibly_pending(self):
        self.accounts.docs[0]['tabs'] = {}
        self.accounts.docs[0]['thesis'] = None
        result = self.project()
        self.assertEqual(result['status'], 'available')
        self.assertTrue(all(s['items'] for s in result['sections']))
        self.assertIn('commercial_section_pending:business', result['limitations'])

    def test_reader_failure_is_unavailable_without_exception_details(self):
        def fail():
            raise RuntimeError('private error')
        self.accounts.list_accounts = fail
        result = self.project()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('private error', repr(result))

    def test_many_catalog_and_details_read_once_per_request(self):
        projector = CommercialAccountReadonly(self.accounts)
        rows = [{**self.row, 'source_key': 'csv:' + str(n)} for n in range(200)]
        contexts = {r['source_key']: self.context for r in rows}
        with patch.object(self.accounts, 'list_accounts', wraps=self.accounts.list_accounts) as catalog:
            with patch.object(self.accounts, 'account', wraps=self.accounts.account) as detail:
                result = projector.project_many(rows, contexts)
                self.assertEqual(catalog.call_count, 1)
                self.assertEqual(detail.call_count, 1)
        self.assertEqual(len(result), 200)
        self.assertTrue(all(p['status'] == 'available' for p in result.values()))
        self.assertEqual(result['csv:0']['binding_kind'], 'verified_linkedin_and_hubspot_company')
        result['csv:0']['originals']['tabs'].clear()
        self.assertTrue(result['csv:1']['originals']['tabs'])

    def test_many_does_not_reuse_prior_source_or_context(self):
        projector = CommercialAccountReadonly(self.accounts)
        row = {**self.row, 'source_key': 'csv:1'}
        self.assertEqual(projector.project_many([row], {'csv:1': self.context})['csv:1']['status'], 'available')
        self.accounts.docs.clear()
        self.assertEqual(projector.project_many([row], {'csv:1': self.context})['csv:1']['status'], 'unlinked')
        self.accounts.docs.append(document())
        self.assertEqual(projector.project_many([row], {})['csv:1']['status'], 'unlinked')

    def test_many_limit_and_duplicate_source_keys(self):
        projector = CommercialAccountReadonly(self.accounts)
        row = {**self.row, 'source_key': 'csv:1'}
        with self.assertRaisesRegex(ValueError, 'commercial_rows_limit_or_invalid'):
            projector.project_many([row] * 201, {})
        result = projector.project_many([row, row], {'csv:1': self.context})
        self.assertEqual(result['csv:1']['status'], 'ambiguous')

    def test_real_injected_reader_discovers_catalog_only_once(self):
        from pathlib import Path
        import tempfile
        from thunder_os.account_cadence_readonly import AccountCadenceReadonly
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contas/synthetic/cadencia.md'
            path.parent.mkdir(parents=True)
            path.write_text('linkedin_url: ' + PROFILE + '\n# Cadência · Synthetic company · Synthetic person\n\n'
                            '## Pesquisa\n' + PROFILE + '\n', encoding='utf-8')
            accounts = AccountCadenceReadonly(directory)
            rows = [{**self.row, 'source_key': 'csv:' + str(n)} for n in range(3)]
            original_sources = AccountCadenceReadonly._sources
            with patch.object(AccountCadenceReadonly, '_sources', autospec=True,
                              wraps=None) as scan:
                # The original implementation remains the actual source reader.
                scan.side_effect = original_sources
                result = CommercialAccountReadonly(accounts).project_many(
                    rows, {r['source_key']: self.context for r in rows})
            self.assertEqual(scan.call_count, 1)
            self.assertTrue(all(p['status'] == 'available' for p in result.values()))


if __name__ == '__main__':
    unittest.main()
