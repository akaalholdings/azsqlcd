"""The contract that a driver adapter must pass (design Part 2 (k), gates G-D1 to G-D5).

Owner-run, on a disposable Azure SQL Database. Read docs/live-testing.md first.

    az login
    export AZSQLCD_LIVE_SERVER=NAME.database.windows.net AZSQLCD_LIVE_DATABASE=D
    uv run --extra db pytest tests/live -q

Without the two variables every test is skipped. pyproject sets testpaths to tests/unit, so a
plain `pytest` never collects this file, and no CI job sets the variables.

The guards of scripts/live_spike.py run first: Azure SQL Database only, no "prod" in the name,
no azsqlcd.meta row other than 'disposable'. The tests make no object that outlives them: they
use temporary tables, and they kill only sessions that they opened themselves. The login needs
the right KILL DATABASE CONNECTION.

A second adapter (pyodbc) is one more entry in ADAPTERS; every test then runs for it too. Run
this file again after every change of the driver version.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path

import pytest

from azsqlcd import names
from azsqlcd import session as db
from azsqlcd.session import AzureCliTokenProvider, Session
from azsqlcd.sqlerrors import ErrorClass, SqlError

SERVER = os.environ.get("AZSQLCD_LIVE_SERVER", "")
DATABASE = os.environ.get("AZSQLCD_LIVE_DATABASE", "")

pytestmark = pytest.mark.skipif(
    not (SERVER and DATABASE),
    reason="live driver contract: set AZSQLCD_LIVE_SERVER and AZSQLCD_LIVE_DATABASE (a disposable database)",
)

# The helper process of live_spike.Background finds the module by this name, so the folder goes on
# the path instead of a load by file name.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
live = importlib.import_module("live_spike")


def _mssql_python() -> db.DriverConnect:
    return partial(db.open_session, pytest.importorskip("mssql_python"))


# name -> a function that gives one connect attempt (keywords, packed token) -> Session
ADAPTERS: dict[str, Callable[[], db.DriverConnect]] = {"mssql-python": _mssql_python}


@pytest.fixture(scope="module", params=sorted(ADAPTERS))
def connect(request: pytest.FixtureRequest) -> Callable[[], Session]:
    driver_connect = ADAPTERS[request.param]()
    provider = live.CachedTokenProvider(AzureCliTokenProvider())

    def open_one() -> Session:
        return db.connect(SERVER, DATABASE, provider, live.APP_NAME, driver_connect=driver_connect)

    first = open_one()
    try:
        # the variable is the confirmation: whoever sets it names the database on purpose
        live.refuse_unless_disposable(first, DATABASE, DATABASE)
    finally:
        first.close()
    return open_one


@pytest.fixture
def opened(connect: Callable[[], Session]) -> Iterator[Callable[[], Session]]:
    """Opens sessions for one test and closes them after it."""
    sessions: list[Session] = []

    def open_one() -> Session:
        sessions.append(connect())
        return sessions[-1]

    yield open_one
    for session in sessions:
        session.close()


def temporary_table() -> str:
    """A global temporary table name: another session can read the table, and it goes with its maker."""
    return f"##azsqlcd_contract_{uuid.uuid4().hex}"


# ------------------------------------------------------------------ G-D5 and the shape of a Session
def test_a_token_connect_gives_a_session_of_azure_sql_database(opened):
    ((spid, edition),) = opened().execute("SELECT @@SPID, CAST(SERVERPROPERTY(N'EngineEdition') AS int);")[0]
    assert type(spid) is int
    assert edition == 5


def test_every_result_set_with_columns_comes_back_in_order_as_rows_of_tuples(opened):
    batch = "SELECT 1 AS [a]; DECLARE @x int = 1; SELECT 2 AS [b], N'c' AS [c]; PRINT N'no columns';"
    assert opened().execute(batch) == [[(1,)], [(2, "c")]]


def test_a_batch_without_a_result_set_gives_no_result_set(opened):
    # the runner reads "no result set" from a guard query as a failed guard, never as a pass
    assert opened().execute("DECLARE @x int = 1;") == []


def test_a_closed_session_refuses_every_batch_and_close_can_be_called_again(opened):
    session = opened()
    session.close()
    session.close()
    assert session.closed
    with pytest.raises(SqlError) as error:
        session.execute("SELECT 1;")
    assert error.value.cls is ErrorClass.SESSION_LOST


def test_a_closed_session_is_gone_from_the_server(opened):
    # a pooled connection would keep its session, with the applock and the open transaction
    first, second = opened(), opened()
    connection_id = first.execute(live.CONNECTION_ID)[0][0][0]
    first.close()
    count = (
        "SELECT COUNT(*) FROM sys.dm_exec_connections "
        f"WHERE [connection_id] = {names.sql_literal(connection_id)};"
    )
    assert live.wait_until(lambda: second.execute(count) == [[(0,)]], 5)


def test_close_in_a_transaction_leaves_no_row(opened):
    # the driver has no cancel: closing the connection is how the tool stops
    holder, writer = opened(), opened()
    table = temporary_table()
    holder.execute(f"CREATE TABLE {table} ([id] int NOT NULL);")
    writer.execute(f"BEGIN TRANSACTION; INSERT INTO {table} ([id]) VALUES (1);")
    writer.close()
    holder.execute("SET LOCK_TIMEOUT 30000;")
    assert holder.execute(f"SELECT COUNT(*) FROM {table} WITH (READCOMMITTEDLOCK);") == [[(0,)]]


# ------------------------------------------------------------------ G-D1
@pytest.mark.parametrize(("name", "batch"), live.L2_CASES, ids=[name for name, _ in live.L2_CASES])
def test_an_error_in_a_later_statement_is_raised_or_ends_the_transaction(opened, name, batch):
    session = opened()
    session.execute(live.SESSION_OPTIONS)
    session.execute("CREATE TABLE #later ([id] int NOT NULL PRIMARY KEY);")
    session.execute("BEGIN TRANSACTION;")
    try:
        session.execute(batch.replace("{t}", "#later"))
    except SqlError:
        return  # raised by execute or by the drain
    assert live.guard(session)[:2] != [1, 1], f"{name}: no error, and the transaction looks sound"


# ------------------------------------------------------------------ G-D2
def test_a_killed_idle_session_is_never_replaced(opened):
    # no SET, no lock, no temporary table: the session that a driver could most easily open again
    victim, killer = opened(), opened()
    (spid,) = victim.execute(live.SPID)[0][0]
    live.kill(killer, spid)
    time.sleep(1.0)
    with pytest.raises(SqlError):
        victim.execute(live.CONNECTION_ID)
    with pytest.raises(SqlError):
        victim.execute(live.CONNECTION_ID)


# ------------------------------------------------------------------ G-D3
def test_batch_text_with_markers_and_escape_clauses_arrives_byte_identical(opened):
    # the batch reads its own text as the server received it, next to a literal with the same marks
    assert opened().execute(live.L4_ECHO_BATCH) == [[(live.L4_LITERAL, live.L4_ECHO_BATCH)]]


# ------------------------------------------------------------------ G-D4
def test_a_session_killed_while_it_waits_for_commit_raises(opened):
    victim = opened()
    victim.execute(live.SESSION_OPTIONS)
    (spid,) = victim.execute(live.SPID)[0][0]
    victim.execute(
        "CREATE TABLE #held ([a] int NULL); BEGIN TRANSACTION; INSERT INTO #held ([a]) VALUES (1);"
    )
    killer = live.Background(SERVER, DATABASE)  # a helper process: this one waits in execute()
    try:
        killer.submit(f"KILL {spid};", delay_s=3.0)
        started = time.monotonic()
        with pytest.raises(SqlError):
            victim.execute("WAITFOR DELAY '00:00:15'; COMMIT TRANSACTION;")
        waited = time.monotonic() - started
        killed = killer.result(60)
    finally:
        killer.close()
    assert killed is not None and not killed["raised"], f"the KILL did not run: {killed}"
    assert waited < 14, "the error came only when the batch had ended by itself"
    with pytest.raises(SqlError):
        victim.execute("SELECT 1;")


# ------------------------------------------------------------------ the class of an error
def test_a_lock_timeout_has_the_class_of_a_clean_stop_and_the_session_lives(opened):
    # exit 24 for a lock timeout depends on this class; the runner then rolls back on the same session
    holder, waiter = opened(), opened()
    table = temporary_table()
    holder.execute(f"CREATE TABLE {table} ([id] int NOT NULL PRIMARY KEY, [v] int NULL);")
    holder.execute(f"INSERT INTO {table} ([id]) VALUES (1);")
    holder.execute(f"BEGIN TRANSACTION; UPDATE {table} SET [v] = 1 WHERE [id] = 1;")
    waiter.execute("SET LANGUAGE us_english; SET LOCK_TIMEOUT 500;")
    with pytest.raises(SqlError) as error:
        waiter.execute(f"SELECT [v] FROM {table} WITH (READCOMMITTEDLOCK, ROWLOCK) WHERE [id] = 1;")
    assert (error.value.cls, error.value.number) == (ErrorClass.LOCK_TIMEOUT, 1222)
    assert not waiter.closed
    assert waiter.execute("SELECT 1;") == [[(1,)]]
