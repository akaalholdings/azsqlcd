"""Live acceptance for azsqlcd: the real modules, end to end, on a disposable Azure SQL Database.

Owner-run. This script connects to a database and writes to it. It never runs in pytest or CI.
Read docs/live-testing.md first.

    az login
    uv run --extra db python scripts/live_acceptance.py --server S --database D
        --confirm-disposable-database D [--out DIR]                       (one line)

The guards are those of scripts/live_spike.py (confirmed name, DB_NAME(), no "prod" in the name,
EngineEdition 5, no azsqlcd.meta row or environment 'disposable'). The sign-in is that of the tool:
the variable AZSQLCD_AUTH (see scripts/live_spike.py). With `sql` the check of the token life is
reported as "not applicable", not as a pass: a SQL login has no access token.

What it does:
  1. Drops the objects of schema [azsqlcd_accept] and the four tables of schema [azsqlcd]: every
     run starts from a database that the tool never saw.
  2. Sends the script of state.setup_sql for environment 'disposable' (the owner is db_owner).
  3. Builds a database repository with twelve commits in a temporary directory, and from each
     commit a release with release.build and release.read_bundle.
  4. Runs plan.compute_plan, runner.deploy and the resolve actions with real sessions, and makes
     each failure that the failure matrix of the design names and that a script can make: a
     batch that fails, a lock timeout, a second runner, a session that is killed, a batch that
     ends the transaction, a data batch that changes a module, drift, a name collision.
Each check is printed as pass or fail with the exit code and the reason code that the tool gave.
A check whose exit code is wrong stops the run: the next checks need the state that it leaves.

The script leaves the database as the last check left it: schema azsqlcd with its rows, schema
azsqlcd_accept, and the two users of setup-sql. Read azsqlcd.run and azsqlcd.step to see what
the tool recorded. The next run removes all of it again.

Output: a table on stdout and <out>/acceptance_report.json (default out: tests/fixtures/live/).
Server, database and login names are replaced in the file.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import live_spike as live

from azsqlcd import __version__, catalog, chain, lex, modules, names, plan, release, runner, state
from azsqlcd import session as db
from azsqlcd.config import Config, load_config
from azsqlcd.errors import ToolError
from azsqlcd.plan import Plan
from azsqlcd.release import Bundle
from azsqlcd.runner import Report, RunError
from azsqlcd.session import AccessToken, Credential, Session, SqlLogin, TokenProvider
from azsqlcd.sqlerrors import SqlError
from azsqlcd.state import rows

SCHEMA = "azsqlcd_accept"
PROJECT = "livetest"
ENV = "disposable"
TARGET = "live"
# Not real: the users that setup-sql makes for these ids can never sign in.
TENANT_ID = "00000000-0000-4000-8000-000000000001"
PLAN_CLIENT_ID = "00000000-0000-4000-8000-0000000000a1"
DEPLOY_CLIENT_ID = "00000000-0000-4000-8000-0000000000a2"
LOCK_TIMEOUT_MS = 5000
APPLOCK_WAIT_S = 2
REPORT_NAME = "acceptance_report.json"

PASS, FAIL, NOT_RUN = "pass", "fail", "not run"
NOT_APPLICABLE = "not applicable"  # a check of an access token, for a sign-in that has none

ITEM = names.qualified(SCHEMA, "Item")
VIEW_KEY = names.object_key("VIEW", SCHEMA, "vw_Item")
COUNT_KEY = names.object_key("PROCEDURE", SCHEMA, "usp_ItemCount")
LEGACY_KEY = names.object_key("PROCEDURE", SCHEMA, "usp_Legacy")
VIEW_PATH = f"schema/views/{SCHEMA}.vw_Item.sql"
COUNT_PATH = f"schema/procedures/{SCHEMA}.usp_ItemCount.sql"
LEGACY_PATH = f"schema/procedures/{SCHEMA}.usp_Legacy.sql"
M1 = "0001__create_item.sql"
M2 = "0002__add_qty.sql"
M3 = "0003__ix_item_qty.sql"
M4 = "0004__ux_item_qty.sql"
M5 = "0005__add_note.sql"
M6 = "0006__ends_the_transaction.sql"
M7 = "0007__touch_items.sql"

# Rows of the failure matrix (design Part 2 (e)) that this script does not make, and where each
# one is tested instead.
NOT_PROVOKED = (
    "Refresh of a managed dependant fails; blocker on a changed column: need the table model "
    "(table_model = true), which this script does not use. Spike item X3 proves the read behind them.",
    "Governance error 40552, 9002, 40549, 40550, 40551, 40544: cannot be made safely. Not tested.",
    "Transient error at connect, serverless resume: spike item L12.",
    "Token expires on an open session: spike item L13.",
    "Loss of the network after COMMIT reached the server: not automated; docs/live-testing.md, section 7.",
)


# ------------------------------------------------------------------ the database repository
def config_toml(server: str, database: str) -> str:
    return f"""[project]
name = "{PROJECT}"
tenant_id = "{TENANT_ID}"
table_model = false
module_chunk = 2
min_token_minutes = 5

[identities]
plan = "{PLAN_CLIENT_ID}"
deploy = "{DEPLOY_CLIENT_ID}"

[env.{ENV}]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "report"
lock_timeout_ms = {LOCK_TIMEOUT_MS}
applock_wait_s = {APPLOCK_WAIT_S}
job_timeout_minutes = 30
targets = [{{ id = "{TARGET}", server = "{server}", database = "{database}" }}]
"""


def migration(file: str, *batches: str, mode: str = "tx") -> str:
    header = f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode {mode}\n"
    return header + "\nGO\n".join(batches) + "\nGO\n"


def module(kind: str, name: str, body: str) -> str:
    return f"CREATE OR ALTER {kind} {names.qualified(SCHEMA, name)} AS\n{body}\n"


def releases(server: str, database: str) -> list[dict[str, str]]:
    """The file tree of each release of the acceptance repository, r1 first. Pure.

    One commit is one release. A release adds at most one migration, and a nontx migration is the
    only change of its release (A7, A8).
    """
    files: dict[str, str] = {
        release.CONFIG_PATH: config_toml(server, database),
        chain.TOMBSTONES_PATH: "# no module is dropped yet\n",
        VIEW_PATH: module("VIEW", "vw_Item", f"SELECT [id], [name] FROM {ITEM};"),
        COUNT_PATH: module("PROCEDURE", "usp_ItemCount", f"SELECT COUNT(*) AS [items] FROM {ITEM};"),
    }
    entries: list[chain.ChainEntry] = []
    trees: list[dict[str, str]] = []

    def add(file: str, *batches: str, mode: str = "tx") -> None:
        text = migration(file, *batches, mode=mode)
        files[f"migrations/{file}"] = text
        entries.append(chain.ChainEntry(file, chain.file_sha256(text.encode()), mode.split()[0]))
        files[chain.SUM_PATH] = chain.format_sum(chain.Chain(False, tuple(entries)))

    def cut() -> None:
        trees.append(dict(files))

    # r1: the schema, one table, one view, one procedure
    add(
        M1,
        f"CREATE SCHEMA {names.quote(SCHEMA)} AUTHORIZATION [dbo];",
        f"CREATE TABLE {ITEM} (\n    [id] int NOT NULL CONSTRAINT [PK_Item] PRIMARY KEY CLUSTERED,\n"
        "    [name] nvarchar(50) NOT NULL\n);",
    )
    cut()
    # r2: nothing that a deploy sends; the release must still be recorded (A6)
    files[f"onboarding/{ENV}/note.txt"] = "a file that no deploy reads\n"
    cut()
    # r3: one module changes, the other does not
    files[VIEW_PATH] = module(
        "VIEW", "vw_Item", f"SELECT [id], [name], LEN([name]) AS [name_length] FROM {ITEM};"
    )
    cut()
    # r4: batch 2 fails while two rows have one name
    add(
        M2,
        f"ALTER TABLE {ITEM} ADD [qty] int NULL;",
        f"ALTER TABLE {ITEM} ADD CONSTRAINT [UQ_Item_name] UNIQUE ([name]);",
    )
    cut()
    # r5, r6: non-transactional migrations, each alone in its release
    add(
        M3,
        f"CREATE NONCLUSTERED INDEX [IX_Item_qty] ON {ITEM} ([qty]) WITH (ONLINE = ON, RESUMABLE = ON);",
        mode="nontx expected-minutes: 1",
    )
    cut()
    add(  # fails while two rows have one qty
        M4,
        f"CREATE UNIQUE NONCLUSTERED INDEX [UX_Item_qty] ON {ITEM} ([qty]) WHERE [qty] IS NOT NULL "
        "WITH (ONLINE = ON);",
        mode="nontx expected-minutes: 1",
    )
    cut()
    # r7: two batches; the session is killed between them
    add(M5, f"ALTER TABLE {ITEM} ADD [note] nvarchar(100) NULL;", f"ALTER TABLE {ITEM} ADD [flag] bit NULL;")
    cut()
    # r8: a batch that ends the transaction of the tool. lint refuses it; the guard must see it
    add(M6, f"ALTER TABLE {ITEM} ADD [g] int NULL;", "COMMIT TRANSACTION;")
    cut()
    # r9: a data batch that changes a managed view when the row -1 exists
    add(
        M7,
        "-- azsqlcd:data\n"
        f"UPDATE {ITEM} SET [note] = N'seen' WHERE [id] > 0;\n"
        f"IF EXISTS (SELECT 1 FROM {ITEM} WHERE [id] = -1)\n"
        f"    EXEC (N'ALTER VIEW {names.qualified(SCHEMA, 'vw_Item')} AS SELECT [id] FROM {ITEM};');",
    )
    cut()
    # r10: a changed view, a tombstone, and a new module whose name exists in the database
    files[VIEW_PATH] = module("VIEW", "vw_Item", f"SELECT [id], [name], [qty] FROM {ITEM};")
    del files[COUNT_PATH]
    files[chain.TOMBSTONES_PATH] = (
        f'[[drop]]\nobject = "{COUNT_KEY}"\nreason = "not used any more (live acceptance)"\n'
    )
    files[LEGACY_PATH] = module("PROCEDURE", "usp_Legacy", "SELECT 1 AS [managed];")
    cut()
    # r11: a module that does not parse; r12: the fix
    files[LEGACY_PATH] = module("PROCEDURE", "usp_Legacy", "SELECT FROM;")
    cut()
    files[LEGACY_PATH] = module("PROCEDURE", "usp_Legacy", "SELECT 2 AS [managed];")
    cut()
    return trees


def commit_releases(repo: Path, trees: Sequence[Mapping[str, str]]) -> list[str]:
    """One commit on main for each tree. Returns the commit ids; origin/main names the last one."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}  # not the settings of the owner

    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True)
        return done.stdout.decode().strip()

    repo.mkdir(parents=True)
    git("-c", "init.defaultBranch=main", "init", "-q")
    settings = {
        "user.name": "azsqlcd live acceptance",
        "user.email": "acceptance@example.invalid",
        "core.autocrlf": "false",
        "commit.gpgsign": "false",
    }
    for name, value in settings.items():
        git("config", name, value)
    commits: list[str] = []
    before: Mapping[str, str] = {}
    for number, tree in enumerate(trees, start=1):
        for path in set(before) - set(tree):
            (repo / path).unlink()
        for path, text in tree.items():
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        git("add", "-A")
        git("commit", "-q", "-m", f"r{number}")
        commits.append(git("rev-parse", "HEAD"))
        before = tree
    git("update-ref", "refs/remotes/origin/main", commits[-1])
    return commits


def build_bundles(repo: Path, commits: Sequence[str], work: Path) -> list[Bundle]:
    """build, write and read_bundle for each commit: the path of a release through the pipeline."""
    bundles: list[Bundle] = []
    for number, commit in enumerate(commits, start=1):
        built = release.build(repo, commit)
        folder = work / f"r{number}"
        release.write(built, folder)
        bundles.append(release.read_bundle(folder, release.digest(built.manifest)))
    return bundles


# ------------------------------------------------------------------ a real session that is watched
type Event = Callable[[Watched], None]


class Watched:
    """The session of a run, with one planned event at one batch. Not a fake: every batch goes to
    the engine. The batch texts stay in memory, to count what was sent; they are never written."""

    def __init__(
        self,
        inner: Session,
        trigger: str | None = None,
        before: Event | None = None,
        after: Event | None = None,
    ) -> None:
        self._inner, self._trigger, self._before, self._after = inner, trigger, before, after
        self._fired = False
        self.batches: list[str] = []
        (self.spid,) = catalog.one_row(inner, live.SPID)

    @property
    def closed(self) -> bool:
        return self._inner.closed

    def execute(self, batch: str) -> db.ResultSets:
        hit = self._trigger is not None and not self._fired and self._trigger in batch
        if hit:
            self._fired = True
            if self._before is not None:
                self._before(self)
        self.batches.append(batch)
        result = self._inner.execute(batch)
        if hit and self._after is not None:
            self._after(self)
        return result

    def close(self) -> None:
        self._inner.close()

    def modules_sent(self) -> int:
        """Batches that are the text of a module file."""
        return sum(1 for batch in self.batches if batch.lstrip().upper().startswith("CREATE OR ALTER"))


class ShortLivedToken:
    """The real token, said to expire in one minute: less than min_token_minutes of the config."""

    def __init__(self, inner: TokenProvider) -> None:
        self._inner = inner

    def get(self) -> AccessToken:
        return AccessToken(self._inner.get().token, int(time.time()) + 60)


# ------------------------------------------------------------------ checks
@dataclass(frozen=True)
class Outcome:
    """How a call of the tool ended: exit code and reason code, as the pipeline would see them."""

    exit_code: int
    reason_code: str
    message: str = ""
    report: Report | None = None

    @property
    def seen(self) -> str:
        return f"{self.exit_code} {self.reason_code}"

    @property
    def run_id(self) -> int:
        if self.report is None or self.report.run_id is None:
            raise RuntimeError("this call of the tool wrote no run row")
        return self.report.run_id


@dataclass(frozen=True)
class Check:
    name: str
    covers: str  # the row of the failure matrix, or the amendment, that the check is for
    result: str  # pass | fail | not run | not applicable
    expected: str
    seen: str
    detail: str = ""


class Halt(Exception):
    """A check failed in a way that leaves the database in another state than the next check needs."""


@dataclass
class Acceptance:
    """One run: the releases, the sessions, and the checks made so far."""

    database: str
    config: Config
    bundles: list[Bundle]
    trees: list[dict[str, str]]
    connect: live.Connect  # a session as the driver gives it; the runner sets its own options
    provider: Credential
    inspector: Session  # the session of the owner: reads from outside a run, KILL, changes "by hand"
    tool_digest: str
    checks: list[Check] = field(default_factory=list)

    # -------------------------------------------------------------- calls of the tool
    def deploy(
        self,
        number: int,
        *,
        expect_plan: Plan | None = None,
        factory: live.Connect | None = None,
        provider: Credential | None = None,
    ) -> Outcome:
        """runner.deploy of release r<number>. Without expect_plan: --inline-plan."""
        audit = runner.Audit(
            approved_by="live acceptance", approved_utc=datetime.now(UTC), triggering_actor="owner"
        )
        try:
            report = runner.deploy(
                self.bundles[number - 1],
                self.config,
                ENV,
                TARGET,
                expect_plan=expect_plan,
                inline_plan=expect_plan is None,
                session_factory=factory or self.connect,
                token_provider=provider or self.provider,
                audit=audit,
                tool_version=__version__,
                tool_digest=self.tool_digest,
            )
        except RunError as error:
            return Outcome(int(error.exit_code), error.reason_code, error.message, error.report)
        return Outcome(report.exit_code, report.reason_code, report.message, report)

    def resolve(self, action: Callable[..., Report], number: int, *subject: Any, **extra: Any) -> Outcome:
        """One resolve action of runner.py, with the bundle of release r<number>."""
        try:
            report = action(
                self.bundles[number - 1],
                self.config,
                ENV,
                TARGET,
                *subject,
                confirm_database=self.database,
                reason="live acceptance",
                session_factory=self.connect,
                token_provider=self.provider,
                audit=runner.Audit(triggering_actor="owner"),
                tool_version=__version__,
                tool_digest=self.tool_digest,
                **extra,
            )
        except RunError as error:
            return Outcome(int(error.exit_code), error.reason_code, error.message, error.report)
        return Outcome(report.exit_code, report.reason_code, report.message, report)

    def reader(self) -> Session:
        """A session for reads from outside a run. It never waits for a lock without an end."""
        opened = self.connect()
        opened.execute(live.SESSION_OPTIONS)
        return opened

    def plan(self, number: int) -> tuple[Outcome, Plan | None]:
        """plan.compute_plan of release r<number>, with the syntax check on a second session."""
        session = self.reader()
        try:
            computed = plan.compute_plan(
                self.bundles[number - 1],
                self.config,
                ENV,
                TARGET,
                session,
                tool_version=__version__,
                tool_digest=self.tool_digest,
                open_second_session=self.connect,
            )
        except ToolError as error:
            return Outcome(int(error.exit_code), error.reason_code, error.message), None
        except SqlError as error:
            return Outcome(-1, "SQL_ERROR_IN_PLAN", str(error)), None
        finally:
            session.close()
        return Outcome(0, computed.outcome.upper()), computed

    def watch(
        self, trigger: str | None = None, *, before: Event | None = None, after: Event | None = None
    ) -> tuple[live.Connect, list[Watched]]:
        """A session factory for one run. Only the first session, the session of the run, is watched;
        the session of the syntax check gets the same batch texts and must not fire the event."""
        seen: list[Watched] = []

        def factory() -> Session:
            opened = self.connect()
            if seen:
                return opened
            seen.append(Watched(opened, trigger, before, after))
            return seen[0]

        return factory, seen

    # -------------------------------------------------------------- reads of the owner
    def value(self, batch: str) -> Any:
        return catalog.one_row(self.inspector, batch)[0]

    def run_count(self) -> int:
        return self.value("SELECT COUNT(*) FROM [azsqlcd].[run];")

    def step_count(self) -> int:
        return self.value("SELECT COUNT(*) FROM [azsqlcd].[step];")

    def run_row(self, run_id: int) -> dict[str, Any]:
        columns = ("status", "failed_step", "error_text", "note", "release_seq")
        row = catalog.one_row(
            self.inspector,
            f"SELECT {', '.join(names.quote(c) for c in columns)} FROM [azsqlcd].[run] "
            f"WHERE [run_id] = {int(run_id)};",
        )
        return dict(zip(columns, row, strict=True))

    def step(self, migration_id: str) -> tuple[Any, ...] | None:
        """(status, kind) of the step row of a migration; None when it has none."""
        found = rows(
            self.inspector,
            "SELECT [status], [kind] FROM [azsqlcd].[step] "
            f"WHERE [migration_id] = {names.sql_literal(migration_id)};",
        )
        return tuple(found[0]) if found else None

    def column(self, name: str) -> bool:
        length = self.value(f"SELECT COL_LENGTH({names.sql_literal(ITEM)}, {names.sql_literal(name)});")
        return length is not None

    def index(self, name: str) -> bool:
        return (
            self.value(
                f"SELECT COUNT(*) FROM sys.indexes WHERE [object_id] = OBJECT_ID({names.sql_literal(ITEM)}) "
                f"AND [name] = {names.sql_literal(name)};"
            )
            == 1
        )

    def recorded(self) -> state.State:
        return state.read_state(self.inspector)

    def source_is(self, key: str, number: int, path: str) -> bool:
        """The object row is managed and holds the checksum of the file of release r<number>."""
        row = self.recorded().objects.get(key)
        wanted = modules.checksum(self.trees[number - 1][path].encode("utf-8"))
        return row is not None and row.status == "managed" and row.source_sha256 == wanted

    def definition(self, key: str) -> Any:
        return catalog.capture_modules(self.inspector, [key]).get(key, {}).get("definition")

    # -------------------------------------------------------------- results
    def record(self, check: Check) -> None:
        self.checks.append(check)
        print(f"{check.result.upper():7} {check.name}  [{check.seen}]", flush=True)
        if check.result == FAIL:
            print(f"        expected {check.expected}. {check.detail}", flush=True)

    def expect(
        self,
        name: str,
        covers: str,
        outcome: Outcome,
        exit_code: int,
        reason_code: str,
        facts: Callable[[], Mapping[str, bool]] | None = None,
    ) -> None:
        """One check: the exit code, the reason code, and facts that the owner session reads.

        A wrong exit code stops the run (Halt). A wrong reason code or fact is a failed check, and
        the run goes on: the state of the database is the one that the next check needs.
        """
        expected = f"{exit_code} {reason_code}"
        if outcome.exit_code != exit_code:
            self.record(Check(name, covers, FAIL, expected, outcome.seen, outcome.message))
            raise Halt(name)
        wrong = [fact for fact, holds in (facts() if facts else {}).items() if not holds]
        problems = [f"not true: {', '.join(wrong)}"] if wrong else []
        if outcome.reason_code != reason_code:
            problems.append(outcome.message)
        result = FAIL if problems else PASS
        self.record(Check(name, covers, result, expected, outcome.seen, ". ".join(problems)))

    def require(self, name: str, covers: str, facts: Mapping[str, bool]) -> None:
        """A check with no call of the tool. A fact that is not true stops the run."""
        wrong = [fact for fact, holds in facts.items() if not holds]
        detail = f"not true: {', '.join(wrong)}" if wrong else ""
        self.record(Check(name, covers, FAIL if wrong else PASS, "all facts true", "facts", detail))
        if wrong:
            raise Halt(name)


def by_hand(a: Acceptance, batch: str) -> None:
    """A change that a person or an application makes outside the tool."""
    a.inspector.execute(batch)


# ------------------------------------------------------------------ scenarios
def setup_state(a: Acceptance) -> None:
    script = state.setup_sql(a.config, ENV, TARGET)
    for _ in range(2):  # the script says that it is safe to run again
        for batch in lex.split_batches(script):
            a.inspector.execute(batch.text)
    a.inspector.execute(live.SESSION_OPTIONS)
    meta = a.recorded().meta
    user = names.sql_literal(f"azsqlcd-{PROJECT}-{ENV}-deploy")
    sid = a.value(f"SELECT CONVERT(char(34), [sid], 1) FROM sys.database_principals WHERE [name] = {user};")
    a.require(
        "the script of setup-sql makes the state and the users; a second run changes nothing",
        "A2",
        {
            "the meta row names project and environment": (meta.project, meta.environment) == (PROJECT, ENV),
            "the run table is empty": a.run_count() == 0,
            "the SID of the deploy user is the client id": str(sid).upper()
            == "0X" + uuid.UUID(DEPLOY_CLIENT_ID).bytes_le.hex().upper(),
        },
    )


def token_life_check(a: Acceptance) -> None:
    """A deploy with a token that has too little life must stop before the connect. A SQL login
    has no token: the check is recorded as not applicable, never as a pass."""
    name, covers = (
        "a token with too little life stops before the connect",
        "Token cannot be minted, or has too little life",
    )
    if isinstance(a.provider, SqlLogin):
        why = "SQL authentication has no access token: the token life is not checked and cannot stop a run"
        a.record(Check(name, covers, NOT_APPLICABLE, "24 TOKEN_TOO_SHORT", "", why))
        return
    short = a.deploy(1, provider=ShortLivedToken(a.provider))
    a.expect(name, covers, short, 24, "TOKEN_TOO_SHORT", lambda: {"no run row": a.run_count() == 0})


def first_release(a: Acceptance) -> None:
    planned, first_plan = a.plan(1)
    a.expect(
        "a plan reads and changes nothing",
        "plan algorithm; syntax check under SET PARSEONLY ON",
        planned,
        0,
        "WORK",
        lambda: {
            "the schema of the release does not exist": a.value(
                f"SELECT SCHEMA_ID({names.sql_literal(SCHEMA)});"
            )
            is None,
            "no run row": a.run_count() == 0,
        },
    )
    assert first_plan is not None
    token_life_check(a)
    applied = a.deploy(1, expect_plan=first_plan)
    a.expect(
        "a deploy with the expected plan applies the migration and the modules",
        "deploy algorithm",
        applied,
        0,
        "OK",
        lambda: {
            "the migration has its step row": a.step(M1) == ("ok", "migration"),
            "the table exists": a.column("name"),
            "the view row holds the checksum of its file": a.source_is(VIEW_KEY, 1, VIEW_PATH),
            "the procedure row holds the checksum of its file": a.source_is(COUNT_KEY, 1, COUNT_PATH),
            "the run row says ok": a.run_row(applied.run_id)["status"] == "ok",
            "the database records release 1": a.recorded().recorded_release_seq == 1,
        },
    )
    by_hand(a, f"INSERT INTO {ITEM} ([id], [name]) VALUES (1, N'a'), (2, N'b');")
    runs = a.run_count()
    factory, seen = a.watch()
    again = a.deploy(1, factory=factory)
    a.expect(
        "a module deploys by checksum and is not sent again",
        "modules by checksum; exit 0, nothing to do",
        again,
        0,
        "OK",
        lambda: {
            "no module text was sent": seen[0].modules_sent() == 0,
            "no run row was added": a.run_count() == runs,
        },
    )


def record_stale_and_past(a: Acceptance) -> None:
    planned, old_plan = a.plan(2)
    a.expect("the plan of a release that changes nothing has a run row to write", "A6", planned, 0, "RECORD")
    assert old_plan is not None
    runs, steps = a.run_count(), a.step_count()
    recorded = a.deploy(2)
    a.expect(
        "a no-op deploy records the release",
        "A6",
        recorded,
        0,
        "OK",
        lambda: {
            "one run row was added": a.run_count() == runs + 1,
            "no step row was added": a.step_count() == steps,
            "the database records release 2": a.recorded().recorded_release_seq == 2,
        },
    )
    runs = a.run_count()
    stale = a.deploy(2, expect_plan=old_plan)
    a.expect(
        "a plan hash mismatch sends no DDL",
        "Stale plan",
        stale,
        22,
        "STALE_PLAN",
        lambda: {"no run row was added": a.run_count() == runs},
    )
    older = a.deploy(1)
    a.expect(
        "an older release on a newer database sends nothing",
        "A6, ahead and consistent",
        older,
        0,
        "ALREADY_PAST",
        lambda: {"no run row was added": a.run_count() == runs},
    )


def changed_module(a: Acceptance) -> None:
    factory, seen = a.watch()
    changed = a.deploy(3, factory=factory)
    a.expect(
        "only the module whose file changed is sent",
        "modules by checksum",
        changed,
        0,
        "OK",
        lambda: {
            "one module text was sent": seen[0].modules_sent() == 1,
            "the report names the view": changed.report is not None
            and changed.report.modules_deployed == (VIEW_KEY,),
            "the view row holds the new checksum": a.source_is(VIEW_KEY, 3, VIEW_PATH),
        },
    )


def failed_batch_lock_timeout_second_runner(a: Acceptance) -> None:
    by_hand(a, f"INSERT INTO {ITEM} ([id], [name]) VALUES (3, N'a');")  # a second row with the name a
    failed = a.deploy(4)

    def nothing_remains() -> dict[str, bool]:
        row = a.run_row(failed.run_id)
        return {
            "no step row of the migration": a.step(M2) is None,
            "the column of batch 1 is gone": not a.column("qty"),
            "the run row says failed": row["status"] == "failed",
            "the run row names batch 2": row["failed_step"] == f"{M2}#2",
            # the engine names the table, the constraint and the duplicate value (a) in its message
            "the stored error text holds no name and no value": "'" not in str(row["error_text"])
            and "(a)" not in str(row["error_text"]),
        }

    a.expect(
        "a failing batch leaves no step row and the run is failed",
        "Run-time error in batch N, tx",
        failed,
        21,
        "BATCH_FAILED",
        nothing_remains,
    )

    blocker = a.connect()
    blocker.execute(f"BEGIN TRANSACTION; UPDATE {ITEM} SET [name] = [name] WHERE [id] = 1;")
    try:
        timed_out = a.deploy(4)
    finally:
        blocker.close()
    a.expect(
        "a lock timeout on a user table is a clean stop",
        "Lock timeout 1222 on a user object",
        timed_out,
        24,
        "LOCK_TIMEOUT",
        lambda: {
            "no step row of the migration": a.step(M2) is None,
            "the run row says failed": a.run_row(timed_out.run_id)["status"] == "failed",
        },
    )

    by_hand(a, f"DELETE FROM {ITEM} WHERE [id] = 3;")
    others: list[Outcome] = []

    def second_runner(_: Watched) -> None:
        # the first runner is inside its transaction and holds the lock
        others.append(a.deploy(4))
        others.append(a.plan(4)[0])

    factory, _ = a.watch("[UQ_Item_name]", before=second_runner)
    first = a.deploy(4, factory=factory)
    a.require(
        "the second runner started inside the first one", "test set-up", {"two calls": len(others) == 2}
    )
    a.expect(
        "a second runner exits 25 while the first holds the lock",
        "Applock not granted",
        others[0],
        25,
        "LOCK_NOT_GRANTED",
    )
    a.expect("a plan sees that a run is live and stops", "A4", others[1], 25, "RUN_LIVE")
    a.expect(
        "the first runner ends as if it were alone",
        "Applock not granted",
        first,
        0,
        "OK",
        lambda: {
            "the migration has its step row": a.step(M2) == ("ok", "migration"),
            "the column exists": a.column("qty"),
            "the database records release 4": a.recorded().recorded_release_seq == 4,
        },
    )


def nontx_lost_after_the_batch(a: Acceptance) -> None:
    marker: list[Any] = []

    def read_marker(_: Watched) -> None:
        marker.append(a.step(M3))  # another session reads committed rows only

    def kill_session(watched: Watched) -> None:
        live.kill(a.inspector, watched.spid)

    factory, _ = a.watch("[IX_Item_qty]", before=read_marker, after=kill_session)
    lost = a.deploy(5, factory=factory)
    a.expect(
        "the nontx marker is committed before dispatch; a session lost in the step is 'outcome unknown'",
        "Connection lost, nontx step",
        lost,
        23,
        "CONNECTION_LOST_NONTX",
        lambda: {
            "the step row said started before the batch was sent": marker == [("started", "nontx")],
            "the step row still says started": a.step(M3) == ("started", "nontx"),
            "the index was built": a.index("IX_Item_qty"),
            "the run row still says running": a.run_row(lost.run_id)["status"] == "running",
        },
    )
    refused = a.deploy(5)
    a.expect(
        "the next deploy marks step and run unknown and refuses",
        "Runner killed or job cancelled; nontx: exit 22 until resolve",
        refused,
        22,
        "STEP_UNRESOLVED",
        lambda: {
            "the step row says unknown": a.step(M3) == ("unknown", "nontx"),
            "the run row says unknown": a.run_row(lost.run_id)["status"] == "unknown",
        },
    )
    a.expect(
        "resolve mark-not-applied refuses while the index exists",
        "A16",
        a.resolve(runner.mark_not_applied, 5, M3),
        22,
        "NOT_PROVEN_ABSENT",
    )
    a.expect(
        "resolve mark-applied closes the step",
        "A16",
        a.resolve(runner.mark_applied, 5, M3),
        0,
        "OK",
        lambda: {"the step row says ok": a.step(M3) == ("ok", "nontx")},
    )
    a.expect("a plan refuses while an unknown run is not cleared", "A5", a.plan(5)[0], 22, "RUN_UNKNOWN")
    a.expect(
        "resolve clear-run, after a lost nontx step",
        "A5",
        a.resolve(runner.clear_run, 5, lost.run_id),
        0,
        "OK",
    )
    a.expect(
        "after the resolve of the nontx step the release is recorded",
        "A6",
        a.deploy(5),
        0,
        "OK",
        lambda: {"the database records release 5": a.recorded().recorded_release_seq == 5},
    )


def nontx_error_with_a_live_session(a: Acceptance) -> None:
    by_hand(a, f"UPDATE {ITEM} SET [qty] = 7;")  # two rows with one qty: the unique index cannot be built
    failed = a.deploy(6)
    a.expect(
        "an error in a nontx step is 'outcome unknown'",
        "Error in a nontx step, connection alive",
        failed,
        23,
        "NONTX_FAILED",
        lambda: {
            "the step row says unknown": a.step(M4) == ("unknown", "nontx"),
            "the run row says unknown": a.run_row(failed.run_id)["status"] == "unknown",
            "the index does not exist": not a.index("UX_Item_qty"),
        },
    )
    by_hand(a, f"UPDATE {ITEM} SET [qty] = [id];")
    a.expect(
        "resolve mark-not-applied, when the catalog proves that nothing remains",
        "A16",
        a.resolve(runner.mark_not_applied, 6, M4),
        0,
        "OK",
        lambda: {"the step row says not_applied": a.step(M4) == ("not_applied", "nontx")},
    )
    a.expect(
        "resolve clear-run, after a failed nontx step",
        "A5",
        a.resolve(runner.clear_run, 6, failed.run_id),
        0,
        "OK",
    )
    a.expect(
        "the next deploy sends the nontx migration again",
        "Error in a nontx step, connection alive",
        a.deploy(6),
        0,
        "OK",
        lambda: {
            "the step row says ok": a.step(M4) == ("ok", "nontx"),
            "the index exists": a.index("UX_Item_qty"),
        },
    )


def session_killed_in_the_transaction(a: Acceptance) -> None:
    def kill_session(watched: Watched) -> None:
        live.kill(a.inspector, watched.spid)  # batch 1 ran; batch 2 is never sent to a live session

    def killed_run() -> Outcome:
        factory, _ = a.watch("[flag]", before=kill_session)
        return a.deploy(7, factory=factory)

    if runner.RECONCILE_BY_LOCKING_READ:
        reconciled_by_the_run(a, killed_run)
        return
    lost = killed_run()
    a.expect(
        "a session killed in the middle of the transaction is 'outcome unknown'",
        "Connection lost, tx segment",
        lost,
        23,
        "CONNECTION_LOST_TX",
        lambda: {
            "no step row of the migration": a.step(M5) is None,
            "the column of batch 1 is gone": not a.column("note"),
            "the run row still says running": a.run_row(lost.run_id)["status"] == "running",
        },
    )
    planned, dead_plan = a.plan(7)
    a.expect(
        "a plan reports a dead run and does not refuse",
        "A4",
        planned,
        0,
        "WORK",
        lambda: {
            "a note names the dead run": dead_plan is not None and any("dead" in n for n in dead_plan.notes)
        },
    )
    # spike L6 on the real run row: the function that runner.RECONCILE_BY_LOCKING_READ switches on
    read = runner.reconcile_by_locking_read(
        a.connect, lost.run_id, 0, lock_timeout_ms=LOCK_TIMEOUT_MS, applock_wait_s=30
    )
    a.expect(
        "a locking read on a new session tells that the unit of work was rolled back",
        "Connection lost, tx segment (after spike L6)",
        Outcome(int(read.exit_code), read.reason_code, read.message),
        24,
        "CONNECTION_LOST_ROLLED_BACK",
        lambda: {"the run row says failed": a.run_row(lost.run_id)["status"] == "failed"},
    )
    again = killed_run()
    a.expect("a second killed run", "Connection lost, tx segment", again, 23, "CONNECTION_LOST_TX")
    healed = a.deploy(7)

    def reconciled() -> dict[str, bool]:
        dead = a.run_row(again.run_id)
        return {
            "the dead run row says failed": dead["status"] == "failed",
            "the dead run row has the note reconciled": dead["note"] == "reconciled",
            "the migration has its step row": a.step(M5) == ("ok", "migration"),
            "both columns exist": a.column("note") and a.column("flag"),
        }

    a.expect(
        "the next deploy reconciles the dead run and applies the release",
        "Runner killed or job cancelled; tx: continues",
        healed,
        0,
        "OK",
        reconciled,
    )


def reconciled_by_the_run(a: Acceptance, killed_run: Callable[[], Outcome]) -> None:
    """runner.RECONCILE_BY_LOCKING_READ is True: the run that lost its session reads the outcome
    on a new session, so a killed session is a clean stop (exit 24) and no dead run row stays."""
    lost = killed_run()

    def rolled_back() -> dict[str, bool]:
        row = a.run_row(lost.run_id)
        return {
            "no step row of the migration": a.step(M5) is None,
            "the column of batch 1 is gone": not a.column("note"),
            "the run row says failed": row["status"] == "failed",
            "the run row has the note of the locking read": "rolled back" in str(row["note"]),
        }

    a.expect(
        "a session killed in the middle of the transaction is read as rolled back on a new session",
        "Connection lost, tx segment (after spike L6)",
        lost,
        24,
        "CONNECTION_LOST_ROLLED_BACK",
        rolled_back,
    )
    planned, next_plan = a.plan(7)
    a.expect(
        "a plan after the reconciled run has no dead run to report",
        "A4",
        planned,
        0,
        "WORK",
        lambda: {
            "no note names a dead run": next_plan is not None
            and not any("dead" in n for n in next_plan.notes)
        },
    )
    a.expect(
        "the next deploy applies the release",
        "Connection lost, tx segment (after spike L6)",
        a.deploy(7),
        0,
        "OK",
        lambda: {
            "the migration has its step row": a.step(M5) == ("ok", "migration"),
            "both columns exist": a.column("note") and a.column("flag"),
        },
    )


def batch_that_ends_the_transaction(a: Acceptance) -> None:
    ended = a.deploy(8)
    a.expect(
        "a batch that ends the transaction is seen by the guard",
        "Guard not (1,1) and no error raised",
        ended,
        23,
        "GUARD_FAILED",
        lambda: {
            "the run row says unknown": a.run_row(ended.run_id)["status"] == "unknown",
            "no step row of the migration": a.step(M6) is None,
            "batch 1 was committed by the script": a.column("g"),
        },
    )
    a.expect("a plan refuses while the run is unknown", "A5", a.plan(8)[0], 22, "RUN_UNKNOWN")
    a.expect(
        "resolve clear-run, after a failed guard", "A5", a.resolve(runner.clear_run, 8, ended.run_id), 0, "OK"
    )
    a.expect(
        "resolve mark-applied of a transactional migration needs a read-back or the force flag",
        "A16",
        a.resolve(runner.mark_applied, 8, M6),
        22,
        "READBACK_REQUIRED",
    )
    a.expect(
        "resolve mark-applied with force records the migration",
        "A16",
        a.resolve(runner.mark_applied, 8, M6, force_no_readback=True),
        0,
        "OK",
        lambda: {"the migration has its step row": a.step(M6) == ("ok", "migration")},
    )
    a.expect(
        "after the resolve of the guard failure the release is recorded",
        "A6",
        a.deploy(8),
        0,
        "OK",
        lambda: {"the database records release 8": a.recorded().recorded_release_seq == 8},
    )


def data_batch_changes_a_module(a: Acceptance) -> None:
    by_hand(a, f"INSERT INTO {ITEM} ([id], [name]) VALUES (-1, N'row that makes the batch change the view');")
    before = a.definition(VIEW_KEY)
    failed = a.deploy(9)
    a.expect(
        "a data batch that changes a managed object fails the read-back",
        "Read-back differs, or an untouched object changed",
        failed,
        21,
        "UNTOUCHED_CHANGED",
        lambda: {
            "the view has its text from before": a.definition(VIEW_KEY) == before,
            "no step row of the migration": a.step(M7) is None,
        },
    )
    by_hand(a, f"DELETE FROM {ITEM} WHERE [id] = -1;")
    a.expect(
        "the same release passes when the batch changes no object",
        "Read-back differs, or an untouched object changed",
        a.deploy(9),
        0,
        "OK",
        lambda: {"the migration has its step row": a.step(M7) == ("ok", "migration")},
    )


def drift_collision_tombstone(a: Acceptance) -> None:
    view, legacy = names.qualified(SCHEMA, "vw_Item"), names.qualified(SCHEMA, "usp_Legacy")
    by_hand(a, f"ALTER VIEW {view} AS SELECT [id], [name], 1 AS [changed_by_hand] FROM {ITEM};")
    by_hand(a, f"CREATE PROCEDURE {legacy} AS SELECT 0 AS [made_by_hand];")
    planned, current = a.plan(9)
    a.expect(
        'drift on a module that the release does not touch is reported (drift = "report")',
        "drift",
        planned,
        0,
        "NOOP",
        lambda: {
            "the plan lists the view as drifted": current is not None
            and [d.key for d in current.drift] == [VIEW_KEY],
            "the plan lists the procedure as unmanaged": current is not None
            and LEGACY_KEY in current.unmanaged,
        },
    )
    a.expect("a create over an unmanaged name is refused", "collision", a.plan(10)[0], 22, "NAME_COLLISION")
    a.expect("resolve adopt-module", "A16", a.resolve(runner.adopt_module, 10, LEGACY_KEY), 0, "OK")
    a.expect("drift on a touched module blocks the plan", "drift", a.plan(10)[0], 22, "DRIFT_TOUCHED")
    a.expect("resolve accept-drift", "A16", a.resolve(runner.accept_drift, 10, VIEW_KEY), 0, "OK")
    planned, tenth = a.plan(10)
    a.expect(
        "the plan names what it overwrites and drops",
        "destructive list",
        planned,
        0,
        "WORK",
        lambda: {
            "two OVERWRITE_MODULE and one DROP_MODULE": tenth is not None
            and sorted(d.code for d in tenth.destructive)
            == ["DROP_MODULE", "OVERWRITE_MODULE", "OVERWRITE_MODULE"]
        },
    )
    assert tenth is not None
    applied = a.deploy(10, expect_plan=tenth)

    def as_the_files_say() -> dict[str, bool]:
        row = a.recorded().objects.get(COUNT_KEY)
        return {
            "a tombstoned module is dropped": not catalog.object_exists(a.inspector, COUNT_KEY),
            "its row says dropped": row is not None and row.status == "dropped",
            "the view row holds the checksum of its file": a.source_is(VIEW_KEY, 10, VIEW_PATH),
            "the adopted procedure holds the checksum of its file": a.source_is(LEGACY_KEY, 10, LEGACY_PATH),
            "the text made by hand is gone": "changed_by_hand" not in str(a.definition(VIEW_KEY)),
        }

    a.expect(
        "the deploy overwrites the accepted and the adopted module and drops the tombstoned one",
        "tombstone; OVERWRITE_MODULE",
        applied,
        0,
        "OK",
        as_the_files_say,
    )


def module_that_does_not_parse(a: Acceptance) -> None:
    runs = a.run_count()
    a.expect(
        "a module that does not parse is refused before any batch",
        "Compile error in a batch (caught by PARSEONLY)",
        a.deploy(11),
        22,
        "PARSEONLY_FAILED",
        lambda: {"no run row was added": a.run_count() == runs},
    )
    a.expect(
        "the next release with the fix deploys; the broken release is skipped",
        "A7, a module-only release may be skipped",
        a.deploy(12),
        0,
        "OK",
        lambda: {
            "the procedure row holds the checksum of its file": a.source_is(LEGACY_KEY, 12, LEGACY_PATH)
        },
    )


def other_environment_and_rebind(a: Acceptance) -> None:
    # as after a refresh from another environment. If the script stops inside this block, set the
    # row back by hand: UPDATE azsqlcd.meta SET environment = 'disposable'.
    by_hand(a, "UPDATE [azsqlcd].[meta] SET [environment] = 'sandbox' WHERE [id] = 1;")
    try:
        other = a.deploy(12)
        rebound = a.resolve(runner.rebind_environment, 12)
    finally:
        by_hand(a, f"UPDATE [azsqlcd].[meta] SET [environment] = '{ENV}' WHERE [id] = 1;")
    a.expect(
        "a database that is bound to another environment is refused",
        "fence",
        other,
        22,
        "FENCE_META_MISMATCH",
    )
    a.expect("resolve rebind-environment", "A16", rebound, 0, "OK")
    a.expect("after the rebind a deploy runs again", "fence", a.deploy(12), 0, "OK")


SCENARIOS: tuple[tuple[str, Callable[[Acceptance], None]], ...] = (
    ("setup-sql", setup_state),
    ("first release: plan, token life, deploy, checksum", first_release),
    ("release with no change, stale plan, older release", record_stale_and_past),
    ("changed module", changed_module),
    ("failing batch, lock timeout, second runner", failed_batch_lock_timeout_second_runner),
    ("nontx step: session lost after the batch", nontx_lost_after_the_batch),
    ("nontx step: error with a live session", nontx_error_with_a_live_session),
    ("session killed in the transaction; reconcile", session_killed_in_the_transaction),
    ("batch that ends the transaction", batch_that_ends_the_transaction),
    ("data batch that changes a module", data_batch_changes_a_module),
    ("drift, name collision, tombstone, adopt, accept", drift_collision_tombstone),
    ("module that does not parse", module_that_does_not_parse),
    ("other environment, rebind", other_environment_and_rebind),
)


# ------------------------------------------------------------------ run
def reset(session: Session) -> None:
    """A database that the tool never saw: no acceptance schema, no state tables.

    Call it only after the guards passed. The schema azsqlcd and the users of setup-sql stay;
    the script of setup-sql makes what is missing.
    """
    live.drop_schema_objects(session, SCHEMA)
    for table in ("object", "step", "run", "meta"):  # object and step point at run
        name = names.qualified(state.SCHEMA, table)
        session.execute(f"IF OBJECT_ID({names.sql_literal(name)}, N'U') IS NOT NULL DROP TABLE {name};")


def run_scenarios(a: Acceptance) -> None:
    """Every scenario in order. After a Halt, or an error that no check planned for, the rest is not run."""
    stopped = False
    for title, scenario in SCENARIOS:
        if stopped:
            a.record(Check(title, "", NOT_RUN, "", "", "an earlier check stopped the run"))
            continue
        print(f"-- {title}", flush=True)
        try:
            scenario(a)
        except Halt:
            stopped = True
        except Exception as error:  # the report must still be written
            stopped = True
            a.record(Check(title, "", FAIL, "", "", f"stopped by {type(error).__name__}: {error}"))


def totals_of(checks: Sequence[Check]) -> dict[str, int]:
    """The count of checks for each result. A check that is not applicable is counted apart."""
    results = (PASS, FAIL, NOT_RUN, NOT_APPLICABLE)
    return {result: sum(1 for check in checks if check.result == result) for result in results}


def exit_code_of(totals: Mapping[str, int]) -> int:
    """0 when no check failed and every check ran. A check that is not applicable is neither."""
    return 0 if totals[FAIL] == 0 and totals[NOT_RUN] == 0 else 1


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live acceptance of azsqlcd on a disposable Azure SQL Database. See docs/live-testing.md."
    )
    parser.add_argument("--server", required=True, help="for example NAME.database.windows.net")
    parser.add_argument("--database", required=True)
    parser.add_argument("--confirm-disposable-database", dest="confirm", required=True)
    parser.add_argument("--out", type=Path, default=Path(live.DEFAULT_OUT))
    return parser.parse_args(argv)


def run(args: argparse.Namespace, connect: live.Connect | None = None) -> int:
    live.refuse_unconfirmed(args.database, args.confirm)  # before any connection
    # a test gives the sessions: the variables of the machine are then not read
    provider = live.live_credential() if connect is None else live.live_credential({})
    if connect is None:
        connect = partial(db.connect, args.server, args.database, provider, live.APP_NAME)
    inspector = connect()
    try:
        live.refuse_unless_disposable(inspector, args.database, args.confirm)
        logins = catalog.one_row(
            inspector, "/* azsqlcd:live_logins */ SELECT ORIGINAL_LOGIN(), USER_NAME(), SUSER_SNAME();"
        )
        hide = live.private_names(args.server, args.database, [str(name) for name in logins if name])
        # the releases first: a machine without git must stop before the database is changed
        trees = releases(args.server, args.database)
        with tempfile.TemporaryDirectory(prefix="azsqlcd-acceptance-", ignore_cleanup_errors=True) as work:
            commits = commit_releases(Path(work) / "repository", trees)
            bundles = build_bundles(Path(work) / "repository", commits, Path(work) / "releases")
        config = load_config(bundles[-1].files[release.CONFIG_PATH].decode("utf-8"))
        inspector.execute(live.SESSION_OPTIONS)
        reset(inspector)
        acceptance = Acceptance(
            database=args.database,
            config=config,
            bundles=bundles,
            trees=trees,
            connect=connect,
            provider=provider,
            inspector=inspector,
            tool_digest=release.tool_digest(),
        )
        run_scenarios(acceptance)
    finally:
        inspector.close()
    checks = acceptance.checks
    totals = totals_of(checks)
    report = {
        "tool_version": __version__,
        "tool_digest": acceptance.tool_digest,
        "checks": [dataclasses.asdict(check) for check in checks],
        "totals": totals,
        "not_provoked": list(NOT_PROVOKED),
    }
    live.write_json(args.out / REPORT_NAME, live.plain(report, hide))
    print()
    live.print_table(
        [("result", "exit and reason seen", "check"), *((c.result, c.seen, c.name) for c in checks)]
    )
    not_applicable = f", {totals[NOT_APPLICABLE]} not applicable" if totals[NOT_APPLICABLE] else ""
    print(f"\n{totals[PASS]} pass, {totals[FAIL]} fail, {totals[NOT_RUN]} not run{not_applicable}.")
    print(f"report: {args.out / REPORT_NAME}")
    return exit_code_of(totals)


def main(argv: Sequence[str] | None = None, *, connect: live.Connect | None = None) -> int:
    try:
        return run(parse_args(argv), connect)
    except ToolError as error:
        print(f"azsqlcd live acceptance: {error}", file=sys.stderr)
        return int(error.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
