# Journey review and provider cadence boundaries

The existing Apollo sequence, Instantly sequences and Thunder account cadences
are separate paths. This change does not convert or enroll between them.
Nova jornada's existing manual-export wizard targets Apollo only.

## Apollo prompt approval

Select the existing Apollo sequence and use “Usar prompts da sequência atual”.
The panel reads the complete sequence, owner/team identity, templates, original
prompts, instructions, variable tokens, fallbacks and recipient overrides. The
existing draft approvals bind their semantic SHA-256, company/campaign sheet,
cohort and checks, for 24 hours. An input change requires another approval.
This is not approval of a final generated email. It does not authorize dispatch
in the Thunder worker. Existing exact-text approvals remain exact-text approvals.

Before the canary/first ten sends, Ygor sets only E1 to Manual email in Apollo:
Sequences → Ygor's Outbound AI Sequence 1 → Steps → step 1 ⋯ → Change step type
→ Manual email → confirm. Do not alter the existing prompts or variables.
Source: https://knowledge.apollo.io/hc/en-us/articles/4409231193101-Create-a-Sequence
Re-read the selected step after this change; it changes the input hash.

Sent-email audit is advisory. Only observed Apollo provider receipts establish
provider identity. Missing original body/proofs/date remains unverified. The
audit highlights em dashes, forbidden phrases and numeric tokens absent from
the campaign's proofs; it cannot establish factual correctness or undo a send.

## Grouped company qualification

One company review displays source/date and current/proposed values. The human
confirms ICP, not-customer and not-excluded together, then explicitly confirms
the exact preview. The existing preview/attempt journal and identity guard
permit one PATCH of the linked existing company and require receipt/readback.
Opt-out, suppression and risk are read-only. Contact, owner, stage, deal and
campaign are unchanged. A timeout cannot be retried blindly; reconcile reads.
Verified readback stores the review in the existing draft and invalidates old
checks/approvals. A fresh check is required before export.

## HubSpot read budget and incompleteness

Participating panel readers share process-local pacing and 429 cooldown for
portal 51412240. The Cadence visibility reader caches schemas for at most 60s;
critical eligibility and write preparation always re-read schemas. New caches
never retain records, associations, deals or memberships. Source pagination
must be complete to prove absence. A 429 or partial read does not clear checks.
Other processes are outside this volatile budget; a remote 429 remains possible.

## CRM registered-state semantics

The refusal check interprets these four read-only fields under the approved
registered-state policy:

| Observation field | HubSpot object/property | Expected current schema | Explicit clear value |
| --- | --- | --- | --- |
| `person_opt_out` | contact `opt_out` | `bool` | `false` |
| `email_opt_out` | contact `hs_email_optout` | `bool` | `false` |
| `company_suppression` | company `thunder_suppressed` | `bool` | `false` |
| `outreach_risk` | contact `thunder_outreach_risk` | enumeration including `low`, `medium`, `high`, `blocked` | `low` or `medium` |

A present `null` or empty string means no value registered in that CRM field
only when its freshly read schema is complete, valid and nonconflicting, the
linked record read is complete, and the requested property key is present.
It is not an explicit `false` or `low`. A missing schema, invalid type/options,
partial read, or omitted value key stays unknown. The current reader also
discards malformed values, so an omitted key cannot safely be interpreted as
empty by the composer. There is no new HubSpot write or schema provisioning.

`check.observation.crm_no_record_fields` lists the four observation-field names
that met the empty-value rule. `crm_coverage` maps each name to `status`
(`none_registered`, `explicit_clear`, or `unknown`), `schema_valid`,
`property_present`, `record_complete`, and the original `property_coverage`.
The reader's strict schema/value flag remains false for empty values;
`crm_pending_fields` continues to expose unresolved fields. Positive opt-out,
suppression, high/blocked risk and linked replies keep the aggregate blocked,
even if another CRM field is empty or an additional read fails.

The existing aggregate reason `canonical_registered_refusals_clear` can pass
only after all four CRM fields are explicitly clear or have valid registered
absence, the registered ledger covers every verified email/profile completely,
and local identity/reply/cadence reads are complete with no linked blocker.
The pass remains scoped to registered HubSpot/toques/local state;
`coverage.global_inbox_complete` is false. It does not assert global absence of
refusal, grant consent, clear unknown inbox history, or replace send preflight.

HubSpot defines native `hs_email_optout` as the property for unsubscribing from
all marketing email. True blocks this check. False or blank does not establish
consent for each subscription type or brand, eligibility for an individual
message, or absence of refusal in other channels. The custom-field empty-value
interpretation above is the approved Thunder policy, not a HubSpot guarantee.

Primary sources consulted for S40 (2026-10-26):

- [HubSpot marketing email subscription status](https://knowledge.hubspot.com/marketing-email/learn-a-contact-s-marketing-email-subscription-status): native true/false/blank semantics and separate subscription/brand statuses.
- [HubSpot Contacts API v3](https://developers.hubspot.com/docs/api-reference/legacy/crm/objects/contacts/guide): requested undefined properties are omitted; defined properties without a value return `null`.
- [HubSpot custom properties](https://knowledge.hubspot.com/properties/create-and-edit-properties): custom properties initially contain null values unless a default was configured.

## Export and operation

The preview is read-only in the draft and does not download or enroll. Export
records the existing internal cohort and creates CSV only after all six checks
and applicable approvals pass. Prompt mode labels templates as templates and
includes the approved input hash. There is no new database or approval layer.
Agent and briefing activation, sends and Apollo enrollment remain separate.

## Reproducible synthetic verification

Use pinned toques.py from runtime/dependencies.json. Unit fixtures block provider
network access. scripts/test_hubspot_readers.py isolates the volatile budget and
clock per test, preserving the real budget and provider fixtures. Dedicated
budget tests assert shared pacing, concurrent schema cache and fresh critical
schema reads. Browser fixtures do not prove a real CRM write or provider send.

## Commercial history and preparation reads

`GET /api/os/v1/new-journey/{draft}/history/{source_key}` is restricted to the
existing authenticated Ygor actor. The key must belong to that draft and its
saved company/contact binding. Before returning originals, the reader verifies
the current HubSpot identity again. Divergence or a missing link does not expose
another person's records. It reads associated CRM activities directly, without
requiring cadence membership or fetching all tasks.

Gmail refreshes only exact, hash-checked known threads whose sender is a verified
contact alias. The current bridge has no recipient search, so discovery remains
partial even when a known thread is complete. A latest observed outbound is not
proof that no later reply exists in another thread. CRM activities and thread
context are not provider delivery receipts. The route performs no state write.

`GET /api/os/v1/campaigns/{campaign}/preparation` shows existing Apollo draft
prompt inputs, the pinned Instantly campaign's original sequence, and Thunder
account cadences explicitly bound to that campaign. It does not assign visitor
account files by name. The Instantly adapter accepts only its pinned workspace
and campaign GETs; static originals and unresolved dynamic content remain
separate. The result grants neither approval nor enrollment authority.

## Management and evidence limits

The Jornada's optional management panel consumes the existing campaign
scoreboard. Receipt-backed reach and human replies are deduplicated by verified
identity. Partial coverage keeps total values unknown and any available observed
count separate. Meetings, opportunities, wins and cost require their own source;
a reply or completed task is not an outcome. Different audiences, periods and
offers do not establish a winning platform. Suggestions remain proposals for
human review and do not update campaign rules, the Brain or provider settings.

Reproduce the browser contract without external network using
`scripts/test_gtm_commercial_panel.cjs` with Playwright from the configured Node
runtime. Fixtures cover 320/390/1440px, originals, source escaping, partial data,
request races, safe links and GET-only history/preparation/management reads.

## Publication readback by resource

Every panel publication records a before/after observation from the running
process, not only THUNDER_EXTERNAL_EFFECTS_ENABLED. Run
`scripts/read_panel_resource_gates.py --expected-sha <published commit>` locally
on srv1433640 as root (the source may be supplied over SSH stdin). Keep the JSON
with the existing release evidence. It is an observation, not permission to act
or a new approval mechanism.

Record separately: pinned ConnectSafely configuration `execution_enabled`, the
four Gmail S34 switches (cadence, CRM, reconcile, reply ingest), assisted dispatch,
its policy and temporary drop-in, the explicit ledger writer, and PGOPTIONS.
An unset configuration value is distinct from explicit false. Where the product
requires literal true, the readback can show effective disabled while retaining
that missing-value distinction. Invalid/unreadable values remain unknown.

A globally enabled flag or an enabled ledger writer does not prove any resource
can dispatch. Conversely, an enabled ConnectSafely configuration must be reported
as enabled, even while agent and Gmail/assisted execution are disabled. Do not
flip unrelated resources to make the readback look uniform. Revalidate source,
PID/cwd, pins and agent/briefing pauses independently after publication.


## Internal canary: operator sent-copy observation (S40)

The explicitly authorized internal canary has one administrative reconciliation
path for its known `RPC_DISPATCH_EXPIRED` failure before the Gmail tool. The
operator supplies a typed, fresh attestation bound to the original job, packet,
ticket, grant and consumed host-marker hash, plus the exact-address/subject
Gmail search. This trusted administrative method is not an HTTP endpoint; it
cannot accept a prospect, generic timeout, missing marker or changed envelope.
The consumed marker and historical journal remain intact.

`reconcile_proven_not_sent` records terminal `failed/proved_not_sent` and an
audit event in the existing repository. It never releases a reservation, queues
a retry, renews the grant, or creates another identity. Its 24-hour observation
window permits only readback of a later operator sent copy of the exact
approved internal envelope; it is not dispatch permission. Confirmation reads
Gmail again, records `provider_sent_copy` and `operator_reported`, and preserves
the old failure audit. Ordinary uncertain-receipt contracts remain unchanged.

For this confirmed receipt only, `manual_reply_preview/confirm` reuse the
existing human-message review and ReplyControl for the same thread, person and
cadence. They write only internal reply/stop state. Global S34 flags, automatic
dispatch, CRM logging and external pauses stay off. The existing Jornada can
show this explicitly selected **Teste interno** and its original texts; the
canary is excluded from default Brazil lists and commercial metrics.

No actual sent copy or reply is claimed before a provider read and internal
persistence/readback. The user sends the manual email, then replies to it.
