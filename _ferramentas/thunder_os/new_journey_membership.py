"""Current per-email membership reads, never enrollment or suppression authority.

Docs checked 2026-10-23: Instantly-ai/instantly-starter-kit/docs/api/leads.md;
developer.instantly.ai/api-reference/{lead/list-leads,workspace/get-workspace}.md.
Instantly contacts[] is an exact email filter, unlike its fuzzy search field.
All workspace campaigns are included; no campaign, owner or active-only filter.
Completed Instantly leads still linked to a campaign remain occupied. Apollo
finished + finished_at proves only ended membership, NOT permission to contact.
Failed/expired/unknown memberships cannot establish availability.
"""
from datetime import datetime
import hashlib
import http.client
import json
import math
import os
import ssl
import time

from .apollo_prospect_readonly import ApolloProspectReadonly
from .external_email_import import INSTANTLY_WORKSPACE, email, require
from .instantly_reader import KEY_FILE, private_text
from .instantly_webhook import _https_request as _get_request, _uuid


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True).encode()).hexdigest()


def _address(value):
    address = email(value)
    require(address.isascii() and not any(c in address for c in '*"\\/\x00'), 'invalid_email')
    return address


def _result(provider, address, status='unknown', reason='read_unavailable', *,
            identity=None, records=(), complete=False):
    proof = {'provider': provider, 'email': address, 'identity': identity,
             'records': sorted(records, key=lambda row: json.dumps(row, sort_keys=True)),
             'status': status, 'reason': reason, 'complete': complete}
    return {'status': status, 'reason': provider + '_' + reason,
            'source_ref': provider + ':' + _digest(proof),
            'coverage': {'complete': complete, 'identity': identity}}


class _Budget:
    def __init__(self, clock, deadline=None):
        self.clock, self.count = clock, 0
        self.deadline = clock() + 15
        if deadline is not None:
            require(type(deadline) in (int, float) and math.isfinite(deadline), 'invalid_deadline')
            self.deadline = min(self.deadline, deadline)

    def check(self):
        remaining = self.deadline - self.clock()
        require(remaining > 0 and self.count < 10, 'membership_budget_exhausted')
        return remaining

    def call(self, transport, method, path, body, headers, timeout):
        remaining = self.check()
        self.count += 1
        result = transport(method, path, body, headers, min(timeout, 8, remaining))
        require(self.clock() < self.deadline, 'membership_budget_exhausted')
        return result


def _wire_allowed(method, path, body):
    if method == 'GET' and path == '/api/v2/workspaces/current' and body is None:
        return True
    if method != 'POST' or path != '/api/v2/leads/list' or not isinstance(body, dict):
        return False
    if set(body) not in ({'contacts', 'limit', 'distinct_contacts'},
                         {'contacts', 'limit', 'distinct_contacts', 'starting_after'}):
        return False
    try:
        return (body['contacts'] == [_address(body['contacts'][0])]
                and type(body['limit']) is int and body['limit'] == 100
                and body['distinct_contacts'] is False
                and ('starting_after' not in body or _uuid(body['starting_after'])))
    except (ValueError, TypeError, IndexError, KeyError):
        return False


def _https_request(method, path, body, headers, timeout):
    """The existing GET transport omits body; only the fixed list read needs JSON."""
    require(_wire_allowed(method, path, body), 'instantly_endpoint_blocked')
    if method == 'GET':
        return _get_request(method, path, body, headers, timeout)
    require(not any(os.environ.get(key) for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR')),
            'tls_override_blocked')
    connection = http.client.HTTPSConnection('api.instantly.ai', 443, timeout=timeout,
                                            context=ssl.create_default_context())
    try:
        connection.request(method, path, body=json.dumps(body).encode(), headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        require(len(raw) <= 1_000_000, 'response_too_large')
        return response.status, raw
    finally:
        connection.close()


class InstantlyMembershipReadOnly:
    """Fixed free workspace/lead reads, caller auth or existing private key file.

    No credential is read at construction. request uses the existing transport
    signature and must honor timeout/response limits; there are no retries.
    A maximum of 200 lead rows and 3 pages (including an empty terminal page).
    """
    def __init__(self, credential_loader=None, *, request=None, clock=None):
        self.loader = credential_loader or (lambda: private_text(KEY_FILE))
        self.request, self.clock = request or _https_request, clock or time.monotonic

    def _read(self, method, path, body, token, budget):
        require(_wire_allowed(method, path, body), 'instantly_endpoint_blocked')
        status, raw = budget.call(self.request, method, path, body, {
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
            'Accept': 'application/json'}, 8)
        require(type(status) is int and status == 200, 'instantly_read_unavailable')
        require(isinstance(raw, bytes) and len(raw) <= 1_000_000, 'invalid_response')
        result = json.loads(raw)
        require(isinstance(result, dict) and not any(key in result for key in
                ('error', 'errors', 'error_code', 'statusCode')), 'invalid_response')
        return result

    def lookup(self, address, *, deadline=None, _budget=None):
        records, identity, occupied, complete = [], None, False, False
        reason = 'read_unavailable'
        try:
            address = _address(address)
            budget = _budget or _Budget(self.clock, deadline)
            budget.check()
            token = self.loader()
            require(isinstance(token, str) and 0 < len(token) <= 4096 and token.isascii()
                    and all(33 <= ord(c) <= 126 for c in token), 'credential_unavailable')
            workspace = self._read('GET', '/api/v2/workspaces/current', None, token, budget)
            require(workspace.get('id') == INSTANTLY_WORKSPACE, 'workspace_mismatch')
            identity = INSTANTLY_WORKSPACE
            cursor, seen, count, invalid = None, set(), 0, False
            for _ in range(3):
                body = {'contacts': [address], 'limit': 100, 'distinct_contacts': False}
                if cursor:
                    body['starting_after'] = cursor
                data = self._read('POST', '/api/v2/leads/list', body, token, budget)
                rows = data.get('items')
                require(isinstance(rows, list) and len(rows) <= 100, 'invalid_page')
                count += len(rows)
                require(count <= 200, 'row_limit')
                for row in rows:
                    require(isinstance(row, dict) and row.get('organization') == identity,
                            'record_identity_mismatch')
                    require(_address(row.get('email')) == address, 'record_identity_mismatch')
                    require(_uuid(row.get('id')) and row['id'] not in seen, 'duplicate_or_invalid_record')
                    seen.add(row['id'])
                    status, campaign = row.get('status'), row.get('campaign')
                    valid = type(status) is int and status in (1, 2, 3, -1, -2, -3)
                    invalid |= not valid or not _uuid(campaign)
                    record = {key: row[key] for key in ('id', 'email', 'organization',
                        'campaign', 'status', 'timestamp_updated') if key in row}
                    require(token not in json.dumps(record), 'invalid_record')
                    records.append(record)
                    occupied |= valid and bool(_uuid(campaign))
                next_cursor = data.get('next_starting_after')
                require(next_cursor is None or _uuid(next_cursor), 'invalid_cursor')
                if not rows:
                    require(next_cursor is None, 'invalid_cursor')
                if next_cursor is None:
                    complete = not invalid
                    reason = 'membership_present' if occupied else 'membership_unresolved' if invalid else 'no_membership'
                    break
                require(next_cursor != cursor and next_cursor not in seen - {rows[-1]['id']},
                        'pagination_repeated')
                cursor = next_cursor
            else:
                reason = 'pagination_incomplete'
        except Exception:
            # Never reflect provider bodies, loader/transport exceptions or credentials.
            reason = 'read_unavailable'
        return _result('instantly', address if isinstance(address, str) else None,
            'occupied' if occupied else 'clear' if complete else 'unknown', reason,
            identity=identity, records=records, complete=complete)


class CrossCampaignReadOnly:
    """Combine current Apollo + Instantly proof for one canonical email.

    HubSpot, alias resolution and suppression remain the caller's responsibility.
    Supply the existing ApolloProspectReadonly, not CSV/provider-shaped callbacks.
    Optional apollo_account_id pins a server-configured team. An injected identity
    on the Apollo client must also agree with the actual profile. lookup(deadline=)
    accepts the checker's absolute monotonic deadline; lookup_many shares 15s for
    at most 200 inputs. No cache, database, mutable provider state or background job.
    """
    def __init__(self, apollo=None, instantly=None, *, apollo_account_id=None, clock=None):
        self.apollo, self.instantly = apollo, instantly
        self.apollo_account_id, self.clock = apollo_account_id, clock or time.monotonic

    def _apollo(self, address, budget):
        records, identity, occupied, complete = [], None, False, False
        reason = 'read_unavailable'
        try:
            original = self.apollo
            require(isinstance(original, ApolloProspectReadonly), 'reader_unavailable')
            budget.check()
            port = ApolloProspectReadonly(original._loader, identity_reader=original._identity,
                request=lambda *args: budget.call(original._request, *args),
                enabled=original.enabled, timeout=min(8, original.timeout), max_rows=200)
            profile = port.profile()
            source = profile['source']
            require(profile['complete'] is True and source['identity_verified'] is True
                    and source['identity_source'] == 'verified_profile', 'identity_unverified')
            identity = source['account_id']
            require(self.apollo_account_id in (None, identity)
                    and source.get('injected_account_id') in (None, identity), 'identity_mismatch')
            invalid, seen, expected_total, total_rows = False, set(), None, 0
            for page in (1, 2):
                result = port.contacts_search(address, page=page, per_page=100)
                source = result['source']
                require(source.get('identity_verified') is True and source.get('account_id') == identity
                        and source.get('injected_account_id') in (None, identity), 'identity_mismatch')
                require(result.get('reason') in (None, 'more_pages', 'page_slice', 'partial_response')
                        and result.get('status') in ('ok', 'partial'), 'partial_response')
                invalid |= result.get('reason') == 'partial_response'
                total, rows = result.get('total'), result.get('items')
                require(type(total) is int and 0 <= total <= 200 and isinstance(rows, list), 'partial_response')
                require(expected_total in (None, total), 'pagination_changed')
                expected_total = total
                total_rows += len(rows)
                for row in rows:
                    require(row['id'] not in seen, 'duplicate_record')
                    seen.add(row['id'])
                    # Apollo q_keywords is fuzzy: a different explicit email is not this person.
                    observed = _address(row.get('email'))
                    if observed != address:
                        continue
                    memberships = row.get('contact_campaign_statuses')
                    known = row.get('contact_campaign_statuses_complete') is True and isinstance(memberships, list)
                    invalid |= not known
                    records.append({'id': row['id'], 'email': observed,
                        'contact_campaign_statuses': sorted(memberships,
                            key=lambda item: item['emailer_campaign_id']) if isinstance(memberships, list) else None,
                        'complete': known})
                    for membership in memberships if isinstance(memberships, list) else []:
                        status = membership.get('status')
                        invalid |= membership.get('state') not in (None, status)
                        occupied |= status in ('active', 'paused')
                        if status == 'finished':
                            try:
                                finished = datetime.fromisoformat(membership['finished_at'].replace('Z', '+00:00'))
                                invalid |= finished.utcoffset() is None
                            except (ValueError, TypeError, KeyError, AttributeError):
                                invalid = True
                        elif status not in ('active', 'paused'):
                            invalid = True
                if not source.get('has_more'):
                    require(total_rows == total, 'pagination_incomplete')
                    complete = not invalid
                    reason = 'membership_present' if occupied else 'membership_unresolved' if invalid else 'no_membership'
                    break
                require(source.get('next_page') == page + 1, 'pagination_incomplete')
            else:
                reason = 'pagination_incomplete'
        except Exception:
            reason = 'read_unavailable'
        return _result('apollo', address, 'occupied' if occupied else 'clear' if complete else 'unknown',
                       reason, identity=identity, records=records, complete=complete)

    def lookup(self, address, *, deadline=None):
        try:
            address = _address(address)
            budget = _Budget(self.clock, deadline)
        except Exception:
            return self._combined(None, [], 'invalid_email_or_deadline')
        apollo = self._apollo(address, budget)
        if isinstance(self.instantly, InstantlyMembershipReadOnly):
            instantly = self.instantly.lookup(address, _budget=budget)
        else:
            instantly = _result('instantly', address)
        return self._combined(address, [apollo, instantly])

    @staticmethod
    def _combined(address, results, reason=None):
        occupied = next((r for r in results if r['status'] == 'occupied'), None)
        complete = len(results) == 2 and all(r['coverage']['complete'] for r in results)
        status = 'occupied' if occupied else 'clear' if complete else 'unknown'
        reason = reason or (occupied['reason'] if occupied else 'providers_no_membership' if complete
                            else 'provider_membership_coverage_incomplete')
        return {'status': status, 'reason': reason, 'source_ref': 'membership:' + _digest({
                    'email': address, 'sources': [r['source_ref'] for r in results], 'reason': reason}),
                'coverage': {'complete': complete, 'providers': ['apollo', 'instantly'],
                             'scope': 'exact_email_all_account_campaigns', 'results': results}}

    def lookup_many(self, addresses, *, deadline=None):
        require(isinstance(addresses, list) and len(addresses) <= 200, 'membership_row_limit')
        budget = _Budget(self.clock, deadline)
        return [self.lookup(value, deadline=budget.deadline) for value in addresses]
