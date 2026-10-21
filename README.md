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

The Mexico E1 writer was not located in this source repository. This candidate does
not replace it, mutate `toques.py`, persist `hubspot_task_id` for future executions,
or schedule a next touch. Integrating those writes with the actual executor and
verifying Apollo/Instantly production bindings remain separate, unresolved work.

Focused isolated checks: `PYTHONPATH=_ferramentas python3 -m unittest
test_thunder_os_cadence_evidence test_thunder_os_cadence_readonly`; use the existing
Playwright harness `_ferramentas/test_painel_cadencias_ui.cjs` for UI coverage.
