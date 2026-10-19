# Hermes LinkedIn cadence executor checks

Profile: standard
Plan: `.specs/features/hermes-linkedin-cadence/plan.md`

## Intent

12 checks in 4 slices · 2 one-way doors · 0 open

## Checks

### S1 - Exact D0 authority

**C1** - Ygor can persist one complete exact four-step approval; non-Ygor, incomplete, changed and expired approvals fail closed (HLC-01, AC 1-3)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_exact_d0_approval_is_immutable _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

**C2** - Missing scope, disabled delegation, global pause and exhausted daily cap cause zero ConnectSafely and HubSpot writes (HLC-01, AC 2)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_authority_gates_have_zero_external_writes _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

**C12** - `POST /api/os/v1/linkedin-cadence-approvals` persists the exact Ygor package without provider calls or queued effects and fails closed for invalid authority/configuration (HLC-01, AC 1-3)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_linkedin_cadence_approval_route_persists_exact_human_package_without_provider_call _ferramentas.test_thunder_os_connectsafely_panel_wiring`
Status: complete

### S2 - Deterministic task gate

**C3** - Exact due subject/marker/contact maps to frozen copy while early, completed, malformed and wrong-contact tasks remain open (HLC-02, AC 4-5)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_task_contract_selects_only_exact_due_step _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

**C4** - Invite requires `NOT_CONNECTED`; messages require `CONNECTED`; every other relationship state causes zero dispatch (HLC-02, AC 6)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_relationship_gate_matches_step_kind _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

**C5** - Source boundary contains no LLM/model/prompt dependency and runtime accepts no generated copy field (HLC-02, AC 4)
Proof: `python3 scripts/check_hermes_linkedin_determinism.py --check test_runtime_is_deterministic`
Status: complete

### S3 - Receipt-bound completion

**C6** - A confirmed exact provider receipt completes only the selected HubSpot task and records `hermes (delegado)` (HLC-03, AC 7)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_confirmed_receipt_completes_task_and_audits_delegate _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

**C7** - blocked/rejected/failed/uncertain outcomes keep the task open, and uncertain is never automatically redelivered (HLC-03, AC 8)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_nonconfirmed_outcomes_stay_open_and_uncertain_is_fenced _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

**C8** - Reobserving a confirmed task is idempotent: one provider call, one completion, one cap unit (HLC-03, AC 9)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_confirmed_replay_is_idempotent _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

### S4 - Bounded runtime configuration

**C9** - Disabled/synthetic/live config validation rejects relative paths, embedded secrets, identity mismatch and unbounded polling; checked-in example remains disabled (HLC-04, AC 10-11)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_runtime_config_is_explicit_bounded_and_secret_free _ferramentas.test_thunder_os_hermes_linkedin_runtime`
Status: complete

**C10** - `--once` synthetic runtime completes one fixture task and prints a sanitized revision-bound summary without sockets or secrets (HLC-04, AC 12)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_synthetic_once_runs_end_to_end_without_network _ferramentas.test_thunder_os_hermes_linkedin_runtime`
Status: complete

**C11** - HubSpot adapter pins portal, owner, exact post-dispatch task contract and receipt before the sole completion patch (HLC-03, AC 7-9; HLC-04, AC 11)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_exact_portal_owner_task_and_receipt_bound_completion _ferramentas.test_thunder_os_hubspot_tasks`
Status: complete

## Coverage

| Set | Member -> proof | Unproven |
| --- | --- | --- |
| authority failures (7) | wrong actor C1 · changed C1 · incomplete C1 · expired C1 · pause C2 · scope C2 · cap C2 | - |
| task mismatch classes (4) | early C3 · completed C3 · malformed C3 · wrong contact C3 | - |
| relationship states (4) | NOT_CONNECTED C4 · CONNECTED C4 · INVITATION_SENT C4 · INVITATION_RECEIVED C4 | - |
| provider outcomes (5) | confirmed C6 · blocked C7 · rejected C7 · failed C7 · uncertain C7 | - |
| replay cases (2) | approval C1 · confirmed task C8 | - |
| runtime modes (3) | disabled C9 · live C9 · synthetic C10 | - |
| HubSpot write gates (5) | portal C11 · owner C11 · contact association C11 · unchanged subject/marker C11 · provider receipt C11 | - |
| approval route statuses (3) | `201` C12 · `400` C12 · `503` C12 | - |

## Test policy

| Decision | Required proof | Coverage expectation |
| --- | --- | --- |
| external dispatch authority | unit tests with spy ports | every named rejection proves zero calls |
| task completion | producer-to-consumer synthetic test | receipt and exact task ID asserted |
| uncertainty | replay test | a second run never calls provider |
| determinism | AST/source boundary check | no LLM/model/prompt import or identifier in runtime module |
| runtime config | parser tests | every effectful mode and secret reference validated |

## Swept

- validation: C1, C3, C9
- authorization: C1, C2
- failure modes: C2, C3, C4, C7
- idempotency: C1, C8
- concurrency: existing repository transaction plus C8 replay; no new parallel dispatcher
- data lifecycle: C6-C8
- dependency failure: C7
- state transitions: C3, C6-C8
- observability: C6, C10

## Handoff

One builder owns the bounded module, tests and candidate runtime files. A fresh verifier reviews the final committed diff and writes `verification.md`.
