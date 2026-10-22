# LinkedIn Preparation, Execution Off

## Bounded policy update (2026-10-21)

Source: the delegated user decision, on Mac/Darwin as `ygorbueno`, checkout
`/Users/ygorbueno/thunder/.worktrees/thunder-os-cadencias`, branch
`codex/cadencias-readonly`, base `d1d7ac28c0e685738a7c65d77a1d5496bd496739`.
The existing SDD ledger remains authoritative for the overall task. This update
owns only this document, `linkedin_preparation.py` and its existing test file.
No commit, push, deploy, operational Registry mutation, CRM operation or send is
authorized. Tests may register synthetic policies in their own temporary database.

The new policy shape is:

```json
{
  "timezone": "lead",
  "send_window": {"start_hour": 7, "end_hour": 21},
  "weekly_invite_limit": null,
  "holiday_countries": ["BR", "MX"],
  "holidays": []
}
```

`null` means no internal weekly ceiling. An account ceiling of 200 remains an
unconfirmed hypothesis and is never applied. A missing total-account counter
does not become a fabricated internal ceiling. Provider blocks/429 and provider
limits remain holds; execution stays hard OFF for every result.

### Landing

| Contract | Literal shape | Alternative rejected |
| --- | --- | --- |
| Lead identity | `next_slot(..., lead_timezone=None)`; `prepare(..., lead_timezone=None, lead_country=None)` | Deriving timezone or country from language or campaign name |
| Window | Start inclusive, end exclusive; preserve an eligible anchor instant | Rounding every anchor to a single daily send hour |
| Calendar uncertainty | New window policy always carries `calendar_unverified`; `due_at` is only a proposal using explicit dates | Treating an empty or partial date list as a complete BR/MX holiday calendar |
| Provider diagnostics | Optional quota mapping keys `state=blocked`, `http_status=401/403/429`, `error_code=provider_unauthorized/provider_forbidden/provider_rate_limited`, `provider_limit_reached=true` hold preparation | Treating no internal ceiling as permission to ignore provider refusal |

### Focused acceptance checks

Proofs below refer to `test_thunder_os_linkedin_preparation` and run with
`python3 -B -m unittest` from `_ferramentas/`.

| Check | Named proof |
| --- | --- |
| C1: old fixed-hour/int-limit policy and existing hashes remain compatible | `LinkedInPreparationTests.test_teto_semanal_da_campanha`, `test_fuso_mexico`, `test_mudanca_de_registro_invalida_lote` |
| C2: new policy normalizes without mutation; invalid windows/countries/types reject at Registry boundary | `LinkedInWindowTests.test_new_policy_validation_and_registry_roundtrip`, `test_invalid_new_policy_is_not_registered` |
| C3: 07:00 is included, 21:00 is excluded, eligible seconds/microseconds survive | `LinkedInWindowTests.test_window_boundaries_preserve_anchor_instant` |
| C4: explicit BR/MX date exclusions and weekends shift the proposal | `LinkedInWindowTests.test_explicit_dates_and_weekends_are_skipped` |
| C5: lead timezones resolve explicitly; unknown zone/country/calendar hold | `LinkedInWindowTests.test_unknown_lead_context_and_calendar_hold` |
| C6: DST gaps/folds produce a real instant no earlier than anchor | `LinkedInWindowTests.test_window_dst_gap_and_fold`, `LinkedInPreparationTests.test_dst_gap_is_skipped_and_fold_never_precedes_anchor` |
| C7: no internal ceiling at 200 or above and no required quota for null | `LinkedInWindowTests.test_no_internal_ceiling_or_prefixed_campaign_dependency` |
| C8: provider refusal/limit remains a hold without an internal ceiling | `LinkedInWindowTests.test_provider_block_remains_a_hold_without_internal_ceiling` |
| C9: preparation makes no durable change; execute refuses | `LinkedInWindowTests.test_preparation_is_read_only_and_executor_remains_off` |
| C10: new policy changes invalidate the existing configuration hash | `LinkedInWindowTests.test_new_policy_changes_invalidate_configuration_hash` |
| C11: a caller-created imaginary DST anchor is normalized to its real instant before checking the window | `LinkedInWindowTests.test_imaginary_anchor_is_checked_as_a_real_instant` |
| C12: provider refusal also holds a legacy fixed-hour policy | `LinkedInPreparationTests.test_provider_refusal_holds_legacy_policy` |

Validation/failure modes: C2/C5/C6/C8. Idempotency/data lifecycle: C9; nothing is
reserved or retained. Authorization: unchanged Registry human gate and execute
hard OFF. Concurrency/ordering: Registry reads stay in one transaction; C6/C9;
no capacity reservation is claimed. Dependency failures: C8, no transport.
State transitions/observability: named holds and explicit unverified calendar.
The principal owns integration and independent verification; no child agents.

### Calendar contract and remaining decision

`next_slot` remains a pure proposal function. It accepts the optional third
argument `lead_timezone` (IANA zone string) and returns an aware UTC datetime.
It skips Saturdays, Sundays and exactly the supplied `holidays` dates. It never
downloads or generates a national calendar. `prepare` accepts keyword-only
`lead_timezone` and `lead_country`; neither language nor a campaign prefix
supplies them. A fixed timezone in an existing policy remains explicit authority
for that old fixed-hour calculation.

For the new window shape, `prepare` returns `hold_reasons` and chooses the first
as `status`: provider refusal, missing/invalid lead timezone, unknown/unsupported
country, unverified calendar, then any configured integer-limit quota hold.
It omits `due_at` if the timezone is unknown. With a known zone, `due_at` is an
unverified proposal, even when the date exclusions list is nonempty.
`holiday_calendar_verified=false` always accompanies this new shape.
No input in this patch can certify a complete calendar or remove that hold.

Recommended follow-up contract, **not implemented or approved here**: a trusted
calendar input records explicit excluded dates, covered countries, inclusive
coverage dates, national/regional scope, source references and a revision/hash.
The principal must resolve with Ygor whether BR/MX exclusions are a union for
every lead or only the verified lead country's calendar, and which regional
holidays count. The current single `holidays` list is applied literally; it
cannot express that selection or prove completeness. Until those decisions and
coverage evidence exist, keep `calendar_unverified`, including for `holidays=[]`.
This is a missing data/contract decision, not a claim that BR/MX holidays were
fully applied.

Provider diagnostic keys are caller-supplied normalized evidence only, not a new
ConnectSafely response parser. No provider transport or live reader was added;
`quota_live_verified` remains false. Existing integer ceilings still require the
same fresh, complete, account-wide evidence for the proposed local week. Null
ceilings set `internal_limit_applied=false` and `quota_required=false`, independent
of campaign name or usage of 200/20000. Provider refusals remain holds even with
otherwise fresh quota evidence. None of these states reserves capacity.

### Local validation

- Baseline: 8 existing preparation tests passed before edits.
- Regression demonstrated: a Cairo imaginary DST anchor originally proposed
  01:30 outside a 00:00-01:00 window; UTC normalization now selects the next real
  eligible business slot. Existing fold and gap tests remain intact.
- Final focused file: 20 tests (8 existing and 12 added).
- Combined isolated command: `python3 -B -m unittest
  test_thunder_os_linkedin_preparation test_thunder_os_campaign_registry
  test_thunder_os_connectsafely_client test_thunder_os_connectsafely_transport -q`
  from `_ferramentas/`: 70 tests passed on Python 3.14.3.
- Tests use temporary SQLite databases, synthetic dates and provider doubles;
  the Registry roundtrip/hash checks use the actual local implementation.
  No real provider quota, external operation, staging/VPS or production was
  verified in this update. Independent verification remains with the principal.
- `python3 -B scripts/check_source_boundary.py`: passed, 211 allowed paths in the
  shared worktree at the final check (210 before concurrent ingress work).
  `git diff --check`: passed. No source or policy Registry outside the three
  assigned files was edited by this specialist.

## Previous fixed-hour preparation

Task 5 follows the master plan and this round's explicit prohibition on sends.
Existing OperationCampaign, exact-text confirmation, toques.py reservation and
receipt/reconciliation remain the only execution path. No old branch is merged,
no operational executor is installed, and MODO_SIMULACAO remains true in F3.

The bounded addition is optional `configuration.linkedin_policy` in the existing
CampaignRegistry: timezone, send_hour, weekly_invite_limit and holidays. The
existing configuration hash includes it, so a revision invalidates the previous
batch confirmation without another approval type. Existing configurations remain
valid and are not rewritten. No production campaign receives invented values.

`LinkedInPreparation.prepare` reads that Registry policy, proposes the next local
business slot and holds an invite at the configured weekly total. Quota input
must cover the entire LinkedIn account, including Chattie, identify the same
account/week and be complete and fresh. OS-only counts cannot prove availability.
The week belongs to the proposed local slot, not merely today's date; evidence
observed in another week is held, including a claimed future-week zero count.
UTC roundtrip rejects nonexistent local times; repeated hours are compared as
UTC instants, so preparation never returns a slot preceding the anchor.
The preparation is not a reservation or authority to send: concurrency, actual
provider budget/readback and the final existing approval gate remain necessary.

`execute` is hard OFF. The status endpoint is read-only. No flag in the legacy
global control activates this class. Before activation, Ygor must confirm the
policy and Chattie allocation, supply a qualified total-account counter and
authorize the real canary. Then wire this preparation into the existing durable
executor, qualify staging, and verify the receipt on its HubSpot task. This is
not delivered as a live executor and AGENTS.md must not claim otherwise.

Acceptance tests use synthetic policies (90/week, Mexico City 09:00, 16 Nov
excluded), including unknown quotas, changed Registry hashes and default OFF.
