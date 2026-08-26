"""US equity-options session calendar — deterministic, offline, no tz database.

Why this exists (2026-08-26 adversarial review). Both scheduled tasks fire on
`/SC WEEKLY /D MON,TUE,WED,THU,FRI` and nothing downstream ever asked whether
the market was actually open. Three ways that bites inside a week-long run:

  * a market holiday on a weekday (2026-09-07 Labor Day is the first one after
    the contest window; the tasks keep firing long after the judges leave),
  * an early close at 13:00 ET, which puts the 12:15 PT close-check round two
    hours after the bell, pricing an unwind off a dead tape,
  * any box not in US Pacific, where the hardcoded /ST values mean something
    else entirely.

The broker's own `/v2/clock` is authoritative and is preferred when reachable
(it also knows about unscheduled closures). This module is the fallback, so a
flaky clock endpoint costs a retry rather than the whole competition.

No `zoneinfo`: Windows ships no system tz database and `tzdata` is not a
dependency of this project. US DST has been second-Sunday-March to
first-Sunday-November since 2007, which is two lines of arithmetic.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

# NYSE / OCC full closures. Two years so the agent does not silently lose the
# calendar the moment it outlives the contest.
HOLIDAYS_2026 = (
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; the 4th is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas Day
)
HOLIDAYS_2027 = (
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
)
HOLIDAYS = frozenset(HOLIDAYS_2026 + HOLIDAYS_2027)

# 13:00 ET closes.
EARLY_CLOSES_2026 = (
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve (a Thursday in 2026)
)
EARLY_CLOSES_2027 = (date(2027, 11, 26),)
EARLY_CLOSES = frozenset(EARLY_CLOSES_2026 + EARLY_CLOSES_2027)

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

#: US Pacific is always exactly three hours behind US Eastern — both zones
#: switch on the same instant — which is the assumption baked into the /ST
#: values in scripts/register_task.cmd. Asserted by the test suite.
PT_BEHIND_ET = timedelta(hours=3)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th `weekday` (Mon=0) of the month, 1-based."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def et_offset(day: date) -> timedelta:
    """UTC offset of US Eastern on `day` (-4 EDT / -5 EST).

    Date granularity on purpose: both switch instants are at 02:00 local,
    hours before the 09:30 open, so no trading minute is ever ambiguous.
    """
    start = _nth_weekday(day.year, 3, 6, 2)     # 2nd Sunday in March
    end = _nth_weekday(day.year, 11, 6, 1)      # 1st Sunday in November
    return timedelta(hours=-4) if start < day < end else timedelta(hours=-5)


def pt_offset(day: date) -> timedelta:
    return et_offset(day) - PT_BEHIND_ET


def et_datetime(day: date, hh: int, mm: int = 0) -> datetime:
    """An ET wall-clock time on `day`, as an aware UTC datetime."""
    naive = datetime(day.year, day.month, day.day, hh, mm)
    return (naive - et_offset(day)).replace(tzinfo=timezone.utc)


def to_et(when: datetime) -> datetime:
    """Aware (or naive-UTC) instant -> naive ET wall clock."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    utc = when.astimezone(timezone.utc).replace(tzinfo=None)
    return utc + et_offset(utc.date())


def is_holiday(day: date) -> bool:
    return day in HOLIDAYS


def is_early_close(day: date) -> bool:
    return day in EARLY_CLOSES


def session_close(day: date) -> time:
    return EARLY_CLOSE if is_early_close(day) else REGULAR_CLOSE


def session_state(when: datetime | None = None) -> tuple[bool, str]:
    """(is_open, why) for an instant, from the built-in calendar.

    `when` is an aware datetime (or naive = UTC). Default: now.
    """
    when = when or datetime.now(timezone.utc)
    et = to_et(when)
    day, clock = et.date(), et.time()

    if day.weekday() >= 5:
        return False, f"calendar: {day.isoformat()} is a weekend"
    if is_holiday(day):
        return False, f"calendar: {day.isoformat()} is a market holiday"
    close = session_close(day)
    if clock < REGULAR_OPEN:
        return False, (f"calendar: {clock.strftime('%H:%M')} ET is before the "
                       f"09:30 open")
    if clock >= close:
        tag = "early close" if is_early_close(day) else "close"
        return False, (f"calendar: {clock.strftime('%H:%M')} ET is after the "
                       f"{close.strftime('%H:%M')} {tag}")
    return True, (f"calendar: {clock.strftime('%H:%M')} ET inside the "
                  f"09:30-{close.strftime('%H:%M')} session")


def clock_state(clock: dict | None) -> tuple[bool, str] | None:
    """(is_open, why) from Alpaca's /v2/clock payload, or None if unusable."""
    if not isinstance(clock, dict) or "is_open" not in clock:
        return None
    if clock.get("is_open"):
        return True, f"clock: market open (broker ts {clock.get('timestamp', '?')})"
    return False, (f"clock: market closed (broker ts "
                   f"{clock.get('timestamp', '?')}, next open "
                   f"{clock.get('next_open', '?')})")
