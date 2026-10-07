"""The database session: token, connect policy, and the adapter over the driver.

No test here loads the driver. connect() is driven through an injected opener; the adapter is
driven through a scripted fake of the driver module. The last tests compare the facts that
session.py assumes with the installed driver, in a child process, and skip when it is absent.
"""

import json
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest

from azsqlcd import session
from azsqlcd.errors import Exit, ToolError
from azsqlcd.session import (
    SQL_COPT_SS_ACCESS_TOKEN,
    TOKEN_SCOPE,
    AccessToken,
    AzureCliTokenProvider,
    DriverSession,
    connect,
    connection_keywords,
    open_session,
    pack_token,
    require_token_life,
)
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error

NOW = 1_800_000_000.0
TRANSIENT_TEXT = "[Microsoft][SQL Server]Database 'sales' on server 'contoso' is not currently available."
LOCK_TIMEOUT_TEXT = "[Microsoft][SQL Server]Lock request time out period exceeded."
KEYWORDS = connection_keywords("contoso.database.windows.net", "sales", "azsqlcd/0.1.0 run=42")


# ------------------------------------------------------------------ fakes at the boundaries
class Tokens:
    """A token provider that mints a new token on every call."""

    def __init__(self, minutes_left: float = 60) -> None:
        self.minted: list[str] = []
        self._expires_on = int(NOW + minutes_left * 60)

    def get(self) -> AccessToken:
        self.minted.append(f"token-{len(self.minted) + 1}")
        return AccessToken(self.minted[-1], self._expires_on)


class Clock:
    """Monotonic time that moves only when the code sleeps or an attempt takes time."""

    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Opener:
    """Stands in for one connect attempt of the driver. Plays its outcomes in order; the last repeats."""

    def __init__(self, *outcomes, clock: Clock | None = None, attempt_takes: float = 0) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[dict, bytes]] = []
        self._clock, self._attempt_takes = clock, attempt_takes

    def __call__(self, keywords, token_struct):
        self.calls.append((dict(keywords), token_struct))
        if self._clock:
            self._clock.t += self._attempt_takes
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def transient() -> SqlError:
    return sql_error(TRANSIENT_TEXT, at_connect=True)


def connect_with(opener, tokens=None, clock=None, **kwargs):
    clock = clock or Clock()
    return connect(
        "contoso.database.windows.net",
        "sales",
        tokens or Tokens(),
        "azsqlcd/0.1.0 run=42",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        driver_connect=opener,
        now=lambda: NOW,
        **kwargs,
    )


class DriverError(Exception):
    """Has the two attributes of an mssql-python exception."""

    def __init__(self, driver_error: str, ddbc_error: str = "") -> None:
        super().__init__(f"Driver Error: {driver_error}; DDBC Error: {ddbc_error}")
        self.driver_error, self.ddbc_error = driver_error, ddbc_error


class DriverWarning(Exception):
    def __init__(self, driver_error: str, ddbc_error: str = "") -> None:
        super().__init__(driver_error)
        self.driver_error, self.ddbc_error = driver_error, ddbc_error


class FailsOnFetch:
    """A result set that has columns and breaks while its rows are read."""

    def __init__(self, error: Exception) -> None:
        self.error = error


class ScriptedCursor:
    """Plays one script per batch. A step is a list of rows (a result set), None (a statement with
    no result set), an exception (raised when the drain reaches it) or FailsOnFetch."""

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = scripts
        self._steps: list = []
        self.execute_calls: list[tuple] = []
        self.nextset_calls = 0
        self.description = None

    def _enter(self) -> None:
        step = self._steps[0]
        if isinstance(step, BaseException):
            raise step
        self.description = None if step is None else [("c", int, None, None, None, None, None)]

    def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        self._steps = list(self._scripts.pop(0)) or [None]
        self._enter()
        return self

    def fetchall(self):
        step = self._steps[0]
        if isinstance(step, FailsOnFetch):
            raise step.error
        return list(step)

    def nextset(self):
        self.nextset_calls += 1
        self._steps.pop(0)
        if not self._steps:
            self.description = None
            return False
        self._enter()
        return True


class ScriptedConnection:
    def __init__(self, scripts: list[list], close_error: Exception | None = None) -> None:
        self.cursor_object = ScriptedCursor(scripts)
        self.cursors_made = 0
        self.close_calls = 0
        self._close_error = close_error

    def cursor(self):
        self.cursors_made += 1
        return self.cursor_object

    def close(self):
        self.close_calls += 1
        if self._close_error:
            raise self._close_error


class FakeDriver:
    """The part of the mssql_python module that session.py uses."""

    Error = DriverError
    Warning = DriverWarning

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple] = []

    def pooling(self, **kwargs) -> None:
        self.calls.append(("pooling", kwargs))

    def connect(self, *args, **kwargs):
        self.calls.append(("connect", args, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def session_over(*scripts, close_error=None) -> tuple[DriverSession, ScriptedConnection]:
    connection = ScriptedConnection(list(scripts), close_error)
    return open_session(FakeDriver(connection), KEYWORDS, b"packed"), connection


# ------------------------------------------------------------------ token
def test_the_token_is_packed_as_length_then_utf16le_bytes():
    packed = pack_token("eyJ0€")
    assert packed[:4] == struct.pack("<I", 10)
    assert packed[4:].decode("utf-16-le") == "eyJ0€"
    assert SQL_COPT_SS_ACCESS_TOKEN == 1256


def test_the_token_is_not_in_the_repr_of_an_access_token():
    assert "s3cret-jwt" not in repr(AccessToken("s3cret-jwt", 123))


def test_a_token_with_too_little_life_stops_before_connect():
    opener = Opener(object())
    with pytest.raises(ToolError) as stop:
        connect_with(opener, Tokens(minutes_left=9.5), min_token_minutes=10)
    assert (stop.value.exit_code, stop.value.reason_code) == (Exit.RETRY_SAFE, "TOKEN_TOO_SHORT")
    assert stop.value.detail == {"minutes_left": 9, "min_minutes": 10}
    assert opener.calls == []


def test_a_token_with_exactly_the_needed_life_connects():
    live = object()
    assert connect_with(Opener(live), Tokens(minutes_left=10), min_token_minutes=10) is live


def test_an_expired_token_stops_even_when_no_life_is_asked_for():
    with pytest.raises(ToolError) as stop:
        require_token_life(AccessToken("t", int(NOW) - 1), 0, NOW)
    assert stop.value.reason_code == "TOKEN_TOO_SHORT"


def test_the_token_of_a_later_attempt_is_checked_too():
    class Aging(Tokens):
        def get(self) -> AccessToken:
            token = super().get()
            return AccessToken(token.token, int(NOW + (11 if len(self.minted) == 1 else 9) * 60))

    opener = Opener(transient())
    with pytest.raises(ToolError) as stop:
        connect_with(opener, Aging(), min_token_minutes=10)
    assert stop.value.reason_code == "TOKEN_TOO_SHORT"
    assert len(opener.calls) == 1


def test_the_azure_cli_provider_asks_for_the_database_scope():
    asked = []

    def get_token(*scopes):
        asked.append(scopes)
        return SimpleNamespace(token="jwt", expires_on=1_800_003_600)

    token = AzureCliTokenProvider(SimpleNamespace(get_token=get_token)).get()
    assert asked == [(TOKEN_SCOPE,)] == [("https://database.windows.net/.default",)]
    assert (token.token, token.expires_on) == ("jwt", 1_800_003_600)


def test_the_default_credential_gives_the_azure_cli_time_to_start(monkeypatch):
    """N4-16. azure-identity waits 10 seconds for az by default. On Windows az.cmd starts a Python
    program through cmd; a cold start with a virus scan takes longer, and the run ends 24
    TOKEN_UNAVAILABLE with a login that is in order."""
    made = []

    class AzureCliCredential:
        def __init__(self, **kwargs):
            made.append(kwargs)

        def get_token(self, *scopes):
            return SimpleNamespace(token="jwt", expires_on=1_800_003_600)

    monkeypatch.setitem(sys.modules, "azure", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "azure.identity", SimpleNamespace(AzureCliCredential=AzureCliCredential))
    provider = AzureCliTokenProvider()
    assert provider.get().token == "jwt"
    assert made == [{"process_timeout": session.AZ_PROCESS_TIMEOUT_S}]
    assert session.AZ_PROCESS_TIMEOUT_S >= 60
    provider.get()
    assert len(made) == 1  # one credential for the life of the provider


def test_a_token_that_cannot_be_minted_is_a_safe_stop():
    def get_token(*scopes):
        raise RuntimeError("Please run 'az login'\nto set up an account")

    with pytest.raises(ToolError) as stop:
        AzureCliTokenProvider(SimpleNamespace(get_token=get_token)).get()
    assert (stop.value.exit_code, stop.value.reason_code) == (Exit.RETRY_SAFE, "TOKEN_UNAVAILABLE")
    assert "RuntimeError: Please run 'az login' to set up an account" in stop.value.message


# ------------------------------------------------------------------ connect policy
def test_the_connection_keywords_are_those_of_the_design():
    assert list(KEYWORDS.items()) == [
        ("Server", "contoso.database.windows.net"),
        ("Database", "sales"),
        ("Encrypt", "yes"),
        ("TrustServerCertificate", "no"),
        ("ConnectRetryCount", "0"),
        ("APP", "azsqlcd/0.1.0 run=42"),
    ]


@pytest.mark.parametrize("bad", ["", "sales\x00;Encrypt=no"])
def test_a_keyword_value_that_the_driver_cannot_carry_is_refused(bad):
    with pytest.raises(ValueError):
        connection_keywords("contoso.database.windows.net", bad, "azsqlcd")


def test_connect_hands_the_keywords_and_the_packed_token_to_the_driver():
    opener, live = Opener(object()), None
    live = connect_with(opener)
    assert live is opener.outcomes[0]
    assert opener.calls == [(KEYWORDS, pack_token("token-1"))]


def test_a_transient_error_at_connect_is_retried_inside_the_budget_and_then_stops_with_a_safe_exit():
    clock, opener = Clock(), Opener(transient())
    with pytest.raises(ToolError) as stop:
        connect_with(opener, clock=clock)
    assert clock.sleeps == [5, 10, 20, 40, 60]
    assert len(opener.calls) == 6
    assert (stop.value.exit_code, stop.value.reason_code) == (Exit.RETRY_SAFE, "CONNECT_FAILED")
    assert stop.value.detail == {
        "attempts": 6,
        "error_class": "TRANSIENT_CONNECT",
        "error_number": 40613,
        "sqlstate": None,
    }


def test_a_wait_that_would_end_after_the_budget_is_not_started():
    clock = Clock()
    opener = Opener(transient(), clock=clock, attempt_takes=20)
    with pytest.raises(ToolError) as stop:
        connect_with(opener, clock=clock)
    # attempts end at 20, 45, 75, 115 and 175 s; the wait of 60 s would end at 235 s
    assert clock.sleeps == [5, 10, 20, 40]
    assert len(opener.calls) == 5
    assert clock.t - 100.0 <= session.CONNECT_BUDGET_S == 180
    assert stop.value.reason_code == "CONNECT_FAILED"


def test_a_transient_error_that_clears_gives_the_session():
    clock, live = Clock(), object()
    opener = Opener(transient(), transient(), live)
    assert connect_with(opener, clock=clock) is live
    assert clock.sleeps == [5, 10]


@pytest.mark.parametrize(
    "error",
    [
        sql_error(
            "[Microsoft][SQL Server]Login failed for user '<token-identified principal>'.", at_connect=True
        ),
        sql_error("[Microsoft]Login timeout expired", sqlstate="HYT00", at_connect=True),
        sql_error("[Microsoft]TCP Provider: Error code 0x2746", sqlstate="08001", at_connect=True),
        sql_error("[Microsoft][SQL Server]The database 'sales' has reached its size quota.", at_connect=True),
    ],
)
def test_an_error_at_connect_that_is_not_transient_is_not_retried(error):
    clock, opener = Clock(), Opener(error)
    with pytest.raises(ToolError) as stop:
        connect_with(opener, clock=clock)
    assert (len(opener.calls), clock.sleeps) == (1, [])
    assert (stop.value.exit_code, stop.value.reason_code) == (Exit.RETRY_SAFE, "CONNECT_FAILED")
    assert stop.value.detail["error_class"] == error.cls.name


def test_a_fresh_token_is_fetched_for_every_attempt():
    tokens, opener = Tokens(), Opener(transient(), transient(), object())
    connect_with(opener, tokens)
    assert tokens.minted == ["token-1", "token-2", "token-3"]
    assert [call[1] for call in opener.calls] == [pack_token(t) for t in tokens.minted]


def test_the_token_never_appears_in_an_exception_message():
    # a driver that echoes what it was given: the worst case
    def leaky(keywords, token_struct):
        token = token_struct[4:].decode("utf-16-le")
        raise sql_error(f"[Microsoft]Cannot authenticate with token {token}", at_connect=True)

    with pytest.raises(ToolError) as stop:
        connect_with(leaky)
    shown = f"{stop.value} {stop.value.message} {stop.value.detail!r} {stop.value.args!r}"
    assert "token-1" not in shown
    assert "<token>" in stop.value.message
    assert stop.value.__cause__ is None and stop.value.__context__ is None


def test_the_connection_string_never_appears_in_an_exception_message():
    with pytest.raises(ToolError) as stop:
        connect_with(Opener(transient()))
    shown = f"{stop.value} {stop.value.detail!r}"
    for part in ("contoso", "Encrypt", "TrustServerCertificate", "azsqlcd/0.1.0"):
        assert part not in shown


def test_an_exception_that_is_not_a_database_error_is_not_turned_into_a_retry():
    clock, opener = Clock(), Opener(RuntimeError("defect"))
    with pytest.raises(RuntimeError):
        connect_with(opener, clock=clock)
    assert (len(opener.calls), clock.sleeps) == (1, [])


def test_importing_the_session_module_loads_no_driver():
    code = (
        "import sys, azsqlcd.session, azsqlcd.sqlerrors; "
        "sys.exit(any(m.split('.')[0] in ('mssql_python', 'azure') for m in sys.modules))"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# ------------------------------------------------------------------ the driver connect
def test_the_connection_is_dedicated_autocommit_and_carries_the_token():
    driver = FakeDriver(ScriptedConnection([]))
    open_session(driver, KEYWORDS, b"packed-token")
    assert driver.calls[0] == ("pooling", {"enabled": False})  # before the connect, or the driver pools
    name, args, kwargs = driver.calls[1]
    assert name == "connect"
    assert kwargs == {"autocommit": True, "attrs_before": {1256: b"packed-token"}}  # and no timeout
    assert args == (
        "Server={contoso.database.windows.net};Database={sales};Encrypt={yes};"
        "TrustServerCertificate={no};ConnectRetryCount={0}",
    )


def test_the_application_name_is_not_sent_because_this_driver_reserves_the_keyword():
    # Design gap, reported: mssql-python 1.15.0 rejects APP and sends APP=MSSQL-Python itself.
    # When the driver accepts APP, the driver-facts test below fails and this omission must go.
    driver = FakeDriver(ScriptedConnection([]))
    open_session(driver, KEYWORDS, b"t")
    assert "APP" not in driver.calls[1][1][0]
    assert "azsqlcd/0.1.0" not in driver.calls[1][1][0]


def test_a_brace_in_a_name_cannot_end_the_value_in_the_connection_string():
    driver = FakeDriver(ScriptedConnection([]))
    open_session(driver, connection_keywords("s", "a}b;Encrypt=no", "app"), b"t")
    assert "Database={a}}b;Encrypt=no};" in driver.calls[1][1][0]


HOSTILE_NAMES = [
    "sales;Encrypt=no",
    "sales};Encrypt=no;TrustServerCertificate=yes;x={",
    "sales}",
    "}};Encrypt={no",
    "{sales;Encrypt=no",
    "sales;Server=evil.example.net",
    " sales = x ",
]


def read_odbc_string(text: str) -> list[tuple[str, str]]:
    """(keyword, value) pairs of a connection string, by the ODBC rule: a value in braces ends at
    the first } that is not doubled; any other value ends at the next ;."""
    pairs, at = [], 0
    while at < len(text):
        equals = text.index("=", at)
        keyword, at = text[at:equals], equals + 1
        if text.startswith("{", at):
            value, at = "", at + 1
            while True:
                end = text.index("}", at)
                value += text[at:end]
                if text.startswith("}}", end):
                    value, at = value + "}", end + 2
                else:
                    at = end + 1
                    break
            assert at == len(text) or text[at] == ";", "text after a closed brace is not a value"
        else:
            end = text.find(";", at)
            value, at = text[at:] if end < 0 else text[at:end], len(text) if end < 0 else end
        pairs.append((keyword, value))
        at += 1
    return pairs


def sent_connection_string(server: str, database: str) -> str:
    driver = FakeDriver(ScriptedConnection([]))
    open_session(driver, connection_keywords(server, database, "app"), b"t")
    return driver.calls[1][1][0]


@pytest.mark.parametrize("hostile", HOSTILE_NAMES)
def test_a_database_name_with_a_semicolon_or_a_brace_cannot_add_a_connection_keyword(hostile):
    """The database name comes from azsqlcd.toml, which refuses only control characters in it."""
    assert read_odbc_string(sent_connection_string("contoso.database.windows.net", hostile)) == [
        ("Server", "contoso.database.windows.net"),
        ("Database", hostile),
        ("Encrypt", "yes"),  # the security keywords are the ones of the tool, once each
        ("TrustServerCertificate", "no"),
        ("ConnectRetryCount", "0"),
    ]


@pytest.mark.parametrize("hostile", HOSTILE_NAMES)
def test_a_server_name_with_a_semicolon_or_a_brace_cannot_add_a_connection_keyword(hostile):
    # azsqlcd.toml refuses such a server; the session does not depend on that
    assert read_odbc_string(sent_connection_string(hostile, "sales")) == [
        ("Server", hostile),
        ("Database", "sales"),
        ("Encrypt", "yes"),
        ("TrustServerCertificate", "no"),
        ("ConnectRetryCount", "0"),
    ]


def test_a_driver_error_at_connect_is_classified_for_the_retry():
    driver = FakeDriver(DriverError("General error", TRANSIENT_TEXT))
    with pytest.raises(SqlError) as raised:
        open_session(driver, KEYWORDS, b"t")
    assert (raised.value.number, raised.value.cls) == (40613, ErrorClass.TRANSIENT_CONNECT)
    assert raised.value.__cause__ is None  # the raw driver text is not chained into a traceback


def test_connect_retries_through_the_adapter_of_the_real_opener():
    # the same wiring as production, with the fake driver module in place of mssql_python
    attempts = []
    live = ScriptedConnection([[[(1,)]]])

    def opener(keywords, token_struct):
        attempts.append(token_struct)
        outcome = DriverError("General error", TRANSIENT_TEXT) if len(attempts) < 3 else live
        return open_session(FakeDriver(outcome), keywords, token_struct)

    clock = Clock()
    db = connect_with(opener, clock=clock)
    assert clock.sleeps == [5, 10]
    assert db.execute("SELECT 1") == [[(1,)]]


# ------------------------------------------------------------------ the adapter: execute and drain
def test_every_result_set_is_drained_and_returned_in_order():
    db, connection = session_over([[(1, "a"), (2, "b")], None, [], [(3,)]])
    assert db.execute("SELECT ...") == [[(1, "a"), (2, "b")], [], [(3,)]]
    assert connection.cursor_object.nextset_calls == 4  # to the end, also past a statement with no rows


def test_a_batch_without_a_result_set_returns_no_result_set():
    db, _ = session_over([None, None])
    assert db.execute("SET NOCOUNT ON; UPDATE t SET c = 1") == []


def test_rows_come_back_as_tuples():
    db, _ = session_over([[[1, "a"]]])
    assert db.execute("SELECT 1, 'a'") == [[(1, "a")]]


def test_an_error_in_a_later_result_set_is_raised_not_swallowed():
    late = DriverError("Syntax error or access violation", LOCK_TIMEOUT_TEXT)
    db, connection = session_over([[(1,)], None, late, [(2,)]])
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1; UPDATE a ...; UPDATE b ...; SELECT 2")
    assert (raised.value.number, raised.value.cls) == (1222, ErrorClass.LOCK_TIMEOUT)
    assert connection.cursor_object.nextset_calls == 2
    assert not db.closed


def test_an_error_of_the_first_statement_is_raised():
    db, _ = session_over(
        [DriverError("Base table or view not found", "[Microsoft][SQL Server]Invalid object name 'x'.")]
    )
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT * FROM x")
    assert (raised.value.number, raised.value.cls) == (208, ErrorClass.OTHER)


def test_an_error_while_rows_are_read_is_raised():
    broken = FailsOnFetch(
        DriverError("Division by zero", "[Microsoft][SQL Server]Divide by zero error encountered.")
    )
    db, _ = session_over([[(1,)], broken])
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1; SELECT 1 / c FROM t")
    assert raised.value.message == "[Microsoft][SQL Server]Divide by zero error encountered."


def test_a_driver_warning_raised_as_an_exception_is_a_database_error_too():
    db, _ = session_over([DriverWarning("General warning", "[Microsoft][SQL Server]Something odd.")])
    with pytest.raises(SqlError):
        db.execute("SELECT 1")


def test_the_batch_text_reaches_the_driver_unchanged_and_without_parameters():
    batch = "SELECT '?', '{d 2020-01-01}', [a?b] -- {call x} ?\r\n/* {fn now()} */"
    db, connection = session_over([None])
    db.execute(batch)
    assert connection.cursor_object.execute_calls == [((batch,), {})]


def test_one_cursor_serves_every_batch():
    db, connection = session_over([[(1,)]], [[(2,)]])
    assert (db.execute("SELECT 1"), db.execute("SELECT 2")) == ([[(1,)]], [[(2,)]])
    assert connection.cursors_made == 1


def test_an_error_after_connect_is_never_retried():
    driver = FakeDriver(ScriptedConnection([[DriverError("General error", TRANSIENT_TEXT)]]))
    db = open_session(driver, KEYWORDS, b"t")
    with pytest.raises(SqlError) as raised:
        db.execute("UPDATE t SET c = 1")
    assert raised.value.cls is ErrorClass.OTHER  # the same text is transient only at connect
    assert raised.value.number == 40613
    assert len(driver.outcome.cursor_object.execute_calls) == 1  # the batch was sent once
    assert [call[0] for call in driver.calls].count("connect") == 1  # and nothing reconnected


def test_a_stored_error_message_holds_no_value_and_the_raw_text_stays_available():
    text = (
        "[Microsoft][SQL Server]Cannot insert duplicate key in object 'dbo.t'. "
        "The duplicate key value is (4711)."
    )
    db, _ = session_over([DriverError("Integrity constraint violation", text)])
    with pytest.raises(SqlError) as raised:
        db.execute("INSERT ...")
    assert "4711" not in str(raised.value) and "dbo.t" not in str(raised.value)
    assert raised.value.raw_message == text


def test_an_exception_that_is_not_from_the_driver_passes_through_unchanged():
    # the caller then treats it as a tool defect (A1); it must not look like an engine error
    db, _ = session_over([RuntimeError("native layer")])
    with pytest.raises(RuntimeError):
        db.execute("SELECT 1")


# ------------------------------------------------------------------ the adapter: lost connection, close
@pytest.mark.parametrize(
    ("label", "sqlstate"),
    [
        ("Communication link failure", "08S01"),
        ("Connection not open", "08003"),
        ("Connection failure during transaction", "08007"),
        ("Statement completion unknown", "40003"),
    ],
)
def test_a_lost_connection_closes_the_session(label, sqlstate):
    lost = DriverError(label, "[Microsoft]TCP Provider: An existing connection was forcibly closed.")
    db, connection = session_over([[(1,)], lost], [[(2,)]])
    with pytest.raises(SqlError) as raised:
        db.execute("COMMIT TRANSACTION")
    assert (raised.value.cls, raised.value.sqlstate) == (ErrorClass.SESSION_LOST, sqlstate)
    assert db.closed
    assert connection.close_calls == 1


def test_a_session_that_is_killed_while_the_client_waits_is_lost():
    # live spike X5 (T5): KILL during WAITFOR ...; COMMIT gives the label of HY000 and this text, no 08 code
    killed = DriverError(
        "General error",
        "[Microsoft]Unspecified error occurred on SQL Server. Connection may have been terminated by the "
        "server.",
    )
    db, connection = session_over([killed], [[(2,)]])
    with pytest.raises(SqlError) as raised:
        db.execute("WAITFOR DELAY '00:00:15'; COMMIT TRANSACTION;")
    assert (raised.value.cls, raised.value.sqlstate) == (ErrorClass.SESSION_LOST, None)
    assert db.closed
    assert connection.close_calls == 1


def test_a_general_error_with_another_text_does_not_close_the_session():
    db, _ = session_over([DriverError("General error", "[Microsoft][SQL Server]azsqlcd spike")], [[(2,)]])
    with pytest.raises(SqlError) as raised:
        db.execute("RAISERROR(N'azsqlcd spike', 16, 1);")
    assert raised.value.cls is ErrorClass.OTHER
    assert not db.closed


def test_nothing_is_sent_on_a_closed_session():
    db, connection = session_over([DriverError("Communication link failure", "[Microsoft]gone")], [[(2,)]])
    with pytest.raises(SqlError):
        db.execute("SELECT 1")
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 2")
    assert raised.value.cls is ErrorClass.SESSION_LOST
    assert len(connection.cursor_object.execute_calls) == 1


def test_a_sqlstate_that_the_driver_does_not_know_is_read_from_its_label():
    odd = DriverError(
        "An error occurred with SQLSTATE code: S0002", "[Microsoft][SQL Server]Invalid object name 'x'."
    )
    db, _ = session_over([odd])
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT * FROM x")
    assert raised.value.sqlstate == "S0002"


def test_an_error_with_an_unknown_label_has_no_sqlstate_and_keeps_the_session():
    db, _ = session_over([DriverError("Syntax error or access violation", "[Microsoft][SQL Server]Boom.")])
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1")
    assert (raised.value.sqlstate, raised.value.cls, db.closed) == (None, ErrorClass.OTHER, False)


def test_close_is_idempotent():
    db, connection = session_over()
    db.close()
    db.close()
    assert (db.closed, connection.close_calls) == (True, 1)


def test_close_never_raises_because_no_decision_follows_it():
    db, connection = session_over(close_error=RuntimeError("SQLSTATE:08S01:link down"))
    db.close()
    assert (db.closed, connection.close_calls) == (True, 1)


# ------------------------------------------------------------------ facts of the installed driver
_DRIVER_FACTS = """
import inspect, json, sys
try:
    import mssql_python
    from mssql_python.connection_string_parser import _ConnectionStringParser
    from mssql_python.exceptions import ConnectionStringParseError, raise_exception, sqlstate_to_exception
    from mssql_python.auth import AADAuth
    from mssql_python.constants import ConstantsDDBC
    from mssql_python.pooling import PoolingManager
except ImportError:
    sys.exit(3)
labels, text, hostile = json.loads(sys.argv[1])
pool_before_connect = [PoolingManager.is_initialized(), PoolingManager.is_enabled()]
mssql_python.pooling(enabled=False)
parser = _ConnectionStringParser(validate_keywords=True)
try:
    parser._parse("Server=s;Database=d;APP=azsqlcd")
    app = "accepted"
except ConnectionStringParseError as exc:
    app = str(exc)
error = sqlstate_to_exception("42000", "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Boom.")
try:
    raise_exception("S0002", "x")
except mssql_python.Error as exc:
    unmapped = exc.driver_error
print(json.dumps({
    "version": mssql_python.__version__,
    "labels": {state: sqlstate_to_exception(state, "x").driver_error for state in labels},
    "app": app,
    "parsed": parser._parse(text),
    "hostile": [parser._parse(one) for one in hostile],
    "connect": list(inspect.signature(mssql_python.connect).parameters),
    "execute": list(inspect.signature(mssql_python.Cursor.execute).parameters),
    "pool_before_connect": pool_before_connect,
    "pool_after_switch_off": [PoolingManager.is_initialized(), PoolingManager.is_enabled()],
    "token_struct": AADAuth.get_token_struct("eyJ0\u20ac").hex(),
    "token_attribute": ConstantsDDBC.SQL_COPT_SS_ACCESS_TOKEN.value,
    "error": [error.driver_error, error.ddbc_error, hasattr(error, "sqlstate")],
    "caught": isinstance(error, mssql_python.Error) and issubclass(mssql_python.Warning, Exception),
    "unmapped": unmapped,
    "pooling": list(inspect.signature(mssql_python.pooling).parameters),
    "cancel": hasattr(mssql_python.Cursor, "cancel"),
}))
"""


@pytest.fixture(scope="module")
def driver_facts():
    driver = FakeDriver(ScriptedConnection([]))
    open_session(driver, KEYWORDS, b"t")
    states = sorted(session._SQLSTATE_BY_LABEL.values())
    hostile = [sent_connection_string("contoso.database.windows.net", name) for name in HOSTILE_NAMES]
    ran = subprocess.run(
        [sys.executable, "-c", _DRIVER_FACTS, json.dumps([states, driver.calls[1][1][0], hostile])],
        capture_output=True,
        text=True,
        encoding="utf-8",  # the child prints JSON in ASCII; never the code page of the machine
        errors="replace",
        check=False,
    )
    if ran.returncode == 3:
        pytest.skip("mssql-python is not installed (extra 'db')")
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout.strip().splitlines()[-1])


def test_driver_labels_of_the_connection_sqlstates_are_the_ones_the_adapter_reads(driver_facts):
    assert {label: state for state, label in driver_facts["labels"].items()} == session._SQLSTATE_BY_LABEL


def test_driver_exceptions_hold_label_and_text_and_no_sqlstate(driver_facts):
    assert driver_facts["error"] == [
        "Syntax error or access violation",
        "[Microsoft][SQL Server]Boom.",
        False,
    ]
    assert driver_facts["caught"] is True
    assert session._UNMAPPED_SQLSTATE.findall(driver_facts["unmapped"]) == ["S0002"]


def test_driver_still_reserves_the_application_name(driver_facts):
    assert "Reserved keyword 'app'" in driver_facts["app"]


def test_driver_reads_our_connection_string_back_to_the_same_keywords(driver_facts):
    assert driver_facts["parsed"] == {
        "server": "contoso.database.windows.net",
        "database": "sales",
        "encrypt": "yes",
        "trustservercertificate": "no",
        "connectretrycount": "0",
    }


def test_driver_reads_a_hostile_database_name_as_one_value_and_no_new_keyword(driver_facts):
    assert driver_facts["hostile"] == [
        {
            "server": "contoso.database.windows.net",
            "database": name,
            "encrypt": "yes",
            "trustservercertificate": "no",
            "connectretrycount": "0",
        }
        for name in HOSTILE_NAMES
    ]


def test_driver_packs_a_token_as_we_do_and_reads_it_from_the_same_attribute(driver_facts):
    assert driver_facts["token_struct"] == pack_token("eyJ0€").hex()
    assert driver_facts["token_attribute"] == SQL_COPT_SS_ACCESS_TOKEN


def test_driver_api_that_the_adapter_calls_is_there(driver_facts):
    assert {"connection_str", "autocommit", "attrs_before"} <= set(driver_facts["connect"])
    assert driver_facts["execute"][:3] == ["self", "operation", "parameters"]
    assert "enabled" in driver_facts["pooling"]
    # the driver starts pooling at the first connect unless it was switched off before
    assert driver_facts["pool_before_connect"] == [False, False]
    assert driver_facts["pool_after_switch_off"] == [True, False]
    assert driver_facts["cancel"] is False  # no cancel: stop = close the connection


def test_the_installed_azure_identity_takes_the_az_timeout_that_the_provider_passes():
    # N4-16: the keyword is passed by name; a release of azure-identity without it would fail every token
    code = (
        "import sys\n"
        "try:\n"
        "    from azure.identity import AzureCliCredential\n"
        "except ImportError:\n"
        "    sys.exit(3)\n"
        "AzureCliCredential(process_timeout=int(sys.argv[1]))\n"
    )
    ran = subprocess.run(
        [sys.executable, "-c", code, str(session.AZ_PROCESS_TIMEOUT_S)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if ran.returncode == 3:
        pytest.skip("azure-identity is not installed (extra 'db')")
    assert ran.returncode == 0, ran.stderr


# ------------------------------------------------------------------ live finding: TOKEN_TOO_SHORT
def test_a_read_only_command_needs_two_minutes_of_token_life_and_not_the_configured_minimum():
    """Live runs: the Azure CLI gives out its cached token until about five minutes before the end
    of that token, and min_token_minutes = 20 of the template refused plan, drift and export."""
    live = object()
    assert connect_with(Opener(live), Tokens(minutes_left=3), min_token_minutes=20, read_only=True) is live
    with pytest.raises(ToolError) as stop:
        connect_with(Opener(live), Tokens(minutes_left=1.5), min_token_minutes=20, read_only=True)
    assert stop.value.detail == {"minutes_left": 1, "min_minutes": 2}
    # a project that asks for less than two minutes gets what it asks for
    assert connect_with(Opener(live), Tokens(minutes_left=1.5), min_token_minutes=1, read_only=True) is live
    require_token_life(AccessToken("t", int(NOW) + 180), 20, NOW, read_only=True)


def test_a_write_command_keeps_the_configured_minimum_and_the_message_says_what_to_do():
    with pytest.raises(ToolError) as stop:
        connect_with(Opener(object()), Tokens(minutes_left=9.5), min_token_minutes=20)
    assert stop.value.reason_code == "TOKEN_TOO_SHORT"
    message = stop.value.message
    assert "the access token expires in 9 minutes; this command needs 20" in message
    assert (
        "the Azure CLI token expires in 9 minutes; wait 9 minutes and run the command again, or sign in "
        "again with az login to get a new token"
    ) in message
    assert "In a workflow: start the job again" in message


def test_a_token_with_too_little_life_is_asked_for_once_more_before_the_stop():
    class Renewed(Tokens):
        def get(self) -> AccessToken:
            token = super().get()
            return AccessToken(token.token, int(NOW + (3 if len(self.minted) == 1 else 30) * 60))

    tokens, live = Renewed(), object()
    assert connect_with(Opener(live), tokens, min_token_minutes=20) is live
    assert len(tokens.minted) == 2
    short = Tokens(minutes_left=3)
    with pytest.raises(ToolError):
        connect_with(Opener(live), short, min_token_minutes=20)
    assert len(short.minted) == 2  # once more, and no loop
