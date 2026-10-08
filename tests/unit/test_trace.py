"""The triage log: it tells what the tool did, in order, and it holds nothing that must stay inside.

The reader of a log was not there and has no database. The log leaves the organisation. So each test
here is one of two kinds: "the fact is in the log" and "the text is not in the log".
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from azsqlcd import lex, session, trace
from azsqlcd.errors import Exit, ToolError, refused
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error
from support.links import symlink_or_skip

FAKE_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.ZmFrZS1wYXlsb2Fk.c2lnbmF0dXJl"
CONNECTION_STRING = "Server=tcp:corp-sql.database.windows.net;Password=Hunter2-Zebra!"


SIGN_IN_VARIABLES = (
    "AZSQLCD_AUTH",
    "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID",
    "AZSQLCD_SQL_USER",
    "AZSQLCD_SQL_PASSWORD",
)


@pytest.fixture(autouse=True)
def no_sign_in_of_the_machine(monkeypatch) -> None:
    """A workstation can have the sign-in variables of the tool set. No test reads them: a login
    with the name of an object of a test would be hidden in the log that the test reads."""
    for name in SIGN_IN_VARIABLES:
        monkeypatch.delenv(name, raising=False)


class FakeSession:
    """The smallest Session: it keeps what it gets and answers from a script."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.batches: list[str] = []
        self.answers = answers or {}
        self.closed = False
        self.spid = 57

    def execute(self, batch: str) -> Any:
        self.batches.append(batch)
        answer = self.answers.get(batch, [])
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self) -> None:
        self.closed = True


def events_of(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def a_trace(tmp_path: Path) -> tuple[trace.Trace, Path]:
    path = tmp_path / "logs" / "azsqlcd-20261007T140311Z-deploy.jsonl"
    return trace.Trace(path), path


# ------------------------------------------------------------------ the recorder
def test_an_event_is_one_json_line_with_time_sequence_and_kind(tmp_path):
    path = tmp_path / "deep" / "er" / "log.jsonl"  # the directory is made
    log = trace.Trace(path, now=lambda: datetime(2026, 10, 7, 14, 3, 11, 120_000, tzinfo=UTC))
    log.event(
        "first", step="0007__add_status.sql", n=3, ok=True, nothing=None, parts=["a", 1], more={"k": 2.5}
    )
    log.event("second", long="x" * 900, place=Path("report") / "plan.json", odd=object())
    assert log.close() is None

    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and raw.count(b"\n") == 2
    first, second = events_of(path)
    assert first == {
        "ts": "2026-10-07T14:03:11.120Z",
        "seq": 1,
        "kind": "first",
        "step": "0007__add_status.sql",
        "n": 3,
        "ok": True,
        "nothing": None,
        "parts": ["a", 1],
        "more": {"k": 2.5},
    }
    assert second["seq"] == 2
    assert second["long"] == "x" * 500  # a string is cut
    assert second["place"] == str(Path("report") / "plan.json")
    assert second["odd"] == "<object>"  # an unknown object is its type, never its text


@pytest.mark.parametrize("name", sorted(trace.FORBIDDEN_FIELDS))
def test_a_forbidden_field_name_is_refused(name, tmp_path):
    log, path = a_trace(tmp_path)
    with pytest.raises(ValueError, match="no field named"):
        log.event("batch", **{name: "SELECT 1"})
    with pytest.raises(ValueError, match="no field named"):
        log.event("batch", **{name.upper(): "SELECT 1"})
    with pytest.raises(ValueError, match="no field named"):
        log.event("batch", detail={"inner": [1], name: "SELECT 1"})  # also inside a value
    with pytest.raises(ValueError, match="no field named"):
        trace.Trace(None).event("batch", **{name: "SELECT 1"})  # the call is wrong with no file too
    log.close()
    assert path.read_text(encoding="utf-8") == ""


def test_the_recorder_sets_time_and_sequence_itself(tmp_path):
    log, _ = a_trace(tmp_path)
    with pytest.raises(ValueError, match="sets the field"):
        log.event("x", seq=7)
    with pytest.raises(ValueError, match="sets the field"):
        log.event("x", ts="1999-01-01")


def test_a_recorder_with_no_path_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log = trace.Trace(None)
    log.event("run", command="lint")
    inner = FakeSession({"SELECT 1;": [[(1,)]]})
    session = trace.open_traced(log, lambda: inner)
    assert session is inner  # nothing is wrapped
    assert log.close() is None and not log.enabled
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ batches
SECRET_BATCHES = [
    f"INSERT INTO [sales].[Customer] ([Name], [Card]) VALUES (N'ZEBRA-7731', N'{FAKE_TOKEN}');",
    "CREATE OR ALTER PROCEDURE [sales].[usp_pay] AS BEGIN\n"
    "  -- the reason is QUOKKA-RESTRICTED\n"
    f"  EXEC sp_addlinkedserver N'{CONNECTION_STRING}';\n"
    "  SELECT 4111111111111111 AS [card_number];\nEND",
    "/* azsqlcd:guard */ SELECT @@TRANCOUNT, N'WOMBAT-LITERAL';",
    "SELECT * FROM [sales].[Order] WHERE [Note] = 'unterminated NUMBAT-9",  # does not lex
]
SECRET_PARTS = [
    "ZEBRA-7731",
    FAKE_TOKEN,
    "QUOKKA",
    CONNECTION_STRING,
    "Hunter2",
    "4111111111111111",
    "WOMBAT",
    "NUMBAT",
    "card_number",
    "ROW-VALUE-KOALA",
    "PK_PLATYPUS",
    "4711-ECHIDNA",
]


def test_the_log_never_holds_batch_text(tmp_path):
    failure = sql_error(
        "Violation of PRIMARY KEY constraint 'PK_PLATYPUS'. Cannot insert duplicate key in object "
        "'sales.Customer'. The duplicate key value is (4711-ECHIDNA)."
    )
    answers = {SECRET_BATCHES[2]: [[(1, "ROW-VALUE-KOALA")]], SECRET_BATCHES[0]: failure}
    inner = FakeSession(answers)
    log, path = a_trace(tmp_path)
    trace.header(log, command="deploy", argv=["deploy", "--reason", "ZEBRA-7731"], tool_version="0.1.0",
                 tool_digest="d" * 64)  # fmt: skip
    session = trace.open_traced(log, lambda: inner)
    for batch in SECRET_BATCHES:
        try:
            session.execute(batch)
        except SqlError as error:
            trace.exception_event(log, error)
    session.close()
    trace.end(log, exit_code=21, reason_code="BATCH_FAILED", message="step 3 failed")
    log.close()

    assert inner.batches == SECRET_BATCHES  # what is sent is not changed
    written = path.read_text(encoding="utf-8")
    for part in SECRET_PARTS:
        assert part not in written, part
    batches = [event for event in events_of(path) if event["kind"] == "batch"]
    assert [event["head"] for event in batches] == [
        "INSERT [sales].[Customer]",
        "CREATE OR ALTER PROCEDURE [sales].[usp_pay]",
        "SELECT",
        "unreadable",
    ]
    assert [event["tag"] for event in batches] == [None, None, "guard", None]
    # what the reader gets in place of the text: enough to find the batch in the release
    import hashlib

    assert batches[1]["sha256"] == hashlib.sha256(SECRET_BATCHES[1].encode()).hexdigest()
    assert batches[1]["chars"] == len(SECRET_BATCHES[1])
    assert batches[2]["result_sets"] == [1]  # the row count of each result set, never a row


# (batch, head, later statements). The head is the class of the statement: keywords and names.
HEADS: list[tuple[str, str, list[str]]] = [
    (
        "CREATE OR ALTER PROCEDURE [sales].[usp_x] @a int = 42 AS BEGIN SELECT 'lit-1'; DELETE [t]; END",
        "CREATE OR ALTER PROCEDURE [sales].[usp_x]",
        [],  # the body of a module is not read
    ),
    ("create proc dbo.p as select 'lit-2'", "CREATE PROCEDURE [dbo].[p]", []),
    ("ALTER VIEW [sales].[v_Open] AS SELECT 1 AS [lit3]", "ALTER VIEW [sales].[v_Open]", []),
    (
        "CREATE TRIGGER [sales].[trg_a] ON [sales].[Order] AFTER INSERT AS SET NOCOUNT ON;",
        "CREATE TRIGGER [sales].[trg_a] ON [sales].[Order]",
        [],
    ),
    (
        "ALTER TABLE [sales].[Order] ADD [Status] varchar(10) NOT NULL DEFAULT 'lit-4';",
        "ALTER TABLE [sales].[Order]",
        [],
    ),
    (
        "CREATE TABLE sales.[Order Line] ([Id] int NOT NULL, [Qty] int DEFAULT 55501)",
        "CREATE TABLE [sales].[Order Line]",
        [],
    ),
    (
        "CREATE INDEX IX_a ON sales.[Order] ([a]) WHERE [a] > 55502",
        "CREATE INDEX [IX_a] ON [sales].[Order]",
        [],
    ),
    (
        "CREATE UNIQUE NONCLUSTERED INDEX [IX_b] ON [sales].[Order] ([b]) WHERE [b] = 'lit-5';",
        "CREATE UNIQUE NONCLUSTERED INDEX [IX_b] ON [sales].[Order]",
        [],
    ),
    ("DROP INDEX [IX_a] ON [sales].[Order];", "DROP INDEX [IX_a] ON [sales].[Order]", []),
    ("DROP TABLE IF EXISTS [sales].[Old];", "DROP TABLE IF EXISTS [sales].[Old]", []),
    (
        "INSERT INTO sales.Customer ([Name]) VALUES (N'lit-6'), (N'lit-7');",
        "INSERT [sales].[Customer]",
        [],
    ),
    ("SELECT [Name] FROM [sales].[Customer] WHERE [Name] = N'lit-8' AND [Id] = 55503", "SELECT", []),
    ("UPDATE [sales].[Order] SET [Note] = 'lit-9' WHERE [Id] = 55504;", "UPDATE [sales].[Order]", []),
    ("DELETE FROM [sales].[Order] WHERE [Id] = 55505", "DELETE [sales].[Order]", []),
    (
        "EXEC sys.sp_rename N'sales.Order.lit10', N'lit11', N'COLUMN';",
        "EXEC [sys].[sp_rename]",
        [],
    ),
    ("sp_rename 'sales.lit12', 'lit13'", "EXEC [sp_rename]", []),  # a call with no EXEC
    (
        "DECLARE @r int; EXEC @r = sys.sp_getapplock @Resource = N'lit-14'; "
        "IF @r < 0 THROW 55508, N'lit-15', 1;",
        "DECLARE",
        ["EXEC [sys].[sp_getapplock]", "IF ... THROW"],
    ),
    (
        "SET XACT_ABORT ON; SET LOCK_TIMEOUT 55506; SET LANGUAGE us_english;",
        "SET XACT_ABORT ON",
        ["SET LOCK_TIMEOUT", "SET LANGUAGE"],
    ),  # fmt: skip
    ("SET PARSEONLY ON;", "SET PARSEONLY ON", []),
    (
        "BEGIN TRANSACTION; INSERT INTO [azsqlcd].[step] VALUES (55507, N'lit-16'); COMMIT TRANSACTION;",
        "BEGIN TRANSACTION",
        ["INSERT [azsqlcd].[step]", "COMMIT TRANSACTION"],
    ),
    ("COMMIT TRANSACTION;", "COMMIT TRANSACTION", []),
    ("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;", "IF ... ROLLBACK TRANSACTION", []),
    (
        "IF EXISTS (SELECT 1 FROM sys.objects WHERE [name] = N'lit-17') DROP PROCEDURE [sales].[p];",
        "IF ... DROP PROCEDURE [sales].[p]",
        [],
    ),
    ("EXEC (N'DROP TABLE lit18')", "EXEC", []),
    ("/* azsqlcd:spid */ SELECT @@SPID;", "SELECT", []),
    ("(SELECT 'lit-19')", "other", []),
    ("   \n  ", "empty", []),
    ("SELECT 'lit-20 has no end", "unreadable", []),  # does not lex
    ("SELECT 1eDELETE FROM [t]", "unreadable", []),  # the lexer refuses it
]


@pytest.mark.parametrize(
    ("batch", "head", "later"), HEADS, ids=[row[1] + f" #{n}" for n, row in enumerate(HEADS)]
)
def test_the_head_of_a_batch_holds_keywords_and_object_names_only(batch, head, later):
    assert trace.batch_outline(batch) == (head, later)
    assert trace.batch_head(batch) == head
    if head in (trace.UNREADABLE, trace.EMPTY, trace.OTHER):
        return
    # each bracketed part is an identifier token of the batch; each other word is of a closed list
    identifiers = {tok.value for tok in lex.tokens(batch) if tok.kind in ("word", "bident", "qident")}
    for told in (head, *later):
        names = re.findall(r"\[((?:[^\]]|\]\])*)\]", told)
        assert set(names) <= identifiers
        rest = re.sub(r"\[(?:[^\]]|\]\])*\]", " ", told).replace(".", " ").split()
        assert set(rest) <= trace.HEAD_VOCABULARY, rest
        assert "lit" not in told.lower() and "555" not in told


def test_the_table_of_heads_covers_the_cases_that_can_leak():
    batches = [batch for batch, _, _ in HEADS]
    assert len(batches) >= 20
    for needed in ("PROCEDURE", "INSERT INTO", "WHERE", "sp_rename", "has no end"):
        assert any(needed in batch for batch in batches), needed


def test_a_batch_with_many_statements_is_cut_with_a_count():
    batch = "BEGIN TRANSACTION; " + "DELETE FROM [t]; " * 14 + "COMMIT TRANSACTION;"
    head, later = trace.batch_outline(batch)
    assert head == "BEGIN TRANSACTION"
    assert later == ["DELETE [t]"] * trace.MAX_LATER_STATEMENTS + ["(+5 more)"]


def test_a_name_with_a_bracket_stays_one_name():
    assert (
        trace.batch_head("ALTER TABLE [sales].[odd]]name] ADD [c] int") == "ALTER TABLE [sales].[odd]]name]"
    )


def test_a_batch_event_has_the_session_the_count_and_the_duration(tmp_path):
    clock = iter([10.0, 10.25, 20.0, 20.004])
    inner = FakeSession({"SELECT 1;": [[(1,), (2,)], []]})
    log, path = a_trace(tmp_path)
    session = trace.TracingSession(inner, log, "main", monotonic=lambda: next(clock))
    assert session.execute("SELECT 1;") is inner.answers["SELECT 1;"]  # the result itself
    assert session.execute("COMMIT TRANSACTION;") == []
    assert session.spid == 57 and session.closed is False  # the rest of the session is as it was
    session.close()
    session.close()
    assert inner.closed and session.closed
    first, second, closed = events_of(path)
    assert (first["session"], first["n"], first["ms"], first["result_sets"]) == ("main", 1, 250.0, [2, 0])
    assert (second["n"], second["ms"], second["head"]) == (2, 4.0, "COMMIT TRANSACTION")
    assert closed == closed | {"kind": "session_closed", "session": "main", "batches": 2, "ms": 254.0}


def test_an_error_is_recorded_redacted_and_re_raised_unchanged(tmp_path):
    lock = SqlError(
        "Lock request time out period exceeded. Object 'sales.SecretTable' (id 4711)",
        number=1222,
        sqlstate="HYT00",
        cls=ErrorClass.LOCK_TIMEOUT,
    )
    crash = RuntimeError("driver said: token=" + FAKE_TOKEN)
    inner = FakeSession({"ALTER TABLE [sales].[Order] ADD [c] int;": lock, "SELECT 2;": crash})
    log, path = a_trace(tmp_path)
    session = trace.TracingSession(inner, log, "main")
    with pytest.raises(SqlError) as caught:
        session.execute("ALTER TABLE [sales].[Order] ADD [c] int;")
    assert caught.value is lock and caught.value.raw_message == lock.raw_message
    with pytest.raises(RuntimeError) as other:
        session.execute("SELECT 2;")
    assert other.value is crash
    log.close()

    first, second = events_of(path)
    assert first["head"] == "ALTER TABLE [sales].[Order]"
    assert first["error"] == {
        "number": 1222,
        "sqlstate": "HYT00",
        "class": "LOCK_TIMEOUT",
        "message": "Lock request time out period exceeded. Object <redacted> <redacted>",
    }
    assert "result_sets" not in first
    assert second["error"] == {"type": "RuntimeError"}  # the text of an unknown error is not written
    written = path.read_text(encoding="utf-8")
    assert "SecretTable" not in written and "4711" not in written and FAKE_TOKEN not in written


def test_a_log_that_cannot_be_written_does_not_change_the_result_of_execute(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the log directory should be")
    never_opened = trace.Trace(blocker / "logs" / "x.jsonl")
    assert isinstance(never_opened.error, OSError)

    opened, _ = a_trace(tmp_path)
    opened._file.close()  # the disk goes away under an open log: every write fails from here

    failure = sql_error("Lock request time out period exceeded.")
    for log in (never_opened, opened):
        inner = FakeSession({"SELECT 1;": [[(1,)]], "SELECT 2;": failure})
        session = trace.TracingSession(inner, log, "main")
        assert session.execute("SELECT 1;") == [[(1,)]]
        with pytest.raises(SqlError) as caught:
            session.execute("SELECT 2;")
        assert caught.value is failure
        trace.header(log, command="deploy", argv=["deploy"], tool_version="0.1.0", tool_digest="d" * 64)
        trace.exception_event(log, failure)
        trace.end(log, exit_code=21, reason_code="BATCH_FAILED", message="m")
        session.close()
        assert inner.batches == ["SELECT 1;", "SELECT 2;"] and inner.closed
        assert isinstance(log.close(), Exception)  # the caller can tell that the log is not complete


def test_a_session_that_does_not_open_is_recorded_and_the_error_is_the_same(tmp_path):
    log, path = a_trace(tmp_path)
    error = refused("DRIVER_MISSING", "mssql-python is not installed")

    def opener() -> Any:
        raise error

    with pytest.raises(ToolError) as caught:
        trace.open_traced(log, opener)
    assert caught.value is error
    (connect,) = events_of(path)
    assert connect["kind"] == "connect" and connect["ok"] is False and connect["session"] == "main"
    assert connect["error"]["reason_code"] == "DRIVER_MISSING"


def test_sessions_are_named_by_the_order_in_which_they_open(tmp_path):
    log, path = a_trace(tmp_path)
    for _ in range(3):
        trace.open_traced(log, FakeSession).execute("SELECT 1;")
    batches = [event for event in events_of(path) if event["kind"] == "batch"]
    assert [event["session"] for event in batches] == ["main", "parse", "session-3"]
    assert [event["n"] for event in batches] == [1, 1, 1]  # the count is per session


# ------------------------------------------------------------------ the first event
def test_argv_values_are_hidden_except_for_the_safe_list():
    argv = [
        "deploy", "--bundle", "release", "--digest", "abc123", "--env", "prod", "--target", "sales-eu",
        "--expect-plan-file", "plan/plan.json", "--out", "report", "--approved-by", "alice,bob",
        "--approved-utc", "2026-10-07T09:00:00Z", "--triggering-actor", "carol",
        "--ci-run-url", "https://github.example/o/r/actions/runs/1", "--confirm-database", "SalesProd",
        "--reason", "ticket 4711: customer data fix", "--mark-applied", "0007__add_status.sql",
        "--show-error-text", "--ci", "github", "--rename=column:[a].[b].[c]=[d]", "--root=.",
        "--some-new-option", "its value", "--inline-plan", "stray", "-x",
    ]  # fmt: skip
    assert trace.safe_argv(argv) == [
        "deploy", "--bundle", "release", "--digest", "abc123", "--env", "prod", "--target", "sales-eu",
        "--expect-plan-file", "plan/plan.json", "--out", "report", "--approved-by", "<set>",
        "--approved-utc", "<set>", "--triggering-actor", "<set>",
        "--ci-run-url", "<set>", "--confirm-database", "<set>",
        "--reason", "<set>", "--mark-applied", "<set>",
        "--show-error-text", "--ci", "github", "--rename=<set>", "--root=.",
        "--some-new-option", "<set>", "--inline-plan", "<arg>", "<arg>",
    ]  # fmt: skip


def test_a_flag_takes_no_value_and_an_option_at_the_end_has_none():
    assert trace.safe_argv(["--no-log", "lint", "--resum", "--reason"]) == [
        "--no-log",
        "lint",
        "--resum",
        "--reason",
    ]
    assert trace.safe_argv(["gen", "--name", "add_status", "--base"]) == [
        "gen",
        "--name",
        "add_status",
        "--base",
    ]


def test_the_run_event_has_the_versions_the_platform_and_the_ci_facts_that_exist(tmp_path):
    log, path = a_trace(tmp_path)
    environ = {
        "GITHUB_RUN_ID": "991",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_REPOSITORY": "contoso/db-sales",
        "GITHUB_JOB": "deploy",
        "RUNNER_OS": "Linux",
        "GITHUB_SHA": "",  # empty: not a fact
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": FAKE_TOKEN,
        "AZURE_CLIENT_SECRET": "Hunter2",
    }
    summary = {
        "project": "sales",
        "environment": "dev",
        "target": "sales-dev",
        "server": "corp-dev.database.windows.net",
        "database": "sales",
        "table_model": True,
        "auth": "entra",
        "tenant_id": "must-not-pass",
    }
    trace.header(log, command="plan", argv=["plan", "--env", "dev", "--reason", "r"], tool_version="0.1.0",
                 tool_digest="a" * 64, config_summary=summary, environ=environ)  # fmt: skip
    trace.config_event(log, **summary)
    trace.config_event(log, **summary)  # the same facts again: written once
    log.close()
    run, config = events_of(path)
    assert run["kind"] == "run" and run["command"] == "plan"
    assert run["argv"] == ["plan", "--env", "dev", "--reason", "<set>"]
    assert (run["tool_version"], run["tool_digest"]) == ("0.1.0", "a" * 64)
    assert re.fullmatch(r"3\.\d+\.\d+\S*", run["python"])
    assert set(run["platform"]) == {"system", "release", "machine"}
    assert set(run["packages"]) == {"mssql-python", "azure-identity"}
    assert all(version == "not installed" or version[0].isdigit() for version in run["packages"].values())
    assert run["ci"] == {
        "GITHUB_RUN_ID": "991",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_REPOSITORY": "contoso/db-sales",
        "GITHUB_JOB": "deploy",
        "RUNNER_OS": "Linux",
    }
    expected = {key: summary[key] for key in trace.CONFIG_KEYS}
    assert run["config"] == expected
    assert config == config | {"kind": "config", **expected}
    written = path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in written and "Hunter2" not in written and "must-not-pass" not in written


# ------------------------------------------------------------------ the SQL login of the environment
# no quote and no parenthesis: redact() would cut the value there, and a part is not the value. The
# session module takes both values out of a driver text before it is redacted (test_auth.py).
SQL_USER, SQL_PASSWORD = "deploy_login_Zq7", "Pw-9x;}{=K3y\\marker"
# the values are hidden when the sign-in is SQL authentication: only then does the tool read them
SQL_SIGN_IN = {"AZSQLCD_AUTH": "sql", "AZSQLCD_SQL_USER": SQL_USER, "AZSQLCD_SQL_PASSWORD": SQL_PASSWORD}


def test_the_variables_whose_values_are_hidden_are_the_ones_of_sql_authentication():
    assert trace.SECRET_VARIABLES == (session.SQL_PASSWORD_VARIABLE, session.SQL_USER_VARIABLE)
    assert trace.SECRET_VARIABLES == ("AZSQLCD_SQL_PASSWORD", "AZSQLCD_SQL_USER")


def test_the_login_and_the_password_of_the_environment_reach_no_event_of_the_log(tmp_path):
    """Run header, config event, batch events, exception events and the end message: a caller, a
    driver or an engine message that holds one of the two values cannot put it into the file."""
    path = tmp_path / "logs" / "log.jsonl"
    environ = SQL_SIGN_IN | {"GITHUB_JOB": SQL_USER}
    log = trace.Trace(path, environ=environ)
    both = f"{SQL_USER} and {SQL_PASSWORD}"
    trace.header(log, command="plan", argv=["plan", "--env", SQL_USER, "--out", SQL_PASSWORD, SQL_USER],
                 tool_version="0.1.0", tool_digest="a" * 64, environ=environ)  # fmt: skip
    trace.config_event(
        log, project=SQL_USER, environment="dev", target=both, server="s", database=SQL_PASSWORD
    )
    failing = f"ALTER TABLE [{SQL_USER}].[Order] ADD [c] int NULL;"
    answers = {failing: SqlError(f"Cannot find the user {SQL_USER}, password {SQL_PASSWORD}.", number=15151)}
    db = trace.open_traced(log, lambda: FakeSession(answers))
    db.execute(f"CREATE TABLE [{SQL_USER}].[t] ([c] int NULL);")
    with pytest.raises(SqlError):
        db.execute(failing)
    with pytest.raises(ToolError):
        trace.open_traced(log, lambda: (_ for _ in ()).throw(refused(SQL_USER, both, **{SQL_USER: 1})))
    trace.exception_event(log, raised(refused("CONNECT_FAILED", both, **{SQL_PASSWORD: both})))
    trace.exception_event(log, raised(SqlError(f"Login failed for {both}", number=18456)))
    log.event("note", nested={"list": [both, {"inner": both}], SQL_USER: SQL_PASSWORD})
    trace.end(log, exit_code=24, reason_code="CONNECT_FAILED", message=f"no connection: {both}")
    assert log.close() is None

    written = path.read_text(encoding="utf-8")
    events = events_of(path)
    assert SQL_USER not in written and SQL_PASSWORD not in written
    assert json.dumps(SQL_PASSWORD)[1:-1] not in written  # nor in its JSON form
    assert [event["kind"] for event in events] == [
        "run", "config", "connect", "batch", "batch", "connect", "exception", "exception", "note", "end",
    ]  # fmt: skip
    assert events[0]["argv"] == ["plan", "--env", "<hidden>", "--out", "<hidden>", "<arg>"]
    assert events[3]["head"] == "CREATE TABLE [<hidden>].[t]"
    assert events[4]["error"]["message"] == "Cannot find the user <hidden>, password <hidden>."
    assert events[-1]["message"] == "no connection: <hidden> and <hidden>"
    assert trace.HIDDEN_VALUE == "<hidden>"
    summary = trace.summarize(path)
    assert SQL_USER not in summary and SQL_PASSWORD not in summary


def test_a_value_is_hidden_before_a_long_string_is_cut(tmp_path):
    # cut first, the start of a password at the end of the 500 characters would stay
    log = trace.Trace(tmp_path / "log.jsonl", environ=SQL_SIGN_IN)
    log.event("note", said="x" * (trace.MAX_STRING - 5) + SQL_PASSWORD + " and more")
    log.close()
    said = events_of(tmp_path / "log.jsonl")[0]["said"]
    assert said == ("x" * (trace.MAX_STRING - 5) + "<hidden> and more")[: trace.MAX_STRING]
    assert SQL_PASSWORD[:5] not in said


def test_a_log_hides_nothing_when_the_variables_are_not_set_or_are_empty(tmp_path):
    path = tmp_path / "log.jsonl"
    log = trace.Trace(path, environ={"AZSQLCD_SQL_USER": "", "AZSQLCD_AUTH": "sql"})
    log.event("note", said="a plain value")
    log.close()
    assert events_of(path)[0]["said"] == "a plain value"


def test_the_recorder_reads_the_variables_of_the_process_when_none_are_given(tmp_path, monkeypatch):
    for name, value in SQL_SIGN_IN.items():
        monkeypatch.setenv(name, value)
    log, path = a_trace(tmp_path)
    log.event("note", said=f"with {SQL_PASSWORD}")
    log.close()
    assert events_of(path)[0]["said"] == "with <hidden>"


def said(tmp_path, environ: dict[str, str], text: str, **more) -> str:
    path = tmp_path / "said.jsonl"
    log = trace.Trace(path, environ=environ)
    log.event("note", said=text, **more)
    log.close()
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("auth", [None, "", "entra", "managed-identity", "SQL"])
def test_a_log_hides_nothing_when_the_sign_in_is_not_sql_authentication(tmp_path, auth):
    """The variables of a SQL login can be set on a machine that signs in another way. The tool
    does not read them then, and a name of the log that equals one of them is not replaced."""
    environ = {"AZSQLCD_SQL_USER": "sales", "AZSQLCD_SQL_PASSWORD": "Order"}
    if auth is not None:
        environ["AZSQLCD_AUTH"] = auth
    text = "the table sales.Order on sales-dev"
    assert json.loads(said(tmp_path, environ, text, items=[{"sales": "Order"}])) | {"ts": 0} == {
        "ts": 0, "seq": 1, "kind": "note", "said": text, "items": [{"sales": "Order"}],
    }  # fmt: skip
    assert trace.SIGN_IN_VARIABLE == session.AUTH_VARIABLE and trace.SIGN_IN_SQL == session.AUTH_SQL


@pytest.mark.parametrize(
    ("login", "text", "written"),
    [
        ("a", "Login failed for user a (a)", "Login failed for user <hidden> (<hidden>)"),
        ("u", "Login failed for user", "Login failed for user"),
        ("sale", "sales.Order of wholesale, by sale.", "sales.Order of wholesale, by <hidden>."),
        ("deploy_login", "deploy_login_old and my_deploy_login and [deploy_login]",
         "deploy_login_old and my_deploy_login and [<hidden>]"),
        ("svc-deploy/01", "as svc-deploy/01, not svc-deploy/012", "as <hidden>, not svc-deploy/012"),
        ("[x]", "the login [x]y", "the login <hidden>y"),  # no word at its ends: hidden wherever it stands
    ],
)  # fmt: skip
def test_a_login_is_hidden_as_a_whole_word_and_never_inside_another_word(tmp_path, login, text, written):
    """A login is short. Replaced inside other words it shows itself by its places and damages the
    log: 'Login f<hidden>iled'."""
    environ = SQL_SIGN_IN | {"AZSQLCD_SQL_USER": login}
    assert json.loads(said(tmp_path, environ, text))["said"] == written


def test_the_password_is_hidden_wherever_it_stands_also_inside_a_word(tmp_path):
    text = f"x{SQL_PASSWORD}y and PWD={{{SQL_PASSWORD.replace('}', '}}')}}}"
    assert json.loads(said(tmp_path, SQL_SIGN_IN, text))["said"] == "x<hidden>y and PWD=<hidden>"


def test_a_value_is_hidden_in_the_form_that_a_connection_string_holds(tmp_path):
    """open_session sends PWD={...} with } doubled. That form is not the value of the variable."""
    doubled = SQL_PASSWORD.replace("}", "}}")
    assert doubled != SQL_PASSWORD
    written = said(
        tmp_path, SQL_SIGN_IN | {"AZSQLCD_SQL_USER": "dep}loy"}, f"UID={{dep}}}}loy}};PWD={{{doubled}}}"
    )
    assert json.loads(written)["said"] == "UID=<hidden>;PWD=<hidden>"
    assert "K3y" not in written and "loy" not in written


def test_a_caller_can_name_the_login_and_the_password_to_hide(tmp_path):
    # cli.main with a credential that the caller gives: the environment does not hold the values
    path = tmp_path / "log.jsonl"
    log = trace.Trace(path, environ={})
    log.hide_sign_in(SQL_USER, SQL_PASSWORD)
    log.event("note", said=f"{SQL_USER} / {SQL_PASSWORD}")
    log.close()
    assert events_of(path)[0]["said"] == "<hidden> / <hidden>"


def test_the_config_event_and_the_summary_name_the_kind_of_sign_in(tmp_path):
    log, path = a_trace(tmp_path)
    trace.header(log, command="plan", argv=["plan"], tool_version="0.1.0", tool_digest="d" * 64)
    trace.config_event(log, project="sales", environment="dev", target="sales-dev", server="s", database="d",
                       table_model=False, auth="managed-identity")  # fmt: skip
    log.close()
    assert events_of(path)[1]["auth"] == "managed-identity"
    assert "sign-in: managed-identity" in trace.summarize(path).splitlines()
    # a log of a version that did not record it: no line
    old, old_path = trace.Trace(tmp_path / "old.jsonl"), tmp_path / "old.jsonl"
    trace.header(old, command="plan", argv=["plan"], tool_version="0.1.0", tool_digest="d" * 64)
    trace.config_event(old, project="sales", environment="dev", target="sales-dev", server="s", database="d")
    old.close()
    assert "sign-in" not in trace.summarize(old_path)


# ------------------------------------------------------------------ exceptions and the end
def raised(error: BaseException) -> BaseException:
    try:
        raise error
    except BaseException as caught:
        return caught


def test_an_unknown_exception_is_its_type_and_its_place_and_never_its_message(tmp_path):
    log, path = a_trace(tmp_path)
    trace.exception_event(log, raised(KeyError("row value WOMBAT-55")))
    log.close()
    (event,) = events_of(path)
    assert event["type"] == "KeyError"
    assert event["stack"][-1] == ["test_trace.py", event["stack"][-1][1], "raised"]
    assert isinstance(event["stack"][-1][1], int)
    assert "WOMBAT" not in path.read_text(encoding="utf-8")


def test_a_tool_error_is_its_reason_code_and_exit_code_with_the_engine_error_behind_it(tmp_path):
    log, path = a_trace(tmp_path)
    # a message that prints a value; the names of a message of names only (207, 208) are shown
    engine = sql_error("Conversion failed when converting the varchar value 'sales.Hidden' to data type int.")
    error = ToolError(
        Exit.FAILED_ROLLED_BACK,
        "BATCH_FAILED",
        "step 3 failed",
        detail={"rename_constraints_sql": "EXEC sys.sp_rename N'SECRET_SCRIPT'", "step": "0007"},
    )
    error.__cause__ = engine
    trace.exception_event(log, raised(error))
    trace.exception_event(log, engine)
    trace.end(log, exit_code=21, reason_code="BATCH_FAILED", message="step 3 failed")
    log.close()
    tool, sql, ended = events_of(path)
    assert (tool["type"], tool["reason_code"], tool["exit_code"]) == ("ToolError", "BATCH_FAILED", 21)
    assert tool["detail_keys"] == ["rename_constraints_sql", "step"]  # the names, never the values
    assert tool["causes"][0] == tool["causes"][0] | {
        "type": "SqlError",
        "number": 245,
        "message": "Conversion failed when converting the varchar value <redacted> to data type int.",
    }
    assert (sql["type"], sql["number"], sql["class"]) == ("SqlError", 245, "OTHER")
    assert ended == ended | {
        "kind": "end",
        "exit_code": 21,
        "reason_code": "BATCH_FAILED",
        "message": "step 3 failed",
    }
    written = path.read_text(encoding="utf-8")
    assert "SECRET_SCRIPT" not in written and "sales.Hidden" not in written


# ------------------------------------------------------------------ where a log is
def test_the_log_directory_is_the_variable_then_the_out_directory_then_the_default():
    assert trace.default_log_dir("report", {"AZSQLCD_LOG_DIR": "/var/log/az"}) == Path("/var/log/az")
    assert trace.default_log_dir("report", {}) == Path("report", "logs")
    assert trace.default_log_dir(None, {"AZSQLCD_LOG_DIR": ""}) == Path(".azsqlcd", "logs")


def test_the_log_file_name_has_the_utc_time_and_the_command():
    now = datetime.fromisoformat("2026-10-07T16:03:11+02:00")
    assert trace.log_file_name("deploy", now) == "azsqlcd-20261007T140311Z-deploy.jsonl"
    assert (
        trace.log_file_name("setup-sql", datetime(2026, 1, 2, 3, 4, 5))
        == "azsqlcd-20260102T030405Z-setup-sql.jsonl"
    )
    assert trace.log_file_name("../x y", now) == "azsqlcd-20261007T140311Z----x-y.jsonl"  # never a path


def test_two_calls_in_one_second_get_two_files(tmp_path):
    now = datetime(2026, 10, 7, 14, 3, 11, tzinfo=UTC)
    first = trace.new_log_path(tmp_path, "deploy", now)
    assert first == tmp_path / "azsqlcd-20261007T140311Z-deploy.jsonl"
    first.write_text("{}")
    second = trace.new_log_path(tmp_path, "deploy", now)
    assert second == tmp_path / "azsqlcd-20261007T140311Z-deploy-2.jsonl"
    second.write_text("{}")
    assert trace.new_log_path(tmp_path, "deploy", now).name == "azsqlcd-20261007T140311Z-deploy-3.jsonl"


def test_the_latest_log_is_the_last_one_written_and_never_the_log_of_a_reader(tmp_path):
    import os

    assert trace.latest_log(tmp_path) is None and trace.latest_log(tmp_path / "missing") is None
    names = [
        "azsqlcd-20261007T140000Z-plan.jsonl",
        "azsqlcd-20261007T140500Z-deploy.jsonl",
        "azsqlcd-20261007T140900Z-show-log.jsonl",
        "azsqlcd-20261007T140901Z-support-bundle.jsonl",
        "azsqlcd-20261007T140901Z-show-log-2.jsonl",
        "notes.jsonl",
    ]
    for age, name in enumerate(names):
        (tmp_path / name).write_text("{}\n")
        os.utime(tmp_path / name, (1_800_000_000 + age, 1_800_000_000 + age))
    (tmp_path / "azsqlcd-20261007T150000Z-deploy.jsonl").mkdir()  # not a file
    assert trace.latest_log(tmp_path) == tmp_path / "azsqlcd-20261007T140500Z-deploy.jsonl"


# ------------------------------------------------------------------ what is sent
def a_failed_run(out: Path) -> Path:
    """The files of a deploy that failed in its third batch: <out>/logs/<log>, plan.json, report.json."""
    log = trace.Trace(out / "logs" / "azsqlcd-20261007T140311Z-deploy.jsonl")
    trace.header(log, command="deploy", argv=["deploy", "--env", "dev", "--target", "sales-dev"],
                 tool_version="0.1.0", tool_digest="ab12" * 16, environ={"GITHUB_RUN_ID": "991"})  # fmt: skip
    trace.config_event(
        log,
        project="sales",
        environment="dev",
        target="sales-dev",
        server="corp-dev.database.windows.net",
        database="sales",
        table_model=True,
    )
    failure = SqlError("Lock request time out period exceeded.", number=1222, cls=ErrorClass.LOCK_TIMEOUT)
    statement = "ALTER TABLE [sales].[Order] ADD [Status] varchar(10) NULL;"
    main = trace.open_traced(log, lambda: FakeSession({statement: failure}))
    parse = trace.open_traced(log, FakeSession)
    parse.execute("SET PARSEONLY ON;")
    parse.close()
    main.execute("/* azsqlcd:spid */ SELECT @@SPID;")
    main.execute("BEGIN TRANSACTION;")
    try:
        main.execute(statement)
    except SqlError as error:
        tool = ToolError(Exit.FAILED_ROLLED_BACK, "BATCH_FAILED", "step 0007__add_status.sql#1 failed")
        tool.__cause__ = error
        trace.exception_event(log, raised(tool))
    main.close()
    trace.end(log, exit_code=21, reason_code="BATCH_FAILED", message="step 0007__add_status.sql#1 failed")
    log.close()
    (out / "plan.json").write_text('{"plan_sha256": "p"}\n')
    (out / "report.json").write_text('{"reason_code": "BATCH_FAILED"}\n')
    assert log.path is not None
    return log.path


def test_the_bundle_holds_the_log_and_the_reports_and_nothing_else(tmp_path):
    out = tmp_path / "report"
    log = a_failed_run(out)
    (out / "manifest.json").write_text('{"release_seq": 57}\n')
    # what lies near a log and must stay at home
    (out / "bundle.tar").write_bytes(b"tar")
    (out / "0007__add_status.sql").write_text("ALTER TABLE x ADD SECRET_COLUMN int;")
    (out / "azsqlcd.toml").write_text('[project]\nname = "sales"\n')
    (out / "logs" / "debug.sql").write_text("SELECT 'SECRET';")
    (out / "logs" / "azsqlcd-20261007T130000Z-plan.jsonl").write_text("{}\n")  # another run

    names = trace.support_bundle(log, tmp_path / "send" / "bundle.zip", tool_digest="cd34" * 16)
    assert names == [log.name, "plan.json", "report.json", "manifest.json", "versions.txt"]
    with zipfile.ZipFile(tmp_path / "send" / "bundle.zip") as bundle:
        assert bundle.namelist() == names
        assert bundle.read(log.name) == log.read_bytes()
        assert bundle.read("report.json") == (out / "report.json").read_bytes()
        versions = bundle.read("versions.txt").decode()
    assert "tool_digest: " + "cd34" * 16 in versions  # of the tool that made the bundle
    assert "tool_digest: " + "ab12" * 16 in versions  # of the run that the log records
    assert "command: deploy" in versions and "python: 3." in versions and "mssql-python: " in versions

    with_config = trace.support_bundle(log, tmp_path / "b2.zip", [out / "azsqlcd.toml"])
    assert with_config == [
        log.name,
        "azsqlcd.toml",
        "plan.json",
        "report.json",
        "manifest.json",
        "versions.txt",
    ]


def test_the_bundle_takes_reports_only_from_the_out_directory_of_the_log(tmp_path):
    logs = tmp_path / "azsqlcd-logs"  # not named `logs`: the directory above it is not an --out
    logs.mkdir()
    log = logs / "azsqlcd-20261007T140311Z-lint.jsonl"
    log.write_text('{"kind": "end"}\n')
    (tmp_path / "manifest.json").write_text('{"name": "a web app, not a release"}')
    assert trace.support_bundle(log, tmp_path / "b.zip") == [log.name, "versions.txt"]


def test_the_bundle_refuses_a_large_file_and_a_path_that_is_not_a_regular_file(tmp_path):
    out = tmp_path / "report"
    log = a_failed_run(out)
    large = tmp_path / "large.json"
    with open(large, "wb") as file:
        file.truncate(trace.MAX_BUNDLE_FILE_BYTES + 1)
    link = tmp_path / "link.json"
    symlink_or_skip(link, out / "plan.json")
    for bad, why in ((large, "over 5 MB"), (tmp_path, "not a regular file"), (link, "not a regular file"),
                     (tmp_path / "missing.json", "not a regular file")):  # fmt: skip
        with pytest.raises(trace.BundleRefused, match=why):
            trace.support_bundle(log, tmp_path / "b.zip", [bad])
        with pytest.raises(trace.BundleRefused, match=why):
            trace.support_bundle(bad, tmp_path / "b.zip")
    assert not (tmp_path / "b.zip").exists()  # a refusal writes no zip

    # a report next to the log that is too large is left out, and the bundle says so
    with open(out / "plan.json", "wb") as file:
        file.truncate(trace.MAX_BUNDLE_FILE_BYTES + 1)
    assert trace.support_bundle(log, tmp_path / "b.zip") == [log.name, "report.json", "versions.txt"]
    with zipfile.ZipFile(tmp_path / "b.zip") as bundle:
        assert "left out:\n  plan.json (is over 5 MB)" in bundle.read("versions.txt").decode()


# ------------------------------------------------------------------ reading a log
def test_the_summary_names_the_last_batch_and_the_error(tmp_path):
    summary = trace.summarize(a_failed_run(tmp_path / "report"))
    lines = summary.splitlines()
    assert lines[0] == "log: azsqlcd-20261007T140311Z-deploy.jsonl"
    assert lines[1].startswith("command: azsqlcd deploy  (tool 0.1.0, digest ab12ab12ab12, python 3.")
    assert "argv: deploy --env dev --target sales-dev" in lines
    assert (
        "target: sales-dev (dev), database sales on corp-dev.database.windows.net, project sales, "
        "table_model true" in lines
    )
    assert "ci: GITHUB_RUN_ID=991" in lines
    assert any(re.fullmatch(r"batches: parse 1 \(\d+ ms\), main 3 \(\d+ ms\)", line) for line in lines)
    last = next(line for line in lines if line.startswith("last batch: "))
    assert last.startswith("last batch: main #3: ALTER TABLE [sales].[Order] (tag none, sha256 ")
    assert (
        "error in batch main #3 (ALTER TABLE [sales].[Order]): [LOCK_TIMEOUT 1222] Lock request time out "
        "period exceeded." in lines
    )
    assert any(
        re.fullmatch(r"exception: ToolError BATCH_FAILED at test_trace\.py:\d+ in raised", x) for x in lines
    )
    assert lines[-1] == "end: exit 21 BATCH_FAILED: step 0007__add_status.sql#1 failed"


def test_the_summary_of_a_process_that_was_killed_says_that_it_has_no_end(tmp_path):
    log, path = a_trace(tmp_path)
    trace.header(log, command="deploy", argv=["deploy"], tool_version="0.1.0", tool_digest="d" * 64)
    session = trace.open_traced(log, FakeSession)
    session.execute("/* azsqlcd:lock */ DECLARE @r int; EXEC @r = sys.sp_getapplock @Resource = N'x';")
    log.close()
    with open(path, "a", encoding="utf-8") as file:
        file.write('{"ts": "2026-10-07T14:03:12.000Z", "seq": 4, "kind": "bat')  # cut by the kill
    summary = trace.summarize(path)
    assert "last batch: main #1: DECLARE (tag lock, " in summary
    assert "  later statements of that batch: EXEC [sys].[sp_getapplock]" in summary
    assert "end: none. The process stopped before it wrote its exit code" in summary
    assert "target: not known" in summary
    assert "note: 1 line(s) of the file are not JSON and were skipped" in summary


def test_the_summary_of_a_file_that_is_not_a_log_does_not_fail(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text(
        'not json\n[1, 2]\n{"kind": "run", "argv": "odd", "ci": 5}\n{"kind": "exception", "stack": 7}\n'
    )
    assert "command: azsqlcd ?" in trace.summarize(path)
    path.write_text("")
    assert trace.summarize(path) == "x.jsonl: no event can be read (0 line(s) are not JSON)"
