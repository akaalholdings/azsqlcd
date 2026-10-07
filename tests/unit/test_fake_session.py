"""FakeSession is the ground under the state, catalog, plan and runner tests.

If the fake answers wrongly, those tests prove nothing. These tests pin what it records, how a
rule is chosen, and what the built-in model of the session says.
"""

import re

import pytest

from azsqlcd.session import Session
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error
from support.fake_session import FakeSession

GUARD = "SELECT @@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID();"
GETLOCK = (
    "DECLARE @r int; EXEC @r = sys.sp_getapplock @Resource = N'azsqlcd:deploy', @LockMode = N'Exclusive', "
    "@LockOwner = N'Session', @LockTimeout = 30000; SELECT @r;"
)
LOCK_MODE = "SELECT APPLOCK_MODE(N'public', N'azsqlcd:deploy', N'Session')"
LOCK_TEST = "SELECT APPLOCK_TEST(N'public', N'azsqlcd:deploy', N'Exclusive', N'Session')"


def lock_timeout() -> SqlError:
    return sql_error("[Microsoft][SQL Server]Lock request time out period exceeded.")


def use(session: Session) -> Session:
    return session


def test_the_fake_has_the_shape_of_a_session():
    db = use(FakeSession())
    assert db.closed is False
    db.close()
    assert db.closed is True


# ------------------------------------------------------------------ record and rules
def test_every_batch_is_recorded_in_the_order_sent():
    db = FakeSession()
    for batch in ("SELECT 1", "UPDATE t SET c = 1", "SELECT 1"):
        db.execute(batch)
    assert db.batches == ["SELECT 1", "UPDATE t SET c = 1", "SELECT 1"]


def test_a_batch_with_no_rule_returns_no_result_set():
    assert FakeSession().execute("UPDATE t SET c = 1") == []


def test_a_rule_answers_by_substring_regex_or_callable():
    db = FakeSession()
    db.respond("FROM [azsqlcd].[meta]", [[("sales", "dev")]])
    db.respond(re.compile(r"from\s+sys\.objects", re.IGNORECASE), [[(1,)], [(2,)]])
    db.respond(lambda batch: batch.startswith("EXEC"), [[("done",)]])
    assert db.execute("SELECT [project] FROM [azsqlcd].[meta]") == [[("sales", "dev")]]
    assert db.execute("SELECT 1 FROM   SYS.OBJECTS") == [[(1,)], [(2,)]]
    assert db.execute("EXEC dbo.p") == [[("done",)]]
    assert db.execute("exec dbo.p") == []


def test_a_caller_that_changes_its_rows_does_not_change_the_rule():
    db = FakeSession()
    db.respond("FROM t", [[(1,), (2,)]])
    db.execute("SELECT c FROM t")[0].clear()
    assert db.execute("SELECT c FROM t") == [[(1,), (2,)]]


def test_the_first_matching_rule_wins():
    db = FakeSession()
    db.respond("FROM t", [[("first",)]])
    db.respond("SELECT c FROM t", [[("second",)]])
    assert db.execute("SELECT c FROM t") == [[("first",)]]


def test_a_callable_result_sees_the_batch_and_may_change_the_state():
    db = FakeSession()

    def script_ends_the_transaction(batch: str):
        db.trancount, db.xact_state = 0, 0
        return [[(batch.upper(),)]]

    db.respond("EXEC dbo.commits_inside", script_ends_the_transaction)
    db.execute("BEGIN TRANSACTION")
    assert db.execute("EXEC dbo.commits_inside") == [[("EXEC DBO.COMMITS_INSIDE",)]]
    assert db.execute(GUARD)[0][0][:2] == (0, 0)


def test_fail_on_raises_the_error_the_given_number_of_times_and_then_steps_aside():
    db = FakeSession()
    error = lock_timeout()
    db.fail_on("ALTER TABLE", error, times=2)
    db.respond("ALTER TABLE", [[("after",)]])
    for _ in range(2):
        with pytest.raises(SqlError) as raised:
            db.execute("ALTER TABLE t ADD c int")
        assert raised.value is error
    assert db.execute("ALTER TABLE t ADD c int") == [[("after",)]]
    assert len(db.batches) == 3  # a failed batch was sent, so it is recorded


def test_fail_on_fails_once_by_default():
    db = FakeSession()
    db.fail_on("ALTER TABLE", lock_timeout())
    with pytest.raises(SqlError):
        db.execute("ALTER TABLE t ADD c int")
    assert db.execute("ALTER TABLE t ADD c int") == []


def test_kill_on_loses_the_session_and_nothing_can_be_sent_after_it():
    db = FakeSession()
    db.kill_on("COMMIT")
    db.execute("BEGIN TRANSACTION")
    with pytest.raises(SqlError) as raised:
        db.execute("COMMIT TRANSACTION")
    assert (raised.value.cls, raised.value.sqlstate) == (ErrorClass.SESSION_LOST, "08S01")
    assert db.closed
    with pytest.raises(SqlError) as later:
        db.execute("SELECT 1")
    assert later.value.cls is ErrorClass.SESSION_LOST
    assert db.batches == ["BEGIN TRANSACTION", "COMMIT TRANSACTION"]  # SELECT 1 never left the client


def test_nothing_can_be_sent_after_close():
    db = FakeSession()
    db.close()
    with pytest.raises(SqlError):
        db.execute("SELECT 1")
    assert db.batches == []


# ------------------------------------------------------------------ questions
def test_sent_returns_the_matching_batches_and_index_of_the_first_position():
    db = FakeSession()
    for batch in ("SELECT 1", "ALTER TABLE a ADD c int", "SELECT 2", "ALTER TABLE b ADD c int"):
        db.execute(batch)
    assert db.sent("ALTER TABLE") == ["ALTER TABLE a ADD c int", "ALTER TABLE b ADD c int"]
    assert db.sent(re.compile(r"SELECT \d")) == ["SELECT 1", "SELECT 2"]
    assert db.sent("DROP") == []
    assert db.index_of("ALTER TABLE") == 1
    assert db.index_of(lambda batch: batch.endswith("2")) == 2


def test_index_of_fails_the_test_when_no_batch_matches():
    db = FakeSession()
    db.execute("SELECT 1")
    with pytest.raises(AssertionError, match="no batch matches 'DROP'"):
        db.index_of("DROP")


def test_assert_order_passes_only_when_the_batches_came_in_that_order():
    db = FakeSession()
    for batch in (
        "BEGIN TRANSACTION",
        "ALTER TABLE t ADD c int",
        "INSERT INTO [azsqlcd].[step] ...",
        "COMMIT",
    ):
        db.execute(batch)
    db.assert_order("BEGIN TRANSACTION", "ALTER TABLE", "INSERT INTO [azsqlcd].[step]", "COMMIT")
    db.assert_order("ALTER TABLE", "COMMIT")
    with pytest.raises(AssertionError, match="INSERT INTO"):
        db.assert_order("COMMIT", "INSERT INTO [azsqlcd].[step]")
    with pytest.raises(AssertionError):
        db.assert_order("BEGIN TRANSACTION", "DROP TABLE")


def test_assert_order_needs_a_later_batch_for_each_matcher():
    db = FakeSession()
    db.execute("INSERT INTO s VALUES (1); COMMIT")
    with pytest.raises(AssertionError):
        db.assert_order("INSERT INTO s", "COMMIT")


# ------------------------------------------------------------------ model: transaction
def test_the_guard_reads_one_open_transaction_with_a_stable_id():
    db = FakeSession()
    assert db.execute("SELECT @@TRANCOUNT") == [[(0,)]]
    db.execute(
        "IF @@SPID <> 57 THROW 51000, N'lost', 1; BEGIN TRANSACTION; UPDATE [azsqlcd].[run] SET x = 1;"
    )
    first = db.execute(GUARD)
    db.execute("ALTER TABLE t ADD c int")
    assert db.execute(GUARD) == first
    (trancount, xact_state, transaction_id) = first[0][0]
    assert (trancount, xact_state) == (1, 1)
    assert isinstance(transaction_id, int)


def test_commit_and_rollback_end_the_transaction():
    db = FakeSession()
    db.execute("BEGIN TRAN")
    db.execute("COMMIT TRANSACTION;")
    assert db.execute("SELECT @@TRANCOUNT, XACT_STATE()") == [[(0, 0)]]
    db.execute("begin transaction")
    db.execute("BEGIN TRANSACTION")
    assert db.execute("SELECT @@TRANCOUNT") == [[(2,)]]
    db.execute("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;")
    assert db.execute("SELECT @@TRANCOUNT, XACT_STATE()") == [[(0, 0)]]


def test_a_script_that_commits_and_begins_again_shows_as_a_new_transaction_id():
    # trancount and xact_state look right again; only the id tells (A5)
    db = FakeSession()
    db.execute("BEGIN TRANSACTION")
    before = db.execute(GUARD)[0][0]
    db.execute("COMMIT; BEGIN TRANSACTION;")
    after = db.execute(GUARD)[0][0]
    assert before[:2] == after[:2] == (1, 1)
    assert before[2] != after[2]


def test_outside_a_transaction_every_statement_has_its_own_transaction_id():
    db = FakeSession()
    first = db.execute("SELECT CURRENT_TRANSACTION_ID()")
    assert db.execute("SELECT CURRENT_TRANSACTION_ID()") != first


@pytest.mark.parametrize(
    "batch",
    [
        "-- ROLLBACK is what we do on error\nUPDATE t SET c = 1",
        "UPDATE t SET note = N'COMMIT; ROLLBACK'",
        "/* BEGIN TRANSACTION */ UPDATE t SET c = 1",
        "UPDATE [COMMIT] SET [ROLLBACK] = 1",
        "CREATE OR ALTER PROCEDURE dbo.p AS BEGIN BEGIN TRY SELECT 1 END TRY "
        "BEGIN CATCH ROLLBACK; THROW; END CATCH END",
        "BEGIN TRY SELECT 1 END TRY BEGIN CATCH SELECT 2 END CATCH",
    ],
)
def test_transaction_words_that_do_not_run_leave_the_transaction_alone(batch):
    db = FakeSession()
    db.execute("BEGIN TRANSACTION")
    db.execute(batch)
    assert db.execute("SELECT @@TRANCOUNT, XACT_STATE()") == [[(1, 1)]]


def test_an_error_ends_the_transaction_as_xact_abort_does():
    db = FakeSession()
    db.fail_on("ALTER TABLE", lock_timeout())
    db.execute("BEGIN TRANSACTION")
    with pytest.raises(SqlError):
        db.execute("ALTER TABLE t ADD c int")
    assert db.execute("SELECT @@TRANCOUNT, XACT_STATE()") == [[(0, 0)]]


def test_a_compile_error_can_leave_the_transaction_open():
    db = FakeSession()
    db.fail_on("ALTR TABLE", sql_error("Incorrect syntax near 'ALTR'."), keeps_transaction=True)
    db.execute("BEGIN TRANSACTION")
    with pytest.raises(SqlError):
        db.execute("ALTR TABLE t ADD c int")
    assert db.execute("SELECT @@TRANCOUNT, XACT_STATE()") == [[(1, 1)]]


def test_a_failed_batch_does_not_begin_a_transaction():
    db = FakeSession()
    db.fail_on("BEGIN TRANSACTION", sql_error("azsqlcd: session or lock lost"))
    with pytest.raises(SqlError):
        db.execute("IF @@SPID <> 57 THROW 51000, N'azsqlcd: session or lock lost', 1; BEGIN TRANSACTION;")
    assert db.trancount == 0


def test_a_rule_that_answers_a_batch_does_not_hide_its_begin_transaction():
    db = FakeSession()
    db.respond("UPDATE [azsqlcd].[run]", [[(1,)]])
    assert db.execute("BEGIN TRANSACTION; UPDATE [azsqlcd].[run] SET x = 1; SELECT @@ROWCOUNT;") == [[(1,)]]
    assert db.trancount == 1


def test_text_that_the_lexer_refuses_is_recorded_and_has_no_meaning():
    db = FakeSession()
    assert db.execute("SELECT 'unterminated") == []
    assert db.batches == ["SELECT 'unterminated"]


# ------------------------------------------------------------------ model: spid and applock
def test_the_spid_is_constant():
    db = FakeSession()
    assert db.execute("SELECT @@SPID") == db.execute("SELECT @@SPID;") == [[(57,)]]
    db.spid = 61
    assert db.execute("SELECT @@SPID AS spid") == [[(61,)]]


def test_a_granted_applock_is_held_until_it_is_released():
    db = FakeSession()
    assert db.execute(LOCK_MODE) == [[("NoLock",)]]
    assert db.execute(GETLOCK) == [[(0,)]]
    assert db.execute(LOCK_MODE) == [[("Exclusive",)]]
    assert db.execute(f"SELECT @@SPID, {LOCK_MODE[7:]}") == [[(57, "Exclusive")]]
    db.execute("EXEC sys.sp_releaseapplock @Resource = N'azsqlcd:deploy', @LockOwner = N'Session';")
    assert db.execute(LOCK_MODE) == [[("NoLock",)]]


@pytest.mark.parametrize(
    ("result", "mode", "test"),
    [(0, "Exclusive", 1), (1, "Exclusive", 1), (-1, "NoLock", 0), (-3, "NoLock", 0)],
)
def test_applock_mode_and_test_follow_the_settable_sp_getapplock_result(result, mode, test):
    db = FakeSession()
    db.applock_result = result
    assert db.execute(LOCK_TEST) == [[(test,)]]  # plan asks without taking the lock (A4)
    assert db.execute(GETLOCK) == [[(result,)]]
    assert db.execute(LOCK_MODE) == [[(mode,)]]


def test_a_lock_lost_in_the_middle_of_a_run_can_be_staged():
    db = FakeSession()
    db.execute(GETLOCK)
    db.applock_held = False
    assert db.execute(LOCK_MODE) == [[("NoLock",)]]


def test_a_rule_replaces_a_built_in_answer():
    db = FakeSession()
    db.respond("@@TRANCOUNT", [[(2, -1, 7)]])
    db.respond("APPLOCK_MODE", [[("Shared",)]])
    db.respond("sp_getapplock", [[(-999,)]])
    assert db.execute(GUARD) == [[(2, -1, 7)]]
    assert db.execute(LOCK_MODE) == [[("Shared",)]]
    assert db.execute(GETLOCK) == [[(-999,)]]
    assert db.applock_held is False  # the rule replaced the whole built-in, also its effect


def test_a_select_that_is_not_only_session_state_gets_no_built_in_answer():
    db = FakeSession()
    assert db.execute("SELECT @@TRANCOUNT, [status] FROM [azsqlcd].[run]") == []
    assert db.execute("SELECT [name] FROM sys.objects") == []
