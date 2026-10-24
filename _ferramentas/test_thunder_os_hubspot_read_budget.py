"""S27 C1/C2: deterministic fake clocks/providers; all network prohibited."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from thunder_os import hubspot_read_budget as budgets
from thunder_os.cadence_readonly import (CadenceReadonlyError,
    HubSpotReadonlyAdapter, _coverage)
from thunder_os.db import Repository
from thunder_os.hubspot_executor import HubSpotHTTP, HubSpotHTTPError, _https_request
from thunder_os.knowledge import KnowledgeStore
from thunder_os.new_journey_checks import KEYS, NewJourneyChecks, SUPPORTED_ICP
from thunder_os.new_journey_hubspot import HubSpotEligibilityRead
from thunder_os.service import ThunderService
from test_thunder_os_cadence_readonly import Provider, task
from test_thunder_os_new_journey_hubspot import FakeHTTP, EMAIL


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class IsolatedReadBudgetResult(unittest.TextTestResult):
    """Explicit fixture isolation for legacy regression suites, never production.

    Use TextTestRunner(resultclass=IsolatedReadBudgetResult). Each test gets
    fresh admission/cooldown state with the production 60s visibility TTL,
    including when a previous test failed. Critical HTTP schemas always reread.
    This helper does not change transport or socket permissions.
    """
    def startTest(self, test):
        clock = Clock()
        self._scope = patch.object(budgets, 'SHARED_READ_BUDGET', budgets.HubSpotReadBudget(
            monotonic=clock, sleep=clock.sleep))
        self._scope.start()
        super().startTest(test)

    def stopTest(self, test):
        self._scope.stop()
        super().stopTest(test)


class HubSpotReadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.budget = budgets.HubSpotReadBudget(monotonic=self.clock, sleep=self.clock.sleep)
        scoped = patch.object(budgets, 'SHARED_READ_BUDGET', self.budget)
        scoped.start()
        self.addCleanup(scoped.stop)
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.getaddrinfo'):
            blocked = patch(target, side_effect=AssertionError('network forbidden'))
            self.addCleanup(blocked.stop)
            self.addCleanup(blocked.start().assert_not_called)

    def port(self, request, token='synthetic-budget-credential', **kwargs):
        return HubSpotHTTP(lambda: token, request=request, **kwargs)

    def adapter(self, request, token='synthetic-budget-credential', **kwargs):
        return HubSpotReadonlyAdapter(lambda: token, request=request, monotonic=self.clock,
            sleep=self.clock.sleep, **kwargs)

    def test_shared_search_budget_and_429_cooldown(self):
        starts, calls = [], []
        def request(method, path, body, headers, timeout):
            starts.append(self.clock())
            calls.append((path, body, timeout))
            return 429 if len(calls) == 3 else 200, b'{"results": [], "total": 0}'
        one = self.adapter(request).session()
        two = self.port(request)
        path = '/crm/v3/objects/contacts/search'
        two._call('POST', path, {})
        one.call('POST', '/crm/v3/objects/tasks/search', {})
        with self.assertRaises(HubSpotHTTPError) as error:
            # Different key, same pinned account: no separate search allowance.
            self.port(request, 'synthetic-other-key')._call('POST', path, {})
        self.assertEqual(error.exception.status, 429)
        self.assertTrue(all(b - a >= .21 - 1e-12 for a, b in zip(starts, starts[1:])))
        with self.assertRaisesRegex(CadenceReadonlyError, 'read_cooldown'):
            one.call('GET', '/account-info/v3/details')
        self.assertEqual(len(calls), 3)
        self.clock.now += 10
        two._call('POST', path, {})
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(0 < row[2] <= 20 for row in calls))

    def test_schema_cache_and_partial_pages_fail_closed(self):
        provider = Provider()
        provider.pages = {'': {'results': [task()], 'total': 2,
            'paging': {'next': {'after': '200'}}},
            '200': {'results': [task('2')], 'total': 2}}
        one = self.adapter(provider).session()
        two = self.adapter(provider).session()
        with ThreadPoolExecutor(max_workers=8) as pool:
            schemas = list(pool.map(lambda _: self.adapter(provider).session().properties('tasks'), range(8)))
        self.assertEqual(sum('/properties/' in row[1] for row in provider.calls), 1)
        schemas[0].clear()
        self.assertTrue(two.properties('tasks'))
        coverage = _coverage()
        rows = one.pages('POST', '/crm/v3/objects/tasks/search', {'limit': 200}, coverage, 'tasks')
        self.assertEqual(len(rows), 2)
        self.assertTrue(coverage['sources']['tasks']['complete'])
        payloads = [body for _, path, body in provider.calls if path.endswith('/search')]
        self.assertEqual(payloads[1]['after'], 200)
        def limited(method, path, body, headers, timeout):
            if body and 'after' in body:
                return 429, b'{}'
            return provider(method, path, body, headers, timeout)
        coverage = _coverage()
        rows = self.adapter(limited).session().pages('POST', '/crm/v3/objects/tasks/search',
            {'limit': 200}, coverage, 'tasks')
        self.assertEqual(len(rows), 1)
        self.assertFalse(coverage['sources']['tasks']['complete'])
        self.assertEqual(coverage['sources']['tasks']['http_status'], 429)
        self.assertTrue(coverage['partial'])

    def test_cooldown_and_daily_limit_cross_keys_do_not_retry(self):
        calls = []
        def daily(*args):
            calls.append(args[1])
            return 429, b'{"policyName":"DAILY"}', {'Retry-After': '1'}
        with self.assertRaises(HubSpotHTTPError):
            self.port(daily)._call('GET', '/account-info/v3/details')
        self.clock.now = 864000
        with self.assertRaisesRegex(HubSpotHTTPError, 'hubspot_read_daily_limit'):
            self.port(daily, 'another-synthetic-key')._call('GET', '/account-info/v3/details')
        self.assertEqual(len(calls), 1)

    def test_retry_after_extends_cooldown_and_missing_or_bad_header_is_bounded(self):
        for retry_after, advance in [('40', 39), ('garbage', 9), ('-1', 9), ('Infinity', 9)]:
            with self.subTest(retry_after=retry_after):
                budget = budgets.HubSpotReadBudget(monotonic=self.clock, sleep=self.clock.sleep)
                calls = []
                def request(*args):
                    calls.append(args)
                    return 429, b'{}', {'rEtRy-AfTeR': retry_after}
                port = self.port(request, read_budget=budget)
                with self.assertRaises(HubSpotHTTPError):
                    port._call('GET', '/account-info/v3/details')
                self.clock.now += advance
                with self.assertRaisesRegex(HubSpotHTTPError, 'hubspot_read_cooldown'):
                    port._call('GET', '/account-info/v3/details')
                self.assertEqual(len(calls), 1)

    def test_pacing_wait_debits_original_deadline_and_request_cap(self):
        provider = Provider()
        session = self.adapter(provider, max_requests=1).session()
        session.call('GET', '/account-info/v3/details')
        with self.assertRaisesRegex(CadenceReadonlyError, 'read_budget_exhausted'):
            session.call('GET', '/account-info/v3/details')
        second = self.adapter(provider).session()
        second.deadline = self.clock() + .05
        with self.assertRaisesRegex(CadenceReadonlyError, 'read_budget_exhausted'):
            second.call('GET', '/account-info/v3/details')
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.clock(), 0)
        self.assertEqual(self.clock.sleeps, [])

    def test_read_pacing_reduces_transport_timeout(self):
        calls = []
        def request(*args):
            calls.append(args[-1])
            return 200, b'{}'
        port = self.port(request, timeout=.5)
        port._call('GET', '/account-info/v3/details')
        port._call('GET', '/account-info/v3/details')
        self.assertAlmostEqual(calls[1], .39)

    def test_lock_wait_has_bounded_deadline(self):
        self.budget._admission.acquire()
        try:
            with self.assertRaisesRegex(budgets.HubSpotReadBudgetError, 'read_budget_exhausted'):
                self.budget.call('fake', 'GET', '/account-info/v3/details', None, {}, .001,
                    lambda *args: self.fail('transport must not run'))
        finally:
            self.budget._admission.release()

    def test_partial_page_due_to_budget_is_not_complete(self):
        provider = Provider()
        provider.pages = {'': {'results': [task()], 'total': 2, 'paging': {'next': {'after': '200'}}}}
        session = self.adapter(provider, max_requests=1).session()
        coverage = _coverage()
        session.pages('POST', '/crm/v3/objects/tasks/search', {'limit': 200}, coverage, 'tasks')
        self.assertFalse(coverage['sources']['tasks']['complete'])
        self.assertEqual(coverage['sources']['tasks']['error'], 'read_budget_exhausted')

    def test_schema_cache_ttl_from_start_not_completion_and_never_stale_on_failure(self):
        calls = []
        raw = b'{"results":[{"name":"name"}]}'
        def request(*args):
            calls.append(args)
            if len(calls) == 1:
                self.clock.now += 2
                return 200, raw
            return 503, b'{}'
        one = self.adapter(request)
        one.session().call('GET', '/crm/v3/properties/contacts')
        self.clock.now = 59.99
        copy = self.adapter(request).session().call('GET', '/crm/v3/properties/contacts')
        copy['results'].clear()
        self.assertTrue(one.session().call('GET', '/crm/v3/properties/contacts')['results'])
        self.assertEqual(len(calls), 1)
        self.clock.now = 60
        with self.assertRaises(CadenceReadonlyError):
            one.session().call('GET', '/crm/v3/properties/contacts')
        self.assertEqual(len(calls), 2)

    def test_cache_key_separates_credentials_object_types_and_has_bounded_entries(self):
        calls = []
        budget = budgets.HubSpotReadBudget(monotonic=self.clock, sleep=self.clock.sleep,
            max_credentials=2, max_schemas=1)
        def request(*args):
            calls.append(args[1])
            return 200, b'{"results":[]}'
        first = self.adapter(request, read_budget=budget)
        first.session().call('GET', '/crm/v3/properties/contacts')
        self.adapter(request, 'different-key', read_budget=budget).session().call('GET', '/crm/v3/properties/contacts')
        first.session().call('GET', '/crm/v3/properties/tasks')
        first.session().call('GET', '/crm/v3/properties/contacts')
        self.adapter(request, 'third-key', read_budget=budget).session().call('GET', '/crm/v3/properties/contacts')
        self.assertEqual(len(calls), 5)
        self.assertLessEqual(len(budget._states), 2)
        self.assertTrue(all(len(s.schemas) <= 1 for s in budget._states.values()))
        self.assertNotIn('different-key', repr(budget._states))
        self.assertAlmostEqual(self.clock(), .44)

    def test_incomplete_malformed_duplicate_or_secret_schema_not_cached(self):
        values = [{'results': [], 'paging': {'next': {'after': '1'}}},
            {'results': [], 'errors': [{'message': 'partial'}]},
            {'results': [], 'status': 'PENDING'},
            {'results': [{'name': 'same'}, {'name': 'same'}]},
            {'results': [{'name': 'synthetic-budget-credential'}]}, {'results': [None]}]
        for value in values:
            with self.subTest(value=value):
                calls = []
                def request(*args):
                    calls.append(args)
                    return 200, json.dumps(value).encode()
                port = self.adapter(request)
                for _ in range(2):
                    port.session().call('GET', '/crm/v3/properties/contacts')
                self.assertEqual(len(calls), 2)

    def test_schema_singleflight_between_visibility_adapters_preserves_mutation_isolation(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def request(*args):
            calls.append(args)
            entered.set()
            self.assertTrue(release.wait(2))
            return 200, b'{"results":[{"name":"email"}]}'
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.adapter(request).session().call, 'GET', '/crm/v3/properties/contacts')
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.adapter(request).session().properties, 'contacts')
            release.set()
            self.assertEqual(first.result()['results'], [{'name': 'email'}])
            self.assertEqual(second.result(), {'email'})
        self.assertEqual(len(calls), 1)

    def test_no_record_association_search_or_portal_cache(self):
        calls = []
        def request(*args):
            calls.append(args)
            return 200, b'{"results":[],"total":0}'
        port = self.port(request)
        for method, path in [('GET', '/account-info/v3/details'),
                ('GET', '/crm/v3/objects/contacts/123?properties=email'),
                ('GET', '/crm/v4/objects/contacts/123/associations/deals?limit=500'),
                ('POST', '/crm/v3/objects/contacts/search')]:
            for _ in range(2):
                port._call(method, path, {} if method == 'POST' else None)
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(not state.schemas for state in self.budget._states.values()))

    def test_writes_keep_existing_allowlist_single_attempt_and_no_read_pacing(self):
        calls = []
        def request(*args):
            calls.append(args)
            return 200, b'{"id":"123"}', {'Retry-After': None}
        port = self.port(request)
        for _ in range(2):
            port._call('PATCH', '/crm/v3/objects/companies/123', {'properties': {'name': 'Synthetic'}})
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.clock.sleeps, [])
        for method, path in [('DELETE', '/crm/v3/objects/companies/123'),
                ('POST', '/crm/v3/properties/companies'), ('PATCH', '/crm/v3/objects/tasks/123')]:
            with self.assertRaisesRegex(HubSpotHTTPError, 'hubspot_endpoint_blocked'):
                port._call(method, path, {})
        self.assertEqual(len(calls), 2)

    def test_account_search_budget_serializes_concurrent_different_credentials(self):
        starts = []
        def request(*args):
            starts.append(self.clock())
            return 200, b'{"results":[],"total":0}'
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.port(request, 'synthetic-key-' + str(n))._call(
                'POST', '/crm/v3/objects/contacts/search', {}), range(24)))
        self.assertEqual(len(starts), 24)
        self.assertTrue(all(b - a >= .21 - 1e-12 for a, b in zip(starts, starts[1:])))

    def test_global_schema_byte_bound(self):
        raw = json.dumps({'results': [{'name': 'name', 'description': 'x' * 950000}]}).encode()
        for index in range(9):
            self.adapter(lambda *args: (200, raw), 'synthetic-' + str(index)).session().call(
                'GET', '/crm/v3/properties/contacts')
        cached = [v for state in self.budget._states.values() for v in state.schemas.values()]
        self.assertLessEqual(sum(len(v[1]) for v in cached), 8_000_000)
        self.assertEqual(len(cached), 8)

    def test_429_invalidates_fresh_schema_and_blocks_cache_hit_without_network(self):
        calls = []
        def request(method, path, *args):
            calls.append(path)
            return (200, b'{"results":[]}') if '/properties/' in path else (429, b'{}')
        port = self.port(request)
        self.adapter(request).session().call('GET', '/crm/v3/properties/contacts')
        with self.assertRaises(HubSpotHTTPError):
            port._call('POST', '/crm/v3/objects/contacts/search', {})
        with self.assertRaisesRegex(CadenceReadonlyError, 'read_cooldown'):
            self.adapter(request).session().call('GET', '/crm/v3/properties/contacts')
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(not state.schemas for state in self.budget._states.values()))

    def test_https_transport_preserves_pair_unpacking_and_cooldown_headers(self):
        response = SimpleNamespace(status=429, read=lambda limit: b'{}',
            getheader=lambda name: '35')
        connection = SimpleNamespace(request=lambda *args, **kwargs: None,
            getresponse=lambda: response, close=lambda: None)
        with patch('http.client.HTTPSConnection', return_value=connection):
            pair = _https_request('GET', '/account-info/v3/details', None, {}, 1)
            status, raw = pair
            self.assertEqual((status, raw), (429, b'{}'))
            port = self.port(lambda *args: pair)
            with self.assertRaises(HubSpotHTTPError):
                port._call('GET', '/account-info/v3/details')
            self.clock.now = 34
            with self.assertRaisesRegex(HubSpotHTTPError, 'hubspot_read_cooldown'):
                port._call('GET', '/account-info/v3/details')

    def test_wrapper_keeps_shared_admission_and_fresh_schema_and_raw_transport_identity(self):
        original_http = FakeHTTP()
        original = HubSpotHTTP(original_http.loader, request=original_http)
        one, two = HubSpotEligibilityRead(original), HubSpotEligibilityRead(original)
        self.assertIs(one.port._read_budget, original._read_budget)
        self.assertIs(two.port._read_budget, original._read_budget)
        self.assertIsNot(one.port._request, original._request)
        self.assertIs(original._request, original_http)
        one.lookup(EMAIL)
        calls = len(original_http.calls)
        self.assertEqual(sum('/properties/' in row[1] for row in original_http.calls), 3)
        two.lookup(EMAIL)
        self.assertGreater(len(original_http.calls), calls)
        self.assertEqual(sum('/properties/' in row[1] for row in original_http.calls), 6)
        self.assertIs(original._request, original_http)

    def test_slow_credential_loader_cannot_issue_request_after_session_deadline(self):
        calls = []
        def load():
            self.clock.now += 2
            return 'synthetic'
        session = HubSpotReadonlyAdapter(load, monotonic=self.clock, budget_seconds=1,
            read_budget=self.budget, request=lambda *args: calls.append(args)).session()
        with self.assertRaisesRegex(CadenceReadonlyError, 'read_budget_exhausted'):
            session.call('GET', '/account-info/v3/details')
        self.assertEqual(calls, [])

    def test_critical_http_schema_fresh_while_visibility_schema_is_cached(self):
        http = FakeHTTP()
        critical = HubSpotHTTP(http.loader, request=http)
        visibility = HubSpotReadonlyAdapter(http.loader, request=http, monotonic=self.clock,
            sleep=self.clock.sleep)
        path = '/crm/v3/properties/contacts'
        first = visibility.session().call('GET', path)
        field = next(p for p in http.schemas['contacts']['results'] if p['name'] == 'campanha_ativa')
        field['type'] = 'number'
        self.assertEqual(visibility.session().call('GET', path), first)
        self.assertEqual(len(http.calls), 1)
        current = critical.metadata('contacts')
        self.assertEqual(current['properties']['campanha_ativa']['type'], 'number')
        reader = HubSpotEligibilityRead(critical).lookup(EMAIL)
        self.assertFalse(reader['property_coverage']['contacts']['campanha_ativa'])
        # A second mutation within the same visibility TTL must also be seen.
        field['type'] = 'string'
        http.schemas['contacts']['results'] = [p for p in http.schemas['contacts']['results']
            if p['name'] != 'executor']
        current = critical.metadata('contacts')
        self.assertEqual(current['properties']['campanha_ativa']['type'], 'string')
        self.assertNotIn('executor', current['properties'])
        self.assertEqual(visibility.session().call('GET', path), first)

    def test_fixture_result_isolates_429_between_independent_cases(self):
        seen = []
        class Limited(unittest.TestCase):
            def runTest(case):
                port = HubSpotHTTP(lambda: 'same-fixture-token', request=lambda *args:
                    (429, b'{"policyName":"DAILY"}'))
                with case.assertRaises(HubSpotHTTPError):
                    port._call('GET', '/account-info/v3/details')
        class Healthy(unittest.TestCase):
            def runTest(case):
                port = HubSpotHTTP(lambda: 'same-fixture-token', request=lambda *args: (200, b'{}'))
                seen.append(port._call('GET', '/account-info/v3/details'))
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=IsolatedReadBudgetResult).run(
            unittest.TestSuite([Limited(), Healthy()]))
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 2)
        self.assertEqual(seen, [{}])

    def test_schema_expired_during_request_is_never_cached(self):
        calls = []
        def request(*args):
            calls.append(args)
            self.clock.now += 61
            return 200, b'{"results":[]}'
        for _ in range(2):
            self.budget.call('synthetic', 'GET', '/crm/v3/properties/contacts', None, {}, 90, request)
        self.assertEqual(len(calls), 2)

    def test_complete_checks_become_unknown_on_partial_membership_and_deals_with_visibility_cache(self):
        import test_thunder_os_new_journey_eligibility as fixture
        with tempfile.TemporaryDirectory() as directory:
            brain = Path(directory) / 'brain'
            brain.mkdir()
            repo = Repository(Path(directory) / 'test.db')
            service = ThunderService(repo, knowledge_store=KnowledgeStore(brain))
            owner = SimpleNamespace()
            fixture.assemble(owner, repo, service, brain, 0)
            visibility = HubSpotReadonlyAdapter(owner.http.loader, request=owner.http,
                monotonic=self.clock, sleep=self.clock.sleep)
            visibility.session().call('GET', '/crm/v3/properties/contacts')
            row = {'source_key': 'csv:1', 'email': fixture.EMAIL, 'linkedin_url': fixture.PROFILE}
            def check():
                result = owner.checker([row], 'BR-APOLLO-FIXTURE')[0]
                return result, {c['key']: c['status'] for c in result['checks']}
            first, checks = check()
            self.assertTrue(all(status == 'pass' for status in checks.values()))
            count = sum('/properties/' in call[1] for call in owner.http.calls)
            owner.provider_partial = True
            result, checks = check()
            self.assertEqual(checks['membership'], 'unknown')
            self.assertTrue(result['retained'])
            self.assertEqual(checks['open_deal'], 'pass')
            owner.http.companies['500']['associations']['deals']['paging'] = {'next': {'after': '200'}}
            result, checks = check()
            self.assertEqual(checks['open_deal'], 'unknown')
            self.assertEqual(checks['membership'], 'unknown')
            self.assertTrue(result['retained'])
            self.assertEqual(sum('/properties/' in call[1] for call in owner.http.calls), count + 6)
            owner.http.deals['4']['properties']['hs_is_closed'] = 'false'
            self.assertEqual(check()[1]['open_deal'], 'blocked')
            owner.http.fail['/crm/v3/objects/contacts/search'] = 429
            result, checks = check()
            self.assertNotEqual(checks['open_deal'], 'pass')
            self.assertNotEqual(checks['membership'], 'pass')
            self.assertTrue(result['retained'])
            self.assertEqual(repo.effects(), [])
            self.assertEqual(repo.outbox(), [])

    def test_cached_schema_cannot_hide_fresh_open_deal_or_incomplete_deals_or_membership(self):
        http = FakeHTTP()
        original = HubSpotHTTP(http.loader, request=http)
        visibility = HubSpotReadonlyAdapter(http.loader, request=http,
            monotonic=self.clock, sleep=self.clock.sleep)
        visibility.session().call('GET', '/crm/v3/properties/contacts')
        first = HubSpotEligibilityRead(original).lookup(EMAIL)
        self.assertTrue(first['coverage']['deals'])
        schema_calls = sum('/properties/' in row[1] for row in http.calls)
        http.deals['4']['properties']['hs_is_closed'] = 'false'
        fresh = HubSpotEligibilityRead(original).lookup(EMAIL)
        self.assertEqual(sum('/properties/' in row[1] for row in http.calls), schema_calls + 3)
        self.assertEqual(next(d for d in fresh['deals'] if d['id'] == '4')['properties']['hs_is_closed'], 'false')
        http.companies['500']['associations']['deals']['paging'] = {'next': {'after': '200'}}
        partial = HubSpotEligibilityRead(original).lookup(EMAIL)
        self.assertFalse(partial['coverage']['deals'])
        with tempfile.TemporaryDirectory() as directory:
            checker = NewJourneyChecks(ThunderService(Repository(Path(directory) / 'test.db')))
            checks = {key: {'status': 'unknown'} for key in KEYS}
            sheet = {'status': 'complete', 'fields': [{'key': 'icp', 'content': SUPPORTED_ICP}]}
            checker._canonical_checks(checks, partial, sheet,
                datetime(2026, 10, 24, tzinfo=timezone.utc), 0, {})
            self.assertEqual(checks['open_deal']['status'], 'blocked')
            self.assertEqual(checks['membership']['status'], 'unknown')
            partial['deals'] = []
            checks = {key: {'status': 'unknown'} for key in KEYS}
            checker._canonical_checks(checks, partial, sheet,
                datetime(2026, 10, 24, tzinfo=timezone.utc), 0, {})
            self.assertEqual(checks['open_deal']['status'], 'unknown')
        http.fail['/crm/v3/objects/contacts/search'] = 429
        incomplete = HubSpotEligibilityRead(original).lookup(EMAIL)
        self.assertEqual(incomplete['status'], 'unknown')
        self.assertFalse(incomplete['coverage']['identity'])


if __name__ == '__main__':
    unittest.main()
