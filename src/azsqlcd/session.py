"""The database session. This is the only module that imports the driver, and it does so lazily.

One dedicated connection, not pooled, autocommit, no client statement timeout. A batch is sent
with no parameters and every result set is drained. Only the connect is retried, only on a
transient error, inside a fixed budget, with a fresh token for every attempt. No batch is ever
sent twice by this module. The token and the connection string are never put in a message.

Facts of mssql-python 1.15.0 that this module depends on (read from the driver source):
  - connect() turns connection pooling on unless pooling(enabled=False) was called before;
  - the keyword APP is reserved: the driver rejects it and always sends APP=MSSQL-Python;
  - an exception holds driver_error (the label of the SQLSTATE) and ddbc_error (the text of the
    first diagnostic record). There is no SQLSTATE attribute and no engine error number;
  - execute() with no parameters hands the text to SQLExecDirect; the Python layer does not scan
    it. The driver does not set SQL_ATTR_NOSCAN, so what the ODBC layer does with { } escape
    clauses is not proven (gate G-D3);
  - the driver log is off by default. Switched on (setup_logging), it writes batch text.
"""

from __future__ import annotations

import contextlib
import re
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

from azsqlcd.errors import ToolError, refused, retry_safe
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error

SQL_COPT_SS_ACCESS_TOKEN = 1256
TOKEN_SCOPE = "https://database.windows.net/.default"
CONNECT_WAITS_S = (5, 10, 20, 40, 60)
CONNECT_BUDGET_S = 180
# azure-identity waits 10 s for az by default. On Windows az.cmd starts a Python program through
# cmd; a cold start takes longer and would end as TOKEN_UNAVAILABLE with a login that is in order.
AZ_PROCESS_TIMEOUT_S = 60
REDACTED_TOKEN = "<token>"

ResultSets = list[list[tuple[Any, ...]]]


class Session(Protocol):
    @property
    def closed(self) -> bool: ...

    def execute(self, batch: str) -> ResultSets:
        """Send one batch with no parameters and drain every result set.

        Returns the rows of each result set that has columns, in order. An error of any statement
        of the batch, also one that surfaces only in the drain, is raised as SqlError.
        """
        ...

    def close(self) -> None: ...


# ------------------------------------------------------------------ token
@dataclass(frozen=True)
class AccessToken:
    token: str = field(repr=False)
    expires_on: int  # POSIX seconds


class TokenProvider(Protocol):
    def get(self) -> AccessToken: ...


class AzureCliTokenProvider:
    """Token of the identity that is logged in to the Azure CLI (azure/login with OIDC, or az login)."""

    def __init__(self, credential: Any = None) -> None:
        self._credential = credential  # an azure-identity credential; AzureCliCredential when None

    def get(self) -> AccessToken:
        credential = self._credential
        if credential is None:
            try:
                from azure.identity import AzureCliCredential  # pyright: ignore[reportMissingImports]
            except ImportError as exc:
                raise _extra_missing("azure-identity") from exc
            credential = self._credential = AzureCliCredential(process_timeout=AZ_PROCESS_TIMEOUT_S)
        try:
            got = credential.get_token(TOKEN_SCOPE)
        except Exception as exc:
            # Every failure to mint a token is the same decision: nothing was sent, stop cleanly.
            reason = " ".join(str(exc).split())[:300]
            raise retry_safe(
                "TOKEN_UNAVAILABLE", f"no access token for the database: {type(exc).__name__}: {reason}"
            ) from exc
        return AccessToken(got.token, int(got.expires_on))


# A command that only reads ends in seconds. The Azure CLI on a workstation gives out its cached
# token until about five minutes before the end of that token, so the configured minimum (20 in
# the template) refused the first plan, drift and export of a new user (live runs).
READ_ONLY_TOKEN_MINUTES = 2


def needed_token_minutes(min_minutes: int, *, read_only: bool = False) -> int:
    return min(min_minutes, READ_ONLY_TOKEN_MINUTES) if read_only else min_minutes


def require_token_life(token: AccessToken, min_minutes: int, now: float, *, read_only: bool = False) -> None:
    """Stop cleanly when the token expires in less than the needed minutes. now is POSIX seconds.

    Needed: min_minutes, and for a command that sends no batch that writes (read_only) at most
    READ_ONLY_TOKEN_MINUTES.
    """
    needed = needed_token_minutes(min_minutes, read_only=read_only)
    seconds_left = token.expires_on - now
    if seconds_left < needed * 60:
        minutes_left = int(seconds_left // 60)
        wait = max(minutes_left, 1)  # the Azure CLI gives a new token when the old one is at its end
        raise retry_safe(
            "TOKEN_TOO_SHORT",
            f"the access token expires in {minutes_left} minutes; this command needs {needed}. On a "
            f"workstation: the Azure CLI token expires in {minutes_left} minutes; wait {wait} minutes and "
            "run the command again, or sign in again with az login to get a new token. In a workflow: "
            "start the job again",
            minutes_left=minutes_left,
            min_minutes=needed,
        )


def pack_token(token: str) -> bytes:
    """The value of SQL_COPT_SS_ACCESS_TOKEN: 4-byte little-endian length, then UTF-16-LE bytes."""
    raw = token.encode("utf-16-le")
    return struct.pack("<I", len(raw)) + raw


# ------------------------------------------------------------------ connect
# Opens one session from connection keywords and a packed token. Raises SqlError.
DriverConnect = Callable[[Mapping[str, str], bytes], Session]


def connection_keywords(server: str, database: str, app_name: str) -> dict[str, str]:
    """The connection keywords of the design (Part 2 (e)), in a fixed order."""
    keywords = {
        "Server": server,
        "Database": database,
        "Encrypt": "yes",
        "TrustServerCertificate": "no",
        "ConnectRetryCount": "0",
        "APP": app_name,
    }
    for key, value in keywords.items():
        if not value or "\x00" in value:
            raise ValueError(f"connection keyword {key} needs a value without NUL")
    return keywords


def connect(
    server: str,
    database: str,
    token_provider: TokenProvider,
    app_name: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    driver_connect: DriverConnect | None = None,
    min_token_minutes: int = 0,
    now: Callable[[], float] = time.time,
    read_only: bool = False,
) -> Session:
    """Open the one session of a command.

    Every attempt gets a fresh token, which must have min_token_minutes of life left (read_only:
    at most READ_ONLY_TOKEN_MINUTES, see require_token_life). A token with less life is asked for
    once more before the stop: a provider that can renew gives a new one. A transient
    error is retried after 5, 10, 20, 40 and 60 seconds while the next wait still ends inside the
    180-second budget. Any other error, and the end of the budget, is exit 24 CONNECT_FAILED: the
    database is untouched.
    """
    keywords = connection_keywords(server, database, app_name)
    if driver_connect is None:
        driver_connect = partial(open_session, _load_driver())
    started = monotonic()
    waits = iter(CONNECT_WAITS_S)
    attempts = 0
    while True:
        token = token_provider.get()
        if token.expires_on - now() < needed_token_minutes(min_token_minutes, read_only=read_only) * 60:
            token = token_provider.get()  # once more; the check below stops when this one is short too
        require_token_life(token, min_token_minutes, now(), read_only=read_only)
        attempts += 1
        try:
            return driver_connect(keywords, pack_token(token.token))
        except SqlError as exc:
            error = exc
        wait = next(waits, None) if error.cls is ErrorClass.TRANSIENT_CONNECT else None
        if wait is None or monotonic() - started + wait > CONNECT_BUDGET_S:
            # The driver has no reason to echo the token. If it ever does, it stops here.
            text = str(error).replace(token.token, REDACTED_TOKEN)
            raise retry_safe(
                "CONNECT_FAILED",
                f"no connection after {attempts} attempt(s): {text}",
                attempts=attempts,
                error_class=error.cls.name,
                error_number=error.number,
                sqlstate=error.sqlstate,
            )
        sleep(wait)


# ------------------------------------------------------------------ driver adapter (mssql-python)
def _extra_missing(package: str) -> ToolError:
    return refused("DRIVER_MISSING", f"{package} is not installed; database commands need the 'db' extra")


def _load_driver() -> Any:
    try:
        import mssql_python  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise _extra_missing("mssql-python") from exc
    return mssql_python


def open_session(driver: Any, keywords: Mapping[str, str], token_struct: bytes) -> DriverSession:
    """One connect attempt with mssql-python. driver is the module (or a fake of it in tests)."""
    # The driver pools by default. A pooled close() keeps the server session alive, with its
    # applock and its open transaction, and gives it to the next connect.
    driver.pooling(enabled=False)
    # APP cannot be sent: this driver reserves the keyword (see the module docstring).
    text = ";".join(f"{key}={_braced(value)}" for key, value in keywords.items() if key != "APP")
    try:
        connection = driver.connect(
            text, autocommit=True, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}
        )
    except (driver.Error, driver.Warning) as exc:
        raise _driver_error(exc, at_connect=True) from None
    return DriverSession(driver, connection)


def _braced(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


class DriverSession:
    """A Session over one mssql-python connection. Only this class touches driver objects."""

    def __init__(self, driver: Any, connection: Any) -> None:
        self._driver_errors = (driver.Error, driver.Warning)
        self._connection = connection
        self._cursor: Any = None
        self.closed = False

    def execute(self, batch: str) -> ResultSets:
        if self.closed:
            raise SqlError("azsqlcd: the session is closed", cls=ErrorClass.SESSION_LOST)
        try:
            if self._cursor is None:
                self._cursor = self._connection.cursor()
            cursor = self._cursor
            cursor.execute(batch)
            result_sets: ResultSets = []
            while True:
                if cursor.description is not None:
                    result_sets.append([tuple(row) for row in cursor.fetchall()])
                if not cursor.nextset():
                    return result_sets
        except self._driver_errors as exc:
            error = _driver_error(exc, at_connect=False)
            if error.cls is ErrorClass.SESSION_LOST:
                self.close()
            raise error from None

    def close(self) -> None:
        """Close the connection. An open transaction is rolled back by the engine. Never raises."""
        if self.closed:
            return
        self.closed = True
        # Closing is the last act on a session; an error here can change no decision. A dead
        # connection makes the driver raise RuntimeError from its native layer.
        with contextlib.suppress(Exception):
            self._connection.close()


_UNMAPPED_SQLSTATE = re.compile(r"SQLSTATE code: ([0-9A-Z]{5})")
# The driver turns a SQLSTATE into an exception class and a fixed label and drops the code
# (mssql_python.exceptions.sqlstate_to_exception). These labels give back the codes that mean
# the connection is gone.
_SQLSTATE_BY_LABEL = {
    "Client unable to establish connection": "08001",
    "Connection name in use": "08002",
    "Connection not open": "08003",
    "Server rejected the connection": "08004",
    "Connection failure during transaction": "08007",
    "Communication link failure": "08S01",
    "Statement completion unknown": "40003",
}

# A session that is killed while the client waits for a batch: the driver raises the label
# "General error" (HY000) with this text of the ODBC layer, and no 08 SQLSTATE (live spike X5).
# The next batch on the connection then raises "Communication link failure".
_LOST_WHILE_WAITING = re.compile(r"Connection may have been terminated by the server", re.IGNORECASE)


def _driver_error(exc: BaseException, *, at_connect: bool) -> SqlError:
    label = str(getattr(exc, "driver_error", ""))
    text = str(getattr(exc, "ddbc_error", "")) or label or str(exc)
    unmapped = _UNMAPPED_SQLSTATE.search(label)
    sqlstate = unmapped.group(1) if unmapped else _SQLSTATE_BY_LABEL.get(label)
    # The text of this driver never holds the engine number, so sql_error reads only known messages.
    error = sql_error(text, sqlstate=sqlstate, at_connect=at_connect)
    if error.cls is ErrorClass.OTHER and not at_connect and _LOST_WHILE_WAITING.search(text):
        return SqlError(text, number=error.number, sqlstate=sqlstate, cls=ErrorClass.SESSION_LOST)
    return error
