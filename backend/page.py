"""The read-only page, rendered here. It shows what operations may see. Its one control is the
recorded cycle's button, on a layer started for it."""

from dataclasses import dataclass
from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader
from sqlalchemy import select

from backend import database as db
from backend import models, record
from backend.cycle import STEPS, Operations
from backend.store import PlaybookSet
from backend.triage import Triage

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_templates = Environment(loader=PackageLoader("backend", "templates"), autoescape=True)


@dataclass(frozen=True)
class CycleShown:
    status: models.CycleState
    # None: the firm's files for the cycle are not on this layer.
    evidence: dict[str, Any] | None


def register(do: Operations) -> dict[str, Any]:
    """The playbooks and their versions, gap reports, curator runs, proposals with their gate
    results, and the approvals and refusals among them."""
    with do.journal.transaction() as connection:
        found = connection.execute(
            select(db.versions).order_by(db.versions.c.playbook, db.versions.c.version)
        )
        versions = [
            {
                "playbook": row["playbook"],
                "version": row["version"],
                "sha256": row["sha256"],
                "state": row["state"],
                "approved": row["approved_by"] is not None,
                "steps": len(row["steps"]),
            }
            for row in found.mappings()
        ]
    return {
        "versions": versions,
        "reports": do.reports.listed().gap_reports,
        "curator_runs": do.curation.listed(),
        "proposals": record.read(do.journal).proposals,
    }


def render(
    triage: Triage,
    playbooks: PlaybookSet,
    run_id: str | None = None,
    register: dict[str, Any] | None = None,
    cycle: CycleShown | None = None,
) -> HTMLResponse:
    listed = triage.latest()
    shown = run_id or (listed[0]["id"] if listed else None)
    html = _templates.get_template("page.html").render(
        runs=listed,
        run=triage.read(shown) if shown else None,
        playbooks=playbooks,
        register=register,
        cycle=cycle,
        titles=STEPS,
    )
    return HTMLResponse(html, headers=HEADERS)
