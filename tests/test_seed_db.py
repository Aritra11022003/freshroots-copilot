"""[C2] seed is idempotent; [C6] FakeDB mirrors the Supabase interface; fetch_all pages."""
from copilot.db import FakeDB, SupabaseDB
from copilot.seed import seed


def test_seed_loads_all_tables_and_is_idempotent():
    db = FakeDB()
    counts = seed(db)
    assert counts == {"skus": 15, "distributors": 4, "calendar": 24, "inventory": 15,
                      "weekly_sales": 240, "assumptions": 6}
    db.update("assumptions", {"signed_off_by": "raghav"}, {"key": "festival_lookahead_weeks"})
    seed(db)                                          # re-run: no dupes, sign-off kept
    assert len(db.tables["weekly_sales"]) == 240
    a = db.fetch_all("assumptions", {"key": "festival_lookahead_weeks"})
    assert a.iloc[0]["signed_off_by"] == "raghav"


def test_seed_types(seed_rows):
    cal = {r["year_month"]: r["is_festival_month"] for r in seed_rows["calendar"]}
    assert cal["2026-10"] is True and cal["2026-09"] is False
    assert isinstance(seed_rows["weekly_sales"][0]["units_sold"], int)


def test_fetch_all_pages_until_empty():
    """2,500 rows at a 1,000-row cap -> 3 full/partial pages + 1 empty page."""
    rows = [{"id": i} for i in range(2500)]
    calls = []

    class Q:
        def __init__(self): self.f = []
        def select(self, *_): return self
        def eq(self, *a): return self
        def order(self, *_): return self
        def range(self, a, b): self.a, self.b = a, b; return self
        def execute(self):
            calls.append((self.a, self.b))
            return type("R", (), {"data": rows[self.a:self.b + 1]})

    db = SupabaseDB.__new__(SupabaseDB)
    db.client = type("C", (), {"table": lambda self, t: Q()})()
    df = db.fetch_all("runs")
    assert len(df) == 2500
    assert calls == [(0, 999), (1000, 1999), (2000, 2999), (2500, 3499)]
