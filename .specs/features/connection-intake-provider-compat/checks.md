# Connection intake provider compatibility checks

Profile: standard
Plan: `.specs/features/connection-intake-provider-compat/plan.md`

## Intent

8 checks in 2 slices · 0 one-way doors · 0 open

## Checks

### S1 - Complete live observation reaches the activation boundary · 3 files · ~24 KB · ~8k

**C1** - With the default bound, contiguous windows whose first older row is on page 6 or page 20 return 60 or 200 rows and stop at cursor 50 or 190 (CONN-01, AC 1)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v -k test_default_window_reaches_sixth_page_boundary test_thunder_os_connection_intake`
Status: complete

**C2** - With the default bound, 20 contiguous nonterminal pages without an older row raise `connection_window_incomplete` and leave checkpoint, profile and research-request state unchanged (CONN-01, AC 2)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v -k test_default_twenty_page_incomplete_window_is_atomic test_thunder_os_connection_ledger`
Status: complete

**C3** - Provider pagination keeps the configured account, returned contiguous cursor and GET-without-body transport contract (CONN-01, AC 3)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v -k test_full_page_stays_open test_thunder_os_connection_provider`
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v -k test_pagination_uses_returned_cursor_and_terminal_empty_page test_thunder_os_connection_provider`
Status: complete

**C7** - The scheduled live entry point projects a sixth-page success as `status=ok` and a 20-page incomplete window as `status=blocked` with `connection_window_incomplete` (CONN-01, AC 1, 2)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/opt/thunder-os/current/_ferramentas python3 -m unittest -v -k test_main_projects_live_success_and_incomplete_error test_thunder_os_connection_runtime`
Status: complete

### S2 - Provider-confirmed safe Unicode vanity reaches canonical identity · 2 files · ~20 KB · ~7k

**C4** - An exact `U+2234` vanity/decoded-URL pair is accepted, preserved and idempotent, while `straße` and `strasse` remain distinct (CONN-02, AC 4, 6)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v -k test_provider_confirmed_mathematical_symbol_is_preserved test_thunder_os_connection_provider`
Status: complete

**C5** - A mismatched vanity/URL pair remains `connection_row_invalid` after one same-page revalidation (CONN-02, AC 5)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v -k test_profile_url_must_match_vanity_name test_thunder_os_connection_provider`
Status: complete

**C6** - Slugs containing a separator, whitespace, control character or unsupported Unicode punctuation remain `connection_row_invalid` (CONN-02, AC 5)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v -k test_unsafe_unicode_vanity_characters_remain_invalid test_thunder_os_connection_provider`
Status: complete

**C8** - The scheduled backfill entry point projects an accepted terminal page as `status=ok` and an invalid provider row as `status=blocked` with `connection_row_invalid` (CONN-02, AC 4, 5)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/opt/thunder-os/current/_ferramentas python3 -m unittest -v -k test_main_projects_backfill_success_and_row_error test_thunder_os_connection_runtime`
Status: complete

## Coverage

| Set (size) | Member -> proof | Unproven |
| --- | --- | --- |
| default page-bound edges (3) | boundary crossed on page 6 C1 · boundary crossed on page 20 C1 · still incomplete after page 20 C2 | - |
| provider transport invariants (4) | GET C3 · no body C3 · configured account C3 · contiguous returned cursor C3 | - |
| symbol identity outcomes (4) | exact `U+2234` C4 · mismatched vanity/URL C5 · idempotent round trip C4 · distinct Unicode spelling C4 | - |
| unsafe slug classes (4) | path separator C6 · whitespace C6 · control C6 · unsupported punctuation C6 | - |
| scheduled consumers (2) | live entry point C7 · backfill entry point C8 | - |
| `thunder-os-connections-live` outcomes (2) | `status=ok` C7 · `status=blocked`, `connection_window_incomplete` C7 | - |
| `thunder-os-connections-backfill` outcomes (2) | `status=ok` C8 · `status=blocked`, `connection_row_invalid` C8 | - |

- Claims naming an error, transport shape or bound: C1-C6 - every proof asserts at that boundary.
- No check claims more cases than its named proof exercises.

## Test policy

The repository documents the runner and isolation rules but does not specify which level proves
decision code or how much of each input set is required, so these rows are the bar for this build.

| Code | Required proofs | Coverage expectation |
| --- | --- | --- |
| Slug-character decision | one provider-level proof for accepted identity and one table-driven provider proof for rejected classes | each newly admitted class plus every rejection class named by AC 5 |
| Pagination-bound decision | one intake proof at the first newly reachable page and one ledger proof at the unchanged upper bound | page 6 success and page 20 failure, including state atomicity |
| Ledger default forwarding | one ledger-level proof | omission of `max_pages` reaches the 20-page domain default |

Evidence:

- `_ferramentas/thunder_os/connection_intake.py`: slug validation and a bounded pagination loop decide accepted identity and complete versus incomplete coverage.
- `_ferramentas/thunder_os/connection_provider.py`: row validation decides provider acceptance and enforces exact vanity-to-URL identity.
- `_ferramentas/thunder_os/connection_ledger.py`: `record_live_poll` forwards the default page bound before entering its transaction.
- Closest repository analogues are the existing connection intake/provider/ledger tests, which assert decision outcomes at their owning module boundary.

Cost: 8 focused checks across 4 test files. Without these rows, the sixth-page, symbol and scheduled-entry-point cases are not exercised by the existing suite.

## Swept

- validation: C4, C5, C6
- failure modes: C2, C5, C6
- idempotency: C4
- authorization: existing - `ConnectionProvider._get` retains GET-only transport and account readback, asserted by C3
- concurrency: existing - no shared-state concurrency contract changes; ledger transaction and existing concurrency tests remain unchanged
- data lifecycle: C2 - incomplete observations persist no checkpoint, profile or research-request state
- dependency failure: existing - provider status, timeout and schema failure behavior is unchanged and covered by the pre-existing focused suite
- state transitions: C2 - no state transition occurs before complete coverage
- observability: C7, C8

## Handoff

- S1-S2 total ~15k, all inside connection intake/provider/ledger - one builder, no handoff planned
