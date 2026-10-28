"""The parts of the tool against one database: closed loops through the command line.

Every step is a call of cli.main in a temporary git repository. Where a command opens a session
it gets one of support.fake_database.FakeDatabase, which keeps a catalog and the state tables with
the parser, the replay and the catalog rows of the tool itself. So nothing here scripts an answer:
what deploy writes is what plan, drift, export and baseline read. No database and no network.

A test that fails because the tool is wrong stays, with xfail(strict=True) and the reason.
"""

import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from azsqlcd import catalog, catalog_tables, chain, cli, emit, names, release, runner, state, tables
from azsqlcd.config import load_config
from azsqlcd.errors import ToolError
from azsqlcd.model import (
    ON_PRIMARY,
    Column,
    DefaultConstraint,
    Expression,
    ForeignKey,
    Index,
    KeyColumn,
    Model,
    Partition,
    PrimaryKey,
    Schema,
    Table,
    TypeRef,
)
from azsqlcd.release import Bundle
from azsqlcd.session import AccessToken
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error
from support.fake_database import FakeDatabase, FakeDbSession

REPO = Path(__file__).resolve().parents[2]
TARGET = ["--env", "dev", "--target", "sales-dev"]
TOML = """
[project]
name = "sales"
tenant_id = "11111111-1111-1111-1111-111111111111"
table_model = {table_model}
module_chunk = 100
min_token_minutes = 20

[identities]
plan = "22222222-2222-2222-2222-222222222222"
deploy = "33333333-3333-3333-3333-333333333333"

[env.dev]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
gated = false
targets = [{{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }}]
"""

# ------------------------------------------------------------------ the objects of release 1
SALES = Schema("sales")
CUSTOMER = Table(
    "sales",
    "Customer",
    (Column("CustomerId", TypeRef("int"), False), Column("Name", TypeRef("nvarchar", length=100), False)),
    (PrimaryKey("PK_Customer", True, (KeyColumn("CustomerId"),)),),
)
ORDER = Table(
    "sales",
    "Order",
    (
        Column("OrderId", TypeRef("int"), False),
        Column("CustomerId", TypeRef("int"), False),
        Column(
            "Status",
            TypeRef("tinyint"),
            False,
            default=DefaultConstraint("DF_Order_Status", Expression.from_sql("((0))")),
        ),
        Column("Reference", TypeRef("nvarchar", length=50), True),
    ),
    (
        PrimaryKey("PK_Order", True, (KeyColumn("OrderId"),)),
        ForeignKey("FK_Order_Customer", ("CustomerId",), "sales", "Customer", ("CustomerId",)),
    ),
    (Index("IX_Order_Status", False, False, (KeyColumn("Status"),)),),
)
VIEW = (
    "schema/views/sales.vw_OpenOrders.sql",
    "CREATE OR ALTER VIEW [sales].[vw_OpenOrders] AS\n"
    "SELECT [OrderId], [Reference] FROM [sales].[Order] WHERE [Status] = 0;\n",
)
FUNCTION = (
    "schema/functions/sales.fn_OpenOrders.sql",
    "CREATE OR ALTER FUNCTION [sales].[fn_OpenOrders] ()\nRETURNS int\nAS\nBEGIN\n"
    "    RETURN (SELECT COUNT(*) FROM [sales].[Order] WHERE [Status] = 0);\nEND;\n",
)
PROCEDURE = (
    "schema/procedures/sales.usp_CountOrders.sql",
    "CREATE OR ALTER PROCEDURE [sales].[usp_CountOrders] AS\n"
    "SELECT COUNT(*) AS [n] FROM [sales].[vw_OpenOrders];\n",
)
VIEW_KEY = names.object_key("VIEW", "sales", "vw_OpenOrders")
FUNCTION_KEY = names.object_key("FUNCTION", "sales", "fn_OpenOrders")
PROCEDURE_KEY = names.object_key("PROCEDURE", "sales", "usp_CountOrders")
MODULES = {VIEW_KEY: VIEW, FUNCTION_KEY: FUNCTION, PROCEDURE_KEY: PROCEDURE}

# ------------------------------------------------------------------ the changes of release 2
ORDER_2 = dataclasses.replace(
    ORDER,
    columns=(*ORDER.columns, Column("Note", TypeRef("nvarchar", length=200), True)),
    indexes=(*ORDER.indexes, Index("IX_Order_Customer", False, False, (KeyColumn("CustomerId"),))),
)
PROCEDURE_2 = (
    PROCEDURE[0],
    "CREATE OR ALTER PROCEDURE [sales].[usp_CountOrders] AS\n"
    "SELECT COUNT(*) AS [n], MAX([Reference]) AS [last] FROM [sales].[vw_OpenOrders];\n",
)
REFRESH_VIEW = "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[vw_OpenOrders]';"


# ------------------------------------------------------------------ helpers
class Tokens:
    def get(self) -> AccessToken:
        return AccessToken("the-access-token-of-the-test", int(time.time()) + 3600)


@dataclasses.dataclass(frozen=True)
class Outcome:
    code: int
    out: str
    err: str

    @property
    def reason(self) -> str:
        """The reason code of a non-zero end: the first word of the first line on stderr."""
        return self.err.split(":", 1)[0]


@dataclasses.dataclass(frozen=True)
class Built:
    """One release: the directory of bundle.tar and manifest.json, and what it holds."""

    dist: Path
    digest: str
    bundle: Bundle
    migration: chain.Migration | None  # the migration that the pull request added
    verified: str = ""  # what `verify` printed for the pull request: its findings

    @property
    def where(self) -> list[str]:
        return ["--bundle", str(self.dist), "--digest", self.digest, *TARGET]

    @property
    def batches(self) -> list[str]:
        return [batch.text for batch in self.migration.batches] if self.migration else []

    @property
    def model(self) -> Model:
        return tables.head_model(self.bundle.files)


class Project:
    """A database repository in a temporary directory, and the command line of the tool."""

    def __init__(self, folder: Path, capsys: pytest.CaptureFixture[str], *, table_model: bool = True) -> None:
        self.folder, self.capsys, self.repo = folder, capsys, folder / "db-sales"
        self.calls = 0
        shutil.copytree(REPO / "templates" / "db-repo", self.repo)
        self.write("azsqlcd.toml", TOML.format(table_model=str(table_model).lower()))
        self.git("init", "-q", "-b", "main")
        self.main = self.commit("repository from the template")

    def git(self, *args: str) -> str:
        identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
        done = subprocess.run(
            ["git", *identity, *args], cwd=self.repo, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def write(self, path: str, text: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_bytes(text.encode("utf-8"))

    def write_objects(self, *objects: Schema | Table) -> None:
        """Table-class files in the canonical text of the tool, as `export` and a formatter write them."""
        for obj in objects:
            path = names.path_for(obj.kind, None if isinstance(obj, Schema) else obj.schema, obj.name)
            self.write(path, emit.emit_object_file(obj))

    def tool(self, *argv: str, db: FakeDatabase | None = None) -> Outcome:
        """One call of the command line. A batch that the fake did not understand fails the test."""
        self.capsys.readouterr()
        code = cli.main(
            list(argv), session_factory=db.session if db is not None else None, token_provider=Tokens()
        )
        captured = self.capsys.readouterr()
        if db is not None:
            assert db.unknown == [], f"the fake did not understand: {db.unknown}\n{captured.err}"
            assert all(session.closed for session in db.sessions), "a command left a session open"
        return Outcome(code, captured.out, captured.err)

    def database(self) -> FakeDatabase:
        """An empty database in which an administrator ran the output of `azsqlcd setup-sql`."""
        script = self.tool("setup-sql", *TARGET, "--root", str(self.repo))
        assert script.code == 0, script.err
        return FakeDatabase("sales", setup_sql=script.out)

    def out(self, name: str) -> Path:
        self.calls += 1
        return self.folder / f"{name}-{self.calls}"

    def release(self, message: str, migration: str | None = None) -> Built:
        """A pull request (gen, lint, verify), its merge to main, and the build of the release.

        migration: the name for `gen`; None when the pull request changes modules only. Each allow
        line that gen wrote gets a reason, then `gen --resum`, as the author of a pull request does.
        """
        root = ["--root", str(self.repo)]
        added: chain.Migration | None = None
        if migration is not None:
            generated = self.tool("gen", "--base", self.main, "--name", migration, *root)
            assert generated.code == 0, generated.err
            file = next(line.split(" ")[1] for line in generated.out.splitlines() if "__" in line)
            path = self.repo / file
            if "reason: TODO" in path.read_text():
                path.write_text(path.read_text().replace("reason: TODO", "reason: agreed with the owner"))
                resum = self.tool("gen", "--base", self.main, "--resum", *root)
                assert resum.code == 0, resum.err
            added = chain.parse_migration(path.read_text(), path.name)
        for command in (["lint"], ["verify", "--base", self.main]):
            checked = self.tool(*command, *root)
            assert checked.code == 0, f"{command[0]}: {checked.out}{checked.err}"
        self.main = self.commit(message)
        self.git("update-ref", "refs/remotes/origin/main", self.main)
        return dataclasses.replace(self.build(added), verified=checked.out)

    def build(self, added: chain.Migration | None = None) -> Built:
        dist = self.out("dist")
        built = self.tool("build", "--commit", self.main, "--out", str(dist), "--root", str(self.repo))
        assert built.code == 0, built.out + built.err
        digest = next(line.split(" ")[1] for line in built.out.splitlines() if line.startswith("digest "))
        return Built(dist, digest, release.read_bundle(dist, digest), added)

    def deploy(self, built: Built, db: FakeDatabase) -> Outcome:
        return self.tool("deploy", *built.where, "--inline-plan", "--out", str(self.out("report")), db=db)

    def report(self) -> dict:
        """report.json of the last deploy or resolve."""
        return json.loads((self.folder / f"report-{self.calls}" / "report.json").read_text())


@pytest.fixture
def project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Project:
    return Project(tmp_path, capsys)


def release_1(project: Project) -> Built:
    project.write_objects(SALES, CUSTOMER, ORDER)
    for path, text in MODULES.values():
        project.write(path, text)
    return project.release("sales schema", "create_sales")


def release_2(project: Project) -> Built:
    project.write_objects(ORDER_2)
    project.write(*PROCEDURE_2)
    return project.release("order notes", "order_note")


def deployed(project: Project, *releases: Built) -> FakeDatabase:
    """An empty database with the state tables of the setup script, and these releases deployed."""
    db = project.database()
    for built in releases:
        done = project.deploy(built, db)
        assert done.code == 0, done.err
    return db


def managed(db: FakeDatabase) -> dict[str, str | None]:
    """Object key -> recorded source checksum, of every managed object."""
    return {
        row["object_key"]: row["source_sha256"] for row in db.rows("object") if row["status"] == "managed"
    }


# ------------------------------------------------------------------ 1. the first release
def test_release_1_creates_every_object_on_an_empty_database_and_a_second_deploy_sends_nothing(project):
    first = release_1(project)
    db = project.database()

    done = project.deploy(first, db)

    assert done.code == 0, done.err
    assert db.model == first.model and set(db.model) == {SALES.key, CUSTOMER.key, ORDER.key}
    for key, (_, text) in MODULES.items():
        stored = db.module(key)
        assert stored is not None and stored.sent == text
        assert stored.uses_ansi_nulls and stored.uses_quoted_identifier
    # the unit of work: the migration batches in file order, then the modules in dependency order
    assert db.ddl == [*first.batches, FUNCTION[1], VIEW[1], PROCEDURE[1]]
    assert set(managed(db)) == {*db.model, *MODULES}
    assert [(row["status"], row["release_seq"], row["segments_committed"]) for row in db.rows("run")] == [
        ("ok", first.bundle.manifest.release_seq, 1)
    ]
    assert [(step["kind"], step["migration_id"], step["status"]) for step in db.rows("step")] == [
        ("migration", first.migration.file, "ok"),
        ("modules", None, "ok"),
    ]
    assert db.lock_holder is None

    sent, runs = len(db.batches), db.rows("run")
    again = project.deploy(first, db)

    assert again.code == 0, again.err
    assert db.ddl == [*first.batches, FUNCTION[1], VIEW[1], PROCEDURE[1]]
    assert db.rows("run") == runs and project.report()["run_id"] is None
    writes = ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "BEGIN TRAN")
    assert [b for b in db.batches[sent:] if b.lstrip().upper().startswith(writes)] == []


# ------------------------------------------------------------------ 2. a release that changes objects
def test_release_2_is_generated_proven_and_applied_exactly_and_leaves_no_drift(project):
    first = release_1(project)
    db = deployed(project, first)
    second = release_2(project)  # gen wrote the migration; lint and verify passed

    assert second.migration is not None and len(second.batches) == 2
    assert second.batches[0].endswith("ALTER TABLE [sales].[Order] ADD [Note] nvarchar(200) NULL;")
    assert "\nCREATE NONCLUSTERED INDEX [IX_Order_Customer] ON [sales].[Order]" in second.batches[1]
    sent = len(db.ddl)

    done = project.deploy(second, db)

    assert done.code == 0, done.err
    # exactly the migration, the changed procedure, and the refresh of the view on the altered table
    assert db.ddl[sent:] == [*second.batches, PROCEDURE_2[1], REFRESH_VIEW]
    assert db.model == second.model and db.model != first.model
    assert db.module(PROCEDURE_KEY).sent == PROCEDURE_2[1]
    assert db.module(VIEW_KEY).sent == VIEW[1]
    report = project.report()
    assert report["modules_deployed"] == [PROCEDURE_KEY] and report["reason_code"] == "OK"
    assert [step["migration_id"] for step in db.rows("step") if step["kind"] == "migration"] == [
        first.migration.file,
        second.migration.file,
    ]

    drift = project.tool("drift", *second.where, db=db)

    assert drift.code == 0 and drift.out.strip() == "no drift"


# ------------------------------------------------------------------ 3. drift
ORDER_3 = dataclasses.replace(
    ORDER_2, columns=(*ORDER_2.columns, Column("Priority", TypeRef("tinyint"), True))
)
PROCEDURE_3 = (
    PROCEDURE[0],
    "CREATE OR ALTER PROCEDURE [sales].[usp_CountOrders] AS\n"
    "SELECT COUNT(*) AS [n], MIN([Reference]) AS [first] FROM [sales].[vw_OpenOrders];\n",
)
HOTFIX = "CREATE OR ALTER PROCEDURE [sales].[usp_CountOrders] AS\nSELECT 0 AS [n];\n"
RESOLVE = ["--confirm-database", "sales", "--reason", "the hotfix was reviewed"]


def release_3(project: Project) -> Built:
    project.write_objects(ORDER_3)
    project.write(*PROCEDURE_3)
    return project.release("order priority", "order_priority")


def test_a_change_behind_the_tool_is_drift_blocks_a_release_that_touches_it_and_can_be_accepted(project):
    first, second = release_1(project), release_2(project)
    db = deployed(project, first, second)
    db.out_of_band(HOTFIX, "ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(400) NULL;")

    drift = project.tool("drift", *second.where, db=db)

    assert (drift.code, drift.reason) == (30, "DRIFT_FOUND")
    found = [line.split(" ")[:2] for line in drift.out.splitlines()]
    assert ["managed", PROCEDURE_KEY] in found and ["managed", ORDER.key] in found
    assert {key for _, key in found} == {PROCEDURE_KEY, ORDER.key}
    assert any(line.startswith(f"managed {PROCEDURE_KEY} definition ") for line in drift.out.splitlines())
    assert any(line.startswith(f"managed {ORDER.key} column [Note] ") for line in drift.out.splitlines())
    assert "SELECT 0" not in drift.out + drift.err and "nvarchar(400)" not in drift.out + drift.err

    # a release that touches both is refused, by the plan job and by the deploy
    third = release_3(project)
    sent, state_before = len(db.ddl), (db.rows("run"), db.rows("step"), db.rows("object"))
    plan = project.tool("plan", *third.where, "--out", str(project.out("plan")), db=db)
    deploy = project.deploy(third, db)

    for refused in (plan, deploy):
        assert (refused.code, refused.reason) == (22, "DRIFT_TOUCHED")
        assert PROCEDURE_KEY in refused.err and ORDER.key in refused.err
    assert db.ddl[sent:] == [] and (db.rows("run"), db.rows("step"), db.rows("object")) == state_before

    # the drift of the module is accepted: its capture is the live text, its source is not known
    accept = project.tool("resolve", *third.where, *RESOLVE, "--accept-drift", PROCEDURE_KEY, db=db)

    assert accept.code == 0, accept.err
    assert managed(db)[PROCEDURE_KEY] is None
    drift = project.tool("drift", *second.where, db=db)
    assert drift.code == 30 and {line.split(" ")[1] for line in drift.out.splitlines()} == {ORDER.key}

    # a table is accepted only when it equals the model of the release; here it must be put back
    table = project.tool("resolve", *third.where, *RESOLVE, "--accept-drift", ORDER.key, db=db)
    assert (table.code, table.reason) == (22, "READBACK_MISMATCH")
    still = project.deploy(third, db)
    assert (still.code, still.reason) == (22, "DRIFT_TOUCHED") and PROCEDURE_KEY not in still.err
    db.out_of_band("ALTER TABLE [sales].[Order] ALTER COLUMN [Note] nvarchar(200) NULL;")

    done = project.deploy(third, db)

    assert done.code == 0, done.err
    assert db.model == third.model and db.module(PROCEDURE_KEY).sent == PROCEDURE_3[1]
    assert project.tool("drift", *third.where, db=db).code == 0


# ------------------------------------------------------------------ 4. a batch that fails
def test_a_batch_that_fails_rolls_the_release_back_and_the_next_deploy_applies_it(project):
    first = release_1(project)
    db = deployed(project, first)
    second = release_2(project)
    before = (db.model, dict(db.modules), db.rows("step"), db.rows("object"), db.rows("run"))
    db.fail_on(second.batches[1], sql_error("The fake engine could not build the index."))

    failed = project.deploy(second, db)

    assert (failed.code, failed.reason) == (21, "BATCH_FAILED")
    assert second.batches[0] in db.ddl  # the first statement ran, and the rollback took it back
    assert (db.model, db.modules, db.rows("step"), db.rows("object")) == before[:4]
    assert db.model == first.model and db.module(PROCEDURE_KEY).sent == PROCEDURE[1]
    ok, run = db.rows("run")
    assert ok == before[4][0]
    assert (run["status"], run["segments_committed"], run["note"]) == ("failed", 0, "BATCH_FAILED")
    assert run["failed_step"] == f"{second.migration.file}#2"
    report = project.report()
    assert (report["exit_code"], report["failed_step"]) == (21, f"{second.migration.file}#2")
    assert report["steps_applied"] == [] and db.lock_holder is None

    done = project.deploy(second, db)

    assert done.code == 0, done.err
    assert db.model == second.model and db.module(PROCEDURE_KEY).sent == PROCEDURE_2[1]
    assert [run["status"] for run in db.rows("run")] == ["ok", "failed", "ok"]
    assert project.tool("drift", *second.where, db=db).code == 0


# ------------------------------------------------------------------ 5. a session that is killed
def test_a_session_killed_in_the_transaction_is_read_as_rolled_back_and_the_next_deploy_applies(project):
    """runner.RECONCILE_BY_LOCKING_READ (live spike L6, acceptance run4): the run that lost its
    session reads the outcome on a new session, so the kill is a clean stop (24) with no dead run."""
    first = release_1(project)
    db = deployed(project, first)
    second = release_2(project)
    before = (db.model, dict(db.modules), db.rows("step"), db.rows("object"), db.rows("run"))
    db.kill_on(PROCEDURE_2[1])  # after both migration batches, inside the transaction

    lost = project.deploy(second, db)

    assert (lost.code, lost.reason) == (24, "CONNECTION_LOST_ROLLED_BACK")
    assert all(batch in db.ddl for batch in second.batches)
    assert (db.model, db.modules, db.rows("step"), db.rows("object")) == before[:4]
    ok, closed = db.rows("run")
    assert ok == before[4][0]
    assert (closed["status"], closed["segments_committed"]) == ("failed", 0)
    assert closed["note"] == "connection lost; the unit of work was rolled back"
    assert closed["finished_utc"] is not None and db.lock_holder is None
    assert len(db.sent("azsqlcd:read_fence_locking")) == 1

    plan = project.tool("plan", *second.where, "--out", str(project.out("plan")), db=db)
    assert plan.code == 0, plan.err
    assert "has the status running" not in plan.out  # no dead run is left for the next deploy

    done = project.deploy(second, db)

    assert done.code == 0, done.err
    assert db.model == second.model and db.module(PROCEDURE_KEY).sent == PROCEDURE_2[1]
    assert [run["status"] for run in db.rows("run")] == ["ok", "failed", "ok"]


def test_a_killed_session_whose_outcome_no_new_session_can_read_is_exit_23_and_is_reconciled_later(
    project, monkeypatch
):
    """The 23 path: here the locking read is switched off; on an engine it is the read that gets
    no lock or no answer. The run row stays `running` and the next deploy reconciles it (A4)."""
    monkeypatch.setattr(runner, "RECONCILE_BY_LOCKING_READ", False)
    first = release_1(project)
    db = deployed(project, first)
    second = release_2(project)
    before = (db.model, dict(db.modules), db.rows("step"), db.rows("object"), db.rows("run"))
    db.kill_on(PROCEDURE_2[1])  # after both migration batches, inside the transaction

    lost = project.deploy(second, db)

    assert (lost.code, lost.reason) == (23, "CONNECTION_LOST_TX")
    assert all(batch in db.ddl for batch in second.batches)
    assert (db.model, db.modules, db.rows("step"), db.rows("object")) == before[:4]
    ok, dead = db.rows("run")
    assert ok == before[4][0]
    assert (dead["status"], dead["segments_committed"], dead["finished_utc"]) == ("running", 0, None)
    assert db.lock_holder is None  # the lock went with the session

    # a plan sees no live run (the applock is free) and tells that the next deploy reconciles
    plan = project.tool("plan", *second.where, "--out", str(project.out("plan")), db=db)
    assert plan.code == 0, plan.err
    assert f"run {dead['run_id']} has the status running" in plan.out

    done = project.deploy(second, db)

    assert done.code == 0, done.err
    assert db.model == second.model and db.module(PROCEDURE_KEY).sent == PROCEDURE_2[1]
    runs = db.rows("run")
    assert [(run["status"], run["note"]) for run in runs] == [
        ("ok", None),
        ("failed", "reconciled"),
        ("ok", None),
    ]


# ------------------------------------------------------------------ 6. a view on a dropped column
ORDER_NO_REFERENCE = dataclasses.replace(
    ORDER, columns=tuple(column for column in ORDER.columns if column.name != "Reference")
)
VIEW_NO_REFERENCE = (VIEW[0], VIEW[1].replace(", [Reference]", ""))
REFS = (
    "schema/procedures/sales.usp_Refs.sql",
    "CREATE OR ALTER PROCEDURE [sales].[usp_Refs] AS\nSELECT [Reference] FROM [sales].[Order];\n",
)
REFS_KEY = names.object_key("PROCEDURE", "sales", "usp_Refs")


def test_a_dropped_column_that_a_view_still_uses_fails_the_deploy_and_nothing_is_committed(project):
    first = release_1(project)
    db = deployed(project, first)
    before = (db.model, dict(db.modules), db.rows("step"), db.rows("object"))
    project.write_objects(ORDER_NO_REFERENCE)  # the view still selects [Reference]
    second = project.release("drop the reference", "drop_reference")
    assert second.batches[0].endswith("ALTER TABLE [sales].[Order] DROP COLUMN [Reference];")
    # the pull request is not refused: the residue lint is a warning, and the deploy is the gate (A19)
    assert f"{VIEW[0]}:2: warning DRP003" in second.verified

    failed = project.deploy(second, db)

    # the view is unchanged and on an altered table, so it is refreshed, and the engine refuses that.
    # N1-F3: this is the finding of A12 for a view, as the sweep has it for a procedure: the reason
    # is DEPENDANT_BROKEN and the message names the module (it was BATCH_FAILED before)
    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN")
    assert VIEW_KEY in failed.err and "ERROR_207" in failed.err
    report = project.report()
    assert report["failed_step"] == f"refresh:{VIEW_KEY}" and report["steps_applied"] == []
    assert second.batches[0] in db.ddl and REFRESH_VIEW not in db.ddl
    assert (db.model, db.modules, db.rows("step"), db.rows("object")) == before
    run = db.rows("run")[-1]
    assert (run["status"], run["segments_committed"], run["error_number"]) == ("failed", 0, 207)
    assert "Reference" not in failed.err + str(run["error_text"])  # A25: the engine message is redacted
    assert project.tool("drift", *first.where, db=db).code == 0


def test_a_dropped_column_that_a_procedure_still_uses_is_found_by_the_sweep_of_dependants(project):
    first = release_1(project)
    project.write(*REFS)
    refs = project.release("a procedure that reads the reference")
    db = deployed(project, first, refs)
    before = (db.model, dict(db.modules), db.rows("step"), db.rows("object"))
    project.write_objects(ORDER_NO_REFERENCE)
    project.write(*VIEW_NO_REFERENCE)  # the view is fixed in the pull request; the procedure is not
    third = project.release("drop the reference", "drop_reference")

    failed = project.deploy(third, db)

    # a procedure is not refreshed; sys.dm_sql_referenced_entities in the transaction finds it (A12)
    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN")
    assert REFS_KEY in failed.err and project.report()["failed_step"] == "dependants"
    assert VIEW_NO_REFERENCE[1] in db.ddl  # the view was sent, and the rollback took it back
    assert (db.model, db.modules, db.rows("step"), db.rows("object")) == before
    assert db.rows("run")[-1]["status"] == "failed"


# ------------------------------------------------------------------ 7. export and baseline
def test_an_export_passes_lint_and_verify_in_a_new_repository_and_a_baseline_records_it(
    project, tmp_path, capsys, monkeypatch
):
    first, second = release_1(project), release_2(project)
    db = deployed(project, first, second)
    exported, sent = tmp_path / "exported", (len(db.ddl), db.rows("run"), db.rows("object"))

    done = project.tool("export", "--root", str(project.repo), *TARGET, "--out", str(exported), db=db)

    assert done.code == 0, done.err
    assert (len(db.ddl), db.rows("run"), db.rows("object")) == sent  # an export changes nothing
    files = {
        path.relative_to(exported).as_posix(): path.read_text() for path in exported.glob("schema/*/*.sql")
    }  # a repository path has forward slashes on every operating system
    paths = {names.path_for(obj.kind, getattr(obj, "schema", None), obj.name) for obj in db.model.values()}
    assert set(files) == paths | {VIEW[0], FUNCTION[0], PROCEDURE[0]}
    assert tables.head_model({path: text.encode() for path, text in files.items()}) == db.model
    assert files[PROCEDURE[0]].strip() == PROCEDURE_2[1].strip()

    # a new repository: one pull request adds the files and switches the table model on (Part 2 (c) 8)
    (tmp_path / "onboard").mkdir()
    onboard = Project(tmp_path / "onboard", capsys, table_model=False)
    shutil.copytree(exported, onboard.repo, dirs_exist_ok=True)
    onboard.write("azsqlcd.toml", TOML.format(table_model="true"))
    onboard.write("migrations/migrations.sum", "azsqlcd-sum 1\nbaseline\n")
    built = onboard.release("onboard the sales database")  # lint and verify pass, the release is built

    # a second database with the same content, made by somebody else
    other = onboard.database()
    other.out_of_band(*db.ddl)
    assert other.model == db.model and set(other.modules) == set(db.modules)
    monkeypatch.chdir(tmp_path)  # baseline writes its report under the working directory

    early = onboard.deploy(built, other)
    assert (early.code, early.reason) == (22, "BASELINE_REQUIRED")

    recorded = onboard.tool(
        "baseline", *built.where, "--confirm-database", "sales", "--out", str(onboard.out("report")), db=other
    )

    assert recorded.code == 0, recorded.err
    assert other.ddl == [] and other.model == db.model
    checksums = managed(other)
    assert set(checksums) == {*other.model, *MODULES}
    # each module is the text of its file, so it is recorded with the checksum of the file
    assert all(checksums[key] is not None for key in MODULES)
    assert all(checksums[key] is None for key in other.model)
    assert [(step["kind"], step["status"]) for step in other.rows("step")] == [("baseline", "ok")]
    assert [(run["command"], run["status"]) for run in other.rows("run")] == [("baseline", "ok")]
    assert (tmp_path / "onboarding" / "dev" / "baseline-diff.md").is_file()

    # the first deploy of the release finds the database as the files say: nothing to send
    deploy = onboard.deploy(built, other)

    assert deploy.code == 0, deploy.err
    assert other.ddl == []
    assert onboard.tool("drift", *built.where, db=other).code == 0


# ------------------------------------------------------------------ 8. a partitioned table
BY_YEAR = Partition("PS_OrderYear", "OrderDate")
ORDER_LINE = Table(
    "sales",
    "OrderLine",
    (
        Column("OrderLineId", TypeRef("bigint"), False),
        Column("OrderDate", TypeRef("date"), False),
        Column("ProductId", TypeRef("int"), False),
        Column("Quantity", TypeRef("int"), False),
    ),
    (PrimaryKey("PK_OrderLine", True, (KeyColumn("OrderDate"), KeyColumn("OrderLineId")), (), BY_YEAR),),
    (Index("IX_OrderLine_Product", False, False, (KeyColumn("ProductId"),), on=BY_YEAR),),
    on=BY_YEAR,
)
ORDER_LINE_2 = dataclasses.replace(
    ORDER_LINE,
    indexes=(
        *ORDER_LINE.indexes,
        Index("IX_OrderLine_Quantity", False, False, (KeyColumn("Quantity"),), on=BY_YEAR),
    ),
)
ORDER_LINE_PATH = "schema/tables/sales.OrderLine.sql"
DROP_PRODUCT_INDEX = "DROP INDEX [IX_OrderLine_Product] ON [sales].[OrderLine];"
PRODUCT_INDEX = "CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId])"
STAGE = Table(
    "sales",
    "OrderLineStage",
    (Column("OrderLineId", TypeRef("bigint"), False), Column("OrderDate", TypeRef("date"), False)),
    on=BY_YEAR,
)


def order_lines(project: Project) -> Built:
    project.write_objects(SALES, ORDER_LINE)
    return project.release("order lines by year", "create_order_line")


def test_a_release_on_a_partition_scheme_that_the_database_does_not_have_is_refused_before_any_statement(
    project,
):
    first = order_lines(project)
    assert "ON [PS_OrderYear]([OrderDate])" in first.batches[1]  # gen wrote the place from the model
    db = project.database()
    before = (db.rows("run"), db.rows("step"), db.rows("object"))

    plan = project.tool("plan", *first.where, "--out", str(project.out("plan")), db=db)
    deploy = project.deploy(first, db)

    for refused in (plan, deploy):
        assert (refused.code, refused.reason) == (22, "PARTITION_SCHEME_MISSING")
        assert "[PS_OrderYear]" in refused.err
    assert db.ddl == [] and len(db.model) == 0
    assert (db.rows("run"), db.rows("step"), db.rows("object")) == before
    assert len(db.sent("azsqlcd:partition_schemes")) == 2  # one read for each plan

    db.partition_schemes.add("ps_orderyear")  # a DBA made the scheme; its name has no letter case
    assert project.deploy(first, db).code == 0


def test_a_partitioned_table_is_created_changed_read_back_watched_for_drift_and_exported(project, tmp_path):
    first = order_lines(project)
    db = project.database()
    db.partition_schemes.add("PS_OrderYear")

    done = project.deploy(first, db)

    assert done.code == 0, done.err  # the read-back passed: the catalog holds the places of the files
    assert db.ddl == first.batches and db.model == first.model
    line = db.model[ORDER_LINE.key]
    assert isinstance(line, Table) and line.on == BY_YEAR
    assert [item.on for _, item in line.placed_items()] == [BY_YEAR, BY_YEAR]
    capture = next(row for row in db.rows("object") if row["object_key"] == ORDER_LINE.key)["catalog_capture"]
    place = {"scheme": "PS_OrderYear", "column": "OrderDate"}
    assert json.loads(capture)["partition"] == place
    assert json.loads(capture)["index [IX_OrderLine_Product]"]["partition"] == place
    drift = project.tool("drift", *first.where, db=db)
    assert drift.code == 0 and drift.out.strip() == "no drift"

    # a second release adds an aligned nonclustered index
    project.write_objects(ORDER_LINE_2)
    second = project.release("order lines by quantity", "order_line_quantity")
    assert len(second.batches) == 1 and second.batches[0].rstrip().endswith("ON [PS_OrderYear]([OrderDate]);")
    sent = len(db.ddl)

    done = project.deploy(second, db)

    assert done.code == 0, done.err
    assert db.ddl[sent:] == second.batches and db.model == second.model
    assert project.tool("drift", *second.where, db=db).code == 0

    # somebody creates an aligned index again on [PRIMARY]: the file says the scheme
    db.out_of_band(DROP_PRODUCT_INDEX, PRODUCT_INDEX + " ON [PRIMARY];")

    drift = project.tool("drift", *second.where, db=db)

    assert (drift.code, drift.reason) == (30, "DRIFT_FOUND")
    lines = drift.out.splitlines()
    assert {line.split(" ")[1] for line in lines} == {ORDER_LINE.key}
    assert [line.split(" ", 2)[2].rsplit(" ", 2)[0] for line in lines] == ["index [IX_OrderLine_Product]"]

    # the export of that database says what the catalog holds, and its files parse to that model
    exported = tmp_path / "exported"
    done = project.tool("export", "--root", str(project.repo), *TARGET, "--out", str(exported), db=db)

    assert done.code == 0, done.err
    files = {
        path.relative_to(exported).as_posix(): path.read_text() for path in exported.glob("schema/*/*.sql")
    }
    assert tables.head_model({path: text.encode() for path, text in files.items()}) == db.model
    text = files[ORDER_LINE_PATH]
    assert text.count("ON [PS_OrderYear]([OrderDate])") == 3 and text.count("ON [PRIMARY]") == 1
    moved = db.model[ORDER_LINE.key]
    assert isinstance(moved, Table) and moved.index("IX_OrderLine_Product").on == ON_PRIMARY
    assert "it is partitioned" not in (exported / "onboarding" / "dev" / "export.md").read_text()

    # without an ON clause the engine puts the index on the scheme of the table: no drift again
    db.out_of_band(DROP_PRODUCT_INDEX, PRODUCT_INDEX + ";")
    assert db.model == second.model
    assert project.tool("drift", *second.where, db=db).code == 0


def test_a_compression_change_of_a_partitioned_heap_rebuilds_all_partitions(project):
    project.write_objects(SALES, STAGE)
    first = project.release("the stage of order lines", "create_stage")
    db = project.database()
    db.partition_schemes.add("PS_OrderYear")
    assert project.deploy(first, db).code == 0
    project.write_objects(dataclasses.replace(STAGE, compression="PAGE"))
    second = project.release("compress the stage", "compress_stage")
    sent = len(db.ddl)

    done = project.deploy(second, db)

    assert done.code == 0, done.err
    (rebuild,) = db.ddl[sent:]
    assert rebuild.rstrip().endswith(
        "ALTER TABLE [sales].[OrderLineStage] REBUILD PARTITION = ALL WITH (DATA_COMPRESSION = PAGE);"
    )
    assert db.model == second.model
    stage = db.model[STAGE.key]
    assert isinstance(stage, Table) and (stage.compression, stage.on) == ("PAGE", BY_YEAR)
    assert project.tool("drift", *second.where, db=db).code == 0


# ------------------------------------------------------------------ further loops
BOUND = (
    "schema/views/sales.vw_Refs.sql",
    "CREATE OR ALTER VIEW [sales].[vw_Refs] WITH SCHEMABINDING AS\n"
    "SELECT [OrderId], [Reference] FROM [sales].[Order];\n",
)
BOUND_KEY = names.object_key("VIEW", "sales", "vw_Refs")
ORDER_WIDE = dataclasses.replace(
    ORDER,
    columns=tuple(
        dataclasses.replace(column, type=TypeRef("nvarchar", length=80))
        if column.name == "Reference"
        else column
        for column in ORDER.columns
    ),
)
TRIGGER = (
    "schema/triggers/sales.tr_Order.sql",
    "CREATE OR ALTER TRIGGER [sales].[tr_Order] ON [sales].[Order] AFTER INSERT, UPDATE AS\n"
    "BEGIN\n    SET NOCOUNT ON;\nEND;\n",
)
TRIGGER_KEY = names.object_key("TRIGGER", "sales", "tr_Order")


def test_a_schema_bound_view_is_unbound_for_the_change_of_its_column_and_bound_again(project):
    first = release_1(project)
    project.write(*BOUND)
    bound = project.release("a schema-bound view")
    db = deployed(project, first, bound)
    assert db.module(BOUND_KEY).is_schema_bound
    project.write_objects(ORDER_WIDE)
    wide = project.release("a longer reference", "widen_reference")
    assert "-- azsqlcd:unbind [sales].[vw_Refs]" in wide.batches[0]  # gen wrote the directive
    sent = len(db.ddl)

    done = project.deploy(wide, db)

    assert done.code == 0, done.err
    unbind = "ALTER VIEW [sales].[vw_Refs] AS\nSELECT [OrderId], [Reference] FROM [sales].[Order];\n"
    # without the unbind the fake engine refuses ALTER COLUMN under a schema-bound view (error 5074)
    assert db.ddl[sent:] == [unbind, *wide.batches, BOUND[1], REFRESH_VIEW]
    assert db.model == wide.model and db.module(BOUND_KEY).is_schema_bound
    assert project.tool("drift", *wide.where, db=db).code == 0


def test_the_trigger_of_a_dropped_table_is_recorded_as_dropped_and_no_drop_is_sent_for_it(project):
    first = release_1(project)
    project.write(*TRIGGER)
    with_trigger = project.release("a trigger")
    db = deployed(project, first, with_trigger)
    stored = db.module(TRIGGER_KEY)
    assert stored is not None and (stored.parent, stored.events) == (("sales", "Order"), ("INSERT", "UPDATE"))
    for path in (names.path_for("TABLE", "sales", "Order"), TRIGGER[0], VIEW[0], FUNCTION[0], PROCEDURE[0]):
        (project.repo / path).unlink()
    gone = (TRIGGER_KEY, VIEW_KEY, FUNCTION_KEY, PROCEDURE_KEY)
    tombstones = "".join(f'[[drop]]\nobject = "{key}"\nreason = "orders moved away"\n\n' for key in gone)
    project.write("schema/_tombstones.toml", tombstones)
    dropped = project.release("drop the orders", "drop_order")
    sent = len(db.ddl)

    done = project.deploy(dropped, db)

    assert done.code == 0, done.err
    # the engine dropped the trigger with its table, so the tool sends no DROP TRIGGER (A18)
    assert db.ddl[sent:] == [
        *dropped.batches,
        "DROP PROCEDURE [sales].[usp_CountOrders];",
        "DROP VIEW [sales].[vw_OpenOrders];",
        "DROP FUNCTION [sales].[fn_OpenOrders];",
    ]
    assert db.modules == {} and db.model == dropped.model
    statuses = {row["object_key"]: row["status"] for row in db.rows("object")}
    assert all(statuses[key] == "dropped" for key in (*gone, ORDER.key))
    assert sorted(project.report()["modules_dropped"]) == sorted(gone)
    assert project.tool("drift", *dropped.where, db=db).code == 0


def test_a_run_that_another_session_holds_the_lock_against_sends_nothing(project):
    first = release_1(project)
    db = project.database()
    other = db.hold_lock()

    deploy = project.deploy(first, db)
    plan = project.tool("plan", *first.where, "--out", str(project.out("plan")), db=db)

    assert (deploy.code, deploy.reason) == (25, "LOCK_NOT_GRANTED")
    assert (plan.code, plan.reason) == (25, "RUN_LIVE")
    assert db.ddl == [] and db.rows("run") == []
    other.close()
    assert project.deploy(first, db).code == 0


def test_a_database_that_is_behind_is_caught_up_release_by_release_and_never_goes_back(project):
    first, second, third = release_1(project), release_2(project), release_3(project)
    db = deployed(project, first)

    ahead = project.deploy(third, db)

    assert (ahead.code, ahead.reason) == (22, "CATCHUP_REQUIRED") and "promote r3 first" in ahead.err
    assert project.deploy(second, db).code == 0 and project.deploy(third, db).code == 0
    sent = len(db.ddl)
    older = project.deploy(first, db)  # an older release on a newer database: no older text is sent
    assert older.code == 0 and "the database is past release r2" in older.out
    assert db.ddl[sent:] == [] and db.model == third.model
    assert db.module(PROCEDURE_KEY).sent == PROCEDURE_3[1]


# ------------------------------------------------------------------ 9. [scope]: one schema at a time
SCOPE = '\n[scope]\nschemas = ["sales", "dbo"]\nexclude = ["scratch"]\nexclude_objects = ["dbo.temp_*"]\n'
CURRENCY = Table(
    "dbo",
    "Currency",
    (Column("Code", TypeRef("char", length=3), False),),
    (PrimaryKey("PK_Currency", True, (KeyColumn("Code"),)),),
)
TEMP_LOAD = Table("dbo", "temp_Load", (Column("Id", TypeRef("int"), False),))
SCRATCH_WORK = Table("scratch", "Work", (Column("Id", TypeRef("int"), False),))
BILLING = Schema("billing")
INVOICE = Table(
    "billing",
    "Invoice",
    (Column("InvoiceId", TypeRef("int"), False), Column("CustomerId", TypeRef("int"), False)),
    (
        PrimaryKey("PK_Invoice", True, (KeyColumn("InvoiceId"),)),
        # a key to a table of a schema that is managed already: the schema export keeps the table
        ForeignKey("FK_Invoice_Customer", ("CustomerId",), "sales", "Customer", ("CustomerId",)),
    ),
)
DUE = (
    "schema/views/billing.vw_Due.sql",
    "CREATE OR ALTER VIEW [billing].[vw_Due] AS\nSELECT [InvoiceId] FROM [billing].[Invoice];\n",
)
DUE_KEY = names.object_key("VIEW", "billing", "vw_Due")
OUTSIDE = {TEMP_LOAD.key, SCRATCH_WORK.key, "SCHEMA:[scratch]", "TABLE:[dbo].[ext_Rates]"}


def estate(project: Project) -> FakeDatabase:
    """A database that the tool never saw: the sales schema of release 1 and a dbo table, a dbo
    table that [scope] excludes by pattern, an external table, an excluded schema, and the schema
    billing, which no list of [scope] names yet."""
    db = project.database()
    objects = (SALES, CUSTOMER, ORDER, CURRENCY, TEMP_LOAD, Schema("scratch"), SCRATCH_WORK, BILLING, INVOICE)
    db.out_of_band(*emit.emit_create_script(Model(objects)))
    db.out_of_band(FUNCTION[1], VIEW[1], PROCEDURE[1], DUE[1])
    db.external_tables.add(("dbo", "ext_Rates"))
    return db


def object_files(folder: Path) -> dict[str, str]:
    return {path.relative_to(folder).as_posix(): path.read_text() for path in folder.glob("schema/*/*.sql")}


def test_a_database_is_onboarded_schema_by_schema_and_what_the_scope_leaves_out_is_never_seen(
    tmp_path, capsys, monkeypatch
):
    project = Project(tmp_path, capsys, table_model=False)
    db = estate(project)
    project.write("azsqlcd.toml", TOML.format(table_model="false") + SCOPE)
    project.main = project.commit("the scope of the onboarding")
    monkeypatch.chdir(tmp_path)  # baseline writes its report under the working directory

    # export: files for the managed objects only. The excluded schema, the pattern and the external
    # table are not read, not written and not listed as unmanaged
    exported = tmp_path / "exported"
    done = project.tool("export", "--root", str(project.repo), *TARGET, "--out", str(exported), db=db)
    assert done.code == 0, done.err
    first_files = object_files(exported)
    paths = {
        names.path_for(o.kind, getattr(o, "schema", None), o.name) for o in (SALES, CUSTOMER, ORDER, CURRENCY)
    }
    assert set(first_files) == paths | {VIEW[0], FUNCTION[0], PROCEDURE[0]}
    report = (exported / "onboarding" / "dev" / "export.md").read_text()
    assert not any(key in report for key in OUTSIDE | {INVOICE.key, DUE_KEY})
    assert "External tables in the schemas that were read: 1. They are skipped by their type" in report
    assert "0 object(s) stay unmanaged" in done.out

    shutil.copytree(exported, project.repo, dirs_exist_ok=True)
    project.write("azsqlcd.toml", TOML.format(table_model="true") + SCOPE)  # the switch, with the files
    project.write("migrations/migrations.sum", "azsqlcd-sum 1\nbaseline\n")
    first = project.release("onboard sales and dbo")
    recorded = project.tool(
        "baseline", *first.where, "--confirm-database", "sales", "--out", str(project.out("r")), db=db
    )
    assert recorded.code == 0, recorded.err
    rows = {row["object_key"]: row for row in db.rows("object")}
    assert set(rows) == {*(tables.head_model(first.bundle.files)), *MODULES}

    # plan: no object outside the scope in the unmanaged list, one note for the schema that no list names
    out = project.out("plan")
    plan = project.tool("plan", *first.where, "--out", str(out), db=db)
    assert plan.code == 0, plan.err
    planned = json.loads((out / "plan.json").read_text())
    assert planned["unmanaged"] == []  # billing is in no list: not managed, and not seen either
    unscoped = [note for note in planned["notes"] if note.startswith("SCHEMA_UNSCOPED")]
    assert len(unscoped) == 1 and "[billing]" in unscoped[0]
    assert f"note: {unscoped[0]}" in plan.out
    assert project.deploy(first, db).code == 0 and db.ddl == []  # the files are the database: nothing to send
    drift = project.tool("drift", *first.where, db=db)
    assert drift.code == 0, drift.err  # an unlisted schema is reported, never drift
    assert drift.out.splitlines() == ["unscoped SCHEMA:[billing] SCHEMA_UNSCOPED - -", "no drift"]

    # the second schema alone: its files, the snapshot with it, and a line of what to add to [scope]
    again = tmp_path / "billing"
    done = project.tool(
        "export", "--root", str(project.repo), *TARGET, "--out", str(again), "--schema", "billing", db=db
    )
    assert done.code == 0, done.err
    billing_paths = {"schema/schemas/billing.sql", "schema/tables/billing.Invoice.sql", DUE[0]}
    assert set(object_files(again)) == billing_paths
    assert 'Add "billing" to [scope] schemas of azsqlcd.toml' in done.out
    snapshot = json.loads((again / "onboarding" / "dev" / "snapshot.json").read_text())
    before = json.loads((project.repo / "onboarding" / "dev" / "snapshot.json").read_text())
    assert snapshot["captures"] == before["captures"] | {
        key: snapshot["captures"][key] for key in (BILLING.key, INVOICE.key)
    }
    shutil.copytree(again, project.repo, dirs_exist_ok=True)
    assert {
        path: text for path, text in object_files(project.repo).items() if path not in billing_paths
    } == first_files
    project.write(
        "azsqlcd.toml", TOML.format(table_model="true") + SCOPE.replace('"dbo"]', '"dbo", "billing"]')
    )
    second = project.release("onboard billing")
    assert "azsqlcd baseline --schema billing" in second.verified  # PRF000: taken as they are

    early = project.tool("plan", *second.where, "--out", str(project.out("plan")), db=db)
    assert (early.code, early.reason) == (22, "BASELINE_REQUIRED") and "--schema billing" in early.err
    others = db.rows("object")
    recorded = project.tool(
        "baseline",
        *second.where,
        "--schema",
        "billing",
        "--confirm-database",
        "sales",
        "--out",
        str(project.out("r")),
        db=db,
    )
    assert recorded.code == 0, recorded.err
    assert db.rows("object")[: len(others)] == others  # the rows of sales and dbo did not change
    added = {row["object_key"] for row in db.rows("object")[len(others) :]}
    assert added == {BILLING.key, INVOICE.key, DUE_KEY}
    assert db.rows("step")[-1]["note"].startswith("schema [billing]: ")
    again = project.tool(
        "baseline", *second.where, "--schema", "billing", "--confirm-database", "sales", db=db
    )
    assert (again.code, again.reason) == (22, "ALREADY_BASELINED")

    out = project.out("plan")
    assert project.tool("plan", *second.where, "--out", str(out), db=db).code == 0
    planned = json.loads((out / "plan.json").read_text())
    assert planned["unmanaged"] == [] and not any("SCHEMA_UNSCOPED" in n for n in planned["notes"])
    sent = len(db.ddl)
    assert project.deploy(second, db).code == 0 and db.ddl[sent:] == []
    drift = project.tool("drift", *second.where, db=db)
    assert (drift.code, drift.out.strip()) == (0, "no drift")


def test_a_schema_that_no_list_names_is_a_note_of_the_plan_in_the_summary_of_github_too(
    tmp_path, capsys, monkeypatch
):
    project = Project(tmp_path, capsys)
    db = estate(project)
    project.write("azsqlcd.toml", TOML.format(table_model="true") + SCOPE)
    project.write(
        "schema/views/sales.vw_New.sql", "CREATE OR ALTER VIEW [sales].[vw_New] AS\nSELECT 1 AS [One];\n"
    )
    built = project.release("a new view")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))

    plan = project.tool("plan", *built.where, "--out", str(project.out("plan")), "--ci", "github", db=db)

    assert plan.code == 0, plan.err
    summary = (tmp_path / "summary").read_text()
    assert "- SCHEMA_UNSCOPED \\[billing\\]: the schema is in neither \\[scope\\] schemas" in summary
    assert "external table(s) in the managed schemas: skipped by their type" in plan.out
    assert "1 external table(s)" in plan.out


def test_export_and_baseline_of_one_schema_refuse_a_schema_that_the_scope_does_not_allow(
    tmp_path, capsys, monkeypatch
):
    project = Project(tmp_path, capsys, table_model=False)
    db = estate(project)
    project.write("azsqlcd.toml", TOML.format(table_model="false") + SCOPE)
    project.main = project.commit("scope")
    root = ["--root", str(project.repo), *TARGET, "--out", str(tmp_path / "x")]
    for schema in ("scratch", "SCRATCH", "azsqlcd"):
        refused = project.tool("export", *root, "--schema", schema, db=db)
        assert (refused.code, refused.reason) == (22, "OUT_OF_SCOPE"), refused.err
    assert "in [scope] exclude of azsqlcd.toml" in project.tool("export", *root, "--schema", "scratch").err
    project.git("update-ref", "refs/remotes/origin/main", project.main)
    built = project.build()
    monkeypatch.chdir(tmp_path)
    for schema in ("billing", "scratch", "azsqlcd"):
        refused = project.tool(
            "baseline", *built.where, "--schema", schema, "--confirm-database", "sales", db=db
        )
        assert (refused.code, refused.reason) == (22, "OUT_OF_SCOPE"), refused.err
    assert db.rows("run") == [] and db.rows("object") == []


def test_a_schema_that_leaves_the_scope_is_no_longer_planned_compared_listed_or_dropped(project):
    ping = (
        "schema/procedures/dbo.usp_Ping.sql",
        "CREATE OR ALTER PROCEDURE [dbo].[usp_Ping] AS\nSELECT 1 AS [One];\n",
    )
    project.write("azsqlcd.toml", TOML.format(table_model="true") + '\n[scope]\nschemas = ["sales", "dbo"]\n')
    project.write(*ping)
    db = deployed(project, release_1(project))
    assert db.module("PROCEDURE:[dbo].[usp_Ping]") is not None
    (project.repo / ping[0]).unlink()  # no tombstone: the tool stops managing dbo and drops nothing
    project.write(
        "azsqlcd.toml",
        TOML.format(table_model="true") + '\n[scope]\nschemas = ["sales"]\nexclude = ["dbo"]\n',
    )
    left = project.release("dbo is not managed any more")
    sent = len(db.ddl)

    done = project.deploy(left, db)

    assert done.code == 0, done.err  # no MODULE_ORPHAN for the managed row of dbo
    assert db.ddl[sent:] == [] and db.module("PROCEDURE:[dbo].[usp_Ping]") is not None
    drift = project.tool("drift", *left.where, db=db)
    assert (drift.code, drift.out.strip()) == (0, "no drift")  # not unmanaged either: it is not seen
    db.out_of_band("CREATE OR ALTER PROCEDURE [dbo].[usp_Ping] AS\nSELECT 2 AS [Two];\n")
    assert project.tool("drift", *left.where, db=db).code == 0  # its recorded row is not compared


RATES = Table(
    "dbo",
    "Rates",
    (Column("Code", TypeRef("char", length=3), False),),
    (PrimaryKey("PK_Rates", True, (KeyColumn("Code"),)),),
)
RATES_KEY = RATES.key


def scoped_toml(table: str) -> str:
    return TOML.format(table_model="true") + f"\n[scope]\n{table}\n"


def test_a_withdrawal_after_a_schema_left_the_scope_proves_and_generates_nothing_for_that_schema(project):
    """Review C1: the model from the history still held dbo.Rates after dbo left the scope; verify
    asked for its file (WDR004) and gen wrote DROP TABLE [dbo].[Rates], which the deploy ran."""
    project.write_objects(SALES, CUSTOMER, ORDER, RATES)
    for path, text in MODULES.values():
        project.write(path, text)
    db = deployed(project, project.release("create", "create_all"))
    project.write_objects(ORDER_2)
    assert project.deploy(project.release("order note", "order_note"), db).code == 0
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales"]\nexclude = ["dbo"]'))
    (project.repo / names.path_for("TABLE", "dbo", "Rates")).unlink()
    assert project.deploy(project.release("dbo leaves the scope"), db).code == 0

    # a pull request withdraws order_note with no replacement: the table file goes back
    project.write_objects(ORDER)
    lines = (project.repo / chain.SUM_PATH).read_text().splitlines()
    lines = [line + " withdrawn" if "order_note" in line else line for line in lines]
    project.write(chain.SUM_PATH, "\n".join(lines) + "\n")
    verified = project.tool("verify", "--root", str(project.repo), "--base", project.main)
    assert verified.code == 0, verified.out  # was: WDR004, asking for a file that is OUT_OF_SCOPE

    # the same branch changes a managed table: gen writes that change only
    email = dataclasses.replace(
        CUSTOMER, columns=(*CUSTOMER.columns, Column("Email", TypeRef("nvarchar", length=200), True))
    )
    project.write_objects(email)
    built = project.release("withdraw order_note, add the email", "customer_email")
    assert built.migration is not None and "[dbo]" not in "".join(built.batches)
    assert len(built.batches) == 1
    assert built.batches[0].endswith("\nALTER TABLE [sales].[Customer] ADD [Email] nvarchar(200) NULL;")
    assert project.deploy(built, db).code == 0
    assert db.table(("dbo", "rates")) is not None  # the excluded table is still there


def test_a_table_that_a_managed_key_references_cannot_leave_the_scope(project):
    """Review minor 9: the pull request that excludes dbo fails verify while sales.Order has a
    foreign key to dbo.Rates (setup.md, section 7); nothing is dropped or lost."""
    order = dataclasses.replace(
        ORDER,
        columns=(*ORDER.columns, Column("RateCode", TypeRef("char", length=3), True)),
        constraints=(
            *ORDER.constraints,
            ForeignKey("FK_Order_Rates", ("RateCode",), "dbo", "Rates", ("Code",)),
        ),
    )
    project.write_objects(SALES, CUSTOMER, order, RATES)
    project.release("create", "create_all")
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales"]\nexclude = ["dbo"]'))
    (project.repo / names.path_for("TABLE", "dbo", "Rates")).unlink()

    verified = project.tool("verify", "--root", str(project.repo), "--base", project.main)

    assert verified.code == 22 and "MDL001" in verified.out and "[dbo].[Rates]" in verified.out


RATES_WIDE = dataclasses.replace(
    RATES, columns=(*RATES.columns, Column("Name", TypeRef("nvarchar", length=50), True))
)
RATES_VIEW = (
    "schema/views/dbo.vw_Rates.sql",
    "CREATE OR ALTER VIEW [dbo].[vw_Rates] AS\nSELECT [Code] FROM [dbo].[Rates];\n",
)
RATES_VIEW_KEY = names.object_key("VIEW", "dbo", "vw_Rates")


def status_of(db: FakeDatabase, key: str) -> str | None:
    return next((row["status"] for row in db.rows("object") if row["object_key"] == key), None)


def test_an_object_that_enters_the_scope_in_a_recorded_schema_is_recorded_by_the_baseline_of_its_schema(
    tmp_path, capsys, monkeypatch
):
    """Review M1: plan and deploy asked for a baseline, and both baselines were ALREADY_BASELINED."""
    project = Project(tmp_path, capsys, table_model=False)
    db = project.database()
    db.out_of_band(*emit.emit_create_script(Model((SALES, CUSTOMER, ORDER, RATES, TEMP_LOAD))))
    db.out_of_band(*(text for _, text in MODULES.values()))
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]\nexclude_objects = ["dbo.temp_*"]'))
    project.write_objects(SALES, CUSTOMER, ORDER, RATES)
    for path, text in MODULES.values():
        project.write(path, text)
    project.write("migrations/migrations.sum", "azsqlcd-sum 1\nbaseline\n")
    monkeypatch.chdir(tmp_path)
    first = project.release("onboard sales and dbo")
    confirm = ["--confirm-database", "sales"]
    assert project.tool("baseline", *first.where, *confirm, db=db).code == 0

    # the pattern goes, and dbo.temp_Load comes in as the database has it
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]'))
    project.write_objects(TEMP_LOAD)
    second = project.release("dbo.temp_Load is managed now")
    assert "azsqlcd baseline --schema dbo" in second.verified  # PRF000
    early = project.tool("plan", *second.where, "--out", str(project.out("plan")), db=db)
    assert (early.code, early.reason) == (22, "BASELINE_REQUIRED")
    assert "azsqlcd baseline --schema dbo" in early.err
    rows = db.rows("object")

    done = project.tool("baseline", *second.where, "--schema", "dbo", *confirm, db=db)

    assert done.code == 0, done.err
    assert db.rows("object")[: len(rows)] == rows and status_of(db, TEMP_LOAD.key) == "managed"
    assert len(db.rows("object")) == len(rows) + 1
    again = project.tool("baseline", *second.where, "--schema", "dbo", *confirm, db=db)
    assert (again.code, again.reason) == (22, "ALREADY_BASELINED") and "nothing to record" in again.err
    sent = len(db.ddl)
    assert project.deploy(second, db).code == 0 and db.ddl[sent:] == []


def test_a_schema_that_leaves_the_scope_and_comes_back_is_compared_with_its_files_again(
    tmp_path, capsys, monkeypatch
):
    """Review M2: the old rows of dbo counted as managed after dbo came back, so its changed file was
    never compared until a read-back failed one release later."""
    project = Project(tmp_path, capsys)
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]'))
    project.write_objects(SALES, CUSTOMER, ORDER, RATES)
    project.write(*RATES_VIEW)
    db = deployed(project, project.release("create", "create_all"))
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales"]\nexclude = ["dbo"]'))
    for path in (names.path_for("TABLE", "dbo", "Rates"), RATES_VIEW[0]):
        (project.repo / path).unlink()
    left = project.release("dbo leaves the scope")
    out = project.out("plan")
    assert project.tool("plan", *left.where, "--out", str(out), db=db).code == 0
    steps = [
        step["id"] for unit in json.loads((out / "plan.json").read_text())["units"] for step in unit["steps"]
    ]
    assert steps == [f"unscope:{RATES_KEY}", f"unscope:{RATES_VIEW_KEY}"]
    sent = len(db.ddl)

    assert project.deploy(left, db).code == 0

    # the rows say: no longer managed. The objects stay, and nothing was sent
    assert db.ddl[sent:] == [] and db.table(("dbo", "Rates")) is not None and db.module(RATES_VIEW_KEY)
    assert status_of(db, RATES_KEY) == status_of(db, RATES_VIEW_KEY) == "dropped"
    updates = len(db.sent("UPDATE [azsqlcd].[object]"))
    assert project.deploy(left, db).code == 0  # marked once: the next deploy has nothing to do
    assert len(db.sent("UPDATE [azsqlcd].[object]")) == updates

    # dbo comes back, and its table file is not what the database has
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]'))
    project.write_objects(RATES_WIDE)
    project.write(*RATES_VIEW)
    back = project.release("dbo comes back")
    assert "azsqlcd baseline --schema dbo" in back.verified  # PRF000
    for command in (
        ["plan", *back.where, "--out", str(project.out("plan"))],
        ["deploy", *back.where, "--inline-plan", "--out", str(project.out("r"))],
    ):
        refused = project.tool(*command, db=db)
        assert (refused.code, refused.reason) == (22, "BASELINE_REQUIRED"), refused.err
        assert "azsqlcd baseline --schema dbo" in refused.err
    monkeypatch.chdir(tmp_path)
    baseline = ["baseline", *back.where, "--schema", "dbo", "--confirm-database", "sales"]
    differs = project.tool(*baseline, db=db)
    assert (differs.code, differs.reason) == (22, "READBACK_MISMATCH"), differs.err
    assert status_of(db, RATES_KEY) == "dropped"  # refused: nothing was recorded

    # a DBA aligns the database with the file; the baseline of dbo records both objects again
    db.out_of_band("ALTER TABLE [dbo].[Rates] ADD [Name] nvarchar(50) NULL;")
    assert project.tool(*baseline, db=db).code == 0
    assert status_of(db, RATES_KEY) == status_of(db, RATES_VIEW_KEY) == "managed"
    sent = len(db.ddl)
    assert project.deploy(back, db).code == 0 and db.ddl[sent:] == []
    assert project.tool("drift", *back.where, db=db).out.strip() == "no drift"


def test_a_target_that_skipped_the_release_in_which_a_schema_left_is_refused_before_any_statement(project):
    """Review V2: the target kept the managed rows of dbo, so the release in which dbo came back with
    a changed table file was planned and deployed with no statement, and drift said "no drift"."""
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]'))
    project.write_objects(SALES, CUSTOMER, ORDER, RATES)
    db = deployed(project, project.release("create", "create_all"))
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales"]\nexclude = ["dbo"]'))
    (project.repo / names.path_for("TABLE", "dbo", "Rates")).unlink()
    left = project.release("dbo leaves the scope")  # no migration: a target can skip it
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]'))
    project.write_objects(RATES_WIDE)
    back = project.release("dbo comes back with a changed table file")
    rows, sent = db.rows("object"), len(db.ddl)

    for command in (
        ["plan", *back.where, "--out", str(project.out("plan"))],
        ["deploy", *back.where, "--inline-plan", "--out", str(project.out("r"))],
    ):
        refused = project.tool(*command, db=db)
        assert (refused.code, refused.reason) == (22, "STALE_MANAGED_ROW"), refused.err
        assert RATES_KEY in refused.err and "azsqlcd baseline --schema dbo" in refused.err
    assert db.ddl[sent:] == [] and db.rows("object") == rows

    # the way out that the refusal names: the release in which dbo left, then the baseline of dbo
    assert project.deploy(left, db).code == 0 and status_of(db, RATES_KEY) == "dropped"
    again = project.tool("plan", *back.where, "--out", str(project.out("plan")), db=db)
    assert (again.code, again.reason) == (22, "BASELINE_REQUIRED")


def test_a_replacement_after_a_schema_left_the_scope_proves_and_sends_nothing_for_that_schema(project):
    """Review V3: the proof of a replacement compared dbo.Rates, which left the scope, so verify
    asked for the dbo statement, and the deploy of that statement failed on every target that had
    not run the withdrawn migration (READBACK_MISMATCH)."""
    project.write_objects(SALES, CUSTOMER, ORDER, RATES)
    first = project.release("create", "create_all")
    dev, prod = deployed(project, first), deployed(project, first)
    project.write_objects(ORDER_2, RATES_WIDE)
    second = project.release("order note", "order_note")
    assert "ALTER TABLE [dbo].[Rates] ADD [Name] nvarchar(50) NULL;" in "".join(second.batches)
    assert project.deploy(second, dev).code == 0  # prod never runs it
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales"]\nexclude = ["dbo"]'))
    (project.repo / names.path_for("TABLE", "dbo", "Rates")).unlink()
    assert project.deploy(project.release("dbo leaves the scope"), dev).code == 0

    # the pull request withdraws 0002 and replaces it with its statements on sales only
    old = (project.repo / "migrations/0002__order_note.sql").read_text().split("\n", 2)[2]
    dbo = "ALTER TABLE [dbo].[Rates] ADD [Name] nvarchar(50) NULL;\nGO\n"
    edge = "-- azsqlcd:allow REPLACEMENT_EDGE 0002__order_note.sql reason: same statements\n"
    head = "-- azsqlcd:migration 0003__order_note_v2\n-- azsqlcd:mode tx\n" + edge
    lines = (project.repo / chain.SUM_PATH).read_text().splitlines()
    lines = [line + " withdrawn" if "order_note" in line else line for line in lines]
    lines.append(f"0003__order_note_v2.sql sha256:{'0' * 64} tx replaces=0002__order_note.sql")
    project.write(chain.SUM_PATH, "\n".join(lines) + "\n")
    root = ["--root", str(project.repo)]

    # with the statement on dbo: refused, as a new migration on an object outside the scope is
    project.write("migrations/0003__order_note_v2.sql", head + old)
    assert project.tool("gen", "--base", project.main, "--resum", *root).code == 0
    verified = project.tool("verify", *root, "--base", project.main)
    assert verified.code == 22 and "WDR002" in verified.out and "TABLE:[dbo].[Rates]" in verified.out
    assert "[scope]" in verified.out

    project.write("migrations/0003__order_note_v2.sql", head + old.replace(dbo, ""))
    assert project.tool("gen", "--base", project.main, "--resum", *root).code == 0
    replaced = project.release("replace order_note on sales only")  # verify: 0, was WDR002
    sent = len(prod.ddl)

    done = project.deploy(replaced, prod)

    assert done.code == 0, done.err
    assert prod.ddl[sent:] and "[dbo]" not in "".join(prod.ddl[sent:])
    assert [column.name for column in prod.table(("dbo", "Rates")).columns] == ["Code"]
    assert project.deploy(replaced, dev).code == 0


PING = (
    "schema/procedures/dbo.usp_Ping.sql",
    "CREATE OR ALTER PROCEDURE [dbo].[usp_Ping] AS\nSELECT 1 AS [One];\n",
)
PING_KEY = names.object_key("PROCEDURE", "dbo", "usp_Ping")


def test_a_tombstone_outside_the_scope_is_refused_and_one_of_an_object_with_no_managed_row_is_a_note(project):
    """Review V4: both tombstones were accepted, and the deploy left the procedure in place with no
    message, so the repository said that it was dropped."""
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]'))
    project.write(*PING)
    db = deployed(project, release_1(project))
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales"]\nexclude = ["dbo"]'))
    (project.repo / PING[0]).unlink()
    tombstone = f'[[drop]]\nobject = "{PING_KEY}"\nreason = "not needed"\n'
    project.write("schema/_tombstones.toml", tombstone)
    lint = project.tool("lint", "--root", str(project.repo))
    assert lint.code == 22
    assert f"schema/_tombstones.toml:2: error OUT_OF_SCOPE: {PING_KEY} is outside [scope]" in lint.out
    (project.repo / "schema/_tombstones.toml").unlink()
    assert project.deploy(project.release("dbo leaves"), db).code == 0
    assert status_of(db, PING_KEY) == "dropped" and db.module(PING_KEY) is not None

    # dbo comes back with a tombstone for the procedure, which has no managed row
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]'))
    project.write("schema/_tombstones.toml", tombstone)
    back = project.release("dbo comes back, usp_Ping is not needed")
    out = project.out("plan")
    planned = project.tool("plan", *back.where, "--out", str(out), db=db)
    assert planned.code == 0, planned.err
    note = (
        f"TOMBSTONE_UNMANAGED {PING_KEY}: the release has a tombstone for it, the database has it, and "
        "this database records no managed row for it"
    )
    assert note in planned.out and note in "".join(json.loads((out / "plan.json").read_text())["notes"])
    sent = len(db.ddl)
    assert project.deploy(back, db).code == 0
    assert db.ddl[sent:] == [] and db.module(PING_KEY) is not None  # the tool drops no unmanaged object


REPORTING = ("schema/schemas/reporting.sql", "CREATE SCHEMA [reporting];\n")
REPORT_REFS = (
    "schema/views/reporting.vw_Refs.sql",
    "CREATE OR ALTER VIEW [reporting].[vw_Refs] AS\nSELECT [OrderId], [Reference] FROM [sales].[Order];\n",
)
REPORT_BOUND = (
    "schema/views/reporting.vw_Bound.sql",
    "CREATE OR ALTER VIEW [reporting].[vw_Bound] WITH SCHEMABINDING AS\n"
    "SELECT [OrderId], [Reference] FROM [sales].[Order];\n",
)


def reporting_leaves(project: Project, *views: tuple[str, str], ack: str = "") -> tuple[FakeDatabase, Built]:
    """Release 1 with the schema reporting and its views; then one release that takes reporting out
    of the scope and widens [sales].[Order].[Reference], which the views use."""
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "reporting"]'))
    for file in (REPORTING, *views):
        project.write(*file)
    db = deployed(project, release_1(project))
    project.write("azsqlcd.toml", scoped_toml(f'schemas = ["sales"]\nexclude = ["reporting"]\n{ack}'))
    for path, _ in (REPORTING, *views):
        (project.repo / path).unlink()
    project.write_objects(ORDER_WIDE)
    return db, project.release("reporting leaves; a longer reference", "widen_reference")


def test_a_schema_bound_module_that_leaves_the_scope_blocks_as_an_unmanaged_one_and_gets_no_unbind(project):
    """Review minor 1: it was taken as managed. TABLE_BLOCKER asked for an unbind line, and the plan
    then refused that line (DIRECTIVE_UNRESOLVED)."""
    db, widen = reporting_leaves(project, REPORT_BOUND)

    assert "unbind" not in "".join(widen.batches)  # gen unbinds no module that leaves the scope
    plan = project.tool("plan", *widen.where, "--out", str(project.out("plan")), db=db)

    assert (plan.code, plan.reason) == (22, "TABLE_BLOCKER")
    blocker = "VIEW:[reporting].[vw_Bound] is schema-bound to [sales].[Order].[Reference] and is not managed"
    assert blocker in plan.err


def test_a_module_that_leaves_the_scope_is_a_dependant_as_an_unmanaged_one_and_is_never_refreshed(
    tmp_path, capsys
):
    """Review minor 2: it was refreshed by the deploy, which a broken view could fail."""
    project = Project(tmp_path / "a", capsys)
    db, widen = reporting_leaves(project, REPORT_REFS)
    plan = project.tool("plan", *widen.where, "--out", str(project.out("plan")), db=db)
    assert (plan.code, plan.reason) == (22, "TABLE_BLOCKER")
    assert "VIEW:[reporting].[vw_Refs] uses [sales].[Order] and is not managed" in plan.err

    # [ack] unmanaged_dependants takes it, as for an unmanaged view; the deploy does not refresh it
    project = Project(tmp_path / "b", capsys)
    ack = '[ack]\nunmanaged_dependants = ["[reporting].[vw_Refs] -> [sales].[Order].[Reference]"]'
    db, widen = reporting_leaves(project, REPORT_REFS, ack=ack)
    sent = len(db.ddl)
    assert project.deploy(widen, db).code == 0
    assert not any("reporting" in batch for batch in db.ddl[sent:])
    assert status_of(db, "VIEW:[reporting].[vw_Refs]") == "dropped"  # left the scope: marked


def test_a_module_outside_the_scope_cannot_be_adopted(project):
    """Review minor 3: resolve --adopt-module made a managed row that nothing would ever compare."""
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales"]\nexclude = ["scratch"]'))
    first = release_1(project)
    db = deployed(project, first)
    db.out_of_band(
        "CREATE SCHEMA [scratch];", "CREATE OR ALTER PROCEDURE [scratch].[usp_Tmp] AS\nSELECT 1 AS [One];\n"
    )
    rows = db.rows("object")

    adopt = project.tool(
        "resolve", *first.where, *RESOLVE, "--adopt-module", "PROCEDURE:[scratch].[usp_Tmp]", db=db
    )

    assert (adopt.code, adopt.reason) == (22, "OUT_OF_SCOPE") and "[scope] exclude" in adopt.err
    assert db.rows("object") == rows


def test_the_export_of_a_schema_that_the_database_does_not_have_is_refused(tmp_path, capsys):
    """Review minor 4: a typo exported nothing and said to add the schema to [scope]."""
    project = Project(tmp_path, capsys, table_model=False)
    db = estate(project)
    project.write("azsqlcd.toml", TOML.format(table_model="false") + SCOPE)
    out = tmp_path / "x"
    root = ["--root", str(project.repo), *TARGET, "--out", str(out)]

    done = project.tool("export", *root, "--schema", "biling", db=db)

    assert (done.code, done.reason) == (22, "SCHEMA_NOT_FOUND") and "[biling]" in done.err
    assert not out.exists() or not any(out.rglob("*.md"))
    assert project.tool("export", *root, "--schema", "BILLING", db=db).code == 0  # names without case


def test_a_trigger_follows_its_table_out_of_the_scope(tmp_path, capsys):
    """Review minor 7: the trigger of a table that a pattern excludes was exported and listed."""
    project = Project(tmp_path, capsys, table_model=False)
    db = project.database()
    db.out_of_band(*emit.emit_create_script(Model((TEMP_LOAD, CURRENCY))))
    trigger = (
        "CREATE OR ALTER TRIGGER [dbo].[tr_Load] ON [dbo].[temp_Load] AFTER INSERT AS\n"
        "BEGIN\n    SET NOCOUNT ON;\nEND;\n"
    )
    db.out_of_band(trigger)
    project.write(
        "azsqlcd.toml",
        TOML.format(table_model="false") + '\n[scope]\nschemas = ["dbo"]\nexclude_objects = ["dbo.temp_*"]\n',
    )
    project.main = project.commit("scope")
    out = tmp_path / "x"
    done = project.tool("export", "--root", str(project.repo), *TARGET, "--out", str(out), db=db)
    assert done.code == 0, done.err
    assert not (out / "schema" / "triggers").exists()
    assert "tr_Load" not in (out / "onboarding" / "dev" / "export.md").read_text()

    # a file for it is outside the scope too, and plan and drift do not list it as unmanaged
    project.write("schema/triggers/dbo.tr_Load.sql", trigger)
    lint = project.tool("lint", "--root", str(project.repo))
    assert lint.code == 22 and "schema/triggers/dbo.tr_Load.sql:1: error OUT_OF_SCOPE" in lint.out
    (project.repo / "schema/triggers/dbo.tr_Load.sql").unlink()
    project.git("update-ref", "refs/remotes/origin/main", project.main)
    built = project.build()
    out = project.out("plan")
    assert project.tool("plan", *built.where, "--out", str(out), db=db).code == 0
    assert json.loads((out / "plan.json").read_text())["unmanaged"] == []
    drift = project.tool("drift", *built.where, db=db)
    assert drift.code == 0 and "tr_Load" not in drift.out


def test_a_managed_trigger_leaves_the_scope_with_its_table_and_is_never_dropped(tmp_path, capsys):
    """Review V1: verify asked for a tombstone (TMB001) and the plan for one (MODULE_ORPHAN); with the
    tombstone the deploy sent DROP TRIGGER for the trigger of a table that the tool stopped managing."""
    project = Project(tmp_path, capsys)
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]'))
    project.write_objects(SALES, CUSTOMER, ORDER, TEMP_LOAD)
    trigger = (
        "schema/triggers/dbo.tr_Load.sql",
        "CREATE OR ALTER TRIGGER [dbo].[tr_Load] ON [dbo].[temp_Load] AFTER INSERT AS\n"
        "BEGIN\n    SET NOCOUNT ON;\nEND;\n",
    )
    key = names.object_key("TRIGGER", "dbo", "tr_Load")
    project.write(*trigger)
    db = deployed(project, project.release("create", "create_all"))
    assert status_of(db, key) == status_of(db, TEMP_LOAD.key) == "managed"

    # dbo.temp_* leaves the scope; the files of the table and of its trigger go (lint, OUT_OF_SCOPE)
    project.write("azsqlcd.toml", scoped_toml('schemas = ["sales", "dbo"]\nexclude_objects = ["dbo.temp_*"]'))
    for path in (names.path_for("TABLE", "dbo", "temp_Load"), trigger[0]):
        (project.repo / path).unlink()
    left = project.release("dbo.temp_* leaves the scope")  # lint and verify: 0, no TMB001
    out = project.out("plan")
    planned = project.tool("plan", *left.where, "--out", str(out), db=db)
    assert planned.code == 0, planned.err  # no MODULE_ORPHAN
    document = json.loads((out / "plan.json").read_text())
    steps = [step["id"] for unit in document["units"] for step in unit["steps"]]
    assert steps == [f"unscope:{TEMP_LOAD.key}", f"unscope:{key}"] and document["destructive"] == []
    sent = len(db.ddl)

    done = project.deploy(left, db)

    assert done.code == 0, done.err
    assert db.ddl[sent:] == [] and db.module(key) is not None and db.table(("dbo", "temp_Load")) is not None
    assert status_of(db, key) == status_of(db, TEMP_LOAD.key) == "dropped"
    drift = project.tool("drift", *left.where, db=db)
    assert (drift.code, drift.out.strip()) == (0, "no drift")


def test_the_baseline_of_a_schema_with_nothing_to_record_writes_no_step(tmp_path, capsys, monkeypatch):
    """Review minor 5: each run wrote one more empty baseline step."""
    project = Project(tmp_path, capsys, table_model=False)
    db = project.database()
    project.write(
        "azsqlcd.toml", TOML.format(table_model="false") + '\n[scope]\nschemas = ["dbo", "billing"]\n'
    )
    project.write("migrations/migrations.sum", "azsqlcd-sum 1\nbaseline\n")
    project.main = project.commit("scope")
    project.git("update-ref", "refs/remotes/origin/main", project.main)
    built = project.build()
    monkeypatch.chdir(tmp_path)

    done = project.tool("baseline", *built.where, "--schema", "billing", "--confirm-database", "sales", db=db)

    assert (done.code, done.reason) == (22, "ALREADY_BASELINED") and "nothing to record" in done.err
    assert db.rows("step") == [] and db.rows("object") == []


def test_without_a_scope_no_read_is_limited_and_no_schema_is_listed(project, tmp_path):
    first = release_1(project)
    db = deployed(project, first)
    assert project.tool("plan", *first.where, "--out", str(project.out("plan")), db=db).code == 0
    assert project.tool("drift", *first.where, db=db).code == 0
    out = tmp_path / "exported"
    assert project.tool("export", "--root", str(project.repo), *TARGET, "--out", str(out), db=db).code == 0
    assert not db.sent("sys.schemas AS sx") and not db.sent("azsqlcd:list_schemas")
    assert not db.sent("is_external] = 0")
    assert "## Scope" not in (out / "onboarding" / "dev" / "export.md").read_text()


# ------------------------------------------------------------------ findings: the tool is wrong
def test_a_module_that_was_dropped_behind_the_tool_can_be_taken_out_with_a_tombstone(project):
    first = release_1(project)
    db = deployed(project, first)
    db.out_of_band("DROP PROCEDURE [sales].[usp_CountOrders];")
    drift = project.tool("drift", *first.where, db=db)
    assert drift.code == 30 and drift.out.startswith(f"missing {PROCEDURE_KEY} exists ")
    accept = project.tool("resolve", *first.where, *RESOLVE, "--accept-drift", PROCEDURE_KEY, db=db)
    assert (accept.code, accept.reason) == (
        22,
        "RESOLVE_NOT_APPLICABLE",
    ) and "needs a tombstone" in accept.err

    (project.repo / PROCEDURE[0]).unlink()  # the tombstone that the refusal asks for
    project.write("schema/_tombstones.toml", f'[[drop]]\nobject = "{PROCEDURE_KEY}"\nreason = "gone"\n')
    tombstone = project.release("the procedure is gone")
    done = project.deploy(tombstone, db)

    assert done.code == 0, done.err  # is: 22 DRIFT_TOUCHED
    assert {row["object_key"]: row["status"] for row in db.rows("object")}[PROCEDURE_KEY] == "dropped"
    assert "DROP PROCEDURE [sales].[usp_CountOrders];" not in db.ddl  # A18: absent = no statement


def test_the_refusal_for_a_migration_that_was_applied_by_hand_names_the_action_that_resolves_it(project):
    first = release_1(project)
    db = deployed(project, first)
    second = release_2(project)
    db.out_of_band(*second.batches)  # the statements of the pending migration, run by hand
    assert db.model == second.model

    refused = project.deploy(second, db)

    assert refused.code == 22 and refused.reason in ("DRIFT_TOUCHED", "NAME_COLLISION")
    try:
        assert "--mark-applied" in refused.err  # is: only --accept-drift
    finally:
        # what the message says to do, and what works
        args = [*second.where, *RESOLVE]
        assert project.tool("resolve", *args, "--accept-drift", ORDER.key, db=db).code == 0
        assert project.deploy(second, db).reason == "BATCH_FAILED"
        assert project.tool("resolve", *args, "--mark-applied", second.migration.file, db=db).code == 0
        assert project.deploy(second, db).code == 0 and db.module(PROCEDURE_KEY).sent == PROCEDURE_2[1]


# ------------------------------------------------------------------ the fake database itself
SETUP_SQL = state.setup_sql(load_config(TOML.format(table_model="true")), "dev", "sales-dev")
ADD_X = "ALTER TABLE [dbo].[T] ADD [x] int NULL;"


def scratch() -> tuple[FakeDatabase, FakeDbSession]:
    db = FakeDatabase("sales", setup_sql=SETUP_SQL)
    db.out_of_band(
        "CREATE TABLE [dbo].[T] ([id] int NOT NULL, CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED ([id]));"
    )
    session = db.session()
    runner.set_session_options(session, 1000)  # the options of a run: XACT_ABORT ON, us_english, ...
    return db, session


def test_the_fake_reads_its_state_tables_from_the_setup_script_of_the_tool():
    db = FakeDatabase("sales", setup_sql=SETUP_SQL)

    assert set(db.state_tables) == set(state.TABLES) and db.model == Model()
    recorded = db.recorded()
    assert (recorded.meta.project, recorded.meta.environment) == ("sales", "dev")
    assert recorded.latest_ok_run is None and recorded.steps == () and recorded.objects == {}
    with pytest.raises(ToolError) as refused:
        FakeDatabase("sales").recorded()  # no setup script ran
    assert refused.value.reason_code == "STATE_MISSING"


def test_the_fake_fails_loud_on_a_batch_that_it_does_not_understand_and_shows_only_its_head():
    db, session = scratch()
    secret = "N'" + "x" * 200 + " the end of a long text'"

    with pytest.raises(AssertionError) as unknown:
        session.execute(f"UPDATE [dbo].[T] SET [id] = 1 WHERE [id] = 2; SELECT {secret};")

    assert db.unknown == ["UPDATE [dbo].[T] SET [id] = 1 WHERE [id] = 2; SELECT N'" + "x" * 15]
    assert "the end of a long text" not in str(unknown.value)
    db.respond("UPDATE [dbo].[T]", [])  # what the fake does not model is answered by a rule
    assert session.execute("UPDATE [dbo].[T] SET [id] = 1 WHERE [id] = 2;") == []


def test_a_rollback_in_the_fake_takes_back_ddl_modules_and_state_rows_and_keeps_identity_values():
    db, session = scratch()
    insert = state.insert_run(
        command="deploy",
        release_seq=1,
        git_sha="a" * 40,
        manifest_sha256="b" * 64,
        plan_sha256="c" * 64,
        tool_version="0.1.0",
        tool_digest="d" * 64,
    )
    assert session.execute(insert) == [[(1,)]]
    before = (db.model, dict(db.modules), db.rows("run"), db.rows("step"))

    session.execute(f"BEGIN TRANSACTION; {state.bump_fence(1)}")
    session.execute(ADD_X)
    session.execute("CREATE OR ALTER VIEW [dbo].[v] AS SELECT [id], [x] FROM [dbo].[T];")
    session.execute(state.insert_step(run_id=1, kind="migration", status="ok", migration_id="0001__a.sql"))
    assert db.rows("run")[0]["segments_committed"] == 1 and len(db.modules) == 1
    assert session.execute("SELECT @@TRANCOUNT, XACT_STATE();") == [[(1, 1)]]
    session.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;")

    assert (db.model, db.modules, db.rows("run"), db.rows("step")) == before
    assert session.execute("SELECT @@TRANCOUNT, XACT_STATE();") == [[(0, 0)]]
    session.execute(state.insert_step(run_id=1, kind="migration", status="ok", migration_id="0001__a.sql"))
    assert db.rows("step")[0]["step_id"] == 2  # the value of the row that was rolled back is used up


def test_an_error_in_a_transaction_of_the_fake_rolls_it_back_as_xact_abort_on_does():
    db, session = scratch()
    session.execute("BEGIN TRANSACTION;")
    session.execute(ADD_X)

    with pytest.raises(SqlError):  # replay.apply refuses: the column exists
        session.execute(ADD_X)

    table = db.model["TABLE:[dbo].[T]"]
    assert isinstance(table, Table) and [column.name for column in table.columns] == ["id"]
    assert session.execute("SELECT @@TRANCOUNT;") == [[(0,)]]
    db.fail_on("ADD [y]", sql_error("Incorrect syntax near 'y'."), keeps_transaction=True)
    session.execute("BEGIN TRANSACTION;")
    with pytest.raises(SqlError):
        session.execute("ALTER TABLE [dbo].[T] ADD [y] int NULL;")
    assert session.execute("SELECT @@TRANCOUNT;") == [[(1,)]]  # a compile error leaves the transaction


def test_a_killed_session_of_the_fake_loses_its_transaction_and_its_lock():
    db, session = scratch()
    lock = "/* azsqlcd:lock */ EXEC sys.sp_getapplock @Resource = N'azsqlcd:deploy';"
    assert session.execute(lock) == [[(0,)]] and db.session().execute(lock) == [[(-1,)]]
    session.execute("BEGIN TRANSACTION;")
    session.execute(ADD_X)
    db.kill_on("ADD [z]")

    with pytest.raises(SqlError) as lost:
        session.execute("ALTER TABLE [dbo].[T] ADD [z] int NULL;")

    assert lost.value.cls is ErrorClass.SESSION_LOST and session.closed and db.lock_holder is None
    table = db.model["TABLE:[dbo].[T]"]
    assert isinstance(table, Table) and [column.name for column in table.columns] == ["id"]
    with pytest.raises(SqlError):
        session.execute("SELECT @@TRANCOUNT;")


def test_a_state_write_of_the_fake_obeys_the_keys_and_lengths_of_the_setup_script():
    db, session = scratch()
    step = state.insert_step(run_id=7, kind="migration", status="ok", migration_id="0001__a.sql")

    with pytest.raises(SqlError) as no_run:  # FK_step_run
        session.execute(step)
    assert no_run.value.number == 547
    db.tables["run"].append(
        dict.fromkeys((c.name for c in db.state_tables["run"].columns), None) | {"run_id": 7}
    )
    session.execute(step)
    with pytest.raises(SqlError) as twice:  # UX_step_migration: a migration is recorded once
        session.execute(step)
    assert twice.value.number == 2601
    too_long = step.replace("N'migration'", "N'migration-and-more'")  # [kind] varchar(10)
    with pytest.raises(SqlError) as cut:
        session.execute(too_long.replace("0001__a.sql", "0002__b.sql"))
    assert cut.value.number == 2628 and len(db.rows("step")) == 1


def test_a_view_of_the_fake_needs_its_table_and_a_schema_bound_view_holds_the_column():
    db, session = scratch()

    with pytest.raises(SqlError) as missing:
        session.execute("CREATE OR ALTER VIEW [dbo].[v] AS SELECT [id] FROM [dbo].[Missing];")
    assert missing.value.number == 208
    session.execute(
        "CREATE OR ALTER PROCEDURE [dbo].[p] AS SELECT [id] FROM [dbo].[Missing];"
    )  # resolved late
    session.execute(ADD_X)
    session.execute("CREATE OR ALTER VIEW [dbo].[v] WITH SCHEMABINDING AS SELECT [id], [x] FROM [dbo].[T];")
    with pytest.raises(SqlError) as bound:
        session.execute("ALTER TABLE [dbo].[T] DROP COLUMN [x];")
    assert bound.value.number == 5074

    assert catalog.dependants_of(session, ["TABLE:[dbo].[T]"]) == [
        catalog.Dependant("VIEW:[dbo].[v]", "VIEW", True)
    ]
    assert catalog.broken_references(session, "PROCEDURE:[dbo].[p]") == ["UNRESOLVED [dbo].[missing]"]
    assert catalog_tables.blockers(session, "TABLE:[dbo].[T]", "x") == ["SCHEMABOUND VIEW:[dbo].[v]"]


def test_the_fake_needs_the_partition_scheme_of_a_statement_and_keeps_the_place_of_every_index():
    db, session = scratch()
    table = "CREATE TABLE [dbo].[P] ([d] date NOT NULL, [n] int NOT NULL) ON [PS_Day]([d]);"

    with pytest.raises(SqlError) as missing:
        session.execute(table)
    assert missing.value.number == 1921 and "Invalid partition scheme" in str(missing.value)
    assert "TABLE:[dbo].[P]" not in db.model
    db.partition_schemes.add("ps_day")  # the name of a scheme has no letter case
    session.execute(table)
    for statement in (
        "CREATE NONCLUSTERED INDEX [IX_other] ON [dbo].[P] ([n]) ON [PS_Other]([n]);",
        "ALTER TABLE [dbo].[P] ADD CONSTRAINT [UQ_P] UNIQUE NONCLUSTERED ([n]) ON [PS_Other]([n]);",
    ):
        with pytest.raises(SqlError) as other:
            session.execute(statement)
        assert other.value.number == 1921
    session.execute("CREATE NONCLUSTERED INDEX [IX_aligned] ON [dbo].[P] ([n]);")
    session.execute("CREATE NONCLUSTERED INDEX [IX_primary] ON [dbo].[P] ([n]) ON [PRIMARY];")
    session.execute("CREATE NONCLUSTERED INDEX [IX_plain] ON [dbo].[T] ([id]) ON [PRIMARY];")

    assert catalog_tables.partition_schemes(session) == ["ps_day"]
    found = catalog_tables.read_model(session, ["TABLE:[dbo].[P]", "TABLE:[dbo].[T]"])
    assert found.unsupported == ()
    partitioned, plain = found.model["TABLE:[dbo].[P]"], found.model["TABLE:[dbo].[T]"]
    assert isinstance(partitioned, Table) and isinstance(plain, Table)
    day = Partition("PS_Day", "d")
    assert partitioned.on == day
    assert {index.name: index.on for index in partitioned.indexes} == {
        "IX_aligned": day,
        "IX_primary": ON_PRIMARY,
    }
    assert [index.on for index in plain.indexes] == [None] and plain.on is None
    # the catalog rows hold the partitioning column that the engine adds to an index without it
    (members,) = [batch for batch in session.batches if "azsqlcd:read_index_columns" in batch]
    assert "ic.[partition_ordinal]" in members
    assert [c.name for c in partitioned.index("IX_aligned").columns] == ["n"]
