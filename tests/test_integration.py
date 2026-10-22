"""Optional: read-only checks against the real Supabase project. Skipped unless the env vars are set."""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
    reason="SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")


def test_seed_tables_readable_and_valid():
    from copilot.db import get_db
    from copilot.pipeline import SEED_TABLES, today_ist
    from copilot.validate import validate
    db = get_db()
    tables = {t: db.fetch_all(t) for t in SEED_TABLES}
    assert len(tables["skus"]) > 0
    assert len(db.fetch_all("assumptions")) == 6
    validate(tables, today_ist())          # must not raise; gaps (if any) are data, not bugs
