# Connection intake provider compatibility verification

**Verdict**: PASS
**Profile**: standard
**Diff range**: 5041176f369824883ca19b763dde42dc03f1493c..334ddf7d108df564379bd1ac2401fb073ecab9af
**Round**: 3 - full
**Verifier**: independent sub-agent (author != verifier)

## Checks

All proofs were rerun at `334ddf7d108df564379bd1ac2401fb073ecab9af` in one batched invocation. The two runtime tests used the proof's exact `PYTHONPATH=_ferramentas:/opt/thunder-os/current/_ferramentas`.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | Default window completes when the first older row is on page 6 or 20 and stops on that page. | focused batch, exit 0 | `_ferramentas/test_thunder_os_connection_intake.py:122` - `self.assertEqual(boundary_cursor + 10, len(rows))`; `:123` - `self.assertEqual(expected_cursors, seen_cursors)` | PASS |
| C2 | Twenty nonterminal pages fail incomplete before checkpoint, profile or request writes. | focused batch, exit 0 | `_ferramentas/test_thunder_os_connection_ledger.py:147` - `assertRaisesRegex(..., "connection_window_incomplete")`; `:153-156` - exact 20 cursors and all three state snapshots unchanged | PASS |
| C3 | Pagination preserves GET/no-body, configured account and returned contiguous cursors. | focused batch, exit 0 | `_ferramentas/test_thunder_os_connection_provider.py:63-66` - GET/no-body/timeout and exact account query; `:97-99` - three rows and cursors `[0, 2, 3]` | PASS |
| C4 | Exact `U+2234` is preserved and idempotent; `straße` and `strasse` stay distinct. | focused batch, exit 0 | `_ferramentas/test_thunder_os_connection_provider.py:207-211` - exact canonical URL, round trip and two non-equalities | PASS |
| C5 | Vanity/URL mismatch remains invalid after one retry at the same cursor. | focused batch, exit 0 | `_ferramentas/test_thunder_os_connection_provider.py:125-128` - `connection_row_invalid` and connection cursors `[0, 0]` | PASS |
| C6 | Separator, whitespace, control and unsupported punctuation remain invalid. | focused batch, exit 0 | `_ferramentas/test_thunder_os_connection_provider.py:214-227` - four-member table and `assertRaisesRegex(..., "connection_row_invalid")` | PASS |
| C7 | Live entrypoint emits success for page 6 and blocked JSON for the 20-page incomplete case. | focused batch, exit 0 | `_ferramentas/test_thunder_os_connection_runtime.py:78-80` - exit 0, `status=ok`, six cursors; `:93-96` - exit 1, exact blocked/error JSON and 20 cursors | PASS |
| C8 | Backfill entrypoint emits blocked invalid-row JSON and accepted-terminal success JSON. | focused batch, exit 0 | `_ferramentas/test_thunder_os_connection_runtime.py:103-111` - exact blocked error and exact `(ok, backfill, True)` tuple | PASS |

Level judgment: C1/C2 exercise the intake and ledger decision boundaries; C3-C6 exercise the provider boundary; C7/C8 exercise the scheduled command/JSON boundary. The page range is sampled at its first newly reachable page and its upper edge; the uniform bounded loop makes intermediate pages equivalent, so no level or sampling gap was found.

## Coverage

The join was recomputed from the plan's criteria/observable sets and the implementation's provider/runtime surfaces, rather than copied from `checks.md`.

| Set (size) | Recomputed from | Member -> proof | Unproven |
| --- | --- | --- | --- |
| default page-bound edges (3) | `plan.md:40-42`; defaults at `connection_intake.py:83` and `connection_ledger.py:147` | page 6 C1 · page 20 C1 · incomplete after page 20 C2 | - |
| provider transport invariants (4) | `connection_provider.py:83-111,160-179` | GET C3 · no body C3 · configured account C3 · contiguous cursor C3 | - |
| symbol identity outcomes (4) | `plan.md:50-52`; canonicalization at `connection_intake.py:21-29` | exact `U+2234` C4 · mismatch C5 · idempotent round trip C4 · distinct Unicode spelling C4 | - |
| unsafe slug classes (4) | `plan.md:51` | separator C6 · whitespace C6 · control C6 · unsupported punctuation C6 | - |
| scheduled consumers (2) | `plan.md:67-71`; dispatch at `connection_runtime.py:158-174` | live C7 · backfill C8 | - |
| live outcomes (2) | `plan.md:67-68` | `status=ok` C7 · blocked `connection_window_incomplete` C7 | - |
| backfill outcomes (2) | `plan.md:69` | `status=ok` C8 · blocked `connection_row_invalid` C8 | - |

No additional enumerated set in `Landing`, `Relations`, `Surface`, the checks' claims or the level evidence lacked a coverage row. `Relations` and `Surface` are explicitly empty, and `Landing` adds no door.

## Test policy rows

| Row | Files it classifies | Required proof | Expectation met |
| --- | --- | --- | --- |
| Slug-character decision | `connection_intake.py`, `connection_provider.py` | accepted provider identity plus table-driven rejected classes | yes - C4 and C6 cover the admitted class and all four rejection classes; C5 covers exact identity mismatch |
| Pagination-bound decision | `connection_intake.py`, `connection_ledger.py` | page 6 intake success and page 20 ledger failure with atomicity | yes - C1 and C2 |
| Ledger default forwarding | `connection_ledger.py` | omit `max_pages` and observe 20-page domain default | yes - C2 omits the argument and asserts cursors `0..19` |

## Swept existing

- Authorization: `ConnectionProvider._get` still hardcodes GET with no body and `_verify_identity` still requires unique account/public-id echo (`connection_provider.py:83-111`); C3 asserts the transport calls.
- Concurrency: `record_live_poll` still persists under one repository transaction (`connection_ledger.py:171-235`); the unchanged concurrent repeat assertions at `test_thunder_os_connection_ledger.py:185-212` passed in the 140-test suite.
- Dependency failure: unchanged rate-limit, timeout, response-size, duplicate-key and pagination failure tests passed in the focused suite.
- No schema, route, persisted enum or external-effect path changed in the diff.

## Faults injected

Each mutation was applied in detached scratch worktree `/tmp/thunder-verify-r3.dhAKmb/tree`, then reverted before the next mutation. All five mutants were killed; the scratch was removed and the real-tree porcelain remained the same.

| Mutation | Location | Killed |
| --- | --- | --- |
| intake default `max_pages` 20 -> 19 | `_ferramentas/thunder_os/connection_intake.py:83` | yes - C1 failed at the page-20 subtest |
| ledger forwarded default `max_pages` 20 -> 19 | `_ferramentas/thunder_os/connection_ledger.py:147` | yes - C2 observed only 19 cursors |
| preserve non-ASCII spelling -> `slug.casefold()` | `_ferramentas/thunder_os/connection_intake.py:29` | yes - C4 collapsed `straße` into `strasse` |
| same-page retry cursor -> `start_index + 1` | `_ferramentas/thunder_os/connection_provider.py:156` | yes - C5 rejected the changed error/cursor behavior |
| runtime blocked status -> `error` | `_ferramentas/thunder_os/connection_runtime.py:171` | yes - both C7 and C8 rejected the JSON |

## Acceptance criteria

| Criterion | Verdict | Evidence |
| --- | --- | --- |
| AC1 pages 6-20 complete at first boundary | ATENDIDO | C1, C7; page-20 mutant killed |
| AC2 page-20 incomplete is atomic | ATENDIDO | C2, C7; ledger-default mutant killed |
| AC3 GET/account/cursor contract | ATENDIDO | C3 and source inspection |
| AC4 exact mathematical symbol preserved | ATENDIDO | C4 |
| AC5 mismatch and unsafe classes fail after at most one same-page retry | ATENDIDO | C5, C6; retry-cursor mutant killed |
| AC6 idempotence without Unicode folding | ATENDIDO | C4; casefold mutant killed |

## Gate

- `python3 /root/.agents/skills/tlc-spec-lean/scripts/validate_plan.py connection-intake-provider-compat` - 0 errors, 0 warnings.
- `python3 /root/.agents/skills/tlc-spec-lean/scripts/validate_checks.py connection-intake-provider-compat` - 0 errors, 0 warnings, profile standard.
- Batched C1-C8 proof invocation - 9 tests passed, 0 failed.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/opt/thunder-os/current/_ferramentas python3 -m unittest -v $(printf '%s\\n' _ferramentas/test_thunder_os_connection_*.py)` - 140 passed, 0 failed.
- `python3 scripts/check_source_boundary.py` - `source boundary ok: 140 tracked paths`.

## Limits

This is source verification only. No network, provider, credential, service, production database, deployment or reprocessing was used; therefore no live runtime or external integration result is claimed.
