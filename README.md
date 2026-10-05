# Thunder OS source and release assembly

This private repository contains Thunder OS domain code, its panel entrypoints, focused tests, and release tooling. Thunder OS is distinct from Thunder GTM and Thunder Cockpit.

## Ownership boundary

- This repository owns `_ferramentas/thunder_os/`, the Thunder OS panel code, selected tests and assembly scripts.
- The operational brain, account dossiers, queues, logs, databases, credentials and environment files remain outside this repository.
- The panel and `touch_ledger.py` use the separately owned `_ferramentas/toques.py`; the candidate builder requires its exact pinned code hash and records it in the manifest instead of vendoring it as source.
- The active VPS panel and agent still use two historical releases. A source commit or candidate does **not** mean the runtime has been migrated; the service switch requires snapshot, rollback rehearsal and observed functional checks.

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
