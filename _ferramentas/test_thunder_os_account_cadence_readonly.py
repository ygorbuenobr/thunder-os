"""Synthetic fixtures only; no provider, network or operational database access."""
from datetime import date, datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from thunder_os import account_cadence_readonly as module
from thunder_os.account_cadence_readonly import AccountCadenceReadonly


DOCUMENT = """---
account_id: example.test
campaign_id: CAMPAIGN-A
company: Fabricante Exemplo
---
# Cad\u00eancia \u00b7 Fabricante Exemplo \u00b7 Pessoa Exemplo

## Pesquisa ABM
| Fato | Fonte | Data de publica\u00e7\u00e3o |
|---|---|---|
| Fato recente, sem reformula\u00e7\u00e3o. | https://example.test/news | 23/10/2026 |
| Fato antigo. | https://example.test/old | 22/10/2025 |
| Anivers\u00e1rio exato. | https://example.test/boundary | 23/10/2025 |
| Data n\u00e3o informada. | https://example.test/no-date | consultada em 23/10/2026 |

## Dossi\u00ea de muni\u00e7\u00e3o
| Muni\u00e7\u00e3o | Conte\u00fado | Fonte | Classifica\u00e7\u00e3o |
|---|---|---|---|
| M1 Gatilho | Um fato `com|pipe` e barra \\| literal. | https://example.test/news | fato |
| M2 Tens\u00e3o | Uma pergunta que permanece hip\u00f3tese. | interno | hip\u00f3tese |
| M3 Mecanismo | N\u00e3o classificar por n\u00famero M3. | interno | |
| M4 Prova | N\u00famero documentado sem extrapola\u00e7\u00e3o. | https://example.test/proof | fato |
| M5 Valor | Recurso reservado. | interno | reserva |
| M6 Tema | Outra hip\u00f3tese. | interno | hip\u00f3tese |
| M7 Alternativa | Reserva, sem contato. | interno | reserva |

## Mapa
| Toque | Data | Canal | Muni\u00e7\u00e3o | Condi\u00e7\u00e3o de parada |
|---|---|---|---|---|
| D0 | 26/10/2026 | E-mail, thread nova | M1, M2 | parar se responder |
| D3 | 29/10 | E-mail na thread | M3 | parar se responder |

## D0 \u00b7 e-mail \u00b7 26/10
**Assunto:** Tema literal

Ol\u00e1, Pessoa.

Texto exato com  dois espa\u00e7os, <script>n\u00e3o executar()</script> e &amp;.

Atenciosamente,
Autor

*(12 palavras \u00b7 parar se responder)*

## D3 \u00b7 e-mail na thread \u00b7 29/10
Texto seguinte sem data de ano presumida.

## Contexto usado
thunder/provas.md SHA256 {proof_hash}
eu/estilo-e-regras.md

## Antes de aprovar
- [bloqueante] Confirmar canal.
- [contexto] Refer\u00eancia informativa.
"""


class AccountCadenceReadonlyTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            port = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(port.assert_not_called)
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve() / 'brain'
        self.root.mkdir()
        self.put('thunder/provas.md', 'Prova sint\u00e9tica.\n')
        self.put('eu/estilo-e-regras.md', 'Estilo sint\u00e9tico.\n')
        self.proof_hash = hashlib.sha256(b'Prova sint\xc3\xa9tica.\n').hexdigest()
        self.document = DOCUMENT.format(proof_hash=self.proof_hash)
        self.path = 'contas/fabricante-exemplo/cadencia.md'
        self.put(self.path, self.document)
        self.reader = AccountCadenceReadonly(self.root, clock=lambda: date(2026, 10, 23))

    def put(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='')
        return path

    def view(self, reader=None):
        return (reader or self.reader).account('example.test', campaign='CAMPAIGN-A')

    def test_identity_campaign_and_no_prototype_fallback(self):
        result = self.reader.list_accounts('CAMPAIGN-A')
        self.assertEqual(len(result['accounts']), 1)
        row = result['accounts'][0]
        self.assertEqual((row['id'], row['company']), ('example.test', 'Fabricante Exemplo'))
        self.assertEqual(row['source']['path'], self.path)
        self.assertEqual(self.reader.list_accounts('CAMPAIGN-B')['accounts'], [])
        self.assertEqual(self.reader.account('absent')['source_status'], 'incomplete')
        self.assertEqual(self.reader.account('absent')['tabs']['cadence'], [])

    def test_account_date_uses_open_source_mtime_with_explicit_provenance(self):
        timestamp = datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc).timestamp()
        os.utime(self.root / self.path, (timestamp, timestamp))
        expected = '2024-02-03T04:05:06+00:00'
        row = self.reader.list_accounts('CAMPAIGN-A')['accounts'][0]
        detail = self.view()
        for observed in (row, detail):
            self.assertEqual(observed['date'], expected)
            self.assertEqual(observed['source']['updated_at'], expected)
            self.assertEqual(observed['date_basis'], 'source_file')
            self.assertEqual(observed['date_sourcepath'], self.path)
        # File recency never fills a missing year in a planned touch.
        self.assertIsNone(detail['tabs']['cadence'][1]['date'])

    def test_missing_source_never_uses_clock_state_or_directory_as_date(self):
        reader = AccountCadenceReadonly(self.root, {'cadences': [{
            'account_id': 'missing-2026-10-22', 'campaign_id': 'CAMPAIGN-A',
            'updated_at': '2026-10-23T10:00:00Z'}]}, clock=lambda: date(2035, 1, 2))
        row = next(r for r in reader.list_accounts()['accounts'] if r['id'] == 'missing-2026-10-22')
        for observed in (row, reader.account(row['id'])):
            self.assertIsNone(observed.get('date'))
            self.assertIsNone(observed.get('date_basis'))
            self.assertIsNone(observed.get('date_sourcepath'))

    def test_source_directory_and_clock_are_not_account_dates(self):
        path = 'contas/source-2020-01-01/cadencia.md'
        self.put(path, self.document.replace('example.test', 'dated-path.test'))
        timestamp = datetime(2023, 7, 8, 9, 10, 11, tzinfo=timezone.utc).timestamp()
        os.utime(self.root / path, (timestamp, timestamp))
        reader = AccountCadenceReadonly(self.root, clock=lambda: date(2035, 1, 2))
        observed = reader.account('dated-path.test')
        self.assertEqual(observed['date'], '2023-07-08T09:10:11+00:00')
        self.assertEqual(observed['date_basis'], 'source_file')
        self.assertEqual(observed['date_sourcepath'], path)

    def test_exact_body_subject_and_raw_source_hash(self):
        result = self.view()
        first = result['tabs']['cadence'][0]
        self.assertEqual(first['subject'], 'Tema literal')
        self.assertEqual(first['text'], 'Ol\u00e1, Pessoa.\n\nTexto exato com  dois espa\u00e7os, <script>n\u00e3o executar()</script> e &amp;.\n\nAtenciosamente,\nAutor')
        self.assertEqual(first['ammunition'], 'M1, M2')
        self.assertEqual(first['stop_condition'], 'parar se responder')
        self.assertEqual(first['date'], '2026-10-26')
        self.assertIsNone(result['tabs']['cadence'][1]['date'])
        self.assertEqual(first['status'], 'draft')
        digest = hashlib.sha256(self.document.encode()).hexdigest()
        self.assertEqual(result['source']['sha256'], digest)
        self.assertEqual(first['source_hash'], digest)
        self.assertEqual(result['source_text'], self.document)

    def test_calendar_month_boundary_and_unknown_publication(self):
        rows = self.view()['tabs']['research']
        self.assertEqual([r['label'] for r in rows], ['fato', 'contexto', 'fato', 'data desconhecida'])
        self.assertIsNone(rows[-1]['published_on'])
        self.assertEqual(module._year_ago(date(2024, 2, 29)), date(2023, 2, 28))
        self.assertEqual(module._year_ago(date(2026, 3, 1)), date(2025, 3, 1))

    def test_dossier_classification_and_escaped_pipes(self):
        dossier = self.view()['tabs']['dossier']
        self.assertEqual([r['key'] for r in dossier], ['M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7'])
        self.assertEqual([r['classification'] for r in dossier], ['fato', 'hipotese', 'nao_classificado', 'fato', 'reserva', 'hipotese', 'reserva'])
        self.assertEqual(dossier[0]['text'], 'Um fato `com|pipe` e barra \\| literal.')

    def test_missing_dossier_is_explicit_and_not_fabricated(self):
        self.put(self.path, '# Cad\u00eancia \u00b7 Fabricante Exemplo \u00b7 Pessoa Exemplo\n')
        account = self.reader.list_accounts()['accounts'][0]
        result = self.reader.account(account['id'])
        self.assertTrue(all(item['text'] is None for item in result['tabs']['dossier']))
        self.assertIn('dossier_missing:M7', result['limitations'])
        self.assertIn('cadence_text_missing', result['limitations'])

    def test_context_expected_current_hash_and_unproven_author_read(self):
        result = self.view()
        context = {row['path']: row for row in result['tabs']['context']}
        self.assertEqual(context[self.path]['usage'], 'observed')
        proof = context['thunder/provas.md']
        self.assertEqual(proof['status'], 'ok')
        self.assertEqual(proof['expected_sha256'], self.proof_hash)
        self.assertEqual(proof['observed_sha256'], self.proof_hash)
        self.assertEqual(proof['usage'], 'referenced_not_proven_read')
        voice = context['eu/estilo-e-regras.md']
        self.assertEqual(voice['status'], 'provenance_unverified')
        self.assertIsNone(voice['expected_sha256'])
        self.assertIsNotNone(voice['observed_sha256'])
        self.assertTrue(any(p.get('reason') == 'context_provenance_unverified' and p['blocking'] for p in result['tabs']['before_approval']))

    def test_context_change_reports_mismatch_without_mutation(self):
        before = self.view()
        self.put('thunder/provas.md', 'Prova alterada.\n')
        result = self.view()
        proof = next(r for r in result['tabs']['context'] if r['path'] == 'thunder/provas.md')
        self.assertEqual(proof['status'], 'hash_mismatch')
        self.assertEqual(proof['expected_sha256'], self.proof_hash)
        self.assertNotEqual(proof['observed_sha256'], self.proof_hash)
        self.assertEqual(result['source'], before['source'])
        self.assertTrue(any(p.get('reason') == 'context_hash_mismatch' and p['blocking'] for p in result['tabs']['before_approval']))
        self.assertTrue(all(t['status'] == 'draft' for t in result['tabs']['cadence']))

    def test_references_traversal_absolute_hidden_and_missing(self):
        self.put(self.path, self.document + '\n## Refer\u00eancias\n../outside.md\n/absolute/private.md\neu/../private.md\neu/.private.md\neu/absent.md\n')
        result = self.view()
        context = {r['path']: r for r in result['tabs']['context']}
        for path in ('../outside.md', '/absolute/private.md', 'eu/../private.md', 'eu/.private.md'):
            self.assertEqual(context[path]['status'], 'outside_allowlist', path)
            self.assertIsNone(context[path]['observed_sha256'])
        self.assertEqual(context['eu/absent.md']['status'], 'missing_file')
        self.assertTrue(any(p['blocking'] and p.get('reason') == 'context_outside_allowlist' for p in result['tabs']['before_approval']))

    def test_symlinks_in_source_and_context_are_not_followed(self):
        outside = Path(self.scratch.name) / 'outside'
        outside.mkdir()
        (outside / 'cadencia.md').write_text(self.document)
        (self.root / 'contas/escape').symlink_to(outside, target_is_directory=True)
        (self.root / 'thunder/link.md').symlink_to(outside / 'cadencia.md')
        self.put(self.path, self.document + '\nthunder/link.md\n')
        listed = self.reader.list_accounts()
        self.assertEqual(len(listed['accounts']), 1)
        self.assertIn('source_symlink_blocked:contas/escape', listed['limitations'])
        link = next(r for r in self.view()['tabs']['context'] if r['path'] == 'thunder/link.md')
        self.assertEqual(link['status'], 'unavailable')

    def test_discovery_symlink_root_escape_and_unknown_directory(self):
        other = self.root / 'unlisted'
        other.mkdir()
        (other / 'cadencia-hidden.md').write_text(self.document)
        (self.root / 'projetos').symlink_to(other, target_is_directory=True)
        self.assertEqual(len(self.reader.list_accounts()['accounts']), 1)
        with self.assertRaises(ValueError):
            self.reader._read(self.root, '../outside.md')
        with self.assertRaises(ValueError):
            self.reader._read(self.root, '/absolute.md')

    def test_fences_never_create_fake_sections_or_references(self):
        self.put(self.path, self.document + '\n```markdown\n## D99\nfake body\n../fake.md\n```\n')
        result = self.view()
        self.assertEqual(len(result['tabs']['cadence']), 2)
        self.assertNotIn('../fake.md', [r['path'] for r in result['tabs']['context']])

    def test_duplicate_sources_fail_closed_without_selection_by_name(self):
        self.put('contas/second/cadencia.md', self.document)
        result = self.view()
        self.assertIn('account_source_ambiguous', result['limitations'])
        self.assertEqual(len(result['source_paths']), 2)
        self.assertEqual(result['tabs']['cadence'], [])

    def test_internal_snapshot_all_accounts_and_campaign_isolation(self):
        reader = AccountCadenceReadonly(self.root, {'cadences': [
            {'account_id': 'example.test', 'execution_context': {'campaign_id': 'CAMPAIGN-A'}},
            {'account_id': 'missing.test', 'execution_context': {'campaign_id': 'CAMPAIGN-A'}},
            {'account_id': 'other.test', 'execution_context': {'campaign_id': 'CAMPAIGN-B'}}]}, clock=lambda: date(2026, 10, 23))
        accounts = reader.list_accounts('CAMPAIGN-A')['accounts']
        self.assertEqual({r['id'] for r in accounts}, {'example.test', 'missing.test'})
        missing = reader.account('missing.test', 'CAMPAIGN-A')
        self.assertIsNone(missing['source']['sha256'])
        self.assertIn('account_source_missing:missing.test', missing['limitations'])

    def test_repo_never_joins_company_names_to_source_identity(self):
        self.put(self.path, self.document.replace('account_id: example.test\n', '').replace('campaign_id: CAMPAIGN-A\n', ''))
        reader = AccountCadenceReadonly(self.root, {'cadences': [
            {'account_id': 'example.test', 'company': 'Fabricante Exemplo', 'campaign_id': 'CAMPAIGN-A'}]})
        result = reader.list_accounts('CAMPAIGN-A')['accounts']
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['source_status'], 'incomplete')
        self.assertIsNone(result[0]['source']['path'])

    def test_campaign_enrichment_requires_exact_id_and_unambiguous_state(self):
        self.put(self.path, self.document.replace('campaign_id: CAMPAIGN-A\n', ''))
        snapshot = {'cadences': [{'account_id': 'example.test', 'campaign_id': 'CAMPAIGN-A'}]}
        reader = AccountCadenceReadonly(self.root, snapshot)
        self.assertEqual(len(reader.list_accounts('CAMPAIGN-A')['accounts']), 1)
        snapshot['cadences'][0]['execution_context'] = {'campaign_id': 'CAMPAIGN-B'}
        result = reader.list_accounts()
        self.assertIn('internal_campaign_conflict:example.test', result['limitations'])
        self.assertEqual(reader.list_accounts('CAMPAIGN-A')['accounts'], [])
        self.assertTrue(all(row['campaign_id'] is None for row in result['accounts']))

    def test_crlf_source_hash_is_over_bytes_and_text_is_not_normalized(self):
        content = self.document.replace('\n', '\r\n')
        self.put(self.path, content)
        result = self.view()
        self.assertEqual(result['source']['sha256'], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(result['source_text'], content)
        self.assertIn('Pessoa.\r\n\r\nTexto', result['tabs']['cadence'][0]['text'])

    def test_default_repository_is_not_opened_or_migrated(self):
        class NotReadonly:
            path = '/never/open/this.sqlite'
            def list_state(self, kind):
                raise AssertionError('Must not call WAL-setting repository')
        result = AccountCadenceReadonly(self.root, NotReadonly()).list_accounts()
        self.assertIn('internal_repo_readonly_snapshot_required', result['limitations'])

    def test_existing_sqlite_readonly_repository_uses_only_disposable_fixture(self):
        from thunder_os.db import Repository
        from thunder_os.today_readonly import ReadonlyRepository
        database = self.root / 'fixture.sqlite'
        fixture = Repository(database)
        fixture.put_state('cadence', 'synthetic-cadence', {
            'account_id': 'missing.test', 'execution_context': {'campaign_id': 'CAMPAIGN-A'}})
        before = database.read_bytes()
        reader = AccountCadenceReadonly(self.root, ReadonlyRepository(database))
        rows = reader.list_accounts('CAMPAIGN-A')['accounts']
        self.assertEqual({row['id'] for row in rows}, {'example.test', 'missing.test'})
        self.assertNotIn('internal_repo_readonly_snapshot_required', reader.list_accounts()['limitations'])
        self.assertEqual(database.read_bytes(), before)

    def test_file_entry_and_context_limits_are_visible(self):
        with patch.object(module, 'MAX_BYTES', 10):
            result = self.reader.list_accounts()
            self.assertEqual(result['accounts'], [])
            self.assertIn('source_unreadable:' + self.path, result['limitations'])
        with patch.object(module, 'MAX_FILES', 0):
            self.assertIn('source_file_limit', self.reader.list_accounts()['limitations'])
        with patch.object(module, 'MAX_ENTRIES', 0):
            self.assertIn('source_entry_limit', self.reader.list_accounts()['limitations'])
        with patch.object(module, 'MAX_TOTAL_BYTES', 1):
            self.assertIn('source_total_bytes_limit', self.reader.list_accounts()['limitations'])
        with patch.object(module, 'MAX_CONTEXT', 1):
            result = self.view()
            self.assertIn('context_reference_limit', result['limitations'])

    def test_invalid_ids_do_not_become_paths(self):
        for invalid in ('../escape', '/absolute', '<script>', '', 'x' * 161, None):
            self.assertEqual(self.reader.account(invalid)['limitations'], ['invalid_account_or_campaign_id'])
        self.assertEqual(self.reader.list_accounts('../escape')['limitations'], ['invalid_campaign_id'])

    def test_legacy_campaign_notes_accessible_but_do_not_invent_copy(self):
        self.put('contas/registro/campanha.md', '# Campanha | Empresa Registro\n\n- D0: rascunho em cart\u00e3o, texto ausente.\n')
        row = next(r for r in self.reader.list_accounts()['accounts'] if r['company'] == 'Empresa Registro')
        result = self.reader.account(row['id'])
        self.assertEqual(result['tabs']['cadence'], [])
        self.assertIn('cadence_text_missing', result['limitations'])
        self.assertIn('campaign_unassigned:contas/registro/campanha.md', result['limitations'])

    def test_legacy_title_dialects_and_no_title_still_expose_source(self):
        for slug, title in (('colon', '# Campanha: Empresa Colon'), ('plain', '# Campanha Empresa Livre'),
                            ('untitled', '## Status operacional')):
            self.put('contas/' + slug + '/campanha.md', title + '\n\nRegistro sem copy.\n')
        accounts = self.reader.list_accounts()['accounts']
        self.assertEqual(len(accounts), 4)
        self.assertEqual(next(r for r in accounts if r['brain_account_slug'] == 'colon')['company'], 'Empresa Colon')
        self.assertTrue(any(r['brain_account_slug'] == 'untitled' for r in accounts))

    def test_model_directory_is_not_an_account_and_no_numbered_section_escape(self):
        self.put('contas/_modelo/campanha.md', '# Campanha: Empresa modelo\n')
        self.assertEqual(len(self.reader.list_accounts()['accounts']), 1)
        self.put(self.path, self.document + '\n## Textos exatos\n### 1. E-mail\nCorpo.\n## Contagens\n### 9. N\u00e3o \u00e9 toque\nContagem.\n')
        self.assertEqual(len(self.view()['tabs']['cadence']), 3)

    def test_reference_hash_does_not_attach_to_another_reference(self):
        self.put(self.path, self.document + '\n## Fontes\neu/estilo-e-regras.md thunder/provas.md SHA256 ' + self.proof_hash + '\n')
        result = self.view()
        voice = [r for r in result['tabs']['context'] if r['path'] == 'eu/estilo-e-regras.md']
        self.assertEqual(len(voice), 1)
        self.assertIsNone(voice[0]['expected_sha256'])

    def test_external_commercial_directory_is_explicit_not_generic_escape(self):
        commercial = Path(self.scratch.name).resolve() / 'commercial'
        commercial.mkdir()
        (commercial / 'Cadencia-Exemplo.md').write_text('# Cad\u00eancia M\u00e9xico: Pessoa / Companhia\n')
        with patch.object(module, 'MAC_BRAIN', self.root), patch.object(module, 'MAC_COMMERCIAL', commercial):
            accounts = self.reader.list_accounts()['accounts']
            row = next(r for r in accounts if r['company'] == 'Companhia')
            self.assertEqual(row['source']['path'], str(commercial / 'Cadencia-Exemplo.md'))
        self.assertEqual(len(self.reader.list_accounts()['accounts']), 1)

    def test_numbered_sections_and_list_dossier_preserve_legacy_text(self):
        text = '# Cad\u00eancia M\u00e9xico: Pessoa / Companhia\n\n## Dossi\u00ea M1-M7\n'
        text += '- M1: Contexto sem data: https://example.test/page\n- M2: Pergunta sem classifica\u00e7\u00e3o.\n'
        text += '\n## Textos exatos\n### 1. E-mail\nAssunto: Literal\n\nHola.\n\n### 2 \u2014 Convite\nConectar?\n'
        self.put(self.path, text)
        row = self.reader.list_accounts()['accounts'][0]
        result = self.reader.account(row['id'])
        self.assertEqual([t['text'] for t in result['tabs']['cadence']], ['Hola.', 'Conectar?'])
        self.assertTrue(all(t['day'] is None for t in result['tabs']['cadence']))
        self.assertEqual(result['tabs']['dossier'][1]['classification'], 'nao_classificado')
        self.assertEqual(result['tabs']['research'][0]['label'], 'data desconhecida')

    def test_pending_notes_do_not_create_approval_authority(self):
        result = self.view()
        rows = result['tabs']['before_approval']
        self.assertTrue(rows[0]['blocking'])
        self.assertFalse(rows[1]['blocking'])
        self.assertIn('draft_is_not_approval_or_delivery', result['limitations'])
        self.assertNotIn('approval', result)

    def test_no_writes_on_repeat_reads(self):
        def inventory():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob('*') if p.is_file()}
        before = inventory()
        self.reader.list_accounts()
        self.view()
        self.view()
        self.assertEqual(before, inventory())


class VisitorIndexTests(unittest.TestCase):
    """Ten synthetic indexed companies: six reviewable, four status-only."""
    SLUGS = ('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta', 'theta', 'iota', 'kappa')
    READINESS = ('pronta para revis\u00e3o',) * 5 + ('pronta para revis\u00e3o (reabertura)',
        'morna: relacionamento existente', 'morna: hist\u00f3rico anterior',
        'bloqueada: j\u00e1 est\u00e1 em MX27-EXAMPLE (M\u00e9xico)', 'descartada: n\u00e3o prospect\u00e1vel')

    def setUp(self):
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            port = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(port.assert_not_called)
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve() / 'brain'
        self.root.mkdir()
        self.put('thunder/provas.md', 'Prova sint\u00e9tica.\n')
        self.put('eu/estilo-e-regras.md', 'Voz sint\u00e9tica.\n')
        self.proof_hash = hashlib.sha256('Prova sint\u00e9tica.\n'.encode()).hexdigest()
        self.brief = '# Brief original\n\n## Leia antes\nBRAIN/eu/estilo-e-regras.md\nBRAIN/thunder/provas.md SHA256 ' + self.proof_hash
        self.brief += '\n\n## Calend\u00e1rio (dias \u00fateis no Brasil)\nD0 seg 26/10/2026 \u00b7 D3 qui 29/10 \u00b7 D7 qui 05/11 \u00b7 D12 qui 12/11 \u00b7 D15 ter 17/11.\n'
        self.put(module.VISITOR_BRIEF, self.brief)
        self.index = '# Cad\u00eancias dos visitantes da p\u00e1gina (Brasil)\n\n| Pasta | Conta e pessoa | M\u00e9todo | Status |\n|---|---|---|---|\n'
        self.documents = {}
        for number, slug in enumerate(self.SLUGS):
            self.index += '| ' + slug + ' | Empresa Sint\u00e9tica ' + str(number) + ' \u00b7 Pessoa Fict\u00edcia | BAY | ' + self.READINESS[number] + ' |\n'
            document = self.document(number)
            self.documents[slug] = document
            self.put(module.VISITOR_DIRECTORY + '/' + slug + '/cadencia.md', document)
        self.put(module.VISITOR_INDEX, self.index)
        self.reader = AccountCadenceReadonly(self.root, clock=lambda: date(2026, 10, 23))

    def put(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='')
        return path

    @staticmethod
    def footer(number):
        return ('*(30 palavras \u00b7 parar se responder)*',
                '*(30 palavras. Parada: qualquer resposta.)*',
                '*Contagem: 30 palavras. Parada: qualquer resposta.*',
                '- Contagem: 30 palavras.\n- CTA: pergunta.\n- Parada: qualquer resposta.',
                '*Contagem: 30 palavras.* Parada: qualquer resposta.',
                'Contagem: 30 palavras. CTA: pergunta. Parada: qualquer resposta.')[number % 6]

    def document(self, number):
        text = '# Empresa Sint\u00e9tica ' + str(number) + ' | Pessoa Fict\u00edcia, Cargo | M\u00e9todo BAY\n\n'
        text += '## Pessoa e empresa (com fontes)\n- Fato literal em 20/10/2026: https://example.test/recent\n- Contexto de 20/09/2025: https://example.test/context\n'
        text += '\n## Dossi\u00ea de muni\u00e7\u00e3o\n'
        if number % 6 in {3, 5}:
            text += '| C\u00f3digo | Conte\u00fado | Fonte e data |\n|---|---|---|\n'
            for key in range(1, 8):
                text += '| M' + str(key) + (' Tese (hip\u00f3tese)' if key == 2 else ' Item') + ' | Texto literal M' + str(key) + '. | https://example.test/source, 20/10/2026 |\n'
        else:
            for key in range(1, 8):
                marker = ' Tens\u00e3o (hip\u00f3tese de setor)' if key == 2 else ' Reserva (reserva)' if key == 5 else ' Item'
                text += '- **M' + str(key) + marker + ':** Texto literal M' + str(key) + '. https://example.test/source\n'
        text += '\n## Tese (1 linha)\nTese literal sem reformula\u00e7\u00e3o.\n\n## Mapa dos toques\n'
        text += '| Toque | Data | Canal | Trabalho | Muni\u00e7\u00e3o |\n|---|---|---|---|---|\n'
        dates = ('26/10/2026', '29/10/2026', '05/11/2026', '12/11/2026', '17/11/2026')
        for day, calendar, ammo in zip(('D0', 'D3', 'D7', 'D12', 'D15'), dates, ('M1, M2', 'M3', 'M4', 'M5', 'M7')):
            text += '| ' + day + ' | ' + calendar + ' | E-mail + LinkedIn | Trabalho literal | ' + ammo + ' |\n'
        text += '\n## Cad\u00eancia\n'
        for day, calendar in zip(('D0', 'D3', 'D7', 'D12', 'D15'), dates):
            channel = 'LinkedIn' if day == 'D7' else 'E-mail'
            if number % 6 == 3:
                heading = day + ' | ' + channel + ' | ' + calendar
            elif number % 6 == 5:
                heading = day + ' \u00b7 ' + calendar + ' \u00b7 ' + channel
            else:
                heading = day + ' \u00b7 ' + channel + ' \u00b7 ' + calendar
            if number % 6 == 1 and day == 'D0':
                heading += ' \u00b7 assunto: Assunto literal'
            text += '\n### ' + heading + '\n'
            if day == 'D0' and number % 6 != 1:
                text += '**Assunto:** Assunto literal\n\n'
            if day == 'D3' and number % 6 == 2:
                text += '**E-mail**\n\n'
            text += self.body(day) + '\n\n' + self.footer(number) + '\n'
            if day == 'D3':
                if number % 6 in {3, 5}:
                    text += '\n### D3' + (' | Convite LinkedIn com nota | ' + calendar if number % 6 == 3 else ' \u00b7 ' + calendar + ' \u00b7 Convite LinkedIn (nota)') + '\n'
                else:
                    text += '\n' + ('**Convite LinkedIn: n\u00e3o enviar**. Nota de reserva:' if number % 6 == 1 else '**Nota do convite LinkedIn** (180 caracteres)' if number % 6 == 2 else '**Convite LinkedIn (nota, 180 caracteres):**') + '\n'
                text += self.note() + '\n\n' + ('- Contagem: 180 caracteres.\n- Aceite n\u00e3o \u00e9 resposta.' if number % 6 == 3 else 'Contagem: 180 caracteres. Aceite n\u00e3o \u00e9 resposta.' if number % 6 == 5 else '*Contagem: 180 caracteres.*') + '\n'
        text += '\n## Autocr\u00edtica\n- Toque mais fraco: D3. Texto cr\u00edtico literal.\n- **O que precisa confirmar antes de aprovar:**\n  1. Canal confirmado.\n  2. Hist\u00f3rico lido.\n'
        return text

    @staticmethod
    def body(day):
        return 'Pessoa fict\u00edcia, texto ' + day + ' com  espa\u00e7os.\n\n1. Item literal.\n2. Outro item.\n\nAutor'

    @staticmethod
    def note():
        return 'Pessoa fict\u00edcia, nota exclusiva com  dois espa\u00e7os. Aceita conectar?'

    def account(self, slug):
        row = next(row for row in self.reader.list_accounts()['accounts'] if row.get('source_slug') == slug)
        return self.reader.account(row['id'])

    def assert_ready(self, slug):
        view = self.account(slug)
        self.assertEqual(view['source_group'], module.VISITOR_GROUP)
        self.assertEqual(view['country'], 'BR')
        self.assertTrue(view['reviewable'])
        self.assertIsNone(view['campaign_id'])
        self.assertEqual(view['readiness'], self.READINESS[self.SLUGS.index(slug)])
        self.assertEqual(view['thesis'], 'Tese literal sem reformula\u00e7\u00e3o.')
        self.assertTrue(all(view['tabs'][name] for name in ('research', 'dossier', 'context', 'cadence', 'before_approval')))
        self.assertEqual(view['source_text'], self.documents[slug])
        touches = view['tabs']['cadence']
        self.assertEqual([t['id'] for t in touches], ['D0:1', 'D3:1', 'D3:2', 'D7:1', 'D12:1', 'D15:1'])
        self.assertEqual(touches[0]['subject'], 'Assunto literal')
        self.assertEqual(touches[0]['text'], self.body('D0'))
        self.assertEqual(touches[1]['text'], self.body('D3'))
        self.assertEqual(touches[2]['text'], self.note())
        for touch in touches:
            self.assertNotIn('Contagem:', touch['text'])
            self.assertNotIn('Parada:', touch['text'])
            self.assertNotIn('Convite LinkedIn', touch['text'])
            self.assertIsNotNone(touch['date'])
            self.assertEqual(touch['source_hash'], view['source']['sha256'])
        self.assertEqual(touches[1]['ammunition'], 'M3')
        self.assertEqual(touches[2]['ammunition'], 'M3')
        self.assertIsNotNone(touches[1]['stop_condition'])
        self.assertEqual(len([item for item in view['tabs']['dossier'] if item['text'] is not None]), 7)
        self.assertEqual(view['tabs']['dossier'][1]['classification'], 'hipotese')
        self.assertEqual(view['tabs']['research'][1]['label'], 'contexto')
        return view

    def assert_hold(self, slug, country='BR'):
        view = self.account(slug)
        self.assertFalse(view['reviewable'])
        self.assertEqual(view['country'], country)
        self.assertIsNone(view['campaign_id'])
        self.assertEqual(view['readiness'], self.READINESS[self.SLUGS.index(slug)])
        self.assertEqual(view['status_reason'], view['readiness'])
        self.assertEqual(view['tabs']['cadence'], [])
        self.assertEqual(view['tabs']['research'], [])
        self.assertEqual(view['tabs']['dossier'], [])
        self.assertTrue(view['source_text_readonly'])
        self.assertEqual(view['source_text'], self.documents[slug])
        self.assertTrue(view['tabs']['before_approval'][0]['blocking'])
        return view

    def test_reviewable_alpha_inline_invite_and_parenthetical_footer(self):
        self.assert_ready('alpha')

    def test_reviewable_beta_reserved_invite_and_inline_subject(self):
        view = self.assert_ready('beta')
        self.assertEqual(view['tabs']['cadence'][2]['status'], 'reserved')
        self.assertFalse(view['tabs']['cadence'][2]['reviewable'])

    def test_reviewable_gamma_email_label_and_note_label(self):
        self.assert_ready('gamma')

    def test_reviewable_delta_vertical_headings_and_bullet_footer(self):
        self.assert_ready('delta')

    def test_reviewable_epsilon_italic_count_and_unitalic_condition(self):
        self.assert_ready('epsilon')

    def test_reviewable_zeta_date_first_and_plaintext_footer(self):
        self.assert_ready('zeta')

    def test_hold_eta_existing_relationship(self):
        self.assert_hold('eta')

    def test_hold_theta_previous_history(self):
        self.assert_hold('theta')

    def test_hold_iota_mexico_only_referenced_campaign(self):
        view = self.assert_hold('iota', 'MX')
        self.assertEqual(view['referenced_campaign_id'], 'MX27-EXAMPLE')

    def test_hold_kappa_discarded_nonprospectable(self):
        self.assert_hold('kappa')

    def test_index_group_is_not_a_registry_campaign_or_name_join(self):
        self.reader.repo = {'cadences': [{'account_id': 'example.test', 'company': 'Empresa Sint\u00e9tica 0', 'campaign_id': 'REGISTRY-A'}]}
        views = [r for r in self.reader.list_accounts()['accounts'] if r.get('source_group') == module.VISITOR_GROUP]
        self.assertEqual(len(views), 10)
        self.assertEqual(sum(r['reviewable'] for r in views), 6)
        self.assertTrue(all(r['campaign_id'] is None for r in views))
        self.assertEqual(self.reader.list_accounts(module.VISITOR_GROUP)['accounts'], [])

    def test_unindexed_h1_company_dialect_is_not_generically_accepted(self):
        self.put('_entrada/claude/outsider/cadencia.md', self.documents['alpha'])
        result = self.reader.list_accounts()
        self.assertEqual(len(result['accounts']), 10)
        self.assertIn('source_format_unsupported:_entrada/claude/outsider/cadencia.md', result['limitations'])

    def test_index_traversal_and_duplicate_paths_fail_closed(self):
        self.put(module.VISITOR_INDEX, self.index.replace('| alpha |', '| ../escape |'))
        self.assertEqual(self.reader.list_accounts()['accounts'], [])
        self.assertIn('visitor_index_invalid:' + module.VISITOR_INDEX, self.reader.list_accounts()['limitations'])
        self.put(module.VISITOR_INDEX, self.index.replace('| beta |', '| alpha |'))
        self.assertEqual(self.reader.list_accounts()['accounts'], [])

    def test_missing_indexed_source_preserves_status_but_blocks_review(self):
        (self.root / module.VISITOR_DIRECTORY / 'alpha/cadencia.md').unlink()
        view = self.account('alpha')
        self.assertFalse(view['reviewable'])
        self.assertEqual(view['source_status'], 'incomplete')
        self.assertEqual(view['readiness'], self.READINESS[0])
        self.assertEqual(view['tabs']['cadence'], [])
        self.assertIsNone(view.get('date'))
        self.assertIsNone(view.get('date_basis'))
        self.assertIsNone(view.get('date_sourcepath'))

    def test_indexed_source_date_uses_its_file_not_index_brief_or_group_name(self):
        path = module.VISITOR_DIRECTORY + '/alpha/cadencia.md'
        timestamp = datetime(2024, 3, 4, 5, 6, 7, tzinfo=timezone.utc).timestamp()
        os.utime(self.root / path, (timestamp, timestamp))
        view = self.account('alpha')
        self.assertEqual(view['date'], '2024-03-04T05:06:07+00:00')
        self.assertEqual(view['date'], view['source']['updated_at'])
        self.assertEqual(view['date_basis'], 'source_file')
        self.assertEqual(view['date_sourcepath'], path)

    def test_original_brief_alias_provenance_hash_change_and_no_author_claim(self):
        view = self.account('alpha')
        context = view['tabs']['context']
        brief = next(row for row in context if row['path'] == module.VISITOR_BRIEF)
        self.assertEqual(brief['usage'], 'read_original_brief')
        voice = next(row for row in context if row['path'] == 'eu/estilo-e-regras.md')
        self.assertEqual(voice['reference_text'], 'BRAIN/eu/estilo-e-regras.md')
        self.assertEqual(voice['status'], 'provenance_unverified')
        self.assertEqual(voice['usage'], 'referenced_not_proven_read')
        self.assertTrue(any(p.get('reason') == 'context_provenance_unverified' and p['blocking'] for p in view['tabs']['before_approval']))
        self.put('thunder/provas.md', 'Prova alterada.\n')
        changed = self.account('alpha')
        proof = next(row for row in changed['tabs']['context'] if row['path'] == 'thunder/provas.md')
        self.assertEqual(proof['status'], 'hash_mismatch')
        self.assertEqual(proof['expected_sha256'], self.proof_hash)

    def test_brief_alias_cannot_escape_or_follow_symlink(self):
        outside = Path(self.scratch.name) / 'outside.md'
        outside.write_text('Forbidden fixture.\n')
        (self.root / 'eu/link.md').symlink_to(outside)
        self.put(module.VISITOR_BRIEF, self.brief + '\nBRAIN/../outside.md\nBRAIN/eu/link.md\n')
        context = self.account('alpha')['tabs']['context']
        self.assertEqual(next(r for r in context if r['path'] == '../outside.md')['status'], 'outside_allowlist')
        self.assertEqual(next(r for r in context if r['path'] == 'eu/link.md')['status'], 'unavailable')

    def test_short_dates_bind_only_the_explicit_original_brief_calendar(self):
        document = self.documents['alpha'].replace('/2026', '')
        self.put(module.VISITOR_DIRECTORY + '/alpha/cadencia.md', document)
        view = self.account('alpha')
        for touch in view['tabs']['cadence']:
            self.assertIsNotNone(touch['date'])
            self.assertEqual(touch['date_provenance'], 'original_brief_calendar')
            self.assertEqual(touch['date_source'], module.VISITOR_BRIEF)
        self.put(module.VISITOR_BRIEF, '# Brief sem calend\u00e1rio\n')
        self.assertTrue(all(touch['date'] is None for touch in self.account('alpha')['tabs']['cadence']))


if __name__ == '__main__':
    unittest.main()
