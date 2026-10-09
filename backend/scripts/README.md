# Demo scripts

For a pre-scripted end-to-end demo that reaches HIGH risk and populates the
review queue, run:

    cd backend
    .\.venv\Scripts\Activate.ps1
    python scripts/demo_high_risk.py --reset

Then open http://localhost:5173/reviews to work the case. See
`scripts/README.md` for what each step does.


## `demo_high_risk.py`

Builds a HIGH-risk invoice from the acme fixture so `/reviews` has a case in it.

The acme fixture on its own scores 18 (LOW), so the risk engine never crosses
the HIGH threshold and no review case is ever created. This script manufactures
that situation: it uploads the same PDF twice (identical sha256, so the
duplicate signal fires), builds a purchase order whose quantity deliberately
disagrees with the invoice's, and lets the risk engine score the result.

The score is **not** fabricated. It is whatever `GET /risk` returns after
`POST /assess-risk` has run, computed by the engine from the real
`AnalysisResult` rows those stages wrote. The script verifies the contributors
sum to `contributing_total` and fails loudly if the score stays below HIGH.

### Preconditions

- Backend running on `http://localhost:8000` (`uvicorn app.main:app`)
- Postgres healthy, migrations applied (`alembic upgrade head`)
- Run from `C:\AI_INVOICE\backend` with the venv active

### Run it

```bash
python scripts/demo_high_risk.py --reset
```

| Flag | Default | Effect |
| --- | --- | --- |
| `--base-url` | `http://localhost:8000` | Backend to talk to |
| `--reset` | off | Empty all business tables first (`users` is never touched) |
| `--po-quantity` | `5` | Quantity on the mismatched PO; the invoice's line has quantity 1 |

Re-running without `--reset` is safe: the PO number is unique, so a second run
gets a 409 and reuses the existing purchase order instead of failing. A reused
PO keeps its original line quantity, so pass `--reset` when changing
`--po-quantity`.

### What each step does

1. Health check — aborts if the backend is not reachable.
2. Reset — empties 11 business tables in dependency order.
3. Seeds the `system@local` reviewer if missing and prints its UUID.
4. Uploads `build_fixture_pdf(FIXTURES[0])` twice; both share one sha256.
5. Runs `/analyze` on both invoices.
6. Creates `PO-DEMO-HIGHRISK` with a mismatched quantity and total.
7. Sets `invoice.po_number` directly — no API endpoint exists for this.
8. Runs `/match-po` and asserts the outcome is `mismatch`.
9. Runs `/check-duplicates` and asserts the verdict is `duplicate`.
10. Runs `/validate` (the duplicate invoice-number check fails here, +15).
11. Runs `/policy-check` — `skipped` on a fresh DB, worth +10.
12. Runs `/assess-risk`.
13. Reads `/risk`, verifies score, level, contributors, and the point sum.
14. Confirms a pending review case exists for invoice #2.

### Then

Open `http://localhost:5173/reviews` to work the case, or
`http://localhost:5173/invoices/{second_id}` for the full timeline.

## Testing note

The test suite expects an empty business database. Tests that assert on
counts (`test_list_invoices_empty` expects `total == 0`) will fail if any
pre-existing rows exist.

After running the demo script or any manual API calls, clear the business
tables before running `pytest`:

    docker exec invoice_intelligence_postgres psql -U invoice -d invoice_intelligence \
      -c "DELETE FROM analysis_results; DELETE FROM audit_events; DELETE FROM review_cases; DELETE FROM line_items; DELETE FROM invoices; DELETE FROM document_pages; DELETE FROM documents; DELETE FROM policy_chunks; DELETE FROM policy_documents; DELETE FROM purchase_order_items; DELETE FROM purchase_orders;"

(The `users` table is not cleared — the seed reviewer must survive.)