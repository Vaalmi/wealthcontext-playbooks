"""WealthContext is only read: a hold instruction is wording, and a failed read writes nothing."""

import shutil

import firm
import pytest
from layer import CROSS_CHECK_DAY, CROSS_CHECK_OPTION, CUTOFF, DAY, OPTION, ROOT
from sqlalchemy import create_engine, text
from stub_model import call

from cli.main import describe

READS = ["/api/get_data_quality_report", "/api/feed_runs", "/api/feed_run_impact"]


@pytest.mark.parametrize("day, option", [(DAY, OPTION), (CROSS_CHECK_DAY, CROSS_CHECK_OPTION)])
@pytest.mark.parametrize("hold", [True, False])
def test_a_hold_instruction_is_wording_and_calls_nothing(make_layer, tmp_path, day, option, hold):
    folder = tmp_path / "playbooks"
    shutil.copytree(ROOT / "playbooks", folder)
    if not hold:
        for file in folder.glob("*/1.yaml"):
            file.write_text(file.read_text().replace("hold_report: true", "hold_report: false"))
    firm.found(folder)  # with its approval, so that the note gives the action
    layer = make_layer(playbooks=folder, replies=lambda: [call("choose_step", option=option)])

    run = layer.triage(day)
    page = layer.request("GET", "/").text
    said = describe(layer.run(run["id"]))

    assert run["state"] == "completed"
    assert run["notes"][0]["action"]["hold_report"] is hold
    if hold:
        assert "This is an instruction to a person and blocks nothing." in page
        assert "hold the advisor's report (an instruction to a person)" in said
    else:
        assert "Do not hold the advisor's report." in page
        assert "do not hold the advisor's report" in said
    # Everything that reached WealthContext, after the run, the page and the command's read:
    assert [path for path, _, _ in layer.wealthcontext.asked] == READS
    assert layer.wealthcontext.methods == ["POST"] * 3
    quality, runs, impact = (sent for _, sent, _ in layer.wealthcontext.asked)
    assert quality == runs == {"as_of": day, "known_as_of": CUTOFF}
    assert set(impact) == {"run_id", "known_as_of"}


@pytest.mark.parametrize("operation", [path.removeprefix("/api/") for path in READS])
def test_wealthcontext_down_at_any_read_writes_nothing(make_layer, operation):
    layer = make_layer(replies=list)
    layer.wealthcontext.down_for = {operation}

    response = layer.as_analyst(
        "POST", "/api/triage-runs", json={"date": CROSS_CHECK_DAY, "cutoff": CUTOFF}
    )

    assert response.status_code == 502
    assert response.json() == {"detail": f"WealthContext refused {operation} (HTTP 503)."}
    engine = create_engine(f"sqlite:///{layer.settings.database}")
    with engine.connect() as connection:
        for table in ("runs", "reads", "exceptions", "notices", "notes", "steps"):
            count = connection.execute(text(f"SELECT count(*) FROM triage_{table}")).scalar_one()
            assert count == 0, table
    engine.dispose()
    assert layer.settings.triage_log.read_bytes() == b"", "the triage log was written"
    assert layer.stubs == [], "the agent was asked"
