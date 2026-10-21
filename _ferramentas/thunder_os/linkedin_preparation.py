"""Read-only calendar and shared-budget preparation, never a send authority."""
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_policy(value):
    fields={'timezone','send_hour','weekly_invite_limit','holidays'}
    if not isinstance(value,Mapping) or set(value)!=fields:
        raise ValueError('linkedin_policy_invalid')
    if (not isinstance(value['timezone'],str) or not 1<=len(value['timezone'])<=128 or type(value['send_hour']) is not int
            or not 0<=value['send_hour']<=23 or type(value['weekly_invite_limit']) is not int
            or not 1<=value['weekly_invite_limit']<=10000
            or not isinstance(value['holidays'],list) or len(value['holidays'])>366):
        raise ValueError('linkedin_policy_invalid')
    try:
        ZoneInfo(value['timezone'])
        holidays=[date.fromisoformat(item).isoformat() for item in value['holidays']]
    except (ValueError,TypeError,OSError,ZoneInfoNotFoundError):
        raise ValueError('linkedin_policy_invalid') from None
    if len(set(holidays))!=len(holidays):
        raise ValueError('linkedin_policy_invalid')
    return {**deepcopy(dict(value)),'holidays':sorted(holidays)}


def next_slot(anchor, policy):
    policy=validate_policy(policy)
    if not isinstance(anchor,datetime) or anchor.tzinfo is None:
        raise ValueError('linkedin_anchor_invalid')
    zone=ZoneInfo(policy['timezone'])
    day=anchor.astimezone(zone).date()
    anchor_utc=anchor.astimezone(timezone.utc)
    for _ in range(740):
        if day.weekday()<5 and day.isoformat() not in policy['holidays']:
            valid=[]
            for fold in (0,1):
                local=datetime.combine(day,time(policy['send_hour']),zone).replace(fold=fold)
                instant=local.astimezone(timezone.utc)
                roundtrip=instant.astimezone(zone)
                if (roundtrip.date()==day and roundtrip.hour==policy['send_hour']
                        and roundtrip.minute==0 and roundtrip.fold==fold and instant>=anchor_utc):
                    valid.append(instant)
            if valid:
                return min(valid)
        day+=timedelta(days=1)
    raise ValueError('linkedin_calendar_unavailable')


class LinkedInPreparation:
    def __init__(self, registry):
        self.registry=registry

    def prepare(self, campaign_id, now, quota=None):
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
        due=next_slot(now,policy)
        zone=ZoneInfo(policy['timezone'])
        local=due.astimezone(zone)
        monday=(local.date()-timedelta(days=local.weekday())).isoformat()
        account=configuration['provider_accounts'].get('linkedin')
        known=False
        if isinstance(quota,Mapping):
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
        result.update(due_at=due.isoformat(),timezone=policy['timezone'],
                      weekly_invite_limit=policy['weekly_invite_limit'])
        if not known:
            return {**result,'status':'quota_unverified'}
        return {**result,'status':'pending_quota' if quota['used']>=policy['weekly_invite_limit']
                else 'ready_for_review','observed_total_account_usage':quota['used']}

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
