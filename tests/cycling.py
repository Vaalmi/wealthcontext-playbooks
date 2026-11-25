"""The recorded cycle, as the tests start it: a layer started for it on the committed founding
versions, a triage model that follows the steps and a curator that gives scripted answers."""

import shutil
import time
from typing import Any

import firm
import gating
import held_out_cases
from layer import ROOT, Layer
from stub_model import ChoosingModel, call

from backend import cycle

DONE = ("done", "stopped")
SUMMARY = gating.SCRIPTED_SUMMARY


def triage_model() -> ChoosingModel:
    """Each step whose conditions the exception meets; a gap report on the correction alone."""

    def gaps(messages: list) -> Any:
        if '"correction"' in messages[1].content:
            return call("file_gap_report", summary=SUMMARY)
        return call("no_gap_report")

    return ChoosingModel(
        messages=iter([]), prompts=[], offered=[], gap_prompts=[], gap_offered=[], gap_answer=gaps
    )


def cycle_layer(make_layer, tmp_path, *answers, **options) -> Layer:
    """A layer started for the recorded cycle, on the committed founding versions. Its curator
    gives these answers, one for each run."""
    waiting = list(answers) or [
        lambda: call("submit_proposal", **gating.honest()),
        lambda: call("no_change"),
    ]
    options.setdefault("choosing", triage_model)
    options.setdefault("curates", lambda: [waiting.pop(0)])
    layer = make_layer(
        playbooks=firm.copy(tmp_path / "playbooks-as-committed"),
        overrides={"cycle": True, "sources": ROOT / "sources"} | options.pop("overrides", {}),
        **options,
    )
    held_out_cases.write(layer.settings.cases)
    hostile = layer.settings.cases / "hostile"
    hostile.mkdir()
    shutil.copy(ROOT / "cases" / "hostile" / f"{cycle.HOSTILE}.yaml", hostile)
    return layer


def press(layer: Layer, **headers: str):
    return layer.request("POST", "/cycle", headers or None, follow_redirects=False)


def played(layer: Layer) -> dict[str, Any]:
    until = time.monotonic() + 120
    while (state := layer.request("GET", "/api/cycle").json())["state"] not in DONE:
        assert time.monotonic() < until, "the cycle did not end"
        time.sleep(0.05)
    return state
