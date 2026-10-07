"""The resolve actions of the runner (A16). Human recovery: each action takes the lock, writes a run
row with the command resolve, makes one change of the recorded state and writes a resolve step.

The database is the Db of test_runner: a FakeSession with the modules that exist and the fence.
"""

import dataclasses
import re
from collections.abc import Callable
from typing import Any

import pytest

from azsqlcd import chain, names, plan, state
from azsqlcd.errors import Exit, ToolError
from azsqlcd.release import Bundle
from azsqlcd.runner import (
    Audit,
    Report,
    RunError,
    StateChange,
    accept_drift,
    adopt_module,
    clear_run,
    mark_applied,
    mark_not_applied,
    rebind_environment,
    state_run,
)
from azsqlcd.sqlerrors import sql_error
from azsqlcd.state import Meta, State, StepRow
from support.fake_session import FakeSession
from unit.test_runner import (
    ADD_C,
    ADD_D,
    BUILD_INDEX,
    CONFIG,
    M1,
    M2,
    NOW,
    RECORDED_COMMIT,
    RUN_ID,
    TABLE,
    TABLE_ROW,
    TOOL_DIGEST,
    Db,
    Hooks,
    Tokens,
    bundle,
    capture,
    chain_sha,
    key,
    live_row,
    mig,
    proc,
    recorded,
    row,
    run_row,
    session_options,
)

REASON = "checked by hand, ticket 4711"
OTHER = key("PROCEDURE", "usp_other")
LEGACY = key("PROCEDURE", "usp_legacy")
HOT_FIX = "CREATE PROCEDURE [sales].[usp_other] AS SELECT 'hot fix';"
WRITE = re.compile(r"(^|[; ])(INSERT INTO|UPDATE) \[azsqlcd\]")


def resolve(
    action: Callable[..., Report],
    b: Bundle,
    db: FakeSession,
    *subject: Any,
    env: str = "dev",
    confirm: str = "sales",
    reason: str = REASON,
    opened: list | None = None,
    **options: Any,
) -> Report:
    def session_factory() -> FakeSession:
        if opened is not None:
            opened.append(db)
        return db

    return action(
        b,
        CONFIG,
        env,
        f"sales-{env}",
        *subject,
        confirm_database=confirm,
        reason=reason,
        session_factory=session_factory,
        token_provider=Tokens(),
        audit=Audit(triggering_actor="dba-1"),
        tool_version="0.1.0",
        tool_digest=TOOL_DIGEST,
        now=lambda: NOW,
        **options,
    )


def refusal(*args: Any, **kwargs: Any) -> RunError:
    with pytest.raises(RunError) as caught:
        resolve(*args, **kwargs)
    return caught.value


def nontx_step(b: Bundle, status: str) -> State:
    """A database whose run 9 died in the nontx migration M1."""
    step = StepRow(3, 9, "nontx", M1, chain_sha(b, M1), status, None)
    return recorded(steps=[step], open_runs=[run_row(9, "running")])


def a_drifted_module() -> tuple[Bundle, State, Db]:
    path, text = proc("usp_other")
    b = bundle(modules=[(path, text)])
    st = recorded(objects={OTHER: row(OTHER, text)})
    db = Db(b, st)
    db.catalog[OTHER] = live_row(OTHER, HOT_FIX)
    return b, st, db


def an_unmanaged_module() -> tuple[Bundle, State, Db]:
    b = bundle(modules=[proc("usp_legacy")])
    st = recorded()
    db = Db(b, st)
    db.catalog[LEGACY] = live_row(LEGACY, "CREATE PROCEDURE [sales].[usp_legacy] AS SELECT 0;")
    return b, st, db


def a_pending_migration() -> tuple[Bundle, State, Db]:
    b = bundle(mig(M1, ADD_C), mig(M2, ADD_D))
    st = recorded(objects={TABLE: TABLE_ROW})
    return b, st, Db(b, st)


# ------------------------------------------------------------------ every action
def marks_applied() -> tuple:
    b = bundle(mig(M1, BUILD_INDEX, mode="nontx"))
    db = Db(b, nontx_step(b, "started"))
    db.respond("azsqlcd:sub_object", [[(1, 0)]])  # the index exists and no build of it is open
    change = "UPDATE [azsqlcd].[step] SET [status] = N'ok'"
    step_note = f"mark-applied: {REASON}"
    return mark_applied, b, db, (M1,), change, step_note, f"mark-applied {M1}"


def marks_not_applied() -> tuple:
    b = bundle(mig(M1, BUILD_INDEX, mode="nontx"))
    db = Db(b, nontx_step(b, "unknown"))
    db.respond("azsqlcd:sub_object", [[(0, 0)]])
    change = "UPDATE [azsqlcd].[step] SET [status] = N'not_applied'"
    return mark_not_applied, b, db, (M1,), change, f"mark-not-applied: {REASON}", f"mark-not-applied {M1}"


def accepts_drift() -> tuple:
    b, _, db = a_drifted_module()
    step_note = f"accept-drift: {REASON}"
    return accept_drift, b, db, (OTHER,), "UPDATE [azsqlcd].[object]", step_note, f"accept-drift {OTHER}"


def adopts_a_module() -> tuple:
    b, _, db = an_unmanaged_module()
    step_note = f"adopt-module: {REASON}"
    return (
        adopt_module,
        b,
        db,
        (LEGACY,),
        "INSERT INTO [azsqlcd].[object]",
        step_note,
        f"adopt-module {LEGACY}",
    )


def clears_a_run() -> tuple:
    b = bundle(mig(M1))
    db = Db(b, recorded(open_runs=[run_row(9, "unknown")]))
    # the resolve step is the change
    return clear_run, b, db, (9,), "azsqlcd:guard", plan.clear_run_note(9), "clear-run 9"


def rebinds_the_environment() -> tuple:
    b = bundle(mig(M1))
    db = Db(b, recorded(env="prod"))  # a copy of prod, restored on the dev server
    db.options = session_options(CONFIG.env["dev"].lock_timeout_ms)
    change = "UPDATE [azsqlcd].[meta] SET [environment] = N'dev'"
    return rebind_environment, b, db, (), change, f"rebind-environment: {REASON}", "rebind-environment dev"


ACTIONS = [
    marks_applied,
    marks_not_applied,
    accepts_drift,
    adopts_a_module,
    clears_a_run,
    rebinds_the_environment,
]


@pytest.mark.parametrize("scenario", ACTIONS)
def test_each_resolve_action_writes_a_run_row_and_a_resolve_step_under_the_lock(scenario):
    action, b, db, subject, change, step_note, run_note = scenario()
    report = resolve(action, b, db, *subject)
    (insert,) = db.sent("INSERT INTO [azsqlcd].[run]")
    assert insert.startswith(
        "INSERT INTO [azsqlcd].[run] ([command], [status], [segments_committed], [release_seq]"
    )
    assert f"N'{run_note}: {REASON}'" in insert and "N'dba-1'" in insert
    (step,) = db.sent("N'resolve', NULL, NULL, N'ok'")
    assert step.startswith("INSERT INTO [azsqlcd].[step] ([run_id], [kind]") and f"VALUES ({RUN_ID}, " in step
    assert f"N'{step_note}'" in step
    # the change and the resolve step are one transaction; the run row is closed after it
    db.assert_order(
        "sp_getapplock",
        "INSERT INTO [azsqlcd].[run]",
        "BEGIN TRANSACTION",
        change,
        "N'resolve', NULL, NULL, N'ok'",
        "COMMIT TRANSACTION;",
        "UPDATE [azsqlcd].[run] SET [status] = N'ok'",
        "sp_releaseapplock",
    )
    assert (report.command, report.exit_code, report.reason_code, report.run_id) == (
        "resolve",
        0,
        "OK",
        RUN_ID,
    )
    assert db.trancount == 0 and db.closed and not db.applock_held and db.fence == 1


@pytest.mark.parametrize("scenario", ACTIONS)
def test_each_resolve_action_refuses_a_wrong_confirm_database(scenario):
    for wrong in ("sales_prod", "Sales", ""):
        action, b, db, subject, *_ = scenario()
        error = refusal(action, b, db, *subject, confirm=wrong)
        assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "CONFIRM_MISMATCH")
        assert error.detail == {"db_name": "sales", "confirmed": wrong}
        # refused before the lock: nothing can have changed
        assert db.sent("sp_getapplock") == [] and db.sent(WRITE) == [] and db.closed


@pytest.mark.parametrize("scenario", ACTIONS)
def test_each_resolve_action_needs_a_reason(scenario):
    action, b, db, subject, *_ = scenario()
    opened: list = []
    error = refusal(action, b, db, *subject, reason="  ", opened=opened)
    assert (error.exit_code, error.reason_code, opened) == (Exit.REFUSED, "REASON_REQUIRED", [])


def test_a_resolve_run_does_not_move_the_recorded_release():
    # the bundle is release 7 of commit c...; the database records release 6 of commit a...
    action, b, db, subject, *_ = clears_a_run()
    assert (b.manifest.release_seq, b.manifest.commit[0]) == (7, "c")
    resolve(action, b, db, *subject)
    (insert,) = db.sent("INSERT INTO [azsqlcd].[run]")
    # the planner reads the release of the newest ok run: a resolve run copies it and changes nothing
    assert f"VALUES (N'resolve', N'running', 0, 6, N'{RECORDED_COMMIT}'," in insert


def test_a_resolve_run_on_a_database_with_no_ok_run_records_release_zero():
    b = bundle(mig(M1))
    db = Db(b, recorded(seq=0, open_runs=[run_row(9, "unknown")]))
    resolve(clear_run, b, db, 9)
    assert (
        f"VALUES (N'resolve', N'running', 0, 0, N'{'0' * 40}'," in db.sent("INSERT INTO [azsqlcd].[run]")[0]
    )


def test_resolve_waits_for_the_lock_and_exits_25_when_another_run_holds_it():
    action, b, db, subject, *_ = clears_a_run()
    db.applock_result = -1
    error = refusal(action, b, db, *subject)
    assert (error.exit_code, error.reason_code) == (Exit.LOCKED, "LOCK_NOT_GRANTED")
    assert db.sent(WRITE) == []


def test_a_resolve_action_on_a_database_of_another_environment_is_refused():
    b = bundle(mig(M1))
    db = Db(b, recorded(env="prod", open_runs=[run_row(9, "unknown")]))
    db.options = session_options(CONFIG.env["dev"].lock_timeout_ms)
    error = refusal(clear_run, b, db, 9)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "FENCE_META_MISMATCH")
    assert db.sent(WRITE) == []


def test_a_change_that_fails_is_rolled_back_and_the_resolve_run_is_failed():
    action, b, db, subject, change, *_ = accepts_drift()
    db.fail_on("N'resolve', NULL, NULL, N'ok'", sql_error("String or binary data would be truncated."))
    error = refusal(action, b, db, *subject)
    assert (error.exit_code, error.reason_code) == (Exit.FAILED_ROLLED_BACK, "BATCH_FAILED")
    db.assert_order(change, "ROLLBACK TRANSACTION", "azsqlcd:read_fence", "[status] = N'failed'")
    assert db.sent("COMMIT TRANSACTION;") == [] and db.fence == 0


# ------------------------------------------------------------------ the frame of a state-only run
NOTE_ROW = "UPDATE [azsqlcd].[meta] SET [environment] = N'dev'"


def a_change(session: Any, recorded_state: State) -> StateChange:
    return StateChange(
        note="what and why",
        step="the-change",
        summary="the change of the test",
        writes=lambda run_id: [
            state.set_meta_environment("dev"),
            state.insert_step(run_id=run_id, kind="baseline", status="ok", note=f"by run {run_id}"),
        ],
    )


def frame(command: str = "baseline", prepare: Any = a_change, **options: Any) -> Report:
    b = options.pop("bundle", None) or bundle(mig(M1))
    options = {"confirm_database": "sales", "tool_version": "0.1.0", "tool_digest": TOOL_DIGEST} | options
    return state_run(command, b, CONFIG, "dev", "sales-dev", prepare, **options)


def test_a_state_only_run_on_a_given_session_runs_the_whole_frame_and_closes_the_session():
    """onboard.baseline has the session of its caller. The frame mints no token and opens nothing."""
    b = bundle(mig(M1))
    db = Db(b, recorded(seq=6))
    seen: list[tuple] = []

    def prepare(session: Any, recorded_state: State) -> StateChange:
        seen.append((session is db, db.applock_held, recorded_state.recorded_release_seq, db.trancount))
        return a_change(session, recorded_state)

    report = frame(prepare=prepare, session=db, bundle=b)
    assert seen == [(True, True, 6, 0)]  # under the lock, with the recorded state, before the transaction
    db.assert_order(
        "SET XACT_ABORT ON",
        "azsqlcd:session_options",
        "azsqlcd:fence_facts",
        "sp_getapplock",
        "azsqlcd:read_state.tables",
        "INSERT INTO [azsqlcd].[run]",
        "BEGIN TRANSACTION; UPDATE [azsqlcd].[run] SET [segments_committed] = [segments_committed] + 1",
        "azsqlcd:guard",
        NOTE_ROW,
        f"N'by run {RUN_ID}'",
        "azsqlcd:guard",
        "COMMIT TRANSACTION;",
        "azsqlcd:trancount",
        "UPDATE [azsqlcd].[run] SET [status] = N'ok'",
        "sp_releaseapplock",
    )
    (insert,) = db.sent("INSERT INTO [azsqlcd].[run]")
    # the run row copies the recorded release and holds the note of the change
    assert f"VALUES (N'baseline', N'running', 0, 6, N'{RECORDED_COMMIT}'," in insert
    assert "N'what and why'" in insert
    assert (report.command, report.exit_code, report.run_id) == ("baseline", 0, RUN_ID)
    assert report.message == f"the change of the test: recorded as run {RUN_ID}"
    assert db.closed and db.fence == 1


@pytest.mark.parametrize("answer", [(0, 0, 1001), (2, 1, 1001), (1, -1, 1001)])
def test_a_state_only_run_whose_begin_batch_leaves_no_sound_transaction_writes_nothing(answer):
    """A5 (TQ-02): the guard directly after BEGIN TRANSACTION. Without it the state writes of a
    baseline or a resolve would run in autocommit, with nothing to roll back."""
    b = bundle(mig(M1))
    db = Db(b, recorded(seq=6))
    db.respond("azsqlcd:guard", [[answer]])
    with pytest.raises(RunError) as caught:
        frame(session=db, bundle=b)
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.UNKNOWN, "GUARD_FAILED")
    assert caught.value.report.failed_step == "begin"
    assert db.sent(NOTE_ROW) == [] and db.sent("INSERT INTO [azsqlcd].[step]") == []
    assert db.sent("COMMIT TRANSACTION;") == [] and len(db.sent("azsqlcd:guard")) == 1


@pytest.mark.parametrize(
    "ways",
    [
        {},  # no session and no way to open one
        {"session_factory": lambda: FakeSession()},  # a factory needs the token provider
        {"token_provider": Tokens()},
        {"session": FakeSession(), "session_factory": lambda: FakeSession(), "token_provider": Tokens()},
        {"session": FakeSession(), "token_provider": Tokens()},
    ],
)
def test_a_state_only_run_takes_a_session_or_the_way_to_open_one_and_never_both(ways):
    with pytest.raises(ValueError):
        frame(**ways)


@pytest.mark.parametrize("command", ["deploy", "init", ""])
def test_a_state_only_run_is_a_resolve_or_a_baseline_and_never_a_deploy(command):
    db = FakeSession()
    with pytest.raises(ValueError):
        frame(command, session=db)
    assert db.batches == []


def test_the_precheck_of_a_state_only_run_refuses_before_any_batch_and_closes_a_given_session():
    b = bundle(mig(M1))
    db = Db(b, recorded())

    def precheck() -> None:
        raise ToolError(Exit.REFUSED, "REASON_REQUIRED", "no reason")

    with pytest.raises(RunError) as caught:
        frame(session=db, bundle=b, precheck=precheck)
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "REASON_REQUIRED")
    assert (caught.value.report.command, caught.value.report.run_id) == ("baseline", None)
    assert db.batches == [] and db.closed


def test_dead_runs_are_reconciled_in_a_state_only_run_only_when_the_caller_asks():
    """A baseline reconciles as a deploy does. A resolve action does not: mark-applied must find
    the step of a dead run as it was left, not refused by its own reconcile."""
    b = bundle(mig(M1))
    dead = recorded(open_runs=[run_row(9, "running")])
    asked, not_asked = Db(b, dead), Db(b, dead)
    frame(session=asked, bundle=b, reconcile=True)
    frame(session=not_asked, bundle=b)
    (closed,) = asked.sent("N'reconciled'")
    assert "SET [status] = N'failed'" in closed and "WHERE [run_id] = 9" in closed
    asked.assert_order("sp_getapplock", "N'reconciled'", "INSERT INTO [azsqlcd].[run]")
    assert not_asked.sent("N'reconciled'") == []


def test_a_dead_run_in_a_nontx_step_stops_a_state_only_run_that_reconciles():
    b = bundle(mig(M1, BUILD_INDEX, mode="nontx"))
    db = Db(b, nontx_step(b, "started"))
    with pytest.raises(RunError) as caught:
        frame(session=db, bundle=b, reconcile=True)
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "STEP_UNRESOLVED")
    assert db.sent("INSERT INTO [azsqlcd].[run]") == [] and db.sent(NOTE_ROW) == []


def test_what_prepare_refuses_is_refused_with_no_run_row_and_a_failed_read_back_is_a_refusal_there():
    b = bundle(mig(M1))
    for raised, exit_code in ((Exit.REFUSED, Exit.REFUSED), (Exit.FAILED_ROLLED_BACK, Exit.REFUSED)):
        db = Db(b, recorded())

        def prepare(session: Any, recorded_state: State, raised: Exit = raised) -> StateChange:
            raise ToolError(raised, "READBACK_MISMATCH", "the catalog differs")

        with pytest.raises(RunError) as caught:
            frame(prepare=prepare, session=db, bundle=b)
        # nothing was executed, so nothing was rolled back: never exit 21 from here
        assert (caught.value.exit_code, caught.value.reason_code) == (exit_code, "READBACK_MISMATCH")
        assert db.sent(WRITE) == [] and db.sent("BEGIN TRANSACTION") == [] and db.closed


def test_a_state_only_run_refuses_another_database_than_the_one_that_was_confirmed_before_the_lock():
    b = bundle(mig(M1))
    db = Db(b, recorded())
    with pytest.raises(RunError) as caught:
        frame(session=db, bundle=b, confirm_database="Sales")  # the name is confirmed exactly
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.REFUSED, "CONFIRM_MISMATCH")
    assert db.sent("sp_getapplock") == [] and db.sent(WRITE) == []


def test_a_write_of_a_state_only_run_that_fails_is_rolled_back_and_proven():
    b = bundle(mig(M1))
    db = Db(b, recorded())
    db.fail_on(NOTE_ROW, sql_error("Lock request time out period exceeded."))
    with pytest.raises(RunError) as caught:
        frame(session=db, bundle=b)
    assert (caught.value.exit_code, caught.value.reason_code) == (Exit.RETRY_SAFE, "LOCK_TIMEOUT")
    assert caught.value.report.failed_step == "the-change"
    db.assert_order(NOTE_ROW, "ROLLBACK TRANSACTION", "azsqlcd:trancount", "azsqlcd:read_fence")
    assert db.sent("COMMIT TRANSACTION;") == [] and db.fence == 0


# ------------------------------------------------------------------ mark-applied
def test_mark_applied_closes_a_nontx_step_whose_outcome_was_not_known():
    for status in ("started", "unknown"):
        b = bundle(mig(M1, BUILD_INDEX, mode="nontx"))
        db = Db(b, nontx_step(b, status))
        db.respond("azsqlcd:sub_object", [[(1, 0)]])
        resolve(mark_applied, b, db, M1)
        (update,) = db.sent("UPDATE [azsqlcd].[step]")
        assert "SET [status] = N'ok'" in update and f"WHERE [migration_id] = N'{M1}'" in update
        assert f"N'marked applied by run {RUN_ID}'" in update
        assert db.sent(BUILD_INDEX) == []  # recorded, not sent
        assert db.sent("N'reconciled'") == []  # the dead run is left for the next deploy
        # the catalog proves the index, under the lock and before the run row (N2-F2)
        (proof,) = db.sent("azsqlcd:sub_object")
        where = "[object_id] = OBJECT_ID(N'[sales].[Order]') AND [name] = N'IX_Order_c'"
        assert f"FROM sys.indexes WHERE {where}" in proof
        assert f"FROM sys.index_resumable_operations WHERE {where}" in proof
        assert db.index_of("sp_getapplock") < db.index_of(proof) < db.index_of("INSERT INTO [azsqlcd].[run]")


@pytest.mark.parametrize(
    ("found", "advice"),
    [
        ((0, 0), "--mark-not-applied"),
        ((0, 1), "Finish or abort the build"),
        ((1, 1), "Finish or abort the build"),
    ],
)
def test_mark_applied_of_an_index_build_is_refused_unless_the_catalog_shows_the_finished_index(found, advice):
    """N2-F2: after a build that was killed before it ran, mark-applied was accepted. The release
    was then recorded with no index, and drift reported nothing: the capture never held the index."""
    b = bundle(mig(M1, BUILD_INDEX, mode="nontx"))
    db = Db(b, nontx_step(b, "unknown"))
    db.respond("azsqlcd:sub_object", [[found]])
    error = refusal(mark_applied, b, db, M1)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RESOLVE_NOT_APPLICABLE")
    assert error.detail == {
        "migration": M1,
        "object": "[sales].[Order].[IX_Order_c]",
        "index_rows": found[0],
        "resumable_rows": found[1],
    }
    assert advice in error.message and db.sent(WRITE) == []


def test_mark_applied_of_a_key_build_reads_the_index_of_the_constraint():
    batch = "ALTER TABLE [sales].[Order] ADD CONSTRAINT [UQ_Order] UNIQUE ([c]) WITH (ONLINE = ON);"
    b = bundle(mig(M1, batch, mode="nontx"))
    db = Db(b, nontx_step(b, "started"))
    db.respond("azsqlcd:sub_object", [[(0, 0)]])
    assert refusal(mark_applied, b, db, M1).detail["object"] == "[sales].[Order].[UQ_Order]"


@pytest.mark.parametrize(
    "batch",
    [
        "ALTER INDEX [IX_Order_c] ON [sales].[Order] REBUILD WITH (ONLINE = ON);",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [CK_Order] CHECK ([c] > 0);",
        "-- azsqlcd:raw\n" + BUILD_INDEX + " DROP INDEX [IX_Order_c] ON [sales].[Order];",
    ],
)
def test_mark_applied_of_a_nontx_batch_that_builds_no_index_the_tool_can_read_keeps_the_rule_it_had(batch):
    """The catalog can prove only the one statement of an index or key build. For every other
    batch the operator inspects the database and the tool takes the word (A16)."""
    b = bundle(mig(M1, batch, mode="nontx"))
    db = Db(b, nontx_step(b, "unknown"))
    resolve(mark_applied, b, db, M1)
    assert db.sent("azsqlcd:sub_object") == [] and len(db.sent("SET [status] = N'ok'")) >= 1


def test_mark_applied_of_a_pending_migration_needs_force_no_readback_without_a_table_model():
    b, _, db = a_pending_migration()
    error = refusal(mark_applied, b, db, M1)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "READBACK_REQUIRED")
    assert db.sent(WRITE) == []

    b, _, db = a_pending_migration()
    resolve(mark_applied, b, db, M1, force_no_readback=True)
    (step,) = db.sent("N'migration'")
    assert f"VALUES ({RUN_ID}, N'migration', N'{M1}', N'{chain_sha(b, M1)}', N'ok'" in step
    assert "N'marked applied by resolve, no read-back'" in step
    assert db.sent(ADD_C) == [] and db.sent("UPDATE [azsqlcd].[object]") == []


def test_mark_applied_with_a_table_model_reads_the_tables_back_against_the_model_after_the_migration():
    b, _, db = a_pending_migration()
    hooks = Hooks()
    resolve(mark_applied, b, db, M1, table_hooks=hooks)
    assert hooks.read_back_calls == [([TABLE], M1, 0)]  # before the run row: a mismatch executes nothing
    (table_row,) = db.sent(lambda batch: batch.startswith("UPDATE [azsqlcd].[object]") and TABLE in batch)
    assert f"N'{state.capture_sha256({'columns': ['a', 'c']})}'" in table_row
    db.assert_order("BEGIN TRANSACTION", table_row, "N'migration'", "N'resolve'", "COMMIT TRANSACTION;")

    b, _, db = a_pending_migration()
    error = refusal(
        mark_applied, b, db, M1, table_hooks=Hooks(mismatch="TABLE:[sales].[Order] differs (columns)")
    )
    assert (error.exit_code, error.reason_code) == (
        Exit.REFUSED,
        "READBACK_MISMATCH",
    )  # nothing was rolled back
    assert db.sent(WRITE) == []


@pytest.mark.parametrize(
    ("migration", "next_pending"),
    [(M2, M1), ("0009__no_such.sql", M1)],  # not the next one of the chain; not of this release
)
def test_mark_applied_takes_only_the_next_pending_transactional_migration(migration, next_pending):
    b, _, db = a_pending_migration()
    error = refusal(mark_applied, b, db, migration, force_no_readback=True)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RESOLVE_NOT_APPLICABLE")
    assert error.detail == {"migration": migration, "next_pending": next_pending}
    assert db.sent(WRITE) == []


def test_mark_applied_refuses_a_nontx_migration_that_was_never_started_and_a_migration_that_is_applied():
    b = bundle(mig(M1, BUILD_INDEX, mode="nontx"))
    never_started = refusal(mark_applied, b, Db(b, recorded()), M1, force_no_readback=True)
    assert never_started.reason_code == "RESOLVE_NOT_APPLICABLE"

    b = bundle(mig(M1, ADD_C))
    st = recorded(steps=[StepRow(1, 1, "migration", M1, chain_sha(b, M1), "ok", None)], seq=7)
    applied = refusal(mark_applied, b, Db(b, st), M1, force_no_readback=True)
    assert (applied.reason_code, applied.detail["next_pending"]) == ("RESOLVE_NOT_APPLICABLE", None)


# ------------------------------------------------------------------ mark-not-applied
def test_mark_not_applied_proves_that_the_index_is_absent_and_that_no_build_of_it_is_paused():
    action, b, db, subject, *_ = marks_not_applied()
    resolve(action, b, db, *subject)
    (proof,) = db.sent("azsqlcd:sub_object")
    where = "[object_id] = OBJECT_ID(N'[sales].[Order]') AND [name] = N'IX_Order_c'"
    assert f"FROM sys.indexes WHERE {where}" in proof
    assert f"FROM sys.index_resumable_operations WHERE {where}" in proof
    (update,) = db.sent("UPDATE [azsqlcd].[step]")
    assert "SET [status] = N'not_applied'" in update and f"WHERE [migration_id] = N'{M1}'" in update
    assert db.index_of("sp_getapplock") < db.index_of(proof) < db.index_of("INSERT INTO [azsqlcd].[run]")


@pytest.mark.parametrize("found", [(1, 0), (0, 1), (1, 1)])
def test_mark_not_applied_refuses_while_the_index_or_a_resumable_build_of_it_exists(found):
    action, b, _, subject, *_ = marks_not_applied()
    db = Db(b, nontx_step(b, "started"))
    db.respond("azsqlcd:sub_object", [[found]])
    error = refusal(action, b, db, *subject)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "NOT_PROVEN_ABSENT")
    assert error.detail == {
        "object": "[sales].[Order].[IX_Order_c]",
        "index_rows": found[0],
        "resumable_rows": found[1],
    }
    assert db.sent(WRITE) == []


@pytest.mark.parametrize(
    ("batch", "name"),
    [
        ('CREATE UNIQUE NONCLUSTERED INDEX IX_u ON sales."Order" (c) WITH (ONLINE = ON);', "IX_u"),
        (
            "ALTER TABLE [sales].[Order] ADD CONSTRAINT [PK_Order] PRIMARY KEY ([id]) WITH (ONLINE = ON);",
            "PK_Order",
        ),
        (
            "ALTER TABLE [sales].[Order] ADD CONSTRAINT [UQ_Order] UNIQUE ([c]) WITH (ONLINE = ON);",
            "UQ_Order",
        ),
    ],
)
def test_mark_not_applied_reads_the_index_that_the_batch_builds_in_any_quoting(batch, name):
    b = bundle(mig(M1, batch, mode="nontx"))
    db = Db(b, nontx_step(b, "started"))
    db.respond("azsqlcd:sub_object", [[(0, 0)]])
    resolve(mark_not_applied, b, db, M1)
    (proof,) = db.sent("azsqlcd:sub_object")
    assert f"[object_id] = OBJECT_ID(N'[sales].[Order]') AND [name] = N'{name}'" in proof


@pytest.mark.parametrize(
    "batch",
    [
        "ALTER INDEX [IX_Order_c] ON [sales].[Order] REBUILD WITH (ONLINE = ON);",
        "ALTER TABLE [sales].[Order] ADD CONSTRAINT [CK_Order] CHECK ([c] > 0);",
        "CREATE INDEX [IX_Order_c] ON [Order] ([c]) WITH (ONLINE = ON);",  # no schema: no guess
    ],
)
def test_mark_not_applied_refuses_a_batch_whose_target_it_cannot_read(batch):
    b = bundle(mig(M1, batch, mode="nontx"))
    db = Db(b, nontx_step(b, "started"))
    error = refusal(mark_not_applied, b, db, M1)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "NOT_PROVEN_ABSENT")
    assert db.sent("azsqlcd:sub_object") == [] and db.sent(WRITE) == []


def test_mark_not_applied_is_only_for_a_nontx_step_whose_outcome_is_not_known():
    b = bundle(mig(M1, ADD_C))
    applied = recorded(steps=[StepRow(1, 1, "migration", M1, chain_sha(b, M1), "ok", None)], seq=7)
    assert refusal(mark_not_applied, b, Db(b, applied), M1).reason_code == "RESOLVE_NOT_APPLICABLE"
    b = bundle(mig(M1, BUILD_INDEX, mode="nontx"))
    assert (
        refusal(mark_not_applied, b, Db(b, nontx_step(b, "ok")), M1).reason_code == "RESOLVE_NOT_APPLICABLE"
    )
    assert refusal(mark_not_applied, b, Db(b, recorded()), M1).reason_code == "RESOLVE_NOT_APPLICABLE"


def test_a_step_that_was_marked_not_applied_is_pending_again_for_the_planner():
    b = bundle(mig(M1, BUILD_INDEX, mode="nontx"))
    st = nontx_step(b, "not_applied")
    work = plan.pending_work(b, dataclasses.replace(st, open_runs=()), 100)
    assert [migration.file for migration in work.pending] == [M1]


# ------------------------------------------------------------------ accept-drift
def test_accept_drift_records_the_live_module_and_forgets_its_source():
    b, _, db = a_drifted_module()
    resolve(accept_drift, b, db, OTHER)
    (upsert,) = db.sent("UPDATE [azsqlcd].[object]")
    assert f"WHERE [object_key] = N'{OTHER}'" in upsert
    assert f"[catalog_sha256] = N'{state.capture_sha256(capture(live_row(OTHER, HOT_FIX)))}'" in upsert
    assert "[source_sha256] = NULL" in upsert  # the next deploy sends the file: OVERWRITE_MODULE
    assert db.sent("CREATE OR ALTER") == []


def test_accept_drift_is_only_for_a_managed_object_that_exists():
    b, _, db = a_drifted_module()
    unmanaged = refusal(accept_drift, b, db, key("PROCEDURE", "usp_unknown"))
    assert (unmanaged.exit_code, unmanaged.reason_code) == (Exit.REFUSED, "RESOLVE_NOT_APPLICABLE")

    b, _, db = a_drifted_module()
    del db.catalog[OTHER]
    gone = refusal(accept_drift, b, db, OTHER)
    assert gone.reason_code == "RESOLVE_NOT_APPLICABLE" and "tombstone" in gone.message
    assert db.sent(WRITE) == []


def test_accept_drift_of_a_table_needs_the_table_model_and_a_database_that_equals_it():
    b, _, db = a_pending_migration()
    without = refusal(accept_drift, b, db, TABLE)
    assert (without.exit_code, without.reason_code) == (Exit.REFUSED, "TABLE_MODEL_REQUIRED")

    b, _, db = a_pending_migration()
    hooks = Hooks()
    resolve(accept_drift, b, db, TABLE, table_hooks=hooks)
    assert hooks.read_back_calls == [([TABLE], None, 0)]  # against the model of the release
    (upsert,) = db.sent("UPDATE [azsqlcd].[object]")
    assert (
        f"N'{state.capture_sha256({'columns': ['a', 'c']})}'" in upsert and "[source_sha256] = NULL" in upsert
    )

    b, _, db = a_pending_migration()
    differs = refusal(accept_drift, b, db, TABLE, table_hooks=Hooks(mismatch="TABLE:[sales].[Order] differs"))
    assert (differs.exit_code, differs.reason_code) == (Exit.REFUSED, "READBACK_MISMATCH")
    assert db.sent(WRITE) == []


# ------------------------------------------------------------------ adopt-module
def test_adopt_module_records_an_unmanaged_module_with_no_source():
    b, st, db = an_unmanaged_module()
    resolve(adopt_module, b, db, LEGACY)
    (upsert,) = db.sent("INSERT INTO [azsqlcd].[object]")
    assert f"N'{LEGACY}', N'managed', NULL, 1," in upsert  # key, status, source_sha256, capture format
    assert db.sent("CREATE OR ALTER") == []
    # the planner then sends the file as an overwrite, which the approver sees
    adopted = recorded(
        objects={LEGACY: row(LEGACY, "CREATE PROCEDURE [sales].[usp_legacy] AS SELECT 0;", source=None)}
    )
    assert [d.code for d in plan.pending_work(b, adopted, 100).destructive] == ["OVERWRITE_MODULE"]


@pytest.mark.parametrize(
    "object_key",
    [
        key("PROCEDURE", "usp_absent"),  # no such module
        key("PROCEDURE", "USP_LEGACY"),  # not the name that the catalog holds
        key("VIEW", "usp_legacy"),  # another kind
        TABLE,  # not a module
        "usp_legacy",  # not a key
    ],
)
def test_adopt_module_takes_only_the_exact_key_of_a_module_that_exists(object_key):
    b, _, db = an_unmanaged_module()
    error = refusal(adopt_module, b, db, object_key)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RESOLVE_NOT_APPLICABLE")
    assert db.sent(WRITE) == []


def test_adopt_module_refuses_a_key_in_another_letter_case():
    """TQ-04: the engine compares names without case, so the capture of [sales].[USP_LEGACY] is the
    capture of usp_legacy. The row is found by its exact key later: only the name that the catalog
    holds is adopted."""
    b, _, db = an_unmanaged_module()
    shouted = key("PROCEDURE", "USP_LEGACY")
    engine_row = live_row(shouted, "CREATE PROCEDURE [sales].[usp_legacy] AS SELECT 0;")
    db.respond("azsqlcd:capture_modules", [[engine_row]])
    error = refusal(adopt_module, b, db, shouted)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RESOLVE_NOT_APPLICABLE")
    assert "exactly the name" in error.message
    assert db.sent(WRITE) == [] and db.sent("BEGIN TRANSACTION") == []


def test_accept_drift_refuses_a_key_that_is_not_managed():
    """TQ-04: the module exists and is not managed. It is refused before the run row and before the
    transaction, not by a state write that finds no row."""
    b, _, db = an_unmanaged_module()
    error = refusal(accept_drift, b, db, LEGACY)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RESOLVE_NOT_APPLICABLE")
    assert "not a managed object" in error.message and error.detail == {"object": LEGACY}
    assert db.sent("BEGIN TRANSACTION") == [] and db.sent(WRITE) == []
    assert db.sent("azsqlcd:capture_modules") == []  # refused before the catalog is read for it


def test_adopt_module_refuses_a_module_that_is_managed():
    b, _, db = a_drifted_module()
    error = refusal(adopt_module, b, db, OTHER)
    assert error.reason_code == "RESOLVE_NOT_APPLICABLE" and "managed already" in error.message


# ------------------------------------------------------------------ clear-run
def test_clear_run_writes_the_step_that_lets_the_planner_run_again():
    action, b, db, subject, *_ = clears_a_run()
    unknown = recorded(open_runs=[run_row(9, "unknown")])
    with pytest.raises(Exception, match="RUN_UNKNOWN"):
        plan.pending_work(b, unknown, 100)
    resolve(action, b, db, *subject)
    (step,) = db.sent("INSERT INTO [azsqlcd].[step]")
    written = StepRow(1, RUN_ID, "resolve", None, None, "ok", "clear-run 9")
    assert f"VALUES ({RUN_ID}, N'resolve', NULL, NULL, N'ok', SYSUTCDATETIME(), N'{written.note}')" in step
    assert plan.pending_work(b, dataclasses.replace(unknown, steps=(written,)), 100).outcome == plan.WORK
    assert db.sent("UPDATE [azsqlcd].[run] SET [status] = N'failed'") == []  # run 9 stays unknown, as history


@pytest.mark.parametrize("open_runs", [[], [run_row(9, "running")], [run_row(8, "unknown")]])
def test_clear_run_is_only_for_a_run_with_the_status_unknown(open_runs):
    b = bundle(mig(M1))
    db = Db(b, recorded(open_runs=open_runs))
    error = refusal(clear_run, b, db, 9)
    assert (error.exit_code, error.reason_code, error.detail) == (
        Exit.REFUSED,
        "RESOLVE_NOT_APPLICABLE",
        {"run_id": 9},
    )
    assert db.sent(WRITE) == []


# ------------------------------------------------------------------ rebind-environment
def a_copy_of(env: str, on: str) -> tuple[Bundle, Db]:
    """A database with the state of one environment, restored on the server of another."""
    b = bundle(mig(M1))
    db = Db(b, recorded(env=env))
    db.options = session_options(CONFIG.env[on].lock_timeout_ms)
    return b, db


def test_rebind_environment_binds_a_refreshed_database_to_the_environment_of_its_target():
    b, db = a_copy_of("prod", on="dev")
    resolve(rebind_environment, b, db)
    (update,) = db.sent("UPDATE [azsqlcd].[meta]")
    assert update.startswith("UPDATE [azsqlcd].[meta] SET [environment] = N'dev' WHERE [id] = 1;")
    assert db.sent("azsqlcd:server_name") == []  # the server is checked only for prod


def test_rebind_environment_binds_to_prod_only_on_the_configured_prod_server():
    b, db = a_copy_of("dev", on="prod")
    db.respond("azsqlcd:server_name", [[("sql-sales-dev",)]])
    error = refusal(rebind_environment, b, db, env="prod")
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "REBIND_PROD_SERVER")
    assert error.detail == {"server": "sql-sales-dev", "configured": "sql-sales-prod.database.windows.net"}
    assert db.sent(WRITE) == []

    b, db = a_copy_of("dev", on="prod")
    db.respond("azsqlcd:server_name", [[("SQL-Sales-Prod",)]])  # server names have no case
    resolve(rebind_environment, b, db, env="prod")
    assert "SET [environment] = N'prod'" in db.sent("UPDATE [azsqlcd].[meta]")[0]


def test_rebind_environment_never_binds_the_state_of_another_project():
    b = bundle(mig(M1))
    st = dataclasses.replace(recorded(env="prod"), meta=Meta(1, "billing", "prod"))
    db = Db(b, st)
    db.options = session_options(CONFIG.env["dev"].lock_timeout_ms)
    error = refusal(rebind_environment, b, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "FENCE_META_MISMATCH")
    assert db.sent(WRITE) == []


def test_rebind_environment_refuses_a_database_that_is_bound_to_the_environment_already():
    b = bundle(mig(M1))
    error = refusal(rebind_environment, b, Db(b, recorded(env="dev")))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RESOLVE_NOT_APPLICABLE")


def test_object_keys_of_the_tests_are_keys():
    assert names.parse_object_key(LEGACY) == ("PROCEDURE", "sales", "usp_legacy")


# ------------------------------------------------------------------ keys without case
AS_TYPED = "PROCEDURE:[SALES].[USP_OTHER]"  # the operator types the key in another letter case


def test_accept_drift_finds_the_managed_row_under_a_key_of_another_case_and_writes_that_row():
    b, _, db = a_drifted_module()
    db.catalog = {AS_TYPED: live_row(AS_TYPED, HOT_FIX)}  # the engine resolves the name without case

    resolve(accept_drift, b, db, AS_TYPED)

    (upsert,) = db.sent("UPDATE [azsqlcd].[object]")
    assert f"WHERE [object_key] = N'{OTHER}'" in upsert and AS_TYPED not in upsert
    assert "[source_sha256] = NULL" in upsert


def test_adopt_module_refuses_a_module_that_is_managed_under_a_key_of_another_case():
    b, _, db = a_drifted_module()
    error = refusal(adopt_module, b, db, AS_TYPED)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "RESOLVE_NOT_APPLICABLE")
    assert "is managed already" in error.message and db.sent(WRITE) == []


def test_mark_applied_marks_a_table_that_the_migration_dropped_under_the_key_of_the_recorded_row():
    b, _, db = a_pending_migration()
    as_written = "TABLE:[SALES].[order]"

    class Dropped(Hooks):
        def touched_table_objects(self, work: Any) -> list[str]:
            return [as_written]

    resolve(mark_applied, b, db, M1, table_hooks=Dropped(captures={as_written: None}))

    (dropped,) = db.sent("SET [status] = N'dropped'")
    assert f"WHERE [object_key] = N'{TABLE}'" in dropped


# ------------------------------------------------------------------ review findings
SWAP_STATEMENTS = (
    "CREATE INDEX [IX_Order_c_new] ON [sales].[Order] ([c]) WITH (ONLINE = ON);",
    "DROP INDEX [IX_Order_c] ON [sales].[Order];",
    "EXEC sys.sp_rename N'[sales].[Order].[IX_Order_c_new]', N'IX_Order_c', N'INDEX';",
)
RAW = (
    "-- azsqlcd:raw TABLE:[sales].[Order] reason: swap the index online\n"
    "-- azsqlcd:allow RAW TABLE:[sales].[Order] reason: reviewed by the DBA\n"
)


@pytest.mark.parametrize(
    "batch",
    [
        RAW + "\n".join(SWAP_STATEMENTS),  # the online index swap: one raw batch of a nontx migration
        RAW + SWAP_STATEMENTS[0],  # a raw batch is not read, whatever it holds
        "-- azsqlcd:data\n" + SWAP_STATEMENTS[0],
        " ".join(SWAP_STATEMENTS[:2]),  # a model batch with a second statement
        SWAP_STATEMENTS[0].rstrip(";") + "\n" + SWAP_STATEMENTS[1],  # the same with no semicolon
    ],
)
def test_mark_not_applied_refuses_a_raw_batch_that_holds_more_than_the_one_index_build(batch):
    """The batch ran to its end and the session was lost before the answer: the step says started.
    [IX_Order_c_new] is absent because the batch renamed it, not because nothing ran. "The index
    that the first statement builds is absent" proves nothing for such a batch; a not_applied step
    would send the whole batch again."""
    b = bundle(mig(M1, batch, mode="nontx"))
    db = Db(b, nontx_step(b, "started"))
    db.respond("azsqlcd:sub_object", [[(0, 0)]])
    error = refusal(mark_not_applied, b, db, M1)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "NOT_PROVEN_ABSENT")
    assert db.sent("azsqlcd:sub_object") == [] and db.sent(WRITE) == []


@pytest.mark.parametrize("action", [adopts_a_module, accepts_drift, marks_applied])
def test_a_reason_text_of_another_resolve_action_does_not_clear_an_unknown_run(action):
    """A5: only resolve --clear-run, which checks the run, clears an unknown run. The planner knows
    the clear by the note of its resolve step, so no other action may write that note: not with a
    reason that an operator types as 'clear-run 9'."""
    act, b, db, subject, *_ = action()
    resolve(act, b, db, *subject, reason=plan.clear_run_note(9))
    (step,) = db.sent("N'resolve', NULL, NULL, N'ok'")
    (note,) = re.findall(r"SYSUTCDATETIME\(\), N'([^']*)'\);$", step)
    assert note != plan.clear_run_note(9) and note.endswith(": clear-run 9")
    unknown = recorded(open_runs=[run_row(9, "unknown")])
    written = StepRow(5, RUN_ID, "resolve", None, None, "ok", note)
    with pytest.raises(ToolError) as refused_plan:
        plan.pending_work(bundle(mig(M1)), dataclasses.replace(unknown, steps=(written,)), 100)
    assert refused_plan.value.reason_code == "RUN_UNKNOWN"


def test_mark_applied_takes_a_replacement_that_was_merged_after_a_later_migration():
    """A database that skipped a withdrawn migration and took the migration after it is not
    diverged; its DBA can also record the replacement by hand."""
    m3, m4 = "0003__c.sql", "0004__d.sql"
    lines = [mig(M1, ADD_C), mig(M2, "ALTER TABLE [sales].[Order] ADD [w] int NOT NULL;"), mig(m3, ADD_D)]
    lines.append(mig(m4, "ALTER TABLE [sales].[Order] ADD [w] int NULL;"))
    b = bundle(*lines)
    entries = list(chain.parse_sum(b.files[chain.SUM_PATH].decode()).entries)
    entries[1] = dataclasses.replace(entries[1], withdrawn=True)
    entries[3] = dataclasses.replace(entries[3], replaces=M2)
    files = {**b.files, chain.SUM_PATH: chain.format_sum(chain.Chain(False, tuple(entries))).encode()}
    b = Bundle(b.manifest, files)
    steps = [StepRow(i, i, "migration", f, chain_sha(b, f), "ok", None) for i, f in ((1, M1), (2, m3))]
    db = Db(b, recorded(steps=steps))
    report = resolve(mark_applied, b, db, m4, force_no_readback=True)
    assert report.exit_code == 0
    (step,) = db.sent("N'migration'")
    assert f"N'{m4}'" in step
