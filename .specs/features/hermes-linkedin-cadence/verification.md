# Hermes LinkedIn cadence executor verification

**Verdict**: FAIL
**Profile**: standard
**Diff range**: 1272988d0746e3d74fd54c8873d77210dcb7bd49..d20b6d6d8f97ace5b5cd5f2256e8c4a47e70dfbe
**Round**: 2 - scoped re-verification of the Round 1 failures plus all checks at HEAD
**Verifier**: independent sub-agent (author != verifier)

## Material findings

1. **MAJOR - a transient HubSpot failure can leave an uncertain contact active forever.** The runner persists local `status=uncertain` before calling `mark_uncertain` (`_ferramentas/thunder_os/hermes_linkedin_cadence.py:268-279`). If that HubSpot PATCH/readback raises, the next run enters the prior-uncertain branch (`:198-224`), attempts receipt reconciliation, and returns without retrying `mark_uncertain`. An isolated probe observed `first_error=hubspot_request_uncertain`, then two runs with local `uncertain`, one CRM attempt only, and the contact still `em_cadencia`. Expected by AC8: `cad_status=incerto` must be durably confirmed or remain a pending side effect retried without another LinkedIn send. Recommended correction: persist a separate CRM-transition-pending state and retry/read back that transition on every uncertain replay before returning or completing reconciliation.
2. **MODERATE - the required pre-queue CRM reread has no regression proof.** Production code currently rereads the task at `_ferramentas/thunder_os/hermes_linkedin_cadence.py:236-243`, but C3 uses a task fixture that never changes between reads. Replacing `latest = self.tasks.get(task_id)` with `latest = task` left C3 green. Expected by AC5 and the standard profile: a test must change `cad_status` or `cadence_id` between the first read and the pre-queue read, then assert zero enqueue/provider calls. This is a surviving mutant, so the profile gate cannot pass.

## Binding sources

| Source | Opened | Contradiction | Uncovered |
| --- | --- | --- | --- |
| `/root/.hermes/profiles/ygor-personal/brain/projetos/thunder-os/cadencia-multicanal/especificacao.md` | yes - lines 13-18, 24-32, 55-62, 80-84 | none for the Job 1 executor shape | Job 3 cross-channel detector and real-runtime proof remain outside this feature; F3 as a whole is not operationally proven |
| `.specs/features/hermes-linkedin-cadence/plan.md` | yes - AC1-12 and both Landing doors | none | AC8 is not durable across a transient CRM failure; AC5 reread has no mutation-resistant proof |
| `.specs/features/thunder-os-v1/design.md` | yes - delegated authority boundary used by the implementation | none | production activation remains separately gated |

## Checks

All named proofs were rerun at `d20b6d6d8f97ace5b5cd5f2256e8c4a47e70dfbe` in one 36-test focused invocation plus the two script proofs. The provider-control regression batch added 102 passing tests. C15 fails at the journey level even though its adapter-only happy path is green.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | exact immutable D0 authority | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:134-149` - Ygor, four steps, replay, changed and expired cases | PASS |
| C2 | pause/scope/delegation/cap cause zero writes | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:151-169` - four rejection cases and zero executes/completions | PASS |
| C12 | approval route persists without effects and rejects wrong authority/config | focused batch, exit 0 | `_ferramentas/test_thunder_os_connectsafely_panel_wiring.py:165-191` - `201`, no job, non-Ygor rejection and unavailable configuration | PASS |
| C3 | exact task/status/cadence gate | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:172-189` - early, complete, malformed, contact, replied and cadence mismatches | PASS |
| C4 | relationship gate matches invite/message | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:192-208` - all four relationship states and execute counts | PASS |
| C5 | no LLM/model/prompt dependency | determinism script, exit 0 | `scripts/check_hermes_linkedin_determinism.py:7-23` - AST/source checks for the runtime boundary | PASS |
| C6 | confirmed receipt completes exact task and audits delegate | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:211-217` - exact task/receipt and actor | PASS |
| C7 | nonconfirmed stays open and uncertain never redelivers | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:219-245` - open task, alert, one execute and reconciled completion | PASS |
| C8 | confirmed replay is idempotent | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:248-252` - one execute, one completion and one cap unit | PASS |
| C13 | delegated invite reconciliation uses exact D0 and no second send | focused batch, exit 0 | `_ferramentas/test_thunder_os_connectsafely_panel_wiring.py:332-379` - expired one-time review, current D0, one reconcile and delegated audit | PASS |
| C9 | explicit bounded secret-free configuration | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:55-72` - rejected paths/bounds/secrets and disabled live example | PASS |
| C14 | installed revision binding and 30-90 second spacing | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:85-97` - mismatch exit 2 and one 37-second delay | PASS |
| C15 | CRM status/cadence, durable uncertain state and receipt note | adapter test exit 0; adversarial journey probe exposes retry loss | `_ferramentas/test_thunder_os_hubspot_tasks.py:58-67` proves only the successful PATCH/readback; runner replay at `_ferramentas/thunder_os/hermes_linkedin_cadence.py:198-224` omits a failed transition retry | FAIL |
| C10 | synthetic end-to-end and sanitized revision summary | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:74-83` - completed outcome, revision, no secret and stored receipt | PASS |
| C11 | exact HubSpot task contract and receipt-bound completion | focused batch, exit 0 | `_ferramentas/test_thunder_os_hubspot_tasks.py:30-43` and `:45-56` - portal/task/contact/cadence, one patch/note, changed task and bad receipt block | PASS |

## Coverage

Coverage was recomputed from the plan, the canonical cadence source, the runtime branches and the provider/ledger gates.

| Set (size) | Recomputed from | Member -> proof | Unproven |
| --- | --- | --- | --- |
| authority failures (7) | plan AC2 and dispatch gates | wrong actor C1; changed C1; incomplete C1; expired C1; pause C2; scope C2; cap C2 | - |
| task mismatch classes (6) | plan AC5 | early C3; completed C3; malformed C3; wrong contact C3; replied/status C3; wrong cadence C3 | changing CRM state between initial and pre-queue reads |
| relationship states (4) | provider enum and AC6 | NOT_CONNECTED C4; CONNECTED C4; INVITATION_SENT C4; INVITATION_RECEIVED C4 | - |
| provider outcomes (5) | console result normalization | confirmed C6; blocked C7; rejected C7; failed C7; uncertain C7/C13 | - |
| replay cases (3) | AC3, AC8, AC9 | approval C1; confirmed task C8; uncertain invitation C13 | failed `cad_status=incerto` transition on uncertain replay |
| runtime modes (3) | config parser | disabled C9; live C9; synthetic C10 | - |
| HubSpot write gates (7) | task/contact adapter | portal C11; owner C11; contact C11; subject/body C11; cadence C15; receipt C11; readback C15 | retry/readback after transient uncertain-status failure |
| Job 1 stop/effect controls (8) | canonical spec lines 55-62 | `cad_status` C3/C15; cadence C3; touch reply/suppression provider batch; daily/weekly provider quota batch; receipt note C11/C15; alert C7; spacing C14; revision C14 | cross-channel replies depend on Job 3 updating `cad_status`; Job 3 is outside this recut |
| approval route statuses (3) | plan Surface | `201` C12; `400` C12; `503` C12 | - |

## Test policy rows

| Row | Files it classifies | Required proof | Expectation met |
| --- | --- | --- | --- |
| external dispatch authority | cadence/application/provider | unit tests with spy ports; every named rejection proves zero calls | yes for the named Job 1 gates |
| task completion | cadence/task adapter | producer-to-consumer synthetic test; receipt and exact task ID asserted | yes - C6, C11, C13 |
| uncertainty | cadence/reconciliation | replay/reconciliation test; a second run never sends and only exact delegated reconciliation may complete | yes - C7, C13 |
| determinism | cadence/runtime source | AST/source boundary check; no LLM/model/prompt dependency | yes - C5 |
| runtime config | parser/runtime | parser tests; every effectful mode and secret reference validated | yes - C9, C14 |

## Swept existing

- Authorization: D0 hash/version, delegated scope/version, global pause, cap, CRM status/cadence and relationship are rechecked. Job 1's second CRM read exists but lacks a race regression test.
- Idempotency/concurrency: canonical touch ownership and repository transactions protect dispatch; sequential replay and exact invitation reconciliation are proven. A transient post-provider CRM write is not modeled as a retryable state.
- Dependency failure: provider uncertainty is fenced from a second send. HubSpot completion after a confirmed receipt remains retryable, but HubSpot `incerto` after an uncertain receipt does not.
- State transitions: local alert and local `uncertain` persist; successful CRM `incerto` readback works; failed CRM transition is lost from the replay path.
- Observability: task note contains copy/time/receipt and alerts are local. No installed service, timer journal, provider receipt or HubSpot readback was inspected.
- Scope boundary: this recut implements Job 1. It blocks on freshly reread `cad_status`; the separate Job 3 detector that turns email/LinkedIn replies into cross-channel stop state is not implemented or runtime-proven here.

## Faults injected

Five behavior-level mutations were applied one at a time in detached scratch worktree `/tmp/thunder-os-hermes-verify-d20b6d6`, then removed. Four were killed and one survived. The scratch was clean before removal and the real tree matched its clean baseline before this report write.

| Mutation | Location | Killed |
| --- | --- | --- |
| remove the second CRM task read before queueing | `_ferramentas/thunder_os/hermes_linkedin_cadence.py:238` | no - C3 stayed green |
| bypass cadence ID comparison | `_ferramentas/thunder_os/hermes_linkedin_cadence.py:171` | yes - C3 failed |
| record delegated reconciliation as `ygor` | `_ferramentas/thunder_os/connectsafely_application.py:457-460` | yes - C13 failed |
| bypass installed revision mismatch | `_ferramentas/thunder_os/hermes_linkedin_runtime.py:134-137` | yes - C14 failed |
| remove inter-task sleep | `_ferramentas/thunder_os/hermes_linkedin_runtime.py:171-173` | yes - C14 failed |

## Acceptance criteria

| Criterion | Verdict | Evidence |
| --- | --- | --- |
| AC1-3 exact immutable D0 authority | ATENDIDOS | C1, C2, C12 |
| AC4 exact deterministic task mapping | ATENDIDO at source/synthetic level | C3, C5 |
| AC5 stop states and pre-queue reread | NÃO VERIFICADO integralmente | code rereads, but the representative mutation survived |
| AC6 relationship gate | ATENDIDO | C4 |
| AC7 receipt-bound completion, note and delegated audit | ATENDIDO at source/synthetic level | C6, C11, C13, C15 happy path |
| AC8 open task, alert, `incerto`, no resend, exact reconciliation | NÃO ATENDIDO | no resend/reconciliation pass; transient HubSpot failure leaves `cad_status` active without retry |
| AC9 confirmed replay idempotency | ATENDIDO for sequential replay | C8; real concurrent provider execution unverified |
| AC10-11 disabled/bounded runtime, revision and spacing | ATENDIDOS at source level | C9, C14; no installation/activation |
| AC12 synthetic journey and sanitized summary | ATENDIDO | C10 |

## Gate

- Focused C1-C15 invocation: 36 passed, 0 failed.
- Provider quota/reply/suppression regression invocation: 102 passed, 0 failed.
- `python3 scripts/check_hermes_linkedin_determinism.py --check test_runtime_is_deterministic` - `hermes_linkedin_runtime_deterministic: ok`.
- `python3 scripts/check_source_boundary.py` - `source boundary ok: 155 tracked paths`.
- `systemd-analyze verify runtime/systemd/thunder-os-hermes-linkedin.service runtime/systemd/thunder-os-hermes-linkedin.timer` - exit 0.
- `validate_plan.py` - 0 errors, 0 warnings.
- `validate_checks.py` - 0 errors, 2 warnings (C5 script selector and C15 whole-module selector).
- Adversarial uncertain-CRM probe - reproduced a non-retried `cad_status` transition.
- Fault injection - 4 killed, 1 survived.

## Limits

Source, local fixtures and synthetic ports only. No credentials, provider, HubSpot, LinkedIn, production database, installed service, deploy, restart, push or external effect was used. The checked-in live example remains disabled. The Job 3 cross-channel reply detector and the rest of canonical F3 are outside this Job 1 executor recut and remain unverified; a green Job 1 revision must not be reported as the full cadence operating in production.
