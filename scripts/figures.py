"""Draws the README's figures: SVG files in guide/figures, a light and a dark one of each.

    uv run python -m scripts.figures

A figure is boxes, arrows and short texts, each placed by hand. A text too wide for its box
stops the drawing, and a test holds the committed files to what this file draws.
"""

from collections.abc import Callable, Sequence
from html import escape
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent / "guide" / "figures"

LIGHT = {
    "panel": "#FBFCFB", "surface": "#FFFFFF", "surface2": "#E9ECEB", "zone": "#F4F6F5",
    "fg": "#18202B", "muted": "#566070", "line": "#CDD3D6", "strong": "#7B8592",
    "edge": "#46505E", "accent": "#2756A6", "accent_soft": "#E2EAF7", "ok": "#1F7A4D",
    "ok_soft": "#DFF1E7", "warn": "#9C5B0E", "warn_soft": "#FAEBD5", "deny": "#B02A22",
    "deny_soft": "#F8E1DF",
}  # fmt: skip
DARK = {
    "panel": "#161C23", "surface": "#1D242D", "surface2": "#252D38", "zone": "#192028",
    "fg": "#E5E9EE", "muted": "#9AA4B1", "line": "#323B47", "strong": "#6A7584",
    "edge": "#A9B2BF", "accent": "#8EB2F2", "accent_soft": "#1D2C47", "ok": "#6FCB9A",
    "ok_soft": "#163325", "warn": "#E8B062", "warn_soft": "#382813", "deny": "#F28B83",
    "deny_soft": "#3B1C1A",
}  # fmt: skip
THEMES = {"": LIGHT, "-dark": DARK}

FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif'
TONES = ("accent", "ok", "warn", "deny")
# How wide one character is taken to be, in each kind of text.
TITLE, LINE, CHIP, LABEL = 7.1, 6.0, 6.3, 6.1
MARGIN = 14

Point = tuple[float, float]


def style(colour: dict[str, str]) -> str:
    c = colour
    tones = "".join(
        f".{t}t{{fill:{c[t]}}}.box.{t},.step.{t}{{fill:{c[t + '_soft']};stroke:{c[t]}}}"
        f".edge.{t}{{stroke:{c[t]}}}.head.{t}{{fill:{c[t]}}}"
        for t in TONES
    )
    return (
        f"text{{font-family:{FONT}}}"
        f".panel{{fill:{c['panel']};stroke:{c['line']}}}"
        f".tt{{font-size:13px;font-weight:600;fill:{c['fg']}}}"
        f".tt14{{font-size:14px;font-weight:600;fill:{c['fg']}}}"
        f".ts{{font-size:11.5px;fill:{c['muted']}}}"
        f".lbl{{font-size:11px;fill:{c['muted']}}}"
        f".zl{{font-size:10.5px;letter-spacing:.08em;font-weight:600;fill:{c['muted']}}}"
        f".chipt{{font-size:11.5px;font-weight:600}}"
        f".stepn{{font-size:19px;font-weight:600;fill:{c['fg']}}}"
        f".lbg{{fill:{c['panel']}}}"
        f".zone{{fill:{c['zone']};stroke:{c['line']};stroke-dasharray:5 4}}"
        f".box{{fill:{c['surface']};stroke:{c['strong']};stroke-width:1.1}}"
        f".box.muted{{fill:{c['surface2']};stroke:{c['line']}}}"
        f".box.ghost{{fill:none;stroke:{c['strong']};stroke-dasharray:6 4}}"
        f".step{{fill:{c['surface']};stroke:{c['strong']};stroke-width:2}}"
        f".edge{{fill:none;stroke:{c['edge']};stroke-width:1.4}}"
        f".edge.dash{{stroke-dasharray:5 4}}"
        f".head{{fill:{c['edge']}}}"
        f".person{{fill:{c['strong']}}}"
        f"{tones}"
    )


def named(*kinds: str) -> str:
    return " ".join(kind for kind in kinds if kind)


class Figure:
    def __init__(self, width: int, height: int, says: str):
        self.width, self.height, self.says = width, height, says
        self.too_wide: list[str] = []
        self._under: list[str] = []
        self._edges: list[str] = []
        self._shapes: list[str] = []
        self._texts: list[str] = []

    def _fits(self, text: str, each: float, room: float) -> None:
        if len(text) * each > room:
            self.too_wide.append(text)

    def _text(self, x: float, y: float, text: str, kind: str, anchor: str = "middle") -> None:
        self._texts.append(
            f'<text class="{kind}" x="{x:g}" y="{y:g}" text-anchor="{anchor}">{escape(text)}</text>'
        )

    def zone(self, x: int, y: int, w: int, h: int, title: str) -> None:
        self._under.append(f'<rect class="zone" x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>')
        self._under.append(f'<text class="zl" x="{x + 14}" y="{y + 20}">{escape(title)}</text>')

    def edge(self, points: Sequence[Point], kind: str = "") -> None:
        path = " ".join(f"{'L' if i else 'M'} {x:g} {y:g}" for i, (x, y) in enumerate(points))
        head = next((tone for tone in TONES if tone in kind.split()), "base")
        self._edges.append(
            f'<path class="{named("edge", kind)}" d="{path}" marker-end="url(#head-{head})"/>'
        )

    def box(
        self, x: int, y: int, w: int, h: int, title: str, lines: Sequence[str] = (), kind: str = ""
    ) -> None:
        self._shapes.append(
            f'<rect class="{named("box", kind)}" x="{x}" y="{y}" width="{w}" height="{h}" rx="7"/>'
        )
        middle = x + w / 2
        top = y + h / 2 - 7.5 * len(lines) + 4.5
        self._fits(title, TITLE, w - 10)
        self._text(middle, top, title, f"tt {kind}t" if kind in TONES else "tt")
        for i, line in enumerate(lines):
            self._fits(line, LINE, w - 8)
            self._text(middle, top + 16 + 15 * i, line, "ts")

    def chip(self, x: int, y: int, w: int, text: str, kind: str) -> None:
        self._fits(text, CHIP, w - 12)
        self._shapes.append(
            f'<rect class="box {kind}" x="{x}" y="{y}" width="{w}" height="26" rx="13"/>'
        )
        self._text(x + w / 2, y + 17, text, f"ts chipt {kind}t")

    def label(self, x: int, y: int, text: str, kind: str = "") -> None:
        width = len(text) * LABEL + 10
        self._texts.append(
            f'<rect class="lbg" x="{x - width / 2:g}" y="{y - 12}" width="{width:g}" height="16" '
            f'rx="3"/>'
        )
        self._text(x, y, text, f"lbl {kind}t" if kind else "lbl")

    def text(self, x: int, y: int, text: str, kind: str = "ts", anchor: str = "middle") -> None:
        self._text(x, y, text, kind, anchor)

    def person(self, x: int, y: int) -> None:
        self._shapes.append(f'<circle class="person" cx="{x}" cy="{y}" r="9"/>')
        self._shapes.append(
            f'<path class="person" d="M {x - 15} {y + 26} Q {x - 15} {y + 12} {x} {y + 12} '
            f'Q {x + 15} {y + 12} {x + 15} {y + 26} Z"/>'
        )

    def step(
        self, x: int, y: int, number: int, title: str, lines: Sequence[str], kind: str = ""
    ) -> None:
        self._shapes.append(f'<circle class="{named("step", kind)}" cx="{x}" cy="{y}" r="22"/>')
        self._text(x, y + 7, str(number), f"stepn {kind}t" if kind else "stepn")
        self._text(x, y + 52, title, "tt14")
        for i, line in enumerate(lines):
            self._text(x, y + 72 + 17 * i, line, "ts")

    def svg(self, colour: dict[str, str]) -> str:
        if self.too_wide:
            raise ValueError(f"too wide for its box: {self.too_wide}")
        heads = "".join(
            f'<marker id="head-{tone}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto"><path class="head {tone}" d="M0 0 L10 5 L0 10 z"/>'
            f"</marker>"
            for tone in ("base", *TONES)
        )
        width, height = self.width + 2 * MARGIN, self.height + 2 * MARGIN
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-MARGIN} {-MARGIN} {width} '
            f'{height}" width="{width}" height="{height}" role="img" '
            f'aria-label="{escape(self.says)}">\n'
            f"<title>{escape(self.says)}</title>\n"
            f"<style>{style(colour)}</style>\n"
            f"<defs>{heads}</defs>\n"
            f'<rect class="panel" x="{0.5 - MARGIN}" y="{0.5 - MARGIN}" width="{width - 1}" '
            f'height="{height - 1}" rx="10"/>\n'
            + "\n".join(self._under + self._edges + self._shapes + self._texts)
            + "\n</svg>\n"
        )


def with_a_gate() -> Figure:
    f = Figure(
        1000,
        420,
        "Two loops. In a knowledge factory with no gate, an agent flags a gap, a curator agent "
        "rewrites the shared knowledge and agents read it: a change rests on nothing, nothing is "
        "tested first, no person approves and no record says why. With Playbooks, the triage "
        "agent files a gap report, the curator proposes a change that rests on firm sources or "
        "gap reports, a gate checks it, the playbook owner approves it, the new version goes into "
        "use and the ledger keeps each check's result and each decision.",
    )
    f.zone(20, 20, 470, 380, "A KNOWLEDGE FACTORY WITH NO GATE")
    f.zone(510, 20, 470, 380, "WITH PLAYBOOKS")
    f.box(155, 62, 200, 56, "Agent at work", ["uses the shared knowledge"], "muted")
    f.box(40, 190, 190, 56, "Curator agent", ["rewrites the knowledge"])
    f.box(280, 190, 190, 56, "Shared knowledge", ["changed in place"])
    f.edge([(215, 118), (215, 150), (135, 150), (135, 190)])
    f.edge([(230, 218), (280, 218)])
    f.edge([(375, 190), (375, 150), (295, 150), (295, 118)])
    f.label(160, 146, "flags a gap")
    f.label(350, 146, "reads it")
    f.chip(35, 300, 205, "a change rests on nothing", "warn")
    f.chip(270, 300, 205, "nothing is tested first", "warn")
    f.chip(35, 346, 205, "no person approves", "warn")
    f.chip(270, 346, 205, "no record of why it changed", "warn")

    f.box(645, 62, 200, 56, "Triage agent", ["applies the playbooks"], "muted")
    f.box(530, 170, 130, 56, "Curator", ["proposes only"])
    f.box(680, 170, 130, 56, "Gate", ["six checks"], "accent")
    f.box(830, 170, 130, 56, "Playbook owner", ["a named person"], "accent")
    f.box(530, 290, 130, 56, "Firm sources", ["memos, corrections"])
    f.box(680, 290, 130, 56, "Ledger", ["checks and decisions"], "muted")
    f.box(830, 290, 130, 56, "Playbooks", ["one file a version"], "ok")
    f.edge([(645, 90), (595, 90), (595, 170)])
    f.edge([(660, 198), (680, 198)])
    f.edge([(810, 198), (830, 198)], "accent")
    f.edge([(595, 290), (595, 226)])
    f.edge([(745, 226), (745, 290)])
    f.edge([(895, 226), (895, 290)], "ok")
    f.edge([(960, 318), (970, 318), (970, 90), (845, 90)], "ok")
    f.label(585, 138, "gap report")
    f.label(670, 156, "proposal")
    f.label(820, 156, "passes")
    f.label(595, 262, "cited")
    f.label(745, 262, "recorded")
    f.label(895, 262, "approves", "ok")
    f.label(908, 86, "version in use")
    f.text(745, 370, "A change rests on a firm source or a gap report,")
    f.text(745, 386, "passes the checks, is approved, and is on record.")
    return f


def the_loop() -> Figure:
    f = Figure(
        1000,
        540,
        "The whole loop. WealthContext's warnings become exceptions by fixed rules. The triage "
        "agent picks a playbook step for each or says none applies, the backend checks the pick, "
        "and a triage note with a receipt comes out. The agent files a gap report for what the "
        "playbooks miss. Gap reports and firm sources go to the curator, which proposes a change; "
        "the gate runs six checks; the playbook owner approves or refuses; an approved change is a "
        "new playbook version in use. A failed check leaves the playbooks unchanged. The ledger "
        "records every source, gap report, proposal, check result, approval and refusal; triage "
        "runs are kept in a log of their own.",
    )
    f.box(210, 30, 170, 62, "WealthContext", ["unchanged, read-only"], "muted")
    f.text(
        396,
        58,
        "Read through three of its published read routes, signed in as operations.",
        anchor="start",
    )
    f.text(
        396,
        74,
        "Nothing in WealthContext changes, and its database is never opened.",
        anchor="start",
    )
    f.zone(8, 112, 984, 364, "WEALTHCONTEXT PLAYBOOKS")
    f.box(
        20,
        150,
        170,
        250,
        "Playbooks",
        [
            "the firm's steps for",
            "each kind of warning",
            "",
            "one file per version,",
            "never written over",
            "",
            "a new version on",
            "each approval",
            "",
            "every note names",
            "the version it used",
        ],
        "ok",
    )
    f.box(210, 150, 170, 70, "Exceptions", ["built by fixed rules", "from the warnings"])
    f.box(400, 150, 170, 70, "Triage agent", ["picks a step from a list,", "or says none applies"])
    f.box(
        590,
        150,
        170,
        70,
        "Backend check",
        ["by fixed fields; one retry,", "then a note with no action"],
        "accent",
    )
    f.box(
        780, 150, 170, 70, "Triage note", ["who to tell, by when,", "hold or not; a receipt"], "ok"
    )
    f.edge([(295, 92), (295, 150)])
    f.edge([(380, 185), (400, 185)])
    f.edge([(570, 185), (590, 185)])
    f.edge([(760, 185), (780, 185)], "ok")
    f.edge([(190, 258), (675, 258), (675, 220)], "ok")
    f.edge([(485, 258), (485, 220)], "ok")
    f.label(300, 254, "steps in use", "ok")
    f.label(295, 126, "warnings")

    f.box(780, 300, 170, 52, "Gap reports", ["what the playbooks miss"], "warn")
    f.box(780, 368, 170, 52, "Firm sources", ["memos, corrections"])
    f.box(590, 325, 170, 70, "Curator", ["proposes a change, and", "cites what it rests on"])
    f.box(400, 325, 170, 70, "Gate", ["six checks, each result", "recorded"], "accent")
    f.box(
        210,
        325,
        170,
        70,
        "Playbook owner",
        ["approves by hash,", "or refuses with a reason"],
        "accent",
    )
    f.edge([(865, 220), (865, 300)], "warn")
    f.edge([(780, 340), (760, 340)])
    f.edge([(780, 382), (760, 382)])
    f.edge([(590, 360), (570, 360)])
    f.edge([(400, 360), (380, 360)], "accent")
    f.edge([(210, 360), (190, 360)], "ok")
    f.label(865, 264, "filed by the agent", "warn")
    f.box(400, 424, 170, 40, "Refused", ["playbooks unchanged"], "deny")
    f.edge([(485, 395), (485, 424)], "deny")
    f.text(580, 449, "when any check fails", "lbl", "start")
    f.box(
        20,
        488,
        960,
        40,
        "The ledger records every source, gap report, proposal, check result, "
        "approval and refusal, each line chained to the one before.",
        [],
        "muted",
    )
    return f


def one_morning() -> Figure:
    f = Figure(
        1000,
        380,
        "2026-10-15 in WealthContext's synthetic data: 21 warnings, 5 informational notices and "
        "one failed feed run. The fifteen warnings on the failed feed's accounts become one "
        "exception and one note. The six other warnings become six exceptions with a note each. "
        "The five notices are listed and get no note. The triage agent's model sees the warning "
        "code, the feed facts and a count, never an account or a household. The backend adds the "
        "accounts and a receipt to the note. For the six, no approved step applies: their notes "
        "say so and give no action, and the agent may file a gap report.",
    )
    f.box(
        20,
        150,
        220,
        84,
        "21 warnings, 5 notices",
        ["WealthContext, 2026-10-15", "one failed feed run: PAS-B"],
    )
    f.box(
        300,
        30,
        260,
        84,
        "15 on the failed feed's accounts",
        ["SOURCE_UNAVAILABLE, source PAS", "one exception, one note for all"],
        "warn",
    )
    f.box(300, 150, 260, 84, "6 other warnings", ["six exceptions,", "a note each"])
    f.box(
        300, 270, 260, 84, "5 informational notices", ["listed for the analyst", "no note"], "muted"
    )
    f.edge([(240, 192), (300, 192)])
    f.edge([(270, 192), (270, 72), (300, 72)])
    f.edge([(270, 192), (270, 312), (300, 312)])
    f.box(
        620,
        30,
        170,
        204,
        "Triage agent",
        [
            "sees the warning code,",
            "PAS · B · FAILED,",
            "and a count",
            "",
            "never an account",
            "or a household",
            "",
            "picks from a closed list",
        ],
    )
    f.edge([(560, 72), (620, 72)])
    f.edge([(560, 192), (620, 192)])
    f.box(
        830,
        30,
        150,
        204,
        "Triage note",
        [
            "tell: a desk",
            "by: a deadline",
            "hold the report?",
            "",
            "accounts added",
            "by the backend",
            "",
            "receipt: version, hash",
        ],
        "ok",
    )
    f.edge([(790, 132), (830, 132)], "ok")
    f.box(
        620,
        270,
        360,
        84,
        "No approved step applies",
        [
            "the six notes say so, and give no action",
            "the agent may file a gap report, for the curator to read",
        ],
        "warn",
    )
    f.edge([(705, 234), (705, 270)], "warn")
    f.text(20, 372, "Counts from WealthContext's synthetic data for 2026-10-15.", anchor="start")
    return f


def what_runs_where() -> Figure:
    f = Figure(
        1000,
        470,
        "What runs where. Three containers on one machine: a backend and two agents. The backend "
        "owns every record and rule and is the only caller of WealthContext, which it reads as "
        "operations through three read routes. The triage agent and the curator each have a key "
        "and a network of their own and reach the backend's API only; each calls a chat model, "
        "which is a provider or the recording. The analyst and the playbook owner use the "
        "playbooks command and a page. The playbook folder holds one file per version and the "
        "ledger; the database and the triage log are in a volume that is never committed.",
    )
    f.zone(20, 20, 960, 430, "ONE MACHINE · LOCAL CONTAINERS")
    f.box(60, 90, 200, 64, "Triage agent", ["its own key and network"])
    f.box(60, 196, 200, 58, "Chat model", ["a provider, or the recording"], "ghost")
    f.box(60, 296, 200, 64, "Curator", ["its own key and network"])
    f.edge([(160, 154), (160, 196)], "dash")
    f.edge([(160, 296), (160, 254)], "dash")
    f.box(
        380,
        150,
        240,
        150,
        "Backend",
        [
            "owns every record and rule",
            "playbook store, gate, page",
            "",
            "the only caller of",
            "WealthContext",
        ],
        "accent",
    )
    f.edge([(260, 122), (320, 122), (320, 190), (380, 190)], "accent")
    f.edge([(260, 328), (320, 328), (320, 262), (380, 262)], "accent")
    f.label(320, 230, "API only", "accent")
    f.box(
        380,
        364,
        240,
        64,
        "WealthContext",
        ["its own containers, unchanged", "three read routes"],
        "muted",
    )
    f.edge([(500, 300), (500, 364)])
    f.label(500, 336, "reads as operations")
    f.text(740, 82, "PEOPLE", "zl", "start")
    f.box(740, 92, 220, 56, "")
    f.person(770, 108)
    f.text(800, 117, "Analyst", "tt", "start")
    f.text(800, 134, "triage, corrections", anchor="start")
    f.box(740, 160, 220, 56, "")
    f.person(770, 176)
    f.text(800, 185, "Playbook owner", "tt", "start")
    f.text(800, 202, "approve, refuse, retire", anchor="start")
    f.edge([(740, 120), (680, 120), (680, 180), (620, 180)])
    f.edge([(740, 196), (620, 196)])
    f.text(850, 236, "the playbooks command and a page")
    f.box(740, 270, 220, 70, "Playbook folder", ["one file per version,", "and the ledger"])
    f.box(
        740,
        358,
        220,
        70,
        "Records volume",
        ["database and triage log,", "never committed"],
        "muted",
    )
    f.edge([(620, 270), (680, 270), (680, 305), (740, 305)])
    f.edge([(680, 305), (680, 393), (740, 393)])
    return f


def the_gate() -> Figure:
    f = Figure(
        1000,
        350,
        "The gate. A proposal goes through six checks: support, personal data, guardrails, base, "
        "overlap and the held-out cases, and every result is recorded. If all pass, the playbook "
        "owner approves by hash, ruling for a source where sources disagree, and the new version "
        "goes into use. If any check fails the proposal is refused and the playbooks are "
        "unchanged. If the playbooks changed since the gate ran, the proposal goes stale and its "
        "gap reports are open again.",
    )
    f.box(20, 108, 150, 64, "Proposal", ["changed steps, each", "with what it rests on"])
    f.zone(200, 20, 495, 240, "THE GATE · SIX CHECKS, EVERY RESULT RECORDED")
    checks = [
        ("Support", ["each change rests on", "what its bundle holds"]),
        ("Personal data", ["no personal data and", "no client identifier"]),
        ("Guardrails", ["no forbidden wording,", "figure or unlisted desk"]),
        ("Base", ["made against the", "versions in use"]),
        ("Overlap", ["no two steps fit", "the same exception"]),
        ("Held-out cases", ["eight cases by rule,", "then three trials each"]),
    ]
    for i, (name, lines) in enumerate(checks):
        f.box(215 + (i % 3) * 157, 52 + (i // 3) * 100, 150, 86, name, lines, "accent")
    f.edge([(170, 140), (200, 140)])
    f.box(
        725,
        98,
        150,
        84,
        "Playbook owner",
        ["approves by hash, and", "rules for a source", "where sources disagree"],
        "accent",
    )
    f.box(895, 105, 85, 70, "In use", ["new version"], "ok")
    f.edge([(695, 140), (725, 140)], "accent")
    f.edge([(875, 140), (895, 140)], "ok")
    f.label(710, 122, "pass", "accent")
    f.box(240, 292, 400, 44, "Any check fails: refused, playbooks unchanged", [], "deny")
    f.edge([(440, 260), (440, 292)], "deny")
    f.box(
        700,
        240,
        280,
        96,
        "Stale",
        [
            "if the playbooks changed since the gate",
            "ran, the proposal goes stale and its",
            "gap reports are open again",
        ],
        "warn",
    )
    f.edge([(800, 182), (800, 240)], "warn")
    return f


def recorded_cycle() -> Figure:
    f = Figure(
        1000,
        250,
        "The recorded cycle in six steps: both dates triaged under the first playbooks; what "
        "arrived since, a memo, a correction and a gap report; the curator's proposal; the gate "
        "and the approval; both dates triaged again under the second playbooks; a poisoned memo "
        "and a hostile proposal, and what stopped each. It replays with no API key.",
    )
    steps = [
        ("Triage, version 1", ["a failed feed and a", "missing cross-check"], ""),
        ("What arrived since", ["a memo, a correction,", "a gap report"], ""),
        ("The proposal", ["changed steps, each", "next to what it rests on"], ""),
        ("Gate and approval", ["each check's result,", "then who approved"], "accent"),
        ("Triage, version 2", ["both dates again, under", "the second playbooks"], "ok"),
        ("The attacks", ["a poisoned memo and a", "hostile proposal, stopped"], "deny"),
    ]
    for i, (title, lines, kind) in enumerate(steps):
        x = 90 + 164 * i
        f.step(x, 52, i + 1, title, lines, kind)
        if i:
            f.edge([(x - 138, 52), (x - 26, 52)])
    f.box(
        20,
        186,
        960,
        50,
        "Replays with no API key",
        [
            "WealthContext's reads are real, every check runs again, and only the models' replies "
            "come from the recording of the live cycle of 2026-11-24.",
        ],
        "muted",
    )
    return f


FIGURES: dict[str, Callable[[], Figure]] = {
    "with-a-gate": with_a_gate,
    "the-loop": the_loop,
    "one-morning": one_morning,
    "what-runs-where": what_runs_where,
    "the-gate": the_gate,
    "recorded-cycle": recorded_cycle,
}


def drawn() -> dict[str, str]:
    """Every figure file's name and what it holds."""
    return {
        f"{name}{theme}.svg": draw().svg(colour)
        for name, draw in FIGURES.items()
        for theme, colour in THEMES.items()
    }


def main() -> None:
    FOLDER.mkdir(parents=True, exist_ok=True)
    for name, svg in drawn().items():
        (FOLDER / name).write_text(svg, encoding="utf-8")
        print(f"guide/figures/{name}")


if __name__ == "__main__":
    main()
