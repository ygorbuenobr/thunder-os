"""S27C8 Journey integration: real local projections, synthetic ports, zero network.

Proofs: alerts/original conservation; campaign-bound canonical inputs; missing
body/proofs stay unverified; receipt identity; unchanged approvals/history and DB.
The main agent owns independent verification of the complete S27 change.
"""
from contextlib import closing, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from thunder_os.campaign_registry import CampaignRegistry
from thunder_os.db import Repository
from thunder_os.external_email_import import APOLLO_TEAM, APOLLO_SEQUENCE, plan_email
from thunder_os.journey_readonly import JourneyReadonly, MISSING, _restriction_phrases
from thunder_os.knowledge import KnowledgeStore
from thunder_os.seqd_client import MAILBOX
from thunder_os.service import ThunderService
from thunder_os.today_readonly import TodayError, TodayReadonly


BR = 'BR-APOLLO-01'
OTHER = 'BR-OTHER-01'
NOW = datetime(2026, 10, 24, 15, tzinfo=timezone.utc)
DATE = '2026-10-24T12:34:56Z'
# Literal canonical lines, copied as fixture data; never read the global Brain in tests.
CANONICAL_RESTRICTIONS = (
    '- Nunca transformar observação de setor em deficiência presumida da conta. '
    'São proibidos diagnósticos sem prova como “vocês perdem oportunidades”, '
    '“o marketing atira para todo lado” e “dependem de indicação/feira”, além de perguntas '
    'binárias que obriguem o lead a admitir problema. Com pesquisa, usar fato verificável '
    'e pergunta aberta sobre a visão da pessoa.\n'
    '- Indicação deve soar como aproximação humana, não encaminhamento corporativo: '
    'saudação natural, contar quem indicou e como a conversa levou até a pessoa, reconhecer '
    'a agenda e pedir primeiro a visão dela. Nunca abrir com “você foi indicada”, despejar '
    'capacidades, falar em “trabalhar juntos” ou pedir reunião para “avaliar se faz sentido”. '
    'A proposta vem depois da escuta.\n'
    '- Copy enterprise de retomada não pede licença para existir. Mostra domínio do negócio, '
    'transforma a dor em impacto comercial, tensiona o custo de continuar igual, apresenta '
    'uma tese específica e conduz um próximo passo. Evitar “isso está na pauta?”, '
    '“vale uma troca?” e mensagens que apenas confirmam interesse.'
)


class JourneySentAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.brain = self.root / 'brain'
        self.brain.mkdir()
        self.repo = Repository(self.root / 'fixture.db')
        with closing(sqlite3.connect(self.repo.path)) as con, con:
            con.execute('CREATE TABLE itens(id TEXT PRIMARY KEY,dados JSON,atualizado TEXT)')
        self.service = ThunderService(self.repo, knowledge_store=KnowledgeStore(self.brain))
        self.register(BR)
        self.register(OTHER, proofs='40% e 90 empresas.')
        self.runner = Mock(side_effect=AssertionError('model forbidden'))
        self.today = TodayReadonly(self.service, runner=self.runner, clock=lambda: NOW.timestamp(),
                                   monotonic=lambda: 0)
        self.rows = []
        self.today._gmail = self.mail_threads
        for method in ('_tasks', '_legacy', '_emails', '_instantly', '_linkedin', '_seqd', '_deals', '_approvals'):
            setattr(self.today, method, Mock(return_value={'status': 'ok', 'respond': [], 'do': []}))
        self.cadence = Mock()
        self.cadence.snapshot.return_value = {'coverage': {'complete': False}, 'campaigns': []}
        self.cadence.timeline.return_value = {'events': [], 'coverage': {'complete': False}}
        self.adapter = JourneyReadonly(self.today, cadence_reader=self.cadence)
        for target in ('socket.socket', 'socket.create_connection', 'socket.getaddrinfo',
                       'urllib.request.urlopen', 'http.client.HTTPConnection.request'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            port = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(port.assert_not_called)

    def register(self, campaign, *, proofs='25 empresas.', restrictions='Nao inventar provas.'):
        fields = {}
        for key, content in {'country': 'BR', 'proofs': proofs, 'restrictions': restrictions}.items():
            path = self.brain / (campaign + '-' + key + '.md')
            raw = ('## ' + key + '\n' + content + '\n').encode()
            path.write_bytes(raw)
            fields[key] = {'path': path.name, 'sha256': hashlib.sha256(raw).hexdigest(), 'section': key}
        CampaignRegistry(self.repo).register({'campaign_id': campaign, 'campaign_version': '1',
            'rules_version': '1', 'run_id': 'fixture', 'platform': 'thunder_os',
            'providers': {'email': 'fake-email'}, 'rules': 'Synthetic fixture only.',
            'configuration': {'flow_version': '1', 'provider_accounts': {'email': 'fixture'}},
            'briefing': {'version': '1', 'fields': fields}}, 'ygor',
            (self.repo.get_state('campaign_config', campaign) or {}).get('version', 0))

    def add_thread(self, text='Resultado garantido — 40% e 25 empresas.', *,
                   campaign=BR, ident='sent-1', source='gmail', **extra):
        msg = {'id': ident, 'canonical_id': source + ':' + ident, 'date': DATE,
            'text': text, 'outgoing': True, 'sender': MAILBOX, 'subject': 'Original subject',
            'body_complete': True, 'receipt': {'provider': 'apollo', 'message_id': ident,
                'verification': 'provider_read'}, **extra}
        self.rows.append({'source': source, 'ident': 'thread-' + ident, 'title': 'Synthetic buyer',
            'campaign': campaign, 'contact_id': '700', 'person_keys': ['buyer@example.invalid'],
            'messages': [msg], 'complete': True, 'action': {}})
        return msg

    def mail_threads(self):
        value = {'status': 'ok', 'respond': [], 'do': []}
        for row in self.rows:
            result = self.today._thread(**row)
            for key in ('respond', 'do'):
                value[key].extend(result.get(key, []))
        return value

    def database(self):
        with closing(sqlite3.connect(self.repo.path)) as con:
            return tuple(con.iterdump())

    def detail(self, snapshot=None):
        snap = snapshot or self.adapter.snapshot(campaign=BR)
        return self.adapter.detail(snap['timeline']['events'][0]['detail_id'], campaign=BR)

    def test_sent_email_audit_flags_prohibited_content_and_preserves_original_everywhere(self):
        body = '  Resultado GARANTIDO — 40% e 25 empresas.\n\n<literal> & texto\n' + 'Original\n' * 2600
        msg = self.add_thread(body)
        original = deepcopy(self.rows)
        self.repo.put_state('suppression', 'buyer@example.invalid', {'reason': 'fixture optout'})
        self.repo.put_state('reply', 'historical', {'content': 'Original reply', 'state': 'classified'})
        self.repo.put_state('approval_pack', 'historical', {'status': 'approved', 'content': 'Exact copy'})
        before = self.database()
        with ExitStack() as guards:
            writers = [guards.enter_context(patch.object(Repository, name,
                side_effect=AssertionError('mutation forbidden'))) for name in (
                    'put_state', 'put_states_atomic', 'append_state', 'enqueue_effect', 'save_with_event')]
            snapshot = self.adapter.snapshot(campaign=BR)
            # Thread, first observed touch and timeline are independent detail doors.
            ids = {row['id'] for stage in snapshot['stages'] for row in stage['items']}
            ids.update(row['detail_id'] for row in snapshot['timeline']['events'])
            self.assertEqual(len(ids), 3)
            for ident in ids:
                detail = self.adapter.detail(ident, campaign=BR)
                audit, = detail['sent_email_audits']
                self.assertEqual(audit['status'], 'flagged')
                self.assertEqual({a['code'] for a in audit['alerts']},
                                 {'em_dash', 'forbidden_phrase', 'number_not_in_proofs'})
                self.assertEqual([a['match'] for a in audit['alerts']
                                  if a['code'] == 'number_not_in_proofs'], ['40%'])
                self.assertEqual((audit['original_id'], audit['source_id'], audit['date']),
                                 ('sent-1', 'gmail:sent-1', DATE))
                self.assertEqual(audit['provider'], 'apollo')
                self.assertTrue(audit['advisory'])
                self.assertEqual(audit['body_hash'], hashlib.sha256(body.encode()).hexdigest())
                self.assertIn(body, [b['text'] for b in detail['blocks']])
                self.assertFalse(detail['approval_available'])
                self.assertFalse(detail['editable'])
            for row in [r for s in snapshot['stages'] for r in s['items']] + snapshot['timeline']['events']:
                self.assertIn('3 alerta', row['summary'])
            for writer in writers:
                writer.assert_not_called()
        self.assertEqual(self.database(), before)
        self.assertEqual(self.rows, original)
        self.assertEqual(msg['text'], body)
        self.runner.assert_not_called()

    def test_missing_or_partial_body_and_date_stay_unverified_without_safe_claim(self):
        for text, complete, date in ((None, True, DATE), ('', True, DATE),
                                     ('Snippet — 40%', False, DATE), ('25 empresas.', True, None)):
            with self.subTest(text=text, complete=complete, date=date):
                self.rows.clear()
                self.today._cache = None
                self.add_thread(text, body_complete=complete, date=date)
                detail = self.detail()
                audit, = detail['sent_email_audits']
                self.assertEqual(audit['status'], 'unverified')
                self.assertEqual(audit['date'], date)
                if not text:
                    self.assertEqual(detail['blocks'][0]['text'], MISSING)
                    self.assertFalse(audit['body_complete'])
                if text and not complete:
                    self.assertTrue(audit['alerts'])
                    self.assertEqual(detail['blocks'][0]['text'], text)

    def test_multiple_sent_messages_keep_per_event_identity_and_advisory_only_projection(self):
        first = self.add_thread('Resultado garantido — 40%')
        self.rows[0]['messages'].extend([
            {**first, 'id': 'inbound', 'canonical_id': 'gmail:inbound', 'date': '2026-10-24T13:00:00Z',
             'text': 'Original incoming', 'outgoing': False, 'receipt': None},
            {**first, 'id': 'later', 'canonical_id': 'gmail:later', 'date': '2026-10-24T14:00:00Z',
             'text': '25 empresas.', 'receipt': {**first['receipt'], 'message_id': 'later'}},
        ])
        original = deepcopy(self.rows)
        snapshot = self.adapter.snapshot(campaign=BR)
        thread = next(row for stage in snapshot['stages'] if stage['id'] == 'followup'
                      for row in stage['items'])
        detail = self.adapter.detail(thread['id'], campaign=BR)
        self.assertEqual([(a['original_id'], a['date'], a['status']) for a in detail['sent_email_audits']],
                         [('sent-1', DATE, 'flagged'), ('later', '2026-10-24T14:00:00Z', 'no_alerts')])
        self.assertEqual([b['text'] for b in detail['blocks'][:3]],
                         [m['text'] for m in self.rows[0]['messages']])
        # Disabling only the sidecar produces the identical operational projection.
        with patch('thunder_os.journey_readonly._sent_email_audit', return_value=None):
            baseline = self.adapter.snapshot(campaign=BR)
            baseline_detail = self.adapter.detail(thread['id'], campaign=BR)
        for stage in snapshot['stages']:
            for row in stage['items']:
                row['summary'] = row['summary'].split(' Auditoria consultiva Apollo:')[0]
        for row in snapshot['timeline']['events']:
            row['summary'] = row['summary'].split(' Auditoria consultiva Apollo:')[0]
        detail.pop('sent_email_audits')
        self.assertEqual(snapshot, baseline)
        self.assertEqual(detail, baseline_detail)
        self.assertEqual(self.rows, original)

    def test_campaign_bound_proofs_and_only_ok_canonical_structured_restrictions(self):
        self.register(BR, restrictions=json.dumps({'forbidden_phrases': ['Texto proibido']}))
        self.add_thread('Texto proibido, resultado garantido; 40% e 25 empresas.')
        self.add_thread('40% e 90 empresas.', campaign=OTHER, ident='other')
        snap = self.adapter.snapshot()
        audits = [self.adapter.detail(row['detail_id'])['sent_email_audits'][0]
                  for row in snap['timeline']['events']]
        first = next(a for a in audits if a['original_id'] == 'sent-1')
        second = next(a for a in audits if a['original_id'] == 'other')
        self.assertEqual([(a['code'], a['match']) for a in first['alerts']],
                         [('forbidden_phrase', 'Texto proibido'), ('number_not_in_proofs', '40%')])
        self.assertEqual(second['status'], 'no_alerts')
        self.assertEqual(second['alerts'], [])
        # A changed hash removes authority even when the on-disk phrase is explicit.
        path = self.brain / (BR + '-restrictions.md')
        path.write_text('## restrictions\n' + json.dumps({'forbidden_phrases': ['Texto proibido']}) + '\nchanged')
        audit, = self.detail()['sent_email_audits']
        self.assertEqual([a['match'] for a in audit['alerts'] if a['code'] == 'forbidden_phrase'],
                         ['resultado garantido'])

    def test_prose_and_malformed_restrictions_keep_defaults_without_invented_phrases(self):
        for content in ('Proibido: Texto proibido', 'Texto proibido', '{invalid',
                        'Nunca usar “Texto proibido', 'Nunca usar "Texto proibido”',
                        'Evitar “Texto proibido"', 'Nunca usar “”',
                        'Nunca usar “' + 'x' * 501 + '”',
                        '{"note": "Nunca usar “Texto proibido”"',
                        json.dumps({'forbidden_phrases': 'Texto proibido'}),
                        json.dumps({'forbidden_phrases': 'Texto proibido',
                                    'note': 'Nunca usar “Texto proibido”'}),
                        json.dumps({'forbidden_phrases': [None]}),
                        json.dumps({'forbidden_phrases': ['x'] * 101})):
            with self.subTest(content=content):
                self.register(BR, restrictions=content)
                self.rows.clear()
                self.today._cache = None
                self.add_thread('Texto proibido; resultado garantido.')
                audit, = self.detail()['sent_email_audits']
                self.assertEqual([a['match'] for a in audit['alerts']], ['resultado garantido'])

    def test_canonical_prose_restrictions_flag_literal_prohibitions(self):
        phrases = ['vocês perdem oportunidades', 'o marketing atira para todo lado',
                   'dependem de indicação/feira', 'você foi indicada', 'trabalhar juntos',
                   'avaliar se faz sentido', 'isso está na pauta?', 'vale uma troca?']
        self.assertEqual(_restriction_phrases(CANONICAL_RESTRICTIONS),
                         ['resultado garantido', 'resultados garantidos', *phrases])
        self.register(BR, restrictions=CANONICAL_RESTRICTIONS)
        body = '; '.join([phrases[0].upper(), *phrases[1:]])
        self.add_thread(body)
        audit, = self.detail()['sent_email_audits']
        self.assertEqual(audit['status'], 'flagged')
        self.assertEqual([(a['code'], a['match']) for a in audit['alerts']],
                         [('forbidden_phrase', p) for p in [phrases[0].upper(), *phrases[1:]]])
        self.assertEqual(audit['body_hash'], hashlib.sha256(body.encode()).hexdigest())

    def test_ordinary_quoted_examples_do_not_become_prohibitions(self):
        restrictions = (
            '- Exemplo de pergunta: “Como você vê a expansão?”\n'
            '- Nunca usar “Texto vedado”. Exemplo permitido: “Texto comum”.\n'
            '- Evitar “Isso está na pauta?”, “vale uma troca?”. Sugestão: "Vamos conversar?"\n'
            '- Exemplo de citação: "Nunca usar palavras inventadas".\n'
            '- A orientação não diz para evitar “Outra frase”.'
        )
        self.assertEqual(_restriction_phrases(restrictions),
                         ['resultado garantido', 'resultados garantidos', 'Texto vedado',
                          'Isso está na pauta?', 'vale uma troca?'])
        self.register(BR, restrictions=restrictions)
        self.add_thread('Como você vê a expansão? Texto comum. Vamos conversar? '
                        'Nunca usar palavras inventadas. Outra frase.')
        audit, = self.detail()['sent_email_audits']
        self.assertEqual(audit['status'], 'no_alerts')
        self.assertEqual(audit['alerts'], [])

    def test_missing_or_non_ok_proofs_never_borrow_other_campaign_proofs(self):
        self.add_thread('40%')
        (self.brain / (BR + '-proofs.md')).write_text('## proofs\n40%')
        audit, = self.detail()['sent_email_audits']
        self.assertEqual(audit['status'], 'unverified')
        self.assertEqual(audit['reason'], 'proofs_unavailable')
        self.assertEqual(audit['alerts'], [])

    def test_provider_identity_requires_real_receipt_never_campaign_or_crm_sent(self):
        cases = [None, {'provider': 'gmail', 'message_id': 'x', 'verification': 'provider_read'},
                 {'provider': 'apollo', 'message_id': 'x', 'verification': 'crm_recorded'},
                 {'provider': 'apollo', 'message_id': '', 'verification': 'provider_read'}]
        for rec in cases:
            with self.subTest(receipt=rec):
                self.rows.clear()
                self.today._cache = None
                self.add_thread(receipt=rec, source='apollo', crm_recorded_sent=True)
                snap = self.adapter.snapshot(campaign=BR)
                self.assertNotIn('sent_email_audits', self.detail(snap))
                self.assertNotIn('Auditoria', snap['timeline']['events'][0]['summary'])
        for flags in ({'outgoing': False}, {'unsent': True}):
            self.rows.clear()
            self.today._cache = None
            self.add_thread(**flags)
            self.assertNotIn('sent_email_audits', self.detail())
        # A different campaign name with an Apollo receipt is still Apollo.
        self.rows.clear()
        self.today._cache = None
        self.add_thread(campaign=OTHER)
        snap = self.adapter.snapshot(campaign=OTHER)
        detail = self.adapter.detail(snap['timeline']['events'][0]['detail_id'], campaign=OTHER)
        self.assertEqual(detail['sent_email_audits'][0]['provider'], 'apollo')

    def test_verified_importer_event_is_audited_without_upgrading_body_completeness(self):
        body = 'Resultado garantido — 40%'
        plan = plan_email({'source': {'provider': 'apollo', 'account_id': APOLLO_TEAM,
            'external_campaign_id': APOLLO_SEQUENCE, 'campaign_id': BR, 'provider_id': 'external-1'},
            'event_type': 'email_sent', 'lead_email': 'buyer@example.invalid', 'occurred_at': DATE,
            'subject': 'Provider original', 'text': body}, '700')
        self.repo.put_state('hubspot_attempt', 'fixture-import', {'status': 'verified_state', 'plan': plan})
        before = self.database()
        detail = self.detail()
        audit, = detail['sent_email_audits']
        self.assertEqual(audit['status'], 'unverified')
        self.assertEqual(audit['reason'], 'body_incomplete')
        self.assertEqual(audit['original_id'], 'external-1')
        self.assertEqual(audit['source_id'], 'apollo:' + APOLLO_TEAM + ':external-1')
        self.assertEqual(detail['blocks'][0]['text'], body)
        self.assertTrue(audit['alerts'])
        self.assertEqual(before, self.database())

    def test_changed_canonical_proofs_invalidate_old_detail_without_approval_changes(self):
        self.add_thread('40%')
        snap = self.adapter.snapshot(campaign=BR)
        self.register(BR, proofs='40%')
        with self.assertRaises(TodayError) as exc:
            self.detail(snap)
        self.assertEqual(exc.exception.code, 'journey_detail_stale')
        new = self.adapter.snapshot(campaign=BR)
        self.assertEqual(self.detail(new)['sent_email_audits'][0]['status'], 'no_alerts')
        self.assertFalse(self.detail(new)['approval_available'])
        self.assertNotIn('alerta', new['timeline']['events'][0]['summary'])


if __name__ == '__main__':
    unittest.main()
