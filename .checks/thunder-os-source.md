# Thunder OS Source Separation

Sources:

- User request and repository URL - create a dedicated private Thunder OS source repository, separate from Thunder GTM.
- `.specs/features/thunder-os-v1/spec.md` - **binding for product boundary**: Thunder OS V1 identity and legacy exclusions.
- `.specs/features/thunder-os-v1/context.md` - **binding for implementation boundary**: product, brain, agent, and legacy separation.
- `.specs/features/thunder-os-v1/design.md` - **binding for runtime boundary**: modular Painel integration; no independently deployed service.

## Out of scope

- VPS, active releases, systemd, panel runtime, databases, brain content, CRM, and deployment - source-repository preparation only; these remain unchanged.
- Thunder GTM/Cockpit and HunterV2 source or data - separate products and owners.
- Full repository migration - the mixed checkout contains private operational and account material.

## Landing

The new repository owns only `_ferramentas/thunder_os/`, selected focused tests, and the approved Thunder OS V1 specification. Shared Painel HTTP/UI and release assembly remain in their existing owner; no runtime or service boundary changes.

| One-way door | Literal shape | Alternative rejected |
| --- | --- | --- |
| GitHub source authority | Exact private repository `ygorbuenobr/thunder-os`; new local `main` branch pushes only to that URL | Reusing the mixed checkout's `origin`, which points to the legacy `thunder-gtm` bare repository and risks conflating products |

- No deployment or runtime changes are part of this work.

## Checks

### S1 - Source-only repository boundary · ~550 KB source/spec set

**C1** - Tracked paths are limited to approved governance/spec files, Thunder OS Python modules, and explicitly selected tests.
Proof: `python3 scripts/check_source_boundary.py`

**C2** - Product docs state this repo is not independently deployable and does not replace the shared Painel/runtime.
Proof: `rg -n "not a deployable replacement|not independently deployable|shared Painel" README.md AGENTS.md`

**C3** - Selected isolated unit tests pass in this repository.
Proof: the single `python3 -m unittest` invocation documented in `README.md`

The copied producer test excludes its one test that reads the real account/contact catalogue from the operational brain; the remaining producer behavior tests are included.

**C4** - The configured push destination is the exact private Thunder OS repository, not the legacy `thunder-gtm` remote.
Proof: `git remote get-url origin` and `gh repo view ygorbuenobr/thunder-os --json visibility,url`

## Swept

- validation: C1
- failure modes: C1 rejects unclassified tracked paths
- idempotency: not in scope - source extraction does not process runtime jobs
- authorization: C4 and push limited to the exact user-created private repository
- concurrency and ordering: not in scope - no runtime mutation
- data lifecycle: C1; operational/customer data is excluded
- external-dependency failure: C3 uses isolated tests; provider integration is excluded
- state transitions: not in scope - no runtime state changes
- observability: command results and commit SHA retained in task output; runtime observability is out of scope
