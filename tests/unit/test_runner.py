"""The runner: deploy. Every test drives deploy() against a FakeSession; no test runs T-SQL.

Db is a FakeSession with the small part of a database that a run reads back: the modules that
exist (a module file that is sent creates one, a DROP removes one) and the committed fence. The
plan is the real compute_plan, so a test that changes the release or the recorded state gets the
units of work that the planner makes for it.
"""

import dataclasses
import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from azsqlcd import catalog, chain, names, plan, release, runner, state
from azsqlcd.catalog import Difference
from azsqlcd.config import load_config
from azsqlcd.errors import Exit, ToolError, retry_safe
from azsqlcd.modules import checksum, read_module, stored_text
from azsqlcd.plan import Plan, TableFindings, Work, compute_plan
from azsqlcd.release import Bundle, Manifest
from azsqlcd.runner import Audit, Report, RunError, deploy, reconcile_by_locking_read
from azsqlcd.session import AccessToken, ResultSets
from azsqlcd.sqlerrors import sql_error
from azsqlcd.state import Meta, ObjectRow, RunRow, State, StepRow
from support.fake_session import FakeSession

COMMIT = "c" * 40
RECORDED_COMMIT = "a" * 40
TOOL_DIGEST = "d" * 64
M1, M2 = "0001__a.sql", "0002__b.sql"
SAME = "same"
FACTS = (5, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0)
TYPE_CODES = {"VIEW": "V", "PROCEDURE": "P", "FUNCTION": "FN", "TRIGGER": "TR"}
# the SET options that the tool sets; the last three are @@OPTIONS bits: XACT_ABORT, NOCOUNT and
# IMPLICIT_TRANSACTIONS (0 = off)
OPTIONS_ON = (1, 1, 1, 1, 1, 1, 0, 16384, 512, 0)
IMPLICIT_BIT = 9  # position of @@OPTIONS & 2 in the row
RUN_ID = 41
STARTED = "2026-10-07T09:00:00.000"
NOW = 1_800_000_000.0
ADD_C = "ALTER TABLE [sales].[Order] ADD [c] int NULL;"
ADD_D = "ALTER TABLE [sales].[Order] ADD [d] int NULL;"
BUILD_INDEX = "CREATE INDEX [IX_Order_c] ON [sales].[Order] ([c]) WITH (ONLINE = ON, RESUMABLE = ON);"
DATA = "-- azsqlcd:data\nUPDATE [sales].[Order] SET [c] = 0 WHERE [c] IS NULL;"
LOCK_TIMEOUT = "Lock request time out period exceeded."
DEADLOCK = (
    "Transaction (Process ID 57) was deadlocked on lock resources with another process and has been "
    "chosen as the deadlock victim. Rerun the transaction."
)
LOG_FULL = "The transaction log for database 'sales' is full due to 'ACTIVE_TRANSACTION'."


def session_options(lock_timeout_ms: int, language: str = "us_english") -> tuple:
    """The row that a session reports for what the tool set: SET options, lock timeout, language."""
    return (*OPTIONS_ON, lock_timeout_ms, language)


TOML = """
[project]
name = "sales"
tenant_id = "11111111-1111-1111-1111-111111111111"
table_model = false
module_chunk = 2
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
targets = [{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }]

[env.prod]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "block"
lock_timeout_ms = 10000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{ id = "sales-prod", server = "sql-sales-prod.database.windows.net", database = "sales" }]
"""
CONFIG = load_config(TOML)


# ------------------------------------------------------------------ a release in memory
@dataclasses.dataclass(frozen=True)
class Line:
    """One line of migrations.sum with the text of its file."""

    file: str
    text: str
    mode: str = "tx"


def mig(file: str, *batches: str, mode: str = "tx") -> Line:
    header = f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode {mode}\n"
    return Line(file, header + "\nGO\n".join(batches or (ADD_C,)) + "\nGO\n", mode)


def key(kind: str, name: str) -> str:
    return names.object_key(kind, "sales", name)


def proc(name: str, body: str = "SELECT 1;") -> tuple[str, str]:
    return f"schema/procedures/sales.{name}.sql", f"CREATE OR ALTER PROCEDURE [sales].[{name}] AS\n{body}\n"


def view(name: str, select: str = "SELECT 1 AS [x]", options: str = "") -> tuple[str, str]:
    return f"schema/views/sales.{name}.sql", f"CREATE OR ALTER VIEW [sales].[{name}]{options} AS\n{select};\n"


def bundle(
    *lines: Line, modules: Sequence[tuple[str, str]] = (), tombstones: Sequence[str] = (), seq: int = 7
) -> Bundle:
    files: dict[str, bytes] = {release.CONFIG_PATH: TOML.encode()}
    entries = []
    for line in lines:
        data = line.text.encode()
        files[f"migrations/{line.file}"] = data
        entries.append(chain.ChainEntry(line.file, chain.file_sha256(data), line.mode))
    if lines:
        files[chain.SUM_PATH] = chain.format_sum(chain.Chain(False, tuple(entries))).encode()
    for path, text in modules:
        files[path] = text.encode()
    if tombstones:
        files[chain.TOMBSTONES_PATH] = "".join(
            f'[[drop]]\nobject = "{object_key}"\nreason = "replaced"\n' for object_key in tombstones
        ).encode()
    manifest = Manifest(
        commit=COMMIT,
        release_seq=seq,
        files=tuple(sorted((path, hashlib.sha256(data).hexdigest()) for path, data in files.items())),
        chain_added_in={line.file: seq for line in lines},
    )
    return Bundle(manifest, files)


def chain_sha(b: Bundle, file: str) -> str:
    return chain.file_sha256(b.files[f"migrations/{file}"])


# ------------------------------------------------------------------ a recorded state
def live_row(
    object_key: str, definition: str, *, ansi: bool = True, quoted: bool = True, schema_bound: bool = False
) -> tuple:
    """One row of the capture_modules result set, in its column order."""
    kind, schema, name = names.parse_object_key(object_key)
    trigger = ("sales", "Order", 0, "INSERT:0:0") if kind == "TRIGGER" else (None, None, None, None)
    return (
        *(object_key, schema, name, TYPE_CODES[kind], definition, ansi, quoted, schema_bound),
        *(None, 1, *trigger),
    )


def capture(live: tuple) -> dict[str, Any]:
    """The capture that the tool makes of a catalog row: what a deploy records."""
    db = FakeSession()
    db.respond("azsqlcd:capture_modules", [[live]])
    return catalog.capture_modules(db, [live[0]])[live[0]]


def row(object_key: str, text: str, *, source: str | None = SAME, **flags: bool) -> ObjectRow:
    """The object row of a module that was deployed with this text. source None = deploy the file again.

    The capture holds the definition as the engine keeps it (CREATE OR ALTER is stored as CREATE);
    the source is the checksum of the text that was sent."""
    captured = capture(live_row(object_key, stored_text(text), **flags))
    recorded_source = checksum(text.encode()) if source == SAME else source
    return ObjectRow("managed", recorded_source, 1, captured, state.capture_sha256(captured))


def run_row(run_id: int, status: str, seq: int = 5, command: str = "deploy") -> RunRow:
    return RunRow(run_id, command, status, 0, seq, RECORDED_COMMIT, "2026-10-01T10:00:00.000")


def recorded(
    *,
    steps: Sequence[StepRow] = (),
    objects: dict[str, ObjectRow] | None = None,
    seq: int = 6,
    open_runs: Sequence[RunRow] = (),
    env: str = "dev",
) -> State:
    """seq is the recorded release: 0 = no run ended ok."""
    latest = run_row(1, "ok", seq) if seq else None
    return State(Meta(1, "sales", env), tuple(open_runs), latest, tuple(steps), objects or {})


# ------------------------------------------------------------------ a database
DROP = re.compile(r"DROP (VIEW|PROCEDURE|FUNCTION|TRIGGER) (.+);")
# Microsoft Learn, SET IMPLICIT_TRANSACTIONS: when ON and @@TRANCOUNT = 0, INSERT, UPDATE, DELETE
# and a SELECT from a table begin a transaction; a SELECT that reads no table begins none
OPENS_IMPLICIT = re.compile(r"(^|; )(UPDATE|INSERT INTO|DELETE) |FROM \[azsqlcd\]")


class Db(FakeSession):
    """FakeSession, and what a run reads back from a database.

    catalog: the modules that exist, as rows of capture_modules. A module file of the release that
      is sent stores the module (stored_as[key] gives another row than the file would, None stores
      nothing); a DROP of a module removes it.
    fence: run.segments_committed as committed. A COMMIT adds the bump of its transaction.
    options: the row that the session reports for its SET options.
    implicit: SET IMPLICIT_TRANSACTIONS is ON. A batch that holds the statement switches it on. Then
      a write or a read of a state table at @@TRANCOUNT 0 opens a transaction that nobody commits.
    indexed_views: names of the views that have an index in the catalog; no other object has one.
    The model changes only when a batch did not fail. A rule that a test adds wins over the
    answers of this class.
    """

    def __init__(self, b: Bundle, st: State, *, facts: tuple = FACTS, user_objects: Sequence[tuple] = ()):
        super().__init__()
        self.catalog: dict[str, tuple] = {
            object_key: live_row(
                object_key,
                r.capture["definition"],
                ansi=r.capture["uses_ansi_nulls"],
                quoted=r.capture["uses_quoted_identifier"],
                schema_bound=r.capture["is_schema_bound"],
            )
            for object_key, r in st.objects.items()
            if r.status == "managed" and "definition" in r.capture
        }
        self.stored_as: dict[str, tuple | None] = {}
        self.fence = 0
        self.options: tuple = session_options(CONFIG.env[st.meta.environment].lock_timeout_ms)
        self.implicit = False
        self.indexed_views: set[str] = set()
        self._bumped = 0
        self._files = {
            module.text: module
            for module in (read_module(path, data) for path, data in b.files.items() if path.count("/") == 2)
        }
        self._state, self._facts, self._user_objects = st, facts, user_objects
        self._answers = False

    def _answer_the_reads(self) -> None:
        st, facts, user_objects = self._state, self._facts, self._user_objects
        runs = [*st.open_runs, *([st.latest_ok_run] if st.latest_ok_run else [])]
        if st.committed_release_seq:  # a deploy that committed a unit of work and did not end ok
            seq = st.committed_release_seq
            runs.append(RunRow(30, "deploy", "failed", 1, seq, COMMIT, "2026-10-02T10:00:00.000"))
        runs.sort(key=lambda r: r.run_id)
        self.respond("azsqlcd:fence_facts", [[facts]])
        self.respond("azsqlcd:session_options", lambda _: [[self._options_now()]])
        self.respond(
            "azsqlcd:has_index",
            lambda batch: [[(int(any(f"[{name}]" in batch for name in self.indexed_views)),)]],
        )
        self.respond("azsqlcd:read_state.tables", [[(table, 1) for table in state.TABLES]])
        self.respond("azsqlcd:read_state.meta", [[dataclasses.astuple(st.meta)]])
        self.respond("azsqlcd:read_state.runs", [[dataclasses.astuple(r) for r in runs]])
        self.respond("azsqlcd:read_state.steps", [[dataclasses.astuple(step) for step in st.steps]])
        self.respond(
            "azsqlcd:read_state.objects",
            [
                [
                    (
                        k,
                        r.status,
                        r.source_sha256,
                        r.capture_format,
                        state.capture_json(r.capture),
                        r.catalog_sha256,
                    )
                    for k, r in sorted(st.objects.items())
                ]
            ],
        )
        self.respond("azsqlcd:capture_modules", self._capture)
        self.respond(
            "azsqlcd:list_user_objects",
            lambda _: [[*((r[1], r[2], r[3]) for r in self.catalog.values()), *user_objects]],
        )
        self.respond("azsqlcd:object_exists", self._exists)
        self.respond("azsqlcd:service_objective", [[("GP_S_Gen5_2",)]])
        self.respond("INSERT INTO [azsqlcd].[run]", [[(RUN_ID,)]])
        self.respond("azsqlcd:run_started", [[(STARTED,)]])
        self.respond("azsqlcd:read_fence", lambda _: [[(self.fence,)]])

    def _options_now(self) -> tuple:
        if not self.implicit:
            return self.options
        return (*self.options[:IMPLICIT_BIT], 2, *self.options[IMPLICIT_BIT + 1 :])

    def _capture(self, batch: str) -> ResultSets:
        if "VALUES" not in batch:
            return [list(self.catalog.values())]
        return [[live for object_key, live in self.catalog.items() if f"(N'{object_key}'," in batch]]

    def _exists(self, batch: str) -> ResultSets:
        return [[(int(any(f"N'{k.split(':', 1)[1]}'" in batch for k in self.catalog)),)]]

    def execute(self, batch: str) -> ResultSets:
        if not self._answers:  # at the first batch, so the rules of a test come first and win
            self._answers = True
            self._answer_the_reads()
        before = self.trancount
        result = super().execute(batch)
        if "SET IMPLICIT_TRANSACTIONS OFF" in batch:
            self.implicit = False
        elif "SET IMPLICIT_TRANSACTIONS ON" in batch:
            self.implicit = True
        elif self.implicit and before == 0 and self.trancount == 0 and OPENS_IMPLICIT.search(batch):
            self.trancount, self.xact_state = 1, 1
        module = self._files.get(batch)
        dropped = DROP.fullmatch(batch)
        if module is not None:
            # the engine keeps the batch with the verb CREATE (modules.stored_text, live spike L7)
            stored = self.stored_as.get(
                module.key, live_row(module.key, stored_text(batch), schema_bound=module.schema_bound)
            )
            if stored is not None:
                self.catalog[module.key] = stored
        elif dropped:
            self.catalog.pop(f"{dropped[1]}:{dropped[2]}", None)
        elif "[segments_committed] + 1" in batch:
            self._bumped = 1
        elif batch == "COMMIT TRANSACTION;":
            self.fence += self._bumped
            self._bumped = 0
        return result

    def after(self, matcher: str) -> list[str]:
        """The batches that were sent after the first batch that holds the text."""
        return self.batches[self.index_of(matcher) + 1 :]


class Tokens:
    def __init__(self, minutes: int = 60) -> None:
        self.minutes = minutes
        self.calls = 0

    def get(self) -> AccessToken:
        self.calls += 1
        return AccessToken("the-access-token", int(NOW + self.minutes * 60))


def run_deploy(
    b: Bundle,
    st: State,
    db: FakeSession | None = None,
    *,
    env: str = "dev",
    expect: Plan | None = None,
    opened: list | None = None,
    sessions: Sequence[FakeSession] = (),
    **options: Any,
) -> Report:
    """deploy() on one target. Without an expected plan it is `deploy --inline-plan`."""
    # an inline plan has no plan job: the deploy opens the session of the syntax check itself
    queue = [db or Db(b, st), *(sessions or ([] if expect else [parse_only_session()]))]
    opened = [] if opened is None else opened

    def session_factory() -> FakeSession:
        opened.append(queue[len(opened)])
        return opened[-1]

    options = {"inline_plan": expect is None, "token_provider": Tokens(), "audit": Audit()} | options
    return deploy(
        b,
        CONFIG,
        env,
        f"sales-{env}",
        expect_plan=expect,
        session_factory=session_factory,
        tool_version="0.1.0",
        tool_digest=TOOL_DIGEST,
        now=lambda: NOW,
        **options,
    )


def parse_only_session() -> FakeSession:
    """A second session that behaves as the engine does under SET PARSEONLY ON."""
    second = FakeSession()
    second.fail_on("SELECT FROM;", sql_error("Incorrect syntax near the keyword 'FROM'.", number=156))
    return second


def failure(*args: Any, **kwargs: Any) -> RunError:
    with pytest.raises(RunError) as caught:
        run_deploy(*args, **kwargs)
    assert caught.value.report.exit_code == caught.value.exit_code  # the report says what the error says
    assert caught.value.report.reason_code == caught.value.reason_code
    return caught.value


def plan_job(b: Bundle, st: State, env: str = "prod") -> Plan:
    """The plan of the plan job: the same function, on its own session, with no lock."""
    return compute_plan(
        b, CONFIG, env, f"sales-{env}", Db(b, st), tool_version="0.1.0", tool_digest=TOOL_DIGEST
    )


def work_texts(b: Bundle, report: Report) -> list[str]:
    """The text of every batch and module of the plan that the run computed."""
    assert report.plan is not None
    return [text for unit in report.plan.units for text in plan.step_texts(b, unit).values()]


def run_status(db: FakeSession, status: str) -> list[str]:
    return db.sent(f"UPDATE [azsqlcd].[run] SET [status] = N'{status}'")


def object_writes(db: FakeSession) -> list[str]:
    return db.sent(re.compile(r"(INSERT INTO|UPDATE) \[azsqlcd\]\.\[object\]"))


def step_writes(db: FakeSession) -> list[str]:
    return db.sent(re.compile(r"(INSERT INTO|UPDATE) \[azsqlcd\]\.\[step\]"))


CHANGED, NEW, OLD = key("PROCEDURE", "usp_changed"), key("VIEW", "vw_new"), key("PROCEDURE", "usp_old")


def a_release(env: str = "dev", seq: int = 6) -> tuple[Bundle, State]:
    """One migration of two batches, one changed module, one new module, one drop."""
    b = bundle(
        mig(M1, ADD_C, ADD_D), modules=[proc("usp_changed", "SELECT 2;"), view("vw_new")], tombstones=[OLD]
    )
    objects = {
        CHANGED: row(CHANGED, "CREATE PROCEDURE [sales].[usp_changed] AS SELECT 1;", source="1" * 64),
        OLD: row(OLD, "CREATE PROCEDURE [sales].[usp_old] AS SELECT 0;"),
    }
    return b, recorded(objects=objects, seq=seq, env=env)


def a_migration() -> tuple[Bundle, State]:
    """One migration of two batches and nothing else."""
    return bundle(mig(M1, ADD_C, ADD_D)), recorded()


def a_nontx_migration() -> tuple[Bundle, State]:
    return bundle(mig(M1, BUILD_INDEX, mode="nontx")), recorded()


# ------------------------------------------------------------------ the unit of work
def test_a_release_is_one_transaction_under_the_lock_and_the_run_row_records_it():
    b, st = a_release()
    db = Db(b, st)
    report = run_deploy(b, st, db)
    changed_text, new_text = proc("usp_changed", "SELECT 2;")[1], view("vw_new")[1]
    db.assert_order(
        "SET XACT_ABORT ON; SET LOCK_TIMEOUT 30000; SET NOCOUNT ON;",
        "azsqlcd:session_options",
        "azsqlcd:fence_facts",
        "sp_getapplock @Resource = N'azsqlcd:deploy', @LockMode = N'Exclusive', @LockOwner = N'Session', "
        "@LockTimeout = 600000",
        "azsqlcd:read_state.runs",
        "INSERT INTO [azsqlcd].[run]",
        "BEGIN TRANSACTION",
        ADD_C,
        ADD_D,
        new_text,  # views before procedures
        changed_text,
        "DROP PROCEDURE [sales].[usp_old];",
        "COMMIT TRANSACTION;",
        "UPDATE [azsqlcd].[run] SET [status] = N'ok'",
        "sp_releaseapplock",
    )
    begin = db.sent("BEGIN TRANSACTION")
    assert len(begin) == 1 and len(db.sent("COMMIT TRANSACTION;")) == 1
    # the first batch of the unit: this session, this lock, the transaction, the fence
    assert begin[0].startswith(
        "IF @@SPID <> 57 OR APPLOCK_MODE(N'public', N'azsqlcd:deploy', N'Session') <> N'Exclusive' THROW"
    )
    assert "[segments_committed] = [segments_committed] + 1 WHERE [run_id] = 41" in begin[0]
    assert db.fence == 1 and db.trancount == 0 and db.closed and not db.applock_held
    assert report.plan is not None
    assert dataclasses.replace(report, plan=None, warnings=()) == Report(
        command="deploy",
        exit_code=0,
        reason_code="OK",
        message="release r7 is recorded as run 41: 5 plan step(s) applied, 2 module(s) deployed, 1 dropped",
        environment="dev",
        target_id="sales-dev",
        run_id=RUN_ID,
        started_utc=STARTED,
        release_seq=7,
        git_sha=COMMIT,
        plan_sha256=report.plan.plan_sha256,
        tool_version="0.1.0",
        tool_digest=TOOL_DIGEST,
        steps_applied=(f"{M1}#1", f"{M1}#2", f"module:{NEW}", f"module:{CHANGED}", f"drop:{OLD}"),
        modules_deployed=(NEW, CHANGED),
        modules_dropped=(OLD,),
        auth="entra",  # the kind of sign-in of the token provider of the test
        syntax_check="ran in this run",  # --inline-plan: the second session parsed the texts
    )


def test_the_guard_reads_the_transaction_after_every_batch_of_the_unit():
    b, st = a_release()
    db = Db(b, st)
    report = run_deploy(b, st, db)
    for text in [*work_texts(b, report), "DROP PROCEDURE [sales].[usp_old];", "BEGIN TRANSACTION"]:
        assert "azsqlcd:guard" in db.after(text)[0], text
    assert "azsqlcd:guard" in db.batches[db.index_of("COMMIT TRANSACTION;") - 1]
    assert "azsqlcd:trancount" in db.after("COMMIT TRANSACTION;")[0]


def test_no_batch_is_sent_twice():
    b, st = a_release()
    db = Db(b, st)
    report = run_deploy(b, st, db)
    texts = work_texts(b, report)
    assert len(texts) == 4
    assert [db.batches.count(text) for text in texts] == [1, 1, 1, 1]

    # an error that is safe to start again is still not sent again by the tool
    db = Db(b, st)
    db.fail_on(ADD_D, sql_error(LOCK_TIMEOUT), times=5)
    error = failure(b, st, db)
    assert error.exit_code == Exit.RETRY_SAFE
    assert [db.batches.count(text) for text in texts] == [1, 1, 0, 0]


def test_the_dispatch_log_refuses_a_step_that_was_sent_before():
    db = FakeSession()
    run = runner._Run(job=None, session=db)  # type: ignore[arg-type]
    runner._send(run, f"{M1}#1", ADD_C)
    with pytest.raises(RuntimeError, match="no batch is sent twice"):
        runner._send(run, f"{M1}#1", ADD_C)
    assert db.batches == [ADD_C] and run.dispatch_log == [f"{M1}#1"] and run.dispatched


def test_step_rows_are_inserted_after_the_last_batch_and_before_commit():
    b, st = a_release()
    db = Db(b, st)
    run_deploy(b, st, db)
    migration, modules_step = step_writes(db)
    assert f"N'migration', N'{M1}', N'{chain_sha(b, M1)}', N'ok'" in migration
    assert "N'modules', NULL, NULL, N'ok'" in modules_step and "N'deployed 2, dropped 1'" in modules_step
    assert (
        migration.startswith("INSERT INTO [azsqlcd].[step] ([run_id], [kind]") and f"({RUN_ID}, " in migration
    )
    last_batch = max(db.index_of(text) for text in ("DROP PROCEDURE [sales].[usp_old];", ADD_D))
    assert last_batch < db.index_of("INSERT INTO [azsqlcd].[step]") < db.index_of("COMMIT TRANSACTION;")
    assert db.trancount == 0


def test_object_rows_hold_the_capture_and_the_checksum_of_what_was_read_back_in_the_transaction():
    b, st = a_release()
    db = Db(b, st)
    run_deploy(b, st, db)
    path, text = proc("usp_changed", "SELECT 2;")
    upsert = db.sent(lambda batch: batch.startswith("UPDATE [azsqlcd].[object]") and CHANGED in batch)
    assert len(upsert) == 1
    # the capture holds the definition as the engine keeps it; the source is the checksum of the file
    expected = capture(live_row(CHANGED, stored_text(text)))
    assert stored_text(text).startswith("CREATE   PROCEDURE [sales].[usp_changed]")
    assert f"[source_sha256] = N'{checksum(text.encode())}'" in upsert[0]
    assert f"[catalog_sha256] = N'{state.capture_sha256(expected)}'" in upsert[0]
    assert "IF @@ROWCOUNT = 0 INSERT INTO [azsqlcd].[object]" in upsert[0]
    dropped = db.sent(lambda batch: "[status] = N'dropped'" in batch and OLD in batch)
    assert len(dropped) == 1
    readback = [i for i, batch in enumerate(db.batches) if "azsqlcd:capture_modules" in batch][-1]
    assert (
        db.index_of("DROP PROCEDURE") < readback < db.index_of(upsert[0]) < db.index_of("COMMIT TRANSACTION;")
    )


def test_a_batch_error_leaves_no_step_row_and_the_run_is_failed():
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_D, sql_error("Invalid column name 'd'.", number=207))
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "BATCH_FAILED")
    assert step_writes(db) == [] and object_writes(db) == [] and db.sent("COMMIT") == []
    # rolled back, proven by @@TRANCOUNT and by the fence, then the run row
    rollback, trancount, fence, status = db.after(ADD_D)
    assert rollback == "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;"
    assert trancount.endswith("SELECT @@TRANCOUNT;") and "azsqlcd:read_fence */" in fence
    assert status.startswith("UPDATE [azsqlcd].[run] SET [status] = N'failed'")
    failed = run_status(db, "failed")[0]
    assert f"[failed_step] = N'{M1}#2'" in failed and "[error_number] = 207" in failed
    assert "[note] = N'BATCH_FAILED'" in failed and f"WHERE [run_id] = {RUN_ID}" in failed
    assert db.closed and db.fence == 0
    report = error.report
    assert (report.run_id, report.started_utc, report.failed_step) == (RUN_ID, STARTED, f"{M1}#2")
    assert (report.steps_applied, report.modules_deployed, report.modules_dropped) == ((), (), ())


@pytest.mark.parametrize(
    "after_the_batch",
    [
        {"trancount": 0, "xact_state": 0},  # the batch committed or rolled back the transaction
        {"trancount": 2},  # the batch began a transaction
        {"xact_state": -1},  # the transaction can only be rolled back
        {"transaction_id": 999_999},  # the batch ended the transaction and began another
    ],
)
def test_a_guard_that_is_not_1_1_id_stops_dispatch_and_exits_23(after_the_batch):
    b, st = a_release()
    db = Db(b, st)

    def batch_changes_the_transaction(_: str) -> ResultSets:
        vars(db).update(after_the_batch)
        return []

    db.respond(ADD_C, batch_changes_the_transaction)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "GUARD_FAILED")
    guard, *later = db.after(ADD_C)
    assert "azsqlcd:guard" in guard
    # nothing more is sent, only the status of the run; an open transaction must not take it away
    assert len(later) == 1
    assert later[0].startswith(
        "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION; UPDATE [azsqlcd].[run] SET [status] = N'unknown'"
    )
    assert f"[failed_step] = N'{M1}#1'" in later[0] and "[note] = N'GUARD_FAILED'" in later[0]
    assert db.sent(ADD_D) == [] and step_writes(db) == [] and db.sent("COMMIT TRANSACTION;") == []
    assert db.sent("sp_releaseapplock") == [] and db.closed


@pytest.mark.parametrize(
    "answer",
    [
        (0, 0, 1001),  # BEGIN TRANSACTION did not open a transaction: the batches would run in autocommit
        (2, 1, 1001),  # the session was in a transaction already
        (1, -1, 1001),  # the new transaction can only be rolled back
        (1, 0, 1001),
    ],
)
def test_a_begin_batch_that_leaves_no_sound_transaction_sends_no_migration_batch(answer):
    """A5 (TQ-02): the first guard is read directly after BEGIN TRANSACTION. Without it the first
    migration batch would be sent before anything proves that a transaction holds it."""
    b, st = a_release()
    db = Db(b, st)
    db.respond("azsqlcd:guard", [[answer]])
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "GUARD_FAILED")
    assert error.report.failed_step == "begin"
    assert db.sent(ADD_C) == [] and db.sent("CREATE OR ALTER") == [] and step_writes(db) == []
    begin_batch, guard, status = db.batches[db.index_of("BEGIN TRANSACTION") :]
    assert "azsqlcd:guard" in guard and "[status] = N'unknown'" in status
    assert db.sent("COMMIT TRANSACTION;") == []


def after_batch(db: FakeSession, text: str, tag: str):
    """A matcher for the first batch with the tag that is sent after the batch with the text."""
    return lambda batch: tag in batch and bool(db.sent(text))


NOT_A_GUARD_ROW = [
    [],  # no result set: the driver could not describe it
    [[]],  # a result set with no row
    [[(1, 1)]],  # a row of another width
    [[(1, 1, 1001), (1, 1, 1001)]],  # two rows
    [[(1, 1, 1001)], [(1, 1, 1001)]],  # two result sets
    [[(1, 1, None)]],
    [[(None, None, None)]],
    [[("1", "1", "1001")]],  # not numbers
    [[(True, True, 1001)]],
]


@pytest.mark.parametrize("answer", NOT_A_GUARD_ROW)
@pytest.mark.parametrize("guard", ["of the new transaction", "after a batch"])
def test_a_guard_query_with_no_result_set_or_a_result_of_another_shape_is_a_guard_failure(guard, answer):
    """Not a pass: the transaction is not proven. Not a crash: the run row must say unknown."""
    b, st = a_release()
    db = Db(b, st)
    if guard == "after a batch":
        db.respond(after_batch(db, ADD_C, "azsqlcd:guard"), answer)
    else:
        db.respond("azsqlcd:guard", answer)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "GUARD_FAILED")
    assert error.report.failed_step == (f"{M1}#1" if guard == "after a batch" else "begin")
    # nothing more is sent, only the status of the run
    (status,) = db.after(ADD_C)[1:] if guard == "after a batch" else db.after("azsqlcd:guard")
    assert status.startswith(
        "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION; UPDATE [azsqlcd].[run] SET [status] = N'unknown'"
    )
    assert db.sent(ADD_D) == [] and step_writes(db) == [] and db.sent("COMMIT TRANSACTION;") == []


@pytest.mark.parametrize("answer", [[], [[]], [[(0, 0)]], [[(None,)]], [[("0",)]], [[(1,)]]])
def test_a_commit_that_is_not_proven_by_a_trancount_of_zero_is_unknown_not_ok(answer):
    b, st = a_release()
    db = Db(b, st)
    db.respond("azsqlcd:trancount", answer)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "GUARD_FAILED")
    assert error.report.failed_step == "commit" and error.report.steps_applied == ()
    assert run_status(db, "unknown") and not run_status(db, "ok") and not run_status(db, "failed")


@pytest.mark.parametrize("answer", [[], [[(0, 0)]], [[(1,)]]])
def test_a_nontx_batch_that_is_not_proven_to_leave_no_transaction_is_unknown(answer):
    b, st = a_nontx_migration()
    db = Db(b, st)
    db.respond("azsqlcd:trancount", answer)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "GUARD_FAILED")
    assert db.sent("BEGIN TRANSACTION") == []  # the closing transaction does not start
    assert "[status] = N'unknown'" in db.batches[-1] and "[azsqlcd].[step]" in db.batches[-1]


ANY_CLASS = [
    sql_error("General error.", sqlstate="HY000"),  # how a killed session can first show
    sql_error(LOCK_TIMEOUT),
    sql_error(DEADLOCK),
    sql_error(LOG_FULL),
]


@pytest.mark.parametrize("error_of_the_guard", ANY_CLASS)
def test_an_error_of_any_class_on_the_guard_path_makes_the_run_unknown(error_of_the_guard):
    """A guard query reads session state only. When it fails, what the session holds is not known:
    the run is never "rolled back" and never "start again", whatever the class of the error."""
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(after_batch(db, ADD_C, "azsqlcd:guard"), error_of_the_guard)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "GUARD_FAILED")
    (status,) = db.after(ADD_C)[1:]
    assert status.startswith(
        "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION; UPDATE [azsqlcd].[run] SET [status] = N'unknown'"
    )
    assert not run_status(db, "failed") and db.sent(ADD_D) == [] and step_writes(db) == []
    assert "[error_text] = N'" in status  # the run row says what the guard query met, redacted


def test_a_guard_error_on_a_session_that_is_closed_goes_the_way_of_a_lost_connection():
    b, st = a_release()
    db = Db(b, st)

    def dies_with_a_general_error(_: str) -> ResultSets:
        db.closed = True  # the session is gone; the error does not say so
        raise sql_error("General error.", sqlstate="HY000")

    db.respond(after_batch(db, ADD_C, "azsqlcd:guard"), dies_with_a_general_error)
    second = second_session(0)
    error = failure(b, st, db, sessions=[parse_only_session(), second])
    # a lost connection, not GUARD_FAILED: the locking read on a new session decides the outcome
    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, "CONNECTION_LOST_ROLLED_BACK")
    assert "azsqlcd:guard" in db.batches[-1]  # nothing can be sent on a closed session, and nothing is
    assert second.sent("WITH (READCOMMITTEDLOCK, ROWLOCK)") and second.closed


@pytest.mark.parametrize("fails", ["ROLLBACK TRANSACTION", "azsqlcd:trancount", "azsqlcd:read_fence"])
@pytest.mark.parametrize("error_of_the_proof", ANY_CLASS)
def test_an_error_of_any_class_on_the_rollback_path_makes_the_run_unknown(fails, error_of_the_proof):
    """After a failed batch the unit is "rolled back" only when the rollback, @@TRANCOUNT = 0 and
    the fence were all read. An error there is never exit 21 or 24."""
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_D, sql_error(LOCK_TIMEOUT))  # alone, this is exit 24: rolled back, start again
    db.fail_on(after_batch(db, ADD_D, fails), error_of_the_proof)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "ROLLBACK_UNVERIFIED")
    assert run_status(db, "unknown") and not run_status(db, "failed")


@pytest.mark.parametrize("fails", ["ROLLBACK TRANSACTION", "azsqlcd:trancount", "azsqlcd:read_fence"])
def test_a_session_that_is_closed_on_the_rollback_path_goes_the_way_of_a_lost_connection(fails):
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_D, sql_error("Invalid column name 'd'.", number=207))

    def dies_with_a_general_error(_: str) -> ResultSets:
        db.closed = True
        raise sql_error("General error.", sqlstate="HY000")

    db.respond(after_batch(db, ADD_D, fails), dies_with_a_general_error)
    second = second_session(0)
    error = failure(b, st, db, sessions=[parse_only_session(), second])
    # not BATCH_FAILED: the rollback was not proven on this session. The new session reads it
    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, "CONNECTION_LOST_ROLLED_BACK")
    assert not run_status(db, "failed") and not run_status(db, "unknown")  # nothing could be written
    assert len(run_status(second, "failed")) == 1  # the new session closes the run row


@pytest.mark.parametrize("fails", ["ROLLBACK TRANSACTION", "azsqlcd:trancount", "azsqlcd:read_fence"])
def test_a_session_that_is_closed_on_the_rollback_path_is_unknown_when_no_new_session_can_read(fails):
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_D, sql_error("Invalid column name 'd'.", number=207))
    db.kill_on(after_batch(db, ADD_D, fails))
    second = second_session(None)  # the run row gives no fence
    error = failure(b, st, db, sessions=[parse_only_session(), second])
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "CONNECTION_LOST_TX")
    assert not run_status(db, "failed") and not run_status(second, "failed")


@pytest.mark.parametrize(
    ("tag", "answer", "reason_code"),
    [
        ("azsqlcd:trancount", [], "ROLLBACK_UNVERIFIED"),
        ("azsqlcd:trancount", [[(None,)]], "ROLLBACK_UNVERIFIED"),
        ("azsqlcd:trancount", [[(0, 0)]], "ROLLBACK_UNVERIFIED"),
        ("azsqlcd:read_fence", [], "FENCE_MISMATCH"),
        ("azsqlcd:read_fence", [[]], "FENCE_MISMATCH"),  # the run row is gone
        ("azsqlcd:read_fence", [[(None,)]], "FENCE_MISMATCH"),
        ("azsqlcd:read_fence", [[(0,), (0,)]], "FENCE_MISMATCH"),
    ],
)
def test_a_rollback_proof_of_another_shape_is_not_a_proof(tag, answer, reason_code):
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_D, sql_error("Invalid column name 'd'.", number=207))
    db.respond(after_batch(db, ADD_D, tag), answer)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, reason_code)
    assert run_status(db, "unknown") and not run_status(db, "failed")


def test_a_plan_hash_mismatch_sends_zero_ddl():
    b, st = a_release(env="prod", seq=6)
    expected = plan_job(b, a_release(env="prod", seq=5)[1])  # the plan job saw another recorded release
    db = Db(b, st)
    error = failure(b, st, db, env="prod", expect=expected)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "STALE_PLAN")
    assert error.detail == {"expected": expected.plan_sha256, "computed": error.report.plan_sha256}
    assert expected.plan_sha256 != error.report.plan_sha256
    assert db.sent("sp_getapplock")  # the plan was computed under the lock
    for text in ("ALTER", "DROP", "INSERT", "UPDATE", "BEGIN TRANSACTION", "sp_refreshsqlmodule"):
        assert db.sent(re.compile(rf"(^|[; ]){text} ")) == [], text
    assert error.report.run_id is None and db.closed


def test_the_plan_of_the_plan_job_runs_when_the_plan_under_the_lock_has_the_same_hash():
    b, st = a_release(env="prod")
    expected = plan_job(b, st)
    db = Db(b, st)
    report = run_deploy(b, st, db, env="prod", expect=expected)
    assert (report.exit_code, report.plan_sha256) == (0, expected.plan_sha256)
    assert f"N'{expected.plan_sha256}'" in db.sent("INSERT INTO [azsqlcd].[run]")[0]
    assert "SET LOCK_TIMEOUT 10000;" in db.batches[0]


def test_deploy_needs_exactly_one_of_the_expected_plan_and_the_inline_plan():
    b, st = a_release(env="prod")
    for options in ({"expect": plan_job(b, st), "inline_plan": True}, {"inline_plan": False}):
        opened: list = []
        with pytest.raises(ValueError, match="exactly one"):
            run_deploy(b, st, opened=opened, env="prod", **options)
        assert opened == []


def test_inline_plan_is_refused_for_a_gated_environment():
    b, st = a_release(env="prod")
    opened: list = []
    tokens = Tokens()
    error = failure(b, st, env="prod", opened=opened, token_provider=tokens)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "INLINE_PLAN_GATED")
    assert opened == [] and tokens.calls == 0  # no session and no token: nothing can have run
    # an environment without reviewers computes its plan under the lock in the deploy job (A26)
    assert run_deploy(*a_release(env="dev")).exit_code == 0


def test_an_inline_plan_runs_the_syntax_check_on_a_second_session_before_anything_is_executed():
    b, st = a_release()
    db, second = Db(b, st), parse_only_session()
    opened: list = []
    report = run_deploy(b, st, db, opened=opened, sessions=[second])
    assert opened == [db, second] and second.closed
    assert second.batches[0] == "SET PARSEONLY ON;" and second.batches[3:] == work_texts(b, report)
    assert not any("skipped" in note for note in report.warnings)

    # a batch that does not parse refuses the run: no run row, no transaction
    db, second = Db(b, st), parse_only_session()
    second.fail_on(ADD_D, sql_error("Incorrect syntax near 'ADD'.", number=102))
    error = failure(b, st, db, sessions=[second])
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "PARSEONLY_FAILED")
    assert db.sent("INSERT INTO [azsqlcd].[run]") == [] and db.sent("BEGIN TRANSACTION") == []
    assert db.sent(ADD_C) == [] and db.closed


def test_with_an_expected_plan_the_deploy_opens_one_session():
    b, st = a_release(env="prod")
    opened: list = []
    report = run_deploy(b, st, env="prod", expect=plan_job(b, st), opened=opened)
    assert len(opened) == 1 and report.exit_code == 0  # the plan job ran the syntax check


def syntax_warnings(report: Report) -> list[str]:
    return [note for note in report.warnings if "syntax check" in note]


def test_a_deploy_of_an_approved_plan_whose_syntax_check_ran_does_not_warn_that_it_was_skipped():
    """Pilot finding: report.json of every deploy of an approved plan held the warning "the syntax
    check (SET PARSEONLY ON) was skipped: this run has no second session". The plan job had parsed
    every text; the deploy has one session by design."""
    b, st = a_release(env="prod")
    approved = compute_plan(
        b,
        CONFIG,
        "prod",
        "sales-prod",
        Db(b, st),
        tool_version="0.1.0",
        tool_digest=TOOL_DIGEST,
        open_second_session=parse_only_session,
    )
    assert approved.syntax_check == "ran"
    opened: list = []
    report = run_deploy(b, st, env="prod", expect=Plan.from_json(approved.to_json()), opened=opened)
    assert (report.exit_code, len(opened), report.plan_sha256) == (0, 1, approved.plan_sha256)
    assert syntax_warnings(report) == [] and json.loads(report.to_json())["warnings"] == list(report.warnings)


def test_a_deploy_of_an_approved_plan_that_was_computed_with_no_second_session_still_warns():
    b, st = a_release(env="prod")
    approved = plan_job(b, st)  # no second session: the plan job did not parse the texts
    assert approved.syntax_check == "skipped"
    assert "the syntax check (SET PARSEONLY ON) was skipped: this run has no second session" in approved.notes
    report = run_deploy(b, st, env="prod", expect=approved)
    assert report.exit_code == 0 and syntax_warnings(report) == [
        "the syntax check (SET PARSEONLY ON) was skipped: the plan job had no second session, and the "
        "deploy of an approved plan does not run the check"
    ]


def test_a_deploy_of_a_plan_file_that_does_not_record_the_syntax_check_warns_that_it_is_not_proven():
    b, st = a_release(env="prod")
    doc = json.loads(plan_job(b, st).to_json())
    del doc["syntax_check"]  # plan.json of a tool that did not write the key
    report = run_deploy(b, st, env="prod", expect=Plan.from_json(json.dumps(doc)))
    assert report.exit_code == 0 and syntax_warnings(report) == [
        "the syntax check (SET PARSEONLY ON) is not proven: the approved plan does not record that the "
        "plan job ran it, and the deploy of an approved plan does not run the check"
    ]


def test_the_report_says_where_the_fact_of_the_syntax_check_comes_from():
    """plan.json is covered by plan_sha256 but for its key syntax_check. A file whose key was
    changed from skipped to ran gives a deploy with no warning. The report must not be silent
    then: it says that the check is a statement of the plan file, not a check of this run."""
    b, st = a_release(env="prod")
    skipped = plan_job(b, st)  # no second session: nothing was parsed
    doc = json.loads(skipped.to_json())
    doc["syntax_check"] = "ran"
    doc["notes"] = [note for note in doc["notes"] if "syntax check" not in note]
    changed = Plan.from_json(json.dumps(doc))  # the hash does not cover the key: the file reads
    assert changed.plan_sha256 == skipped.plan_sha256

    report = run_deploy(b, st, env="prod", expect=changed)

    assert report.exit_code == 0 and syntax_warnings(report) == []
    assert report.syntax_check == "ran in the plan job (read from plan.json; not in plan_sha256)"
    assert json.loads(report.to_json())["syntax_check"] == report.syntax_check
    assert report.syntax_check == runner.SYNTAX_OF_PLAN_FILE


def test_the_report_names_a_syntax_check_of_its_own_run_a_skipped_one_and_none():
    b, st = a_release()
    inline = run_deploy(b, st, opened=[])  # --inline-plan: the check runs here, on a second session
    assert inline.syntax_check == "ran in this run"
    b, st = a_release(env="prod")
    assert run_deploy(b, st, env="prod", expect=plan_job(b, st)).syntax_check == "skipped"
    doc = json.loads(plan_job(b, st).to_json())
    del doc["syntax_check"]
    assert run_deploy(b, st, env="prod", expect=Plan.from_json(json.dumps(doc))).syntax_check == "skipped"
    # a run with no unit of work has no text to parse: None (test_scenarios.py reads it from a
    # deploy that only records a release)
    assert dataclasses.fields(Report)[-2].name == "syntax_check" and Report.syntax_check is None


@pytest.mark.parametrize(
    ("raised", "reason_code"),
    [
        (retry_safe("CONNECT_FAILED", "no connection after 6 attempt(s)"), "CONNECT_FAILED"),
        (sql_error("Login timeout expired", sqlstate="08001"), "CONNECTION_LOST"),
    ],
)
def test_a_session_that_cannot_be_opened_is_a_clean_stop_with_a_report(raised, reason_code):
    b, st = a_release()

    def no_session() -> FakeSession:
        raise raised

    with pytest.raises(RunError) as caught:
        deploy(
            b,
            CONFIG,
            "dev",
            "sales-dev",
            expect_plan=None,
            inline_plan=True,
            session_factory=no_session,
            token_provider=Tokens(),
            audit=Audit(),
            tool_version="0.1.0",
            tool_digest=TOOL_DIGEST,
            now=lambda: NOW,
        )
    report = caught.value.report
    assert (report.exit_code, report.reason_code, report.run_id) == (Exit.RETRY_SAFE, reason_code, None)
    assert (report.release_seq, report.git_sha, report.plan_sha256) == (7, COMMIT, None)


@pytest.mark.parametrize("result", [-1, -2, -3, -999])
def test_an_applock_result_that_is_not_0_or_1_sends_zero_ddl_and_exits_25(result):
    b, st = a_release()
    db = Db(b, st)
    db.applock_result = result
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.LOCKED, "LOCK_NOT_GRANTED")
    assert "sp_getapplock" in db.batches[-1]  # nothing after the lock request, not even a read of the state
    assert db.sent("read_state") == [] and db.closed


def test_a_lock_that_was_granted_after_a_wait_is_held():
    b, st = a_release()
    db = Db(b, st)
    db.applock_result = 1  # granted after other locks were released
    assert run_deploy(b, st, db).exit_code == 0


# ------------------------------------------------------------------ non-transactional step
def test_the_nontx_marker_is_committed_before_the_batch_is_dispatched():
    b, st = a_nontx_migration()
    db = Db(b, st)
    at_dispatch: list[tuple[int, list[str]]] = []

    def record(_: str) -> ResultSets:
        at_dispatch.append((db.trancount, list(db.batches)))
        return []

    db.respond(BUILD_INDEX, record)
    report = run_deploy(b, st, db)
    ((trancount, before),) = at_dispatch  # the batch is sent once
    marker = before[-2]
    assert (
        trancount == 0 and db.sent("BEGIN TRANSACTION")[0] not in before
    )  # autocommit: no transaction is open
    assert marker.startswith("IF @@SPID <> 57 OR APPLOCK_MODE(N'public', N'azsqlcd:deploy', N'Session')")
    assert "INSERT INTO [azsqlcd].[step] ([run_id], [kind], [migration_id], [file_sha256], [status]" in marker
    assert f"VALUES ({RUN_ID}, N'nontx', N'{M1}', N'{chain_sha(b, M1)}', N'started'" in marker
    # then one small transaction closes the step
    db.assert_order(
        BUILD_INDEX,
        "azsqlcd:trancount",
        "BEGIN TRANSACTION",
        "UPDATE [azsqlcd].[step] SET [status] = N'ok'",
        "COMMIT TRANSACTION;",
        "UPDATE [azsqlcd].[run] SET [status] = N'ok'",
    )
    assert (report.exit_code, report.steps_applied) == (0, (f"{M1}#1",))


def test_a_nontx_migration_that_resolve_marked_not_applied_reuses_its_step_row():
    b, st = a_nontx_migration()
    st = dataclasses.replace(st, steps=(StepRow(3, 9, "nontx", M1, chain_sha(b, M1), "not_applied", None),))
    db = Db(b, st)
    run_deploy(b, st, db)
    marker = db.batches[db.index_of(BUILD_INDEX) - 1]
    # the unique index on migration_id allows one row for a migration
    assert "UPDATE [azsqlcd].[step] SET [status] = N'started'" in marker and f"[run_id] = {RUN_ID}" in marker
    assert db.sent("INSERT INTO [azsqlcd].[step]") == []


@pytest.mark.parametrize(
    ("message", "reason_code"),
    [("Incorrect syntax near 'ONLINE'.", "NONTX_FAILED"), (LOG_FULL, "GOVERNANCE_LIMIT")],
)
def test_an_error_in_a_nontx_step_makes_the_step_and_the_run_unknown_and_exits_23(message, reason_code):
    b, st = a_nontx_migration()
    db = Db(b, st)
    db.fail_on(BUILD_INDEX, sql_error(message), times=5)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, reason_code)
    (status,) = db.after(BUILD_INDEX)  # one batch more
    assert "UPDATE [azsqlcd].[step] SET [status] = N'unknown'" in status and f"N'{M1}'" in status
    assert "UPDATE [azsqlcd].[run] SET [status] = N'unknown'" in status
    assert len(db.sent(BUILD_INDEX)) == 1  # nothing is retried
    assert "--mark-applied" in error.message and "--mark-not-applied" in error.message


def test_a_killed_session_in_a_nontx_step_exits_23_and_the_step_stays_started():
    b, st = a_nontx_migration()
    db = Db(b, st)
    db.kill_on(BUILD_INDEX)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "CONNECTION_LOST_NONTX")
    assert BUILD_INDEX in db.batches[-1] and db.sent("= N'unknown'") == []


def test_an_error_after_the_nontx_batch_is_unknown_because_the_batch_is_applied():
    b, st = a_nontx_migration()
    db = Db(b, st)
    db.fail_on("UPDATE [azsqlcd].[step] SET [status] = N'ok'", sql_error(LOCK_TIMEOUT))
    error = failure(b, st, db)
    # in a transaction this is exit 24; here the index is built and its step is not recorded
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "NONTX_FAILED")
    assert "UPDATE [azsqlcd].[step] SET [status] = N'unknown'" in db.batches[-1]


# ------------------------------------------------------------------ failure matrix, transaction
# A session that is lost in the transaction: the run opens a new session, takes the deploy lock
# and reads run.segments_committed with a locking read (live spike L6, acceptance run4). The read
# decides: exit 24. When it cannot decide: exit 23.
def lock_not_granted() -> FakeSession:
    second = second_session(0)
    second.applock_result = -1  # the lost session is not gone yet, or another run took the lock
    return second


@pytest.mark.parametrize(
    ("new_session", "exit_code", "reason_code"),
    [
        (lambda: second_session(0), Exit.RETRY_SAFE, "CONNECTION_LOST_ROLLED_BACK"),
        (lock_not_granted, Exit.UNKNOWN, "CONNECTION_LOST_TX"),
        (lambda: second_session(None), Exit.UNKNOWN, "CONNECTION_LOST_TX"),  # no run row is read
    ],
)
def test_a_killed_session_in_a_tx_unit_is_decided_on_a_new_session_and_nothing_more_is_sent(
    new_session, exit_code, reason_code
):
    b, st = a_release()
    db, second = Db(b, st), new_session()
    db.kill_on(ADD_C)
    opened: list = []
    error = failure(b, st, db, opened=opened, sessions=[second], expect=plan_job(b, st, env="dev"))
    assert (error.exit_code, error.reason_code) == (exit_code, reason_code)
    assert ADD_C in db.batches[-1] and db.closed  # nothing more on the session that was lost
    assert opened == [db, second] and second.closed
    assert error.report.run_id == RUN_ID and error.report.failed_step == f"{M1}#1"
    decided = exit_code is Exit.RETRY_SAFE
    assert len(run_status(second, "failed")) == (1 if decided else 0)
    assert not run_status(second, "unknown") and not second.sent(ADD_C)  # the new session sends no DDL


def test_a_new_session_that_cannot_be_opened_after_a_lost_connection_leaves_the_run_unknown():
    b, st = a_release()
    db = Db(b, st)
    db.kill_on(ADD_C)
    calls: list[int] = []

    def session_factory() -> FakeSession:
        calls.append(1)
        if len(calls) > 1:
            raise retry_safe("CONNECT_FAILED", "the server did not answer")
        return db

    with pytest.raises(RunError) as caught:
        deploy(
            b,
            CONFIG,
            "dev",
            "sales-dev",
            expect_plan=plan_job(b, st, env="dev"),
            inline_plan=False,
            session_factory=session_factory,
            token_provider=Tokens(),
            audit=Audit(),
            tool_version="0.1.0",
            tool_digest=TOOL_DIGEST,
            now=lambda: NOW,
        )
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.UNKNOWN, "CONNECTION_LOST_TX")
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("fence", "exit_code", "reason_code"),
    [
        (1, Exit.RETRY_SAFE, "CONNECTION_LOST_COMMITTED"),  # COMMIT reached the server
        (0, Exit.RETRY_SAFE, "CONNECTION_LOST_ROLLED_BACK"),
        (None, Exit.UNKNOWN, "CONNECTION_LOST_TX"),
    ],
)
def test_a_session_that_is_lost_at_commit_is_read_as_committed_or_rolled_back_on_a_new_session(
    fence, exit_code, reason_code
):
    b, st = a_release()
    db, second = Db(b, st), second_session(fence)
    db.kill_on("COMMIT TRANSACTION;")
    error = failure(b, st, db, sessions=[parse_only_session(), second])
    assert (error.exit_code, error.reason_code, error.report.failed_step) == (
        exit_code,
        reason_code,
        "commit",
    )
    if fence is not None:
        outcome = "committed" if fence else "rolled back"
        (status,) = run_status(second, "failed")
        assert f"N'connection lost; the unit of work was {outcome}'" in status
        assert error.detail == {"run_id": RUN_ID, "committed": bool(fence)}


@pytest.mark.parametrize(("message", "reason_code"), [(LOCK_TIMEOUT, "LOCK_TIMEOUT"), (DEADLOCK, "DEADLOCK")])
def test_lock_timeout_1222_or_deadlock_1205_in_a_tx_unit_rolls_back_and_exits_24(message, reason_code):
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_C, sql_error(message))
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, reason_code)
    db.assert_order(
        ADD_C, "ROLLBACK TRANSACTION", "azsqlcd:trancount", "azsqlcd:read_fence", "[status] = N'failed'"
    )
    assert db.sent(ADD_D) == [] and step_writes(db) == [] and db.trancount == 0


def test_a_lock_timeout_whose_number_cannot_be_read_is_a_failed_batch():
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_C, sql_error("Sperranforderung: Timeout"))  # not the en-US text, no number
    assert failure(b, st, db).exit_code == Exit.FAILED_ROLLED_BACK


def test_a_governance_error_is_not_retried():
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_C, sql_error(LOG_FULL), times=5)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "GOVERNANCE_LIMIT")
    assert error.detail["error_number"] == 9002
    assert len(db.sent(ADD_C)) == 1 and db.sent(ADD_D) == []
    assert "never retried" in error.message and "nontx" in error.message  # the hint of the failure matrix


def test_the_error_text_in_the_run_row_is_redacted():
    b, st = a_release()
    db = Db(b, st)
    message = (
        "Violation of PRIMARY KEY constraint 'PK_Order'. Cannot insert duplicate key in object "
        "'sales.Order'. The duplicate key value is (4711, jo@example.com)."
    )
    db.fail_on(ADD_C, sql_error(message, number=2627))
    error = failure(b, st, db)
    stored = run_status(db, "failed")[0]
    assert "[error_text] = N'Violation of PRIMARY KEY constraint <redacted>." in stored
    assert "[error_number] = 2627" in stored
    for text in (stored, error.message, str(error), error.report.to_json(), json.dumps(error.detail)):
        assert "jo@example.com" not in text and "4711" not in text and "PK_Order" not in text


def test_no_batch_text_and_no_token_reach_the_report_or_the_run_row():
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_D, sql_error("Invalid column name 'd'.", number=207))
    error = failure(b, st, db)
    told = (
        error.report.to_json() + str(error) + json.dumps(error.detail) + "".join(db.sent("[azsqlcd].[run]"))
    )
    for secret in ("ADD [c]", "ADD [d]", "SELECT 2", "the-access-token"):
        assert secret not in told
    assert "plan" not in json.loads(error.report.to_json())  # the plan has its own file


def test_a_fence_that_is_ahead_after_a_rollback_makes_the_run_unknown():
    b, st = a_release()
    db = Db(b, st)

    def commit_and_fail(_: str) -> ResultSets:
        db.fence = 1  # the batch committed the transaction of the unit, then raised
        raise sql_error("Invalid column name 'd'.", number=207)

    db.respond(ADD_D, commit_and_fail)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "FENCE_MISMATCH")
    assert error.detail == {"step": f"{M1}#2", "segments_committed": 1, "known": 0}
    assert run_status(db, "unknown") and not run_status(db, "failed")


def test_a_rollback_that_leaves_a_transaction_open_makes_the_run_unknown():
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(ADD_D, sql_error("Invalid column name 'd'.", number=207))
    db.respond("azsqlcd:trancount", [[(1,)]])
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "ROLLBACK_UNVERIFIED")


@pytest.mark.parametrize(
    ("fails", "exit_code", "reason_code", "run_row"),
    [
        ("azsqlcd:list_user_objects", Exit.REFUSED, "TOOL_DEFECT", None),  # in the plan, no run row yet
        ("azsqlcd:run_started", Exit.REFUSED, "TOOL_DEFECT", "failed"),  # run row, no batch of the unit
        ("BEGIN TRANSACTION", Exit.UNKNOWN, "TOOL_DEFECT_AFTER_DISPATCH", "unknown"),
        (ADD_C, Exit.UNKNOWN, "TOOL_DEFECT_AFTER_DISPATCH", "unknown"),
        ("INSERT INTO [azsqlcd].[step]", Exit.UNKNOWN, "TOOL_DEFECT_AFTER_DISPATCH", "unknown"),
    ],
)
def test_an_unknown_exception_before_dispatch_exits_22_after_dispatch_23(
    fails, exit_code, reason_code, run_row
):
    b, st = a_release()
    db = Db(b, st)
    db.fail_on(fails, KeyError("SELECT [secret] FROM the batch text"))
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (exit_code, reason_code)
    assert isinstance(error.__cause__, ToolError) and isinstance(error.__cause__.__cause__, KeyError)
    assert "KeyError" in error.message and "secret" not in error.message + error.report.to_json()
    assert db.sent("COMMIT TRANSACTION;") == [] and db.closed
    if run_row is None:
        assert db.sent("UPDATE [azsqlcd].[run]") == [] and db.sent("INSERT INTO [azsqlcd].[run]") == []
    else:
        assert len(run_status(db, run_row)) == 1 and f"[note] = N'{reason_code}'" in db.batches[-1]


@pytest.mark.parametrize(
    ("fails", "error", "exit_code", "reason_code", "tells"),
    [
        (
            "INSERT INTO [azsqlcd].[run]",
            "The INSERT permission was denied on the object 'run', database 'sales'.",
            Exit.REFUSED,
            "SQL_ERROR",
            "denied on the object <redacted>, database <redacted>",
        ),
        ("azsqlcd:read_state.steps", LOCK_TIMEOUT, Exit.RETRY_SAFE, "LOCK_TIMEOUT", "Lock request time out"),
        # TQ-04: a deadlock on a state read before dispatch is safe to start again, as a lock timeout is
        ("azsqlcd:read_state.steps", DEADLOCK, Exit.RETRY_SAFE, "DEADLOCK", "deadlock victim"),
        ("azsqlcd:list_user_objects", None, Exit.RETRY_SAFE, "CONNECTION_LOST", "Communication link failure"),
    ],
)
def test_a_database_error_before_the_unit_of_work_executes_nothing(
    fails, error, exit_code, reason_code, tells
):
    b, st = a_release()
    db = Db(b, st)
    if error is None:
        db.kill_on(fails)
    else:
        db.fail_on(fails, sql_error(error))
    found = failure(b, st, db)
    assert (found.exit_code, found.reason_code) == (exit_code, reason_code)
    assert tells in found.message
    assert db.sent("BEGIN TRANSACTION") == [] and db.sent(ADD_C) == [] and db.closed


def test_an_unknown_end_outside_a_unit_of_work_leaves_the_run_row_unknown():
    """TQ-04: the unit is committed and the write of `ok` stops on an error that the tool does not
    know. The run row must say unknown: a row that says failed would let the next plan run as if
    nothing was applied."""
    b, st = a_migration()
    db = Db(b, st)
    db.fail_on("UPDATE [azsqlcd].[run] SET [status] = N'ok'", RuntimeError("native layer"))
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "TOOL_DEFECT_AFTER_DISPATCH")
    assert db.fence == 1
    (status,) = db.sent("UPDATE [azsqlcd].[run] SET [status]")[1:]  # after the write of ok that failed
    assert "[status] = N'unknown'" in status and "[note] = N'TOOL_DEFECT_AFTER_DISPATCH'" in status
    assert run_status(db, "failed") == []


@pytest.mark.parametrize(
    ("rule", "answer"),
    [
        ("azsqlcd:spid", [[(57.5,)]]),  # the session id goes into the text of every guarded batch
        ("azsqlcd:spid", [[(None,)]]),
        ("INSERT INTO [azsqlcd].[run]", [[(0,)]]),  # no identity value: no row was written
        ("INSERT INTO [azsqlcd].[run]", [[(True,)]]),
        ("INSERT INTO [azsqlcd].[run]", [[("41",)]]),
    ],
)
def test_a_session_id_or_a_run_id_that_is_no_whole_number_stops_the_run_before_dispatch(rule, answer):
    b, st = a_migration()
    db = Db(b, st)
    db.respond(rule, answer)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "TOOL_DEFECT")
    assert db.sent("BEGIN TRANSACTION") == [] and db.sent(ADD_C) == []


def test_a_run_row_that_cannot_be_closed_after_the_commit_is_safe_to_start_again():
    b, st = a_release()
    db = Db(b, st)
    db.fail_on("UPDATE [azsqlcd].[run] SET [status] = N'ok'", sql_error(LOCK_TIMEOUT))
    error = failure(b, st, db)
    # the release is applied; the next deploy reconciles the running row and records the release (A6)
    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, "RUN_NOT_CLOSED")
    assert db.fence == 1 and len(error.report.steps_applied) == 5
    assert db.sent("UPDATE [azsqlcd].[run] SET [status] = N'failed'") == []


# ------------------------------------------------------------------ connection, session, lock
def test_a_token_with_too_little_life_stops_before_a_session_is_opened():
    b, st = a_release()
    opened: list = []
    error = failure(b, st, opened=opened, token_provider=Tokens(minutes=19))  # min_token_minutes = 20
    assert (error.exit_code, error.reason_code, opened) == (Exit.RETRY_SAFE, "TOKEN_TOO_SHORT", [])


def test_session_options_that_are_not_as_set_refuse_the_run_before_the_lock():
    b, st = a_release()
    db = Db(b, st)
    db.options = (1, 0, *db.options[2:])  # QUOTED_IDENTIFIER is off
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "SESSION_OPTIONS")
    assert db.sent("sp_getapplock") == []


def test_the_session_speaks_us_english_because_engine_errors_are_recognised_by_their_english_text():
    """sqlerrors knows 1222, 1205 and 2020 by the en-US message. In another language a lock timeout
    would be a failed batch (21, not 24) and error 2020 of the dependant sweep would not be seen."""
    b, st = a_release()
    db = Db(b, st)
    run_deploy(b, st, db)
    assert db.batches[0].endswith("SET NUMERIC_ROUNDABORT OFF; SET LANGUAGE us_english;")
    assert db.batches[1].endswith("@@LOCK_TIMEOUT, @@LANGUAGE;")  # set, then asserted
    german = Db(b, st)
    german.options = session_options(30000, language="Deutsch")
    error = failure(b, st, german)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "SESSION_OPTIONS")
    assert error.detail["found"][-1] == "Deutsch" and error.detail["expected"][-1] == "us_english"
    assert german.sent("sp_getapplock") == []


def test_a_migration_batch_that_changes_the_language_fails_the_run_before_a_module_is_created():
    b, st = a_release()
    db = Db(b, st)

    def set_language(_: str) -> ResultSets:
        db.options = session_options(30000, language="Français")
        return []

    db.respond(ADD_D, set_language)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "SESSION_OPTIONS")
    assert db.sent("CREATE OR ALTER") == [] and step_writes(db) == []


def test_a_migration_batch_that_changes_the_session_options_fails_the_run_before_a_module_is_created():
    b, st = a_release()
    db = Db(b, st)

    def set_ansi_nulls_off(_: str) -> ResultSets:
        db.options = (0, *db.options[1:])  # CREATE OR ALTER would store the module with the flag off
        return []

    db.respond(ADD_D, set_ansi_nulls_off)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "SESSION_OPTIONS")
    assert db.sent("CREATE OR ALTER") == [] and db.sent("ROLLBACK TRANSACTION") and step_writes(db) == []


@pytest.mark.parametrize(
    ("facts", "reason_code"),
    [
        ((3, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_ENGINE_EDITION"),
        ((5, "READ_ONLY", "sales", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_READ_ONLY"),
        ((5, "READ_WRITE", "other", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_DB_NAME"),
    ],
)
def test_the_fence_refuses_the_database_before_the_lock_is_taken(facts, reason_code):
    b, st = a_release()
    db = Db(b, st, facts=facts)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, reason_code)
    assert db.sent("sp_getapplock") == []


def test_a_database_of_another_environment_is_refused_before_a_dead_run_is_reconciled():
    b, _ = a_release()
    st = recorded(open_runs=[run_row(9, "running")], env="prod")  # the session is on the dev target
    db = Db(b, st)
    db.options = session_options(30000)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "FENCE_META_MISMATCH")
    assert db.sent("UPDATE") == []


def test_a_running_row_of_a_dead_run_is_reconciled_under_the_lock():
    b, st = a_release()
    st = dataclasses.replace(st, open_runs=(run_row(9, "running"),))
    db = Db(b, st)
    report = run_deploy(b, st, db)
    (reconciled,) = db.sent("N'reconciled'")
    assert (
        "UPDATE [azsqlcd].[run] SET [status] = N'failed'" in reconciled
        and "WHERE [run_id] = 9;" in reconciled
    )
    # the lock is the proof that run 9 is dead (A4); the plan and the new run come after
    db.assert_order(
        "sp_getapplock", "N'reconciled'", "azsqlcd:list_user_objects", "INSERT INTO [azsqlcd].[run]"
    )
    assert report.exit_code == 0


def test_a_dead_run_with_a_started_nontx_step_becomes_unknown_and_the_deploy_refuses():
    b, _ = a_nontx_migration()
    started = StepRow(3, 9, "nontx", M1, chain_sha(b, M1), "started", None)
    st = recorded(steps=[started], open_runs=[run_row(9, "running")])
    db = Db(b, st)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "STEP_UNRESOLVED")
    (reconciled,) = db.sent("N'reconciled'")
    assert "UPDATE [azsqlcd].[step] SET [status] = N'unknown'" in reconciled and f"N'{M1}'" in reconciled
    assert (
        "UPDATE [azsqlcd].[run] SET [status] = N'unknown'" in reconciled
        and "WHERE [run_id] = 9;" in reconciled
    )
    assert reconciled.startswith("BEGIN TRANSACTION;") and reconciled.endswith("COMMIT TRANSACTION;")
    assert db.sent(BUILD_INDEX) == [] and db.sent("INSERT INTO [azsqlcd].[run]") == [] and db.trancount == 0


def test_a_run_that_is_unknown_is_not_reconciled_away():
    b, _ = a_release()
    st = recorded(open_runs=[run_row(9, "unknown")])
    db = Db(b, st)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RUN_UNKNOWN")
    assert db.sent("UPDATE") == []


# ------------------------------------------------------------------ no-op (A6)
def test_a_no_op_deploy_records_the_release():
    path, text = proc("usp_a")
    b = bundle(modules=[(path, text)], seq=7)
    st = recorded(objects={key("PROCEDURE", "usp_a"): row(key("PROCEDURE", "usp_a"), text)}, seq=6)
    db = Db(b, st)
    report = run_deploy(b, st, db)
    (insert,) = db.sent("INSERT INTO [azsqlcd].[run]")
    assert f"VALUES (N'deploy', N'running', 0, 7, N'{COMMIT}'," in insert
    assert len(run_status(db, "ok")) == 1
    assert db.sent("BEGIN TRANSACTION") == [] and step_writes(db) == [] and db.sent("CREATE OR ALTER") == []
    assert (report.exit_code, report.reason_code, report.run_id, report.steps_applied) == (
        0,
        "OK",
        RUN_ID,
        (),
    )


@pytest.mark.parametrize(("recorded_release", "reason_code"), [(7, "OK"), (8, "ALREADY_PAST")])
def test_a_release_that_is_recorded_or_past_sends_nothing(recorded_release, reason_code):
    path, text = proc("usp_a", "SELECT 'text of release 7';")
    b = bundle(modules=[(path, text)], seq=7)
    object_key = key("PROCEDURE", "usp_a")
    # for the newer release the database holds newer text: it must not be replaced by older text
    source = SAME if recorded_release == 7 else "8" * 64
    st = recorded(objects={object_key: row(object_key, text, source=source)}, seq=recorded_release)
    db = Db(b, st)
    report = run_deploy(b, st, db)
    assert (report.exit_code, report.reason_code, report.run_id) == (0, reason_code, None)
    assert (
        db.sent(re.compile(r"(INSERT INTO|UPDATE) \[azsqlcd\]")) == [] and db.sent("BEGIN TRANSACTION") == []
    )
    assert db.sent("CREATE OR ALTER") == [] and not db.applock_held and db.closed


# ------------------------------------------------------------------ modules, drops, unbind
def test_a_tombstoned_trigger_that_is_already_gone_sends_no_drop():
    trigger = key("TRIGGER", "tr_Order_audit")
    drop_table = "-- azsqlcd:allow DROP_TABLE [sales].[Order] reason: retired\nDROP TABLE [sales].[Order];"
    b = bundle(mig(M1, drop_table), tombstones=[trigger])
    st = recorded(
        objects={trigger: row(trigger, "CREATE TRIGGER [sales].[tr_Order_audit] ON [sales].[Order] ...")}
    )
    db = Db(b, st)

    def the_table_takes_its_trigger_along(_: str) -> ResultSets:
        del db.catalog[trigger]
        return []

    db.respond("DROP TABLE", the_table_takes_its_trigger_along)
    report = run_deploy(b, st, db)
    assert db.sent("DROP TRIGGER") == []
    assert db.sent("azsqlcd:object_exists")  # asked inside the transaction, after the table was dropped
    assert db.index_of("DROP TABLE") < db.index_of("azsqlcd:object_exists")
    assert len(db.sent(lambda batch: "[status] = N'dropped'" in batch and trigger in batch)) == 1
    assert (report.modules_dropped, report.steps_applied) == ((trigger,), (f"{M1}#1", f"drop:{trigger}"))


def test_two_unbind_directives_for_one_module_send_one_alter():
    bound = key("VIEW", "vw_bound")
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[Order]", " WITH SCHEMABINDING")
    unbind = "-- azsqlcd:unbind [sales].[vw_bound]\n"
    alter_a = "ALTER TABLE [sales].[Order] ALTER COLUMN [a] bigint NOT NULL;"
    b = bundle(mig(M1, unbind + alter_a, unbind + ADD_C), modules=[(path, text)])
    st = recorded(objects={bound: row(bound, text, schema_bound=True)})
    db = Db(b, st)
    report = run_deploy(b, st, db)
    alters = db.sent(re.compile(r"^ALTER VIEW \[sales\]\.\[vw_bound\] AS"))
    assert len(alters) == 1 and "SCHEMABINDING" not in alters[0]
    assert len(db.sent(lambda batch: "[source_sha256] = NULL WHERE" in batch and bound in batch)) == 1
    # unbound above the first batch, bound again by its file after the migration
    db.assert_order(alters[0], "[source_sha256] = NULL WHERE", alter_a, ADD_C, text)
    assert db.batches.count(text) == 1 and report.modules_deployed == (bound,)
    rebound = db.sent(
        lambda batch: batch.startswith("UPDATE [azsqlcd].[object]") and "catalog_capture" in batch
    )
    assert f"[source_sha256] = N'{checksum(text.encode())}'" in rebound[0]


def test_a_module_that_drifted_after_the_plan_is_not_unbound():
    bound = key("VIEW", "vw_bound")
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[Order]", " WITH SCHEMABINDING")
    b = bundle(mig(M1, ADD_C, "-- azsqlcd:unbind [sales].[vw_bound]\n" + ADD_D), modules=[(path, text)])
    st = recorded(objects={bound: row(bound, text, schema_bound=True)})
    db = Db(b, st)

    def another_text(_: str) -> ResultSets:
        db.catalog[bound] = live_row(
            bound, "CREATE VIEW [sales].[vw_bound] AS SELECT 'by hand'", schema_bound=True
        )
        return []

    db.respond(ADD_C, another_text)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DRIFT_TOUCHED")
    assert db.sent("ALTER VIEW") == [] and "by hand" not in error.message + json.dumps(error.detail)


@pytest.mark.parametrize(
    ("stored", "properties"),
    [
        ({"schema_bound": True}, ["is_schema_bound"]),
        ({"quoted": False}, ["uses_quoted_identifier"]),
        ({"ansi": False}, ["uses_ansi_nulls"]),
        (None, ["exists"]),
    ],
)
def test_a_module_that_is_not_stored_as_its_file_says_fails_the_read_back(stored, properties):
    b, st = a_release()
    db = Db(b, st)
    text = stored_text(view("vw_new")[1])  # the definition is right: only the named property differs
    db.stored_as[NEW] = None if stored is None else live_row(NEW, text, **stored)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "READBACK_MISMATCH")
    assert error.detail == {"objects": [{"object": NEW, "properties": properties}]}
    assert error.report.failed_step == "readback" and "SELECT 1" not in error.message
    assert step_writes(db) == [] and object_writes(db) == [] and db.sent("ROLLBACK TRANSACTION")


def test_the_text_read_back_compares_the_definition_with_what_the_engine_keeps_of_the_file(monkeypatch):
    """Live spike L7: the engine stores CREATE OR ALTER as CREATE and every other byte as sent."""
    b, st = a_release()
    text = view("vw_new")[1]
    assert runner.MODULE_TEXT_READBACK is True  # proven live: spike L7, acceptance run4
    assert run_deploy(b, st, Db(b, st)).exit_code == 0  # Db stores modules.stored_text of the batch

    def stored(definition: str) -> Db:
        db = Db(b, st)
        db.stored_as[NEW] = live_row(NEW, definition)
        return db

    # another body than the file has
    error = failure(b, st, stored(stored_text(text).replace("SELECT 1", "SELECT 2")))
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "READBACK_MISMATCH")
    assert error.detail == {"objects": [{"object": NEW, "properties": ["definition"]}]}
    assert "SELECT" not in error.message  # names of properties only, never definition text (A25)
    # the batch byte for byte is not what an engine holds: the compare is with the stored form
    assert failure(b, st, stored(text)).detail == error.detail
    # one space where the engine keeps three ("CREATE   VIEW") is another text
    assert failure(b, st, stored(text.replace("CREATE OR ALTER", "CREATE"))).detail == error.detail
    # line ends and trailing blanks are not a difference (the normal form of the checksum)
    assert run_deploy(b, st, stored(stored_text(text).replace("\n", "  \r\n"))).exit_code == 0

    monkeypatch.setattr(runner, "MODULE_TEXT_READBACK", False)  # the switch still switches
    assert run_deploy(b, st, stored("CREATE VIEW [sales].[vw_new] AS SELECT 'other' AS [x];")).exit_code == 0


def test_a_data_batch_that_changes_an_untouched_managed_object_fails_the_run():
    other = key("PROCEDURE", "usp_other")
    path, text = proc("usp_other")
    b = bundle(mig(M1, ADD_C, DATA), modules=[(path, text)])
    st = recorded(objects={other: row(other, text)})
    db = Db(b, st)

    def data_batch_changes_a_module(_: str) -> ResultSets:
        db.catalog[other] = live_row(
            other, "CREATE PROCEDURE [sales].[usp_other] AS SELECT 'changed by data';"
        )
        return []

    db.respond("UPDATE [sales].[Order]", data_batch_changes_a_module)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "UNTOUCHED_CHANGED")
    assert error.detail == {"objects": [{"object": other, "properties": ["definition"]}]}
    assert "changed by data" not in error.message + error.report.to_json()
    assert step_writes(db) == [] and db.sent("COMMIT TRANSACTION;") == [] and db.sent("ROLLBACK TRANSACTION")
    assert "[failed_step] = N'readback'" in run_status(db, "failed")[0]


def test_a_data_batch_that_drops_an_untouched_managed_object_fails_the_run():
    other = key("PROCEDURE", "usp_other")
    path, text = proc("usp_other")
    b = bundle(mig(M1, DATA), modules=[(path, text)])
    st = recorded(objects={other: row(other, text)})
    db = Db(b, st)

    def gone(_: str) -> ResultSets:
        del db.catalog[other]
        return []

    db.respond("UPDATE [sales].[Order]", gone)
    assert failure(b, st, db).detail == {"objects": [{"object": other, "properties": ["exists"]}]}


def test_drift_that_was_there_before_the_run_does_not_fail_a_data_batch():
    other = key("PROCEDURE", "usp_other")
    path, text = proc("usp_other")
    b = bundle(mig(M1, DATA), modules=[(path, text)])
    st = recorded(objects={other: row(other, text)})
    db = Db(b, st)
    db.catalog[other] = live_row(other, "CREATE PROCEDURE [sales].[usp_other] AS SELECT 'hot fix';")
    report = run_deploy(b, st, db)  # [env.dev] has drift = "report"
    assert report.exit_code == 0 and report.plan is not None and [d.key for d in report.plan.drift] == [other]
    snapshots = [batch for batch in db.after("BEGIN TRANSACTION") if "azsqlcd:capture_modules" in batch]
    assert len(snapshots) == 2  # every managed module, before the batches and after them


def test_the_first_converge_is_one_transaction_for_each_chunk():
    files = [proc("usp_a"), proc("usp_b"), proc("usp_c")]
    keys = [key("PROCEDURE", name) for name in ("usp_a", "usp_b", "usp_c")]
    b = bundle(modules=files)
    st = recorded(
        objects={k: row(k, f"CREATE PROCEDURE {k.split(':')[1]} AS SELECT 0;", source=None) for k in keys}
    )
    db = Db(b, st)
    report = run_deploy(b, st, db)  # module_chunk = 2
    assert (
        len(db.sent("BEGIN TRANSACTION")) == 2 and len(db.sent("COMMIT TRANSACTION;")) == 2 and db.fence == 2
    )
    first_commit = db.index_of("COMMIT TRANSACTION;")
    assert (
        db.index_of(files[1][1])
        < db.index_of("N'deployed 2, dropped 0'")
        < first_commit
        < db.index_of(files[2][1])
    )
    assert report.modules_deployed == tuple(keys) and len(db.sent("INSERT INTO [azsqlcd].[run]")) == 1

    # a chunk that fails is rolled back; the chunks before it stay
    db = Db(b, st)
    db.fail_on(files[2][1], sql_error("Invalid object name 'sales.Gone'."))
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "BATCH_FAILED")
    assert error.report.modules_deployed == tuple(keys[:2]) and db.fence == 1
    assert len(run_status(db, "failed")) == 1


@pytest.mark.parametrize(
    ("kind", "name", "text"),
    [
        ("VIEW", "vw_old", "CREATE VIEW [sales].[vw_old] AS SELECT 1 AS [x];"),
        ("FUNCTION", "fn_old", "CREATE FUNCTION [sales].[fn_old]() RETURNS int AS BEGIN RETURN 1; END;"),
        ("TRIGGER", "tr_old", "CREATE TRIGGER [sales].[tr_old] ON [sales].[Order] AFTER INSERT AS RETURN;"),
        ("PROCEDURE", "usp_old", "CREATE PROCEDURE [sales].[usp_old] AS SELECT 0;"),
    ],
)
def test_a_module_is_dropped_with_the_statement_of_its_kind(kind, name, text):
    """TQ-04: DROP PROCEDURE of a view is an error of the engine, and of a function too."""
    gone = key(kind, name)
    b = bundle(modules=[proc("usp_keep")], tombstones=[gone])
    st = recorded(objects={gone: row(gone, text)})
    db = Db(b, st)
    report = run_deploy(b, st, db)
    assert db.sent("DROP ") == [f"DROP {kind} [sales].[{name}];"]
    assert report.modules_dropped == (gone,) and gone not in db.catalog


# ------------------------------------------------------------------ table model
TABLE = names.object_key("TABLE", "sales", "Order")
TABLE_ROW = ObjectRow("managed", None, 1, {"columns": ["a"]}, state.capture_sha256({"columns": ["a"]}))
DEPENDANT = key("VIEW", "vw_dependant")
BROKEN_COLUMN = ("sales", "Order", None, 5, 0, 0, 0, "U ")  # is_all_columns_found = 0 for a table
COLUMNS_NOT_FOUND = "COLUMNS_NOT_FOUND [sales].[Order]"


class Hooks:
    """A table model with fixed answers. It records what the runner asked."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.read_back_calls: list[tuple[list[str], str | None, int]] = []
        self.drift_calls = 0

    def touched_table_objects(self, work: Work) -> list[str]:
        return [TABLE]

    def table_drift(self, session: Any, state: State, keys: Sequence[str]) -> dict[str, list[Difference]]:
        self.drift_calls += 1
        drift = self.answers.get("drift", [{}])
        return drift[min(self.drift_calls, len(drift)) - 1]

    def blockers_and_dependants(self, session: Any, work: Work, config: Any) -> TableFindings:
        return TableFindings()

    def sub_object_collisions(self, session: Any, work: Work, state: State) -> list[str]:
        return []

    def refresh_set(self, session: Any, work: Work) -> list[str]:
        return self.answers.get("refresh", [])

    def unrecorded_objects(self, work: Work, state: State) -> list[str]:
        return []

    def dependants_to_check(self, session: Any, work: Work) -> list[str]:
        return self.answers.get("dependants", [])

    def dependant_findings(self, session: Any, keys: Sequence[str]) -> dict[str, list[str]]:
        """before: key -> what the engine said of the dependant before the change; else no finding."""
        return {k: list(self.answers.get("before", {}).get(k, [])) for k in keys}

    def read_back(self, session: Any, bundle: Bundle, keys: Sequence[str], through: str | None) -> dict:
        self.read_back_calls.append((list(keys), through, session.trancount))
        if "mismatch" in self.answers:
            raise ToolError(Exit.FAILED_ROLLED_BACK, "READBACK_MISMATCH", self.answers["mismatch"])
        return self.answers.get("captures", {TABLE: {"columns": ["a", "c"]}})


def a_table_release(*batches: str) -> tuple[Bundle, State, Db]:
    path, text = view("vw_dependant", "SELECT [a] FROM [sales].[Order]")
    b = bundle(mig(M1, *batches), modules=[(path, text)])
    st = recorded(objects={DEPENDANT: row(DEPENDANT, text), TABLE: TABLE_ROW})
    return b, st, Db(b, st, user_objects=[("sales", "Order", "U")])


def test_with_a_table_model_dependants_are_refreshed_and_checked_and_tables_are_read_back():
    b, st, db = a_table_release()
    hooks = Hooks(refresh=[DEPENDANT], dependants=[DEPENDANT])
    report = run_deploy(b, st, db, table_hooks=hooks)
    db.assert_order(
        ADD_C,
        "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[vw_dependant]';",
        "azsqlcd:guard",
        "sys.dm_sql_referenced_entities(N'[sales].[vw_dependant]'",
        "UPDATE [azsqlcd].[object]",
        "INSERT INTO [azsqlcd].[step]",
        "COMMIT TRANSACTION;",
    )
    assert hooks.read_back_calls == [
        ([TABLE], None, 1)
    ]  # in the transaction, against the model of the release
    (table_row,) = db.sent(lambda batch: batch.startswith("UPDATE [azsqlcd].[object]") and TABLE in batch)
    assert "[source_sha256] = NULL" in table_row
    assert f"N'{state.capture_sha256({'columns': ['a', 'c']})}'" in table_row
    assert report.steps_applied == (f"{M1}#1", f"refresh:{DEPENDANT}")


REFRESH = "EXEC sys.sp_refreshsqlmodule @name = N'[sales].[vw_dependant]';"


@pytest.mark.parametrize(
    ("message", "finding"),
    [("Invalid column name 'a'.", "ERROR_207"), ("Invalid object name 'sales.Order'.", "ERROR_208")],
)
def test_a_dependant_whose_refresh_fails_is_a_broken_dependant_and_the_module_is_named(message, finding):
    """N1-F3: a view that still uses a dropped column fails at its refresh step, before the sweep
    of dependants. It ended as BATCH_FAILED with the name redacted; a procedure in the same state
    ended as DEPENDANT_BROKEN. Live: sp_refreshsqlmodule raises 207 for a dropped column."""
    b, st, db = a_table_release()
    db.fail_on(REFRESH, sql_error(message))  # a failed sp_refreshsqlmodule ends the transaction
    error = failure(b, st, db, table_hooks=Hooks(refresh=[DEPENDANT], dependants=[DEPENDANT]))
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": DEPENDANT, "findings": [finding], "step": f"refresh:{DEPENDANT}"}
    assert DEPENDANT in error.message and error.report.failed_step == f"refresh:{DEPENDANT}"
    assert "withdraw the migration" in error.message  # what the next pull request must hold (N2-F1)
    # rolled back and proven, as every failed batch; the run row holds the engine error, redacted
    rollback, trancount, fence, status = db.after(REFRESH)
    assert rollback == "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;" and "azsqlcd:read_fence */" in fence
    assert "[note] = N'DEPENDANT_BROKEN'" in status and f"[error_number] = {finding[6:]}" in status
    assert "N'failed'" in status and step_writes(db) == [] and db.sent("azsqlcd:broken_references") == []


@pytest.mark.parametrize(
    ("message", "exit_code", "reason_code"),
    [
        (LOCK_TIMEOUT, Exit.RETRY_SAFE, "LOCK_TIMEOUT"),
        (LOG_FULL, Exit.FAILED_ROLLED_BACK, "GOVERNANCE_LIMIT"),
    ],
)
def test_a_refresh_that_stops_for_a_lock_or_a_limit_is_not_a_broken_dependant(
    message, exit_code, reason_code
):
    b, st, db = a_table_release()
    db.fail_on(REFRESH, sql_error(message))
    error = failure(b, st, db, table_hooks=Hooks(refresh=[DEPENDANT]))
    assert (error.exit_code, error.reason_code) == (exit_code, reason_code)


def test_a_session_that_is_lost_in_a_refresh_goes_the_way_of_a_lost_connection():
    b, st, db = a_table_release()
    db.kill_on(REFRESH)
    error = failure(
        b, st, db, table_hooks=Hooks(refresh=[DEPENDANT]), sessions=[parse_only_session(), second_session(0)]
    )
    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, "CONNECTION_LOST_ROLLED_BACK")


def test_a_managed_dependant_that_does_not_bind_after_the_change_fails_the_run():
    b, st, db = a_table_release()
    db.respond("azsqlcd:broken_references", [[BROKEN_COLUMN]])
    error = failure(b, st, db, table_hooks=Hooks(dependants=[DEPENDANT]))
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": DEPENDANT, "findings": [COLUMNS_NOT_FOUND]}
    assert db.sent("ROLLBACK TRANSACTION") and step_writes(db) == []


NOT_BOUND = (
    'The dependencies reported for entity "sales.vw_dependant" might not include references to all '
    "columns. This is either because the entity references an object that does not exist or because "
    "of an error in one or more statements in the entity."
)


def test_error_2020_in_the_dependant_sweep_ends_the_sweep_and_the_rollback_is_proven_not_assumed():
    """A12: error 2020 is the finding. It does NOT end the transaction, also with XACT_ABORT ON
    (live spike E2: the guard stays (1, 1) after it). The sweep stops at the first dependant that
    does not bind; the tool rolls back itself, proves the rollback and ends 21."""
    second = key("VIEW", "vw_second")
    dependant, other = view("vw_dependant", "SELECT [a] FROM [sales].[Order]"), view("vw_second")
    b = bundle(mig(M1), modules=[dependant, other])
    objects = {DEPENDANT: row(DEPENDANT, dependant[1]), second: row(second, other[1]), TABLE: TABLE_ROW}
    st = recorded(objects=objects)
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    assert sql_error(NOT_BOUND).number == 2020
    # as the engine: the transaction stays open after the error
    db.fail_on("azsqlcd:broken_references", sql_error(NOT_BOUND), keeps_transaction=True)
    error = failure(b, st, db, table_hooks=Hooks(dependants=[DEPENDANT, second]))
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": DEPENDANT, "findings": [catalog.REFERENCES_NOT_BOUND]}
    # the sweep stopped at the first dependant, and no guard and no read came before the proof
    rollback, trancount, fence, status = db.after("azsqlcd:broken_references")
    assert rollback == "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;"
    assert trancount.endswith("SELECT @@TRANCOUNT;") and "azsqlcd:read_fence */" in fence
    assert status.startswith("UPDATE [azsqlcd].[run] SET [status] = N'failed'")
    assert "[note] = N'DEPENDANT_BROKEN'" in status and "[failed_step] = N'dependants'" in status
    (read,) = db.sent("azsqlcd:broken_references")
    assert "vw_dependant" in read
    assert (db.trancount, db.fence) == (0, 0) and step_writes(db) == [] and not run_status(db, "unknown")


def test_error_2020_with_a_rollback_that_cannot_be_proven_is_unknown():
    b, st, db = a_table_release()
    db.fail_on("azsqlcd:broken_references", sql_error(NOT_BOUND), keeps_transaction=True)
    db.respond(after_batch(db, "azsqlcd:broken_references", "azsqlcd:read_fence"), [[(1,)]])
    error = failure(b, st, db, table_hooks=Hooks(dependants=[DEPENDANT]))
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "FENCE_MISMATCH")


# what the engine says of a sound procedure with a #temp table (measured on Azure SQL Database,
# RO-2): `UPDATE cc ... FROM #changes AS cc` gives a row [cc] with no id and no schema
ALIAS = (None, "cc", None, None, 0, 1, 0, None)
UNRESOLVED_ALIAS = "UNRESOLVED [cc]"


def test_a_finding_that_a_dependant_had_before_the_change_does_not_fail_the_run():
    """RO-1, RO-2: on a real database a sound module has findings. The check is differential."""
    b, st, db = a_table_release()
    db.respond("azsqlcd:broken_references", [[BROKEN_COLUMN, ALIAS]])
    old = {DEPENDANT: [COLUMNS_NOT_FOUND, UNRESOLVED_ALIAS]}
    report = run_deploy(b, st, db, table_hooks=Hooks(dependants=[DEPENDANT], before=old))
    assert report.exit_code == 0 and db.sent("COMMIT TRANSACTION;")
    # the dependant is not exempt: it is read after the change, and only its old findings are ignored
    (read,) = db.sent("azsqlcd:broken_references")
    assert "vw_dependant" in read and db.index_of(read) > db.index_of(ADD_C)
    assert report.plan is not None
    assert report.plan.dependant_findings == {DEPENDANT: (COLUMNS_NOT_FOUND, UNRESOLVED_ALIAS)}
    assert report.plan.pre_broken == (DEPENDANT,)


def test_a_dependant_with_old_findings_fails_the_run_for_a_new_finding_and_only_that_is_reported():
    b, st, db = a_table_release()
    db.respond("azsqlcd:broken_references", [[BROKEN_COLUMN, ALIAS]])
    hooks = Hooks(dependants=[DEPENDANT], before={DEPENDANT: [UNRESOLVED_ALIAS]})
    error = failure(b, st, db, table_hooks=hooks)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": DEPENDANT, "findings": [COLUMNS_NOT_FOUND]}
    assert "[cc]" not in error.message and db.sent("COMMIT TRANSACTION;") == []


# the texts of the three errors that say "this module does not bind" (live spike X3: the driver
# returns only the first error, and 207 or 208 arrive before 2020)
INVALID_COLUMN = "Invalid column name 'a'."
INVALID_OBJECT = "Invalid object name 'sales.Order'."
NOT_BOUND_ERRORS = [(NOT_BOUND, "ERROR_2020"), (INVALID_COLUMN, "ERROR_207"), (INVALID_OBJECT, "ERROR_208")]


def test_the_three_errors_of_a_module_that_does_not_bind_are_the_findings_of_the_catalog():
    assert {sql_error(message).number for message, _ in NOT_BOUND_ERRORS} == catalog.NOT_BOUND_NUMBERS
    assert {finding for _, finding in NOT_BOUND_ERRORS} == catalog.NOT_BOUND_FINDINGS


@pytest.mark.parametrize(("message", "finding"), NOT_BOUND_ERRORS)
def test_a_dependant_that_the_engine_could_not_bind_before_the_change_does_not_fail_for_the_same_error(
    message, finding
):
    """A sound procedure can raise 2020 (measured, RO-2), and a dependant that a column or a table
    was taken from before this release raises 207 or 208. The same error after the change is no
    new finding: it would fail every release that touches its table. RP2-1: it is read again (the
    errors do not end the transaction), because another error number is a new finding."""
    b, st, db = a_table_release()
    db.fail_on("azsqlcd:broken_references", sql_error(message), keeps_transaction=True)
    hooks = Hooks(dependants=[DEPENDANT], before={DEPENDANT: [finding]})
    report = run_deploy(b, st, db, table_hooks=hooks)
    assert report.exit_code == 0 and len(db.sent("azsqlcd:broken_references")) == 1
    assert db.sent("COMMIT TRANSACTION;")


def test_error_2020_after_the_change_fails_a_dependant_that_had_other_findings_before():
    b, st, db = a_table_release()
    db.fail_on("azsqlcd:broken_references", sql_error(NOT_BOUND), keeps_transaction=True)
    hooks = Hooks(dependants=[DEPENDANT], before={DEPENDANT: [COLUMNS_NOT_FOUND]})
    error = failure(b, st, db, table_hooks=hooks)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": DEPENDANT, "findings": [catalog.REFERENCES_NOT_BOUND]}
    assert (db.trancount, db.fence) == (0, 0) and not run_status(db, "unknown")


def test_a_table_that_does_not_equal_the_model_after_the_unit_fails_the_run():
    b, st, db = a_table_release()
    error = failure(
        b, st, db, table_hooks=Hooks(mismatch="TABLE:[sales].[Order] differs from the model (columns)")
    )
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "READBACK_MISMATCH")
    assert db.sent("ROLLBACK TRANSACTION") and object_writes(db) == []


def test_a_data_batch_that_changes_an_untouched_table_fails_the_run():
    other = names.object_key("TABLE", "sales", "Customer")
    b, st, db = a_table_release(ADD_C, DATA)
    st = dataclasses.replace(st, objects={**st.objects, other: TABLE_ROW})
    db = Db(b, st, user_objects=[("sales", "Order", "U"), ("sales", "Customer", "U")])
    changed = {other: [Difference("columns", "1" * 64, "2" * 64)]}
    # the plan and the snapshot before the batches see no drift; after the data batch the table differs
    error = failure(b, st, db, table_hooks=Hooks(drift=[{}, {}, changed]))
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "UNTOUCHED_CHANGED")
    assert error.detail == {"objects": [{"object": other, "properties": ["columns"]}]}


# ------------------------------------------------------------------ audit and report
def test_the_run_row_holds_who_approved_and_what_ran():
    b, st = a_release(env="prod")
    expected = plan_job(b, st)
    db = Db(b, st)
    audit = Audit(
        approved_by="octo-approver",
        approved_utc=datetime(2026, 10, 7, 8, 30, tzinfo=UTC),
        triggering_actor="octo-author",
        ci_actor="github-actions",
        ci_run_url="https://github.example/akaalholdings/db-sales/actions/runs/77",
    )
    run_deploy(b, st, db, env="prod", expect=expected, audit=audit)
    (insert,) = db.sent("INSERT INTO [azsqlcd].[run]")
    for value in (
        "N'deploy'",
        f"N'{COMMIT}'",
        f"N'{release.digest(b.manifest)}'",
        f"N'{expected.plan_sha256}'",
        f"N'{TOOL_DIGEST}'",
        f"N'{RECORDED_COMMIT}'",  # previous_git_sha: the release that was recorded before
        "N'octo-approver'",
        "N'2026-10-07T08:30:00.000'",
        "N'octo-author'",
        "N'github-actions'",
        "N'https://github.example/akaalholdings/db-sales/actions/runs/77'",
    ):
        assert value in insert, value
    assert db.trancount == 0 and db.index_of(insert) < db.index_of("BEGIN TRANSACTION")  # autocommit


def test_the_report_is_json_with_the_server_time_of_the_run():
    b, st = a_release()
    report = run_deploy(b, st)
    doc = json.loads(report.to_json())
    assert doc == {
        "command": "deploy",
        "exit_code": 0,
        "reason_code": "OK",
        "message": report.message,
        "environment": "dev",
        "target_id": "sales-dev",
        "run_id": RUN_ID,
        "started_utc": STARTED,
        "release_seq": 7,
        "git_sha": COMMIT,
        "plan_sha256": report.plan_sha256,
        "tool_version": "0.1.0",
        "tool_digest": TOOL_DIGEST,
        "steps_applied": list(report.steps_applied),
        "modules_deployed": [NEW, CHANGED],
        "modules_dropped": [OLD],
        "failed_step": None,
        "warnings": list(report.warnings),
        "auth": "entra",  # the kind of sign-in of the token provider of the test
        "syntax_check": "ran in this run",  # --inline-plan: the second session parsed the texts
    }
    assert report.to_json().endswith("}\n") and any(
        "tables are not modelled" in note for note in report.warnings
    )


# ------------------------------------------------------------------ lost connection, locking read
def second_session(fence: int | None) -> FakeSession:
    second = FakeSession()
    second.respond("azsqlcd:session_options", [[session_options(30000)]])
    if fence is not None:
        second.respond("azsqlcd:read_fence_locking", [[(fence,)]])
    return second


@pytest.mark.parametrize(
    ("fence", "reason_code", "note"),
    [
        (1, "CONNECTION_LOST_COMMITTED", "N'connection lost; the unit of work was committed'"),
        (0, "CONNECTION_LOST_ROLLED_BACK", "N'connection lost; the unit of work was rolled back'"),
    ],
)
def test_the_locking_read_tells_a_committed_unit_from_a_rolled_back_one(fence, reason_code, note):
    second = second_session(fence)
    error = reconcile_by_locking_read(lambda: second, RUN_ID, 0, lock_timeout_ms=30000, applock_wait_s=600)
    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, reason_code)
    second.assert_order("SET LOCK_TIMEOUT 30000", "sp_getapplock", "WITH (READCOMMITTEDLOCK, ROWLOCK)", note)
    assert "UPDATE [azsqlcd].[run] SET [status] = N'failed'" in second.sent(note)[0] and second.closed


@pytest.mark.parametrize("problem", ["lock not granted", "read times out", "no row", "fence behind"])
def test_a_locking_read_that_gives_no_answer_leaves_the_run_unknown(problem):
    second = second_session({"lock not granted": 2, "fence behind": 0}.get(problem))
    if problem == "lock not granted":
        second.applock_result = -1
    if problem == "read times out":
        second.fail_on("WITH (READCOMMITTEDLOCK, ROWLOCK)", sql_error(LOCK_TIMEOUT))
    error = reconcile_by_locking_read(lambda: second, RUN_ID, 1, lock_timeout_ms=30000, applock_wait_s=600)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "CONNECTION_LOST_TX")
    assert second.sent("UPDATE") == [] and second.closed


def test_with_the_locking_read_switched_off_a_lost_connection_is_exit_23_and_no_session_is_opened(
    monkeypatch,
):
    assert runner.RECONCILE_BY_LOCKING_READ is True  # proven live: spike L6, acceptance run4
    monkeypatch.setattr(runner, "RECONCILE_BY_LOCKING_READ", False)
    b, st = a_release()
    db, second = Db(b, st), second_session(0)
    db.kill_on(ADD_C)
    opened: list = []
    error = failure(b, st, db, opened=opened, sessions=[second], expect=plan_job(b, st, env="dev"))
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "CONNECTION_LOST_TX")
    assert opened == [db] and second.batches == [] and ADD_C in db.batches[-1]


# ------------------------------------------------------------------ keys without case
# The release can spell a key in another letter case than the row of the state has. The catalog
# compares names without case, so both are one object, and a state write goes to the row.
class SpelledHooks(Hooks):
    """Hooks whose release spells the table in another letter case than the state."""

    spelled = "TABLE:[SALES].[order]"

    def touched_table_objects(self, work: Work) -> list[str]:
        return [self.spelled]


def where_key(batch: str) -> str:
    return batch.split("WHERE [object_key] = N'")[1].split("'")[0]


def test_a_statement_that_spells_a_table_in_another_letter_case_still_marks_the_recorded_row_dropped():
    b, st, db = a_table_release("DROP TABLE [SALES].[order];")
    # the read-back finds the table gone, under the key of the statement
    hooks = SpelledHooks(captures={SpelledHooks.spelled: None})

    report = run_deploy(b, st, db, table_hooks=hooks)

    assert report.exit_code == 0 and hooks.read_back_calls == [([SpelledHooks.spelled], None, 1)]
    (dropped,) = db.sent("SET [status] = N'dropped'")
    assert where_key(dropped) == TABLE  # the row that the state holds, not a row that does not exist
    db.assert_order("DROP TABLE [SALES].[order];", dropped, "COMMIT TRANSACTION;")


def test_a_table_under_a_key_of_another_case_is_recorded_in_the_row_that_the_state_holds():
    b, st, db = a_table_release()
    hooks = SpelledHooks(captures={SpelledHooks.spelled: {"columns": ["a", "c"]}})

    run_deploy(b, st, db, table_hooks=hooks)

    (table_row,) = [batch for batch in object_writes(db) if "TABLE:" in batch]
    assert table_row.startswith("UPDATE [azsqlcd].[object]") and where_key(table_row) == TABLE
    assert f"N'{SpelledHooks.spelled}'" not in table_row.split(" IF @@ROWCOUNT = 0 ")[0]


def test_a_module_file_that_differs_from_the_recorded_key_only_by_case_is_the_same_object():
    stored_key = "PROCEDURE:[Sales].[USP_Changed]"
    b = bundle(modules=[proc("usp_changed", "SELECT 2;")])
    st = recorded(
        objects={stored_key: row(stored_key, "CREATE PROCEDURE [Sales].[USP_Changed] AS SELECT 1;")}
    )
    db = Db(b, st)

    report = run_deploy(b, st, db)

    assert report.modules_deployed == (CHANGED,) and len(db.sent(proc("usp_changed", "SELECT 2;")[1])) == 1
    (module_row,) = object_writes(db)
    assert where_key(module_row) == stored_key  # one object, one row
    assert f"N'{checksum(proc('usp_changed', 'SELECT 2;')[1].encode())}'" in module_row


def test_a_tombstone_under_a_key_of_another_case_drops_the_module_and_marks_its_row():
    stored_key = "PROCEDURE:[Sales].[USP_Old]"
    b = bundle(tombstones=[OLD])
    st = recorded(objects={stored_key: row(stored_key, "CREATE PROCEDURE [Sales].[USP_Old] AS SELECT 0;")})
    db = Db(b, st)
    db.respond("azsqlcd:object_exists", [[(1,)]])  # the engine finds the object under either spelling

    report = run_deploy(b, st, db)

    assert report.modules_dropped == (OLD,)
    (dropped,) = db.sent("SET [status] = N'dropped'")
    assert where_key(dropped) == stored_key
    db.assert_order("DROP PROCEDURE [sales].[usp_old];", dropped, "COMMIT TRANSACTION;")


def test_a_touched_table_under_another_case_is_not_reported_as_an_untouched_object_that_changed():
    b, st, db = a_table_release(ADD_C, DATA)
    changed = {TABLE: [Difference("columns", "1" * 64, "2" * 64)]}
    # the plan and the snapshot before the batches see no drift; after the batches the table differs,
    # and it is the table that the release touches
    hooks = SpelledHooks(drift=[{}, {}, changed], captures={SpelledHooks.spelled: {"columns": ["a", "c"]}})
    assert run_deploy(b, st, db, table_hooks=hooks).exit_code == 0


def test_an_unbind_under_a_key_of_another_case_reads_the_recorded_row_and_forgets_its_source():
    path, text = view("vw_bound", "SELECT 1 AS [x]", " WITH SCHEMABINDING")
    stored_key, written = "VIEW:[SALES].[VW_BOUND]", key("VIEW", "vw_bound")
    alter = "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [a] bigint NULL;"
    b = bundle(mig(M1, alter), modules=[(path, text)])
    st = recorded(objects={stored_key: row(stored_key, text, schema_bound=True)})
    db = Db(b, st)
    # the engine resolves the name, and holds the text as it stores it
    db.catalog[written] = live_row(written, stored_text(text), schema_bound=True)

    report = run_deploy(b, st, db)

    assert report.exit_code == 0 and report.modules_deployed == (written,)
    (forgotten,) = db.sent("SET [source_sha256] = NULL WHERE")
    assert where_key(forgotten) == stored_key
    db.assert_order("ALTER VIEW [sales].[vw_bound] AS", forgotten, alter, text, "COMMIT TRANSACTION;")


# ------------------------------------------------------------------ A6: a release that committed, run not ok
def test_an_older_release_sends_no_module_text_over_a_release_that_committed_and_did_not_end_ok():
    """r7 (modules only) committed its unit of work and its run row never became ok. The record is
    still r6, and the file of r6 differs from what the database holds. A deploy of r6 must not put
    the older text back."""
    x = key("PROCEDURE", "usp_x")
    r6, r7 = proc("usp_x", "SELECT 6;"), proc("usp_x", "SELECT 7;")
    b = bundle(modules=[r6], seq=6)
    st = dataclasses.replace(recorded(objects={x: row(x, r7[1])}, seq=6), committed_release_seq=7)
    db = Db(b, st)
    report = run_deploy(b, st, db)
    assert (report.exit_code, report.reason_code) == (0, "ALREADY_PAST")
    assert db.sent(r6[1]) == [] and db.sent("BEGIN TRANSACTION") == [] and report.run_id is None
    assert "r7" in report.message
    # the control: with no such run the same release is deployed
    plain = recorded(objects={x: row(x, r7[1])}, seq=6)
    assert run_deploy(b, plain).modules_deployed == (x,)


# ------------------------------------------------------------------ A1: an error that is not a SqlError
def test_an_exception_that_is_not_a_sql_error_in_the_last_run_row_write_is_exit_23_with_a_report():
    """The driver can raise something else than its own error on a dead connection. After the first
    batch every end of deploy() is a RunError with a Report; the command line writes report.json
    from it. An exception that escaped would be reported as 22 'Nothing was executed'."""
    b, st = a_migration()
    db = Db(b, st)
    dead = RuntimeError("native layer: the connection is dead")
    db.fail_on("UPDATE [azsqlcd].[run] SET [status]", dead, times=5)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "TOOL_DEFECT_AFTER_DISPATCH")
    assert db.fence == 1  # the unit of work is committed, and the report says what was applied
    assert error.report.steps_applied == (f"{M1}#1", f"{M1}#2") and error.report.run_id == RUN_ID
    assert error.report.failed_step == "close" and "RuntimeError" in error.message
    assert "connection is dead" not in error.message and db.closed


def test_an_exception_in_the_release_of_the_lock_or_in_the_close_changes_no_outcome():
    """The run row is ok and committed before the lock is given back; closing the session gives
    the lock back in any case."""
    b, st = a_migration()
    db = Db(b, st)
    db.fail_on("sp_releaseapplock", RuntimeError("native layer: the connection is dead"))
    report = run_deploy(b, st, db)
    assert (report.exit_code, report.reason_code) == (0, "OK") and len(run_status(db, "ok")) == 1

    class CloseFails(Db):
        def close(self) -> None:
            super().close()
            raise RuntimeError("native layer: close failed")

    db = CloseFails(b, st)
    assert run_deploy(b, st, db).exit_code == 0
    db = CloseFails(b, st)
    db.fail_on(ADD_D, sql_error("Invalid column name 'd'.", number=207))
    assert failure(b, st, db).reason_code == "BATCH_FAILED"  # the RunError, not the error of the close


# ------------------------------------------------------------------ implicit transactions
IMPLICIT_ON = (
    "-- azsqlcd:data\nSET IMPLICIT_TRANSACTIONS ON;\nUPDATE [sales].[Order] SET [c] = 0 WHERE [c] IS NULL;"
)


def test_a_batch_that_leaves_implicit_transactions_on_fails_the_run_before_commit():
    """With the option on, every later state write of the tool at @@TRANCOUNT 0 opens a transaction
    that nobody commits: the run row would stay `running` behind an exit 0. Inside the transaction
    of the unit the option changes nothing, so no guard sees it. The options are read again after
    the last batch, also when no module step follows."""
    b = bundle(mig(M1, ADD_C, IMPLICIT_ON))
    st = recorded()
    db = Db(b, st)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "SESSION_OPTIONS")
    assert "IMPLICIT_TRANSACTIONS" in error.message
    assert db.sent("COMMIT TRANSACTION;") == [] and db.fence == 0 and step_writes(db) == []
    db.assert_order(IMPLICIT_ON, "azsqlcd:session_options", "ROLLBACK")
    assert (error.detail["found"][IMPLICIT_BIT], error.detail["expected"][IMPLICIT_BIT]) == (2, 0)
    assert run_status(db, "ok") == []


def test_a_session_that_starts_with_implicit_transactions_on_gets_the_option_switched_off():
    """RS-9: a login or a driver can open the session with the option on. The tool sets it off with
    its other options and reads it back, so no state write opens a transaction that nobody commits."""
    b, st = a_migration()
    db = Db(b, st)
    db.implicit = True
    report = run_deploy(b, st, db)
    assert report.exit_code == 0 and (db.trancount, db.fence) == (0, 1)
    assert "SET NOCOUNT ON; SET IMPLICIT_TRANSACTIONS OFF; SET ANSI_NULLS" in db.batches[0]
    assert "@@OPTIONS & 16384, @@OPTIONS & 512, @@OPTIONS & 2, @@LOCK_TIMEOUT, @@LANGUAGE;" in db.batches[1]


def test_a_session_that_keeps_implicit_transactions_on_is_refused_before_the_lock():
    b, st = a_migration()
    db = Db(b, st)
    db.options = (*OPTIONS_ON[:IMPLICIT_BIT], 2, 30000, "us_english")
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "SESSION_OPTIONS")
    assert "IMPLICIT_TRANSACTIONS" in error.message and db.sent("sp_getapplock") == []


def test_a_nontx_batch_that_leaves_implicit_transactions_on_is_an_unknown_outcome():
    b = bundle(mig(M1, BUILD_INDEX + "\nSET IMPLICIT_TRANSACTIONS ON;", mode="nontx"))
    st = recorded()
    db = Db(b, st)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "SESSION_OPTIONS")
    assert db.sent("COMMIT TRANSACTION;") == [] and run_status(db, "ok") == []


def test_a_run_row_write_that_is_left_in_an_open_transaction_is_not_reported_as_exit_0():
    """The backstop: exit 0 says that the run row is `ok` and committed. After the write the session
    must hold no open transaction; else the close of the session rolls the write back."""
    b, st = a_migration()
    db = Db(b, st)

    def implicit_from_here(_: str) -> ResultSets:
        db.implicit = True  # nothing that the tool reads before the write shows it
        return []

    db.respond("COMMIT TRANSACTION;", implicit_from_here)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, "RUN_NOT_CLOSED")
    assert db.fence == 1 and db.trancount == 1  # the unit is committed; the write of `ok` is not
    assert "azsqlcd:trancount" in db.after("UPDATE [azsqlcd].[run] SET [status] = N'ok'")[0]
    assert error.report.steps_applied == (f"{M1}#1", f"{M1}#2")


def test_the_run_row_is_proven_committed_before_the_lock_is_given_back():
    b, st = a_migration()
    db = Db(b, st)
    run_deploy(b, st, db)
    db.assert_order(
        "COMMIT TRANSACTION;",
        "UPDATE [azsqlcd].[run] SET [status] = N'ok'",
        "azsqlcd:trancount",
        "sp_releaseapplock",
    )


# ------------------------------------------------------------------ A12: dependants of a table that is gone
READER = key("PROCEDURE", "usp_reader")
# what the engine says of a module whose table is gone: referenced_id NULL, not caller dependent
UNRESOLVED = ("sales", "Order", None, None, 0, 0, 0, None)


def a_table_that_goes(ddl: str) -> tuple[Bundle, State, Db]:
    """A release that drops or renames [sales].[Order]; the managed procedure usp_reader reads it."""
    path, text = proc("usp_reader", "SELECT [a] FROM [sales].[Order];")
    b = bundle(mig(M1, ddl), modules=[(path, text)])
    st = recorded(objects={READER: row(READER, text), TABLE: TABLE_ROW})
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    # the engine: once the table is gone, OBJECT_ID of its old name is NULL and no dependency row has it
    db.respond(
        "azsqlcd:dependants_of", lambda _: [[]] if db.sent(ddl) else [[("sales", "usp_reader", "P", 0)]]
    )
    db.respond("azsqlcd:broken_references", [[UNRESOLVED]])
    return b, st, db


@pytest.mark.parametrize(
    "ddl", ["DROP TABLE [sales].[Order];", "EXEC sys.sp_rename N'[sales].[Order]', N'Order2', N'OBJECT';"]
)
def test_a_managed_procedure_on_a_dropped_or_renamed_table_fails_the_run_with_dependant_broken(ddl):
    """The dependants are the ones that the plan read before the change. Read after it, the catalog
    names none, and a procedure that still reads the old name would be committed broken."""
    b, st, db = a_table_that_goes(ddl)
    hooks = Hooks(dependants=[READER], captures={TABLE: None})
    error = failure(b, st, db, table_hooks=hooks)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": READER, "findings": ["UNRESOLVED [sales].[Order]"]}
    assert db.index_of("azsqlcd:broken_references") > db.index_of(ddl)  # checked after the change
    assert db.sent("COMMIT TRANSACTION;") == [] and step_writes(db) == []
    assert error.report.plan is not None and error.report.plan.dependants == (READER,)


def test_the_sweep_checks_the_dependants_of_the_plan_and_no_module_that_the_unit_dropped():
    old = key("PROCEDURE", "usp_old")
    b, st, db = a_table_release()
    b = bundle(mig(M1), modules=[view("vw_dependant", "SELECT [a] FROM [sales].[Order]")], tombstones=[old])
    st = dataclasses.replace(
        st, objects={**st.objects, old: row(old, "CREATE PROCEDURE [sales].[usp_old] AS SELECT 0;")}
    )
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    # a key of the plan in another letter case is the same module
    hooks = Hooks(dependants=[old, DEPENDANT.upper().replace("VIEW:", "VIEW:")])
    report = run_deploy(b, st, db, table_hooks=hooks)
    assert report.exit_code == 0 and report.modules_dropped == (old,)
    (checked,) = db.sent("azsqlcd:broken_references")
    assert "vw_dependant" in checked.lower() and db.sent("azsqlcd:dependants_of") == []


def test_a_dependant_with_other_findings_under_the_lock_makes_the_plan_stale():
    """dependant_findings is in plan_sha256: the approver saw which findings the deploy ignores."""
    b, st, _ = a_table_release()
    approved = compute_plan(
        b,
        CONFIG,
        "dev",
        "sales-dev",
        a_table_release()[2],
        tool_version="0.1.0",
        tool_digest=TOOL_DIGEST,
        table_hooks=Hooks(dependants=[DEPENDANT]),
    )
    assert approved.pre_broken == () and approved.dependants == (DEPENDANT,)
    assert approved.dependant_findings == {DEPENDANT: ()}
    b, st, db = a_table_release()
    db.respond("azsqlcd:broken_references", [[BROKEN_COLUMN]])
    broken_now = Hooks(dependants=[DEPENDANT], before={DEPENDANT: [COLUMNS_NOT_FOUND]})
    error = failure(b, st, db, expect=approved, table_hooks=broken_now)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "STALE_PLAN")
    assert db.sent("BEGIN TRANSACTION") == [] and db.sent(ADD_C) == []


# ------------------------------------------------------------------ indexed views
TOTALS = key("VIEW", "vw_totals")
TOTALS_V1 = "CREATE VIEW [sales].[vw_totals] WITH SCHEMABINDING AS SELECT [a] FROM [sales].[Order];"


def test_a_release_that_changes_an_indexed_view_is_refused_and_nothing_is_sent():
    """ALTER VIEW drops every index of the view. The plan under the lock refuses the release."""
    changed = view("vw_totals", "SELECT [a], [c] FROM [sales].[Order]", " WITH SCHEMABINDING")
    b = bundle(mig(M1), modules=[changed])
    st = recorded(objects={TOTALS: row(TOTALS, TOTALS_V1, source="1" * 64, schema_bound=True)})
    db = Db(b, st)
    db.indexed_views.add("vw_totals")
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "INDEXED_VIEW")
    assert db.sent(changed[1]) == [] and db.sent("BEGIN TRANSACTION") == [] and error.report.run_id is None


def an_index_appears_on(db: Db, batch: str, name: str) -> None:
    def create_index(_: str) -> ResultSets:
        db.indexed_views.add(name)
        return []

    db.respond(batch, create_index)


def test_a_view_that_gets_an_index_in_the_unit_is_not_altered_by_a_later_module_step():
    """The plan saw no index. A batch of the same unit created one (a raw batch can); the module
    step after it would drop that index again and report exit 0."""
    changed = view("vw_totals", "SELECT [a], [c] FROM [sales].[Order]", " WITH SCHEMABINDING")
    b = bundle(mig(M1), modules=[changed, proc("usp_a")])
    st = recorded(objects={TOTALS: row(TOTALS, TOTALS_V1, source="1" * 64, schema_bound=True)})
    db = Db(b, st)
    an_index_appears_on(db, ADD_C, "vw_totals")
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "INDEXED_VIEW")
    assert error.detail == {"object": TOTALS} and error.report.failed_step == f"module:{TOTALS}"
    assert db.sent(changed[1]) == [] and db.sent("COMMIT TRANSACTION;") == []
    # only a view is asked: a procedure has no index
    assert all("vw_totals" in asked for asked in db.sent("azsqlcd:has_index"))


def test_a_view_that_gets_an_index_in_the_unit_is_not_unbound():
    bound = view("vw_totals", "SELECT [a] FROM [sales].[Order]", " WITH SCHEMABINDING")
    alter = "ALTER TABLE [sales].[Order] ALTER COLUMN [a] bigint;"
    b = bundle(mig(M1, ADD_C, f"-- azsqlcd:unbind [sales].[vw_totals]\n{alter}"), modules=[bound])
    st = recorded(objects={TOTALS: row(TOTALS, bound[1], schema_bound=True)})
    db = Db(b, st)
    an_index_appears_on(db, ADD_C, "vw_totals")
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "INDEXED_VIEW")
    assert db.sent("ALTER VIEW") == [] and db.sent(alter) == [] and db.sent("COMMIT TRANSACTION;") == []


# ------------------------------------------------------------------ the recovery text of a lost nontx session
def test_the_recovery_text_of_a_lost_nontx_session_names_only_actions_that_the_tool_accepts():
    """The session is gone, so the run row stays `running`, and --clear-run takes only a run with
    the status unknown. The message must not send the operator into that refusal."""
    b, st = a_nontx_migration()
    db = Db(b, st)
    db.kill_on(BUILD_INDEX)
    error = failure(b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.UNKNOWN, "CONNECTION_LOST_NONTX")
    assert run_status(db, "unknown") == []  # no write reached the database
    assert f"--mark-applied {M1}" in error.message and f"--mark-not-applied {M1}" in error.message
    assert "--clear-run" not in error.message and "next deploy" in error.message
    # with the session alive the run row is set to unknown, and --clear-run is the last step
    db = Db(b, st)
    db.fail_on(BUILD_INDEX, sql_error("Incorrect syntax near 'ONLINE'."))
    error = failure(b, st, db)
    assert error.reason_code == "NONTX_FAILED" and len(run_status(db, "unknown")) == 1
    assert f"then --clear-run {RUN_ID}" in error.message


@pytest.mark.parametrize("named_by", ["the file", "the catalog"])
def test_a_module_that_the_unit_created_on_a_table_that_is_gone_is_checked_too(named_by):
    """The list of the plan was read before the change and cannot hold a module that this unit
    created. Its file names the table, or the catalog names it as a dependant after the change.
    The engine creates a procedure on a name that does not resolve; the sweep finds it."""
    new = key("PROCEDURE", "usp_new")
    body = "SELECT [a] FROM [sales].[Order];" if named_by == "the file" else "SELECT [a] FROM [sales].[O];"
    b = bundle(mig(M1, "DROP TABLE [sales].[Order];"), modules=[proc("usp_new", body), proc("usp_other")])
    st = recorded(objects={TABLE: TABLE_ROW})
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    if named_by == "the catalog":  # [sales].[O] is a synonym of the table
        db.respond("azsqlcd:dependants_of", [[("sales", "usp_new", "P", 0)]])
    db.respond("azsqlcd:broken_references", [[UNRESOLVED, ALIAS]])
    error = failure(b, st, db, table_hooks=Hooks(captures={TABLE: None}))
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": new, "findings": ["UNRESOLVED [sales].[Order]"]}
    (checked,) = db.sent("azsqlcd:broken_references")  # usp_other does not use the table
    assert "usp_new" in checked and db.index_of(checked) > db.index_of(proc("usp_new", body)[1])


def test_a_module_that_the_unit_sent_is_not_failed_for_what_the_engine_says_of_a_sound_module():
    """No finding of before exists for a module that the unit created, and the engine bound its
    text when it was sent. A table with is_all_columns_found = 0 and an alias with no id are what
    the engine reports for a sound procedure with a #temp table (RO-2): they do not fail the run."""
    b = bundle(mig(M1), modules=[proc("usp_new", "SELECT [a] FROM [sales].[Order];")])
    st = recorded(objects={TABLE: TABLE_ROW})
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    db.respond("azsqlcd:broken_references", [[BROKEN_COLUMN, ALIAS]])
    report = run_deploy(b, st, db, table_hooks=Hooks())
    assert report.exit_code == 0 and len(db.sent("azsqlcd:broken_references")) == 1


@pytest.mark.parametrize(("message", "finding"), NOT_BOUND_ERRORS)
def test_an_error_that_says_a_module_that_the_unit_sent_does_not_bind_fails_the_run(message, finding):
    """Live spike X3: for a procedure that uses a dropped column the engine raises 207, not 2020."""
    b = bundle(mig(M1), modules=[proc("usp_new", "SELECT [a] FROM [sales].[Order];")])
    st = recorded(objects={TABLE: TABLE_ROW})
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    db.fail_on("azsqlcd:broken_references", sql_error(message), keeps_transaction=True)
    error = failure(b, st, db, table_hooks=Hooks())
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail["findings"] == [finding]
    assert (db.trancount, db.fence) == (0, 0) and not run_status(db, "unknown")


# ------------------------------------------------------------------ RP2-1, RP2-2, RP2-3
DROP_A = "ALTER TABLE [sales].[Order] DROP COLUMN [a];"
TEMP_READER = "SELECT o.[a] INTO #t FROM [sales].[Order] AS o; SELECT * FROM #t;"


def a_reader_that_the_engine_cannot_verify(
    *batches: str, body: str = TEMP_READER
) -> tuple[Bundle, State, Db]:
    """An unchanged managed procedure with a #temp table: the engine reports is_all_columns_found = 0
    for [sales].[Order] before and after every change (RO-2). The row names no column."""
    path, text = proc("usp_reader", body)
    b = bundle(mig(M1, *batches), modules=[(path, text)])
    st = recorded(objects={READER: row(READER, text), TABLE: TABLE_ROW})
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    db.respond("azsqlcd:broken_references", [[BROKEN_COLUMN]])
    columns = {"a", "b"}
    if db_batch := next((batch for batch in batches if batch == DROP_A), None):
        after = [[(TABLE, "b")]]
        db.respond(
            "azsqlcd:table_columns", lambda _: after if db.sent(db_batch) else [[(TABLE, "a"), (TABLE, "b")]]
        )
    else:
        db.respond("azsqlcd:table_columns", [[(TABLE, name) for name in sorted(columns)]])
    return b, st, db


def test_a_dropped_column_fails_a_dependant_that_had_a_table_level_finding_before_the_change():
    """RP2-1: `COLUMNS_NOT_FOUND [sales].[Order]` before the change does not cover a column that
    the release takes away. The engine row is the same before and after, so the runner names the
    column: the finding is new and the run fails."""
    b, st, db = a_reader_that_the_engine_cannot_verify(DROP_A)
    hooks = Hooks(
        dependants=[READER], before={READER: [COLUMNS_NOT_FOUND]}, captures={TABLE: {"columns": ["b"]}}
    )
    error = failure(b, st, db, table_hooks=hooks)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": READER, "findings": ["COLUMN_GONE [sales].[Order].[a]"]}
    assert "send the module again" in error.message  # the way through when the name is another column
    assert db.sent("COMMIT TRANSACTION;") == [] and step_writes(db) == []


def test_a_dropped_column_that_the_dependant_does_not_name_does_not_fail_it():
    b, st, db = a_reader_that_the_engine_cannot_verify(
        DROP_A, body="SELECT o.[b] INTO #t FROM [sales].[Order] AS o; SELECT N'[a]' AS [x] FROM #t; -- a"
    )
    hooks = Hooks(
        dependants=[READER], before={READER: [COLUMNS_NOT_FOUND]}, captures={TABLE: {"columns": ["b"]}}
    )
    report = run_deploy(b, st, db, table_hooks=hooks)
    assert report.exit_code == 0 and db.sent("COMMIT TRANSACTION;")


def test_an_added_column_does_not_fail_a_dependant_that_had_a_table_level_finding_before():
    b, st, db = a_reader_that_the_engine_cannot_verify(ADD_C)
    hooks = Hooks(dependants=[READER], before={READER: [COLUMNS_NOT_FOUND]})
    report = run_deploy(b, st, db, table_hooks=hooks)
    assert report.exit_code == 0 and db.sent("COMMIT TRANSACTION;")


def test_a_dependant_with_no_table_level_finding_costs_no_column_read():
    b, st, db = a_table_release()
    report = run_deploy(b, st, db, table_hooks=Hooks(dependants=[DEPENDANT]))
    assert report.exit_code == 0 and db.sent("azsqlcd:table_columns") == []


def test_another_error_number_after_the_change_is_a_new_finding_of_a_dependant_that_did_not_bind():
    """RP2-1: error 2020 before the change (a sound procedure can raise it, RO-2) does not cover
    error 207 after it: the finding holds the number."""
    b, st, db = a_table_release()
    db.fail_on("azsqlcd:broken_references", sql_error(INVALID_COLUMN), keeps_transaction=True)
    hooks = Hooks(dependants=[DEPENDANT], before={DEPENDANT: [catalog.REFERENCES_NOT_BOUND]})
    error = failure(b, st, db, table_hooks=hooks)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": DEPENDANT, "findings": ["ERROR_207"]}


def test_a_dependant_that_the_release_changes_is_checked_by_the_rule_for_sent_modules():
    """RP2-2 (a): the new text uses a #temp table, so the engine reports is_all_columns_found = 0
    for it. That is no finding of before, and it is what the engine says of a sound module."""
    old = proc("usp_reader", "SELECT [a] FROM [sales].[Order];")
    new = proc("usp_reader", "SELECT o.[a], o.[c] INTO #t FROM [sales].[Order] AS o; SELECT * FROM #t;")
    b = bundle(mig(M1, ADD_C), modules=[new])
    st = recorded(objects={READER: row(READER, old[1]), TABLE: TABLE_ROW})
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    db.respond("azsqlcd:broken_references", lambda _: [[BROKEN_COLUMN]] if db.sent(new[1]) else [[]])
    report = run_deploy(b, st, db, table_hooks=Hooks(dependants=[READER]))
    assert report.exit_code == 0 and report.modules_deployed == (READER,)
    assert len(db.sent("azsqlcd:broken_references")) == 1


def test_a_changed_dependant_that_did_not_bind_before_is_read_after_the_change():
    """RP2-2 (b): the old text gave error 2020. The new text is another module: it is read."""
    old = proc("usp_reader", "SELECT [a] FROM [sales].[Order];")
    b = bundle(
        mig(M1, "DROP TABLE [sales].[Order];"),
        modules=[proc("usp_reader", "SELECT [a], 1 AS [n] FROM [sales].[Order];")],
    )
    st = recorded(objects={READER: row(READER, old[1]), TABLE: TABLE_ROW})
    db = Db(b, st, user_objects=[("sales", "Order", "U")])
    db.fail_on("azsqlcd:broken_references", sql_error(NOT_BOUND), times=9, keeps_transaction=True)
    hooks = Hooks(
        dependants=[READER], before={READER: [catalog.REFERENCES_NOT_BOUND]}, captures={TABLE: None}
    )
    error = failure(b, st, db, table_hooks=hooks)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail == {"object": READER, "findings": [catalog.REFERENCES_NOT_BOUND]}


class DboHooks(Hooks):
    def touched_table_objects(self, work: Work) -> list[str]:
        return [names.object_key("TABLE", "dbo", "Order")]


@pytest.mark.parametrize(
    ("body", "schema", "finding"),
    [
        ("SELECT [a] FROM [Order];", None, "UNRESOLVED [Order]"),
        ("SELECT [a] FROM [dbo].[Order];", "dbo", "UNRESOLVED [dbo].[Order]"),
    ],
)
def test_a_new_procedure_that_names_a_dropped_dbo_table_fails_the_run(body, schema, finding):
    """RP2-3: a one-part name is an object of dbo; the engine row has no schema then."""
    table = names.object_key("TABLE", "dbo", "Order")
    b = bundle(mig(M1, "DROP TABLE [dbo].[Order];"), modules=[proc("usp_new", body)])
    st = recorded(objects={table: TABLE_ROW})
    db = Db(b, st, user_objects=[("dbo", "Order", "U")])
    db.respond("azsqlcd:broken_references", [[(schema, "Order", None, None, 0, 0, 0, None)]])
    error = failure(b, st, db, table_hooks=DboHooks(captures={table: None}))
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "DEPENDANT_BROKEN")
    assert error.detail["findings"] == [finding]
