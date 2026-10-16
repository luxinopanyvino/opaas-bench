"""Every package `src/opaas` imports is declared in pyproject.toml.

A dependency that only arrives transitively works on the machine where it
happens to be installed and fails where it is not. `cli/audit.py` imported
`click`, which typer stopped depending on in 0.17: the suite passed on a
developer machine that had `click` through `huggingface-hub`, and failed in CI
for five merges. Imports of the optional extras (torch, transformers,
huggingface-hub) are allowed, because they are made inside the functions that
need them.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Import name → the distribution pyproject.toml declares it under.
DECLARED = {
    "httpx": "httpx", "typer": "typer", "pydantic": "pydantic", "rich": "rich",
    "cryptography": "cryptography", "psutil": "psutil", "segno": "segno",
    # The classifiers extra, imported lazily (CLAUDE.md, code conventions).
    "torch": "torch", "transformers": "transformers", "huggingface_hub": "huggingface-hub",
    # The token extra: the root signs from a PKCS#11 hardware token (D65).
    "pkcs11": "python-pkcs11",
    # The dev extra, used only by scripts/ to regenerate the sprite.
    "PIL": "pillow", "numpy": "numpy",
}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_every_third_party_import_is_a_declared_dependency():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    undeclared = {}
    for path in sorted((ROOT / "src" / "opaas").rglob("*.py")):
        for root in _imported_roots(path):
            if root in sys.stdlib_module_names or root in ("opaas", "__future__"):
                continue
            if root not in DECLARED or f'"{DECLARED[root]}' not in pyproject:
                undeclared.setdefault(root, []).append(path.relative_to(ROOT).as_posix())
    assert not undeclared, (
        f"Imported but not declared in pyproject.toml: {undeclared}. Declare it, or use what "
        "a declared dependency already provides."
    )
