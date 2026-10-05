# Thunder OS agent Python path contract v1

This is a deployment contract for `thunder-os-agent.service`, not an installed
unit or authorization to change the active service. The agent's release root is
`/opt/thunder-os/agent-current`. A candidate puts the separately owned, pinned
`toques.py` at `_ferramentas/toques.py`; imports of `toques` require that
`_ferramentas` itself be on `PYTHONPATH`.

The effective agent environment must include both paths, in this order:

The versioned drop-in is `thunder-os-agent-pythonpath.conf` in this directory.
It contains the exact environment assignment and must be installed alongside
the candidate release; this document alone does not change the effective unit.

Before a later authorized activation, verify the actual agent unit and all
drop-ins, the `agent-current` target, candidate manifest/source SHA, pinned
`toques.py` hash, database compatibility, and the agent's other environment
settings. Snapshot the current unit/drop-ins and relevant state; retain the
known-good release and a tested rollback route. Run a candidate import check
with this exact effective path before switching the service. A root-only
`PYTHONPATH` must fail that check.

Promote the release and unit configuration serially only after those checks.
Confirm the effective environment and process paths, start and stop behavior,
and the agent's functional readback before declaring success. On regression,
restore the prior unit/drop-ins and release target; restore database state only
after checking schema compatibility. This contract does not activate, restart,
or modify any VPS service.
