"""The example database repository stays a working repository of the tool.

examples/demo-db is the first thing that a new user copies, and docs/quickstart.md walks through
one change of it: a new column, a new index and a changed procedure. The tests here make that
walk again. Every step is a call of cli.main in a temporary git repository; no database and no
network. A test that fails says one of three things: the example no longer fits the tool, the tool
prints another text than the quickstart shows, or the tool is wrong.
"""

import contextlib
import io
import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from azsqlcd import chain, cli, emit, lex, modules, names, release
from azsqlcd.config import load_config
from azsqlcd.model import AliasType, Check, ForeignKey, PrimaryKey, Sequence, Synonym, Table, TableType
from azsqlcd.model import Schema as SchemaObject
from azsqlcd.parse import parse_object_file

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "demo-db"
TEMPLATE = REPO / "templates" / "db-repo"
QUICKSTART = REPO / "docs" / "quickstart.md"

FIRST = "0001__initial_schema.sql"
SECOND = "0002__customer_loyalty_tier.sql"
SUM_HEADER = "azsqlcd-sum 1\n"
CUSTOMER = "schema/tables/sales.Customer.sql"
PROCEDURE = "schema/procedures/sales.usp_CreateCustomer.sql"

# ---- the change of the quickstart: three edits of object files
LAST_COLUMN = (
    "    [CreatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Customer_CreatedUtc] DEFAULT (SYSUTCDATETIME()),\n"
)
NEW_COLUMN = "    [LoyaltyTier] tinyint NOT NULL CONSTRAINT [DF_Customer_LoyaltyTier] DEFAULT (0),\n"
NEW_INDEX = (
    "GO\n"
    "CREATE NONCLUSTERED INDEX [IX_Customer_LoyaltyTier] ON [sales].[Customer] ([LoyaltyTier])\n"
    "    INCLUDE ([DisplayName]);\n"
)
PROCEDURE_EDITS = (
    ("    @CountryCode char(2),\n", "    @CountryCode char(2),\n    @LoyaltyTier tinyint = 0,\n"),
    (
        "    INSERT INTO [sales].[Customer] ([Email], [DisplayName], [CountryCode])\n",
        "    INSERT INTO [sales].[Customer] ([Email], [DisplayName], [CountryCode], [LoyaltyTier])\n",
    ),
    (
        "    VALUES (@Email, @DisplayName, UPPER(@CountryCode));\n",
        "    VALUES (@Email, @DisplayName, UPPER(@CountryCode), @LoyaltyTier);\n",
    ),
)

# ---- what the tool writes and prints for it. docs/quickstart.md shows the same texts.
ADD_COLUMN = (
    "ALTER TABLE [sales].[Customer] ADD [LoyaltyTier] tinyint NOT NULL "
    "CONSTRAINT [DF_Customer_LoyaltyTier] DEFAULT (0);\n"
)
CREATE_INDEX = (
    "CREATE NONCLUSTERED INDEX [IX_Customer_LoyaltyTier] ON [sales].[Customer] ([LoyaltyTier]) "
    "INCLUDE ([DisplayName]);\n"
)
ALLOW_TODO = "-- azsqlcd:allow LONG_LOCK [sales].[Customer] reason: TODO\n"
REASON = "the table has under 100000 rows; the index build takes seconds"
ALLOW_REASON = f"-- azsqlcd:allow LONG_LOCK [sales].[Customer] reason: {REASON}\n"
HEADER = "-- azsqlcd:migration 0002__customer_loyalty_tier\n-- azsqlcd:mode tx\n"
GENERATED = HEADER + ADD_COLUMN + "GO\n" + ALLOW_TODO + CREATE_INDEX + "GO\n"
REVIEWED = HEADER + ADD_COLUMN + "GO\n" + ALLOW_REASON + CREATE_INDEX + "GO\n"

GEN_PRINTS = (
    f"wrote migrations/{SECOND}\n"
    "wrote migrations/migrations.sum\n"
    "1 allow line(s) need a reason: replace TODO, then run azsqlcd gen --resum\n"
)
TODO_FINDING = f"migrations/{SECOND}:5: error ALLOW_REASON: TODO is not a reason; write the reason\n"
STALE_SUM_FINDING = (
    f"migrations/migrations.sum:3: error CHN004: the sha256 of this line is not the sha256 of "
    f"migrations/{SECOND}. If the migration is not merged yet, run azsqlcd gen --resum; "
    "a merged migration never changes\n"
)
NOT_PROVEN_FINDING = (
    f"{CUSTOMER}:1: error PRF001: TABLE:[sales].[Customer]: columns[loyaltytier].type.name: "
    "the migrations give another value than the object file\n"
)
CLEAN = "0 error(s), 0 warning(s)\n"
TARGET_PRINTS = "demo-dev: demo_sales on replace-me-dev.database.windows.net\njob timeout: 120 min\n"


# ------------------------------------------------------------------ helpers
@dataclass(frozen=True)
class Done:
    """One call of the command line: the exit code and what it printed."""

    code: int
    out: str
    err: str


def run(*args: str) -> Done:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return Done(code, out.getvalue(), err.getvalue())


def git(repo: Path, *args: str) -> str:
    settings = (
        "user.name=Test",
        "user.email=test@example.com",
        "commit.gpgsign=false",
        "core.autocrlf=false",
    )
    options = [part for setting in settings for part in ("-c", setting)]
    done = subprocess.run(["git", *options, *args], cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def edit(repo: Path, path: str, old: str, new: str) -> None:
    """Replace the one place where the file holds old. The file must hold it once."""
    text = (repo / path).read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{path} does not hold this text once: {old!r}"
    (repo / path).write_bytes(text.replace(old, new).encode("utf-8"))


def outputs(path: Path) -> dict[str, str]:
    """The keys that a command wrote to the file of GITHUB_OUTPUT; the file is emptied."""
    found = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())
    path.write_text("", encoding="utf-8")
    return found


def object_files() -> dict[str, str]:
    """path relative to the example -> text, for every object file of the example."""
    return {
        path.relative_to(EXAMPLE).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(EXAMPLE.glob("schema/*/*.sql"))
    }


def table_class_objects() -> list:
    return [
        parse_object_file(text, path)[0]
        for path, text in object_files().items()
        if names.key_for_path(path)[0] in names.TABLE_CLASS_KINDS
    ]


def function_kind(text: str) -> str:
    words = [tok.text.upper() for tok in lex.significant(lex.tokenize(text))]
    returned = words[words.index("RETURNS") + 1]
    return "inline" if returned == "TABLE" else "multi-statement" if returned.startswith("@") else "scalar"


def empty_repository(folder: Path) -> tuple[Path, str]:
    """A git repository with the files of the example that hold no object: (path, commit)."""
    folder.mkdir()
    for path in ("azsqlcd.toml", ".gitattributes", "schema/_tombstones.toml"):
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE / path, folder / path)
    (folder / "migrations").mkdir()
    (folder / chain.SUM_PATH).write_text(SUM_HEADER, encoding="utf-8")
    git(folder, "init", "-q", "-b", "main")
    return folder, commit(folder, "empty database repository")


# ------------------------------------------------------------------ the walk of the quickstart
@dataclass
class Walk:
    """What each step of docs/quickstart.md gave. The fixture asserts nothing: each test reads the
    steps that it is about, so one wrong step does not hide the others."""

    repo: Path
    base: str = ""  # the commit of main that the change starts from
    lint_of_the_copy: Done | None = None
    verify_with_no_migration: Done | None = None
    gen: Done | None = None
    generated: str = ""  # the migration file as gen wrote it
    sum_after_gen: str = ""
    lint_with_todo: Done | None = None
    lint_before_resum: Done | None = None
    resum: Done | None = None
    reviewed: str = ""  # the migration file with the reason
    sum_after_resum: str = ""
    lint: Done | None = None
    verify: Done | None = None
    verify_of_another_migration: Done | None = None
    sum_restored: str = ""
    verify_restored: Done | None = None
    merged: str = ""  # the commit of main that holds the change
    build: Done | None = None
    built: dict[str, str] | None = None
    dist: Path | None = None
    targets: Done | None = None
    matrix: dict[str, str] | None = None


@pytest.fixture(scope="module")
def walk(tmp_path_factory: pytest.TempPathFactory) -> Walk:
    top = tmp_path_factory.mktemp("quickstart")
    repo, dist, ci = top / "db-demo", top / "dist", top / "github_output"
    ci.write_text("", encoding="utf-8")
    root = ("--root", str(repo))
    step = Walk(repo)

    # ---- a repository of its own from the example; origin/main stands in for the pushed main
    shutil.copytree(EXAMPLE, repo)
    git(repo, "init", "-q", "-b", "main")
    step.base = commit(repo, "Demo sales database")
    git(repo, "update-ref", "refs/remotes/origin/main", step.base)
    step.lint_of_the_copy = run("lint", *root)

    # ---- the change: a column, an index, a procedure
    git(repo, "switch", "-q", "-c", "feat/loyalty-tier")
    edit(repo, CUSTOMER, LAST_COLUMN, LAST_COLUMN + NEW_COLUMN)
    (repo / CUSTOMER).write_bytes((repo / CUSTOMER).read_bytes() + NEW_INDEX.encode("utf-8"))
    for old, new in PROCEDURE_EDITS:
        edit(repo, PROCEDURE, old, new)
    step.verify_with_no_migration = run("verify", "--base", step.base, *root)

    # ---- gen, the reason of the allow line, gen --resum
    step.gen = run("gen", "--name", "customer_loyalty_tier", *root)
    migration = repo / "migrations" / SECOND
    step.generated = migration.read_text(encoding="utf-8") if migration.is_file() else ""
    step.sum_after_gen = (repo / chain.SUM_PATH).read_text(encoding="utf-8")
    step.lint_with_todo = run("lint", *root)
    if migration.is_file():
        migration.write_bytes(step.generated.replace("reason: TODO", f"reason: {REASON}").encode("utf-8"))
    step.lint_before_resum = run("lint", *root)
    step.resum = run("gen", "--resum", *root)
    step.reviewed = migration.read_text(encoding="utf-8") if migration.is_file() else ""
    step.sum_after_resum = (repo / chain.SUM_PATH).read_text(encoding="utf-8")
    step.lint = run("lint", *root)
    step.verify = run("verify", "--base", step.base, *root)

    # ---- a migration that makes another column than the table file holds, then the right one again
    if migration.is_file():
        migration.write_bytes(step.reviewed.replace("tinyint", "smallint").encode("utf-8"))
        run("gen", "--resum", *root)
        step.verify_of_another_migration = run("verify", "--base", step.base, *root)
        migration.write_bytes(step.reviewed.encode("utf-8"))
        run("gen", "--resum", *root)
    step.sum_restored = (repo / chain.SUM_PATH).read_text(encoding="utf-8")
    step.verify_restored = run("verify", "--base", step.base, *root)

    # ---- the merge to main and what the release job runs
    commit(repo, "Add the loyalty tier of a customer")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "--ff-only", "feat/loyalty-tier")
    step.merged = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", step.merged)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GITHUB_OUTPUT", str(ci))
        patch.setenv("GITHUB_STEP_SUMMARY", str(top / "github_summary"))
        step.build = run("build", "--commit", "HEAD", "--out", str(dist), *root, "--ci", "github")
        step.built = outputs(ci)
        step.dist = dist
        where = ("--bundle", str(dist), "--digest", step.built.get("digest", ""))
        step.targets = run("targets", *where, "--env", "dev", "--ci", "github")
        step.matrix = outputs(ci)
    return step


# ------------------------------------------------------------------ the example as it is shipped
def test_the_example_holds_the_objects_that_its_readme_counts():
    kinds = Counter(names.key_for_path(path)[0] for path in object_files())
    assert kinds == {
        "SCHEMA": 3,
        "TABLE": 12,
        "SEQUENCE": 2,
        "TYPE": 2,
        "SYNONYM": 1,
        "VIEW": 6,
        "FUNCTION": 4,
        "PROCEDURE": 8,
        "TRIGGER": 2,
    }
    objects = table_class_objects()
    assert Counter(type(obj) for obj in objects) == {
        SchemaObject: 3,
        Table: 12,
        Sequence: 2,
        AliasType: 1,
        TableType: 1,
        Synonym: 1,
    }
    tables = [obj for obj in objects if isinstance(obj, Table)]
    # a table file of the example shows each feature of the model at least once
    columns = [column for table in tables for column in table.columns]
    assert all(any(isinstance(c, PrimaryKey) for c in table.constraints) for table in tables)
    assert sum(isinstance(c, ForeignKey) for table in tables for c in table.constraints) >= 12
    assert sum(isinstance(c, Check) for table in tables for c in table.constraints) >= 12
    assert sum(column.default is not None for column in columns) >= 12
    assert {column.computed.persisted for column in columns if column.computed is not None} == {True, False}
    assert any(column.identity is not None for column in columns)
    assert any(column.type is not None and column.type.schema == "product" for column in columns)
    indexes = [index for table in tables for index in table.indexes]
    assert any(index.unique for index in indexes) and any(index.clustered for index in indexes)
    assert any(index.filter is not None for index in indexes) and any(index.included for index in indexes)
    assert any(index.options for index in indexes)

    module_files = [
        modules.read_module(path, text.encode("utf-8"))
        for path, text in object_files().items()
        if names.key_for_path(path)[0] in names.MODULE_KINDS
    ]
    assert [m.name for m in module_files if m.kind == "VIEW" and m.schema_bound] == ["vw_OrderTotals"]
    functions = Counter(function_kind(m.text) for m in module_files if m.kind == "FUNCTION")
    assert functions == {"scalar": 2, "inline": 1, "multi-statement": 1}


def test_the_config_of_the_example_has_five_environments_and_names_no_real_server():
    config = load_config((EXAMPLE / "azsqlcd.toml").read_text(encoding="utf-8"))
    assert list(config.env) == ["dev", "sandbox", "test", "preprod", "prod"]  # the promotion order
    assert config.project.table_model is True
    assert [env.gated for env in config.env.values()] == [False, False, True, True, True]
    for env in config.env.values():
        (target,) = env.targets
        assert target.id == f"demo-{env.name}"
        assert target.server == f"replace-me-{env.name}.database.windows.net"
    # an example value cannot be taken for a real identity: every GUID is zeros and a counter
    guids = [config.project.tenant_id, *config.identities.values()]
    assert all(re.fullmatch(r"00000000-0000-0000-0000-00000000000[0-9]", guid) for guid in guids)
    assert len(set(guids)) == len(guids)


def test_every_table_class_file_of_the_example_is_the_canonical_text():
    """A reader copies these files as the pattern for a new table: they are byte for byte what
    the tool writes for the object, not only a text that the lint accepts."""
    for path, text in object_files().items():
        if names.key_for_path(path)[0] in names.TABLE_CLASS_KINDS:
            (obj,) = parse_object_file(text, path)
            assert emit.emit_object_file(obj) == text, path


def test_the_example_keeps_the_files_of_the_template_unchanged():
    """The workflows, the code owners and the line-end rules have one source: templates/db-repo."""
    copied = [".gitattributes", "schema/_tombstones.toml"]
    copied += [path.relative_to(TEMPLATE).as_posix() for path in sorted(TEMPLATE.glob(".github/**/*"))]
    files = [path for path in copied if (TEMPLATE / path).is_file()]
    assert len(files) == 6  # .gitattributes, tombstones, CODEOWNERS and three workflows
    for path in files:
        assert (EXAMPLE / path).read_bytes() == (TEMPLATE / path).read_bytes(), path
    everything = {p.relative_to(TEMPLATE).as_posix() for p in TEMPLATE.rglob("*") if p.is_file()}
    assert everything - set(files) == {"README.md", "azsqlcd.toml", "migrations/migrations.sum"}


def test_the_first_migration_of_the_example_is_proven_against_its_object_files(tmp_path):
    """verify, from a revision with no object: the shipped migration creates the shipped model."""
    repo, empty = empty_repository(tmp_path / "db-demo")
    shutil.copytree(EXAMPLE, repo, dirs_exist_ok=True)
    assert (repo / chain.SUM_PATH).read_text(encoding="utf-8") != SUM_HEADER

    done = run("verify", "--base", empty, "--root", str(repo))

    assert (done.code, done.out, done.err) == (0, CLEAN, "")


def test_the_first_migration_of_the_example_is_what_gen_writes(tmp_path):
    """The README says that nobody wrote 0001 by hand. gen writes the same bytes again."""
    repo, empty = empty_repository(tmp_path / "db-demo")
    shutil.copytree(EXAMPLE / "schema", repo / "schema", dirs_exist_ok=True)

    done = run("gen", "--base", empty, "--name", "initial_schema", "--root", str(repo))

    assert done.code == 0, done.err
    assert done.out == f"wrote migrations/{FIRST}\nwrote migrations/migrations.sum\n"
    for path in (f"migrations/{FIRST}", chain.SUM_PATH):
        assert (repo / path).read_bytes() == (EXAMPLE / path).read_bytes(), path
    # the function that a CHECK constraint calls is deployed above the table that has the constraint
    text = (repo / "migrations" / FIRST).read_text(encoding="utf-8")
    assert "-- azsqlcd:deploy-module [product].[fn_IsValidSku]\nCREATE TABLE [product].[Product] (" in text
    assert "azsqlcd:allow" not in text  # nothing is destructive and no table existed before


# ------------------------------------------------------------------ one change, as the quickstart tells it
def test_lint_accepts_the_example_as_it_is_copied(walk):
    assert walk.lint_of_the_copy == Done(0, CLEAN, "")


def test_verify_refuses_a_table_change_that_has_no_migration_and_shows_what_gen_would_write(walk):
    done = walk.verify_with_no_migration
    assert done.code == 22 and done.err.startswith("VERIFY_FAILED: ")
    assert "migrations/migrations.sum:1: error PRF002: " in done.out
    assert ADD_COLUMN in done.out and CREATE_INDEX in done.out


def test_gen_writes_the_migration_that_a_reader_expects(walk):
    assert walk.gen == Done(0, GEN_PRINTS, "")
    assert walk.generated == GENERATED
    # two statements for the two changes of the table file, in the fixed order: column, then index
    migration = chain.parse_migration(walk.generated, SECOND)
    assert migration.mode == "tx" and [batch.kind for batch in migration.batches] == ["model", "model"]
    # the procedure is not in the migration: its file is the change
    assert "usp_CreateCustomer" not in walk.generated
    # one new line at the end of the chain, with the checksum of the file as gen wrote it
    first_line = (EXAMPLE / chain.SUM_PATH).read_text(encoding="utf-8")
    sha = chain.file_sha256(GENERATED.encode("utf-8"))
    assert walk.sum_after_gen == f"{first_line}{SECOND} sha256:{sha} tx\n"


def test_a_generated_allow_line_stops_lint_until_a_person_writes_the_reason(walk):
    """An index build on a table that exists holds a lock. The tool asks for the reason and does
    not take TODO for one, so the pull request cannot merge with nobody having read the line."""
    done = walk.lint_with_todo
    assert (done.code, done.out) == (22, TODO_FINDING + "1 error(s), 0 warning(s)\n")
    assert done.err.startswith("LINT_FAILED: ") and done.err.endswith("azsqlcd lint: exit 22\n")


def test_a_hand_edit_of_a_new_migration_needs_gen_resum(walk):
    """The chain line holds the checksum of the file, so an edit shows until the line is written again."""
    done = walk.lint_before_resum
    assert (done.code, done.out) == (22, STALE_SUM_FINDING + "1 error(s), 0 warning(s)\n")
    assert walk.resum == Done(0, "wrote migrations/migrations.sum\n", "")
    assert walk.reviewed == REVIEWED
    sha = chain.file_sha256(REVIEWED.encode("utf-8"))
    assert walk.sum_after_resum.splitlines()[-1] == f"{SECOND} sha256:{sha} tx"
    assert walk.sum_after_resum.splitlines()[:-1] == walk.sum_after_gen.splitlines()[:-1]


def test_lint_and_verify_accept_the_reviewed_change(walk):
    assert walk.lint == Done(0, CLEAN, "")
    assert walk.verify == Done(0, CLEAN, "")


def test_verify_refuses_a_migration_that_does_not_give_the_object_files(walk):
    """The offline proof: the migration makes a smallint column and the table file says tinyint.
    The finding names the object and the property, and the right migration passes again."""
    done = walk.verify_of_another_migration
    assert done is not None and done.code == 22
    assert done.out == NOT_PROVEN_FINDING + "1 error(s), 0 warning(s)\n"
    assert walk.sum_restored == walk.sum_after_resum
    assert walk.verify_restored == Done(0, CLEAN, "")


def test_build_makes_one_release_of_the_merged_commit(walk):
    assert walk.build is not None and walk.built is not None and walk.dist is not None
    assert walk.build.code == 0, walk.build.err
    assert (walk.built["release"], walk.built["exit_code"], walk.built["reason_code"]) == ("r2", "0", "OK")
    digest = walk.built["digest"]
    assert walk.build.out == f"{CLEAN}release r2 of commit {walk.merged}: 45 file(s)\ndigest {digest}\n"
    assert sorted(path.name for path in walk.dist.iterdir()) == ["bundle.tar", "manifest.json"]

    bundle = release.read_bundle(walk.dist, digest)
    assert (bundle.manifest.commit, bundle.manifest.release_seq) == (walk.merged, 2)
    # each migration belongs to the release that added its chain line: the catch-up rule reads this
    assert bundle.manifest.chain_added_in == {FIRST: 1, SECOND: 2}
    # the release holds the reviewed migration and the changed files, read from git
    assert bundle.files[f"migrations/{SECOND}"].decode("utf-8") == REVIEWED
    assert NEW_COLUMN in bundle.files[CUSTOMER].decode("utf-8")
    assert "@LoyaltyTier tinyint = 0," in bundle.files[PROCEDURE].decode("utf-8")
    # only what a release needs: no workflow, no README
    assert {path.split("/", 1)[0] for path in bundle.files} == {"azsqlcd.toml", "schema", "migrations"}


def test_targets_gives_the_matrix_row_of_the_dev_database(walk):
    assert walk.targets is not None and walk.matrix is not None
    assert walk.targets == Done(0, TARGET_PRINTS, "")
    assert walk.matrix["timeout"] == "120"
    assert json.loads(walk.matrix["matrix"]) == [
        {
            "id": "demo-dev",
            "server": "replace-me-dev.database.windows.net",
            "database": "demo_sales",
            "plan_client_id": "00000000-0000-0000-0000-000000000001",
            "deploy_client_id": "00000000-0000-0000-0000-000000000003",
            "tenant_id": "00000000-0000-0000-0000-000000000000",
            "gated": False,
            "auth": "oidc",  # the example has no `auth` key: the default
        }
    ]


def test_a_change_of_a_module_alone_needs_no_migration(tmp_path):
    repo = tmp_path / "db-demo"
    shutil.copytree(EXAMPLE, repo)
    git(repo, "init", "-q", "-b", "main")
    base = commit(repo, "Demo sales database")
    for old, new in PROCEDURE_EDITS[:1]:
        edit(repo, PROCEDURE, old, new)

    generated = run("gen", "--base", base, "--name", "procedure_only", "--root", str(repo))
    verified = run("verify", "--base", base, "--root", str(repo))

    assert generated == Done(0, "no migration: only modules changed\n", "")
    assert sorted(path.name for path in (repo / "migrations").iterdir()) == [FIRST, "migrations.sum"]
    assert verified == Done(0, CLEAN, "")


# ------------------------------------------------------------------ the quickstart
def test_the_quickstart_shows_the_edits_and_the_texts_of_this_walk(walk):
    """docs/quickstart.md promises exact output. Each text here is what the walk above made or
    printed, so the document cannot drift from the tool without a failing test."""
    text = QUICKSTART.read_text(encoding="utf-8")
    shown = {
        "the new column": NEW_COLUMN,
        "the new index": NEW_INDEX,
        "the generated migration": walk.generated,
        "the output of gen": walk.gen.out,
        "the allow line with the reason": ALLOW_REASON,
        "the finding for TODO": TODO_FINDING,
        "the finding for a stale checksum": STALE_SUM_FINDING,
        "the finding of the proof": NOT_PROVEN_FINDING,
        "the output of targets": walk.targets.out,
        "the release line of build": ": 45 file(s)\n",
    }
    shown |= {f"procedure edit {n}": new for n, (_, new) in enumerate(PROCEDURE_EDITS, start=1)}
    missing = [what for what, block in shown.items() if not block or block not in text]
    assert missing == []


# ------------------------------------------------------------------ the releases on a fake database
# The two tests below send the releases of the example through plan and runner. The session is the
# fake database of the offline end-to-end test (test_runner.Db with the table catalog of
# test_catalog_tables.rows_from_model). That fake is one database of the project "sales", so the
# tests give the copy of the example the azsqlcd.toml of that test. The object files and the
# migrations are those of the example. The helpers are imported inside the functions: a change of
# them fails these two tests and no other test of this file.
def is_module(path: str) -> bool:
    return names.key_for_path(path)[0] in names.MODULE_KINDS


def module_texts(files: dict[str, bytes]) -> dict[str, modules.ModuleFile]:
    """module text -> module file, for the module files of a release."""
    found = {
        path: data
        for path, data in files.items()
        if path.startswith("schema/") and path.count("/") == 2 and is_module(path)
    }
    return {m.text: m for m in (modules.read_module(path, data) for path, data in found.items())}


def repository_of_the_fake(folder: Path) -> tuple[Path, str]:
    """The example as a git repository whose config names the database of the fake: (path, commit)."""
    from unit.test_e2e_offline import TOML

    shutil.copytree(EXAMPLE, folder)
    (folder / "azsqlcd.toml").write_text(TOML, encoding="utf-8")
    git(folder, "init", "-q", "-b", "main")
    first = commit(folder, "Demo sales database")
    git(folder, "update-ref", "refs/remotes/origin/main", first)
    return folder, first


def built(repo: Path, dist: Path) -> release.Bundle:
    done = run("build", "--commit", "HEAD", "--out", str(dist), "--root", str(repo))
    assert done.code == 0, done.err
    return release.read_bundle(dist, done.out.splitlines()[-1].removeprefix("digest "))


def deploy_on(db, dist: Path, bundle: release.Bundle, out: Path) -> tuple[Done, dict, object]:
    """deploy --inline-plan to dev on the fake: (the call, report.json, the session of the syntax check)."""
    from unit.test_cli import Sessions, Tokens
    from unit.test_runner import parse_only_session

    second = parse_only_session()
    where = ("--bundle", str(dist), "--digest", release.digest(bundle.manifest))
    args = ["deploy", *where, "--env", "dev", "--target", "sales-dev", "--inline-plan", "--out", str(out)]
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(args, session_factory=Sessions(db, second), token_provider=Tokens())
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    return Done(code, stdout.getvalue(), stderr.getvalue()), report, second


def test_the_first_release_of_the_example_deploys_to_an_empty_database_in_one_ordered_run(tmp_path):
    """Every batch of the migration once and in file order, the function that a CHECK constraint
    calls directly above its table, then each other module once and after the modules it needs."""
    from azsqlcd import gen
    from unit.test_catalog_tables import COLUMNS, rows_from_model
    from unit.test_runner import Db, recorded

    repo, _ = repository_of_the_fake(tmp_path / "db-demo")
    bundle = built(repo, tmp_path / "dist")
    texts = module_texts(bundle.files)
    module_bundle = release.Bundle(bundle.manifest, {m.path: bundle.files[m.path] for m in texts.values()})
    db = Db(module_bundle, recorded(seq=0))
    rows = rows_from_model(gen.load_model(bundle.files))
    for name in COLUMNS:  # the tables are in the catalog from the first CREATE TABLE on

        def read(batch: str, name: str = name) -> list:
            visible = db.sent("CREATE TABLE [product].[Category]")
            return [[tuple(found.values()) for found in rows[name]] if visible else []]

        db.respond(f"/* azsqlcd:read_{name} */", read)

    done, report, second = deploy_on(db, tmp_path / "dist", bundle, tmp_path / "report")

    assert (done.code, report["reason_code"]) == (0, "OK"), done.err
    migration = chain.parse_migration(bundle.files[f"migrations/{FIRST}"].decode("utf-8"), FIRST)
    batches = [batch.text for batch in migration.batches]
    function = next(text for text, m in texts.items() if m.name == "fn_IsValidSku")
    table = next(text for text in batches if "CREATE TABLE [product].[Product] (" in text)
    work = [text for text in db.batches if text in batches or text in texts]
    assert work[: len(batches) + 1] == [
        *batches[: batches.index(table)],
        function,
        *batches[batches.index(table) :],
    ]
    after_the_migration = work[len(batches) + 1 :]
    assert sorted(after_the_migration) == sorted(set(texts) - {function})
    at = {texts[text].key: n for n, text in enumerate(work) if text in texts}
    edges = modules.build_edges(list(texts.values()), ())
    assert all(at[needed] < at[key] for key, needs in edges.items() for needed in needs)
    # no batch is sent twice, and the syntax check read each one once before anything ran
    for text in (*batches, *texts):
        assert (db.batches.count(text), second.batches.count(text)) == (1, 1)
    assert len(report["modules_deployed"]) == 20 and report["failed_step"] is None
    assert not any(secret in done.out + done.err for secret in ("CREATE TABLE", "SELECT", "fn_IsValidSku ("))


def test_the_change_of_the_quickstart_deploys_to_a_database_that_holds_the_first_release(tmp_path):
    """Release r2 sends the two statements of its migration and the one procedure that changed. The
    plan shows the index build with the reason that the author wrote, for the person who approves."""
    import dataclasses

    from azsqlcd import gen, state
    from azsqlcd.state import ObjectRow, State, StepRow
    from unit.test_catalog_tables import COLUMNS, rows_from_model
    from unit.test_runner import RUN_ID, Db, recorded, row, run_row
    from unit.test_tables import captures_of

    repo, first = repository_of_the_fake(tmp_path / "db-demo")
    base_files = gen.read_working_tree(repo)
    base_model = gen.load_model(base_files)
    root = ("--root", str(repo))
    edit(repo, CUSTOMER, LAST_COLUMN, LAST_COLUMN + NEW_COLUMN)
    (repo / CUSTOMER).write_bytes((repo / CUSTOMER).read_bytes() + NEW_INDEX.encode("utf-8"))
    for old, new in PROCEDURE_EDITS:
        edit(repo, PROCEDURE, old, new)
    assert run("gen", "--name", "customer_loyalty_tier", *root).code == 0
    migration_file = repo / "migrations" / SECOND
    migration_file.write_bytes(REVIEWED.encode("utf-8"))
    assert run("gen", "--resum", *root).code == 0
    assert run("verify", "--base", first, *root).code == 0
    git(repo, "update-ref", "refs/remotes/origin/main", commit(repo, "Add the loyalty tier of a customer"))
    bundle = built(repo, tmp_path / "dist")
    assert bundle.manifest.release_seq == 2
    texts = module_texts(bundle.files)

    # ---- the state that the deploy of r1 left, and a catalog that holds its tables
    objects = {
        m.key: row(m.key, m.text, schema_bound=m.schema_bound) for m in module_texts(base_files).values()
    }
    objects |= {
        key: ObjectRow("managed", None, 1, captured, state.capture_sha256(captured))
        for key, captured in captures_of(base_model).items()
    }
    steps = (
        StepRow(
            1, RUN_ID, "migration", FIRST, chain.file_sha256(base_files[f"migrations/{FIRST}"]), "ok", None
        ),
        StepRow(2, RUN_ID, "modules", None, None, "ok", "deployed 19, dropped 0"),
    )
    latest = dataclasses.replace(run_row(RUN_ID, "ok", seq=1), git_sha=first)
    holds_first = State(recorded().meta, (), latest, steps, objects)
    tables = [(obj.schema, obj.name, "U") for obj in base_model.values() if isinstance(obj, Table)]
    module_bundle = release.Bundle(bundle.manifest, {m.path: bundle.files[m.path] for m in texts.values()})
    db = Db(module_bundle, holds_first, user_objects=tables)
    before, after = rows_from_model(base_model), rows_from_model(gen.load_model(bundle.files))
    for name in COLUMNS:  # the catalog shows the new column and the new index once both were sent

        def read(batch: str, name: str = name) -> list:
            rows = after if db.sent(CREATE_INDEX.strip()) else before
            return [[tuple(found.values()) for found in rows[name]]]

        db.respond(f"/* azsqlcd:read_{name} */", read)

    done, report, _ = deploy_on(db, tmp_path / "dist", bundle, tmp_path / "report")

    assert (done.code, report["reason_code"]) == (0, "OK"), done.err
    procedure = next(text for text, m in texts.items() if m.name == "usp_CreateCustomer")
    batches = [batch.text for batch in chain.parse_migration(REVIEWED, SECOND).batches]
    assert [text for text in db.batches if text in batches or text in texts] == [*batches, procedure]
    assert report["modules_deployed"] == ["PROCEDURE:[sales].[usp_CreateCustomer]"]
    plan_doc = json.loads((tmp_path / "report" / "plan.json").read_text(encoding="utf-8"))
    assert [(item["code"], item["object"], item["reason"]) for item in plan_doc["destructive"]] == [
        ("LONG_LOCK", "[sales].[Customer]", REASON)
    ]
