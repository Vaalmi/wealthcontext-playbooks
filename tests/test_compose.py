"""The resolved compose configuration: who is on WealthContext's network, who holds the token."""

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys

import pytest
import yaml
from layer import ROOT

from modelkit.replay import Recording
from scripts import cycle
from scripts.compose_boundaries import breaks

TOKEN = "operations-token-for-this-test"
ENV_FILES = {
    ".local/backend.env": f"WEALTHCONTEXT_TOKEN={TOKEN}\nTRIAGE_AGENT_KEY=key\n",
    ".local/triage-agent.env": "TRIAGE_AGENT_KEY=key\nTRIAGE_MODEL=replay:recordings/x.json\n",
    ".local/curator.env": "CURATOR_KEY=other-key\nCURATOR_MODEL=replay:recordings/y.json\n",
}
MODES = [False, True]
OVERLAYS = {"triage-agent": "compose.live.yaml", "curator": "compose.live-curation.yaml"}


def resolved_here(live: bool, agent: str = "triage-agent") -> dict:
    """Resolved from the files alone, so that the check needs no Docker."""
    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    if live:
        laid_over = yaml.safe_load((ROOT / OVERLAYS[agent]).read_text())
        config["networks"].update(laid_over["networks"])
        for name, changes in laid_over["services"].items():
            service = config["services"][name]
            service["networks"] = sorted({*service["networks"], *changes["networks"]})
    for service in config["services"].values():
        environment = {}
        for file in service.pop("env_file", []):
            pairs = (line.partition("=") for line in ENV_FILES[file].splitlines())
            environment.update({name: value for name, _, value in pairs})
        service["environment"] = environment | service.get("environment", {})
        service["ports"] = [{"host_ip": port.split(":")[0]} for port in service.get("ports", [])]
    return config


@pytest.mark.parametrize("live", MODES)
def test_only_the_backend_reaches_wealthcontext_and_holds_the_token(live):
    config = resolved_here(live)

    assert breaks(config, live=live) == []
    on_their_network = {
        name for name, s in config["services"].items() if "wealthcontext" in s["networks"]
    }
    with_the_token = {name for name, s in config["services"].items() if TOKEN in json.dumps(s)}
    assert on_their_network == with_the_token == {"backend"}


@pytest.mark.skipif(not shutil.which("docker"), reason="the files are resolved without Docker too")
@pytest.mark.parametrize("live", MODES)
def test_compose_itself_resolves_to_the_same_boundaries(tmp_path, live):
    files = ["compose.yaml", "compose.live.yaml"] if live else ["compose.yaml"]
    named = []
    for file in files:
        (tmp_path / file).write_text((ROOT / file).read_text())
        named += ["-f", str(tmp_path / file)]
    for file, content in ENV_FILES.items():
        (tmp_path / file).parent.mkdir(exist_ok=True)
        (tmp_path / file).write_text(content)
    command = ["docker", "compose", "--env-file", os.devnull, *named, "config", "--format", "json"]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0 and "is not a docker command" in done.stderr:
        pytest.skip("no Compose here: the files are resolved without it too")
    assert done.returncode == 0, done.stderr

    config = json.loads(done.stdout)

    assert breaks(config, live=live) == []
    for name in ("triage-agent", "curator"):
        agent = config["services"][name]
        assert "wealthcontext" not in agent["networks"] and TOKEN not in json.dumps(agent)


BROKEN = {
    "on their network": lambda c: c["services"]["triage-agent"]["networks"].append("wealthcontext"),
    "given the token": lambda c: c["services"]["triage-agent"]["environment"].update(
        WEALTHCONTEXT_TOKEN=TOKEN
    ),
    "given it renamed": lambda c: c["services"]["triage-agent"]["environment"].update(OTHER=TOKEN),
    "host networking": lambda c: c["services"]["triage-agent"].update(network_mode="host"),
    "a mount": lambda c: c["services"]["triage-agent"].update(volumes=["./.local:/local"]),
    "a way out": lambda c: c["services"]["triage-agent"]["networks"].append("edge"),
    "agents not internal": lambda c: c["networks"]["agents"].update(internal=False),
    "beyond loopback": lambda c: c["services"]["backend"]["ports"].append({"host_ip": "0.0.0.0"}),
    "the curator on their network": lambda c: c["services"]["curator"]["networks"].append(
        "wealthcontext"
    ),
    "the curator given the token": lambda c: c["services"]["curator"]["environment"].update(
        WEALTHCONTEXT_TOKEN=TOKEN
    ),
    "the curator given the cases": lambda c: c["services"]["curator"].update(
        volumes=["./cases:/app/cases"]
    ),
    "curation not internal": lambda c: c["networks"]["curation"].update(internal=False),
    "a third service": lambda c: c["services"].update(
        other={"networks": ["wealthcontext"], "environment": {}}
    ),
}


@pytest.mark.parametrize("broken", BROKEN)
def test_the_check_fails_when_a_boundary_is_broken(broken):
    config = copy.deepcopy(resolved_here(False))
    BROKEN[broken](config)

    assert breaks(config)


def test_the_way_out_is_for_a_live_run_only():
    assert breaks(resolved_here(True), live=False)


def test_the_cycle_hands_docker_no_key_from_the_shell(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key-in-the-shell")
    monkeypatch.setenv("OPENAI_API_KEY", "a-key-in-the-shell")
    monkeypatch.setenv("OPENAI_ORG_ID", "an-organization")
    monkeypatch.setenv("SOME_OTHER_SECRET", "another")

    given = cycle.environment()

    assert given["ANTHROPIC_API_KEY"] == given["OPENAI_API_KEY"] == ""
    assert "SOME_OTHER_SECRET" not in given and "OPENAI_ORG_ID" not in given


def test_a_live_run_reads_the_key_from_the_env_file_only(monkeypatch, tmp_path):
    monkeypatch.setattr(cycle, "ROOT", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key-in-the-shell")

    with pytest.raises(cycle.Stopped, match="No live run"):
        cycle.model_settings(live=True)
    assert cycle.model_settings(live=False) == {"TRIAGE_MODEL": cycle.REPLAY}


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("anthropic:claude-haiku-5-5", "ANTHROPIC_API_KEY"),
        ("openai:gpt-6-luna", "OPENAI_API_KEY"),
        ("openai:gpt-6.1-terra:high", "OPENAI_API_KEY"),
    ],
)
def test_a_live_run_takes_either_providers_key_and_that_key_alone(
    monkeypatch, tmp_path, model, key
):
    monkeypatch.setattr(cycle, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(f"TRIAGE_MODEL={model}\nANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n")

    taken = cycle.model_settings(live=True)

    assert taken == {
        "TRIAGE_MODEL": model,
        key: {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}[key],
    }


@pytest.mark.parametrize(
    ("model", "why"),
    [
        ("openai:gpt-6.2-sol:none", "names a reasoning effort its model does not take"),
        ("anthropic:claude-haiku-5-5:high", "names a reasoning effort its model does not take"),
        ("openai:gpt-6-unpriced", "names a model with no price here"),
    ],
)
def test_no_live_target_starts_on_a_model_or_effort_it_cannot_run(
    monkeypatch, tmp_path, model, why
):
    monkeypatch.setattr(cycle, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"CURATOR_MODEL={model}\nANTHROPIC_API_KEY=a\nOPENAI_API_KEY=o\n"
    )

    with pytest.raises(cycle.Stopped, match=why):
        cycle.curator_settings(live=True)


def test_the_agents_settings_file_never_holds_the_operations_token(monkeypatch, tmp_path):
    monkeypatch.setattr(cycle, "LOCAL", tmp_path / "local")

    cycle.write_settings(TOKEN, {"TRIAGE_MODEL": cycle.REPLAY})

    assert TOKEN in (tmp_path / "local" / "backend.env").read_text()
    for other in ("triage-agent.env", "playbooks.env"):
        assert TOKEN not in (tmp_path / "local" / other).read_text()
    assert (tmp_path / "local" / "backend.env").stat().st_mode & 0o077 == 0


def test_the_curators_settings_file_holds_its_key_and_no_other(monkeypatch, tmp_path):
    monkeypatch.setattr(cycle, "LOCAL", tmp_path / "local")

    cycle.write_settings(TOKEN, {"TRIAGE_MODEL": cycle.REPLAY})

    def written(name: str) -> dict[str, str]:
        return cycle.settings_in(tmp_path / "local" / f"{name}.env")

    curator, backend = written("curator"), written("backend")
    assert curator.keys() == {"CURATOR_KEY", "CURATOR_MAX_DOLLARS", "CURATOR_MODEL"}
    assert curator["CURATOR_MAX_DOLLARS"] == "2"
    # The backend records the model the curator is configured with: one setting, written twice.
    assert curator["CURATOR_MODEL"] == backend["CURATOR_MODEL"] == cycle.CURATION_REPLAY
    assert curator["CURATOR_KEY"] == backend["CURATOR_KEY"] != backend["TRIAGE_AGENT_KEY"]
    assert "CURATOR_KEY" not in written("triage-agent") | written("playbooks")
    assert TOKEN not in (tmp_path / "local" / "curator.env").read_text()


def test_the_curator_shares_no_network_with_the_triage_agent():
    services = resolved_here(False)["services"]

    assert services["curator"]["networks"] == ["curation"]
    assert not set(services["curator"]["networks"]) & set(services["triage-agent"]["networks"])


def test_the_curators_image_holds_no_case_source_playbook_or_backend():
    stages = (ROOT / "Dockerfile").read_text().split("\nFROM ")
    (stage,) = [each for each in stages if each.splitlines()[0].endswith(" AS curator")]
    copied = [line.split()[1] for line in stage.splitlines() if line.startswith("COPY")]

    assert copied == [
        "modelkit",
        "curator",
        "recordings/scripted-curation.json",
        "recordings/first-cycle-curation.json",
    ]


def test_the_curators_recordings_hold_only_its_own_replies():
    """The triage agent's recording holds the held-out trials' replies, which tell what the
    expected notes say. The curator's image is given a recording of its own instead."""
    curator_tools = {"submit_proposal", "no_change"}
    for name in ("scripted-curation.json", "first-cycle-curation.json"):
        replies = json.loads((ROOT / "recordings" / name).read_text())["replies"]
        assert replies and {c["name"] for r in replies for c in r["tool_calls"]} <= curator_tools


def test_a_live_curation_reads_its_model_and_key_from_the_env_file_only(monkeypatch, tmp_path):
    monkeypatch.setattr(cycle, "ROOT", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key-in-the-shell")
    monkeypatch.setenv("CURATOR_MODEL", "anthropic:claude-opus-5-5")

    with pytest.raises(cycle.Stopped, match="No live run: .env needs CURATOR_MODEL"):
        cycle.curator_settings(live=True)
    (tmp_path / ".env").write_text(
        "CURATOR_MODEL=anthropic:claude-opus-5-5\nANTHROPIC_API_KEY=placeholder\n"
    )
    assert cycle.curator_settings(live=True) == {
        "CURATOR_MODEL": "anthropic:claude-opus-5-5",
        "ANTHROPIC_API_KEY": "placeholder",
    }


@pytest.mark.parametrize("agent", OVERLAYS)
def test_in_a_live_run_only_the_agent_on_a_live_model_has_a_way_out(agent):
    config = resolved_here(True, agent)

    assert breaks(config, live=True) == []
    assert breaks(config, live=False)
    out = {name for name, s in config["services"].items() if "provider" in s["networks"]}
    assert out == {agent}


@pytest.mark.parametrize("curation", MODES)
def test_the_cycle_lays_over_that_agents_file_and_no_other(curation):
    given = " ".join(cycle.ours("up", live=True, curation=curation))

    assert ("compose.live-curation.yaml" in given) is curation
    assert ("compose.live.yaml" in given) is not curation


@pytest.mark.parametrize(
    ("model", "key"),
    [("anthropic:claude-opus-5-5", "ANTHROPIC_API_KEY"), ("openai:gpt-6-luna", "OPENAI_API_KEY")],
)
def test_the_backend_records_the_trials_model_and_holds_no_providers_key(
    monkeypatch, tmp_path, model, key
):
    monkeypatch.setattr(cycle, "LOCAL", tmp_path / "local")
    curating = {"CURATOR_MODEL": "openai:gpt-6.1-terra:high", "OPENAI_API_KEY": "placeholder"}

    cycle.write_settings(TOKEN, {"TRIAGE_MODEL": model, key: "placeholder"}, curating=curating)

    backend = cycle.settings_in(tmp_path / "local" / "backend.env")
    agent = cycle.settings_in(tmp_path / "local" / "triage-agent.env")
    curator = cycle.settings_in(tmp_path / "local" / "curator.env")
    # The gate records the model its held-out trials ran on: one setting, written twice.
    assert backend["TRIAGE_MODEL"] == agent["TRIAGE_MODEL"] == model
    assert backend["CURATOR_MODEL"] == curator["CURATOR_MODEL"] == "openai:gpt-6.1-terra:high"
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} & set(backend)
    assert agent[key] == curator["OPENAI_API_KEY"] == "placeholder"
    # Each agent holds its own provider's key and no other.
    assert set(agent) - {key} == {"TRIAGE_AGENT_KEY", "TRIAGE_MAX_DOLLARS", "TRIAGE_MODEL"}


@pytest.mark.parametrize(
    ("live", "correction", "day"),
    [(True, False, "2026-10-15"), (True, True, "2026-10-09"), (False, True, "2026-10-09")],
)
def test_each_part_triages_the_date_it_is_for(monkeypatch, live, correction, day):
    triaged, corrected = [], []
    monkeypatch.setattr(cycle, "start", lambda *a, **k: None)
    monkeypatch.setattr(cycle, "unchanged", lambda: None)
    monkeypatch.setattr(cycle, "settings_in", lambda path: {"PLAYBOOKS_URL": "http://127.0.0.1:1"})
    monkeypatch.setattr(cycle, "describe", lambda result: "")
    monkeypatch.setattr(cycle, "differences", lambda result, event: [])
    monkeypatch.setattr(
        cycle, "triage", lambda d, at=None: triaged.append(d) or {"state": "completed", "date": d}
    )
    monkeypatch.setattr(
        cycle, "correct", lambda result, at=None: corrected.append(result["date"]) or []
    )
    given = argparse.Namespace(live=live, correction=correction, curation=False)

    assert cycle.parts(given) == 0

    assert triaged == [day]
    assert corrected == ([day] if correction else [])


def test_the_page_target_starts_for_the_recorded_cycle_and_plays_nothing(monkeypatch, capsys):
    started = []
    monkeypatch.setattr(cycle, "start", lambda *a, **k: started.append((a, k)))
    monkeypatch.setattr(cycle, "play", lambda: pytest.fail("the cycle was played"))
    monkeypatch.setattr(cycle, "command", lambda *a, **k: pytest.fail("a command was run"))
    monkeypatch.setattr(cycle, "unchanged", lambda: None)
    monkeypatch.setattr(cycle, "settings_in", lambda path: {"PLAYBOOKS_URL": "http://127.0.0.1:1"})
    monkeypatch.setattr(sys, "argv", ["cycle", "--page"])

    assert cycle.main() == 0

    # Not live and not recording: the committed recordings, the button offered.
    assert started == [((False,), {"cycle": True})]
    said = capsys.readouterr().out
    assert "The page: http://127.0.0.1:1/" in said
    assert "Press its button to run the recorded cycle, or run `uv run playbooks cycle`." in said


@pytest.mark.parametrize("part", ["--correction", "--curation"])
def test_the_page_target_is_for_the_recorded_cycle_alone(monkeypatch, part):
    monkeypatch.setattr(cycle, "start", lambda *a, **k: pytest.fail("started"))
    monkeypatch.setattr(sys, "argv", ["cycle", "--page", part])

    with pytest.raises(SystemExit):
        cycle.main()


@pytest.mark.parametrize("setting", ["TRIAGE_MODEL", "CURATOR_MODEL"])
def test_no_live_target_starts_on_the_cases_writer(monkeypatch, tmp_path, setting):
    (tmp_path / ".env").write_text(
        f"{setting}=anthropic:gemini-4-neon\nANTHROPIC_API_KEY=placeholder\n"
    )
    monkeypatch.setattr(cycle, "ROOT", tmp_path)

    with pytest.raises(cycle.Stopped, match="wrote the held-out cases' expected notes"):
        cycle._from_env(setting)


def _recording(
    text: str = "",
    args: dict | None = None,
    invalid: str | None = None,
    name: str = "choose_step",
) -> str:
    reply = {
        "request": "0" * 64,
        "text": text,
        "tool_calls": [{"name": name, "args": args or {"option": "a/b"}}],
        "invalid_tool_calls": [{"name": "choose_step", "args": invalid}] if invalid else [],
    }
    return json.dumps({"origin": "recorded", "about": "a live cycle", "replies": [reply]})


@pytest.mark.parametrize("path", sorted((ROOT / "recordings").glob("*.json")), ids=str)
def test_every_committed_recording_passes_what_a_live_one_must(path):
    assert cycle.unpublishable(Recording.read(path)) is None


@pytest.mark.parametrize(
    ("recording", "rule"),
    [
        (_recording("Quillon Vantree WC-AC-0001"), "client_identifier"),
        (_recording(args={"summary": "Ring Quillon Vantree first"}), "personal_data"),
        (_recording(invalid='{"summary": "Write to someone.else@example.com"}'), "personal_data"),
        # A tool it was not offered, refused and answered again: its name is the model's too.
        (_recording(name="quillon-vantree"), "personal_data"),
        # A number is read as its digits, as the identifier rule reads text; a yes or no is not.
        (_recording(args={"version": 123456}), "client_identifier"),
        (_recording(args={"hold_report": True, "version": 2}), None),
        (_recording("hidden​words"), "not_plain_text"),
    ],
)
def test_a_live_reply_that_breaks_a_rule_is_found_wherever_it_is(recording, rule):
    assert cycle.unpublishable(Recording.model_validate_json(recording)) == rule


def test_no_recording_is_written_when_either_breaks_a_rule(monkeypatch, tmp_path):
    written = {
        cycle.RECORDING_IN["TRIAGE_MODEL"]: _recording(),
        cycle.RECORDING_IN["CURATOR_MODEL"]: _recording("Quillon Vantree said so"),
    }
    monkeypatch.setattr(
        cycle,
        "RECORDED",
        {setting: tmp_path / path.name for setting, path in cycle.RECORDED.items()},
    )
    monkeypatch.setattr(cycle, "run", lambda *command, **_: written[command[-1]])

    with pytest.raises(cycle.Stopped) as stopped:
        cycle.keep_recordings()

    assert "first-cycle-curation.json is not written" in str(stopped.value)
    assert "personal_data" in str(stopped.value) and "Quillon" not in str(stopped.value)
    assert list(tmp_path.iterdir()) == []
