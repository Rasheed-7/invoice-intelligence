"""Per-contributor caps, thresholds, and tuning constants for the MVP risk model.

These weights are engineering judgment, not empirically calibrated. They are
labeled an MVP risk model. Empirical calibration would require a labeled
dataset of historically-risk-scored invoices, which we do not have.

Design: each finding category contributes a bounded number of points (its
per-contributor cap), the contributions are summed, and the total is capped
at MAX_SCORE. Caps keep any single severe finding from dominating the score,
while the global cap keeps the output on a fixed 0..100 scale no matter how
many findings fire at once.
"""

__all__ = [
    "MAX_SCORE",
    "CAP_DUPLICATE",
    "CAP_PO_MISMATCH",
    "CAP_VALIDATION_FAILURE",
    "CAP_STRONG_ANOMALY",
    "CAP_POLICY_UNCERTAINTY",
    "CAP_LOW_EXTRACTION_CONFIDENCE",
    "CAP_MISSING_REQUIRED_FIELD",
    "RISK_LEVEL_MEDIUM_MIN",
    "RISK_LEVEL_HIGH_MIN",
    "AUTO_APPROVE_MAX",
    "REJECT_MIN",
    "LOW_CONFIDENCE_THRESHOLD",
    "REQUIRED_FIELDS",
]

#: Global ceiling for the final score. The uncapped sum is preserved on the
#: assessment as `contributing_total` so capping is auditable.
MAX_SCORE = 100

#: Highest cap: a confirmed duplicate is the strongest single risk signal —
#: the same economic claim submitted twice. Sits above every other cap.
CAP_DUPLICATE = 35

#: A PO mismatch contradicts an independent record of what was ordered.
#: Below CAP_DUPLICATE (a mismatch disputes terms; a duplicate repeats the
#: whole claim) and tied with CAP_VALIDATION_FAILURE (both are arithmetic
#: contradictions of comparable weight).
CAP_PO_MISMATCH = 25

#: Failed validation checks contradict the invoice's own arithmetic. Tied with
#: CAP_PO_MISMATCH; above CAP_STRONG_ANOMALY because a proven arithmetic
#: error outweighs a statistical deviation.
CAP_VALIDATION_FAILURE = 25

#: A strong statistical deviation is evidence, not proof. Below both
#: contradiction caps (25) and above CAP_POLICY_UNCERTAINTY (10), since an
#: outlier at least describes this invoice's own numbers.
CAP_STRONG_ANOMALY = 20

#: Low extraction confidence means the inputs themselves are uncertain.
#: Below CAP_STRONG_ANOMALY: uncertain inputs warrant caution, but a measured
#: deviation in trusted numbers is more informative than doubt about the
#: numbers at all. Above CAP_MISSING_REQUIRED_FIELD per field, since
#: low-but-present confidence still carries some signal.
CAP_LOW_EXTRACTION_CONFIDENCE = 20

#: A missing required field leaves the model blind on one dimension. Smallest
#: per-field signal: absence of evidence, not evidence of wrongdoing. Well
#: below every finding cap; several missing fields can still add up.
CAP_MISSING_REQUIRED_FIELD = 10

#: Uncertainty from policy retrieval (e.g. no applicable policy found, so no
#: external rule corroborates the invoice). Smallest finding-level cap, tied
#: with a single missing field: it describes the absence of corroboration,
#: not a contradiction.
CAP_POLICY_UNCERTAINTY = 10

#: Score at or above which the invoice reads as medium risk. Sits above
#: AUTO_APPROVE_MAX (20) so the auto-approve band is strictly low-risk, and
#: well below RISK_LEVEL_HIGH_MIN (60) so medium spans a genuine middle band.
RISK_LEVEL_MEDIUM_MIN = 25

#: Score at or above which the invoice reads as high risk. Below REJECT_MIN
#: (85) so high risk still defaults to human review rather than auto-reject.
RISK_LEVEL_HIGH_MIN = 60

#: Highest score that may auto-approve. Below RISK_LEVEL_MEDIUM_MIN (25) so
#: only firmly low-risk invoices skip review.
AUTO_APPROVE_MAX = 20

#: Score at or above which the model recommends reject. Below MAX_SCORE
#: (100) so the band is reachable, and above RISK_LEVEL_HIGH_MIN (60) so
#: rejection is a subset of high risk, not its entirety.
REJECT_MIN = 85

#: Per-field extraction confidence below which the field counts as uncertain.
#: A 0.6 floor treats fixture-grade matches (>= 0.9) and explicit regex hits
#: (0.85) as trusted while flagging heuristic and missing-field confidences.
LOW_CONFIDENCE_THRESHOLD = 0.6

#: Fields whose absence contributes to risk. Each missing field adds up to
#: CAP_MISSING_REQUIRED_FIELD; all four missing still stays below any single
#: contradiction cap, since missing data is weaker evidence than wrong data.
REQUIRED_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "total_amount")
