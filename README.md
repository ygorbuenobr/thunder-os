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

Apollo/Instantly attribution is an optional read-only runtime binding, injected by
`painel_servidor.main` through `THUNDER_OS_CADENCE_ACTIVITY_BINDINGS`, a JSON array
of objects with exactly `campaign_id`, `provider` (`apollo` or `instantly`),
`campaign_guid` and `source_id`. The operator must verify the integration's actual
HubSpot `hs_campaign_guid` and `hs_engagement_source_id` before configuring a pair.
No real pair is bundled or enabled by this change. Missing/invalid bindings and
partial/schema-failed reads remain visibly unknown, never an assumed zero.
Only an exact pair can attribute an email or meeting to a known campaign; a contact
association alone cannot. `SENT` is a CRM-recorded send, not a delivery receipt.
Explicitly attributed `INCOMING_EMAIL` records count as received replies, without
claiming a human classification. Meetings count `SCHEDULED`, `COMPLETED` and
`RESCHEDULED`; canceled/no-show records remain visible but do not count. Missing
classification fields, invalid timestamps, unavailable schema and partial pages
keep aggregate metrics null, with observed counters separate.

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

Apollo/Instantly remain **not integrated for campaign attribution in this
candidate**. The main coordinator's REST investigation on 2026-10-21 confirmed
that `hs_campaign_guid` and `hs_engagement_source_id` exist on emails/meetings,
but observed no populated campaign GUID/pair and no runtime bindings. The MCP
property lookup alone was insufficient to establish schema absence. Observed
provider sends are not equivalent to correlated HubSpot activity. Metrics remain
unknown, not zero sends. Configuring integrations/bindings, production writes,
installation and activation remain outside this local change.

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

Focused isolated checks: `PYTHONPATH=_ferramentas python3 -m unittest
test_thunder_os_cadence_evidence test_thunder_os_cadence_readonly`; use the existing
Playwright harness `_ferramentas/test_painel_cadencias_ui.cjs` for UI coverage.
