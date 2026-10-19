# Campaign execution confirmation — local candidate

Request: explicit human confirmation of platform + campaign + version/rules before campaign start, resume or execution. Clear conversational context only permits research/drafting. One exact confirmation covers the bounded batch; no per-call confirmation inside that unchanged batch.

## Contract and boundary

The existing approval JSON gains `execution_confirmation`. No table/schema migration, new permission, credential, custom rule or separate authorization store. The existing cadence may carry `execution_context` with platform (`thunder_os`, `chattie`, `instantly`), campaign ID/version, rules version/SHA-256, configuration SHA-256, run ID and channel-to-provider IDs. This is a snapshot, not authority by itself.

`ThunderService` now uses `CampaignRegistry` in the existing `thunder_state` store. Authenticated Ygor-only configure/revoke service methods and HTTP mutation routes use expected-version CAS. Records validate rules text, platform, run, provider/account mapping and hashes; the configuration digest includes the registry revision, preventing ABA re-registration from reviving an old approval. Missing, malformed or revoked records fail closed. Arbitrary callback loaders are rejected. Adapter identity attributes must match both configured provider and account. OS execution requires platform `thunder_os`; this does not register or run Chattie/Instantly campaigns. All records used here are synthetic temporary test data.

The existing human approval route additionally requires the exact `execution_confirmation` returned in the review. It binds context, batch ID/version, IDs+hashes of every approved future step and global-control version. The server persists the authenticated actor and expiry. The panel asks a specific campaign/platform/version question before POST; missing canonical context hides execution approval and asks for resolution. Generic `confirm=true`, context alone and another platform's confirmation are insufficient.

Before queueing, and again after reservation and all preflight I/O, the worker reloads current approval/cadence/control and canonical context, validates scope/expiry/provider and the queue's confirmation hash. Parallel duplicate effects still use the existing unique outbox key, claim/lease and ledger reservation. Pause/resume changes control version, requiring fresh approval. Old queued effects cannot inherit a fresh confirmation. Restart/retry without the new bounded-batch integration remains blocked; no release of an uncertain reservation. Post-reservation denial retains the touch and marks the job `needs_reconciliation`.

## Deliberate fail-closed compatibility

Legacy drafts remain readable/preparable. Existing payload-only approvals are not execution confirmation. Direct EffectCoordinator/fallback, orphan likes, manual retry, meeting CRM mirror and ConnectSafely/social, SEQD and HubSpot console execute routes lack this canonical integration, so this candidate explicitly blocks them before effects. Their preparation/read/reconciliation behavior is retained. This is intentionally not a drop-in production release; these routes need bounded confirmation integration before use. Production is unchanged.

This code cannot prevent manual actions inside external Chattie/Instantly panels, direct SDK/provider calls, external agents, independent cron jobs or another unmodified OS release. It is not a global kill switch. No campaign has been activated/deactivated or registered.

## Acceptance evidence

Offline tests use TemporaryDirectory SQLite, synthetic `.invalid` identities, fake provider/ledger and no network. The external `toques.py` dependency is represented only by an import-time `ErroBanco` stub; no production ledger is loaded. These tests do not prove the real ledger, browser session, provider or runtime composition.

Covered: clear context with no confirmation; generic approval; different platform/campaign/batch; obsolete version/rules/configuration; expiry; wrong runtime provider; parallel unconfirmed jobs; duplicate parallel confirmed jobs; correct exact scope; pause/resume; old queue with new confirmation; recipient expansion; context/loader missing; malformed context; rules changing during reservation and final preflight; payload-only coordinator and all legacy consoles denied; explicit human review question. Payload approval unit tests remain passing.

Run `python3 scripts/test_campaign_confirmation_offline.py` , `node scripts/test_campaign_confirmation_panel.cjs` and `python3 scripts/check_source_boundary.py`. The panel source-function harness checks cancel/no POST, explicit payload, hidden legacy button and fresh confirmation for an approved batch; it is not a browser test. The runner executes the focused acceptance suite only. Existing console integration tests expecting the previous execution API need migration with the missing canonical integration; no full legacy suite pass is claimed. No tests were removed or weakened.

The final worker gate, synchronous adapter call, ledger completion and outbox completion now run inside a `BEGIN IMMEDIATE` dispatch fence in the same repository as the canonical registry. The fence requires a current nonempty claim owner and valid lease. Concurrent configuration/revocation writers wait; same-thread registry mutation is rejected. A writer committed before the fence invalidates dispatch. A writer arriving after dispatch acquired the fence cannot undo the in-flight call. Lease expiry while holding this fence cannot permit another claimant; uncertain provider outcomes are committed without automatic retry. This protects cooperating paths using this SQLite database, not external configuration stores or processes bypassing the registry.

Tradeoff: the synchronous provider call holds the SQLite write lock; slow providers block other writers and can trigger their busy timeout. Real ledger/provider composition, failure timing and latency must be qualified before deployment. This candidate keeps real adapters disabled. No distributed transaction or rollback of an external send is claimed.

Independent review found and verified fixes for malformed types, stale preflight, post-reservation state and missing claim ownership. Final focused evidence: 51 Python tests (including existing approval/DB integration tests), panel source harness and source boundary. New cases cover registry CAS/revocation/ABA, expiry/revocation between queue and worker, cross-connection configuration/revocation serialization, lease expiry, wrong claimant, adapter account/provider mismatch, uncertain send and meeting mirror denial. Browser/HTTP end-to-end remains unverified.

## Deployment decisions and rollback

Before deployment: reconcile this isolated branch with concurrent main/remote changes; provision reviewed campaign records through the authenticated registry; qualify actual adapter/ledger identity and lock timeouts; integrate any currently blocked routes before enabling them; specify safe new-batch recovery for existing jobs; migrate affected legacy execution fixtures and run full staging/browser gates. The local loader and transactional fence are implemented, not pending architecture. No schema migration is needed.

Branch `codex/campaign-execution-confirmation` starts from local c570914, isolated because canonical main had other uncommitted work and diverged 6/4 from fetched origin/main 1ee6ead. No merge/reset of that checkout. No push, deployment, service restart, job activation or production mutation in this task. Rollback: omit this candidate or revert its task commit on the branch; no data migration to undo. Do not roll back production to bypass a confirmation requirement.

## Route coverage

| Route | Local behavior | Evidence |
|---|---|---|
| Campaign configure/revoke | Human actor + CAS; canonical loader | Registry unit tests, service tests; HTTP wiring inspected |
| Cadence approve/queue/worker | Exact confirmation + final transactional fence | Offline mock end-to-end and concurrency tests |
| Panel approval | Specific context question; cancel prevents POST | Source-function DOM harness, not browser E2E |
| EffectCoordinator/fallback | Blocked before external send | Negative test |
| ConnectSafely/social, SEQD, HubSpot consoles | Execute blocked | Four negative console cases |
| Orphan like / manual retry / meeting mirror | Blocked before external effect | Negative route tests; meeting may retain internal pending state |
| External panels, direct SDK calls, other releases/jobs | Outside this candidate | No coverage claim |
