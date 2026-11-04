import unittest

from thunder_os.apollo_linkedin_reader import CampaignBindings, list_open_tasks


class BindingsTest(unittest.TestCase):
    def setUp(self):
        self.binding = dict(company_id='company', team_id='team', campaign_id='campaign',
                            account_id='account1', public_id='sender', credential_ref='secret-ref', owner_ids=['owner'])
        self.routes = CampaignBindings([self.binding])
        from types import SimpleNamespace
        credential = lambda: 'token'
        credential.credential_ref = 'secret-ref'
        self.client = SimpleNamespace(account_id='account1', expected_public_id='sender', request=SimpleNamespace(account_id='account1', credential=credential))
        self.args = dict(client=self.client, company_id='company', team_id='team', campaign_id='campaign', owner_id='owner')

    def test_exact_binding_and_availability(self):
        calls = []
        def status(account):
            calls.append(account)
            return {'id': account, 'available': True}
        result = self.routes.resolve(**self.args, account_status_reader=status)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['credential_ref'], 'secret-ref')
        self.assertEqual(calls, ['account1'])

    def test_no_silent_fallback_or_unverified_account(self):
        for status in (lambda _: None, lambda _: {'id': 'account2', 'available': True},
                       lambda _: {'id': 'account1', 'available': False}):
            result = self.routes.resolve(**self.args, account_status_reader=status)
            self.assertEqual(result['reason'], 'default_account_unavailable')
            self.assertEqual(result['account_id'], 'account1')
        def failure(_):
            raise RuntimeError('unavailable')
        self.assertEqual(self.routes.resolve(**self.args, account_status_reader=failure)['status'], 'blocked')

    def test_company_team_campaign_owner_are_bound(self):
        for key in ('company_id', 'team_id', 'campaign_id', 'owner_id'):
            args = dict(self.args, **{key: 'other'})
            self.assertEqual(self.routes.resolve(**args, account_status_reader=lambda _: self.fail('status read'))['status'], 'blocked')

    def test_duplicate_and_missing_config_are_rejected(self):
        with self.assertRaises(ValueError):
            CampaignBindings([self.binding, self.binding])
        with self.assertRaises(ValueError):
            CampaignBindings([dict(self.binding, credential_ref='')])

    def test_get_list_unavailable_is_unknown_not_zero(self):
        result = list_open_tasks()
        self.assertEqual(result['coverage'], 'unknown')
        self.assertEqual(result['reason'], 'apollo_get_task_list_unavailable')


if __name__ == '__main__':
    unittest.main()

class ApolloReaderTest(unittest.TestCase):
    def setUp(self):
        import json
        from thunder_os.apollo_linkedin_reader import ApolloLinkedInReader
        self.json = json
        self.calls = []
        self.pages = {}
        def request(method, path, body, headers, timeout):
            self.calls.append((method, path, body))
            if path.endswith('api_profile'):
                return 200, json.dumps({'user': {'id': 'actor', 'team_id': 'team'}}).encode()
            if path.endswith('/search'):
                return 200, json.dumps(self.pages[body['page']]).encode()
            return 200, json.dumps({'task': {'id': path.rsplit('/', 1)[-1]}}).encode()
        self.reader = ApolloLinkedInReader(lambda: 'token', team_id='team', api_actor_id='actor',
                                          owner_ids=['owner'], request=request)
    def page(self, page, total, rows):
        return {'tasks': rows, 'pagination': {'page': page, 'per_page': 100,
                'total_entries': total, 'total_pages': (total + 99) // 100}}
    def row(self, i, **overrides):
        return dict({'id': str(i), 'type': 'linkedin_step_connect', 'status': 'scheduled',
                     'user_id': 'owner'}, **overrides)
    def test_paginates_and_filters_without_false_zero(self):
        self.pages[1] = self.page(1, 101, [self.row(i) for i in range(100)])
        self.pages[2] = self.page(2, 101, [self.row(100, user_id='other')])
        result = self.reader.list_open_tasks()
        self.assertEqual(result['coverage'], 'complete')
        self.assertEqual(len(result['tasks']), 100)
        self.assertEqual([x[2]['page'] for x in self.calls if x[0] == 'POST'], [1, 2])
    def test_failure_and_inconsistent_pagination_leave_coverage_unknown(self):
        self.pages[1] = self.page(1, 101, [self.row(i) for i in range(100)])
        self.assertEqual(self.reader.list_open_tasks()['coverage'], 'unknown')
        self.pages[2] = self.page(2, 102, [self.row(100), self.row(101)])
        result = self.reader.list_open_tasks()
        self.assertEqual(result['coverage'], 'unknown')
        self.assertEqual(result['reason'], 'pagination_incomplete')
        self.pages[2] = self.page(2, 101, [self.row(0)])
        self.assertEqual(self.reader.list_open_tasks()['coverage'], 'unknown')
    def test_empty_result_requires_pagination_evidence(self):
        self.pages[1] = self.page(1, 0, [])
        self.assertEqual(self.reader.list_open_tasks()['coverage'], 'complete')
        del self.pages[1]['pagination']
        self.assertEqual(self.reader.list_open_tasks()['coverage'], 'unknown')
    def test_non_search_mutations_cannot_reach_transport(self):
        paths = ['/api/v1/tasks', '/api/v1/tasks/task/complete', '/api/v1/tasks/search?x=1',
                 '/api/v1/tasks/search/', 'https://api.apollo.io/api/v1/tasks/search']
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            for path in paths + (['/api/v1/tasks/search'] if method != 'POST' else []):
                with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                    self.reader._call(method, path, {'page': 1, 'per_page': 100})
        self.assertEqual(self.calls, [])
    def test_transport_rejects_mutations_before_network(self):
        from thunder_os.apollo_linkedin_reader import _https_request
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            for path in ('/api/v1/tasks', '/api/v1/tasks/search?x=1', '/api/v1/tasks/1/complete'):
                with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                    _https_request(method, path, {'page': 1, 'per_page': 100}, {}, 1)
        for method in ('PUT', 'PATCH', 'DELETE'):
            with self.assertRaises(ValueError):
                _https_request(method, '/api/v1/tasks/search', {'page': 1, 'per_page': 100}, {}, 1)
    def test_missing_owner_is_exposed_to_executor_as_blocker(self):
        row = self.row('task'); row.pop('user_id')
        self.pages[1] = self.page(1, 1, [row])
        self.assertEqual(self.reader.list_open_tasks()['tasks'], [row])
