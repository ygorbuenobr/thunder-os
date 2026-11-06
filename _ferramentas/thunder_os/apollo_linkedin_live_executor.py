"""Fail-closed entry point for the Apollo/LinkedIn canary.

There is intentionally no provider transport here. The legacy send paths do not
share the account lock and pause changes are not fenced with dispatch. Until
those invariants and a legitimate Apollo approval issuer exist, no argument or
environment variable enables a live send or Apollo task completion.
"""
from __future__ import annotations

import argparse
import json
import re

from .apollo_linkedin_attention import EXCLUDED_TASK

_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def execution_status(task, authorization):
    """Report why execution is impossible without contacting any provider."""
    if not valid_id(task) or not valid_id(authorization):
        return {'status': 'blocked', 'reason': 'configuration_invalid', 'executable': False}
    if task == EXCLUDED_TASK:
        return {'status': 'blocked', 'reason': 'fixture_protegida', 'executable': False}
    return {'status': 'blocked', 'reason': 'live_composition_unavailable',
            'executable': False,
            'missing': ['shared_send_lock', 'pause_dispatch_fence',
                        'apollo_approval_issuer', 'receipt_and_completion_composition']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Apollo LinkedIn canary (disabled)')
    parser.add_argument('--task', required=True)
    parser.add_argument('--authorization', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    result = execution_status(args.task, args.authorization)
    result['mode'] = 'execute_requested' if args.execute else 'dry_run'
    print(json.dumps(result, ensure_ascii=False))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
