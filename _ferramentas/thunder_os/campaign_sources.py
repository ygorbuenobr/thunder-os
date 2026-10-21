"""Bounded source-card preparation; evidence is not intent or send authority."""
from collections.abc import Mapping
from copy import deepcopy
import csv
from datetime import date,datetime
import hashlib
import io
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .cadencia_radar_f3b import canonical_domain, evidence_key, name_key, normalize_signal, parse_catalog, source_url
from .connection_intake import profile_key
from .models import payload_hash

MAX_ROWS=1000


def read_list(text):
    if not isinstance(text,str) or len(text)>1_000_000:
        raise ValueError('source_csv_invalid')
    reader=csv.DictReader(io.StringIO(text))
    fields={'company','domain','email','linkedin_url'}
    if not reader.fieldnames or set(reader.fieldnames)!=fields or len(reader.fieldnames)!=len(fields):
        raise ValueError('source_csv_columns_invalid')
    rows=[]
    for row in reader:
        if len(rows)>=MAX_ROWS or None in row or any(v is None for v in row.values()):
            raise ValueError('source_csv_bounds_invalid')
        rows.append(dict(row))
    return rows


def _public_evidence(url,kind):
    value=source_url(url)
    parsed=urlsplit(value)
    try:
        port=parsed.port
    except ValueError:
        raise ValueError('source_url_invalid') from None
    if port is not None or parsed.scheme!='https':
        raise ValueError('source_url_invalid')
    host=parsed.hostname
    if kind=='engagers':
        valid=host in {'linkedin.com','www.linkedin.com'} and parsed.path.startswith(('/posts/','/feed/update/'))
    else:
        valid=(host in {'linkedin.com','www.linkedin.com'} and parsed.path.startswith('/jobs/view/')) or host=='gupy.io' or host.endswith('.gupy.io')
    if not valid:
        raise ValueError('source_host_invalid')
    return value


def _fit(check,today,domain):
    if (not isinstance(check,Mapping) or check.get('complete') is not True
            or check.get('checked_on')!=today.isoformat() or not isinstance(check.get('source'),str)
            or not check['source'].strip() or 'campanha_ativa' not in check
            or (check['campanha_ativa'] is not None and not isinstance(check['campanha_ativa'],str))):
        return 'canonical_checks_unknown'
    try:
        if canonical_domain(check.get('domain'))!=domain:return 'canonical_identity_unknown'
    except ValueError:
        return 'canonical_identity_unknown'
    if check.get('campanha_ativa'):
        return 'already_in_campaign'
    revenue=check.get('annual_revenue_brl')
    if (check.get('b2b') is not True or check.get('country')!='BR'
            or check.get('sector') not in {'industry','distribution'}
            or type(revenue) not in (int,float) or not math.isfinite(revenue) or revenue<=10_000_000
            or any(check.get(k) is not False for k in ('client','open_deal','suppressed','risk'))):
        return 'icp_not_confirmed'
    return None


def prepare_sources(kind, rows, *, today, catalog_markdown, account_lookup=None,
                    approved_seeds=(), source_complete=False):
    if kind not in {'list','jobs','engagers'} or type(today) is not date or not isinstance(rows,list) or len(rows)>MAX_ROWS:
        raise ValueError('source_batch_invalid')
    catalog=parse_catalog(catalog_markdown)
    seeds={profile_key(value) for value in approved_seeds}
    report={'kind':kind,'collected_on':today.isoformat(),'source_complete':source_complete is True,
            'coverage_scope':'provided_bounded_batch','candidates':[],'held':[],
            'external_writes':0,'crm_writes':0,'approved':False,
            'catalog_sha256':hashlib.sha256(catalog_markdown.encode()).hexdigest()}
    grouped={}
    blocked=set()
    blocked_domains=set()
    names={}
    profiles={}
    for raw in rows:
        if not isinstance(raw,Mapping):continue
        try:domain=canonical_domain(raw.get('domain'))
        except ValueError:domain=None
        try:
            name=name_key(raw.get('company'))
            if domain:names.setdefault(name,set()).add(domain)
        except ValueError:pass
        if kind=='engagers':
            try:profiles.setdefault(profile_key(raw.get('linkedin_url')),set()).add(domain)
            except ValueError:pass
    blocked.update(key for key,domains in profiles.items() if len(domains)>1)
    unresolved_risk=False
    for raw in rows:
        if not isinstance(raw,Mapping) or not str(raw.get('catalog_id','')).startswith('E12-'):continue
        try:
            normalize_signal(raw,'research',catalog,today)
        except (ValueError,TypeError) as error:
            if str(error)=='expired_signal':continue
        targets=set()
        try:targets.add(canonical_domain(raw.get('domain')))
        except ValueError:pass
        try:targets.update(names.get(name_key(raw.get('company')),set()))
        except ValueError:pass
        blocked_domains.update(targets)
        unresolved_risk=unresolved_risk or not targets
    checks={}
    for raw in rows:
        row=deepcopy(raw)
        reason=None
        try:
            if not isinstance(row,Mapping):raise ValueError('source_row_invalid')
            domain=canonical_domain(row.get('domain'))
            if kind=='engagers' and profile_key(row.get('linkedin_url')) in blocked:
                raise ValueError('conflicting_person_company')
            company=row.get('company')
            if not isinstance(company,str) or not 1<=len(company.strip())<=300:
                raise ValueError('company_required')
            if str(row.get('catalog_id','')).startswith('E12-'):
                try:
                    normalize_signal(row,'research',catalog,today)
                except ValueError as error:
                    if str(error)=='expired_signal':
                        raise ValueError('expired_risk_signal') from None
                blocked_domains.add(domain)
                raise ValueError('risk_signal')
            if source_complete is not True:raise ValueError('source_coverage_incomplete')
            evidence=None
            if kind!='list':
                published=date.fromisoformat(row.get('published_on',''))
                age=(today-published).days
                if age<0:raise ValueError('future_publication')
                if kind=='jobs' and age>30:raise ValueError('job_older_than_30_days')
                if kind=='engagers' and age>14:raise ValueError('engagement_older_than_14_days')
                if kind=='engagers' and profile_key(row.get('seed_profile')) not in seeds:
                    raise ValueError('seed_not_approved')
                row['url']=_public_evidence(row.get('url'),kind)
                if kind=='engagers' and row.get('catalog_id')!='P03-engajou':
                    raise ValueError('engagement_signal_mismatch')
                adapter='connectsafely_search_jobs' if kind=='jobs' and urlsplit(row['url']).hostname in {'linkedin.com','www.linkedin.com'} else 'gupy' if kind=='jobs' else 'research'
                evidence=normalize_signal(row,adapter,catalog,today)
                if evidence['risk']:raise ValueError('risk_signal')
            if domain not in checks:
                try:
                    checks[domain]=account_lookup(domain) if callable(account_lookup) else None
                except Exception:
                    checks[domain]=None
                    raise ValueError('canonical_checks_unavailable') from None
            reason=_fit(checks[domain],today,domain)
            if reason:raise ValueError(reason)
            key=profile_key(row.get('linkedin_url')) if kind=='engagers' else domain
            if key in blocked:raise ValueError('conflicting_person_company')
            candidate=grouped.setdefault(key,{'id':payload_hash({'kind':kind,'key':key}),
                'company':company,'domain':domain,'kind':kind,'status':'aguardando_aprovacao',
                'signals':[],'recurrence_score':0,'purchase_intent_verified':False,
                'copy_context':{'company':company,'domain':domain,'sector':checks[domain]['sector']},
                'research_request':{'capability':'research','account_id':domain},
                'contact_profile':key if kind=='engagers' else None,
                'crm_write_authorized':False})
            if kind=='engagers' and candidate['domain']!=domain:
                blocked.add(key)
                grouped.pop(key,None)
                raise ValueError('conflicting_person_company')
            if evidence and evidence_key(evidence['url']) not in {evidence_key(s['url']) for s in candidate['signals']}:
                candidate['signals'].append(evidence)
                candidate['recurrence_score']=len(candidate['signals']) if kind=='engagers' else 0
        except (ValueError,TypeError,KeyError) as exc:
            reason=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z][a-z0-9_]*',str(exc)) else 'source_fields_invalid'
            report['held'].append({'company':row.get('company') if isinstance(row,Mapping) else None,
                'domain':row.get('domain') if isinstance(row,Mapping) else None,
                'source_id':row.get('source_id') if isinstance(row,Mapping) else None,'reason':reason})
            if kind=='engagers' and isinstance(row,Mapping):
                try:report['held'][-1]['contact_profile']=profile_key(row.get('linkedin_url'))
                except ValueError:pass
    report['candidates']=sorted((c for c in grouped.values() if c['domain'] not in blocked_domains),
                                key=lambda c:(-c['recurrence_score'],c['domain']))
    report['risk_identity_unresolved']=unresolved_risk
    if unresolved_risk:
        report['candidates']=[]
    report['blocked_domains']=sorted(blocked_domains)
    return report


class ReadOnlySources:
    """Injected, bounded readers; no credential discovery or guessed HTTP routes."""
    def __init__(self, ports=None):
        self.ports=dict(ports or {})

    def collect(self, kind, query, *, enabled=False):
        if enabled is not True or kind not in self.ports:
            return {'status':'configuration_pending','rows':[],'complete':False,'provider_calls':0}
        if kind not in {'jobs','gupy','engagers'} or not isinstance(query,Mapping):
            raise ValueError('source_query_invalid')
        # One collector invocation; its adapter owns pagination and its quota check.
        result=self.ports[kind](deepcopy(dict(query)))
        if (not isinstance(result,Mapping) or not isinstance(result.get('rows'),list)
                or len(result['rows'])>MAX_ROWS or type(result.get('complete')) is not bool):
            raise ValueError('source_response_invalid')
        return deepcopy(dict(result))


class CampaignSources:
    def __init__(self, repo, clock=None):
        self.repo=repo
        self.clock=clock or (lambda:datetime.now(ZoneInfo('America/Sao_Paulo')).date())

    def record(self, batch_id, report):
        if (not isinstance(batch_id,str) or not 1<=len(batch_id)<=100
                or not isinstance(report,Mapping) or report.get('approved') is not False
                or report.get('crm_writes')!=0 or report.get('external_writes')!=0):
            raise ValueError('source_observation_invalid')
        return self.repo.put_state('campaign_source',batch_id,deepcopy(dict(report)))

    def snapshot(self):
        batches=sorted(self.repo.list_state('campaign_source'),key=lambda row:row.get('updated_at',''))
        current=[row for row in batches if row.get('collected_on')==self.clock().isoformat()]
        accounts={}
        profiles={}
        for row in current:
            positives={candidate['domain']:candidate for candidate in row.get('candidates',[])}
            negative=set(row.get('blocked_domains',[]))
            for held in row.get('held',[]):
                try:
                    domain=canonical_domain(held.get('domain'))
                    if domain not in positives:negative.add(domain)
                    if held.get('contact_profile'):profiles.setdefault(held['contact_profile'],set()).add(domain)
                except ValueError:pass
            for domain in negative:accounts.pop(domain,None)
            for domain,candidate in positives.items():
                if domain not in negative:accounts[domain]=deepcopy(candidate)
                if candidate.get('contact_profile'):profiles.setdefault(candidate['contact_profile'],set()).add(domain)
        conflicted={key for key,domains in profiles.items() if len(domains)>1}
        candidates=[c for c in accounts.values() if c.get('contact_profile') not in conflicted]
        if any(row.get('risk_identity_unresolved') is True for row in current):candidates=[]
        return {'automatic_collection_enabled':False,'crm_write_enabled':False,'source':'repository',
            'sources':[{key:row.get(key) for key in ('id','kind','collected_on','source_complete','coverage_scope')}
                       for row in batches],
            'candidates':candidates,
            'held':[c for row in reversed(current) for c in row.get('held',[])],
            'seed_profiles_configured':False,'candidate_total_verified':False}
