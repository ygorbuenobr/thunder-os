"""Offline attestation of the patched legacy source, never a host-wide proof.

The expected digest must come from the reviewed release/manifest, not from the
candidate being inspected. No target module is imported and no database opened.
"""
import ast
import hashlib
import re
from pathlib import Path


class LegacyExclusionError(ValueError):
    pass


_GUARD = '''def exigir_leitura_legada(metodo):
    if metodo != "GET":
        raise ErroEnvio("legacy_writer_excluded; usar facade coordenada")
'''


def _body(function):
    result = function.body
    if result and isinstance(result[0], ast.Expr) and isinstance(result[0].value, ast.Constant) and isinstance(result[0].value.value, str):
        result = result[1:]
    return result


def _same(left, right):
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def verify_legacy_exclusion(path, *, expected_sha256):
    """Fail closed on hash, guarded sinks or entrypoint drift; no mutation."""
    if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise LegacyExclusionError('reviewed_digest_required')
    source = Path(path).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != expected_sha256:
        raise LegacyExclusionError('legacy_artifact_changed')
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        raise LegacyExclusionError('legacy_source_invalid') from None
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if len(functions) != sum(isinstance(n, ast.FunctionDef) for n in tree.body):
        raise LegacyExclusionError('legacy_function_redefined')
    # The retired source has no transport at all. Keep the previous verifier
    # below for historical reviewed installations; never rewrite their pins.
    retired_raise = ast.parse('raise ErroEnvio("legacy_writer_excluded; integração legada aposentada; use ConnectSafely pelo painel")').body[0]
    retired_names = ('exigir_leitura_legada', 'api', 'api_post_multipart', 'post_convite',
                     'aquecer', '_aquecer', 'enviar', 'comentar', 'convidar', 'inbox',
                     'aceites', 'executar_envios', 'executar_comentarios', 'executar_convites')
    retired = all(name in functions and len(_body(functions[name])) == 1
                  and _same(_body(functions[name])[0], retired_raise) for name in retired_names)
    if retired:
        calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
        if any(call in ('urllib.request.urlopen', 'urllib.request.Request') for call in calls):
            raise LegacyExclusionError('legacy_network_sink_changed')
        if any(isinstance(n, ast.Constant) and n.value == 'THUNDER_TOQUES_OBRIGATORIO' for n in ast.walk(tree)):
            raise LegacyExclusionError('legacy_reservation_override_present')
        return {'sha256': digest, 'writes_excluded': True, 'reads_excluded': True,
                'guarded_transports': [], 'scope': 'reviewed_source_only',
                'live_installation_verified': False}
    guard = functions.get('exigir_leitura_legada')
    if guard is None or not _same(ast.Module(body=_body(guard), type_ignores=[]), ast.Module(body=ast.parse(_GUARD).body[0].body, type_ignores=[])):
        raise LegacyExclusionError('legacy_exclusion_guard_changed')
    expected = {
        'api': 'exigir_leitura_legada(metodo)',
        'api_post_multipart': 'exigir_leitura_legada("POST")',
        'post_convite': 'exigir_leitura_legada("POST")',
        'aquecer': 'exigir_leitura_legada("POST")',
        '_aquecer': 'exigir_leitura_legada("POST")',
        'enviar': 'if not dry_run:\n exigir_leitura_legada("POST")',
        'comentar': 'if not dry_run:\n exigir_leitura_legada("POST")',
        'convidar': 'if not dry_run:\n exigir_leitura_legada("POST")',
    }
    for name, statement in expected.items():
        fn = functions.get(name)
        if fn is None or not _body(fn) or not _same(_body(fn)[0], ast.parse(statement).body[0]):
            raise LegacyExclusionError('legacy_entrypoint_unguarded')
    # Pin both Request construction and the actual network call, across ALL
    # module nodes, including nested functions and class bodies.
    owners = {}
    def visit(node, owner='<module>'):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        if isinstance(node, ast.Call) and ast.unparse(node.func) in ('urllib.request.urlopen', 'urllib.request.Request'):
            key = (owner, ast.unparse(node.func))
            owners[key] = owners.get(key, 0) + 1
        for child in ast.iter_child_nodes(node):
            visit(child, owner)
    visit(tree)
    sinks = {name for name in ('api', 'api_post_multipart', 'post_convite')}
    if owners != {(name, call): 1 for name in sinks for call in ('urllib.request.urlopen', 'urllib.request.Request')}:
        raise LegacyExclusionError('legacy_network_sink_changed')
    if any(isinstance(n, ast.Constant) and n.value == 'THUNDER_TOQUES_OBRIGATORIO' for n in ast.walk(tree)):
        raise LegacyExclusionError('legacy_reservation_override_present')
    return {'sha256': digest, 'writes_excluded': True, 'guarded_transports': sorted(sinks),
            'scope': 'reviewed_source_only', 'live_installation_verified': False}
