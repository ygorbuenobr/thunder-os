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

### Current operational checkpoint

- The VPS web and agent symlinks now resolve to the verified `993e0fca3f902c567895e8a35e47d8b0b20a3cb6` release. The web service returned HTTP 200 locally. The agent is active but its canonical control state is `paused=true`; an active process is not evidence of a completed research turn.
- The read-only ConnectSafely live and historical timers are enabled. The first production historical batch advanced exactly 100 profiles: 93 `needs_identity`, 7 `needs_fit`, 0 research jobs. The 10 baseline history rows were reclassified by the `993e0fc` correction; no dossier can be claimed from this batch.
- The exact original 41-card manifest has SHA-256 `2802b624bc7614f972d73dced86340e9c9df06659105f76d1b9b9e37b777859e`. A fresh full GET-only audit and canonical-lock apply produced 8 superseded cards and 33 inconclusive cards, preserving history. Diego's superseded card is not proof the suggested text was sent. Receipt: `/opt/thunder-os/staging/connection-copilot/legacy-41-apply-993e0fc.json`, SHA-256 `982a303700a2be88ad6067801c62fbc7608654b26f4dc1dd63dc09156fe052f5`.
- Isolated Hermes auth preflight initially failed because the pinned read-only dependency root had 92 unlisted Python cache files. A new clean root `/opt/hermes-agent-ro-thunder-os-09544f73e610` contains the same 14,745 manifest files with matching hashes and no cache files. An external staged lock changing only its root paths passed dependency verification and read-only authentication preflight (`hermes-auth-store`, `gpt-5.6-terra`); no model turn was executed. The unit/lock change is under independent review and not yet active.
- C12 remains open: no qualified dossier and no observed new-plus-historical end-to-end panel journey. The 33 inconclusive old cards remain pending without positive provider evidence. Do not unpause the agent or claim complete activation from tests, timers, or HTTP health alone.

- VPS effective web release `7bc74b02a7aef6f00f2fab097163b7d662462b35` and agent release `a235b97241c9b2e5a08b66a78c709f94d300a2dd` remain active; neither was modified. New source is local only.
- Read-only ConnectSafely GET `/linkedin/accounts` and `/linkedin/connections` returned HTTP 200 with expected pagination keys for the configured account. No profile or message content was printed.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.scratch/dependencies:_ferramentas python3 -m unittest discover -s _ferramentas -p 'test_*.py' -q` passed 211 tests after the latest local changes. These are mock/temporary-DB checks, not a VPS journey.
- Canonical panel DB read-only baseline: 41 `pendente` cards (21 DM, 18 comments, 2 WhatsApps); 12 lack `texto`. A bounded comparison against the already persisted inbox observations found no unique exact positive DM match. This is absence of proof, not proof that no message was sent.
- Active agent unit's `PYTHONPATH` does not expose bundled `_ferramentas/toques.py` to an absolute import. Candidate/agent activation requires an explicit unit adjustment and isolated import proof.
- The active ConnectSafely account returned HTTP 200 for a GET-only connection page; its `connectedDate` format was observed as `Conexão feita em 5 de outubro de 2026`. A targeted contract test for that format passed.
- A candidate from commit `0cc1f67` was built and verified in local and VPS staging (91 hashed files). Its isolated panel answered HTTP 200 against a SQLite clone; panel and agent production symlinks remained unchanged. The first real 41-card manifest preflight blocked because the live monitor config is schema v2, whereas that candidate accepted only v1. It was preserved as `superseded-0cc1f67`; the accepted v2 fix was included in the next candidate.
- Candidate `0f666a6` passed local and VPS bundle integrity checks. A manifest of the exact 41 current pending cards was generated from the live DB with SHA-256 `2802b624bc7614f972d73dced86340e9c9df06659105f76d1b9b9e37b777859e`. The full read-only provider audit timed out at 240 seconds without a receipt and did not mutate a card. Audit-only chunks and a cross-chunk consolidation, including duplicate-receipt rejection, are under local review; 215 local tests pass. They are not in this candidate yet. No production symlink or unit changed.
- Candidate `f601e21` was verified on the VPS. Fourteen GET-only audit chunks covered the original 41-card manifest, and consolidation completed with 8 `superseded`, 33 `inconclusive`, 0 `close`, 0 applied. Diego's card `ev-1cc53c85f59c` is `superseded` by a later thread event, not a verified send of the suggested text. The consolidated receipt is `/opt/thunder-os/staging/connection-copilot/legacy-41-consolidated-f601e21.json` (mode 0600). This does not authorize write from provisional chunks; any apply requires fresh full readback and canonical panel locking.
- A live-provider backfill canary against the SQLite clone stopped before provider access on an invalid LinkedIn URL in a brain ficha. A bounded parser correction now rejects the entire malformed URL while preserving valid exact mappings and ambiguity failures. The apply CLI also checks the canonical brain lock path, but its match to the effective panel process root remains a live preflight gate. Local full suite: 218 tests; these changes are not in candidate `f601e21` yet.
- Candidate `17b218c` passed bundle checks on VPS. The clone backfill advanced past catalog parsing, then rejected a real full-English-month `connectedDate`. A GET-only shape check of 100 rows found 10 Portuguese, 15 full October, and 75 full September dates; all identity fields had the expected string types. The date parser now covers these observed forms and rejects unknown months; 219 local tests pass and focused independent review accepted the change. No live backfill checkpoint or production queue was changed.
- Candidate `7f3aea2` reached the first 100 real provider rows against the isolated SQLite clone, then rejected 9 LinkedIn public identifiers containing Unicode lowercase letters. A sanitized GET-only shape check confirmed each URL slug equals its vanity name and retains the `/in/` path. The parser now accepts Unicode alphanumeric slug characters with the same host/path and exact vanity checks, while rejecting separators and escapes. This is still a clone-only correction; production state and services remain unchanged.
- Candidate `abb8aef` processed 100 real historical rows on the isolated clone: 93 `needs_identity`, 7 `needs_fit`, 0 research jobs; a separate live-poll clone canary observed 0 newer connections. The cloned DB passed integrity and its checkpoint recorded exactly 100 processed profiles. A consistent production DB backup was restored and verified with the same SHA-256; candidate web answered HTTP 200 on isolated port 18790. An initial transfer was rejected for unlisted `__pycache__` files and preserved outside releases; the clean 91-file transfer passed bundle verification. No service was switched. Independent review then found Unicode `casefold()` collisions (`straße`/`strasse`) and a non-idempotent `İ` key, so this candidate is blocked for activation. The parser now preserves non-ASCII identity characters while lowercasing ASCII only, without NFC normalization that would conflate `K` and `K`; focused collision and idempotency tests pass. Retest and re-review are required before activation.
- Release `3641dc1` passed bundle verification and isolated old/new panel HTTP checks, then replaced only the web symlink under a deploy flock. Effective panel PID and cwd matched the new release; HTTP returned 200. The new live-intake service ran once successfully, followed by a 100-profile historical backfill on production with no outbound calls other than ConnectSafely GET. Readback found 83 `needs_identity`, 7 `needs_fit`, and 10 baseline `historical` profiles: the baseline rows were skipped when backfill encountered existing profile records. Timers were not enabled. This is a real triage gap; reclassification of baseline history must pass clone and production readback before scheduling.
- New panel source defaults to skipping legacy `materializar_aceites()` during manual control reconciliation. The old acceptances timer is still active and untouched; its output remains historical input, not authority for a new approval card.
- A matching DM/comment must now have a parseable card creation time and a later provider event. A later *different* DM can only supersede the draft, never prove that its suggested text was sent.
- The first independent review rejected activation for shared-account panel regression, count-only/one-shot reconciliation, and an unreachable approach-review stage; the next review accepted those local fixes but found a post-commit receipt gap in the CLI. A pre-apply journal, directory sync before commit, recovery command and injected failures at replacement and directory sync were added. The reviewer re-ran 29 focused tests and accepted these local fixes, explicitly **not** approving production apply. Candidate build, snapshot/rollback rehearsal, serial deployment, real 41-card audit and end-to-end canaries remain pending.
