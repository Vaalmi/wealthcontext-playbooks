"""The API's models. What comes in is closed: no unknown field, and no text that is unchecked.

Four inputs are text: a memo, a correction's reason, a gap report's summary and a
proposal. Each is checked by `personal_data` before any of it is kept.
"""

import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from backend import personal_data
from backend.exceptions import FeedRunRef, Notice
from backend.playbook_format import By, Scope, Slug, When
from backend.store import RecordId, RetireReason, Ruling, Sha256, VersionInUse
from backend.vocabulary import FeedFacts, WarningCode
from backend.wealthcontext import Read

RunState = Literal["working", "completed", "failed"]
NoteState = Literal["pending", "chosen", "no_step", "unverified"]
ToolName = Literal["choose_step", "no_step_applies", "file_gap_report", "no_gap_report", "other"]
CorrectionState = Literal["recorded", "reviewed"]
ReviewOutcome = Literal["filed", "none", "refused_twice"]
GapReportState = Literal["open", "cited", "resolved", "dismissed"]
# A checked proposal was handed in to be checked, and is final: it can never be approved.
ProposalState = Literal[
    "drafted", "gate_failed", "gate_passed", "approved", "stale", "refused", "checked"
]
# What ended a proposal, by the playbook owner's move. Stale: an approval that was refused.
DecisionKind = Literal["approved", "refused", "stale"]
CheckName = Literal["support", "personal", "guardrails", "base", "overlap", "held_out"]
CuratorRunState = Literal["working", "completed", "completed_no_change", "failed"]
CuratorTool = Literal["submit_proposal", "no_change", "other"]
CuratorFailure = Literal[
    "refused_twice",
    "model_failed",
    "cost_cap",
    "recording_mismatch",
    "backend_refused",
    "unexpected_error",
]
# A closed list, as for a retirement's reason: the ledger is permanent.
DismissReason = Literal["not_a_gap", "duplicate", "already_covered", "out_of_scope"]
RefuseReason = Literal["wrong_change", "against_the_sources", "source_not_trusted", "not_needed"]
UnusableReason = Literal[
    "no_tool_call", "several_tool_calls", "unknown_tool", "invalid_arguments", "no_answer"
]
FailureReason = Literal[
    "model_failed", "cost_cap", "recording_mismatch", "backend_refused", "unexpected_error"
]
# Each restart spends a number, failed ones too, so the bound is far beyond a person's use.
MAX_ATTEMPTS = 1_000_000
Attempt = Annotated[int, Field(ge=1, le=MAX_ATTEMPTS)]
MAX_VERSION = 1_000_000
Version = Annotated[int, Field(ge=1, le=MAX_VERSION)]
# Every call in a reply is recorded. A reply is capped at 4096 tokens, so it holds fewer.
MAX_CALLS_IN_A_REPLY = 4096
# Far above each text's own bound, so that a text too long is refused by that rule, with a
# line in the record, and a model's reply never falls outside its operation.
MAX_TEXT = 100_000
Text = Annotated[str, StringConstraints(max_length=MAX_TEXT)]

_CUTOFF = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})"
)


def _day(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _cutoff(value: str) -> str:
    if not _CUTOFF.fullmatch(value):
        raise ValueError("not an instant")
    try:
        instant = datetime.fromisoformat(value).astimezone(UTC)
    except OverflowError:
        raise ValueError("not an instant") from None
    return instant.isoformat().replace("+00:00", "Z")


Day = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_day)
]
Cutoff = Annotated[str, StringConstraints(max_length=40), AfterValidator(_cutoff)]


class In(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class StartTriage(In):
    date: Day
    cutoff: Cutoff


class Retire(In):
    reason: RetireReason


class Intake(In):
    content: Text


class CorrectTell(In):
    field: Literal["tell"]
    should_be: Slug


class CorrectBy(In):
    field: Literal["by"]
    should_be: By


class CorrectHold(In):
    field: Literal["hold_report"]
    should_be: StrictBool


Change = Annotated[CorrectTell | CorrectBy | CorrectHold, Field(discriminator="field")]


class RecordCorrection(In):
    change: Change
    reason: Text


class Dismiss(In):
    reason: DismissReason


class Refuse(In):
    reason: RefuseReason


def _model_name(value: str) -> str:
    # The whole check, with no exception for digits: none is safe in a permanent record.
    if personal_data.broken_rule(value, personal_data.MODEL_NAME_LENGTH):
        raise ValueError("not a model's name")
    return value


ModelName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z][a-z0-9_]{0,23}:[A-Za-z0-9][A-Za-z0-9._:/-]{0,71}"
        r"( \((scripted|recorded)\))?$"
    ),
    AfterValidator(_model_name),
]


class Usage(In):
    # The model's setting as the agent built it, never a word a model wrote. It is written
    # to the ledger with a gap report's step, so it is one `provider:name`, or a recording's
    # with how it was made, and it passes the checks on text like any other.
    model: ModelName
    input_tokens: Annotated[StrictInt, Field(ge=0, le=100_000_000)] | None
    output_tokens: Annotated[StrictInt, Field(ge=0, le=100_000_000)] | None
    cost_usd: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,6}\.[0-9]{6}$")] | None


Turn = Annotated[StrictInt, Field(ge=1, le=2)]


class StepChoice(In):
    kind: Literal["step"]
    playbook: Slug
    step: Slug


class NoStepChoice(In):
    kind: Literal["no_step"]


Choice = Annotated[StepChoice | NoStepChoice, Field(discriminator="kind")]


class RecordChoice(In):
    turn: Turn
    choice: Choice
    usage: Usage


class UnusableReply(In):
    turn: Turn
    reason: UnusableReason
    calls: Annotated[list[ToolName], Field(max_length=MAX_CALLS_IN_A_REPLY)]
    usage: Usage


class FailTriage(In):
    reason: FailureReason


class FileGapReport(In):
    turn: Turn
    summary: Text
    usage: Usage


class NoGapReport(In):
    turn: Turn
    usage: Usage


class ReviewFiles(In):
    kind: Literal["gap_report"]
    summary: Text


class ReviewFilesNone(In):
    kind: Literal["no_gap_report"]


Review = Annotated[ReviewFiles | ReviewFilesNone, Field(discriminator="kind")]


class RecordReview(In):
    turn: Turn
    review: Review
    usage: Usage


class StopReview(In):
    reason: FailureReason


# An account number has six digits or more, so no number a model writes may have as many.
ProposedVersion = Annotated[StrictInt, Field(ge=1, le=99_999)]
# A reply's bound, as for its calls. What a proposal may say is checked after it is parsed,
# so that each refusal is one line the model reads and a step on the run.
Names = Annotated[list[Text], Field(max_length=MAX_CALLS_IN_A_REPLY)]


class AddStep(In):
    change: Literal["add"]
    playbook: Text
    # None: the playbook is a new one.
    version: ProposedVersion | None
    step: Text
    code: WarningCode
    when: When
    tell: Text
    by: Text
    hold_report: StrictBool
    line: Text
    rests_on: Names


class ChangeStep(In):
    change: Literal["change"]
    playbook: Text
    version: ProposedVersion
    step: Text
    code: WarningCode
    when: When
    tell: Text
    by: Text
    hold_report: StrictBool
    line: Text
    rests_on: Names


class RemoveStep(In):
    change: Literal["remove"]
    playbook: Text
    version: ProposedVersion
    step: Text
    rests_on: Names


StepChange = Annotated[AddStep | ChangeStep | RemoveStep, Field(discriminator="change")]


class Disagreement(In):
    between: Names
    about: Text


class Proposed(In):
    changes: Annotated[list[StepChange], Field(max_length=MAX_CALLS_IN_A_REPLY)]
    disagreements: Annotated[list[Disagreement], Field(max_length=MAX_CALLS_IN_A_REPLY)]


class RecordProposal(In):
    turn: Turn
    proposal: Proposed
    usage: Usage


class NoChange(In):
    turn: Turn
    usage: Usage


class UnusableCuratorReply(In):
    turn: Turn
    reason: UnusableReason
    calls: Annotated[list[CuratorTool], Field(max_length=MAX_CALLS_IN_A_REPLY)]
    usage: Usage


class FailCuratorRun(In):
    reason: CuratorFailure


class Health(Out):
    status: Literal["ok"]


class SetInUse(Out):
    id: str
    version: int
    hash: str
    in_use: list[VersionInUse]


class Started(Out):
    id: str
    attempt: int
    state: RunState
    asked: Literal["accepted", "unknown"]


class Moved(Out):
    state: RunState | NoteState


class Refused(Out):
    refusal: str


class Offered(Out):
    """A step as the triage agent is shown it. The action stays with the backend."""

    playbook: str
    step: str
    when: When
    line: str


class PendingNote(Out):
    """An exception as the triage agent is shown it: no subject, no result, no trace."""

    id: str
    code: WarningCode
    feed: FeedFacts | None
    warning_count: int
    options: list[Offered]
    # The line of each turn already refused on it. After a second, only the fallback is left.
    refusals: list[str]


class UncoveredNote(Out):
    """An exception no playbook covers, as the agent is asked whether to file a gap report."""

    id: str
    code: WarningCode
    feed: FeedFacts | None
    warning_count: int
    refusals: list[str]


class TriageWork(Out):
    state: RunState
    # What the run's earlier calls cost, as recorded, so that one cap holds over every attempt.
    spent_usd: str | None
    notes: list[PendingNote]
    uncovered: list[UncoveredNote]


class ReviewWork(Out):
    """A correction as the agent is shown it: what it says, and never the step's action."""

    state: CorrectionState
    spent_usd: str | None
    code: WarningCode
    feed: FeedFacts | None
    warning_count: int
    step: Offered
    version: int
    field: Literal["tell", "by", "hold_report"]
    should_be: bool | str
    reason: str
    refusals: list[str]


class ReceiptSet(Out):
    id: str
    version: int
    hash: str


class Receipt(Out):
    """A triage receipt: what traces a note back to WealthContext and to the playbooks."""

    date: str
    cutoff: str
    reads: list[Read]
    feed_run: FeedRunRef | None
    playbook_set: ReceiptSet


class NoteStep(Out):
    playbook: str
    version: int
    id: str
    # None: no approval is recorded for the version.
    approved_by: str | None


class Action(Out):
    tell: str
    desk: str
    by: str
    hold_report: bool
    line: str


class Note(Out):
    id: str
    code: WarningCode
    subjects: list[str]
    state: NoteState
    statement: str | None
    step: NoteStep | None
    action: Action | None
    receipt: Receipt


class StepTaken(Out):
    note_id: str
    attempt: int
    turn: int
    tool: ToolName | None
    option: str | None
    outcome: Literal["accepted", "refused"]
    line: str | None
    model: str


class UsageTotal(Out):
    models: list[str]
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: str | None


class TriageRun(Out):
    id: str
    attempt: int
    state: RunState
    stalled: bool
    failure: str | None
    date: str
    cutoff: str
    playbook_set: ReceiptSet
    started_at: str
    finished_at: str | None
    notes: list[Note]
    notices: list[Notice]
    steps: list[StepTaken]
    usage: UsageTotal


class Retired(Out):
    playbook: str
    version: int
    state: Literal["retired"]
    now_in_use: int | None
    playbook_set: ReceiptSet


class RecordEntry(Out):
    line: int
    at: str
    who: str
    event: str
    data: dict[str, JsonValue]
    hash: str
    playbook_set: ReceiptSet | None


class Failure(Out):
    """Why a check failed, on which change of the proposal. It repeats none of its text."""

    # None: the failure is the proposal's as a whole, or one of its disagreements'.
    change: int | None
    # The step as `playbook/step`, or None where the proposal's names could not be shown.
    step: str | None
    rule: str
    line: str


class CheckResult(Out):
    check: CheckName
    # not_run: the held-out trials, when an earlier check had already failed the proposal.
    outcome: Literal["passed", "failed", "not_run"]
    failures: list[Failure]


class HeldOutRecord(Out):
    """What the held-out check ran: the case set, the model, and every trial, misses too."""

    case_set_sha256: str
    writer: str
    # The setting the backend holds for the trials' model, and each model a trial's calls named.
    trial_model: str
    models: list[str]
    # Each case graded by fixed rules, with the playbooks in use and once the proposal is made.
    graded: list[dict[str, JsonValue]]
    trials: list[dict[str, JsonValue]]
    cost_usd: str


class GateRefusal(Out):
    """A gate that could not run, and why. The proposal stays as it was."""

    proposal_id: str
    at: str
    who: str
    reason: str
    line: str


class GateRun(Out):
    """One run of the checks on one proposal: the gate's, or a check of one handed in."""

    id: str
    kind: Literal["gate", "check"]
    proposal_id: str
    at: str
    who: str
    playbook_set: ReceiptSet
    bundle_sha256: str
    outcome: Literal["passed", "failed"]
    checks: list[CheckResult]
    # The pairs of bundle items that disagree about an outcome the proposal changes.
    disagreements: list[list[str]]
    # None for a check of a proposal handed in, which runs no held-out case.
    held_out: HeldOutRecord | None


class ListedProposal(Out):
    id: str
    state: ProposalState
    # None: handed in to be checked, and no curator's.
    run_id: str | None
    sha256: str
    recorded_at: str
    # None: it carried text the checks keep out of the record, so only its hash is kept.
    content: dict[str, JsonValue] | None
    gate_runs: list[GateRun]
    gate_refusals: list[GateRefusal]
    # None: it is not approved, refused or stale.
    decision: "Decision | None"


class CheckedProposal(Out):
    id: str
    state: Literal["checked"]
    sha256: str
    content_kept: bool
    gate_run: GateRun


class RuleFor(In):
    """The playbook owner's ruling on one disagreement, as `read_proposal` lists it."""

    change: Annotated[StrictInt, Field(ge=1, le=MAX_CALLS_IN_A_REPLY)] | None
    between: Annotated[list[Slug], Field(max_length=MAX_CALLS_IN_A_REPLY)]
    ruled_for: Slug


class Approve(In):
    sha256: Sha256
    gate_run: RecordId
    # One for each disagreement to rule on. Each is looked up among the proposal's, and only
    # a match is kept.
    rulings: Annotated[list[RuleFor], Field(max_length=MAX_CALLS_IN_A_REPLY)] = []


class ApproveFounding(In):
    # The founding line's hash: what the owner read, named by the hash `read_founding` shows.
    sha256: Sha256


class FoundingMemoShown(Out):
    id: str
    sha256: str
    # Whether the memo here is the one the founding line names, by the hash of its bytes.
    as_named: bool
    # None: no memo with that ID is here.
    words: str | None


class FoundingVersionShown(Out):
    id: str
    version: int
    sha256: str
    state: str
    as_named: bool
    # The version's file whole, as the owner approves it.
    text: str
    rests_on: list[FoundingMemoShown]


class DesksShown(Out):
    sha256: str
    as_named: bool
    text: str


class FoundingShown(Out):
    """The founding versions as the playbook owner reads them before approving."""

    # The founding line's hash: what an approval names.
    hash: str
    # None: nobody has approved them.
    approved_by: str | None
    approved_at: str | None
    versions: list[FoundingVersionShown]
    desks: DesksShown


class FoundingApproved(Out):
    line: int
    versions: list[VersionInUse]
    playbook_set: ReceiptSet


class Approved(Out):
    proposal_id: str
    state: Literal["approved"]
    line: int
    versions: list[VersionInUse]
    rulings: list[Ruling]
    resolved: list[str]
    playbook_set: ReceiptSet


class ProposalRefused(Out):
    proposal_id: str
    state: Literal["refused"]
    line: int
    reason: RefuseReason
    reopened: list[str]


class Cited(Out):
    """A source as a proposal cites it, with its exact words."""

    reference: str
    kind: Literal["memo", "correction", "gap_report"]
    sha256: str
    words: str
    desk: str | None
    date: str | None
    scope: Scope | Literal["none"] | None
    correction: dict[str, JsonValue] | None


class Disputed(Out):
    """A changed outcome that is disputed, as the gate found it for one change."""

    change: int
    # The pairs that disagree there by the rules. None, where only the proposal lists one.
    pairs: list[list[str]]
    speaking: list[str]
    # What that change rests on that covers the outcome.
    supported_by: list[str]


class ToRuleOn(Out):
    change: int | None
    between: list[str]


class StepBefore(Out):
    """A step in the version a change applies to. With no recorded approval, no action."""

    version: int
    approval_recorded: bool
    code: WarningCode
    when: When
    tell: str | None
    desk: str | None
    by: str | None
    hold_report: bool | None
    line: str


class ChangeShown(Out):
    change: StepChange
    # The desk's name for the step's tell. None for a removal.
    desk: str | None
    before: StepBefore | None
    cites: list[Cited]


class DisagreementShown(Out):
    between: list[str]
    about: str
    sources: list[Cited]


class Evidence(Out):
    """A gate run's results. All none, and no check: the gate did not run."""

    gate_run: str | None
    outcome: Literal["passed", "failed"] | None
    playbook_set: ReceiptSet | None
    checks: list[CheckResult]


class ProposalShown(Out):
    """What approving would change, next to the exact words it rests on."""

    id: str
    state: ProposalState
    sha256: str
    gate_run: Evidence
    changes: list[ChangeShown]
    disagreements: list[DisagreementShown]
    found_disagreements: list[list[str]]
    # What an approval's ruling must settle.
    to_rule_on: list[ToRuleOn]


class RestedOn(Out):
    reference: str
    kind: Literal["memo", "correction", "gap_report"]
    sha256: str


class WhatChanged(Out):
    # What was changed, or for a refusal what would have been.
    changes: Annotated[list[StepChange], Field(min_length=1)]
    # The versions put in use. None for a refusal: it changes no playbook.
    versions: list[VersionInUse]


class Why(Out):
    rests_on: Annotated[list[RestedOn], Field(min_length=1)]
    rulings: list[Ruling]
    reason: RefuseReason | None


class Decision(Out):
    """An approval or a refusal: what changed, why, on what evidence and who decided."""

    decision: DecisionKind
    line: int
    at: str
    what_changed: WhatChanged
    why: Why
    evidence: Evidence
    who: Literal["playbook_owner"]

    @model_validator(mode="after")
    def whole(self) -> Self:
        if self.decision == "approved" and not (
            self.what_changed.versions and self.evidence.checks
        ):
            raise ValueError("an approval names its versions and each check's result")
        if self.decision == "refused" and self.why.reason is None:
            raise ValueError("a refusal has a reason")
        return self


class Record(Out):
    entries: list[RecordEntry]
    proposals: list[ListedProposal]


class Rebuilt(Out):
    ledger: int
    triage_log: int


class FirmSource(Out):
    id: str
    kind: Literal["memo", "correction"]
    date: str
    desk: str | None
    scope: Scope | Literal["none"]
    correction: dict[str, JsonValue] | None
    text: str
    sha256: str


class FirmSourceList(Out):
    sources: list[FirmSource]


class GapFiled(Out):
    id: str
    reference: str
    state: GapReportState


class Answered(Out):
    answer: Literal["no_gap_report"]


class GapReport(Out):
    id: str
    reference: str
    state: GapReportState
    playbook: str | None
    version: int | None
    code: WarningCode
    feed: FeedFacts | None
    summary: str
    trace_id: str
    run_id: str
    note_id: str
    correction_id: str | None
    proposal_id: str | None
    dismissed: DismissReason | None
    filed_at: str


class GapReportList(Out):
    gap_reports: list[GapReport]


class CorrectionRecorded(Out):
    id: str
    source_id: str
    state: CorrectionState
    attempt: int
    asked: Literal["accepted", "unknown", "not_started", "not_asked"]
    why: str | None


class Reviewed(Out):
    state: CorrectionState
    gap_report: str | None


class ReviewStep(Out):
    attempt: int
    turn: int
    tool: ToolName | None
    option: str | None
    outcome: Literal["accepted", "refused"]
    line: str | None
    model: str


class Correction(Out):
    id: str
    source_id: str
    run_id: str
    note_id: str
    date: str
    scope: Scope
    version: int
    step: str
    field: Literal["tell", "by", "hold_report"]
    should_be: bool | str
    reason: str
    state: CorrectionState
    attempt: int
    # False: nobody is working its review, which can be asked for again now.
    asked: bool
    stalled: bool
    stopped: str | None
    outcome: ReviewOutcome | None
    gap_report: str | None
    recorded_at: str
    reviewed_at: str | None
    steps: list[ReviewStep]
    usage: UsageTotal


class CorrectionList(Out):
    corrections: list[Correction]


class BundleItem(Out):
    """An item as the curator is given it."""

    kind: Literal["firm_source", "gap_report", "playbooks_in_use", "triage_outcome_summary"]
    reference: str
    content: dict[str, JsonValue]


class CuratorWork(Out):
    state: CuratorRunState
    spent_usd: str | None
    items: list[BundleItem]
    refusals: list[str]


class ManifestItem(Out):
    kind: Literal["firm_source", "gap_report", "playbooks_in_use", "triage_outcome_summary"]
    reference: str
    sha256: str


class Manifest(Out):
    sha256: str
    items: list[ManifestItem]


class CurationStarted(Out):
    id: str
    attempt: int
    state: CuratorRunState
    asked: Literal["accepted", "unknown"]


class CuratorMoved(Out):
    state: CuratorRunState


class ProposalRecorded(Out):
    id: str
    state: ProposalState
    sha256: str


class Proposal(Out):
    """A suggested change, with what it rests on. None of it is in use."""

    id: str
    state: ProposalState
    sha256: str
    recorded_at: str
    changes: list[StepChange]
    disagreements: list[Disagreement]


class CuratorStep(Out):
    attempt: int
    turn: int
    tool: CuratorTool | None
    outcome: Literal["accepted", "refused"]
    line: str | None
    model: str


class CuratorRun(Out):
    id: str
    attempt: int
    state: CuratorRunState
    stalled: bool
    failure: str | None
    model: str
    bundle: Manifest
    started_at: str
    finished_at: str | None
    proposal: Proposal | None
    steps: list[CuratorStep]
    usage: UsageTotal


class CycleState(Out):
    """How far the recorded cycle has got. Its steps' evidence is in the records."""

    state: Literal["not_run", "running", "done", "stopped"]
    # The step being played, or the last one played; 0 before it starts.
    step: int
    # Why it stopped, when it did.
    line: str | None
    started_at: str | None
    finished_at: str | None
    # Whether the page offers its button: not yet run, on records still empty.
    offered: bool


class CuratorRunList(Out):
    runs: list[CuratorRun]


class ListedRun(Out):
    id: str
    date: str
    cutoff: str
    state: RunState
    stalled: bool
    playbook_set: ReceiptSet
    started_at: str
    finished_at: str | None


class TriageRunList(Out):
    runs: list[ListedRun]
