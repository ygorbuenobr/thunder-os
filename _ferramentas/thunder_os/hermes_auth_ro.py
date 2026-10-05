"""Read-only operation auth. Official credential API, no token-file parsing.

Hermes 0.21.4 read_only=True still has corrupt-backup and pool-quota recovery
branches. The scoped audit guard denies filesystem mutation/network/processes
and rejects the result even if Hermes catches the denial internally. Deployment
must additionally mount all authentication/configuration paths read-only.
"""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ENDPOINT = 'https://chatgpt.com/backend-api/codex'


class ReadOnlyAuthError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_guard_depth = 0
_violation = False
_hook_installed = False


def _audit(event, args):
    global _violation
    if not _guard_depth:
        return
    denied = event in {
        'os.remove', 'os.rename', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.chown',
        'os.truncate', 'os.utime', 'os.link', 'os.symlink', 'os.setxattr', 'os.removexattr',
        'os.system', 'os.fork', 'os.forkpty', 'os.posix_spawn', 'subprocess.Popen',
        'socket.connect', 'socket.bind', 'socket.sendto', 'socket.getaddrinfo',
        'socket.gethostbyname', 'socket.gethostbyaddr', 'shutil.copyfile', 'shutil.copymode',
        'shutil.copystat', 'shutil.rmtree', 'shutil.move', 'os.chdir',
    }
    if event == 'open':
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        denied = (isinstance(mode, str) and any(char in mode for char in 'wax+')) or (
            isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
    if denied:
        _violation = True
        raise ReadOnlyAuthError('auth_readonly_violation')


@contextmanager
def readonly_resolution():
    global _guard_depth, _violation, _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit)
        _hook_installed = True
    outer = _guard_depth == 0
    if outer:
        _violation = False
    _guard_depth += 1
    bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = bytecode
        _guard_depth -= 1
        if outer and _violation:
            raise ReadOnlyAuthError('auth_readonly_violation')


def require_endpoint(value, *, request=False):
    try:
        url = urlsplit(str(value))
        path = '/backend-api/codex/responses' if request else '/backend-api/codex'
        if (url.scheme != 'https' or url.hostname != 'chatgpt.com' or url.port not in (None, 443)
                or url.username is not None or url.password is not None or url.query or url.fragment
                or url.path.rstrip('/') != path):
            raise ValueError()
    except (TypeError, ValueError):
        raise ReadOnlyAuthError('auth_endpoint_blocked') from None
    return ENDPOINT


def load_operation_config(profile):
    """Read YAML without Hermes config migrations, backup, dotenv or defaults."""
    with readonly_resolution():
        import yaml
        with (Path(profile) / 'config.yaml').open('rb') as stream:
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ReadOnlyAuthError('auth_config_invalid')
        try:
            data = yaml.safe_load(raw)
            cfg = data['model']
            model = cfg['default']
            if (not isinstance(cfg, dict) or cfg.get('provider') != 'openai-codex'
                    or not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/+\-]*', model)
                    or cfg.get('openai_runtime') == 'codex_app_server'):
                raise ValueError()
            endpoint = cfg.get('base_url') or ENDPOINT
            require_endpoint(endpoint)
        except ReadOnlyAuthError:
            raise
        except Exception:
            raise ReadOnlyAuthError('auth_config_invalid') from None
    # Neither arbitrary config keys nor any credential values escape this projection.
    return {'model': {'provider': 'openai-codex', 'default': model, 'base_url': ENDPOINT}}


def resolve_operation_runtime(*, requested, target_model):
    if requested != 'openai-codex' or not isinstance(target_model, str) or not target_model:
        raise ReadOnlyAuthError('auth_config_invalid')
    try:
        with readonly_resolution():
            from hermes_cli.auth_codex import resolve_codex_runtime_credentials, _codex_access_token_is_expiring
            runtime = resolve_codex_runtime_credentials(read_only=True, force_refresh=False, refresh_if_expiring=False)
            if (not isinstance(runtime, dict) or runtime.get('provider') != 'openai-codex'
                    or not isinstance(runtime.get('api_key'), str) or not runtime['api_key']
                    or runtime.get('source') != 'hermes-auth-store'):
                raise ReadOnlyAuthError('auth_unavailable')
            require_endpoint(runtime.get('base_url'))
            if _codex_access_token_is_expiring(runtime['api_key'], 0):
                raise ReadOnlyAuthError('auth_expired')
    except ReadOnlyAuthError:
        raise
    except Exception:
        raise ReadOnlyAuthError('auth_unavailable') from None
    return {**runtime, 'api_mode': 'codex_responses', 'base_url': ENDPOINT}


def create_operation_client(*, api_key, base_url, max_retries=0, timeout=120):
    require_endpoint(base_url)
    if max_retries != 0:
        raise ReadOnlyAuthError('auth_config_invalid')
    import httpx
    from openai import OpenAI
    from agent.codex_headers import codex_cloudflare_headers
    def exact_request(request):
        require_endpoint(str(request.url), request=True)
    http = httpx.Client(trust_env=False, follow_redirects=False, verify=True,
        timeout=timeout, event_hooks={'request': [exact_request]})
    try:
        return OpenAI(api_key=api_key, base_url=ENDPOINT, http_client=http, max_retries=0,
            timeout=timeout, default_headers=codex_cloudflare_headers(api_key, base_url=ENDPOINT))
    except Exception:
        http.close()
        raise
