"""Engine config (config.json) and the 6 assumptions Raghav must sign off."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


@dataclass
class Config:
    short_window_weeks: int = 4
    long_window_weeks: int = 12
    high_demand_ratio: float = 1.20
    high_demand_multiplier: float = 1.5
    normal_min_weeks: float = 6
    normal_max_weeks: float = 14
    perishable_min_weeks: float = 3
    perishable_max_weeks: float = 4
    high_perishability_values: list = field(default_factory=lambda: ["High"])
    known_perishability_values: list = field(default_factory=lambda: ["High", "Low"])
    festival_uplift: float = 0.25
    festival_lookahead_weeks: int = 0
    festival_uplift_applies_to_high_demand_check: bool = False
    include_on_order_in_cover: bool = True
    min_history_weeks: int = 12
    distributor_drop_threshold: float = 0.30
    distributor_baseline_weeks: int = 8
    distributor_min_share: float = 0.05
    standard_qty_default_weeks: int = 4
    standard_qty_default_round_to: int = 10
    override_reason_codes: list = field(default_factory=lambda: ["OTHER"])
    backtest_gate: float = 0.80
    backtest_min_history_weeks: int = 16

    @classmethod
    def load(cls, path: str | Path = CONFIG_PATH) -> "Config":
        raw = {k: v for k, v in json.loads(Path(path).read_text()).items() if not k.startswith("_")}
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown config keys: {sorted(unknown)}")
        return cls(**raw)


# The six assumptions on the Sign-off page. Values are derived from config.json, so a
# change goes through code review; a changed value clears its sign-off (see sync_assumptions).
ASSUMPTION_HELP = {
    "include_on_order_in_cover":
        "Stock used for weeks-of-cover includes stock already on order. Today there is no "
        "on-order feed, so on_order = 0 for every SKU (DEFAULT).",
    "festival_uplift_applies_to_high_demand_check":
        "If false, the +25% festival uplift only raises expected demand (cover). The 4w-vs-12w "
        "high-demand check uses raw sales.",
    "festival_lookahead_weeks":
        "Weeks added to the run date before looking up the festival month in the calendar. "
        "0 = the run date's own month.",
    "standard_qty_default_rule":
        "Only used with --allow-defaults, for SKUs missing a standard quantity. Every defaulted "
        "SKU goes to the exception queue.",
    "distributor_drop_threshold":
        "Exception guard. Skipped today: sales are SKU-level, not distributor-level.",
    "perishability_source":
        "Where high-perishability comes from and which cover window it gets.",
}


def assumption_values(cfg: Config) -> dict[str, str]:
    return {
        "include_on_order_in_cover": str(cfg.include_on_order_in_cover).lower(),
        "festival_uplift_applies_to_high_demand_check":
            str(cfg.festival_uplift_applies_to_high_demand_check).lower(),
        "festival_lookahead_weeks": str(cfg.festival_lookahead_weeks),
        "standard_qty_default_rule":
            f"{cfg.standard_qty_default_weeks} x 12-week average, rounded up to the nearest "
            f"{cfg.standard_qty_default_round_to}",
        "distributor_drop_threshold":
            f"drop >= {cfg.distributor_drop_threshold:.0%} vs own {cfg.distributor_baseline_weeks}-week "
            f"average, distributor share >= {cfg.distributor_min_share:.0%}",
        "perishability_source":
            f"skus.perishability: {'/'.join(cfg.high_perishability_values)} = "
            f"{cfg.perishable_min_weeks:g}-{cfg.perishable_max_weeks:g} wk window, otherwise "
            f"{cfg.normal_min_weeks:g}-{cfg.normal_max_weeks:g} wk",
    }
