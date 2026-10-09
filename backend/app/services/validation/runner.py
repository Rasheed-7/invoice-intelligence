"""Orchestrator: run every validation check against one invoice.

Pure and deterministic — no DB, no FastAPI, no I/O. Composes the individual
checks in :mod:`app.services.validation.checks` into one
:class:`~app.services.validation.types.ValidationReport`.

Overall-outcome precedence: failed > warning > skipped > passed
(any failed check makes the report failed; only skipped checks (and passes)
with no failed/warning checks means skipped; otherwise passed).
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.services.validation.checks import (
    check_currency_presence,
    check_date_ordering,
    check_duplicate_invoice_number,
    check_line_item_totals,
    check_line_items_sum,
    check_subtotal_plus_tax,
    check_tax_rate_consistency,
)
from app.services.validation.types import (
    CheckResult,
    ValidationOutcome,
    ValidationReport,
)

__all__ = ["ALL_CHECKS", "ALL_DB_CHECKS", "run_all_checks"]


ALL_CHECKS: list[Callable[[Invoice], CheckResult]] = [
    check_line_item_totals,
    check_line_items_sum,
    check_subtotal_plus_tax,
    check_tax_rate_consistency,
    check_date_ordering,
    check_currency_presence,
]


ALL_DB_CHECKS: list[Callable[[Invoice, Session], CheckResult]] = [
    check_duplicate_invoice_number,
]


def run_all_checks(invoice: Invoice, db: Session | None = None) -> ValidationReport:
    """Run every check in ``ALL_CHECKS`` and ``ALL_DB_CHECKS`` order and build the report.

    Each pure check is a function of the invoice. DB checks require a database session.
    If db is None, db checks are skipped.
    """
    results = [check(invoice) for check in ALL_CHECKS]

    for db_check in ALL_DB_CHECKS:
        if db is None:
            results.append(
                CheckResult(
                    check_name=db_check.__name__.removeprefix("check_"),
                    rule_id=db_check.__name__.removeprefix("check_").upper(),
                    outcome=ValidationOutcome.SKIPPED,
                    message="Database session required",
                    details={"reason": "no db session"},
                    severity="info",
                )
            )
        else:
            results.append(db_check(invoice, db))


    counts = {"passed": 0, "failed": 0, "warning": 0, "skipped": 0}
    for result in results:
        counts[result.outcome.value] += 1

    if counts["failed"] > 0:
        overall = ValidationOutcome.FAILED
    elif counts["warning"] > 0:
        overall = ValidationOutcome.WARNING
    elif counts["passed"] == 0 and counts["skipped"] > 0:
        overall = ValidationOutcome.SKIPPED
    else:
        overall = ValidationOutcome.PASSED

    return ValidationReport(
        invoice_id=invoice.id,
        checks=results,
        overall_outcome=overall,
        summary=counts,
    )
