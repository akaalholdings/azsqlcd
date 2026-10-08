"""The database session. This is the only module that imports the driver, and it does so lazily.

One dedicated connection, not pooled, autocommit, no client statement timeout. A batch is sent
with no parameters and every result set is drained. Only the connect is retried, only on a
transient error, inside a fixed budget, with a fresh token for every attempt. No batch is ever
sent twice by this module. The token and the connection string are never put in a message.

Three ways to sign in, chosen by the variable AZSQLCD_AUTH (credential_from_environment):
  entra             the token of the Azure CLI session (the default);
  managed-identity  the token of the managed identity of the machine, with no Azure CLI;
  sql               SQL authentication, for a workstation. The login and the password come only
                    from the environment. They are in no repr and in no message, and the text of
                    every driver error passes this module with both taken out. Refused in GitHub
                    Actions, at every way in: the environment, a login of a caller, connect().

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
import os
import re
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol

from azsqlcd.errors import ToolError, refused, retry_safe
from azsqlcd.sqlerrors import HIDDEN, ErrorClass, SignInFilter, SqlError, sql_error

SQL_COPT_SS_ACCESS_TOKEN = 1256
TOKEN_SCOPE = "https://database.windows.net/.default"
CONNECT_WAITS_S = (5, 10, 20, 40, 60)
CONNECT_BUDGET_S = 180
# azure-identity waits 10 s for az by default. On Windows az.cmd starts a Python program through
# cmd; a cold start takes longer and would end as TOKEN_UNAVAILABLE with a login that is in order.
AZ_PROCESS_TIMEOUT_S = 60
REDACTED_TOKEN = "<token>"

AUTH_VARIABLE = "AZSQLCD_AUTH"
MANAGED_IDENTITY_CLIENT_ID_VARIABLE = "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID"
SQL_USER_VARIABLE = "AZSQLCD_SQL_USER"
SQL_PASSWORD_VARIABLE = "AZSQLCD_SQL_PASSWORD"
AUTH_ENTRA, AUTH_MANAGED_IDENTITY, AUTH_SQL = "entra", "managed-identity", "sql"
AUTH_KINDS = (AUTH_ENTRA, AUTH_MANAGED_IDENTITY, AUTH_SQL)
HIDDEN_LOGIN = HIDDEN
# The password policy of Azure SQL Database: at least 8 characters. A shorter value is a mistake,
# and taken out of a text (sqlerrors.SignInFilter) it would damage the words that hold it.
MIN_PASSWORD_LENGTH = 8
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

ResultSets = list[list[tuple[Any, ...]]]


class Session(Protocol):
    @property
    def closed(self) -> bool: ...

    def execute(self, batch: str) -> ResultSets:
        """Send one batch with no parameters and drain every result set.

        Returns the rows of each result set that has columns, in order. An error of any statement
        of the batch, also one that surfaces only in the drain, is raised as SqlError. Its message
        keeps a name as the statement wrote it only for the batch that wrote the name
        (SqlError.for_batch).
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
            return AccessToken(got.token, int(got.expires_on))
        except Exception as exc:
            # Every failure to mint a token is the same decision: nothing was sent, stop cleanly.
            reason = _token_failure(exc)
        # Raised outside the handler: the error of the identity library, whose text can hold a
        # token, is not chained to this one.
        raise retry_safe("TOKEN_UNAVAILABLE", f"no access token for the database: {reason}")


class ManagedIdentityTokenProvider:
    """Token of the managed identity of this machine, asked at its identity endpoint. No Azure CLI.

    client_id: the client id of a user-assigned identity. None: the system-assigned identity.
    """

    def __init__(self, client_id: str | None = None, credential: Any = None) -> None:
        self.client_id = client_id
        self._credential = credential  # an azure-identity credential; ManagedIdentityCredential when None

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"  # the kind of sign-in is told, never the client id

    def get(self) -> AccessToken:
        credential = self._credential
        if credential is None:
            try:
                from azure.identity import (  # pyright: ignore[reportMissingImports]
                    ManagedIdentityCredential,
                )
            except ImportError as exc:
                raise _extra_missing("azure-identity") from exc
            options = {} if self.client_id is None else {"client_id": self.client_id}
            credential = self._credential = ManagedIdentityCredential(**options)
        try:
            got = credential.get_token(TOKEN_SCOPE)
            return AccessToken(got.token, int(got.expires_on))
        except Exception as exc:
            reason = _token_failure(exc, self.client_id).rstrip(".")
        # Raised outside the handler: the error of the identity library, whose text can name the
        # client id or hold a token, is not chained to this one.
        raise retry_safe(
            "TOKEN_UNAVAILABLE",
            f"no access token for the database from the managed identity ({AUTH_VARIABLE}="
            f"{AUTH_MANAGED_IDENTITY}): {reason}. Check: this machine has a managed identity; "
            f"{MANAGED_IDENTITY_CLIENT_ID_VARIABLE}, when it is set, is the client id of a user-assigned "
            "identity that is assigned to this machine; the identity has a user in the database",
        )


# A JWT, whole or cut: base64url text that starts with eyJ (the start of a JSON object), and the
# parts that follow it after a dot.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*){0,2}")


def _token_failure(exc: BaseException, client_id: str | None = None) -> str:
    """'<type>: <text>' of an error of the identity library, in one line of at most 300 characters
    of text. A token in the text is written as <token>, and the client id, with or without its
    dashes, as <client id>: a library or an endpoint that echoes a response puts both there."""
    reason = _JWT.sub(REDACTED_TOKEN, " ".join(str(exc).split()))
    if client_id is not None:
        with_or_without_dashes = "-?".join(re.escape(part) for part in client_id.split("-"))
        reason = re.sub(with_or_without_dashes, "<client id>", reason, flags=re.IGNORECASE)
    return f"{type(exc).__name__}: {reason[:300]}"


def _is_control(character: str) -> bool:
    return ord(character) < 32 or 127 <= ord(character) < 160


def check_sql_login(user: str, password: str) -> None:
    """Refuse a login or a password that cannot be passed to the driver safely. No message holds a
    part of either value.

    Raises ToolError REFUSED: SQL_AUTH_MISSING (empty), AUTH_INVALID (NUL or another control
    character: the ODBC layer ends a connection string at NUL, and no login needs a line break;
    a password of fewer than MIN_PASSWORD_LENGTH characters).
    """
    for variable, value in ((SQL_USER_VARIABLE, user), (SQL_PASSWORD_VARIABLE, password)):
        if not value:
            raise refused(
                "SQL_AUTH_MISSING",
                f"{AUTH_VARIABLE}={AUTH_SQL} needs the login in {SQL_USER_VARIABLE} and the password in "
                f"{SQL_PASSWORD_VARIABLE}: {variable} is not set or is empty",
                variable=variable,
            )
        if any(_is_control(character) for character in value):
            raise refused(
                "AUTH_INVALID",
                f"the value of {variable} holds a control character (NUL, a line break, a tab or another "
                "one). It cannot be passed to the driver safely. No connection was tried",
                variable=variable,
            )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise refused(
            "AUTH_INVALID",
            f"the value of {SQL_PASSWORD_VARIABLE} has fewer than {MIN_PASSWORD_LENGTH} characters. Azure "
            "SQL Database accepts no such password: check that the variable holds the password. No "
            "connection was tried",
            variable=SQL_PASSWORD_VARIABLE,
        )


@dataclass(frozen=True)
class SqlLogin:
    """SQL authentication: a login and its password. Neither is in repr() or str()."""

    user: str = field(repr=False)
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        check_sql_login(self.user, self.password)


# What connect() and the runner sign in with: a provider of access tokens, or a SQL login.
Credential = TokenProvider | SqlLogin


def auth_kind(credential: Credential) -> str:
    """entra, managed-identity or sql: the kind of sign-in, for the plan, the report and the log."""
    if isinstance(credential, SqlLogin):
        return AUTH_SQL
    return AUTH_MANAGED_IDENTITY if isinstance(credential, ManagedIdentityTokenProvider) else AUTH_ENTRA


def in_github_actions(environ: Mapping[str, str], *, ci_github: bool = False) -> bool:
    """True when the command runs in a job of GitHub Actions, by what the job says of itself:
    the command has --ci github, GITHUB_ACTIONS is "true" or "1" (any case, spaces around it
    dropped), or GITHUB_RUN_ID is set. Two variables, so that a step that sets one of them to
    another value is still a job."""
    said = environ.get("GITHUB_ACTIONS", "").strip().casefold()
    return ci_github or said in ("true", "1") or bool(environ.get("GITHUB_RUN_ID", "").strip())


def refuse_sql_in_github_actions(environ: Mapping[str, str], *, ci_github: bool = False) -> None:
    """Stop when the command runs in GitHub Actions (in_github_actions): SQL authentication is for
    a workstation. This guards against a mistake. It is no control against the author of a
    workflow, who can remove the variables that it reads.

    Raises ToolError REFUSED AUTH_INVALID. The message holds no value of a variable.
    """
    if in_github_actions(environ, ci_github=ci_github):
        raise refused(
            "AUTH_INVALID",
            f"{AUTH_VARIABLE}={AUTH_SQL} is refused in GitHub Actions (the variable GITHUB_ACTIONS is "
            "true, the variable GITHUB_RUN_ID is set, or the command has --ci github). A workflow signs "
            f"in with OIDC ({AUTH_ENTRA}) or with a managed identity ({AUTH_MANAGED_IDENTITY}); SQL "
            "authentication is for a workstation",
            variable=AUTH_VARIABLE,
        )


def credential_from_environment(environ: Mapping[str, str], *, ci_github: bool = False) -> Credential:
    """The sign-in that AZSQLCD_AUTH names. Not set, empty or "entra": the Azure CLI session.

    ci_github: the command has --ci github. No message holds the value of a variable.
    Raises ToolError REFUSED: AUTH_INVALID (another value; a client id that is not a GUID; "sql" in
    GitHub Actions; a control character in the login or the password; a password of fewer than
    MIN_PASSWORD_LENGTH characters), SQL_AUTH_MISSING.
    """
    kind = environ.get(AUTH_VARIABLE) or AUTH_ENTRA
    if kind == AUTH_ENTRA:
        return AzureCliTokenProvider()
    if kind == AUTH_MANAGED_IDENTITY:
        client_id = environ.get(MANAGED_IDENTITY_CLIENT_ID_VARIABLE) or None
        if client_id is not None and not _GUID.fullmatch(client_id):
            raise refused(
                "AUTH_INVALID",
                f"{MANAGED_IDENTITY_CLIENT_ID_VARIABLE} must be a GUID: the client id of a user-assigned "
                "managed identity. Leave it unset for the system-assigned identity of the machine",
                variable=MANAGED_IDENTITY_CLIENT_ID_VARIABLE,
            )
        return ManagedIdentityTokenProvider(client_id)
    if kind == AUTH_SQL:
        # before the login is read: a workflow that has no login gets the same answer
        refuse_sql_in_github_actions(environ, ci_github=ci_github)
        return SqlLogin(environ.get(SQL_USER_VARIABLE, ""), environ.get(SQL_PASSWORD_VARIABLE, ""))
    raise refused(
        "AUTH_INVALID",
        f"{AUTH_VARIABLE} must be one of: {', '.join(AUTH_KINDS)} (exact, lower case). Not set or empty "
        f"means {AUTH_ENTRA}",
        variable=AUTH_VARIABLE,
    )


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
# Opens one session from connection keywords and a packed token. Raises SqlError. The token is
# None for SQL authentication: the keywords then hold UID and PWD.
DriverConnect = Callable[[Mapping[str, str], bytes | None], Session]


def connection_keywords(
    server: str, database: str, app_name: str, login: SqlLogin | None = None
) -> dict[str, str]:
    """The connection keywords of the design (Part 2 (e)), in a fixed order. With a SQL login:
    the same keywords, so the same encryption, then UID and PWD."""
    keywords = {
        "Server": server,
        "Database": database,
        "Encrypt": "yes",
        "TrustServerCertificate": "no",
        "ConnectRetryCount": "0",
        "APP": app_name,
    }
    if login is not None:
        keywords |= {"UID": login.user, "PWD": login.password}
    for key, value in keywords.items():
        if not value or "\x00" in value:
            raise ValueError(f"connection keyword {key} needs a value without NUL")
    return keywords


# A failed login, as the engine (18456) and the driver (SQLSTATE 28000) tell it.
_LOGIN_FAILURE = re.compile(r"login failed|invalid authorization specification|password", re.IGNORECASE)


def connect(
    server: str,
    database: str,
    token_provider: Credential,
    app_name: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    driver_connect: DriverConnect | None = None,
    min_token_minutes: int = 0,
    now: Callable[[], float] = time.time,
    read_only: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Session:
    """Open the one session of a command. token_provider: a provider of tokens, or a SqlLogin.

    Every attempt gets a fresh token, which must have min_token_minutes of life left (read_only:
    at most READ_ONLY_TOKEN_MINUTES, see require_token_life). A token with less life is asked for
    once more before the stop: a provider that can renew gives a new one. A transient
    error is retried after 5, 10, 20, 40 and 60 seconds while the next wait still ends inside the
    180-second budget. Any other error, and the end of the budget, is exit 24 CONNECT_FAILED: the
    database is untouched.

    A SqlLogin has no token: no token is asked for, and a token life is not applicable. The
    message of a failed SQL login holds the error number and the class, never the text of the
    driver, which can echo the login. A SqlLogin opens no session in GitHub Actions, whoever made
    it (refuse_sql_in_github_actions; environ: the variables that are read, those of the process
    when None): 22 AUTH_INVALID, and nothing is asked of the driver.
    """
    login = token_provider if isinstance(token_provider, SqlLogin) else None
    if login is not None:
        refuse_sql_in_github_actions(os.environ if environ is None else environ)
    keywords = connection_keywords(server, database, app_name, login)
    if driver_connect is None:
        driver_connect = partial(open_session, _load_driver())
    started = monotonic()
    waits = iter(CONNECT_WAITS_S)
    attempts = 0
    while True:
        token = None
        if not isinstance(token_provider, SqlLogin):
            token = token_provider.get()
            if token.expires_on - now() < needed_token_minutes(min_token_minutes, read_only=read_only) * 60:
                token = token_provider.get()  # once more; the check below stops when this one is short too
            require_token_life(token, min_token_minutes, now(), read_only=read_only)
        attempts += 1
        try:
            return driver_connect(keywords, None if token is None else pack_token(token.token))
        except SqlError as exc:
            error = exc
        wait = next(waits, None) if error.cls is ErrorClass.TRANSIENT_CONNECT else None
        if wait is None or monotonic() - started + wait > CONNECT_BUDGET_S:
            if login is None:
                # The driver has no reason to echo the token. If it ever does, it stops here.
                text = str(error).replace(_have_token(token).token, REDACTED_TOKEN)
            elif (
                error.number == 18456 or error.sqlstate == "28000" or _LOGIN_FAILURE.search(error.raw_message)
            ):
                number = "no error number" if error.number is None else f"error {error.number}"
                text = (
                    f"{number}, class {error.cls.name}; the driver message is not shown for SQL "
                    f"authentication. Check the login in {SQL_USER_VARIABLE}, the password in "
                    f"{SQL_PASSWORD_VARIABLE}, and that the login has a user in the database"
                )
            else:
                # taken out of the full text, then redacted: redact() cuts a text at a quote
                raw = SignInFilter(login.user, login.password).hide_echo(error.raw_message)
                text = str(SqlError(raw, number=error.number, sqlstate=error.sqlstate, cls=error.cls))
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


def _have_token(token: AccessToken | None) -> AccessToken:
    if token is None:
        raise RuntimeError("a sign-in with a token has no token")
    return token


def open_session(driver: Any, keywords: Mapping[str, str], token_struct: bytes | None) -> DriverSession:
    """One connect attempt with mssql-python. driver is the module (or a fake of it in tests).

    token_struct None: SQL authentication with UID and PWD of the keywords. Every value is in
    braces with } doubled, so no character of a login or a password ends its value or adds a
    keyword. The text of an error of this session holds neither of the two.
    """
    # The driver pools by default. A pooled close() keeps the server session alive, with its
    # applock and its open transaction, and gives it to the next connect.
    driver.pooling(enabled=False)
    # APP cannot be sent: this driver reserves the keyword (see the module docstring).
    text = ";".join(f"{key}={_braced(value)}" for key, value in keywords.items() if key != "APP")
    hidden = SignInFilter(keywords.get("UID", ""), keywords.get("PWD", ""))
    options: dict[str, Any] = {"autocommit": True}
    if token_struct is not None:
        options["attrs_before"] = {SQL_COPT_SS_ACCESS_TOKEN: token_struct}
    try:
        return DriverSession(driver, driver.connect(text, **options), hidden)
    except (driver.Error, driver.Warning) as exc:
        error = _driver_error(exc, at_connect=True, hidden=hidden)
    # Raised outside the handler: the exception of the driver, whose text is not redacted and can
    # hold a login, is neither the cause nor the context of this one.
    raise error


def _braced(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


class DriverSession:
    """A Session over one mssql-python connection. Only this class touches driver objects."""

    def __init__(self, driver: Any, connection: Any, hidden: SignInFilter | None = None) -> None:
        self._driver_errors = (driver.Error, driver.Warning)
        self._connection = connection
        # takes the login and the password of SQL authentication out of an error text
        self._hidden = hidden or SignInFilter()
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
            # for_batch: a name of the statement stays in the message when this batch wrote it
            error = _driver_error(exc, at_connect=False, hidden=self._hidden).for_batch(batch)
        if error.cls is ErrorClass.SESSION_LOST:
            self.close()
        raise error  # outside the handler: the exception of the driver is not chained (open_session)

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


def _driver_error(exc: BaseException, *, at_connect: bool, hidden: SignInFilter | None = None) -> SqlError:
    label = str(getattr(exc, "driver_error", ""))
    text = str(getattr(exc, "ddbc_error", "")) or label or str(exc)
    unmapped = _UNMAPPED_SQLSTATE.search(label)
    sqlstate = unmapped.group(1) if unmapped else _SQLSTATE_BY_LABEL.get(label)
    # The text of this driver never holds the engine number, so sql_error reads only known messages.
    error = sql_error(text, sqlstate=sqlstate, at_connect=at_connect)
    cls = error.cls
    if cls is ErrorClass.OTHER and not at_connect and _LOST_WHILE_WAITING.search(text):
        cls = ErrorClass.SESSION_LOST
    if hidden is None:
        hidden = SignInFilter()
    if cls is error.cls and not hidden:
        return error
    # The number and the class are read from the full text; then the login and the password go,
    # and with them the rest of a text that echoes the connection string.
    return SqlError(hidden.hide_echo(text), number=error.number, sqlstate=sqlstate, cls=cls)
