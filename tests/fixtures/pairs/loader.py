"""Reads the fixture pairs of this directory. README.md describes the layout."""

from pathlib import Path

from azsqlcd.diff import RenameSpec, parse_rename_spec
from azsqlcd.model import Model
from azsqlcd.parse import parse_object_file

ROOT = Path(__file__).resolve().parent
CASES = sorted(p.name for p in ROOT.iterdir() if (p / "base").is_dir())


def object_files(case: str, side: str) -> list[tuple[str, str]]:
    """(path as the parser wants it, text) of every object file of one side, 'base' or 'head'."""
    root = ROOT / case / side
    found = sorted(root.glob("schema/*/*.sql"))
    return [(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")) for path in found]


def model(case: str, side: str) -> Model:
    return Model(obj for path, text in object_files(case, side) for obj in parse_object_file(text, path))


def renames(case: str) -> list[RenameSpec]:
    path = ROOT / case / "renames.txt"
    if not path.exists():
        return []
    return [parse_rename_spec(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
