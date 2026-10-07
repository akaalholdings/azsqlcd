"""Live scenario for the table model of azsqlcd (table_model = true), on a disposable database.

Owner-run. This script connects to a database and writes to it. It never runs in pytest or CI.
The guards are those of scripts/live_spike.py. Run it BEFORE scripts/live_acceptance.py when both
are wanted: each of the two drops the four state tables of schema [azsqlcd] at its start.

    az login
    uv run --extra db python scripts/live_tables.py --server S --database D
        --confirm-disposable-database D [--out DIR]                       (one line)

What it does, with the real modules (gen.generate, gen.verify, release.build, plan.compute_plan
and runner.deploy with tables.Hooks) and the real catalog:
  1. Drops the objects of schema [azsqlcd_tm] and the four state tables, then sends setup-sql.
  2. Release r2: object files for a schema, two tables with a foreign key, defaults, a CHECK, a
     computed column and an index, and a view. `gen` writes the migration. Plan, deploy with the
     expected plan, read-back against the catalog, object rows. A second deploy sends nothing.
  3. Release r3: two new columns and two new indexes (one filtered). `gen`, plan, deploy.
  4. Drift: a column that is added by hand is reported by the plan of r3 (drift = "report").
  5. Release r4 holds that column: the plan refuses (DRIFT_TOUCHED). resolve --mark-applied with
     a read-back records the migration; the release is then recorded.
  6. Release r5 drops a column that the view uses, and changes the view in the same release.
     Release r6 drops a column that a managed procedure still uses: DEPENDANT_BROKEN, rolled back.
Each check is printed as pass or fail with the exit code and the reason code that the tool gave.

Output: a table on stdout and <out>/tables_report.json. The schema [azsqlcd_tm] and the state
stay in the database.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import live_acceptance as acc
import live_spike as live

from azsqlcd import (
    __version__,
    chain,
    emit,
    gen,
    lex,
    names,
    parse,
    plan,
    release,
    runner,
    state,
    tables,
)
from azsqlcd import session as db
from azsqlcd.config import Config, load_config
from azsqlcd.errors import ToolError
from azsqlcd.plan import Plan
from azsqlcd.release import Bundle
from azsqlcd.runner import RunError
from azsqlcd.session import AzureCliTokenProvider, Session, TokenProvider
from azsqlcd.sqlerrors import SqlError

SCHEMA = "azsqlcd_tm"
ENV, TARGET = acc.ENV, acc.TARGET
REPORT_NAME = "tables_report.json"
CUSTOMER = names.qualified(SCHEMA, "Customer")
ORDER = names.qualified(SCHEMA, "Order")
VIEW = names.qualified(SCHEMA, "vw_OpenOrders")
ORDER_KEY = names.object_key("TABLE", SCHEMA, "Order")
CUSTOMER_KEY = names.object_key("TABLE", SCHEMA, "Customer")
VIEW_KEY = names.object_key("VIEW", SCHEMA, "vw_OpenOrders")
VIEW_PATH = f"schema/views/{SCHEMA}.vw_OpenOrders.sql"
PROCEDURE_KEY = names.object_key("PROCEDURE", SCHEMA, "usp_OrderRegions")
PROCEDURE_PATH = f"schema/procedures/{SCHEMA}.usp_OrderRegions.sql"

SCHEMA_FILE = f"CREATE SCHEMA {names.quote(SCHEMA)};\n"
CUSTOMER_FILE = f"""CREATE TABLE {CUSTOMER} (
    [CustomerId] int NOT NULL,
    [Name] nvarchar(100) NOT NULL,
    [Email] varchar(200) NULL,
    [CreatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Customer_CreatedUtc] DEFAULT (sysutcdatetime()),
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([CustomerId]),
    CONSTRAINT [UQ_Customer_Email] UNIQUE NONCLUSTERED ([Email])
);
"""
ORDER_COLUMNS = """    [OrderId] int NOT NULL,
    [CustomerId] int NOT NULL,
    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT ((0)),
    [Amount] decimal(18, 2) NOT NULL,
    [Note] nvarchar(50) NULL,
    [Total] AS ([Amount] * (2)),
"""
ORDER_CONSTRAINTS = f"""    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]),
    CONSTRAINT [CK_Order_Status] CHECK ([Status] < (9)),
    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId])
        REFERENCES {CUSTOMER} ([CustomerId]) ON DELETE CASCADE
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Customer] ON {ORDER} ([CustomerId])
    INCLUDE ([Status]);
"""
NEW_COLUMNS = """    [Priority] tinyint NOT NULL CONSTRAINT [DF_Order_Priority] DEFAULT ((1)),
    [ShippedOn] date NULL,
"""
NEW_INDEXES = f"""GO
CREATE NONCLUSTERED INDEX [IX_Order_ShippedOn] ON {ORDER} ([ShippedOn]);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Open] ON {ORDER} ([Status])
    WHERE ([Status] > (0));
"""
HOTFIX_COLUMN = "    [Region] char(2) NULL,\n"


def order_file(*, new: bool = False, hotfix: bool = False, note: bool = True) -> str:
    columns = ORDER_COLUMNS if note else ORDER_COLUMNS.replace("    [Note] nvarchar(50) NULL,\n", "")
    return (
        f"CREATE TABLE {ORDER} (\n"
        + columns
        + (NEW_COLUMNS if new else "")
        + (HOTFIX_COLUMN if hotfix else "")
        + ORDER_CONSTRAINTS
        + (NEW_INDEXES if new else "")
    )


def view_file(columns: str) -> str:
    return f"CREATE OR ALTER VIEW {VIEW} AS\nSELECT {columns} FROM {ORDER} WHERE [Status] = 0;\n"


def procedure_file(column: str) -> str:
    name = names.qualified(SCHEMA, "usp_OrderRegions")
    body = f"SELECT {column}, COUNT(*) AS [n] FROM {ORDER} GROUP BY {column};"
    return f"CREATE OR ALTER PROCEDURE {name} AS\n{body}\n"


def canonical(path: str, text: str) -> str:
    """The text of a table-class file as `azsqlcd export` would write it (lint NF000 asks for it)."""
    (obj,) = parse.parse_object_file(text, path)
    return emit.emit_object_file(obj)


# ------------------------------------------------------------------ the database repository
class Repo:
    """A git repository in a temporary directory; main moves with each commit."""

    def __init__(self, root: Path) -> None:
        self.root = root
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self._env = env | {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        root.mkdir(parents=True)
        self.git("-c", "init.defaultBranch=main", "init", "-q")
        for name, value in {
            "user.name": "azsqlcd live tables",
            "user.email": "tables@example.invalid",
            "core.autocrlf": "false",
            "commit.gpgsign": "false",
        }.items():
            self.git("config", name, value)

    def git(self, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=self.root, env=self._env, capture_output=True, check=True)
        return done.stdout.decode().strip()

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def write_object(self, kind: str, schema: str | None, name: str, text: str) -> None:
        path = names.path_for(kind, schema, name)
        self.write(path, canonical(path, text))

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", sha)
        return sha


@dataclass(frozen=True)
class Built:
    number: int
    bundle: Bundle
    migration: str | None  # file name of the migration that gen wrote for this release
    migration_text: str | None
    verify_errors: tuple[str, ...]


# ------------------------------------------------------------------ one run
@dataclass
class Scenario(acc.Acceptance):
    """acc.Acceptance with the table hooks of the release in every call of the tool."""

    built: dict[int, Built] = field(default_factory=dict)

    def hooks(self, number: int) -> tables.Hooks:
        return tables.Hooks(self.built[number].bundle, self.config)

    def deploy(  # type: ignore[override]
        self, number: int, *, expect_plan: Plan | None = None, factory: live.Connect | None = None
    ) -> acc.Outcome:
        audit = runner.Audit(
            approved_by="live tables", approved_utc=datetime.now(UTC), triggering_actor="owner"
        )
        try:
            report = runner.deploy(
                self.built[number].bundle,
                self.config,
                ENV,
                TARGET,
                expect_plan=expect_plan,
                inline_plan=expect_plan is None,
                session_factory=factory or self.connect,
                token_provider=self.provider,
                audit=audit,
                tool_version=__version__,
                tool_digest=self.tool_digest,
                table_hooks=self.hooks(number),
            )
        except RunError as error:
            return acc.Outcome(int(error.exit_code), error.reason_code, error.message, error.report)
        return acc.Outcome(report.exit_code, report.reason_code, report.message, report)

    def resolve(self, action: Callable[..., Any], number: int, *subject: Any, **extra: Any) -> acc.Outcome:
        try:
            report = action(
                self.built[number].bundle,
                self.config,
                ENV,
                TARGET,
                *subject,
                confirm_database=self.database,
                reason="live tables",
                session_factory=self.connect,
                token_provider=self.provider,
                audit=runner.Audit(triggering_actor="owner"),
                tool_version=__version__,
                tool_digest=self.tool_digest,
                **extra,
            )
        except RunError as error:
            return acc.Outcome(int(error.exit_code), error.reason_code, error.message, error.report)
        return acc.Outcome(report.exit_code, report.reason_code, report.message, report)

    def plan(self, number: int) -> tuple[acc.Outcome, Plan | None]:
        session = self.reader()
        try:
            computed = plan.compute_plan(
                self.built[number].bundle,
                self.config,
                ENV,
                TARGET,
                session,
                tool_version=__version__,
                tool_digest=self.tool_digest,
                open_second_session=self.connect,
                table_hooks=self.hooks(number),
            )
        except ToolError as error:
            return acc.Outcome(int(error.exit_code), error.reason_code, _told(error)), None
        except SqlError as error:
            return acc.Outcome(-1, "SQL_ERROR_IN_PLAN", str(error)), None
        finally:
            session.close()
        return acc.Outcome(0, computed.outcome.upper()), computed

    # -------------------------------------------------------------- reads of the owner
    def managed(self, key: str) -> bool:
        row = self.recorded().objects.get(key)
        return row is not None and row.status == "managed"

    def capture_labels(self, key: str) -> list[str]:
        row = self.recorded().objects.get(key)
        return sorted(row.capture) if row is not None else []

    def order_column(self, name: str) -> bool:
        return (
            self.value(f"SELECT COL_LENGTH({names.sql_literal(ORDER)}, {names.sql_literal(name)});")
            is not None
        )

    def order_index(self, name: str) -> bool:
        found = self.value(
            f"SELECT COUNT(*) FROM sys.indexes WHERE [object_id] = OBJECT_ID({names.sql_literal(ORDER)}) "
            f"AND [name] = {names.sql_literal(name)};"
        )
        return found == 1


def _told(error: ToolError) -> str:
    """The message and what the detail lists (objects, blockers), as the command line prints them."""
    lines = [error.message]
    for name in ("objects", "blockers", "failures", "refusals"):
        found = error.detail.get(name)
        lines += [str(item) for item in found] if isinstance(found, list) else []
    return " | ".join(lines)


def write_migration(repo: Repo, name: str) -> tuple[str, str]:
    """`azsqlcd gen` for the working tree, as a person does it: gen writes `reason: TODO` on each
    allow line, the person writes the reason, `gen --resum` writes the chain line again.
    Returns (file name, text) of the migration. No database."""
    base = repo.git("rev-parse", "HEAD")
    result = gen.generate(repo.root, base, name)
    gen.write_result(repo.root, result)
    if result.file is None or result.text is None:
        raise RuntimeError(f"gen wrote no migration {name}: {result.reason}")
    text = result.text
    if "reason: TODO" in text:
        text = text.replace("reason: TODO", "reason: live table scenario")
        (repo.root / "migrations" / result.file).write_bytes(text.encode("utf-8"))
        gen.write_resum(repo.root, gen.resum(repo.root, base))
    return result.file, text


def verify_errors(repo: Repo) -> tuple[str, ...]:
    """The errors of the pull-request check for the working tree against the last commit."""
    found = gen.verify(repo.root, repo.git("rev-parse", "HEAD"))
    return tuple(f"{f.path}:{f.line} {f.code} {f.message}" for f in found if f.severity == "error")


def release_of(s: Scenario, repo: Repo, work: Path, number: int, name: str | None, message: str) -> None:
    """gen (when name is given), verify, commit, build, read_bundle: release r<number>."""
    migration, text = write_migration(repo, name) if name is not None else (None, None)
    errors = verify_errors(repo)
    commit = repo.commit(message)
    built = release.build(repo.root, commit)
    folder = work / f"r{number}"
    release.write(built, folder)
    bundle = release.read_bundle(folder, release.digest(built.manifest))
    s.built[number] = Built(number, bundle, migration, text, errors)
    print(f"   r{number}: {message}; migration {migration}; verify errors: {len(errors)}", flush=True)
    for error in errors:
        print(f"      verify: {error}", flush=True)
    if text is not None:
        for line in text.splitlines():
            print(f"      | {line}", flush=True)


# ------------------------------------------------------------------ scenarios
def create_tables(s: Scenario, repo: Repo, work: Path) -> None:
    repo.write_object("SCHEMA", None, SCHEMA, SCHEMA_FILE)
    repo.write_object("TABLE", SCHEMA, "Customer", CUSTOMER_FILE)
    repo.write_object("TABLE", SCHEMA, "Order", order_file())
    repo.write(VIEW_PATH, view_file("[OrderId], [Note]"))
    release_of(s, repo, work, 2, "create_tm", "two tables with a foreign key, an index and a view")
    s.require(
        "verify has no error for the generated migration (r2)",
        "gen, proof",
        {"no error": not s.built[2].verify_errors},
    )
    planned, first = s.plan(2)
    s.expect(
        "a plan with the table model reads and changes nothing",
        "plan algorithm with table hooks",
        planned,
        0,
        "WORK",
        lambda: {
            "the schema does not exist": s.value(f"SELECT SCHEMA_ID({names.sql_literal(SCHEMA)});") is None
        },
    )
    assert first is not None
    applied = s.deploy(2, expect_plan=first)

    def as_the_model_says() -> dict[str, bool]:
        fk = s.value(
            "SELECT COUNT(*) FROM sys.foreign_keys WHERE [name] = N'FK_Order_Customer' "
            f"AND [parent_object_id] = OBJECT_ID({names.sql_literal(ORDER)}) "
            "AND [delete_referential_action] = 1;"
        )
        return {
            "the migration has its step row": s.step(s.built[2].migration or "") == ("ok", "migration"),
            "both tables are managed": s.managed(ORDER_KEY) and s.managed(CUSTOMER_KEY),
            "the schema is managed": s.managed(names.object_key("SCHEMA", None, SCHEMA)),
            "the view is managed": s.managed(VIEW_KEY),
            "the foreign key exists with ON DELETE CASCADE": fk == 1,
            "the index exists": s.order_index("IX_Order_Customer"),
            "the run row says ok": s.run_row(applied.run_id)["status"] == "ok",
        }

    s.expect(
        "the deploy creates the tables in one transaction and the read-back agrees with the catalog",
        "deploy algorithm with table hooks; read-back of table-class objects",
        applied,
        0,
        "OK",
        as_the_model_says,
    )
    print(f"   capture labels of {ORDER_KEY}: {s.capture_labels(ORDER_KEY)}", flush=True)
    runs = s.run_count()
    factory, seen = s.watch()
    again = s.deploy(2, factory=factory)
    s.expect(
        "a second deploy of the release sends nothing",
        "exit 0, nothing to do",
        again,
        0,
        "OK",
        lambda: {
            "no run row was added": s.run_count() == runs,
            "no CREATE or ALTER was sent": not any(
                batch.lstrip().upper().startswith(("CREATE", "ALTER")) for batch in seen[0].batches
            ),
        },
    )
    by_hand = f"INSERT INTO {CUSTOMER} ([CustomerId], [Name]) VALUES (1, N'a'); "
    by_hand += f"INSERT INTO {ORDER} ([OrderId], [CustomerId], [Amount], [Note]) VALUES (1, 1, 10.50, N'n');"
    acc.by_hand(s, by_hand)


def add_columns_and_indexes(s: Scenario, repo: Repo, work: Path) -> None:
    repo.write_object("TABLE", SCHEMA, "Order", order_file(new=True))
    release_of(s, repo, work, 3, "order_priority", "two columns and two indexes")
    s.require(
        "verify has no error for the generated migration (r3)",
        "gen, proof",
        {"no error": not s.built[3].verify_errors},
    )
    planned, third = s.plan(3)
    s.expect("the plan of the structural change", "plan algorithm with table hooks", planned, 0, "WORK")
    assert third is not None
    print(
        f"   plan steps: {[(step.kind, step.id) for unit in third.units for step in unit.steps]}", flush=True
    )
    print(
        f"   table facts: {third.table_facts}; destructive: {[d.code for d in third.destructive]}", flush=True
    )
    applied = s.deploy(3, expect_plan=third)
    s.expect(
        "the deploy adds the columns and the indexes; the read-back agrees with the catalog",
        "deploy algorithm with table hooks",
        applied,
        0,
        "OK",
        lambda: {
            "the migration has its step row": s.step(s.built[3].migration or "") == ("ok", "migration"),
            "both columns exist": s.order_column("Priority") and s.order_column("ShippedOn"),
            "both indexes exist": s.order_index("IX_Order_ShippedOn") and s.order_index("IX_Order_Open"),
            "the row got the default": s.value(f"SELECT [Priority] FROM {ORDER} WHERE [OrderId] = 1;") == 1,
            "the database records release 3": s.recorded().recorded_release_seq == 3,
        },
    )
    planned, quiet = s.plan(3)
    s.expect(
        "a plan after the deploy finds nothing to do and no drift",
        "drift of table-class objects",
        planned,
        0,
        "NOOP",
        lambda: {"no drift": quiet is not None and not quiet.drift},
    )


def drift_and_hotfix(s: Scenario, repo: Repo, work: Path) -> None:
    acc.by_hand(s, f"ALTER TABLE {ORDER} ADD [Region] char(2) NULL;")
    planned, drifted = s.plan(3)
    s.expect(
        'a column that was added by hand is reported as drift (drift = "report")',
        "drift of table-class objects",
        planned,
        0,
        "NOOP",
        lambda: {
            "the plan lists the table as drifted": drifted is not None
            and [d.key for d in drifted.drift] == [ORDER_KEY]
        },
    )
    if drifted is not None:
        print(
            f"   drift: {[(d.key, [x.property for x in d.differences]) for d in drifted.drift]}", flush=True
        )
    repo.write_object("TABLE", SCHEMA, "Order", order_file(new=True, hotfix=True))
    release_of(s, repo, work, 4, "order_region", "the column that was added by hand")
    migration = s.built[4].migration or ""
    blocked, _ = s.plan(4)
    s.expect(
        "drift on a table that the release changes blocks the plan", "drift", blocked, 22, "DRIFT_TOUCHED"
    )
    refused = s.deploy(4)
    s.expect(
        "the deploy refuses for the same reason and sends nothing", "drift", refused, 22, "DRIFT_TOUCHED"
    )
    marked = s.resolve(runner.mark_applied, 4, migration, table_hooks=s.hooks(4))
    s.expect(
        "resolve mark-applied reads the catalog back against the model and records the migration",
        "A16, read-back of a hand-applied migration",
        marked,
        0,
        "OK",
        lambda: {"the migration has its step row": s.step(migration) == ("ok", "migration")},
    )
    recorded = s.deploy(4)
    s.expect(
        "after the resolve the release is recorded and no drift stays",
        "A6",
        recorded,
        0,
        "OK",
        lambda: {"the database records release 4": s.recorded().recorded_release_seq == 4},
    )
    planned, quiet = s.plan(4)
    s.expect(
        "a plan after the resolve finds no drift",
        "drift of table-class objects",
        planned,
        0,
        "NOOP",
        lambda: {"no drift": quiet is not None and not quiet.drift},
    )


def drop_a_column_with_its_dependants(s: Scenario, repo: Repo, work: Path) -> None:
    # r5: the column goes, and the view that uses it changes in the same release (A12)
    repo.write_object("TABLE", SCHEMA, "Order", order_file(new=True, hotfix=True, note=False))
    repo.write(VIEW_PATH, view_file("[OrderId], [Amount]"))
    repo.write(PROCEDURE_PATH, procedure_file("[Region]"))
    release_of(s, repo, work, 5, "order_drop_note", "drop a column; the view that uses it changes too")
    s.require(
        "verify has no error for the generated migration (r5)",
        "gen, proof",
        {"no error": not s.built[5].verify_errors},
    )
    planned, fifth = s.plan(5)
    s.expect(
        "the plan of a column drop names the drop as destructive",
        "destructive list; plan step 8",
        planned,
        0,
        "WORK",
        lambda: {
            "DROP_COLUMN is listed": fifth is not None
            and "DROP_COLUMN" in [d.code for d in fifth.destructive]
        },
    )
    assert fifth is not None
    print(
        f"   plan steps: {[(step.kind, step.id) for unit in fifth.units for step in unit.steps]}", flush=True
    )
    print(
        f"   destructive: {[(d.code, d.object) for d in fifth.destructive]}; pre_broken: {fifth.pre_broken}",
        flush=True,
    )
    dropped = s.deploy(5, expect_plan=fifth)
    s.expect(
        "the deploy drops the column and deploys the changed view in one transaction",
        "A12; modules after migrations in one unit of work",
        dropped,
        0,
        "OK",
        lambda: {
            "the column is gone": not s.order_column("Note"),
            "the view and the procedure are managed": s.managed(VIEW_KEY) and s.managed(PROCEDURE_KEY),
            "the database records release 5": s.recorded().recorded_release_seq == 5,
        },
    )
    # r6: a column that a managed procedure still uses is dropped; the procedure is not changed
    repo.write_object("TABLE", SCHEMA, "Order", order_file(new=True, hotfix=False, note=False))
    release_of(s, repo, work, 6, "order_drop_region", "drop a column that a managed procedure uses")
    planned, sixth = s.plan(6)
    print(f"   plan of r6: {planned.seen} {planned.message[:600]}", flush=True)
    if sixth is not None:
        print(
            f"   plan steps: {[(step.kind, step.id) for unit in sixth.units for step in unit.steps]}",
            flush=True,
        )
    broken = s.deploy(6)

    def nothing_remains() -> dict[str, bool]:
        row = s.run_row(broken.run_id)
        return {
            "no step row of the migration": s.step(s.built[6].migration or "") is None,
            "the column still exists": s.order_column("Region"),
            "the run row says failed": row["status"] == "failed",
        }

    s.expect(
        "a column drop that breaks a managed procedure is stopped, and the unit of work is rolled back",
        "A12, dependant of an altered table",
        broken,
        21,
        "DEPENDANT_BROKEN",
        nothing_remains,
    )
    print(f"   the tool said: {broken.message[:600]}", flush=True)


SCENARIOS: tuple[tuple[str, Callable[[Scenario, Repo, Path], None]], ...] = (
    ("r2: create two tables with a foreign key and an index (gen, plan, deploy, read-back)", create_tables),
    ("r3: add two columns and two indexes", add_columns_and_indexes),
    ("drift after ALTER by hand; r4 holds the same change; mark-applied with read-back", drift_and_hotfix),
    (
        "r5: drop a column with its view; r6: a drop that breaks a managed procedure",
        drop_a_column_with_its_dependants,
    ),
)


# ------------------------------------------------------------------ run
def reset(session: Session) -> None:
    """No objects of this scenario, no state tables. Call it only after the guards passed."""
    live.drop_schema_objects(session, SCHEMA)
    for table in ("object", "step", "run", "meta"):
        name = names.qualified(state.SCHEMA, table)
        session.execute(f"IF OBJECT_ID({names.sql_literal(name)}, N'U') IS NOT NULL DROP TABLE {name};")


def setup(session: Session, config: Config) -> None:
    for batch in lex.split_batches(state.setup_sql(config, ENV, TARGET)):
        session.execute(batch.text)
    session.execute(live.SESSION_OPTIONS)


def run(args: argparse.Namespace) -> int:
    live.refuse_unconfirmed(args.database, args.confirm)
    provider: TokenProvider = live.CachedTokenProvider(AzureCliTokenProvider())
    connect = partial(db.connect, args.server, args.database, provider, live.APP_NAME)
    inspector = connect()
    checks: list[acc.Check] = []
    try:
        live.refuse_unless_disposable(inspector, args.database, args.confirm)
        toml = acc.config_toml(args.server, args.database).replace(
            "table_model = false", "table_model = true"
        )
        config = load_config(toml)
        inspector.execute(live.SESSION_OPTIONS)
        reset(inspector)
        setup(inspector, config)
        with tempfile.TemporaryDirectory(prefix="azsqlcd-tables-") as work:
            repo = Repo(Path(work) / "repository")
            repo.write(release.CONFIG_PATH, toml)
            repo.write(chain.SUM_PATH, chain.format_sum(chain.Chain(False, ())))
            repo.write(chain.TOMBSTONES_PATH, "# no module is dropped yet\n")
            repo.commit("r1: the repository")
            scenario = Scenario(
                database=args.database,
                config=config,
                bundles=[],
                trees=[],
                connect=connect,
                provider=provider,
                inspector=inspector,
                tool_digest=release.tool_digest(),
                checks=checks,
            )
            stopped = False
            for title, step in SCENARIOS:
                if stopped:
                    scenario.record(
                        acc.Check(title, "", acc.NOT_RUN, "", "", "an earlier check stopped the run")
                    )
                    continue
                print(f"-- {title}", flush=True)
                try:
                    step(scenario, repo, Path(work) / "releases")
                except acc.Halt:
                    stopped = True
                except Exception as error:  # the report must still be written
                    stopped = True
                    told = _told(error) if isinstance(error, ToolError) else str(error)
                    scenario.record(
                        acc.Check(title, "", acc.FAIL, "", "", f"stopped by {type(error).__name__}: {told}")
                    )
    finally:
        inspector.close()
    totals = {r: sum(1 for c in checks if c.result == r) for r in (acc.PASS, acc.FAIL, acc.NOT_RUN)}
    hide = live.private_names(args.server, args.database)
    report = {
        "tool_version": __version__,
        "checks": [dataclasses.asdict(c) for c in checks],
        "totals": totals,
    }
    live.write_json(args.out / REPORT_NAME, live.plain(report, hide))
    print()
    live.print_table(
        [("result", "exit and reason seen", "check"), *((c.result, c.seen, c.name) for c in checks)]
    )
    print(f"\n{totals[acc.PASS]} pass, {totals[acc.FAIL]} fail, {totals[acc.NOT_RUN]} not run.")
    return 0 if totals[acc.FAIL] == 0 and totals[acc.NOT_RUN] == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live table-model scenario of azsqlcd. Disposable database only."
    )
    parser.add_argument("--server", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--confirm-disposable-database", dest="confirm", required=True)
    parser.add_argument("--out", type=Path, default=Path(live.DEFAULT_OUT))
    try:
        return run(parser.parse_args(argv))
    except ToolError as error:
        print(f"azsqlcd live tables: {_told(error)}", file=sys.stderr)
        return int(error.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
