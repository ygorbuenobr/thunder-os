# Apollo + LinkedIn: simulated design

**Spec**: `.specs/features/apollo-linkedin/spec.md`
**Status**: simulated slice, not production-ready

## Architecture Overview

Simulated Apollo task → reader → eligibility and campaign/account checks → atomic scoped reservation in the installation's single official store → executor lock for the selected account → immediate open-task reread → simulated ConnectSafely action → simulated Apollo completion → explicit result. Apollo remains owner of real sequences and emails. No simulated component is wired to production.

## Code Reuse Analysis

`apollo_sequence_readonly.py` is bounded to sequence tools and must not silently gain write capability. `hermes_linkedin_cadence.py` contains useful safety patterns but consumes HubSpot-specific tasks; do not impersonate those tasks. Existing provider and repository tests guide test isolation and state semantics. Do not modify Hermes core or introduce a service, queue or vendor dependency.

## Components

- Reader accepts synthetic task observations and exposes confirmed attributes and missing evidence.
- Simulator validates explicit company, Apollo organization, campaign, task, recipient, default account, approval and response coverage.
- The installation has exactly one official store, shared by all executor processes; missing or unavailable official storage refuses initialization. Independent authoritative stores for one installation are unsupported.
- Reservation protects `(company, Apollo task ID, action type)` before the effect; an executor lock serializes execution by selected LinkedIn account. Immediately before the effect, reread the task and fail closed if it is missing, changed, closed or unavailable.
- A confirmed action and Apollo completion are separate durable simulation states; uncertain outcome requires intervention and no replay of LinkedIn.
- Fake Apollo and LinkedIn providers are test-only; no adapter accepts production credentials or performs network operations.

## Data Models

Reservation uniqueness is company + Apollo task ID + action type; organization, campaign, sequence position, recipient and selected account are independently validated authorization bindings and do not permit duplicate reservations. A repeat with conflicting identity or changed input is blocked, not merged. Follow-up steps remain distinct operations. Approval refers to campaign version and allowed rendered content; no unsupported equivalence is inferred.

## Error Handling Strategy

| Condition | Result |
| --- | --- |
| Empty observations | Empty list; no provider call |
| Missing owner/text/identity, wrong account, stale response | Block with reason |
| Unsupported type, closed or skipped task | Block with reason |
| Provider timeout / uncertainty | Needs attention; do not complete Apollo or retry LinkedIn |
| Apollo completion failure after confirmation | Reconcile Apollo only; never repeat LinkedIn |
| Apollo deleted / no verified readback | Do not assert completion |
| Official store absent or unavailable | Refuse initialization; no effect |
| Account lock held by another process | Do not perform a concurrent effect for that account |
| Immediate task reread unavailable or not open | Block; no effect |

## Risks & Concerns

Actual sequence-generated task search provided ID, type, status, due date, contact and sequence position; detail did not return owner or invitation template. These omissions cannot be inferred from a fixture. Separate-process tests establish only the simulated single-store contract. A real provider may change state between checks; no cross-provider exactly-once guarantee is claimed. Live responses, authorization scope, completion and returned proof remain unverified.

## Tech Decisions

Use Python and existing test conventions. Model multi-account routing explicitly; defer full multi-company product. No Node backend or Mastra in this deterministic slice. Live Apollo and ConnectSafely transports, UI and scheduler remain out of scope.
