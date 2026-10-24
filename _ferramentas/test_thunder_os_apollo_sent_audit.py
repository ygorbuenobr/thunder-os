"""S27C8: deterministic advisory audit of fake sent messages, never writes."""
import copy
import hashlib
import unittest
from unittest.mock import patch

from thunder_os.apollo_sent_audit import audit_sent_email


def event(body='Nossa equipe atende 25 empresas.'):
    return {'id': 'sent-1', 'sent_at': '2026-10-24T12:00:00Z',
            'body_text': body, 'body_complete': True, 'source': 'apollo'}


class SentAuditTests(unittest.TestCase):
    def setUp(self):
        net = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        net.start()
        self.addCleanup(net.stop)

    def test_sent_email_audit_flags_prohibited_content(self):
        row = event('Resultado GARANTIDO — reduzimos 40% em 10 dias; atende 25 empresas.')
        original = copy.deepcopy(row)
        result = audit_sent_email(row, '25 empresas, avaliação em 10 dias.',
                                  forbidden_phrases=['resultado garantido'])
        self.assertEqual(result['status'], 'flagged')
        self.assertEqual({a['code'] for a in result['alerts']},
            {'em_dash', 'forbidden_phrase', 'number_not_in_proofs'})
        self.assertEqual([a['match'] for a in result['alerts']
                          if a['code'] == 'number_not_in_proofs'], ['40%'])
        self.assertEqual(result['original_id'], row['id'])
        self.assertEqual(result['date'], row['sent_at'])
        self.assertEqual(result['body_hash'], hashlib.sha256(row['body_text'].encode()).hexdigest())
        self.assertEqual(row, original)

    def test_number_tokens_do_not_allow_substrings_or_invent_semantic_proof(self):
        result = audit_sent_email(event('10 clientes, 25% e 1.500 unidades, 2026.'),
                                  '100 clientes, 25% e 1.500 unidades, 2026.', [])
        self.assertEqual([a['match'] for a in result['alerts']], ['10'])
        self.assertEqual(result['status'], 'flagged')
        allowed = audit_sent_email(event('25 empresas.'), '25 empresas.', [])
        self.assertEqual(allowed['status'], 'no_alerts')
        self.assertIn('lexical', allowed['reason'])

    def test_incomplete_missing_body_proofs_or_provenance_never_reports_clean(self):
        cases = [
            ({**event(), 'body_complete': False}, '25'),
            ({k: v for k, v in event().items() if k != 'body_complete'}, '25'),
            ({**event(), 'body_text': ''}, '25'),
            ({**event(), 'body_text': None, 'snippet': '25 empresas'}, '25'),
            ({**event(), 'id': None}, '25'),
            ({**event(), 'sent_at': None}, '25'),
            (event(), None), (event(), ''), (None, '25'),
            ({**event(), 'body_complete': 1}, '25'),
            ({**event(), 'body_complete': True, 'content_complete': False}, '25'),
        ]
        for row, proofs in cases:
            with self.subTest(row=row, proofs=proofs):
                self.assertEqual(audit_sent_email(row, proofs)['status'], 'unverified')
        partial = audit_sent_email({**event('— 40'), 'body_complete': False}, '25', [])
        self.assertEqual(partial['status'], 'unverified')
        self.assertEqual({a['code'] for a in partial['alerts']},
                         {'em_dash', 'number_not_in_proofs'})

    def test_aliases_and_html_keep_original_hash(self):
        html = '<p>Resultado &mdash; 40%</p>'
        row = {'message_id': 'sent-2', 'date': '2026-10-24', 'body_html': html,
               'content_complete': True}
        result = audit_sent_email(row, '25%', [])
        self.assertEqual(result['status'], 'flagged')
        self.assertEqual(result['original_id'], 'sent-2')
        self.assertEqual(result['body_hash'], hashlib.sha256(html.encode()).hexdigest())
        self.assertEqual({a['code'] for a in result['alerts']},
                         {'em_dash', 'number_not_in_proofs'})
        for key in ('text', 'body', 'content'):
            self.assertEqual(audit_sent_email({'original_id': 's1', 'date': '2026',
                key: '25 empresas.', 'body_complete': True}, '25', [])['status'], 'no_alerts')

    def test_invalid_and_ambiguous_inputs_are_bounded_and_unverified(self):
        for row, proofs, phrases in [
            ({**event(), 'body_text': '\ud800'}, '25', []),
            (event(), '25', 'not-a-list'), (event(), '25', ['']),
            (event(), '25', [None]), (event(), '25', ['x'] * 101),
            ({**event(), 'text': 'Conflicting text'}, '25', []),
            (event('x' * 1_000_001), '25', []),
            (event(), 'x' * 1_000_001, []),
        ]:
            self.assertEqual(audit_sent_email(row, proofs, phrases)['status'], 'unverified')

    def test_default_forbidden_phrases_and_repeatability(self):
        row = event('Resultado garantido — 99%')
        self.assertEqual(audit_sent_email(row, '25'), audit_sent_email(row, '25'))
        self.assertIn('forbidden_phrase', {a['code'] for a in audit_sent_email(row, '25')['alerts']})


if __name__ == '__main__':
    unittest.main()
