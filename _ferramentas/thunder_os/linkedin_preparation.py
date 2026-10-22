"""Read-only calendar and shared-budget preparation, never a send authority."""
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_policy(value):
    common={'timezone','weekly_invite_limit','holidays'}
    if not isinstance(value,Mapping) or set(value) not in (
            common|{'send_hour'},common|{'send_window','holiday_countries'}):
        raise ValueError('linkedin_policy_invalid')
    window=value.get('send_window')
    if 'send_window' in value:
        countries=value['holiday_countries']
        if (not isinstance(window,Mapping) or set(window)!={'start_hour','end_hour'}
                or type(window['start_hour']) is not int or type(window['end_hour']) is not int
                or not 0<=window['start_hour']<window['end_hour']<=24
                or not isinstance(countries,list) or not 1<=len(countries)<=2
                or any(not isinstance(country,str) or country not in ('BR','MX') for country in countries)
                or len(set(countries))!=len(countries)):
            raise ValueError('linkedin_policy_invalid')
    elif type(value['send_hour']) is not int or not 0<=value['send_hour']<=23:
        raise ValueError('linkedin_policy_invalid')
    limit=value['weekly_invite_limit']
    if (not isinstance(value['timezone'],str) or not 1<=len(value['timezone'])<=128
            or (limit is not None and (type(limit) is not int or not 1<=limit<=10000))
            or not isinstance(value['holidays'],list) or len(value['holidays'])>366):
        raise ValueError('linkedin_policy_invalid')
    try:
        if value['timezone']!='lead' or window is None:
            ZoneInfo(value['timezone'])
        holidays=[date.fromisoformat(item).isoformat() for item in value['holidays']]
    except (ValueError,TypeError,OSError,ZoneInfoNotFoundError):
        raise ValueError('linkedin_policy_invalid') from None
    if len(set(holidays))!=len(holidays):
        raise ValueError('linkedin_policy_invalid')
    normalized={**deepcopy(dict(value)),'holidays':sorted(holidays)}
    if window is not None:
        normalized['holiday_countries']=sorted(value['holiday_countries'])
    return normalized


def _zone(policy, lead_timezone):
    if policy['timezone']!='lead':
        return ZoneInfo(policy['timezone'])
    if lead_timezone is None:
        raise ValueError('linkedin_lead_timezone_required')
    if not isinstance(lead_timezone,str) or not 1<=len(lead_timezone)<=128:
        raise ValueError('linkedin_lead_timezone_invalid')
    try:
        return ZoneInfo(lead_timezone)
    except (ValueError,OSError,ZoneInfoNotFoundError):
        raise ValueError('linkedin_lead_timezone_invalid') from None


def _local_instants(day, hour, minute, zone):
    valid=[]
    for fold in (0,1):
        local=datetime.combine(day,time(hour,minute),zone).replace(fold=fold)
        instant=local.astimezone(timezone.utc)
        roundtrip=instant.astimezone(zone)
        if (roundtrip.date()==day and roundtrip.hour==hour
                and roundtrip.minute==minute and roundtrip.fold==fold):
            valid.append(instant)
    return valid


def next_slot(anchor, policy, lead_timezone=None):
    """Propose a slot using explicit exclusions, without certifying a calendar."""
    policy=validate_policy(policy)
    if not isinstance(anchor,datetime) or anchor.tzinfo is None or anchor.utcoffset() is None:
        raise ValueError('linkedin_anchor_invalid')
    zone=_zone(policy,lead_timezone)
    anchor_utc=anchor.astimezone(timezone.utc)
    local_anchor=anchor_utc.astimezone(zone)
    day=local_anchor.date()
    window=policy.get('send_window')
    for _ in range(740):
        if day.weekday()<5 and day.isoformat() not in policy['holidays']:
            if window is None:
                minutes=(policy['send_hour']*60,)
            else:
                if day==local_anchor.date() and window['start_hour']<=local_anchor.hour<window['end_hour']:
                    return anchor_utc
                minutes=range(window['start_hour']*60,window['end_hour']*60)
            # Scan wall minutes so a DST gap at opening can resume inside the window.
            for minute in minutes:
                valid=[instant for instant in _local_instants(day,*divmod(minute,60),zone)
                       if instant>=anchor_utc]
                if valid:
                    return min(valid)
        day+=timedelta(days=1)
    raise ValueError('linkedin_calendar_unavailable')


class LinkedInPreparation:
    def __init__(self, registry):
        self.registry=registry

    def prepare(self, campaign_id, now, quota=None, *, lead_timezone=None, lead_country=None):
        with self.registry.repo.transaction():
            context=self.registry.load(campaign_id)
            configuration=self.registry.configuration(campaign_id)
        result={'executor_enabled':False,'simulation':True,'execution_context':context,
                'reserved':False,'sent':False,'quota_live_verified':False}
        if context['platform']!='thunder_os':
            return {**result,'status':'external_platform'}
        policy=configuration.get('linkedin_policy')
        if policy is None:
            return {**result,'status':'policy_missing'}
        limit=policy['weekly_invite_limit']
        holds=[]
        if isinstance(quota,Mapping) and (quota.get('state')=='blocked'
                or quota.get('http_status') in (401,403,429)
                or quota.get('error_code') in ('provider_unauthorized','provider_forbidden','provider_rate_limited')
                or quota.get('provider_limit_reached') is True):
            holds.append('provider_blocked')
        result.update(timezone=policy['timezone'],weekly_invite_limit=limit,
                      internal_limit_applied=limit is not None,quota_required=limit is not None)
        due=None
        try:
            due=next_slot(now,policy,lead_timezone)
        except ValueError as exc:
            if str(exc) in ('linkedin_lead_timezone_required','linkedin_lead_timezone_invalid'):
                holds.append('lead_timezone_unverified')
            elif str(exc)=='linkedin_calendar_unavailable':
                holds.append('calendar_unavailable')
            else:
                raise
        if due is not None:
            zone=_zone(policy,lead_timezone)
            local=due.astimezone(zone)
            monday=(local.date()-timedelta(days=local.weekday())).isoformat()
            result.update(due_at=due.isoformat(),timezone=zone.key)
        if 'send_window' in policy:
            result.update(holiday_calendar_verified=False,
                          holiday_countries=list(policy['holiday_countries']))
            if not isinstance(lead_country,str) or not lead_country:
                holds.append('lead_country_unverified')
            elif lead_country not in policy['holiday_countries']:
                holds.append('holiday_country_unsupported')
            # Explicit dates do not prove complete national/regional calendar coverage.
            holds.append('calendar_unverified')
        account=configuration['provider_accounts'].get('linkedin')
        known=False
        if limit is not None and due is not None and isinstance(quota,Mapping):
            try:
                observed=datetime.fromisoformat(quota.get('observed_at',''))
                observed_local=observed.astimezone(zone) if observed.tzinfo is not None else None
                observed_week=(observed_local.date()-timedelta(days=observed_local.weekday())).isoformat() if observed_local else None
                known=(account is not None and context['providers'].get('linkedin')=='connectsafely'
                    and quota.get('account_id')==account and quota.get('scope')=='all_tools'
                    and quota.get('complete') is True and quota.get('week_start')==monday and observed_week==monday
                    and type(quota.get('used')) is int and quota['used']>=0
                    and observed.tzinfo is not None and 0<=(now-observed).total_seconds()<=300)
            except (TypeError,ValueError,OverflowError):
                known=False
        if limit is not None:
            if not known:
                holds.append('quota_unverified')
            else:
                result['observed_total_account_usage']=quota['used']
                if quota['used']>=limit:
                    holds.append('pending_quota')
        return {**result,'status':holds[0] if holds else 'ready_for_review','hold_reasons':holds}

    def snapshot(self):
        campaigns=[]
        for row in self.registry.list_campaigns():
            configuration=self.registry.configuration(row['id']) if row['active'] else {}
            campaigns.append({'campaign_id':row['id'],'platform':row['platform'],
                'policy_configured':'linkedin_policy' in configuration})
        return {'executor_enabled':False,'simulation':True,'quota_reader_configured':False,
                'campaigns':campaigns,'source':'campaign_registry','canary_sent':False}

    def execute(self, payload):
        raise ValueError('linkedin_executor_disabled')
