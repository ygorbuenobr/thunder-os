"""Hunter/Ygor evidence scoreboard. Read-only, bounded; no inferred commercial events."""
from collections import defaultdict
from copy import deepcopy
from contextlib import contextmanager
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .cadence_readonly import OWNER_ID, CadenceReadonlyError

PIPELINE = '903417452'
QUALIFIED = '1366314827'
WON = '1367676183'
DEAL_PROPS = ('pipeline', 'dealstage', 'hubspot_owner_id', 'thunder_campaign_id', 'amount', 'deal_currency_code', 'closedate')
MEETING_PROPS = ('hubspot_owner_id', 'hs_meeting_start_time', 'hs_meeting_end_time', 'hs_meeting_outcome', 'hs_createdate')


def stamp(value):
    try:
        if isinstance(value, str) and value.isdigit():
            return datetime.fromtimestamp(int(value)/1000, timezone.utc)
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def valid_history(row, prop):
    histories = row.get('propertiesWithHistory')
    if not isinstance(histories, dict):
        return None
    entries = histories.get(prop)
    if (not isinstance(entries, list) or not entries or any(
            not isinstance(item, dict) or stamp(item.get('timestamp')) is None
            or (item.get('value') is not None and not isinstance(item.get('value'), str))
            for item in entries)):
        return None
    return [(stamp(item['timestamp']), item.get('value')) for item in entries]


def at(row, prop, when):
    entries = valid_history(row, prop)
    if entries is None:
        return None
    eligible = [(t, value) for t, value in entries if t <= when]
    if not eligible:
        return None
    latest = max(t for t, _ in eligible)
    values = {value for t, value in eligible if t == latest}
    return next(iter(values)) if len(values) == 1 else None


def first_stage(row, stage, prop='dealstage'):
    entries = valid_history(row, prop)
    matches = [t for t, value in entries or [] if value == stage]
    return min(matches) if matches else None


def associated(row, kind):
    associations = row.get('associations')
    if not isinstance(associations, dict):
        return None
    data = associations.get(kind)
    if not isinstance(data, dict) or data.get('paging') or not isinstance(data.get('results'), list):
        return None
    ids = [item.get('id') for item in data['results'] if isinstance(item, dict)]
    return set(ids) if len(ids) == len(data['results']) and all(isinstance(i, str) and i.isdigit() for i in ids) else None


class HunterScoreboard:
    def __init__(self, adapter, repo, *, clock=None, max_pages=5):
        self.adapter, self.repo = adapter, repo
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_pages = max(1, min(max_pages, 10))
        self._lock = threading.RLock()
        self._tokens = {}
        self._contexts = []

    @contextmanager
    def _guard(self):
        if not self._lock.acquire(timeout=0.25):
            raise ValueError("hunter_busy; retry the same cursor")
        try:
            yield
        finally:
            self._lock.release()

    def snapshot(self, campaign_snapshot, replies):
        with self._guard():
            self._expire()
            if len(self._contexts) >= 4:
                completed = next((c for c in self._contexts if not c.get('pending',True)),None)
                if completed is not None:
                    self._contexts.remove(completed); self._expire()
            if len(self._contexts) >= 4:
                raise ValueError('hunter_snapshot_capacity; wait or continue an existing snapshot')
            context = {'now':self.clock(), 'campaigns':deepcopy(campaign_snapshot),
                'replies':deepcopy(replies), 'reads':{}, 'contact_rows':{},
                'id':secrets.token_urlsafe(18), 'touched':time.monotonic(), 'generation':0}
            self._contexts.append(context)
            try:
                return self._advance(context)
            except Exception:
                self._contexts.remove(context)
                self._expire()
                raise

    def _expire(self):
        self._contexts = [c for c in self._contexts if time.monotonic()-c['touched'] < 900]
        active = {c['id'] for c in self._contexts}
        self._tokens = {token:entry for token,entry in self._tokens.items() if entry['context']['id'] in active}

    def continue_snapshot(self, token):
        with self._guard():
            self._expire()
            entry = self._tokens.get(token) if isinstance(token,str) else None
            if entry is None:
                raise ValueError('hunter_cursor_expired_or_invalid')
            context = entry['context']; context['touched'] = time.monotonic()
            if entry.get('response') is not None:
                return deepcopy(entry['response'])  # Lost response/concurrent repeat: no provider retry.
            try:
                result = self._advance(context)
            except Exception:
                self._contexts.remove(context)
                self._expire()
                raise
            entry['response'] = deepcopy(result)
            # Retain current and previous acknowledgements only, bounding replay memory.
            tokens = [(key,value) for key,value in self._tokens.items() if value['context'] is context]
            for key,_ in tokens[:-3]:
                self._tokens.pop(key,None)
            return result

    def _advance(self, context):
        context['generation'] += 1
        result = self._collect_snapshot(context['campaigns'],context['replies'],context)
        resumable = {'page_limit','read_budget_exhausted','awaiting_hunter_deals'}
        pending = any(state.get('reason') in resumable for state in context['reads'].values())
        pending = pending or context.get('contacts_pending',False)
        pending = bool(pending and not result.get('error'))
        context['pending'] = pending
        token = secrets.token_urlsafe(24) if pending else None
        if token:
            self._tokens[token] = {'context':context,'response':None}
        context['touched'] = time.monotonic()
        result['progress'] = {'snapshot_id':context['id'], 'generation':context['generation'],
            'started_at':context['now'].isoformat(), 'observed_until':self.clock().isoformat(),
            'cursor':token, 'can_continue':bool(token),
            'enumeration_finished':bool(context['reads']) and all(s.get('complete') for s in context['reads'].values()) and not context.get('contacts_pending') and not result.get('error'),
            'expires_after_idle_seconds':900, 'chunk_budget_seconds':8,
            'consistency':'paginated_non_atomic; fixed period and frozen receipt/reply inputs',
            'sources':{kind:{'records':len(state.get('rows',[])), 'pages':state.get('pages',0),
                'complete':state.get('complete',False),'reason':state.get('reason')}
                for kind,state in context['reads'].items()}}
        return result

    def _list(self, session, kind, properties, associations, state=None):
        state = state if state is not None else {}
        result = state.setdefault('rows',[])
        seen = state.setdefault('seen',set())
        cursors = state.setdefault('cursors',set())
        after, declared_total = state.get('after'), state.get('total')
        for _ in range(self.max_pages):
            query = {'limit': 50, 'archived': 'false', 'properties': ','.join(properties),
                     'propertiesWithHistory': ','.join(properties), 'associations': ','.join(associations)}
            if after is not None:
                query['after'] = after
            if kind == 'deals':
                search = {'filterGroups':[{'filters':[
                    {'propertyName':'pipeline','operator':'EQ','value':PIPELINE},
                    {'propertyName':'hubspot_owner_id','operator':'EQ','value':OWNER_ID}]}],
                    'limit':50, 'properties':['pipeline','hubspot_owner_id']}
                if after is not None:
                    search['after'] = after
                listed = session.call('POST', '/crm/v3/objects/deals/search', search)
                total = listed.get('total')
                if type(total) is not int or total < 0 or (declared_total is not None and total != declared_total):
                    return result,False,'deal_search_total_unverified'
                declared_total = total
                state['total'] = total
                items = listed.get('results')
                if not isinstance(items,list) or len(items)>50 or any(
                    not isinstance(item,dict) or not isinstance(item.get('id'),str)
                    or not item['id'].isdigit() for item in items):
                    return result,False,'invalid_deal_search'
                ids = [item['id'] for item in items]
                if len(set(ids)) != len(ids):
                    return result,False,'duplicate_deal_search'
                if ids:
                    batch = session.call('POST','/crm/v3/objects/deals/batch/read',
                        {'properties':list(properties),'propertiesWithHistory':list(properties),
                         'inputs':[{'id':ident} for ident in ids]})
                    loaded = batch.get('results')
                    if (batch.get('errors') or not isinstance(loaded,list) or len(loaded)!=len(ids)
                        or any(not isinstance(item,dict) for item in loaded)
                        or {item.get('id') for item in loaded} != set(ids)):
                        return result,False,'incomplete_deal_history_batch'
                else:
                    loaded=[]
                response={'results':loaded, **({'paging':listed['paging']} if 'paging' in listed else {})}
            else:
                response = session.call('GET', '/crm/v3/objects/' + kind + '?' + urlencode(query))
            rows = response.get('results')
            if not isinstance(rows, list) or len(rows) > 50:
                return result, False, 'invalid_list'
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or not row['id'].isdigit() or row['id'] in seen or row.get('archived') is not False
                    or (row.get('properties') is not None and not isinstance(row['properties'],dict))):
                    return result, False, 'duplicate_or_invalid_object'
                seen.add(row['id']); result.append(row)
            state['pages'] = state.get('pages',0)+1
            next_page = (response.get('paging') or {}).get('next')
            if next_page is None:
                if kind == 'deals' and len(result) != declared_total:
                    return result,False,'incomplete_deal_search'
                return result, True, None
            after = next_page.get('after') if isinstance(next_page, dict) else None
            if not isinstance(after, (str, int)) or str(after) in cursors:
                return result, False, 'invalid_cursor'
            cursors.add(str(after))
            state['after'] = after
        return result, False, 'page_limit'

    def _meetings(self, session, state, deals):
        """Read only Ygor meetings currently associated with enumerated Hunter deals.

        Search association filters prove a current relation, never its history.
        Groups avoid an unbounded IN query. Cross-group duplicates count once.
        """
        rows = state.setdefault('rows',[])
        by_id = state.setdefault('by_id',{})
        groups = state.setdefault('groups',[list(deals)[i:i+50] for i in range(0,len(deals),50)])
        for _ in range(self.max_pages):
            index = state.get('group',0)
            if index >= len(groups):
                return rows,True,None
            group = groups[index]
            body = {'filterGroups':[{'filters':[
                {'propertyName':'hubspot_owner_id','operator':'EQ','value':OWNER_ID},
                {'propertyName':'associations.deal','operator':'IN','values':group}]}],
                'limit':50,'properties':list(MEETING_PROPS)}
            if state.get('after') is not None:
                body['after'] = state['after']
            page = session.call('POST','/crm/v3/objects/meetings/search',body)
            total, items = page.get('total'),page.get('results')
            if type(total) is not int or total < 0 or (state.get('total') is not None and state['total'] != total):
                return rows,False,'meeting_search_total_unverified'
            state['total'] = total
            if not isinstance(items,list) or len(items)>50 or any(not isinstance(r,dict)
                    or not isinstance(r.get('id'),str) or not r['id'].isdigit() for r in items):
                return rows,False,'invalid_meeting_search'
            ids = [r['id'] for r in items]
            seen = state.setdefault('group_seen',set())
            if len(ids)!=len(set(ids)) or set(ids)&seen:
                return rows,False,'duplicate_meeting_search'
            batch = session.call('POST','/crm/v3/objects/meetings/batch/read',
                {'inputs':[{'id':ident} for ident in ids], 'properties':list(MEETING_PROPS),
                 'propertiesWithHistory':list(MEETING_PROPS)}) if ids else {'results':[]}
            loaded = batch.get('results')
            if (batch.get('errors') or not isinstance(loaded,list) or len(loaded)!=len(ids)
                or any(not isinstance(r,dict) for r in loaded) or {r.get('id') for r in loaded} != set(ids)):
                return rows,False,'incomplete_meeting_history_batch'
            for row in loaded:
                props = row.get('properties')
                if row.get('archived') is not False or not isinstance(props,dict) or props.get('hubspot_owner_id') != OWNER_ID:
                    return rows,False,'meeting_owner_unverified'
                if row['id'] in by_id and by_id[row['id']] != row:
                    return rows,False,'meeting_changed_during_read'
            for row in loaded:
                if row['id'] not in by_id:
                    by_id[row['id']] = deepcopy(row)
                    rows.append({**deepcopy(row),'_hunter_query_scope':True})
            seen.update(ids); state['pages'] = state.get('pages',0)+1
            next_page = (page.get('paging') or {}).get('next')
            if next_page is None:
                if len(seen) != total:
                    return rows,False,'incomplete_meeting_search'
                state.update(group=index+1, after=None, total=None, group_seen=set(), cursors=set())
                if index+1 >= len(groups):
                    return rows,True,None
            else:
                cursor = next_page.get('after') if isinstance(next_page,dict) else None
                cursors = state.setdefault('cursors',set())
                if not isinstance(cursor,(str,int)) or str(cursor) in cursors:
                    return rows,False,'invalid_meeting_cursor'
                cursors.add(str(cursor)); state['after'] = cursor
        return rows,False,'page_limit'

    def _collect_snapshot(self, campaign_snapshot, replies, context):
        now = context['now']; end = now; start = now - timedelta(days=30)
        period = {'start': start.isoformat(), 'end': end.isoformat(), 'timezone': 'UTC', 'days': 30}
        output = {'owner': {'id': OWNER_ID, 'name': 'Ygor'}, 'pipeline': {'id': PIPELINE, 'name': 'Hunter'},
            'baseline': 'hunter-approved-2026-10-25',
            'definitions': {'opportunity_stage':QUALIFIED, 'won_stage':WON,
                'event_basis':'first recorded history entry; never current stage alone',
                'positive':'human-verified interest in recurring external partnership',
                'meeting_scope':'current Hunter associations; historical attribution unverified'},
            'period': period, 'collected_at': now.isoformat(), 'read_only': True, 'external_writes': 0,
            'costs': {'value': None, 'complete': False, 'reason': 'expense_sources_and_hourly_cost_missing'},
            'limitations': ['active_nonarchived_records_only', 'no_global_inbox_coverage',
                'current_owner_portfolio_and_event_owner_verified', 'stage_history_is_recorded_history_not_creation_proof'],
            'sources': {}, 'metrics': {}, 'campaigns': [], 'cohorts': [], 'unattributed': {}}
        session = self.adapter.session()
        if hasattr(session,'deadline'):
            session.deadline = min(session.deadline, session.adapter.monotonic()+8)
        try:
            session.identity()
            pipelines = session.call('GET', '/crm/v3/pipelines/deals').get('results', [])
            target = [p for p in pipelines if p.get('id') == PIPELINE]
            if len(target) != 1 or not {QUALIFIED, WON} <= {s.get('id') for s in target[0].get('stages', [])}:
                raise CadenceReadonlyError('hunter_pipeline_contract_changed')
        except CadenceReadonlyError as exc:
            output['error'] = exc.code; output['complete'] = False
            return output
        data = {}
        for kind, props, assoc in [('deals', DEAL_PROPS, ('contacts',)), ('meetings', MEETING_PROPS, ('contacts','deals'))]:
            state = context['reads'].setdefault(kind,{})
            http_status = state.get('http_status')
            if state.get('complete') or (state.get('reason') and state['reason'] not in {'page_limit','read_budget_exhausted','awaiting_hunter_deals'}):
                rows,complete,reason = state.get('rows',[]),state.get('complete',False),state.get('reason')
            else:
                try:
                    if kind == 'meetings':
                        if not context['reads'].get('deals',{}).get('complete'):
                            rows,complete,reason = state.get('rows',[]),False,('awaiting_hunter_deals'
                                if context['reads'].get('deals',{}).get('reason') in {'page_limit','read_budget_exhausted'}
                                else 'hunter_deal_scope_unavailable')
                        else:
                            rows,complete,reason = self._meetings(session,state,{r['id']:r for r in data['deals'] if (r.get('properties') or {}).get('hubspot_owner_id') == OWNER_ID and (r.get('properties') or {}).get('pipeline') == PIPELINE})
                    else:
                        rows, complete, reason = self._list(session, kind, props, assoc, state)
                except CadenceReadonlyError as exc:
                    rows, complete, reason = state.get('rows',[]), False, exc.code
                    http_status = exc.status
                state.update(complete=complete,reason=reason,http_status=http_status)
                state['rows'] = rows
            data[kind] = rows
            output['sources'][kind] = {'complete': complete, 'reason': reason, 'records_read': len(rows),
                'http_status': http_status, 'source': 'HubSpot CRM v3 ' + kind, 'period': period, 'population': 'nonarchived records; Ygor/Hunter filter',
                'max_pages': self.max_pages}
        known = {r['id'] for r in campaign_snapshot.get('campaigns', []) if isinstance(r.get('id'), str) and r['id'].startswith('BR-')}
        # No numeric campaign attribution is derived from names or current contact campaign.
        events, exclusions = [], defaultdict(int)
        deals = {r['id']: r for r in data['deals']}
        for deal in deals.values():
            props = deal.get('properties') or {}
            if not {'hubspot_owner_id', 'pipeline'} <= props.keys():
                exclusions['deal_scope_unverified'] += 1; continue
            if props.get('hubspot_owner_id') != OWNER_ID or props.get('pipeline') != PIPELINE:
                continue
            for key, stage in [('opportunities', QUALIFIED), ('won', WON)]:
                when = first_stage(deal, stage)
                if when is None:
                    if valid_history(deal, 'dealstage') is None:
                        exclusions[key + '_history_unavailable'] += 1
                    continue
                if not start <= when < end:
                    continue
                if (at(deal, 'pipeline', when) != PIPELINE or at(deal, 'hubspot_owner_id', when) != OWNER_ID
                    or at(deal, 'dealstage', when) != stage):
                    exclusions[key + '_historical_scope_unverified'] += 1
                    continue
                campaign = at(deal, 'thunder_campaign_id', when)
                campaign = campaign if campaign in known else None
                events.append({'metric': key, 'id': 'deal:' + deal['id'] + ':' + key,
                    'at': when.isoformat(), 'campaign_id': campaign, 'source': 'HubSpot deal stage history',
                    'contact_ids': sorted(associated(deal, 'contacts') or [])})
        meeting_ids = set()
        for meeting in data['meetings']:
            props = meeting.get('properties') or {}
            if 'hubspot_owner_id' not in props:
                exclusions['meetings_owner_unknown'] += 1; continue
            if props.get('hubspot_owner_id') != OWNER_ID:
                continue
            when = stamp(props.get('hs_meeting_start_time'))
            scheduled_at = first_stage(meeting, 'SCHEDULED', 'hs_meeting_outcome')
            if scheduled_at is not None and at(meeting, 'hs_meeting_outcome', scheduled_at) != 'SCHEDULED':
                scheduled_at = None
            if when is None:
                exclusions['meetings_time_unknown'] += 1; continue
            in_period = start <= when < end
            scheduled_in_period = scheduled_at is not None and start <= scheduled_at < end
            if scheduled_at is None:
                exclusions['meetings_scheduled_history_unknown'] += 1
            if not in_period and not scheduled_in_period:
                continue
            scope_time = scheduled_at if scheduled_at is not None else min(when, now)
            related = associated(meeting, 'deals')
            hunter = [deals[i] for i in related or [] if i in deals
                      and (deals[i].get('properties') or {}).get('pipeline') == PIPELINE
                      and (deals[i].get('properties') or {}).get('hubspot_owner_id') == OWNER_ID
                      and at(deals[i], 'pipeline', scope_time) == PIPELINE
                      and at(deals[i], 'hubspot_owner_id', scope_time) == OWNER_ID]
            if not meeting.get('_hunter_query_scope') and (related is None or not hunter or len(hunter) != len(related)):
                exclusions['meetings_hunter_scope_unverified'] += 1; continue
            if at(meeting, 'hubspot_owner_id', scope_time) != OWNER_ID:
                exclusions['meetings_owner_history_unverified'] += 1; continue
            outcome = props.get('hs_meeting_outcome')
            if outcome not in {'SCHEDULED','COMPLETED','NO_SHOW','CANCELED','RESCHEDULED'}:
                exclusions['meetings_outcome_unknown'] += 1; continue
            ident = 'meeting:' + meeting['id']
            if ident in meeting_ids:
                continue
            meeting_ids.add(ident)
            # CRM associations have no historical effective timestamp here.
            # Current relation may scope an observation, never backdate attribution.
            campaign = None
            exclusions['meetings_relationship_history_unverified'] += 1
            base = {'id': ident, 'at': when.isoformat(), 'campaign_id': campaign,
                    'source': 'HubSpot meeting event; current Hunter association only',
                    'attribution_reason': 'historical_relationship_unverified', 'contact_ids': sorted(associated(meeting, 'contacts') or [])}
            if scheduled_in_period and outcome in {'SCHEDULED','COMPLETED','NO_SHOW'}:
                events.append({**base, 'metric':'meetings_scheduled', 'at':scheduled_at.isoformat()})
            if in_period and outcome in {'SCHEDULED','COMPLETED','NO_SHOW'}:
                events.append({**base, 'metric':'meetings_scheduled_due'})
            if in_period and outcome == 'COMPLETED':
                events.append({**base, 'metric':'meetings_held'})
            if outcome in {'CANCELED','RESCHEDULED','NO_SHOW'}:
                exclusions['meeting_' + outcome.lower()] += 1
        # Existing receipt source remains bounded: never claim global first touch.
        receipts = {}
        for campaign in campaign_snapshot.get('campaigns', []):
            if campaign.get('id') not in known:
                continue
            for task in campaign.get('tasks', []):
                receipt = task.get('receipt') or {}
                when = stamp(receipt.get('confirmed_at'))
                contact = receipt.get('contact_id')
                source = (receipt.get('provider'), receipt.get('account_id'), receipt.get('message_id'))
                if (task.get('outcome') != 'sent_with_receipt' or task.get('link_status') != 'linked'
                    or receipt.get('verification') != 'recorded_confirmation' or task.get('contact_ids') != [contact]
                    or not isinstance(contact,str) or not contact.isdigit() or not all(isinstance(v,str) and v for v in source)
                    or when is None or receipt.get('campaign_id',campaign['id']) != campaign['id']):
                    continue
                value = (contact, campaign['id'], when)
                if source in receipts and receipts[source] != value:
                    receipts[source] = None
                else:
                    receipts.setdefault(source, value)
        contacts, attempted_contacts = {}, set()
        cached_contacts = context['contact_rows']
        context['contacts_pending'] = False
        for item in receipts.values():
            if item is None or item[0] in attempted_contacts:
                continue
            if item[0] not in cached_contacts:
                if len(attempted_contacts) >= 50:
                    exclusions['contact_owner_read_limit'] += 1
                    context['contacts_pending'] = True
                    continue
                attempted_contacts.add(item[0])
                try:
                    row = session.call('GET','/crm/v3/objects/contacts/' + item[0] + '?properties=hubspot_owner_id&associations=deals')
                    cached_contacts[item[0]] = row if row.get('id') == item[0] and row.get('archived') is False else None
                except CadenceReadonlyError as exc:
                    if exc.code == 'read_budget_exhausted':
                        context['contacts_pending'] = True
                    else:
                        cached_contacts[item[0]] = None
                    exclusions['contact_owner_unavailable'] += 1
                    continue
            row = cached_contacts.get(item[0])
            if row is None:
                exclusions['contact_owner_unavailable'] += 1
                continue
            related = associated(row, 'deals')
            scoped = bool(related) and all(ident in deals
                and (deals[ident].get('properties') or {}).get('pipeline') == PIPELINE
                and (deals[ident].get('properties') or {}).get('hubspot_owner_id') == OWNER_ID
                for ident in related or [])
            contacts[item[0]] = (row.get('properties') or {}).get('hubspot_owner_id') == OWNER_ID and scoped
            if not scoped:
                exclusions['contact_hunter_scope_unverified'] += 1
        first = {}
        for source, item in receipts.items():
            if item is None or not contacts.get(item[0]):
                continue
            person, campaign, when = item
            if person not in first or when < first[person][1]:
                first[person] = (campaign, when)
            elif when == first[person][1] and campaign != first[person][0]:
                first[person] = (None, when)
                exclusions['first_touch_campaign_conflict'] += 1
        cohort_people = {p: v for p,v in first.items() if start <= v[1] < end}
        responded, positive, reply_sources = set(), set(), {}
        reply_groups = defaultdict(list)
        for item in replies:
            source = item.get('source') or {}
            key = tuple(source.get(k) for k in ('provider','account_id','provider_message_id'))
            if all(isinstance(v,str) and v for v in key):
                reply_groups[key].append(item)
            else:
                exclusions['reply_source_unverified'] += 1
        clean_replies = []
        for group in reply_groups.values():
            signatures = {repr((r.get('hubspot_contact_id'), r.get('campaign_id'), r.get('received_at'),
                r.get('human_reply'), r.get('identity_confirmed'), r.get('classification'),
                sorted((r.get('qualification') or {}).items()))) for r in group}
            if len(signatures) != 1:
                exclusions['reply_identity_or_classification_conflict'] += 1
            else:
                clean_replies.append(group[0])
        for reply in clean_replies:
            person = reply.get('hubspot_contact_id'); when = stamp(reply.get('received_at'))
            source = reply.get('source') or {}
            key = tuple(source.get(k) for k in ('provider','account_id','provider_message_id'))
            if not all(isinstance(v,str) and v for v in key):
                exclusions['reply_source_unverified'] += 1; continue
            if (reply.get('identity_confirmed') is not True or reply.get('human_reply') is not True
                or person not in cohort_people or when is None or not cohort_people[person][1] <= when < end
                or reply.get('campaign_id') != cohort_people[person][0]):
                continue
            if key in reply_sources and reply_sources[key] != person:
                exclusions['reply_identity_conflict'] += 1; continue
            reply_sources[key] = person; responded.add(person)
            qualification = reply.get('qualification') or {}
            if (reply.get('classification') == 'positive' and qualification.get('external_recurring_partnership') is True
                and qualification.get('verified_by') == 'ygor' and qualification.get('source_ref')):
                positive.add(person)
            elif reply.get('classification') == 'positive':
                exclusions['positive_partnership_evidence_pending'] += 1
        def metric(key, source_kind):
            selected = [e for e in events if e['metric'] == key]
            complete = (output['sources'][source_kind]['complete']
                        and not (source_kind == 'deals' and exclusions.get('deal_scope_unverified'))) and not any(
                k.startswith(key.split('_')[0]) and ('unverified' in k or 'unavailable' in k or 'unknown' in k) for k in exclusions)
            return {'value':len(selected) if complete or selected else None, 'observed_value':len(selected),
                    'complete':complete, 'source':output['sources'][source_kind]['source'],
                    'source_ids':[e['id'] for e in selected], 'unit':'events' if key.startswith('meetings') else 'deals'}
        output['metrics'] = {k:metric(k, 'meetings' if k.startswith('meetings') else 'deals') for k in
                             ('opportunities','won','meetings_scheduled','meetings_scheduled_due','meetings_held')}
        for key, people in [('reached',set(cohort_people)),('responded',responded),('positive',positive)]:
            output['metrics'][key] = {'value':len(people) if people else None,'observed_value':len(people),
                'complete':False,'source':'recorded receipts and classified local replies', 'unit':'people',
                'reason':'bounded_evidence_not_global_cohort', 'population':'Ygor; current Hunter-associated contacts; first confirmed touch in available evidence'}
        output['rates'] = {'human_response':{'numerator':len(responded),'denominator':len(cohort_people)},
                           'qualified_positive':{'numerator':len(positive),'denominator':len(cohort_people)},
                           'positive_among_responses':{'numerator':len(positive),'denominator':len(responded)},
                           'meeting_attendance':{'numerator':output['metrics']['meetings_held']['observed_value'],
                                                'denominator':output['metrics']['meetings_scheduled_due']['observed_value']}}
        for rate in output['rates'].values():
            rate.update(value=rate['numerator']/rate['denominator'] if rate['denominator'] else None,
                        complete=False, scope='observed_sample_only')
        if exclusions.get('positive_partnership_evidence_pending'):
            for key in ('qualified_positive', 'positive_among_responses'):
                output['rates'][key]['value'] = None
                output['rates'][key]['reason'] = 'qualification_review_pending'
        grouped = defaultdict(list)
        for person, (campaign, when) in cohort_people.items():
            grouped[when.strftime('%Y-%m')].append(person)
        output['cohorts'] = [{'month':month,'people':len(people),'responded':len(set(people)&responded),
            'qualified_positive':len(set(people)&positive),'maturation_days':30,'provisional':True,
            'first_touch_scope':'available_confirmed_receipts_only'} for month,people in sorted(grouped.items())]
        for campaign in sorted(known):
            output['campaigns'].append({'id':campaign,'events':{key:sum(e['metric']==key and e['campaign_id']==campaign for e in events)
                for key in output['metrics'] if key in ('opportunities','won','meetings_scheduled','meetings_scheduled_due','meetings_held')},
                'reached_observed':sum(c==campaign for c,t in cohort_people.values())})
        output['unattributed'] = {key:sum(e['metric']==key and e['campaign_id'] is None for e in events)
                                 for key in ('opportunities','won','meetings_scheduled','meetings_scheduled_due','meetings_held')}
        output['unattributed']['reached_observed'] = sum(c is None for c,t in cohort_people.values())
        for campaign in output['campaigns']:
            campaign['events'] = {key: {'observed_value':count,
                'value':count if count or output['metrics'][key]['complete'] else None,
                'complete':output['metrics'][key]['complete']} for key,count in campaign['events'].items()}
        output['unattributed'] = {key:{'observed_value':count,
            'value':count if count or output['metrics'].get(key,{}).get('complete') else None,
            'complete':output['metrics'].get(key,{}).get('complete',False)} for key,count in output['unattributed'].items()}
        output['events'] = events; output['exclusions'] = dict(exclusions)
        output['complete'] = False
        return output
