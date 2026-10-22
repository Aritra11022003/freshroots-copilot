"""ISO week helpers. Sales weeks are 'YYYY-Www' (Monday start)."""
from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def week_monday(week: str) -> date | None:
    m = WEEK_RE.match(str(week).strip())
    if not m:
        return None
    try:
        return date.fromisocalendar(int(m[1]), int(m[2]), 1)
    except ValueError:
        return None


def week_label(monday: date) -> str:
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}"


def last_complete_week(run_date) -> date:
    """Monday of the last full week before run_date (the in-progress week is ignored)."""
    d = pd.Timestamp(run_date).date()
    return d - timedelta(days=d.weekday()) - timedelta(weeks=1)


def week_range(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(weeks=1)
    return out


def year_month(d) -> str:
    d = pd.Timestamp(d)
    return f"{d.year}-{d.month:02d}"
