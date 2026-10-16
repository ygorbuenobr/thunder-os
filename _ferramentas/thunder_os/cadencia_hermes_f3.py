"""F3 offline rehearsal: immutable synthetic snapshot -> proposed effects only.

No provider, credential, scheduler or operational-ledger imports. This is deliberately
not registered with the OS worker. A fixture receipt is never a real delivery receipt.
HubSpot remains the future authority; ledger snapshots stand for canonical toques.py.
"""
from __future__ import annotations

import argparse
import json
import random
import re
from urllib.parse import unquote, urlsplit
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SP = ZoneInfo('America/Sao_Paulo')
MODO_SIMULACAO = True
TITLES = {'LinkedIn: convite': 0, **{f'LinkedIn: msg {i}': i for i in (1, 2, 3)}}


def canonical_profile(value):
    """Strict offline identity key, never a URL to fetch.

    Only public /in/ slugs on the two explicit LinkedIn hosts are accepted.
    Ambiguous/non-ASCII slugs fail closed instead of guessing identity aliases.
    """
    if not isinstance(value, str) or not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError('invalid_profile')
    parsed = urlsplit(value)
    if (parsed.scheme.lower() not in ('http', 'https')
            or parsed.hostname not in ('linkedin.com', 'www.linkedin.com')
            or parsed.username is not None or parsed.password is not None
            or parsed.port is not None):
        raise ValueError('invalid_profile')
    path = unquote(parsed.path, errors='strict')
    match = re.fullmatch(r'/in/([A-Za-z0-9-]+)/?', path)
    if not match:
        raise ValueError('invalid_profile')
    return 'https://www.linkedin.com/in/' + match.group(1).lower()


def identity_groups(contacts):
    """Build person-level stops before any job, including out-of-scope evidence.

    Contradictory observations are blocked, never resolved by input order. Optional
    email/member IDs detect conflicts only; they never merge distinct profiles.
    """
    identities, groups, stopped = {}, {}, set()
    reasons = {}
    for cid, contact in contacts.items():
        try:
            profile = canonical_profile(contact.get('linkedin_url'))
        except (ValueError, UnicodeError):
            stopped.add(cid)
            reasons[cid] = 'invalid_profile_identity'
            continue
        identities[cid] = profile
        groups.setdefault(profile, []).append(cid)
    # If a secondary identity points at multiple profiles, block every candidate.
    secondary = {}
    for cid, contact in contacts.items():
        for field in ('email', 'linkedin_member_id'):
            value = contact.get(field)
            if isinstance(value, str) and value.strip():
                key = (field, value.strip().lower() if field == 'email' else value.strip())
                secondary.setdefault(key, []).append(cid)
    conflicting = set()
    for ids in secondary.values():
        profiles = {identities.get(cid) for cid in ids}
        if len(profiles) > 1:
            conflicting.update(ids)
    evidence_fields = (
        'owner_id', 'sequence_id', 'test_id', 'status', 'start_date', 'accepted_at',
        'history_complete', 'relation_known', 'email_dates', 'linkedin_dates',
        'version', 'texts', 'approved_texts', 'ledger', 'confirmed_messages',
    )
    for profile, ids in groups.items():
        records = [contacts[cid] for cid in ids]
        conflict = any(cid in conflicting for cid in ids)
        for field in ('email', 'linkedin_member_id'):
            values = [c[field] for c in records if c.get(field) is not None]
            if any(not isinstance(v, str) or not v.strip() for v in values):
                conflict = True
            else:
                normalized = {v.strip().lower() if field == 'email' else v.strip() for v in values}
                conflict |= len(normalized) > 1
        conflict |= any(any(c.get(field) != records[0].get(field) for c in records[1:])
                        for field in evidence_fields)
        unsafe = any(c.get('reply') is not None or 'reply' not in c
                     or c.get('suppressed') is not False for c in records)
        if conflict or unsafe:
            stopped.update(ids)
            reason = 'conflicting_identity_evidence' if conflict else 'canonical_person_stopped'
            reasons.update({cid: reason for cid in ids})
    return identities, groups, stopped, reasons


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed.astimezone(SP)


def business_add(day, count):
    while count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count -= 1
    return day


def message_dates(contact):
    """Five business days between messages; skip known email dates.

    Returns earliest eligibility dates, not send appointments. The executor also
    guards fixed D0/D3/D8/D13 dates. Includes planned AND confirmed email dates
    in the snapshot. Confirmed LI dates
    anchor following messages so a delayed send never compresses the interval.
    """
    start = date.fromisoformat(contact['start_date'])
    accepted = timestamp(contact['accepted_at']).date()
    first = business_add(start, 5)
    if accepted > first:
        first = business_add(accepted, 1)
    emails = {date.fromisoformat(d) for d in contact['email_dates']}
    confirmed = contact['confirmed_messages']
    result = []
    for step in range(1, 4):
        day = first if step == 1 else business_add(date.fromisoformat(result[-1]), 5)
        while day.weekday() >= 5 or day in emails:
            day = business_add(day, 1)
        if str(step) in confirmed:
            day = date.fromisoformat(confirmed[str(step)])
        result.append(day.isoformat())
    return result


def simulate(snapshot, now, *, mode=MODO_SIMULACAO, seed=0):
    """Return proposals, never mutate the input or execute an external action.

    Required snapshot schema is exercised by test_thunder_os_cadencia_hermes_f3.fixture.
    All completeness booleans represent fixture assumptions, not live validation.
    """
    if mode is not True:
        raise ValueError('real_mode_not_implemented_or_authorized')
    if now.tzinfo is None:
        raise ValueError('timezone_required')
    now = now.astimezone(SP)
    today = now.date()
    report = {'mode': 'MODO_SIMULACAO', 'external_effects': 0,
              'intentions': [], 'decisions': [], 'alerts': []}

    def emit(kind, contact_id, **values):
        report['intentions'].append({'kind': kind, 'contact_id': contact_id, **values})

    def block(target, reason):
        report['decisions'].append({'target': target, 'decision': 'blocked', 'reason': reason})

    if snapshot.get('synthetic') is not True or snapshot.get('complete') is not True:
        block('snapshot', 'synthetic_complete_snapshot_required')
        return report
    if today.weekday() >= 5 or not 8 <= now.hour < 19:
        block('jobs', 'outside_business_window_08_19_SP')
        return report
    scope = ('owner_id', 'sequence_id', 'test_id')
    if any(not snapshot.get(k) for k in scope):
        block('snapshot', 'scope_required')
        return report
    if not isinstance(snapshot.get('contacts'), list) or not isinstance(snapshot.get('tasks'), list):
        block('snapshot', 'contacts_and_tasks_required')
        return report
    contacts = {}
    for contact in snapshot['contacts']:
        cid = contact.get('id')
        if not cid or cid in contacts:
            block('snapshot', 'duplicate_or_missing_contact_id')
            return report
        contacts[cid] = contact
    eligible = {cid: c for cid, c in contacts.items()
                if all(c.get(k) == snapshot[k] for k in scope)}
    identities, groups, stopped, identity_reasons = identity_groups(contacts)
    for cid, reason in identity_reasons.items():
        block(cid, reason)
    versions = {}
    # Job 3 runs first: any reply wins over overdue jobs in this snapshot.
    for cid, c in eligible.items():
        if 'reply' not in c:
            stopped.add(cid)
            block(cid, 'reply_observation_required')
            continue
        reply = c.get('reply')
        if reply:
            stopped.add(cid)
            if reply.get('channel') not in ('email', 'linkedin') or not reply.get('id') or not reply.get('text'):
                block(cid, 'unverified_reply')
                continue
            emit('would_block_linkedin', cid, reason='reply')
            emit('would_update_contact', cid, properties={'cad_status': 'respondeu'})
            emit('would_cancel_cadence_tasks', cid, sequence_id=snapshot['sequence_id'], test_id=snapshot['test_id'])
            if reply.get('kind') in ('opt_out', 'refusal'):
                emit('would_suppress', cid, channels=['email', 'linkedin', 'whatsapp'], reason=reply['kind'])
            emit('would_run_skill', cid, skill='acompanhar-respostas', reply_id=reply['id'])
            emit('would_create_urgent_task', cid, owner_id=snapshot['owner_id'], priority='HIGH',
                 due_date=today.isoformat(), reply_text=reply['text'],
                 suggested_reply=None, suggestion_status='pending_acompanhar_respostas',
                 sequence_stop='manual_required_unverified_api',
                 dedupe_key=f"{snapshot['test_id']}:{cid}:reply:{reply['id']}")
        elif c.get('suppressed') is not False:
            stopped.add(cid)
            block(cid, 'suppressed_or_unknown')
    # Job 2: catch up on/after D10; do not repeat an already-recorded decision.
    for cid, c in eligible.items():
        if cid in stopped:
            continue
        required = ('history_complete', 'relation_known')
        if any(c.get(k) is not True for k in required) or c.get('status') != 'em_cadencia':
            stopped.add(cid)
            block(cid, 'unknown_history_relation_or_inactive_status')
            continue
        try:
            start = date.fromisoformat(c['start_date'])
            if start.weekday() >= 5 or start > today:
                raise ValueError('invalid_start')
            if c.get('accepted_at') and timestamp(c['accepted_at']) > now:
                raise ValueError('future_acceptance')
            # Explicit empty collections are different from missing observations.
            for key in ('email_dates', 'linkedin_dates'):
                if not isinstance(c[key], list):
                    raise ValueError('invalid_history')
                for day in c[key]:
                    date.fromisoformat(day)
            if not isinstance(c['ledger'], dict) or not isinstance(c['confirmed_messages'], dict):
                raise ValueError('invalid_ledger')
        except (ValueError, KeyError, TypeError):
            stopped.add(cid)
            block(cid, 'invalid_or_missing_contact_evidence')
            continue
        version = c.get('version')
        if version not in (None, 'A', 'B'):
            stopped.add(cid)
            block(cid, 'invalid_version')
            continue
        if today >= business_add(start, 10) and version is None:
            version = 'A' if c.get('accepted_at') else 'B'
            properties = {'cad_email_4_versao': version}
            if version == 'B':
                text = c.get('texts', {}).get('cad_email_4b')
                if not text or c.get('approved_texts', {}).get('cad_email_4b') != text:
                    stopped.add(cid)
                    block(cid, 'email_4b_not_approved')
                    continue
                properties['cad_email_4'] = text
                emit('would_cancel_step', cid, step=3)
                emit('would_replace_step', cid, step=2, action='like_recent_post')
                report['alerts'].append({'contact_id': cid, 'reason': 'D13_token_resolution_requires_real_canary'})
            emit('would_update_contact', cid, properties=properties)
        versions[cid] = version
    for ids in groups.values():
        if any(cid in stopped for cid in ids):
            stopped.update(ids)
    # Job 1. Quota is global to the provider account, not per contact or task.
    quota = snapshot.get('quota', {})
    daily, weekly = quota.get('daily_remaining'), quota.get('weekly_invites_remaining')
    if type(daily) is not int or type(weekly) is not int or daily < 0 or not 0 <= weekly <= 184:
        block('executor', 'quota_unknown_or_invalid')
        return report
    rng = random.Random(seed)
    planned_people = set()
    seen_tasks = set()
    try:
        tasks = sorted(snapshot['tasks'], key=lambda t: (timestamp(t['due_at']), t['id']))
    except (ValueError, KeyError, TypeError):
        block('executor', 'invalid_task_snapshot')
        return report
    for task in tasks:
        tid, cid = task['id'], task.get('contact_id')
        if tid in seen_tasks:
            block(tid, 'duplicate_task')
            continue
        seen_tasks.add(tid)
        c = eligible.get(cid)
        if not c or not all(task.get(k) == snapshot[k] for k in scope):
            block(tid, 'outside_scope')
            continue
        if task.get('status') != 'open' or task.get('title') not in TITLES or timestamp(task['due_at']) > now:
            block(tid, 'not_due_open_linkedin_task')
            continue
        if cid in stopped:
            block(tid, 'contact_stopped')
            continue
        profile = identities[cid]
        planned_email_days = {business_add(date.fromisoformat(c['start_date']), offset)
                              for offset in (0, 3, 8, 13)}
        if (profile in planned_people or today in planned_email_days
                or today.isoformat() in c['email_dates'] + c['linkedin_dates']):
            block(tid, 'same_day_conflict')
            continue
        step = TITLES[task['title']]
        if (step and str(step) in c['confirmed_messages']):
            block(tid, 'step_already_confirmed')
            continue
        if not step and today < business_add(date.fromisoformat(c['start_date']), 1):
            block(tid, 'before_D1')
            continue
        action = 'invite' if step == 0 else 'message'
        text = c.get('texts', {}).get(f'cad_li_{step}')
        target = f'msg{step}' if step else 'invite'
        version = versions.get(cid)
        if step and version == 'B':
            if step != 2:
                block(tid, 'version_B_message_cancelled')
                continue
            # Late acceptance after B needs a business decision, never silently resume.
            if c.get('accepted_at'):
                block(tid, 'late_acceptance_after_B_requires_review')
                continue
            posts = []
            for post in c.get('posts', []):
                try:
                    published = timestamp(post['published_at'])
                    if timedelta(0) <= now - published <= timedelta(days=60) and str(post['url']).startswith('https://www.linkedin.com/'):
                        posts.append((published, post['url']))
                except (KeyError, TypeError, ValueError):
                    continue
            if not posts:
                block(tid, 'no_recent_dated_post')
                continue
            action, text, target = 'like', None, max(posts)[1]
        elif step:
            if not c.get('accepted_at'):
                block(tid, 'acceptance_required')
                continue
            try:
                due = message_dates(c)[step - 1]
            except (KeyError, ValueError, TypeError):
                block(tid, 'invalid_message_schedule')
                continue
            if today.isoformat() < due:
                block(tid, 'waiting_message_interval')
                continue
            if step > 1 and str(step - 1) not in c['confirmed_messages']:
                block(tid, 'predecessor_confirmation_required')
                continue
        elif c.get('accepted_at'):
            block(tid, 'already_connected')
            continue
        if action != 'like' and (not text or text != c.get('approved_texts', {}).get(f'cad_li_{step}')):
            block(tid, 'exact_text_approval_required')
            continue
        if action == 'invite' and len(text) > 200:
            block(tid, 'invite_over_200_chars')
            continue
        ledger_state = c['ledger'].get(target, 'clear')
        if ledger_state != 'clear':
            block(tid, f'ledger_{ledger_state}_no_retry')
            continue
        if daily == 0 or (action == 'invite' and weekly == 0):
            block(tid, 'quota_exhausted')
            continue
        daily -= 1
        weekly -= int(action == 'invite')
        planned_people.add(profile)
        effect_key = f"{snapshot['test_id']}:{profile}:{action}:{target}"
        emit('would_reserve_touch', cid, task_id=tid, effect_key=effect_key,
             ledger='canonical_toques.py', prerequisite='atomic_reserve_before_provider')
        emit('would_send', cid, task_id=tid, action=action, text=text, target=target,
             effect_key=effect_key, delay_seconds=rng.randint(30, 90))
        outcome = snapshot.get('provider_outcomes', {}).get(tid)
        if outcome is None:
            continue  # Dry-run is not a receipt; task must remain open.
        status = outcome.get('status')
        receipt = outcome.get('receipt_id')
        if status not in ('failed', 'uncertain', 'confirmed') or (status == 'confirmed' and not receipt):
            status = 'uncertain'
        emit('would_record_receipt', cid, task_id=tid, effect_key=effect_key,
             synthetic=True, fixture_status=status, fixture_receipt_id=receipt)
        if status == 'confirmed':
            emit('would_complete_task', cid, task_id=tid, synthetic=True,
                 note={'text': text, 'at': now.isoformat(), 'provider_id': receipt})
        else:
            report['alerts'].append({'task_id': tid, 'reason': f'fixture_provider_{status}', 'task_remains_open': True})
            if status == 'uncertain':
                emit('would_mark_uncertain', cid, properties={'cad_status': 'incerto'}, retry=False)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True, help='Synthetic JSON snapshot only')
    parser.add_argument('--now', required=True, help='ISO8601 with timezone')
    args = parser.parse_args(argv)
    with open(args.fixture, encoding='utf-8') as handle:
        snapshot = json.load(handle)
    print(json.dumps(simulate(snapshot, timestamp(args.now)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
