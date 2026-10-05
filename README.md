# Thunder OS domain source

This private repository contains the Thunder OS domain package, its focused tests, and the approved V1 product specification. Thunder OS is distinct from Thunder GTM and Thunder Cockpit.

## Ownership boundary

- This repository owns `_ferramentas/thunder_os/` domain and orchestration modules and the explicitly selected `test_thunder_os_*.py` tests.
- The shared Painel HTTP/UI, deployment bundle assembly, operational brain, account dossiers, queues, logs, databases, credentials, and environment files remain outside this repository.
- The current Painel integration imports Thunder OS code from the shared runtime. This repository alone is **not a deployable replacement** for `painel.r3xdigital.tech` or the active agent runtime.
- `touch_ledger.py` integrates with the separately owned `_ferramentas/toques.py`; this repository intentionally does not vendor that shared ledger.
- Creating this source repository does not split services, change runtime ownership, or authorize deployment.

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

Agent/service journeys, provider and Panel integration, the brain account-catalog fixture, and runtime unit-file checks require separately owned runtime/data; they are not represented as standalone tests here. The account-specific producer test is intentionally omitted from the copied test file; the remaining producer tests are retained. The selected suite does not prove deployment or live integration.
