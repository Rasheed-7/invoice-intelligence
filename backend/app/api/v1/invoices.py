"""Invoice upload and retrieval endpoints."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.invoice import InvoiceDetailResponse, InvoiceListResponse, InvoiceResponse
from app.schemas.validation import ValidationCheckResponse, ValidationSummaryResponse
from app.services.anomaly_service import get_anomaly_result, run_anomaly_check
from app.services.document_processing import (
    DocumentNotFound,
    DocumentProcessingError,
    process_document_extraction,
)
from app.services.documents import (
    InvalidExtension,
    UnsupportedContentType,
    UploadTooLarge,
    create_document,
)
from app.services.duplicate_service import get_duplicate_result, run_duplicate_check
from app.services.invoices import (
    InvoiceAlreadyDeleted,
    create_invoice_from_upload,
    get_invoice,
    get_invoice_including_deleted,
    list_invoices,
    soft_delete_invoice,
)
from app.services.llm_extraction import (
    ExtractionSourceEmpty,
    LLMExtractionFailed,
    run_llm_extraction,
)
from app.services.po_matching_service import get_po_match_result, run_po_matching
from app.services.policy_analysis import get_policy_evidence, run_policy_analysis
from app.services.risk_service import get_risk_assessment, run_risk_assessment
from app.services.storage import get_storage
from app.services.validation_service import (
    ValidationFailed,
    get_validation_results,
    run_validation,
)

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post(
    "/upload",
    response_model=InvoiceDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document and create an invoice",
)
async def upload_invoice(
    file: Annotated[UploadFile, File()],
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Upload a document and create an associated invoice in INGESTED status."""

    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="filename is required",
        )

    data = await file.read()

    try:
        document = create_document(
            db,
            filename=filename,
            content_type=file.content_type or "application/octet-stream",
            data=data,
            storage=get_storage(),
        )
    except UploadTooLarge as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="file exceeds maximum size",
        ) from exc
    except UnsupportedContentType as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="unsupported content type",
        ) from exc
    except InvalidExtension as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid file extension",
        ) from exc

    invoice = create_invoice_from_upload(db, document=document)

    db.commit()
    db.refresh(document)
    db.refresh(invoice)

    return InvoiceDetailResponse.from_orm_invoice(invoice)


@router.get(
    "",
    response_model=InvoiceListResponse,
    summary="List invoices with pagination",
)
def list_invoices_endpoint(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InvoiceListResponse:
    """Return a paginated list of invoices ordered by created_at DESC."""

    invoices, total = list_invoices(db, limit=limit, offset=offset)

    return InvoiceListResponse(
        items=[InvoiceResponse.from_orm_invoice(inv) for inv in invoices],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{invoice_id}",
    response_model=InvoiceDetailResponse,
    summary="Fetch a single invoice by id",
)
def read_invoice(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Return a single invoice with document details.

    404 when the id never existed; 410 Gone when it existed but was
    soft-deleted - the resource was real, and saying otherwise would send
    clients hunting for a typo that isn't there. Analysis endpoints
    deliberately keep returning 404: for them a deleted invoice is simply
    not an actionable invoice.
    """

    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        if get_invoice_including_deleted(db, invoice_id) is not None:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="invoice deleted",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    return InvoiceDetailResponse.from_orm_invoice(invoice)


@router.delete(
    "/{invoice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete an invoice",
)
def delete_invoice(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Soft-delete an invoice: set ``deleted_at``, preserve everything else.

    204 on success, 404 when the id never existed, 409 when the invoice is
    already deleted (deletion is intentionally not idempotent so a retried
    DELETE surfaces as a client error rather than silently "succeeding"
    twice). The row, its analysis results, audit events, line items and
    document all survive - only the invoice disappears from active views.
    """
    try:
        soft_delete_invoice(db, invoice_id=invoice_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        ) from exc
    except InvoiceAlreadyDeleted as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="invoice is already deleted",
        ) from exc


@router.post(
    "/{invoice_id}/extract",
    response_model=InvoiceDetailResponse,
    summary="Extract text from the invoice's document",
)
def extract_invoice(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Run extraction for the invoice's document, then return fresh state."""

    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    try:
        process_document_extraction(
            db,
            document_id=invoice.document_id,
            storage=get_storage(),
        )
    except DocumentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="document not found",
        ) from exc
    except DocumentProcessingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Re-fetch so the response reflects the post-extraction document state.
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    return InvoiceDetailResponse.from_orm_invoice(invoice)


@router.post(
    "/{invoice_id}/analyze",
    response_model=InvoiceDetailResponse,
    summary="Run AI extraction on an invoice",
)
def analyze_invoice(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Run the full extraction pipeline: text extraction + LLM extraction.

    Idempotency: this route may be called repeatedly. Each call re-runs extraction
    and overwrites invoice fields and line items. This is intentional - it allows
    re-running after fixture changes or provider swaps.

    Partial-failure semantics: if text extraction succeeds but LLM extraction fails,
    the document is left in TEXT_EXTRACTED state and the invoice remains in its
    previous status. The next /analyze call will skip text extraction and only retry
    the LLM step.
    """
    import logging

    logger = logging.getLogger(__name__)

    # Load invoice
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    document = invoice.document

    # Run text extraction if needed
    from app.models.enums import DocumentProcessingStatus

    if (
        document.extracted_text is None or document.extracted_text.strip() == ""
    ) and document.processing_status != DocumentProcessingStatus.TEXT_EXTRACTED:
        try:
            process_document_extraction(
                db,
                document_id=document.id,
                storage=get_storage(),
            )
        except DocumentNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="document not found",
            ) from exc
        except DocumentProcessingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="text extraction failed",
            ) from exc

        # Re-fetch document to get fresh state after text extraction
        invoice = get_invoice(db, invoice_id)
        if invoice is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="invoice not found",
            )
        document = invoice.document

    # Run LLM extraction
    try:
        run_llm_extraction(db, invoice_id=invoice_id)
    except ExtractionSourceEmpty as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no extractable text in document",
        ) from exc
    except LLMExtractionFailed as exc:
        # Do not leak internal error details to the client
        logger.warning("LLM extraction failed for invoice %s: %s", invoice_id, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="AI extraction failed",
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        ) from exc

    # Re-fetch to return fresh state
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    return InvoiceDetailResponse.from_orm_invoice(invoice)


@router.post(
    "/{invoice_id}/validate",
    response_model=InvoiceDetailResponse,
    summary="Run deterministic validation checks on an invoice",
)
def validate_invoice(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Run every validation check and persist the results as analysis rows.

    Idempotency: re-calling /validate re-runs the checks against current
    invoice state, replaces this invoice's prior calculation_check results
    (other analysis types are untouched), appends a fresh audit event, and
    re-applies the status transition. Repeat calls do not accumulate rows.
    """

    try:
        run_validation(db, invoice_id=invoice_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        ) from exc
    except ValidationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="validation could not run",
        ) from exc

    # Re-fetch so the response reflects the post-validation state.
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    return InvoiceDetailResponse.from_orm_invoice(invoice)


@router.get(
    "/{invoice_id}/validation",
    response_model=ValidationSummaryResponse,
    summary="Get persisted validation results for an invoice",
)
def read_validation(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> ValidationSummaryResponse:
    """Return this invoice's persisted calculation_check results.

    An empty check list with overall_status "skipped" is a valid state meaning
    validation never ran - not an error. overall_status follows the same
    precedence as ValidationReport: failed > warning > skipped > passed.
    """
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    rows = get_validation_results(db, invoice_id)

    if not rows:
        # Validation never ran (or wrote nothing): a valid state, not an
        # error - the caller can POST /validate to produce results.
        return ValidationSummaryResponse(
            invoice_id=invoice_id,
            overall_status="skipped",
            checks=[],
            counts={"passed": 0, "failed": 0, "warning": 0, "skipped": 0},
        )

    checks = [
        ValidationCheckResponse(
            check_name=row.details["check_name"],
            rule_id=row.details["rule_id"],
            status=str(row.status.value),
            message=row.details["message"],
            severity=row.details["severity"],
            details=row.details,
        )
        for row in rows
    ]

    counts = {"passed": 0, "failed": 0, "warning": 0, "skipped": 0}
    for row in rows:
        counts[str(row.status.value)] += 1

    if counts["failed"] > 0:
        overall_status: Literal["passed", "failed", "warning", "skipped"] = "failed"
    elif counts["warning"] > 0:
        overall_status = "warning"
    elif counts["passed"] == 0 and counts["skipped"] > 0:
        overall_status = "skipped"
    else:
        overall_status = "passed"

    return ValidationSummaryResponse(
        invoice_id=invoice_id,
        overall_status=overall_status,
        checks=checks,
        counts=counts,
    )


@router.post(
    "/{invoice_id}/match-po",
    response_model=InvoiceDetailResponse,
    summary="Run deterministic PO matching on an invoice",
)
def match_purchase_order(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Run PO matching, persist the report, and return fresh invoice state.

    Idempotency: re-calling replaces this invoice's prior po_match result
    (other analysis types are untouched), appends a fresh audit event, and
    re-applies the status transition. Repeat calls do not accumulate rows.
    """

    try:
        run_po_matching(db, invoice_id=invoice_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        ) from exc

    # Re-fetch so the response reflects the post-match state (a MISMATCH may
    # have moved the invoice to NEEDS_REVIEW).
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    return InvoiceDetailResponse.from_orm_invoice(invoice)


def _empty_po_match_payload(invoice_id: UUID) -> dict[str, object]:
    """Neutral response used when no PO match result exists for an invoice.

    This is a valid empty state, not an error: matching simply has not been
    run yet, and the caller can POST /match-po to produce a result.
    """
    return {
        "invoice_id": str(invoice_id),
        "po_reference": None,
        "po_found": False,
        "overall_outcome": "skipped",
        "line_matches": [],
        "unmatched_invoice_items": [],
        "unmatched_po_items": [],
        "mismatch_summary": {},
        "vendor_matches": None,
        "total_matches": None,
        "notes": ["no PO match has been run"],
    }


@router.get(
    "/{invoice_id}/po-match",
    summary="Get the persisted PO match report for an invoice",
)
def read_po_match(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    """Return this invoice's persisted PO match report.

    An unknown invoice 404s, same as POST /match-po - a real invoice that was
    never matched is a different thing and must not look the same as one that
    does not exist. When matching has never run on an invoice that does exist,
    a neutral skipped-shaped payload is returned instead of 404. That is a
    valid empty state, not an error.
    """
    if get_invoice(db, invoice_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    result = get_po_match_result(db, invoice_id)
    if result is None or result.details is None:
        return _empty_po_match_payload(invoice_id)

    # details is already a JSON-serializable dict: run_po_matching stored
    # report.model_dump(mode="json") when the result was written.
    return result.details


# --- policy evidence --------------------------------------------------------


def _empty_policy_finding_payload() -> dict[str, object]:
    """Neutral response used when no policy check has run for an invoice.

    This is a valid empty state, not an error: the caller can POST
    /policy-check to produce a real finding. It is deliberately *not* shaped
    like a real "no policy matched" finding - ``rule_id`` is NOT_RUN rather than
    NO_APPLICABLE_POLICY, so a client can never mistake "we have not looked"
    for "we looked and found nothing applicable".
    """
    return {
        "rule_id": "NOT_RUN",
        "status": "skipped",
        "message": "policy analysis has not been run",
        "evidence": [],
    }


@router.post(
    "/{invoice_id}/policy-check",
    response_model=InvoiceDetailResponse,
    summary="Retrieve policy evidence for an invoice",
)
def run_policy_check(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Run policy retrieval, persist the finding, and return fresh invoice state.

    Separate from /analyze and /validate on purpose: retrieval needs an
    embedded policy corpus, which is optional, so making it part of the main
    pipeline would fail invoices whenever no policy has been ingested.

    The invoice status is never changed here - evidence does not by itself
    force review.
    """

    try:
        run_policy_analysis(db, invoice_id=invoice_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        ) from exc

    invoice = get_invoice(db, invoice_id)
    if invoice is None:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    return InvoiceDetailResponse.from_orm_invoice(invoice)


@router.get(
    "/{invoice_id}/policy-evidence",
    summary="Get the persisted policy evidence finding for an invoice",
)
def read_policy_evidence(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    """Return the stored PolicyFinding for this invoice.

    404 only when the invoice does not exist. An invoice that exists but has
    never been checked gets the neutral NOT_RUN payload, which is deliberately
    distinguishable from a real NO_APPLICABLE_POLICY finding.
    """
    if get_invoice(db, invoice_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    result = get_policy_evidence(db, invoice_id)
    if result is None or result.details is None:
        return _empty_policy_finding_payload()

    # details is the PolicyFinding dumped as JSON when the check ran.
    return result.details


# --- duplicates and anomalies ------------------------------------------------


def _empty_duplicate_payload(invoice_id: UUID) -> dict[str, object]:
    """Neutral response when duplicate detection has never run.

    A valid empty state, not an error: the caller can POST /check-duplicates to
    produce a real report. The note makes clear this is "not looked" rather than
    "looked and found nothing", even though the verdict field has to carry a
    real enum value.
    """
    return {
        "invoice_id": str(invoice_id),
        "verdict": "unique",
        "top_match": None,
        "all_matches": [],
        "notes": ["duplicate check has not been run"],
    }


def _empty_anomaly_payload(invoice_id: UUID) -> dict[str, object]:
    """Neutral response when anomaly detection has never run.

    ``insufficient_history`` is the honest verdict here: with no history there
    is genuinely nothing to compare against, which is exactly the state an
    unrun check is in.
    """
    return {
        "invoice_id": str(invoice_id),
        "verdict": "insufficient_history",
        "method": "none",
        "score": None,
        "contributions": [],
        "history_size": 0,
        "notes": ["anomaly check has not been run"],
    }


def _detail_or_404(db: Session, invoice_id: UUID) -> InvoiceDetailResponse:
    """Re-fetch an invoice as a detail response, 404 if it vanished."""
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )
    return InvoiceDetailResponse.from_orm_invoice(invoice)


@router.post(
    "/{invoice_id}/check-duplicates",
    response_model=InvoiceDetailResponse,
    summary="Run duplicate invoice detection",
)
def check_duplicates(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Run duplicate detection, persist the finding, and return fresh state.

    A confirmed DUPLICATE may move the invoice to NEEDS_REVIEW; a suspected one
    does not. Terminal statuses are never downgraded.
    """
    try:
        run_duplicate_check(db, invoice_id=invoice_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        ) from exc

    # Re-fetch so the response reflects the post-check status.
    return _detail_or_404(db, invoice_id)


@router.get(
    "/{invoice_id}/duplicates",
    summary="Get the persisted duplicate report for an invoice",
)
def read_duplicates(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    """Return the stored DuplicateReport, or a neutral payload if never run.

    An unknown invoice 404s: a real invoice that was never checked is a
    different thing and must not look the same as one that does not exist.
    """
    if get_invoice(db, invoice_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    result = get_duplicate_result(db, invoice_id)
    if result is None or result.details is None:
        return _empty_duplicate_payload(invoice_id)

    return result.details


@router.post(
    "/{invoice_id}/check-anomalies",
    response_model=InvoiceDetailResponse,
    summary="Run anomaly detection against the vendor's history",
)
def check_anomalies(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Run anomaly detection, persist the finding, and return fresh state.

    Only a STRONG_ANOMALY may move the invoice to NEEDS_REVIEW. Terminal
    statuses are never downgraded.
    """
    try:
        run_anomaly_check(db, invoice_id=invoice_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        ) from exc

    return _detail_or_404(db, invoice_id)


@router.get(
    "/{invoice_id}/anomalies",
    summary="Get the persisted anomaly report for an invoice",
)
def read_anomalies(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    """Return the stored AnomalyReport, or a neutral payload if never run."""
    if get_invoice(db, invoice_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    result = get_anomaly_result(db, invoice_id)
    if result is None or result.details is None:
        return _empty_anomaly_payload(invoice_id)

    return result.details


# --- risk scoring -------------------------------------------------------------

#: Marks a response as "never scored" rather than "scored and found nothing".
#: A client distinguishes the two cases by this note, not by the score.
_RISK_NOT_RUN_NOTE = "risk assessment has not been run"


class RiskAssessmentEmpty(BaseModel):
    """Neutral payload for an invoice that has never been risk-scored.

    ``level`` and ``recommended_action`` are deliberately ``"unknown"`` rather
    than a real RiskLevel/RecommendedAction value: "never ran" is a distinct
    state from "ran and concluded low", and collapsing the two would let a
    caller mistake an unscored invoice for a clean one. RiskAssessment itself is
    left untouched - this shape only exists to describe the absence of a result.
    """

    model_config = ConfigDict(extra="forbid")

    invoice_id: str
    score: None = None
    level: Literal["unknown"] = "unknown"
    recommended_action: Literal["unknown"] = "unknown"
    contributors: list[dict[str, object]] = Field(default_factory=list)
    contributing_total: int = 0
    caps_applied: bool = False
    notes: list[str] = Field(
        default_factory=lambda: [_RISK_NOT_RUN_NOTE],
    )


@router.post(
    "/{invoice_id}/assess-risk",
    response_model=InvoiceDetailResponse,
    summary="Run rule-based risk scoring for an invoice",
)
def assess_risk_endpoint(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceDetailResponse:
    """Score the invoice's prior analyses, persist the result, return state.

    Reads every existing AnalysisResult (calculation checks, PO match, policy
    evidence, duplicate verdict, anomaly verdict), scores them with the pure
    engine, and writes one RISK_SCORE row plus a RISK_SCORED audit event.

    Idempotent: a re-run replaces this invoice's prior RISK_SCORE row rather
    than appending, so repeated calls converge on one current score. Rows of
    other analysis types are never touched.

    A REJECT recommendation is the only analysis stage that may set the terminal
    REJECTED status; high risk without one routes to NEEDS_REVIEW. Terminal
    statuses are never downgraded.
    """
    try:
        run_risk_assessment(db, invoice_id=invoice_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        ) from exc

    # Re-fetch so the response reflects the post-scoring status transition.
    return _detail_or_404(db, invoice_id)


@router.get(
    "/{invoice_id}/risk",
    summary="Get the persisted risk assessment for an invoice",
)
def read_risk_assessment(
    invoice_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object] | RiskAssessmentEmpty:
    """Return the stored risk assessment, or a neutral payload if never run.

    The persisted ``details`` blob is the full ``RiskAssessment`` dump, returned
    unchanged so every contributor and note survives the round-trip. Note that
    ``details.score`` is the 0-100 score; the AnalysisResult.score column is the
    normalized 0.0000-1.0000 fraction, which is deliberately not exposed here
    to avoid two representations of one number in one payload.
    """
    if get_invoice(db, invoice_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invoice not found",
        )

    result = get_risk_assessment(db, invoice_id)
    if result is None or result.details is None:
        # Neutral empty state, not an error - the caller can distinguish "ran
        # and scored 0" from "never ran" by the notes field.
        return RiskAssessmentEmpty(invoice_id=str(invoice_id))

    return result.details
