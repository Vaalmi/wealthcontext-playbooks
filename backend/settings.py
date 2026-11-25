"""Every setting of the backend, read once from the environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database: Path
    playbooks: Path
    # It names accounts and households, so it is never in the playbook folder.
    triage_log: Path
    wealthcontext_url: str
    agent_url: str
    # Secrets are left out of the repr, so that none reaches a log or a failed assertion.
    wealthcontext_token: str = field(repr=False)
    analyst_token: str = field(repr=False)
    playbook_owner_token: str = field(repr=False)
    triage_agent_key: str = field(repr=False)
    # With no key, the curator's operations refuse. With no model, none is started.
    curator_key: str = field(default="", repr=False)
    curator_url: str = "http://curator:8000"
    curator_model: str = ""
    # The held-out cases: compared by hash with each bundle, and run by the gate.
    cases: Path = Path("cases")
    # The model the gate records for its held-out trials, which the triage agent runs.
    # With none, the gate does not run.
    triage_model: str = ""
    # The most one gate run's trials may cost, in dollars.
    gate_max_dollars: str = "10"
    # Whether this layer offers the recorded cycle, which plays the firm's people. Only a
    # layer started for it does, and it runs once, on records that are still empty.
    cycle: bool = False
    # The firm's sources as committed: what the recorded cycle's people hand in.
    sources: Path = Path("sources")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] = os.environ) -> "Settings":
        get = environment.get
        return cls(
            database=Path(get("PLAYBOOKS_DATABASE", "data/playbooks.sqlite")),
            playbooks=Path(get("PLAYBOOKS_FOLDER", "playbooks")),
            triage_log=Path(get("PLAYBOOKS_TRIAGE_LOG", "data/triage-log.jsonl")),
            wealthcontext_url=get("WEALTHCONTEXT_URL", "http://app:8000"),
            agent_url=get("TRIAGE_AGENT_URL", "http://triage-agent:8000"),
            wealthcontext_token=get("WEALTHCONTEXT_TOKEN", ""),
            analyst_token=get("PLAYBOOKS_ANALYST_TOKEN", ""),
            playbook_owner_token=get("PLAYBOOKS_OWNER_TOKEN", ""),
            triage_agent_key=get("TRIAGE_AGENT_KEY", ""),
            curator_key=get("CURATOR_KEY", ""),
            curator_url=get("CURATOR_URL", "http://curator:8000"),
            curator_model=get("CURATOR_MODEL", ""),
            cases=Path(get("PLAYBOOKS_CASES", "cases")),
            triage_model=get("TRIAGE_MODEL", ""),
            gate_max_dollars=get("GATE_MAX_DOLLARS", "10"),
            cycle=get("PLAYBOOKS_CYCLE", "") == "recorded",
            sources=Path(get("PLAYBOOKS_SOURCES", "sources")),
        )
