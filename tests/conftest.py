import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot.seed import load_seed_rows  # noqa: E402

RUN_DATE = "2026-10-22"   # Thursday, W43; last complete week W42; October = festival


@pytest.fixture(autouse=True)
def zoho_off_by_default(monkeypatch):
    """Tests never touch live Zoho: the flag is off unless a test turns it on."""
    monkeypatch.setenv("ZOHO_ENABLED", "false")


@pytest.fixture
def seed_rows():
    return load_seed_rows()


@pytest.fixture
def seed_tables(seed_rows):
    return {t: pd.DataFrame(rows) for t, rows in seed_rows.items()}
