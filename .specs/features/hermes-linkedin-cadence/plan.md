# Hermes LinkedIn cadence executor

Sources:

- Ygor's decision on 2026-10-19: ConnectSafely performs LinkedIn cadence effects and Hermes is the deterministic executor; Claude is not a runtime dependency.
- `brain/projetos/thunder-os/cadencia-multicanal/especificacao.md`: the whole cadence is approved at D0; LinkedIn steps run automatically and HubSpot tasks are completed only after a provider receipt.
- `.specs/features/thunder-os-v1/design.md`: delegated effects require a separate scope, daily ceiling, kill switch and audit actor `hermes (delegado)`.
- Ygor's superseding decisions on 2026-10-29: tasks do not require Sequences enrollment; add exact-preview human campaign confirmation and identity-divergence confirmation; pre-dispatch fresh suppression reads are scoped to the canary contacts. Email canary is owner-only; LinkedIn must be a distinct user-designated profile. No dispatch without exact text and a new explicit approval; Mexico stays untouched through 2026-10-30. Only the separately reviewed session hotfix has production approval.

## Problem

Thunder OS already has a guarded ConnectSafely write path and a persisted Hermes delegation, while HubSpot contains the LinkedIn task stages of `Cadência Thunder v1`. They are not connected: no deterministic worker consumes an approved HubSpot task, proves that its exact recipient and copy belong to a human-approved cadence, dispatches through ConnectSafely, and completes the task only after a confirmed receipt. The current manual ConnectSafely execution scope cannot safely stand in for that contract.

After this change, a bounded Hermes runner can execute exactly one due HubSpot LinkedIn task from an active D0 approval, without an LLM call, and leave every blocked, failed or uncertain task open. Activation, deployment and a real provider canary remain separate decisions.

## Out of scope

| Excluded | Why |
| --- | --- |
| Saving or editing the HubSpot sequence | The nine-stage sequence already exists; this change consumes its tasks. |
| Campaign email and WhatsApp dispatch | Only owner-email SEQD canary preparation/readback wiring is added by the 2026-10-29 decision. Actual sending still needs separate exact-text approval. |
| Claude as executor or copy generator | The selected runtime is deterministic Hermes; copy is frozen at D0. |
| Real LinkedIn, HubSpot or production writes during verification | No external canary, deploy or restart was authorized. |
| Reusing Thunder Cockpit/GTM | Thunder OS is the resolved product boundary. |

## Assumptions

| Assumption | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Authority | Ygor approves an exact contact-scoped package at D0, plus the exact task preview/campaign confirmation; identity divergence requires a separate observation-bound decision. | D0 alone no longer grants dispatch authority; the canary still requires a new explicit exact-text approval. | y |
| Runtime model | Conventional deterministic worker, no model or prompt call after approval. | Availability cannot depend on Claude credits and copy must not drift. | y |
| Task contract | Exact subjects/body markers: `LinkedIn: convite`/`cad_li_0`, then `msg 1..3`/`cad_li_1..3`. | These are the saved HubSpot stages and make malformed tasks fail closed. | y |
| Default daily cap | 30 delegated LinkedIn cadence effects/day, configurable downwards. | Matches the current bounded operating target; provider quota is still checked independently. | y |

**Open questions:** none for source implementation. Production activation still requires an explicit release/deploy decision and a named canary target.

## Criteria

### S1: Exact D0 authority (P1)

**Acceptance Criteria**

1. WHEN Ygor approves a contact-scoped LinkedIn package THEN Thunder OS SHALL persist the HubSpot contact ID, cadence ID, four exact task markers, exact ConnectSafely plans and their hashes, expiry and approver.
2. IF the actor is not Ygor, the step set is incomplete/duplicated, a plan changes, the approval expires, the global pause is active, or the Hermes `linkedin_cadence` delegation is disabled/out of scope THEN the runner SHALL perform zero provider and HubSpot writes.
3. WHEN the exact same approval is replayed THEN Thunder OS SHALL return the existing authority; a different payload under the same approval ID SHALL conflict.

### S2: Deterministic task gate (P1)

**Acceptance Criteria**

4. WHEN one open due HubSpot task has the exact approved subject, marker, contact, active `cad_status` and persisted task-to-cadence binding THEN the runner SHALL select the matching frozen step without invoking an LLM or accepting generated copy. Sequences enrollment may be empty; it is not authority or a prerequisite.
5. IF the task is early, completed, unassociated, associated to another contact, malformed, from another cadence, or the contact is not `aprovado`/`em_cadencia` THEN it SHALL remain open and return a stable blocked reason. The CRM state SHALL be read again immediately before queueing so a reply/status change wins.
6. IF the invite step observes anything other than `NOT_CONNECTED`, or a message step observes anything other than `CONNECTED`, THEN it SHALL not dispatch. Exact-preview human campaign confirmation, fresh identity evidence (including explicit divergence confirmation where needed), fresh per-contact suppression reads and canonical duplicate/quota controls are independently required before dispatch.

### S3: Receipt-bound completion (P1)

**Acceptance Criteria**

7. WHEN ConnectSafely confirms the exact effect with a receipt THEN the runner SHALL persist an audit record with actor `hermes (delegado)` and complete only that HubSpot task with a note containing copy, time and receipt.
8. IF ConnectSafely blocks, rejects, fails or returns an uncertain outcome THEN the HubSpot task SHALL remain open and a local alert SHALL persist; an uncertain outcome SHALL set `cad_status=incerto`, be fenced from redelivery and allow only receipt reconciliation under the same delegated authority.
9. WHEN the same confirmed task is observed again THEN no second provider write or daily-cap consumption SHALL occur.

### S4: Bounded runtime configuration (P1)

**Acceptance Criteria**

10. The checked-in runner SHALL default to disabled external effects, require explicit absolute configuration paths and support a synthetic mode that cannot open provider or HubSpot sockets.
11. The live configuration contract SHALL name the HubSpot portal/owner, ConnectSafely account/public identity, polling bound, weekdays 08:00-19:00 Sao Paulo, random 30-90 second inter-action spacing, daily cap, kill switch and a revision file equal to the configured source revision; secrets SHALL be referenced by environment variable name, never stored in the file.
12. WHEN a `--once` synthetic run receives a valid fixture THEN it SHALL emit a sanitized revision/mode/outcome summary without provider sockets or secrets. Missing identity evidence SHALL block; with explicit injected synthetic identity evidence it SHALL stop at the human-confirmation preview, not auto-complete. The separate authenticated synthetic panel-to-delegate proof SHALL complete the exact task only after the human confirmation and receipt. This supersedes the original auto-completing CLI fixture under the 2026-10-29 human-confirmation decision.

## Traceability

| ID | Slice | Criteria | Status |
| --- | --- | --- | --- |
| HLC-01 | S1 | 1-3 | implemented; independent verification pending |
| HLC-02 | S2 | 4-6 | implemented; independent verification pending |
| HLC-03 | S3 | 7-9 | implemented; independent verification pending |
| HLC-04 | S4 | 10-12 | implemented; independent verification pending |

## Flow

1. Ygor-approved package -> HLC-01 approval landing -> immutable approval state.
2. bounded HubSpot task reader -> exact subject/marker/contact gate -> frozen ConnectSafely plan.
3. ConnectSafely relationship read + canonical ledger preflight -> delegated queue authorization.
4. exact ConnectSafely job -> confirmed receipt -> HubSpot task completion + audit.
5. any non-confirmed outcome -> task stays open; uncertain attempt remains fenced for reconciliation.

## Relations

No SQL migration. New state kinds use the existing versioned `thunder_state` store. Existing canonical touch tables remain the effect authority.

## Surface

| Route | In | Out | Status |
| --- | --- | --- | --- |
| `POST /api/os/v1/linkedin-cadence-approvals` | exact contact/cadence/expiry/four-step package | persisted approval envelope | `201`, `400`, `503` |

## Observable

| Surface | Decision | Landing |
| --- | --- | --- |
| `--once` stdout | one sanitized JSON summary with revision, mode, task and outcome | AC 12 |
| approval API | exact package is visible before approval; response returns its review hash/version | AC 1-3 |
| persisted audit | actor `hermes (delegado)`, approval/task/receipt references | AC 7 |
| HubSpot task | completed only after a confirmed ConnectSafely receipt | AC 7-9 |
| blocked execution | stable local reason; original task stays open | AC 2, 5, 6, 8 |

## Landing

| One-way door | Literal shape | Alternative rejected |
| --- | --- | --- |
| Approval envelope | Contact-scoped, four-step exact plan hashes | Treating the generic `hermes` actor string or HubSpot task alone as authority. |
| Completion authority | ConnectSafely confirmed receipt | Completing on queue acceptance, HTTP acceptance or an uncertain invitation result. |
| Canary read-only wiring (2026-10-29) | Reuse the panel's hash-pinned live configuration; compose optional allowlisted readers even when execution is disabled, and inject the scoped SEQD transport into the existing console. No provider calls at composition. | Enabling effects to obtain read access, or silently retaining the unrestricted SEQD transport when a canary scope is configured. |

## Impact

| Front | What changes |
| --- | --- |
| domain | Hermes becomes the deterministic executor for approved LinkedIn cadence steps. |
| security | D0 approval, persisted delegation, global pause, cap and exact hashes are all rechecked before dispatch. |
| operations | Candidate timer can poll every 30 minutes after a separate deploy/activation. |
| UX | HubSpot task remains the human-visible operational trace; failures do not disappear as completed tasks. |
