import json
import unittest
from datetime import datetime, timezone

from thunder_os.connection_intake import collect_recent_connections
from thunder_os.connection_provider import (
    ConnectionProvider, ConnectionProviderError, MAX_RESPONSE_BYTES,
)


ACCOUNT = "a" * 24
OTHER_ACCOUNT = "b" * 24
PUBLIC_ID = "owner-example"
NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def row(number, date="Connected on Oct 5, 2026"):
    vanity = f"person-{number}"
    return {"vanityName": vanity, "profileUrl": f"https://www.linkedin.com/in/{vanity}/",
            "connectedDate": date, "firstName": "Example", "headline": "Engineer"}


def page(start, rows, *, end=False, account=ACCOUNT, next_index=None):
    return {"success": True, "accountId": account, "sortBy": "recentlyAdded",
            "startIndex": start, "limit": 10, "count": len(rows), "connections": rows,
            "endOfList": end,
            "nextStartIndex": start + len(rows) if next_index is None else next_index}


class FakeTransport:
    def __init__(self, pages=(), *, accounts=None):
        self.pages = list(pages)
        self.accounts = ([{"id": ACCOUNT, "publicId": PUBLIC_ID}] if accounts is None
                         else accounts)
        self.calls = []

    def __call__(self, method, path, query, body, *, timeout):
        self.calls.append((method, path, query, body, timeout))
        if path == "/linkedin/accounts":
            result = 200, self.accounts
        elif path == "/linkedin/connections":
            result = self.pages.pop(0)
        else:
            raise AssertionError("unexpected route")
        status, payload = result
        return status, json.dumps(payload).encode()


class ConnectionProviderTests(unittest.TestCase):
    def provider(self, transport):
        return ConnectionProvider(ACCOUNT, PUBLIC_ID, transport, timeout_seconds=4)

    def test_full_page_stays_open(self):
        fake = FakeTransport([(200, page(0, [row(i) for i in range(10)]))])
        result = self.provider(fake).fetch_page(0)
        self.assertEqual(10, result["count"])
        self.assertFalse(result["endOfList"])
        self.assertEqual(10, result["nextStartIndex"])
        self.assertEqual("2026-10-05T00:00:00-03:00",
                         result["connections"][0]["connectedDate"])
        self.assertEqual(["/linkedin/accounts", "/linkedin/connections"],
                         [call[1] for call in fake.calls])
        self.assertTrue(all(call[0] == "GET" and call[3] is None and call[4] == 4
                            for call in fake.calls))
        self.assertEqual({"accountId": ACCOUNT, "startIndex": 0,
                          "limit": 10, "sortBy": "recentlyAdded"}, fake.calls[1][2])

    def test_provider_portuguese_date_observed_on_active_account(self):
        fake = FakeTransport([(200, page(0, [row(1, "Conexão feita em 5 de outubro de 2026")], end=True))])
        result = self.provider(fake).fetch_page(0)
        self.assertEqual("2026-10-05T00:00:00-03:00",
                         result["connections"][0]["connectedDate"])

    def test_provider_full_english_month_observed_on_older_pages(self):
        fake = FakeTransport([(200, page(0, [row(1, "Connected on October 5, 2026"),
                                             row(2, "Connected on September 29, 2026")], end=True))])
        result = self.provider(fake).fetch_page(0)
        self.assertEqual(["2026-10-05T00:00:00-03:00", "2026-09-29T00:00:00-03:00"],
                         [item["connectedDate"] for item in result["connections"]])
        bad_page = (200, page(0, [row(1, "Connected on Smarch 5, 2026")], end=True))
        invalid = FakeTransport([bad_page, bad_page])
        with self.assertRaisesRegex(ConnectionProviderError, "connection_row_invalid"):
            self.provider(invalid).fetch_page(0)

    def test_partial_page_is_not_terminal(self):
        fake = FakeTransport([(200, page(0, [row(1), row(2)]))])
        result = self.provider(fake).fetch_page(0)
        self.assertEqual(2, result["nextStartIndex"])
        self.assertFalse(result["endOfList"])

    def test_pagination_uses_returned_cursor_and_terminal_empty_page(self):
        fake = FakeTransport([(200, page(0, [row(1), row(2)])),
                              (200, page(2, [row(3)])),
                              (200, page(3, [], end=True))])
        result = collect_recent_connections(self.provider(fake).fetch_page,
                                            account_id=ACCOUNT, activation_at=NOW)
        self.assertEqual(3, len(result))
        self.assertEqual([0, 2, 3], [call[2]["startIndex"] for call in fake.calls
                                      if call[1] == "/linkedin/connections"])

    def test_terminal_page_with_rows_is_complete(self):
        fake = FakeTransport([(200, page(0, [row(1)], end=True))])
        result = collect_recent_connections(self.provider(fake).fetch_page,
                                            account_id=ACCOUNT, activation_at=NOW)
        self.assertEqual(1, len(result))
        self.assertEqual(1, sum(call[1] == "/linkedin/connections" for call in fake.calls))

    def test_wrong_account_blocks_before_connections_request(self):
        fake = FakeTransport(accounts=[{"id": OTHER_ACCOUNT, "publicId": PUBLIC_ID}],
                             pages=[(200, page(0, [row(1)]))])
        with self.assertRaisesRegex(ConnectionProviderError, "account_identity_mismatch"):
            self.provider(fake).fetch_page(0)
        self.assertEqual(["/linkedin/accounts"], [call[1] for call in fake.calls])

    def test_wrong_account_on_page_blocks_result(self):
        fake = FakeTransport([(200, page(0, [row(1)], account=OTHER_ACCOUNT))])
        with self.assertRaisesRegex(ConnectionProviderError, "account_identity_mismatch"):
            self.provider(fake).fetch_page(0)

    def test_profile_url_must_match_vanity_name(self):
        invalid = row(1)
        invalid["profileUrl"] = "https://www.linkedin.com/in/someone-else/"
        bad_page = (200, page(0, [invalid]))
        fake = FakeTransport([bad_page, bad_page])
        with self.assertRaisesRegex(ConnectionProviderError, "connection_row_invalid"):
            self.provider(fake).fetch_page(0)

    def test_transient_invalid_row_is_revalidated_at_same_cursor(self):
        invalid = row(1)
        invalid["connectedDate"] = None
        fake = FakeTransport([(200, page(0, [invalid], end=True)),
                              (200, page(0, [row(1)], end=True))])
        result = self.provider(fake).fetch_page(0)
        self.assertEqual(1, result["count"])
        self.assertEqual([0, 0], [call[2]["startIndex"] for call in fake.calls
                                  if call[1] == "/linkedin/connections"])

    def test_persistent_invalid_row_fails_after_one_revalidation(self):
        invalid = row(1)
        invalid["connectedDate"] = None
        fake = FakeTransport([(200, page(0, [invalid], end=True)),
                              (200, page(0, [invalid], end=True))])
        with self.assertRaisesRegex(ConnectionProviderError, "connection_row_invalid"):
            self.provider(fake).fetch_page(0)
        self.assertEqual(2, sum(call[1] == "/linkedin/connections" for call in fake.calls))

    def test_only_one_row_revalidation_per_provider_run(self):
        bad = row(1)
        bad["connectedDate"] = None
        good0 = (200, page(0, [row(1)]))
        bad0 = (200, page(0, [bad]))
        bad1 = (200, page(1, [bad], end=True))
        fake = FakeTransport([bad0, good0, bad1])
        provider = self.provider(fake)
        self.assertEqual(1, provider.fetch_page(0)["count"])
        with self.assertRaisesRegex(ConnectionProviderError, "connection_row_invalid"):
            provider.fetch_page(1)
        self.assertEqual([0, 0, 1], [call[2]["startIndex"] for call in fake.calls
                                     if call[1] == "/linkedin/connections"])

    def test_profile_company_is_bound_to_exact_account_and_profile(self):
        class ProfileTransport(FakeTransport):
            def __call__(self, method, path, query, body, *, timeout):
                if path == "/linkedin/profile":
                    self.calls.append((method, path, query, body, timeout))
                    payload = {"success": True, "accountId": ACCOUNT,
                               "profileId": "person-1", "profile": {
                                   "publicIdentifier": "person-1",
                                   "currentCompany": "Alpha Logistics",
                                   "experience": [{"companyName": "Alpha Logistics"}],
                               }}
                    return 200, json.dumps(payload).encode()
                return super().__call__(method, path, query, body, timeout=timeout)
        fake = ProfileTransport()
        result = self.provider(fake).fetch_profile_company("https://linkedin.com/in/person-1")
        self.assertEqual(("Alpha Logistics", "Alpha Logistics"), result)
        self.assertEqual({"accountId": ACCOUNT, "profileId": "person-1"}, fake.calls[-1][2])
        self.assertTrue(all(call[0] == "GET" and call[3] is None for call in fake.calls))

    def test_accented_public_identifier_is_preserved_and_matched(self):
        accented = row(1)
        accented["vanityName"] = "joão-silva"
        accented["profileUrl"] = "https://www.linkedin.com/in/jo%C3%A3o-silva/"
        fake = FakeTransport([(200, page(0, [accented], end=True))])
        result = self.provider(fake).fetch_page(0)
        self.assertEqual("https://www.linkedin.com/in/joão-silva",
                         result["connections"][0]["profileUrl"])

    def test_unicode_identifier_cannot_escape_profile_path(self):
        invalid = row(1)
        invalid["vanityName"] = "joão/silva"
        invalid["profileUrl"] = "https://www.linkedin.com/in/jo%C3%A3o%2Fsilva/"
        bad_page = (200, page(0, [invalid], end=True))
        fake = FakeTransport([bad_page, bad_page])
        with self.assertRaisesRegex(ConnectionProviderError, "connection_row_invalid"):
            self.provider(fake).fetch_page(0)

    def test_unicode_identifier_does_not_fold_distinct_spellings(self):
        different = row(1)
        different["vanityName"] = "straße"
        different["profileUrl"] = "https://www.linkedin.com/in/strasse/"
        bad_page = (200, page(0, [different], end=True))
        fake = FakeTransport([bad_page, bad_page])
        with self.assertRaisesRegex(ConnectionProviderError, "connection_row_invalid"):
            self.provider(fake).fetch_page(0)

    def test_429_does_not_retry_or_echo_provider_body(self):
        fake = FakeTransport([(429, {"error": "secret-value"})])
        with self.assertRaises(ConnectionProviderError) as caught:
            self.provider(fake).fetch_page(0)
        self.assertEqual("provider_rate_limited", str(caught.exception))
        self.assertEqual(1, sum(call[1] == "/linkedin/connections" for call in fake.calls))

    def test_timeout_does_not_retry_or_echo_exception(self):
        def timed_out(method, path, query, body, *, timeout):
            raise TimeoutError("secret-value")

        with self.assertRaises(ConnectionProviderError) as caught:
            self.provider(timed_out).fetch_page(0)
        self.assertEqual("provider_timeout", str(caught.exception))

    def test_response_size_and_duplicate_keys_fail_closed(self):
        class BadTransport:
            def __init__(self, raw):
                self.raw = raw

            def __call__(self, method, path, query, body, *, timeout):
                return 200, self.raw

        for raw, code in ((b" " * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
                          (b'[{"id":"x","id":"y"}]', "provider_schema_invalid")):
            with self.subTest(code=code):
                with self.assertRaises(ConnectionProviderError) as caught:
                    self.provider(BadTransport(raw)).fetch_page(0)
                self.assertEqual(code, str(caught.exception))

    def test_invalid_cursor_and_false_terminal_flag_fail_closed(self):
        fake = FakeTransport([(200, page(0, [row(1)], next_index=10))])
        with self.assertRaisesRegex(ConnectionProviderError, "pagination_unverified"):
            self.provider(fake).fetch_page(0)
        fake = FakeTransport([(200, page(0, [], end=False))])
        with self.assertRaisesRegex(ConnectionProviderError, "pagination_unverified"):
            self.provider(fake).fetch_page(0)


if __name__ == "__main__":
    unittest.main()
