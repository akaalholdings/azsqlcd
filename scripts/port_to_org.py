"""Rewrite the references to the tool repository after a copy to another organisation, or a rename.

The workflows, the action, the templates, the example, the documents, scripts/setup_repo.py and
some tests name the tool repository as OWNER/REPO, and other repositories and teams of the same
owner (OWNER/db-sales, @OWNER/dba). GitHub resolves these names, so every one must change when the
repository moves. Standard library only: the script runs before `uv sync`.

    python scripts/port_to_org.py --org NEW_ORG                  rewrite; print each file and its count
    python scripts/port_to_org.py --org NEW_ORG --repo NEW_NAME  the repository also has a new name
    python scripts/port_to_org.py --org NEW_ORG --check          change nothing; exit 1 if one is left
    python scripts/port_to_org.py --org C --from B/NAME          a later move: B/NAME is the source

What is a reference: the owner name as a whole word, in any letter case (GitHub compares owner
names without letter case). A name that only holds the owner name (OWNER-eu, OWNERltd, OWNER.com)
is another name and stays. With --repo, the repository name changes only directly after the owner
(OWNER/REPO, OWNER/REPO.git): the command, the package and the action keep their name.

Read: action.yml, README.md, pyproject.toml, and every text file under .github/, templates/,
examples/, docs/, scripts/ and tests/. Not rewritten: this script, which names the source so that
--check knows what to look for after a port, and its test. Never rewritten: src/azsqlcd. A
reference there is reported and makes the exit code 1; remove it by a normal pull request.

Exit code: 0 = done, or --check found nothing; 1 = --check found a reference, or a source file of
the tool holds one; 2 = wrong arguments.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Where the tool was built. A port does not change this line: see NOT_REWRITTEN.
SOURCE = "akaalholdings/azsqlcd"
SCOPE = (
    "action.yml",
    "README.md",
    "pyproject.toml",
    ".github",
    "templates",
    "examples",
    "docs",
    "scripts",
    "tests",
)
NOT_REWRITTEN = ("scripts/port_to_org.py", "tests/unit/test_port_to_org.py")
TOOL_SOURCE = "src/azsqlcd"
CACHE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".git"})

_WORD = "A-Za-z0-9_-"
_OWNER_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Port:
    old_owner: str
    old_repo: str
    new_owner: str
    new_repo: str


def _pattern(owner: str) -> re.Pattern[str]:
    """The owner as a whole word, with the path segment after it when there is one."""
    segment = rf"[{_WORD}]+(?:\.[{_WORD}]+)*"
    return re.compile(
        rf"(?<![{_WORD}]){re.escape(owner)}(?:/(?P<name>{segment}))?(?![{_WORD}])(?!\.[A-Za-z0-9])",
        re.IGNORECASE,
    )


def rewrite(text: str, port: Port) -> tuple[str, int]:
    """The text with every reference to the old owner rewritten, and the number of references changed.

    A reference that already has the new form counts as 0, so a second run changes nothing.
    """
    changed = 0

    def new_form(match: re.Match[str]) -> str:
        nonlocal changed
        name = match.group("name")
        if name is None:
            new = port.new_owner
        else:
            stem, dot_git = (name[:-4], name[-4:]) if name.lower().endswith(".git") else (name, "")
            if stem.lower() == port.old_repo.lower():
                stem = port.new_repo
            new = f"{port.new_owner}/{stem}{dot_git}"
        if new != match.group(0):
            changed += 1
        return new

    return _pattern(port.old_owner).sub(new_form, text), changed


def _text_files(root: Path, top: str) -> list[str]:
    """Relative POSIX paths of the files under root/top, or [top] for a file. Links are not followed."""
    start = root / top
    if start.is_file() and not start.is_symlink():
        return [top]
    found: list[str] = []
    for folder, folders, files in os.walk(start):
        folders[:] = sorted(name for name in folders if name not in CACHE_DIRS)
        for name in files:
            path = Path(folder) / name
            if not path.is_symlink():
                found.append(path.relative_to(root).as_posix())
    return found


def files_in_scope(root: Path) -> list[str]:
    """Every file that a port may rewrite, sorted."""
    found = {path for top in SCOPE for path in _text_files(root, top)}
    return sorted(found - set(NOT_REWRITTEN))


def _read(path: Path) -> str | None:
    """The text of a UTF-8 file with its line ends as they are; None for a file that is not text."""
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def port_files(root: Path, port: Port, check: bool) -> list[tuple[str, int]]:
    """(path, references) for each file in scope that holds an old reference. Writes unless `check`."""
    found: list[tuple[str, int]] = []
    for relative in files_in_scope(root):
        text = _read(root / relative)
        if text is None:
            continue
        new_text, count = rewrite(text, port)
        if count:
            found.append((relative, count))
            if not check:
                # bytes, not text mode: text mode on Windows writes CRLF and the repository pins LF
                (root / relative).write_bytes(new_text.encode("utf-8"))
    return found


def references_in_tool_source(root: Path, port: Port) -> list[tuple[str, int]]:
    """(path, references) for each file of src/azsqlcd that names the old owner. Reads only."""
    found: list[tuple[str, int]] = []
    for relative in sorted(_text_files(root, TOOL_SOURCE)):
        text = _read(root / relative)
        count = rewrite(text, port)[1] if text is not None else 0
        if count:
            found.append((relative, count))
    return found


def run(root: Path, port: Port, check: bool, out: Callable[[str], None]) -> int:
    old, new = f"{port.old_owner}/{port.old_repo}", f"{port.new_owner}/{port.new_repo}"
    out(f"{'check' if check else 'port'}: {old} -> {new}")
    found = port_files(root, port, check)
    for path, count in found:
        out(f"{count:5d}  {path}")
    total = sum(count for _, count in found)
    in_source = references_in_tool_source(root, port)
    for path, count in in_source:
        out(f"{count:5d}  {path}  NOT CHANGED: this script does not rewrite the source of the tool")
    if check:
        out(f"{total} reference(s) to {port.old_owner} left in {len(found)} file(s)")
    else:
        out(f"rewrote {total} reference(s) in {len(found)} file(s)")
    return 1 if in_source or (check and found) else 0


def _owner_and_repo(value: str) -> tuple[str, str]:
    owner, _, repo = value.partition("/")
    if not _OWNER_NAME.fullmatch(owner) or not _REPO_NAME.fullmatch(repo):
        raise argparse.ArgumentTypeError("must be OWNER/REPO")
    return owner, repo


def main(argv: Sequence[str] | None = None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite the references to the tool repository after a copy to another organisation."
    )
    parser.add_argument("--org", required=True, metavar="NEW_ORG", help="owner, in the letter case of GitHub")
    parser.add_argument("--repo", metavar="NEW_REPO_NAME", help="new repository name (default: no change)")
    parser.add_argument(
        "--from",
        dest="source",
        type=_owner_and_repo,
        default=_owner_and_repo(SOURCE),
        metavar="OWNER/REPO",
        help=f"the references to replace (default: {SOURCE})",
    )
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 if a reference is left")
    args = parser.parse_args(argv)
    old_owner, old_repo = args.source
    new_repo = args.repo if args.repo is not None else old_repo
    if not _OWNER_NAME.fullmatch(args.org):
        parser.error("--org is the name of the organisation only: letters, digits and '-', no URL, no '/'")
    if not _REPO_NAME.fullmatch(new_repo) or new_repo in (".", "..") or new_repo.lower().endswith(".git"):
        parser.error("--repo is the name of the repository only: letters, digits, '.', '_' and '-'")
    port = Port(old_owner=old_owner, old_repo=old_repo, new_owner=args.org, new_repo=new_repo)
    return run(root or Path(__file__).resolve().parent.parent, port, args.check, print)


if __name__ == "__main__":
    sys.exit(main())
