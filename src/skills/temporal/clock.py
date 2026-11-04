"""What time it is. A local model's own sense of "now" is the date its training data ends, so planning is told today's
date, with a time zone, explicitly. The clock can be replaced (tests, replays) and the zone set with TITAN_TIMEZONE."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config


@dataclass(frozen=True)
class Now:
    local: datetime            # timezone-aware, in the configured (or system) zone
    utc: datetime
    zone: str                  # a name such as "Asia/Kolkata", or "system" when the computer's own zone is used

    @property
    def date(self) -> date:
        return self.local.date()

    @property
    def year(self) -> int:
        return self.local.year

    @property
    def quarter(self) -> int:
        return (self.local.month - 1) // 3 + 1

    def offset_label(self) -> str:
        offset = self.local.utcoffset()
        minutes = int(offset.total_seconds() // 60) if offset is not None else 0
        sign = "+" if minutes >= 0 else "-"
        hours, mins = divmod(abs(minutes), 60)
        return f"UTC{sign}{hours:02d}:{mins:02d}"

    def describe(self) -> str:
        """One line for a prompt: weekday, date, time, zone and calendar quarter."""
        abbrev = self.local.tzname() or ""
        abbrev = abbrev if 0 < len(abbrev) <= 5 and " " not in abbrev else ""       # Windows gives long names; skip those
        shown = f"{self.local:%H:%M} {abbrev} ({self.offset_label()})".replace("  ", " ")
        return f"{self.local:%Y-%m-%d} ({self.local:%A}), {shown}; calendar quarter Q{self.quarter} {self.year}"

    def as_dict(self) -> dict:
        return {"local": self.local.isoformat(timespec="seconds"), "utc": self.utc.isoformat(timespec="seconds"), "zone": self.zone}


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


def zone_problem(name: str) -> str | None:
    """None if `name` is empty (use the system zone) or a real IANA zone, else a message for the configuration check."""
    if not name:
        return None
    try:
        ZoneInfo(name)
        return None
    except (ZoneInfoNotFoundError, ValueError):
        return f"TITAN_TIMEZONE={name!r} is not a time zone name (use for example Asia/Kolkata or America/New_York)"


def now(clock: Callable[[], datetime] | None = None, zone: str | None = None) -> Now:
    """The current moment. `clock` returns an aware datetime; `zone` overrides TITAN_TIMEZONE. An unusable zone falls back
    to the computer's own, so planning never fails over a typo (the configuration check reports it)."""
    utc = (clock or _system_clock)()
    if utc.tzinfo is None:
        raise ValueError("the clock must return a timezone-aware datetime")
    utc = utc.astimezone(timezone.utc)
    name = config.TIMEZONE if zone is None else zone
    if name and zone_problem(name) is None:
        return Now(utc.astimezone(ZoneInfo(name)), utc, name)
    return Now(utc.astimezone(), utc, "system")
