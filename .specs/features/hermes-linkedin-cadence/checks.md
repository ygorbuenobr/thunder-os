# Hermes LinkedIn cadence executor checks

Profile: standard
Plan: `.specs/features/hermes-linkedin-cadence/plan.md`

## Intent

26 checks in 5 slices. C1-C17 retain their historical scope; C18-C26 add the 2026-10-29 reconciliation and canary decisions. Current final-revision verification is required; the old verification report is not proof of these additions.

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

**C3** - Exact due subject/marker/contact/status/cadence maps to frozen copy while early, completed, malformed, wrong-contact, replied and wrong-cadence tasks remain open (HLC-02, AC 4-5)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_task_contract_selects_only_exact_due_step _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

**C4** - Invite requires `NOT_CONNECTED`; messages require `CONNECTED`; every other relationship state causes zero dispatch (HLC-02, AC 6)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_relationship_gate_matches_step_kind _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

**C16** - A reply/status change arriving during the relationship read is caught by the immediate second CRM read and causes zero dispatch (HLC-02, AC 5-6)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_reply_arriving_during_relationship_read_blocks_dispatch _ferramentas.test_thunder_os_hermes_linkedin_cadence`
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

**C13** - An uncertain delegated invitation is reconciled under its unchanged D0 authority, never re-sent, and only then completes its exact HubSpot task (HLC-03, AC 8-9)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_delegated_invite_reconciles_under_exact_d0_authority _ferramentas.test_thunder_os_connectsafely_panel_wiring`
Status: complete

**C17** - A failed HubSpot `incerto` write, including PATCH applied plus failed readback, converges idempotently on replay while the provider job remains fenced from redelivery (HLC-03, AC 8-9)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_uncertain_crm_status_write_retries_without_provider_redelivery _ferramentas.test_thunder_os_hermes_linkedin_cadence`
Status: complete

### S4 - Bounded runtime configuration

**C9** - Disabled/synthetic/live config validation rejects relative paths, embedded secrets, identity mismatch and unbounded polling; checked-in example remains disabled (HLC-04, AC 10-11)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_runtime_config_is_explicit_bounded_and_secret_free _ferramentas.test_thunder_os_hermes_linkedin_runtime`
Status: complete

**C14** - Runtime refuses a revision-file mismatch and inserts exactly one bounded random 30-90 second delay between two due tasks (HLC-04, AC 11)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_runtime_binds_revision_and_spaces_multiple_tasks _ferramentas.test_thunder_os_hermes_linkedin_runtime`
Status: complete

**C15** - HubSpot status/cadence is read with the task, uncertain changes `cad_status` with readback, and confirmed completion writes a receipt-bound note (HLC-02, AC 4-5; HLC-03, AC 7-8)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v _ferramentas.test_thunder_os_hubspot_tasks`
Status: complete

**C10** - `--once` synthetic runtime completes one fixture task and prints a sanitized revision-bound summary without sockets or secrets (HLC-04, AC 12)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_synthetic_once_runs_end_to_end_without_network _ferramentas.test_thunder_os_hermes_linkedin_runtime`
Status: complete

**C11** - HubSpot adapter pins portal, owner, exact post-dispatch task contract and receipt before the sole completion patch (HLC-03, AC 7-9; HLC-04, AC 11)
Proof: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:/root/.hermes/profiles/ygor-personal/brain/_ferramentas:/root/.hermes/profiles/ygor-personal/brain python3 -m unittest -v -k test_exact_portal_owner_task_and_receipt_bound_completion _ferramentas.test_thunder_os_hubspot_tasks`
Status: complete

## Coverage

### S5 - Human-confirmed task delegation and scoped canary (2026-10-29)

**C18** - Missing/changed/revoked human campaign confirmation cannot reserve or enqueue; the exact confirmation permits the delegated job in simulation.
Proof: `PYTHONPATH=_ferramentas python3 -m unittest test_thunder_os_delegated_campaign_confirmation.DelegatedConfirmationTests`
Status: verification pending

**C19** - Task-to-cadence binding is explicit and contact-exact, with no Sequences enrollment prerequisite; ambiguous/missing bindings fail closed.
Proof: `PYTHONPATH=_ferramentas python3 -m unittest test_thunder_os_hubspot_tasks`
Status: verification pending

**C20** - URL/name/current-company comparison binds divergence confirmation to the exact preview/observation; absence, change or expiry blocks dispatch.
Proof: `PYTHONPATH=_ferramentas python3 -m unittest test_thunder_os_delegated_identity test_thunder_os_delegated_panel_runtime`
Status: verification pending

**C21** - Fresh per-contact suppression reads fail closed for each failed, partial or suppressed source; missing sources never become clean observations.
Proof: `PYTHONPATH=_ferramentas python3 -m unittest test_thunder_os_delegated_suppression test_thunder_os_delegated_suppression_hubspot`
Status: verification pending

**C22** - Real adapter contracts restrict reads to at most two canary contacts, verify HubSpot ID by exact email first, reject owner LinkedIn, and restrict SEQD send/readback to owner email/owned attempt IDs. This does not assert live provider delivery.
Proof: `PYTHONPATH=_ferramentas python3 -m unittest test_thunder_os_canary_readers`
Status: verification pending

**C23** - Optional readers and scoped SEQD transport compose in the actual panel with effects disabled and no startup provider/credential IO; digest/recipient mismatches do not bypass scope.
Proof: `PYTHONPATH=_ferramentas python3 -m unittest test_thunder_os_connectsafely_panel_wiring.PanelWiringTests.test_disabled_panel_composes_optional_canary_readers_without_effect_ports_or_io test_thunder_os_connectsafely_panel_wiring.PanelWiringTests.test_disabled_canary_readers_do_not_bypass_digest_or_recipient_scope test_thunder_os_connectsafely_panel_wiring.PanelWiringTests.test_real_panel_main_injects_scoped_seqd_transport_while_all_effects_are_off`
Status: verification pending

**C24** - A session requires the restricted Unix ingress and the configured nginx master/worker UID/executable; forged TCP/local Unix/independent nginx requests fail, while authenticated expected nginx supports nonce/replay protection.
Proof: `PYTHONPATH=_ferramentas python3 -m unittest test_thunder_os_confirmation_socket test_thunder_os_proxy_session`
Status: verification pending

**C25** - Desktop/mobile UI displays both identities, mismatch and exact text, explicitly confirms only the exact observation, and verifies readback; failed/stale/mismatched results never show success or send/enqueue.
Proof: `node _ferramentas/test_painel_delegated_identity_ui.cjs` with an explicitly available Playwright/Chrome runtime. Uses synthetic identities, not a real-contact approval.
Status: verification pending

**C26** - Final source boundary/integrity passes; the same immutable staging bundle runs panel and paused agent and rehearses rollback/return without external network or production data.
Proof: `python3 scripts/check_source_boundary.py`; `python3 scripts/check_runtime_bundle.py --expected-source-sha SOURCE_SHA CANDIDATE`; `/opt/thunder-os/staging/cadencia-alinhamento-20261029/staging-drill.py` in a private network namespace with explicit final candidate path/SHA.
Status: verification pending

Coverage additions: confirmation present/missing/changed/revoked C18; task binding exact/missing/ambiguous C19; identity same/divergent/changed/expired C20/C25; suppression clean/replied/opt-out/failure/partial/missing C21/C22; canary own-email/second-profile/own-profile-rejected/out-of-scope C22; wiring enabled/disabled/bad-digest C23; session expected-nginx/TCP/non-nginx/shadow-nginx/replay C24. Provider send and human authenticated production login require separate evidence and are not inferred from these tests.

| Set | Member -> proof | Unproven |
| --- | --- | --- |
| authority failures (7) | wrong actor C1 · changed C1 · incomplete C1 · expired C1 · pause C2 · scope C2 · cap C2 | - |
| task mismatch classes (6) | early C3 · completed C3 · malformed C3 · wrong contact C3 · replied C3 · wrong cadence C3 | - |
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
| uncertainty | replay/reconciliation test | a second run never sends; only exact delegated reconciliation may complete |
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
