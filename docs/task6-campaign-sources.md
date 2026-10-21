# Campaign Sources: Read And Prepare Only

Task 6 reuses `normalize_signal` / canonical catalog parsing and
`connection_intake.profile_key`. The 30-day task-specific job cutoff is stricter
than the shared catalog's 45/60 days; the catalog itself is not rewritten.
Engagers expire after 14 days and require an explicitly approved seed profile.
Recurrence is the number of distinct post URLs, an internal ranking tie-breaker,
not a purchase-intent score or a replacement for the catalog's weight.

Source evidence and copy context are separate: engagement/seed evidence cannot
become the opener of an outreach message. No copy or send is generated here.
ICP confirmation requires fresh canonical checks, B2B Brazil industry/distribution,
revenue above BRL10m and no current campaign, client, open deal or suppression.
The canonical check must identify the exact domain and explicitly include the
current campaign property; omitted association/identity never means unoccupied.
Unknown data remains held; a job or LinkedIn title cannot prove revenue or intent.

CSV uses the standard csv parser and a small canonical export contract (company,
domain, email, linkedin_url). Real collectors may map an Apollo export to this
contract; cells cannot declare approval, fit or authorization. ConnectSafely/Gupy
cards arrive through bounded read ports; these functions never discover credentials
or invent an undocumented HTTP endpoint. Missing collectors remain unconfigured.

Historical batches remain available as provenance, not current eligibility.
The view uses today's observations in update order: a newer exclusion removes
an older candidate, and partial current risk or conflicting profile/domain
evidence is processed before fit filtering. Unresolved risk holds the batch.
Public status/candidate views use the existing Repository only. No contacts,
tasks, campaign memberships, webhooks or provider settings are created. Automatic
source collection is not configured by this slice. Real reads and their coverage
are reported separately from synthetic acceptance tests. Twenty plus twenty
qualified candidates are not claimed without source and ICP evidence.
