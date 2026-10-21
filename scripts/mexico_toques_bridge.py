"""Pinned toques adapter; JSON stdin/stdout, no migration and no provider calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_ferramentas'))
from thunder_os.mexico_receipt import binding, validate_receipt

PIN = '84eb0c7a86fe8fbe72c11427e747895b30d6acc66135eb36e4a8d3aa28e1e875'


def load_dependency(path):
    path = Path(path).resolve(strict=True)
    if hashlib.sha256(path.read_bytes()).hexdigest() != PIN:
        raise ValueError('TOQUES_DEPENDENCY_MISMATCH')
    spec = importlib.util.spec_from_file_location('mexico_pinned_toques', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MexicoTouches:
    def __init__(self, module):
        self.module = module

    def apply(self, request):
        b = binding(request['binding'])
        subject = request['subject']
        if not isinstance(subject, str) or not subject.strip() or '\n' in subject or '\r' in subject:
            raise ValueError('SUBJECT_INVALID')
        # Preserve the historical subject key. Later steps may use a scoped key
        # only after accounting for a confirmed, exactly linked earlier touch.
        base = self.module.ja_existe('email', 'email', b['recipient'], subject)
        target = subject
        if b['touch'] != 'E1':
            if not base:
                raise ValueError('PREVIOUS_TOUCH_UNVERIFIED')
            try:
                prior = binding(json.loads(base['ref']))
                receipt = self._view(base, prior, False)['receipt']
                if (not receipt or any(prior[k] != b[k] for k in
                        ('campaign_id', 'batch', 'contact_id', 'sender', 'recipient'))
                        or int(prior['touch'][1:]) >= int(b['touch'][1:])):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise ValueError('PREVIOUS_TOUCH_UNVERIFIED') from None
            target = subject + ' #codex_local:' + b['campaign_id'] + ':' + b['batch'] + ':' + b['touch']
        args = ('email', 'email', b['recipient'], target)
        row = self.module.ja_existe(*args)
        if row:
            try:
                if json.loads(row['ref']) != b:
                    raise ValueError()
            except (TypeError, ValueError):
                raise ValueError('TOUCH_HISTORY_BINDING_MISMATCH') from None
        op = request['op']
        if op == 'reserve':
            if row:
                return self._view(row, b, False)
            identifier = self.module.reservar(*args, agente='codex_local',
                ref=json.dumps(b, sort_keys=True), detalhe=json.dumps({'binding': b, 'state': 'SEND_INTENT'}))
            row = self.module.ja_existe(*args)
            if not row or json.loads(row['ref']) != b:
                raise ValueError('TOUCH_RESERVATION_CONFLICT')
            return self._view(row, b, identifier is not None)
        if op == 'lookup':
            return self._view(row, b, False) if row else None
        if not row or str(row['id']) != str(request.get('touch_id')):
            raise ValueError('TOUCH_ID_MISMATCH')
        if row['status'] == 'feito':
            # The upstream API allows feito -> incerto. This caller never does.
            current = self._view(row, b, False)
            if op == 'confirm' and current['receipt'] != validate_receipt(request['receipt'], b):
                raise ValueError('TERMINAL_RECEIPT_MISMATCH')
            return current
        if op == 'confirm':
            receipt = validate_receipt(request['receipt'], b)
            detail = {'binding': b, 'state': 'PROVIDER_CONFIRMED', 'receipt': receipt}
            self._finish(row['id'], 'feito', json.dumps(detail), receipt['message_id'])
        elif op == 'uncertain':
            self._finish(row['id'], 'incerto', json.dumps({'binding': b, 'state': 'UNCERTAIN_NO_RETRY'}))
        else:
            raise ValueError('TOUCH_OPERATION_INVALID')
        result = self._view(self.module.ja_existe(*args), b, False)
        if op == 'confirm' and result['receipt'] != receipt:
            raise ValueError('TERMINAL_RECEIPT_MISMATCH')
        return result

    def _finish(self, identifier, status, detail, provider_id=None):
        # Same conditional-write pattern as TouchLedger.finish. Upstream concluir
        # lacks this predicate, so its read-then-write API cannot protect feito.
        with self.module._banco() as (connection, m):
            connection.cursor().execute(
                'UPDATE toques SET status=' + m + ', detalhe=' + m
                + ', provider_id=COALESCE(' + m + ',provider_id), atualizado_em=CURRENT_TIMESTAMP'
                + ' WHERE id=' + m + " AND status IN ('reservado','incerto')",
                (status, detail, provider_id, identifier))

    @staticmethod
    def _view(row, b, created):
        if json.loads(row['ref']) != b:
            raise ValueError('TOUCH_HISTORY_BINDING_MISMATCH')
        receipt = None
        if row['status'] == 'feito':
            detail = json.loads(row['detalhe'])
            receipt = validate_receipt(detail['receipt'], b)
            if row['provider_id'] != receipt['message_id']:
                raise ValueError('TOUCH_PROVIDER_MISMATCH')
        return {'touch_id': str(row['id']), 'status': row['status'], 'created': created, 'receipt': receipt}


def main():
    try:
        request = json.load(sys.stdin)
        result = MexicoTouches(load_dependency(sys.argv[1])).apply(request)
        print(json.dumps({'ok': True, 'result': result}))
        return 0
    except Exception as exc:
        # Driver/tool exception strings may include secrets; only contract codes leave here.
        code = str(exc) if isinstance(exc, ValueError) and str(exc).isupper() else 'TOQUES_OPERATION_FAILED'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
