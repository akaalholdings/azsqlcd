"""Onboarding and drift for modules. Every test drives the code against a FakeSession; no test runs T-SQL.

Db is a FakeSession with a small catalog (the modules that exist) and a recorded state. The
baseline writes through the real frame of the runner, so the batches of the lock, the run row and
the transaction are the ones a deploy sends.
"""

import dataclasses
import hashlib
import json
import re
import tomllib
from collections.abc import Sequence
from typing import Any

import pytest

from azsqlcd import catalog, chain, emit, lex, lint, names, onboard, plan, release, state, tables
from azsqlcd.catalog import Difference
from azsqlcd.catalog_tables import read_model
from azsqlcd.config import load_config
from azsqlcd.errors import Exit, ToolError
from azsqlcd.model import DefaultConstraint, Expression, Model, Schema
from azsqlcd.modules import checksum, read_module
from azsqlcd.onboard import (
    BaselineItem,
    BaselineResult,
    DriftItem,
    ExportResult,
    Quarantined,
    baseline,
    drift,
    export_drift,
    export_modules,
)
from azsqlcd.release import Bundle, Manifest
from azsqlcd.runner import RunError
from azsqlcd.session import ResultSets
from azsqlcd.sqlerrors import sql_error
from azsqlcd.state import Meta, ObjectRow, RunRow, State, StepRow
from fixtures.modules.live import LIVE, ORDER_TOTALS, Live
from support.fake_session import FakeSession
from unit.test_catalog_tables import (
    COLUMNS,
    CUSTOMER,
    CUSTOMER_KEY,
    ORDER,
    ORDER_KEY,
    PRICE_HISTORY_KEY,
    PRICE_KEY,
    SALES,
    CatalogRows,
    only,
    price_rows,
    rows_from_model,
    session_for,
)

COMMIT = "c" * 40
RECORDED_COMMIT = "a" * 40
TOOL_DIGEST = "d" * 64
RUN_ID = 41
STARTED = "2026-10-07T09:00:00.000"
FACTS = (5, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0)
OPTIONS_ON = (
    1,
    1,
    1,
    1,
    1,
    1,
    0,
    16384,
    512,
    0,
)  # the SET options of the runner, IMPLICIT_TRANSACTIONS last; lock timeout and language follow
TYPE_CODES = {"VIEW": "V", "PROCEDURE": "P", "FUNCTION": "FN", "TRIGGER": "TR"}
TYPE_KINDS = {code: kind for kind, code in TYPE_CODES.items()}
LOCK_TIMEOUT = "Lock request time out period exceeded."
SYNTAX_ERROR = "Incorrect syntax near the keyword 'FROM'."
DDL_WORDS = {"CREATE", "ALTER", "DROP", "TRUNCATE", "SP_REFRESHSQLMODULE", "SP_RENAME"}
# how plan.parse_check gives the session back: the setting, then a read that proves that statements run
PARSEONLY_END = ["SET PARSEONLY OFF;", "/* azsqlcd:parseonly_off */ SELECT @@SPID;"]

TOML = """
[project]
name = "sales"
tenant_id = "11111111-1111-1111-1111-111111111111"
table_model = false
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


# ------------------------------------------------------------------ a release, a catalog, a state
def key(kind: str, name: str, schema: str = "sales") -> str:
    return names.object_key(kind, schema, name)


def proc(name: str, body: str = "SELECT 1;") -> tuple[str, str]:
    """(path, text) of a procedure file of the release."""
    return f"schema/procedures/sales.{name}.sql", f"CREATE OR ALTER PROCEDURE [sales].[{name}] AS\n{body}\n"


def stored(name: str, body: str = "SELECT 1;") -> str:
    """The definition as the engine holds it after CREATE PROCEDURE from a Windows client."""
    return f"CREATE PROCEDURE [sales].[{name}] AS\r\n{body}  \r\n\r\n"


def bundle(
    *modules: tuple[str, str],
    ack: Sequence[str] | None = None,
    ack_text: str | None = None,
    tombstones: Sequence[str] = (),
) -> Bundle:
    files = {
        release.CONFIG_PATH: TOML.encode(),
        chain.SUM_PATH: chain.format_sum(chain.Chain(True, ())).encode(),
    }
    for path, text in modules:
        files[path] = text.encode()
    if ack is not None:
        ack_text = "modules = [" + ", ".join(json.dumps(k) for k in ack) + "]\n"
    if ack_text is not None:
        files["onboarding/dev/overwrite-ack.toml"] = ack_text.encode()
    if tombstones:
        files[chain.TOMBSTONES_PATH] = "".join(
            f'[[drop]]\nobject = {json.dumps(object_key)}\nreason = "replaced"\n' for object_key in tombstones
        ).encode()
    manifest = Manifest(
        commit=COMMIT,
        release_seq=7,
        files=tuple(sorted((path, hashlib.sha256(data).hexdigest()) for path, data in files.items())),
        chain_added_in={},
    )
    return Bundle(manifest, files)


def live(
    object_key: str,
    definition: str | None,
    *,
    ansi: bool = True,
    quoted: bool = True,
    can_view: int = 1,
    events: str = "INSERT:0:0",
) -> tuple:
    """One row of the capture_modules result set as the catalog gives it: no requested key."""
    kind, schema, name = names.parse_object_key(object_key)
    trigger = ("sales", "Order", 0, events) if kind == "TRIGGER" else (None, None, None, None)
    return (None, schema, name, TYPE_CODES[kind], definition, ansi, quoted, False, None, can_view, *trigger)


def captured(row: tuple) -> dict[str, Any]:
    """The capture that the tool makes of a catalog row."""
    db = FakeSession()
    db.respond("azsqlcd:capture_modules", [[row]])
    (capture,) = catalog.capture_modules(db, None).values()
    return capture


def managed(row: tuple, source: str | None = None) -> ObjectRow:
    capture = captured(row)
    return ObjectRow("managed", source, 1, capture, state.capture_sha256(capture))


def recorded(
    *,
    steps: Sequence[StepRow] = (),
    objects: dict[str, ObjectRow] | None = None,
    latest: RunRow | None = None,
    env: str = "dev",
) -> State:
    """The state of a target after the setup script: a meta row, and what a test adds."""
    return State(Meta(1, "sales", env), (), latest, tuple(steps), objects or {})


BASELINE_STEP = StepRow(1, 1, "baseline", None, None, "ok", None)
REQUESTED = re.compile(r"\(N'((?:[^']|'')*)', OBJECT_ID\(")
OBJECT_KEY = re.compile(r"WHERE \[object_key\] = N'([^']*)'")


class Db(FakeSession):
    """FakeSession, and what the code reads from a database.

    modules: the rows of the modules that exist. A read by key resolves a name as a
      case-insensitive catalog does, whatever the kind of the object is.
    A rule that a test adds wins over the answers of this class.
    """

    def __init__(
        self,
        *modules: tuple,
        st: State | None = None,
        facts: tuple = FACTS,
        export_facts: Sequence[tuple] = (),
        user_objects: Sequence[tuple] = (),
        indexed_views: Sequence[tuple] = (),
    ) -> None:
        super().__init__()
        self.modules = list(modules)
        self._state, self._facts = st or recorded(), facts
        self._export_facts, self._user_objects = export_facts, user_objects
        self._indexed_views = indexed_views  # (schema, name) of each view that has an index
        self._answers = False

    def _answer_the_reads(self) -> None:
        st = self._state
        lock_timeout_ms = CONFIG.env["dev"].lock_timeout_ms  # every run of these tests is for dev
        self.respond("azsqlcd:fence_facts", [[self._facts]])
        self.respond("azsqlcd:session_options", [[(*OPTIONS_ON, lock_timeout_ms, "us_english")]])
        self.respond("azsqlcd:read_state.tables", [[(table, 1) for table in state.TABLES]])
        self.respond("azsqlcd:read_state.meta", [[dataclasses.astuple(st.meta)]])
        self.respond(
            "azsqlcd:read_state.runs", [[dataclasses.astuple(r) for r in [st.latest_ok_run] if r is not None]]
        )
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
        # the numbered procedures are read in a batch of their own
        self.respond("azsqlcd:export_facts", [[f for f in self._export_facts if f[0] != "NUMBERED"]])
        self.respond("azsqlcd:numbered_procedures", [[f for f in self._export_facts if f[0] == "NUMBERED"]])
        self.respond("azsqlcd:indexed_views", [list(self._indexed_views)])
        self.respond(
            "azsqlcd:list_user_objects", [[*((r[1], r[2], r[3]) for r in self.modules), *self._user_objects]]
        )
        self.respond("INSERT INTO [azsqlcd].[run]", [[(RUN_ID,)]])
        self.respond("azsqlcd:run_started", [[(STARTED,)]])
        self.respond("azsqlcd:read_fence", [[(0,)]])

    def _capture(self, batch: str) -> ResultSets:
        if "VALUES" not in batch:
            return [list(self.modules)]
        found = []
        for requested in (text.replace("''", "'") for text in REQUESTED.findall(batch)):
            _, schema, name = names.parse_object_key(requested)
            wanted = (str(schema).casefold(), name.casefold())
            found += [
                (requested, *r[1:]) for r in self.modules if (r[1].casefold(), r[2].casefold()) == wanted
            ]
        return [found]

    def execute(self, batch: str) -> ResultSets:
        if not self._answers:  # at the first batch, so the rules of a test come first and win
            self._answers = True
            self._answer_the_reads()
        return super().execute(batch)


def run_baseline(db: FakeSession, b: Bundle, *, report_only: bool = False, **options: Any) -> BaselineResult:
    options = {"confirm_database": "sales", "tool_version": "0.1.0", "tool_digest": TOOL_DIGEST} | options
    return baseline(db, b, CONFIG, "dev", "sales-dev", report_only=report_only, **options)


def refusal(db: FakeSession, b: Bundle, **options: Any) -> ToolError:
    with pytest.raises(ToolError) as caught:
        run_baseline(db, b, **options)
    return caught.value


def words(batch: str) -> set[str]:
    """The unquoted words of a batch, upper case. A word in a string or in a comment is not one."""
    return {t.text.upper() for t in lex.significant(lex.tokenize(batch)) if t.kind == "word"}


def status_of(result: BaselineResult) -> dict[str, str]:
    return {item.object_key: item.status for item in result.items}


def export(db: FakeSession, toml: str = TOML, *, syntax_errors_raise: bool = True) -> ExportResult:
    """export_modules on a session that behaves as the engine does under SET PARSEONLY ON."""
    if syntax_errors_raise:
        db.fail_on("SELECT FROM;", sql_error(SYNTAX_ERROR, number=156))
    return export_modules(db, load_config(toml))


# ------------------------------------------------------------------ baseline: what is recorded
def test_a_module_whose_live_text_equals_the_file_is_baselined_with_its_checksum_a_different_one_with_null():
    same, changed = proc("usp_same"), proc("usp_changed", "SELECT 2;")
    rows = [
        live(key("PROCEDURE", "usp_same"), stored("usp_same")),
        live(key("PROCEDURE", "usp_changed"), stored("usp_changed")),
    ]
    db = Db(*rows)

    result = run_baseline(db, bundle(same, changed, ack=[key("PROCEDURE", "usp_changed")]))

    assert result.items == (
        BaselineItem(
            key("PROCEDURE", "usp_changed"),
            "differs",
            None,
            "live text: onboarding/dev/modules/sales.usp_changed.sql",
        ),
        BaselineItem(key("PROCEDURE", "usp_same"), "equal", checksum(same[1].encode())),
    )
    # the row holds the capture of the live module; only the module with the text of its file has a source
    assert db.sent("UPDATE [azsqlcd].[object]") == [
        state.upsert_object(
            key("PROCEDURE", "usp_changed"), run_id=RUN_ID, capture=captured(rows[1]), source_sha256=None
        ),
        state.upsert_object(
            key("PROCEDURE", "usp_same"),
            run_id=RUN_ID,
            capture=captured(rows[0]),
            source_sha256=checksum(same[1].encode()),
        ),
    ]


def test_the_live_text_is_compared_after_the_header_rewrite_of_export():
    # A15: the catalog holds the name [Sales].[USP_X]; export would write that name into the header,
    # so the stale header [sales].[usp_x] is not the text of the file, although the bytes agree
    renamed = live(key("PROCEDURE", "USP_X", schema="Sales"), stored("usp_x"))
    result = run_baseline(Db(renamed), bundle(proc("usp_x")), report_only=True)

    assert status_of(result) == {key("PROCEDURE", "usp_x"): "differs"}
    (live_text,) = result.live_files.values()
    assert live_text == b"CREATE OR ALTER PROCEDURE [Sales].[USP_X] AS\nSELECT 1;  \n\n"


def test_a_file_whose_name_is_taken_by_another_kind_is_not_recorded():
    view = ("schema/views/sales.x.sql", "CREATE OR ALTER VIEW [sales].[x] AS\nSELECT 1 AS [a];\n")
    db = Db(live(key("PROCEDURE", "x"), stored("x")))

    result = run_baseline(db, bundle(view))

    assert status_of(result) == {key("VIEW", "x"): "missing here", key("PROCEDURE", "x"): "only here"}
    assert "NAME_COLLISION" in result.items[1].note
    assert db.sent("UPDATE [azsqlcd].[object]") == []


def test_baseline_records_the_run_and_one_baseline_step_in_one_transaction_under_the_lock():
    db = Db(live(key("PROCEDURE", "usp_x"), stored("usp_x")))

    result = run_baseline(db, bundle(proc("usp_x")))

    db.assert_order(
        "SET XACT_ABORT ON",
        "azsqlcd:fence_facts",
        "sp_getapplock",
        "azsqlcd:read_state.tables",
        "INSERT INTO [azsqlcd].[run]",
        "BEGIN TRANSACTION",
        "UPDATE [azsqlcd].[object]",
        "INSERT INTO [azsqlcd].[step]",
        "azsqlcd:guard",
        "COMMIT TRANSACTION",
        "UPDATE [azsqlcd].[run] SET [status] = N'ok'",
        "sp_releaseapplock",
    )
    (step,) = db.sent("INSERT INTO [azsqlcd].[step]")
    assert f"VALUES ({RUN_ID}, N'baseline', NULL, NULL, N'ok'" in step
    assert result.report is not None
    assert (result.report.command, result.report.exit_code, result.report.run_id) == ("baseline", 0, RUN_ID)
    assert f"Recorded as run {RUN_ID}" in result.report_md
    assert db.closed  # the lock dies with the session


def test_a_baseline_does_not_move_the_recorded_release():
    # the run row of a baseline copies the release of the last ok run: the baseline deployed nothing
    fresh = Db()
    run_baseline(fresh, bundle())
    (first,) = fresh.sent("INSERT INTO [azsqlcd].[run]")
    assert f"VALUES (N'baseline', N'running', 0, 0, N'{'0' * 40}'" in first

    deployed = Db(st=recorded(latest=RunRow(3, "deploy", "ok", 0, 5, RECORDED_COMMIT, STARTED)))
    run_baseline(deployed, bundle())
    (later,) = deployed.sent("INSERT INTO [azsqlcd].[run]")
    assert f"VALUES (N'baseline', N'running', 0, 5, N'{RECORDED_COMMIT}'" in later

    # the row of a resolve run records no release (state.read_state), so there is none to copy
    resolved = Db(st=recorded(latest=RunRow(3, "resolve", "ok", 0, 5, RECORDED_COMMIT, STARTED)))
    run_baseline(resolved, bundle())
    (after_resolve,) = resolved.sent("INSERT INTO [azsqlcd].[run]")
    assert f"VALUES (N'baseline', N'running', 0, 0, N'{'0' * 40}'" in after_resolve


def test_baseline_sends_no_create_alter_or_drop():
    assert "CREATE" in words(proc("usp_x")[1])  # the check sees DDL that is sent as a batch
    files = [proc("usp_same"), proc("usp_changed", "SELECT 2;"), proc("usp_new")]
    rows = [
        live(key("PROCEDURE", "usp_same"), stored("usp_same")),
        live(key("PROCEDURE", "usp_changed"), stored("usp_changed")),
        live(key("PROCEDURE", "usp_only_here"), stored("usp_only_here")),
    ]
    written, reported = Db(*rows), Db(*rows)

    run_baseline(written, bundle(*files, ack=[key("PROCEDURE", "usp_changed")]))
    run_baseline(reported, bundle(*files), report_only=True)

    assert written.sent("UPDATE [azsqlcd].[object]")  # module text was sent, as a value of a state row
    assert [batch for batch in written.batches + reported.batches if DDL_WORDS & words(batch)] == []


def test_a_report_only_baseline_reads_and_writes_nothing():
    changed = live(key("PROCEDURE", "usp_changed"), stored("usp_changed", "SELECT 'live-marker';"))
    db = Db(changed)

    result = run_baseline(db, bundle(proc("usp_changed")), report_only=True)

    assert all(batch.startswith("/* azsqlcd:") and "SELECT" in words(batch) for batch in db.batches)
    assert db.sent("sp_getapplock") == [] and not db.closed
    # the live text is a file for the review of the overwrite; the report names it and holds no text
    assert result.live_files == {
        "onboarding/dev/modules/sales.usp_changed.sql": (
            b"CREATE OR ALTER PROCEDURE [sales].[usp_changed] AS\nSELECT 'live-marker';  \n\n"
        )
    }
    assert "live-marker" not in result.report_md
    assert "onboarding/dev/modules/sales.usp_changed.sql" in result.report_md
    assert result.overwrite_modules == result.unacknowledged == (key("PROCEDURE", "usp_changed"),)
    assert result.report is None


def test_the_live_text_of_a_module_with_a_secret_literal_is_not_returned_as_a_file():
    secret = stored("usp_changed", "OPEN SYMMETRIC KEY k DECRYPTION BY PASSWORD = 'hunter2';")
    result = run_baseline(
        Db(live(key("PROCEDURE", "usp_changed"), secret)), bundle(proc("usp_changed")), report_only=True
    )

    assert status_of(result) == {key("PROCEDURE", "usp_changed"): "differs"}
    assert result.live_files == {} and "hunter2" not in result.report_md


# ------------------------------------------------------------------ baseline: refusals
def test_baseline_refuses_until_every_overwritten_module_is_acknowledged():
    files = [proc("usp_a", "SELECT 2;"), proc("usp_b", "SELECT 2;")]
    rows = [
        live(key("PROCEDURE", "usp_a"), stored("usp_a")),
        live(key("PROCEDURE", "usp_b"), stored("usp_b")),
    ]

    for ack in (None, [key("PROCEDURE", "usp_a")]):
        db = Db(*rows)
        error = refusal(db, bundle(*files, ack=ack))
        assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "BASELINE_ACK_REQUIRED")
        assert key("PROCEDURE", "usp_b") in error.detail["objects"]
        assert db.sent("INSERT INTO [azsqlcd]") == [] and db.sent("UPDATE [azsqlcd]") == []
        assert db.sent("BEGIN TRANSACTION") == []
    assert error.detail["objects"] == [key("PROCEDURE", "usp_b")]

    db = Db(*rows)
    result = run_baseline(db, bundle(*files, ack=[key("PROCEDURE", "usp_a"), key("PROCEDURE", "usp_b")]))
    assert result.overwrite_modules == (key("PROCEDURE", "usp_a"), key("PROCEDURE", "usp_b"))
    assert result.unacknowledged == () and len(db.sent("INSERT INTO [azsqlcd].[step]")) == 1


@pytest.mark.parametrize(
    "ack_text",
    [
        "modules = [",
        'modules = ["PROCEDURE:[sales].[usp_a]"]\nreason = "reviewed"\n',
        'modules = "PROCEDURE:[sales].[usp_a]"\n',
        'modules = ["usp_a"]\n',
        'modules = ["TABLE:[sales].[Order]"]\n',
    ],
)
def test_an_acknowledgement_file_that_cannot_be_read_as_a_list_of_module_keys_refuses_before_any_batch(
    ack_text,
):
    db = Db()
    error = refusal(db, bundle(ack_text=ack_text))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "BASELINE_ACK_INVALID")
    assert db.batches == []


def test_a_second_baseline_is_refused():
    db = Db(live(key("PROCEDURE", "usp_x"), stored("usp_x")), st=recorded(steps=[BASELINE_STEP]))

    error = refusal(db, bundle(proc("usp_x")))

    assert isinstance(error, RunError)
    assert (error.exit_code, error.reason_code, error.report.run_id) == (
        Exit.REFUSED,
        "ALREADY_BASELINED",
        None,
    )
    assert db.sent("INSERT INTO [azsqlcd]") == [] and db.sent("UPDATE [azsqlcd]") == []
    # the report of a database that is baselined says so, and is still given
    again = run_baseline(Db(st=recorded(steps=[BASELINE_STEP])), bundle(), report_only=True)
    assert "baseline step already" in again.report_md


def test_baseline_refuses_another_database_than_the_one_that_was_confirmed():
    db = Db()
    error = refusal(db, bundle(), confirm_database="sales_copy")
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "CONFIRM_MISMATCH")
    assert db.sent("sp_getapplock") == [] and db.sent("INSERT INTO [azsqlcd]") == []

    other = Db(facts=(5, "READ_WRITE", "sales_copy", "SQL_Latin1_General_CP1_CI_AS", 0))
    assert (
        refusal(other, bundle(), confirm_database="sales_copy", report_only=True).reason_code
        == "FENCE_DB_NAME"
    )


def test_baseline_report_only_refuses_another_database_name():
    # TQ-09: the report of a baseline is the review of an overwrite; it must be the report of the
    # database that the operator named, and it stops before the state and the modules are read
    db = Db(live(key("PROCEDURE", "usp_x"), stored("usp_x")))

    error = refusal(db, bundle(proc("usp_x")), confirm_database="sales_copy", report_only=True)

    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "CONFIRM_MISMATCH")
    assert error.detail == {"db_name": "sales", "confirmed": "sales_copy"}
    assert len(db.batches) == 1 and db.batches[0].startswith("/* azsqlcd:fence_facts */")
    # the name is compared as it is written: --confirm-database is typed by a human for this run
    assert (
        refusal(Db(), bundle(), confirm_database="SALES", report_only=True).reason_code == "CONFIRM_MISMATCH"
    )
    assert run_baseline(Db(), bundle(), confirm_database="sales", report_only=True).report is None


def test_baseline_refuses_a_database_that_is_bound_to_another_environment():
    db = Db(st=recorded(env="prod"))
    assert refusal(db, bundle()).reason_code == "FENCE_META_MISMATCH"
    assert db.sent("INSERT INTO [azsqlcd]") == []


def test_baseline_stops_when_another_run_holds_the_lock():
    db = Db(live(key("PROCEDURE", "usp_x"), stored("usp_x")))
    db.applock_result = -1

    error = refusal(db, bundle(proc("usp_x")))

    assert (error.exit_code, error.reason_code) == (Exit.LOCKED, "LOCK_NOT_GRANTED")
    assert db.sent("azsqlcd:read_state") == [] and db.sent("INSERT INTO [azsqlcd]") == []


def test_a_baseline_closes_the_run_row_of_a_run_that_died_as_a_deploy_does():
    """A4: under the lock every run row with the status running is a dead run."""
    db = Db()
    db.respond("azsqlcd:read_state.runs", [[(9, "baseline", "running", 0, 0, "0" * 40, STARTED)]])

    run_baseline(db, bundle())

    (closed,) = db.sent("N'reconciled'")
    assert "SET [status] = N'failed'" in closed and "WHERE [run_id] = 9" in closed
    db.assert_order("sp_getapplock", "N'reconciled'", "INSERT INTO [azsqlcd].[run]")


def test_the_session_of_a_baseline_gets_the_options_of_a_run_before_the_fence():
    db = Db()
    run_baseline(db, bundle())
    assert db.batches[0].startswith("SET XACT_ABORT ON; SET LOCK_TIMEOUT 30000; SET NOCOUNT ON;")
    assert db.batches[0].endswith("SET LANGUAGE us_english;")
    db.assert_order("azsqlcd:session_options", "azsqlcd:fence_facts", "sp_getapplock")


def test_a_failed_state_write_rolls_the_baseline_back_and_leaves_no_baseline_step():
    db = Db(live(key("PROCEDURE", "usp_x"), stored("usp_x")))
    db.fail_on("UPDATE [azsqlcd].[object]", sql_error(LOCK_TIMEOUT))

    error = refusal(db, bundle(proc("usp_x")))

    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, "LOCK_TIMEOUT")
    db.assert_order("BEGIN TRANSACTION", "UPDATE [azsqlcd].[object]", "ROLLBACK", "azsqlcd:read_fence")
    assert db.sent("INSERT INTO [azsqlcd].[step]") == [] and db.sent("COMMIT") == []
    assert len(db.sent("UPDATE [azsqlcd].[run] SET [status] = N'failed'")) == 1


# ------------------------------------------------------------------ baseline: the table model
MODEL = Model([SALES, ORDER, CUSTOMER])
SCHEMA_KEY = SALES.key
TABLE_CONFIG = load_config(TOML.replace("table_model = false", "table_model = true"))
SNAPSHOT_PATH = "onboarding/prod/snapshot.json"
USP_X = key("PROCEDURE", "usp_x")


def table_files(model: Model = MODEL) -> list[tuple[str, str]]:
    """(path, text) of the table-class files of a release: the canonical text of the model."""
    return [
        (
            names.path_for(obj.kind, None if isinstance(obj, Schema) else obj.schema, obj.name),
            emit.emit_object_file(obj),
        )
        for obj in model.values()
    ]


def with_tables(db: Db, catalog_of: Model | CatalogRows = MODEL) -> Db:
    """The session also answers the table reader, as a database that holds this model."""
    rows = rows_from_model(catalog_of) if isinstance(catalog_of, Model) else catalog_of
    for name in COLUMNS:
        db.respond(f"/* azsqlcd:read_{name} */", [[tuple(row.values()) for row in rows[name]]])
    return db


def a_table_release(*more: tuple[str, str]) -> tuple[Bundle, tables.Hooks]:
    b = bundle(proc("usp_x"), *table_files(), *more)
    return b, tables.Hooks(b, TABLE_CONFIG)


def object_rows(db: FakeSession) -> dict[str, str]:
    """object key -> the batch that writes its row."""
    return {OBJECT_KEY.findall(batch)[0]: batch for batch in db.sent("UPDATE [azsqlcd].[object]")}


def test_a_baseline_with_a_table_model_records_each_object_of_the_model_with_no_source_checksum():
    b, hooks = a_table_release()
    db = with_tables(Db(live(USP_X, stored("usp_x"))))

    result = run_baseline(db, b, table_hooks=hooks)

    rows = object_rows(db)
    assert set(rows) == {USP_X, SCHEMA_KEY, ORDER_KEY, CUSTOMER_KEY}
    captures = read_model(session_for(rows_from_model(MODEL))).captures
    for table_key in (SCHEMA_KEY, ORDER_KEY, CUSTOMER_KEY):
        assert "[source_sha256] = NULL" in rows[table_key]  # the model is the source of a table
        assert f"N'{state.capture_sha256(captures[table_key])}'" in rows[table_key]
    assert f"N'{checksum(proc('usp_x')[1].encode())}'" in rows[USP_X]
    (step,) = db.sent("N'baseline', NULL, NULL, N'ok'")
    assert "N'3 table-class object(s), 1 module(s), 0 to overwrite'" in step
    # read under the lock, written in one transaction; no object DDL
    db.assert_order(
        "sp_getapplock", "azsqlcd:read_tabulars", "BEGIN TRANSACTION", step, "COMMIT TRANSACTION;"
    )
    assert not {word for batch in db.batches for word in words(batch)} & DDL_WORDS
    assert "3 object(s) of the table model" in result.report_md and result.table_items == ()


def a_column_differs(rows: CatalogRows) -> None:
    only(rows, "columns", name="Status")["is_nullable"] = 1


def a_table_is_missing(rows: CatalogRows) -> None:
    customer = only(rows, "tabulars", name="Customer")
    rows["tabulars"].remove(customer)


@pytest.mark.parametrize(
    ("change", "object_key"), [(a_column_differs, ORDER_KEY), (a_table_is_missing, CUSTOMER_KEY)]
)
def test_a_table_that_differs_from_the_model_or_is_missing_refuses_the_baseline_and_nothing_is_written(
    change, object_key
):
    b, hooks = a_table_release()
    catalog_rows = rows_from_model(MODEL)
    change(catalog_rows)
    db = with_tables(Db(live(USP_X, stored("usp_x"))), catalog_rows)

    error = refusal(db, b, table_hooks=hooks)

    # nothing was executed, so the read-back that differs is a refusal and not a rollback
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "READBACK_MISMATCH")
    assert object_key in [found["object"] for found in error.detail["objects"]]
    assert db.sent("INSERT INTO [azsqlcd]") == [] and db.sent("UPDATE [azsqlcd]") == []
    assert db.sent("BEGIN TRANSACTION") == []


def test_after_the_switch_to_a_table_model_a_second_baseline_records_the_table_objects_only():
    b, hooks = a_table_release()
    hot_fix = live(USP_X, stored("usp_x", "SELECT 'changed since the first baseline';"))
    st = recorded(steps=[BASELINE_STEP], objects={USP_X: managed(live(USP_X, stored("usp_x")), "1" * 64)})
    db = with_tables(Db(hot_fix, st=st))

    result = run_baseline(db, b, table_hooks=hooks)

    # the module rows stay as they are: a module that changed since is drift, not a new baseline
    assert set(object_rows(db)) == {SCHEMA_KEY, ORDER_KEY, CUSTOMER_KEY}
    assert db.sent("azsqlcd:capture_modules") == [] and result.items == ()
    (step,) = db.sent("N'baseline', NULL, NULL, N'ok'")
    assert "N'3 table-class object(s)'" in step
    assert "baseline step already" in result.report_md


def test_a_database_whose_tables_are_recorded_is_not_baselined_again():
    b, hooks = a_table_release()
    table_row = ObjectRow("managed", None, 1, {"kind": "TABLE"}, state.capture_sha256({"kind": "TABLE"}))
    for objects in ({ORDER_KEY: table_row}, {ORDER_KEY: dataclasses.replace(table_row, status="dropped")}):
        db = with_tables(Db(st=recorded(steps=[BASELINE_STEP], objects=objects)))
        error = refusal(db, b, table_hooks=hooks)
        assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "ALREADY_BASELINED")
        assert db.sent("azsqlcd:read_tabulars") == [] and db.sent("INSERT INTO [azsqlcd]") == []


def reference_snapshot(model: Model = MODEL) -> tuple[str, str]:
    """(path, text) of the snapshot that `export --env prod` wrote for a database with this model."""
    exported = tables.export_tables(session_for(rows_from_model(model)), TABLE_CONFIG)
    return SNAPSHOT_PATH, json.dumps(exported.snapshot)


def test_a_baseline_report_compares_the_tables_with_the_snapshot_of_the_reference_database():
    b, hooks = a_table_release(reference_snapshot())
    here = rows_from_model(MODEL)
    only(here, "columns", name="Note")["max_length"] = 200
    named_by_the_engine = only(here, "defaults", name="DF_Order_Status")
    named_by_the_engine.update(name="DF__Order__Status__1A2B3C4D", is_system_named=1)
    db = with_tables(Db(live(USP_X, stored("usp_x"))), here)

    result = run_baseline(db, b, report_only=True, table_hooks=hooks)

    found = {item.object_key: (item.state, item.properties) for item in result.table_items}
    assert found == {
        SCHEMA_KEY: ("equal", ()),
        CUSTOMER_KEY: ("equal", ()),
        ORDER_KEY: ("differs", ("column [Note]",)),
    }
    assert f"| {ORDER_KEY} | differs | column [Note] |" in result.report_md
    assert f"against {SNAPSHOT_PATH} of the release" in result.report_md
    # the constraint that the engine named has the shape of the reference: a rename, not a difference
    renames = result.rename_constraints_sql
    assert renames.startswith(
        "EXEC sys.sp_rename N'[sales].[DF__Order__Status__1A2B3C4D]', N'DF_Order_Status'"
    )
    assert "rename-constraints.sql" in result.report_md
    assert status_of(result) == {USP_X: "equal"} and result.report is None
    assert all(batch.startswith("/* azsqlcd:") for batch in db.batches)  # read-only
    assert "((0))" not in result.report_md and "[Status]<(9)" not in result.report_md  # no expression text


def test_a_baseline_report_without_the_reference_snapshot_says_so_and_compares_modules_only():
    b, hooks = a_table_release()
    db = with_tables(Db(live(USP_X, stored("usp_x"))))

    result = run_baseline(db, b, report_only=True, table_hooks=hooks)

    assert result.table_items == () and result.rename_constraints_sql == ""
    assert f"Not compared: the release holds no {SNAPSHOT_PATH}" in result.report_md
    assert status_of(result) == {USP_X: "equal"}
    assert "CONSTRAINT_NAMES" not in result.report_md


def test_a_baseline_report_without_the_reference_snapshot_still_names_engine_named_constraints():
    # live: a repository with [env.dev] only has no onboarding/prod/snapshot.json; the report said
    # "Modules only" and the baseline that writes then refused with CONSTRAINT_NAMES
    b, hooks = a_table_release()
    db = with_tables(Db(live(USP_X, stored("usp_x"))), engine_named_primary_key())

    result = run_baseline(db, b, report_only=True, table_hooks=hooks)

    assert result.table_items == () and result.rename_constraints_sql == RENAME_PRIMARY_KEY
    assert f"Not compared: the release holds no {SNAPSHOT_PATH}" in result.report_md
    assert "Modules only" not in result.report_md and "READBACK_MISMATCH" in result.report_md
    assert (
        f"`{CUSTOMER_KEY} PRIMARY KEY [PK__Customer__3214EC07A1B2C3D4] -> [PK_Customer]`" in result.report_md
    )


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"format": 2, "captures": {}, "column_order": {}}'])
def test_a_reference_snapshot_that_the_export_did_not_write_is_refused(text):
    b, hooks = a_table_release((SNAPSHOT_PATH, text))
    with pytest.raises(ToolError) as caught:
        run_baseline(with_tables(Db()), b, report_only=True, table_hooks=hooks)
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "SNAPSHOT_INVALID")


def test_without_a_table_model_the_baseline_report_says_that_tables_are_not_compared():
    result = run_baseline(Db(), bundle(), report_only=True)
    assert "Modules only. Table-class objects are not compared." in result.report_md
    assert "## Table-class objects" not in result.report_md


# ------------------------------------------------------------------ unmanaged objects
def test_an_unmanaged_live_module_is_reported_and_never_planned_for_a_drop():
    ours, theirs = (
        live(key("PROCEDURE", "usp_x"), stored("usp_x")),
        live(key("PROCEDURE", "usp_dba"), stored("usp_dba")),
    )
    # the release even holds a tombstone for the module that it has no file for
    release_files = bundle(proc("usp_x"), tombstones=[key("PROCEDURE", "usp_dba")])
    db = Db(ours, theirs)

    result = run_baseline(db, release_files)

    assert status_of(result) == {key("PROCEDURE", "usp_x"): "equal", key("PROCEDURE", "usp_dba"): "only here"}
    rows = [OBJECT_KEY.findall(batch)[0] for batch in db.sent("UPDATE [azsqlcd].[object]")]
    assert rows == [key("PROCEDURE", "usp_x")]
    # no object row, so the module is not managed: a plan on the recorded state drops nothing
    after = recorded(
        steps=[BASELINE_STEP],
        objects={object_key: managed(ours, checksum(proc("usp_x")[1].encode())) for object_key in rows},
    )
    work = plan.pending_work(release_files, after, 100)
    assert work.drops == () and [step for unit in work.units for step in unit.steps] == []
    # drift reports it, and it is not drift
    found = drift(Db(ours, theirs, st=after), release_files, CONFIG, "dev", "sales-dev")
    assert found.items == (DriftItem(key("PROCEDURE", "usp_dba"), "exists", None, None, "unmanaged"),)
    assert not found.has_drift


# ------------------------------------------------------------------ drift
class TableDrift:
    """The table model, as far as drift uses it: it gives the differences that a test stages."""

    def __init__(self, differing: dict[str, list[Difference]]) -> None:
        self.differing = differing
        self.asked: list[str] = []

    def table_drift(self, session, state, keys):
        self.asked = list(keys)
        return self.differing


def test_drift_output_never_holds_definition_text():
    was = live(key("PROCEDURE", "usp_x"), stored("usp_x", "SELECT 'stored-marker';"))
    now = live(key("PROCEDURE", "usp_x"), stored("usp_x", "SELECT 'live-marker';"), ansi=False)
    db = Db(now, st=recorded(objects={key("PROCEDURE", "usp_x"): managed(was)}))

    result = drift(db, bundle(proc("usp_x")), CONFIG, "dev", "sales-dev")

    assert [(item.object_key, item.property, item.cls) for item in result.items] == [
        (key("PROCEDURE", "usp_x"), "definition", "managed"),
        (key("PROCEDURE", "usp_x"), "uses_ansi_nulls", "managed"),
    ]
    assert result.has_drift
    for item in result.items:
        assert re.fullmatch(r"[0-9a-f]{64}", str(item.stored_hash)) and item.stored_hash != item.live_hash
        assert re.fullmatch(r"[0-9a-f]{64}", str(item.live_hash))
    assert "marker" not in repr(result) and "SELECT" not in repr(result)
    assert all(batch.startswith("/* azsqlcd:") for batch in db.batches)  # read-only


def test_a_managed_module_that_is_gone_is_drift():
    gone = managed(live(key("PROCEDURE", "usp_gone"), stored("usp_gone")))
    result = drift(
        Db(st=recorded(objects={key("PROCEDURE", "usp_gone"): gone})), bundle(), CONFIG, "dev", "sales-dev"
    )

    assert result.items == (
        DriftItem(key("PROCEDURE", "usp_gone"), "exists", gone.catalog_sha256, None, "missing"),
    )
    assert result.has_drift


def test_a_managed_module_that_equals_its_capture_and_a_dropped_row_are_not_drift():
    row = live(key("PROCEDURE", "usp_x"), stored("usp_x"))
    dropped = dataclasses.replace(
        managed(live(key("PROCEDURE", "usp_old"), stored("usp_old"))), status="dropped"
    )
    objects = {key("PROCEDURE", "usp_x"): managed(row), key("PROCEDURE", "usp_old"): dropped}

    result = drift(Db(row, st=recorded(objects=objects)), bundle(), CONFIG, "dev", "sales-dev")

    assert result.items == () and not result.has_drift


def test_drift_of_table_class_objects_comes_from_the_table_model():
    table = "TABLE:[sales].[Order]"
    row = ObjectRow("managed", None, 1, {"columns": []}, state.capture_sha256({"columns": []}))
    hooks = TableDrift({table: [Difference("columns", "1" * 64, "2" * 64)]})
    db = Db(st=recorded(objects={table: row}), user_objects=[("sales", "Order", "U"), ("audit", "Log", "U")])

    with_model = drift(db, bundle(), CONFIG, "dev", "sales-dev", table_hooks=hooks)
    without = drift(Db(st=recorded(objects={table: row})), bundle(), CONFIG, "dev", "sales-dev")

    assert hooks.asked == [table]
    assert with_model.items == (
        DriftItem("TABLE:[audit].[Log]", "exists", None, None, "unmanaged"),
        DriftItem(table, "columns", "1" * 64, "2" * 64, "managed"),
    )
    assert without.items == () and not without.has_drift  # no table model: tables are not compared


def test_drift_does_not_report_the_history_table_of_a_temporal_table_as_unmanaged():
    table = "TABLE:[sales].[Order]"
    row = ObjectRow("managed", None, 1, {"columns": []}, state.capture_sha256({"columns": []}))
    live_tables = [("sales", "Order", "U"), ("sales", "Order_History", "U"), ("audit", "Log", "U")]
    unmanaged = lambda found: [i.object_key for i in found.items if i.cls == "unmanaged"]  # noqa: E731

    db = Db(st=recorded(objects={table: row}), user_objects=live_tables)
    db.respond("azsqlcd:history_tables", [[("SALES", "order_history", "sales", "Order")]])
    found = drift(db, bundle(), CONFIG, "dev", "sales-dev", table_hooks=TableDrift({}))
    # the engine owns it; a table that nothing owns is still reported
    assert unmanaged(found) == ["TABLE:[audit].[Log]"] and not found.has_drift
    assert all(batch.startswith("/* azsqlcd:") for batch in db.batches)  # read-only

    # the same name with no system-versioned table behind it is a table like any other: after
    # DROP of the temporal table the former history table shows up here
    plain = Db(st=recorded(objects={table: row}), user_objects=live_tables)
    found = drift(plain, bundle(), CONFIG, "dev", "sales-dev", table_hooks=TableDrift({}))
    assert unmanaged(found) == ["TABLE:[audit].[Log]", "TABLE:[sales].[Order_History]"]

    # no table model: no table is listed, and the catalog is not asked
    without = Db(st=recorded(objects={table: row}), user_objects=live_tables)
    assert drift(without, bundle(), CONFIG, "dev", "sales-dev").items == ()
    assert without.sent("azsqlcd:history_tables") == []


@pytest.mark.parametrize(
    ("facts", "reason_code"),
    [
        ((8, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_ENGINE_EDITION"),
        ((5, "READ_ONLY", "sales", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_READ_ONLY"),
        ((5, "READ_WRITE", "sales_copy", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_DB_NAME"),
        ((5, "READ_WRITE", "sales", "Latin1_General_100_CS_AS", 1), "FENCE_CASE_SENSITIVE"),
    ],
)
def test_drift_refuses_a_database_that_fails_the_fence(facts, reason_code):
    # TQ-09: drift of the wrong database would be reported under the name of the target
    row = live(key("PROCEDURE", "usp_x"), stored("usp_x"))
    db = Db(row, st=recorded(objects={key("PROCEDURE", "usp_x"): managed(row)}), facts=facts)

    with pytest.raises(ToolError) as caught:
        drift(db, bundle(), CONFIG, "dev", "sales-dev")

    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, reason_code)
    assert len(db.batches) == 1 and db.batches[0].startswith("/* azsqlcd:fence_facts */")  # nothing was read


def test_drift_refuses_a_database_that_is_bound_to_another_environment():
    with pytest.raises(ToolError) as caught:
        drift(Db(st=recorded(env="prod")), bundle(), CONFIG, "dev", "sales-dev")
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "FENCE_META_MISMATCH")


def test_the_live_text_of_one_drifted_module_is_exported_under_the_name_of_its_key():
    db = Db(live(key("PROCEDURE", "USP_X"), "ALTER PROCEDURE sales.usp_old AS\r\nSELECT 2;\r\n"))

    path, data = export_drift(db, key("PROCEDURE", "usp_x"))

    assert path == "schema/procedures/sales.usp_x.sql"
    assert data == b"CREATE OR ALTER PROCEDURE [sales].[usp_x] AS\nSELECT 2;\n"
    assert read_module(path, data).key == key("PROCEDURE", "usp_x")  # the plan reads the file


@pytest.mark.parametrize(
    ("row", "asked", "reason_code"),
    [
        (
            live(key("PROCEDURE", "usp_x"), stored("usp_x", "CREATE LOGIN a WITH PASSWORD = 'hunter2';")),
            None,
            "SECRET_LITERAL",
        ),
        (live(key("PROCEDURE", "usp_x"), None), None, "MODULE_NOT_EXPORTABLE"),
        (live(key("PROCEDURE", "usp_x"), "SELECT 1;"), None, "MODULE_NOT_EXPORTABLE"),
        (live(key("PROCEDURE", "usp_other"), stored("usp_other")), None, "MODULE_NOT_EXPORTABLE"),
        (live(key("PROCEDURE", "usp_x"), stored("usp_x")), key("VIEW", "usp_x"), "MODULE_NOT_EXPORTABLE"),
        (live(key("PROCEDURE", "usp_x"), stored("usp_x")), "TABLE:[sales].[Order]", "MODULE_NOT_EXPORTABLE"),
        (live(key("PROCEDURE", "usp_x"), stored("usp_x")), "usp_x", "MODULE_NOT_EXPORTABLE"),
    ],
)
def test_a_live_text_that_cannot_be_a_module_file_is_not_exported(row, asked, reason_code):
    with pytest.raises(ToolError) as caught:
        export_drift(Db(row), asked or key("PROCEDURE", "usp_x"))
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, reason_code)
    assert "hunter2" not in caught.value.message and "SELECT 1" not in caught.value.message


# ------------------------------------------------------------------ export
def test_export_corrects_a_stale_name_left_by_sp_rename_and_reports_the_edit():
    renamed = live(
        key("PROCEDURE", "usp_new", schema="dbo"), "CREATE PROCEDURE dbo.usp_old AS\r\nSELECT 1;\r\n"
    )

    result = export(Db(renamed))

    path = "schema/procedures/dbo.usp_new.sql"
    assert result.files == {path: b"CREATE OR ALTER PROCEDURE [dbo].[usp_new] AS\nSELECT 1;\n"}
    assert f"| {path} | verb: CREATE -> CREATE OR ALTER |" in result.report_md
    assert f"| {path} | name: dbo.usp_old -> [dbo].[usp_new] |" in result.report_md
    assert result.unmanaged == ()


def test_an_exported_file_is_a_module_file_that_the_tool_reads_under_the_catalog_key():
    rows = [
        live(key("VIEW", "vw_x"), "create view sales.vw_x as select 1 as a"),
        live(key("FUNCTION", "fn_x"), "CREATE FUNCTION [sales].[fn_x] () RETURNS int AS BEGIN RETURN 1; END"),
        live(
            key("TRIGGER", "tr_x"), "CREATE TRIGGER [sales].[tr_x] ON [sales].[Order] AFTER INSERT AS RETURN;"
        ),
    ]

    result = export(Db(*rows))

    assert {read_module(path, data).key for path, data in result.files.items()} == {
        key("VIEW", "vw_x"),
        key("FUNCTION", "fn_x"),
        key("TRIGGER", "tr_x"),
    }
    assert result.files["schema/views/sales.vw_x.sql"] == b"CREATE OR ALTER view sales.vw_x as select 1 as a"
    assert "select 1 as a" not in result.report_md  # A25: the report holds names and edits, no text


def test_an_encrypted_module_is_quarantined():
    db = Db(live(key("PROCEDURE", "usp_enc"), None), live(key("PROCEDURE", "usp_x"), stored("usp_x")))

    result = export(db)

    assert list(result.files) == ["schema/procedures/sales.usp_x.sql"]
    (quarantined,) = result.unmanaged
    assert (quarantined.object_key, quarantined.code) == (key("PROCEDURE", "usp_enc"), "ENCRYPTED")


def test_a_module_that_cannot_be_read_for_lack_of_permission_stops_the_export():
    db = Db(
        live(key("PROCEDURE", "usp_hidden"), None, can_view=0),
        live(key("PROCEDURE", "usp_x"), stored("usp_x")),
    )

    with pytest.raises(ToolError) as caught:
        export(db)

    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "NO_VIEW_DEFINITION")
    assert db.sent("PARSEONLY") == []  # nothing was sent after the read


@pytest.mark.parametrize(
    "body",
    [
        "CREATE DATABASE SCOPED CREDENTIAL c WITH IDENTITY = 'u', SECRET = 'hunter2';",
        "EXEC (N'CREATE LOGIN a WITH PASSWORD = ''hunter2''');",  # dynamic SQL: lint reads no string
        "SELECT 1; -- was: password = 'hunter2'",
    ],
)
def test_a_module_with_a_secret_literal_is_quarantined_not_written_to_a_file(body):
    db = Db(
        live(key("PROCEDURE", "usp_secret"), stored("usp_secret", body)),
        live(key("PROCEDURE", "usp_x"), stored("usp_x")),
    )

    result = export(db)

    assert list(result.files) == ["schema/procedures/sales.usp_x.sql"]
    (quarantined,) = result.unmanaged
    assert (quarantined.object_key, quarantined.code) == (key("PROCEDURE", "usp_secret"), "SECRET_LITERAL")
    assert "hunter2" not in result.report_md + quarantined.reason
    assert [batch for batch in db.batches if "hunter2" in batch] == []


@pytest.mark.parametrize(
    ("row", "facts", "code"),
    [
        (live(key("PROCEDURE", "usp_q"), stored("usp_q"), ansi=False), [], "SET_OPTIONS"),
        (live(key("PROCEDURE", "usp_q"), stored("usp_q"), quoted=False), [], "SET_OPTIONS"),
        (live(key("PROCEDURE", "usp_q"), stored("usp_q")), [("SIGNED", "sales", "usp_q", "P ")], "SIGNED"),
        (
            live(key("PROCEDURE", "usp_q"), stored("usp_q")),
            [("NUMBERED", "sales", "usp_q", "P ")],
            "UNSUPPORTED",
        ),
        (
            live(
                key("TRIGGER", "usp_q"),
                "CREATE TRIGGER [sales].[usp_q] ON [sales].[Order] AFTER INSERT AS RETURN;",
                events="INSERT:1:0;UPDATE:0:0",
            ),
            [],
            "TRIGGER_ORDER",
        ),
        (live(key("PROCEDURE", "usp_q"), "/* the header is gone */ SELECT 1;"), [], "HEADER"),
        (
            live(key("PROCEDURE", "usp_q"), "CREATE PROCEDURE [sales].[usp_q] AS SELECT 1 AS\nGO\n"),
            [],
            "HEADER",
        ),
        (live(key("PROCEDURE", "a/b"), stored("a/b")), [], "UNSUPPORTED"),
        # nvarchar holds a lone surrogate; a UTF-8 file cannot
        (live(key("PROCEDURE", "usp_q"), stored("usp_q", "SELECT N'\ud800';")), [], "UNSUPPORTED"),
    ],
)
def test_a_module_that_a_file_cannot_carry_is_quarantined_with_its_code(row, facts, code):
    other = live(key("PROCEDURE", "usp_x"), stored("usp_x"))

    result = export(Db(row, other, export_facts=facts))

    assert list(result.files) == ["schema/procedures/sales.usp_x.sql"]
    (quarantined,) = result.unmanaged
    assert (quarantined.object_key, quarantined.code) == (
        names.object_key(TYPE_KINDS[row[3]], row[1], row[2]),
        code,
    )
    assert f"| {code} |" in result.report_md


def test_two_modules_with_one_file_name_do_not_overwrite_each_other():
    # [a.b].[c] and [a].[b.c] both give the file a.b.c.sql
    first = live(names.object_key("PROCEDURE", "a", "b.c"), "CREATE PROCEDURE [a].[b.c] AS SELECT 1;")
    second = live(names.object_key("PROCEDURE", "a.b", "c"), "CREATE PROCEDURE [a.b].[c] AS SELECT 2;")

    result = export(Db(first, second))

    assert result.files == {
        "schema/procedures/a.b.c.sql": b"CREATE OR ALTER PROCEDURE [a.b].[c] AS SELECT 2;"
    }
    assert [(q.object_key, q.code) for q in result.unmanaged] == [("PROCEDURE:[a].[b.c]", "UNSUPPORTED")]


def test_objects_that_are_no_module_file_at_all_are_reported_as_unsupported():
    facts = [("DATABASE_TRIGGER", None, "tr_ddl_audit", "TR"), ("CLR", "sales", "usp_clr", "PC")]

    result = export(Db(export_facts=facts))

    assert result.files == {}
    assert [(q.object_key, q.code) for q in result.unmanaged] == [
        ("DATABASE_TRIGGER:[tr_ddl_audit]", "UNSUPPORTED"),
        (key("PROCEDURE", "usp_clr"), "UNSUPPORTED"),
    ]


def test_the_list_for_azsqlcd_toml_is_valid_and_keeps_every_entry_of_the_file():
    listed = "PROCEDURE:[SALES].[USP_FIXED]"  # the catalog resolves a name without case
    toml = TOML + f'\n[unmanaged]\nobjects = ["TABLE:[audit].[Log]", "{listed}", "VIEW:[sales].[vGone]"]\n'
    awkward = names.object_key("PROCEDURE", "sales", 'a"b\\c')  # the name cannot be a file name
    db = Db(
        live(awkward, None),
        live(key("PROCEDURE", "usp_fixed"), stored("usp_fixed", "SELECT 'listed-marker';")),
        live(key("PROCEDURE", "usp_other"), stored("usp_other")),
        export_facts=[("DATABASE_TRIGGER", None, "tr_ddl", "TR")],
    )

    result = export(db, toml)

    # a module that [unmanaged] objects lists is left out, as a listed table is: no file, no
    # quarantine entry, and its text is never sent to the source
    assert set(result.files) == {"schema/procedures/sales.usp_other.sql"}
    assert [q.object_key for q in result.unmanaged] == ["DATABASE_TRIGGER:[tr_ddl]", awkward]
    assert not any("listed-marker" in batch for batch in db.batches)
    assert "left out because `[unmanaged] objects` lists them: 1" in result.report_md
    block = result.report_md.split("```toml\n")[1].split("```")[0]
    # every entry of the file stays, also one whose object is gone; a database trigger has no object key
    wanted = [listed, awkward, "TABLE:[audit].[Log]", "VIEW:[sales].[vGone]"]
    assert tomllib.loads(block) == {"unmanaged": {"objects": wanted}}
    assert load_config(TOML + "\n" + block).unmanaged_objects == tuple(wanted)


def test_an_entry_of_the_list_and_a_quarantined_object_of_the_same_name_give_one_line():
    toml = TOML + '\n[unmanaged]\nobjects = ["PROCEDURE:[SALES].[clr_proc]"]\n'
    db = Db(export_facts=[("CLR", "sales", "clr_proc", "PC")])

    result = export(db, toml)

    block = result.report_md.split("```toml\n")[1].split("```")[0]
    assert tomllib.loads(block) == {"unmanaged": {"objects": ["PROCEDURE:[SALES].[clr_proc]"]}}


def test_module_text_is_sent_to_the_source_only_under_parseonly_after_the_canary():
    texts = [stored("usp_a"), stored("usp_b")]
    db = Db(
        *(live(key("PROCEDURE", name), text) for name, text in zip(("usp_a", "usp_b"), texts, strict=True))
    )

    result = export(db)

    files = [data.decode() for data in result.files.values()]
    start = db.index_of("SET PARSEONLY ON;")
    assert db.batches[start:] == [
        "SET PARSEONLY ON;",
        "SELECT 1/0;",
        "SELECT FROM;",
        *files,
        *PARSEONLY_END,
    ]
    assert [batch for batch in db.batches[:start] if DDL_WORDS & words(batch)] == []


def test_a_module_that_does_not_parse_on_the_source_is_quarantined():
    db = Db(
        live(key("PROCEDURE", "usp_bad"), stored("usp_bad", "SELECT FROM")),
        live(key("PROCEDURE", "usp_x"), stored("usp_x")),
    )
    db.fail_on("usp_bad", sql_error("Incorrect syntax near 'SELECT FROM'.", number=102))

    result = export(db)

    assert list(result.files) == ["schema/procedures/sales.usp_x.sql"]
    assert result.unmanaged == (
        Quarantined(
            key("PROCEDURE", "usp_bad"),
            "PARSEONLY",
            "the text does not parse on the source: Incorrect syntax near <redacted>.",
        ),
    )
    assert db.batches[-2:] == PARSEONLY_END  # the session is given back as it was


@pytest.mark.parametrize("canary", ["a statement ran", "a syntax error raised no error"])
def test_the_export_stops_before_any_module_text_when_parseonly_cannot_be_trusted(canary):
    db = Db(live(key("PROCEDURE", "usp_x"), stored("usp_x")))
    if canary == "a statement ran":
        db.fail_on("SELECT 1/0;", sql_error("Divide by zero error encountered.", number=8134))

    with pytest.raises(ToolError) as caught:
        export(db, syntax_errors_raise=canary == "a statement ran")

    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "PARSEONLY_CANARY")
    assert db.sent("PROCEDURE [sales].[usp_x]") == []
    assert db.batches[-2:] == PARSEONLY_END


def test_a_text_that_could_switch_the_check_off_is_never_sent():
    risky = stored("usp_risky", "SET PARSEONLY OFF;\nDROP TABLE [sales].[Order];")
    db = Db(live(key("PROCEDURE", "usp_risky"), risky))

    result = export(db)

    assert result.files == {} and [(q.object_key, q.code) for q in result.unmanaged] == [
        (key("PROCEDURE", "usp_risky"), "PARSEONLY")
    ]
    assert db.sent("PARSEONLY") == []  # no text is left to check, so the session setting is not touched


# ------------------------------------------------------------------ definitions as a real catalog holds them
def live_row(found: Live) -> tuple:
    """The capture_modules row of a live fixture: the type code padded, as the driver gives char(2)."""
    assert len(found.type_code) == 2
    return (
        None,
        found.schema,
        found.name,
        found.type_code,
        found.definition,
        True,
        True,
        found.schema_bound,
        None,
        1,
        None,
        None,
        None,
        None,
    )


def test_definitions_as_a_real_catalog_holds_them_are_exported_and_then_baselined_as_equal():
    # measured on Azure SQL Database: white space before CREATE, CRLF with bare LF, 'CREATE   VIEW'
    # for a module made with CREATE OR ALTER, and sys.objects.type padded to two characters
    rows = [live_row(found) for found in LIVE]
    files = {names.path_for(f.kind, f.schema, f.name): f.file_text.encode() for f in LIVE}

    exported = export(Db(*rows))

    assert exported.unmanaged == () and exported.files == files
    assert exported.report_md.count("verb: CREATE -> CREATE OR ALTER") == len(LIVE)
    assert "name: " not in exported.report_md  # the header edit is the verb, and nothing else
    # A15: a baseline of the same catalog against the exported files finds every module equal
    release_of_the_export = bundle(*((path, data.decode()) for path, data in files.items()))
    baselined = run_baseline(Db(*rows), release_of_the_export, report_only=True)
    assert {(item.object_key, item.status, item.source_sha256) for item in baselined.items} == {
        (names.object_key(f.kind, f.schema, f.name), "equal", checksum(f.file_text.encode())) for f in LIVE
    }
    assert len(baselined.items) == len(LIVE)
    assert baselined.live_files == {} and baselined.overwrite_modules == ()


def test_a_padded_type_code_gives_the_kind_of_the_module_and_of_an_export_fact():
    found = ORDER_TOTALS
    facts = [("INDEXED_VIEW", found.schema, found.name, "V "), ("SIGNED", "sales", "Order", "U ")]
    db = Db(live_row(found), export_facts=facts, user_objects=[("sales", "Order", "U ")])

    result = export(db)

    key_ = names.object_key("VIEW", found.schema, found.name)
    assert result.files == {} and [(q.object_key, q.code) for q in result.unmanaged] == [
        (key_, "UNSUPPORTED")
    ]
    capture = catalog.capture_modules(Db(live_row(found)), None)[key_]
    assert (capture["kind"], capture["type"]) == ("VIEW", "V")  # the capture holds the code without padding


# ------------------------------------------------------------------ export: modules and tables as one result
def export_all(db: Db, toml: str = TOML) -> onboard.Export:
    db.fail_on("SELECT FROM;", sql_error(SYNTAX_ERROR, number=156))  # the engine under SET PARSEONLY ON
    return onboard.export(db, load_config(toml))


def test_the_export_command_gets_modules_and_tables_as_one_result_with_one_list_for_azsqlcd_toml():
    toml = TOML + '\n[unmanaged]\nobjects = ["TABLE:[audit].[Log]", "PROCEDURE:[sales].[usp_fixed]"]\n'
    catalog_rows = rows_from_model(MODEL)
    only(catalog_rows, "tabulars", name="Customer")["ledger_type"] = 2  # outside the model
    encrypted = key("PROCEDURE", "usp_hidden")
    db = with_tables(Db(live(USP_X, stored("usp_x")), live(encrypted, None)), catalog_rows)

    result = export_all(db, toml)

    assert set(result.files) == {"schema/procedures/sales.usp_x.sql", "schema/schemas/sales.sql"}
    assert result.files["schema/schemas/sales.sql"] == emit.emit_object_file(SALES).encode()
    # the module with no text, the table that the model cannot hold, and the table that needs it
    assert sorted((q.object_key, q.code) for q in result.unmanaged) == [
        (encrypted, "ENCRYPTED"),
        (CUSTOMER_KEY, "UNSUPPORTED"),
        (ORDER_KEY, "DEPENDS_ON_UNMANAGED"),
    ]
    assert result.report_md.count("## List for azsqlcd.toml") == 1
    # RO-5: one block for azsqlcd.toml in the whole report, and it holds both parts
    assert result.report_md.count("```toml") == 1 and result.report_md.count("[unmanaged]\n") == 1
    whole_list = result.report_md.split("## List for azsqlcd.toml")[1].split("```toml\n")[1].split("```")[0]
    assert tomllib.loads(whole_list) == {
        "unmanaged": {
            "objects": [
                "PROCEDURE:[sales].[usp_fixed]",  # an entry of the file stays, whatever the database holds
                encrypted,
                "TABLE:[audit].[Log]",
                CUSTOMER_KEY,
                ORDER_KEY,
            ]
        }
    }
    assert "## Table-class objects" in result.report_md and "## Header edits" in result.report_md
    assert result.snapshot["captures"] == {SCHEMA_KEY: {"kind": "SCHEMA", "owner": "dbo"}}
    assert result.rename_constraints_sql == ""
    # module text goes to the source only between SET PARSEONLY ON and OFF; all else is a read
    on, off = db.index_of("SET PARSEONLY ON;"), db.index_of("SET PARSEONLY OFF;")
    assert all(batch.startswith("/* azsqlcd:") for batch in db.batches[:on] + db.batches[off + 1 :])
    # OM-11: the tables are read before the session is ever set to parse only
    assert db.sent("azsqlcd:read_tabulars") != [] and db.index_of("azsqlcd:read_tabulars") < on
    assert db.batches[off:] == PARSEONLY_END  # no catalog read after the module check


def test_the_export_gives_a_temporal_table_its_file_and_names_its_history_table_as_owned_by_the_engine():
    result = export_all(with_tables(Db(), price_rows()))

    assert set(result.files) == {
        "schema/schemas/sales.sql",
        "schema/tables/sales.Price.sql",
        "schema/tables/sales.Quote.sql",
    }
    price_file = result.files["schema/tables/sales.Price.sql"].decode()
    assert "WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [sales].[Price_History]));" in price_file
    # the history table: no file, not unmanaged, not in the list for azsqlcd.toml, named in the report
    assert result.history_tables == ((PRICE_HISTORY_KEY, PRICE_KEY),)
    assert result.unmanaged == ()
    assert f"- `{PRICE_HISTORY_KEY}`: history table of `{PRICE_KEY}`, owned by the engine" in result.report_md
    whole_list = result.report_md.split("## List for azsqlcd.toml")[1]
    assert "Price_History" not in whole_list
    assert export_all(with_tables(Db())).history_tables == ()  # a database with no temporal table


def test_the_snapshot_and_the_rename_script_of_the_export_are_those_of_the_table_part():
    catalog_rows = rows_from_model(MODEL)
    default = only(catalog_rows, "defaults", name="DF_Order_Status")
    default.update(name="DF__Order__Status__1A2B", is_system_named=1)

    result = export_all(with_tables(Db(), catalog_rows))

    table_part = tables.export_tables(session_for(catalog_rows), CONFIG)
    assert result.snapshot == table_part.snapshot and set(result.snapshot["captures"]) == set(MODEL)
    assert result.rename_constraints_sql == table_part.rename_constraints_sql != ""
    assert result.files == table_part.files and table_part.report_md in result.report_md


def test_the_export_stops_when_a_table_file_would_hold_a_secret_literal():
    secret = DefaultConstraint("DF_Customer_Id", Expression.from_sql("(len('password = ''hunter2'''))"))
    column = dataclasses.replace(CUSTOMER.columns[0], default=secret)
    customer = dataclasses.replace(CUSTOMER, columns=(column,))
    db = with_tables(Db(live(USP_X, stored("usp_x"))), Model([SALES, ORDER, customer]))

    with pytest.raises(ToolError) as caught:
        export_all(db)

    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "SECRET_LITERAL")
    assert caught.value.detail == {"paths": ["schema/tables/sales.Customer.sql"]}
    assert "hunter2" not in str(caught.value)


# ------------------------------------------------------------------ indexed views (OM-01)
INDEXED = key("VIEW", "vTotals")
INDEXED_BODY = "SELECT [CustomerId], COUNT_BIG(*) AS [n] FROM [sales].[Order] GROUP BY [CustomerId];\n"
INDEXED_LIVE = "CREATE VIEW [sales].[vTotals] WITH SCHEMABINDING AS\n" + INDEXED_BODY
INDEXED_FILE = (
    "schema/views/sales.vTotals.sql",
    "CREATE OR ALTER VIEW [sales].[vTotals] WITH SCHEMABINDING AS\n" + INDEXED_BODY,
)


def test_an_indexed_view_is_quarantined_at_export():
    # ALTER VIEW drops every index of the view, so a module file must never exist for it
    db = Db(
        live(INDEXED, INDEXED_LIVE),
        live(USP_X, stored("usp_x")),
        export_facts=[("INDEXED_VIEW", "sales", "vTotals", "V ")],
    )

    result = export(db)

    assert list(result.files) == ["schema/procedures/sales.usp_x.sql"]
    (quarantined,) = result.unmanaged
    assert (quarantined.object_key, quarantined.code) == (INDEXED, "UNSUPPORTED")
    assert "indexed view" in quarantined.reason
    assert db.sent("COUNT_BIG") == []  # its text is not sent to be parsed either


def test_a_baseline_does_not_record_an_indexed_view_and_drift_reports_it_as_unmanaged():
    rows = [live(INDEXED, INDEXED_LIVE), live(USP_X, stored("usp_x"))]
    release_files = bundle(INDEXED_FILE, proc("usp_x"))
    db = Db(*rows, indexed_views=[("sales", "vTotals")])

    result = run_baseline(db, release_files)

    # the text of the view is the text of its file; it is still not managed
    assert status_of(result) == {INDEXED: "only here", USP_X: "equal"}
    (view,) = [item for item in result.items if item.object_key == INDEXED]
    assert "indexed view" in view.note and view.source_sha256 is None
    assert result.overwrite_modules == () and result.live_files == {}
    assert [OBJECT_KEY.findall(batch)[0] for batch in db.sent("UPDATE [azsqlcd].[object]")] == [USP_X]

    after = recorded(
        steps=[BASELINE_STEP], objects={USP_X: managed(rows[1], checksum(proc("usp_x")[1].encode()))}
    )
    found = drift(
        Db(*rows, st=after, indexed_views=[("sales", "vTotals")]), release_files, CONFIG, "dev", "sales-dev"
    )
    assert found.items == (DriftItem(INDEXED, "exists", None, None, "unmanaged"),)
    assert not found.has_drift


def test_a_view_without_an_index_is_baselined_as_any_module():
    db = Db(live(INDEXED, INDEXED_LIVE))

    assert status_of(run_baseline(db, bundle(INDEXED_FILE))) == {INDEXED: "equal"}


def test_a_managed_view_that_got_an_index_is_drift():
    # a deploy of its file would drop the index; the runner refuses that, and drift says why
    row = live(INDEXED, INDEXED_LIVE)
    db = Db(row, st=recorded(objects={INDEXED: managed(row)}), indexed_views=[("SALES", "VTOTALS")])

    result = drift(db, bundle(), CONFIG, "dev", "sales-dev")

    assert [(item.object_key, item.property, item.cls) for item in result.items] == [
        (INDEXED, "has_index", "managed")
    ]
    assert result.has_drift and result.items[0].stored_hash != result.items[0].live_hash


# ------------------------------------------------------------------ equal text is not an equal module (OM-05)
@pytest.mark.parametrize(
    ("options", "word"), [({"ansi": False}, "ANSI_NULLS"), ({"quoted": False}, "QUOTED_IDENTIFIER")]
)
def test_a_module_with_the_text_of_its_file_and_legacy_set_options_is_not_baselined_as_equal(options, word):
    same = proc("usp_same")
    object_key = key("PROCEDURE", "usp_same")
    row = live(object_key, stored("usp_same"), **options)

    result = run_baseline(Db(row), bundle(same), report_only=True)

    (item,) = result.items
    assert (item.status, item.source_sha256) == ("differs", None)
    assert word in item.note and f"{word} OFF" in result.report_md
    # the text is the text of the file: no live file to compare, and still an overwrite to acknowledge
    assert result.live_files == {} and result.overwrite_modules == (object_key,)
    error = refusal(Db(row), bundle(same))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "BASELINE_ACK_REQUIRED")
    # acknowledged: recorded with no source checksum, so the plan lists OVERWRITE_MODULE
    db = Db(row)
    run_baseline(db, bundle(same, ack=[object_key]))
    (written,) = db.sent("UPDATE [azsqlcd].[object]")
    assert "[source_sha256] = NULL" in written


TRIGGER_FILE = (
    "schema/triggers/sales.trg.sql",
    "CREATE OR ALTER TRIGGER [sales].[trg] ON [sales].[Order] AFTER INSERT AS\nSELECT 1;\n",
)
TRIGGER_LIVE = "CREATE TRIGGER [sales].[trg] ON [sales].[Order] AFTER INSERT AS\nSELECT 1;\n"


def test_a_disabled_or_ordered_trigger_with_the_text_of_its_file_is_not_baselined_as_equal():
    trigger = key("TRIGGER", "trg")
    enabled = live(trigger, TRIGGER_LIVE)
    disabled = (*enabled[:12], 1, enabled[13])
    ordered = live(trigger, TRIGGER_LIVE, events="INSERT:1:0")

    def baselined(row: tuple) -> BaselineItem:
        (item,) = run_baseline(Db(row), bundle(TRIGGER_FILE), report_only=True).items
        return item

    assert baselined(enabled) == BaselineItem(trigger, "equal", checksum(TRIGGER_FILE[1].encode()))
    assert baselined(disabled).status == "differs" and "disabled" in baselined(disabled).note
    assert baselined(ordered).status == "differs" and "order" in baselined(ordered).note


# ------------------------------------------------------------------ engine-named constraints (OM-06)
def engine_named_primary_key() -> CatalogRows:
    here = rows_from_model(MODEL)
    primary = only(here, "indexes", name="PK_Customer")
    primary.update(name="PK__Customer__3214EC07A1B2C3D4", constraint_is_system_named=1)
    return here


RENAME_PRIMARY_KEY = (
    "EXEC sys.sp_rename N'[sales].[PK__Customer__3214EC07A1B2C3D4]', N'PK_Customer', N'OBJECT';\nGO\n"
)


def test_a_write_baseline_refuses_engine_named_constraints_and_gives_the_rename_script():
    b, hooks = a_table_release()
    db = with_tables(Db(live(USP_X, stored("usp_x"))), engine_named_primary_key())

    error = refusal(db, b, table_hooks=hooks)

    # recorded under the name of the file, the constraint would be a name that the database does not have
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "CONSTRAINT_NAMES")
    assert error.detail["rename_constraints_sql"] == RENAME_PRIMARY_KEY
    assert error.detail["constraints"] == [
        f"{CUSTOMER_KEY} PRIMARY KEY [PK__Customer__3214EC07A1B2C3D4] -> [PK_Customer]"
    ]
    assert db.sent("INSERT INTO [azsqlcd]") == [] and db.sent("UPDATE [azsqlcd]") == []
    assert db.sent("BEGIN TRANSACTION") == []


def test_a_baseline_report_names_the_engine_named_constraints_that_a_write_baseline_refuses():
    b, hooks = a_table_release(reference_snapshot())
    db = with_tables(Db(live(USP_X, stored("usp_x"))), engine_named_primary_key())

    result = run_baseline(db, b, report_only=True, table_hooks=hooks)

    assert result.rename_constraints_sql == RENAME_PRIMARY_KEY
    assert "CONSTRAINT_NAMES" in result.report_md and "rename-constraints.sql" in result.report_md
    assert (
        f"`{CUSTOMER_KEY} PRIMARY KEY [PK__Customer__3214EC07A1B2C3D4] -> [PK_Customer]`" in result.report_md
    )
    # a database whose constraints have the names of the files has no such section
    clean = run_baseline(
        with_tables(Db(live(USP_X, stored("usp_x")))), b, report_only=True, table_hooks=hooks
    )
    assert "CONSTRAINT_NAMES" not in clean.report_md and clean.rename_constraints_sql == ""


def test_an_engine_named_constraint_that_the_model_does_not_hold_does_not_stop_a_baseline():
    # an unmanaged sub-object: it is left out of the comparison and of the capture, and needs no name
    here = rows_from_model(MODEL)
    head = only(here, "tabulars", name="Customer")
    here["checks"].append(
        {
            "parent_object_id": head["object_id"],
            "name": "CK__Customer__1A2B3C4D",
            "definition": "([CustomerId]>(0))",
            "is_system_named": 1,
            "is_disabled": 0,
            "is_not_trusted": 0,
            "is_not_for_replication": 0,
        }
    )
    b, hooks = a_table_release()
    db = with_tables(Db(live(USP_X, stored("usp_x"))), here)

    result = run_baseline(db, b, table_hooks=hooks)

    assert result.report is not None and result.report.exit_code == 0


# ------------------------------------------------------------------ export writes what lint accepts (OM-08)
def lint_errors(files: dict[str, bytes]) -> list[tuple[str, str]]:
    found = lint.lint_repo({release.CONFIG_PATH: TOML.encode(), **files})
    return [(finding.code, finding.path) for finding in found if finding.severity == "error"]


@pytest.mark.parametrize(
    ("definition", "finding"),
    [
        ("CREATE PROCEDURE sales.p1 AS\nSET NOEXEC ON;\nSELECT 1;\nSET NOEXEC OFF;\n", "FORBIDDEN_TOKEN"),
        ("-- azsqlcd:data\nCREATE PROCEDURE sales.p1 AS\nSELECT 1;\n", "DIR001"),
    ],
)
def test_every_module_file_that_export_writes_passes_lint_or_is_quarantined(definition, finding):
    db = Db(live(key("PROCEDURE", "p1"), definition), live(USP_X, stored("usp_x")))

    result = export(db)

    assert list(result.files) == ["schema/procedures/sales.usp_x.sql"]
    (quarantined,) = result.unmanaged
    assert (quarantined.object_key, quarantined.code) == (key("PROCEDURE", "p1"), "LINT")
    assert finding in quarantined.reason and "| LINT |" in result.report_md
    assert lint_errors(result.files) == []
    assert db.sent("[sales].[p1]") == []  # a text that is no file is not sent to be parsed


def test_a_view_and_function_cycle_of_the_live_modules_does_not_reach_the_files():
    # the one-part name Active in the function is read as the view: lint ORD004 would stop every build
    view = live(
        names.object_key("VIEW", "dbo", "Active"), "CREATE VIEW dbo.Active AS SELECT dbo.fnActive() AS n"
    )
    function = (
        None,
        "dbo",
        "fnActive",
        "FN",
        "CREATE FUNCTION dbo.fnActive() RETURNS int AS BEGIN "
        "RETURN (SELECT COUNT(*) FROM dbo.t WHERE Active = 1) END",
        *view[5:],
    )

    result = export(Db(view, function))

    assert len(result.files) == 1 and [q.code for q in result.unmanaged] == ["LINT"]
    assert "ORD004" in result.unmanaged[0].reason
    assert lint_errors(result.files) == []


# ------------------------------------------------------------------ export order (OM-11)
class StickyParseOnly(Db):
    """A session on which SET PARSEONLY OFF, sent under PARSEONLY ON, does not give the setting back:
    every later batch is parsed and not run, so a catalog read gives no result set."""

    parse_only = False

    def execute(self, batch: str) -> ResultSets:
        if batch.strip() == "SET PARSEONLY ON;":
            self.parse_only = True
        if self.parse_only and batch.lstrip().startswith("/* azsqlcd:read_"):
            self.batches.append(batch)
            return []
        return super().execute(batch)


def test_export_does_not_lose_the_tables_when_the_session_still_parses_only_after_the_module_check():
    db = with_tables(StickyParseOnly(live(USP_X, stored("usp_x"))))

    result = export_all(db)

    assert "schema/tables/sales.Order.sql" in result.files
    assert set(result.snapshot["captures"]) == set(MODEL)
    assert db.sent("azsqlcd:read_tabulars") != []
