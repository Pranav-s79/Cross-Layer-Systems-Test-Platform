"""Project scaffolding: `crosslayer new adapter NAME`.

Generates a complete, working adapter project (log-only adapter, event
catalog, contracts, contract tests, sample log, sample experiment, and
tests). The generated tests pass before any edits, so a contributor starts
from green and keeps it green while adapting the adapter to their system.
"""

from __future__ import annotations

import keyword
import re
from importlib.resources import files
from pathlib import Path

from crosslayer.core.errors import ConfigError

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_RESERVED = {"crosslayer", "tests", "test", "examples", "experiments"}

# generated path -> template file
ADAPTER_FILES = {
    "pyproject.toml": "pyproject.toml.tmpl",
    "README.md": "README.md.tmpl",
    "{name}/__init__.py": None,
    "{name}/adapter.py": "adapter.py.tmpl",
    "{name}/contracts.yaml": "contracts.yaml.tmpl",
    "{name}/contract_tests.yaml": "contract_tests.yaml.tmpl",
    "examples/sample.log": "sample.log.tmpl",
    "experiments/analyze_sample.yaml": "experiment.yaml.tmpl",
    "tests/test_{name}.py": "test_adapter.py.tmpl",
}


def class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part) + "Adapter"


def validate_name(name: str) -> None:
    if not _NAME_RE.match(name) or "__" in name or name.endswith("_"):
        raise ConfigError(
            f"adapter name {name!r} must be lowercase snake_case, start with a letter, 2-41 characters"
        )
    if keyword.iskeyword(name) or name in _RESERVED:
        raise ConfigError(f"adapter name {name!r} is reserved")


def render(template: str, name: str) -> str:
    values = {
        "@@NAME@@": name,
        "@@CLASS@@": class_name(name),
        "@@TITLE@@": name.replace("_", " ").title(),
        "@@DIST@@": name.replace("_", "-"),
    }
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    leftover = re.findall(r"@@[A-Z]+@@", template)
    if leftover:
        raise RuntimeError(f"unrendered placeholders {sorted(set(leftover))}")
    return template


def create_adapter_project(name: str, parent: Path) -> Path:
    """Create ``parent/name`` with a working adapter project; return its path."""
    validate_name(name)
    root = parent / name
    if root.exists():
        raise ConfigError(f"{root} already exists")
    templates = files("crosslayer.scaffold").joinpath("templates")
    for target, template in ADAPTER_FILES.items():
        path = root / target.format(name=name)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "" if template is None else render(templates.joinpath(template).read_text(encoding="utf-8"), name)
        path.write_text(text, encoding="utf-8", newline="\n")
    return root
