"""Offline contracts for an explicitly requested campaign report.

Validation checks shape and correlation, not the authenticity of evidence. The
caller must obtain evidence from trusted runtime records. This module does not
schedule work, read providers, deliver reports, or authorize external effects.
"""
from collections.abc import Mapping
from datetime import datetime
import re


def _text(value, limit=2000):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def validate_request(request):
    if (not isinstance(request, Mapping)
            or set(request) != {'type', 'campaign_id', 'request_id', 'origin'}
            or request['type'] != 'campaign_report'
            or request['campaign_id'] != 'BR-APOLLO-01'
            or not isinstance(request['origin'], str)
            or request['origin'] not in {'panel', 'telegram'}
            or not isinstance(request['request_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request['request_id'])):
        raise ValueError('invalid campaign report request')
    return dict(request)


def validate_report(request, report):
    request = validate_request(request)
    fields = {'request_id', 'campaign_id', 'summary', 'sources', 'limitations', 'delegation', 'review'}
    if (not isinstance(report, Mapping) or set(report) != fields
            or report['request_id'] != request['request_id']
            or report['campaign_id'] != request['campaign_id']
            or not _text(report['summary'], 10000)):
        raise ValueError('invalid report identity or fields')
    sources = report['sources']
    if not isinstance(sources, list) or len(sources) != 2:
        raise ValueError('Apollo and HubSpot sources required')
    providers = set()
    for source in sources:
        if (not isinstance(source, Mapping) or set(source) != {'provider', 'observed_at', 'coverage'}
                or not isinstance(source['provider'], str)
                or source['provider'] not in {'apollo', 'hubspot'}
                or not isinstance(source['coverage'], str)
                or source['coverage'] not in {'complete', 'partial', 'unknown'}
                or not isinstance(source['observed_at'], str)):
            raise ValueError('invalid source')
        try:
            observed = datetime.fromisoformat(source['observed_at'].replace('Z', '+00:00'))
            if observed.utcoffset() is None:
                raise ValueError('timezone required')
        except ValueError as exc:
            raise ValueError('invalid source timestamp') from exc
        providers.add(source['provider'])
    if providers != {'apollo', 'hubspot'}:
        raise ValueError('Apollo and HubSpot sources required')
    limits = report['limitations']
    if (not isinstance(limits, list) or any(not _text(x) for x in limits)
            or (any(s['coverage'] != 'complete' for s in sources) and not limits)):
        raise ValueError('partial or unknown coverage requires limitations')
    for name in ('delegation', 'review'):
        evidence = report[name]
        if (not isinstance(evidence, Mapping) or set(evidence) != {'execution_id', 'status'}
                or evidence['status'] != 'completed' or not _text(evidence['execution_id'], 100)):
            raise ValueError('completed delegation and review records required')
    return dict(report)
