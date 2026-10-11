# Connection intake provider compatibility

Sources:

- Conversation on 2026-10-11 - fixes are limited to Thunder OS; Thunder GTM is excluded.
- Live read-only diagnostic on `srv1433640` - the live window needs a sixth provider page and one historical row carries `U+2234` in a vanity slug that exactly matches its LinkedIn profile URL.
- `.specs/features/thunder-os-v1/spec.md` - existing fail-closed, identity-bound and no-external-effect contracts.

## Problem

The Thunder OS connection intake currently rejects two valid read-only observations: a live connection window whose activation boundary lies beyond the fixed five-page default, and a provider-confirmed LinkedIn vanity slug containing the mathematical symbol `U+2234`. The scheduled live/backfill readers consequently stop before advancing their checkpoints, so new connection evidence cannot reach identity and research preparation even though the provider account and pagination are valid. No throughput or user-impact metric was supplied.

After this ships, a complete bounded window can cross the observed sixth page, and a provider row whose safe Unicode symbol slug exactly matches its decoded LinkedIn `/in/` URL can enter the normal identity pipeline. Invalid, mismatched or incomplete observations remain blocked without writes.

## Out of scope

| Excluded | Why |
| --- | --- |
| Thunder GTM, Cockpit, ABM/account-first queues and deliverability | The user explicitly limited this change to Thunder OS. |
| Provider writes, messages, invitations, email, CRM writes or credit use | Connection intake is GET-only and this repair must not broaden authority. |
| Manual production reprocessing | Source correction and isolated verification come first; existing checkpoints remain preserved. |
| Dotin identity provisioning | No authenticated Dotin identity or supported endpoint is present on the VPS; inventing one would cross the credential boundary. |

## Assumptions

| Assumption | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Live window bound | Raise the default from 5 to the existing validated ceiling of 20 pages. | It fixes the observed sixth-page boundary without adding an unbounded crawl or a new configuration contract. | y |
| Safe provider symbol | Admit Unicode mathematical symbols only under the existing exact decoded URL-to-vanity equality check. | `U+2234` is provider-observed; broad punctuation/control/space acceptance is unnecessary and weaker. | y |
| Runtime activation | Build and verify an immutable candidate, then promote the verified Thunder OS release. | Source, release and runtime are separate authorities; Ygor explicitly confirmed the plan and promotion on 2026-10-11. | y |

**Open questions:** none - all resolved or logged above.

## Criteria

### S1: Complete live observation reaches the activation boundary (P1)

**Acceptance Criteria**

1. WHEN the activation boundary is first crossed on provider page 6 through 20 THEN Thunder OS SHALL return the complete ordered window and stop at that first boundary-crossing page.
2. IF no older-than-activation row or terminal page is observed after 20 pages THEN Thunder OS SHALL raise `connection_window_incomplete` before any checkpoint, profile or research-request write.
3. WHILE fetching the connection window Thunder OS SHALL use only GET requests bound to the configured provider account and shall preserve the returned contiguous cursor order.

**Independent test:** Run the focused intake and ledger suites with synthetic paginated fixtures; no network or production database is available to the test.

### S2: Provider-confirmed safe Unicode vanity reaches canonical identity (P1)

**Acceptance Criteria**

4. WHEN a vanity slug containing a Unicode mathematical symbol exactly equals the percent-decoded slug of a valid HTTPS LinkedIn `/in/` URL THEN Thunder OS SHALL preserve that slug in the canonical profile URL.
5. IF the vanity and decoded URL differ, or the slug contains a path separator, whitespace, control character or unsupported Unicode punctuation THEN Thunder OS SHALL raise `connection_row_invalid` after at most the existing one same-page revalidation.
6. The canonical profile normalization SHALL remain idempotent and SHALL not fold distinct Unicode spellings into one identity.

**Independent test:** Run the focused provider and intake suites with `U+2234`, mismatch, separator, control, punctuation and round-trip fixtures; all transports are mocked.

## Traceability

| ID | Slice | Criteria | Status |
| --- | --- | --- | --- |
| CONN-01 | S1 | 1, 2, 3 | Implemented; independent verification pending |
| CONN-02 | S2 | 4, 5, 6 | Implemented; independent verification pending |

## Observable

| Surface | Decision | Landing |
| --- | --- | --- |
| scheduled task `thunder-os-connections-live` | output remains the existing JSON summary on success | existing - `connection_runtime.run_once` |
| scheduled task `thunder-os-connections-live` | incomplete coverage retains static error `connection_window_incomplete` | AC 2 |
| scheduled task `thunder-os-connections-backfill` | invalid row retains static error `connection_row_invalid` | AC 5 |
| scheduled tasks | flags and invocation stay unchanged | existing - current systemd unit contract |
| scheduled tasks | partial failure must not persist intake state | AC 2 |

## Flow

This change reuses the existing provider validation, intake classification and transactional ledger; it does not introduce a second reader or cursor.

1. ConnectSafely GET page -> `ConnectionProvider` (exists) - validates account echo, schema, vanity/URL identity and contiguous provider cursor.
2. validated page -> `collect_recent_connections` (exists) - accumulates ordered rows until terminal coverage or the activation-day boundary.
3. complete window -> `record_live_poll` (exists) - classifies and atomically persists checkpoint/profile/research preparation; incomplete input exits before the transaction.

## Relations

None - no stored-data shape change.

## Surface

None - no externally consumed route or command signature changes.

## Landing

| One-way door | Literal shape | Alternative rejected |
| --- | --- | --- |
| None | No schema, public contract, dependency or persisted enum is added. | A new continuation checkpoint was rejected because it would alter stored state and recovery semantics when the existing bounded reader already supports 20 pages. |

- Nothing else in this change is hard to reverse.

## Impact

| Front | What changes |
| --- | --- |
| domain | `complete live window` keeps its meaning; its default bounded observation expands from 5 to 20 pages. |
| domain | `canonical profile slug` additionally recognizes Unicode mathematical symbols only within the existing exact provider vanity/URL equality check. |
| stored data | Nothing to migrate; existing checkpoints and profile identities remain unchanged. |
| runtime | A future immutable candidate would affect the panel-side live/backfill timers only; the separately released Thunder OS agent is unchanged. |
