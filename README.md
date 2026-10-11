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
# Local shared preparation candidate

The optional `thunder_os.shared_preparation` module reuses the existing OS workflow,
repository, consumer and outbox. The panel Handler has opt-in shared HTTP routes;
the normal CLI has no shared composition and returns 503 for those routes. Telegram
profiles and MCP tools are not registered by this candidate. Constructing the existing
service without the optional policy retains the existing behavior. No new provider/model,
credentials, scheduler, database or Brain is introduced.

A trusted local composition must inject a `SharedContext(KnowledgeStore(...),
brain_commit=...)`, an `account_reader` returning canonical identity, existing owner,
complete dated HubSpot/Gmail/LinkedIn history metadata and stop/reconciliation status,
and a route-bound `SharedPrincipal` resolved from existing authenticated authority.
The Brain commit callable must reflect the actual checkout; the module verifies
document and manifest hashes but cannot authenticate a caller-supplied commit claim.
Private style/learning files are projected only through operator-selected excerpts.
Missing/stale history fails closed. Owner `None` permits preparation for Ygor without
transferring CRM ownership. Stop checks are conservative at the account level.

`LocalOSAdapter.call` exposes only `get_context`, `request_preparation`, `get_work`.
It is a callable protocol boundary, not an HTTP/MCP server or an authentication
implementation. Hermes is restricted to `telegram` / `ygor-personal`. Read permission
does not imply prepare/decision permission. Envelope identity, authority, receipt,
model, executable and arbitrary file fields are rejected.

The existing `painel_servidor.Handler` routes are:

- GET `/api/os/v1/shared/context?account=...`
- POST `/api/os/v1/shared/preparations`
- GET `/api/os/v1/shared/work/<work_id>`

`painel_servidor.main(shared_factory=...)` invokes `configure_shared_server` on the
existing service. The explicit factory returns `(SharedPreparation, principals)`;
the mapping keys must equal the existing authenticated technical actor, with only
nonempty read/prepare scopes. Core and service/repository must be the same objects.
The helper takes an immutable snapshot of these bindings. It does not resolve or
create credentials, grant actors, discover providers or provide a fallback identity.
The existing loopback proxy identity, actor allowlist, Hermes credential and POST
origin/nonce checks remain in force. Their real proxy trust is not certified by the
synthetic tests. Without a factory the shared namespace is unavailable (503).

Canonical preparation identity is owner + canonical account + scope + capability +
explicit revision. Cross-channel equivalents share one work item; incompatible
transport replays conflict. An intentional new revision is a distinct unit of work.
The shared worker refuses transports configured for multiple attempts and persists
a reservation before dispatch. Cached parsed output survives loss of the completion
lease; unknown/interrupted inference is never automatically dispatched again. Its
resolution requires trusted provider evidence and a separately authorized operational
reconciliation. There is no implicit second-model review. Research and copy are
separate capabilities: a workflow requesting both deliberately has two units of work.
Deterministic context/status calls make no inference calls; ordinary agent chat still
uses its configured model and is not made free by this module.

Result and selected learning/decision handoffs use the existing outbox, stable files
under `_entrada/<actor>`, atomic no-overwrite writes and readback. `synced` means local
file readback only; it does not certify Brain Git transport or consumption elsewhere.
Conflicts/failures remain pending and queryable. Learning decisions record attribution
and supersession, without automatically editing canonical rules. Production identities,
history readers, channel tool registration and release/activation remain separate gates.

Offline acceptance command:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v _ferramentas/test_thunder_os_shared_preparation.py
```

These tests use temporary synthetic records, an import-only stub for the separately
owned `toques` dependency, fake generation and denied sockets/provider processes.
They prove local contracts, not real channel authentication or editorial compliance.

Actual local HTTP boundary acceptance:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest -v _ferramentas/test_thunder_os_shared_http.py _ferramentas/test_thunder_os_shared_preparation.py
```

The HTTP tests open only ephemeral numeric loopback sockets and use the actual
Handler/session/nonce guards with synthetic principals and credentials. Provider
sockets and subprocesses stay denied. A sandbox that denies local bind needs an
authorized loopback test execution; a bind error is not a passing route test.
