"""Architecture rule: the core never imports an integration package.

Integrations are discovered through entry points. If anything under
crosslayer/core imported crosslayer.integrations, adding a new target would
require editing the core, which is exactly what the platform must avoid.
"""

from __future__ import annotations

import ast
from pathlib import Path

import crosslayer.core

CORE_DIR = Path(crosslayer.core.__file__).parent


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_core_does_not_import_integrations():
    offenders = {
        str(path.relative_to(CORE_DIR)): sorted(m for m in _imports(path) if m.startswith("crosslayer.integrations"))
        for path in CORE_DIR.rglob("*.py")
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert offenders == {}, f"core modules import integrations: {offenders}"


def test_core_has_no_third_party_dependencies_beyond_pyyaml():
    allowed_third_party = {"yaml"}
    stdlib = set(__import__("sys").stdlib_module_names)
    found: set[str] = set()
    for path in CORE_DIR.rglob("*.py"):
        for module in _imports(path):
            top = module.split(".")[0]
            if top not in stdlib and top != "crosslayer":
                found.add(top)
    assert found <= allowed_third_party, f"unexpected core dependencies: {found - allowed_third_party}"
