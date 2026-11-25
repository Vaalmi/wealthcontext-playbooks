"""The time, and the one way an instant is written."""

from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now(UTC)


def stamp(instant: datetime) -> str:
    # Always to the microsecond, so that stamps sort as the instants they name.
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
