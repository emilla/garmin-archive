"""What to archive: each entry names a source and how to fetch it.

Daily sources are fetched once per calendar day. Snapshot sources describe the
account as a whole and are fetched on every sync. Adding a source here is all
that is needed to start archiving it.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from garminconnect import Garmin

DailyFetch = Callable[[Garmin, str], Any]

DAILY_SOURCES: dict[str, DailyFetch] = {
    "user_summary": lambda g, d: g.get_user_summary(d),
    "heart_rates": lambda g, d: g.get_heart_rates(d),
    "sleep": lambda g, d: g.get_sleep_data(d),
    "stress": lambda g, d: g.get_stress_data(d),
    "body_battery_events": lambda g, d: g.get_body_battery_events(d),
    "hrv": lambda g, d: g.get_hrv_data(d),
    "respiration": lambda g, d: g.get_respiration_data(d),
    "spo2": lambda g, d: g.get_spo2_data(d),
    "steps": lambda g, d: g.get_steps_data(d),
    "floors": lambda g, d: g.get_floors(d),
    "intensity_minutes": lambda g, d: g.get_intensity_minutes_data(d),
    "training_readiness": lambda g, d: g.get_training_readiness(d),
    "training_status": lambda g, d: g.get_training_status(d),
    "max_metrics": lambda g, d: g.get_max_metrics(d),
    "hydration": lambda g, d: g.get_hydration_data(d),
    "weigh_ins": lambda g, d: g.get_daily_weigh_ins(d),
    "blood_pressure": lambda g, d: g.get_blood_pressure(d, d),
    "nutrition_food_log": lambda g, d: g.get_nutrition_daily_food_log(d),
}

# Not fetched unless named with --sources or GARMIN_ARCHIVE_SOURCES. They
# return nothing unless the matching feature is used in Garmin Connect.
OPTIONAL_DAILY_SOURCES: dict[str, DailyFetch] = {
    "menstrual": lambda g, d: g.get_menstrual_data_for_date(d),
    "lifestyle_logging": lambda g, d: g.get_lifestyle_logging_data(d),
}

ALL_DAILY_SOURCES = {**DAILY_SOURCES, **OPTIONAL_DAILY_SOURCES}


def _gear(g: Garmin) -> Any:
    profile_number = (g.get_device_last_used() or {}).get("userProfileNumber")
    return g.get_gear(profile_number) if profile_number else None


SNAPSHOT_SOURCES: dict[str, Callable[[Garmin], Any]] = {
    "user_profile": lambda g: g.get_user_profile(),
    "userprofile_settings": lambda g: g.get_userprofile_settings(),
    "devices": lambda g: g.get_devices(),
    "gear": _gear,
    "personal_records": lambda g: g.get_personal_record(),
    "goals": lambda g: g.get_goals("active"),
    "earned_badges": lambda g: g.get_earned_badges(),
}


def select_daily_sources(names: str | None) -> dict[str, DailyFetch]:
    """Resolve a comma-separated list (or None for the defaults)."""
    if not names:
        return dict(DAILY_SOURCES)
    selected = {}
    for name in (n.strip() for n in names.split(",")):
        if not name:
            continue
        if name == "default":
            selected.update(DAILY_SOURCES)
        elif name in ALL_DAILY_SOURCES:
            selected[name] = ALL_DAILY_SOURCES[name]
        else:
            known = ", ".join(sorted(ALL_DAILY_SOURCES))
            raise ValueError(f"unknown source '{name}'. Known sources: {known}")
    return selected


def iso(day: date) -> str:
    return day.isoformat()
