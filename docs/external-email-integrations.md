# External email integrations: A/B before T4

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
