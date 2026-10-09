# Backend

The FastAPI backend for the [Invoice Intelligence & Compliance Platform](../README.md).
Setup, architecture, and the full project overview are in the [root README](../README.md). This file covers backend-specific notes.

## Module layout

```
app/
├── main.py            — app factory, mounts v1 routers
├── api/v1/            — HTTP routes (thin: validate, delegate, serialize)
│   └── router.py      — aggregates the v1 sub-routers
├── core/              — configuration and logging
├── db/                — engine, session, seed
│   └── init.py        — seed_system_user
├── models/            — SQLAlchemy models
├── schemas/           — Pydantic schemas (HTTP responses, extraction contracts)
└── services/          — business logic, no FastAPI imports
    ├── llm/                  — LLMProvider protocol + hybrid mock
    ├── embeddings/           — EmbeddingProvider protocol + hash mock
    ├── validation/           — pure check functions + runner
    ├── po_matching/          — pure matcher
    ├── duplicate_detection/  — pure signals + scorer
    ├── anomaly_detection/    — features + statistical + Isolation Forest
    ├── risk/                 — pure engine + weights
    └── review/               — review decision types

alembic/               — migrations (versions/ holds the revisions)
tests/                 — pytest
scripts/               — demo scripts (see scripts/README.md)
```

The subpackages under `services/` are pure logic and are imported by the thin
top-level service modules of the same concern (for example `risk_service.py`
wraps `risk/`). Settings come from the environment and a `.env` file in this
directory (see `.env.example`).

Note: `services/review/` exists in the actual tree and is not in the original layout — it holds the review decision/resolution types.

## Running tests

```bash
pytest -v
ruff check .
```

Each test runs inside a transaction savepoint that is rolled back afterwards, so tests rely on that rollback rather than cleaning up explicitly.

The test suite expects an empty business database. Tests that assert on
counts (`test_list_invoices_empty` expects `total == 0`) will fail if
pre-existing rows exist.

After running the demo script or any manual API calls, clear the business
tables before running `pytest`:

    docker exec invoice_intelligence_postgres psql -U invoice -d invoice_intelligence \
      -c "DELETE FROM analysis_results; DELETE FROM audit_events; DELETE FROM review_cases; DELETE FROM line_items; DELETE FROM invoices; DELETE FROM document_pages; DELETE FROM documents; DELETE FROM policy_chunks; DELETE FROM policy_documents; DELETE FROM purchase_order_items; DELETE FROM purchase_orders;"

(The `users` table is not cleared — the seed reviewer must survive.)

## Seeding the system reviewer

Alembic creates the schema but seeds no rows, and the seed is required for the review workflow to function. Run it once after `alembic upgrade head`.

The seed is idempotent — running it twice will not create a second reviewer.

```bash
python -c "from app.db.session import SessionLocal; from app.db.init import seed_system_user; s = SessionLocal(); seed_system_user(s); s.commit(); s.close()"
```

## Migrations

```bash
alembic upgrade head
alembic revision --autogenerate -m "describe the change"
```

Always review the generated migration before applying it — autogenerate cannot see
data-only changes.

## Demo

See [scripts/README.md](scripts/README.md) for the demo scripts and what they exercise.
