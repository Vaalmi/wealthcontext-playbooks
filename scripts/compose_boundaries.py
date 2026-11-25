"""What must hold of the resolved compose configuration.

Only the backend is on WealthContext's network, and only the backend holds the operations
token. The cycle checks this before it starts anything, and a test checks it too.
"""

import json
from typing import Any

BACKEND = "backend"
WEALTHCONTEXT_NETWORK = "wcplaybooks-wealthcontext_default"
TOKEN = "WEALTHCONTEXT_TOKEN"
# The one way out, for a live model's provider, and only in a live run.
PROVIDER_NETWORK = "provider"


def breaks(config: dict[str, Any], *, live: bool = False) -> list[str]:
    services: dict[str, dict] = config["services"]
    networks: dict[str, dict] = {k: v or {} for k, v in config.get("networks", {}).items()}
    found: list[str] = []

    theirs = {
        key
        for key, network in networks.items()
        if network.get("external") or network.get("name") == WEALTHCONTEXT_NETWORK
    }
    joined = {name for name, service in services.items() if theirs & set(service["networks"])}
    if joined != {BACKEND}:
        found.append("only the backend is on WealthContext's network")

    holders = {name for name, s in services.items() if TOKEN in (s.get("environment") or {})}
    if holders != {BACKEND}:
        found.append("only the backend has the operations token")
    token = (services[BACKEND].get("environment") or {}).get(TOKEN)

    for name, service in services.items():
        for port in service.get("ports") or []:
            if port.get("host_ip") != "127.0.0.1":
                found.append(f"{name} publishes a port beyond loopback")
        if name == BACKEND:
            continue
        if token and token in json.dumps(service):
            found.append(f"{name} carries the operations token")
        for setting in ("network_mode", "pid", "privileged", "extra_hosts", "ports"):
            if service.get(setting):
                found.append(f"{name} sets {setting}")
        # A mount could hand an agent the token's file, the database or the Docker socket.
        for setting in ("volumes", "secrets", "configs", "volumes_from"):
            if service.get(setting):
                found.append(f"{name} mounts {setting}")
        for network in service["networks"]:
            if network in theirs or networks.get(network, {}).get("internal"):
                continue
            if not (live and network == PROVIDER_NETWORK):
                found.append(f"{name} has a way out through {network}")
    return found
