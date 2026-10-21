# LinkedIn Preparation, Execution Off

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
