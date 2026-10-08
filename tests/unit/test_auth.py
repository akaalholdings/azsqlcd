"""The three ways to sign in: AZSQLCD_AUTH = entra (the default), managed-identity or sql.

No test here opens a connection, asks a real identity endpoint or starts the Azure CLI. The session
module is driven through a fake of the driver module and a fake of azure.identity; the command line
is driven through cli.main with a FakeSession. The last tests compare what session.py assumes with
the installed packages, in a child process, and skip when they are absent.

The marker login and the marker password of this file are in the environment of a call, as on a
workstation. No file that a command writes and no stream may hold either of them.
"""

import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from azsqlcd import cli, session, trace
from azsqlcd.errors import Exit, ToolError
from azsqlcd.session import (
    AccessToken,
    AzureCliTokenProvider,
    ManagedIdentityTokenProvider,
    SqlLogin,
    auth_kind,
    connect,
    connection_keywords,
    credential_from_environment,
    open_session,
)
from azsqlcd.sqlerrors import ErrorClass, SqlError, sql_error
from unit import test_onboard as onboard_db
from unit.test_cli import (
    ENGINE_MESSAGE,
    FLAGS,
    Sessions,
    a_deploy,
    a_drift,
    a_failing_deploy,
    a_plan,
    a_plan_doc,
    an_existing_database,
    ci_files,
    events_of,
    on_disk,
    target,
    write,
)
from unit.test_cli import (
    Tokens as CliTokens,
)
from unit.test_resolve import ACTIONS
from unit.test_runner import NOW, TOML, Tokens, a_migration, failure, parse_only_session, run_deploy

MARKER_USER = "deploy_login_Zq7"
MARKER_PASSWORD = "Pw-9x;}{=K3y!marker"  # the characters that end a value or start a keyword in ODBC
CLIENT_ID = "0a1b2c3d-1111-2222-3333-444455556666"
SERVER, DATABASE, APP = "sql-example.database.windows.net", "sales", "azsqlcd/0.1.0 run=42"
LOGIN_FAILED = f"[Microsoft][SQL Server]Login failed for user '{MARKER_USER}'."


WORKFLOW_VARIABLES = ("GITHUB_ACTIONS", "GITHUB_RUN_ID")  # what a job of GitHub Actions says of itself


def sql_environment(**more: str) -> dict[str, str]:
    return {
        "AZSQLCD_AUTH": "sql",
        "AZSQLCD_SQL_USER": MARKER_USER,
        "AZSQLCD_SQL_PASSWORD": MARKER_PASSWORD,
    } | more


@pytest.fixture
def sql_auth(monkeypatch) -> None:
    """A workstation that signs in with SQL authentication: the three variables, and no workflow."""
    for name, value in sql_environment().items():
        monkeypatch.setenv(name, value)
    for name in WORKFLOW_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_workflow(monkeypatch) -> None:
    """The tests of this file read the environment as a workstation gives it, also in a CI run."""
    for name in (*WORKFLOW_VARIABLES, "AZSQLCD_AUTH", "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    for name in ("AZSQLCD_SQL_USER", "AZSQLCD_SQL_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def refusal(environ: dict[str, str], **options: Any) -> ToolError:
    with pytest.raises(ToolError) as stop:
        credential_from_environment(environ, **options)
    return stop.value


def shown(error: BaseException) -> str:
    """Every text of an error that a caller, a log or a traceback can show."""
    parts = [str(error), repr(error), repr(error.args), repr(getattr(error, "detail", None))]
    parts += [str(getattr(error, name, "")) for name in ("message", "raw_message")]
    for chained in (error.__cause__, error.__context__):
        if chained is not None:
            parts.append(shown(chained))
    return " ".join(parts)


def assert_no_marker(text: str) -> None:
    assert MARKER_USER not in text
    assert MARKER_PASSWORD not in text


# ------------------------------------------------------------------ the choice
@pytest.mark.parametrize("environ", [{}, {"AZSQLCD_AUTH": ""}, {"AZSQLCD_AUTH": "entra"}])
def test_without_the_variable_the_sign_in_is_the_token_of_the_azure_cli_as_before(environ):
    credential = credential_from_environment(environ)
    assert type(credential) is AzureCliTokenProvider
    assert auth_kind(credential) == "entra"


def test_the_names_of_the_variables_and_of_the_three_values_are_fixed():
    assert session.AUTH_VARIABLE == "AZSQLCD_AUTH"
    assert session.MANAGED_IDENTITY_CLIENT_ID_VARIABLE == "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID"
    assert (session.SQL_USER_VARIABLE, session.SQL_PASSWORD_VARIABLE) == (
        "AZSQLCD_SQL_USER",
        "AZSQLCD_SQL_PASSWORD",
    )
    assert session.AUTH_KINDS == ("entra", "managed-identity", "sql")


@pytest.mark.parametrize("value", ["SQL", "Entra", " sql", "sql ", "msi", "oidc", "managed_identity", "0"])
def test_any_other_value_is_refused_and_the_message_lists_the_three_values(value):
    error = refusal({"AZSQLCD_AUTH": value})
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "AUTH_INVALID")
    assert "AZSQLCD_AUTH" in error.message and "entra, managed-identity, sql" in error.message


def test_a_wrong_value_is_not_echoed():
    # a value of a variable is never printed: a password can be pasted into the wrong one
    error = refusal({"AZSQLCD_AUTH": MARKER_PASSWORD})
    assert_no_marker(shown(error))


def test_managed_identity_takes_the_system_identity_or_the_client_id_of_a_user_assigned_one():
    system = credential_from_environment({"AZSQLCD_AUTH": "managed-identity"})
    assert type(system) is ManagedIdentityTokenProvider and system.client_id is None
    empty = credential_from_environment(
        {"AZSQLCD_AUTH": "managed-identity", "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID": ""}
    )
    assert empty.client_id is None  # a workflow gives an empty value when the row has none
    given = credential_from_environment(
        {"AZSQLCD_AUTH": "managed-identity", "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID": CLIENT_ID}
    )
    assert given.client_id == CLIENT_ID
    assert auth_kind(system) == auth_kind(given) == "managed-identity"
    assert CLIENT_ID not in repr(given)


@pytest.mark.parametrize("bad", ["not-a-guid", CLIENT_ID + "0", CLIENT_ID[:-1], f"{{{CLIENT_ID}}}", " "])
def test_a_client_id_that_is_not_a_guid_is_refused_and_not_echoed(bad):
    error = refusal({"AZSQLCD_AUTH": "managed-identity", "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID": bad})
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "AUTH_INVALID")
    assert "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID" in error.message and "GUID" in error.message
    assert bad.strip() == "" or bad not in shown(error)


def test_the_client_id_is_not_read_for_another_sign_in():
    credential = credential_from_environment({"AZSQLCD_MANAGED_IDENTITY_CLIENT_ID": "not-a-guid"})
    assert type(credential) is AzureCliTokenProvider


def test_sql_takes_the_login_and_the_password_of_the_environment():
    credential = credential_from_environment(sql_environment())
    assert type(credential) is SqlLogin and auth_kind(credential) == "sql"
    assert (credential.user, credential.password) == (MARKER_USER, MARKER_PASSWORD)


@pytest.mark.parametrize(
    ("missing", "value"),
    [
        ("AZSQLCD_SQL_USER", None),
        ("AZSQLCD_SQL_USER", ""),
        ("AZSQLCD_SQL_PASSWORD", None),
        ("AZSQLCD_SQL_PASSWORD", ""),
    ],
)
def test_sql_without_a_login_or_a_password_names_the_variable_and_never_a_value(missing, value):
    environ = sql_environment()
    if value is None:
        del environ[missing]
    else:
        environ[missing] = value
    error = refusal(environ)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "SQL_AUTH_MISSING")
    assert missing in error.message
    assert_no_marker(shown(error))


@pytest.mark.parametrize(
    ("environ", "options"),
    [
        (sql_environment(GITHUB_ACTIONS="true"), {}),
        (sql_environment(), {"ci_github": True}),
        # refused before the login is read: a workflow that has no login gets the same answer
        ({"AZSQLCD_AUTH": "sql", "GITHUB_ACTIONS": "true"}, {}),
    ],
)
def test_sql_is_refused_in_github_actions_and_no_override_exists(environ, options):
    error = refusal(environ, **options)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "AUTH_INVALID")
    assert "GitHub Actions" in error.message and "managed-identity" in error.message
    assert "workstation" in error.message
    assert_no_marker(shown(error))


@pytest.mark.parametrize("value", ["false", "", "0", "False", "no"])
def test_a_machine_that_says_it_is_no_workflow_is_a_workstation(value):
    assert type(credential_from_environment(sql_environment(GITHUB_ACTIONS=value))) is SqlLogin
    assert type(credential_from_environment(sql_environment(GITHUB_RUN_ID=""))) is SqlLogin


@pytest.mark.parametrize(
    "more",
    [
        {"GITHUB_ACTIONS": "True"},
        {"GITHUB_ACTIONS": "TRUE"},
        {"GITHUB_ACTIONS": " true"},
        {"GITHUB_ACTIONS": "true "},
        {"GITHUB_ACTIONS": "1"},
        {"GITHUB_RUN_ID": "900"},  # a step that runs `GITHUB_ACTIONS=false azsqlcd ...`
        {"GITHUB_ACTIONS": "false", "GITHUB_RUN_ID": "900"},
        {"GITHUB_ACTIONS": "", "GITHUB_RUN_ID": "900"},
    ],
)
def test_sql_is_refused_for_every_way_a_job_of_github_actions_shows_itself(more):
    """The value of GITHUB_ACTIONS is read without case and without the spaces around it, and the
    run id of the job counts too: one variable that a step sets to another value opens nothing."""
    error = refusal(sql_environment(**more))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "AUTH_INVALID")
    assert "GITHUB_ACTIONS" in error.message and "GITHUB_RUN_ID" in error.message
    assert_no_marker(shown(error))
    assert session.in_github_actions(more) and not session.in_github_actions({})
    assert session.in_github_actions({}, ci_github=True)


@pytest.mark.parametrize("more", [{"GITHUB_ACTIONS": "true"}, {"GITHUB_RUN_ID": "900"}])
def test_connect_opens_no_sql_session_in_a_workflow_whoever_made_the_login(more):
    # the refusal does not hang on the function that read the environment
    driver = FakeDriver()
    with pytest.raises(ToolError) as stop:
        connect(
            SERVER,
            DATABASE,
            SqlLogin(MARKER_USER, MARKER_PASSWORD),
            APP,
            driver_connect=lambda keywords, token: open_session(driver, keywords, token),
            environ=more,
        )
    assert (stop.value.exit_code, stop.value.reason_code) == (Exit.REFUSED, "AUTH_INVALID")
    assert driver.calls == []
    assert_no_marker(shown(stop.value))


def test_connect_reads_the_environment_of_the_process_for_a_sql_login(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    driver = FakeDriver()
    with pytest.raises(ToolError) as stop:
        sql_connect(driver)
    assert stop.value.reason_code == "AUTH_INVALID" and driver.calls == []
    # a token sign-in is what a workflow uses: it is not refused
    opened = connect(
        SERVER,
        DATABASE,
        Tokens(),
        APP,
        driver_connect=lambda keywords, token: open_session(driver, keywords, token),
        now=lambda: NOW,
    )
    assert len(driver.calls) == 1 and not opened.closed


def test_the_other_sign_ins_are_not_refused_in_github_actions():
    for kind in ("entra", "managed-identity"):
        environ = {"AZSQLCD_AUTH": kind, "GITHUB_ACTIONS": "true"}
        assert auth_kind(credential_from_environment(environ, ci_github=True)) == kind


# ------------------------------------------------------------------ managed identity
class FakeIdentity:
    """Stands in for the module azure.identity: counts what is made, and gives a fixed token."""

    def __init__(self, error: Exception | None = None, minutes: float = 60) -> None:
        self.made: list[tuple[str, dict[str, Any]]] = []
        self.asked: list[tuple[str, ...]] = []
        outer = self

        class Credential:
            kind = ""

            def __init__(self, **options: Any) -> None:
                outer.made.append((self.kind, options))

            def get_token(self, *scopes: str) -> Any:
                outer.asked.append(scopes)
                if error is not None:
                    raise error
                return SimpleNamespace(token="jwt-of-the-identity", expires_on=int(NOW + minutes * 60))

        self.ManagedIdentityCredential = type("ManagedIdentityCredential", (Credential,), {"kind": "mi"})
        self.AzureCliCredential = type("AzureCliCredential", (Credential,), {"kind": "cli"})

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "FakeIdentity":
        monkeypatch.setitem(sys.modules, "azure", SimpleNamespace())
        monkeypatch.setitem(sys.modules, "azure.identity", self)
        return self


def test_the_managed_identity_is_asked_for_the_database_scope_with_no_azure_cli(monkeypatch):
    identity = FakeIdentity().install(monkeypatch)
    provider = ManagedIdentityTokenProvider()
    token = provider.get()
    assert (token.token, token.expires_on) == ("jwt-of-the-identity", int(NOW + 3600))
    assert identity.asked == [("https://database.windows.net/.default",)] == [(session.TOKEN_SCOPE,)]
    assert identity.made == [("mi", {})]  # the system-assigned identity, and no AzureCliCredential
    provider.get()
    assert len(identity.made) == 1  # one credential for the life of the provider


def test_a_user_assigned_identity_is_named_by_its_client_id(monkeypatch):
    identity = FakeIdentity().install(monkeypatch)
    ManagedIdentityTokenProvider(CLIENT_ID).get()
    assert identity.made == [("mi", {"client_id": CLIENT_ID})]


def test_no_token_from_the_managed_identity_is_the_stop_of_the_azure_cli_path_and_says_what_to_check(
    monkeypatch,
):
    class CredentialUnavailableError(Exception):
        pass

    text = f"ManagedIdentityCredential authentication unavailable.\nNo identity {CLIENT_ID.upper()} here."
    FakeIdentity(CredentialUnavailableError(text)).install(monkeypatch)
    with pytest.raises(ToolError) as stop:
        ManagedIdentityTokenProvider(CLIENT_ID).get()
    error = stop.value

    def no_login(*scopes: str) -> Any:
        raise RuntimeError("Please run 'az login'")

    with pytest.raises(ToolError) as cli_path:
        AzureCliTokenProvider(SimpleNamespace(get_token=no_login)).get()
    assert (error.exit_code, error.reason_code) == (Exit.RETRY_SAFE, "TOKEN_UNAVAILABLE")
    assert (cli_path.value.exit_code, cli_path.value.reason_code) == (error.exit_code, error.reason_code)
    for told in (
        "AZSQLCD_AUTH=managed-identity",
        "this machine has a managed identity",
        "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID",
        "assigned to this machine",
        "CredentialUnavailableError: ManagedIdentityCredential authentication unavailable. No identity",
    ):
        assert told in error.message
    assert "No identity <client id> here. Check: " in error.message
    assert CLIENT_ID not in shown(error).lower()  # the kind is told, never the client id


# a JWT: three base64url parts with dots between them; the first starts with eyJ ('{"')
JWT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiJodHRwczovL2RhdGFiYXNlIn0.c2lnbmF0dXJl"
TOKEN_TEXTS = [
    f"client {CLIENT_ID.upper()} token {JWT}",
    f"Unexpected response: {{'access_token': '{JWT}', 'client_id': '{CLIENT_ID.replace('-', '')}'}}",
    f"Unexpected response: {{'client_id': '{CLIENT_ID.replace('-', '').upper()}', 'token': '{JWT[:60]}",
    "x" * 280 + f" {JWT}",  # the token starts before the cut of the text and ends after it
]


@pytest.mark.parametrize("text", TOKEN_TEXTS)
@pytest.mark.parametrize("provider", ["managed-identity", "entra"])
def test_a_token_or_a_client_id_in_the_error_of_the_identity_library_is_not_shown(provider, text):
    """An endpoint or a library that echoes a response puts a token into its error text. The
    client id is hidden with and without its dashes."""

    def fails(*scopes: str) -> Any:
        raise RuntimeError(text)

    credential = SimpleNamespace(get_token=fails)
    made = (
        ManagedIdentityTokenProvider(CLIENT_ID, credential)
        if provider == "managed-identity"
        else AzureCliTokenProvider(credential)
    )
    with pytest.raises(ToolError) as stop:
        made.get()
    error = stop.value
    assert error.reason_code == "TOKEN_UNAVAILABLE" and "RuntimeError: " in error.message
    told = shown(error)
    assert JWT not in told and JWT[:20] not in told and "eyJ" not in told
    assert error.__cause__ is None and error.__context__ is None  # the text of the library is not chained
    if JWT[:60] in text and "x" * 280 not in text:
        assert "<token>" in error.message
    if provider == "managed-identity":
        assert CLIENT_ID not in told.lower() and CLIENT_ID.replace("-", "") not in told.lower()
        assert "<client id>" in error.message or "x" * 280 in text


def test_the_token_life_of_a_managed_identity_is_checked_as_for_the_azure_cli(monkeypatch):
    FakeIdentity(minutes=9.5).install(monkeypatch)
    opened: list[Any] = []
    with pytest.raises(ToolError) as stop:
        connect(
            SERVER,
            DATABASE,
            ManagedIdentityTokenProvider(),
            APP,
            driver_connect=lambda keywords, token: opened.append(token),
            min_token_minutes=10,
            now=lambda: NOW,
        )
    assert stop.value.reason_code == "TOKEN_TOO_SHORT" and opened == []
    assert stop.value.detail == {"minutes_left": 9, "min_minutes": 10}


def test_a_managed_identity_connects_with_its_token_and_the_keywords_of_entra(monkeypatch):
    FakeIdentity().install(monkeypatch)
    seen: list[tuple[dict[str, str], bytes | None]] = []
    live = object()

    def driver_connect(keywords, token):
        seen.append((dict(keywords), token))
        return live

    opened = connect(
        SERVER, DATABASE, ManagedIdentityTokenProvider(), APP, driver_connect=driver_connect, now=lambda: NOW
    )
    assert opened is live
    assert seen == [(connection_keywords(SERVER, DATABASE, APP), session.pack_token("jwt-of-the-identity"))]


# ------------------------------------------------------------------ SQL authentication: the login
def test_neither_the_login_nor_the_password_is_in_the_repr_or_the_str_of_the_credential():
    login = SqlLogin(MARKER_USER, MARKER_PASSWORD)
    assert_no_marker(f"{login!r} {login!s} {[login]!r} {(login,)!s}")


@pytest.mark.parametrize("control", ["\x00", "\n", "\r", "\t", "\x1b", "\x7f", "\x85"])
@pytest.mark.parametrize("part", ["user", "password"])
def test_a_control_character_in_the_login_or_the_password_is_refused_before_a_connection(control, part):
    """An OS environment cannot hold NUL, so the check is called directly."""
    values = {"user": MARKER_USER, "password": MARKER_PASSWORD}
    values[part] = values[part][:4] + control + values[part][4:]
    with pytest.raises(ToolError) as stop:
        session.check_sql_login(**values)
    error = stop.value
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "AUTH_INVALID")
    variable = "AZSQLCD_SQL_USER" if part == "user" else "AZSQLCD_SQL_PASSWORD"
    assert variable in error.message and "control character" in error.message
    assert_no_marker(shown(error))
    assert MARKER_USER[:4] not in shown(error) and MARKER_PASSWORD[:4] not in shown(error)
    with pytest.raises(ToolError):  # no credential of that kind can exist
        SqlLogin(**values)


@pytest.mark.parametrize("password", ["p", "Pw-9x;}", "1234567", "}}}}}}}"])
def test_a_password_of_fewer_than_eight_characters_is_refused_and_not_echoed(password):
    """Azure SQL Database accepts no password of fewer than 8 characters. A value that short is a
    mistake (a wrong variable), and taken out of a text it would damage every word that holds it."""
    error = refusal(sql_environment(AZSQLCD_SQL_PASSWORD=password))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "AUTH_INVALID")
    assert "AZSQLCD_SQL_PASSWORD" in error.message and "8 characters" in error.message
    assert error.detail == {"variable": "AZSQLCD_SQL_PASSWORD"}
    assert password not in shown(error) or len(password) == 1
    with pytest.raises(ToolError):
        SqlLogin(MARKER_USER, password)
    assert SqlLogin("a", "12345678").user == "a"  # eight are enough; a login has no least length


def test_a_login_that_can_be_passed_is_accepted_with_every_printable_character():
    session.check_sql_login("user@server name", "p}}{{;=' \"\\ ü€ {end}")
    session.check_sql_login(MARKER_USER, MARKER_PASSWORD)


def test_the_keywords_of_sql_authentication_are_those_of_entra_and_the_login():
    entra = connection_keywords(SERVER, DATABASE, APP)
    with_login = connection_keywords(SERVER, DATABASE, APP, SqlLogin(MARKER_USER, MARKER_PASSWORD))
    assert list(with_login.items()) == [*entra.items(), ("UID", MARKER_USER), ("PWD", MARKER_PASSWORD)]
    # never weaker: the same encryption, and the certificate of the server is checked
    assert (with_login["Encrypt"], with_login["TrustServerCertificate"]) == ("yes", "no")


def test_connect_hands_the_login_to_the_driver_and_asks_for_no_token():
    seen: list[tuple[dict[str, str], bytes | None]] = []
    live = object()

    def driver_connect(keywords, token):
        seen.append((dict(keywords), token))
        return live

    login = SqlLogin(MARKER_USER, MARKER_PASSWORD)
    # a token life cannot be asked of a login: min_token_minutes is not applicable, never a refusal
    opened = connect(SERVER, DATABASE, login, APP, driver_connect=driver_connect, min_token_minutes=10_000)
    assert opened is live
    assert seen == [(connection_keywords(SERVER, DATABASE, APP, login), None)]


# ------------------------------------------------------------------ SQL authentication: the driver
class DriverError(Exception):
    def __init__(self, driver_error: str, ddbc_error: str = "") -> None:
        super().__init__(f"Driver Error: {driver_error}; DDBC Error: {ddbc_error}")
        self.driver_error, self.ddbc_error = driver_error, ddbc_error


class Cursor:
    description = None

    def __init__(self, error: Exception | None) -> None:
        self._error = error

    def execute(self, batch: str) -> None:
        if self._error is not None:
            raise self._error

    def nextset(self) -> bool:
        return False


class Connection:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def cursor(self) -> Cursor:
        return Cursor(self._error)

    def close(self) -> None:
        pass


class FakeDriver:
    """The part of the mssql_python module that session.py uses."""

    Error = DriverError
    Warning = type("DriverWarning", (Exception,), {})

    def __init__(self, outcome: Any = None) -> None:
        self.outcome = Connection() if outcome is None else outcome
        self.calls: list[tuple[tuple, dict]] = []

    def pooling(self, **options: Any) -> None:
        pass

    def connect(self, *args: Any, **options: Any) -> Any:
        self.calls.append((args, options))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


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


HOSTILE = [
    "p;Encrypt=no",
    "p};Encrypt=no;TrustServerCertificate=yes;x={",
    "p}",
    "}};Encrypt={no",
    "{p;Encrypt=no",
    "p;Server=evil.example.net",
    " p = x ",
    "p}}",
    "}",
    "{}",
    "p;PWD=other;UID=other",
    MARKER_PASSWORD,
]


def as_passwords(hostile: str) -> list[str]:
    """The hostile text as a password. The tool takes no password of fewer than 8 characters, so
    a shorter text is tried at the start and at the end of a longer one."""
    return [hostile] if len(hostile) >= 8 else [f"{hostile}-padding", f"padding-{hostile}"]


def hostile_cases(hostile: str) -> list[tuple[str, str]]:
    return [(MARKER_USER, password) for password in as_passwords(hostile)] + [(hostile, MARKER_PASSWORD)]


def sent_connection_string(user: str, password: str) -> tuple[str, dict[str, Any]]:
    driver = FakeDriver()
    open_session(driver, connection_keywords(SERVER, DATABASE, APP, SqlLogin(user, password)), None)
    (args, options) = driver.calls[0]
    return args[0], options


@pytest.mark.parametrize("hostile", HOSTILE)
def test_no_character_of_a_password_or_a_login_can_end_its_value_or_add_a_keyword(hostile):
    for user, password in hostile_cases(hostile):
        text, _ = sent_connection_string(user, password)
        assert read_odbc_string(text) == [
            ("Server", SERVER),
            ("Database", DATABASE),
            ("Encrypt", "yes"),  # the security keywords are the ones of the tool, once each
            ("TrustServerCertificate", "no"),
            ("ConnectRetryCount", "0"),
            ("UID", user),
            ("PWD", password),
        ]


def test_a_sql_session_is_dedicated_autocommit_and_carries_no_token_attribute():
    _, options = sent_connection_string(MARKER_USER, MARKER_PASSWORD)
    assert options == {"autocommit": True}
    # and a token session is what it was
    driver = FakeDriver()
    open_session(driver, connection_keywords(SERVER, DATABASE, APP), b"packed")
    assert driver.calls[0][1] == {"autocommit": True, "attrs_before": {1256: b"packed"}}
    assert "UID" not in driver.calls[0][0][0] and "PWD" not in driver.calls[0][0][0]


def sql_connect(driver: FakeDriver, **options: Any) -> Any:
    def driver_connect(keywords, token):
        return open_session(driver, keywords, token)

    login = SqlLogin(MARKER_USER, MARKER_PASSWORD)
    return connect(
        SERVER, DATABASE, login, APP, driver_connect=driver_connect, sleep=lambda s: None, **options
    )


@pytest.mark.parametrize(
    "error",
    [
        DriverError("Invalid authorization specification", LOGIN_FAILED),
        DriverError("Invalid authorization specification", ""),
        DriverError("General error", f"Login failed. UID={MARKER_USER};PWD={MARKER_PASSWORD}"),
        DriverError("General error", f"Password validation failed for {MARKER_USER} ({MARKER_PASSWORD})"),
    ],
)
def test_a_failed_sql_login_shows_the_number_and_the_class_and_never_the_driver_message(error):
    with pytest.raises(ToolError) as stop:
        sql_connect(FakeDriver(error))
    failed = stop.value
    assert (failed.exit_code, failed.reason_code) == (Exit.RETRY_SAFE, "CONNECT_FAILED")
    assert "the driver message is not shown for SQL authentication" in failed.message
    assert "class OTHER" in failed.message
    assert ("error 18456" in failed.message) == ("Login failed for user" in error.ddbc_error)
    assert "Login failed" not in failed.message and "Password validation" not in failed.message
    assert failed.detail["attempts"] == 1 and failed.detail["error_class"] == "OTHER"
    assert failed.__cause__ is None and failed.__context__ is None
    assert_no_marker(shown(failed))


def test_a_failed_token_login_still_shows_the_redacted_driver_message():
    def driver_connect(keywords, token):
        return open_session(
            FakeDriver(DriverError("Invalid authorization specification", LOGIN_FAILED)), {}, b""
        )

    with pytest.raises(ToolError) as stop:
        connect(SERVER, DATABASE, Tokens(), APP, driver_connect=driver_connect, now=lambda: NOW)
    assert "[OTHER 18456] [Microsoft][SQL Server]Login failed for user <redacted>." in stop.value.message
    assert "is not shown" not in stop.value.message


def test_another_connect_error_of_a_sql_session_is_shown_without_the_login_and_the_password():
    # not a failed login: the text helps (a firewall rule, a network error). A driver that echoes
    # what it was given, outside quotes: the worst case
    text = (
        f"[Microsoft]Client with IP address '203.0.113.7' is not allowed to access the server. "
        f"Tried {MARKER_USER} with {MARKER_PASSWORD}"
    )
    with pytest.raises(ToolError) as stop:
        sql_connect(FakeDriver(DriverError("General error", text)))
    failed = stop.value
    assert failed.reason_code == "CONNECT_FAILED"
    assert "Client with IP address <redacted> is not allowed to access the server." in failed.message
    assert "is not shown" not in failed.message
    assert_no_marker(shown(failed))


def test_a_driver_connect_that_is_given_by_a_caller_cannot_leak_the_login_either():
    def leaky(keywords, token):
        raise sql_error(
            f"[Microsoft]Cannot connect as {keywords['UID']} with {keywords['PWD']}", at_connect=True
        )

    login = SqlLogin(MARKER_USER, MARKER_PASSWORD)
    with pytest.raises(ToolError) as stop:
        connect(SERVER, DATABASE, login, APP, driver_connect=leaky)
    assert "Cannot connect as" in stop.value.message
    assert stop.value.__cause__ is None and stop.value.__context__ is None
    assert_no_marker(f"{stop.value} {stop.value.message} {stop.value.detail!r} {stop.value.args!r}")


def test_a_password_with_a_quote_cannot_stay_in_part_in_the_message_of_a_connect_error():
    parts = ("Pw-first", "second-part")
    login = SqlLogin(MARKER_USER, f"{parts[0]}'{parts[1]}")

    def leaky(keywords, token):
        raise sql_error(f"[Microsoft]Cannot open the server with {keywords['PWD']} now", at_connect=True)

    with pytest.raises(ToolError) as stop:
        connect(SERVER, DATABASE, login, APP, driver_connect=leaky)
    assert "[OTHER] [Microsoft]Cannot open the server with <hidden> now" in stop.value.message
    assert not [part for part in parts if part in shown(stop.value)]


def test_a_transient_error_of_a_sql_session_is_retried_as_for_a_token():
    waits: list[float] = []
    text = "[Microsoft][SQL Server]Database 'sales' on server 'sql-example' is not currently available."
    driver = FakeDriver(DriverError("General error", text))
    login = SqlLogin(MARKER_USER, MARKER_PASSWORD)
    clock = [100.0]

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += seconds

    with pytest.raises(ToolError) as stop:
        connect(
            SERVER,
            DATABASE,
            login,
            APP,
            driver_connect=lambda keywords, token: open_session(driver, keywords, token),
            sleep=sleep,
            monotonic=lambda: clock[0],
        )
    assert waits == [5, 10, 20, 40, 60] and len(driver.calls) == 6
    assert stop.value.detail["error_class"] == "TRANSIENT_CONNECT"


def test_an_engine_error_of_a_batch_of_a_sql_session_holds_neither_the_login_nor_the_password():
    text = (
        f"[Microsoft][SQL Server]Cannot find the user '{MARKER_USER}'. Tried {MARKER_USER}/{MARKER_PASSWORD}"
    )
    db = sql_connect(FakeDriver(Connection(DriverError("General error", text))))
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1;")
    assert "Cannot find the user" in raised.value.raw_message  # the text is there for --show-error-text
    assert_no_marker(shown(raised.value))
    assert_no_marker(f"{db!r} {vars(db)!r}")


def test_a_password_with_a_quote_is_taken_out_before_the_text_is_redacted():
    """redact() hides a text from a quote on. Taken out after it, the part of the password before
    the quote would stay in the message."""
    parts = ("Pw-first", "second-part", "third-part", "fourth-part")
    password = f"{parts[0]}'{parts[1]}\"{parts[2]}({parts[3]}"
    text = f"[Microsoft]Invalid value {password} for the login {MARKER_USER}"
    driver = FakeDriver(Connection(DriverError("General error", text)))
    db = connect(
        SERVER,
        DATABASE,
        SqlLogin(MARKER_USER, password),
        APP,
        driver_connect=lambda keywords, token: open_session(driver, keywords, token),
    )
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1;")
    told = shown(raised.value)
    assert "Invalid value <hidden> for the login <hidden>" in raised.value.raw_message
    assert MARKER_USER not in told and not [part for part in parts if part in told]


def test_the_class_of_an_error_is_read_before_a_login_is_taken_out_of_its_text():
    # a login that is a word of an engine message: the decision of the tool must not change
    login = SqlLogin("Lock", "transaction")
    driver = FakeDriver(Connection(DriverError("General error", "Lock request time out period exceeded.")))
    db = connect(
        SERVER,
        DATABASE,
        login,
        APP,
        driver_connect=lambda keywords, token: open_session(driver, keywords, token),
    )
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1;")
    assert (raised.value.number, raised.value.cls) == (1222, ErrorClass.LOCK_TIMEOUT)
    assert "Lock" not in raised.value.raw_message


# ------------------------------------------------------------------ a driver that echoes its string
# What open_session sends holds the password in braces with } doubled: PWD={Pw}}9...}. A driver that
# puts that string into an error text, whole or cut, shows the password in a form that is not the
# value of the variable.
ECHOED_PASSWORDS = [
    "Pw}9;x=K3 y$m\\rk-echoed",  # } is doubled in the string that is sent
    "Pw}9;x=K'3 \"y$m\\rk-echoed",  # and quotes, where redact() cuts a text
    "}}Pw}-9x}}",  # nothing but braces at its ends
    "Pässwörd-Ünï-ключ-echoed",
    MARKER_PASSWORD,
]
ECHO_CUTS = {"whole": None, "cut in the password": ("PWD=", 9), "cut in the login": ("UID=", 8)}


def echoed(sent: str, cut: tuple[str, int] | None) -> str:
    """The connection string that was sent, as a driver can put it into a message: whole, or cut
    some characters after a keyword."""
    return sent if cut is None else sent[: sent.index(cut[0]) + len(cut[0]) + cut[1]]


def assert_no_part(text: str, password: str, user: str = MARKER_USER) -> None:
    """Neither the login nor any five characters in a row of the password, raw or with } doubled."""
    assert user not in text and user[:8] not in text and user[-8:] not in text
    for form in (password, password.replace("}", "}}")):
        found = [form[at : at + 5] for at in range(len(form) - 4) if form[at : at + 5] in text]
        assert found == []


class EchoDriver(FakeDriver):
    """A driver whose connect fails with a text that holds the connection string it was given."""

    def __init__(self, cut: tuple[str, int] | None = None) -> None:
        super().__init__()
        self.cut = cut

    def connect(self, *args: Any, **options: Any) -> Any:
        self.calls.append((args, options))
        text = f"[Microsoft][ODBC Driver 18 for SQL Server]Invalid connection string attribute {args[0]}"
        raise DriverError("General error", echoed(text, self.cut))


@pytest.mark.parametrize("cut", list(ECHO_CUTS))
@pytest.mark.parametrize("password", ECHOED_PASSWORDS)
def test_a_connection_string_that_the_driver_echoes_at_connect_shows_no_part_of_the_password(password, cut):
    driver = EchoDriver(ECHO_CUTS[cut])
    with pytest.raises(ToolError) as stop:
        connect(
            SERVER,
            DATABASE,
            SqlLogin(MARKER_USER, password),
            APP,
            driver_connect=lambda keywords, token: open_session(driver, keywords, token),
        )
    failed = stop.value
    assert f"PWD={{{password.replace('}', '}}')}}}" in driver.calls[0][0][0]  # the string held it
    assert failed.reason_code == "CONNECT_FAILED" and "Invalid connection string attribute" in failed.message
    assert_no_part(shown(failed), password)


@pytest.mark.parametrize("cut", list(ECHO_CUTS))
@pytest.mark.parametrize("password", ECHOED_PASSWORDS)
def test_a_connection_string_that_the_driver_echoes_in_a_batch_error_shows_no_part_of_the_password(
    password, cut
):
    driver = FakeDriver()
    db = connect(
        SERVER,
        DATABASE,
        SqlLogin(MARKER_USER, password),
        APP,
        driver_connect=lambda keywords, token: open_session(driver, keywords, token),
    )
    sent = driver.calls[0][0][0]
    text = echoed(
        f"[Microsoft][SQL Server]Lock request time out period exceeded. Connection: {sent}", ECHO_CUTS[cut]
    )
    driver.outcome._error = DriverError("General error", text)
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1;")
    # the decision is read from the full text; what is kept holds no part of the sign-in
    assert (raised.value.number, raised.value.cls) == (1222, ErrorClass.LOCK_TIMEOUT)
    assert "Lock request time out period exceeded." in raised.value.raw_message
    assert_no_part(shown(raised.value), password)


def test_a_text_that_names_a_connection_keyword_of_the_sign_in_is_cut_there_only_for_a_sql_login():
    text = "[Microsoft]Attribute too long: Server={x};UID={someone};PWD={some"
    db = sql_connect(FakeDriver(Connection(DriverError("General error", text))))
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1;")
    assert raised.value.raw_message == "[Microsoft]Attribute too long: Server={x};<hidden>"
    # a token session has no login to hide: its text is what it was
    token_db = open_session(FakeDriver(Connection(DriverError("General error", text))), {}, b"packed")
    with pytest.raises(SqlError) as raised:
        token_db.execute("SELECT 1;")
    assert raised.value.raw_message == text


class Bridge:
    """A cursor and a connection of the driver over a FakeSession: what the fake answers is given
    as the driver gives it, and an engine error becomes a driver error whose text can echo the
    connection string."""

    description: Any = None

    def __init__(self, driver: "BridgeDriver", backend: Any) -> None:
        self.driver, self.backend = driver, backend
        self.sets: list = []
        self.at = 0

    def cursor(self) -> "Bridge":
        return self

    def execute(self, batch: str) -> None:
        self.sets, self.at, self.description = [], 0, None
        try:
            self.sets = list(self.backend.execute(batch))
        except SqlError as error:
            label = "Communication link failure" if error.cls is ErrorClass.SESSION_LOST else "General error"
            text = self.driver.echo(
                f"[Microsoft][SQL Server]{error.raw_message}", self.driver.calls[-1][0][0]
            )
            raise DriverError(label, text) from None
        self.description = ("c",) if self.sets else None

    def fetchall(self) -> list:
        return self.sets[self.at]

    def nextset(self) -> bool:
        self.at += 1
        self.description = ("c",) if self.at < len(self.sets) else None
        return self.at < len(self.sets)

    def close(self) -> None:
        self.backend.close()


class BridgeDriver(FakeDriver):
    """The driver module of a call of cli.main with no session_factory: the real connect, the real
    open_session and the real DriverSession run, over the FakeSessions of a test."""

    def __init__(self, *backends: Any, echo: Any = None, fail: Any = None) -> None:
        super().__init__()
        self.backends, self.echo, self.fail = list(backends), echo or (lambda text, sent: text), fail
        self.opened = 0

    def connect(self, *args: Any, **options: Any) -> Any:
        self.calls.append((args, options))
        if self.fail is not None:
            raise DriverError("General error", self.fail(args[0]))
        self.opened += 1
        return Bridge(self, self.backends[self.opened - 1])


@pytest.fixture
def echoing(monkeypatch, tmp_path):
    """A workstation with the SQL sign-in and a password with }; returns install(driver)."""
    password = ECHOED_PASSWORDS[1]
    for name, value in sql_environment(AZSQLCD_SQL_PASSWORD=password).items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)

    def install(driver: BridgeDriver) -> str:
        monkeypatch.setattr(session, "_load_driver", lambda: driver)
        return password

    return install


@pytest.mark.parametrize("cut", list(ECHO_CUTS))
@pytest.mark.parametrize("more", [(), ("--show-error-text",)], ids=["redacted", "show-error-text"])
def test_no_output_of_a_deploy_holds_the_password_when_a_batch_error_echoes_the_connection_string(
    tmp_path, echoing, capsys, more, cut
):
    argv, db = a_failing_deploy(tmp_path, *more)
    driver = BridgeDriver(
        db, parse_only_session(), echo=lambda text, sent: echoed(f"{text} Connection: {sent}", ECHO_CUTS[cut])
    )
    password = echoing(driver)

    code = cli.main(without_ci(argv))

    assert code == 21 and driver.opened == 2
    [log] = (tmp_path / "triage-logs").glob("azsqlcd-*-deploy.jsonl")
    assert cli.main(["support-bundle", "--log", str(log), "--out", str(tmp_path / "send.zip")]) == 0
    assert cli.main(["show-log", "--log", str(log)]) == 0
    printed = capsys.readouterr()
    assert "BATCH_FAILED: step 0001__a.sql#1 failed" in printed.err
    # the streams, report.json, the triage log, both members of the bundle, and what the runner
    # wrote into azsqlcd.run and azsqlcd.step
    everything = printed.out + printed.err + everything_written(tmp_path) + "\n".join(db.batches)
    assert (
        '"reason_code": "BATCH_FAILED"' in everything
        and "[azsqlcd].[run] SET [status] = N'failed'" in everything
    )
    assert_no_part(everything, password)
    undone = everything.replace("\\\\", "\\").replace('\\"', '"')  # the escapes of a JSON file
    assert_no_part(undone, password)


@pytest.mark.parametrize("cut", list(ECHO_CUTS))
@pytest.mark.parametrize("command", ["plan", "deploy"])
def test_no_output_of_a_command_holds_the_password_when_the_connect_error_echoes_the_connection_string(
    tmp_path, echoing, capsys, command, cut
):
    argv = a_plan(tmp_path)[0] if command == "plan" else a_deploy(tmp_path)[0]
    driver = BridgeDriver(
        fail=lambda sent: echoed(f"[Microsoft]Invalid connection string attribute {sent}", ECHO_CUTS[cut])
    )
    password = echoing(driver)

    code = cli.main([*without_ci(argv), "--show-error-text"])

    printed = capsys.readouterr()
    assert code == 24 and printed.err.startswith("CONNECT_FAILED: no connection after 1 attempt(s): ")
    everything = printed.out + printed.err + everything_written(tmp_path)
    assert '"reason_code": "CONNECT_FAILED"' in everything
    assert_no_part(everything, password)
    assert_no_part(everything.replace("\\\\", "\\").replace('\\"', '"'), password)


def test_a_short_login_is_taken_out_of_a_driver_text_as_a_word_and_not_out_of_other_words():
    login = SqlLogin("a", "Passw0rd-of-a")
    text = "[Microsoft][SQL Server]Cannot open database requested by the login a. The login failed."
    driver = FakeDriver(Connection(DriverError("General error", text)))
    db = connect(
        SERVER,
        DATABASE,
        login,
        APP,
        driver_connect=lambda keywords, token: open_session(driver, keywords, token),
    )
    with pytest.raises(SqlError) as raised:
        db.execute("SELECT 1;")
    assert raised.value.raw_message == text.replace("login a.", "login <hidden>.")


def test_the_log_of_a_call_with_a_credential_of_the_caller_hides_that_login(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZSQLCD_LOG_DIR", str(tmp_path / "triage-logs"))
    argv, db, _ = a_deploy(tmp_path / MARKER_USER)  # the login is a part of a path of argv
    code = cli.main(
        without_ci(argv),
        session_factory=Sessions(db, parse_only_session()),
        token_provider=SqlLogin(MARKER_USER, MARKER_PASSWORD),
    )
    assert code == 0, capsys.readouterr().err
    [log] = (tmp_path / "triage-logs").glob("azsqlcd-*-deploy.jsonl")
    written = log.read_text(encoding="utf-8")
    assert MARKER_USER not in written and "<hidden>" in written


# ------------------------------------------------------------------ the names of an engine message
@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("batch_marker", True),  # the batch wrote [batch_marker]: the reader of a failed deploy needs it
        ("S3CR3TVALUE", False),  # the batch did not: a statement that dynamic SQL built from data
    ],
)
def test_the_real_session_keeps_the_name_of_207_only_when_the_batch_that_it_sent_wrote_the_name(
    tmp_path, monkeypatch, capsys, name, kept
):
    """cli.main over the real connect, open_session and DriverSession, with a token."""
    monkeypatch.chdir(tmp_path)
    argv, db = a_failing_deploy(tmp_path)
    driver = BridgeDriver(db, parse_only_session())
    db.fail_on("[batch_marker]", sql_error(f"Invalid column name '{name}'.", number=207))
    del db._rules[-2]  # the rule of a_failing_deploy: this message takes its place
    monkeypatch.setattr(session, "_load_driver", lambda: driver)

    code = cli.main(without_ci(argv), token_provider=CliTokens())

    printed = capsys.readouterr()
    assert code == 21 and driver.opened == 2, printed.err
    (status,) = db.sent("UPDATE [azsqlcd].[run] SET [status] = N'failed'")
    everything = printed.out + printed.err + everything_written(tmp_path) + status
    shown_name = f"[OTHER 207] [Microsoft][SQL Server]Invalid column name '{name}'."
    assert (f"BATCH_FAILED: step 0001__a.sql#1 failed: {shown_name}" in printed.err) is kept
    assert (name in everything) is kept
    if not kept:
        assert "Invalid column name <redacted>." in printed.err
    # the row of azsqlcd.run never holds a name as the statement wrote it: no batch is known there
    assert "Invalid column name <redacted>." in status.replace("''", "'")


# ------------------------------------------------------------------ the runner
def test_a_deploy_with_a_sql_login_has_no_token_life_to_check_and_reports_no_minutes():
    b, st = a_migration()
    report = run_deploy(b, st, token_provider=SqlLogin(MARKER_USER, MARKER_PASSWORD))
    assert report.exit_code == 0
    assert report.plan is not None and report.plan.token_minutes_left is None
    assert_no_marker(report.to_json() + report.plan.to_json() + repr(report))


def test_a_deploy_with_a_token_still_stops_on_a_short_token():
    b, st = a_migration()
    opened: list = []
    error = failure(b, st, opened=opened, token_provider=Tokens(minutes=19))
    assert error.reason_code == "TOKEN_TOO_SHORT" and opened == []


# ------------------------------------------------------------------ the command line
def call(argv: list[str], *sessions: Any) -> tuple[int, Sessions]:
    """cli.main with the sessions of a test and the sign-in of the environment."""
    factory = Sessions(*sessions)
    return cli.main(argv, session_factory=factory), factory


def without_ci(argv: list[str]) -> list[str]:
    return [word for word in argv if word not in ("--ci", "github")]


def everything_written(folder: Path) -> str:
    text = ""
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix == ".zip":
            with zipfile.ZipFile(path) as bundle_zip:
                text += "".join(bundle_zip.read(name).decode("utf-8") for name in bundle_zip.namelist())
        elif path.is_file() and path.suffix != ".tar":
            text += path.read_text(encoding="utf-8", errors="replace")
    return text


def test_plan_with_a_sql_login_says_the_sign_in_and_that_token_minutes_are_not_applicable(
    tmp_path, sql_auth, capsys
):
    argv, db = a_plan(tmp_path)

    code, sessions = call(without_ci(argv), db, parse_only_session())

    printed = capsys.readouterr()
    plan_doc = json.loads((tmp_path / "plan" / "plan.json").read_text())
    assert code == 0, printed.err
    assert plan_doc["token_minutes_left"] is None
    assert "sign-in: sql" in plan_doc["notes"]
    assert "sign-in: sql" in printed.out.splitlines()
    assert "token minutes left: not applicable (SQL authentication has no access token)" in printed.out
    assert [s.closed for s in sessions.opened] == [True, True]
    assert_no_marker(printed.out + printed.err + everything_written(tmp_path))


def test_the_plan_file_of_a_sql_plan_is_the_plan_that_a_deploy_expects(tmp_path, sql_auth):
    argv, plan_db = a_plan(tmp_path)
    assert call(without_ci(argv), plan_db, parse_only_session())[0] == 0
    plan_file = tmp_path / "plan" / "plan.json"
    from azsqlcd.plan import Plan  # the note is outside the hash: the file reads back

    assert "sign-in: sql" in Plan.from_json(plan_file.read_text()).notes


def test_the_summary_shows_token_minutes_as_not_applicable_only_for_a_sql_login():
    summary = "\n".join(cli._plan_summary(a_plan_doc(), "sql"))
    assert "| Token minutes left | not applicable (SQL authentication has no access token) |" in summary
    assert "| Sign-in | sql |" in summary
    token = "\n".join(cli._plan_summary(a_plan_doc(token_minutes_left=58), "managed-identity"))
    assert "| Token minutes left | 58 |" in token and "| Sign-in | managed-identity |" in token
    assert "not applicable" not in token


def run_every_database_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """plan, deploy (one that fails in a batch), drift, baseline (report and write), export,
    resolve; then the support bundle of the failed deploy. Returns the exit codes."""
    monkeypatch.chdir(tmp_path)
    codes = []
    argv, db = a_plan(tmp_path / "plan-run")
    codes.append(call(without_ci(argv), db, parse_only_session())[0])
    argv, db = a_failing_deploy(tmp_path / "deploy-run", "--show-error-text")
    codes.append(call(without_ci(argv), db, parse_only_session())[0])
    argv, db, _ = a_deploy(tmp_path / "deploy-ok")
    codes.append(call(without_ci(argv), db, parse_only_session())[0])
    argv, db = a_drift(tmp_path / "drift-run", drifted=True)
    codes.append(call(without_ci(argv), db)[0])
    where, db = an_existing_database(tmp_path / "baseline-run")
    codes.append(call(["baseline", "--report-only", *where], db)[0])
    where, db = an_existing_database(tmp_path / "baseline-write", ack=["PROCEDURE:[sales].[usp_changed]"])
    codes.append(call(["baseline", *where, "--out", str(tmp_path / "baseline-report")], db)[0])
    write(tmp_path / "repo", "azsqlcd.toml", TOML)
    export = ["export", "--root", str(tmp_path / "repo"), *target(), "--out", str(tmp_path / "export")]
    codes.append(call(export, onboard_db.Db())[0])
    action, b, db, subject, _, _, _ = ACTIONS[0]()
    resolve = ["resolve", *on_disk(tmp_path / "resolve-run", b), *target(), "--confirm-database", "sales"]
    resolve += ["--reason", "a reason", FLAGS[action], *(str(part) for part in subject or ("dev",))]
    codes.append(call([*resolve, "--out", str(tmp_path / "resolve-report")], db)[0])
    log = sorted((tmp_path / "triage-logs").glob("azsqlcd-*-deploy.jsonl"))[0]
    codes.append(cli.main(["support-bundle", "--log", str(log), "--out", str(tmp_path / "send.zip")]))
    codes.append(cli.main(["show-log", "--log", str(log)]))
    return codes


def test_no_file_and_no_stream_of_any_database_command_holds_the_login_or_the_password(
    tmp_path, sql_auth, monkeypatch, capsys
):
    codes = run_every_database_command(tmp_path, monkeypatch)

    printed = capsys.readouterr()
    assert codes[:3] == [0, 21, 0], printed.err
    assert codes[3:] == [30, 0, 0, 0, 0, 0, 0], printed.err
    written = everything_written(tmp_path)
    assert_no_marker(printed.out + printed.err + written)
    # the files were written: the search is not of an empty set
    for holds in ('"reason_code": "BATCH_FAILED"', '"kind": "batch"', '"auth": "sql"', "plan_sha256"):
        assert holds in written
    assert f"(--show-error-text): {ENGINE_MESSAGE}" in printed.err


def test_the_log_and_the_report_say_which_sign_in_was_used_and_nothing_of_the_identity(
    tmp_path, sql_auth, monkeypatch, capsys
):
    argv, db, _ = a_deploy(tmp_path)
    assert call(without_ci(argv), db, parse_only_session())[0] == 0
    report = json.loads((tmp_path / "report" / "report.json").read_text())
    assert report["auth"] == "sql"
    [log] = (tmp_path / "triage-logs").glob("azsqlcd-*-deploy.jsonl")
    config = next(event for event in events_of(log) if event["kind"] == "config")
    assert config["auth"] == "sql"
    assert "sign-in: sql" in trace.summarize(log).splitlines()
    plan_doc = json.loads((tmp_path / "report" / "plan.json").read_text())  # of --inline-plan
    assert plan_doc["token_minutes_left"] is None and "sign-in: sql" in plan_doc["notes"]
    assert_no_marker(capsys.readouterr().out + everything_written(tmp_path))


def test_a_managed_identity_is_named_as_the_sign_in_and_its_client_id_is_in_no_file(
    tmp_path, monkeypatch, capsys
):
    FakeIdentity().install(monkeypatch)
    ci = ci_files(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("AZSQLCD_AUTH", "managed-identity")
    monkeypatch.setenv("AZSQLCD_MANAGED_IDENTITY_CLIENT_ID", CLIENT_ID)
    argv, db, _ = a_deploy(tmp_path)

    code, _ = call(argv, db, parse_only_session())

    printed = capsys.readouterr()
    assert code == 0, printed.err
    assert json.loads((tmp_path / "report" / "report.json").read_text())["auth"] == "managed-identity"
    plan_doc = json.loads((tmp_path / "report" / "plan.json").read_text())
    minutes = plan_doc["token_minutes_left"]  # the token of the identity has an expiry: a number
    assert type(minutes) is int and minutes > 0 and "sign-in: managed-identity" in plan_doc["notes"]
    summary = ci.summary.read_text(encoding="utf-8")
    assert "| Sign-in | managed-identity |" in summary and f"| Token minutes left | {minutes} |" in summary
    everything = printed.out + printed.err + ci.text() + everything_written(tmp_path)
    assert CLIENT_ID not in everything and "jwt-of-the-identity" not in everything


def test_the_token_of_the_test_is_the_sign_in_entra(tmp_path, monkeypatch):
    ci = ci_files(tmp_path, monkeypatch)
    argv, db, _ = a_deploy(tmp_path)
    assert cli.main(argv, session_factory=Sessions(db, parse_only_session()), token_provider=Tokens()) == 0
    assert json.loads((tmp_path / "report" / "report.json").read_text())["auth"] == "entra"
    assert "| Sign-in | entra |" in ci.summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("how", ["--ci github", "GITHUB_ACTIONS"])
@pytest.mark.parametrize("command", ["plan", "deploy", "drift", "resolve"])
def test_a_sql_login_is_refused_in_a_workflow_before_a_session_opens(
    how, command, tmp_path, sql_auth, monkeypatch, capsys
):
    ci = ci_files(tmp_path, monkeypatch)
    if command == "plan":
        argv, _ = a_plan(tmp_path)
    elif command == "deploy":
        argv, _, _ = a_deploy(tmp_path)
    elif command == "drift":
        argv, _ = a_drift(tmp_path, drifted=False)
    else:
        b, _ = a_migration()
        argv = ["resolve", *on_disk(tmp_path, b), *target(), "--confirm-database", "sales"]
        argv += ["--reason", "a reason", "--mark-not-applied", "0001__a.sql", "--ci", "github"]
    if how == "GITHUB_ACTIONS":
        argv = without_ci(argv)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")

    code, sessions = call(argv)

    printed = capsys.readouterr()
    assert code == 22 and sessions.opened == []
    assert (
        printed.err.startswith("AUTH_INVALID: ") and "SQL authentication is for a workstation" in printed.err
    )
    if how == "--ci github":
        assert ci.outputs()["reason_code"] == "AUTH_INVALID"
    assert_no_marker(printed.out + printed.err + everything_written(tmp_path))


@pytest.mark.parametrize("how", ["--ci github", "GITHUB_ACTIONS", "GITHUB_ACTIONS=True", "GITHUB_RUN_ID"])
@pytest.mark.parametrize("command", ["plan", "deploy"])
def test_a_sql_login_that_the_caller_gives_is_refused_in_a_workflow_before_a_session_opens(
    how, command, tmp_path, monkeypatch, capsys
):
    """The refusal is of the sign-in, not of the function that read the environment: a SqlLogin
    that an embedding program hands to cli.main opens no session in a workflow."""
    ci_files(tmp_path, monkeypatch)
    argv = a_plan(tmp_path)[0] if command == "plan" else a_deploy(tmp_path)[0]
    if how != "--ci github":
        argv = without_ci(argv)
        name, _, value = how.partition("=")
        monkeypatch.setenv(name, value or ("true" if name == "GITHUB_ACTIONS" else "900"))
    sessions = Sessions()

    code = cli.main(argv, session_factory=sessions, token_provider=SqlLogin(MARKER_USER, MARKER_PASSWORD))

    printed = capsys.readouterr()
    assert code == 22 and sessions.opened == []
    assert (
        printed.err.startswith("AUTH_INVALID: ") and "SQL authentication is for a workstation" in printed.err
    )
    assert_no_marker(printed.out + printed.err + everything_written(tmp_path))


def test_a_token_that_the_caller_gives_is_not_refused_in_a_workflow(tmp_path, monkeypatch, sql_auth):
    # the variables of a SQL login are set, and the sign-in of the call is a token: nothing to refuse
    ci_files(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    argv, db, _ = a_deploy(tmp_path)
    assert cli.main(argv, session_factory=Sessions(db, parse_only_session()), token_provider=Tokens()) == 0
    assert json.loads((tmp_path / "report" / "report.json").read_text())["auth"] == "entra"


@pytest.mark.parametrize(
    ("environ", "reason_code"),
    [
        ({"AZSQLCD_AUTH": "password"}, "AUTH_INVALID"),
        ({"AZSQLCD_AUTH": "sql"}, "SQL_AUTH_MISSING"),
        ({"AZSQLCD_AUTH": "sql", "AZSQLCD_SQL_USER": MARKER_USER}, "SQL_AUTH_MISSING"),
        ({"AZSQLCD_AUTH": "managed-identity", "AZSQLCD_MANAGED_IDENTITY_CLIENT_ID": "x"}, "AUTH_INVALID"),
    ],
)
def test_a_sign_in_that_cannot_be_used_is_exit_22_before_a_session_opens(
    environ, reason_code, tmp_path, monkeypatch, capsys
):
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    argv, _ = a_plan(tmp_path)

    code, sessions = call(without_ci(argv))

    printed = capsys.readouterr()
    assert code == 22 and sessions.opened == []
    assert printed.err.startswith(f"{reason_code}: ")
    assert f"azsqlcd plan: exit {22}" in printed.err
    assert_no_marker(printed.err)


def test_an_offline_command_does_not_read_the_sign_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AZSQLCD_AUTH", "not-a-sign-in")
    write(tmp_path, "azsqlcd.toml", TOML)
    assert cli.main(["setup-sql", "--env", "dev", "--target", "sales-dev", "--root", str(tmp_path)]) == 0


def test_the_password_has_no_command_line_argument_and_no_key_in_the_configuration():
    parser, commands = cli.parsers()
    options = {
        option
        for command in commands.values()
        for action in command._actions
        for option in action.option_strings
    }
    assert not [option for option in options if any(word in option for word in ("password", "pwd", "user"))]
    from azsqlcd.config import load_config

    for line in ('sql_password = "x"', 'password = "x"'):
        with pytest.raises(ToolError) as stop:
            load_config(TOML.replace("[project]", f"[project]\n{line}"))
        assert stop.value.reason_code == "CONFIG_INVALID"


# ------------------------------------------------------------------ facts of the installed packages
def test_the_installed_azure_identity_takes_the_client_id_that_the_provider_passes():
    code = (
        "import sys\n"
        "try:\n"
        "    from azure.identity import ManagedIdentityCredential\n"
        "except ImportError:\n"
        "    sys.exit(3)\n"
        "ManagedIdentityCredential()\n"
        "ManagedIdentityCredential(client_id=sys.argv[1])\n"
    )
    ran = subprocess.run(
        [sys.executable, "-c", code, CLIENT_ID], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if ran.returncode == 3:
        pytest.skip("azure-identity is not installed (extra 'db')")
    assert ran.returncode == 0, ran.stderr


_DRIVER_READS = """
import json, sys
try:
    from mssql_python.connection_string_builder import _ConnectionStringBuilder
    from mssql_python.connection_string_parser import _ConnectionStringParser
except ImportError:
    sys.exit(3)
out = []
for text in json.loads(sys.stdin.read()):
    parsed = _ConnectionStringParser(validate_keywords=True)._parse(text)
    normal = _ConnectionStringParser._normalize_params(parsed, warn_rejected=False)
    rebuilt = _ConnectionStringBuilder(normal).build()
    out.append([parsed, rebuilt])
print(json.dumps(out))
"""


def test_the_installed_driver_reads_a_hostile_login_as_one_value_and_builds_no_new_keyword():
    """The driver parses the string of the tool and builds its own for ODBC: both steps are read."""
    cases = [case for hostile in HOSTILE for case in hostile_cases(hostile)]
    texts = [sent_connection_string(user, password)[0] for user, password in cases]
    ran = subprocess.run(
        [sys.executable, "-c", _DRIVER_READS],
        input=json.dumps(texts),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if ran.returncode == 3:
        pytest.skip("mssql-python is not installed (extra 'db')")
    assert ran.returncode == 0, ran.stderr
    for (user, password), (parsed, rebuilt) in zip(
        cases, json.loads(ran.stdout.strip().splitlines()[-1]), strict=True
    ):
        assert parsed == {
            "server": SERVER,
            "database": DATABASE,
            "encrypt": "yes",
            "trustservercertificate": "no",
            "connectretrycount": "0",
            "uid": user,
            "pwd": password,
        }
        assert sorted(read_odbc_string(rebuilt)) == sorted(
            [
                ("Server", SERVER),
                ("Database", DATABASE),
                ("Encrypt", "yes"),
                ("TrustServerCertificate", "no"),
                ("ConnectRetryCount", "0"),
                ("UID", user),
                ("PWD", password),
            ]
        )


def test_an_access_token_keeps_its_token_out_of_its_repr():
    assert "jwt" not in repr(AccessToken("jwt", 1))
