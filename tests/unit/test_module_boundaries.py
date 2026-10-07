"""No module of the tool uses a private name of another module.

A name that starts with an underscore can change with no notice. A module that reads such a name
of another module breaks at run time when it does, and one rule then has two owners. What two
modules need has one public home with a docstring.

The test reads the source with ast; it imports nothing of the tool.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = "azsqlcd"
SOURCE = Path(__file__).resolve().parents[2] / "src" / PACKAGE
MODULES = sorted(path.stem for path in SOURCE.glob("*.py"))


def is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def violations(module: str, source: str) -> list[str]:
    """'<module>.py:<line>: <what>' for each private name of another module that the source uses.

    Found: `from azsqlcd.x import _y`, `from azsqlcd import _x`, and `x._y` or `azsqlcd.x._y`
    where x is bound by an import to another module of the tool. A name that an import bound is
    taken to stay that module in the whole file.
    """
    tree = ast.parse(source)
    found: list[str] = []
    bound: dict[str, str] = {}  # local name -> module of the tool

    def note(node: ast.AST, what: str) -> None:
        found.append(f"{module}.py:{getattr(node, 'lineno', 0)}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            parts = node.module.split(".")
            if parts[0] != PACKAGE:
                continue
            for alias in node.names:
                if len(parts) == 1:  # from azsqlcd import x
                    if alias.name in MODULES:
                        bound[alias.asname or alias.name] = alias.name
                    if is_private(alias.name):
                        note(node, f"imports the private module {PACKAGE}.{alias.name}")
                elif is_private(alias.name) and parts[1] != module:  # from azsqlcd.x import _y
                    note(node, f"imports {alias.name} of {node.module}")
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            for alias in node.names:  # from . import x; from .x import _y
                if node.module is None and alias.name in MODULES:
                    bound[alias.asname or alias.name] = alias.name
                elif node.module and is_private(alias.name) and node.module != module:
                    note(node, f"imports {alias.name} of .{node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:  # import azsqlcd.x as y
                parts = alias.name.split(".")
                if parts[0] == PACKAGE and len(parts) == 2 and alias.asname:
                    bound[alias.asname] = parts[1]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not is_private(node.attr):
            continue
        owner = node.value
        if isinstance(owner, ast.Name):
            other = bound.get(owner.id)
        elif (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == PACKAGE
        ):
            other = owner.attr  # azsqlcd.x._y
        else:
            other = None
        if other is not None and other != module:
            note(node, f"reads {other}.{node.attr}")
    return sorted(found)


def test_the_package_has_the_modules_that_the_scan_reads():
    assert {"plan", "runner", "onboard", "lint", "state", "catalog", "config", "names"} <= set(MODULES)


@pytest.mark.parametrize("module", MODULES)
def test_a_module_uses_no_private_name_of_another_module(module):
    source = (SOURCE / f"{module}.py").read_text(encoding="utf-8")
    assert violations(module, source) == []


@pytest.mark.parametrize(
    ("source", "what"),
    [
        ("from azsqlcd import plan\nplan._fence(1, 2)\n", "reads plan._fence"),
        ("from azsqlcd import plan as p\nx = p._PARSEONLY_ON\n", "reads plan._PARSEONLY_ON"),
        ("from azsqlcd import runner\n\ndef f():\n    return runner._Job\n", "reads runner._Job"),
        ("from azsqlcd.lint import _SECRET\n", "imports _SECRET of azsqlcd.lint"),
        ("from azsqlcd.lint import Finding, _secret_findings as found\n", "imports _secret_findings"),
        ("import azsqlcd.state as st\nst._hex('a', 1)\n", "reads state._hex"),
        ("import azsqlcd\nazsqlcd.state._RUN\n", "reads state._RUN"),
        ("from . import catalog\ncatalog._chunks([])\n", "reads catalog._chunks"),
        ("from .names import _KEY\n", "imports _KEY of .names"),
    ],
)
def test_the_scan_finds_each_way_to_reach_a_private_name(source, what):
    (found,) = violations("onboard", source)
    assert what in found and found.startswith("onboard.py:")


@pytest.mark.parametrize(
    "source",
    [
        "from azsqlcd import plan\nplan.check_fence(1, 2)\n",
        "from azsqlcd.plan import Plan, check_fence\n",
        "from azsqlcd import onboard\nonboard._own_helper()\n",  # its own module
        "from azsqlcd.onboard import _own_helper\n",
        "from azsqlcd import plan\nplan.__name__\nplan.__doc__\n",  # not private names
        "import re\nre._cache\nself = object()\nself._x\n",  # not a module of the tool
        "from azsqlcd import plan\n\nclass A:\n    def f(self, run):\n        return run._plan\n",
    ],
)
def test_the_scan_leaves_public_names_own_names_and_other_objects_alone(source):
    assert violations("onboard", source) == []
