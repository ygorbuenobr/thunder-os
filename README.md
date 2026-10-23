# Thunder OS source and release assembly

This private repository contains Thunder OS domain code, its panel entrypoints, focused tests, release tooling and the technical reference for comparing the operating model with other GTM systems. Thunder OS is distinct from Thunder GTM, Thunder Cockpit and HunterV2.

## What this system is

Thunder OS is a governed GTM operating system, not an autonomous outreach bot. It separates:

1. **Signal and context**: account-first research, connection intake, identity resolution and evidence freshness.
2. **Decision and workflow**: versioned work items, cadences, approvals, pauses and learning experiments.
3. **Effects**: a separately guarded layer that reserves an exact touch, executes once, records a receipt or uncertainty, and reconciles the provider outcome.
4. **Human control**: first touch, new person, content changes, spending and suppression overrides remain human decisions. A delegated agent never creates its own authority.

The canonical architecture, data models and requirements are in `.specs/features/thunder-os-v1/`. The following comparison dimensions are intentional and should be used when evaluating another GTM system:

| Dimension | Thunder OS contract |
| --- | --- |
| Source of truth | Explicit authority per datum: curated knowledge, SQLite workflow, PostgreSQL touch ledger and provider receipt/readback |
| Research | Account-first evidence with source URL, publication/observation dates, alternative hypothesis and gaps |
| Cadence | Versioned steps with target, channel, content, sequence and interval bound to approval |
| Authorization | Exact payload/hash, actor, expiry, daily ceiling, kill switch and global pause |
| External effect | Reserve before call; confirmed receipt or `uncertain`; never automatic retry after an uncertain provider outcome |
| Agents | Can prepare evidence and work; cannot approve, spend, invent receipts or bypass a provider boundary |
| Auditability | Append-only events, idempotency keys, manifests, source/dependency hashes and production readback |
| Learning | Evidence produces a bounded recommendation or experiment; no automatic rule promotion |

## Ownership boundary

- This repository owns `_ferramentas/thunder_os/`, the Thunder OS panel code, selected tests and assembly scripts.
- The operational brain, account dossiers, queues, logs, databases, credentials and environment files remain outside this repository.
- The panel and `touch_ledger.py` use the separately owned `_ferramentas/toques.py`; the candidate builder requires its exact pinned code hash and records it in the manifest instead of vendoring it as source.
- A source commit or candidate does **not** mean the runtime has been migrated. Production is an immutable release with a manifest; a service switch requires snapshot, rollback rehearsal and observed functional checks.
- Do not infer direction from a timestamp alone. Compare the source SHA, every source-owned file hash in the release manifest and the pinned `toques.py` dependency hash before calling GitHub and production synchronized.

## Interfaces and component map

### Human console and local API

`_ferramentas/painel_servidor.py` is the loopback HTTP adapter. The stable product namespace is `/api/os/v1/`: `session`, `today`, `connections`, account context, work, research, copy, cadences, replies, effects, controls and integration preview/reconcile routes. Browser mutation requires an authenticated actor, same-origin/nonce checks and server-side authorization. Legacy `/api/` routes are compatibility surface, not the clean OS contract.

### Core workflow

| Component | Responsibility | Relevant interfaces |
| --- | --- | --- |
| `models.py` | Domain IDs, payload hashing, state transitions | `parse_work_item`, `transition`, `payload_hash` |
| `db.py` | SQLite transactions, migrations, outbox, leases and read models | `Repository` |
| `service.py` | Only workflow-state writer and orchestration boundary | `ThunderService` |
| `cadence.py` | Due calculation, order, reply stop and reconciliation | `CadenceEngine` |
| `approvals.py` | Exact versioned approval packs | `ApprovalService` |
| `knowledge.py` | Hashed safe reads and validated fact promotion | `KnowledgeStore` |

### Intake, research and agents

- `connection_runtime.py`, `connection_intake.py`, `connection_ledger.py` and `connection_backfill.py` implement provider-read → canonical identity → eligibility. A connection or profile read never authorizes contact.
- `research_producer.py` plans and collects bounded public evidence; `research.py` validates its provenance and returns one of `abordar`, `acompanhar`, `pesquisar_mais` or `nao_priorizar`.
- `agent_gateway.py`, `agent_consumer.py`, `continuous_consumer.py` and `hermes_text_child.py` receive validated work envelopes and return evidence or draft work. They cannot declare approval, execution or receipt.

### Effects and integrations

- `worker.py`, `touch_ledger.py` and provider adapters form the effect boundary. Reservation, final validation, receipt and reconciliation are independent from draft generation.
- ConnectSafely, HubSpot and SEQD adapters are channel/CRM capabilities. Read access never creates write authority. Preview, execute and reconcile are distinct paths.
- `ops.py`, `connection_reconcile_cli.py`, `scripts/build_runtime_bundle.py` and `scripts/check_runtime_bundle.py` provide operational inspection, recoverable reconciliation and immutable assembly.

## Local tests

### Jornada and Account Read Views

The panel's Jornada replaces the Today presentation without changing delivery
workers. It projects Brazil-only observations using existing readers; Mexico
remains in Campanhas. Unknown totals stay unknown, with a source and coverage
reason. Provider receipts, not task completion or an approved draft, prove sends.
Full observed thread bodies remain available after an outgoing reply so that
an older inbound message is not misrepresented as unanswered.

Authenticated Ygor read routes (no provider mutation or model call):

- `GET /api/os/v1/journey?campaign=BR-APOLLO-01`
- `GET /api/os/v1/journey/details/<observed-id>?campaign=BR-APOLLO-01`
- `GET /api/os/v1/journey/accounts`
- `GET /api/os/v1/journey/accounts/<observed-id>`

The only optional query is one `campaign`. Unknown identifiers are not arbitrary
file paths or permission to fetch a different CRM contact. The account reader
shows existing Markdown, provenance and missing evidence; a reference to a file
does not prove an agent previously read it. No account text is generated by GET.
Local editor changes never imply sending or a provider receipt. Existing backend
approval/context/identity gates remain authoritative, as do paused agent controls.
No database migration, bulk contact import or campaign activation is performed.

Ygor's clarified interaction contract: a persisted approval authorizes execution
of the exact selected touch, not a content-only review and not the other future
touches. Reuse the existing approval pack, campaign confirmation and worker
receipt checks. An account document without a canonical cadence, step and
identity is a read/edit preview, never implicit enrollment or execution authority.
The agent remains paused during this panel rollout; approval is not a receipt.

From the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest \
  _ferramentas/test_thunder_os_models.py \
  _ferramentas/test_thunder_os_db_integracao.py \
  _ferramentas/test_thunder_os_approvals.py \
  _ferramentas/test_thunder_os_cadence.py \
  _ferramentas/test_thunder_os_research.py \
  _ferramentas/test_thunder_os_research_producer.py \
  _ferramentas/test_thunder_os_knowledge.py \
  _ferramentas/test_thunder_os_learning.py \
  _ferramentas/test_thunder_os_hermes_dependency.py \
  _ferramentas/test_thunder_os_hermes_auth_ro.py
```

Run the source-boundary guard before committing:

```sh
python3 scripts/check_source_boundary.py
```

Run focused connection tests separately as they are added. Agent/service journeys, provider and Panel integration, the brain account-catalog fixture, and runtime unit-file checks require VPS staging; local tests do not prove deployment or live integration. The account-specific producer test is intentionally omitted from the copied test file; the remaining producer tests are retained.

### Cadence Evidence (Task 2 Candidate)

`cadence_evidence.py` reads the legacy pipe title only through the explicit mapping
`MX27-CDMX-D0-20261021 -> MX27-CDMX`. Other batch prefixes are not aliases. Gmail
confirmation paragraphs require matching metadata, the pinned mailbox, a complete
exact CRM contact association, recipient and payload hash. Later reconciliation
can supersede an older error. This is `recorded_confirmation` from the task body,
not a new Gmail read or evidence of delivery. Completed tasks without this evidence
remain distinct; uncertain sends have no retry action. Missing coverage preserves
null totals and separate observed counts. Replies and meetings remain null without
a campaign attribution contract. A portal/owner identity failure stops all further
provider reads, leaving only unknown local registry projections.

The Task 3 read-only candidate supersedes the earlier Apollo/Instantly GUID/source
binding decision. Commercial email attribution is now strictly by complete HubSpot
email/contact ID associations, the exact current contact `campanha_ativa`, and an
active validated Registry campaign with platform `apollo` or `instantly`. It is
labelled `contact_current_campaign`. No prefix, title, name, address, GUID, event
source ID or inferred enrollment time establishes campaign membership. Legacy
`THUNDER_OS_CADENCE_ACTIVITY_BINDINGS` input is ignored by this reader, including
malformed input; its parser/import remains for server compatibility.

An old email can therefore appear under the contact's current commercial campaign.
This deliberate current-association view is **not historical membership proof**.
Returned events carry `historical_membership_verified=false`,
`delivery_verified=false`, `origin_provider=null` and sanitized
`origin_identifiers` where available. `provider` denotes the Registry platform,
explicitly labelled `provider_semantics=registry_platform_not_origin`; it does not
prove which integration produced the CRM record. No provider origin is guessed.
The consumer renders current-contact attribution as "Envio registrado · campanha
Apollo/Instantly", with "Campanha atual do contato; origem e entrega não
verificadas." Legacy provider-origin evidence retains its previous label.

For Apollo/Instantly groups, `metrics.contacts` and `observed_contacts` count the
distinct proved IDs across the group's tasks and attributed emails. Repeated
emails and a contact present in both sources count once. The heading is "Contatos
observados"; this is not a provider enrollment total. Incomplete task/email
coverage keeps `contacts=null`, with distinct proved `observed_contacts` separate.

`metrics.external_sent` counts only CRM `SENT` + outbound `EMAIL`, with
`metric_availability.external_sent=crm_recorded_sent_not_delivery`. Events expose
`crm_recorded_sent` separately from `receipt=null`. Replies and meetings remain
null for campaigns: a received CRM email is not inferred to be a campaign reply
or a human response. Existing per-contact context metrics remain separately scoped.
No meeting search or attribution by an event GUID/source pair runs in this path.

Missing properties produce `configuration_pending`; omitted/unknown/multiple
campaign values, incomplete associations, missing contact records and conflicting
associated campaigns do not attribute an event or establish zero sends. A contact
field explicitly returned as JSON null or an empty string proves no current
campaign (`without_campaign`), whereas an omitted field remains unknown. An email
whose fully read associated contacts agree on no current campaign, or on an exact
active Registry campaign on another platform, is outside this reader's scope.
These exclusions remain in `unattributed_activities` with `contact_without_campaign`
or `contact_campaign_out_of_scope`; they do not degrade Apollo/Instantly totals,
even if the excluded email lacks classification metadata. Mixed/conflicting contact
campaigns and unknown, inactive or unregistered values still keep totals null. Bounded
pagination, batch status, schema and budget failures preserve null totals with
separate observed counters and explicit coverage. A zero is possible only after a
complete read. Email-only attributed contacts may use the existing timeline route.
`contact_conflicts` exposes task/current-campaign and executor/Registry differences;
it does not block admission or change the contact. Executor differences are visible
diagnostics, not an extra email-attribution gate. `unattributed_activities` preserves
observed records with the reason attribution was unavailable.

The external Codex Mexico E1 runner has now been recovered and adapted in
`scripts/mexico_e1_runner.cjs`. It is a callable source module with no autorun,
HTTP route, scheduler or activation. `createMexicoContext` binds the current MCP
Gmail, HubSpot and ConnectSafely tool names to the original prepared package,
ledger, derived checkpoint, workbook and already-held `fcntl` process.
`runMexicoE1` calls the real `mexico_toques_bridge.py` before and after the Gmail
effect. Importing/constructing this module does not send, update CRM or acquire a
lock. The current authorization covers local source/tests/review only.

The caller supplies the canonical operation root and workbook path, the original
`sendLock` child handle and its `LOCK_HELD` acknowledgement, the actual MCP tools
object, and an absolute path to the pinned dependency. The consumer reads existing
`.work/cdmx-d0-prepared.json` and `.work/cdmx-email-send-ledger.jsonl`; an explicitly
invoked run appends/fsyncs that same ledger and replaces only the existing derived
`.work/cdmx-email-results.json`. It does not rewrite the package/queue. Task IDs are
normalized from legacy numbers to strings; contact-level `STOP_ON_REPLY` events
without a task ID remain effective and are retained in the checkpoint. A durable
intent or uncertain result blocks another send, including when toques has no row.

The runner retains the original E1 scope, approval/copy hash, workbook exclusion,
exact contact association, suppression/reply/history preflight, held lock, dated
window and 65-80 second minimum send pacing (inside the approved 60-120 second
range). Portal `51412240`, owner `92901049` and the mailbox are pinned; the
consumer checks the authenticated identities. Missing suppression fields are
unknown and block sending; explicit null/empty values remain distinct from absent
fields. MCP transports supply their own request deadline; the runner awaits each
effect and does not race a timeout against an abandoned send or retry it. The
Python ledger/workbook subprocesses have a 15 second deadline. The historical
window expires at `2026-10-22T06:00:00Z`; this source grants no new approval/window.

The canonical hash is `SHA256(subject + '\n' + text)` with one actual newline.
After a successful send, Gmail `full` readback must match message ID, `SENT`, exact
sender/recipient, subject and HTML body. `PROVIDER_CONFIRMED` is saved in the
existing ledger and toques becomes `feito` before CRM reconciliation. A structured
JSON receipt in the task binds campaign, batch, task, contact, step, mailbox,
recipient and payload hash. Its deterministic marker/key prevents re-appending.
CRM completion and exact body readback are checked separately from HTTP/tool
acceptance. A later CRM failure yields `PROVIDER_CONFIRMED_CRM_PENDING`, preserving
the confirmed receipt. Replay reconciles CRM without another Gmail send. A lost
persistence response never grants retry authority. A full readback followed by a
ledger write failure still remains a factual provider confirmation; the runner
attempts to persist it in toques and retains its receipt for reconciliation.

`mexico_toques_bridge.py` checks dependency SHA256
`84eb0c7a86fe8fbe72c11427e747895b30d6acc66135eb36e4a8d3aa28e1e875` before import.
Local checks use `.scratch/dependencies/toques.py`; operational adoption must
explicitly supply the same verified dependency from its existing bundle. The
current Brain copy has another hash and is refused. There is no vendored copy,
manifest update, schema change or migration. Campaign/task/contact/hash/mailbox
bindings use supported `ref`/`detalhe` JSON, never a silently ignored
`hubspot_task_id` keyword. Reservation uses the canonical API and unique key.
Finalization uses the existing `TouchLedger.finish` conditional SQL pattern over
the actual dependency's `_banco`: only `reservado`/`incerto` may change. This
protects terminal `feito` against a late uncertain write or competing receipt.

E1 retains the existing normalized subject target. A later step sharing its
subject uses a step-qualified target only after checking an exactly linked,
confirmed earlier subject record. Legacy unlinked, uncertain or conflicting
history blocks it. The structured reader accepts the exact step from its binding;
legacy prose receipts remain E1-only. The exported runner itself remains E1-only:
no E2 automation or follow-up scheduler is activated by these adapter/reader tests.

Provenance: `scripts/recover_mexico_runner.cjs` parses the original session JSONL
as data, without evaluating historical calls or their trailing invocation. Records
32661, 32944, 35212 and 35217 identify the stored runner, independent-item
continuation, ledger-derived checkpoint patch and subsequent invocation. The
cooked original source has 10082 characters, SHA256
`9281ed8790b92bb07ee798c70c431b0344063e82d37bf6dc1673057b78d42b52`;
after the two recorded edits, 10268 characters, SHA256
`de28f79e6bdec4e4dc9a2066126249af4d436d57c845fae466af7f288cf5910c`.
The callable module is an adaptation of that source, not a byte-identical export:
private records/paths moved to the operation context, the hash contract explicitly
uses a real newline, toques and structured receipts were wired, and CRM failure was
separated from provider uncertainty. The raw historical source stays outside Git.

Task 3 update (2026-10-21): the reader was published as `2a08b0d`; the two CONTACT
string properties were subsequently created and GET-verified in portal `51412240`,
group `cadencia_thunder`, under explicit operator authorization. No contact values
were assigned. Earlier 404 observations describe the pre-creation runtime only.

The membership workflow and concrete HTTP port are prepared with focused fake
tests, including `test_lead_ocupado_bloqueia` and `test_resposta_libera`.
The panel exposes only `GET /api/os/v1/campaign-membership/<contact>/<campaign>`;
both composition switches are explicitly `mutation_enabled=False`, independently
of legacy external-effects flags. There is no membership execution HTTP route.
The read-only cadence projection shows task/current-campaign and executor
conflicts. An internal proposal is never membership truth: HubSpot remains the
source, and local CAS cannot serialize external CRM writers or platform enrollment.
See `docs/task3-membership.md` for activation prerequisites and known limits.
Release and assignment remain OFF pending a separately authorized operational
activation using the existing approval machinery; changing an environment flag
alone does not enable them. No bulk assignment, external pause or send is enabled.

## External email activity preparation

`apollo_readonly` reads the existing scoped sequence and its completed content;
`external_email_import` plans minimal contact creation or existing-contact reuse
and factual EMAIL logging. It never dispatches an email or assigns a campaign.
`instantly_webhook` requires a custom authentication header and independent API
readback before preparing an event; an automatic reply is not a human reply.

The panel publishes a read-only external-email status, a default-disabled event
ingress (HTTP 410 before body/provider access), and an authenticated stateless
simulation endpoint. All CRM writers remain OFF, no webhook is registered, and
no Apollo/Instantly runtime credential is provisioned by this code. A live canary
must wait for the explicitly missing HubSpot EMAIL-write permission; do not switch
credentials or use another interface to bypass it. Source IDs are audit metadata,
not fabricated RFC Message-IDs. See `docs/external-email-integrations.md` for the
activation prerequisites, deduplication and uncertain-result recovery contract.

Focused runner checks (Python 3.14.3, Node 22.23.1 locally):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas \
  THUNDER_TEST_TOQUES_PATH="$PWD/.scratch/dependencies/toques.py" \
  THUNDER_TEST_WORKBOOK_PYTHON=/absolute/path/to/python-with-openpyxl \
  python3 -m unittest test_thunder_os_mexico_runner \
  test_thunder_os_cadence_evidence test_thunder_os_cadence_readonly
python3 scripts/check_source_boundary.py
git diff --check
```

The suite uses the actual pinned dependency with a temporary SQLite table matching
its existing unique key, an injected `_banco` for interleaving tests, and the real
CLI bridge from the callable runner. MCP providers are fakes. The context consumer
test uses synthetic prepared/ledger files, a real temporary `fcntl` holder and an
openpyxl synthetic workbook; the system Python may need a separately configured
workbook interpreter. No real provider effect, PostgreSQL/production write,
installation or active automation is demonstrated by these tests.

Focused isolated checks: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest
test_thunder_os_cadence_contact_campaign test_thunder_os_cadence_evidence test_thunder_os_cadence_readonly`; use the existing
Playwright harness `_ferramentas/test_painel_cadencias_ui.cjs` for UI coverage.

The Task 3 round 2 browser harness intercepts all requests, uses synthetic responses
and checks current-contact Apollo/Instantly labels at 1440 and 390 pixels. It keeps
screenshots under `.scratch/task3-reader-round2-ui/`, without overwriting prior
evidence. It uses the already available bundled Node/Playwright, with no install:

```sh
NODE_PATH=/Users/ygorbueno/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/ygorbueno/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  _ferramentas/test_painel_cadencias_ui.cjs
```

## Campaign Sheet (PRD-1)

The panel calls this read-only projection **Ficha da campanha**, not the Hermes
daily briefing. The existing campaign config may contain `briefing` with
`version` and `fields`: `offer`, `icp`, `proofs`, `objective`, `restrictions`,
`cta`, `language`, `country`. Each present field contains only `path`, `sha256`
and `section`, or a bounded list of these references when multiple sources are
required: a relative Markdown path in the configured Brain, the SHA-256 of
the whole UTF-8 file's bytes, and an exact Markdown heading. Content is read
from the Brain, not copied into the registry. Missing references are valid
incomplete metadata, not permission to approve or send.

The existing `configuration_hash` includes the sheet and optional child
`batches` only when present; legacy configs retain their exact previous hash.
There is no new authorization field, table or approval store. A child batch
has `batch_id` and explicit `source_codes`; a source code cannot belong to two
active parents. No runtime task title, receipt or contact association is
rewritten by adding this model.

`GET /api/os/v1/campaign-sheets` uses the existing authenticated Ygor gate.
The card shows each source, expected/observed hash, modification date and section
text. Missing, unreadable, changed or empty sections mark **ficha incompleta**.
Research and draft preparation remain available; approval and the pre-dispatch
checks for cadence effects and SEQD/LinkedIn previews require a current complete
sheet. Config changes invalidate the existing scope hash; Brain byte changes
also block execution without a new registration/review/approval. Editing a
Brain file never silently updates its approved reference.

The code does not populate real sheets or migrate batches automatically. Current
campaigns without a sheet display incomplete until their references are reviewed
and registered through the authenticated route. A panel publication and the six
factual sheet registrations were separately authorized on 2026-10-23; this does
not authorize outbound, CRM writes, batch migration or activation of the agent.
The canonical preview and identity audit are in the private Brain protocol at
`projetos/thunder-os/cadencia-multicanal/campaign-sheet/`; no CRM data is shipped
in the source repository. Existing uncertain outcomes and receipts are retained.

```sh
PYTHONPATH=_ferramentas python3 -m unittest test_thunder_os_campaign_sheet \
  test_thunder_os_campaign_registry test_thunder_os_approvals
node scripts/test_campaign_sheet_panel.cjs
```

## Today Work (PRD-4)

The four operator views are Hoje, Campanhas, Contas and Sistema. Today Work is
a read-only projection with Responder, Fazer and Aprovar blocks. Each item
exposes owner, campaign, date and one primary action. Mexico campaign codes and
confirmed associated identities are excluded from Today, not from Campaigns.
Legacy approval cards remain consultable as an archive; they do not become
executable approvals. A historical referral is context for a human action,
never permission to message the indicated person.

`GET /api/os/v1/today-work` uses the existing authenticated Ygor boundary and
a bounded cache. Source failures are reported independently. Replies require
human-inbound evidence and chronology; incomplete coverage cannot prove the
absence of a reply. Bounce, automatic mail and unverified identities do not
silently become commercial opportunities. CRM reads never log email or create
contacts/tasks. No new database, ledger or authorization mechanism is added.

Draft generation is separate from polling. An explicit authenticated human
request may use the existing tool-free Hermes Codex OAuth child on the
server-resolved message context; there is no paid API fallback, provider tool
execution or agent activation. A draft is not approval or an outbound message.
Existing approval, exact-text and uncertain-no-retry protections remain in force.
