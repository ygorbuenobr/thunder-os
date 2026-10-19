# Hermes LinkedIn cadence executor verification

**Verdict**: FAIL
**Profile**: standard
**Diff range**: 1ee6ead..1272988d0746e3d74fd54c8873d77210dcb7bd49
**Round**: 1 - full
**Verifier**: independent sub-agent (author != verifier)

## Material findings

1. **MAJOR - delegated invitations cannot reach receipt-bound completion.** The real ConnectSafely client intentionally returns `uncertain` after the invite POST (`_ferramentas/thunder_os/connectsafely_client.py:429`). The only invitation reconciliation path rejects every job whose actor is not exactly `ygor` (`_ferramentas/thunder_os/connectsafely_application.py:375-377`), while delegated jobs are persisted as `hermes (delegado)`. The cadence runner never calls reconciliation; it only rereads an already-updated job (`_ferramentas/thunder_os/hermes_linkedin_cadence.py:181-194`). Observed result: D1 can dispatch and be fenced, but cannot be reconciled by the implemented Hermes path into the confirmed receipt required to complete the HubSpot task. Expected: the same immutable delegated authority must be accepted by a bounded reconciliation path, with no second send.
2. **MAJOR - the worker omits binding F3 stop and state controls.** The approved source requires checking `cad_status`, replies in any channel, the touch ledger, daily/weekly quota, then writing a task note/alert and setting `cad_status=incerto` on uncertainty (`/root/.hermes/profiles/ygor-personal/brain/projetos/thunder-os/cadencia-multicanal/especificacao.md:55-62`). The HubSpot adapter fetches task fields only (`_ferramentas/thunder_os/hubspot_tasks.py:45-47`) and its only mutation is task status (`_ferramentas/thunder_os/hubspot_tasks.py:122-138`); the run loop executes tasks back-to-back without the specified 30-90 second spacing (`_ferramentas/thunder_os/hermes_linkedin_runtime.py:148-157`). A contact marked `respondeu`/`encerrado`, or with an email reply not yet represented in the LinkedIn ledger, is therefore not blocked by this executor.
3. **MAJOR - persisted `cadence_id` is not part of task authority.** Approval stores `cadence_id`, but `_match` selects on active approval + contact + marker only (`_ferramentas/thunder_os/hermes_linkedin_cadence.py:153-163`). The HubSpot task port exposes no sequence/cadence identity. An unrelated task with the same subject/body on the same contact is indistinguishable and can authorize the frozen effect. Expected: bind each approved step to an exact HubSpot task/enrollment identity or an independently verified cadence marker that the task reader rechecks.
4. **MODERATE - the emitted source revision is configuration text, not runtime evidence.** `load_config` accepts any non-empty string (`_ferramentas/thunder_os/hermes_linkedin_runtime.py:33-35`) and `run_once` echoes it (`_ferramentas/thunder_os/hermes_linkedin_runtime.py:148-157`). C10's test proves only that `fixture-revision` is echoed, not that the running bytes are `1272988`. This cannot support a revision-bound runtime claim.

## Checks

All named proofs were rerun at `1272988d0746e3d74fd54c8873d77210dcb7bd49` in one focused unittest invocation plus the two script proofs. The unittest batch ran 33 tests, including all named methods, with 0 failures.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | exact immutable D0 authority | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:129` - stored approval; `:133-143` - wrong actor, incomplete, changed and expired rejected | PASS |
| C2 | pause/scope/delegation/cap cause zero writes | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:145-163` - four rejection cases and exact zero executes/completions | PASS |
| C12 | approval route persists without effects and rejects authority/config gaps | focused batch, exit 0 | `_ferramentas/test_thunder_os_connectsafely_panel_wiring.py:181-191` - `201`, no jobs, non-Ygor rejection and `503` | PASS |
| C3 | only exact due task contract is selected | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:166-183` - early, completed, malformed and wrong-contact table with zero effects | PASS |
| C4 | relationship gate matches invite/message kind | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:185-201` - four states for both action kinds and exact execute count | PASS |
| C5 | deterministic source boundary | `check_hermes_linkedin_determinism.py`, exit 0 | `scripts/check_hermes_linkedin_determinism.py:7-23` - AST rejects named model/prompt dependencies | PASS |
| C6 | every confirmed ConnectSafely effect can complete exact task and audit delegate | focused batch, exit 0 but incomplete action coverage | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:204-210` proves a fake immediate receipt only; real invite is always uncertain at `_ferramentas/thunder_os/connectsafely_client.py:429` and delegated reconciliation is rejected at `_ferramentas/thunder_os/connectsafely_application.py:375-377` | FAIL |
| C7 | non-confirmed outcomes stay open and uncertainty is fenced | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:212-236` - blocked/denied/uncertain and one-call replay | PASS |
| C8 | confirmed sequential replay is idempotent | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_cadence.py:239-243` - one execute, one completion, one cap unit | PASS |
| C9 | bounded, secret-free explicit config | focused batch, exit 0 | `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:51-66` - rejected mutations, disabled example and disabled live candidate | PASS |
| C10 | synthetic once journey plus revision-bound sanitized summary | focused batch, exit 0 but assertion is weaker than claim | `_ferramentas/test_thunder_os_hermes_linkedin_runtime.py:68-77` - synthetic completion and no literal `secret`; `source_revision` is only echoed fixture input | FAIL |
| C11 | exact HubSpot task contract and receipt gate completion | focused batch, exit 0 | `_ferramentas/test_thunder_os_hubspot_tasks.py:24-34` - exact task/receipt and one patch; `:36-46` - changed subject and bad receipt cause zero patch | PASS |

Level judgment: unit and synthetic boundaries are well exercised for the covered message path. There is a sampling/level gap at the action boundary: the only completion proof uses an injected generic `sent` result, while the production invite action requires a distinct reconciliation protocol. No concurrency fault was injected because the current failure is earlier and deterministic; sequential idempotency is proven, but parallel `run_task` observation remains unverified.

## Coverage

Coverage was recomputed from the plan, the approved cadence source, and the production provider branches rather than copied from `checks.md`.

| Set (size) | Recomputed from | Member -> proof | Unproven |
| --- | --- | --- | --- |
| authority failures (7) | plan AC2 and approval/dispatch gates | wrong actor C1; changed C1; incomplete C1; expired C1; pause C2; scope C2; cap C2 | - |
| task mismatch classes (4) | plan AC5 | early C3; completed C3; malformed C3; wrong contact C3 | - |
| relationship states (4) | provider enum at `connectsafely_client.py:216-223` | NOT_CONNECTED C4; CONNECTED C4; INVITATION_SENT C4; INVITATION_RECEIVED C4 | - |
| provider outcomes (5) | console normalized result branches | confirmed C6; blocked C7; rejected C7; failed C7; uncertain C7 | confirmed invite path |
| LinkedIn action completion paths (2) | `STEP_CONTRACT` and provider send branches | DM immediate receipt C6 | invite reconciliation under delegated authority |
| replay cases (2) | plan AC3/AC9 | approval C1; confirmed task C8 | - |
| runtime modes (3) | config parser | disabled C9; live C9; synthetic C10 | - |
| HubSpot write gates (5) | task adapter | portal C11; owner C11; contact C11; unchanged subject/marker C11; receipt C11 | - |
| approved F3 pre-send controls (5) | cadence source lines 55-62 | touch-ledger and provider quota are delegated to existing ConnectSafely path | `cad_status`; reply in any channel; weekly invite ceiling evidence; alert/state mirror |
| approval route statuses (3) | plan route surface | `201` C12; `400` C12; `503` C12 | - |

The plan's `Surface` route statuses and both `Landing` doors were included. The additional action-path and F3-control sets expose omissions hidden by the original coverage join.

## Test policy rows

| Row | Files it classifies | Required proof | Expectation met |
| --- | --- | --- | --- |
| external dispatch authority | cadence/application/provider | every named rejection proves zero calls | no - the listed local authority rejections pass, but cadence identity and approved F3 stop-state controls are absent |
| task completion | cadence/task adapter | producer-to-consumer synthetic test with receipt and exact task ID | no - DM-like synthetic path passes; delegated invitation completion has no valid reconciliation path |
| uncertainty | cadence replay | second run never calls provider | yes - C7 |
| determinism | cadence/runtime source | no LLM/model/prompt dependency | yes - C5 |
| runtime config | parser/runtime | every effectful mode and secret reference validated | no - schema/secret references are bounded, but claimed source revision is not bound to running code |

## Swept existing

- Authorization: D0 hash/version, delegation scope, pause and cap are rechecked, but task selection does not recheck `cadence_id` and F3 contact state is absent.
- Concurrency: the ConnectSafely effect hash/canonical ledger protects identical effects after reservation; C8 covers sequential replay. Parallel cadence-task claim behavior was not proven.
- Dependency failure: post-dispatch failures become uncertain and are fenced; HubSpot completion failure remains `provider_confirmed` for readback retry.
- State transitions: local `uncertain` is terminal for automatic send, but it is not mirrored to HubSpot `cad_status=incerto` as the approved source requires.
- Runtime/config: candidate systemd units passed `systemd-analyze verify`; they were not installed or started. The unit and live example both leave effects disabled.

## Faults injected

Five behavior-level mutations were applied one at a time in detached scratch worktree `/tmp/thunder-os-hermes-verify-1272988`. All were killed. The scratch was returned clean and the real-tree porcelain stayed equal to its baseline.

| Mutation | Location | Killed |
| --- | --- | --- |
| invert Ygor approval-authority condition | `_ferramentas/thunder_os/hermes_linkedin_cadence.py:91` | yes - C1 failed |
| invert due-time comparison | `_ferramentas/thunder_os/hermes_linkedin_cadence.py:144` | yes - C3 failed |
| require `delivered` instead of `sent` for completion | `_ferramentas/thunder_os/hermes_linkedin_cadence.py:214` | yes - C6 failed |
| permit `max_tasks_per_run=31` | `_ferramentas/thunder_os/hermes_linkedin_runtime.py:38` | yes - C9 failed |
| remove post-dispatch task-contract comparison | `_ferramentas/thunder_os/hubspot_tasks.py:129` | yes - C11's changed-task case failed |

## Acceptance criteria

| Criterion | Verdict | Evidence |
| --- | --- | --- |
| AC1-3 exact immutable D0 authority | ATENDIDOS | C1, C2, C12 |
| AC4-5 exact due task mapping | NÃO ATENDIDOS integralmente | C3 proves subject/marker/contact, but stored `cadence_id` is never matched to task/enrollment |
| AC6 relationship gate | ATENDIDO | C4 |
| AC7 receipt-bound completion and audit | NÃO ATENDIDO | C6 fails for the invite action |
| AC8 non-confirmed stays open and uncertainty fences retry | ATENDIDO | C7 |
| AC9 confirmed replay idempotency | ATENDIDO for sequential replay | C8; parallel observation unverified |
| AC10-11 bounded disabled-by-default configuration | ATENDIDOS at source level | C9 and config inspection; no activation performed |
| AC12 synthetic journey and sanitized summary | NÃO ATENDIDO integralmente | journey/sanitization passes, but source revision is caller text rather than verified runtime identity |

## Gate

- Focused C1-C4/C6-C12 unittest batch - 33 passed, 0 failed.
- `python3 scripts/check_hermes_linkedin_determinism.py --check test_runtime_is_deterministic` - `hermes_linkedin_runtime_deterministic: ok`.
- `python3 scripts/check_source_boundary.py` - `source boundary ok: 154 tracked paths`.
- `systemd-analyze verify runtime/systemd/thunder-os-hermes-linkedin.service runtime/systemd/thunder-os-hermes-linkedin.timer` - exit 0.
- Prior full-suite evidence supplied by the author: 372 passed; separate PostgreSQL quarantine proof did not start because its provisioned socket was absent. That prior result was not substituted for any focused check above.

## Limits

Source/synthetic verification only. No provider, credential, HubSpot, LinkedIn, production database, deploy, restart, push or external effect was used. The checked-in live example and systemd unit remain disabled, so no runtime/provider success is claimed.
