"""Turns what was read from WealthContext into exceptions and informational notices."""

from typing import cast

from pydantic import BaseModel, ConfigDict

from backend.vocabulary import FeedFacts, FeedStatus, SourceFamily, WarningCode
from backend.wealthcontext import Flag, Impact, Reading

# Which warning a failed feed produces, by the feed's source family.
PRODUCED: dict[SourceFamily, WarningCode] = {
    "PAS": "SOURCE_UNAVAILABLE",
    "CUSTODY": "RECONCILIATION_UNAVAILABLE",
}


class _Found(BaseModel):
    model_config = ConfigDict(frozen=True)


class FeedRunRef(_Found):
    id: str
    source_system: str
    feed: str
    status: FeedStatus


class ExceptionFound(_Found):
    code: WarningCode
    subjects: list[str]
    facts: FeedFacts | None
    feed_run: FeedRunRef | None


class Notice(_Found):
    code: WarningCode
    subject: str


def _produced_by(flag: Flag, impact: Impact) -> bool:
    source = impact.run.source()
    if source is None:
        return False
    family, _ = source
    return (
        flag.subject in impact.affected_accounts
        and flag.code == PRODUCED[family]
        and flag.detail.get("source") == family
    )


def build(reading: Reading) -> tuple[list[ExceptionFound], list[Notice]]:
    """One exception for each failed feed's warnings, one for each other warning.

    Every warning lands in exactly one exception. Runs come newest first, so a warning
    that two runs of one feed could explain goes to the latest.
    """
    notices = [
        Notice(code=flag.code, subject=flag.subject)
        for flag in reading.flags
        if flag.severity == "info"
    ]
    left = [flag for flag in reading.flags if flag.severity == "warning"]
    found: list[ExceptionFound] = []
    for impact in reading.impacts:
        source = impact.run.source()
        if source is None:
            continue
        mine = [flag for flag in left if _produced_by(flag, impact)]
        if not mine:
            continue
        left = [flag for flag in left if not _produced_by(flag, impact)]
        family, custodian = source
        status = cast(FeedStatus, impact.run.status)  # only runs that failed have an impact
        found.append(
            ExceptionFound(
                code=PRODUCED[family],
                subjects=[flag.subject for flag in mine],
                facts=FeedFacts(source_family=family, custodian=custodian, feed_status=status),
                feed_run=FeedRunRef(
                    id=impact.run.id,
                    source_system=impact.run.source_system,
                    feed=impact.run.feed,
                    status=status,
                ),
            )
        )
    found.extend(
        ExceptionFound(code=flag.code, subjects=[flag.subject], facts=None, feed_run=None)
        for flag in left
    )
    return found, notices
