"""One bounded Apollo task scan for an explicitly approved campaign.

This module does not schedule itself. Dispatch additionally requires an explicit
command-line action, a separate worker activation and the existing execution
kill switch. Individual task authorization, locking, reservation and receipts
remain the coordinator's responsibility.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re

from .apollo_linkedin_attention import EXCLUDED_TASK
from .apollo_linkedin_live_executor import build_campaign_runtime, valid_campaign_version, valid_id
from .models import payload_hash


def _activated():
    return (os.environ.get('THUNDER_APOLLO_LI_WORKER_ENABLED') == 'true'
            and os.environ.get('THUNDER_APOLLO_LI_EXECUTION_ENABLED') == 'true')


class CampaignWorker:
    """Scan once, under the authority and storage of the existing coordinator."""
    def __init__(self, coordinator, *, campaign_id, approval_id, campaign_version,
                 budget_seconds=5, max_pages=3, max_tasks=1):
        if (not valid_id(campaign_id) or not valid_id(approval_id)
                or not valid_campaign_version(campaign_version)
                or type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds)
                or not 0 < budget_seconds <= 30 or type(max_pages) is not int
                or not 1 <= max_pages <= 500 or type(max_tasks) is not int
                or max_tasks != 1):
            raise ValueError('worker_configuration_invalid')
        self.coordinator = coordinator
        self.campaign_id, self.approval_id, self.campaign_version = campaign_id, approval_id, campaign_version
        self.budget_seconds, self.max_pages, self.max_tasks = budget_seconds, max_pages, max_tasks

    def run_once(self, *, execute=False):
        if type(execute) is not bool:
            raise ValueError('worker_configuration_invalid')
        result = {'state': 'blocked', 'coverage': 'unknown', 'reason': None,
                  'items': [], 'dispatch_enabled': False}
        if execute and not _activated():
            return {**result, 'reason': 'worker_disabled'}
        try:
            approval = self.coordinator.authority.require(self.campaign_id, self.approval_id, self.campaign_version)
            binding = approval['binding']
            reader = self.coordinator.apollo
            if (reader.team_id != binding['team_id'] or reader.owner_ids != frozenset(binding['owner_ids'])
                    or not isinstance(binding.get('sequence_id'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', binding['sequence_id'])
                    or not isinstance(binding.get('company_id'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', binding['company_id'])):
                return {**result, 'reason': 'reader_binding_mismatch'}
            if execute and not self.coordinator._gate():
                return {**result, 'reason': 'execution_disabled'}
            with reader.attention_budget(seconds=self.budget_seconds, max_pages=self.max_pages):
                scan = reader.list_open_tasks()
            if (not isinstance(scan, dict) or scan.get('coverage') not in ('complete', 'partial', 'unknown')
                    or not isinstance(scan.get('tasks'), list)):
                return {**result, 'reason': 'scan_invalid'}
            result['coverage'] = scan['coverage']
            result['reason'] = scan.get('reason')
            result['state'] = 'observed' if scan['coverage'] == 'complete' else 'partial'
            seen = set()
            eligible = []
            for task in scan['tasks']:
                if not isinstance(task, dict) or not valid_id(task.get('id')):
                    result.update(state='partial', coverage='unknown', reason='task_invalid')
                    return result
                ident = task['id']
                if ident in seen:
                    result.update(state='partial', coverage='unknown', reason='duplicate_task')
                    return result
                seen.add(ident)
                if ident == EXCLUDED_TASK:
                    result['items'].append({'task_id': ident, 'state': 'blocked', 'reason': 'fixture_protegida'})
                    continue
                sequence = task.get('emailer_campaign_id')
                if sequence is None:
                    result['items'].append({'task_id': ident, 'state': 'blocked', 'reason': 'sequence_unverified'})
                    continue
                if sequence != binding['sequence_id']:
                    continue
                if task.get('status') != 'scheduled' or task.get('user_id') not in binding['owner_ids']:
                    result['items'].append({'task_id': ident, 'state': 'blocked', 'reason': 'task_binding_unverified'})
                    continue
                if task.get('type') not in ('linkedin_step_message', 'linkedin_step_connect'):
                    result['items'].append({'task_id': ident, 'state': 'blocked', 'reason': 'action_not_supported'})
                    continue
                previous = self.coordinator.repo.get_apollo_linkedin_reservation(
                    binding['company_id'], ident, task['type'])
                if previous is not None:
                    result['items'].append({'task_id': ident, 'state': 'blocked', 'reason': 'reservation_exists'})
                    continue
                eligible.append(ident)
            cursor_key = payload_hash({'company': binding['company_id'], 'campaign': self.campaign_id})
            cursor_version = 0
            if execute and scan['coverage'] == 'complete' and eligible:
                cursor = self.coordinator.repo.get_state('apollo_linkedin_worker_cursor', cursor_key)
                if cursor is not None:
                    if (not isinstance(cursor, dict) or cursor.get('company') != binding['company_id']
                            or cursor.get('campaign') != self.campaign_id
                            or not valid_id(cursor.get('last_task')) or type(cursor.get('version')) is not int
                            or cursor['version'] < 1):
                        return {**result, 'state': 'blocked', 'reason': 'cursor_invalid'}
                    cursor_version = cursor['version']
                eligible.sort()
                if cursor is not None:
                    pivot = bisect.bisect_right(eligible, cursor['last_task'])
                    eligible = eligible[pivot:] + eligible[:pivot]
            for ident in eligible[:self.max_tasks]:
                if not execute:
                    result['items'].append({'task_id': ident, 'state': 'observed', 'reason': 'not_dispatched'})
                    continue
                # Partial coverage cannot imply that a scan is a complete queue.
                if scan['coverage'] != 'complete':
                    result['items'].append({'task_id': ident, 'state': 'blocked', 'reason': 'coverage_incomplete'})
                    continue
                if not _activated() or not self.coordinator._gate():
                    result['items'].append({'task_id': ident, 'state': 'blocked', 'reason': 'execution_disabled'})
                    result['reason'] = 'execution_disabled'
                    break
                outcome = self.coordinator.run_campaign(ident, self.campaign_id, self.approval_id,
                                                        self.campaign_version, dry_run=False)
                if not isinstance(outcome, dict) or not isinstance(outcome.get('state'), str):
                    outcome = {'state': 'blocked', 'reason': 'outcome_unavailable'}
                result['items'].append({'task_id': ident, 'state': outcome['state'],
                                        'reason': outcome.get('reason')})
                result['dispatch_enabled'] = True
                try:
                    saved = self.coordinator.repo.put_states_atomic([('apollo_linkedin_worker_cursor', cursor_key,
                        {'company': binding['company_id'], 'campaign': self.campaign_id, 'last_task': ident},
                        cursor_version)])[0]
                    cursor_version = saved['version']
                except Exception:
                    result.update(state='partial', reason='cursor_persistence_unavailable')
                    break
            result['eligible_observed'] = len(eligible)
            result['deferred'] = max(0, len(eligible) - self.max_tasks)
            return result
        except Exception:
            return {**result, 'state': 'blocked', 'reason': 'worker_unavailable'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Uma varredura de campanha Apollo + LinkedIn')
    parser.add_argument('--campaign', required=True)
    parser.add_argument('--approval', required=True)
    parser.add_argument('--campaign-version', required=True)
    parser.add_argument('--max-pages', type=int, default=3)
    parser.add_argument('--budget-seconds', type=float, default=5)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    if (not 1 <= args.max_pages <= 500 or not math.isfinite(args.budget_seconds)
            or not 0 < args.budget_seconds <= 30):
        parser.error('invalid scan budget')
    if args.execute and not _activated():
        result = {'state': 'blocked', 'coverage': 'unknown', 'reason': 'worker_disabled',
                  'items': [], 'dispatch_enabled': False}
    else:
        try:
            coordinator = build_campaign_runtime(args.campaign, args.approval, args.campaign_version,
                                                 readonly=not args.execute)
            result = CampaignWorker(coordinator, campaign_id=args.campaign, approval_id=args.approval,
                                    campaign_version=args.campaign_version, max_pages=args.max_pages,
                                    budget_seconds=args.budget_seconds).run_once(execute=args.execute)
        except Exception:
            result = {'state': 'blocked', 'coverage': 'unknown', 'reason': 'worker_unavailable',
                      'items': [], 'dispatch_enabled': False}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['state'] == 'observed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
