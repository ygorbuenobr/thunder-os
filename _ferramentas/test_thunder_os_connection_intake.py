import unittest
from datetime import datetime, timezone

from thunder_os.connection_intake import (
    ConnectionIntakeError, classify_connections, collect_recent_connections, profile_key,
)


NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


class ConnectionIntakeTests(unittest.TestCase):
    def classify(self, rows, **overrides):
        options = dict(activation_at=NOW, seen=set(), account_by_profile={},
                       contacted=set(), coverage_complete=True, baseline=False)
        options.update(overrides)
        return classify_connections(rows, **options)

    def test_only_new_mapped_connection_is_ready_for_research(self):
        url = "https://www.linkedin.com/in/diego-example?trk=feed"
        decisions, seen = self.classify(
            [{"profileUrl": url, "connectedDate": "Conexão feita em 5 de outubro de 2026"}],
            account_by_profile={url: "mazer.example"})
        self.assertEqual("research", decisions[0]["status"])
        self.assertEqual("mazer.example", decisions[0]["account_id"])
        self.assertEqual({profile_key(url)}, seen)
        self.assertEqual([], self.classify(
            [{"profileUrl": url, "connectedDate": "Conexão feita em 5 de outubro de 2026"}],
            seen=seen, account_by_profile={url: "mazer.example"})[0])

    def test_baseline_old_and_already_contacted_do_not_request_copy(self):
        old = "https://linkedin.com/in/old"
        contacted = "https://linkedin.com/in/contacted"
        decisions, seen = self.classify([
            {"profileUrl": old, "connectedDate": "2026-10-04T13:00:00Z"},
            {"profileUrl": contacted, "connectedDate": "2026-10-05T13:00:00Z"},
        ], contacted={contacted}, account_by_profile={contacted: "acme.example"})
        self.assertEqual(["already_contacted"], [row["status"] for row in decisions])
        self.assertEqual(2, len(seen))

    def test_chattie_connection_without_invite_is_not_lost(self):
        decisions, _ = self.classify([
            {"profileUrl": "https://linkedin.com/in/chattie", "connectedDate": 1791205200000}
        ])
        self.assertEqual("needs_identity", decisions[0]["status"])
        self.assertNotIn("account_id", decisions[0])

    def test_first_complete_poll_only_establishes_baseline(self):
        row = {"profileUrl": "https://linkedin.com/in/preexisting",
               "connectedDate": "Conexão feita em 5 de outubro de 2026"}
        decisions, seen = self.classify([row], baseline=True)
        self.assertEqual([], decisions)
        self.assertEqual({profile_key(row["profileUrl"])}, seen)
        self.assertEqual([], self.classify([row], seen=seen)[0])

    def test_first_poll_keeps_connections_from_a_later_day(self):
        row = {"profileUrl": "https://linkedin.com/in/tomorrow",
               "connectedDate": "Conexão feita em 6 de outubro de 2026"}
        decisions, _ = self.classify([row], baseline=True)
        self.assertEqual("needs_identity", decisions[0]["status"])

    def test_provider_calendar_handles_utc_day_boundary(self):
        activation = datetime(2026, 10, 6, 2, 30, tzinfo=timezone.utc)
        row = {"profileUrl": "https://linkedin.com/in/evening",
               "connectedDate": "Conexão feita em 5 de outubro de 2026"}
        decisions, _ = self.classify([row], activation_at=activation)
        self.assertEqual("needs_identity", decisions[0]["status"])

    def test_equivalent_encoded_url_is_already_contacted(self):
        row = {"profileUrl": "https://linkedin.com/in/%61lice",
               "connectedDate": "Conexão feita em 5 de outubro de 2026"}
        decisions, _ = self.classify([row],
            account_by_profile={"https://linkedin.com/in/alice": "acme.example"},
            contacted={"https://www.linkedin.com/in/alice"})
        self.assertEqual("already_contacted", decisions[0]["status"])

    def test_partial_window_and_bad_data_fail_before_cursor_advance(self):
        original = {"https://linkedin.com/in/existing"}
        row = {"profileUrl": "https://linkedin.com/in/new", "connectedDate": "2026-10-05T13:00:00Z"}
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_window_incomplete"):
            self.classify([row], seen=original, coverage_complete=False)
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_window_incomplete"):
            self.classify([row], coverage_complete=1)
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_date_invalid"):
            self.classify([row, {"profileUrl": "https://linkedin.com/in/other", "connectedDate": None}], seen=original)
        self.assertEqual({"https://linkedin.com/in/existing"}, original)

    def test_collects_real_provider_shape_until_older_day(self):
        seen_cursors = []
        def fetch(cursor):
            seen_cursors.append(cursor)
            return {"connections": [
                {"profileUrl": "https://linkedin.com/in/new", "connectedDate": "Conexão feita em 5 de outubro de 2026"},
                {"profileUrl": "https://linkedin.com/in/old", "connectedDate": "Conexão feita em 4 de outubro de 2026"},
            ], "endOfList": False, "nextStartIndex": 2, "success": True,
                "accountId": "acct-1", "sortBy": "recentlyAdded", "startIndex": cursor, "count": 2}
        rows = collect_recent_connections(fetch, account_id="acct-1", activation_at=NOW)
        self.assertEqual(2, len(rows))
        self.assertEqual([0], seen_cursors)

    def test_partial_or_out_of_order_provider_window_fails_closed(self):
        def partial(cursor):
            return {"connections": [{"profileUrl": f"https://linkedin.com/in/p{cursor}",
                     "connectedDate": "Conexão feita em 5 de outubro de 2026"}],
                    "endOfList": False, "nextStartIndex": cursor + 1, "success": True,
                    "accountId": "acct-1", "sortBy": "recentlyAdded", "startIndex": cursor, "count": 1}
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_window_incomplete"):
            collect_recent_connections(partial, account_id="acct-1", activation_at=NOW, max_pages=2)
        def unordered(cursor):
            return {"connections": [
                {"profileUrl": "https://linkedin.com/in/old", "connectedDate": "Conexão feita em 4 de outubro de 2026"},
                {"profileUrl": "https://linkedin.com/in/new", "connectedDate": "Conexão feita em 5 de outubro de 2026"},
            ], "endOfList": True, "nextStartIndex": None, "success": True,
                "accountId": "acct-1", "sortBy": "recentlyAdded", "startIndex": cursor, "count": 2}
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_order_invalid"):
            collect_recent_connections(unordered, account_id="acct-1", activation_at=NOW)

    def test_provider_account_echo_must_match(self):
        def wrong_account(cursor):
            return {"connections": [], "endOfList": True, "nextStartIndex": None,
                    "success": True, "accountId": "other", "sortBy": "recentlyAdded",
                    "startIndex": cursor, "count": 0}
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_page_invalid"):
            collect_recent_connections(wrong_account, account_id="acct-1", activation_at=NOW)

    def test_provider_cursor_cannot_skip_an_unread_position(self):
        calls = []
        def skipping(cursor):
            calls.append(cursor)
            return {"connections": [{"profileUrl": "https://linkedin.com/in/new",
                     "connectedDate": "Conexão feita em 5 de outubro de 2026"}],
                    "endOfList": False, "nextStartIndex": 2, "success": True,
                    "accountId": "acct-1", "sortBy": "recentlyAdded",
                    "startIndex": cursor, "count": 1}
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_cursor_invalid"):
            collect_recent_connections(skipping, account_id="acct-1", activation_at=NOW)
        self.assertEqual([0], calls)

    def test_conflicting_identity_and_untrusted_urls_fail_closed(self):
        self.assertEqual("https://www.linkedin.com/in/joão-silva",
                         profile_key("https://linkedin.com/in/jo%C3%A3o-silva/"))
        self.assertNotEqual(profile_key("https://linkedin.com/in/straße"),
                            profile_key("https://linkedin.com/in/strasse"))
        self.assertEqual(profile_key(profile_key("https://linkedin.com/in/%C4%B0")),
                         profile_key("https://linkedin.com/in/%C4%B0"))
        with self.assertRaisesRegex(ConnectionIntakeError, "connection_account_ambiguous"):
            self.classify([], account_by_profile={
                "https://linkedin.com/in/same": "a.example",
                "https://www.linkedin.com/in/same?trk=other": "b.example",
            })
        for value in ("http://linkedin.com/in/a", "https://evil.example/in/a", "https://linkedin.com/company/a",
                      "https://[linkedin.com/in/alice", "https://linkedin.com/in/a%2Fb"):
            with self.assertRaisesRegex(ConnectionIntakeError, "connection_profile_invalid"):
                profile_key(value)


if __name__ == "__main__":
    unittest.main()
