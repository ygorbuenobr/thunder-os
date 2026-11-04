# Apollo + LinkedIn: simulated task flow

## Problem Statement

Apollo owns sequences and email, but the OS cannot yet process its manual LinkedIn tasks. This slice proves task selection and execution safety with simulated providers only; it does not enable live integration.

## Goals

- [ ] Interpret a bounded simulated Apollo task and show an actionable outcome.
- [ ] Simulate an approved LinkedIn action and completion without duplicate effects.

## Out of Scope

| Feature | Reason |
| --- | --- |
| Live provider access, scheduler, deployment, Hermes wiring | This slice is exclusively simulated. |
| Native email, generic CRM, complete multi-tenant platform | Separate product work. |
| Concurrent executors using two independent authoritative stores | One official store per installation is the supported operating model. |

## Assumptions & Open Questions

| Assumption / decision | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Campaign account | Explicit campaign default | Avoid silent sender changes | y |
| Authorization | One approval of campaign and rules | User decision; no real send authorized here | y |
| Missing user or personalized text | Block when needed | Actual sequence-generated task detail omitted both | n |
| Task content and owner verification in production | Open, no inferred value | A future live validation must establish authoritative source | n |
| Reply coverage | Missing or stale coverage blocks | No action based on incomplete evidence | y |
| Authoritative storage | Exactly one official shared store per installation | User decision; all executor processes must use it | y |
| Uniqueness | Company + Apollo task ID + action type | User decision; organization and account are validated bindings, not alternative uniqueness keys | y |

**Open questions:** recorded above; actual sequence content, owner and completion semantics remain unverified.

## User Stories

### P1: Read and simulate an approved task

**User Story**: As operator, I want the OS to identify an Apollo task and simulate the correct LinkedIn action so I can validate the future journey without a real send.

**Why P1**: It validates the critical path without connecting live execution.

**Acceptance Criteria**:

1. WHEN an observed task includes task ID, scheduled status, supported type, contact, profile, sequence and position THEN the reader SHALL expose those values without fabricating missing fields. (AL-01)
2. IF the task list is empty THEN the reader SHALL return an empty result without an execution. (AL-02)
3. IF task identity, campaign association, owner or required rendered text cannot be established THEN the executor SHALL block and report the reason. (AL-03)
4. IF a task is not scheduled or its type is unsupported THEN the executor SHALL block it without invoking the simulated action. (AL-04)
5. IF the campaign is not approved or its default LinkedIn account is absent or mismatched THEN the executor SHALL block without switching accounts. (AL-05)
6. IF a reply is observed or response coverage is unavailable or stale THEN the executor SHALL block without invoking the simulated action. (AL-06)
7. WHEN an approved eligible task is processed THEN the executor SHALL reserve its scoped identity before invoking the simulated action. (AL-07)
8. IF a task is processed repeatedly or concurrently THEN the executor SHALL invoke the simulated LinkedIn action at most once for that task identity. (AL-08)
9. IF the simulated LinkedIn outcome is uncertain or rejected THEN the executor SHALL leave the Apollo task incomplete. (AL-09)
10. WHEN the simulated LinkedIn outcome is confirmed THEN the executor SHALL attempt simulated Apollo completion. (AL-10)
11. IF Apollo completion fails or is uncertain after a confirmed LinkedIn action THEN a retry SHALL reconcile Apollo without repeating LinkedIn. (AL-11)
12. IF Apollo completion returns a deleted task or unverified state THEN the executor SHALL not report confirmed completion. (AL-12)
13. The system SHALL use only simulated providers in this slice. (AL-13)
14. IF the official store for the installation is missing or unavailable THEN the executor SHALL refuse initialization. (AL-17)
15. The system SHALL enforce one reservation per company, Apollo task ID and action type in the official store. (AL-18)
16. WHEN an eligible task is processed THEN the executor SHALL acquire an executor lock for its LinkedIn account before the simulated external effect. (AL-19)
17. WHEN an executor is about to perform the simulated effect THEN the executor SHALL reread the task and verify it remains open. (AL-20)
18. IF that reread is unavailable, incomplete or no longer open THEN the executor SHALL block without invoking the simulated effect. (AL-21)

**Independent Test**: isolated fake-provider tests cover eligible and blocked tasks, empty source, concurrency, timeout, replay and completion semantics. Separate OS processes using the same temporary official store contend for the same task and account lock; missing official storage prevents initialization.

## Edge Cases

- IF a personalized field contains unresolved placeholders THEN the executor SHALL block the action. (AL-14)
- IF the task or campaign changes before simulated execution THEN the executor SHALL block stale authorization. (AL-15)
- IF organization or company association mismatches THEN the executor SHALL block the task. (AL-16)

## Requirement Traceability

| Requirement ID | Story | Phase | Status |
| --- | --- | --- | --- |
| AL-01 | Read | Tasks | Pending |
| AL-02 | Empty input | Tasks | Pending |
| AL-03 | Missing evidence | Tasks | Pending |
| AL-04 | State and type | Tasks | Pending |
| AL-05 | Approval and account | Tasks | Pending |
| AL-06 | Reply coverage | Tasks | Pending |
| AL-07 | Reserve | Tasks | Pending |
| AL-08 | Deduplicate | Tasks | Pending |
| AL-09 | Provider outcome | Tasks | Pending |
| AL-10 | Complete | Tasks | Pending |
| AL-11 | Reconcile | Tasks | Pending |
| AL-12 | Deleted task | Tasks | Pending |
| AL-13 | Fake only | Tasks | Pending |
| AL-14 | Placeholders | Tasks | Pending |
| AL-15 | Changed task | Tasks | Pending |
| AL-16 | Isolation | Tasks | Pending |
| AL-17 | Official store availability | Tasks | Pending |
| AL-18 | Scoped uniqueness | Tasks | Pending |
| AL-19 | Account executor lock | Tasks | Pending |
| AL-20 | Fresh task readback | Tasks | Pending |
| AL-21 | Readback failure | Tasks | Pending |

## Success Criteria

- [ ] Every scoped acceptance criterion has direct test evidence.
- [ ] No production network, credentials, live provider, scheduler or data is used.
- [ ] Independent review validates implementation and reports any gap.

## T3: isolated ConnectSafely account read (design; not enabled)

The simulation-only requirements above remain unchanged. This separate T3 design prepares a safe account-status read while keeping the independently authorized ConnectSafely action path. It does not authorize a real send, provider mutation, production network in tests, or any change to the protected Apollo fixture.

1. **RO-01:** WHEN the panel requests account status THEN it SHALL only access an account-status interface with no capability to invoke a ConnectSafely write and no access to a send-capable credential.
2. **RO-02:** IF the provider credential is not proven read-scoped THEN isolation SHALL be enforced outside the panel process and security identity; a GET-only wrapper around a write-capable key is not proof of this boundary.
3. **RO-03:** WHEN account status is queried THEN company, account and expected public identity SHALL be bound explicitly; missing, mismatched, stale or unavailable evidence SHALL produce a visible blocked/unknown result rather than a ready state.
4. **RO-04:** IF the broker, credential, configured account mapping, transport or authorization is missing, inconsistent or times out THEN the panel SHALL fail closed with a bounded, visible status; it SHALL not fall back to the send client or default account.
5. **RO-05:** IF a caller requests a mutation, arbitrary URL, arbitrary account, or malformed/oversized input THEN the isolated read interface SHALL reject it before any provider call. Secrets and unnecessary provider data SHALL not enter the response or logs.
6. **RO-06:** A successful account read SHALL NOT prove or expose the separately verified execution-credential binding, a provider delivery receipt or approval to send.
7. **RO-07:** Tests SHALL use fake credentials and fake provider responses; they SHALL exercise normal and adversarial inputs without real network or external actions. A distinct staging test SHALL establish OS identity separation and peer authorization before any operational claim.
8. **RO-08:** The existing action path remains independently available only to its authorized executor and retains campaign approval, account binding, reservation, locking, kill switch and explicit real-send authorization.
