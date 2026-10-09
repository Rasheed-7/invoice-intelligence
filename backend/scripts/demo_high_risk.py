"""Build a HIGH-risk demo invoice so /reviews has a case in it.

The acme fixture on its own scores 18 (LOW), which means the human review
workflow can never be demonstrated against it: nothing ever crosses the HIGH
threshold, so the risk service never opens a review case.

This script manufactures that situation using only real work. Every step is a
real endpoint call or a real database write. Nothing is stubbed:

* the same PDF bytes are uploaded twice, so the duplicate signal fires on the
  IDENTICAL_FILE sha256 match the detector computes from the stored documents;
* a purchase order is created whose quantity deliberately disagrees with the
  invoice's, so PO matching produces a HIGH-severity quantity mismatch;
* the risk engine then reads the AnalysisResult rows those stages wrote and
  scores them. The score printed at the end is whatever GET /risk returns.

The one thing the script writes directly is ``invoice.po_number`` on the second
invoice, because no API endpoint exists for attaching a PO reference to an
existing invoice. Everything else goes through the public API.

Run it with::

    python scripts/demo_high_risk.py --reset

This is a demo harness, not a test. It is deliberately outside ``tests/`` so
pytest never collects it.
"""

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

import httpx

# The script lives in <repo>/scripts/ but imports from <repo>/app, and Python
# puts the *script's* directory on sys.path rather than the working directory.
# Resolve the backend root from __file__ so the script works from any cwd.
_BACKEND_ROOT: Final = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# `app` imports are deliberately function-local: they must happen after
# sys.path is adjusted, and keeping them out of module scope keeps this file
# importable (and lintable) without the backend package on the path.

DEFAULT_BASE_URL: Final = "http://localhost:8000"
DEFAULT_PO_QUANTITY: Final = 5
DEMO_PO_NUMBER: Final = "PO-DEMO-HIGHRISK"
DEMO_VENDOR: Final = "Acme Corporation"
SYSTEM_USER_EMAIL: Final = "system@local"
UPLOAD_FILENAME: Final = "acme.pdf"

#: Business tables emptied by --reset, in dependency order (children first).
#: `users` is deliberately absent: the system user must survive a reset so the
#: reviewer identity stays stable across runs.
RESET_TABLES: Final = (
    "analysis_results",
    "audit_events",
    "review_cases",
    "line_items",
    "invoices",
    "document_pages",
    "documents",
    "policy_chunks",
    "policy_documents",
    "purchase_order_items",
    "purchase_orders",
)

#: RISK_LEVEL_HIGH_MIN in app/services/risk/weights.py. Duplicated here so the
#: script fails loudly against its own stated expectation rather than silently
#: importing the constant and always agreeing with itself.
HIGH_RISK_MIN_SCORE: Final = 60

#: Number of numbered steps printed by this script. Only used for the progress
#: header, so a re-order is a one-line change.
TOTAL_STEPS: Final = 14

#: The risk contributors that must be present for this scenario to be the one
#: the script claims it builds.
REQUIRED_CONTRIBUTORS: Final = frozenset({"duplicate_finding", "po_match_finding"})

#: Generous per-request timeout: extraction runs PDF text extraction plus the
#: LLM step, and scikit-learn import on a cold process is not instant.
REQUEST_TIMEOUT: Final = httpx.Timeout(180.0, connect=10.0)


class StepError(RuntimeError):
    """A demo step failed. Carries the step name and whatever the API said."""

    def __init__(self, step: str, message: str, body: str | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.message = message
        self.body = body


# --- output helpers ---------------------------------------------------------


def _rule(char: str = "-", width: int = 75) -> str:
    return char * width


def _step(number: int, title: str) -> None:
    print()
    print(f"[{number}/{TOTAL_STEPS}] {title}")
    print(_rule())


def _info(message: str) -> None:
    print(f"        {message}")


def _ok(message: str) -> None:
    print(f"    OK  {message}")


# --- JSON access helpers ----------------------------------------------------
#
# The API returns untyped JSON. These narrow it to the concrete types the
# script expects, raising a clear StepError rather than letting a wrong shape
# surface as an obscure TypeError three steps later.


def _as_dict(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise StepError(where, f"expected a JSON object at {where}, got {type(value).__name__}")
    return value


def _as_list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise StepError(where, f"expected a JSON array at {where}, got {type(value).__name__}")
    return value


def _as_str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise StepError(where, f"expected a string at {where}, got {type(value).__name__}")
    return value


def _as_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StepError(where, f"expected an integer at {where}, got {type(value).__name__}")
    return value


def _as_decimal(value: object, where: str) -> Decimal | None:
    """Read a JSON money/quantity value as a Decimal, or None when absent.

    Numeric columns come back from the API as JSON strings ("5.0000") because
    the backend serializes Decimal to preserve precision. Comparing those
    against a plain int with ``==`` would silently report a mismatch on every
    run, so they are normalized here.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise StepError(where, f"expected a number at {where}, got bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise StepError(where, f"expected a numeric string at {where}, got {value!r}") from exc
    raise StepError(where, f"expected a number at {where}, got {type(value).__name__}")


def _pretty(value: object) -> str:
    return json.dumps(value, indent=2, default=str)


# --- HTTP helpers -----------------------------------------------------------


def _call(
    client: httpx.Client,
    step: str,
    method: str,
    path: str,
    *,
    expected: tuple[int, ...] = (200,),
    json_body: dict[str, object] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, object]:
    """Call the API and return the decoded object body, or raise StepError.

    A non-expected status always raises, carrying the raw response body: a demo
    harness that swallows a 422 and carries on produces a demo that is quietly
    wrong.
    """

    try:
        response = client.request(
            method,
            path,
            json=json_body,
            files=files,
            params=params,
        )
    except httpx.RequestError as exc:
        raise StepError(step, f"{method} {path} failed: {exc}") from exc

    if response.status_code not in expected:
        raise StepError(
            step,
            f"{method} {path} returned HTTP {response.status_code} "
            f"(expected {'/'.join(str(code) for code in expected)})",
            response.text,
        )

    if not response.content:
        return {}
    try:
        decoded = response.json()
    except ValueError as exc:
        raise StepError(step, f"{method} {path} returned non-JSON", response.text) from exc

    if not isinstance(decoded, dict):
        raise StepError(step, f"{method} {path} returned a non-object JSON body", response.text)
    return decoded


# --- step 1: health ---------------------------------------------------------


def step_health(client: httpx.Client) -> None:
    _step(1, "Health check")
    payload = _call(client, "health", "GET", "/api/health")
    status = _as_str(payload.get("status"), "health.status")
    service = _as_str(payload.get("service"), "health.service")
    if status != "ok":
        raise StepError("health", f"backend reports status={status!r}")
    _ok(f"backend reachable: {service} (status={status})")


# --- step 2: reset ----------------------------------------------------------


def step_reset(enabled: bool) -> None:
    _step(2, "Reset business tables" if enabled else "Reset business tables (skipped)")

    from sqlalchemy import text

    from app.db.session import SessionLocal

    if not enabled:
        _info("no --reset flag: running against the existing database state")
        return

    session = SessionLocal()
    try:
        for table in RESET_TABLES:
            session.execute(text(f"DELETE FROM {table}"))
        session.commit()
    except Exception as exc:
        # Any driver or constraint failure is a demo-step failure, not a crash:
        # it is reported with the step name and exits non-zero.
        session.rollback()
        raise StepError("reset", f"failed to empty business tables: {exc}") from exc
    finally:
        session.close()

    _ok(f"emptied {len(RESET_TABLES)} tables (users left untouched)")
    _info(", ".join(RESET_TABLES))


# --- step 3: system user ----------------------------------------------------


def step_system_user() -> str:
    _step(3, "Ensure the system reviewer exists")

    from sqlalchemy import text

    from app.db.init import seed_system_user
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        existing = session.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": SYSTEM_USER_EMAIL},
        ).first()
        if existing is None:
            # seed_system_user flushes without committing; the caller owns the
            # transaction, so the commit below is ours to make.
            seed_system_user(session)
            session.commit()
            _ok(f"created system user {SYSTEM_USER_EMAIL}")
        else:
            _ok(f"system user {SYSTEM_USER_EMAIL} already present")

        reviewer_id = session.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": SYSTEM_USER_EMAIL},
        ).scalar_one()
    except StepError:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        raise StepError("system-user", f"could not ensure the system user: {exc}") from exc
    finally:
        session.close()

    _info(f"reviewer UUID: {reviewer_id}")
    return str(reviewer_id)


# --- steps 4 and 5: upload and analyze --------------------------------------


def _upload(client: httpx.Client, step: str, pdf_bytes: bytes) -> str:
    payload = _call(
        client,
        step,
        "POST",
        "/api/v1/invoices/upload",
        expected=(201,),
        files={"file": (UPLOAD_FILENAME, pdf_bytes, "application/pdf")},
    )
    invoice_id = _as_str(payload.get("id"), f"{step}.id")
    sha = _as_str(payload.get("document_filename"), f"{step}.document_filename")
    _ok(f"{step}: invoice {invoice_id} created from {sha}")
    return invoice_id


def step_upload_twice(client: httpx.Client) -> tuple[str, str]:
    _step(4, "Upload the acme fixture twice")

    from app.services.llm.fixture_pdfs import build_fixture_pdf
    from app.services.llm.fixtures import FIXTURES

    entry = FIXTURES[0]
    pdf_bytes = build_fixture_pdf(entry)
    _info(f"fixture: {entry.fixture_id} ({entry.description})")
    _info(f"generated {len(pdf_bytes)} bytes, sha256={entry.sha256}")

    first_id = _upload(client, "upload #1", pdf_bytes)
    second_id = _upload(client, "upload #2", pdf_bytes)
    _ok("both uploads share one sha256, so IDENTICAL_FILE will fire on invoice #2")
    return first_id, second_id


def _analyze(client: httpx.Client, step: str, invoice_id: str) -> None:
    payload = _call(client, step, "POST", f"/api/v1/invoices/{invoice_id}/analyze")
    vendor = payload.get("vendor_name")
    total = payload.get("total_amount")
    number = payload.get("invoice_number")
    _ok(
        f"{step}: vendor={vendor!r} invoice_number={number!r} "
        f"total={total!r} currency={payload.get('currency')!r}"
    )


def step_analyze_both(client: httpx.Client, first_id: str, second_id: str) -> None:
    _step(5, "Analyze both invoices")
    _analyze(client, "analyze #1", first_id)
    _analyze(client, "analyze #2", second_id)


# --- step 6: mismatched purchase order --------------------------------------


def step_create_po(client: httpx.Client, po_quantity: int) -> str:
    _step(6, f"Create the deliberately mismatched purchase order {DEMO_PO_NUMBER}")

    line_total = f"{100 * po_quantity:.2f}"
    body: dict[str, object] = {
        "po_number": DEMO_PO_NUMBER,
        "vendor_name": DEMO_VENDOR,
        "total_amount": line_total,
        "items": [
            {
                "position": 1,
                "description": "Widget",
                "quantity": po_quantity,
                "unit_price": "100.00",
                "line_total": line_total,
            }
        ],
    }

    # po_number is unique, so a second run without --reset gets a 409. Rather
    # than failing, fetch the existing PO: the demo only needs a PO to compare
    # against, not a freshly minted one.
    try:
        payload = _call(
            client,
            "create-po",
            "POST",
            "/api/v1/purchase-orders",
            expected=(201,),
            json_body=body,
        )
        _ok(f"created {DEMO_PO_NUMBER} with 1 line of quantity {po_quantity}")
    except StepError as exc:
        if "HTTP 409" not in exc.message:
            raise
        _info("409 on po_number: reusing the purchase order from a previous run")
        payload = _call(
            client,
            "fetch-po",
            "GET",
            f"/api/v1/purchase-orders/by-number/{DEMO_PO_NUMBER}",
        )
        _ok(f"reusing existing {DEMO_PO_NUMBER}")

    po_id = _as_str(payload.get("id"), "purchase-order.id")
    existing_items = _as_list(payload.get("items"), "purchase-order.items")
    if existing_items:
        first = _as_dict(existing_items[0], "purchase-order.items[0]")
        _info(
            f"PO line: quantity={first.get('quantity')!r} "
            f"unit_price={first.get('unit_price')!r} "
            f"line_total={first.get('line_total')!r}"
        )
        _info("invoice line: quantity=1 unit_price='100.00' line_total='100.00'")
        if _as_decimal(first.get("quantity"), "purchase-order.items[0].quantity") == po_quantity:
            _ok(f"quantity mismatch confirmed: PO says {po_quantity}, invoice says 1")
        else:
            _info(
                "NOTE: the existing PO line quantity differs from --po-quantity. "
                "Pass --reset to rebuild it from scratch."
            )
    return po_id


# --- step 7: attach the PO reference ----------------------------------------


def step_set_po_number(second_id: str) -> None:
    _step(7, "Attach the PO reference to invoice #2 (direct DB update)")

    # No API endpoint exists for setting a PO reference on an existing invoice
    # - the extraction pipeline normally populates it from the document. The
    # demo script sets it directly so the PO matching stage has something to
    # compare.
    from sqlalchemy import text

    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        result = session.execute(
            text("UPDATE invoices SET po_number = :po WHERE id = CAST(:id AS uuid)"),
            {"po": DEMO_PO_NUMBER, "id": second_id},
        )
        session.commit()
        if result.rowcount != 1:
            raise StepError(
                "set-po-number",
                f"expected to update 1 invoice row, updated {result.rowcount}",
            )
    except StepError:
        session.rollback()
        raise
    except Exception as exc:
        session.rollback()
        raise StepError("set-po-number", f"could not set po_number: {exc}") from exc
    finally:
        session.close()

    _ok(f"invoice {second_id}.po_number = {DEMO_PO_NUMBER}")


# --- steps 8 to 12: analysis stages -----------------------------------------


def step_match_po(client: httpx.Client, second_id: str) -> None:
    _step(8, "Run PO matching on invoice #2")

    invoice = _call(client, "match-po", "POST", f"/api/v1/invoices/{second_id}/match-po")
    _info(f"invoice status after match: {invoice.get('status')!r}")

    # POST /match-po returns the invoice, not the report, so read the persisted
    # report to confirm the outcome rather than assuming it from the status.
    report = _call(client, "read-po-match", "GET", f"/api/v1/invoices/{second_id}/po-match")
    outcome = _as_str(report.get("overall_outcome"), "po-match.overall_outcome")
    if outcome != "mismatch":
        raise StepError(
            "match-po",
            f"expected overall_outcome 'mismatch', got {outcome!r}",
            _pretty(report),
        )

    _ok(f"PO match outcome: {outcome}")
    _info(f"po_reference={report.get('po_reference')!r} po_found={report.get('po_found')!r}")
    summary = _as_dict(report.get("mismatch_summary"), "po-match.mismatch_summary")
    _info(f"mismatch_summary: {_pretty(summary)}")
    _info(f"total_matches={report.get('total_matches')!r}")

    for raw in _as_list(report.get("line_matches"), "po-match.line_matches"):
        match = _as_dict(raw, "po-match.line_matches[]")
        for raw_mismatch in _as_list(match.get("mismatches"), "line_matches[].mismatches"):
            mismatch = _as_dict(raw_mismatch, "line_matches[].mismatches[]")
            _info(
                f"  - {mismatch.get('field')}: po={mismatch.get('po_value')!r} "
                f"invoice={mismatch.get('invoice_value')!r} "
                f"diff={mismatch.get('difference')!r} "
                f"severity={mismatch.get('severity')!r}"
            )


def step_check_duplicates(client: httpx.Client, second_id: str, first_id: str) -> None:
    _step(9, "Run duplicate detection on invoice #2")

    invoice = _call(
        client,
        "check-duplicates",
        "POST",
        f"/api/v1/invoices/{second_id}/check-duplicates",
    )
    _info(f"invoice status after duplicate check: {invoice.get('status')!r}")

    report = _call(client, "read-duplicates", "GET", f"/api/v1/invoices/{second_id}/duplicates")
    verdict = _as_str(report.get("verdict"), "duplicates.verdict")
    if verdict != "duplicate":
        raise StepError(
            "check-duplicates",
            f"expected verdict 'duplicate', got {verdict!r}",
            _pretty(report),
        )

    _ok(f"duplicate verdict: {verdict}")
    _info(f"notes: {report.get('notes')!r}")

    top = report.get("top_match")
    if top is None:
        raise StepError("check-duplicates", "verdict is 'duplicate' but top_match is null")
    top_match = _as_dict(top, "duplicates.top_match")
    _info(
        f"top match: invoice={top_match.get('matching_invoice_id')} "
        f"score={top_match.get('score')}"
    )
    signals = [str(signal) for signal in _as_list(top_match.get("signals"), "top_match.signals")]
    _info(f"signals: {', '.join(signals)}")
    _info(f"(expected to match invoice #1: {first_id})")


def step_validate(client: httpx.Client, second_id: str) -> None:
    _step(10, "Run deterministic validation on invoice #2")

    invoice = _call(client, "validate", "POST", f"/api/v1/invoices/{second_id}/validate")
    _info(f"invoice status after validate: {invoice.get('status')!r}")

    summary = _call(client, "read-validation", "GET", f"/api/v1/invoices/{second_id}/validation")
    _ok(f"validation overall_status: {summary.get('overall_status')!r}")
    counts = _as_dict(summary.get("counts"), "validation.counts")
    _info(f"check counts: {_pretty(counts)}")
    for raw in _as_list(summary.get("checks"), "validation.checks"):
        check = _as_dict(raw, "validation.checks[]")
        if check.get("status") in ("failed", "warning"):
            _info(f"  - {check.get('check_name')}: {check.get('message')!r}")


def step_policy_check(client: httpx.Client, second_id: str) -> None:
    _step(11, "Run the policy check on invoice #2")

    _call(client, "policy-check", "POST", f"/api/v1/invoices/{second_id}/policy-check")
    evidence = _call(
        client, "read-policy", "GET", f"/api/v1/invoices/{second_id}/policy-evidence"
    )
    rule_id = _as_str(evidence.get("rule_id"), "policy-evidence.rule_id")
    status = _as_str(evidence.get("status"), "policy-evidence.status")
    _ok(f"policy finding: rule_id={rule_id} status={status}")
    _info(f"message: {evidence.get('message')!r}")
    if rule_id == "NOT_RUN":
        raise StepError("policy-check", "policy analysis did not run")
    if rule_id == "NO_APPLICABLE_POLICY":
        _info(
            "no policies are ingested, so the check ran and found nothing to cite. "
            "That is worth +10 to the risk score, not a failure."
        )


def step_assess_risk(client: httpx.Client, second_id: str) -> None:
    _step(12, "Score the invoice with the risk engine")

    invoice = _call(client, "assess-risk", "POST", f"/api/v1/invoices/{second_id}/assess-risk")
    _ok(f"invoice status after assess-risk: {invoice.get('status')!r}")


def fetch_risk(client: httpx.Client, second_id: str) -> dict[str, object]:
    risk = _call(client, "read-risk", "GET", f"/api/v1/invoices/{second_id}/risk")
    return risk


def verify_risk(risk: dict[str, object], po_quantity: int) -> None:
    _step(13, "Verify the risk assessment")

    score = _as_int(risk.get("score"), "risk.score")
    level = _as_str(risk.get("level"), "risk.level")
    action = _as_str(risk.get("recommended_action"), "risk.recommended_action")
    total = _as_int(risk.get("contributing_total"), "risk.contributing_total")
    contributors = _as_list(risk.get("contributors"), "risk.contributors")

    points_sum = 0
    rule_ids: set[str] = set()
    for raw in contributors:
        contributor = _as_dict(raw, "risk.contributors[]")
        rule_id = _as_str(contributor.get("rule_id"), "contributor.rule_id")
        points = _as_int(contributor.get("points"), "contributor.points")
        points_sum += points
        rule_ids.add(rule_id)
        _info(f"  {rule_id:<28} +{points:<3} ({contributor.get('severity')})")

    _info("")
    _info(f"sum of contributor points = {points_sum}")
    _info(f"contributing_total        = {total}")
    if points_sum != total:
        raise StepError(
            "verify-risk",
            f"contributor points ({points_sum}) do not sum to contributing_total ({total})",
        )
    _ok("contributor points sum exactly to contributing_total")
    _ok(f"score {score} (cap 100), level={level}, recommended_action={action}")

    missing = REQUIRED_CONTRIBUTORS - rule_ids
    if missing:
        raise StepError(
            "verify-risk",
            f"expected contributors {sorted(REQUIRED_CONTRIBUTORS)}, missing {sorted(missing)}",
            _pretty(risk),
        )
    _ok(f"contributors include {', '.join(sorted(REQUIRED_CONTRIBUTORS))}")

    if score < HIGH_RISK_MIN_SCORE:
        raise StepError(
            "verify-risk",
            f"score {score} is below the HIGH threshold ({HIGH_RISK_MIN_SCORE}).\n"
            f"       Contributors that fired: "
            f"{', '.join(sorted(rule_ids)) or 'none'}.\n"
            f"       Adjust and re-run: --po-quantity 5 (default) makes the PO\n"
            f"       quantity disagree with the invoice's quantity of 1, which is\n"
            f"       what produces the po_match_finding worth +25. A value of 1\n"
            f"       would make the quantities agree and drop that contributor.\n"
            f"       With --reset the demo is rebuilt from scratch, so the\n"
            f"       purchase order line always reflects the new --po-quantity.",
        )

    if level != "high" and action not in ("review", "reject"):
        raise StepError(
            "verify-risk",
            f"level={level!r} with recommended_action={action!r} does not route to review",
        )
    _ok(f"HIGH risk confirmed at score {score} (threshold {HIGH_RISK_MIN_SCORE})")

    if po_quantity == 1:
        _info(
            "NOTE: --po-quantity 1 makes the PO quantity agree with the invoice, "
            "so po_match_finding can only come from the total mismatch."
        )


def verify_review_case(client: httpx.Client, second_id: str) -> str:
    _step(14, "Verify the review case exists")
    payload = _call(
        client,
        "reviews",
        "GET",
        "/api/v1/reviews",
        params={"status": "pending", "limit": "200"},
    )
    items = _as_list(payload.get("items"), "reviews.items")
    _info(f"pending review cases: {len(items)} (total matching filter: {payload.get('total')})")

    for raw in items:
        case = _as_dict(raw, "reviews.items[]")
        if case.get("invoice_id") == second_id:
            case_id = _as_str(case.get("id"), "review-case.id")
            _ok(f"review case {case_id} is open for invoice {second_id}")
            _info(f"status: {case.get('status')!r}")
            _info(f"reason: {case.get('reason')!r}")
            return case_id

    raise StepError(
        "reviews",
        f"no pending review case references invoice {second_id}; "
        f"the risk score did not open one. Pending cases: "
        f"{[case.get('id') for case in map(_as_dict, items)]}",
    )


# --- summary ----------------------------------------------------------------


def print_summary(
    *,
    first_id: str,
    second_id: str,
    risk: dict[str, object],
    case_id: str,
    base_url: str,
) -> None:
    contributors = _as_list(risk.get("contributors"), "risk.contributors")
    rule_width = max(
        (len(str(_as_dict(raw, "risk.contributors[]").get("rule_id"))) for raw in contributors),
        default=len("policy_finding"),
    )

    print()
    print("=" * 75)
    print("  HIGH-RISK DEMO READY")
    print("=" * 75)
    print(f"  Invoice #1 (clean):     {first_id}")
    print(f"  Invoice #2 (high risk): {second_id}")
    print(f"    Risk score:           {risk.get('score')} / 100")
    print(f"    Level:                {risk.get('level')}")
    print(f"    Recommended action:   {risk.get('recommended_action')}")
    print(f"    Contributing total:   {risk.get('contributing_total')}")
    print("    Contributors:")
    for raw in contributors:
        contributor = _as_dict(raw, "risk.contributors[]")
        rule_id = str(contributor.get("rule_id"))
        points = contributor.get("points")
        print(f"      - {rule_id:<{rule_width}}  +{points}")
    print(f"  Review case:            {case_id}")
    print()
    print("  Open these in the browser:")
    print(f"    http://localhost:5173/invoices/{second_id}")
    print("    http://localhost:5173/reviews")
    print()
    print(f"  Backend: {base_url}")
    print("=" * 75)


# --- orchestration ----------------------------------------------------------


def run_demo(base_url: str, reset: bool, po_quantity: int) -> int:
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=REQUEST_TIMEOUT) as client:
        step_health(client)
        step_reset(reset)
        step_system_user()
        first_id, second_id = step_upload_twice(client)
        step_analyze_both(client, first_id, second_id)
        step_create_po(client, po_quantity)
        step_set_po_number(second_id)
        step_match_po(client, second_id)
        step_check_duplicates(client, second_id, first_id)
        step_validate(client, second_id)
        step_policy_check(client, second_id)
        step_assess_risk(client, second_id)
        risk = fetch_risk(client, second_id)
        verify_risk(risk, po_quantity)
        case_id = verify_review_case(client, second_id)
        print_summary(
            first_id=first_id,
            second_id=second_id,
            risk=risk,
            case_id=case_id,
            base_url=base_url,
        )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="demo_high_risk.py",
        description=(
            "Build a HIGH-risk invoice from the acme fixture so /reviews has a case "
            "in it. Every step is a real API call or a real database write; the risk "
            "score printed at the end is whatever GET /risk returns."
        ),
        epilog=(
            "Running twice without --reset is safe: the purchase order number is "
            "unique, so the second run reuses the existing PO instead of failing. "
            "Note that a reused PO keeps its original line quantity - pass --reset "
            "to rebuild the demo from scratch with a new --po-quantity."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Backend base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Empty all business tables before running. Off by default, so an "
            "unflagged run respects whatever is already in the database. "
            "The users table is never touched."
        ),
    )
    parser.add_argument(
        "--po-quantity",
        type=int,
        default=DEFAULT_PO_QUANTITY,
        help=(
            "Quantity on the intentionally mismatched purchase order "
            f"(default: {DEFAULT_PO_QUANTITY}). The invoice's line item has "
            "quantity 1, so any value other than 1 produces a HIGH-severity "
            "quantity mismatch worth +25 to the risk score."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.po_quantity < 1:
        print("FAILED: --po-quantity must be a positive integer", file=sys.stderr)
        return 2

    try:
        return run_demo(args.base_url, args.reset, args.po_quantity)
    except StepError as exc:
        print(file=sys.stderr)
        print("=" * 75, file=sys.stderr)
        print(f"FAILED at step: {exc.step}", file=sys.stderr)
        print("=" * 75, file=sys.stderr)
        print(exc.message, file=sys.stderr)
        if exc.body:
            print("-" * 75, file=sys.stderr)
            print("Response body:", file=sys.stderr)
            print(exc.body, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nFAILED: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())