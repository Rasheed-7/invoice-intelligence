# Architecture

## Untrusted document text

Text extracted from uploaded documents is stored verbatim in the database and
is never interpreted or executed by the extraction layer. Prompt-injection
defense for the LLM extraction step belongs to the LLM provider layer, which
must treat all document text strictly as data rather than instructions.

## Extraction pipeline layering

### Schema separation

The extraction layer maintains two distinct schema types:

1. **Extraction schemas** (`app/schemas/extraction.py`) — Internal data
   structures that model the LLM provider's output. These include confidence
   scores, source attribution, and nested structures like line items. They are
   optimized for validation, transformation, and persistence.

2. **HTTP response schemas** (`app/schemas/invoice.py`) — Public API contracts
   that define what clients receive. These are flattened, simplified views of
   the domain model, excluding internal metadata like confidence scores and
   audit trails.

This separation ensures that the internal extraction format can evolve (to add
confidence thresholds, new validation rules, or alternative provider outputs)
without breaking the public API.

### The adapter layer

The `extraction_from_llm_response` function in `app/schemas/extraction.py`
is the single point where raw LLM provider output is interpreted and converted
into the internal `InvoiceExtraction` schema. All provider-specific quirks
(field naming, structure, serialization) are handled here. This design keeps
provider implementations focused on communication, not data interpretation.

### Endpoint distinction: /extract vs /analyze

- **POST /api/v1/invoices/{id}/extract** — Text extraction only. Runs PyMuPDF
  (or the appropriate document processor) to populate `document_pages` with raw
  text. Updates `document_processing_status` to `text_extracted`. Idempotent:
  re-running replaces existing pages.

- **POST /api/v1/invoices/{id}/analyze** — Full orchestration. Ensures text
  is extracted (calls `/extract` internally if needed), then invokes the LLM
  provider to extract structured fields. Validates the extraction schema,
  persists fields and line items to the invoice, updates status to `extracted`,
  and writes an audit event. Idempotent: re-running replaces line items and
  updates all fields.

Both endpoints are idempotent by design. Re-extraction is safe and useful for
testing, fixture generation, and recovery from provider failures.

## Deterministic validation layer

Validation is a separate layer from extraction because the two jobs demand
different tools. Extraction reads unstructured input — it needs an LLM to
interpret human language. Validation checks arithmetic — `quantity ×
unit_price == line_total` and friends — which must never go through an LLM:
a language model approximates and occasionally hallucinates, while Python's
`Decimal` arithmetic on persisted values is exact, deterministic, and testable.
LLMs read; Python does arithmetic.

### Passed vs skipped

Every check returns exactly one of `passed` / `failed` / `warning` /
`skipped`. `skipped` is a first-class outcome, not silence: it means the
check could not run because an input was missing, and the result always
carries the reason (e.g. "missing required fields"). This keeps the no-fabrication
guarantee the extraction layer makes — a check that cannot run says so
explicitly instead of guessing pass or fail.

### Status transitions

The persisted report's overall outcome drives the invoice lifecycle:

- `passed` → `validated`
- `failed` or `warning` → `needs_review`
- `skipped` → unchanged (nothing meaningful was validated, so the invoice
  stays in its prior state)
- a terminal status (`approved`, `rejected`, or `failed`) → unchanged,
  because re-running validation must never downgrade a decision a human or
  upstream process already made

### Why re-validation replaces prior results

Each run of `POST /validate` deletes the invoice's existing
`calculation_check` `AnalysisResult` rows and writes a fresh set, rather than
appending. Validation results describe the *current* persisted invoice state —
line items, totals, dates — so stale rows from a previous extraction would
contradict the fresh ones and make the audit trail unreadable. Replacement
also keeps the per-invoice row count constant (one row per check), which is
what makes re-running safe to repeat.

## Duplicate and anomaly detection

Two independent detectors feed the risk model. Each persists its own
`AnalysisResult` row and can be re-run on its own; neither disturbs the
other's rows.

### Why duplicate signals are categorized (definitive vs corroborating)

Duplicate detection sums per-signal weights into a pair score (capped at 1.0),
then maps the score to a verdict: `>= 0.95` is `DUPLICATE`, `>= 0.6` is
`SUSPECTED_DUPLICATE`, anything lower is `UNIQUE`.

Two signals are **definitive** — each settles the verdict on its own:

- `identical_file` (weight 1.0): the same bytes were uploaded twice. It clears
  the duplicate threshold by score alone.
- `same_vendor_invoice_number` (weight 0.75, floored at 0.75): no vendor
  legitimately issues two invoices under one number, so it forces `DUPLICATE`
  by verdict override even though its weight alone would only reach
  `SUSPECTED`.

The remaining signals are **corroborating** — additive but capped so weak
evidence cannot convict on its own:

- `same_po_amount` (0.5), `same_vendor_amount` (0.4), `line_item_overlap`
  (0.35). The duplicate threshold (0.95) sits above the sum of the two
  mid-weight signals (0.5 + 0.4 = 0.9), so corroborating evidence lands in
  `SUSPECTED` rather than `DUPLICATE`. Only a confirmed `DUPLICATE` routes an
  invoice to `NEEDS_REVIEW`; a suspected one is surfaced without gating the
  pipeline.

### Why Isolation Forest starts at 20 (statistical power vs data)

Anomaly detection always runs the statistical baseline first: it works on
small samples (5+ prior invoices) and can say *which* feature deviated and by
how much (per-feature z-scores). Isolation Forest activates only at 20+ prior
invoices because its multivariate model needs a real sample before it means
anything — fitting 100 trees on a handful of points would produce noise
dressed as signal.

### Why raw_decision is exposed and score is None for IF

`decision_function` is a relative, corpus-dependent ranking value — not a
probability. Measured on clustered invoices, a 40x amount outlier landed at
-0.094 against an inlier's +0.017: the ordering was reliable, but rescaling
into 0..1 produced a number that looked meaningful while being arbitrary.
Thresholds therefore operate on the raw decision value (`< -0.15` is strong,
`< -0.05` is mild), `score` stays `None`, and the UI reports the decision
value as-is with the note "Lower = more anomalous". No calibrated probability
is claimed.

The forest also cannot attribute: it scores an invoice as a whole. The report
therefore pairs the forest's verdict with attribution borrowed from the
statistical baseline (clearly labelled), and says so explicitly rather than
inventing reasons. Below 5 prior invoices the system returns
`INSUFFICIENT_HISTORY` — never a fabricated score.

### The composition rule: final verdict = max severity across both detectors

When both methods run, the verdict is the more severe of the two rather than
the forest's alone. The forest is blind to signals the baseline can see (such
as a burst of recent submissions); letting it silently override a stronger
baseline finding would discard real evidence. Severity ordering is
`INSUFFICIENT_HISTORY < NORMAL < MILD_ANOMALY < STRONG_ANOMALY` — the absence
of evidence never outranks a positive finding. Only `STRONG_ANOMALY` routes an
invoice to `NEEDS_REVIEW`; a mild one is a warning, not a red flag.

## Risk engine

The risk engine is the last stage: it reads the rows every earlier stage wrote
and combines them into one 0–100 score with per-finding attribution. It is
split across three pieces, and the split is the point:

- `app/services/risk/engine.py` — **pure**. No SQLAlchemy, no FastAPI, no
  `AnalysisResult` reads, no I/O of any kind. `assess_risk(inputs, id)` maps an
  `AnalysisInputs` snapshot to a `RiskAssessment` and nothing else. The same
  inputs always produce a byte-identical output, which is what makes the score
  reproducible and auditable.
- `app/services/risk_service.py` — the only place the two meet.
  `build_analysis_inputs` reads prior `AnalysisResult` rows and folds them into
  the snapshot; `run_risk_assessment` calls the engine and writes the result
  back. This file contains **no scoring logic at all** — no weights, no
  thresholds, no verdicts of its own. Every point in the score originates in a
  verdict some earlier stage already produced.
- `app/services/risk/weights.py` — the constants, in one place so there is
  exactly one file to audit when asking "why does this weigh what it weighs".

### The contributor model: per-finding cap + sum + global cap

Each finding category contributes a bounded number of points — its
per-contributor cap. The contributions are summed, and the sum is clamped to
`MAX_SCORE = 100`.

The per-category caps exist so no single severe finding can dominate. A
confirmed duplicate is capped at 35, above everything else, because the same
economic claim submitted twice is the strongest available signal; but it can
never exceed 35 no matter how many independent duplicate signals support it.
The global cap keeps the output on a fixed 0–100 scale no matter how many
categories fire at once, so the number stays comparable across invoices.

Capping is auditable rather than silent: the uncapped sum is preserved on the
assessment as `contributing_total`, and `caps_applied` records whether the
clamp fired. A reviewer seeing a score of 100 can therefore see that the raw
total was 145 and understand why the number stopped there.

The weights are engineering judgment, not empirically calibrated, and every
assessment says so in its `notes`. Calibrating them would require a labeled
dataset of historically-risk-scored invoices, which does not exist.

### Why `insufficient_history` does not contribute

Anomaly detection returns `INSUFFICIENT_HISTORY` when a vendor has too little
prior history to compare against. The risk engine assigns that verdict **zero
points**, and the rule generalizes: a stage that never ran, or that ran and
could not form an opinion, is recorded as `None` and contributes nothing.

Absence of evidence is not evidence of absence. Treating "we could not tell" as
"we found a problem" would systematically penalize new vendors — exactly the
population least likely to have history — and would make the score measure data
volume rather than risk. The same reasoning governs the policy check, where
three states are kept distinct: evidence found, check ran and found nothing,
and check never ran.

### Why the score is a fraction in the DB but 0–100 in the API

`AnalysisResult.score` is `Numeric(5, 4)`, so a risk score of 90 is persisted
as `0.9000`. This is not a second model of the risk — it is the same number on
a scale the column can hold to four decimals of precision, matching every other
`AnalysisResult` that stores a normalized score.

`details`, the JSONB blob holding the full `RiskAssessment`, keeps the 0–100
integer. The API and frontend read `details.score` and never touch the column,
so there is exactly one representation in the payload a client sees. `GET
/invoices/{id}/risk` returns `details` unchanged for the same reason: exposing
both forms in one response would leave a caller to guess which one is
authoritative.

### Status transitions: only reject and high can move an invoice

`run_risk_assessment` writes the score, an audit event, and — conditionally —
a status change:

- `recommended_action == reject` → `rejected`. This is the **only** analysis
  stage permitted to set a terminal status. A reviewer can still override it,
  but nothing else in the pipeline will second-guess the decision.
- `level == high` without a reject recommendation → `needs_review`. High risk
  reaches a human; it does not close the invoice out on its own.
- anything else, including `level == medium` → **unchanged**. Medium risk is a
  warning, not a red flag. Scoring is advisory, and a model that stalled every
  partially-suspicious invoice would bury the genuinely bad ones.
- a terminal status (`approved`, `rejected`, `failed`) → **unchanged**, always.
  Re-scoring records a new number but must never reopen a decision a human or
  an upstream process already made.

### The consistency guard is dead code on purpose

`assess_risk` computes `level` and `recommended_action` independently, then
applies two overrides: high risk can never auto-approve, and a low score can
never reject.

Given the current constants **neither override can fire.** `RISK_LEVEL_HIGH_MIN`
is 60 and `AUTO_APPROVE_MAX` is 20, so a high-risk score is always above the
auto-approve ceiling; `RISK_LEVEL_MEDIUM_MIN` is 25 and `REJECT_MIN` is 85, so a
low score is always below the reject floor. The guards are unreachable today.

They are kept anyway. A future weight change that reorders those thresholds
would otherwise produce output that contradicts itself — an invoice described
as high risk and cleared for automatic payment in the same response — and the
failure would be silent, because both halves would still be individually
correct. The guard converts a subtle inconsistency into a visible `review`.

## Review workflow

A review case is the hand-off from the automatic pipeline to a person. The risk
engine owns the decision to *request* review; a human owns the decision about
the invoice.

`run_risk_assessment` opens a case when the assessment is `high` or the
recommendation is `reject`, in the same transaction as the score — so the score,
the audit events, and the case land or fail together, and a reviewer is never
looking at work with no recorded cause. `medium` deliberately does **not** open
a case: a warning is not a red flag, and a model that interrupted a human for
every partially-suspicious invoice would bury the genuinely bad ones.

At most one case exists per invoice (`invoice_id` is uniquely constrained), and
`create_review_case` is idempotent, so re-running `/assess-risk` converges on a
single case rather than accumulating duplicate work items.

A resolved case records which human decided, what they decided, and when
(`resolved_at`). `dismiss` is deliberately distinct from `approve`/`reject`: it
closes the case as "no action needed" and leaves the invoice's own status alone,
because it is a statement about the *case*, not about the invoice.

### Terminal statuses are honored

The risk engine can set an invoice to REJECTED. That status is terminal:
every subsequent service, including a reviewer's decision, refuses to change
it. A reviewer who approves a terminally-rejected invoice closes the case
with their decision recorded in the audit trail, but the invoice stays
rejected.

This is a deliberate MVP policy: "can a human override a system terminal
status?" is a product decision that deserves explicit buy-in, not a silent
slip-in. The UI surfaces both states rather than picking one.

## Soft delete

`DELETE /api/v1/invoices/{id}` soft-deletes an invoice: one column
(`invoices.deleted_at`, timezone-aware and indexed) plus one audit event.
Nothing else is touched. The `Document` row survives because the same bytes
may back other invoices (storage is content-addressed by SHA-256), and the
`AnalysisResult`, `AuditEvent`, `LineItem` and `ReviewCase` rows survive
because erasing them would erase the history an auditor is entitled to see.

Read paths all filter on `deleted_at IS NULL`:

- `GET /invoices` omits deleted rows from both the page and the count.
- `get_invoice` filters them out, which every downstream analysis service
  (`validation`, `po_match`, `policy`, `duplicates`, `anomalies`, `risk`)
  loads through — so a deleted invoice is "not found" to all of them with no
  per-service code. Direct candidate queries (duplicate candidates, anomaly
  history, the duplicate-invoice-number validation check) carry the same
  predicate explicitly.
- Re-uploading identical bytes creates a *new* invoice; deletion never
  resurrects or blocks a future upload.

**Why 410 Gone, not 404.** `GET /invoices/{id}` distinguishes the two
failure modes: an id that never existed is `404 Not Found`, an id that
existed and was deleted is `410 Gone` with `detail: "invoice deleted"`.
Calling a real resource "not found" would send clients hunting for typos
that aren't there, and 410 is the status whose entire meaning is "this
resource existed and is permanently gone." Analysis endpoints
(`/analyze`, `/validate`, `/assess-risk`, …) deliberately keep `404`: for
them a deleted invoice is simply not an actionable invoice, and they have
nothing to say about its history.

Deletion is *not* idempotent: a second `DELETE` returns `409 Conflict`
(`detail: "invoice is already deleted"`) so a retried request surfaces as a
client error instead of silently appearing to succeed twice. There is no
hard delete and no undelete endpoint; restoring a row is a database
operation performed deliberately by an operator.


### Reviewer identity is an env var, not authentication

The MVP ships no auth. `POST /reviews/{id}/resolve` requires a `reviewer_id`,
and that id is read by the frontend from `VITE_REVIEWER_ID` (see
`frontend/.env.example`). Every browser hitting a given build claims the same
identity. When the variable is unset the resolve form renders **disabled** with
an explanatory message rather than submitting a request that would 422, or
worse, attributing a decision to a placeholder identity.

This is an honest placeholder, not a softened version of authentication: it
makes the `reviewer_id` column meaningful so the audit trail is exercised
end-to-end, while being obviously unsuitable for a shared deployment. The
replacement is a session lookup that swaps the constant in
`frontend/src/config/reviewer.ts` for a real identity — the schema and the
service already carry the id, so nothing server-side has to change.

The frontend also cannot display the raw decision after the fact — see the
next section, which records that as a known limitation rather than a bug.

### Known limitation: decision direction is not stored

ReviewCase records `status`, `resolution`, and `resolved_at`, but not which
decision (approve / reject / dismiss) was made. Two cases can produce the
same final invoice status: a reject decision, and an approve decision on an
already-rejected invoice (which the terminal guard blocks). Because the
direction is not recoverable client-side, the UI deliberately does not
assert it — it displays the resolution text and the case status, and leaves
the interpretation to the reader.

Adding a `decision` column to ReviewCase would resolve this and enable a
precise UI. It is a small migration and a straightforward service change.
Deferred to keep Step 20 focused on the workflow itself.

