"""The backend's app and its routes, each a thin call into the modules that own the rules."""

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute

from backend import auth, cycle, dependencies, models, page, record, triage_log
from backend.approval import Approval
from backend.asking import Curator, TriageAgent
from backend.chainlog import LogInvalid
from backend.corrections import Corrections
from backend.curator_runs import CuratorRuns
from backend.founding import FoundingVersions
from backend.gap_reports import GapReports
from backend.gate.run import Gate
from backend.gate.trials import Trials
from backend.journal import Journal
from backend.playbook_format import Slug
from backend.proposal_view import ProposalView
from backend.refusals import Refusal
from backend.settings import Settings
from backend.sources import Sources
from backend.store import PlaybookSet, Store, StoreInvalid
from backend.triage import Triage
from backend.wealthcontext import WealthContext

log = logging.getLogger(__name__)

# Served on loopback. Another name in the Host header is a page elsewhere reaching for it.
HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "backend"})
WORK = "/api/agent/triage-runs/{run_id}/attempts/{attempt}"
REVIEW = "/api/agent/corrections/{correction_id}/attempts/{attempt}"
CURATION = "/api/agent/curator-runs/{run_id}/attempts/{attempt}"
RunAttempt = Annotated[models.Attempt, Path()]
PlaybookId = Annotated[Slug, Path()]
VersionNumber = Annotated[models.Version, Path()]


def _refused(status: int, line: str) -> JSONResponse:
    return JSONResponse({"detail": line}, status_code=status)


def _same_page(request: Request) -> None:
    """The button is pressed on this layer's own page, never from a page elsewhere. A browser
    says where a request comes from in Sec-Fetch-Site; one that does not, in its Origin, which
    this page's no-referrer policy makes "null" for its own form."""
    site = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    if site is not None:
        allowed = site in ("same-origin", "none")
    elif origin is not None:
        # This page's own origin exactly: its scheme, name and port.
        allowed = origin == f"http://{request.headers.get('host', '')}"
    else:
        allowed = True  # not a browser: no page elsewhere sent it
    if not allowed:
        raise Refusal(403, "The cycle is run from this layer's own page.")


def _outside_playbooks(settings: Settings) -> None:
    """The triage log and the database name accounts. The playbook folder may be a repository."""
    folder = settings.playbooks
    if not folder.is_dir():
        raise StoreInvalid("The playbook folder is not there")
    named: list[tuple[str, FilePath]] = [
        ("triage log", settings.triage_log),
        ("database", settings.database),
    ]
    for what, path in named:
        # By the folder itself, so that neither a link nor another spelling leads into it.
        above = path.resolve().parents
        if any(each.exists() and each.samefile(folder) for each in above):
            raise StoreInvalid(f"The {what} may not be in the playbook folder")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    _outside_playbooks(settings)
    auth.kept_apart(settings)
    wealthcontext = WealthContext(settings)
    store = Store(settings.playbooks)
    runs = triage_log.kept(settings.triage_log)
    try:
        journal = Journal.start(settings.database, store.kept, runs)
    except BaseException:
        wealthcontext.close()
        raise
    agent = TriageAgent(settings)
    reports = GapReports(journal, runs, store)
    triage = Triage(journal, runs, store, wealthcontext, agent, reports)
    corrections = Corrections(journal, store, agent, reports)
    sources = Sources(journal, store)
    curator = Curator(settings)
    curation = CuratorRuns(journal, store, curator, settings)
    trials = Trials()
    gate = Gate(journal, store, settings, agent, trials)
    approval = Approval(journal, store, gate.running)
    founding = FoundingVersions(journal, store, settings.sources)
    view = ProposalView(journal, store)
    operations = cycle.Operations(
        journal, triage, sources, corrections, reports, curation, gate, approval, view, store
    )
    played = cycle.Cycle(operations, lambda: cycle.Files.read(settings.sources, settings.cases))

    def shown_cycle() -> page.CycleShown | None:
        if not settings.cycle:
            return None
        models_set = [settings.triage_model, settings.curator_model]
        evidence = cycle.evidence(operations, played.made(), models_set)
        return page.CycleShown(played.status(), evidence)

    def worked(run_id: str) -> Triage | Trials:
        """A held-out trial is worked at a triage run's addresses, and kept apart from one."""
        return trials if trials.holds(run_id) else triage

    def in_use() -> PlaybookSet:
        with journal.transaction() as connection:
            return store.current(connection)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        wealthcontext.close()
        agent.close()
        curator.close()
        journal.close()

    app = FastAPI(
        title="WealthContext Playbooks",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        # A slash redirect would send the path back in its Location.
        redirect_slashes=False,
    )
    app.state.settings, app.state.journal, app.state.store = settings, journal, store

    @app.middleware("http")
    async def loopback_names_only(request: Request, call_next):
        if re.sub(r":[0-9]+$", "", request.headers.get("host", "")) not in HOSTS:
            return _refused(400, "That host is not served here.")
        return await call_next(request)

    @app.exception_handler(Refusal)
    async def refusal(_: Request, refused: Refusal) -> JSONResponse:
        return _refused(refused.status, refused.line)

    @app.exception_handler(RequestValidationError)
    async def malformed(request: Request, _: RequestValidationError) -> JSONResponse:
        # Not even a field's name is repeated: an unknown field's name is what was sent.
        route = request.scope.get("route")
        named = route.operation_id if isinstance(route, APIRoute) else "this operation"
        return _refused(422, f"The request does not fit {named}: a field is missing or malformed.")

    @app.exception_handler(LogInvalid)
    @app.exception_handler(StoreInvalid)
    async def unusable(_: Request, found: Exception) -> JSONResponse:
        # It names a file and a line of this layer's own, and nothing that was sent.
        log.error("%s", found)
        return _refused(500, f"{found}.")

    @app.exception_handler(Exception)
    async def broken(_: Request, __: Exception) -> JSONResponse:
        return _refused(500, "The backend could not do that.")

    @app.get(
        "/health", operation_id="health", description="Say that the backend is up.", tags=["public"]
    )
    def health() -> models.Health:
        return models.Health(status="ok")

    @app.get(
        "/",
        operation_id="show_page",
        description="Show the newest triage run and the playbooks in use.",
        tags=["public"],
        response_class=HTMLResponse,
    )
    def show_page() -> HTMLResponse:
        return page.render(
            triage, in_use(), register=page.register(operations), cycle=shown_cycle()
        )

    @app.post(
        "/cycle",
        operation_id="press_cycle_button",
        description="Run the recorded cycle once, on a layer started for it, then show the page.",
        tags=["public"],
        status_code=303,
    )
    def press_cycle_button(request: Request) -> RedirectResponse:
        _same_page(request)
        if not settings.cycle:
            raise Refusal(404, "This layer was not started for the recorded cycle.")
        played.start()
        return RedirectResponse("/", status_code=303)

    @app.get(
        "/api/cycle",
        operation_id="read_cycle",
        description="Say how far the recorded cycle has got, and why it stopped if it did.",
        tags=["public"],
    )
    def read_cycle() -> models.CycleState:
        if not settings.cycle:
            raise Refusal(404, "This layer was not started for the recorded cycle.")
        return played.status()

    @app.get(
        "/triage-runs/{run_id}",
        operation_id="show_triage_run",
        description="Show one triage run with its notes and receipts.",
        tags=["public"],
        response_class=HTMLResponse,
    )
    def show_triage_run(run_id: str) -> HTMLResponse:
        return page.render(triage, in_use(), run_id)

    @app.get(
        "/api/playbooks",
        operation_id="read_playbooks",
        description="List the playbook versions in use and the set's hash.",
        tags=["people"],
    )
    def read_playbooks(_: dependencies.AnyPerson) -> models.SetInUse:
        playbooks = in_use()
        return models.SetInUse(
            id=playbooks.id,
            version=playbooks.version,
            hash=playbooks.hash,
            in_use=list(playbooks.versions),
        )

    @app.get(
        "/api/founding",
        operation_id="read_founding",
        description="Read the founding versions whole, beside their memos' words, to approve them.",
        tags=["people"],
        dependencies=[dependencies.PlaybookOwner],
    )
    def read_founding() -> models.FoundingShown:
        return founding.read()

    @app.post(
        "/api/founding/approve",
        operation_id="approve_founding",
        description="Approve the founding versions, naming the founding line's hash.",
        tags=["people"],
        dependencies=[dependencies.PlaybookOwner],
    )
    def approve_founding(sent: models.ApproveFounding) -> models.FoundingApproved:
        return founding.approve(sent)

    @app.post(
        "/api/playbooks/{playbook}/versions/{version}/retire",
        operation_id="retire",
        description="Retire the version in use. The one before it is then in use again.",
        tags=["people"],
        dependencies=[dependencies.PlaybookOwner],
    )
    def retire(playbook: PlaybookId, version: VersionNumber, sent: models.Retire) -> models.Retired:
        return approval.retire(playbook, version, sent)

    @app.get(
        "/api/record",
        operation_id="read_record",
        description="List what happened to the firm's knowledge, from the first ledger line on.",
        tags=["people"],
    )
    def read_record(_: dependencies.AnyPerson) -> models.Record:
        return record.read(journal)

    @app.post(
        "/api/rebuild",
        operation_id="rebuild",
        description="Make the whole database again from the ledger and the triage log.",
        tags=["people"],
        dependencies=[dependencies.PlaybookOwner],
    )
    def rebuild() -> models.Rebuilt:
        return models.Rebuilt.model_validate(journal.rebuild())

    @app.post(
        "/api/triage-runs",
        operation_id="start_triage",
        description="Triage a date and cutoff, or start its stalled run again, "
        "then ask the triage agent.",
        tags=["people"],
        status_code=202,
        dependencies=[dependencies.Analyst],
    )
    def start_triage(sent: models.StartTriage) -> models.Started:
        return triage.start(sent)

    @app.get(
        "/api/triage-runs",
        operation_id="read_triage_runs",
        description="List the triage runs, newest first, with the playbook set each ran under.",
        tags=["people"],
    )
    def read_triage_runs(_: dependencies.AnyPerson) -> models.TriageRunList:
        listed = [
            models.ListedRun(
                id=run["id"],
                date=run["date"],
                cutoff=run["cutoff"],
                state=run["state"],
                stalled=run["stalled"],
                playbook_set=models.ReceiptSet(
                    id=run["set_id"], version=run["set_version"], hash=run["set_hash"]
                ),
                started_at=run["started_at"],
                finished_at=run["finished_at"],
            )
            for run in triage.latest(limit=1000)
        ]
        return models.TriageRunList(runs=listed)

    @app.get(
        "/api/triage-runs/{run_id}",
        operation_id="read_triage",
        description="Read one triage run with its notes, notices and receipts.",
        tags=["people"],
    )
    def read_triage(run_id: str, _: dependencies.AnyPerson) -> models.TriageRun:
        return triage.read(run_id)

    @app.post(
        "/api/sources",
        operation_id="intake",
        description="Take in a memo as a firm source, with the scope it states, once checked.",
        tags=["people"],
        dependencies=[dependencies.PlaybookOwner],
    )
    def intake(sent: models.Intake) -> models.FirmSource:
        return sources.intake(sent)

    @app.get(
        "/api/sources",
        operation_id="read_sources",
        description="List the firm sources taken in: memos and corrections.",
        tags=["people"],
    )
    def read_sources(_: dependencies.AnyPerson) -> models.FirmSourceList:
        return sources.listed()

    @app.post(
        "/api/triage-runs/{run_id}/notes/{note_id}/corrections",
        operation_id="record_correction",
        description="Correct a note's chosen step, then ask the triage agent to review it.",
        tags=["people"],
        dependencies=[dependencies.Analyst],
    )
    def record_correction(
        run_id: str, note_id: str, sent: models.RecordCorrection
    ) -> models.CorrectionRecorded:
        return corrections.record(run_id, note_id, sent)

    @app.get(
        "/api/corrections",
        operation_id="read_corrections",
        description="List the corrections and their reviews.",
        tags=["people"],
    )
    def read_corrections(_: dependencies.AnyPerson) -> models.CorrectionList:
        return corrections.listed()

    @app.get(
        "/api/corrections/{correction_id}",
        operation_id="read_correction",
        description="Read one correction and its review.",
        tags=["people"],
    )
    def read_correction(correction_id: str, _: dependencies.AnyPerson) -> models.Correction:
        return corrections.read(correction_id)

    @app.post(
        "/api/corrections/{correction_id}/request-review",
        operation_id="request_review",
        description="Ask the triage agent again to review a correction that nobody is reviewing.",
        tags=["people"],
        dependencies=[dependencies.Analyst],
    )
    def request_review(correction_id: str) -> models.CorrectionRecorded:
        return corrections.request_review(correction_id)

    @app.get(
        "/api/gap-reports",
        operation_id="read_gap_reports",
        description="List the gap reports and their states, with no client identifier.",
        tags=["people"],
    )
    def read_gap_reports(_: dependencies.AnyPerson) -> models.GapReportList:
        return reports.listed()

    @app.post(
        "/api/gap-reports/{report_id}/dismiss",
        operation_id="dismiss_gap_report",
        description="Dismiss an open gap report, with a reason from a closed list.",
        tags=["people"],
        dependencies=[dependencies.PlaybookOwner],
    )
    def dismiss_gap_report(report_id: str, sent: models.Dismiss) -> models.GapReport:
        return reports.dismiss(report_id, sent)

    @app.post(
        "/api/curator-runs",
        operation_id="start_curator_run",
        description="Assemble and hash the bundle, or start its stalled run again, "
        "then ask the curator.",
        tags=["people"],
        status_code=202,
        dependencies=[dependencies.PlaybookOwner],
    )
    def start_curator_run() -> models.CurationStarted:
        return curation.start()

    @app.get(
        "/api/curator-runs",
        operation_id="read_curator_runs",
        description="List every curator run: its bundle's manifest, its steps and its proposal.",
        tags=["people"],
    )
    def read_curator_runs(_: dependencies.AnyPerson) -> models.CuratorRunList:
        return models.CuratorRunList(runs=curation.listed())

    @app.get(
        "/api/curator-runs/{run_id}",
        operation_id="read_curator_run",
        description="Read one curator run: its bundle's manifest, its steps and its proposal.",
        tags=["people"],
    )
    def read_curator_run(run_id: str, _: dependencies.AnyPerson) -> models.CuratorRun:
        return curation.read(run_id)

    @app.post(
        "/api/proposals/{proposal_id}/gate-runs",
        operation_id="run_gate",
        description="Run every check on a drafted proposal and record each result.",
        tags=["people"],
    )
    def run_gate(proposal_id: str, who: dependencies.AnyPerson) -> models.GateRun:
        return gate.run(proposal_id, who)

    @app.post(
        "/api/checked-proposals",
        operation_id="check_proposal",
        description="Run the fixed-rule checks on a proposal handed in; it is never approved.",
        tags=["people"],
    )
    def check_proposal(
        sent: models.Proposed, who: dependencies.AnyPerson
    ) -> models.CheckedProposal:
        return gate.check(sent, who)

    @app.post(
        "/api/proposals/{proposal_id}/approve",
        operation_id="approve",
        description="Approve a proposal that passed the gate, naming its hash and its gate run.",
        tags=["people"],
        dependencies=[dependencies.PlaybookOwner],
    )
    def approve(proposal_id: str, sent: models.Approve) -> models.Approved:
        return approval.approve(proposal_id, sent)

    @app.post(
        "/api/proposals/{proposal_id}/refuse",
        operation_id="refuse",
        description="Refuse a proposal not yet decided, with a reason from a closed list.",
        tags=["people"],
        dependencies=[dependencies.PlaybookOwner],
    )
    def refuse(proposal_id: str, sent: models.Refuse) -> models.ProposalRefused:
        return approval.refuse(proposal_id, sent)

    @app.get(
        "/api/proposals/{proposal_id}",
        operation_id="read_proposal",
        description="Read a proposal's changed steps next to the exact words of what they cite.",
        tags=["people"],
    )
    def read_proposal(proposal_id: str, _: dependencies.AnyPerson) -> models.ProposalShown:
        return view.read(proposal_id)

    @app.get(
        WORK,
        operation_id="read_triage_work",
        description="Read a run's pending notes, with no identifier of any client or result.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def read_triage_work(run_id: str, attempt: RunAttempt) -> models.TriageWork:
        return worked(run_id).work(run_id, attempt)

    @app.post(
        WORK + "/notes/{note_id}/choice",
        operation_id="record_choice",
        description="Record the step chosen for a note, or that none applies, once it is checked.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def record_choice(
        run_id: str, attempt: RunAttempt, note_id: str, sent: models.RecordChoice
    ) -> models.Moved:
        return worked(run_id).record_choice(run_id, attempt, note_id, sent)

    @app.post(
        WORK + "/notes/{note_id}/unusable-reply",
        operation_id="record_unusable_reply",
        description="Record a reply that was no usable tool call as a refused turn.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def record_unusable_reply(
        run_id: str, attempt: RunAttempt, note_id: str, sent: models.UnusableReply
    ) -> models.Refused:
        return worked(run_id).record_unusable_reply(run_id, attempt, note_id, sent)

    @app.post(
        WORK + "/notes/{note_id}/fallback",
        operation_id="record_fallback",
        description="Settle a note as unverified after its second refusal.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def record_fallback(run_id: str, attempt: RunAttempt, note_id: str) -> models.Moved:
        return worked(run_id).record_fallback(run_id, attempt, note_id)

    @app.post(
        WORK + "/notes/{note_id}/gap-report",
        operation_id="file_gap_report",
        description="File a gap report on an exception no playbook covers, once it is checked.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def file_gap_report(
        run_id: str, attempt: RunAttempt, note_id: str, sent: models.FileGapReport
    ) -> models.GapFiled:
        if trials.holds(run_id):
            trials.refuse_gap_question(
                run_id, attempt, note_id, "file_gap_report", sent.turn, sent.usage
            )
        return triage.file_gap_report(run_id, attempt, note_id, sent)

    @app.post(
        WORK + "/notes/{note_id}/no-gap-report",
        operation_id="record_no_gap_report",
        description="Record that no gap report is filed on an exception no playbook covers.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def record_no_gap_report(
        run_id: str, attempt: RunAttempt, note_id: str, sent: models.NoGapReport
    ) -> models.Answered:
        if trials.holds(run_id):
            trials.refuse_gap_question(
                run_id, attempt, note_id, "no_gap_report", sent.turn, sent.usage
            )
        return triage.record_no_gap_report(run_id, attempt, note_id, sent)

    @app.post(
        WORK + "/complete",
        operation_id="complete_triage",
        description="Complete a run whose notes are all settled.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def complete_triage(run_id: str, attempt: RunAttempt) -> models.Moved:
        return worked(run_id).complete(run_id, attempt)

    @app.post(
        WORK + "/fail",
        operation_id="fail_triage",
        description="Fail a run, with a reason from a closed list.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def fail_triage(run_id: str, attempt: RunAttempt, sent: models.FailTriage) -> models.Moved:
        return worked(run_id).fail(run_id, attempt, sent)

    @app.get(
        REVIEW,
        operation_id="read_review_work",
        description="Read a correction to review, with no identifier of any client or result.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def read_review_work(correction_id: str, attempt: RunAttempt) -> models.ReviewWork:
        return corrections.work(correction_id, attempt)

    @app.post(
        REVIEW + "/review",
        operation_id="record_review",
        description="End a review: file a gap report, once it is checked, or file none.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def record_review(
        correction_id: str, attempt: RunAttempt, sent: models.RecordReview
    ) -> models.Reviewed:
        return corrections.record_review(correction_id, attempt, sent)

    @app.post(
        REVIEW + "/unusable-reply",
        operation_id="record_unusable_review",
        description="Record a review's reply that was no usable tool call as a refused turn.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def record_unusable_review(
        correction_id: str, attempt: RunAttempt, sent: models.UnusableReply
    ) -> models.Refused:
        return corrections.record_unusable_reply(correction_id, attempt, sent)

    @app.post(
        REVIEW + "/fallback",
        operation_id="record_review_fallback",
        description="End a review with no gap report after its second refusal.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def record_review_fallback(correction_id: str, attempt: RunAttempt) -> models.Reviewed:
        return corrections.record_fallback(correction_id, attempt)

    @app.post(
        REVIEW + "/stop",
        operation_id="stop_review",
        description="Record why a review stopped. The correction stands.",
        tags=["agent"],
        dependencies=[dependencies.TriageAgentKey],
    )
    def stop_review(
        correction_id: str, attempt: RunAttempt, sent: models.StopReview
    ) -> models.Reviewed:
        return corrections.record_stopped(correction_id, attempt, sent)

    @app.get(
        CURATION,
        operation_id="read_curator_work",
        description="Read a curator run's bundle, with no identifier of any client or result.",
        tags=["agent"],
        dependencies=[dependencies.CuratorKey],
    )
    def read_curator_work(run_id: str, attempt: RunAttempt) -> models.CuratorWork:
        return curation.work(run_id, attempt)

    @app.post(
        CURATION + "/proposal",
        operation_id="record_proposal",
        description="Record the run's one proposal, once its form and what it names are checked.",
        tags=["agent"],
        dependencies=[dependencies.CuratorKey],
    )
    def record_proposal(
        run_id: str, attempt: RunAttempt, sent: models.RecordProposal
    ) -> models.ProposalRecorded:
        return curation.record_proposal(run_id, attempt, sent)

    @app.post(
        CURATION + "/no-change",
        operation_id="record_no_change",
        description="End a curator run with no proposal.",
        tags=["agent"],
        dependencies=[dependencies.CuratorKey],
    )
    def record_no_change(
        run_id: str, attempt: RunAttempt, sent: models.NoChange
    ) -> models.CuratorMoved:
        return curation.record_no_change(run_id, attempt, sent)

    @app.post(
        CURATION + "/unusable-reply",
        operation_id="record_unusable_curator_reply",
        description="Record a curator's reply that was no usable tool call as a refused turn.",
        tags=["agent"],
        dependencies=[dependencies.CuratorKey],
    )
    def record_unusable_curator_reply(
        run_id: str, attempt: RunAttempt, sent: models.UnusableCuratorReply
    ) -> models.Refused:
        return curation.record_unusable_reply(run_id, attempt, sent)

    @app.post(
        CURATION + "/fail",
        operation_id="fail_curator_run",
        description="Fail a curator run, with a reason from a closed list.",
        tags=["agent"],
        dependencies=[dependencies.CuratorKey],
    )
    def fail_curator_run(
        run_id: str, attempt: RunAttempt, sent: models.FailCuratorRun
    ) -> models.CuratorMoved:
        return curation.fail(run_id, attempt, sent)

    return app
