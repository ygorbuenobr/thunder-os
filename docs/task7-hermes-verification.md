# Panel Verification And Scoreboard

This round enables only `verify`: authenticated Ygor asks about an exact contact
already observed in the HubSpot reader. AgentGateway validates a stateless
verification envelope; it does not enqueue AgentWorker work or mutate domain
state. The server supplies a small source packet from the selected contact,
including partial coverage. No other lead or full Brain transcript is uploaded.

Hermes uses the existing ygor-personal Codex OAuth configuration through the
existing isolated read-only dependency. One bounded child, tools=[], store=false,
no OAuth refresh, no model/provider fallback and no gateway/bot restart.
Question/source text is data, never executable instructions. Answer references
must belong to server-issued source IDs; links are assembled by the backend.
Failure is visibly unavailable, not a simulated Hermes success or automatic retry.
Exact canonical touch/channel values are preserved: E2 is not LinkedIn L2.
If a requested touch is absent, the server returns a source-only insufficiency
answer without calling the model, labelled `answer_origin=source_projection`.
This is a deterministic precheck, not a model fallback or inferred no-send.
Multiple explicit touch codes are treated as ambiguous: ask one at a time,
without a model call. Mentioning L2 cannot bypass an absent-E2 precheck.
The reader also accepts the observed `[campaign][YGOR|LOA][touch]` format;
the optional actor label never establishes contact, owner or campaign authority.
The original two-bracket and sequence/pipe formats retain their identity rules.

Change-route and send remain OFF in the backend. Existing OperationCampaign and
exact-text approval still govern any separately authorized future integration;
this read path cannot grant them. No CRM note, lead pause, campaign config or
Mexico executor installation is performed. Query scratch is task-owned and
ephemeral under the existing runtime directory, not a new database.

Scoreboard keeps unknown metrics null. Learning proposals use only confirmed
classified human replies of the same campaign, no auto-replies or inferred
interest. The existing LearningEngine criterion is an association diagnostic,
not a market benchmark; promotion continues to require Ygor's explicit decision.
