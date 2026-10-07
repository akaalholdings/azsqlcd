"""Releases through five environments: stories of more than one release, through the command line.

One database repository, and one fake database for each of dev, sandbox, test, preprod and prod
(support.fake_database.FakeDatabase). Every step is a call of cli.main, as a job of the pipeline
makes it: dev and sandbox deploy with --inline-plan; test, preprod and prod run `plan` and then
`deploy --expect-plan-file` (A26). A story asserts the exit code, the reason code, what was sent
and what the database holds at the end. Structural changes only: no data batch.

The stories: promotion, catch-up, a stale plan, an older release, a column rename, a dropped table
under a view, tombstones, withdraw and replace, an index build outside a transaction, two deploys
at once, onboarding of a database of 30 objects, letter case. Then further stories: a database
that joins late, and a migration that is withdrawn with no replacement.

The first commit of a repository (the template) is release r1, so the first release of a story is
r2: the stories take the number from the release (Built.seq) and never write one.

A test that fails because the tool is wrong stays, with xfail(strict=True) and the reason.
"""

import dataclasses
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from azsqlcd import chain, cli, emit, names, release, tables
from azsqlcd.model import (
    Column,
    DefaultConstraint,
    Expression,
    ForeignKey,
    Index,
    KeyColumn,
    Model,
    PrimaryKey,
    Schema,
    Table,
    TypeRef,
)
from azsqlcd.modules import stored_text
from azsqlcd.release import Bundle
from azsqlcd.session import AccessToken
from azsqlcd.sqlerrors import sql_error
from support.fake_database import FakeDatabase

REPO = Path(__file__).resolve().parents[2]
ENVS = ("dev", "sandbox", "test", "preprod", "prod")
GATED = ("test", "preprod", "prod")
HEAD = """
[project]
name = "sales"
tenant_id = "11111111-1111-1111-1111-111111111111"
table_model = {table_model}
module_chunk = 100
min_token_minutes = 20

[identities]
plan = "22222222-2222-2222-2222-222222222222"
deploy = "33333333-3333-3333-3333-333333333333"
"""
ENV = """
[env.{env}]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
gated = {gated}
targets = [{{ id = "sales-{env}", server = "sql-sales-{env}.database.windows.net", database = "sales" }}]
"""
APPROVAL = ["--approved-by", "release-managers", "--approved-utc", "2026-10-07T09:00:00+00:00"]
RESOLVE = ["--confirm-database", "sales", "--reason", "inspected by the DBA on call"]


def toml(table_model: bool = True) -> str:
    """azsqlcd.toml of the five environments: dev and sandbox have no gate, the other three have one."""
    envs = "".join(ENV.format(env=env, gated=str(env in GATED).lower()) for env in ENVS)
    return HEAD.format(table_model=str(table_model).lower()) + envs


# ------------------------------------------------------------------ the objects of the first release
INT, TINY = TypeRef("int"), TypeRef("tinyint")
SALES = Schema("sales")
CUSTOMER = Table(
    "sales",
    "Customer",
    (Column("CustomerId", INT, False), Column("Name", TypeRef("nvarchar", length=100), False)),
    (PrimaryKey("PK_Customer", True, (KeyColumn("CustomerId"),)),),
)
ORDER = Table(
    "sales",
    "Order",
    (
        Column("OrderId", INT, False),
        Column("CustomerId", INT, False),
        Column(
            "Status", TINY, False, default=DefaultConstraint("DF_Order_Status", Expression.from_sql("((0))"))
        ),
        Column("Reference", TypeRef("nvarchar", length=50), True),
    ),
    (
        PrimaryKey("PK_Order", True, (KeyColumn("OrderId"),)),
        ForeignKey("FK_Order_Customer", ("CustomerId",), "sales", "Customer", ("CustomerId",)),
    ),
    (Index("IX_Order_Status", False, False, (KeyColumn("Status"),)),),
)
# the view does not name [Reference]: a rename of that column leaves it sound
VIEW = (
    "schema/views/sales.vw_OpenOrders.sql",
    "CREATE OR ALTER VIEW [sales].[vw_OpenOrders] AS\n"
    "SELECT [OrderId], [CustomerId] FROM [sales].[Order] WHERE [Status] = 0;\n",
)
FUNCTION = (
    "schema/functions/sales.fn_OpenOrders.sql",
    "CREATE OR ALTER FUNCTION [sales].[fn_OpenOrders] ()\nRETURNS int\nAS\nBEGIN\n"
    "    RETURN (SELECT COUNT(*) FROM [sales].[Order] WHERE [Status] = 0);\nEND;\n",
)


def procedure(body: str) -> tuple[str, str]:
    """The file of [sales].[usp_FindOrder] with this body."""
    head = "CREATE OR ALTER PROCEDURE [sales].[usp_FindOrder] @Reference nvarchar(50) AS\n"
    return "schema/procedures/sales.usp_FindOrder.sql", head + body + "\n"


PROCEDURE = procedure("SELECT [OrderId] FROM [sales].[Order] WHERE [Reference] = @Reference;")
VIEW_KEY = names.object_key("VIEW", "sales", "vw_OpenOrders")
FUNCTION_KEY = names.object_key("FUNCTION", "sales", "fn_OpenOrders")
PROCEDURE_KEY = names.object_key("PROCEDURE", "sales", "usp_FindOrder")
MODULES = {VIEW_KEY: VIEW, FUNCTION_KEY: FUNCTION, PROCEDURE_KEY: PROCEDURE}
REFRESH_VIEW = "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[vw_OpenOrders]';"
DROP_PROCEDURE = "DROP PROCEDURE [sales].[usp_FindOrder];"


def with_columns(table: Table, *columns: Column) -> Table:
    return dataclasses.replace(table, columns=(*table.columns, *columns))


def with_indexes(table: Table, *indexes: Index) -> Table:
    return dataclasses.replace(table, indexes=(*table.indexes, *indexes))


# ------------------------------------------------------------------ changes that the stories use
NOTE = Column("Note", TypeRef("nvarchar", length=200), True)
ORDER_NOTE = with_columns(ORDER, NOTE)
ORDER_PRIORITY = with_columns(ORDER_NOTE, Column("Priority", TINY, True))
FIND_TOP = procedure("SELECT TOP (1) [OrderId] FROM [sales].[Order] WHERE [Reference] = @Reference;")
FIND_NOTE = procedure("SELECT [OrderId], [Note] FROM [sales].[Order] WHERE [Reference] = @Reference;")


# ------------------------------------------------------------------ helpers
class Tokens:
    def get(self) -> AccessToken:
        return AccessToken("the-access-token-of-the-test", int(time.time()) + 3600)


@dataclasses.dataclass(frozen=True)
class Outcome:
    code: int
    out: str
    err: str
    folder: Path | None = None  # --out of the command

    @property
    def reason(self) -> str:
        """The reason code of a non-zero end: the first word of the first line on stderr."""
        return self.err.split(":", 1)[0]

    @property
    def report(self) -> dict:
        """report.json of a deploy, a baseline or a resolve."""
        assert self.folder is not None, "the command was given no --out"
        return json.loads((self.folder / "report.json").read_text())


@dataclasses.dataclass(frozen=True)
class Built:
    """One release: the directory of bundle.tar and manifest.json, and what it holds."""

    dist: Path
    digest: str
    bundle: Bundle
    migration: chain.Migration | None  # the migration that the pull request added
    verified: str = ""  # what `verify` printed for the pull request: its findings

    @property
    def seq(self) -> int:
        return self.bundle.manifest.release_seq

    @property
    def file(self) -> str:
        """The migration id of the migration that the pull request added."""
        assert self.migration is not None
        return self.migration.file

    @property
    def batches(self) -> list[str]:
        return [batch.text for batch in self.migration.batches] if self.migration else []

    @property
    def model(self) -> Model:
        return tables.head_model(self.bundle.files)

    def where(self, env: str) -> list[str]:
        return ["--bundle", str(self.dist), "--digest", self.digest, "--env", env, "--target", f"sales-{env}"]


class Pipeline:
    """A database repository in a temporary directory, its databases, and the jobs of a release."""

    def __init__(self, folder: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.folder, self.capsys, self.repo = folder, capsys, folder / "db-sales"
        self.calls = 0
        self.dbs: dict[str, FakeDatabase] = {}
        shutil.copytree(REPO / "templates" / "db-repo", self.repo)
        self.write("azsqlcd.toml", toml())
        self.git("init", "-q", "-b", "main")
        self.main = ""
        self.push("repository from the template")

    # -------------------------------------------------------------- the repository
    def git(self, *args: str) -> str:
        identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
        done = subprocess.run(
            ["git", *identity, *args], cwd=self.repo, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    def push(self, message: str) -> None:
        """Commit the working tree to main with no check. A story uses merge(); this is the way around it."""
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        self.main = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.main)

    def write(self, path: str, text: str) -> None:
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_bytes(text.encode("utf-8"))

    def read(self, path: str) -> str:
        return (self.repo / path).read_text(encoding="utf-8")

    def delete(self, *paths: str) -> None:
        for path in paths:
            (self.repo / path).unlink()

    def write_objects(self, *objects: Schema | Table) -> None:
        """Table-class files in the canonical text of the tool, as `export` and a formatter write them."""
        for obj in objects:
            path = names.path_for(obj.kind, None if isinstance(obj, Schema) else obj.schema, obj.name)
            self.write(path, emit.emit_object_file(obj))

    def tombstone(self, *keys: str) -> None:
        """Add a [[drop]] entry for each key to schema/_tombstones.toml."""
        more = "".join(f'\n[[drop]]\nobject = "{key}"\nreason = "no caller is left"\n' for key in keys)
        self.write(chain.TOMBSTONES_PATH, self.read(chain.TOMBSTONES_PATH) + more)

    # -------------------------------------------------------------- the command line
    def tool(
        self, *argv: str, db: FakeDatabase | None = None, out: Path | None = None, alone: bool = True
    ) -> Outcome:
        """One call of the command line. A batch that the fake did not understand fails the test.

        alone=False: another command runs on the database at the same time, so a session of it is open.
        """
        self.capsys.readouterr()
        code = cli.main(
            [*argv, *(["--out", str(out)] if out else [])],
            session_factory=db.session if db is not None else None,
            token_provider=Tokens(),
        )
        captured = self.capsys.readouterr()
        if db is not None:
            assert db.unknown == [], f"the fake did not understand: {db.unknown}\n{captured.err}"
            assert not alone or all(session.closed for session in db.sessions), "a session is open"
        return Outcome(code, captured.out, captured.err, out)

    def offline(self, *argv: str) -> Outcome:
        """lint, verify, gen, build, setup-sql: a command with no database, in the working tree."""
        return self.tool(*argv, "--root", str(self.repo))

    def out(self, name: str) -> Path:
        self.calls += 1
        return self.folder / f"{name}-{self.calls}"

    # -------------------------------------------------------------- a pull request and its release
    def gen(self, name: str, *rename: str, reasons: bool = True) -> Path:
        """`azsqlcd gen --name`; the file that it wrote. reasons: write a reason on each allow line
        and run `gen --resum`, as the author of a pull request does."""
        renames = [arg for item in rename for arg in ("--rename", item)]
        generated = self.offline("gen", "--base", self.main, "--name", name, *renames)
        assert generated.code == 0, generated.err
        path = self.repo / next(line.split(" ")[1] for line in generated.out.splitlines() if "__" in line)
        if reasons and "reason: TODO" in path.read_text():
            path.write_text(path.read_text().replace("reason: TODO", "reason: agreed with the owner"))
            self.resum()
        return path

    def resum(self) -> None:
        done = self.offline("gen", "--base", self.main, "--resum")
        assert done.code == 0, done.err

    def replace_migration(self, old: str, name: str, reason: str, statements: str) -> Path:
        """Withdraw a merged migration and add its replacement (A31): the chain line of `old` gets
        the word `withdrawn`, a new line with replaces= names the new file, and `gen --resum`
        writes its checksum. The pull request changes no table-class file."""
        lines = self.read(chain.SUM_PATH).splitlines()
        at = next(index for index, line in enumerate(lines) if line.startswith(old + " "))
        number = max(int(line[:4]) for line in lines if line[:4].isdigit()) + 1
        stem = f"{number:04d}__{name}"
        lines[at] += " withdrawn"
        lines.append(f"{stem}.sql sha256:{'0' * 64} tx replaces={old}")
        self.write(chain.SUM_PATH, "\n".join(lines) + "\n")
        header = (
            f"-- azsqlcd:migration {stem}\n-- azsqlcd:mode tx\n"
            f"-- azsqlcd:allow REPLACEMENT_EDGE {old} reason: {reason}\n"
        )
        self.write(f"migrations/{stem}.sql", header + statements)
        self.resum()
        return self.repo / "migrations" / f"{stem}.sql"

    def checks(self) -> tuple[Outcome, Outcome]:
        """The two checks of a pull request: lint, and verify against main."""
        return self.offline("lint"), self.offline("verify", "--base", self.main)

    def merge(self, message: str, migration: Path | None = None) -> Built:
        """The checks pass, the pull request is merged to main, and the release is built."""
        lint, verify = self.checks()
        assert lint.code == 0, f"lint: {lint.out}{lint.err}"
        assert verify.code == 0, f"verify: {verify.out}{verify.err}"
        self.push(message)
        added = chain.parse_migration(migration.read_text(), migration.name) if migration else None
        return dataclasses.replace(self.build(added), verified=verify.out)

    def release(self, message: str, migration: str | None = None, *rename: str) -> Built:
        """A pull request with the files of the working tree: gen (when migration names one), the
        checks, the merge and the build. migration None: the pull request changes modules only."""
        return self.merge(message, self.gen(migration, *rename) if migration is not None else None)

    def build(self, added: chain.Migration | None = None) -> Built:
        dist = self.out("dist")
        built = self.offline("build", "--commit", self.main, "--out", str(dist))
        assert built.code == 0, built.out + built.err
        digest = next(line.split(" ")[1] for line in built.out.splitlines() if line.startswith("digest "))
        return Built(dist, digest, release.read_bundle(dist, digest), added)

    # -------------------------------------------------------------- the databases and the jobs
    def database(self, env: str) -> FakeDatabase:
        """An empty database of the environment in which an administrator ran `azsqlcd setup-sql`."""
        script = self.offline("setup-sql", "--env", env, "--target", f"sales-{env}")
        assert script.code == 0, script.err
        self.dbs[env] = FakeDatabase("sales", setup_sql=script.out)
        return self.dbs[env]

    def plan(self, built: Built, env: str, *, alone: bool = True) -> tuple[Outcome, Path]:
        """The plan job: (its end, the path of plan.json)."""
        folder = self.out("plan")
        done = self.tool("plan", *built.where(env), db=self.dbs[env], out=folder, alone=alone)
        return done, folder / "plan.json"

    def deploy(self, built: Built, env: str, plan: Path | None = None, *, alone: bool = True) -> Outcome:
        """The deploy job: with the plan file that was approved, or with --inline-plan."""
        mode = ["--inline-plan"] if plan is None else ["--expect-plan-file", str(plan), *APPROVAL]
        where = (*built.where(env), *mode)
        return self.tool("deploy", *where, db=self.dbs[env], out=self.out("report"), alone=alone)

    def promote(self, built: Built, env: str) -> Outcome:
        """The stage of one environment (A26). With a gate: the plan job, then the deploy of that
        plan; a plan that is refused ends the stage. No gate: one deploy with --inline-plan."""
        if env not in GATED:
            return self.deploy(built, env)
        planned, file = self.plan(built, env)
        return self.deploy(built, env, file) if planned.code == 0 else planned

    def resolve(self, built: Built, env: str, *action: str, alone: bool = True) -> Outcome:
        where = (*built.where(env), *RESOLVE, *action)
        return self.tool("resolve", *where, db=self.dbs[env], out=self.out("report"), alone=alone)

    def baseline(self, built: Built, env: str, *, report_only: bool = False) -> Outcome:
        where = (*built.where(env), "--confirm-database", "sales", *(["--report-only"] * report_only))
        return self.tool(
            "baseline", *where, db=self.dbs[env], out=None if report_only else self.out("report")
        )

    def drift(self, built: Built, env: str) -> Outcome:
        return self.tool("drift", *built.where(env), db=self.dbs[env])


@pytest.fixture
def pipe(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Pipeline:
    return Pipeline(tmp_path, capsys)


def release_1(pipe: Pipeline) -> Built:
    """The first release: the schema, two tables, a view, a function and a procedure."""
    pipe.write_objects(SALES, CUSTOMER, ORDER)
    for path, text in MODULES.values():
        pipe.write(path, text)
    return pipe.release("sales schema", "create_sales")


def everywhere(pipe: Pipeline, *releases: Built, envs: tuple[str, ...] = ENVS) -> None:
    """New databases for these environments, and each release promoted through them in order."""
    for env in envs:
        pipe.database(env)
    for built in releases:
        for env in envs:
            done = pipe.promote(built, env)
            assert done.code == 0, f"r{built.seq} to {env}: {done.err}"


def texts(db: FakeDatabase) -> dict[str, str]:
    """Object key -> the text of each module as the database holds it (modules.stored_text)."""
    return {module.key: module.definition for module in db.modules.values()}


def frozen(db: FakeDatabase) -> tuple:
    """All that a command can change: the catalog, the state tables, and the count of DDL batches."""
    return db.model, dict(db.modules), db.rows("run"), db.rows("step"), db.rows("object"), len(db.ddl)


def catalog(db: FakeDatabase) -> tuple:
    """The catalog and the records of what is applied. A run that failed leaves these as they were."""
    return db.model, dict(db.modules), db.rows("step"), db.rows("object")


def runs(db: FakeDatabase) -> list[tuple[str, str, int]]:
    return [(run["command"], run["status"], run["release_seq"]) for run in db.rows("run")]


def migrations(db: FakeDatabase) -> list[tuple[str, str]]:
    """(migration id, status) of every migration step, in the order they were recorded."""
    return [(step["migration_id"], step["status"]) for step in db.rows("step") if step["migration_id"]]


def rows(db: FakeDatabase, *kinds: str) -> list[tuple[str, str]]:
    """(object key, status) of the object rows of these kinds, in key order."""
    found = [(row["object_key"], row["status"]) for row in db.rows("object")]
    return sorted(row for row in found if row[0].split(":")[0] in kinds)


def committed(db: FakeDatabase, since: int) -> list[list[str]]:
    """The catalog changes of each transaction that was committed after batch number `since`."""
    changes, found = set(db.ddl), []
    current: list[str] | None = None
    for batch in db.batches[since:]:
        if "BEGIN TRANSACTION;" in batch:
            current = []
        elif batch.startswith("COMMIT TRANSACTION") and current is not None:
            found.append(current)
            current = None
        elif current is not None and batch in changes:
            current.append(batch)
    return found


# ------------------------------------------------------------------ 1. promotion
def test_one_release_is_promoted_through_five_environments_and_all_five_end_equal(pipe):
    first = release_1(pipe)
    for env in ENVS:
        pipe.database(env)

    # dev and sandbox have no reviewers: one deploy job, and the plan is made under the lock
    for env in ("dev", "sandbox"):
        done = pipe.deploy(first, env)
        assert done.code == 0, done.err
    # test, preprod and prod have a gate: no inline plan; the plan job, an approval, then that plan
    plans: dict[str, Path] = {}
    for env in GATED:
        db = pipe.dbs[env]
        inline = pipe.deploy(first, env)
        assert (inline.code, inline.reason) == (22, "INLINE_PLAN_GATED") and db.batches == []
        planned, plans[env] = pipe.plan(first, env)
        assert planned.code == 0 and "work, pending = true" in planned.out
        assert db.ddl == [] and db.rows("run") == []  # a plan changes nothing
    # a plan is for one target: the plan of preprod does not open prod
    wrong = pipe.deploy(first, "prod", plans["preprod"])
    assert (wrong.code, wrong.reason) == (22, "STALE_PLAN") and pipe.dbs["prod"].ddl == []
    for env in GATED:
        done = pipe.deploy(first, env, plans[env])
        assert done.code == 0, done.err

    for env in ENVS:
        db = pipe.dbs[env]
        assert db.model == first.model and texts(db) == {
            key: stored_text(text) for key, (_, text) in MODULES.items()
        }
        assert db.ddl == [*first.batches, FUNCTION[1], VIEW[1], PROCEDURE[1]]
        (run,) = db.rows("run")
        assert (run["command"], run["status"], run["release_seq"]) == ("deploy", "ok", first.seq)
        # the same artefact everywhere, and the approval of a gated stage is recorded with the run
        assert (run["git_sha"], run["manifest_sha256"]) == (first.bundle.manifest.commit, first.digest)
        assert run["approved_by"] == ("release-managers" if env in GATED else None)
        assert pipe.drift(first, env).code == 0
    assert len({pipe.dbs[env].rows("run")[0]["plan_sha256"] for env in ENVS}) == 5

    # the next release, a migration and a changed procedure, takes the same way
    pipe.write_objects(ORDER_NOTE)
    pipe.write(*FIND_NOTE)
    second = pipe.release("order notes", "order_note")
    for env in ENVS:
        db = pipe.dbs[env]
        sent = len(db.ddl)
        done = pipe.promote(second, env)
        assert done.code == 0, done.err
        assert db.ddl[sent:] == [*second.batches, FIND_NOTE[1], REFRESH_VIEW]
    dev = pipe.dbs["dev"]
    assert dev.model == second.model != first.model and dev.module(PROCEDURE_KEY).sent == FIND_NOTE[1]
    for env in ENVS:
        db = pipe.dbs[env]
        assert (db.model, texts(db)) == (dev.model, texts(dev))
        assert runs(db) == [("deploy", "ok", first.seq), ("deploy", "ok", second.seq)]
        assert pipe.drift(second, env).code == 0


# ------------------------------------------------------------------ 2. catch-up
def test_a_database_three_releases_behind_is_caught_up_in_order_and_may_skip_a_module_only_release(pipe):
    first = release_1(pipe)
    pipe.write_objects(ORDER_NOTE)
    note = pipe.release("order notes", "order_note")  # a migration
    pipe.write(*FIND_NOTE)
    reads = pipe.release("the procedure reads the note")  # modules only
    pipe.write_objects(ORDER_PRIORITY)
    priority = pipe.release("order priority", "order_priority")  # a migration
    everywhere(pipe, first, envs=("dev", "preprod", "prod"))
    for built in (note, reads, priority):
        assert pipe.promote(built, "dev").code == 0
    dev, preprod, prod = (pipe.dbs[env] for env in ("dev", "preprod", "prod"))

    # prod records the first release. The newest release, and the one before it, name the release to take
    before = frozen(prod)
    for built in (priority, reads):
        refused = pipe.promote(built, "prod")
        assert (refused.code, refused.reason) == (22, "CATCHUP_REQUIRED")
        assert f"promote r{note.seq} first" in refused.err
    assert frozen(prod) == before

    # preprod takes the three releases one by one, as dev did
    for built in (note, reads, priority):
        done = pipe.promote(built, "preprod")
        assert done.code == 0, done.err
    # prod takes the two releases that hold a migration; the module-only release between them is skipped
    assert pipe.promote(note, "prod").code == 0
    sent = len(prod.ddl)
    done = pipe.promote(priority, "prod")
    assert done.code == 0, done.err
    # each release applied only its own migration, and the newest one brings the text of the procedure
    assert prod.ddl[sent:] == [*priority.batches, FIND_NOTE[1], REFRESH_VIEW]
    assert runs(prod) == [("deploy", "ok", built.seq) for built in (first, note, priority)]
    assert runs(preprod) == [("deploy", "ok", built.seq) for built in (first, note, reads, priority)]
    for env in ("preprod", "prod"):
        db = pipe.dbs[env]
        assert migrations(db) == [(built.file, "ok") for built in (first, note, priority)]
        assert (db.model, texts(db)) == (dev.model, texts(dev)) and db.model == priority.model
        assert pipe.drift(priority, env).code == 0


# ------------------------------------------------------------------ 3. a stale plan
def test_a_plan_that_is_older_than_another_deploy_is_refused_as_stale_and_nothing_is_sent(pipe):
    first = release_1(pipe)
    pipe.write(*FIND_TOP)
    top = pipe.release("the first match only")  # modules only
    pipe.write_objects(ORDER_NOTE)
    note = pipe.release("order notes", "order_note")
    everywhere(pipe, first, envs=("prod",))
    prod = pipe.dbs["prod"]

    # the plan of the newest release is made, and it waits for its approval
    planned, approved = pipe.plan(note, "prod")
    assert planned.code == 0, planned.err
    # meanwhile the release before it is promoted to prod
    assert pipe.promote(top, "prod").code == 0
    before = frozen(prod)

    stale = pipe.deploy(note, "prod", approved)

    assert (stale.code, stale.reason) == (22, "STALE_PLAN")
    assert frozen(prod) == before and prod.lock_holder is None
    assert stale.report["run_id"] is None and stale.report["steps_applied"] == []

    # the stage starts again from the plan job
    planned, fresh = pipe.plan(note, "prod")
    done = pipe.deploy(note, "prod", fresh)
    assert planned.code == 0 and done.code == 0, planned.err + done.err
    assert prod.model == note.model and prod.module(PROCEDURE_KEY).sent == FIND_TOP[1]
    # a plan is used once: the deploy job that is started again with it sends nothing
    before = frozen(prod)
    again = pipe.deploy(note, "prod", fresh)
    assert (again.code, again.reason) == (22, "STALE_PLAN") and frozen(prod) == before


# ------------------------------------------------------------------ 4. an older release
def test_an_older_release_after_a_newer_one_sends_nothing_and_its_module_text_never_arrives(pipe):
    first = release_1(pipe)
    pipe.write(*FIND_TOP)
    top = pipe.release("the first match only")  # modules only: an environment may skip it
    pipe.write_objects(ORDER_NOTE)
    pipe.write(*FIND_NOTE)
    note = pipe.release("order notes", "order_note")
    everywhere(pipe, first, note, envs=("dev", "prod"))

    for env in ("dev", "prod"):
        db = pipe.dbs[env]
        before = frozen(db)
        for older in (top, first):
            plan = None
            if env in GATED:  # the plan job of the stage says it first
                planned, plan = pipe.plan(older, env)
                assert planned.code == 0 and "already_past, pending = false" in planned.out
            done = pipe.deploy(older, env, plan)
            # exit 0 with a note: the stage of an older release only proves that the database holds it
            assert done.code == 0, done.err
            assert done.report["reason_code"] == "ALREADY_PAST" and done.report["run_id"] is None
            assert f"past release r{older.seq}" in done.out
        assert frozen(db) == before
        # the text of the older release was never sent, not even to be parsed
        assert FIND_TOP[1] not in db.batches
        assert db.module(PROCEDURE_KEY).sent == FIND_NOTE[1] and db.model == note.model


# ------------------------------------------------------------------ 5. a column rename
RENAME = "column:[sales].[Order].[Reference]=[CustomerRef]"
ORDER_RENAMED = dataclasses.replace(
    ORDER,
    columns=tuple(
        dataclasses.replace(column, name="CustomerRef") if column.name == "Reference" else column
        for column in ORDER.columns
    ),
)
FIND_RENAMED = procedure("SELECT [OrderId] FROM [sales].[Order] WHERE [CustomerRef] = @Reference;")


def test_a_column_rename_with_its_procedure_is_one_transaction_and_the_view_of_the_table_still_binds(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("dev", "prod"))
    pipe.write_objects(ORDER_RENAMED)
    migration = pipe.gen("rename_reference", RENAME)
    rename = "EXEC sys.sp_rename N'[sales].[Order].[Reference]', N'CustomerRef', N'COLUMN';"
    assert rename in migration.read_text() and "DROP COLUMN" not in migration.read_text()
    # the residue lint names the procedure that still uses the old name (a warning, A19)
    _, verify = pipe.checks()
    assert verify.code == 0 and f"{PROCEDURE[0]}:2: warning REN001" in verify.out
    pipe.write(*FIND_RENAMED)  # the same pull request changes the procedure
    renamed = pipe.merge("rename the reference of an order", migration)
    assert "REN001" not in renamed.verified

    for env in ("dev", "prod"):
        db = pipe.dbs[env]
        sent = len(db.batches)
        done = pipe.promote(renamed, env)

        assert done.code == 0, done.err
        # one transaction: the rename, the procedure, and the refresh of the view on the table
        assert committed(db, sent) == [[*renamed.batches, FIND_RENAMED[1], REFRESH_VIEW]]
        assert db.model == renamed.model
        view, find = db.module(VIEW_KEY), db.module(PROCEDURE_KEY)
        assert view.sent == VIEW[1] and db.problems(view) == ([], [])
        assert find.sent == FIND_RENAMED[1] and db.problems(find) == ([], [])
        assert pipe.drift(renamed, env).code == 0


def test_a_column_rename_that_leaves_its_procedure_behind_is_rolled_back(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("dev",))
    dev = pipe.dbs["dev"]
    pipe.write_objects(ORDER_RENAMED)
    renamed = pipe.release("rename the reference of an order", "rename_reference", RENAME)
    assert "warning REN001" in renamed.verified  # the pull request is not refused: the deploy is the gate
    before = catalog(dev)

    failed = pipe.promote(renamed, "dev")

    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN") and PROCEDURE_KEY in failed.err
    assert renamed.batches[0] in dev.ddl  # the rename was sent, and the rollback took it back
    assert catalog(dev) == before and dev.model == first.model
    assert runs(dev)[-1] == ("deploy", "failed", renamed.seq) and failed.report["steps_applied"] == []


# ------------------------------------------------------------------ 6. a dropped table under a view
AUDIT = Table(
    "sales",
    "Audit",
    (Column("AuditId", INT, False), Column("What", TypeRef("nvarchar", length=100), False)),
    (PrimaryKey("PK_Audit", True, (KeyColumn("AuditId"),)),),
)
AUDIT_VIEW = (
    "schema/views/sales.vw_Audit.sql",
    "CREATE OR ALTER VIEW [sales].[vw_Audit] AS\nSELECT [AuditId], [What] FROM [sales].[Audit];\n",
)
AUDIT_VIEW_KEY = names.object_key("VIEW", "sales", "vw_Audit")
DROP_AUDIT = "DROP TABLE [sales].[Audit];"


def audit(pipe: Pipeline, *envs: str) -> Built:
    """Two releases in these environments: the sales schema, then an audit table with a view on it."""
    first = release_1(pipe)
    pipe.write_objects(AUDIT)
    pipe.write(*AUDIT_VIEW)
    added = pipe.release("an audit table and its view", "audit")
    everywhere(pipe, first, added, envs=envs)
    return added


def audit_dropped(pipe: Pipeline) -> Built:
    """The release of a pull request that drops the audit table and forgets the view on it."""
    pipe.delete(names.path_for("TABLE", "sales", "Audit"))
    return pipe.release("drop the audit table", "drop_audit")


def test_a_dropped_table_under_a_view_passes_lint_only_with_its_allow_line_and_is_never_committed(pipe):
    added = audit(pipe, "dev", "prod")
    pipe.delete(names.path_for("TABLE", "sales", "Audit"))
    migration = pipe.gen("drop_audit", reasons=False)
    text = migration.read_text()
    allow = "-- azsqlcd:allow DROP_TABLE [sales].[Audit] reason: TODO\n"
    assert allow + DROP_AUDIT in text

    # gen wrote the allow line; TODO is not a reason
    lint, _ = pipe.checks()
    assert (lint.code, lint.reason) == (22, "LINT_FAILED") and ":3: error ALLOW_REASON" in lint.out
    # the statement with no allow line
    migration.write_text(text.replace(allow, ""))
    pipe.resum()
    lint, verify = pipe.checks()
    assert (lint.code, lint.reason) == (22, "LINT_FAILED") and ":3: error DROP_TABLE" in lint.out
    assert (verify.code, verify.reason) == (22, "VERIFY_FAILED")
    # with the line and a reason the pull request passes; the view is a warning only (A19)
    migration.write_text(text.replace("TODO", "the audit moved to the log service"))
    pipe.resum()
    lint, verify = pipe.checks()
    assert lint.code == 0 and verify.code == 0, lint.out + verify.out
    assert f"{AUDIT_VIEW[0]}:2: warning DRP003" in verify.out
    dropped = pipe.merge("drop the audit table", migration)

    for env in ("dev", "prod"):
        db = pipe.dbs[env]
        before = catalog(db)

        failed = pipe.promote(dropped, env)

        # the deploy is the gate: the table was dropped in the transaction, and nothing of it stays
        # N1-F3: the refresh of the view fails, and that is the finding of A12, with the view named
        assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN"), failed.err
        assert AUDIT_VIEW_KEY in failed.err
        assert dropped.batches[0] in db.ddl and failed.report["steps_applied"] == []
        assert catalog(db) == before and db.model == added.model
        assert db.problems(db.module(AUDIT_VIEW_KEY)) == ([], [])  # the view still binds
        assert runs(db)[-1] == ("deploy", "failed", dropped.seq)
        assert pipe.drift(added, env).code == 0


def test_a_module_only_fix_of_a_failed_release_is_refused_and_both_messages_name_the_way_that_works(pipe):
    """N2-F1, as the tool is now: the failed deploy and the refusal of the fix release both say
    what the next pull request must hold. The way itself is the test after the next one."""
    audit(pipe, "dev")
    dropped = audit_dropped(pipe)
    failed = pipe.promote(dropped, "dev")
    assert (failed.code, failed.reason) == (21, "DEPENDANT_BROKEN")
    assert "withdraw the migration" in failed.err and "CATCHUP_REQUIRED" in failed.err

    pipe.delete(AUDIT_VIEW[0])
    pipe.tombstone(AUDIT_VIEW_KEY)
    fixed = pipe.release("the view goes with its table")
    refused = pipe.promote(fixed, "dev")

    assert (refused.code, refused.reason) == (22, "CATCHUP_REQUIRED")
    assert f"promote r{dropped.seq} first" in refused.err
    assert "withdraw the migration" in refused.err and "REPLACEMENT_EDGE" in refused.err


@pytest.mark.xfail(
    strict=True,
    reason="N2-F1, open owner decision on A7. A release that failed in its transaction because of a "
    "module (21 DEPENDANT_BROKEN) has its migration rolled back, so the migration is still pending. "
    "The pull request that only changes or tombstones the module did not add that migration, and "
    "plan._refuse_catch_up refuses it with 22 CATCHUP_REQUIRED 'promote r<failed> first'. The rule "
    "'module-only work never needs a catch-up' holds already and does not reach this case: here a "
    "migration is pending. Both messages and the runbook now name the way that works (withdraw and "
    "replace the migration in the pull request that fixes the module). To make this test pass, a "
    "later release must be allowed to carry the migration of a release that cannot be applied",
)
def test_the_pull_request_that_fixes_the_module_of_a_failed_release_can_be_deployed(pipe):
    audit(pipe, "dev")
    dev = pipe.dbs["dev"]
    dropped = audit_dropped(pipe)
    assert pipe.promote(dropped, "dev").code == 21

    # what the message asks for: "Fix the module in the same pull request, or tombstone it"
    pipe.delete(AUDIT_VIEW[0])
    pipe.tombstone(AUDIT_VIEW_KEY)
    fixed = pipe.release("the view goes with its table")
    done = pipe.promote(fixed, "dev")

    assert done.code == 0, done.err  # is: 22 CATCHUP_REQUIRED, promote r<dropped> first
    assert dev.model == fixed.model and dev.module(AUDIT_VIEW_KEY) is None


def test_today_the_fix_of_a_failed_release_must_withdraw_and_replace_its_migration(pipe):
    audit(pipe, "dev", "prod")
    dropped = audit_dropped(pipe)
    assert pipe.promote(dropped, "dev").code == 21  # the promotion stops at dev

    # one pull request: the view gets its tombstone, and the same DROP TABLE comes again as a replacement
    pipe.delete(AUDIT_VIEW[0])
    pipe.tombstone(AUDIT_VIEW_KEY)
    statements = f"-- azsqlcd:allow DROP_TABLE [sales].[Audit] reason: the audit moved\n{DROP_AUDIT}\nGO\n"
    again = pipe.replace_migration(dropped.file, "drop_audit_and_its_view", "the same statement", statements)
    fixed = pipe.merge("drop the audit table and its view", again)

    for env in ("dev", "prod"):
        db = pipe.dbs[env]
        sent = len(db.ddl)
        done = pipe.promote(fixed, env)
        assert done.code == 0, done.err
        assert db.ddl[sent:] == [*fixed.batches, "DROP VIEW [sales].[vw_Audit];"]
        assert db.model == fixed.model and db.module(AUDIT_VIEW_KEY) is None
        assert migrations(db)[-1] == (fixed.file, "ok") and (dropped.file, "ok") not in migrations(db)
        assert pipe.drift(fixed, env).code == 0


# ------------------------------------------------------------------ 7. tombstones
def test_a_deleted_procedure_needs_a_tombstone_and_is_then_dropped_in_every_environment(pipe):
    first = release_1(pipe)
    everywhere(pipe, first)
    pipe.delete(PROCEDURE[0])

    # the file is gone and no tombstone says so: the pull request is refused
    _, verify = pipe.checks()
    assert (verify.code, verify.reason) == (22, "VERIFY_FAILED")
    assert f"{PROCEDURE[0]}:1: error TMB001" in verify.out

    pipe.tombstone(PROCEDURE_KEY)
    gone = pipe.release("the procedure has no caller")
    planned, _ = pipe.plan(gone, "prod")  # the approver sees the drop first
    assert "DESTRUCTIVE: 1 item(s)" in planned.out and f"DROP_MODULE {PROCEDURE_KEY}" in planned.out
    for env in ENVS:
        db = pipe.dbs[env]
        sent = len(db.ddl)
        done = pipe.promote(gone, env)
        assert done.code == 0, done.err
        assert db.ddl[sent:] == [DROP_PROCEDURE] and done.report["modules_dropped"] == [PROCEDURE_KEY]
        assert set(texts(db)) == {VIEW_KEY, FUNCTION_KEY}
        assert rows(db, "PROCEDURE") == [(PROCEDURE_KEY, "dropped")]
        assert pipe.drift(gone, env).code == 0
    # a second deploy of the release finds nothing to drop
    prod = pipe.dbs["prod"]
    before = frozen(prod)
    assert pipe.promote(gone, "prod").code == 0 and frozen(prod) == before


def test_a_deleted_file_with_no_tombstone_that_reaches_main_all_the_same_drops_nothing(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("prod",))
    prod = pipe.dbs["prod"]
    pipe.delete(PROCEDURE[0])
    pipe.push("merged with no check")  # verify did not run: a direct push, or a ruleset that was off
    orphan = pipe.build()
    before = frozen(prod)

    refused = pipe.promote(orphan, "prod")

    assert (refused.code, refused.reason) == (22, "MODULE_ORPHAN") and PROCEDURE_KEY in refused.err
    assert frozen(prod) == before and prod.module(PROCEDURE_KEY).sent == PROCEDURE[1]


# ------------------------------------------------------------------ 8. withdraw and replace
CUSTOMER_REGION = with_columns(CUSTOMER, Column("Region", TypeRef("nvarchar", length=10), False))
NOT_EMPTY = (
    "ALTER TABLE only allows columns to be added that can contain nulls, or have a DEFAULT definition "
    "specified, or the column being added is an identity or timestamp column, or alternatively if none "
    "of the previous conditions are satisfied the table must be empty to allow addition of this column. "
    "Column 'Region' cannot be added to non-empty table 'Customer' because it does not satisfy these "
    "conditions."
)
REGION_FILLED = (
    "ALTER TABLE [sales].[Customer] ADD [Region] nvarchar(10) NOT NULL "
    "CONSTRAINT [DF_Customer_Region_fill] DEFAULT (N'');\nGO\n"
    "ALTER TABLE [sales].[Customer] DROP CONSTRAINT [DF_Customer_Region_fill];\nGO\n"
)


def test_a_migration_that_fails_in_test_is_withdrawn_and_replaced_and_all_five_end_with_one_model(pipe):
    first = release_1(pipe)
    everywhere(pipe, first)
    pipe.write_objects(CUSTOMER_REGION)
    region = pipe.release("the region of a customer", "customer_region")
    assert region.batches[0].endswith("ALTER TABLE [sales].[Customer] ADD [Region] nvarchar(10) NOT NULL;")

    # dev and sandbox have empty tables: the release passes. The table of test has rows
    for env in ("dev", "sandbox"):
        assert pipe.promote(region, env).code == 0
    test = pipe.dbs["test"]
    test.fail_on(region.batches[0], sql_error(NOT_EMPTY, number=4901))
    failed = pipe.promote(region, "test")
    assert (failed.code, failed.reason) == (21, "BATCH_FAILED")
    assert failed.report["failed_step"] == f"{region.file}#1" and test.model == first.model

    # the fix: the merged migration is withdrawn; its replacement gives the same model in two statements
    replacement = pipe.replace_migration(
        region.file, "customer_region_filled", "the rows that exist get an empty region", REGION_FILLED
    )
    filled = pipe.merge("withdraw and replace the region migration", replacement)
    assert filled.model == region.model

    # dev and sandbox hold the change of the withdrawn migration: the release is recorded, nothing is sent
    for env in ("dev", "sandbox"):
        db = pipe.dbs[env]
        sent = len(db.ddl)
        done = pipe.promote(filled, env)
        assert done.code == 0 and db.ddl[sent:] == [], done.err
        assert migrations(db) == [(first.file, "ok"), (region.file, "ok")]
    # test, preprod and prod never applied it: they run the replacement, and never the withdrawn file
    for env in GATED:
        db = pipe.dbs[env]
        sent = len(db.ddl)
        done = pipe.promote(filled, env)
        assert done.code == 0 and db.ddl[sent:] == filled.batches, done.err
        assert migrations(db) == [(first.file, "ok"), (filled.file, "ok")]
    for env in ENVS:
        db = pipe.dbs[env]
        assert db.model == filled.model and texts(db) == texts(pipe.dbs["dev"])
        assert runs(db)[-1] == ("deploy", "ok", filled.seq)
        assert pipe.drift(filled, env).code == 0


# ------------------------------------------------------------------ 9. an index build outside a transaction
ORDER_INDEXED = with_indexes(ORDER, Index("IX_Order_Customer", False, False, (KeyColumn("CustomerId"),)))
ONLINE = (
    " WITH (ONLINE = ON (WAIT_AT_LOW_PRIORITY (MAX_DURATION = 5 MINUTES, ABORT_AFTER_WAIT = SELF)), "
    "RESUMABLE = ON);"
)


def index_release(pipe: Pipeline) -> Built:
    """A release with one change: an index on [sales].[Order], built online and outside a transaction."""
    pipe.write_objects(ORDER_INDEXED)
    path = pipe.gen("order_customer_index")
    text = path.read_text().replace("-- azsqlcd:mode tx", "-- azsqlcd:mode nontx expected-minutes: 5")
    text = "".join(line for line in text.splitlines(keepends=True) if "allow LONG_LOCK" not in line)
    path.write_text(text.replace("([CustomerId]);", "([CustomerId])" + ONLINE))
    pipe.resum()
    return pipe.merge("an index on the customer of an order", path)


def step_of(db: FakeDatabase, migration: str) -> list[tuple[str, str]]:
    """(kind, status) of the step rows of one migration: one row, whatever happened to it."""
    return [(step["kind"], step["status"]) for step in db.rows("step") if step["migration_id"] == migration]


def test_an_index_build_that_was_killed_before_it_ran_is_marked_not_applied_and_built_again(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("dev",))
    dev = pipe.dbs["dev"]
    index = index_release(pipe)
    dev.kill_on(index.batches[0])
    start = len(dev.batches)

    lost = pipe.deploy(index, "dev")

    assert (lost.code, lost.reason) == (23, "CONNECTION_LOST_NONTX")
    assert "--mark-applied" in lost.err and "--mark-not-applied" in lost.err
    # the marker was committed before the batch, and the batch ran in no transaction: the step row
    # outlives the session, and says that the outcome is not known
    sent = dev.batches[start:]
    marker = next(at for at, batch in enumerate(sent) if "INSERT INTO [azsqlcd].[step]" in batch)
    assert sent[-1] == index.batches[0] and marker == len(sent) - 2
    assert "BEGIN TRANSACTION" not in sent[marker]
    assert step_of(dev, index.file) == [("nontx", "started")]
    assert dev.rows("run")[-1]["status"] == "running" and dev.model == first.model

    # the deploy job is started again: it closes the dead run and sends nothing; a human must look
    again = pipe.deploy(index, "dev")
    assert (again.code, again.reason) == (22, "STEP_UNRESOLVED")
    assert step_of(dev, index.file) == [("nontx", "unknown")] and dev.rows("run")[-1]["status"] == "unknown"
    unknown = dev.rows("run")[-1]["run_id"]
    assert index.batches[0] not in dev.ddl

    # the DBA finds no index. The catalog proves it, so the step can be marked as not applied
    marked = pipe.resolve(index, "dev", "--mark-not-applied", index.file)
    assert marked.code == 0, marked.err
    assert step_of(dev, index.file) == [("nontx", "not_applied")]
    blocked = pipe.deploy(index, "dev")
    assert (blocked.code, blocked.reason) == (22, "RUN_UNKNOWN") and f"--clear-run {unknown}" in blocked.err
    assert pipe.resolve(index, "dev", "--clear-run", str(unknown)).code == 0

    done = pipe.deploy(index, "dev")

    assert done.code == 0, done.err
    assert dev.ddl[-1] == index.batches[0] and dev.ddl.count(index.batches[0]) == 1
    assert dev.model == index.model and step_of(dev, index.file) == [("nontx", "ok")]
    assert [(command, status) for command, status, _ in runs(dev)] == [
        ("deploy", "ok"),
        ("deploy", "unknown"),
        ("resolve", "ok"),
        ("resolve", "ok"),
        ("deploy", "ok"),
    ]
    assert pipe.drift(index, "dev").code == 0


def test_an_index_build_that_finished_before_its_session_died_is_marked_applied_and_not_built_twice(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("prod",))
    prod = pipe.dbs["prod"]
    index = index_release(pipe)
    planned, approved = pipe.plan(index, "prod")
    assert planned.code == 0 and "unit 1 (nontx)" in planned.out
    prod.kill_on(index.batches[0], after=True)

    lost = pipe.deploy(index, "prod", approved)

    assert (lost.code, lost.reason) == (23, "CONNECTION_LOST_NONTX")
    assert prod.model == index.model  # the index is there; the tool does not know it
    assert step_of(prod, index.file) == [("nontx", "started")]
    again = pipe.deploy(index, "prod", approved)  # the deploy job, started again with its plan
    assert (again.code, again.reason) == (22, "STEP_UNRESOLVED")
    unknown = prod.rows("run")[-1]["run_id"]

    # the wrong action is refused: the catalog shows the index
    absent = pipe.resolve(index, "prod", "--mark-not-applied", index.file)
    assert (absent.code, absent.reason) == (22, "NOT_PROVEN_ABSENT")
    assert step_of(prod, index.file) == [("nontx", "unknown")]
    assert pipe.resolve(index, "prod", "--mark-applied", index.file).code == 0
    assert step_of(prod, index.file) == [("nontx", "ok")]
    planned, _ = pipe.plan(index, "prod")
    assert (planned.code, planned.reason) == (22, "RUN_UNKNOWN")
    assert pipe.resolve(index, "prod", "--clear-run", str(unknown)).code == 0

    sent = len(prod.ddl)
    done = pipe.promote(index, "prod")

    assert done.code == 0, done.err
    assert prod.ddl[sent:] == [] and prod.ddl.count(index.batches[0]) == 1  # recorded, not built again
    assert runs(prod)[-1] == ("deploy", "ok", index.seq) and prod.model == index.model
    assert pipe.drift(index, "prod").code == 0
    # the next release changes the same table: no drift stops it, and the table is read back
    pipe.write_objects(with_columns(ORDER_INDEXED, NOTE))
    note = pipe.release("order notes", "order_note")
    assert pipe.promote(note, "prod").code == 0 and prod.model == note.model
    assert pipe.drift(note, "prod").code == 0


def test_when_the_plan_job_comes_first_after_a_killed_index_build_the_next_deploy_closes_the_run(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("prod",))
    prod = pipe.dbs["prod"]
    index = index_release(pipe)
    _, approved = pipe.plan(index, "prod")
    prod.kill_on(index.batches[0])
    assert pipe.deploy(index, "prod", approved).code == 23

    # the stage is started again from the plan job: a plan takes no lock and closes no run
    planned, _ = pipe.plan(index, "prod")
    assert (planned.code, planned.reason) == (22, "STEP_UNRESOLVED")
    assert "--mark-applied or --mark-not-applied" in planned.err
    assert pipe.resolve(index, "prod", "--mark-not-applied", index.file).code == 0

    done = pipe.promote(index, "prod")

    # the step is resolved, so the deploy closes the dead run as failed and builds the index
    assert done.code == 0, done.err
    assert prod.model == index.model and step_of(prod, index.file) == [("nontx", "ok")]
    assert [(run["status"], run["note"]) for run in prod.rows("run") if run["command"] == "deploy"] == [
        ("ok", None),
        ("failed", "reconciled"),
        ("ok", None),
    ]
    assert pipe.drift(index, "prod").code == 0


def test_mark_applied_of_an_index_build_is_refused_when_the_catalog_shows_no_index(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("dev",))
    dev = pipe.dbs["dev"]
    index = index_release(pipe)
    dev.kill_on(index.batches[0])
    assert pipe.deploy(index, "dev").code == 23
    assert pipe.deploy(index, "dev").reason == "STEP_UNRESOLVED" and dev.model == first.model

    marked = pipe.resolve(index, "dev", "--mark-applied", index.file)

    assert (marked.code, marked.reason) == (22, "RESOLVE_NOT_APPLICABLE")
    assert "--mark-not-applied" in marked.err  # the action that is true for this database
    assert step_of(dev, index.file) == [("nontx", "unknown")]


# ------------------------------------------------------------------ 10. two deploys at once
def test_a_second_deploy_at_the_same_time_gets_exit_25_and_sends_no_ddl(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("dev",))
    dev = pipe.dbs["dev"]
    pipe.write_objects(ORDER_NOTE)
    note = pipe.release("order notes", "order_note")
    second: dict[str, Outcome] = {}
    seen: dict[str, object] = {}

    def other_jobs() -> None:
        """What other jobs do while the first deploy is in its transaction, on sessions of their own."""
        sent, state = len(dev.ddl), (dev.rows("run"), dev.rows("step"), dev.rows("object"))
        second["deploy"] = pipe.deploy(note, "dev", alone=False)
        second["plan"] = pipe.plan(note, "dev", alone=False)[0]
        second["resolve"] = pipe.resolve(note, "dev", "--clear-run", "1", alone=False)
        seen["ddl"] = dev.ddl[sent:]
        seen["state unchanged"] = (dev.rows("run"), dev.rows("step"), dev.rows("object")) == state
        seen["run of the first deploy"] = dev.rows("run")[-1]["status"]

    dev.before(note.batches[0], other_jobs)  # the first deploy holds the lock and is about to alter the table
    sent = len(dev.ddl)

    done = pipe.deploy(note, "dev")

    assert (second["deploy"].code, second["deploy"].reason) == (25, "LOCK_NOT_GRANTED")
    assert second["deploy"].report["run_id"] is None and "Nothing was sent" in second["deploy"].err
    assert (second["plan"].code, second["plan"].reason) == (25, "RUN_LIVE")
    assert (second["resolve"].code, second["resolve"].reason) == (25, "LOCK_NOT_GRANTED")
    assert seen == {"ddl": [], "state unchanged": True, "run of the first deploy": "running"}
    # the first deploy is not disturbed, and the release is applied once
    assert done.code == 0, done.err
    assert dev.ddl[sent:] == [*note.batches, REFRESH_VIEW] and dev.model == note.model
    assert runs(dev) == [("deploy", "ok", first.seq), ("deploy", "ok", note.seq)]
    assert dev.lock_holder is None
    # the job that lost starts again when the other ended: nothing is left to do
    before = frozen(dev)
    assert pipe.deploy(note, "dev").code == 0 and frozen(dev) == before


# ------------------------------------------------------------------ 11. onboarding
# 2 schemas, 8 tables with keys and indexes, and a sequence: the 11 table-class objects
LEGACY_TABLES = """
CREATE SCHEMA [ref];

CREATE SCHEMA [sales];

CREATE TABLE [ref].[Country] ([CountryId] int NOT NULL, [Name] nvarchar(100) NOT NULL,
    CONSTRAINT [PK_Country] PRIMARY KEY CLUSTERED ([CountryId]),
    CONSTRAINT [UQ_Country_Name] UNIQUE NONCLUSTERED ([Name]));

CREATE TABLE [ref].[Currency] ([CurrencyId] int NOT NULL, [Code] char(3) NOT NULL,
    CONSTRAINT [PK_Currency] PRIMARY KEY CLUSTERED ([CurrencyId]));

CREATE TABLE [sales].[Customer] ([CustomerId] int NOT NULL, [Name] nvarchar(100) NOT NULL,
    [CountryId] int NOT NULL, CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([CustomerId]),
    CONSTRAINT [FK_Customer_Country] FOREIGN KEY ([CountryId]) REFERENCES [ref].[Country] ([CountryId]));

CREATE NONCLUSTERED INDEX [IX_Customer_CountryId] ON [sales].[Customer] ([CountryId]);

CREATE TABLE [sales].[Product] ([ProductId] int NOT NULL, [Name] nvarchar(100) NOT NULL,
    [Price] decimal(12, 2) NOT NULL, CONSTRAINT [PK_Product] PRIMARY KEY CLUSTERED ([ProductId]),
    CONSTRAINT [CK_Product_Price] CHECK ([Price] >= (0)));

CREATE TABLE [sales].[Order] ([OrderId] int NOT NULL, [CustomerId] int NOT NULL,
    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT ((0)), [Reference] nvarchar(50) NULL,
    [PlacedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Order_PlacedUtc] DEFAULT (sysutcdatetime()),
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]),
    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]));

CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);

CREATE NONCLUSTERED INDEX [IX_Order_CustomerId] ON [sales].[Order] ([CustomerId]) INCLUDE ([Status]);

CREATE TABLE [sales].[OrderLine] ([OrderLineId] int IDENTITY(1, 1) NOT NULL, [OrderId] int NOT NULL,
    [ProductId] int NOT NULL, [Quantity] int NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderLineId]),
    CONSTRAINT [FK_OrderLine_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]),
    CONSTRAINT [FK_OrderLine_Product] FOREIGN KEY ([ProductId]) REFERENCES [sales].[Product] ([ProductId]));

CREATE NONCLUSTERED INDEX [IX_OrderLine_OrderId] ON [sales].[OrderLine] ([OrderId]);

CREATE TABLE [sales].[Invoice] ([InvoiceId] int NOT NULL, [OrderId] int NOT NULL,
    [Amount] decimal(12, 2) NOT NULL, [CurrencyId] int NOT NULL,
    CONSTRAINT [PK_Invoice] PRIMARY KEY CLUSTERED ([InvoiceId]),
    CONSTRAINT [FK_Invoice_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]),
    CONSTRAINT [FK_Invoice_Currency] FOREIGN KEY ([CurrencyId]) REFERENCES [ref].[Currency] ([CurrencyId]));

CREATE TABLE [sales].[Payment] ([PaymentId] int NOT NULL, [InvoiceId] int NOT NULL,
    [Amount] decimal(12, 2) NOT NULL, CONSTRAINT [PK_Payment] PRIMARY KEY CLUSTERED ([PaymentId]),
    CONSTRAINT [FK_Payment_Invoice] FOREIGN KEY ([InvoiceId]) REFERENCES [sales].[Invoice] ([InvoiceId]));

CREATE NONCLUSTERED INDEX [IX_Payment_InvoiceId] ON [sales].[Payment] ([InvoiceId]);

CREATE SEQUENCE [sales].[OrderNumber] AS bigint START WITH 1000 INCREMENT BY 1 MINVALUE 1000
    MAXVALUE 9223372036854775807 NO CYCLE CACHE 50;
"""
# 6 views, 4 functions, 8 procedures and a trigger, as DBAs wrote them over the years: CREATE and
# not CREATE OR ALTER, names with and with no brackets, PROC, lower-case keywords. In an order in
# which they can be created.
LEGACY_MODULES = {
    "VIEW:[sales].[vw_OpenOrders]": "CREATE VIEW sales.vw_OpenOrders AS\n"
    "SELECT [OrderId], [CustomerId] FROM [sales].[Order] WHERE [Status] = 0;\n",
    "VIEW:[sales].[vw_CustomerOrders]": "create view [sales].[vw_CustomerOrders]\nas\n"
    "select c.[Name], o.[OrderId] from [sales].[Customer] c\n"
    "join [sales].[Order] o on o.[CustomerId] = c.[CustomerId]\n",
    "VIEW:[sales].[vw_OrderTotals]": "CREATE VIEW [sales].[vw_OrderTotals] AS\n"
    "SELECT l.[OrderId], SUM(l.[Quantity] * p.[Price]) AS [Total]\n"
    "FROM [sales].[OrderLine] l JOIN [sales].[Product] p ON p.[ProductId] = l.[ProductId]\n"
    "GROUP BY l.[OrderId];\n",
    "VIEW:[sales].[vw_Products]": "CREATE VIEW [sales].[vw_Products] AS\n"
    "SELECT [ProductId], [Name], [Price] FROM [sales].[Product];\n",
    "VIEW:[sales].[vw_UnpaidInvoices]": "CREATE VIEW [sales].[vw_UnpaidInvoices] AS\n"
    "SELECT i.[InvoiceId], i.[Amount] FROM [sales].[Invoice] i\n"
    "WHERE NOT EXISTS (SELECT 1 FROM [sales].[Payment] p WHERE p.[InvoiceId] = i.[InvoiceId]);\n",
    "VIEW:[ref].[vw_Countries]": "CREATE VIEW ref.vw_Countries AS\n"
    "SELECT [CountryId], [Name] FROM [ref].[Country];\n",
    "FUNCTION:[sales].[fn_OrderTotal]": "CREATE FUNCTION [sales].[fn_OrderTotal] (@OrderId int)\n"
    "RETURNS decimal(12, 2)\nAS\nBEGIN\n"
    "    RETURN (SELECT [Total] FROM [sales].[vw_OrderTotals] WHERE [OrderId] = @OrderId);\nEND;\n",
    "FUNCTION:[sales].[fn_OpenOrderCount]": "CREATE FUNCTION sales.fn_OpenOrderCount ()\n"
    "RETURNS int\nAS\nBEGIN\n    RETURN (SELECT COUNT(*) FROM [sales].[Order] WHERE [Status] = 0);\nEND;\n",
    "FUNCTION:[sales].[tvf_CustomerOrders]": "CREATE FUNCTION [sales].[tvf_CustomerOrders]\n"
    "(@CustomerId int)\nRETURNS TABLE\nAS\n"
    "RETURN (SELECT [OrderId], [Status] FROM [sales].[Order] WHERE [CustomerId] = @CustomerId);\n",
    "FUNCTION:[ref].[fn_CountryName]": "CREATE FUNCTION [ref].[fn_CountryName] (@CountryId int)\n"
    "RETURNS nvarchar(100)\nAS\nBEGIN\n"
    "    RETURN (SELECT [Name] FROM [ref].[Country] WHERE [CountryId] = @CountryId);\nEND;\n",
    PROCEDURE_KEY: PROCEDURE[1].replace("CREATE OR ALTER", "CREATE"),
    "PROCEDURE:[sales].[usp_AddOrder]": "CREATE PROC sales.usp_AddOrder @CustomerId int AS\n"
    "INSERT INTO [sales].[Order] ([OrderId], [CustomerId])\n"
    "VALUES (NEXT VALUE FOR [sales].[OrderNumber], @CustomerId);\n",
    "PROCEDURE:[sales].[usp_CloseOrder]": "CREATE PROCEDURE [sales].[usp_CloseOrder] @OrderId int AS\n"
    "UPDATE [sales].[Order] SET [Status] = 2 WHERE [OrderId] = @OrderId;\n",
    "PROCEDURE:[sales].[usp_CustomerList]": "CREATE PROCEDURE [sales].[usp_CustomerList] AS\n"
    "SELECT [CustomerId], [Name] FROM [sales].[Customer];\n",
    "PROCEDURE:[sales].[usp_ProductList]": "CREATE PROCEDURE [sales].[usp_ProductList] AS\n"
    "SELECT [ProductId], [Name], [Price] FROM [sales].[vw_Products];\n",
    "PROCEDURE:[sales].[usp_InvoiceList]": "CREATE PROCEDURE [sales].[usp_InvoiceList] AS\n"
    "SELECT [InvoiceId], [Amount] FROM [sales].[vw_UnpaidInvoices];\n",
    "PROCEDURE:[sales].[usp_PaymentList]": "CREATE PROCEDURE [sales].[usp_PaymentList] @InvoiceId int AS\n"
    "SELECT [PaymentId], [Amount] FROM [sales].[Payment] WHERE [InvoiceId] = @InvoiceId;\n",
    "PROCEDURE:[ref].[usp_Countries]": "CREATE PROCEDURE [ref].[usp_Countries] AS\n"
    "SELECT [CountryId], [Name] FROM [ref].[vw_Countries];\n",
    "TRIGGER:[sales].[tr_Order_Touch]": "CREATE TRIGGER [sales].[tr_Order_Touch] ON [sales].[Order]\n"
    "AFTER UPDATE AS\nBEGIN\n    SET NOCOUNT ON;\nEND;\n",
}
CLOSE_KEY = "PROCEDURE:[sales].[usp_CloseOrder]"
CLOSE_PATH = "schema/procedures/sales.usp_CloseOrder.sql"
FIND_IN_DEV = LEGACY_MODULES[PROCEDURE_KEY].replace("[OrderId] FROM", "[OrderId], [Status] FROM")
ACK_PATH = "onboarding/dev/overwrite-ack.toml"
PAYMENT_INDEX = "CREATE NONCLUSTERED INDEX [IX_Payment_InvoiceId] ON [sales].[Payment] ([InvoiceId]);"


def legacy(db: FakeDatabase, changed: dict[str, str] | None = None) -> None:
    """A database that was made by hand: the tool has recorded nothing of it. changed: module key ->
    another text for that module."""
    modules = LEGACY_MODULES | (changed or {})
    assert set(modules) == set(LEGACY_MODULES)
    db.out_of_band(*LEGACY_TABLES.strip().split("\n\n"), *modules.values())


def test_a_database_of_30_objects_is_exported_baselined_in_two_environments_and_then_released(
    pipe, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)  # baseline writes onboarding/<env>/ under the working directory
    pipe.write("azsqlcd.toml", toml(table_model=False))
    pipe.push("a repository with no object file")
    prod, dev = pipe.database("prod"), pipe.database("dev")
    legacy(prod)
    # dev differs: a procedure with another text, an index that is missing, and an index of its own
    legacy(dev, {PROCEDURE_KEY: FIND_IN_DEV})
    dev.out_of_band(
        "DROP INDEX [IX_Payment_InvoiceId] ON [sales].[Payment];",
        "CREATE NONCLUSTERED INDEX [IX_Order_Reference] ON [sales].[Order] ([Reference]);",
    )
    assert len(prod.model) + len(prod.modules) == 30

    # export from prod, the reference: one file for each object, and nothing changes in prod
    exported, before = tmp_path / "exported", frozen(prod)
    where = ("--root", str(pipe.repo), "--env", "prod", "--target", "sales-prod")
    done = pipe.tool("export", *where, db=prod, out=exported)
    assert done.code == 0 and "30 object file(s); 0 object(s) stay unmanaged" in done.out, done.err
    assert frozen(prod) == before and prod.batches != []
    files = {
        path.relative_to(exported).as_posix(): path.read_text() for path in exported.glob("schema/*/*.sql")
    }  # a repository path has forward slashes on every operating system
    assert len(files) == 30
    assert tables.head_model({path: text.encode() for path, text in files.items()}) == prod.model
    assert files[CLOSE_PATH] == LEGACY_MODULES[CLOSE_KEY].replace("CREATE", "CREATE OR ALTER")

    # one pull request: the files, the table model on, and a chain that starts with a baseline
    shutil.copytree(exported, pipe.repo, dirs_exist_ok=True)
    pipe.write("azsqlcd.toml", toml())
    pipe.write(chain.SUM_PATH, "azsqlcd-sum 1\nbaseline\n")
    onboard = pipe.merge("onboard the sales database")
    early = pipe.promote(onboard, "prod")
    assert (early.code, early.reason) == (22, "BASELINE_REQUIRED")

    # prod: every object is recorded as it is; no statement is sent, and the first deploy has nothing to do
    recorded = pipe.baseline(onboard, "prod")
    assert recorded.code == 0, recorded.err
    assert prod.ddl == [] and frozen(prod)[:2] == before[:2]
    statuses = dict(rows(prod, *names.KINDS))
    assert len(statuses) == 30 and set(statuses.values()) == {"managed"}
    checksums = {row["object_key"]: row["source_sha256"] for row in prod.rows("object")}
    assert all((checksums[key] is not None) == (key in LEGACY_MODULES) for key in checksums)
    assert runs(prod) == [("baseline", "ok", 0)]
    assert pipe.promote(onboard, "prod").code == 0 and prod.ddl == []
    assert pipe.drift(onboard, "prod").code == 0

    # dev, report only: the procedure needs an acknowledgement, one table differs, one index is extra
    report = pipe.baseline(onboard, "dev", report_only=True)
    assert report.code == 0 and "modules: 1 differs, 18 equal" in report.out
    assert "table-class objects: 11 compared, 1 differ or are missing" in report.out
    page = (tmp_path / "onboarding" / "dev" / "baseline-diff.md").read_text()
    assert (
        f"| {PROCEDURE_KEY} | differs | live text: onboarding/dev/modules/sales.usp_FindOrder.sql |" in page
    )
    assert "| TABLE:[sales].[Payment] | differs | index [IX_Payment_InvoiceId] |" in page
    assert "index [IX_Order_Reference] exists only here: an unmanaged sub-object" in page
    live = (tmp_path / "onboarding" / "dev" / "modules" / "sales.usp_FindOrder.sql").read_text()
    assert "SELECT [OrderId], [Status] FROM" in live  # the text of dev, for the review of the overwrite
    assert dev.rows("run") == [] and dev.ddl == []

    # dev, to record: refused for the procedure until a pull request acknowledges the overwrite
    refused = pipe.baseline(onboard, "dev")
    assert (refused.code, refused.reason) == (22, "BASELINE_ACK_REQUIRED") and PROCEDURE_KEY in refused.err
    pipe.write(ACK_PATH, f'modules = ["{PROCEDURE_KEY}"]\n')
    acknowledged = pipe.merge("dev: the procedure of the files replaces the one of dev")
    # then refused for the table that lacks an index of the model; the tool writes no alignment DDL
    refused = pipe.baseline(acknowledged, "dev")
    assert (refused.code, refused.reason) == (22, "READBACK_MISMATCH")
    assert "TABLE:[sales].[Payment]" in refused.err and "ix_payment_invoiceid" in refused.err
    assert dev.rows("run") == [] and dev.rows("object") == [] and dev.ddl == []
    dev.out_of_band(PAYMENT_INDEX)  # a DBA aligns dev by hand
    recorded = pipe.baseline(acknowledged, "dev")
    assert recorded.code == 0, recorded.err
    assert dev.ddl == [] and runs(dev) == [("baseline", "ok", 0)]
    # the procedure is recorded with no source; the index of dev alone stays, unmanaged
    unknown = [row["object_key"] for row in dev.rows("object") if row["source_sha256"] is None]
    assert [key for key in unknown if key in LEGACY_MODULES] == [PROCEDURE_KEY]

    # the first deploy to dev sends the one module that was acknowledged, and nothing else
    planned, _ = pipe.plan(acknowledged, "dev")
    assert f"OVERWRITE_MODULE {PROCEDURE_KEY}" in planned.out
    done = pipe.promote(acknowledged, "dev")
    assert done.code == 0 and dev.ddl == [PROCEDURE[1]], done.err
    assert pipe.promote(acknowledged, "prod").code == 0 and prod.ddl == []

    # the first release after the baseline: a column and a changed procedure. Only they are sent
    order = onboard.model[ORDER.key]
    pipe.write_objects(with_columns(order, NOTE))
    pipe.write(CLOSE_PATH, files[CLOSE_PATH].replace("[Status] = 2", "[Status] = 3"))
    changed = pipe.release("order notes, and another status of a closed order", "order_note")
    for env in ("dev", "prod"):
        db = pipe.dbs[env]
        sent = len(db.ddl)
        done = pipe.promote(changed, env)
        assert done.code == 0, done.err
        assert db.ddl[sent:] == [
            *changed.batches,
            pipe.read(CLOSE_PATH),
            # the unchanged views and the table-valued function on the altered table (A12)
            "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[tvf_CustomerOrders]';",
            "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[vw_CustomerOrders]';",
            "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[vw_OpenOrders]';",
        ]
        assert len(rows(db, *names.KINDS)) == 30 and pipe.drift(changed, env).code == 0
    assert prod.model == changed.model
    # dev is the model too, but for the index of its own, which no file holds
    extra = Index("IX_Order_Reference", False, False, (KeyColumn("Reference"),))
    assert dev.model[ORDER.key] == with_indexes(with_columns(order, NOTE), extra)
    others = [key for key in prod.model if key != ORDER.key]
    assert [dev.model[key] for key in others] == [prod.model[key] for key in others]
    # 17 modules are still the text of the DBAs in both; two hold the text of their files
    same = {key for key in LEGACY_MODULES if texts(dev)[key] == texts(prod)[key] == LEGACY_MODULES[key]}
    assert set(LEGACY_MODULES) - same == {PROCEDURE_KEY, CLOSE_KEY}


# ------------------------------------------------------------------ 12. letter case
FIND_BY_HAND = "CREATE PROCEDURE [sales].[USP_FINDORDER] @Reference nvarchar(50) AS\nSELECT 1 AS [OrderId];\n"
CATALOG_KEY = "PROCEDURE:[sales].[USP_FINDORDER]"


def test_a_module_recorded_in_another_letter_case_and_a_table_in_another_case_keep_one_row_each(pipe):
    pipe.write_objects(SALES, CUSTOMER, ORDER)
    pipe.write(*VIEW)
    pipe.write(*FUNCTION)
    first = pipe.release("sales schema", "create_sales")
    everywhere(pipe, first, envs=("dev", "prod"))
    dev, prod = pipe.dbs["dev"], pipe.dbs["prod"]
    # in prod a DBA made the procedure by hand, with the name in capitals
    prod.out_of_band(FIND_BY_HAND)

    # the release: the procedure as a file, and a migration whose author wrote the table in capitals
    pipe.write(*PROCEDURE)
    pipe.write_objects(ORDER_NOTE)
    migration = pipe.gen("order_note")
    migration.write_text(migration.read_text().replace("[sales].[Order]", "[SALES].[ORDER]"))
    pipe.resum()
    note = pipe.merge("order notes and the procedure", migration)
    assert len(note.batches) == 1 and "ALTER TABLE [SALES].[ORDER] ADD [Note]" in note.batches[0]
    assert pipe.promote(note, "dev").code == 0
    assert rows(dev, "TABLE", "PROCEDURE") == [
        (PROCEDURE_KEY, "managed"),
        (CUSTOMER.key, "managed"),
        (ORDER.key, "managed"),
    ]
    assert dev.model == note.model and pipe.drift(note, "dev").code == 0

    # prod: the name is taken by an object that the tool did not make
    taken = pipe.promote(note, "prod")
    assert (taken.code, taken.reason) == (22, "NAME_COLLISION") and "--adopt-module" in taken.err
    # it is adopted under the name that the catalog holds, which is not the letter case of the file
    inexact = pipe.resolve(note, "prod", "--adopt-module", PROCEDURE_KEY)
    assert (inexact.code, inexact.reason) == (22, "RESOLVE_NOT_APPLICABLE")
    assert pipe.resolve(note, "prod", "--adopt-module", CATALOG_KEY).code == 0

    sent = len(prod.ddl)
    done = pipe.promote(note, "prod")

    assert done.code == 0, done.err
    assert prod.ddl[sent:] == [*note.batches, PROCEDURE[1], REFRESH_VIEW]
    # one row for the procedure and one for the table: the keys that were recorded first
    assert rows(prod, "TABLE", "PROCEDURE") == [
        (CATALOG_KEY, "managed"),
        (CUSTOMER.key, "managed"),
        (ORDER.key, "managed"),
    ]
    assert prod.model == note.model and sorted(prod.model) == sorted(dev.model)
    assert prod.module(PROCEDURE_KEY).sent == PROCEDURE[1]
    assert pipe.drift(note, "prod").code == 0
    before = frozen(prod)
    assert pipe.promote(note, "prod").code == 0 and frozen(prod) == before  # nothing is pending

    # later releases find the row: a change is one ALTER, a tombstone is one DROP
    pipe.write(*FIND_NOTE)
    reads = pipe.release("the procedure reads the note")
    sent = len(prod.ddl)
    assert pipe.promote(reads, "prod").code == 0 and prod.ddl[sent:] == [FIND_NOTE[1]]
    assert rows(prod, "PROCEDURE") == [(CATALOG_KEY, "managed")] and pipe.drift(reads, "prod").code == 0
    pipe.delete(PROCEDURE[0])
    pipe.tombstone(PROCEDURE_KEY)
    gone = pipe.release("the procedure has no caller")
    sent = len(prod.ddl)
    assert pipe.promote(gone, "prod").code == 0 and prod.ddl[sent:] == [DROP_PROCEDURE]
    assert rows(prod, "PROCEDURE") == [(CATALOG_KEY, "dropped")] and prod.module(PROCEDURE_KEY) is None
    assert pipe.drift(gone, "prod").code == 0


CAPITALS = "schema/procedures/SALES.USP_FINDORDER.sql"


def test_a_module_file_that_is_renamed_by_letter_case_passes_the_checks_and_the_plan_alike(pipe):
    first = release_1(pipe)
    everywhere(pipe, first, envs=("dev",))
    dev = pipe.dbs["dev"]
    # two renames, so that a file system that ignores case renames the file too
    pipe.git("mv", PROCEDURE[0], PROCEDURE[0] + ".tmp")
    pipe.git("mv", PROCEDURE[0] + ".tmp", CAPITALS)
    pipe.write(CAPITALS, FIND_TOP[1].replace("[sales].[usp_FindOrder]", "[SALES].[USP_FINDORDER]"))
    _, verify = pipe.checks()
    if verify.code != 0:  # is: TMB001, the file of the old key is removed
        assert "error TMB001" in verify.out
        pipe.tombstone(PROCEDURE_KEY)  # what TMB001 asks for
    renamed = pipe.merge("the procedure in capitals")

    done = pipe.promote(renamed, "dev")

    assert done.code == 0, done.err  # is: 22 TOMBSTONE_CONFLICT
    assert rows(dev, "PROCEDURE") == [(PROCEDURE_KEY, "managed")] and len(dev.modules) == 3
    assert pipe.drift(renamed, "dev").code == 0


# ------------------------------------------------------------------ further stories
def test_a_new_database_takes_the_newest_release_in_one_unit_and_never_runs_a_withdrawn_migration(pipe):
    first = release_1(pipe)
    pipe.write_objects(CUSTOMER_REGION)
    region = pipe.release("the region of a customer", "customer_region")
    replacement = pipe.replace_migration(
        region.file, "customer_region_filled", "the rows that exist get an empty region", REGION_FILLED
    )
    filled = pipe.merge("withdraw and replace the region migration", replacement)
    pipe.write(*FIND_TOP)
    top = pipe.release("the first match only")
    everywhere(pipe, first, region, filled, top, envs=("dev",))
    dev = pipe.dbs["dev"]

    # a target that was added to azsqlcd.toml later: an empty database after setup-sql
    for env in ("sandbox", "prod"):
        db = pipe.database(env)
        sent = len(db.batches)
        done = pipe.promote(top, env)

        # no release to be caught up from: the whole chain and every module, in one transaction (A7)
        assert done.code == 0, done.err
        expected = [*first.batches, *filled.batches, FUNCTION[1], VIEW[1], FIND_TOP[1]]
        assert committed(db, sent) == [expected] and region.batches[0] not in db.batches
        assert migrations(db) == [(first.file, "ok"), (filled.file, "ok")]
        assert runs(db) == [("deploy", "ok", top.seq)]
        assert (db.model, texts(db)) == (dev.model, texts(dev))
        assert pipe.drift(top, env).code == 0


def test_a_module_only_release_is_not_skipped_before_an_index_build_and_the_refusal_names_it(pipe):
    first = release_1(pipe)
    pipe.write(*FIND_TOP)
    top = pipe.release("the first match only")  # modules only
    index = index_release(pipe)
    everywhere(pipe, first, envs=("prod",))
    prod = pipe.dbs["prod"]
    before = frozen(prod)

    # an index build is the only change of its run (A8), and here the procedure is pending too
    refused = pipe.promote(index, "prod")

    assert (refused.code, refused.reason) == (22, "NONTX_NOT_ALONE")
    assert PROCEDURE_KEY in refused.err and f"promote r{top.seq} first" in refused.err
    assert frozen(prod) == before
    assert pipe.promote(top, "prod").code == 0
    sent = len(prod.ddl)
    assert pipe.promote(index, "prod").code == 0 and prod.ddl[sent:] == index.batches
    assert prod.model == index.model and prod.module(PROCEDURE_KEY).sent == FIND_TOP[1]


def index_history(pipe: Pipeline) -> tuple[Built, Built, Built]:
    """Three releases in dev: the sales schema, an index build outside a transaction, a column."""
    first = release_1(pipe)
    index = index_release(pipe)
    pipe.write_objects(with_columns(ORDER_INDEXED, NOTE))
    note = pipe.release("order notes", "order_note")
    everywhere(pipe, first, index, note, envs=("dev",))
    return first, index, note


def test_a_new_database_after_an_index_build_in_the_chain_takes_the_releases_one_by_one(pipe):
    first, index, note = index_history(pipe)
    dev, sandbox = pipe.dbs["dev"], pipe.database("sandbox")

    # the chain holds a non-transactional migration, so it cannot run in one unit of work
    for built in (note, index):
        refused = pipe.promote(built, "sandbox")
        assert (refused.code, refused.reason) == (22, "NONTX_NOT_ALONE") and index.file in refused.err
    assert sandbox.ddl == [] and sandbox.rows("run") == []

    # from the release before the index build on, each release in its turn
    for built in (first, index, note):
        done = pipe.promote(built, "sandbox")
        assert done.code == 0, done.err
    assert (sandbox.model, texts(sandbox)) == (dev.model, texts(dev))
    assert runs(sandbox) == runs(dev) and migrations(sandbox) == migrations(dev)
    assert pipe.drift(note, "sandbox").code == 0


def test_the_refusal_for_a_new_database_after_an_index_build_names_the_release_to_start_from(pipe):
    first, index, note = index_history(pipe)
    pipe.database("sandbox")

    refused = pipe.promote(note, "sandbox")

    assert (refused.code, refused.reason) == (22, "NONTX_NOT_ALONE")
    assert f"promote r{first.seq} first" in refused.err  # is: no release is named


CUSTOMER_EMAIL = with_columns(CUSTOMER, Column("Email", TypeRef("nvarchar", length=200), True))


def region_withdrawn(pipe: Pipeline) -> tuple[Built, Built]:
    """dev applied a migration that failed in test. Then the team gave the change up: the chain line
    is withdrawn with no replacement, and the table file is as before. (the release of the
    migration, the release of its withdrawal)."""
    first = release_1(pipe)
    everywhere(pipe, first, envs=("dev", "test"))
    pipe.write_objects(CUSTOMER_REGION)
    region = pipe.release("the region of a customer", "customer_region")
    assert pipe.promote(region, "dev").code == 0
    pipe.dbs["test"].fail_on(region.batches[0], sql_error(NOT_EMPTY, number=4901))
    assert pipe.promote(region, "test").code == 21
    pipe.write_objects(CUSTOMER)
    lines = pipe.read(chain.SUM_PATH).splitlines()
    assert lines[-1].startswith(region.file)
    pipe.write(chain.SUM_PATH, "\n".join([*lines[:-1], lines[-1] + " withdrawn"]) + "\n")
    # verify passes: the table files hold the model without the migration (WDR004)
    return region, pipe.merge("no region of a customer")


def test_a_database_that_ran_a_migration_that_was_withdrawn_alone_stops_the_next_change_of_its_table(pipe):
    _, withdrawal = region_withdrawn(pipe)
    dev, test = pipe.dbs["dev"], pipe.dbs["test"]
    for env in ("dev", "test"):  # the withdrawal is recorded in both, and nothing is sent
        sent = len(pipe.dbs[env].ddl)
        assert pipe.promote(withdrawal, env).code == 0 and pipe.dbs[env].ddl[sent:] == []
    assert test.model == withdrawal.model != dev.model  # dev holds a column that no file holds

    pipe.write_objects(CUSTOMER_EMAIL)
    email = pipe.release("the e-mail of a customer", "customer_email")
    assert pipe.promote(email, "test").code == 0
    before = catalog(dev)
    failed = pipe.promote(email, "dev")

    # the read-back in the transaction is the gate: the table is not the table of the files
    assert (failed.code, failed.reason) == (21, "READBACK_MISMATCH")
    assert CUSTOMER.key in failed.err and "columns[region]" in failed.err
    assert catalog(dev) == before

    # the repair: a DBA drops the column by hand. That is drift on a table that the release changes
    dev.out_of_band("ALTER TABLE [sales].[Customer] DROP COLUMN [Region];")
    touched = pipe.promote(email, "dev")
    assert (touched.code, touched.reason) == (22, "DRIFT_TOUCHED")
    # it is accepted against the release that the database records, not against the release that waits
    early = pipe.resolve(email, "dev", "--accept-drift", CUSTOMER.key)
    assert (early.code, early.reason) == (22, "READBACK_MISMATCH")
    assert pipe.resolve(withdrawal, "dev", "--accept-drift", CUSTOMER.key).code == 0
    done = pipe.promote(email, "dev")
    assert done.code == 0, done.err
    assert dev.model == test.model == email.model and pipe.drift(email, "dev").code == 0


@pytest.mark.xfail(
    strict=True,
    reason="a migration is withdrawn with no replacement (the way out of WDR004) after dev applied it. "
    "The runbook and known-gaps say that `drift` reports the database that ran it. It does not: drift "
    "compares the catalog with the recorded capture, and the capture holds the column (onboard.drift). "
    "The plan and the deploy of the withdrawal record the release with no note (plan.pending_work "
    "knows the applied step of a withdrawn line that nothing replaces). The first sign is the next "
    "release that changes the table: 21 READBACK_MISMATCH, in that environment only",
)
def test_the_tool_tells_that_a_database_holds_the_change_of_a_migration_that_was_withdrawn_alone(pipe):
    region, withdrawal = region_withdrawn(pipe)
    dev = pipe.dbs["dev"]

    planned, _ = pipe.plan(withdrawal, "dev")
    done = pipe.deploy(withdrawal, "dev")
    drift = pipe.drift(withdrawal, "dev")

    assert (planned.code, done.code) == (0, 0) and dev.model != withdrawal.model
    # is: a plan with no note, a deploy that records the release, and 'no drift'
    assert drift.code == 30 or region.file in planned.out + done.out
