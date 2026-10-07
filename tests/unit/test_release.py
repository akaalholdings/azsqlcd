"""A release is one commit of main, read from git objects; only its content decides its identity."""

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import azsqlcd
from azsqlcd import chain, release
from azsqlcd.errors import Exit, ToolError

# The temporary repositories do not depend on the git configuration of the machine.
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
TOML = '[project]\nname = "sales"\n'
VIEW = "CREATE OR ALTER VIEW [dbo].[v] AS SELECT 1 AS [x];\n"
VIEW_PATH = "schema/views/dbo.v.sql"
ACCENT_PATH = f"schema/views/dbo.caf{chr(0xE9)}.sql"
LONG_PATH = f"schema/procedures/dbo.{'p' * 120}.sql"
CRLF_PATH = "schema/views/dbo.crlf.sql"
CRLF_BYTES = b"CREATE OR ALTER VIEW [dbo].[crlf] AS\r\nSELECT 1 AS [x];\r\n"
M1, M2, M3, M4 = "0001__a.sql", "0002__b.sql", "0003__c.sql", "0004__d.sql"


def run_git(repo: Path, *args: str | bytes) -> str:
    done = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, capture_output=True, check=True)
    return done.stdout.decode().strip()


def new_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run_git(path, "-c", "init.defaultBranch=main", "init", "-q")
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.invalid"),
        ("init.defaultBranch", "main"),
        ("core.autocrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        run_git(path, "config", key, value)
    return path


def commit(repo: Path, message: str, files: dict[str, str | bytes]) -> str:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode() if isinstance(content, str) else content)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def set_origin_main(repo: Path, sha: str) -> None:
    run_git(repo, "update-ref", "refs/remotes/origin/main", sha)


def migration(file: str) -> str:
    return f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode tx\nSELECT 1;\n"


def with_migrations(*files: str) -> dict[str, str | bytes]:
    """The newest migration file and the sum file that lists all of them."""
    entries = tuple(chain.ChainEntry(f, chain.file_sha256(migration(f).encode()), "tx") for f in files)
    return {
        f"migrations/{files[-1]}": migration(files[-1]),
        "migrations/migrations.sum": chain.format_sum(chain.Chain(False, entries)),
    }


def refusal(code: str, call, *args, **kwargs) -> ToolError:
    with pytest.raises(ToolError) as e:
        call(*args, **kwargs)
    assert (e.value.exit_code, e.value.reason_code) == (Exit.REFUSED, code)
    return e.value


@pytest.fixture(scope="module")
def history(tmp_path_factory) -> SimpleNamespace:
    """main:  c1 - c2 - c3 - merge - c5 (origin/main) - local
                \\           /
    feature:     f1 ------ f2          f1 adds migration 0002, f2 adds 0003, c5 adds 0004
    """
    repo = new_repo(tmp_path_factory.mktemp("history") / "repo")
    first = {
        "azsqlcd.toml": TOML,
        "README.md": "not part of a release\n",
        "docs/schema/notes.sql": "-- 'schema' is not the first folder here\n",
        "schema.md": "a file, not the schema folder\n",
        VIEW_PATH: VIEW,
        CRLF_PATH: CRLF_BYTES,
        "schema/_tombstones.toml": "",
        "onboarding/prod/snapshot.json": "{}\n",
    }
    c1 = commit(repo, "c1", first | with_migrations(M1))
    run_git(repo, "branch", "feature")
    c2 = commit(repo, "c2", {VIEW_PATH: VIEW + "-- changed in c2\n"})
    c3 = commit(
        repo, "c3", {LONG_PATH: "CREATE OR ALTER PROCEDURE [dbo].[p] AS SELECT 1;\n", ACCENT_PATH: VIEW}
    )
    run_git(repo, "checkout", "-q", "feature")
    f1 = commit(repo, "f1", with_migrations(M1, M2))
    f2 = commit(repo, "f2", with_migrations(M1, M2, M3))
    run_git(repo, "checkout", "-q", "main")
    run_git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge = run_git(repo, "rev-parse", "HEAD")
    c5 = commit(repo, "c5", with_migrations(M1, M2, M3, M4))
    set_origin_main(repo, c5)
    local = commit(repo, "not pushed", {"schema/views/dbo.w.sql": VIEW.replace("[v]", "[w]")})
    return SimpleNamespace(repo=repo, c1=c1, c2=c2, c3=c3, f1=f1, f2=f2, merge=merge, c5=c5, local=local)


@pytest.fixture(scope="module")
def built_c5(history) -> release.Release:
    return release.build(history.repo, history.c5)


@pytest.fixture
def dist(built_c5, tmp_path) -> SimpleNamespace:
    """bundle.tar and manifest.json of commit c5 in a fresh directory."""
    folder = tmp_path / "dist"
    release.write(built_c5, folder)
    return SimpleNamespace(folder=folder, release=built_c5, digest=release.digest(built_c5.manifest))


# ------------------------------------------------------------------ build
def test_a_release_holds_the_raw_blobs_of_the_release_paths_and_nothing_else(history):
    built = release.build(history.repo, history.c5)
    assert set(built.files) == {
        "azsqlcd.toml",
        VIEW_PATH,
        CRLF_PATH,
        ACCENT_PATH,
        LONG_PATH,
        "schema/_tombstones.toml",
        "onboarding/prod/snapshot.json",
        "migrations/migrations.sum",
        *(f"migrations/{m}" for m in (M1, M2, M3, M4)),
    }
    assert built.files[VIEW_PATH] == (VIEW + "-- changed in c2\n").encode()
    assert built.files[CRLF_PATH] == CRLF_BYTES
    assert built.files["schema/_tombstones.toml"] == b""
    assert built.manifest.commit == history.c5
    assert built.manifest.files == tuple(
        sorted((path, hashlib.sha256(data).hexdigest()) for path, data in built.files.items())
    )


def test_the_commit_may_be_given_as_any_revision_and_the_manifest_holds_the_full_sha(history):
    assert release.build(history.repo, "origin/main").manifest.commit == history.c5
    assert release.build(history.repo, history.merge[:12]).manifest.commit == history.merge


def test_release_seq_is_the_number_of_first_parent_commits_up_to_the_commit(history):
    seqs = [
        release.build(history.repo, c).manifest.release_seq for c in (history.c1, history.c3, history.merge)
    ]
    assert seqs == [1, 3, 4]
    assert release.build(history.repo, history.c5).manifest.release_seq == 5  # 7 commits are reachable
    counted = run_git(history.repo, "rev-list", "--count", "--first-parent", history.c5)
    assert counted == "5"


def test_a_commit_that_is_not_on_the_first_parent_chain_of_main_cannot_be_released(history):
    # f1 and f2 are merged into main, but main never pointed at them
    for sha in (history.f1, history.f2, history.local):
        refusal("NOT_ON_MAIN", release.build, history.repo, sha)
    built = release.build(history.repo, history.local, main_ref="refs/heads/main")
    assert built.manifest.release_seq == 6


def clone_with_branch_commit(tmp_path: Path) -> SimpleNamespace:
    """A repository whose refs/remotes/origin/main is c1, and a commit on a branch that main never had."""
    repo = new_repo(tmp_path / "repo")
    c1 = commit(repo, "c1", {"azsqlcd.toml": TOML, VIEW_PATH: VIEW})
    set_origin_main(repo, c1)
    run_git(repo, "checkout", "-q", "-b", "unreviewed")
    branch = commit(repo, "unreviewed", {VIEW_PATH: VIEW + "-- not reviewed\n"})
    return SimpleNamespace(repo=repo, c1=c1, branch=branch)


def test_build_refuses_commit_off_main_even_when_a_tag_is_named_origin_main(tmp_path):
    """LB-01. git resolves the short name origin/main to refs/tags/origin/main first, and tag
    creation is not restricted. main is read from the exact ref refs/remotes/origin/main."""
    made = clone_with_branch_commit(tmp_path)
    refusal("NOT_ON_MAIN", release.build, made.repo, made.branch)
    run_git(made.repo, "tag", "origin/main", made.branch)
    run_git(made.repo, "update-ref", "refs/tags/refs/remotes/origin/main", made.branch)
    assert run_git(made.repo, "rev-parse", "refs/remotes/origin/main") == made.c1  # main did not move
    assert run_git(made.repo, "rev-parse", "origin/main^{commit}") == made.branch  # the short name did
    refusal("NOT_ON_MAIN", release.build, made.repo, made.branch)
    assert release.build(made.repo, made.c1).manifest.commit == made.c1


def test_a_tag_cannot_stand_in_for_a_main_ref_that_does_not_exist(tmp_path):
    made = clone_with_branch_commit(tmp_path)
    run_git(made.repo, "update-ref", "-d", "refs/remotes/origin/main")
    run_git(made.repo, "update-ref", "refs/tags/refs/remotes/origin/main", made.branch)
    # rev-parse would find the tag; the exact ref does not exist
    assert run_git(made.repo, "rev-parse", "refs/remotes/origin/main^{commit}") == made.branch
    refusal("GIT_FAILED", release.build, made.repo, made.branch)


@pytest.mark.parametrize(
    "main_ref",
    ["main", "origin/main", "heads/main", "remotes/origin/main", "refs/tags/v1", "HEAD", "--all", ""],
)
def test_the_main_ref_is_a_full_ref_name_and_never_a_tag(history, main_ref):
    error = refusal("MAIN_REF_INVALID", release.build, history.repo, history.c5, main_ref=main_ref)
    assert error.detail == {"ref": main_ref}


def test_chain_added_in_names_the_release_that_added_each_line_across_a_merge_commit(history):
    built = release.build(history.repo, history.c5)
    # 0002 and 0003 reached main with the merge, which is release 4; on the branch they were commits 2 and 3
    assert built.manifest.chain_added_in == {M1: 1, M2: 4, M3: 4, M4: 5}


def test_an_older_release_knows_only_its_own_chain_lines(history):
    assert release.build(history.repo, history.merge).manifest.chain_added_in == {M1: 1, M2: 4, M3: 4}
    assert release.build(history.repo, history.c3).manifest.chain_added_in == {M1: 1}


def test_the_working_tree_and_the_index_are_never_read(tmp_path):
    repo = new_repo(tmp_path / "repo")
    committed = {"azsqlcd.toml": TOML, VIEW_PATH: VIEW} | with_migrations(M1)
    set_origin_main(repo, commit(repo, "c1", committed))
    (repo / VIEW_PATH).write_bytes(b"CREATE OR ALTER VIEW [dbo].[v] AS SELECT 2 AS [edited];\n")
    (repo / "schema/views/dbo.untracked.sql").write_bytes(VIEW.encode())
    (repo / "migrations" / M1).unlink()
    (repo / "azsqlcd.toml").write_bytes(b'[project]\nname = "staged"\n')
    run_git(repo, "add", "azsqlcd.toml")
    built = release.build(repo, "HEAD")
    assert built.files == {path: str(text).encode() for path, text in committed.items()}


def test_a_repository_without_a_working_tree_gives_the_same_release(history, tmp_path):
    run_git(tmp_path, "clone", "-q", "--bare", str(history.repo), "bare.git")
    set_origin_main(tmp_path / "bare.git", history.c5)
    assert release.build(tmp_path / "bare.git", history.c5) == release.build(history.repo, history.c5)


def test_a_shallow_clone_cannot_be_released_because_it_cannot_count_the_releases(history, tmp_path):
    run_git(tmp_path, "clone", "-q", "--depth", "1", history.repo.as_uri(), "shallow")
    assert run_git(tmp_path / "shallow", "rev-list", "--count", "--first-parent", "HEAD") == "1"
    refusal("SHALLOW_REPOSITORY", release.build, tmp_path / "shallow", "HEAD")


@pytest.mark.parametrize("revision", ["no-such-branch", "0" * 40, "--all", "-h", ""])
def test_an_unknown_revision_is_refused_and_never_read_as_a_git_option(history, revision):
    refusal("GIT_FAILED", release.build, history.repo, revision)
    # as the ref of main it is refused before git sees it: not a full ref name
    refusal("MAIN_REF_INVALID", release.build, history.repo, history.c5, main_ref=revision)
    refusal("GIT_FAILED", release.build, history.repo, history.c5, main_ref="refs/remotes/origin/" + revision)


def test_a_revision_that_git_does_not_know_is_named_in_the_refusal(history):
    """RO-4, the first live run: a repository without origin/main gave only "Needed a single
    revision", and nothing said which revision."""
    e = refusal("GIT_FAILED", release.build, history.repo, "no-such-commit")
    assert "'no-such-commit' is not a commit of this repository" in e.message
    assert e.detail == {"revision": "no-such-commit"}
    e = refusal("GIT_FAILED", release.build, history.repo, history.c5, main_ref="refs/remotes/origin/gone")
    assert "the ref of main, 'refs/remotes/origin/gone', is not in this repository" in e.message
    assert e.detail == {"revision": "refs/remotes/origin/gone"}
    # read_tree is the reader of `verify` and `lint`: the same message
    e = refusal("GIT_FAILED", release.read_tree, history.repo, "origin/no-such-main")
    assert "'origin/no-such-main' is not a commit of this repository" in e.message


def test_a_replace_ref_does_not_change_what_a_release_holds(tmp_path):
    """TQ-07. `git replace` puts other content behind an object name, for every git command that
    is not told to ignore it. The manifest must hold the hash of the blob that the commit names."""
    repo = new_repo(tmp_path / "repo")
    sha = commit(repo, "c1", {"azsqlcd.toml": TOML, VIEW_PATH: VIEW} | with_migrations(M1))
    set_origin_main(repo, sha)
    other = VIEW.replace("SELECT 1", "SELECT 2").encode()
    (tmp_path / "other.sql").write_bytes(other)
    for path, forged in ((VIEW_PATH, other), (f"migrations/{M1}", other)):
        original = run_git(repo, "rev-parse", f"{sha}:{path}")
        replacement = run_git(repo, "hash-object", "-w", str(tmp_path / "other.sql"))
        run_git(repo, "replace", "-f", original, replacement)
        assert run_git(repo, "cat-file", "-p", original).encode() + b"\n" == forged  # git itself is fooled
    built = release.build(repo, sha)
    assert built.files[VIEW_PATH] == VIEW.encode()
    assert built.files[f"migrations/{M1}"] == migration(M1).encode()
    assert dict(built.manifest.files)[VIEW_PATH] == hashlib.sha256(VIEW.encode()).hexdigest()
    assert release.read_tree(repo, sha)[VIEW_PATH] == VIEW.encode()


def test_every_git_command_ignores_replace_refs_and_a_revision_is_never_an_option(history, monkeypatch):
    """TQ-07. With `--verify` and the suffix ^{commit}, git refuses every revision that looks like
    an option even without --end-of-options (see the test above this one: '--all', '-h'). The flag
    is a second lock, so this test reads the command lines, which is the only place it shows."""
    commands: list[list[str]] = []
    real_run = subprocess.run

    def spy(command, **kwargs):
        commands.append(list(command))
        return real_run(command, **kwargs)

    monkeypatch.setattr(release.subprocess, "run", spy)
    release.build(history.repo, history.c5)
    refusal("GIT_FAILED", release.build, history.repo, "--all")
    # N4-09: the program is a full path, so Windows never searches the current directory for git.exe
    program = shutil.which("git")
    assert program and Path(program).is_absolute()
    assert commands and all(c[:2] == [program, "--no-replace-objects"] for c in commands)
    verifies = [c for c in commands if c[2] == "rev-parse" and "--verify" in c]
    assert len(verifies) >= 3  # the commit, the commit of main, the tree, '--all'
    for c in verifies:
        assert c[-2] == "--end-of-options" and c[-1].endswith("^{commit}")
    assert [
        program,
        "--no-replace-objects",
        "rev-parse",
        "--verify",
        "--end-of-options",
        "--all^{commit}",
    ] in (commands)


def test_a_chain_line_that_no_first_parent_commit_added_is_refused(history, monkeypatch):
    """TQ-07. chain_added_in must name a release for every chain line. Real git always lists the
    commit that brought a line to the first-parent chain (a merge too: see the test of the merge
    commit), so the refusal is reached here with a git that gives no commit for the file."""
    real_git = release.git

    def no_history_for_the_sum(args, cwd, **kwargs):
        if args[0] == "rev-list" and args[-1] == chain.SUM_PATH:
            return b""
        return real_git(args, cwd, **kwargs)

    monkeypatch.setattr(release, "git", no_history_for_the_sum)
    e = refusal("GIT_FAILED", release.build, history.repo, history.c5)
    assert f"no first-parent commit adds {M1} to {chain.SUM_PATH}" in e.message


def test_an_object_that_is_not_a_blob_is_no_file_content(history):
    """TQ-07. A tree, a commit and a name that git does not know give None, never bytes."""
    blob = run_git(history.repo, "rev-parse", f"{history.c5}:{VIEW_PATH}")
    tree = run_git(history.repo, "rev-parse", f"{history.c5}:schema")
    found = release._objects(history.repo, [tree, blob, history.c5, f"{history.c5}:no/such/path", blob])
    assert found == [None, (VIEW + "-- changed in c2\n").encode(), None, None, found[1]]
    assert release._objects(history.repo, []) == []


def test_a_commit_with_a_broken_chain_file_cannot_be_released(tmp_path):
    repo = new_repo(tmp_path / "repo")
    files = {"azsqlcd.toml": TOML} | with_migrations(M1)
    files["migrations/migrations.sum"] = str(files["migrations/migrations.sum"]).replace(" tx", " sometimes")
    set_origin_main(repo, commit(repo, "c1", files))
    refusal("CHAIN_INVALID", release.build, repo, "HEAD")


@pytest.mark.parametrize(
    ("mode", "path"),
    [
        ("120000", b"schema/views/dbo.link.sql"),
        ("120000", b"schema"),  # LB-09: a symbolic link in the place of a release folder
        ("120000", b"migrations"),
        ("120000", b"onboarding"),
        ("120000", b"azsqlcd.toml"),
        pytest.param(
            "100644", b"schema/views/dbo\\v.sql", marks=pytest.mark.skipif(os.name == "nt", reason="posix")
        ),
        pytest.param(
            "100644", b"schema/views/\xff.sql", marks=pytest.mark.skipif(os.name == "nt", reason="posix")
        ),
    ],
)
def test_a_tree_entry_that_is_not_a_plain_file_with_a_safe_utf8_path_cannot_be_released(tmp_path, mode, path):
    repo = new_repo(tmp_path / "repo")
    commit(repo, "c1", {"azsqlcd.toml": TOML})
    blob = run_git(repo, "hash-object", "-w", "azsqlcd.toml")
    run_git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},".encode() + path)
    run_git(repo, "commit", "-q", "-m", "odd entry")
    set_origin_main(repo, run_git(repo, "rev-parse", "HEAD"))
    refusal("TREE_INVALID", release.build, repo, "HEAD")


def odd_entry(tmp_path: Path, path: bytes, mode: str = "100644") -> Path:
    """A repository whose HEAD (= origin/main) holds azsqlcd.toml and one entry that the file
    system of the test machine may not be able to hold: it goes to the index only."""
    repo = new_repo(tmp_path / "repo")
    commit(repo, "c1", {"azsqlcd.toml": TOML})
    blob = run_git(repo, "hash-object", "-w", "azsqlcd.toml")
    run_git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},".encode() + path)
    run_git(repo, "commit", "-q", "-m", "odd entry")
    set_origin_main(repo, run_git(repo, "rev-parse", "HEAD"))
    return repo


@pytest.mark.skipif(os.name == "nt", reason="Git for Windows does not put such a path in the index")
@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("schema/views/aux.v.sql", "device name"),
        ("schema/schemas/NUL.sql", "device name"),
        ("schema/procedures/Com1.usp_Log.sql", "device name"),
        ("schema/views/dbo.a:b.sql", "cannot hold"),
        ("schema/views/dbo.what?.sql", "cannot hold"),
        ("schema/views/dbo.a*b.sql", "cannot hold"),
        ('schema/views/dbo.q"q.sql', "cannot hold"),
        ("schema/views/dbo.a<b>.sql", "cannot hold"),
        ("schema/views/dbo.a|b.sql", "cannot hold"),
        ("schema/views/dbo.a\tb.sql", "cannot hold"),
        ("schema/views/dbo.v .sql", "ends with a dot or a space"),
        ("schema/views/dbo.v..sql", "ends with a dot or a space"),
        ("migrations/prn.txt", "device name"),
        ("onboarding/prod./export.md", "ends with a dot or a space"),
        (f"schema/procedures/{'s' * 128}.{'n' * 128}.sql", "255"),
    ],
)
def test_a_path_that_windows_cannot_check_out_cannot_be_released(tmp_path, path, why):
    """N4-04. Git on Linux and macOS holds these paths; Git for Windows refuses the checkout of the
    whole repository. The planner does not read such a file as an object file, so without this
    refusal the build is green and the object is never deployed."""
    repo = odd_entry(tmp_path, path.encode())
    for call in (release.build, release.read_tree):
        e = refusal("TREE_INVALID", call, repo, "HEAD")
        assert repr(path) in e.message and why in e.message


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("schema/views/aux.v.sql", "device name"),
        ("schema/views/dbo.a:b.sql", "cannot hold"),
        ("schema/views/dbo.v .sql", "ends with a dot or a space"),
    ],
)
def test_a_bundle_with_a_path_that_windows_cannot_check_out_is_refused(dist, path, why):
    # build never writes such a path: a bundle that holds one was made by something else
    raw = json.loads((dist.folder / "manifest.json").read_bytes())
    raw = canonical(raw | {"files": sorted([*raw["files"], [path, "0" * 64]])})
    (dist.folder / "manifest.json").write_bytes(raw)
    e = refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, hashlib.sha256(raw).hexdigest())
    assert repr(path) in e.message and why in e.message


@pytest.mark.parametrize(
    ("path", "root"),
    [
        ("Schema/views/dbo.v.sql", "schema"),
        ("SCHEMA/views/dbo.v.sql", "schema"),
        ("Migrations/0001__a.sql", "migrations"),
        ("Onboarding/prod/export.md", "onboarding"),
        ("Azsqlcd.toml", "azsqlcd.toml"),
        ("AZSQLCD.TOML", "azsqlcd.toml"),
        ("Schema", "schema"),  # a file in the place of the folder
        ("m\u0131grat\u0131ons/0001__a.sql", "migrations"),  # dotless i: NTFS upper case is I
        ("\u017fchema/views/dbo.v.sql", "schema"),  # long s: folds to s
    ],
)
def test_a_release_root_in_another_letter_case_cannot_be_released(tmp_path, path, root):
    """N4-03. On Windows and macOS the working-tree reader opens Schema/ as schema/, so lint and
    verify are green; git holds 'Schema/...', which is outside the release. Skipped in silence,
    the build is green too and the files are never deployed."""
    repo = odd_entry(tmp_path, path.encode())
    for call in (release.build, release.read_tree):
        e = refusal("TREE_INVALID", call, repo, "HEAD")
        assert repr(path) in e.message and repr(root) in e.message and "lower case" in e.message
        assert e.detail["path"] == path


def test_a_folder_that_only_holds_the_name_of_a_release_root_stays_outside_a_release(tmp_path):
    repo = new_repo(tmp_path / "repo")
    files = {
        "azsqlcd.toml": TOML,
        "docs/Schema/notes.sql": "-- not the first folder\n",
        "Schemas/x.sql": "-- another name\n",
        "Schema.md": "a file with another name\n",
        "tools/Migrations/readme.md": "x\n",
    }
    set_origin_main(repo, commit(repo, "c1", files))
    assert set(release.build(repo, "HEAD").files) == {"azsqlcd.toml"}
    assert release.root_case_problem("schema") is None and release.root_case_problem("azsqlcd.toml") is None
    assert release.root_case_problem("docs") is None


def test_a_root_file_named_like_a_release_folder_is_not_part_of_a_release(tmp_path):
    repo = new_repo(tmp_path / "repo")
    set_origin_main(repo, commit(repo, "c1", {"azsqlcd.toml": TOML, "onboarding": "a file, not a folder\n"}))
    assert set(release.build(repo, "HEAD").files) == {"azsqlcd.toml"}


def test_read_tree_gives_the_release_paths_of_any_revision(history):
    files = release.read_tree(history.repo, history.c1)
    assert files[VIEW_PATH] == VIEW.encode()
    assert sorted(path for path in files if path.startswith("migrations/")) == [
        f"migrations/{M1}",
        "migrations/migrations.sum",
    ]
    assert not {"README.md", "schema.md", "docs/schema/notes.sql"} & set(files)


def test_git_returns_stdout_bytes_and_refuses_when_git_fails(history):
    assert release.git(["rev-parse", "HEAD"], history.repo) == history.local.encode() + b"\n"
    e = refusal("GIT_FAILED", release.git, ["rev-parse", "--verify", "no-such-ref"], history.repo)
    assert "rev-parse" in e.message
    refusal("GIT_FAILED", release.git, ["rev-parse", "HEAD"], history.repo.parent / "no-such-folder")


def test_git_is_refused_when_it_is_not_on_path(history, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    e = refusal("GIT_FAILED", release.git, ["rev-parse", "HEAD"], history.repo)
    assert "git could not be started" in e.message and "PATH" in e.message


@pytest.mark.parametrize("where", ["repository", "current directory"])
def test_a_git_program_inside_the_repository_or_the_current_directory_is_never_run(
    tmp_path, monkeypatch, where
):
    """N4-09. Windows looks for git.exe in the current directory before PATH; the current directory
    is the checkout of a pull request. A program found there is refused, not started."""
    repo = new_repo(tmp_path / "repo")
    commit(repo, "c1", {"azsqlcd.toml": TOML})
    folder = repo if where == "repository" else tmp_path / "elsewhere"
    folder.mkdir(exist_ok=True)
    ran = tmp_path / "ran"
    fake = folder / ("git.bat" if os.name == "nt" else "git")
    fake.write_bytes(
        f"@echo x> {ran}\r\n".encode() if os.name == "nt" else f"#!/bin/sh\necho x > '{ran}'\n".encode()
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(folder) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(folder if where == "current directory" else tmp_path)
    e = refusal("GIT_FAILED", release.git, ["rev-parse", "HEAD"], repo)
    assert "git could not be started" in e.message and str(folder.resolve()) in e.message
    assert not ran.exists()


# ------------------------------------------------------------------ manifest and digest
def test_the_manifest_json_is_canonical_and_holds_identity_only():
    path = f"schema/views/dbo.{chr(0xE9)}.sql"
    manifest = release.Manifest(
        commit="c" * 40,
        release_seq=7,
        files=((path, "2" * 64), ("azsqlcd.toml", "1" * 64)),
        chain_added_in={M2: 7, M1: 3},
    )
    expected = (
        '{"chain_added_in":{"0001__a.sql":3,"0002__b.sql":7},"commit":"' + "c" * 40 + '",'
        '"files":[["azsqlcd.toml","' + "1" * 64 + '"],["' + path + '","' + "2" * 64 + '"]],"release_seq":7}\n'
    )
    assert release.manifest_json(manifest) == expected.encode("utf-8")
    assert release.digest(manifest) == hashlib.sha256(expected.encode("utf-8")).hexdigest()


def test_the_digest_is_the_sha256_of_the_manifest_file(dist):
    raw = (dist.folder / "manifest.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == dist.digest
    assert json.loads(raw)["commit"] == dist.release.manifest.commit


def test_the_digest_does_not_change_when_the_tool_version_changes(history, monkeypatch):
    before = release.build(history.repo, history.c5)
    version = azsqlcd.__version__
    monkeypatch.setattr(azsqlcd, "__version__", "99.0.0")
    after = release.build(history.repo, history.c5)
    assert release.digest(after.manifest) == release.digest(before.manifest)
    raw = release.manifest_json(after.manifest)
    assert set(json.loads(raw)) == {"commit", "release_seq", "files", "chain_added_in"}
    assert b"99.0.0" not in raw and version.encode() not in raw


def test_the_digest_changes_with_every_part_of_the_identity(history):
    manifest = release.build(history.repo, history.c5).manifest
    other_file = (manifest.files[0][0], "0" * 64)
    changed = [
        release.Manifest("0" * 40, manifest.release_seq, manifest.files, manifest.chain_added_in),
        release.Manifest(manifest.commit, manifest.release_seq + 1, manifest.files, manifest.chain_added_in),
        release.Manifest(manifest.commit, manifest.release_seq, manifest.files[1:], manifest.chain_added_in),
        release.Manifest(
            manifest.commit, manifest.release_seq, (other_file, *manifest.files[1:]), manifest.chain_added_in
        ),
        release.Manifest(
            manifest.commit, manifest.release_seq, manifest.files, manifest.chain_added_in | {M4: 4}
        ),
    ]
    assert len({release.digest(m) for m in [manifest, *changed]}) == 6


# ------------------------------------------------------------------ write
def test_two_builds_of_one_commit_give_the_same_digest_and_the_same_tar_bytes(history, tmp_path):
    run_git(tmp_path, "clone", "-q", str(history.repo), "elsewhere")
    set_origin_main(tmp_path / "elsewhere", history.c5)
    first = release.build(history.repo, history.c5)
    second = release.build(tmp_path / "elsewhere", history.c5)
    release.write(first, tmp_path / "one")
    release.write(second, tmp_path / "two" / "nested")
    assert release.digest(first.manifest) == release.digest(second.manifest)
    for name in ("bundle.tar", "manifest.json"):
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / "nested" / name).read_bytes()


def test_the_tar_holds_nothing_of_the_machine_or_the_time_of_the_build(dist):
    with tarfile.open(dist.folder / "bundle.tar", mode="r:") as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == sorted(dist.release.files)
    for m in members:
        assert (m.type, m.mode, m.mtime, m.uid, m.gid, m.uname, m.gname) == (
            tarfile.REGTYPE,
            0o644,
            0,
            0,
            0,
            "",
            "",
        )
    assert sorted(p.name for p in dist.folder.iterdir()) == ["bundle.tar", "manifest.json"]


# ------------------------------------------------------------------ read_bundle
def test_a_bundle_reads_back_as_the_release_that_was_written(dist):
    bundle = release.read_bundle(dist.folder, dist.digest)
    assert bundle.manifest == dist.release.manifest
    assert bundle.files == dist.release.files
    assert release.digest(bundle.manifest) == dist.digest


def canonical(doc: dict) -> bytes:
    return (json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda doc: doc | {"commit": "not-a-sha; DROP"}, "commit"),
        (lambda doc: doc | {"commit": "A" * 40}, "commit"),
        (lambda doc: doc | {"commit": "a" * 39}, "commit"),
        (lambda doc: doc | {"release_seq": 0}, "release_seq"),
        (lambda doc: doc | {"release_seq": -5}, "release_seq"),
        (lambda doc: doc | {"files": sorted([*doc["files"], doc["files"][-1]])}, "more than once"),
        (
            lambda doc: doc | {"files": sorted([*doc["files"], [doc["files"][0][0], "0" * 64]])},
            "more than once",
        ),
        (lambda doc: doc | {"files": sorted([*doc["files"], ["README.md", "0" * 64]])}, "outside"),
        (lambda doc: doc | {"files": sorted([*doc["files"], ["schema/../x.sql", "0" * 64]])}, "safe"),
        (lambda doc: doc | {"files": [[doc["files"][0][0], "xyz"], *doc["files"][1:]]}, "sha256"),
        (lambda doc: doc | {"chain_added_in": {"../../x": 1}}, "no migration"),
        (lambda doc: doc | {"chain_added_in": doc["chain_added_in"] | {M1: 0}}, "release_seq"),
        (lambda doc: doc | {"chain_added_in": doc["chain_added_in"] | {M1: 99}}, "release_seq"),
    ],
)
def test_read_bundle_refuses_manifest_with_duplicate_path_bad_commit_or_bad_seq(dist, change, why):
    """LB-12. The digest pins the manifest bytes, not their meaning: a manifest that build never
    writes is refused even when its digest is the expected one."""
    raw = canonical(change(json.loads((dist.folder / "manifest.json").read_bytes())))
    (dist.folder / "manifest.json").write_bytes(raw)
    error = refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, hashlib.sha256(raw).hexdigest())
    assert why in error.message


def test_the_bundle_is_never_extracted_to_disk(dist, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the bundle must be read in memory")

    monkeypatch.setattr(tarfile.TarFile, "extract", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    monkeypatch.chdir(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert release.read_bundle(dist.folder, dist.digest).files == dist.release.files
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def regular(name: str, data: bytes = b"SELECT 1;\n") -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), data


def link(name: str, kind: bytes, target: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info, b""


def rewrite_tar(folder: Path, edit) -> list[str]:
    """Apply edit to the member list of bundle.tar; manifest.json stays as it is."""
    with tarfile.open(folder / "bundle.tar", mode="r:") as tar:
        members = [(m, tar.extractfile(m).read()) for m in tar]
    edit(members)
    with tarfile.open(folder / "bundle.tar", mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with tarfile.open(folder / "bundle.tar", mode="r:") as tar:
        return tar.getnames()


def swap(members: list, name: str, new: tuple[tarfile.TarInfo, bytes]) -> None:
    index = next(i for i, (info, _) in enumerate(members) if info.name == name)
    members[index] = new


@pytest.mark.parametrize(
    ("edit", "name", "why"),
    [
        (
            lambda ms: ms.append(regular("schema/views/dbo.extra.sql")),
            "schema/views/dbo.extra.sql",
            "not in the manifest",
        ),
        (lambda ms: ms.append(regular("README.md")), "README.md", "outside"),
        (lambda ms: ms.append(ms[0]), "azsqlcd.toml", "more than once"),
        (
            lambda ms: swap(ms, VIEW_PATH, link(VIEW_PATH, tarfile.SYMTYPE, "azsqlcd.toml")),
            VIEW_PATH,
            "not a regular file",
        ),
        (
            lambda ms: swap(ms, VIEW_PATH, link(VIEW_PATH, tarfile.LNKTYPE, ACCENT_PATH)),
            VIEW_PATH,
            "not a regular file",
        ),
        (
            lambda ms: ms.append(link("schema/views", tarfile.DIRTYPE, "")),
            "schema/views",
            "not a regular file",
        ),
        (
            lambda ms: ms.append(regular("schema/../../evil.sql")),
            "schema/../../evil.sql",
            "not a safe relative path",
        ),
        (lambda ms: ms.append(regular("../evil.sql")), "../evil.sql", "not a safe relative path"),
        (lambda ms: ms.append(regular("/tmp/evil.sql")), "/tmp/evil.sql", "not a safe relative path"),
        (
            lambda ms: ms.append(regular("schema\\..\\evil.sql")),
            "schema\\..\\evil.sql",
            "not a safe relative path",
        ),
    ],
)
def test_a_bundle_with_an_extra_duplicate_link_or_traversing_member_is_refused(dist, edit, name, why):
    assert name in rewrite_tar(dist.folder, edit)
    e = refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, dist.digest)
    assert repr(name) in e.message and why in e.message


def test_a_hard_link_to_a_file_with_the_right_content_is_still_refused(dist):
    # tarfile would follow the link and return the bytes of the target, which have the listed sha256
    assert dist.release.files[ACCENT_PATH] == VIEW.encode()
    rewrite_tar(
        dist.folder, lambda ms: ms.append(link("schema/views/dbo.extra.sql", tarfile.LNKTYPE, ACCENT_PATH))
    )
    refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, dist.digest)


def test_a_bundle_with_a_missing_file_is_refused(dist):
    rewrite_tar(dist.folder, lambda ms: ms.pop(3))
    e = refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, dist.digest)
    assert "not in the bundle" in e.message


def test_one_changed_file_byte_is_refused(dist):
    def flip(members: list) -> None:
        info, data = next(m for m in members if m[0].name == f"migrations/{M2}")
        swap(members, info.name, (info, data[:5] + bytes([data[5] ^ 1]) + data[6:]))

    rewrite_tar(dist.folder, flip)
    e = refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, dist.digest)
    assert f"migrations/{M2}" in e.message and "sha256" in e.message


def test_one_changed_manifest_byte_is_refused(dist):
    raw = (dist.folder / "manifest.json").read_bytes()
    (dist.folder / "manifest.json").write_bytes(raw.replace(b'"release_seq":5', b'"release_seq":6'))
    e = refusal("DIGEST_MISMATCH", release.read_bundle, dist.folder, dist.digest)
    assert e.detail["expected"] == dist.digest and e.detail["actual"] != dist.digest


@pytest.mark.parametrize("wrong", ["0" * 64, "", "DIGEST"])
def test_the_manifest_must_have_the_expected_digest(dist, wrong):
    refusal("DIGEST_MISMATCH", release.read_bundle, dist.folder, wrong)
    refusal("DIGEST_MISMATCH", release.read_bundle, dist.folder, dist.digest.upper())


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda doc: json.dumps(doc, indent=2),
        lambda doc: json.dumps(doc | {"tool_version": "0.1.0"}, sort_keys=True, separators=(",", ":")) + "\n",
        lambda doc: json.dumps(doc | {"release_seq": "5"}, sort_keys=True, separators=(",", ":")) + "\n",
        lambda doc: (
            json.dumps(doc | {"files": doc["files"][::-1]}, sort_keys=True, separators=(",", ":")) + "\n"
        ),
        lambda doc: (
            json.dumps(doc | {"files": [["../x", "0" * 64]]}, sort_keys=True, separators=(",", ":")) + "\n"
        ),
        lambda doc: "[]\n",
        lambda doc: "not json",
    ],
)
def test_a_manifest_that_is_not_the_canonical_identity_document_is_refused_even_with_its_own_digest(
    dist, rewrite
):
    raw = rewrite(json.loads((dist.folder / "manifest.json").read_bytes())).encode()
    (dist.folder / "manifest.json").write_bytes(raw)
    refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, hashlib.sha256(raw).hexdigest())


def test_a_bundle_that_is_not_a_plain_tar_is_refused(dist, tmp_path):
    plain = (dist.folder / "bundle.tar").read_bytes()
    with tarfile.open(dist.folder / "bundle.tar", mode="w:gz") as tar:  # the right content, compressed
        for path, data in sorted(dist.release.files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, dist.digest)
    (dist.folder / "bundle.tar").write_bytes(plain[: len(plain) // 2 - 100])
    refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, dist.digest)
    (dist.folder / "bundle.tar").unlink()
    refusal("BUNDLE_INVALID", release.read_bundle, dist.folder, dist.digest)
    refusal("BUNDLE_INVALID", release.read_bundle, tmp_path / "no-such-folder", dist.digest)


# ------------------------------------------------------------------ tool digest
def test_the_tool_digest_is_the_hash_of_the_python_sources_by_sorted_relative_path(tmp_path, monkeypatch):
    package = tmp_path / "pkg"
    (package / "sub").mkdir(parents=True)
    (package / "b.py").write_bytes(b"B = 1\n")
    (package / "a.py").write_bytes(b"A = 1\n")
    (package / "sub" / "c.py").write_bytes(b"C = 1\n")
    (package / "notes.txt").write_bytes(b"not source\n")
    monkeypatch.setattr(release, "__file__", str(package / "release.py"))

    first = release.tool_digest()
    assert first == hashlib.sha256(b"a.py\0A = 1\n\0b.py\0B = 1\n\0sub/c.py\0C = 1\n\0").hexdigest()
    (package / "notes.txt").write_bytes(b"changed\n")
    assert release.tool_digest() == first
    (package / "sub" / "c.py").write_bytes(b"C = 2\n")
    changed = release.tool_digest()
    assert changed != first
    (package / "a.py").rename(package / "z.py")
    assert release.tool_digest() not in (first, changed)


def test_the_tool_digest_is_the_same_for_a_checkout_with_crlf_line_ends(tmp_path, monkeypatch):
    """N4-08. A git checkout on Windows without the eol attribute has CRLF in every .py file. The
    digest goes into plan_sha256 and azsqlcd.run: it must be the digest that CI computes."""
    digests = []
    for name, line_end in (("lf", b"\n"), ("crlf", b"\r\n")):
        package = tmp_path / name
        package.mkdir()
        (package / "a.py").write_bytes(b"A = 1" + line_end + b"B = 2" + line_end)
        (package / "b.py").write_bytes(b"")
        monkeypatch.setattr(release, "__file__", str(package / "release.py"))
        digests.append(release.tool_digest())
    assert digests[0] == digests[1] == hashlib.sha256(b"a.py\0A = 1\nB = 2\n\0b.py\0\0").hexdigest()


def test_the_tool_digest_reads_the_installed_package():
    digest = release.tool_digest()
    assert re.fullmatch("[0-9a-f]{64}", digest)
    assert digest != hashlib.sha256(b"").hexdigest()
