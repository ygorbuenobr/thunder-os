# Thunder OS Connection Copilot

Sources:

- User-approved Thunder OS connection, research and queue plan in this task - binding behavior and rollout order.
- `AGENTS.md` and `.specs/features/thunder-os-v1/` - existing product boundaries; the explicit source-ownership decision in this task supersedes the source-only deployment restriction.
- Active VPS units and releases observed on 2026-10-05 - runtime baseline, not an implementation authority.

## Out of scope

- Automatic LinkedIn sends, invitations, reactions or comments - this flow only reads and prepares work.
- Thunder GTM, Cockpit, HunterV2 and Chattie implementation or data migration - separate owners.
- Paid enrichment or model calls without a finite, configured budget and an observed cost receipt.

## Landing

The `thunder-os` repository becomes the source/build owner of the OS panel, agent and monitor, while operational state remains in the VPS data location. ConnectSafely is the observation source for social state, not for company identity. New-connection polling and historical backfill have separate checkpoints.

| One-way door | Literal shape | Alternative rejected |
| --- | --- | --- |
| Source authority | Versioned Thunder OS assembly from `ygorbuenobr/thunder-os` | Continuing to build OS releases from `/srv/thunder-gtm.git`, which conflates product authorities |
| Connection identity | Canonical LinkedIn profile key scoped to ConnectSafely account, with provenance for account mapping | Name/headline match, which can link the wrong company |
| External state | Positive readback closes a pending card; unavailable readback remains inconclusive | Treating an old dashboard label or local send attempt as provider proof |

## Checks

### S1 - Source and recovery authority

**C1** - Candidate assembly contains the active OS panel, agent and monitor entrypoints with a single Thunder OS source SHA.
Proof: `python3 scripts/check_runtime_bundle.py <candidate>` and effective unit inspection before activation.

**C2** - Snapshot integrity and an isolated rollback rehearsal pass for panel and agent before either service changes.
Proof: candidate recovery preflight and temporary-symlink rollback test with checksum output.

### S2 - Connection ledger and polling

**C3** - A complete ConnectSafely poll records account-scoped canonical profiles and advances only its live checkpoint.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_ledger.ConnectionLedgerTests.test_complete_live_poll`.

**C4** - Incomplete, reordered or wrong-account pages leave both checkpoint and connection ledger unchanged.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_ledger.ConnectionLedgerTests.test_invalid_poll_is_atomic`.

**C5** - Repeating a poll does not duplicate a connection or research request.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_ledger.ConnectionLedgerTests.test_repeated_poll_is_idempotent`.

### S3 - Historical triage and research

**C6** - Backfill processes at most 100 historical profiles per day without moving the live cursor.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_backfill.ConnectionBackfillTests.test_daily_limit_and_cursor_isolation`.

**C7** - Unknown or conflicting company identity stays in identification work; only evidenced fit enters research. The queue admits at most 5 new research jobs per Sao Paulo day, and waiting profiles resume on later days.
Proof: `PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest _ferramentas.test_thunder_os_connection_backfill _ferramentas.test_thunder_os_connection_runtime` (6 tests passed before the extra invalid-page test; rerun consolidated after final edits).

**C8** - Research result, sources, gaps and next action remain correlated to the original profile/account and survive a worker restart without duplicate execution.
Proof: `python3 -m unittest _ferramentas.test_thunder_os_connection_worker.ConnectionWorkerTests.test_restart_keeps_one_job`.

### S4 - Panel and existing-card reconciliation

**C9** - Panel differentiates identity, research, dossier review, approach approval and no-action states. Approach approval requires a current, profile-targeted draft cadence tied to the research hash and sources; account-level copy alone is insufficient. This is display only, not send authorization.
Proof: `PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest _ferramentas.test_thunder_os_connection_panel -q` (10 local tests passed).

**C10** - Positive ConnectSafely readback may propose closure of already-completed cards with evidence; missing readback never marks a send complete. Runtime pagination and an atomic, compare-before-write applicator are implemented locally. Before `--apply`, the CLI creates a recoverable receipt journal. A post-commit receipt failure retains that journal, and `--recover-receipt` reconstructs the item dispositions from the panel DB without another provider call. The applicator has not touched production.
Proof: `PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest _ferramentas.test_thunder_os_connection_reconcile _ferramentas.test_thunder_os_connection_reconcile_runtime _ferramentas.test_thunder_os_connection_reconcile_apply _ferramentas.test_thunder_os_connection_reconcile_cli` (mocked provider, temporary SQLite and post-commit receipt-failure injection); real provider audit and 41-card disposition remain open.

**C11** - The existing 41-card baseline (21 DMs, 18 comments, 2 WhatsApps observed on 2026-10-05) is classified item by item by channel and provenance, without deleting history or resending Diego's message. Cards without a channel-specific positive receipt remain inconclusive.
Proof: sanitized before/after reconciliation report with one disposition per original card and channel-specific read-only provider receipts where available.

### S5 - Production journey

**C12** - One new and one historical connection traverse observed provider read, persisted state, qualified dossier and panel display in the active VPS runtime, with zero outbound sends.
Proof: correlated runtime receipts, provider GET evidence, database readback and panel browser check after serial activation.

## Swept

- Validation: C4, C7.
- Failure modes: C4, C8, C10.
- Idempotency and retry: C5, C8.
- Authorization: outbound excluded; C9 only exposes review.
- Concurrency and ordering: C4, C5, C8.
- Data lifecycle: C11 preserves existing history.
- External dependency failure: C4, C10.
- State transitions: C3, C7, C9, C10.
- Observability: C11, C12.

## Handoff

Single coordinated build across source, runtime and panel. Do not hand off mid-slice; independent reviewer receives the final diff and all proofs after the last slice.

## Evidence so far (2026-10-05)

- VPS effective web release `7bc74b02a7aef6f00f2fab097163b7d662462b35` and agent release `a235b97241c9b2e5a08b66a78c709f94d300a2dd` remain active; neither was modified. New source is local only.
- Read-only ConnectSafely GET `/linkedin/accounts` and `/linkedin/connections` returned HTTP 200 with expected pagination keys for the configured account. No profile or message content was printed.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest discover -s _ferramentas -p 'test_*.py' -q` passed 211 tests after the latest local changes. These are mock/temporary-DB checks, not a VPS journey.
- Canonical panel DB read-only baseline: 41 `pendente` cards (21 DM, 18 comments, 2 WhatsApps); 12 lack `texto`. A bounded comparison against the already persisted inbox observations found no unique exact positive DM match. This is absence of proof, not proof that no message was sent.
- Active agent unit's `PYTHONPATH` does not expose bundled `_ferramentas/toques.py` to an absolute import. Candidate/agent activation requires an explicit unit adjustment and isolated import proof.
- The active ConnectSafely account returned HTTP 200 for a GET-only connection page; its `connectedDate` format was observed as `Conexão feita em 5 de outubro de 2026`. A targeted contract test for that format passed.
- New panel source defaults to skipping legacy `materializar_aceites()` during manual control reconciliation. The old acceptances timer is still active and untouched; its output remains historical input, not authority for a new approval card.
- A matching DM/comment must now have a parseable card creation time and a later provider event. A later *different* DM can only supersede the draft, never prove that its suggested text was sent.
- The first independent review rejected activation for shared-account panel regression, count-only/one-shot reconciliation, and an unreachable approach-review stage; the next review accepted those local fixes but found a post-commit receipt gap in the CLI. A pre-apply journal, directory sync before commit, recovery command and injected failures at replacement and directory sync were added. The reviewer re-ran 29 focused tests and accepted these local fixes, explicitly **not** approving production apply. Candidate build, snapshot/rollback rehearsal, serial deployment, real 41-card audit and end-to-end canaries remain pending.
