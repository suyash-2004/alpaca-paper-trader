from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
BAR_MIN = 5


def to_et(ts: datetime) -> datetime:
    return ts.astimezone(ET)


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def session_open(day) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=ET)


def bar_end(ts: datetime) -> datetime:
    return ts + timedelta(minutes=BAR_MIN)
