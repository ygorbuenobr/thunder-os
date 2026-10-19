# Campaign execution confirmation — local candidate

Request: explicit human confirmation of platform + campaign + version/rules before campaign start, resume or execution. Clear conversational context only permits research/drafting. One exact confirmation covers the bounded batch; no per-call confirmation inside that unchanged batch.

## Contract and boundary

The existing approval JSON gains `execution_confirmation`. No table/schema migration, new permission, credential, custom rule or separate authorization store. The existing cadence may carry `execution_context` with platform (`thunder_os`, `chattie`, `instantly`), campaign ID/version, rules version/SHA-256, configuration SHA-256, run ID and channel-to-provider IDs. This is a snapshot, not authority by itself.

`ThunderService` takes a **trusted** `campaign_context_loader(campaign_id)` from composition. It must return the current, verified, immutable campaign/rules/configuration snapshot; it must not read a request body, agent output or provider text as authority. No production loader is installed in this candidate. Missing loader/context fails closed. Worker provider IDs must come from trusted adapter composition, not the effect payload. OS execution requires platform `thunder_os`; this does not register or run Chattie/Instantly campaigns.

The existing human approval route additionally requires the exact `execution_confirmation` returned in the review. It binds context, batch ID/version, IDs+hashes of every approved future step and global-control version. The server persists the authenticated actor and expiry. The panel asks a specific campaign/platform/version question before POST; missing canonical context hides execution approval and asks for resolution. Generic `confirm=true`, context alone and another platform's confirmation are insufficient.

Before queueing, and again after reservation and all preflight I/O, the worker reloads current approval/cadence/control and canonical context, validates scope/expiry/provider and the queue's confirmation hash. Parallel duplicate effects still use the existing unique outbox key, claim/lease and ledger reservation. Pause/resume changes control version, requiring fresh approval. Old queued effects cannot inherit a fresh confirmation. Restart/retry without the new bounded-batch integration remains blocked; no release of an uncertain reservation. Post-reservation denial retains the touch and marks the job `needs_reconciliation`.

## Deliberate fail-closed compatibility

Legacy drafts remain readable/preparable. Existing payload-only approvals are not execution confirmation. Direct EffectCoordinator/fallback, orphan likes, manual retry, meeting CRM mirror and ConnectSafely/social, SEQD and HubSpot console execute routes lack this canonical integration, so this candidate explicitly blocks them before effects. Their preparation/read/reconciliation behavior is retained. This is intentionally not a drop-in production release; these routes need bounded confirmation integration before use. Production is unchanged.

This code cannot prevent manual actions inside external Chattie/Instantly panels, direct SDK/provider calls, external agents, independent cron jobs or another unmodified OS release. It is not a global kill switch. No campaign has been activated/deactivated or registered.

## Acceptance evidence

Offline tests use TemporaryDirectory SQLite, synthetic `.invalid` identities, fake provider/ledger and no network. The external `toques.py` dependency is represented only by an import-time `ErroBanco` stub; no production ledger is loaded. These tests do not prove the real ledger, browser session, provider or runtime composition.

Covered: clear context with no confirmation; generic approval; different platform/campaign/batch; obsolete version/rules/configuration; expiry; wrong runtime provider; parallel unconfirmed jobs; duplicate parallel confirmed jobs; correct exact scope; pause/resume; old queue with new confirmation; recipient expansion; context/loader missing; malformed context; rules changing during reservation and final preflight; payload-only coordinator and all legacy consoles denied; explicit human review question. Payload approval unit tests remain passing.

Run `python3 scripts/test_campaign_confirmation_offline.py` , `node scripts/test_campaign_confirmation_panel.cjs` and `python3 scripts/check_source_boundary.py`. The panel source-function harness checks cancel/no POST, explicit payload, hidden legacy button and fresh confirmation for an approved batch; it is not a browser test. The runner executes the focused acceptance suite only. Existing console integration tests expecting the previous execution API need migration with the missing canonical integration; no full legacy suite pass is claimed. No tests were removed or weakened.

Independent review found malformed-type handling, stale-rule preflight and post-reservation state issues; all received fixes/regression cases. A real atomic fence spanning canonical configuration revision and external dispatch is **not** established by a callback read alone: production loader/adapter integration must establish that protocol or keep execution disabled. UI browser/HTTP end-to-end remains unverified; the panel change is source only.

## Deployment decisions and rollback

Before deployment: choose/provision the canonical campaign/rules registry and immutable versioning; compose verified loader + actual adapter IDs; integrate remaining bounded-batch routes; specify safe restart/new-batch recovery for existing jobs; validate atomic configuration fencing, provider callback/lease boundaries and actual UI; migrate affected integration fixtures; reconcile source with concurrent main/remote changes; run full staging gates. No schema migration is required by this contract; broader registry/migration is a separate decision.

Branch `codex/campaign-execution-confirmation` starts from local c570914, isolated because canonical main had other uncommitted work and diverged 6/4 from fetched origin/main 1ee6ead. No merge/reset of that checkout. No push, deployment, service restart, job activation or production mutation in this task. Rollback: omit this candidate or revert its task commit on the branch; no data migration to undo. Do not roll back production to bypass a confirmation requirement.
