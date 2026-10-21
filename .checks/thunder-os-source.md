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

## Tarefa 3 antecipada: leitor por campanha atual (2026-10-21)

Profile: light. Continues the master v2 plan, Task 3, and its existing cadence
checks. Binding clarification: Ygor explicitly selects commercial attribution by
associated contact ID + exact `campanha_ativa` + Registry platform `apollo` or
`instantly`, labelled `contact_current_campaign`. Event GUID/source IDs and
membership history are not attribution prerequisites. Base: clean `8cf7f1aa`.
Ownership: cadence_readonly.py, its focused tests, README and this checklist;
source boundary allowlist only for the new focused test. No other writer owner.

Landing: email search -> complete email/contact ID associations -> contact batch
read -> current campaign -> validated Registry. Reuse bounded pagination, budgets,
identity checks and cache. Historical emails may follow the current commercial
association; no enrollment timestamp or historical membership is inferred.

- T3-C1: exact current campaign with a registered Apollo/Instantly platform attributes
  CRM email once; old GUID/source bindings cannot assign it.
  Proof: `test_thunder_os_cadence_contact_campaign.ContactCampaignTests.test_current_campaign_exact_registry_not_event_fields`.
- T3-C2: missing/unknown/multiple campaign values or conflicting associated campaigns
  yield no attribution and unknown totals, with observable reasons.
  Proof: `ContactCampaignTests.test_missing_unknown_multiple_and_conflicting_contacts`.
  Additive clarification, review round 1 (confirmed P2, Ygor's explicit contract):
  "missing" means omitted contact field, absent schema, or unavailable data. A
  successfully read field explicitly equal to JSON null or the empty string proves
  no current campaign; a single exact active Registry campaign on a platform other
  than Apollo/Instantly proves that email is outside this reader's campaign scope.
  With complete contact IDs/reads, these two exclusions remain visible but do not
  mark the global external-email coverage partial or invalidate other campaigns'
  totals, even when that excluded email lacks classification metadata. Unknown,
  unregistered, inactive, malformed, conflicting or omitted values remain unknown;
  no trimming, autofill, campaign alias or implicit zero is introduced.
  Proof: `ContactCampaignTests.test_control_explicit_empty_other_platform_and_omitted`
  and `ContactCampaignTests.test_excluded_email_classification_does_not_degrade_totals`,
  both exercised for Apollo and Instantly. The original None/empty fixtures are
  updated to this explicit superseding decision, not silently dropped.
- T3-C3: paginated emails and contact associations are bounded; incomplete pages,
  schema, batches and budgets retain null totals and separate observed counts.
  Proof: `ContactCampaignTests.test_pagination_and_partial_coverage` and
  `ContactCampaignTests.test_contact_schema_batch_and_budget_failures`.
- T3-C4: CRM SENT is separate from delivery; incoming email does not populate
  campaign replies or meetings, and provenance identifiers do not become authority.
  Proof: `ContactCampaignTests.test_recorded_sent_not_delivery_or_reply_inference`.
- T3-C5: only a proved contact ID is admitted to a commercial timeline; an email
  linked to multiple contacts cannot leak through another contact's timeline.
  Proof: `ContactCampaignTests.test_email_only_timeline_and_contact_isolation`.
- T3-C6: executor/task campaign conflicts are visible in a pure read projection.
  No admission/release/CRM mutation is claimed.
  Proof: `ContactCampaignTests.test_displays_conflict`.

Run proofs with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas python3 -m unittest
test_thunder_os_cadence_contact_campaign test_thunder_os_cadence_readonly
test_thunder_os_cadence_evidence`. Synthetic fixtures and transport only.

Swept: validation C1/C2; partial failures/budgets C3; deduplication C1; authorization
uses existing adapter identity/read allowlist; concurrency uses existing cache lock;
no persistence/state transitions; dependency errors C3; observable limitations C2-C4.
No new DB, ledger, fence, provider calls, schema or CRM writes. No commit/push/deploy.

Full Task 3 remaining contract: authoritative contact admission/release writer,
executor value mapping, concurrent competing registrations and audit note when
marking replied. Existing ReplyCoordinator stops cadence state but does not clear
HubSpot campaign/executor or append that note. `test_lead_ocupado_bloqueia` and
`test_resposta_libera` remain unimplemented until that writer/contract is assigned;
a read-only test cannot prove admission blocking or release. Independent review
belongs to the principal after this diff, not this author.

### Evidence and handoff

- Environment confirmed: Mac / Darwin / ygorbueno, worktree
  `/Users/ygorbueno/thunder/.worktrees/thunder-os-cadencias`, branch
  `codex/cadencias-readonly`, clean baseline and unchanged HEAD
  `8cf7f1aa07f85b44977ccbe0c673ef5bf81ad4db`. Python 3.14.3.
- Baseline: 58 cadence reader/evidence tests passed before the patch.
- Pre-review local validation: 96 tests passed in the reader, evidence, Registry and
  panel-route suites. Command:
  `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=_ferramentas:.scratch/dependencies python3 -m unittest test_thunder_os_cadence_contact_campaign test_thunder_os_cadence_readonly test_thunder_os_cadence_evidence test_painel_cadencias test_thunder_os_campaign_registry`.
- Panel test import first failed with missing `toques`; rerun uses the pre-existing
  `.scratch/dependencies/toques.py` for import only. Panel tests use temporary state,
  fake transports/server and disabled external effects. No production DB/provider.
- `python3 scripts/check_source_boundary.py`: 183 allowed tracked/unignored paths.
  `git diff --check`: exit 0. The new test is untracked and must be included in review.
- C1-C6 have local synthetic proofs. The server constructor is exercised through the
  existing `test_main_wires_existing_repository_registry_into_read_console` with a
  temporary real Registry; its state and effects remain unchanged by the reader.
- API response gains `contact_memberships`, `contact_conflicts`,
  `unattributed_activities` and the attribution/evidence fields documented in README.
  That pre-review version had no HTML changes; visibility of contact-conflict
  diagnostics in the browser is not claimed. Round 2 consumer labels are below.
- No independent verifier, real HubSpot API read, schema mutation, autofill,
  admission block/release, live UI/browser journey, source commit, push or deploy here.
  The principal owns independent review and the next user-approved publication.

### Review round 1: confirmed P2 coverage regression

The principal/Tesla review reproduced an Apollo total of 1 becoming null when an
unrelated email had an explicitly empty campaign or a registered HubSpot sequence
campaign. The reader had conflated out-of-scope records with unknown membership.
The additive T3-C2 clarification above was recorded before changing the reader.
The new regression proofs failed in 13 subcases before the fix, including both
platforms and excluded emails lacking classification metadata.

The bounded fix distinguishes explicit null/empty from an omitted field, excludes
proved other-platform campaigns without degrading global external-email coverage,
and retains unknown/conflicting/incomplete membership as partial. Existing read
limits and commercial current-campaign attribution are unchanged. Only the reader,
focused test, README and this checklist changed in this review round.

Round 1 validation: 99 tests passed with the full command above; source boundary
and diff whitespace checks passed. This is local synthetic evidence, not a live
read or a new independent verification. Task 3 admission/release remains pending.
The frozen full candidate diff and file hashes are generated under `.scratch/`
for Tesla; no commit, publication, writer, schema or live-state change is authorized.

### Review round 2: observed contacts and consumer evidence labels

Source: Ygor's explicit round 2 request and Tesla's two confirmed P2 findings.
The email-only contact was absent from metrics.contacts; the consumer also rendered
the Registry platform as a proved send origin. This is an additive refinement of
C1/C3/C4/C5, recorded before changing code. All earlier checks remain binding.

- T3-C7: for registered Apollo/Instantly groups, contacts/observed_contacts count
  DISTINCT proved contact IDs across tasks and attributed emails. Repeated emails
  and IDs present in both sources count once. These are observed CRM contacts, not
  provider enrollment counts. Totals stay null if task or email coverage is unknown;
  the distinct proved observations remain separately available, including failures.
  Proof: `ContactCampaignTests.test_contacts_union_distinct_tasks_and_attributed_emails`
  and `ContactCampaignTests.test_contacts_union_preserves_unknown_coverage`.
- T3-C8: only current-contact attribution renders "Envio registrado · campanha
  Apollo/Instantly", with "Campanha atual do contato; origem e entrega não
  verificadas." Legacy provider evidence keeps its previous label. The heading is
  "Contatos observados"; no redesign or new interaction is introduced.
  Proof: `_ferramentas/test_painel_cadencias_ui.cjs`, mocked Playwright at 1440/390,
  plus rendered screenshots inspected by this author. This does not prove live UI.

Additional write ownership is restricted to painel_modelo.html cadReadEvidence and
the contacts heading, and the existing UI harness fixtures/assertions/output paths.
Apply installed frontend-ui-ux skill; use the existing bundled Node/Playwright with
NODE_PATH, no install. Screenshots go under `.scratch/task3-reader-round2-ui/`,
preserving previous evidence. No writer, admission/release, production or publication.

Round 2 results: the two new backend tests reproduced six failing subcases before
the fix. The final Python command above passed 101 tests. The existing offline
Playwright harness passed at 1440x1000 and 390x844 with bundled Node v24.19.0 and
Playwright 1.62.1, using the NODE_PATH and command in README. It asserts current
Apollo/Instantly copy, the legacy label, distinct observed contact display, escaping,
keyboard-operated details, no page overflow, no page errors and GET-only requests.
Both current-contact screenshots were visually inspected; the new labels/notes
wrap in their existing containers. No CSS/layout redesign or new UI action.

`python3 scripts/check_source_boundary.py`: 183 allowed paths. `git diff --check`:
exit 0. Full source freeze: `.scratch/task3-reader-round2-frozen.diff`; file/screenshot
hash manifest: `.scratch/task3-reader-round2-files.sha256`; captured command output:
`.scratch/task3-reader-round2-validation.txt`. The previous round 1 freeze and
screenshots remain preserved. Main/Tesla owns independent review; no send, writer,
live API call, schema mutation, commit, push, publication or deployment in this round.
