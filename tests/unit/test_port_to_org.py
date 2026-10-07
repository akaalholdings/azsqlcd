"""scripts/port_to_org.py: the references to the tool repository after a copy to another organisation.

GitHub resolves `uses: OWNER/REPO@tag`, `@OWNER/team` and `repo:OWNER/NAME` by name. One reference
that keeps the old owner is a workflow that does not start, or a federated credential that does
not match. So the tests ask for every reference, and for nothing else.

The fixtures are small trees under tmp_path. One test ports a copy of the files of this
repository; it imports nothing from the copy and reads text only.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "port_to_org.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("port_to_org", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


port_to_org = _load()

OLD = port_to_org.SOURCE  # "<owner>/<repository>" of the place where the tool was built
OLD_OWNER, OLD_REPO = OLD.split("/")
PORT = port_to_org.Port(old_owner=OLD_OWNER, old_repo=OLD_REPO, new_owner="contoso", new_repo=OLD_REPO)

TEMPLATE_DB = "templates/db-repo/.github/workflows/db.yml"
EXAMPLE_DB = "examples/demo-db/.github/workflows/db.yml"

# One file for each place that the script reads, with each form of a reference that the repository has.
TREE = {
    "action.yml": f"# the action of {OLD}\nname: azsqlcd\n",
    ".github/workflows/stage.yml": (
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
        f"      - uses: {OLD}@v0.1.0\n"
    ),
    TEMPLATE_DB: f"    uses: {OLD}/.github/workflows/stage.yml@v0.1.0\n",
    "templates/db-repo/.github/CODEOWNERS": f"/schema/**        @{OLD_OWNER}/dba\n",
    EXAMPLE_DB: f"    uses: {OLD}/.github/workflows/verify.yml@v0.1.0\n",
    "scripts/setup_repo.py": f'TOOL_REPO = "{OLD}"\n',
    "docs/setup.md": (
        f"`astral-sh/setup-uv`, and `{OLD_OWNER}/*`.\n"
        f"`gh search issues --owner {OLD_OWNER} --label azsqlcd-incident --state open`\n"
        f"The subject is `repo:{OLD_OWNER}/db-sales:environment:dev-plan`.\n"
    ),
    "README.md": f"git clone https://github.com/{OLD}\ngit clone https://github.com/{OLD}.git\ncd azsqlcd\n",
    "pyproject.toml": f'[project]\nname = "azsqlcd"\n\n[project.urls]\nsource = "https://github.com/{OLD}"\n',
    "tests/unit/test_setup_repo.py": (
        f'REPO = "{OLD_OWNER}/db-sales"\nassert parts[1:] == ["{OLD_OWNER}", "teams", "dba"]\n'
    ),
}
PORTED = {
    "action.yml": "# the action of contoso/azsqlcd\nname: azsqlcd\n",
    ".github/workflows/stage.yml": (
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
        "      - uses: contoso/azsqlcd@v0.1.0\n"
    ),
    TEMPLATE_DB: "    uses: contoso/azsqlcd/.github/workflows/stage.yml@v0.1.0\n",
    "templates/db-repo/.github/CODEOWNERS": "/schema/**        @contoso/dba\n",
    EXAMPLE_DB: "    uses: contoso/azsqlcd/.github/workflows/verify.yml@v0.1.0\n",
    "scripts/setup_repo.py": 'TOOL_REPO = "contoso/azsqlcd"\n',
    "docs/setup.md": (
        "`astral-sh/setup-uv`, and `contoso/*`.\n"
        "`gh search issues --owner contoso --label azsqlcd-incident --state open`\n"
        "The subject is `repo:contoso/db-sales:environment:dev-plan`.\n"
    ),
    "README.md": (
        "git clone https://github.com/contoso/azsqlcd\n"
        "git clone https://github.com/contoso/azsqlcd.git\n"
        "cd azsqlcd\n"
    ),
    "pyproject.toml": (
        '[project]\nname = "azsqlcd"\n\n[project.urls]\nsource = "https://github.com/contoso/azsqlcd"\n'
    ),
    "tests/unit/test_setup_repo.py": (
        'REPO = "contoso/db-sales"\nassert parts[1:] == ["contoso", "teams", "dba"]\n'
    ),
}


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def read_tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def run(root: Path, *args: str) -> int:
    return port_to_org.main(list(args), root=root)


# ----------------------------------------------------------------------------- the port


def test_every_reference_is_rewritten_and_the_check_then_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root = write_tree(tmp_path, TREE)
    before = read_tree(root)

    assert run(root, "--org", "contoso", "--check") == 1
    assert read_tree(root) == before  # --check writes nothing
    assert f"14 reference(s) to {OLD_OWNER} left in 10 file(s)" in capsys.readouterr().out

    assert run(root, "--org", "contoso") == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == f"port: {OLD} -> contoso/azsqlcd"
    assert "    3  docs/setup.md" in printed and "    2  README.md" in printed  # each file with its count
    assert printed[-1] == "rewrote 14 reference(s) in 10 file(s)"
    assert {path: data.decode("utf-8") for path, data in read_tree(root).items()} == PORTED

    assert run(root, "--org", "contoso", "--check") == 0
    assert "0 reference(s)" in capsys.readouterr().out


def test_a_second_run_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    root = write_tree(tmp_path, TREE)
    assert run(root, "--org", "contoso", "--repo", "sql-cicd") == 0
    after_first = read_tree(root)
    capsys.readouterr()

    assert run(root, "--org", "contoso", "--repo", "sql-cicd") == 0
    assert read_tree(root) == after_first
    assert capsys.readouterr().out.splitlines()[1:] == ["rewrote 0 reference(s) in 0 file(s)"]


@pytest.mark.parametrize(
    "line",
    [
        f"uses: {OLD_OWNER}-eu/azsqlcd@v0.1.0",  # another organisation: '-' is a letter of an owner name
        f"uses: {OLD_OWNER}ltd/azsqlcd@v0.1.0",
        f"uses: not{OLD_OWNER}/azsqlcd@v0.1.0",
        f"uses: my-{OLD_OWNER}/azsqlcd@v0.1.0",
        f"mail info@{OLD_OWNER}.co.uk",  # a DNS name, not an owner
        f"https://{OLD_OWNER}.github.io/azsqlcd",
        f"backup_{OLD_OWNER}_2026",
    ],
)
def test_the_organisation_name_inside_an_unrelated_word_is_not_touched(line: str):
    assert port_to_org.rewrite(line, PORT) == (line, 0)


@pytest.mark.parametrize(
    ("line", "ported"),
    [
        (f"uses: {OLD}@v0.1.0", "uses: contoso/azsqlcd@v0.1.0"),
        (f"uses: {OLD.upper()}@v0.1.0", "uses: contoso/azsqlcd@v0.1.0"),  # GitHub ignores the letter case
        (f"`{OLD_OWNER}/*`", "`contoso/*`"),
        (f"@{OLD_OWNER}/dba", "@contoso/dba"),
        (f"see {OLD}.", "see contoso/azsqlcd."),  # the dot ends the sentence
        (f"the organisation {OLD_OWNER}.", "the organisation contoso."),
    ],
)
def test_a_reference_is_the_owner_as_a_whole_word_in_any_letter_case(line: str, ported: str):
    assert port_to_org.rewrite(line, PORT) == (ported, 1)


def test_a_new_repository_name_replaces_only_the_tool_repository():
    renamed = port_to_org.Port(
        old_owner=OLD_OWNER, old_repo=OLD_REPO, new_owner="contoso", new_repo="sql-cicd"
    )
    text = (
        f"uses: {OLD}@v0.1.0\n"
        f"git clone https://github.com/{OLD}.git\n"
        f"repo:{OLD_OWNER}/db-sales:environment:prod:job_workflow_ref:{OLD}/.github/workflows/stage.yml@refs/tags/v0.1.0\n"
        f"{OLD}-demo is another repository\n"
        "uv run azsqlcd lint; label azsqlcd-incident; schema [azsqlcd]\n"
    )
    assert port_to_org.rewrite(text, renamed) == (
        "uses: contoso/sql-cicd@v0.1.0\n"
        "git clone https://github.com/contoso/sql-cicd.git\n"
        "repo:contoso/db-sales:environment:prod:job_workflow_ref:contoso/sql-cicd/.github/workflows/stage.yml@refs/tags/v0.1.0\n"
        "contoso/azsqlcd-demo is another repository\n"
        "uv run azsqlcd lint; label azsqlcd-incident; schema [azsqlcd]\n",  # the command keeps its name
        5,
    )


def test_a_rename_inside_the_same_organisation_is_stable(tmp_path: Path):
    root = write_tree(tmp_path, {"action.yml": f"uses: {OLD}@v0.1.0 and {OLD_OWNER}/db-sales\n"})
    assert run(root, "--org", OLD_OWNER, "--repo", "sql-cicd") == 0
    assert (root / "action.yml").read_text(encoding="utf-8") == (
        f"uses: {OLD_OWNER}/sql-cicd@v0.1.0 and {OLD_OWNER}/db-sales\n"
    )
    assert run(root, "--org", OLD_OWNER, "--repo", "sql-cicd", "--check") == 0


def test_a_later_move_names_its_own_source(tmp_path: Path):
    root = write_tree(tmp_path, {"action.yml": f"uses: {OLD}@v0.1.0\n"})
    assert run(root, "--org", "contoso") == 0
    assert run(root, "--org", "fabrikam", "--check") == 0  # nothing names the first source any more
    assert run(root, "--org", "fabrikam", "--from", "contoso/azsqlcd", "--check") == 1
    assert run(root, "--org", "fabrikam", "--from", "contoso/azsqlcd") == 0
    assert (root / "action.yml").read_text(encoding="utf-8") == "uses: fabrikam/azsqlcd@v0.1.0\n"


# ----------------------------------------------------------------------------- what a port leaves alone


def test_the_source_of_the_tool_is_never_rewritten_and_a_reference_there_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    source = f'HOME = "https://github.com/{OLD}"\n'
    root = write_tree(tmp_path, {**TREE, "src/azsqlcd/cli.py": source})
    assert run(root, "--org", "contoso") == 1
    assert (root / "src/azsqlcd/cli.py").read_text(encoding="utf-8") == source
    assert "    1  src/azsqlcd/cli.py  NOT CHANGED" in capsys.readouterr().out
    assert (root / "action.yml").read_text(encoding="utf-8") == PORTED["action.yml"]  # the rest is ported
    assert run(root, "--org", "contoso", "--check") == 1  # and the check stays red until the source is clean


def here() -> str:
    """OWNER/REPO of this repository as its files name it: the source, or the target of a port."""
    setup_repo = (REPO / "scripts" / "setup_repo.py").read_text(encoding="utf-8")
    (name,) = re.findall(r'^TOOL_REPO = "([^"\n]+)"$', setup_repo, flags=re.MULTILINE)
    return name


def test_no_source_file_of_the_tool_names_an_organisation():
    # The tool is the same code in every organisation, and its digest is part of every plan hash.
    for owner in {OLD_OWNER, here().split("/")[0]}:
        port = port_to_org.Port(old_owner=owner, old_repo=OLD_REPO, new_owner="contoso", new_repo=OLD_REPO)
        assert port_to_org.references_in_tool_source(REPO, port) == []


def test_the_script_and_its_test_keep_the_name_of_the_source(tmp_path: Path):
    # After a port, --check must still know which name to look for.
    kept = {path: f'SOURCE = "{OLD}"\n' for path in port_to_org.NOT_REWRITTEN}
    root = write_tree(tmp_path, {**TREE, **kept})
    assert run(root, "--org", "contoso") == 0
    assert {path: (root / path).read_text(encoding="utf-8") for path in kept} == kept
    assert run(root, "--org", "contoso", "--check") == 0
    assert all((REPO / path).is_file() for path in port_to_org.NOT_REWRITTEN)


def test_line_ends_and_every_other_byte_of_a_rewritten_file_are_kept(tmp_path: Path):
    # Text mode would write CRLF on Windows and LF here; the repository pins LF and hashes bytes.
    data = f"﻿uses: {OLD}@v0.1.0\r\nname: café\r\n\tlast line, no line end".encode()
    (tmp_path / "action.yml").write_bytes(data)
    assert run(tmp_path, "--org", "contoso") == 0
    assert (tmp_path / "action.yml").read_bytes() == data.replace(OLD.encode(), b"contoso/azsqlcd")


def test_caches_and_files_that_are_not_text_are_not_read_as_references(tmp_path: Path):
    compiled = f"uses: {OLD}@v0.1.0\n".encode()
    binary = b"\xff\xfe" + OLD.encode()
    root = write_tree(tmp_path, {"action.yml": "name: azsqlcd\n"})
    for relative, data in {
        "tests/unit/__pycache__/test_x.pyc": compiled,
        "tests/fixtures/b.tar": binary,
    }.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)
    assert run(root, "--org", "contoso", "--check") == 0
    assert run(root, "--org", "contoso") == 0
    assert (root / "tests/unit/__pycache__/test_x.pyc").read_bytes() == compiled
    assert (root / "tests/fixtures/b.tar").read_bytes() == binary


@pytest.mark.parametrize(
    "args",
    [
        ["--org", "my org"],
        ["--org", "https://github.com/contoso"],
        ["--org", "contoso/azsqlcd"],
        ["--org", "-contoso"],
        ["--org", "contoso", "--repo", "tools/azsqlcd"],
        ["--org", "contoso", "--repo", "azsqlcd.git"],
        ["--org", "contoso", "--from", "contoso"],
        [],
    ],
)
def test_a_name_that_github_cannot_hold_is_refused_before_a_file_is_changed(tmp_path: Path, args: list[str]):
    root = write_tree(tmp_path, TREE)
    before = read_tree(root)
    with pytest.raises(SystemExit) as stopped:
        run(root, *args)
    assert stopped.value.code == 2
    assert read_tree(root) == before


# ----------------------------------------------------------------------------- this repository


def uses_lines(text: str) -> list[str]:
    return re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, flags=re.MULTILINE)


def test_the_workflow_file_tests_of_the_tool_still_pass_after_a_port(tmp_path: Path):
    """test_workflow_files.py, test_cli.py and test_setup_repo.py each hold the repository name as a
    constant and compare it with the YAML files and with setup_repo.py. They pass after a port only
    if both sides changed in the same way. This test ports a copy and reads the text of both sides.
    The source of the port is the name that this repository has now, so the test holds in every
    organisation that the repository is ported to.
    """
    copy = tmp_path / "copy"
    for relative in [*port_to_org.files_in_scope(REPO), *port_to_org.NOT_REWRITTEN]:
        (copy / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, copy / relative)
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    old, new = here(), "contoso-data/sql-cicd"

    def text(relative: str, root: Path = copy) -> str:
        return (root / relative).read_text(encoding="utf-8")

    assert run(copy, "--org", "contoso-data", "--repo", "sql-cicd", "--from", old) == 0

    # the constants that the tests and the setup script compare with
    assert f'\nOWN = "{new}"\n' in text("tests/unit/test_workflow_files.py")
    assert f'.startswith("{new}@")' in text("tests/unit/test_cli.py")
    assert f'\nTOOL_REPO = "{new}"\n' in text("scripts/setup_repo.py")
    assert '\nREPO = "contoso-data/db-sales"\n' in text("tests/unit/test_setup_repo.py")
    assert '["contoso-data", "teams", "dba"]' in text("tests/unit/test_setup_repo.py")
    assert f":job_workflow_ref:{new}/.github/workflows/" in text("tests/unit/test_setup_repo.py")

    # the other side: every `uses:` of every YAML file that those tests read
    yaml_files = [
        path
        for path in port_to_org.files_in_scope(REPO)
        if path == "action.yml" or (path.endswith(".yml") and "/workflows/" in path)
    ]
    assert len(yaml_files) >= 14  # action.yml, 7 of the tool, 3 of the template, 3 of the example
    own = 0
    for path in yaml_files:
        before, after = uses_lines(text(path, REPO)), uses_lines(text(path))
        assert len(before) == len(after), path
        for old_uses, new_uses in zip(before, after, strict=True):
            if old_uses.startswith(old):
                own += 1
                assert new_uses == new + old_uses[len(old) :], path  # same path, same tag
                assert new_uses.endswith(f"@v{version}"), path
            else:
                assert new_uses == old_uses, path  # a pin of a third-party action is not touched
    assert own > 20

    # each reusable workflow that the template calls is a file of the ported tool repository
    for path in yaml_files:
        for called in re.findall(rf"uses: {re.escape(new)}/(\.github/workflows/[a-z]+\.yml)@", text(path)):
            assert (copy / called).is_file(), f"{path}: {called}"

    # CODEOWNERS of the template names a team of the new organisation
    assert set(re.findall(r"@([^/\s]+)/", text("templates/db-repo/.github/CODEOWNERS"))) == {"contoso-data"}

    assert run(copy, "--org", "contoso-data", "--repo", "sql-cicd", "--from", old, "--check") == 0
