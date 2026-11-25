"""The one client to WealthContext. It holds the operations token and knows three read routes.

Each envelope is parsed into types here. Anything missing, cut short or inconsistent is
refused, so that no note is ever built from part of an answer.
"""

import base64
import json
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated, Any, Literal, Protocol, Self, get_args

import httpx2
from pydantic import BaseModel, ConfigDict, JsonValue, StringConstraints, ValidationError

from backend.settings import Settings
from backend.vocabulary import Custodian, FeedStatus, SourceFamily, WarningCode

PERSONA = "OPS-01"
ROLE = "ops"
Route = Literal["get_data_quality_report", "feed_runs", "feed_run_impact"]
ROUTES: tuple[Route, ...] = get_args(Route)
ErrorCode = Literal[
    "UNAUTHENTICATED",
    "NOT_AUTHORIZED",
    "NOT_FOUND",
    "INVALID_ARGUMENT",
    "AS_OF_OUT_OF_RANGE",
    "UNSUPPORTED_CURRENCY",
    "RESULT_TOO_LARGE",
    "POLICY_UNAVAILABLE",
    "INTERNAL",
]

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")]
Feed = Literal[
    "crm",
    "custody_accounts",
    "securities",
    "pas_snapshots",
    "custody_snapshots",
    "prices",
    "transactions",
    "lots",
    "fx",
]
SourceSystem = Literal["CRM", "PAS-A", "PAS-B", "CUSTODY-A", "CUSTODY-B", "FX", "SECMASTER"]

# The two feeds that name the accounts a failed run affected, and who sends each.
SNAPSHOT_SOURCES: dict[tuple[str, str], tuple[SourceFamily, Custodian]] = {
    ("pas_snapshots", "PAS-A"): ("PAS", "A"),
    ("pas_snapshots", "PAS-B"): ("PAS", "B"),
    ("custody_snapshots", "CUSTODY-A"): ("CUSTODY", "A"),
    ("custody_snapshots", "CUSTODY-B"): ("CUSTODY", "B"),
}
SNAPSHOT_FEEDS = frozenset(feed for feed, _ in SNAPSHOT_SOURCES)


class NotOperations(Exception):
    """The client was given something other than an operations sign-in."""


class WealthContextFailed(Exception):
    """WealthContext refused, is down, or gave an answer that cannot be used."""


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Flag(_Wire):
    code: WarningCode
    severity: Literal["info", "warning"]
    subject: Identifier
    # Read only to group. It can hold figures, so it is never stored or shown.
    detail: dict[str, JsonValue]


class _Listed[Item](_Wire):
    items: list[Item]
    item_count: int
    total_item_count: int
    truncated: bool

    def whole(self) -> bool:
        return not self.truncated and len(self.items) == self.item_count == self.total_item_count


class _Envelope[Data](_Wire):
    contract_version: Literal["1.0"]
    trace_id: Identifier
    result_id: Identifier
    status: Literal["COMPLETE", "INCOMPLETE"]
    as_of: date | None
    valuation_date: date
    known_as_of: datetime
    quality_flags: list[Flag]
    data: Data


class _HouseholdQuality(_Wire):
    flags: _Listed[Flag]


class _QualityData(_Wire):
    # Both lists here stop at twenty. The envelope's own list of flags does not.
    items: list[_HouseholdQuality]


class FeedRun(_Wire):
    id: Identifier
    source_system: SourceSystem
    feed: Feed
    business_date: date
    status: Literal["SUCCESS", "PARTIAL", "FAILED", "TIMEOUT"]

    def source(self) -> tuple[SourceFamily, Custodian] | None:
        return SNAPSHOT_SOURCES.get((self.feed, self.source_system))


class _Affected(_Wire):
    account_id: Identifier | None


class _ImpactData(_Wire):
    run_id: Identifier
    source_system: SourceSystem
    feed: Feed
    business_date: date
    status: FeedStatus
    account_attribution: Literal["MANIFEST", "NOT_ATTRIBUTABLE"]
    affected_accounts: _Listed[_Affected] | None


class Read(_Wire):
    """One read of WealthContext, as a triage receipt names it."""

    operation: Route
    result_id: Identifier
    trace_id: Identifier
    feed_run_id: Identifier | None


class Impact(_Wire):
    run: FeedRun
    affected_accounts: frozenset[str]


class Reading(_Wire):
    """Everything one triage run read, for one date and one cutoff."""

    reads: list[Read]
    flags: list[Flag]
    # Newest first, as WealthContext orders runs.
    impacts: list[Impact]


def _claims_of(token: str) -> dict:
    """The token's own claims. WealthContext checks the signature; this reads who it is for."""
    try:
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return {}
    return claims if isinstance(claims, dict) and header and signature else {}


class WealthContext:
    def __init__(self, settings: Settings):
        token = settings.wealthcontext_token
        claims = _claims_of(token)
        if claims.get("sub") != PERSONA or claims.get("role") != ROLE:
            raise NotOperations("WEALTHCONTEXT_TOKEN must be an operations sign-in (OPS-01).")
        self._http = httpx2.Client(
            base_url=settings.wealthcontext_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _read[Data](
        self, route: Route, arguments: dict[str, str], shape: type[_Envelope[Data]]
    ) -> _Envelope[Data]:
        try:
            response = self._http.post(f"/api/{route}", json=arguments)
        except httpx2.HTTPError:
            raise WealthContextFailed("WealthContext cannot be reached.") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200:
            error = body.get("error") if isinstance(body, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            known = code if code in get_args(ErrorCode) else f"HTTP {response.status_code}"
            raise WealthContextFailed(f"WealthContext refused {route} ({known}).")
        return _parsed(route, arguments, shape, body)

    def read_day(self, day: str, cutoff: str) -> Reading:
        """The quality report, the feed runs, and the impact of each run that did not succeed."""
        return _reading(day, cutoff, self._read)


def read_envelopes(day: str, cutoff: str, envelopes: Mapping[str, Any]) -> Reading:
    """A day read from envelopes already in hand, as a held-out case holds them.

    `envelopes` holds the quality report and the feed runs by route, and each impact under
    `feed_run_impact` by its run's ID. They are parsed and checked as WealthContext's are.
    """

    def given[Data](
        route: Route, arguments: dict[str, str], shape: type[_Envelope[Data]]
    ) -> _Envelope[Data]:
        body = envelopes.get(route)
        if route == "feed_run_impact":
            body = body.get(arguments["run_id"]) if isinstance(body, Mapping) else None
        if body is None:
            raise WealthContextFailed(f"The envelopes hold no {route} for that.")
        return _parsed(route, arguments, shape, body)

    return _reading(day, cutoff, given)


class _Fetch(Protocol):
    def __call__[Data](
        self, route: Route, arguments: dict[str, str], shape: type[_Envelope[Data]]
    ) -> _Envelope[Data]: ...


def _parsed[Data](
    route: Route, arguments: dict[str, str], shape: type[_Envelope[Data]], body: Any
) -> _Envelope[Data]:
    try:
        envelope = shape.model_validate(body)
    except ValidationError:
        raise WealthContextFailed(f"WealthContext's {route} was not in its shape.") from None
    if envelope.known_as_of != datetime.fromisoformat(arguments["known_as_of"]):
        raise WealthContextFailed(f"WealthContext's {route} is for another cutoff.")
    if "as_of" in arguments and envelope.as_of != date.fromisoformat(arguments["as_of"]):
        raise WealthContextFailed(f"WealthContext's {route} is for another date.")
    return envelope


def _reading(day: str, cutoff: str, read: _Fetch) -> Reading:
    asked = {"as_of": day, "known_as_of": cutoff}
    quality = read("get_data_quality_report", asked, _Envelope[_QualityData])
    listed = read("feed_runs", asked, _Envelope[_Listed[FeedRun]])
    if not _named(quality.quality_flags) >= _named(
        [flag for household in quality.data.items for flag in household.flags.items]
    ):
        raise WealthContextFailed("WealthContext's report leaves out a warning it lists.")
    if not listed.data.whole():
        raise WealthContextFailed("WealthContext cut its list of feed runs short.")
    if listed.valuation_date != quality.valuation_date:
        raise WealthContextFailed("WealthContext's reads are for different valuation dates.")
    reads = [
        _receipted("get_data_quality_report", quality, None),
        _receipted("feed_runs", listed, None),
    ]
    impacts = []
    for run in listed.data.items:
        if run.status == "SUCCESS":
            continue
        answer = read(
            "feed_run_impact",
            {"run_id": run.id, "known_as_of": cutoff},
            _Envelope[_ImpactData],
        )
        reads.append(_receipted("feed_run_impact", answer, run.id))
        impacts.append(Impact(run=run, affected_accounts=_affected(run, answer.data)))
    return Reading(reads=reads, flags=quality.quality_flags, impacts=impacts)


def _named(flags: list[Flag]) -> set[tuple[str, str, str]]:
    return {(flag.code, flag.severity, flag.subject) for flag in flags}


def _receipted(route: Route, envelope: _Envelope, run_id: str | None) -> Read:
    return Read(
        operation=route,
        result_id=envelope.result_id,
        trace_id=envelope.trace_id,
        feed_run_id=run_id,
    )


def _affected(run: FeedRun, data: _ImpactData) -> frozenset[str]:
    described = (data.run_id, data.source_system, data.feed, data.business_date, data.status)
    if described != (run.id, run.source_system, run.feed, run.business_date, run.status):
        raise WealthContextFailed("WealthContext's impact describes another feed run.")
    if run.feed not in SNAPSHOT_FEEDS:
        return frozenset()
    listed = data.affected_accounts
    if run.source() is None or data.account_attribution != "MANIFEST" or listed is None:
        raise WealthContextFailed("WealthContext's impact does not name the feed's accounts.")
    if not listed.whole():
        raise WealthContextFailed("WealthContext cut its list of affected accounts short.")
    return frozenset(item.account_id for item in listed.items if item.account_id)
