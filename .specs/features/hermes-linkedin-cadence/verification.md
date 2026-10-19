# Hermes LinkedIn cadence executor verification

**Verdict**: FAIL
**Profile**: standard
**Diff range**: d20b6d6d8f97ace5b5cd5f2256e8c4a47e70dfbe..dc033b1d26f8e187f3fab4cff4da9d460b50a3e8
**Round**: 3 - scoped re-verification of the two Round 2 blockers plus C1-C17 at HEAD
**Verifier**: independent sub-agent (author != verifier)

## Material findings

1. **MAJOR - a successful HubSpot PATCH followed by a failed readback cannot be confirmed on replay.** The runner now persists `crm_uncertain_confirmed=false` and retries `mark_uncertain` (`_ferramentas/thunder_os/hermes_linkedin_cadence.py:198-208`), which correctly handles a failure before the PATCH. The production adapter, however, rejects any current status different from the old expected status before it reads the desired terminal state (`_ferramentas/thunder_os/hubspot_tasks.py:141-149`). An isolated producer-to-consumer probe made the first PATCH take effect (`cad_status=incerto`) and then raised a readback connection error. Replay made no second provider delivery, but the second CRM call rejected `incerto != em_cadencia`; local state remained `crm_uncertain_confirmed=false` and every later replay remains `hubspot_uncertain_status_pending`. C17's fixture raises before changing status (`_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:263-278`), so it cannot detect this partial-success case. Expected by AC8/C17: replay must first accept/read back an already-`incerto` contact, or otherwise make the transition idempotent, while preserving zero redelivery.

The Round 2 second-read blocker is closed: C16 changes CRM status during relationship lookup, the reread at `_ferramentas/thunder_os/hermes_linkedin_cadence.py:246-253` blocks with zero dispatch, and replacing that reread with the stale task makes C16 fail.

## Binding sources

| Source | Opened | Contradiction | Uncovered |
| --- | --- | --- | --- |
| `/root/.hermes/profiles/ygor-personal/brain/projetos/thunder-os/cadencia-multicanal/especificacao.md` | yes - Job 1/Job 3 and F3 boundaries | none for Job 1 source shape | Job 3 cross-channel detector and real-runtime proof remain outside this feature |
| `.specs/features/hermes-linkedin-cadence/plan.md` | yes - AC1-12 and both Landing doors | none | AC8 remains non-durable after PATCH-success/readback-failure |
| `.specs/features/hermes-linkedin-cadence/checks.md` | yes - C1-C17 | C17's proof is narrower than its claim | partial-success HubSpot transition is unproven and fails adversarially |

## Checks

All named C1-C17 proofs were covered by a 38-test focused invocation at `dc033b1d26f8e187f3fab4cff4da9d460b50a3e8`; determinism, source boundary, systemd and both TLC validators were rerun separately.

| Check | Claim | Evidence | Result |
| --- | --- | --- | --- |
| C1 | immutable exact D0 authority | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:136-151` | PASS |
| C2 | pause/scope/delegation/cap zero-write gates | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:153-172` | PASS |
| C12 | approval route persists without effects | `_ferramentas/test_thunder_os_connectsafely_panel_wiring.py:165-191` | PASS |
| C3 | exact task/status/cadence gate | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:174-192` | PASS |
| C4 | invite/message relationship gate | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:194-211` | PASS |
| C16 | status change during relationship read blocks dispatch | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:213-218`; stale-read mutation failed this test | PASS |
| C5 | deterministic source boundary | `scripts/check_hermes_linkedin_determinism.py:7-23` | PASS |
| C6 | receipt-bound completion and delegated audit | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:220-226` | PASS |
| C7 | nonconfirmed outcomes remain open and fenced | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:228-255` | PASS |
| C8 | confirmed replay idempotency | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:257-261` | PASS |
| C13 | exact delegated invite reconciliation | `_ferramentas/test_thunder_os_connectsafely_panel_wiring.py:332-379` | PASS |
| C17 | failed `incerto` write/readback resumes without redelivery | named proof green only for pre-write failure; adversarial partial-success probe remains pending forever | FAIL |
| C9 | disabled/bounded secret-free configuration | `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:55-72` | PASS |
| C14 | revision binding and 30-90 second spacing | `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:85-97` | PASS |
| C15 | CRM status/cadence/readback and receipt note | `_ferramentas/test_thunder_os_hubspot_tasks.py:58-67` | PASS for happy path; does not cover partial success |
| C10 | synthetic end-to-end and sanitized summary | `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:74-83` | PASS |
| C11 | exact HubSpot task and receipt completion contract | `_ferramentas/test_thunder_os_hubspot_tasks.py:30-56` | PASS |

## Coverage

Coverage was recomputed from the plan, canonical cadence source, runtime branches and provider/ledger gates.

| Set (size) | Recomputed member -> proof | Unproven |
| --- | --- | --- |
| authority failures (7) | wrong actor/changed/incomplete/expired C1; pause/scope/cap C2 | - |
| task mismatch classes (7) | early/completed/malformed/contact/replied/cadence C3; mid-read status change C16 | - |
| relationship states (4) | NOT_CONNECTED/CONNECTED/INVITATION_SENT/INVITATION_RECEIVED C4 | - |
| provider outcomes (5) | confirmed C6; blocked/rejected/failed/uncertain C7 | - |
| replay cases (4) | approval C1; confirmed C8; invitation C13; uncertain CRM C17 | PATCH-success/readback-failure |
| runtime modes (3) | disabled/live C9; synthetic C10 | - |
| HubSpot write gates (8) | portal/owner/contact/subject/body/receipt C11; status/readback C15 | idempotent readback after partial success |
| Job 1 stop/effect controls (8) | status/cadence C3/C15/C16; reply/suppression/quota provider regressions; receipt note C11/C15; alert C7; spacing/revision C14 | cross-channel replies depend on Job 3 |
| approval route statuses (3) | `201`/`400`/`503` C12 | - |

## Test policy rows

| Row | Required proof | Verdict |
| --- | --- | --- |
| external dispatch authority | spy ports and zero calls on rejection | met for named Job 1 gates |
| task completion | synthetic producer-to-consumer receipt and exact task | met |
| uncertainty | no redelivery plus durable CRM/reconciliation replay | not met for PATCH-success/readback-failure |
| determinism | AST/source boundary | met |
| runtime config | parser plus bounded effectful modes | met at source level |

## Swept existing

- Authorization: D0 hash/version, delegated scope/version, pause, cap, CRM status/cadence and relationship are rechecked. The second CRM read is now mutation-resistant.
- Idempotency/concurrency: provider redelivery remains fenced. The CRM transition is not idempotent when its write succeeds but acknowledgement/readback fails.
- Dependency failure: failure before the CRM PATCH is retried; failure after the PATCH strands local confirmation because `mark_uncertain` rejects the already-desired state.
- State transitions: external `incerto` and local `crm_uncertain_confirmed=false` can diverge indefinitely.
- Observability: local alert reports `hubspot_uncertain_status_pending`; no provider, HubSpot, installed service or journal was accessed.
- Scope boundary: this source implements Job 1. Job 3, which detects replies across channels and updates stop state, remains outside this recut and is not claimed ready.

## Faults injected

Mutations ran in detached task-owned worktrees and were fully reverted/removed afterward.

| Mutation/fault | Location | Killed |
| --- | --- | --- |
| replace immediate second CRM read with stale initial task | `_ferramentas/thunder_os/hermes_linkedin_cadence.py:248` | yes - C16 failed because no `CadenceBlocked` was raised |
| disable replay retry of pending `cad_status=incerto` | `_ferramentas/thunder_os/hermes_linkedin_cadence.py:199` | yes - C17 failed with one CRM call instead of two |
| PATCH takes effect, then readback connection fails | boundary between `_ferramentas/thunder_os/hubspot_tasks.py:146-149` and runner replay | no - replay stays pending with external `incerto` and local confirmation false |

## Acceptance criteria

| Criterion | Verdict | Evidence |
| --- | --- | --- |
| AC1-4 exact immutable authority and deterministic mapping | ATENDIDOS | C1-C3, C5, C12 |
| AC5 stop states and pre-queue reread | ATENDIDO at source/synthetic level | C3 and mutation-resistant C16 |
| AC6 relationship gate | ATENDIDO | C4 |
| AC7 receipt completion, note and audit | ATENDIDO at source/synthetic level | C6, C11, C13, C15 |
| AC8 uncertain state, `incerto`, no resend, reconciliation | NÃO ATENDIDO | no resend passes, but partial-success CRM readback cannot converge |
| AC9 replay idempotency | ATENDIDO for confirmed/provider execution | C8, C13; CRM transition exception above |
| AC10-12 disabled/bounded runtime and synthetic journey | ATENDIDOS at source level | C9, C10, C14 |

## Gate

- Focused C1-C17 invocation: 38 passed, 0 failed.
- Two Round 2 implementation-removal mutations: both killed by C16/C17.
- Partial-success HubSpot fault probe: reproduced non-convergent local state; gate FAIL.
- ConnectSafely regression discovery: 126 runnable tests passed; the PostgreSQL quarantine class did not start because `/private/tmp/thunder-social-reconcile/.s.PGSQL.55439` is absent.
- Determinism: `hermes_linkedin_runtime_deterministic: ok`.
- Source boundary: `source boundary ok: 155 tracked paths`.
- systemd unit verification: exit 0.
- `validate_plan.py`: 0 errors, 0 warnings.
- `validate_checks.py`: 0 errors, 2 existing selector warnings (C5 and C15).

## Limits

Only committed source, local fixtures, synthetic ports and isolated mutations were used. No credentials, network provider, LinkedIn, HubSpot, production database, installed unit, deploy, restart, push or external effect was used. The live example remains disabled. Job 1 is not ready to activate until the idempotent CRM readback gap above is fixed and reverified. Job 3 cross-channel reply detection is a separate source/runtime capability outside this executor and remains unverified.
