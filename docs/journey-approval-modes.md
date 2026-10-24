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
