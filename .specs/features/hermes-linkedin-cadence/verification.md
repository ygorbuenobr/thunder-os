# Hermes LinkedIn cadence executor verification

**Verdict**: PASS
**Profile**: standard
**Diff range**: 1ee6ead69f3e89913d943993ca0c8d2c0817c306..9356f0ec77cd82b4fdaa61fe62e53b9c09fb3aed
**Round**: 4 - scoped re-verification of the Round 3 C17 blocker; all C1-C17 proofs rerun at HEAD
**Verifier**: independent sub-agent (author != verifier)

## Result

The Round 3 blocker is closed. If HubSpot applies `cad_status=incerto` but the following readback fails, replay reads the already-terminal value, performs no second PATCH, sets `crm_uncertain_confirmed=true`, and does not redeliver through ConnectSafely. The implementation is verified as a disabled source candidate for Job 1 only; no real provider, CRM, installed timer or production journey was exercised.

## Binding sources

| Source | Opened | Contradiction | Uncovered |
| --- | --- | --- | --- |
| `/root/.hermes/profiles/ygor-personal/brain/projetos/thunder-os/cadencia-multicanal/especificacao.md` | yes - Job 1, Job 3 and F3 boundaries | none for the Job 1 source scope | - |
| `.specs/features/hermes-linkedin-cadence/plan.md` | yes - AC1-12, Flow and Landing doors | none | - |
| `.specs/features/hermes-linkedin-cadence/checks.md` | yes - C1-C17 and profile standard | none | - |

## Checks

The 39-test focused invocation at `9356f0ec77cd82b4fdaa61fe62e53b9c09fb3aed` ran every named unit/synthetic proof for C1-C4 and C6-C17. C5 and repository/runtime validators ran separately.

| Check | Claim | Proof run and located evidence | Result |
| --- | --- | --- | --- |
| C1 | immutable exact D0 authority | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:138-151` asserts actor, four steps, replay, rejection and expiry | PASS |
| C2 | pause/scope/delegation/cap cause zero writes | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:168-171` asserts rejection, zero execution and zero completion | PASS |
| C12 | approval route persists without effects | focused batch exit 0; `_ferramentas/test_thunder_os_connectsafely_panel_wiring.py:181-191` asserts `201`, Ygor authority, no jobs and invalid-authority/config failures | PASS |
| C3 | exact task/status/cadence gate | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:174-192` enumerates six mismatches and asserts zero writes | PASS |
| C4 | invite/message relationship gate | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:194-210` enumerates four states and asserts the exact permitted count | PASS |
| C16 | immediate CRM reread catches reply/status change | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:213-218` asserts `cadence_status_blocked`, zero executions and zero completions; stale-read mutation carried from Round 3 at `dc033b1` was killed | PASS |
| C5 | deterministic source boundary | `python3 scripts/check_hermes_linkedin_determinism.py --check test_runtime_is_deterministic` exit 0; `scripts/check_hermes_linkedin_determinism.py:7-23` rejects model/prompt dependencies | PASS |
| C6 | receipt-bound completion and delegated audit | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:220-226` asserts completion receipt and `hermes (delegado)` | PASS |
| C7 | nonconfirmed outcomes stay open and uncertain is fenced | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:243-254` asserts no completion, local alert, replay and one execution | PASS |
| C8 | confirmed replay is idempotent | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:257-261` asserts one provider call, one completion and one cap unit | PASS |
| C13 | delegated invite reconciliation under exact D0 authority | focused batch exit 0; `_ferramentas/test_thunder_os_connectsafely_panel_wiring.py:374-380` asserts completed state, exact receipt, delegated actor and one reconcile | PASS |
| C17 | PATCH-applied/readback-failed replay converges without redelivery | focused batch exit 0; runner assertions at `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:263-280` prove one delivery, replay and `crm_uncertain_confirmed=true`; adapter assertions at `_ferramentas/test_thunder_os_hubspot_tasks.py:73-87` prove one PATCH and convergence after failed readback | PASS |
| C9 | bounded, secret-free disabled/live/synthetic config | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:55-72` asserts invalid bounds rejected and checked-in/live examples disabled | PASS |
| C14 | revision binding and 30-90 second spacing | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:85-97` asserts mismatch exit and one 37-second delay | PASS |
| C15 | CRM status/readback and receipt note | focused batch exit 0; `_ferramentas/test_thunder_os_hubspot_tasks.py:61-87` asserts idempotent `incerto` readback and one PATCH; `_ferramentas/test_thunder_os_hubspot_tasks.py:42-46` asserts completion note receipt | PASS |
| C10 | synthetic end-to-end and sanitized summary | focused batch exit 0; `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:74-83` forbids sockets and asserts completion, revision, no secret and receipt | PASS |
| C11 | exact HubSpot task and receipt completion contract | focused batch exit 0; `_ferramentas/test_thunder_os_hubspot_tasks.py:37-46` asserts exact IDs, one completion PATCH and receipt note; `_ferramentas/test_thunder_os_hubspot_tasks.py:54-59` asserts invalid scope/receipt produces zero PATCH | PASS |

## Coverage

Coverage was recomputed from the plan, canonical Job 1 source, runtime branches and provider/ledger gates.

| Set (size) | Recomputed member -> proof | Unproven |
| --- | --- | --- |
| authority failures (7) | wrong actor/changed/incomplete/expired C1; pause/scope/cap C2 | - |
| task mismatch classes (7) | early/completed/malformed/contact/replied/cadence C3; mid-read status change C16 | - |
| relationship states (4) | NOT_CONNECTED/CONNECTED/INVITATION_SENT/INVITATION_RECEIVED C4 | - |
| provider outcomes (5) | confirmed C6; blocked/rejected/failed/uncertain C7 | - |
| replay cases (4) | approval C1; confirmed C8; invitation C13; uncertain CRM C17 | - |
| runtime modes (3) | disabled/live C9; synthetic C10 | - |
| HubSpot write gates (8) | portal/owner/contact/subject/body/receipt C11; status/readback C15/C17 | - |
| Job 1 stop/effect controls (8) | status/cadence C3/C15/C16; canonical ledger reply/suppression/duplicate/quota gates carried from ConnectSafely regression coverage; receipt note C11/C15; alert C7; spacing/revision C14 | - |
| approval route statuses (3) | `201`/`400`/`503` C12 | - |

## Test policy rows

| Row | Required proof | Expectation met |
| --- | --- | --- |
| external dispatch authority | spy ports and zero calls on rejection | yes - C1-C4, C12, C16 |
| task completion | producer-to-consumer synthetic receipt and exact task | yes - C6, C10, C11, C13 |
| uncertainty | replay/reconciliation plus durable CRM convergence | yes - C7, C13 and C17 across runner and production adapter contract |
| determinism | AST/source boundary | yes - C5 and source-boundary validator |
| runtime config | parser plus bounded effectful modes | yes at source level - C9, C10, C14 |

## Swept existing

- Authorization: exact D0 hash/version, delegated scope/version, pause, cap, CRM status/cadence and relationship are rechecked; C16 still proves the immediate second CRM read.
- Idempotency/concurrency: confirmed jobs, uncertain provider jobs, invite reconciliation and the CRM `incerto` transition are fenced against replay.
- Dependency failure: the Round 4 adapter accepts a previously applied `incerto` transition after a lost readback while rejecting other changed statuses.
- State transitions: external `incerto` and local `crm_uncertain_confirmed` now converge on replay.
- Observability: source records local alert, delegated audit and receipt-bound task note; no production logs or external receipts were inspected.
- Scope boundary: this feature implements the deterministic Job 1 executor only. Job 3 cross-channel response detection is a separate capability and is not implied by this PASS.

## Faults injected

Mutations used detached task-owned worktrees; the Round 4 scratch worktree was removed and the real-tree baseline remained unchanged before writing this report.

| Mutation | Location | Killed |
| --- | --- | --- |
| change `if current == "incerto"` to an unreachable sentinel | `_ferramentas/thunder_os/hubspot_tasks.py:145` | yes - both adapter idempotency/readback tests failed; focused mutation command exited 1 |
| replace immediate second CRM read with stale initial task | `_ferramentas/thunder_os/hermes_linkedin_cadence.py` | yes - carried from Round 3 at `dc033b1`; C16 failed |
| disable replay retry of pending `cad_status=incerto` | `_ferramentas/thunder_os/hermes_linkedin_cadence.py` | yes - carried from Round 3 at `dc033b1`; C17 failed |

## Acceptance criteria

| Criterion | Verdict | Evidence |
| --- | --- | --- |
| AC1-4 exact immutable authority and deterministic mapping | ATENDIDOS | C1-C3, C5, C12 |
| AC5 stop states and pre-queue reread | ATENDIDO at source/synthetic level | C3 and mutation-resistant C16 |
| AC6 relationship gate | ATENDIDO | C4 |
| AC7 receipt completion, note and audit | ATENDIDO at source/synthetic level | C6, C11, C13, C15 |
| AC8 uncertain state, `incerto`, no resend and reconciliation | ATENDIDO at source/synthetic level | C7, C13, C17; PATCH/readback partial success now converges |
| AC9 replay idempotency | ATENDIDO at source/synthetic level | C8, C13, C17 |
| AC10-12 disabled/bounded runtime and synthetic journey | ATENDIDOS at source level | C9, C10, C14 |

## Gate

- Focused C1-C17 invocation: 39 passed, 0 failed at HEAD.
- Round 4 adapter mutation: killed; two adapter tests failed as expected and the mutation command exited 1.
- C16 and runner retry mutations: carried from the immediately preceding Round 3 and remain covered by green proofs at HEAD.
- Determinism: `hermes_linkedin_runtime_deterministic: ok`.
- Source boundary: `source boundary ok: 155 tracked paths`.
- systemd candidate verification: exit 0.
- `validate_plan.py`: 0 errors, 0 warnings.
- `validate_checks.py`: 0 errors, 2 existing selector warnings (C5 and C15).

## Limits

- This PASS covers committed Thunder OS source for the deterministic Job 1 executor and local synthetic contracts only.
- Job 3 reply detection across HubSpot and LinkedIn, sequence removal/cancellation, suppression propagation and the response task for Ygor remain separate and unverified here.
- No credentials, network provider, LinkedIn, HubSpot, production database, installed unit, deploy, restart, push or external effect was used. The live example remains disabled.
- No real HubSpot PATCH/readback, ConnectSafely receipt or production timer execution was proven. Activation still requires an authorized deployment and named canary.
- The unavailable PostgreSQL quarantine environment was not rerun in this bounded round, by scope; no claim is made about that integration.
