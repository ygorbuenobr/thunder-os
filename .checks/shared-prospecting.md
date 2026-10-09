# Shared commercial preparation — homologation source candidate

Profile: light. Base: `5041176f369824883ca19b763dde42dc03f1493c`.

The authorized source scope is additive, optional commercial preparation in the existing Thunder OS and panel. One generation dispatch per unit of work, dedupe before inference, current history/identity/stop checks and explicit reconciliation after an unknown inference remain binding. Existing Social source is retained. Uncommitted DM changes are not part of this branch.

Tests use synthetic principals/providers and temporary data. No deployment, active database, real provider calls, credentials/grants, Brain content changes, channel registration, outreach, CRM writes or campaign queue actions are authorized by this source candidate. Loading receipts prove selected context delivery; real model editorial compliance and live authenticated consumers remain unverified. No mandatory second-model review or automatic rule promotion is introduced.

Sources: repository `AGENTS.md`; existing service, repository, workflow, worker, consumer, knowledge and panel contracts; the concrete C1–C16 acceptance criteria below. Publication to an isolated homologation branch is authorized separately from activation.

## Landing

Extend KnowledgeStore with bounded, selected commercial projection and immutable handoff. Bind an optional SharedPreparation policy to ThunderService. Thin local adapters reuse AgentGateway, WorkItem, Repository/state/events/outbox, AgentWorker and ContinuousConsumer; default composition remains disabled. No new DB, Brain, scheduler, provider or dependency.

| Persisted contract | Literal shape | Constraint / alternative rejected |
| --- | --- | --- |
| Context snapshot | schema_version=1, commercial package version, content hash, file hashes, supplied Brain commit, read time, history coverage, selected documents; all authority flags false | Existing Brain and catalogue retained; raw sessions/private profiles excluded |
| Shared operation | key SHA256(owner, canonical account, scope, capability, explicit revision); immutable origins keyed by authenticated principal/channel/request_id | Transport request_id alone cannot dedupe equivalent requests across channels |
| Shared state in existing repository | shared_operation, shared_origin, shared_link, shared_loading, shared_generation, shared_handoff, shared_learning/decision | Separate work ledger would split ownership and claim authority |
| Generation fence | reserved before dispatch; completed output cached; uncertain/reserved replay requires reconciliation; max_attempts=1 | Blind retries can incur duplicate inference cost |
| Brain handoff | existing outbox kind shared_brain_handoff, stable file `_entrada/<actor>/shared-<work_id>.json`, selected proposals/decisions use `shared-learning-<hash>.json` / `shared-decision-<hash>.json`; hash collision is a conflict, never overwritten | Do not promote whole model conversations or declare sync before readback |
| Local HTTP route contract | GET `/api/os/v1/shared/context?account=...`, POST `/api/os/v1/shared/preparations`, GET `/api/os/v1/shared/work/<work_id>` in existing Handler | Reuse the panel server, proxy identity guard and nonce; no new auth server or shared fallback identity |
| Opt-in composition | startup `shared_factory=None`; factory returns core and exact authenticated-actor→SharedPrincipal mapping with only read/prepare scopes | Default environment grants and inferred actor/profile mapping are forbidden; production identities remain a separate unavailable contract |

Nothing else in this change is hard to reverse. These are additive, opt-in local source contracts; no existing persisted record is migrated.

## Checks

### S1 — context and canonical ingress

**C1** — Cross-channel continuation in both directions preserves canonical work/result and technical origin.
Proof: `SharedPreparationTests.test_cross_channel_continuation_both_directions`

**C2** — Concurrent aliases and wording variants create one operation/outbox and one fake generation dispatch, including concurrent workers and replay.
Proof: `SharedPreparationTests.test_concurrent_equivalent_requests_generate_once`

**C3** — Exact request replay is idempotent; incompatible payload conflicts; explicit new revision creates a distinct historical operation.
Proof: `SharedPreparationTests.test_replay_conflict_and_explicit_revision`

**C4** — Four actors receive identical commercial hashes/version and loading receipts; eight commercial rules remain present; private profile/session data is excluded. This does not certify real model compliance.
Proof: `SharedPreparationTests.test_context_receipts_four_actors_eight_rules_no_private_data`
Proof: `SharedPreparationTests.test_context_bounds_manifest_and_selected_projection`
Proof: `SharedPreparationTests.test_text_child_receives_selected_packet`

**C5** — New work receives updated Brain content; prior work preserves historical reference; mutation during generation rejects its dependent artifact.
Proof: `SharedPreparationTests.test_context_changes_between_and_during_work`
Proof: `SharedPreparationTests.test_continuous_context_drift_is_visible_and_finalized`

**C6** — Missing/stale history, other owner, ambiguous identity and invalid address block preparation before generation/copy/spend.
Proof: `SharedPreparationTests.test_owner_identity_history_fail_closed`

**C11** — Missing/unauthorized principal and forged actor/receipt fields fail closed with 401/403/422 at the local adapter; unknown work is 404 and replay mismatch 409. No principal provisioning.
Proof: `SharedPreparationTests.test_adapter_authorization_and_forged_fields`

### S2 — one dispatch, consumer, result and handoff

**C7** — Both channels read the same OS result; Brain export is idempotent after interruption, conflicts stay pending and are visible.
Proof: `SharedPreparationTests.test_result_and_idempotent_handoff_with_conflict`
Proof: `SharedPreparationTests.test_handoff_rejects_actor_and_entry_directory_symlinks`

**C8** — Learning proposals and attributed approval/rejection with supersession never modify canonical rules automatically.
Proof: `SharedPreparationTests.test_learning_is_proposal_not_rule`

**C9** — Expired leases/restart cannot infer again; cached output is reused; unknown/timeout remains reconciliation; unsafe retry configuration is refused before inference.
Proof: `SharedPreparationTests.test_cached_output_after_lease_loss_no_second_inference`
Proof: `SharedPreparationTests.test_interruption_timeout_and_unsafe_runner_no_retry`

**C10** — Reply/refusal/opt-out/invalid/receipt stops are rechecked between channels and around execution; zero sends/spend/CRM writes; draft is not a receipt or deal.
Proof: `SharedPreparationTests.test_stops_before_and_during_generation_zero_effects`
Proof: `SharedPreparationTests.test_existing_os_reply_and_receipt_also_stop`
Proof: `SharedPreparationTests.test_guarded_runner_rechecks_stops_owner_history_context_after_collection`

**C12** — Paused preparator remains blocked and consultable, no adapter activates it. The continuous consumer accepts only persisted shared links after its existing gates/cutoff.
Proof: `SharedPreparationTests.test_paused_and_continuous_consumer_eligibility`

### S3 — existing panel HTTP boundary

**C13** — The three real panel HTTP routes deliver restricted context, govern preparation and read the same canonical result; HTTP replay never generates a second job/call.
Proof: `SharedHTTPTests.test_http_context_preparation_status_and_replay`

**C14** — Existing proxy/nonce guards reject missing/unknown identity, spoofed actor, wrong origin and replayed nonce; a recognized actor without a shared binding is denied. Read-only scope cannot prepare, and unknown capability/model/receipt fields are rejected before creating work.
Proof: `SharedHTTPTests.test_http_existing_auth_nonce_and_scopes_fail_closed`
Proof: `SharedHTTPTests.test_http_unknown_capabilities_and_forged_fields`

**C15** — Production startup and local server use the same opt-in composition helper; no factory means shared routes return 503. Invalid actor/scope/core mappings fail closed without creating a principal/grant.
Proof: `SharedHTTPTests.test_http_disabled_without_composition`
Proof: `SharedHTTPTests.test_composition_rejects_unknown_actor_scope_and_wrong_core`
Proof: `SharedHTTPTests.test_existing_main_uses_same_explicit_composition`

**C16** — HTTP parsing rejects extra/repeated account parameters and unsupported methods/routes; paused requests remain consultable and do not execute a model or effects.
Proof: `SharedHTTPTests.test_http_schema_methods_and_paused_work`

## Swept

- validation: C3, C4, C6, C11, C16; closed envelopes, bounded source projection, manifest hashes and HTTP parsing.
- failure modes: C5, C7, C9; context drift, filesystem failure, uncertain inference.
- idempotency: C2, C3, C7, C9; semantic operation, origin, generation and handoff.
- authorization: C11, C8, C14, C15; trusted injected principal, separate read/prepare/decision permissions, existing HTTP proxy/nonce guards; no grant creation.
- concurrency: C2, C9; existing SQLite BEGIN IMMEDIATE, leases and version fences.
- data lifecycle: C3, C4, C8; historical references retained; selected learning only, no automatic rule promotion.
- dependency failure: C6, C7, C9; missing history or export stays explicit and actionable.
- state transitions: existing `models.ALLOWED_WORK_TRANSITIONS`; C5, C9, C12 cover shared needs_human and paused behavior.
- observability: C1, C4, C7, C9; origin/loading/context hashes, generation status, handoff status and block reason are queryable.

## Coverage

| Set (size) | Members -> proof | Unproven |
| --- | --- | --- |
| Technical consumers (4) | dotin, hermes, claude, codex -> C4 | real authenticated runtime consumption for all four |
| Entry directions (2) | dotin→Telegram, Telegram→dotin -> C1 | real channel mounting |
| Local adapter methods (3) | get_context, request_preparation, get_work -> C11/C1; panel routes -> C13 | production composition and real tools registration |
| Adapter responses (7) | 200, 201, 401, 403, 404, 409, 422 -> C11; loopback HTTP -> C13–C16 | real reverse proxy/authentication gateway |
| Commercial rules (8) | owner, history, vacancy≠intent, recurring partnership, dossier provenance/counter-signal, short human copy, attributed third-party case, exact approval/stops/real receipts -> C4 | editorial compliance by real model |
| Stops (6) | reply, refusal, opt_out, invalid_address, receipt_exists, uncertain_receipt -> C10 | live provider/CRM ingestion |
| History sources (3) | HubSpot, Gmail, LinkedIn complete/fresh metadata -> C6 | live fetch completeness |
| Generation states (3) | reserved, completed, uncertain -> C9 | provider-side reconciliation after unknown inference |
| Handoff outcomes (3) | pending, synced, conflict -> C7 | actual Brain Git transport |
| Core startup (1 opt-in assembly) | synthetic local service/adapter/continuous consumer -> C12 | live consumer composition and activation |
| Panel HTTP boundary (3 routes) | context, preparation, work -> C13 | live reverse proxy, dotin and Telegram consumption |
| Panel authorization (7 refusals) | missing identity, unknown identity, unmapped actor, wrong origin, nonce replay, actor spoof, read-only prepare -> C14 | real principal/grant authenticity |
| Panel composition (2 assemblies) | common helper in main and loopback server -> C15 | production composition remains default None |

Claims naming status/response shape: C11 crosses the callable adapter boundary; C13–C16 cross the actual panel HTTP boundary over ephemeral loopback sockets. Provider sockets/process execution remain denied. Existing baseline: 93 tests passed before implementation.

## Handoff and verification boundary

The complete C1–C16 feature and named proof assertions are retained from the previously reviewed local candidate. The functional source and tests must remain byte-identical to that candidate. Publication packaging replaces local provenance reports with this source-only checklist; it does not change a proof, test assertion or feature contract.

Run all 26 named Shared/HTTP proofs with the 93 baseline tests (119), the 57 committed ConnectSafely panel/client/transport tests, and the 12 original Social PostgreSQL tests against one new disposable PG16 cluster: 188 individually named tests, no skips. DM snapshot tests are excluded because their uncommitted implementation is excluded from this branch. The separate combined local candidate passed 219 tests and is not evidence that the publication branch ran 219.

After the final source commit, a fresh independent verifier checks the whole base..HEAD diff, exact code/test carry, current staged source boundary, all C1–C16 named results, the finite 188-test invocation and absence of private operational material. Detailed local execution logs, personal paths and authorization records remain outside Git.

Real proxy/principal authority, trusted live history readers, MCP/Telegram registration, Brain Git transport, worker composition and production release/activation remain separate homologation gates. A local PASS is not a deployed journey or an external receipt.
