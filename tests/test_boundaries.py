"""What each part may import, and who may make an HTTP client."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("triage_agent", "curator")

# Each module of an agent, and that agent's modules it may import. Both agents are alike.
ALLOWED = {
    "connection": set(),
    "api": {"connection"},
    "tools": {"api", "connection"},
    "harness": {"api", "connection", "tools"},
    "entrypoint": {"connection", "harness"},
}
CONNECTION = "connection"
HTTP_CLIENT = "httpx2.Client("
APP = "entrypoint"
# What a deployable never imports.
NEVER = {
    "triage_agent": {"backend", "cli", "scripts", "curator"},
    "curator": {"backend", "cli", "scripts", "triage_agent"},
    "modelkit": {"backend", "cli", "scripts", "triage_agent", "curator"},
    "backend": {"triage_agent", "curator", "modelkit", "cli", "scripts", "langchain", "langgraph"},
    "cli": {"backend", "triage_agent", "curator", "modelkit", "scripts"},
}


def tree_of(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def own_imports(package: str, module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree_of(ROOT / package / f"{module}.py")):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            if node.module:
                names.add(node.module.split(".")[0])
            else:
                names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == package:
                names |= {alias.name for alias in node.names}
            elif node.module.startswith(f"{package}."):
                names.add(node.module.removeprefix(f"{package}.").split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{package}."):
                    names.add(alias.name.removeprefix(f"{package}.").split(".")[0])
    return names


def imported_packages(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree_of(path)):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
    return found


@pytest.mark.parametrize("package", PACKAGES)
def test_every_module_is_listed(package):
    assert {path.stem for path in (ROOT / package).glob("*.py")} - {"__init__"} == set(ALLOWED)


@pytest.mark.parametrize("package", PACKAGES)
def test_each_module_imports_only_what_it_may(package):
    for module, allowed in ALLOWED.items():
        assert own_imports(package, module) <= allowed, module


@pytest.mark.parametrize("package", PACKAGES)
def test_nothing_imports_the_app(package):
    for module in ALLOWED:
        assert APP not in own_imports(package, module), module


@pytest.mark.parametrize("package", PACKAGES)
def test_only_the_connection_makes_an_http_client(package):
    for module in ALLOWED.keys() - {CONNECTION}:
        source = (ROOT / package / f"{module}.py").read_text(encoding="utf-8")
        assert HTTP_CLIENT not in source, module


@pytest.mark.parametrize("package", sorted(NEVER))
def test_a_deployable_never_imports_what_it_must_not(package):
    for path in (ROOT / package).rglob("*.py"):
        assert not imported_packages(path) & NEVER[package], path.name
