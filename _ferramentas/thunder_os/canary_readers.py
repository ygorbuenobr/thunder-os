"""Per-contact canary observations through fixed, read-only provider routes.

No API is called by construction. A deployment-owned allowlist (at most two
contacts) is checked before authentication or IO. The email canary has no
LinkedIn prerequisite; a separate LinkedIn target must be supplied explicitly.
Missing data or exhausted pagination is an unavailable observation, never clean.

Contracts: Apollo search-for-contacts; Instantly email/list-email and workspaces;
ConnectSafely GET profile, conversations/exists and synchronous messages.
SEQD sending remains behind SEQDConsole's exact preview/approval/one-shot gate.
"""
from dataclasses import dataclass
import argparse
from functools import partial
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .delegated_suppression import ContactScope, DelegatedSuppression, SuppressionUnavailable
from .hubspot_executor import HubSpotHTTP
from .hubspot_console import systemd_hubspot_credential
from .integration_hubspot import PORTAL_ID
from .seqd_client import MAILBOX, SEQDClient, SEQDError


class CanaryReadError(ValueError):
    pass


def require(condition, code='canary_observation_unavailable'):
    if not condition:
        raise CanaryReadError(code)


def _address(value):
    require(isinstance(value, str) and len(value) <= 254 and value == value.strip().lower()
            and re.fullmatch(r'[^\s@<>*]+@[a-z0-9.-]+\.[a-z]{2,63}', value), 'canary_email_invalid')
    return value


def _id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def _json_response(response):
    require(isinstance(response, tuple) and len(response) in (2, 3))
    status, raw = response[:2]
    if status == 429:
        raise CanaryReadError('canary_provider_rate_limited')
    require(type(status) is int and status == 200)
    require(isinstance(raw, bytes) and len(raw) <= 1_000_000)
    data = json.loads(raw)
    require(isinstance(data, dict))
    return data


@dataclass(frozen=True)
class CanaryContact:
    contact_id: str
    email: str
    channels: tuple
    email_providers: tuple
    linkedin_public_id: str = ''
    linkedin_profile_urn: str = ''
    apollo_contact_id: str = ''


class CanaryScope:
    def __init__(self, config, *, owner_public_id):
        require(isinstance(config, dict) and config.get('version') == 1, 'canary_config_invalid')
        require(set(config) <= {'version', 'contacts', 'hubspot', 'apollo', 'instantly', 'owner_profile_urn'},
                'canary_config_invalid')
        # Optional pinned member URN of the owner. The provider reports message
        # senders by member ID even when the owner is pinned by vanity slug.
        self.owner_profile_urn = config.get('owner_profile_urn')
        require(self.owner_profile_urn is None or (isinstance(self.owner_profile_urn, str)
                and re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}', self.owner_profile_urn)),
                'canary_config_invalid')
        rows = config.get('contacts')
        require(isinstance(rows, list) and 1 <= len(rows) <= 2, 'canary_config_invalid')
        self.by_id, self.by_email, self.by_profile = {}, {}, {}
        for row in rows:
            require(isinstance(row, dict) and set(row) <= {'hubspot_contact_id', 'email', 'channels',
                'email_providers', 'linkedin_public_id', 'linkedin_profile_urn', 'apollo_contact_id'}, 'canary_config_invalid')
            ident, address = row.get('hubspot_contact_id'), _address(row.get('email'))
            require(isinstance(ident, str) and re.fullmatch(r'[1-9][0-9]{0,29}', ident), 'canary_config_invalid')
            channels, providers = row.get('channels'), row.get('email_providers')
            require(isinstance(channels, list) and channels and len(channels) == len(set(channels))
                and set(channels) <= {'email', 'linkedin'}, 'canary_config_invalid')
            require(isinstance(providers, list) and providers and len(providers) == len(set(providers))
                and set(providers) <= {'apollo', 'instantly'}, 'canary_config_invalid')
            if 'email' in channels:
                require(address == MAILBOX, 'email_canary_must_target_owner')
            public, urn = row.get('linkedin_public_id', ''), row.get('linkedin_profile_urn', '')
            if 'linkedin' in channels:
                require(_id(public) and public != owner_public_id
                    and isinstance(urn, str) and re.fullmatch(r'urn:li:fsd_profile:[A-Za-z0-9_-]{1,200}', urn)
                    and _id(row.get('apollo_contact_id')), 'linkedin_canary_target_required')
            else:
                require(not public and not urn and not row.get('apollo_contact_id'), 'canary_config_invalid')
            require(ident not in self.by_id and address not in self.by_email
                and (not urn or urn not in self.by_profile), 'canary_duplicate_target')
            contact = CanaryContact(ident, address, tuple(channels), tuple(providers), public, urn,
                                    row.get('apollo_contact_id', ''))
            self.by_id[ident] = self.by_email[address] = contact
            if urn:
                self.by_profile[urn] = contact
        require(self.owner_profile_urn not in self.by_profile, 'canary_config_invalid')

    def contact(self, contact_id):
        require(contact_id in self.by_id, 'contact_outside_canary')
        return self.by_id[contact_id]

    def email(self, address):
        require(address in self.by_email, 'contact_outside_canary')
        return self.by_email[address]


class HubSpotCanaryReader:
    """Search by email before reading a pinned ID, on every observation."""
    def __init__(self, scope, port):
        self.scope, self.port = scope, port

    def read(self, contact_id):
        target = self.scope.contact(contact_id)
        require(str(self.port._call('GET', '/account-info/v3/details').get('portalId')) == PORTAL_ID,
                'canary_hubspot_portal_mismatch')
        props = ['email', 'firstname', 'lastname', 'company', 'hs_linkedin_url', 'linkedin_url', 'hs_email_optout']
        search = self.port._call('POST', '/crm/v3/objects/contacts/search', {
            'filterGroups': [{'filters': [{'propertyName': 'email', 'operator': 'EQ', 'value': target.email}]}],
            'properties': ['email'], 'limit': 2})
        rows = search.get('results')
        require(search.get('total') == 1 and not search.get('paging', {}).get('next')
            and isinstance(rows, list) and len(rows) == 1 and str(rows[0].get('id')) == contact_id
            and rows[0].get('properties', {}).get('email', '').lower() == target.email,
            'canary_hubspot_email_id_mismatch')
        result = self.port._call('GET', '/crm/v3/objects/contacts/' + contact_id + '?' +
                                 urlencode({'properties': ','.join(props)}))
        values = result.get('properties')
        require(str(result.get('id')) == contact_id and isinstance(values, dict)
            and values.get('email', '').lower() == target.email, 'canary_hubspot_email_id_mismatch')
        return values

    def optout(self, contact_id):
        props = self.read(contact_id)
        require('hs_email_optout' in props, 'canary_hubspot_optout_unavailable')
        value = props['hs_email_optout']
        require(value is None or type(value) is bool or value in ('', 'true', 'false'),
                'canary_hubspot_optout_unavailable')
        return {'target': contact_id, 'complete': True, 'suppressed': value is True or value == 'true',
                'coverage': 'exact_contact', 'no_record': value in ('', None)}


class ApolloCanaryReader:
    """Fresh contact search, never a full-campaign sweep or enrichment.

    No saved contact means no record in this source, scoped to the exact email.
    A saved contact in a sequence without an explicit reply observation is
    unknown and is rejected; active/paused status alone does not prove a
    negative reply. A saved contact never placed in any Apollo sequence (no
    membership and no reply field) has received no Apollo email, so Apollo
    holds no reply for it (owner decision, 2026-11-03); opt-out and bounce
    still suppress, and a present but malformed reply field still fails.
    """
    def __init__(self, scope, team_id, credential_loader, *, request=None):
        from .apollo_readonly import _https_request
        require(_id(team_id), 'canary_apollo_team_invalid')
        self.scope, self.team_id, self.loader = scope, team_id, credential_loader
        self.request = request or _https_request

    def _call(self, method, path, body=None):
        require((method == 'GET' and path == '/api/v1/users/api_profile' and body is None)
            or (method == 'POST' and path == '/api/v1/contacts/search' and isinstance(body, dict)
                and set(body) == {'q_keywords', 'page', 'per_page'} and body['page'] == 1
                and body['per_page'] == 10 and body['q_keywords'] in self.scope.by_email), 'canary_endpoint_blocked')
        token = self.loader()
        require(isinstance(token, str) and token and not any(c.isspace() for c in token), 'canary_credential_unavailable')
        return _json_response(self.request(method, path, body,
            {'x-api-key': token, 'Content-Type': 'application/json'}, 8))

    def contacts(self, address):
        self.scope.email(address)
        profile = self._call('GET', '/api/v1/users/api_profile')
        user = profile.get('user', profile)
        require(isinstance(user, dict) and user.get('team_id') == self.team_id, 'canary_apollo_team_mismatch')
        data = self._call('POST', '/api/v1/contacts/search', {'q_keywords': address, 'page': 1, 'per_page': 10})
        page, rows = data.get('pagination'), data.get('contacts')
        require(isinstance(page, dict) and type(page.get('total_entries')) is int
            and 0 <= page['total_entries'] <= 10 and page.get('page') == 1
            and page.get('per_page') == 10 and isinstance(rows, list) and len(rows) == page['total_entries']
            and data.get('partial_results_only') is not True, 'canary_apollo_search_incomplete')
        require(all(isinstance(row, dict) and _id(row.get('id'))
            and row.get('team_id', self.team_id) == self.team_id for row in rows))
        return [row for row in rows if isinstance(row.get('email'), str) and row['email'].lower() == address]

    def __call__(self, address):
        rows = self.contacts(address)
        require(len(rows) <= 1, 'canary_apollo_contact_ambiguous')
        if not rows:
            return {'target': address, 'complete': True, 'suppressed': False, 'no_record': True,
                    'coverage': 'exact_email_saved_contacts'}
        row = rows[0]
        memberships = row.get('contact_campaign_statuses')
        require(isinstance(memberships, list) and len(memberships) <= 50, 'canary_apollo_reply_unavailable')
        optout = row.get('email_unsubscribed')
        require(optout is None or type(optout) is bool, 'canary_apollo_reply_unavailable')
        row_reply = row.get('email_replied')
        require('email_replied' not in row or type(row_reply) is bool, 'canary_apollo_reply_unavailable')
        never_sequenced = not memberships and 'email_replied' not in row
        require(bool(memberships) or type(row_reply) is bool or never_sequenced, 'canary_apollo_reply_unavailable')
        blocked = optout is True or row_reply is True or row.get('email_status') in ('unsubscribed', 'bounced')
        if memberships:
            for item in memberships:
                require(isinstance(item, dict), 'canary_apollo_reply_unavailable')
                replied = item.get('replied', row_reply)
                require('replied' not in item or type(replied) is bool, 'canary_apollo_reply_unavailable')
                reason = item.get('inactive_reason')
                if (item.get('in_response_to_emailer_message_id') or reason in
                        ('replied', 'contact_replied', 'unsubscribed', 'bounced')):
                    blocked = True
                elif type(replied) is bool:
                    blocked = blocked or replied
                else:
                    raise CanaryReadError('canary_apollo_reply_unavailable')
        return {'target': address, 'complete': True, 'suppressed': blocked,
                'no_record': False, 'coverage': 'exact_email_saved_contacts'}


class InstantlyCanaryReader:
    def __init__(self, scope, workspace_id, credential_loader, *, request=None):
        from .instantly_webhook import _https_request
        require(_id(workspace_id), 'canary_instantly_workspace_invalid')
        self.scope, self.workspace_id, self.loader = scope, workspace_id, credential_loader
        self.request = request or _https_request

    def _get(self, path):
        valid = path == '/api/v2/workspaces/current'
        if not valid:
            parsed = urlsplit(path)
            query = parse_qs(parsed.query, strict_parsing=True)
            valid = (not parsed.scheme and not parsed.netloc and not parsed.fragment
                and parsed.path == '/api/v2/emails' and set(query) in
                ({'lead', 'limit', 'mode', 'preview_only'}, {'lead', 'limit', 'mode', 'preview_only', 'starting_after'})
                and query.get('lead', [None])[0] in self.scope.by_email
                and all(len(values) == 1 for values in query.values()) and query.get('limit') == ['10']
                and query.get('mode') == ['emode_all'] and query.get('preview_only') == ['true']
                and ('starting_after' not in query or _id(query['starting_after'][0])))
        require(valid, 'canary_endpoint_blocked')
        token = self.loader()
        require(isinstance(token, str) and token and not any(c.isspace() for c in token), 'canary_credential_unavailable')
        return _json_response(self.request('GET', path, None,
            {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, 8))

    def __call__(self, address):
        self.scope.email(address)
        require(self._get('/api/v2/workspaces/current').get('id') == self.workspace_id,
                'canary_instantly_workspace_mismatch')
        cursor, seen, any_record, blocked = None, set(), False, False
        for _ in range(3):
            query = {'lead': address, 'limit': 10, 'mode': 'emode_all', 'preview_only': 'true'}
            if cursor:
                query['starting_after'] = cursor
            data = self._get('/api/v2/emails?' + urlencode(query))
            rows = data.get('items')
            require(isinstance(rows, list) and len(rows) <= 10)
            for row in rows:
                require(isinstance(row, dict) and row.get('organization_id') == self.workspace_id
                    and isinstance(row.get('lead'), str) and row['lead'].lower() == address
                    and _id(row.get('id')) and row['id'] not in seen, 'canary_instantly_contact_mismatch')
                seen.add(row['id']); any_record = True
                require(type(row.get('ue_type')) is int and row['ue_type'] in (1, 2, 3, 4),
                        'canary_instantly_reply_unavailable')
                blocked = blocked or row['ue_type'] == 2
            next_cursor = data.get('next_starting_after')
            if next_cursor is None:
                return {'target': address, 'complete': True, 'suppressed': blocked,
                        'no_record': not any_record, 'coverage': 'exact_email_unibox'}
            require(_id(next_cursor) and next_cursor != cursor and rows, 'canary_instantly_search_incomplete')
            cursor = next_cursor
        raise CanaryReadError('canary_instantly_search_incomplete')


class CanaryConnectSafelyHTTPS:
    """Read-only extension for explicit forceRefresh and synchronous messages."""
    def __init__(self, account_id, credential_loader, *, connection_factory=None):
        from .connectsafely_transport import ConnectSafelyHTTPS
        class ReadHTTPS(ConnectSafelyHTTPS):
            def _route(self, method, path, query, body):
                require(method == 'GET' and body is None, 'canary_endpoint_blocked')
                if path == '/linkedin/profile':
                    require(query.get('forceRefresh') == 'true', 'canary_endpoint_blocked')
                    return super()._route(method, path, {k:v for k,v in query.items() if k != 'forceRefresh'}, body)
                if path.endswith('/messages'):
                    require(query.get('sync') == 'true', 'canary_endpoint_blocked')
                    return super()._route(method, path, {k:v for k,v in query.items() if k != 'sync'}, body)
                return super()._route(method, path, query, body)
        self.port = ReadHTTPS(account_id, credential_loader, connection_factory=connection_factory)

    def __call__(self, *args):
        return self.port(*args)


class ConnectSafelyCanaryReader:
    def __init__(self, scope, client):
        self.scope, self.client = scope, client

    def profile(self, contact, plan):
        require('linkedin' in contact.channels and plan.get('account_id') == self.client.account_id
            and plan.get('recipient_public_id') == contact.linkedin_public_id
            and plan.get('recipient_profile_urn') == contact.linkedin_profile_urn
            and self.client._profile_identity(contact.linkedin_profile_urn) !=
                self.client._profile_identity(self.client.self_profile_id), 'contact_outside_canary')
        self.client.verify_identity()
        data = self.client._call('GET', '/linkedin/profile', {'accountId': self.client.account_id,
            'profileId': contact.linkedin_public_id, 'forceRefresh': 'true'})
        raw = data.get('profile')
        require(data.get('success') is True and data.get('accountId') == self.client.account_id
            and isinstance(raw, dict) and raw.get('publicIdentifier') == contact.linkedin_public_id
            and raw.get('entityUrn') == contact.linkedin_profile_urn,
            'canary_connectsafely_profile_mismatch')
        # The deployed profile API can omit cache telemetry. The transport must
        # still send forceRefresh=true. Omission is explicitly unreported, not
        # manufactured provider proof of an uncached response.
        require('cached' not in data or type(data['cached']) is bool and data['cached'] is False,
                'canary_profile_freshness_unavailable')
        cache_state = 'unreported' if 'cached' not in data else 'fresh_reported'
        name = ' '.join(raw.get(key, '') for key in ('firstName', 'lastName')).strip()
        company = raw.get('companyName') or raw.get('currentCompany')
        if isinstance(company, dict):
            company = company.get('name')
        if not company:
            positions = raw.get('positions', raw.get('experience', []))
            require(isinstance(positions, list))
            def explicitly_current(item):
                if not isinstance(item, dict):
                    return False
                if 'isCurrent' in item:
                    require(type(item['isCurrent']) is bool, 'canary_current_company_unavailable')
                    return item['isCurrent']
                duration = item.get('duration')
                return isinstance(duration, str) and re.search(
                    r'\s[-–—]\s*(?:Present|Current)(?:\s*[·•]|\s*$)', duration, re.I) is not None
            current = [item for item in positions if explicitly_current(item)]
            require(len(current) == 1, 'canary_current_company_unavailable')
            company = current[0].get('companyName') or current[0].get('company', {}).get('name')
        require(isinstance(name, str) and name and isinstance(company, str) and company,
                'canary_current_company_unavailable')
        return {'account_id': self.client.account_id, 'profile_urn': contact.linkedin_profile_urn,
            'linkedin_url': 'https://www.linkedin.com/in/' + contact.linkedin_public_id,
            'name': name, 'company': company,
            'freshness':{'request':'forceRefresh=true', 'provider_cache':cache_state}}

    def __call__(self, urn):
        require(urn in self.scope.by_profile, 'contact_outside_canary')
        contact = self.scope.by_profile[urn]
        require(self.client._profile_identity(urn) != self.client._profile_identity(self.client.self_profile_id),
                'linkedin_canary_must_not_target_owner')
        self.client.verify_identity()
        data = self.client._call('GET', '/linkedin/conversations/exists/' + quote(contact.linkedin_public_id, safe=''),
                                 {'accountId': self.client.account_id})
        require(data.get('success') is True and data.get('accountId') == self.client.account_id
            and data.get('profileUrn') == urn and type(data.get('hasConversation')) is bool,
            'canary_conversation_mismatch')
        if not data['hasConversation']:
            return {'target': urn, 'complete': True, 'suppressed': False, 'no_record': True,
                    'coverage': 'exact_profile_conversation'}
        thread = data.get('conversationUrn')
        require(isinstance(thread, str) and thread.startswith('urn:li:'), 'canary_conversation_mismatch')
        seen, cursor, blocked = set(), None, False
        owner = self.client._profile_identity(self.client.self_profile_id)
        recipient = self.client._profile_identity(urn)
        for _ in range(3):
            query = {'accountId': self.client.account_id, 'count': 10, 'sync': 'true'}
            if cursor:
                query['cursor'] = cursor
            data = self.client._call('GET', '/linkedin/conversations/' + quote(thread, safe='') + '/messages', query)
            require(data.get('success') is True and data.get('accountId') == self.client.account_id
                and data.get('conversationUrn') == thread and type(data.get('hasMore')) is bool
                and data.get('fetchingMore', False) is False, 'canary_conversation_incomplete')
            participants = data.get('participants')
            aliases = {self.client.expected_public_id: owner, contact.linkedin_public_id: recipient}
            pinned_owner = self.client._profile_identity(self.scope.owner_profile_urn) if self.scope.owner_profile_urn else None
            if pinned_owner:
                require(pinned_owner != recipient, 'canary_conversation_mismatch')
                aliases[pinned_owner] = owner
            # The provider can emit both vanity slug and bare profile ID for
            # the same participant. Resolve only these already pinned aliases;
            # every row must still belong to exactly the two principals.
            require(isinstance(participants, list) and 2 <= len(participants) <= 20
                and all(isinstance(item, dict) and isinstance(item.get('profileId'), str) for item in participants)
                and {aliases.get(item.get('profileId'), self.client._profile_identity(item.get('profileId')))
                     for item in participants} == {owner, recipient}
                and owner and recipient and owner != recipient, 'canary_conversation_mismatch')
            rows = data.get('messages')
            require(isinstance(rows, list) and len(rows) <= 10)
            for row in rows:
                require(isinstance(row, dict) and isinstance(row.get('messageUrn'), str)
                    and row['messageUrn'] not in seen and type(row.get('isSentByOwner')) is bool)
                seen.add(row['messageUrn'])
                sender = self.client._sender_identity(row)
                sender = aliases.get(sender, sender)
                require(sender in {owner, recipient}
                    and row['isSentByOwner'] == (sender == owner), 'canary_sender_unverified')
                blocked = blocked or sender == recipient
            if data['hasMore'] is False:
                return {'target': urn, 'complete': True, 'suppressed': blocked, 'no_record': not seen,
                        'coverage': 'exact_profile_conversation'}
            next_cursor = data.get('nextCursor')
            require(isinstance(next_cursor, str) and re.fullmatch(r'[0-9]{1,16}', next_cursor)
                and (cursor is None or int(next_cursor) < int(cursor)) and rows,
                'canary_conversation_incomplete')
            cursor = next_cursor
        raise CanaryReadError('canary_conversation_incomplete')


def _linkedin_key(value):
    """Canonical profile key; Apollo stores LinkedIn URLs as http://www.linkedin.com/in/….

    Only the scheme of a LinkedIn host is upgraded; any other host or shape fails.
    """
    from .connection_intake import ConnectionIntakeError, profile_key
    if isinstance(value, str) and re.match(r'(?i)http://(?:www\.)?linkedin\.com/in/', value.strip()):
        value = 'https://' + value.strip()[len('http://'):]
    try:
        return profile_key(value)
    except ConnectionIntakeError:
        raise CanaryReadError('canary_linkedin_url_invalid') from None


class CanaryReaders:
    def __init__(self, scope, hubspot, linkedin, email_readers, *, clock=time.monotonic):
        self.scope, self.hubspot, self.linkedin, self.email_readers = scope, hubspot, linkedin, email_readers
        self.clock = clock
        self.suppression = DelegatedSuppression(linkedin=linkedin,
            email_readers={name: self.email_status for name in ('apollo', 'instantly')}, hubspot=hubspot.optout, clock=clock)

    def email_status(self, address):
        contact = self.scope.email(address)
        observations = {}
        for provider in contact.email_providers:
            require(provider in self.email_readers, 'canary_email_reader_unavailable')
            result = self.email_readers[provider](address)
            require(result.get('target') == address and result.get('complete') is True
                and type(result.get('suppressed')) is bool)
            observations[provider] = result
        return {'target': address, 'complete': True,
            'suppressed': any(result['suppressed'] for result in observations.values()), 'sources': observations}

    def check_email(self, address):
        """Email-only freshness: HubSpot + selected email sources, no LI lookup."""
        contact = self.scope.email(address)
        require('email' in contact.channels, 'contact_outside_canary')
        started = self.clock()
        hubspot, providers = self.hubspot.optout(contact.contact_id), self.email_status(address)
        require(self.clock() - started <= 30, 'canary_observation_expired')
        require(hubspot.get('complete') is True and hubspot.get('suppressed') is False
            and providers['suppressed'] is False, 'canary_suppression_not_cleared')
        return {'target': address, 'contact_id': contact.contact_id, 'complete': True,
                'completed_at': started, 'sources': {'hubspot': hubspot, **providers['sources']}}

    def identity(self, contact_id, plan):
        contact = self.scope.contact(contact_id)
        require('linkedin' in contact.channels, 'contact_outside_canary')
        props = self.hubspot.read(contact_id)
        apollo = self.email_readers.get('apollo')
        require(isinstance(apollo, ApolloCanaryReader), 'canary_apollo_identity_unavailable')
        rows = apollo.contacts(contact.email)
        require(len(rows) == 1 and rows[0].get('id') == contact.apollo_contact_id,
                'canary_apollo_identity_mismatch')
        origin = _linkedin_key(rows[0].get('linkedin_url'))
        urls = [_linkedin_key(props[key]) for key in ('linkedin_url', 'hs_linkedin_url') if props.get(key)]
        require(origin and urls and set(urls) == {origin}, 'canary_hubspot_apollo_url_mismatch')
        name = ' '.join(props.get(key) or '' for key in ('firstname', 'lastname')).strip()
        return {'contact': {'id': contact_id, 'linkedin_url': origin, 'name': name, 'company': props.get('company')},
                'profile': self.linkedin.profile(contact, plan)}

    def contact_scope(self, job, preview):
        ident = job.get('delegated_authority', {}).get('hubspot_contact_id')
        # Current authority is resolved by the application before dispatch; this
        # resolver only binds the explicit contact allowlist to the exact plan.
        if ident is None:
            ident = job.get('delegated_authority', {}).get('contact_id')
        contact = self.scope.contact(ident)
        plan = preview.get('plan', {})
        require('linkedin' in contact.channels and plan.get('recipient_public_id') == contact.linkedin_public_id
            and plan.get('recipient_profile_urn') == contact.linkedin_profile_urn, 'contact_outside_canary')
        return ContactScope(contact.contact_id, contact.linkedin_profile_urn, contact.email, contact.email_providers[0])


class CanarySEQDClient:
    """The existing SEQD transport, narrowed to the self-email canary.

    Preparation is side-effect free. Calls to send still require the console's
    signed exact-preview envelope; this wrapper additionally re-reads suppression
    immediately before delegating to the existing authenticated transport.
    """
    def __init__(self, readers, client=None):
        self.readers, self.client = readers, client or SEQDClient()
        self.repository = getattr(readers, 'repository', None)
        self.message_ids, self.thread_ids, self.effect_keys = set(), set(), set()
        self.message_threads = {}

    def status(self):
        return self.client.status()

    def contact_search(self, address, cursor=None):
        target = self.readers.scope.email(address)
        require('email' in target.channels, 'contact_outside_canary')
        result = self.client.contact_search(address, cursor)
        # SEQDClient validates these exact-email results and their IDs. Keep the
        # observed namespace so the canary cannot become a global inbox browser.
        self.message_ids.update(row['id'] for row in result['messages'])
        self.thread_ids.update(row['thread_id'] for row in result['messages'])
        self.message_threads.update({row['id']:row['thread_id'] for row in result['messages']})
        return result

    def messages(self, limit=10, cursor=None):
        raise SEQDError('canary_global_inbox_disabled')

    def read(self, identifier):
        require(identifier in self.message_ids, 'message_outside_canary')
        return self.client.read(identifier)

    def conversation(self, identifier):
        require(identifier in self.thread_ids, 'conversation_outside_canary')
        return self.client.conversation(identifier)

    def read_context(self, identifier):
        """Context has its own completeness flags and can never be a receipt.

        This explicit read uses the existing MIME-aware thread endpoint. It does
        not turn an error from strict read() into proof that a message was read.
        """
        require(identifier in self.message_threads, 'message_outside_canary')
        result = self.conversation(self.message_threads[identifier])
        thread = result.get('thread', {})
        rows = thread.get('messages', [])
        matches = [row for row in rows if row.get('id') == identifier]
        require(len(matches) == 1 and thread.get('context_only') is True
            and thread.get('receipt_eligible') is False, 'canary_context_unavailable')
        message = matches[0]
        return {'status':'ok', 'context_only':True, 'receipt_eligible':False,
            'message':message, 'complete':thread.get('complete') is True and message.get('complete') is True,
            'truncated':thread.get('truncated'), 'coverage':thread.get('coverage')}

    def reconcile(self, key):
        if key not in self.effect_keys and self.repository is not None:
            attempt = self.repository.get_state('seqd_attempt', key)
            preview = self.repository.get_state('seqd_preview', (attempt or {}).get('preview_id', ''))
            require(preview and preview.get('effect_key') == key
                and preview.get('plan', {}).get('to') == MAILBOX, 'attempt_outside_canary')
            self.readers.scope.email(MAILBOX)
        else:
            require(key in self.effect_keys, 'attempt_outside_canary')
        return self.client.reconcile(key)

    def send(self, envelope):
        target = self.readers.scope.email(envelope.get('to'))
        require('email' in target.channels and target.email == MAILBOX, 'contact_outside_canary')
        self.readers.check_email(target.email)
        key = envelope.get('idempotency_key')
        require(isinstance(key, str) and _id(key), 'canary_attempt_invalid')
        self.effect_keys.add(key)
        result = self.client.send(envelope)
        if isinstance(result, dict) and isinstance(result.get('provider_id'), str):
            self.message_ids.add(result['provider_id'])
        return result


def build_canary_readers(config, *, owner_public_id, linkedin_client=None):
    """Read-only composition, also available while all effect gates are off."""
    from .instantly_reader import private_text
    def credential(path):
        require(isinstance(path, str) and Path(path).is_absolute() and not Path(path).is_symlink(),
                'canary_credential_reference_invalid')
        return partial(private_text, path)
    scope = CanaryScope(config, owner_public_id=owner_public_id)
    hub = config.get('hubspot', {})
    require(isinstance(hub, dict) and set(hub) <= {'credential_file'} and PORTAL_ID == '51412240',
            'canary_config_invalid')
    loader = credential(hub['credential_file']) if hub.get('credential_file') else systemd_hubspot_credential
    hubspot = HubSpotCanaryReader(scope, HubSpotHTTP(loader))
    linkedin = ConnectSafelyCanaryReader(scope, linkedin_client) if linkedin_client is not None else None
    providers = {}
    for name, cls, ident in (('apollo', ApolloCanaryReader, 'team_id'),
                            ('instantly', InstantlyCanaryReader, 'workspace_id')):
        conf = config.get(name)
        if conf is None:
            continue
        require(isinstance(conf, dict) and set(conf) == {ident, 'credential_file'}, 'canary_config_invalid')
        providers[name] = cls(scope, conf[ident], credential(conf['credential_file']))
    require(all(name in providers for contact in scope.by_id.values() for name in contact.email_providers),
            'canary_email_reader_unavailable')
    return CanaryReaders(scope, hubspot, linkedin, providers)


def install_canary_readers(app, repository, cfg):
    """Executable live wiring. No config means no additional authority or IO."""
    config = cfg.get('live', {}).get('canary_readers')
    if config is None:
        return None
    from .connectsafely_live import credential
    from .connectsafely_client import ConnectSafelyClient
    live = cfg['live']
    current = getattr(getattr(app, 'provider', None), 'client', None)
    account = current.account_id if current is not None else live['account_id']
    public = current.expected_public_id if current is not None else live['public_id']
    owner = current.self_profile_id if current is not None else live['self_profile_id']
    transport = CanaryConnectSafelyHTTPS(account, credential(live['api_credential_file']))
    client = ConnectSafelyClient(account, public, self_profile_id=owner, request=transport)
    readers = build_canary_readers(config, owner_public_id=public,
                                  linkedin_client=client)
    readers.repository = repository
    app.canary_readers = readers
    app.delegated_suppression = readers.suppression
    # Bind through the current repository authority, never a caller-supplied ID.
    def contact_scope(job, preview):
        from .hermes_linkedin_cadence import validate_delegated_plan
        approval = validate_delegated_plan(repository, job['delegated_authority'], preview['plan'], app.clock)
        return readers.contact_scope({**job, 'delegated_authority': {
            **job['delegated_authority'], 'hubspot_contact_id': approval['hubspot_contact_id']}}, preview)
    app.delegated_contact_scope = contact_scope
    return readers


def main(argv=None):
    """Reproducible read-only canary probe. There is deliberately no send flag."""
    parser = argparse.ArgumentParser(description='Read-only canary coverage; never dispatches')
    parser.add_argument('--config', required=True, help='Absolute private canary-readers JSON (no embedded secrets)')
    parser.add_argument('--owner-public-id', required=True)
    parser.add_argument('--email', required=True)
    parser.add_argument('--read-context', action='store_true', help='Probe SEQD status and one observed thread without printing content')
    args = parser.parse_args(argv)
    report = {'read_only':True, 'writes':0, 'send_enabled':False, 'sources':{}}
    try:
        path = Path(args.config)
        require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and path.stat().st_size <= 32768, 'canary_config_invalid')
        readers = build_canary_readers(json.loads(path.read_text()), owner_public_id=args.owner_public_id)
        contact = readers.scope.email(args.email)
        require('email' in contact.channels, 'contact_outside_canary')
        started = time.monotonic()
        for name, reader, target in [('hubspot', readers.hubspot.optout, contact.contact_id), *[
                (name, readers.email_readers[name], contact.email) for name in contact.email_providers]]:
            try:
                report['sources'][name] = reader(target)
            except Exception as exc:
                code = str(exc) if re.fullmatch(r'(?:canary|hubspot)_[a-z_]{1,100}', str(exc)) else 'provider_read_failed'
                report['sources'][name] = {'complete':False, 'error_code':code}
                break  # No retries or continuing after a failed/rate-limited read.
        expected = {'hubspot', *contact.email_providers}
        clear = set(report['sources']) == expected and all(item.get('complete') is True
            and item.get('suppressed') is False for item in report['sources'].values())
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['status'] = 'observed_clear' if clear and report['elapsed_seconds'] <= 30 else 'blocked'
        if args.read_context:
            port = CanarySEQDClient(readers)
            try:
                status = port.status()
                page = port.contact_search(contact.email)
                seqd = {'status':status.get('status'), 'send_enabled':status.get('send_enabled'),
                    'listing_complete':page.get('coverage', {}).get('listing_complete'),
                    'messages_returned':len(page.get('messages', []))}
                if page.get('messages'):
                    context = port.read_context(page['messages'][0]['id'])
                    seqd.update(context_complete=context['complete'], truncated=context['truncated'],
                        context_only=True, receipt_eligible=False, coverage_reasons=context.get('coverage', {}).get('reasons'))
                report['sources']['seqd'] = seqd
            except Exception as exc:
                report['sources']['seqd'] = {'complete':False,
                    'error_code':str(exc) if re.fullmatch(r'(?:seqd|canary)_[a-z_]{1,100}', str(exc)) else 'provider_read_failed'}
                report['status'] = 'blocked'
        print(json.dumps(report, sort_keys=True))
        return 0 if report['status'] == 'observed_clear' else 2
    except Exception as exc:
        report['status'] = 'blocked'
        report['error_code'] = str(exc) if re.fullmatch(r'canary_[a-z_]{1,100}', str(exc)) else 'canary_preflight_unavailable'
        print(json.dumps(report, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
