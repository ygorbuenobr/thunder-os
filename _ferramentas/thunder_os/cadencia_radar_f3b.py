"""F3b: daily radar rehearsal over synthetic, normalized source batches.

No provider transports, credentials, CRM writers, database or scheduling hooks.
Adapters below describe fixture capabilities, not verified native API schemas.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import unicodedata
from datetime import date, timedelta
from urllib.parse import urlsplit, urlunsplit

MODO_SIMULACAO = True
MINIMUM_SOURCES = ('connectsafely_search_jobs', 'google_news', 'company_registry')
ADAPTERS = {
    'connectsafely_search_jobs': 'search-jobs',
    'google_news': 'news_with_publication_filter',
    'company_registry': 'new_branches_casa_dos_dados_or_cnpj_biz',
    'gupy': 'normalized_jobs', 'events': 'normalized_events',
    'apollo': 'normalized_research', 'research': 'normalized_research',
    'internal': 'normalized_interactions',
}
SEGMENTS = ('fabricantes_rede_revendas', 'distribuidores_tecnicos')
ROLES = ('marketing', 'comercial', 'revops', 'crm')
FRONTS = {'crm', 'revops', 'hubspot', 'outbound', 'pré-vendas', 'geração de demanda',
          'geração de demanda b2b', 'rotina comercial', 'rotina comercial integrada a vendas',
          'posicionamento', 'posicionamento e narrativa', 'conteúdo', 'conteúdo de autoridade',
          'mídia', 'mídia e performance', 'tracking', 'cro', 'landing pages e cro',
          'crm, tracking e mensuração', 'narrativa', 'mensuração'}


def parse_catalog(markdown):
    """Read all 17 IDs/weights/validities from the supplied canonical Markdown.

    momento-setor is an internal key for section 3 (the catalogue gives no ID).
    Its 60 days / max weight 2 come from that section, not a provider.
    """
    catalog = {}
    for line in markdown.splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) < 4 or not re.fullmatch(r'[EP]\d{2}-[a-z0-9-]+', cells[0]):
            continue
        key = cells[0]
        if key in catalog:
            raise ValueError('duplicate_catalog_id')
        weight = re.match(r'^[0-3](?:\D|$)', cells[2])
        days = re.search(r'\d+', cells[3])
        if not weight or not days:
            raise ValueError('invalid_catalog_weight_or_validity')
        catalog[key] = {'weight': int(cells[2][0]), 'days': int(days.group()),
                        'event_relative': key.startswith('E10-')}
    expected = {f'E{i:02}' for i in range(1,13)} | {f'P{i:02}' for i in range(1,6)}
    if len(catalog) != 17 or {key[:3] for key in catalog} != expected:
        raise ValueError('complete_catalog_required')
    catalog['momento-setor'] = {'weight': 2, 'days': 60, 'event_relative': False}
    return catalog


def canonical_domain(value):
    if not isinstance(value, str):
        raise ValueError('domain_required')
    value = value.lower().strip().removeprefix('www.').rstrip('.')
    if len(value) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value):
        raise ValueError('invalid_domain')
    labels = value.split('.')
    if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in labels):
        raise ValueError('invalid_domain')
    if not re.fullmatch(r'[a-z]{2,63}', labels[-1]):
        raise ValueError('invalid_domain')
    return value


def name_key(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('company_required')
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


def iso_day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('publication_date_required')
    return date.fromisoformat(value)


def source_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError('source_url_required')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('source_url_required')
    return value


def evidence_key(url):
    parsed = urlsplit(url)
    return urlunsplit(('https', parsed.hostname.lower().removeprefix('www.'),
                       parsed.path.rstrip('/'), '', ''))


def search_plan(segment, roles, today):
    if segment not in SEGMENTS or not roles or any(role not in ROLES for role in roles):
        raise ValueError('supported_segment_and_roles_required')
    return [{'source': source, 'operation': ADAPTERS[source], 'segment': segment,
             'roles': sorted(set(roles)) if source == 'connectsafely_search_jobs' else [],
             'published_after': (today-timedelta(days=180)).isoformat(),
             'published_before': today.isoformat(), 'mode': 'fixture_only'}
            for source in MINIMUM_SOURCES]


def normalize_signal(row, source, catalog, today):
    domain = canonical_domain(row.get('domain'))
    name_key(row.get('company'))
    key = row.get('catalog_id')
    if key not in catalog:
        raise ValueError('unknown_catalog_id')
    policy = catalog[key]
    if 'weight' in row and (type(row['weight']) is not int or row['weight'] != policy['weight']):
        raise ValueError('weight_conflicts_with_catalog')
    if 'validity_days' in row and row['validity_days'] != policy['days']:
        raise ValueError('validity_conflicts_with_catalog')
    published = iso_day(row.get('published_on'))
    if published > today:
        raise ValueError('future_publication')
    url = source_url(row.get('url'))
    if key.startswith('P04-') and source != 'internal':
        raise ValueError('P04_requires_internal_evidence')
    if source in ('connectsafely_search_jobs', 'gupy') and key[:3] not in ('E01','E02','E03'):
        raise ValueError('job_adapter_signal_mismatch')
    if source == 'company_registry' and not key.startswith('E06-'):
        raise ValueError('registry_adapter_signal_mismatch')
    anchor = published
    if key[:3] in ('E05','P02','E10'):
        anchor = iso_day(row.get('event_on'))
        if key[:3] != 'E10' and anchor > today:
            raise ValueError('future_role_change')
        if key[:3] == 'E10' and anchor > today+timedelta(days=30):
            raise ValueError('event_beyond_30_day_radar')
    expiry = anchor+timedelta(days=policy['days'])
    if today > expiry or (today-published).days > 365:
        raise ValueError('expired_signal')
    for field in ('fact', 'alternative', 'event_key'):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f'{field}_required')
    front = row.get('front', '')
    if not isinstance(front, str):
        raise ValueError('front_invalid')
    is_context = front.casefold().strip() not in FRONTS or policy['weight'] == 1
    if key == 'momento-setor' and not row.get('account_connection'):
        is_context = True
    return {**copy.deepcopy(row), 'domain': domain, 'source': source, 'url': url,
            'weight': policy['weight'], 'validity_days': policy['days'],
            'expires_on': expiry.isoformat(), 'context_only': is_context,
            'risk': key.startswith('E12-')}


def simulate(snapshot, today, catalog_markdown, *, mode=MODO_SIMULACAO):
    if mode is not True:
        raise ValueError('real_mode_not_implemented_or_authorized')
    if type(today) is not date:
        raise ValueError('explicit_date_required')
    catalog = parse_catalog(catalog_markdown)
    report = {'mode':'MODO_SIMULACAO', 'external_effects':0,
              'catalog_sha256':hashlib.sha256(catalog_markdown.encode()).hexdigest(),
              'search_plan':[], 'candidates':[], 'context':[], 'rejected':[], 'signal_counts':{}}

    def reject(reason, domain=None, source=None):
        report['rejected'].append({'reason':reason, 'domain':domain, 'source':source})

    if snapshot.get('synthetic') is not True or not snapshot.get('owner_id'):
        reject('synthetic_snapshot_and_owner_required')
        return report
    try:
        report['search_plan'] = search_plan(snapshot.get('segment'), snapshot.get('roles'), today)
    except ValueError as exc:
        reject(str(exc))
        return report
    batches = snapshot.get('sources', {})
    if not isinstance(batches, dict) or any(s not in batches for s in MINIMUM_SOURCES):
        reject('minimum_source_batches_required')
        return report
    # Complete fixture sets are mandatory; missing pages can hide exclusion/risk evidence.
    for source, batch in batches.items():
        if (source not in ADAPTERS or not isinstance(batch, dict) or batch.get('status') != 'ok'
                or batch.get('complete') is not True or not isinstance(batch.get('rows'), list)
                or any(not isinstance(row, dict) for row in batch['rows'])):
            reject('source_batch_failed_or_incomplete', source=source)
            return report
    accounts = snapshot.get('accounts')
    if not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts):
        reject('account_checks_required')
        return report
    by_domain, names, domains, blocked = {}, {}, {}, set()

    def identity(company, raw_domain):
        domain, name = canonical_domain(raw_domain), name_key(company)
        names.setdefault(name,set()).add(domain)
        domains.setdefault(domain,set()).add(name)
        return domain

    for a in accounts:
        try:
            domain = identity(a.get('name'),a.get('domain'))
        except ValueError as exc:
            reject(str(exc))
            return report  # Invalid account identity may conceal an exclusion.
        normalized = {**a,'name':name_key(a['name']),'domain':domain}
        if domain in by_domain and normalized != by_domain[domain]:
            blocked.add(domain)
        by_domain[domain] = normalized
    rows = []
    for source in sorted(batches):
        for row in batches[source]['rows']:
            try:
                domain = identity(row.get('company'),row.get('domain'))
            except ValueError as exc:
                reject(str(exc),source=source)
                continue
            rows.append((source, domain, row))
    for domain, aliases in domains.items():
        if len(aliases)>1:
            blocked.add(domain)
    for matches in names.values():
        if len(matches)>1:
            blocked.update(matches)
    for domain in sorted(blocked):
        reject('conflicting_company_identity_or_checks',domain)
    signals = {}
    for source, domain, row in rows:
        try:
            item = normalize_signal(row,source,catalog,today)
        except (ValueError, TypeError) as exc:
            reject(str(exc),domain,source)
            # Incomplete risk evidence cannot silently clear a safety concern.
            if str(row.get('catalog_id','')).startswith('E12-') and str(exc) != 'expired_signal':
                blocked.add(domain)
            continue
        if item['risk']:
            blocked.add(domain)
            reject('risk_E12',domain,source)
        elif item['context_only']:
            report['context'].append(item)
        else:
            signals.setdefault(domain,[]).append(item)
    for domain in sorted(signals):
        if domain in blocked:
            continue
        account = by_domain.get(domain)
        if not account or account.get('checks_complete') is not True:
            reject('account_checks_unknown',domain)
            continue
        if (account.get('segment') != snapshot['segment'] or account.get('size_compatible') is not True
                or any(account.get(k) is not False for k in ('client','beverages','open_deal','mexico'))
                or type(account.get('exists_in_crm')) is not bool
                or account.get('owner_id') not in (None,snapshot['owner_id'])
                or (account['exists_in_crm'] and account.get('owner_id') != snapshot['owner_id'])):
            reject('icp_exclusion_or_unknown',domain)
            continue
        # All vacancies count as a single family, including cross-posts/providers.
        groups = {}
        for item in signals[domain]:
            key = 'jobs' if item['catalog_id'][:3] in ('E01','E02','E03') else item['event_key']
            groups.setdefault(key,[]).append(item)
        selected = []
        for group in groups.values():
            ordered = sorted(group,key=lambda s:(-s['weight'],s['catalog_id'],s['url']))
            best = copy.deepcopy(ordered[0])
            best['evidence'] = [{'url':s['url'],'published_on':s['published_on'],'source':s['source']}
                                for s in ordered]
            selected.append(best)
        report['signal_counts'][domain] = len(selected)
        strong = any(s['weight']==3 for s in selected)
        convergent = any(a['weight']==b['weight']==2
                        and a['catalog_id'] != b['catalog_id'] and a['event_key'] != b['event_key']
                        and evidence_key(a['url']) != evidence_key(b['url'])
                        and a.get('convergence_key') and a.get('convergence_key')==b.get('convergence_key')
                        and a.get('convergence_reason') and b.get('convergence_reason')
                        and a['front'].casefold().strip()==b['front'].casefold().strip()
                        for i,a in enumerate(selected) for b in selected[i+1:])
        if not strong and not convergent:
            reject('insufficient_independent_convergent_signals',domain)
            continue
        selected.sort(key=lambda s:(not s['catalog_id'].startswith('P04-'),-s['weight'],s['catalog_id']))
        report['candidates'].append({'company':signals[domain][0]['company'],'domain':domain,
                                     'arm':'B','status':'aguardando_aprovacao','signals':selected,
                                     'priority':0 if any(s['catalog_id'].startswith('P04-') for s in selected) else 1,
                                     'crm_write_authorized':False,
                                     'd0_handoff':'pending_Ygor_approval_and_segment_thesis'})
    report['candidates'].sort(key=lambda c:(c['priority'],c['domain']))
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',required=True)
    parser.add_argument('--catalog',required=True,help='Canonical sinais.md; read only')
    parser.add_argument('--today',required=True)
    args=parser.parse_args(argv)
    with open(args.fixture,encoding='utf-8') as handle:
        fixture=json.load(handle)
    with open(args.catalog,encoding='utf-8') as handle:
        catalog=handle.read()
    print(json.dumps(simulate(fixture,iso_day(args.today),catalog),ensure_ascii=False,indent=2))


if __name__=='__main__': main()
