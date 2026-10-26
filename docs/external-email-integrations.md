# External email integrations: A/B before T4

## Periodic Instantly API Reader (2026-10-22)

This later slice authorizes source reads and the internal observation cache only,
not the EMAIL importer, pause control, sending, or campaign configuration.
`InstantlyReader` runs one daemon worker in the canonical panel process, every
60 seconds (completion-to-next-attempt). An in-process lock and persisted attempt
time prevent concurrent refresh/restart bursts. No new service, timer, database,
queue, contact assignment, or approval mechanism is introduced.

Explicit opt-in: `/etc/thunder-os/instantly_reader_enabled` must be a regular,
private root/current-user file containing `read-only-v1`. Credential is read from
`/etc/thunder-os/instantly_api_key` (regular private file, no symlinks/FIFOs,
max4096 bytes). These are separate from the capture-only webhook secret. Removing
or renaming the opt-in stops subsequent collections and hides cached source data
without a service restart; the worker checks it each round. Never print secrets.
No provider write permissions are inferred from successful reads.

Transport supports GET current workspace and fixed-campaign GET email list only:
workspace `2ead5b1b-02e1-45d7-b2f7-76e686418250`, campaign
`ed7ebc41-c816-40d8-832c-f7200a911c0c` / `BR-INSTANTLY-01`.
List parameters: mode=emode_all, preview_only=false, sort_order=desc,50 per page,
at most10 pages (500 records) per round, below the endpoint20/minute limit.
Every round enumerates again, including old messages whose classification changed.
Cursor loops, malformed envelopes, rate limits, HTTP errors and timeouts fail the
round without inline retries; next round waits60 seconds. UUID deduplication,
workspace/campaign/direction binding and invalid-record counts protect the result.
Exhaustion of the cap or rejected records labels coverage partial, not a global
total. Completeness describes only this campaign's Unibox email-list surface,
not every enrolled lead, mailbox, or delivery outcome.

`ue_type=1/3` counts stored outbound messages, not deliveries;4 is scheduled and
excluded. Incoming2 with explicit integer is_auto_reply=0 is a provider non-auto
candidate, not confirmed interest. Integer1 is automatic. Null/unknown stays
unclassified, except a narrowly recognized automatic-response subject prefix,
displayed with classification_source=subject. This conservative subject rule can
only downgrade to automatic, never upgrade a message to human interest. Counts
measure messages, while leads_with_sent separately counts distinct lead emails.
Bodies become plain text; scripts/styles are excluded; excerpts are explicit.

The existing Repository stores one replaceable `thunder_state` snapshot under
kind=instantly_reader_snapshot, id=BR-INSTANTLY-01. This contains private message
text/addresses, not credentials/HTML/attachments, and remains behind panel login.
Successful source time is preserved on failure; unknown is not zero. Last success
older than180 seconds becomes stale. GET
`/api/os/v1/integrations/instantly/reader` is authenticated as Ygor and cache-only;
query parameters and writes are rejected. Browser refresh does not call providers.
Cadências shows these observations separately from HubSpot totals and does not
infer a CRM contact by name. No webhook registration is required for this reader.

Proofs: test_thunder_os_instantly_reader (provider, observation, cache and HTTP),
test_painel_cadencias_ui.cjs (responsive, failure and unsafe-content fixtures),
plus the dated real source/cache/public-panel canary in the execution report.
Offline tests do not prove a real human reply when the source has none classified.
Rollback is the prior panel release plus rename of the explicit reader opt-in;
retain the cache/credentials for recovery. No entire-DB restore is necessary.

Official [email-list contract](https://developer.instantly.ai/api-reference/email/list-email)
documents pagination, email types, body excerpts, classifications and rate limits.

## Capture-Only Registration (2026-10-21)

The user approved receiving Instantly events before the one-lead CRM canary.
The exact public route is
`https://painel.r3xdigital.tech/api/os/v1/integrations/instantly/events`.
Authentication is the custom header `X-Thunder-Instantly-Secret`; the value is
kept in `/etc/thunder-os/instantly_webhook_secret` (root, mode0600), never Git,
URL parameters, access logs or chat. This is not an Instantly API key.
The root-only0600 `/etc/thunder-os/instantly_webhook_auth.conf` include checks the
same header in Nginx's rewrite phase, before request body handling. Request
buffering is OFF for the exact route. The backend independently checks the header
again, including duplicates. Never print `nginx -T` or the private include.

`InstantlyCapture` has no provider/CRM port. The server authenticates before
parsing the body. It accepts only the pinned workspace/campaign below and the
three event types `email_sent`, `reply_received`, `auto_reply_received`. It
persists only allowlisted IDs, sender/lead addresses and event timestamp in the
existing Repository `thunder_state` kind `instantly_webhook_inbox`. Text, HTML,
subject and arbitrary webhook fields are not retained or trusted. A duplicate
identity returns202 without another insert; conflicting metadata returns409.
`provider_verified=false`, `email_logged=false`, `writer_enabled=false` and
`external_platforms_stopped=false` remain explicit. Received does not mean logged.

The fixed private secret file is the explicit capture-only opt-in. Without it,
startup leaves capture disabled; unsafe/missing secret at request time fails
closed. Removing the opt-in from runtime configuration and restarting ONLY the
panel disables ingress; archive/rename the known secret rather than deleting it
when performing an authorized rollback. No gateway, bot or campaign restart.

Operator helper `scripts/provision_instantly_ingress.py` is VPS/root/hostname
pinned, uses the existing panel promotion lock, checks the exact Nginx digest,
preserves one original configuration, and never rotates an existing secret.
Run `prepare --expected-config-sha256 HASH` before the panel promotion. Run
`expose --expected-config-sha256 HASH --expected-source-sha SHA` only after the
reviewed panel release is active: it checks current/health SHA and an authenticated
invalid-payload422 locally, then adds an exact Nginx location, `nginx -t` and
reload (not restart). All other panel paths retain their current login. It strips
the human actor header; the webhook never gains a human session or nonce.

Rollback: helper `rollback --expected-config-sha256 CURRENT_HASH` restores the
original Nginx file and reloads; separately restore the panel release symlink
using the existing release rollback procedure. Nginx backup:
`/opt/thunder-os/staging/panel-cadencias/instantly-ingress-config/nginx-before.conf`.
Do not overwrite unrelated Nginx edits if its digest changed. This helper creates
no webhook subscription, EMAIL activity, queue, new database or scheduled worker.
Rollback does not require the secret or private auth include: revoking credentials
must never prevent removing exposure. Both private files stay outside the release
and Git, and are retained for authorized recovery rather than automatically deleted.

After the user manually registers the URL/header for BR-INSTANTLY-01, verify one
real event and its provider email UUID, then perform only the authorized one-lead
CRM canary with canonical API readback and EMAIL permission. Capture tests using
synthetic `.invalid` identities are not that canary and are not commercial facts.
No external pause, send or cohort import follows from successful event delivery.

Local implementation on `codex/cadencias-readonly`, base
`8f1255184a0a5a00f867f54d45812e5818dcc349`. No activation, deployment,
provider write, enrichment or real CRM write belongs to this slice.

## Frozen wiring contract

- `apollo_readonly.contract_status()` and `instantly_webhook.contract_status()`:
  pure JSON-safe status, no credential discovery, network or Repository creation.
- Both modules expose `dry_run(payload=None)`: optional caller-private input,
  deterministic synthetic default fixtures, no network, credentials or database.
  Instantly uses a fictional Pavlack record at `.invalid`. Result is
  `structural_dry_run`, `simulation=true`, `structurally_valid`,
  `needs_provider_readback=true`, `provider_verified=false`, `email_logged=false`,
  `writes=0`, `db_writes=0`, `persisted=false`; source content is never echoed.
- `ApolloReadOnly(credential_loader=None, identity_reader=None, request=None,
  enabled=False, timeout=15).job(contacts, *, hubspot=None)`: bounded read job;
  `identity_reader()` must independently return the credential-bound `team_id`.
  `hubspot` is the canonical read port, optional for source-only preparation.
- `InstantlyWebhook(secret_loader=None, provider=None, enabled=False,
  header_name='X-Thunder-Instantly-Secret').handle(headers, payload)`:
  default `http_status=410` BEFORE secret/provider/database access. Enabled mode
  authenticates the shared custom header, validates pinned payload identity, then
  calls `provider.read_event(payload)` for canonical identity/content. Returns
  preparation, never a send request or automatic T4 pause.
- `InstantlyReadOnly(credential_loader=None, request=None, timeout=15)` implements
  that readback port over fixed `api.instantly.ai`, API v2.
- `HubSpotExternalEmail(credential_loader=None, request=None, enabled=False,
  authorize=None, timeout=15)` supplies canonical contact search, EMAIL reads and
  narrowly allowed create requests. No PATCH of existing contacts.
- `ExternalEmailImport(repository, transport, *, enabled=False, authorize=None)`:
  `execute(plan)` and `reconcile(plan, *, recovered_id=None)`. Explicit trusted
  runtime capability and both writer switches are required. Use the existing
  authorization semantics; no extra human approval type is introduced.
- Pure `plan_contact(contact, snapshot)` and `plan_email(event, contact_id)` return
  hash-bound plans. Errors are `ExternalEmailError.code`, never provider bodies.

HTTP server, simulation authentication, UI, README, boundary manifest and runtime
loaders belong to the principal. No module reads guessed Apollo/Instantly env vars
or searches `/etc/thunder-os`. Missing injected configuration is
`configuration_pending`. Do not instantiate a Repository in disabled handlers.

## Acceptance And Ownership

Only the three new modules, their three `_ferramentas/test_thunder_os_*.py` files
and this document belong to this specialist. TLC Implement light builder phase;
the principal owns the independent verifier over the frozen diff.

- C1 default OFF before credentials/network/DB: `test_defaults_off`,
  `test_disabled_before_secret_or_provider`.
- C2 primary + additional emails, complete canonical search, no overwrite:
  `test_alias_dedupe_no_overwrite`, `test_incomplete_and_ambiguous_block`,
  `test_canonical_search_paginates_and_filters_aliases`.
- C3 stable provider ID, no invented RFC Message-ID, EMAIL association readback:
  `test_provider_id_is_not_rfc_message_id`, `test_email_readback_requires_association`.
- C4 committed attempt before write; timeout/restart/concurrency cannot replay:
  `test_attempt_persisted_timeout_never_retries`, `test_concurrent_duplicate`.
  Calls inside an already-open Repository transaction are rejected before any
  transport or attempt mutation; the same guard covers prepared membership writes.
- C5 Apollo endpoints/filter/pages/batches/missing IDs: `test_job_wire_contract`,
  `test_partial_content_is_explicit`, `test_bad_identity_and_pagination_block`.
- C6 webhook header + pinned identity + canonical provider readback:
  `test_authentication_and_pinned_identity`, `test_real_readback_wire_contract`,
  `test_payload_cannot_override_provider`.
- C7 auto reply != human, stable event keys and out-of-order preparation:
  `test_auto_reply_is_not_human`, `test_duplicate_and_out_of_order`.
  Automatic or unverified replies are retained, not imported as INCOMING_EMAIL:
  that CRM direction alone currently counts as response evidence in the reader.
  Only provider-verified `human_reply is True` may produce an inbound import plan,
  and sealed-plan validation enforces the same rule before transport access.
- C8 no real source data in tracked fixtures: synthetic `.invalid` examples only;
  final diff inspection and source boundary check (principal owns allowlist).

Validation/failure/dependency errors are explicit and redacted. Existing
`Repository` transactions and `hubspot_attempt`/`hubspot_identity` state are reused;
no database/schema/ledger/fence/approval type added. Plan content is runtime data,
kept out of Git. Event order never reopens contact membership or changes campaign
state. Default status/simulation do not persist data. External writers and search
index delay remain outside the single-attempt guarantee; no exactly-once claim.

## Provider Contract And Activation

Apollo: fixed team `6a18b5e2bba6e300203fd691`, sequence
`6ad7c2af749725001465b81d`, campaign `BR-APOLLO-01`, API v1. Official
[search](https://docs.apollo.io/reference/search-for-outreach-emails) is GET
`/api/v1/emailer_messages/search`, scoped by `emailer_campaign_ids[]`, `page`,
`per_page` (100 maximum, 500 pages). Official
[content](https://docs.apollo.io/reference/get-email-content) is POST
`/api/v1/emailer_messages/get_content`, `ids` at most 10, `body_format=plain`.
Both are documented as zero-credit reads. Scopes: `emailer_messages_search` and
`emailer_messages_get_content` (or endpoint-scoped API key). Missing content IDs
mean no sent content available, never permission to invent content or timestamp.
Rate limits depend on the account plan; abort 429/5xx/timeout, no inline retry.
An explicit later read job may be rerun. No enrichment endpoint is present.

Instantly: fixed workspace `2ead5b1b-02e1-45d7-b2f7-76e686418250`, campaign
`ed7ebc41-c816-40d8-832c-f7200a911c0c`, local campaign `BR-INSTANTLY-01`.
The official [conventions](https://github.com/Instantly-ai/instantly-starter-kit/blob/main/docs/conventions.md)
specify custom webhook headers and no HMAC. Follow the official
[payload schema](https://developer.instantly.ai/guides/webhook-events.md).
Readback uses API v2 [current workspace](https://developer.instantly.ai/api-reference/workspace/get-workspace.md)
and [email by ID](https://developer.instantly.ai/api-reference/email/get-email.md),
scopes `workspaces:read`, `emails:read`. Pin `organization_id`, `campaign_id`, lead,
sender account, type and provider email UUID. Only canonical `ue_type=2` with
`is_auto_reply=0` proves a human-reply candidate. Unknown classification stays
unknown. Global documented limits: 100 requests/second and 6000/minute;
`GET /emails` list has a separate 20/minute limit. This slice uses no list/send API.
No subscription, secret or key is created. Duplicate events have the same stable
effect key; arrival time is not message identity. A receipt/HTTP 200 means prepared
only; it does not prove CRM persistence, human response handling, or external pause.

HubSpot: pinned portal `51412240`, CRM v3 contacts and logged EMAIL activities.
[Email activities API](https://developers.hubspot.com/docs/api-reference/legacy/crm/activities/emails/guide)
creates logs via `/crm/v3/objects/emails`; it does not request email sending.
Readback checks EMAIL properties AND contact association. Provider internal IDs
are correlation references; never write them into `hs_email_message_id` or RFC
headers. An immutable attempt binds source account/message/type to the exact plan
and returned object ID. Message identity excludes mutable reply classification;
changed classification/content requires reconciliation, never a second log.
Ambiguous writes stay pending with `retry_write=false`;
recover a canonical object ID through read-only investigation before reconciliation.
No timeout retry is enabled, even if search returns zero.

### Real Evidence Versus Local Evidence

Private input files were inspected for schema only, outside Git. The principal's
canonical REST contact read (198 contacts, 237 identifiers, six primary chunks of
40 plus all additional-email records, 9 total calls including identity/schema)
reported **189 would_create / 2 existing / 7 no_email / 0 ambiguous**, complete
additional-email coverage of 42 records. This is contact identity evidence, not
email deliverability, campaign membership, EMAIL dedupe or send readiness.
Source: private `canonical-contact-dryrun-result.json` in
`/Users/ygorbueno/thunder/artifacts/thunder-os-execucao-ate-tarefa7/`.

The principal's subsequent private Apollo read retrieved 49 metadata records and
48 content records in five batches. One omitted ID remains held (metadata reported
failed/bounced); absence alone does not prove why content was omitted. There are
48 content-backed EMAIL candidates, not 49 deliveries. The source content exposes
recipient `to`, body and `sent_at`, without an actual sender header. The plan logs
those facts and deliberately omits `hs_email_headers`/from rather than deriving a
real sender from enrollment. Source reference is an internal ID, never RFC Message-ID.

EMAIL dedupe for those candidates remains unknown. The port provides a bounded
canonical EMAIL list (up to 50 pages of 100) for preflight: exact source marker
matches are read back without a second log; possible legacy logs at the same
time/subject/contact without that marker require reconciliation. Partial listing
cannot prove absence. No real EMAIL dedupe was performed by this specialist.

The real
Bianca canary is RETAINED: connector `REQUIRES_PERMISSION_MODIFICATION` still
blocks EMAIL writes. Do not rotate tokens, change channels or bypass permissions.
Only the principal may revisit that already authorized canary after permission
resolution. The rest of the 198-contact cohort remains dry-run-only. This specialist performs
no provider or CRM calls with real credentials.

Before activation the principal must supply reviewed runtime loaders, prove the
credential-bound provider account, verify EMAIL permission and existing activity
dedupe, wire authenticated simulation and the 410 OFF endpoint, then obtain the
independent review. Neither toggles nor passing mocks establish provider readiness.

## Builder Validation And Freeze

Mac/Darwin, user `ygorbueno`, assigned worktree and base SHA confirmed. Stack is
Python standard library, `unittest`, existing SQLite `Repository` and HTTPS ports.
37 focused tests and 102 membership/cadence regressions passed (139 total):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest \
  test_thunder_os_external_email_import test_thunder_os_apollo_readonly \
  test_thunder_os_instantly_webhook test_thunder_os_campaign_membership \
  test_thunder_os_cadence_contact_campaign test_thunder_os_cadence_evidence \
  test_thunder_os_cadence_readonly
```

Test state is isolated under `.scratch/external-email/`; synthetic temporary
databases are removed by `TemporaryDirectory`. Validation output, frozen diff and
SHA-256 list stay there for the principal's reviewer. Source boundary passed with
the principal's allowlist changes. No commit, push, deploy, CRM/provider write,
webhook subscription, secret provisioning, canary execution or outbound send.
Review and live activation remain the principal's responsibility; these are
builder proofs, not independent TLC verification or runtime readiness.

## Email Receipt Readback: Prepared Integration, Not Activated

The continuation after `992f075` wires a read-only receipt reader into the panel,
the continuous consumer's root service and its internal `GatedService`. It reuses
`THUNDER_OS_SEQD_READ_ENABLED=true`, the existing private `seqd_bridge` credential
and the pinned SEQD mailbox/workspace. Construction performs no HTTP or OAuth.
No new route, credential, table, ledger, background polling or send transport is
introduced. The reader cannot send, reconcile a remote attempt, or retry a send.

Receipt formats `gmail:<message-id>` and `email:gmail:<message-id>` are handled
explicitly. The latter is the existing neutral email adapter envelope; neither
format by itself proves an email exists. Confirmation requires a fresh read from
the pinned sender account, an unambiguous single sent message, recipient, subject
and complete plain text. Drafts, missing headers, multiple recipients and partial
content remain blocked. Internal step IDs come from a unique persisted effect
binding, not from Gmail. No missing identity/effect is fabricated for old mail.

Research/copy work without an operational result is unaffected. A receipt reader
does not create the missing operational work/effect producer and does not turn
HubSpot activity logs into sender-account evidence. It establishes a message in
the sender account, not delivery to or appearance in the recipient's inbox.

### Readiness And Real-History Check

The live inspection before this change found panel/main `969f8ef` and agent
`d562788`, with distinct symlinks and intact manifests. The active database had
zero internal cadences/identities/approvals/effect jobs; six registry entries are
not six executable internal cadences. Existing external campaigns are not adopted
or changed by this code.

The SEQD bridge on `127.0.0.1:8792` was not listening. No service was started to
hide this blocker. An independent Gmail connector read confirmed an existing
historical self-test against its canonical SEQD preview and sent receipt. That
read is evidence about the historical message, NOT proof of the panel/SEQD route
or a production work/effect binding. Private IDs/hashes and the current review
status belong in the Brain's `cadencia-multicanal` record, not customer fixtures.

### Publication Procedure: Operator Approval Required

1. Recheck the current panel and agent PID/cwd/symlinks and manifest integrity.
   Bind the approved candidate to the exact reviewed source SHA and pinned
   dependency in `runtime/dependencies.json`; never build the dirty main checkout.
2. Resolve the read-backend blocker under a separate explicit operational scope.
   Do not restart frozen GTM, campaigns, SEQD send queues or a personal bot.
   Verify account identity and one exact historical message through the actual
   configured reader. Keep sending flags and campaign configuration unchanged.
3. Re-run the email reader/composition tests, all 36 eval scenarios and affected
   regressions. Have an independent reviewer bind the result to the final diff.
4. Panel rollout uses its existing immutable bundle/promotion procedure and retains
   the then-active `current` as rollback. No database migration/backfill is part
   of the reader. A panel promotion alone does NOT upgrade the agent.
5. Do NOT promote the old agent blindly to the panel candidate. The inspected
   `d562788..992f075` delta already contains 133 files, including previous features.
   The operator must approve that broader source delta or a separately qualified
   agent release. Preserve `agent-current` independently. Propagation to the
   internal `GatedService` is required, not just the root consumer service.
6. Verify the promoted process path/manifest, read-only history and unchanged
   research/copy flow. Do not create real effects, approvals or CRM entries merely
   to make a smoke test pass. No sends or retries are authorized by this runbook.

### Rollback And Remaining Activation Gates

- Roll back only the component just promoted to its recorded immutable previous
  release, using the existing serialized promotion/rollback mechanism. A restart
  is an explicit operational action, never an automatic test side effect.
- No reversal of campaign data, identities or sent receipts is required: this
  integration does not migrate them. Preserve any newly written work/events;
  never restore an old database to disguise a failed read.
- Keep previous panel `969f8ef` and agent `d562788` until fresh runtime checks
  select the actual rollback targets; the values here are dated observations.
- Missing backend, identity, unique sent effect, matching provider/account or
  complete readback blocks operational verification. It must not trigger a send,
  fallback provider, automatic retry or success claim.

### Confirmed Backend Contract Blocker

The inspected bridge code at
`/var/lib/seqd-thunder-bridge/build/b6c3dd6aefe0247b50156b6dd9aeb818cd332ea3/bridge/bridge.cjs:39`
returns only `id`, `thread_id`, `body`, `truncated`, `from`, `to`, `subject`, `date`.
Its native helper computes `sent` but the bridge drops that field; labels and
Cc/Bcc are also absent. Starting that old bridge alone would not qualify it.
The reader explicitly rejects this incomplete response. Full Gmail MIME evidence
or a complete trusted response is required; no default false/empty fields are
invented for missing evidence. HTML-only mail is not verified as complete plain
text by this reader.

The independently reproduced `F-EMAIL-1` counterexample contained an approved
plain part plus additional inline HTML. The reader now accepts only an integral
root `text/plain` MIME message without child parts or attachments; all multipart
structures, including `multipart/alternative`, are rejected. The same restriction
must hold in the prepared bridge's normalized read-only response. This deliberately
does not claim to verify HTML email or silently convert it to plain text.

An in-memory, non-installed candidate probe on the VPS reused the panel's actual
credential-file setting and attempted only `GET /v1/status`, socket timeout five
seconds. It returned `email_read_unavailable` while the bridge was offline, with
no message request, retry or state write. An earlier probe without that setting
failed before HTTP; it is not counted as transport evidence. No token was printed,
copied into the repository, rotated or provisioned.

The prepared reader identifies provider `gmail`, account the exact mailbox in
the Mexico registry. SEQD is its access transport. Apollo, Instantly, HubSpot
sequences and other sender accounts are not silently mapped to Gmail. The five
second timeout bounds each socket operation, not an absolute wall-clock deadline
against a peer streaming arbitrarily slowly. Backend availability/schema and any
service/credential deployment still require an explicit operational decision.

### Manual divergent internal observation (local candidate; not activation)

This path does not confirm a receipt, attribute an automatic send, renew an observation
window, release a reservation or retry a job. It is restricted by the existing internal
canary binding and requires the original `failed/proved_not_sent` job and its original
24-hour observation window. The original job, assisted record, grant and journal remain
unchanged. Multipart bodies are contextual evidence, not exact-send receipt evidence.

The authenticated Ygor POST routes under `/api/os/v1/integrations/codex-assisted/`
use the existing origin and nonce checks:

* `manual-observation-preview`: `job_id`, `message_id`, `declared_author`.
* `manual-observation-record`: those fields plus `review_hash` and `approval`.
  Approval must be exactly `{action: "record_manual_divergence", review_hash, confirmed: true}`.
* `manual-observation-reply-preview`: `job_id`, `message_id` (incoming reply).
* `manual-observation-reply-record`: those fields plus `review_hash` and `approval`.
  Approval must be exactly `{action: "record_manual_reply_and_pause", review_hash, confirmed: true}`.

A message's text is never authorization. Each record requires separate explicit approval
of the current preview by the authenticated operator. Declared authorship is stored as
unverified, alongside complete observed content, MIME alternatives, headers, source IDs,
hashes, expected content and differences. Fresh reads bind the reply to the same identities,
thread and RFC In-Reply-To. Reply approval includes the current cadence hash; concurrent
cancellation invalidates it. One SQLite transaction records the reply and internal pause,
with immutable observation/audit records and idempotent retries. No PostgreSQL write,
CRM call or external pause is part of this operation.

The Journey projection labels manual divergence and manual response separately, retains
`proved_not_sent` for the automatic step, and excludes the internal test from commercial
metrics. A hash, approval and matching audit event are required for that projection.

Promotion of the panel and read-only Gmail bridge code requires separate approval and
release/rollback verification. Recording either real observation or real reply requires
its own explicit approval after fresh previews; publication alone authorizes neither.
The existing observation deadline is not extended by this candidate. After expiry, these
operations fail closed. No live execution was performed while implementing this path.
