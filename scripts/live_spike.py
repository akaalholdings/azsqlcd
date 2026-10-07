"""Live spike for azsqlcd: proves on a real Azure SQL Database what the design only assumes.

Owner-run. This script connects to a database and writes to it. It never runs in pytest or CI.
Read docs/live-testing.md first.

    az login
    uv run --extra db python scripts/live_spike.py --server S --database D
        --confirm-disposable-database D [--items L1,L5] [--out DIR]       (one line)
    uv run python scripts/live_spike.py --list            print the items; no connection is made

Guards, all required, checked before the first write:
  - --confirm-disposable-database repeats --database exactly, and DB_NAME() is that name;
  - the name does not contain "prod";
  - SERVERPROPERTY('EngineEdition') is 5 (Azure SQL Database);
  - the database has no azsqlcd.meta row, or the row says environment 'disposable'.

Every object the script makes is in schema [azsqlcd_spike]. The script drops the objects of that
schema, the schema, and the test user [azsqlcd_spike_user] at the start (leftovers of a run that
was stopped) and at the end. A statement that would make a second schema runs in a transaction
that is rolled back.

Each item is one function. It returns {id, title, result, observed, decides}; result is pass,
fail, inconclusive or manual. "decides" says what the result changes in the tool.

Output under --out (default tests/fixtures/live/):
  spike/<id>.json      one file for each item that ran
  spike/summary.json   results of every item file in the folder, the driver gates, the constants
  error_texts.json     engine error texts as the driver returns them
  catalog_rows.json    sys.sql_modules text, sys.dm_sql_referenced_entities rows, the rows of a
                       temporal table, the engine form of each masking function
  normal_forms.json    how the engine writes DEFAULT, CHECK, computed and filter expressions
Server, database and login names are replaced in every file. Read the files before a commit.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.metadata
import json
import multiprocessing
import platform
import re
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from azsqlcd import __version__, catalog, lex, modules, names, runner, sqlerrors
from azsqlcd import session as db
from azsqlcd.errors import ToolError, refused
from azsqlcd.session import AccessToken, AzureCliTokenProvider, Session, TokenProvider
from azsqlcd.sqlerrors import ErrorClass, SqlError
from azsqlcd.state import rows

SPIKE_SCHEMA = "azsqlcd_spike"
SPIKE_USER = "azsqlcd_spike_user"
DISPOSABLE = "disposable"
APP_NAME = f"azsqlcd/{__version__} live"
DEFAULT_OUT = "tests/fixtures/live"
LOCK_RESOURCE = "azsqlcd_spike:lock"  # never the lock of the tool: a spike takes no part in a deploy

PASS, FAIL, INCONCLUSIVE, MANUAL = "pass", "fail", "inconclusive", "manual"

# L1 to L17 are the items of the blueprint (live_spikes). L5b is the second half of L5. X1 to X10
# are probes that the build added: they close "unverified_live" entries of the module reports.
ITEM_IDS = (
    "L1",
    "L2",
    "L3",
    "L4",
    "L5",
    "L5b",
    "L6",
    "L7",
    "L8",
    "L9",
    "L10",
    "L11",
    "L12",
    "L13",
    "L14",
    "L15",
    "L16",
    "L17",
    "X1",
    "X2",
    "X3",
    "X4",
    "X5",
    "X6",
    "X7",
    "X8",
    "X9",
    "X10",
)
TITLES = {
    "L1": "token connect, pooling off, session options (G-D5)",
    "L2": "an error in statement 2..n of a batch is seen (G-D1)",
    "L3": "compile error and deferred compile error inside a transaction",
    "L4": "batch text with ? and { } arrives byte-identical (G-D3)",
    "L5": "a killed idle session is not replaced silently (G-D2)",
    "L5b": "close during a transaction leaves no row",
    "L6": "locking read of the fence row after a killed session (reconcile)",
    "L7": "what sys.sql_modules stores after CREATE OR ALTER and after ALTER",
    "L8": "engine normal forms of DEFAULT, CHECK, computed and filter expressions",
    "L9": "catalog reads inside the open transaction, 500 objects",
    "L10": "SET PARSEONLY ON through the driver (canary)",
    "L11": "ordering facts: deferred names, function in a CHECK, chained SCHEMABINDING",
    "L12": "serverless auto-resume inside the connect budget",
    "L13": "token expiry on an open session (soak)",
    "L14": "rights of a db_ddladmin member (second principal)",
    "L15": "session applock: owner, second session, kill, close",
    "L16": "fence columns; sp_refreshsqlmodule failure rolls back cleanly",
    "L17": "nontx: killed RESUMABLE build, ADD NOT NULL with a default",
    "X1": "error texts of 208, 2714, 1222, 1205, 2627, 245 as the driver returns them",
    "X2": "guard values after a run-time error with XACT_ABORT ON",
    "X3": "dependants after a column or table drop: errors 207, 208, 2020, #temp tables (A12)",
    "X4": "CREATE USER ... WITH SID = ..., TYPE = E",
    "X5": "a session killed in the COMMIT batch raises (G-D4)",
    "X6": "sequence: what ALTER SEQUENCE ... RESTART WITH does to start_value",
    "X7": "sys.numbered_procedures, and sys.indexes of a view (export facts)",
    "X8": "the session-option row with IMPLICIT_TRANSACTIONS on and off",
    "X9": "temporal table: sys.tables.temporal_type, sys.periods, drop with versioning off",
    "X10": "masked columns: sys.masked_columns and the form of each masking function",
}
# Design (k): any gate that fails means the pyodbc adapter comes before any production use.
GATES = {"G-D1": "L2", "G-D2": "L5", "G-D3": "L4", "G-D4": "X5", "G-D5": "L1"}
CONSTANTS = {"MODULE_TEXT_READBACK": "L7", "RECONCILE_BY_LOCKING_READ": "L6"}

# The options that runner.py sets on its session, so a probe sees what a deploy sees. L1 calls the
# function of the runner itself and records the language of a session before any SET.
SESSION_OPTIONS = (
    "SET XACT_ABORT ON; SET LOCK_TIMEOUT 30000; SET NOCOUNT ON; SET IMPLICIT_TRANSACTIONS OFF; "
    "SET ANSI_NULLS, QUOTED_IDENTIFIER, ANSI_PADDING, ANSI_WARNINGS, ARITHABORT, "
    "CONCAT_NULL_YIELDS_NULL ON; SET NUMERIC_ROUNDABORT OFF; SET LANGUAGE us_english;"
)
GUARD = "SELECT @@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID();"
ROLLBACK = "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;"
SPID = "SELECT @@SPID;"
# The two batches that plan sends to leave its syntax check (plan._PARSEONLY_OFF, _PARSEONLY_IS_OFF)
PARSEONLY_OFF = "SET PARSEONLY OFF;"
PARSEONLY_IS_OFF = "/* azsqlcd:parseonly_off */ SELECT @@SPID;"
CONNECTION_ID = (
    "SELECT CAST([connection_id] AS char(36)) FROM sys.dm_exec_connections WHERE [session_id] = @@SPID;"
)
DROP_PASSES = 4
L9_OBJECTS = 500
L9_MAX_SECONDS = 10.0
SOAK_INTERVAL_S = 300

type Connect = Callable[[], Session]


# ------------------------------------------------------------------ guards
_GUARD_FACTS = (
    "/* azsqlcd:live_guard */ SELECT CAST(SERVERPROPERTY(N'EngineEdition') AS int), DB_NAME(), "
    "OBJECT_ID(N'[azsqlcd].[meta]', N'U');"
)
_GUARD_META = "/* azsqlcd:live_guard_meta */ SELECT [environment] FROM [azsqlcd].[meta];"


def refuse_unconfirmed(database: str, confirm: str) -> None:
    """The two guards that need no connection. Raises ToolError 22."""
    if confirm != database:
        raise refused(
            "LIVE_NOT_CONFIRMED",
            "--confirm-disposable-database must repeat --database exactly. No connection was made",
        )
    if "prod" in database.casefold():
        raise refused(
            "LIVE_PROD_NAME",
            f"the database name {database!r} contains 'prod'. A live test never runs there. No "
            "connection was made",
        )


def refuse_unless_disposable(session: Session, database: str, confirm: str) -> None:
    """All guards. Sends two SELECT batches and nothing else. Raises ToolError 22."""
    refuse_unconfirmed(database, confirm)
    edition, db_name, meta_id = catalog.one_row(session, _GUARD_FACTS)
    if db_name != database:
        raise refused(
            "LIVE_DB_NAME",
            f"the session is in database {db_name!r}; the confirmed name is {database!r}. Nothing was "
            "written",
            db_name=db_name,
        )
    if edition != 5:
        raise refused(
            "LIVE_ENGINE_EDITION",
            f"this is not Azure SQL Database (engine edition {edition!r}). Nothing was written",
            engine_edition=edition,
        )
    if meta_id is not None:
        bound = [str(row[0]) for row in rows(session, _GUARD_META)]
        if any(environment != DISPOSABLE for environment in bound):
            raise refused(
                "LIVE_BOUND_ENVIRONMENT",
                f"this database is bound to environment {bound[0]!r} (azsqlcd.meta). A live test runs "
                "only where that row is absent or says 'disposable'. Nothing was written",
                environment=bound[0],
            )


# ------------------------------------------------------------------ connection
class CachedTokenProvider:
    """One call of az for many connects: a token is used again while it has ten minutes left."""

    def __init__(self, inner: TokenProvider, now: Callable[[], float] = time.time) -> None:
        self._inner = inner
        self._now = now
        self._token: AccessToken | None = None

    def get(self) -> AccessToken:
        if self._token is None or self._token.expires_on - self._now() < 600:
            self._token = self._inner.get()
        return self._token


def load_driver() -> Any:
    """The driver module, for open_session(). Only the live scripts and tests/live load it here."""
    try:
        return importlib.import_module("mssql_python")
    except ImportError:
        raise refused(
            "DRIVER_MISSING", "mssql-python is not installed; start the script with `uv run --extra db`"
        ) from None


def recording_connect(log: list[dict[str, Any]]) -> db.DriverConnect:
    """A connect attempt that also writes its duration, and the full text of its error, to log."""
    driver = load_driver()

    def connect_once(keywords: Mapping[str, str], token_struct: bytes) -> Session:
        started = time.monotonic()
        try:
            opened = db.open_session(driver, keywords, token_struct)
        except SqlError as error:
            seconds = round(time.monotonic() - started, 2)
            log.append({"connected": False, "seconds": seconds, "error": error_facts(error)})
            raise
        log.append({"connected": True, "seconds": round(time.monotonic() - started, 2)})
        return opened

    return connect_once


def live_connect(server: str, database: str, provider: TokenProvider) -> Session:
    """One session as the tool opens it: session.connect, token of the Azure CLI login."""
    return db.connect(server, database, provider, APP_NAME)


def kill(killer: Session, spid: int) -> None:
    """KILL one session of this script. Needs the right KILL DATABASE CONNECTION."""
    if type(spid) is not int:
        raise ValueError("a session id is a whole number")
    killer.execute(f"KILL {spid};")


def wait_until(check: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


# ------------------------------------------------------------------ a session in a helper process
def _background_main(pipe: Any, server: str, database: str) -> None:
    """Body of the helper process: one session that runs the batches which the parent sends."""
    try:
        opened = live_connect(server, database, AzureCliTokenProvider())
        opened.execute(SESSION_OPTIONS)
        (spid,) = catalog.one_row(opened, SPID)
    except Exception as error:  # the parent must hear of every failure, whatever it is
        pipe.send(("failed", f"{type(error).__name__}: {error}"))
        return
    pipe.send(("spid", spid))
    while True:
        message = pipe.recv()
        if message is None:
            break
        delay_s, batch = message
        time.sleep(delay_s)
        pipe.send(("done", attempt(opened, batch)))
    opened.close()


class Background:
    """A session in a helper process, for a batch that must run while this process does other work.

    A process, not a thread: it is not known that the driver lets a second thread run while a
    batch waits for the server.
    """

    def __init__(self, server: str, database: str) -> None:
        context = multiprocessing.get_context("spawn")
        self._pipe, child_end = context.Pipe()
        self._process = context.Process(
            target=_background_main, args=(child_end, server, database), daemon=True
        )
        self._process.start()
        kind, value = self._receive(db.CONNECT_BUDGET_S + 60)
        if kind != "spid":
            self.close()
            raise RuntimeError(f"the helper process has no session: {value}")
        self.spid: int = value

    def submit(self, batch: str, delay_s: float = 0.0) -> None:
        """Send the batch after delay_s seconds. Does not wait."""
        self._pipe.send((delay_s, batch))

    def result(self, timeout_s: float) -> dict[str, Any] | None:
        """What attempt() gave for the submitted batch. None: it did not end in time."""
        kind, value = self._receive(timeout_s)
        return value if kind == "done" else None

    def _receive(self, timeout_s: float) -> tuple[str, Any]:
        if not self._pipe.poll(timeout_s):
            return "timeout", None
        try:
            return self._pipe.recv()
        except EOFError:
            return "failed", "the helper process ended"

    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._pipe.send(None)
        self._process.join(5)
        if self._process.is_alive():
            self._process.terminate()


# ------------------------------------------------------------------ observations and output
def error_facts(error: SqlError) -> dict[str, Any]:
    return {
        "class": error.cls.name,
        "number": error.number,
        "sqlstate": error.sqlstate,
        "text": error.raw_message,  # full text: a disposable database holds nothing private
        "redacted": error.message,
    }


def attempt(session: Session, batch: str) -> dict[str, Any]:
    """Send one batch. An error of the engine is an observation here, not a failure of the script."""
    started = time.monotonic()
    try:
        result = session.execute(batch)
    except SqlError as error:
        seconds = round(time.monotonic() - started, 3)
        return {"raised": True, "seconds": seconds, "error": error_facts(error)}
    seconds = round(time.monotonic() - started, 3)
    return {"raised": False, "seconds": seconds, "result_sets": [[list(r) for r in rs] for rs in result]}


def guard(session: Session) -> list[Any]:
    """[@@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID()]: the guard of the runner (A5)."""
    return list(catalog.one_row(session, GUARD))


def first_value(outcome: Mapping[str, Any]) -> Any:
    """The first column of the first row of an attempt(); None when there is none."""
    result_sets = outcome.get("result_sets") or []
    return result_sets[0][0][0] if result_sets and result_sets[0] else None


def plain(value: Any, hide: Mapping[str, str]) -> Any:
    """A JSON value of the data, with every private name replaced (server, database, login)."""
    if isinstance(value, Mapping):
        return {str(key): plain(item, hide) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item, hide) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = "0x" + bytes(value).hex() if isinstance(value, (bytes, bytearray)) else str(value)
    for private, public in hide.items():
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(private)}(?![A-Za-z0-9_])", public, text, flags=re.I)
    return text


def private_names(server: str, database: str, logins: Sequence[str] = ()) -> dict[str, str]:
    """What must not reach a file that can be committed. Longest first: a short name can be a part."""
    found = {server: "SERVER_NAME", server.split(".")[0]: "SERVER_NAME", database: "DATABASE_NAME"}
    found |= {login: "LOGIN_NAME" for login in logins if login}
    return {name: found[name] for name in sorted(found, key=len, reverse=True) if name}


def write_json(path: Path, data: Any) -> None:
    """Write JSON. Data that the database gave must go through plain(data, hide) first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline: a fixture file has LF line ends on every platform (the default gives CRLF on Windows)
    path.write_text(
        json.dumps(plain(data, {}), indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
    )


def merge_json(path: Path, data: Mapping[str, Any]) -> None:
    """Add the keys of data to the JSON object in the file: a run of some items keeps the others."""
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    write_json(path, {**existing, **data})


def print_table(table: Sequence[Sequence[str]]) -> None:
    widths = [max(len(row[column]) for row in table) for column in range(len(table[0]))]
    for row in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())


# ------------------------------------------------------------------ cleanup
_DROP_KIND = {
    "V": "VIEW",
    "P": "PROCEDURE",
    "U": "TABLE",
    "FN": "FUNCTION",
    "IF": "FUNCTION",
    "TF": "FUNCTION",
    "SO": "SEQUENCE",
    "SN": "SYNONYM",
}


def _in_schema(session: Session, schema: str, batch: str) -> list[tuple[Any, ...]]:
    """Rows of a query that names the schema. A row of another schema stops the caller at once."""
    found = rows(session, batch)
    foreign = [row for row in found if row[0] != schema]
    if foreign:
        raise RuntimeError(f"a cleanup query for schema {schema} gave a row of schema {foreign[0][0]!r}")
    return found


def drop_schema_objects(session: Session, schema: str) -> list[str]:
    """Drop every object of one schema, then the schema. Returns the statements that were sent.

    Call it only after the guards passed. Triggers and constraints go with their table. An
    object that another one still needs is dropped in a later pass.
    """
    name = names.sql_literal(schema)
    objects = (
        "/* azsqlcd:live_objects */ SELECT s.[name], o.[name], RTRIM(o.[type]) FROM sys.objects AS o "
        "JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id] "
        f"WHERE s.[name] = {name} AND o.[parent_object_id] = 0 AND o.[is_ms_shipped] = 0 "
        "ORDER BY o.[create_date] DESC, o.[object_id] DESC;"
    )
    resumable = (
        "/* azsqlcd:live_resumable */ SELECT s.[name], o.[name], r.[name] "
        "FROM sys.index_resumable_operations AS r JOIN sys.objects AS o ON o.[object_id] = r.[object_id] "
        f"JOIN sys.schemas AS s ON s.[schema_id] = o.[schema_id] WHERE s.[name] = {name};"
    )
    sent: list[str] = []

    def send(statement: str) -> None:
        sent.append(statement)
        with contextlib.suppress(SqlError):  # what stays is found by the count at the end
            session.execute(statement)

    for _, table, index in _in_schema(session, schema, resumable):
        # a paused index build stops DROP TABLE
        send(f"ALTER INDEX {names.quote(index)} ON {names.qualified(schema, table)} ABORT;")
    for _ in range(DROP_PASSES):
        found = _in_schema(session, schema, objects)
        if not found:
            break
        unknown = sorted({str(code) for _, _, code in found} - set(_DROP_KIND))
        if unknown:
            raise RuntimeError(
                f"schema {schema} holds an object type that the cleanup does not know: {unknown}"
            )
        for _, object_name, code in sorted(found, key=lambda row: list(_DROP_KIND).index(row[2])):
            send(f"DROP {_DROP_KIND[code]} {names.qualified(schema, object_name)};")
    left = _in_schema(session, schema, objects)
    if left:
        raise RuntimeError(f"the cleanup of schema {schema} is not complete: {len(left)} object(s) remain")
    drop = f"IF SCHEMA_ID({name}) IS NOT NULL DROP SCHEMA {names.quote(schema)};"
    session.execute(drop)
    return [*sent, drop]


def remove_spike_objects(session: Session) -> list[str]:
    """Everything this script can leave behind: schema azsqlcd_spike and the one test user."""
    sent = drop_schema_objects(session, SPIKE_SCHEMA)
    user = (
        f"IF DATABASE_PRINCIPAL_ID({names.sql_literal(SPIKE_USER)}) IS NOT NULL "
        f"DROP USER {names.quote(SPIKE_USER)};"
    )
    session.execute(user)
    return [*sent, user]


# ------------------------------------------------------------------ run context
@dataclass(frozen=True)
class Options:
    server: str
    database: str
    confirm: str
    out: Path
    items: tuple[str, ...]
    second_principal: str | None = None
    expect_paused: bool = False
    soak_past_expiry_minutes: int = 0
    repetitions: int = 20
    rows: int = 300_000
    compare_normal_forms: Path | None = None
    list_items: bool = False


@dataclass(frozen=True)
class ItemResult:
    id: str
    title: str
    result: str  # pass | fail | inconclusive | manual
    observed: dict[str, Any]
    decides: str


@dataclass
class Fixtures:
    """What the items captured for tests/fixtures/live."""

    errors: dict[str, Any] = field(default_factory=dict)
    catalog_rows: dict[str, Any] = field(default_factory=dict)
    normal_forms: dict[str, Any] | None = None

    def error(self, label: str, expected_number: int | None, outcome: Mapping[str, Any]) -> None:
        """Keep the error of an attempt() under a label. An attempt that raised nothing adds nothing."""
        if outcome.get("raised"):
            self.errors[label] = {"expected_number": expected_number, **outcome["error"]}


@dataclass
class Ctx:
    """What an item works with. Sessions that an item opens are closed after the item."""

    options: Options
    connect: Connect
    main: Session  # the session of the owner: setup, checks from outside, KILL
    provider: TokenProvider | None = None  # None: the connect function was given by a test
    first_connect: list[dict[str, Any]] = field(default_factory=list)
    fixtures: Fixtures = field(default_factory=Fixtures)
    _sessions: list[Session] = field(default_factory=list)
    _helpers: list[Background] = field(default_factory=list)

    def open(self, *, options: bool = True) -> Session:
        """A new session. options False: exactly as the driver gives it."""
        opened = self.connect()
        self._sessions.append(opened)
        if options:
            opened.execute(SESSION_OPTIONS)
        return opened

    def track(self, opened: Session) -> Session:
        self._sessions.append(opened)
        return opened

    def background(self) -> Background:
        helper = Background(self.options.server, self.options.database)
        self._helpers.append(helper)
        return helper

    def close_opened(self) -> None:
        for helper in self._helpers:
            helper.close()
        for opened in self._sessions:
            opened.close()
        self._helpers.clear()
        self._sessions.clear()


def obj(name: str) -> str:
    """[azsqlcd_spike].[name]"""
    return names.qualified(SPIKE_SCHEMA, name)


def lit(text: str) -> str:
    return names.sql_literal(text)


def key(kind: str, name: str) -> str:
    return names.object_key(kind, SPIKE_SCHEMA, name)


def done(item_id: str, result: str, observed: dict[str, Any], decides: str) -> ItemResult:
    return ItemResult(item_id, TITLES[item_id], result, observed, decides)


def item_json(result: ItemResult, hide: Mapping[str, str]) -> dict[str, Any]:
    """The file content of one item. Only observed holds text of the database, so only it is scrubbed."""
    return {
        "id": result.id,
        "title": result.title,
        "result": result.result,
        "observed": plain(result.observed, hide),
        "decides": result.decides,
    }


def take_lock(session: Session, wait_ms: int) -> Any:
    """sp_getapplock in the batch shape of the runner. 0 or 1 = granted."""
    (result,) = catalog.one_row(
        session,
        f"DECLARE @r int; EXEC @r = sys.sp_getapplock @Resource = {lit(LOCK_RESOURCE)}, "
        f"@LockMode = N'Exclusive', @LockOwner = N'Session', @LockTimeout = {int(wait_ms)}; SELECT @r;",
    )
    return result


def lock_mode(session: Session) -> Any:
    return catalog.one_row(session, f"SELECT APPLOCK_MODE(N'public', {lit(LOCK_RESOURCE)}, N'Session');")[0]


def lock_is_free(session: Session) -> Any:
    """APPLOCK_TEST as plan uses it (A4): 1 = free, 0 = a session holds the lock."""
    return catalog.one_row(
        session, f"SELECT APPLOCK_TEST(N'public', {lit(LOCK_RESOURCE)}, N'Exclusive', N'Session');"
    )[0]


def in_transaction(session: Session, batch: str) -> tuple[dict[str, Any], list[Any]]:
    """BEGIN TRANSACTION, the batch, the guard, ROLLBACK. Returns (attempt, guard after it)."""
    session.execute("BEGIN TRANSACTION;")
    outcome = attempt(session, batch)
    after = guard(session)
    session.execute(ROLLBACK)
    return outcome, after


def engine_stored_text(sent: str) -> str:
    """What sys.sql_modules.definition holds after the engine ran the module batch `sent`.

    Measured by item L7 on Azure SQL Database (11 header shapes, 6 module kinds): the engine keeps
    the batch byte for byte, with one edit in the verb. CREATE OR ALTER: the words OR and ALTER
    are deleted; the white space and the comments around them stay ("CREATE   VIEW"). ALTER: the
    word becomes CREATE. CREATE: no change. This is the expectation of the probes. The tool has
    its own statement of the rule in modules.py; L7 compares the two through the read-back of the
    runner. Raises LexError when the text has no module header.
    """
    header = lex.module_header(sent)
    if header.verb == "CREATE":
        return sent
    if header.verb == "ALTER":
        start, end = header.verb_span
        return sent[:start] + "CREATE" + sent[end:]
    for word in reversed(lex.significant(lex.tokenize(sent))[1:3]):  # ALTER, then OR
        sent = sent[: word.pos] + sent[word.pos + len(word.text) :]
    return sent


# ------------------------------------------------------------------ L1
def _driver_version() -> str | None:
    try:
        return importlib.metadata.version("mssql-python")
    except importlib.metadata.PackageNotFoundError:
        return None


def l1_token_connect(ctx: Ctx) -> ItemResult:
    first = ctx.open(options=False)
    spid, language = catalog.one_row(first, "SELECT @@SPID, @@LANGUAGE;")
    connection_id = rows(first, CONNECTION_ID)[0][0]
    first.close()
    second = ctx.open()

    def gone() -> bool:
        count = f"SELECT COUNT(*) FROM sys.dm_exec_connections WHERE [connection_id] = {lit(connection_id)};"
        return catalog.one_row(second, count)[0] == 0

    pooling_off = wait_until(gone, 5)
    try:
        # the real function of the runner: it sets the options and then asserts what it reads back
        runner.set_session_options(second, 5000)
        options: dict[str, Any] = {"asserted": True}
    except ToolError as error:
        options = {"asserted": False, "reason_code": error.reason_code, **error.detail}
    values = catalog.one_row(
        second,
        "SELECT CAST(1 AS bit), CAST(1 AS tinyint), CAST(1 AS bigint), CAST(1.5 AS decimal(5, 2)), "
        "SYSUTCDATETIME(), CAST(N'x' AS nchar(2)), CAST(0x01 AS varbinary(4)), NEWID(), CAST(NULL AS int);",
    )
    minutes = None
    if ctx.provider is not None:
        minutes = int((ctx.provider.get().expires_on - time.time()) // 60)
    observed = {
        "spid": spid,
        "language_before_any_set": language,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "driver": _driver_version(),
        "first_connect_attempts": ctx.first_connect,
        "token_minutes_left": minutes,
        "pooling_off": pooling_off,
        "runner_session_options": options,
        "value_types": [type(value).__name__ for value in values],
    }
    ok = type(spid) is int and pooling_off and options["asserted"]
    return done(
        "L1",
        PASS if ok else FAIL,
        observed,
        "Gate G-D5 on this machine. Run this item also on Windows with Python 3.14 and on the runner "
        "image. pooling_off false: a closed session keeps its applock, so the driver cannot be used. "
        "runner_session_options not asserted: every deploy would stop with SESSION_OPTIONS. "
        "language_before_any_set other than us_english: a session that does not go through the runner "
        "(the session of plan) gets engine messages that sqlerrors.py cannot read; it must send SET "
        "LANGUAGE us_english too.",
    )


# ------------------------------------------------------------------ L2
# {t} is a table with the one column [id] int NOT NULL PRIMARY KEY. tests/live uses the same list.
L2_CASES: tuple[tuple[str, str], ...] = (
    (
        "error in statement 2 of 3, no result set before it",
        "INSERT INTO {t} ([id]) VALUES (1); INSERT INTO {t} ([id]) VALUES (1); "
        "INSERT INTO {t} ([id]) VALUES (2);",
    ),
    (
        "error in statement 3 of 3, no result set before it",
        "INSERT INTO {t} ([id]) VALUES (1); INSERT INTO {t} ([id]) VALUES (2); "
        "INSERT INTO {t} ([id]) VALUES (1);",
    ),
    ("error in statement 2 of 3, after a result set", "SELECT 1 AS [a]; SELECT 1/0 AS [b]; SELECT 3 AS [c];"),
    (
        "error in statement 3 of 3, after two result sets",
        "SELECT 1 AS [a]; SELECT 2 AS [b]; SELECT 1/0 AS [c];",
    ),
    (
        "error in statement 2 of 3, between two reads of the table",
        "SELECT [id] FROM {t}; INSERT INTO {t} ([id]) VALUES (1), (1); SELECT [id] FROM {t};",
    ),
    (
        "RAISERROR of severity 16 after a result set (XACT_ABORT does not stop the batch)",
        "SELECT 1 AS [a]; RAISERROR(N'azsqlcd spike', 16, 1); SELECT 2 AS [b];",
    ),
    ("THROW after a result set", "SELECT 1 AS [a]; THROW 51000, N'azsqlcd spike', 1; SELECT 2 AS [b];"),
    (
        "error in statement 2 with NOCOUNT OFF (row counts between the statements)",
        "SET NOCOUNT OFF; INSERT INTO {t} ([id]) VALUES (1); INSERT INTO {t} ([id]) VALUES (1);",
    ),
    ("error after a PRINT message", "PRINT N'azsqlcd spike'; SELECT 1/0 AS [a];"),
)


def later_statement_cases(session: Session, table: str) -> list[dict[str, Any]]:
    """Run L2_CASES, each in its own transaction. detected = the tool would see the error."""
    cases: list[dict[str, Any]] = []
    for name, batch in L2_CASES:
        session.execute("SET NOCOUNT ON;")
        outcome, after = in_transaction(session, batch.replace("{t}", table))
        detected = outcome["raised"] or after[:2] != [1, 1]
        cases.append({"case": name, "detected": detected, "attempt": outcome, "guard_after": after})
    return cases


def l2_later_statement_error(ctx: Ctx) -> ItemResult:
    table = obj("l2_t")
    ctx.main.execute(f"CREATE TABLE {table} ([id] int NOT NULL CONSTRAINT [l2_pk] PRIMARY KEY);")
    cases = later_statement_cases(ctx.open(), table)
    missed = [case["case"] for case in cases if not case["detected"]]
    return done(
        "L2",
        FAIL if missed else PASS,
        {"cases": cases, "not_detected": missed},
        "Gate G-D1. pass: the drain of session.py and the guard after every batch are enough. fail: "
        "an error can pass unseen through this driver, so the pyodbc adapter is needed.",
    )


# ------------------------------------------------------------------ L3
def l3_compile_errors(ctx: Ctx) -> ItemResult:
    table, new = obj("l3_t"), obj("l3_new")
    ctx.main.execute(f"CREATE TABLE {table} ([a] int NULL);")
    batches = (
        ("syntax error: the batch does not parse", "SELECT 1 AS [a]; SELECT FROM;"),
        ("unknown column of a table that exists: the batch does not compile", f"SELECT [nope] FROM {table};"),
        (
            "unknown table: compiled when the statement runs",
            f"SELECT 1 AS [a]; SELECT [a] FROM {obj('l3_no')};",
        ),
        (
            "unknown column of a table that the same batch makes: compiled when the statement runs",
            f"CREATE TABLE {new} ([a] int NULL); SELECT [nope] FROM {new};",
        ),
        ("conversion error at run time", "SELECT 1 AS [a]; SELECT CAST('abc' AS int) AS [b];"),
    )
    session = ctx.open()
    cases: list[dict[str, Any]] = []
    for name, batch in batches:
        outcome, after = in_transaction(session, batch)
        ctx.fixtures.error(f"L3 {name}", None, outcome)
        detected = outcome["raised"] or after[:2] != [1, 1]
        cases.append({"case": name, "detected": detected, "attempt": outcome, "guard_after": after})
    missed = [case["case"] for case in cases if not case["detected"]]
    return done(
        "L3",
        FAIL if missed else PASS,
        {"cases": cases, "not_detected": missed},
        "guard_after tells which errors leave the transaction open (1, 1, id) and which end it (0, 1, "
        "new id: outside a transaction XACT_STATE() reads 1 in a statement with CURRENT_TRANSACTION_ID()). "
        "The runner rolls back with IF @@TRANCOUNT > 0 ROLLBACK and then reads @@TRANCOUNT = 0; both "
        "shapes must be in the list. fail: a compile error passed unseen, see gate G-D1.",
    )


# ------------------------------------------------------------------ L4
# Text that an ODBC layer could read as a parameter marker or an escape clause. Every piece is
# inside a literal, a comment or a bracket name, where it must stay untouched.
L4_LITERAL = "? {d '2022-03-04'} {call sp_who(?)} {fn CURDATE()} {t '01:02:03'} {?= call f(?)}"
L4_ECHO_BATCH = (
    "/* echo ? {d '2020-01-01'} {call x(?)} {fn NOW()} */ "
    "SELECT " + names.sql_literal(L4_LITERAL) + " AS [v ? {fn LCASE('y')}], t.[text] "
    "FROM sys.dm_exec_requests AS r CROSS APPLY sys.dm_exec_sql_text(r.[sql_handle]) AS t "
    "WHERE r.[session_id] = @@SPID; -- ? {ts '2021-02-03 04:05:06'}"
)
_L4_NAME = "l4 ? {d '2020-01-01'} {fn NOW()} {call x(?)}"


def l4_batch_text(ctx: Ctx) -> ItemResult:
    session = ctx.open()
    text = (
        "CREATE OR ALTER PROCEDURE " + obj(_L4_NAME) + "\n"
        "AS\n"
        "-- line comment ? {d '2021-02-03'} {call sp_who(?)} {fn UCASE('x')}\n"
        "/* block comment ? {ts '2021-02-03 04:05:06'} {oj a LEFT OUTER JOIN b ON 1 = 1} */\n"
        "SELECT " + lit(L4_LITERAL) + " AS [col ? {fn LCASE('y')}], '{d ''1999-12-31''}' AS [plain ?];\n"
    )
    created = attempt(session, text)
    if created["raised"]:
        return done(
            "L4",
            INCONCLUSIVE,
            {"create": created},
            "Gate G-D3 is open. The engine refused the module. Read the error: when it shows text that "
            "differs from the text in this script, the driver changed the batch and the gate fails.",
        )
    stored = rows(
        session,
        "SELECT o.[name], m.[definition] FROM sys.objects AS o "
        "JOIN sys.sql_modules AS m ON m.[object_id] = o.[object_id] "
        f"WHERE o.[schema_id] = SCHEMA_ID({lit(SPIKE_SCHEMA)}) AND o.[name] LIKE N'l4 %';",
    )
    executed = attempt(session, "EXEC " + obj(_L4_NAME) + ";")
    echoed = attempt(session, L4_ECHO_BATCH)
    echo_row = (echoed.get("result_sets") or [[]])[0]
    observed = {
        "name_equal": bool(stored) and stored[0][0] == _L4_NAME,
        # the engine stores the batch with the verb CREATE (item L7); every other byte must be equal
        "definition_equal": bool(stored) and stored[0][1] == engine_stored_text(text),
        "definition": stored[0][1] if stored else None,
        "executed": executed,
        "literal_equal": first_value(executed) == L4_LITERAL,
        "echo": echoed,
        # the batch as the server received it; None when the request view gave no row
        "echo_equal": (echo_row[0] == [L4_LITERAL, L4_ECHO_BATCH]) if echo_row else None,
    }
    ok = observed["name_equal"] and observed["definition_equal"] and observed["literal_equal"]
    ok = ok and observed["echo_equal"] is not False
    return done(
        "L4",
        PASS if ok else FAIL,
        observed,
        "Gate G-D3. pass: file bytes reach the engine unchanged, also the capture JSON that state.py "
        "sends inside a literal. fail: the pyodbc adapter is needed, or the adapter must switch the "
        "escape scan off (SQL_ATTR_NOSCAN).",
    )


# ------------------------------------------------------------------ L5, L5b
def _after_kill(ctx: Ctx, victim: Session, probe: str) -> dict[str, Any]:
    (spid,) = catalog.one_row(victim, SPID)
    kill(ctx.main, spid)
    time.sleep(1.0)  # the session is idle: the client learns of the kill only when it sends again
    first = attempt(victim, probe)
    return {"first_execute": first, "second_execute": attempt(victim, probe), "closed_flag": victim.closed}


def l5_killed_idle_session(ctx: Ctx) -> ItemResult:
    holder = ctx.open()
    granted = take_lock(holder, 0)
    with_lock = _after_kill(
        ctx, holder, f"SELECT @@SPID, APPLOCK_MODE(N'public', {lit(LOCK_RESOURCE)}, N'Session');"
    )
    # no SET, no lock, no temporary table: the session that a driver could most easily replace
    bare = ctx.open(options=False)
    before = rows(bare, CONNECTION_ID)[0][0]
    without_state = _after_kill(ctx, bare, CONNECTION_ID)
    replaced = [
        name
        for name, case in (("with applock", with_lock), ("without session state", without_state))
        if not case["first_execute"]["raised"]
    ]
    observed = {
        "applock_granted": granted,
        "with_applock": with_lock,
        "without_session_state": without_state,
        "connection_id_before": before,
        "lock_free_after_kill": lock_is_free(ctx.main),
        "ran_on_a_new_session": replaced,
    }
    return done(
        "L5",
        FAIL if replaced else PASS,
        observed,
        "Gate G-D2. pass: with ConnectRetryCount=0 a dead session stays dead; the assert of @@SPID and "
        "APPLOCK_MODE in the BEGIN TRANSACTION batch is a second line only. fail: a batch can run on a "
        "new session without the lock, so the pyodbc adapter is needed. first_execute.error.class "
        "other than SESSION_LOST: the runner learns of the loss only at its next batch.",
    )


def l5b_close_in_transaction(ctx: Ctx) -> ItemResult:
    table = obj("l5b_step")
    ctx.main.execute(f"CREATE TABLE {table} ([id] int NOT NULL CONSTRAINT [l5b_pk] PRIMARY KEY);")
    writer = ctx.open()
    writer.execute(f"BEGIN TRANSACTION; INSERT INTO {table} ([id]) VALUES (1);")
    writer.close()
    reader = ctx.open()
    # a locking read waits for the rollback of the closed session and never reads its row
    counted = attempt(reader, f"SELECT COUNT(*) FROM {table} WITH (READCOMMITTEDLOCK);")
    count = first_value(counted)
    return done(
        "L5b",
        PASS if count == 0 else FAIL,
        {"rows_after_close": count, "read": counted},
        "pass: closing the connection is a safe stop (the driver has no cancel). fail: close() commits "
        "or keeps the session in a pool; the tool cannot stop a run safely with this driver.",
    )


# ------------------------------------------------------------------ L6
def l6_reconcile_by_locking_read(ctx: Ctx) -> ItemResult:
    fence, ballast = obj("l6_fence"), obj("l6_ballast")
    main = ctx.main
    main.execute(
        f"CREATE TABLE {fence} ([run_id] int NOT NULL CONSTRAINT [l6_pk] PRIMARY KEY, "
        "[segments_committed] int NOT NULL);"
    )
    main.execute(f"INSERT INTO {fence} ([run_id], [segments_committed]) VALUES (1, 0);")
    main.execute(
        f"CREATE TABLE {ballast} ([id] int IDENTITY(1, 1) NOT NULL CONSTRAINT [l6_ballast_pk] PRIMARY KEY, "
        "[filler] char(200) NOT NULL);"
    )
    bump = f"UPDATE {fence} SET [segments_committed] = [segments_committed] + 1 WHERE [run_id] = 1;"
    # rows that the engine must undo, so that the rollback of the killed session takes time
    undo_work = (
        f"INSERT INTO {ballast} ([filler]) SELECT TOP (5000) 'x' "
        "FROM sys.all_columns AS a CROSS JOIN sys.all_columns AS b;"
    )
    # the read of state.read_fence_locking, on the fence table of the spike
    read = f"SELECT [segments_committed] FROM {fence} WITH (READCOMMITTEDLOCK, ROWLOCK) WHERE [run_id] = 1;"
    reader = ctx.open()
    expected = 0
    blocked: dict[str, Any] | None = None
    repetitions: list[dict[str, Any]] = []
    for number in range(1, ctx.options.repetitions + 1):
        for commits in (False, True):
            writer = ctx.open()
            took = take_lock(writer, 0)
            (spid,) = catalog.one_row(writer, SPID)
            writer.execute(f"BEGIN TRANSACTION; {bump} {'' if commits else undo_work}")
            if commits:
                writer.execute("COMMIT TRANSACTION;")
                expected += 1
            elif blocked is None:
                # while the writer lives, the read must wait and time out; it must not read the old value
                reader.execute("SET LOCK_TIMEOUT 2000;")
                blocked = attempt(reader, read)
                ctx.fixtures.error("1222 locking read of a row with an open update", 1222, blocked)
            kill(main, spid)
            started = time.monotonic()
            reader.execute("SET LOCK_TIMEOUT 60000;")
            # the lock of a killed session is free only when the session is gone
            granted = take_lock(reader, 60000)
            outcome = attempt(reader, read)
            if granted in (0, 1):  # a lock that is not held cannot be released: the engine raises
                reader.execute(
                    f"EXEC sys.sp_releaseapplock @Resource = {lit(LOCK_RESOURCE)}, @LockOwner = N'Session';"
                )
            writer.close()
            repetitions.append(
                {
                    "repetition": number,
                    "writer_committed": commits,
                    "writer_lock": took,
                    "reader_lock": granted,
                    "read": first_value(outcome),
                    "expected": expected,
                    "seconds": round(time.monotonic() - started, 3),
                    "error": outcome.get("error"),
                }
            )
    wrong = [r for r in repetitions if r["read"] != r["expected"] or r["reader_lock"] not in (0, 1)]
    waits = blocked is not None and blocked["raised"]
    observed = {
        "read_while_the_writer_lives": blocked,
        "repetitions": repetitions,
        "wrong": wrong,
        "slowest_seconds": max((r["seconds"] for r in repetitions), default=None),
    }
    return done(
        "L6",
        PASS if waits and not wrong else FAIL,
        observed,
        "pass: runner.RECONCILE_BY_LOCKING_READ may be set True; a connection that is lost in a "
        "transaction then ends as exit 24 (start again) and not as exit 23. fail: it stays False and "
        "every lost connection in a unit of work needs a human. A loss of the network in the COMMIT "
        "itself is not in this item: see X5 and docs/live-testing.md, section 7.",
    )


# ------------------------------------------------------------------ L7
def _l7_modules() -> list[tuple[str, str, str]]:
    """(case, object name, text as a module file holds it)"""
    table = obj("l7_t")
    return [
        (
            "procedure with comments above the header",
            "l7_p",
            f"-- comment above the header\n/* block comment */\nCREATE OR ALTER PROCEDURE {obj('l7_p')}\n"
            "    @a int = 1\nAS\nSELECT @a AS [a];\n",
        ),
        (
            "procedure with AS between parameter and type, no parentheses",
            "l7_pa",
            f"CREATE OR ALTER PROCEDURE {obj('l7_pa')} @a AS int = 1 AS SELECT @a AS [a];\n",
        ),
        (
            "view with CRLF line ends and spaces at line ends",
            "l7_v",
            f"CREATE OR ALTER VIEW {obj('l7_v')}\r\nAS\r\nSELECT 1 AS [x]   \r\n;\r\n",
        ),
        (
            "scalar function",
            "l7_fn",
            f"CREATE OR ALTER FUNCTION {obj('l7_fn')} (@a int)\nRETURNS int\nAS\nBEGIN\n"
            "    RETURN @a + 1;\nEND;\n",
        ),
        (
            "inline table-valued function",
            "l7_if",
            f"CREATE OR ALTER FUNCTION {obj('l7_if')} (@a int)\nRETURNS TABLE\nAS\n"
            "RETURN (SELECT @a AS [a]);\n",
        ),
        (
            "multi-statement table-valued function",
            "l7_tf",
            f"CREATE OR ALTER FUNCTION {obj('l7_tf')} ()\nRETURNS @r TABLE ([a] int NULL)\nAS\nBEGIN\n"
            "    INSERT INTO @r ([a]) VALUES (1);\n    RETURN;\nEND;\n",
        ),
        (
            "trigger",
            "l7_tr",
            f"CREATE OR ALTER TRIGGER {obj('l7_tr')} ON {table}\nAFTER INSERT\nAS\nSET NOCOUNT ON;\n",
        ),
        (
            "lower-case verb with extra spaces",
            "l7_lc",
            f"create   or   alter   procedure {obj('l7_lc')} as select 1 as [x];",
        ),
        (
            "comments between the words of the verb",
            "l7_cm",
            f"CREATE /* c1 */ OR /* c2 */ ALTER -- c3\n VIEW {obj('l7_cm')} AS SELECT 1 AS [x];",
        ),
        (
            "line break and tab between the words of the verb",
            "l7_ws",
            f"CREATE\nOR\tALTER\r\nPROCEDURE {obj('l7_ws')} AS SELECT 1 AS [x];",
        ),
        (
            "PROC, mixed case, empty lines before and after the text",
            "l7_mx",
            f"\n\n  Create Or Alter Proc {obj('l7_mx')} As Select N'\u00e9\u4e2d\t' As [x];  \n\n\t",
        ),
    ]


_L7_VERB = re.compile(r"CREATE\s+OR\s+ALTER", re.IGNORECASE)


def _line_ends(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "CRLF" if crlf and not lf else "LF" if lf and not crlf else "mixed" if crlf else "none"


def _stored_facts(name: str, sent: str, file_text: str, definition: str | None) -> dict[str, Any]:
    """How the stored definition relates to the text that was sent and to the module file."""
    if definition is None:
        return {"stored": False}
    try:
        verb: str | None = lex.module_header(definition).verb
    except lex.LexError:
        verb = None
    try:
        exported = modules.rewrite_for_export(definition, SPIKE_SCHEMA, name)[0]
        export_equal: bool | None = modules.checksum(exported.encode()) == modules.checksum(
            file_text.encode()
        )
    except ToolError:
        export_equal = None
    try:
        expected: str | None = engine_stored_text(sent)
    except lex.LexError:
        expected = None
    return {
        "stored": True,
        "equal": definition == sent,
        "normal_form_equal": modules.checksum(definition.encode()) == modules.checksum(sent.encode()),
        # byte for byte the batch with the one edit of the verb that the engine makes
        "stored_text_equal": definition == expected,
        # what baseline compares (A15): the definition after the header rewrite of export
        "export_rewrite_equals_file": export_equal,
        "verb": verb,
        "starts_with": definition[:40],
        "line_ends": _line_ends(definition),
        "length_sent": len(sent),
        "length_stored": len(definition),
    }


def _l7_runner_problems(session: Session, text: str) -> list[str] | str:
    """The read-back of the runner on the real catalog row, with the compare of the text switched on.

    [] = the runner accepts what the engine stored for this file. A text is the reason code when
    the file is not a module file of the tool.
    """
    header = lex.module_header(text)
    path = f"schema/{names.KIND_DIRS[header.kind]}/{header.schema}.{header.name}.sql"
    try:
        module = modules.read_module(path, text.encode("utf-8"))
    except ToolError as error:
        return error.reason_code
    capture = catalog.capture_modules(session, [module.key]).get(module.key)
    switch = runner.MODULE_TEXT_READBACK
    runner.MODULE_TEXT_READBACK = True
    try:
        return runner._module_problems(module, capture)
    finally:
        runner.MODULE_TEXT_READBACK = switch


def l7_stored_module_text(ctx: Ctx) -> ItemResult:
    session = ctx.open()
    session.execute(f"CREATE TABLE {obj('l7_t')} ([a] int NULL);")
    observed: list[dict[str, Any]] = []
    fixture: list[dict[str, Any]] = []
    for case, name, text in _l7_modules():
        altered = _L7_VERB.sub(lambda found: "alter" if found[0].islower() else "ALTER", text, count=1)
        steps = [("CREATE OR ALTER, object is new", text), ("CREATE OR ALTER, object exists", text)]
        if altered != text:
            steps.append(("ALTER", altered))
        for step, sent in steps:
            outcome = attempt(session, sent)
            found = rows(
                session,
                "SELECT m.[definition] FROM sys.sql_modules AS m "
                f"WHERE m.[object_id] = OBJECT_ID({lit(obj(name))});",
            )
            definition = found[0][0] if found and not outcome["raised"] else None
            facts = _stored_facts(name, sent, text, definition)
            if sent == text and definition is not None:
                # what runner.MODULE_TEXT_READBACK switches on, for every file text that was deployed
                facts["runner_read_back_problems"] = _l7_runner_problems(session, text)
            observed.append({"case": case, "step": step, "error": outcome.get("error"), **facts})
            fixture.append({"case": case, "step": step, "sent": sent, "definition": definition})
    ctx.fixtures.catalog_rows["sql_modules"] = fixture
    ok = all(o.get("stored_text_equal") for o in observed)
    ok = ok and all(o.get("runner_read_back_problems", []) == [] for o in observed)
    return done(
        "L7",
        PASS if ok else FAIL,
        {
            "modules": observed,
            "not_as_stored_text": [
                f"{o['case']}: {o['step']}" for o in observed if not o.get("stored_text_equal")
            ],
            "refused_by_the_read_back": [
                f"{o['case']}: {o['step']}" for o in observed if o.get("runner_read_back_problems")
            ],
            "export_differs_from_file": [
                f"{o['case']}: {o['step']}" for o in observed if o.get("export_rewrite_equals_file") is False
            ],
        },
        "The engine never stores the text as it was sent: it stores the verb CREATE in place of CREATE "
        "OR ALTER and of ALTER (equal and normal_form_equal are false). pass: the stored text is the "
        "batch with that one edit in every case, and runner._module_problems with the compare switched "
        "on accepts every deployed file, so runner.MODULE_TEXT_READBACK may be set True. fail: it "
        "stays False. not_as_stored_text names the cases where the engine did something else; "
        "refused_by_the_read_back names the files that the runner would refuse with property "
        "'definition' (the read-back must compare with modules.stored_text of the file, not with the "
        "file). export_differs_from_file after CREATE OR ALTER: baseline cannot set source_sha256 for "
        "such a file and the first deploy sends the module again (A15); expected only where the verb "
        "of the file is not written `CREATE OR ALTER ` with one space.",
    )


# ------------------------------------------------------------------ L8
L8_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("int", "0"),
    ("int", "(0)"),
    ("int", "-1"),
    ("int", "1+2"),
    ("int", "NULL"),
    ("nvarchar(10)", "N'x'"),
    ("varchar(10)", "'x'"),
    ("datetime2(3)", "getdate()"),
    ("datetime2(3)", "SYSUTCDATETIME()"),
    ("uniqueidentifier", "newid()"),
    ("bit", "CAST(0 AS bit)"),
    ("date", "CONVERT(date, '20200101')"),
    ("decimal(9, 2)", "1.50"),
    ("varbinary(4)", "0x00"),
)
L8_CHECKS = (
    "[c] > 0",
    "c>0 and c<10",
    "[c] IN (1, 2, 3)",
    "[c] BETWEEN 1 AND 5",
    "[s] LIKE 'a%'",
    "LEN([s]) > 0",
    "[c] IS NOT NULL OR [s] <> N''",
    "NOT ([c] = 1)",
)
L8_COMPUTED = (
    "[a] + [b]",
    "isnull(a, 0) * 2",
    "CONVERT(varchar(10), [a])",
    "CAST([a] AS bigint)",
    "UPPER([s])",
    "CASE WHEN [a] > 0 THEN 1 ELSE 0 END",
    "[a] % 2",
)
L8_FILTERS = ("[c] IS NOT NULL", "[c] > 0 AND [c] < 100", "[s] = 'A'", "[c] IN (1, 2)", "[d] >= '20200101'")


def _l8_probes() -> list[tuple[str, str, str, str]]:
    """(class, input, statement that stores the expression, query that reads the engine text)"""
    default, check, computed, filtered = obj("l8_def"), obj("l8_chk"), obj("l8_cmp"), obj("l8_flt")
    probes: list[tuple[str, str, str, str]] = []
    for n, (type_name, expression) in enumerate(L8_DEFAULTS, start=1):
        probes.append(
            (
                "default",
                f"{type_name}: {expression}",
                f"ALTER TABLE {default} ADD [d{n}] {type_name} NULL "
                f"CONSTRAINT [l8_df_{n}] DEFAULT {expression};",
                "SELECT [definition] FROM sys.default_constraints "
                f"WHERE [parent_object_id] = OBJECT_ID({lit(default)}) AND [name] = N'l8_df_{n}';",
            )
        )
    for n, expression in enumerate(L8_CHECKS, start=1):
        probes.append(
            (
                "check",
                expression,
                f"ALTER TABLE {check} ADD CONSTRAINT [l8_ck_{n}] CHECK ({expression});",
                "SELECT [definition] FROM sys.check_constraints "
                f"WHERE [parent_object_id] = OBJECT_ID({lit(check)}) AND [name] = N'l8_ck_{n}';",
            )
        )
    for n, expression in enumerate(L8_COMPUTED, start=1):
        probes.append(
            (
                "computed",
                expression,
                f"ALTER TABLE {computed} ADD [c{n}] AS ({expression});",
                "SELECT [definition] FROM sys.computed_columns "
                f"WHERE [object_id] = OBJECT_ID({lit(computed)}) AND [name] = N'c{n}';",
            )
        )
    for n, expression in enumerate(L8_FILTERS, start=1):
        probes.append(
            (
                "filter",
                expression,
                f"CREATE NONCLUSTERED INDEX [l8_ix_{n}] ON {filtered} ([c]) WHERE {expression};",
                "SELECT [filter_definition] FROM sys.indexes "
                f"WHERE [object_id] = OBJECT_ID({lit(filtered)}) AND [name] = N'l8_ix_{n}';",
            )
        )
    return probes


def normal_form_differences(here: Mapping[str, Any], there: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Inputs whose engine text differs between two captures of L8. Pure."""
    differences: list[dict[str, Any]] = []
    for kind in sorted(set(here) | set(there)):
        ours = {entry["input"]: entry.get("engine") for entry in here.get(kind, [])}
        theirs = {entry["input"]: entry.get("engine") for entry in there.get(kind, [])}
        for expression in sorted(set(ours) | set(theirs)):
            if ours.get(expression) != theirs.get(expression):
                differences.append(
                    {
                        "class": kind,
                        "input": expression,
                        "here": ours.get(expression),
                        "there": theirs.get(expression),
                    }
                )
    return differences


def l8_normal_forms(ctx: Ctx) -> ItemResult:
    compare = ctx.options.compare_normal_forms
    other = json.loads(compare.read_text(encoding="utf-8")) if compare is not None else None
    session = ctx.open()
    session.execute(f"CREATE TABLE {obj('l8_def')} ([id] int NOT NULL);")
    session.execute(f"CREATE TABLE {obj('l8_chk')} ([c] int NULL, [s] nvarchar(20) NULL);")
    session.execute(f"CREATE TABLE {obj('l8_cmp')} ([a] int NULL, [b] int NULL, [s] nvarchar(20) NULL);")
    session.execute(f"CREATE TABLE {obj('l8_flt')} ([c] int NULL, [s] varchar(10) NULL, [d] date NULL);")
    forms: dict[str, list[dict[str, Any]]] = {}
    for kind, expression, statement, read in _l8_probes():
        outcome = attempt(session, statement)
        entry: dict[str, Any] = {"input": expression}
        if outcome["raised"]:
            entry["error"] = outcome["error"]["text"]
        else:
            entry["engine"] = catalog.one_row(session, read)[0]
        forms.setdefault(kind, []).append(entry)
    ctx.fixtures.normal_forms = forms
    decides = (
        "pass (equal text on two databases): onboarding compares catalog with catalog and needs no "
        "accept list. fail: the compare of expression text between environments needs an accept list. "
        "The file normal_forms.json is also the fixture for the table part of catalog.py."
    )
    if other is None:
        return done(
            "L8",
            INCONCLUSIVE,
            {"normal_forms": forms, "next": "run L8 on the second database with --compare-normal-forms"},
            decides,
        )
    differences = normal_form_differences(forms, other)
    return done(
        "L8", FAIL if differences else PASS, {"normal_forms": forms, "differences": differences}, decides
    )


# ------------------------------------------------------------------ L9
def l9_catalog_in_transaction(ctx: Ctx) -> ItemResult:
    session = ctx.open()
    table, trigger = obj("l9_t"), obj("l9_tr")
    session.execute("BEGIN TRANSACTION;")
    session.execute(
        f"DECLARE @i int = 1, @sql nvarchar(max); WHILE @i <= {L9_OBJECTS} BEGIN "
        f"SET @sql = N'CREATE PROCEDURE {names.quote(SPIKE_SCHEMA)}.[l9_p' + CAST(@i AS nvarchar(10)) "
        "+ N'] AS SELECT ' + CAST(@i AS nvarchar(10)) + N' AS [n];'; "
        "EXEC sys.sp_executesql @sql; SET @i += 1; END;"
    )
    session.execute(f"CREATE TABLE {table} ([id] int NOT NULL CONSTRAINT [l9_pk] PRIMARY KEY, [a] int NULL);")
    session.execute(f"CREATE NONCLUSTERED INDEX [l9_ix] ON {table} ([a]);")
    session.execute(f"CREATE OR ALTER TRIGGER {trigger} ON {table} AFTER INSERT AS SET NOCOUNT ON;")
    keys = [key("PROCEDURE", f"l9_p{n}") for n in range(1, L9_OBJECTS + 1)]
    seconds: dict[str, float] = {}

    def timed(name: str, read: Callable[[], Any]) -> Any:
        started = time.monotonic()
        value = read()
        seconds[name] = round(time.monotonic() - started, 3)
        return value

    by_key = timed("capture_modules(keys)", lambda: catalog.capture_modules(session, keys))
    everything = timed("capture_modules(all)", lambda: catalog.capture_modules(session, None))
    listed = timed("list_user_objects", lambda: catalog.list_user_objects(session))
    trigger_capture = everything.get(key("TRIGGER", "l9_tr"), {})
    where = f"WHERE [object_id] = OBJECT_ID({lit(table)})"
    (columns,) = catalog.one_row(session, f"SELECT COUNT(*) FROM sys.columns {where};")
    (indexes,) = catalog.one_row(session, f"SELECT COUNT(*) FROM sys.indexes {where} AND [name] = N'l9_ix';")
    session.execute(f"DROP TABLE {table};")
    trigger_after_drop = catalog.object_exists(session, key("TRIGGER", "l9_tr"))  # A18
    still_open = guard(session)[:2]
    session.execute("ROLLBACK TRANSACTION;")
    (left,) = catalog.one_row(
        ctx.main,
        f"SELECT COUNT(*) FROM sys.objects WHERE [schema_id] = SCHEMA_ID({lit(SPIKE_SCHEMA)}) "
        "AND [name] LIKE N'l9[_]%';",
    )
    numbered = key("PROCEDURE", "l9_p")[:-1]  # PROCEDURE:[azsqlcd_spike].[l9_p
    observed = {
        "modules_read_by_key": len(by_key),
        "modules_read_without_keys": sum(1 for found in everything if found.startswith(numbered)),
        "table_listed": any(o.schema == SPIKE_SCHEMA and o.name == "l9_t" for o in listed),
        "trigger_parent": trigger_capture.get("parent"),
        "trigger_events": trigger_capture.get("events"),
        "columns": columns,
        "index_rows": indexes,
        "trigger_exists_after_drop_of_its_table": trigger_after_drop,
        "guard_after_the_reads": still_open,
        "objects_left_after_rollback": left,
        "seconds": seconds,
    }
    seen = (
        observed["modules_read_by_key"] == L9_OBJECTS
        and observed["modules_read_without_keys"] == L9_OBJECTS
        and observed["table_listed"]
        and (columns, indexes) == (2, 1)
        and trigger_capture.get("parent") == table
        and trigger_after_drop is False
        and still_open == [1, 1]
        and left == 0
    )
    fast = all(value <= L9_MAX_SECONDS for value in seconds.values())
    return done(
        "L9",
        FAIL if not seen else PASS if fast else INCONCLUSIVE,
        observed,
        "pass: read-back, the A12 sweep and the A18 check can run inside the transaction of the unit "
        f"of work. inconclusive: the reads are right and one took more than {L9_MAX_SECONDS:.0f} s; the "
        "owner decides if that time under the schema locks is acceptable. fail: the catalog does not "
        "show the DDL of the open transaction, so the read-back design does not hold.",
    )


# ------------------------------------------------------------------ L10
def l10_parse_only(ctx: Ctx) -> ItemResult:
    parser = ctx.open()
    ctx.main.execute(f"CREATE TABLE {obj('l10_t')} ([a] int NULL);")
    parser.execute("SET PARSEONLY ON;")
    made = f"CREATE TABLE {obj('l10_made')} ([a] int NULL);"
    good_module = f"CREATE OR ALTER PROCEDURE {obj('l10_good')} AS SELECT 1 AS [x];"
    observed: dict[str, Any] = {
        "canary_syntax_error": attempt(parser, "SELECT FROM;"),
        "canary_division": attempt(parser, "SELECT 1/0;"),
        "bad_module": attempt(parser, f"CREATE OR ALTER PROCEDURE {obj('l10_bad')} AS SELECT FROM;"),
        "good_module": attempt(parser, good_module),
        "bad_alter_table": attempt(parser, f"ALTER TABLE {obj('l10_t')} ADD COLUMN [c] int NULL;"),
        "alter_of_a_table_that_does_not_exist": attempt(
            parser, f"ALTER TABLE {obj('l10_no')} ADD [c] int NULL;"
        ),
        "create_table": attempt(parser, made),
        "select_in_a_later_batch": attempt(parser, "SELECT 1 AS [x];"),
        # the reason for FORBIDDEN_TOKEN in plan.py: the setting is read when the batch is parsed
        "batch_that_switches_the_setting_off": attempt(parser, "SET PARSEONLY OFF; SELECT 1 AS [ran];"),
    }
    (created,) = catalog.one_row(
        ctx.main,
        f"SELECT COUNT(*) FROM sys.objects WHERE [schema_id] = SCHEMA_ID({lit(SPIKE_SCHEMA)}) "
        "AND [name] IN (N'l10_made', N'l10_good', N'l10_bad');",
    )
    observed["objects_created"] = created
    # how plan leaves the syntax check: SET PARSEONLY OFF in a batch of its own, then one SELECT
    # that must give a row. The batch above may have switched the setting off, so it is set again.
    parser.execute("SET PARSEONLY ON;")
    observed["parseonly_off_check_while_on"] = attempt(parser, PARSEONLY_IS_OFF)
    observed["set_parseonly_off"] = attempt(parser, PARSEONLY_OFF)
    observed["parseonly_off_check_after_off"] = attempt(parser, PARSEONLY_IS_OFF)
    while_on, after_off = observed["parseonly_off_check_while_on"], observed["parseonly_off_check_after_off"]
    syntax, division = observed["canary_syntax_error"], observed["canary_division"]
    # plan reads an error of any other class as "this says nothing about the text"
    syntax_error_seen = syntax["raised"] and syntax["error"]["class"] == ErrorClass.OTHER.name
    ok = (
        syntax_error_seen
        and not division["raised"]
        and division["result_sets"] == []
        and observed["bad_module"]["raised"]
        and observed["bad_alter_table"]["raised"]
        and not observed["good_module"]["raised"]
        and not observed["create_table"]["raised"]
        and observed["select_in_a_later_batch"].get("result_sets") == []
        and created == 0
        # the check of plan tells the two states apart: no result set while the session parses only
        and while_on.get("result_sets") == []
        and not observed["set_parseonly_off"]["raised"]
        and type(first_value(after_off)) is int
    )
    return done(
        "L10",
        PASS if ok else FAIL,
        observed,
        "pass: the syntax check of plan (step 9) is sound through this driver. fail: plan must stop "
        "with PARSEONLY_CANARY on every target; a release then has no syntax check before the "
        "transaction. batch_that_switches_the_setting_off returned a row: the FORBIDDEN_TOKEN guard "
        "of plan.py is needed and stays. parseonly_off_check_while_on with a row, or "
        "parseonly_off_check_after_off without one: the batch `/* azsqlcd:parseonly_off */ SELECT "
        "@@SPID;` does not tell plan that the session left the syntax check; plan must not read its "
        "catalog queries on that session.",
    )


# ------------------------------------------------------------------ L11
def _l11_deferred_names(ctx: Ctx) -> dict[str, Any]:
    """(a) Which module kinds can be created before the object that they use."""
    main = ctx.main
    base, missing, no_function = obj("l11_base"), obj("l11_missing"), obj("l11_no_fn")
    main.execute(f"CREATE TABLE {base} ([a] int NULL);")
    cases = (
        ("view", f"CREATE OR ALTER VIEW {obj('l11_v')} AS SELECT [x] FROM {missing};"),
        (
            "inline table-valued function",
            f"CREATE OR ALTER FUNCTION {obj('l11_if')} () RETURNS TABLE AS "
            f"RETURN (SELECT [x] FROM {missing});",
        ),
        (
            "multi-statement table-valued function",
            f"CREATE OR ALTER FUNCTION {obj('l11_tf')} () RETURNS @r TABLE ([x] int NULL) AS BEGIN "
            f"INSERT INTO @r ([x]) SELECT [x] FROM {missing}; RETURN; END;",
        ),
        (
            "scalar function",
            f"CREATE OR ALTER FUNCTION {obj('l11_fn')} () RETURNS int AS BEGIN "
            f"RETURN (SELECT COUNT(*) FROM {missing}); END;",
        ),
        ("procedure", f"CREATE OR ALTER PROCEDURE {obj('l11_p')} AS SELECT [x] FROM {missing};"),
        (
            "trigger",
            f"CREATE OR ALTER TRIGGER {obj('l11_tr')} ON {base} AFTER INSERT AS "
            f"INSERT INTO {missing} ([x]) SELECT [a] FROM inserted;",
        ),
        (
            "procedure that calls a procedure that does not exist",
            f"CREATE OR ALTER PROCEDURE {obj('l11_p2')} AS EXEC {obj('l11_no_proc')};",
        ),
        (
            "procedure that uses a scalar function that does not exist",
            f"CREATE OR ALTER PROCEDURE {obj('l11_p3')} AS SELECT {no_function}() AS [x];",
        ),
        (
            "scalar function that uses a scalar function that does not exist",
            f"CREATE OR ALTER FUNCTION {obj('l11_fn2')} () RETURNS int AS BEGIN RETURN {no_function}(); END;",
        ),
    )
    created: dict[str, Any] = {}
    for name, batch in cases:
        outcome = attempt(main, batch)
        ctx.fixtures.error(f"L11 create before its reference: {name}", None, outcome)
        created[name] = {"created": not outcome["raised"], "error": outcome.get("error")}
    return created


def _l11_function_in_check(ctx: Ctx) -> dict[str, Any]:
    """(b) CREATE OR ALTER of a function that a CHECK and a computed column use."""
    main = ctx.main
    function, table = obj("l11_f"), obj("l11_c")
    text = f"CREATE OR ALTER FUNCTION {function} (@a int) RETURNS int AS BEGIN RETURN @a; END;"
    main.execute(text)
    main.execute(
        f"CREATE TABLE {table} ([a] int NULL, [b] AS ({function}([a])), "
        f"CONSTRAINT [l11_ck] CHECK ({function}([a]) >= 0));"
    )
    same = attempt(main, text)
    changed = attempt(main, text.replace("RETURN @a;", "RETURN @a + 0;"))
    ctx.fixtures.error("3729 CREATE OR ALTER of a function that a CHECK uses", 3729, changed)
    return {"same_text": same, "changed_text": changed}


def _l11_chained_schemabinding(ctx: Ctx) -> dict[str, Any]:
    """(c) Unbind two chained schema-bound views with the rewrite of the tool, dependant first."""
    main = ctx.main
    table, lower, upper = obj("l11_t"), obj("l11_v1"), obj("l11_v2")
    keys = [key("VIEW", "l11_v1"), key("VIEW", "l11_v2")]
    bind_lower = f"CREATE OR ALTER VIEW {lower} WITH SCHEMABINDING AS SELECT [a], [b] FROM {table};"
    bind_upper = f"CREATE OR ALTER VIEW {upper} WITH SCHEMABINDING AS SELECT [a] FROM {lower};"
    main.execute(f"CREATE TABLE {table} ([a] int NOT NULL, [b] int NULL);")
    main.execute(bind_lower)
    main.execute(bind_upper)
    main.execute(f"GRANT SELECT ON OBJECT::{lower} TO [public];")
    ids = f"SELECT OBJECT_ID({lit(lower)}), OBJECT_ID({lit(upper)});"
    grants = (
        "SELECT COUNT(*) FROM sys.database_permissions WHERE [class] = 1 "
        f"AND [major_id] = OBJECT_ID({lit(lower)}) AND [permission_name] = N'SELECT';"
    )
    change = f"ALTER TABLE {table} ALTER COLUMN [b] bigint NULL;"

    def bound() -> list[Any]:
        captured = catalog.capture_modules(main, keys)
        return [captured[k]["is_schema_bound"] for k in keys]

    ids_before = list(catalog.one_row(main, ids))
    captured = catalog.capture_modules(main, keys)
    unbind_lower = modules.rewrite_for_unbind(captured[keys[0]]["definition"], SPIKE_SCHEMA, "l11_v1")
    unbind_upper = modules.rewrite_for_unbind(captured[keys[1]]["definition"], SPIKE_SCHEMA, "l11_v2")
    facts: dict[str, Any] = {
        "table_change_while_bound": attempt(main, change),
        "unbind_of_the_referenced_view_first": attempt(main, unbind_lower),
        "unbind_of_the_dependant": attempt(main, unbind_upper),
        "unbind_of_the_referenced_view": attempt(main, unbind_lower),
    }
    facts["schema_bound_after_unbind"] = bound()
    facts["table_change_after_unbind"] = attempt(main, change)
    facts["object_ids_kept"] = list(catalog.one_row(main, ids)) == ids_before
    facts["grant_kept"] = catalog.one_row(main, grants)[0] == 1
    facts["bind_again"] = [attempt(main, bind_lower), attempt(main, bind_upper)]
    facts["schema_bound_after_bind"] = bound()
    facts["object_ids_kept_after_bind"] = list(catalog.one_row(main, ids)) == ids_before
    facts["holds"] = (
        not facts["unbind_of_the_dependant"]["raised"]
        and not facts["unbind_of_the_referenced_view"]["raised"]
        and facts["schema_bound_after_unbind"] == [False, False]
        and not facts["table_change_after_unbind"]["raised"]
        and facts["object_ids_kept"]
        and facts["grant_kept"]
        and facts["schema_bound_after_bind"] == [True, True]
        and facts["object_ids_kept_after_bind"]
    )
    return facts


def l11_ordering_facts(ctx: Ctx) -> ItemResult:
    deferred = _l11_deferred_names(ctx)
    function = _l11_function_in_check(ctx)
    chained = _l11_chained_schemabinding(ctx)
    # A17 breaks a cycle of procedures and triggers in name order: each must be creatable first
    a17 = all(
        deferred[name]["created"]
        for name in ("procedure", "trigger", "procedure that calls a procedure that does not exist")
    )
    return done(
        "L11",
        PASS if a17 and chained["holds"] else FAIL,
        {
            "created_before_its_reference": deferred,
            "procedures_and_triggers_defer_names": a17,
            "function_used_by_check_and_computed_column": function,
            "chained_schemabinding": chained,
        },
        "pass: a cycle of procedures and triggers can be deployed in any order (A17), and unbind in "
        "the order dependant first keeps object id and grants (A15, design (d)). fail on the first: "
        "A17 must become an error. fail on the second: the unbind directive cannot be used. Kinds that "
        "were not created before their reference need the token-scan order or `-- azsqlcd:after`. "
        "function_used_by_check: an error for the same text is why a deploy-module directive skips "
        "text that the database holds.",
    )


# ------------------------------------------------------------------ L12, L13
def l12_serverless_resume(ctx: Ctx) -> ItemResult:
    decides = (
        "pass: the connect budget of 180 s (session.CONNECT_BUDGET_S) covers an auto-resume and the "
        "error text of a paused database is read as transient. fail: add the captured text to "
        "sqlerrors._KNOWN_MESSAGES as 40613, or make the budget longer."
    )
    if not ctx.options.expect_paused:
        steps = [
            "Use the serverless database. Pause it: az sql db pause --resource-group RG --server NAME "
            "--name D (or wait for the auto-pause delay).",
            "Check: az sql db show --resource-group RG --server NAME --name D --query status gives Paused.",
            "Run: uv run --extra db python scripts/live_spike.py --server S --database D "
            "--confirm-disposable-database D --items L12 --expect-paused",
        ]
        return done("L12", MANUAL, {"steps": steps}, decides)
    failed = [a for a in ctx.first_connect if not a["connected"]]
    for number, entry in enumerate(failed, start=1):
        outcome = {"raised": True, "error": entry["error"]}
        ctx.fixtures.error(f"connect to a paused serverless database, attempt {number}", 40613, outcome)
    observed = {
        "attempts": ctx.first_connect,
        "seconds_in_attempts": round(sum(a["seconds"] for a in ctx.first_connect), 1),
    }
    if not failed:
        observed["note"] = "the first attempt connected: the database was not paused"
        return done("L12", INCONCLUSIVE, observed, decides)
    return done("L12", PASS, observed, decides)


class _FixedToken:
    def __init__(self, token: AccessToken) -> None:
        self._token = token

    def get(self) -> AccessToken:
        return self._token


def l13_token_soak(ctx: Ctx) -> ItemResult:
    decides = (
        "The session ends at the expiry: a unit of work must fit in the life of its token, and "
        "min_token_minutes in azsqlcd.toml must cover the longest transaction; long builds belong in "
        "RESUMABLE nontx migrations. The session lives on: the row 'Token expires on an open session' "
        "of the failure matrix never happens, and min_token_minutes only has to cover the connect."
    )
    past = ctx.options.soak_past_expiry_minutes
    if past <= 0 or ctx.provider is None:
        steps = [
            "az login, then at once: uv run --extra db python scripts/live_spike.py --server S "
            "--database D --confirm-disposable-database D --items L13 --soak-past-expiry-minutes 90",
            "The run takes the life of the token (60 to 90 minutes) plus 90 minutes. Do not let "
            "the machine sleep.",
            "For the token life of the pipeline: read token_minutes_left of item L1 in a run that "
            "starts right after azure/login.",
        ]
        return done("L13", MANUAL, {"steps": steps}, decides)
    token = AzureCliTokenProvider().get()  # not cached: the session must hold exactly this token
    held = ctx.track(live_connect(ctx.options.server, ctx.options.database, _FixedToken(token)))
    at_connect = round((token.expires_on - time.time()) / 60, 1)
    end = token.expires_on + past * 60
    probes: list[dict[str, Any]] = []
    long_sent = ended = False
    while time.time() < end and not ended:
        if not long_sent and token.expires_on - time.time() <= SOAK_INTERVAL_S + 60:
            kind, batch = (
                "long statement across the expiry",
                "WAITFOR DELAY '00:10:00'; SELECT SYSUTCDATETIME();",
            )
            long_sent = True
        else:
            time.sleep(max(0.0, min(SOAK_INTERVAL_S, end - time.time())))
            kind, batch = "short statement", "SELECT SYSUTCDATETIME();"
        outcome = attempt(held, batch)
        ended = outcome["raised"]
        minutes = round((time.time() - token.expires_on) / 60, 1)
        probes.append({"kind": kind, "minutes_after_expiry": minutes, **outcome})
        print(f"  L13 {minutes:+.1f} min from the expiry: {kind}: {'error' if ended else 'ok'}", flush=True)
        ctx.fixtures.error("statement on a session whose token expired", None, outcome)
    observed = {"token_minutes_at_connect": at_connect, "session_ended": ended, "probes": probes}
    before_expiry = ended and probes[-1]["minutes_after_expiry"] < 0
    return done("L13", INCONCLUSIVE if before_expiry else PASS, observed, decides)


# ------------------------------------------------------------------ L14
def _l14_steps() -> list[str]:
    user = names.quote("azsqlcd_spike_deploy")
    return [
        "Make a user with the rights that setup-sql gives to the deploy identity (run as db_owner):",
        f"CREATE USER {user} WITHOUT LOGIN;",
        f"ALTER ROLE [db_ddladmin] ADD MEMBER {user};",
        f"GRANT VIEW DEFINITION TO {user};",
        f"GRANT VIEW DATABASE STATE TO {user};",
        f"GRANT SELECT ON OBJECT::[sys].[sql_expression_dependencies] TO {user};",
        f"If schema azsqlcd exists: GRANT SELECT, INSERT, UPDATE ON SCHEMA::[azsqlcd] TO {user};",
        "Run: uv run --extra db python scripts/live_spike.py --server S --database D "
        "--confirm-disposable-database D --items L14 --second-principal azsqlcd_spike_deploy",
        f"Afterwards: DROP USER {user};",
        "After scripts/live_acceptance.py ran, the user azsqlcd-livetest-disposable-deploy exists with "
        "exactly the rights of setup-sql; use that name for the strongest check.",
    ]


def _try(read: Callable[[], Any]) -> dict[str, Any]:
    """Call a read of the tool. Any error is the observation: it names the missing right."""
    try:
        value = read()
    except SqlError as error:
        return {"ok": False, "error": error_facts(error)}
    except (ToolError, RuntimeError, TypeError, ValueError) as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return {"ok": True, "value": value if isinstance(value, (bool, int, str, list, dict)) else str(value)}


def _expect(read: Callable[[], Any], wanted: Callable[[Any], bool]) -> dict[str, Any]:
    """_try, and the value must be the one that a principal with enough rights gets."""
    found = _try(read)
    if found["ok"] and not wanted(found["value"]):
        found = {**found, "ok": False, "error": "the read ran and gave another value than expected"}
    return found


def l14_permissions(ctx: Ctx) -> ItemResult:
    decides = (
        "pass: db_ddladmin with the grants of setup-sql is enough for deploy. fail: each entry of "
        "missing names a statement or a read that the principal may not run; add the smallest grant "
        "to state.setup_sql and run the item again. Run it also with a user that has only the rights "
        "of the plan identity; the statements then fail and every read must still pass."
    )
    principal = ctx.options.second_principal
    if principal is None:
        return done("L14", MANUAL, {"steps": _l14_steps()}, decides)
    main = ctx.main
    base, view, made = obj("l14_base"), obj("l14_v"), obj("l14_t")
    other_schema = names.quote(SPIKE_SCHEMA + "_l14")  # only ever made in a transaction that is rolled back
    main.execute(f"CREATE TABLE {base} ([a] int NULL, [b] int NULL);")
    main.execute(f"CREATE OR ALTER VIEW {view} AS SELECT [a], [b] FROM {base};")
    (state_exists,) = catalog.one_row(main, "SELECT OBJECT_ID(N'[azsqlcd].[meta]', N'U');")
    session = ctx.open()
    switched = attempt(session, f"EXECUTE AS USER = {lit(principal)};")
    if switched["raised"]:
        observed = {"execute_as": switched, "steps": _l14_steps()}
        return done("L14", INCONCLUSIVE, observed, decides)
    who = list(
        catalog.one_row(
            session, "SELECT USER_NAME(), IS_ROLEMEMBER(N'db_ddladmin'), IS_ROLEMEMBER(N'db_owner');"
        )
    )
    create_table = f"CREATE TABLE {made} ([a] int NULL, [b] int NULL);"
    statements: dict[str, list[str]] = {
        "CREATE SCHEMA": [f"CREATE SCHEMA {other_schema};"],
        "CREATE SCHEMA ... AUTHORIZATION [dbo]": [f"CREATE SCHEMA {other_schema} AUTHORIZATION [dbo];"],
        "CREATE TABLE": [create_table],
        "sp_rename of a column": [
            create_table,
            f"EXEC sys.sp_rename @objname = {lit(made + '.[b]')}, @newname = N'c', @objtype = N'COLUMN';",
        ],
        "sp_rename of a table": [
            create_table,
            f"EXEC sys.sp_rename @objname = {lit(made)}, @newname = N'l14_t2', @objtype = N'OBJECT';",
        ],
        "CREATE OR ALTER PROCEDURE": [f"CREATE OR ALTER PROCEDURE {obj('l14_p')} AS SELECT 1 AS [x];"],
        "ALTER of a table that another principal made": [f"ALTER TABLE {base} ADD [c] int NULL;"],
        "sp_refreshsqlmodule": [f"EXEC sys.sp_refreshsqlmodule @name = {lit(view)};"],
        "DROP of a view that another principal made": [f"DROP VIEW {view};"],
    }
    ran: dict[str, Any] = {}
    for name, batches in statements.items():
        session.execute("BEGIN TRANSACTION;")
        outcome: dict[str, Any] = {"raised": False}
        for batch in batches:
            outcome = attempt(session, batch)
            if outcome["raised"]:
                break
        session.execute(ROLLBACK)  # nothing of these statements stays
        ran[name] = {"ok": not outcome["raised"], "error": outcome.get("error")}
    view_id, base_id = f"OBJECT_ID({lit(view)})", f"OBJECT_ID({lit(base)})"
    count_from = {
        "sys.sql_expression_dependencies": "sys.sql_expression_dependencies "
        f"WHERE [referencing_id] = {view_id}",
        "sys.dm_sql_referencing_entities": f"sys.dm_sql_referencing_entities({lit(base)}, N'OBJECT')",
        "sys.dm_sql_referenced_entities": f"sys.dm_sql_referenced_entities({lit(view)}, N'OBJECT')",
        "sys.dm_db_partition_stats": f"sys.dm_db_partition_stats WHERE [object_id] = {base_id}",
        "sys.dm_exec_sessions": "sys.dm_exec_sessions WHERE [session_id] = @@SPID",
        "sys.sql_modules": f"sys.sql_modules WHERE [object_id] = {view_id} AND [definition] IS NOT NULL",
    }
    reads: dict[str, Any] = {}
    for name, source in count_from.items():
        outcome = attempt(session, f"SELECT COUNT(*) FROM {source};")
        count = first_value(outcome)
        reads[name] = {"ok": type(count) is int and count >= 1, "rows": count, "error": outcome.get("error")}
    resumable = attempt(session, "SELECT COUNT(*) FROM sys.index_resumable_operations;")
    reads["sys.index_resumable_operations"] = {"ok": not resumable["raised"], "error": resumable.get("error")}
    view_key, table_key = key("VIEW", "l14_v"), key("TABLE", "l14_base")
    # the reads of the tool itself, as plan and deploy send them
    reads["catalog.fence_facts"] = _try(lambda: asdict(catalog.fence_facts(session)))
    reads["catalog.list_user_objects"] = _expect(
        lambda: len(catalog.list_user_objects(session)), lambda count: count >= 2
    )
    reads["catalog.capture_modules"] = _expect(
        lambda: sorted(catalog.capture_modules(session, [view_key])), lambda keys: keys == [view_key]
    )
    reads["catalog.dependants_of"] = _expect(
        lambda: [d.key for d in catalog.dependants_of(session, [table_key])], lambda keys: view_key in keys
    )
    reads["catalog.broken_references"] = _expect(
        lambda: catalog.broken_references(session, view_key), lambda broken: broken == []
    )
    reads["catalog.table_facts"] = _expect(
        lambda: sorted(catalog.table_facts(session, [table_key])), lambda keys: keys == [table_key]
    )
    reads["catalog.applock_test"] = _try(lambda: catalog.applock_test(session))
    reads["catalog.service_objective"] = _try(lambda: catalog.service_objective(session))
    reads["sp_getapplock"] = _expect(lambda: take_lock(session, 0), lambda result: result in (0, 1))
    state_rights = None
    if state_exists is not None:
        state_rights = list(
            catalog.one_row(
                session,
                "SELECT "
                + ", ".join(
                    f"HAS_PERMS_BY_NAME(N'[azsqlcd]', N'SCHEMA', N'{right}')"
                    for right in ("SELECT", "INSERT", "UPDATE", "DELETE")
                )
                + ";",
            )
        )
    session.execute("REVERT;")
    missing = sorted(name for name, found in {**ran, **reads}.items() if not found["ok"])
    observed = {
        "principal": who,
        "statements": ran,
        "reads": reads,
        "rights_on_schema_azsqlcd_select_insert_update_delete": state_rights,
        "missing": missing,
    }
    return done("L14", FAIL if missing else PASS, observed, decides)


# ------------------------------------------------------------------ L15
def l15_applock(ctx: Ctx) -> ItemResult:
    main = ctx.main
    owner, other = ctx.open(), ctx.open()
    facts: dict[str, Any] = {"granted": take_lock(owner, 0)}
    facts["mode_on_the_owner"] = lock_mode(owner)
    facts["mode_on_another_session"] = lock_mode(other)
    facts["test_on_another_session"] = lock_is_free(other)
    facts["second_request"] = take_lock(other, 0)
    # the runner writes the end of a run after a rollback: the session lock must outlive the transaction
    owner.execute("BEGIN TRANSACTION;")
    facts["error_in_a_transaction"] = attempt(owner, "SELECT 1/0;")["raised"]
    facts["mode_after_the_rollback"] = lock_mode(owner)
    (spid,) = catalog.one_row(owner, SPID)
    kill(main, spid)
    facts["free_after_kill"] = wait_until(lambda: lock_is_free(other) == 1, 10)
    facts["granted_after_kill"] = take_lock(other, 5000)
    other.close()
    third = ctx.open()
    facts["free_after_close"] = wait_until(lambda: lock_is_free(third) == 1, 10)
    facts["release_result"] = [take_lock(third, 0)] + list(
        catalog.one_row(
            third,
            f"DECLARE @r int; EXEC @r = sys.sp_releaseapplock @Resource = {lit(LOCK_RESOURCE)}, "
            "@LockOwner = N'Session'; SELECT @r;",
        )
    )
    step = obj("l15_step")
    main.execute(
        f"CREATE TABLE {step} ([step_id] int IDENTITY(1, 1) NOT NULL CONSTRAINT [l15_pk] PRIMARY KEY, "
        "[migration_id] nvarchar(200) NULL);"
    )
    index = attempt(
        main,
        f"CREATE UNIQUE NONCLUSTERED INDEX [l15_ux] ON {step} ([migration_id]) "
        "WHERE [migration_id] IS NOT NULL;",
    )
    rows_in = attempt(main, f"INSERT INTO {step} ([migration_id]) VALUES (NULL), (NULL), (N'0001__a.sql');")
    duplicate = attempt(main, f"INSERT INTO {step} ([migration_id]) VALUES (N'0001__a.sql');")
    ctx.fixtures.error("2601 second step row for one migration", 2601, duplicate)
    facts["filtered_unique_index"] = {"create": index, "rows": rows_in, "duplicate": duplicate}
    ok = (
        facts["granted"] in (0, 1)
        and facts["mode_on_the_owner"] == "Exclusive"
        and facts["mode_on_another_session"] == "NoLock"
        and facts["test_on_another_session"] == 0
        and facts["second_request"] not in (0, 1)
        and facts["mode_after_the_rollback"] == "Exclusive"
        and facts["free_after_kill"]
        and facts["granted_after_kill"] in (0, 1)
        and facts["free_after_close"]
        and facts["release_result"] == [0, 0]
        and not index["raised"]
        and not rows_in["raised"]
        and duplicate["raised"]
    )
    return done(
        "L15",
        PASS if ok else FAIL,
        facts,
        "pass: the session applock is the only liveness authority (A4): a dead run never holds it, "
        "plan can test it without taking it, and a migration is recorded once. fail: the lock design "
        "of deploy and the reconcile of dead runs do not hold; do not deploy.",
    )


# ------------------------------------------------------------------ L16
def l16_fence_and_refresh(ctx: Ctx) -> ItemResult:
    main = ctx.main
    collation = attempt(
        main,
        "SELECT [collation_name], [catalog_collation_type_desc] FROM sys.databases WHERE [name] = DB_NAME();",
    )
    facts = catalog.fence_facts(main)
    (server_name,) = catalog.one_row(main, "SELECT CAST(SERVERPROPERTY(N'ServerName') AS nvarchar(128));")
    found = (collation.get("result_sets") or [[]])[0]
    database_collation, catalog_collation = found[0] if found else (None, None)
    effective = database_collation if catalog_collation == "DATABASE_DEFAULT" else catalog_collation
    expected_sensitive = (
        None if effective is None else ("_CS" in effective.upper() or "_BIN" in effective.upper())
    )

    table, view = obj("l16_t"), obj("l16_v")
    main.execute(f"CREATE TABLE {table} ([a] int NULL, [b] int NULL);")
    main.execute(f"CREATE OR ALTER VIEW {view} AS SELECT [a], [b] FROM {table};")
    session = ctx.open()
    session.execute("BEGIN TRANSACTION;")
    session.execute(f"ALTER TABLE {table} DROP COLUMN [b];")
    refresh = attempt(session, f"EXEC sys.sp_refreshsqlmodule @name = {lit(view)};")
    ctx.fixtures.error("sp_refreshsqlmodule of a view that uses a dropped column", None, refresh)
    after = guard(session)
    session.execute(ROLLBACK)
    (columns,) = catalog.one_row(
        main, f"SELECT COUNT(*) FROM sys.columns WHERE [object_id] = OBJECT_ID({lit(table)});"
    )
    observed = {
        "sys_databases": collation,
        "fence_facts": asdict(facts),
        "case_sensitive_expected_from_the_collation": expected_sensitive,
        "server_name_is_first_label_of_the_server": str(server_name).casefold()
        == ctx.options.server.split(".")[0].casefold(),
        "refresh_of_a_broken_view": refresh,
        "guard_after_the_failed_refresh": after,
        "columns_after_rollback": columns,
    }
    ok = (
        not collation["raised"]
        and facts.case_sensitive == expected_sensitive
        and refresh["raised"]
        and columns == 2
    )
    return done(
        "L16",
        PASS if ok else FAIL,
        observed,
        "pass: the fence can refuse a case-sensitive catalog (FENCE_CASE_SENSITIVE), and a refresh "
        "that fails rolls the unit of work back with a readable error. fail on the collation: the "
        "probe of catalog.fence_facts must change before a case-sensitive database is met. "
        "server_name_is_first_label_of_the_server false: resolve --rebind-environment prod refuses "
        "every server (REBIND_PROD_SERVER) and its comparison must change.",
    )


# ------------------------------------------------------------------ L17
def l17_nontx(ctx: Ctx) -> ItemResult:
    main = ctx.main
    big = obj("l17_big")
    object_id = f"OBJECT_ID({lit(big)})"
    main.execute(
        f"CREATE TABLE {big} ([id] int IDENTITY(1, 1) NOT NULL CONSTRAINT [l17_pk] PRIMARY KEY, "
        "[k] int NOT NULL, [filler] char(100) NOT NULL);"
    )
    print(f"  L17 fills a table with {ctx.options.rows} rows", flush=True)
    filled = attempt(
        main,
        f"INSERT INTO {big} ([k], [filler]) SELECT TOP ({int(ctx.options.rows)}) "
        "ABS(CHECKSUM(NEWID())) % 100000, 'x' "
        "FROM sys.all_columns AS a CROSS JOIN sys.all_columns AS b CROSS JOIN sys.all_columns AS c;",
    )
    (pk_index,) = catalog.one_row(
        main, f"SELECT [name] FROM sys.indexes WHERE [object_id] = {object_id} AND [is_primary_key] = 1;"
    )
    # the two columns that resolve --mark-not-applied reads
    view_columns = attempt(
        main, "SELECT [object_id], [name] FROM sys.index_resumable_operations WHERE 1 = 0;"
    )
    pages = f"SELECT SUM([used_page_count]) FROM sys.dm_db_partition_stats WHERE [object_id] = {object_id};"
    (pages_before,) = catalog.one_row(main, pages)
    added = attempt(main, f"ALTER TABLE {big} ADD [flag] int NOT NULL CONSTRAINT [l17_df] DEFAULT (0);")
    (pages_after,) = catalog.one_row(main, pages)
    (objective,) = catalog.one_row(
        main, "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), N'ServiceObjective') AS nvarchar(128));"
    )

    operation = (
        "SELECT [state_desc], [percent_complete] FROM sys.index_resumable_operations "
        f"WHERE [object_id] = {object_id} AND [name] = N'l17_ix';"
    )
    builder = ctx.background()
    builder.submit(
        f"CREATE NONCLUSTERED INDEX [l17_ix] ON {big} ([k]) INCLUDE ([filler]) "
        "WITH (ONLINE = ON, RESUMABLE = ON, MAXDOP = 1);"
    )
    running: list[Any] | None = None
    build: dict[str, Any] | None = None
    deadline = time.monotonic() + 300
    while running is None and build is None and time.monotonic() < deadline:
        found = rows(main, operation)
        if found:
            running = list(found[0])
        else:
            build = builder.result(0.2)  # not None: the build ended before the view showed it
    after_kill: list[Any] | None = None
    if running is not None:
        kill(main, builder.spid)
        build = builder.result(120)
        found = rows(main, operation)
        after_kill = list(found[0]) if found else None
    abort = attempt(main, f"ALTER INDEX [l17_ix] ON {big} ABORT;") if after_kill is not None else None
    observed = {
        "rows": ctx.options.rows,
        "service_objective": objective,
        "fill": {"raised": filled["raised"], "seconds": filled["seconds"], "error": filled.get("error")},
        "index_of_a_primary_key_has_the_constraint_name": pk_index == "l17_pk",
        "resumable_view_has_object_id_and_name": not view_columns["raised"],
        "add_not_null_with_default": {
            "raised": added["raised"],
            "seconds": added["seconds"],
            "used_pages_before": pages_before,
            "used_pages_after": pages_after,
            "metadata_only": not added["raised"] and pages_after <= pages_before + 8,
        },
        "build_seen_running": running,
        "build_outcome_in_its_session": build,
        "operation_after_kill": after_kill,
        "abort": abort,
    }
    if running is None:
        observed["note"] = "the build ended before sys.index_resumable_operations showed it: use more --rows"
        result = INCONCLUSIVE
    else:
        ok = after_kill is not None and pk_index == "l17_pk" and not view_columns["raised"]
        result = PASS if ok else FAIL
    return done(
        "L17",
        result,
        observed,
        "pass: after a killed nontx build the catalog shows what remains, so resolve --mark-not-applied "
        "can refuse (NOT_PROVEN_ABSENT) and --mark-applied can be chosen by a human. fail: a killed "
        "build leaves no row, so --mark-not-applied proves nothing and must be removed or changed. "
        "add_not_null_with_default is a fact for the plan summary: metadata_only false means the "
        "statement belongs in the size-of-data list of lint (LCK001).",
    )


# ------------------------------------------------------------------ X1
def _deadlock(ctx: Ctx, table: str) -> list[dict[str, Any]]:
    """Two sessions that each hold a row the other one wants. Returns the attempts that the engine
    ended, without those that only waited too long (then no deadlock was made)."""
    other = ctx.background()
    mine = ctx.open()
    mine.execute("SET DEADLOCK_PRIORITY LOW;")  # the engine then picks this session as the victim
    mine.execute(f"BEGIN TRANSACTION; UPDATE {table} SET [v] = 'mine' WHERE [id] = 1;")
    other.submit(f"BEGIN TRANSACTION; UPDATE {table} SET [v] = 'other' WHERE [id] = 2;")
    other.result(60)
    other.submit(f"UPDATE {table} SET [v] = 'other' WHERE [id] = 1;")  # waits for this session
    time.sleep(2.0)
    victim = attempt(mine, f"UPDATE {table} SET [v] = 'mine' WHERE [id] = 2;")  # closes the cycle
    mine.execute(ROLLBACK)
    survivor = other.result(60)
    other.submit(ROLLBACK)
    other.result(30)
    return [
        outcome
        for outcome in (victim, survivor)
        if outcome is not None
        and outcome["raised"]
        and outcome["error"]["class"] != ErrorClass.LOCK_TIMEOUT.name
    ]


def x1_error_texts(ctx: Ctx) -> ItemResult:
    main = ctx.main
    table = obj("x1_t")
    create = f"CREATE TABLE {table} ([id] int NOT NULL CONSTRAINT [x1_pk] PRIMARY KEY, [v] varchar(10) NULL);"
    main.execute(create)
    main.execute(f"INSERT INTO {table} ([id]) VALUES (1), (2);")
    session = ctx.open()
    found: dict[int, dict[str, Any] | None] = {
        208: attempt(session, f"SELECT [a] FROM {obj('x1_missing')};"),
        2714: attempt(session, create),
        2627: attempt(session, f"INSERT INTO {table} ([id]) VALUES (1);"),
        245: attempt(session, "SELECT CAST('abc' AS int);"),
    }
    # @@ERROR in the next batch: a possible second source of the number, which the driver drops
    next_batch = {"245": first_value(attempt(session, "SELECT @@ERROR;"))}
    holder = ctx.open()
    holder.execute(f"BEGIN TRANSACTION; UPDATE {table} SET [v] = 'held' WHERE [id] = 1;")
    session.execute("SET LOCK_TIMEOUT 500;")
    found[1222] = attempt(
        session, f"SELECT [v] FROM {table} WITH (READCOMMITTEDLOCK, ROWLOCK) WHERE [id] = 1;"
    )
    next_batch["1222"] = first_value(attempt(session, "SELECT @@ERROR;"))
    holder.execute(ROLLBACK)
    deadlocked = _deadlock(ctx, table)
    found[1205] = deadlocked[0] if deadlocked else None
    if ctx.provider is not None:
        # a database that does not exist: one connect attempt, no retry
        keywords = db.connection_keywords(ctx.options.server, SPIKE_SCHEMA + "_no_such_database", APP_NAME)
        try:
            db.open_session(load_driver(), keywords, db.pack_token(ctx.provider.get().token)).close()
            found[4060] = {"raised": False}
        except SqlError as error:
            found[4060] = {"raised": True, "error": error_facts(error)}
    texts: dict[str, Any] = {}
    for number, outcome in found.items():
        raised = outcome is not None and outcome["raised"]
        text = outcome["error"]["text"] if outcome is not None and raised else None
        if outcome is not None and number == 4060:
            # a token login gets the text of a failed login (18456), not the text of 4060
            ctx.fixtures.error("18456 connect to a database that does not exist", 18456, outcome)
        elif outcome is not None:
            ctx.fixtures.error(f"{number}", number, outcome)
        texts[str(number)] = {
            "raised": raised,
            "error": outcome.get("error") if outcome else None,
            "number_read_from_the_text": sqlerrors.known_number(text) if text else None,
        }
    # the numbers on which the tool takes a decision and that this item can raise
    decisive = [texts[str(number)] | {"number": number} for number in (208, 2714, 1222, 1205, 2627, 245)]
    not_raised = [entry["number"] for entry in decisive if not entry["raised"]]
    unread = [
        entry["number"]
        for entry in decisive
        if entry["raised"] and entry["number_read_from_the_text"] != entry["number"]
    ]
    return done(
        "X1",
        FAIL if unread else INCONCLUSIVE if not_raised else PASS,
        {
            "texts": texts,
            "not_recognised": unread,
            "not_raised_by_the_probe": not_raised,
            "at_error_in_the_next_batch": next_batch,
        },
        "pass: sqlerrors.known_number reads the real texts; put error_texts.json in place of "
        "ENGINE_TEXT in tests/unit/test_sqlerrors.py. fail: change sqlerrors._KNOWN_MESSAGES to the "
        "captured text; until then a lock timeout or a deadlock ends as exit 21 and not 24. "
        "inconclusive: a probe did not make its error (not_raised_by_the_probe); run the item again. "
        "at_error_in_the_next_batch equal to the number: SELECT @@ERROR could replace the text match. "
        "4060: the text and class of a connect to a database that does not exist. The class "
        "TRANSIENT_CONNECT means that a wrong database name is tried again for 180 s; the class OTHER "
        "means one attempt (a token login gets the text of a failed login, 18456, not the text of 4060).",
    )


# ------------------------------------------------------------------ X2
def x2_guard_values(ctx: Ctx) -> ItemResult:
    session = ctx.open()
    session.execute("BEGIN TRANSACTION;")
    begun = guard(session)
    session.execute("SELECT 1 AS [a];")
    after_select = guard(session)
    session.execute(f"CREATE TABLE {obj('x2_t')} ([a] int NULL);")
    after_ddl = guard(session)
    error = attempt(session, "SELECT 1/0;")
    after_error = guard(session)
    outside = [guard(session), guard(session)]
    session.execute("BEGIN TRANSACTION;")
    first = guard(session)
    session.execute("COMMIT TRANSACTION; BEGIN TRANSACTION;")
    swapped = guard(session)
    session.execute("BEGIN TRANSACTION;")
    nested = guard(session)
    session.execute(ROLLBACK)
    session.execute("BEGIN TRANSACTION;")
    swallowed = attempt(session, "BEGIN TRY SELECT 1/0; END TRY BEGIN CATCH END CATCH;")
    ctx.fixtures.error("3998 an error that a TRY block swallowed, at the end of the batch", 3998, swallowed)
    doomed = guard(session)
    session.execute(ROLLBACK)
    # XACT_STATE() without CURRENT_TRANSACTION_ID() in the statement
    alone = list(catalog.one_row(session, "SELECT @@TRANCOUNT, XACT_STATE();"))
    observed = {
        "after_begin": begun,
        "after_a_select": after_select,
        "after_ddl": after_ddl,
        "run_time_error_raised": error["raised"],
        "after_a_run_time_error": after_error,
        "outside_a_transaction": outside,
        "before_a_batch_that_commits_and_begins": first,
        "after_a_batch_that_commits_and_begins": swapped,
        "after_a_batch_that_begins_a_second_time": nested,
        "outside_a_transaction_without_the_transaction_id": alone,
        "error_swallowed_by_try_catch_raised": swallowed["raised"],
        "error_swallowed_by_try_catch": swallowed.get("error"),
        "after_an_error_swallowed_by_try_catch": doomed,
        "value_types": [type(value).__name__ for value in begun],
    }
    ok = (
        begun[:2] == [1, 1]
        and type(begun[2]) is int
        and after_select == begun
        and after_ddl == begun
        and error["raised"]
        # outside a transaction the guard reads (0, 1, a new id): CURRENT_TRANSACTION_ID() gives the
        # statement a transaction of its own, and XACT_STATE() then reads 1. Only @@TRANCOUNT and
        # the id tell that the transaction of the unit of work is gone.
        and after_error[0] == 0
        and after_error[2] != begun[2]
        and alone == [0, 0]
        and swapped[:2] == [1, 1]
        and swapped[2] != first[2]
        and nested[0] == 2
        and nested[2] == swapped[2]
        # an uncommittable transaction does not outlive its batch: the engine raises 3998 at the end
        # of the batch and rolls back, so the guard never reads XACT_STATE() = -1 between batches
        and swallowed["raised"]
        and doomed[0] == 0
        and doomed[2] != nested[2]
    )
    return done(
        "X2",
        PASS if ok else FAIL,
        observed,
        "pass: the guard (1, 1, transaction id) of A5 sees a rollback by XACT_ABORT, a batch that "
        "commits and begins again, a second BEGIN and an error that a TRY block swallowed. fail: the "
        "guard does not prove that the session is still in the transaction of the unit of work; the "
        "runner must not be used until the guard is changed. Facts: outside a transaction the guard "
        "reads (0, 1, new id), never (0, 0, id); XACT_STATE() is 0 there only in a statement without "
        "CURRENT_TRANSACTION_ID(). An error that a TRY block swallowed raises 3998 at the end of the "
        "batch and the engine rolls back.",
    )


# ------------------------------------------------------------------ X3
def x3_dependants_after_column_drop(ctx: Ctx) -> ItemResult:
    main = ctx.main
    table = obj("x3_t")
    main.execute(f"CREATE TABLE {table} ([a] int NULL, [b] int NULL);")
    dependants = {
        "view that uses the column": ("VIEW", "x3_v_b", f"AS SELECT [a], [b] FROM {table};"),
        "view that does not use the column": ("VIEW", "x3_v_a", f"AS SELECT [a] FROM {table};"),
        "view with SELECT *": ("VIEW", "x3_v_star", f"AS SELECT * FROM {table};"),
        "procedure that uses the column": ("PROCEDURE", "x3_p_b", f"AS SELECT [b] FROM {table};"),
        "inline function that uses the column": (
            "FUNCTION",
            "x3_if_b",
            f"() RETURNS TABLE AS RETURN (SELECT [a], [b] FROM {table});",
        ),
        # RO-2: sound procedures of a real database have findings of this read. The tool compares
        # the findings after a change with those before it; these cases show both sides.
        "procedure with a #temp table joined to the table": (
            "PROCEDURE",
            "x3_p_tmp",
            "AS CREATE TABLE #t ([a] int NULL); "
            f"SELECT s.[a] FROM {table} AS s JOIN #t AS t ON t.[a] = s.[a];",
        ),
        "procedure with a #temp table that uses the column": (
            "PROCEDURE",
            "x3_p_tmp_b",
            "AS CREATE TABLE #t ([a] int NULL); "
            f"SELECT s.[b] FROM {table} AS s JOIN #t AS t ON t.[a] = s.[a];",
        ),
        "procedure with UPDATE alias FROM #temp AS alias": (
            "PROCEDURE",
            "x3_p_upd",
            "AS CREATE TABLE #t ([a] int NULL, [n] int NULL); "
            f"UPDATE x SET x.[n] = s.[a] FROM #t AS x JOIN {table} AS s ON s.[a] = x.[a];",
        ),
    }
    for kind, name, rest in dependants.values():
        main.execute(f"CREATE OR ALTER {kind} {obj(name)} {rest}")
    main.execute(f"CREATE OR ALTER PROCEDURE {obj('x3_p_gone')} AS SELECT [x] FROM {obj('x3_gone')};")
    listed = [d.key for d in catalog.dependants_of(main, [key("TABLE", "x3_t")])]
    no_object = _try(lambda: catalog.broken_references(main, key("PROCEDURE", "x3_p_gone")))

    session = ctx.open()
    drop = f"ALTER TABLE {table} DROP COLUMN [b];"
    cases: dict[str, Any] = {}
    raw_rows: dict[str, Any] = {}
    for case, (kind, name, _) in dependants.items():
        module_key = key(kind, name)
        raw = (
            "SELECT [referenced_schema_name], [referenced_entity_name], [referenced_minor_name], "
            "[referenced_id], [is_caller_dependent], [is_all_columns_found], [is_incomplete] "
            f"FROM sys.dm_sql_referenced_entities({lit(obj(name))}, N'OBJECT');"
        )
        before = _try(lambda module_key=module_key: catalog.broken_references(main, module_key))
        session.execute("BEGIN TRANSACTION;")
        session.execute(drop)
        seen = attempt(session, raw)
        state_after = guard(session)
        session.execute(ROLLBACK)
        if "#temp" in case:
            # not measured yet: the first error of the read, when there is one
            ctx.fixtures.error(f"referenced entities after a column drop: {case}", None, seen)
        else:
            # the driver returns the first record: 207 (Invalid column name). 2020 follows it, unseen
            ctx.fixtures.error(f"207 referenced entities after a column drop: {case}", 207, seen)
        raw_rows[case] = seen.get("result_sets", seen.get("error"))
        session.execute("BEGIN TRANSACTION;")
        session.execute(drop)
        found = _try(lambda module_key=module_key: catalog.broken_references(session, module_key))
        session.execute(ROLLBACK)
        refreshed = None
        if kind != "PROCEDURE":
            session.execute("BEGIN TRANSACTION;")
            session.execute(drop)
            refresh = attempt(session, f"EXEC sys.sp_refreshsqlmodule @name = {lit(obj(name))};")
            guard_after_refresh = guard(session)
            after_refresh = (
                _try(lambda module_key=module_key: catalog.broken_references(session, module_key))
                if guard_after_refresh[:2] == [1, 1]
                else None
            )
            session.execute(ROLLBACK)
            refreshed = {
                "raised": refresh["raised"],
                "error": refresh.get("error"),
                "guard_after": guard_after_refresh,
                "broken_references_after": after_refresh,
            }
        cases[case] = {
            "listed_as_dependant": module_key in listed,
            "broken_references_before": before,
            "raw_read_after_the_drop": {"raised": seen["raised"], "error": seen.get("error")},
            "guard_after_the_raw_read": state_after,
            "broken_references_after_the_drop": found,
            # what the differential check of the tool acts on (tables.Hooks.dependant_findings)
            "findings_added_by_the_drop": sorted(set(found["value"]) - set(before["value"]))
            if before["ok"] and found["ok"]
            else None,
            "refresh_after_the_drop": refreshed,
        }
    ctx.fixtures.catalog_rows["referenced_entities_after_column_drop"] = raw_rows
    # the same read when the whole table is gone
    view_key = key("VIEW", "x3_v_a")
    session.execute("BEGIN TRANSACTION;")
    session.execute(f"DROP TABLE {table};")
    gone_raw = attempt(
        session,
        "SELECT [referenced_entity_name], [referenced_id], [is_all_columns_found] "
        f"FROM sys.dm_sql_referenced_entities({lit(obj('x3_v_a'))}, N'OBJECT');",
    )
    gone_guard = guard(session)
    gone_found = _try(lambda: catalog.broken_references(session, view_key))
    session.execute(ROLLBACK)
    ctx.fixtures.error("208 referenced entities of a view whose table was dropped", 208, gone_raw)
    missing_table = attempt(
        main,
        "SELECT [referenced_entity_name] "
        f"FROM sys.dm_sql_referenced_entities({lit(obj('x3_p_gone'))}, N'OBJECT');",
    )
    ctx.fixtures.error(
        "2020 referenced entities of a procedure that names a missing table", 2020, missing_table
    )
    table_dropped = {
        "raw_read": {"raised": gone_raw["raised"], "error": gone_raw.get("error")},
        "guard_after_the_raw_read": gone_guard,
        "broken_references": gone_found,
    }
    uses, unused = cases["view that uses the column"], cases["view that does not use the column"]
    broken, sound = uses["broken_references_after_the_drop"], unused["broken_references_after_the_drop"]
    ok = (
        uses["listed_as_dependant"]
        and unused["listed_as_dependant"]
        and broken["ok"]
        and bool(broken["value"])  # the tool sees that the view no longer binds
        and sound == {"ok": True, "value": []}  # and reports nothing for a view that still does
        # a procedure that names a missing table: the engine raises 2020 and gives no row
        and no_object["ok"]
        and bool(no_object["value"])
        and gone_found["ok"]
        and bool(gone_found["value"])
    )
    return done(
        "X3",
        PASS if ok else FAIL,
        {
            "dependants_of_the_table": listed,
            "module_that_names_no_object": no_object,
            "cases": cases,
            "view_whose_table_was_dropped": table_dropped,
        },
        "pass: the A12 gate holds for views: a dependant that a change breaks fails the run "
        "(DEPENDANT_BROKEN) and a sound one does not. fail: A12 does not protect dependants; do not "
        "use drop, rename or retype of a column with table_model before it is fixed. "
        "broken_references_after_the_drop with ok false: the first error text of the read is not "
        "recognised (the engine raises 207 or 208 before 2020 and the driver returns the first); take "
        "it from error_texts.json into sqlerrors._KNOWN_MESSAGES and catalog.NOT_BOUND_NUMBERS. "
        "guard_after_the_raw_read (1, 1): the failed read does not end the transaction. The other "
        "cases are facts: which kinds the read reports, and what sp_refreshsqlmodule repairs. The "
        "three #temp cases decide the A12 rule for a procedure that uses a temporary table: "
        "broken_references_before not empty means that a sound procedure has findings, so only "
        "findings_added_by_the_drop may fail a run. findings_added_by_the_drop empty for the #temp "
        "procedure that uses the dropped column: the read cannot see that break, and A12 does not "
        "protect such a procedure.",
    )


# ------------------------------------------------------------------ X4
def x4_create_user_with_sid(ctx: Ctx) -> ItemResult:
    main = ctx.main
    user, name = names.quote(SPIKE_USER), lit(SPIKE_USER)
    # the form of state.setup_sql: the 16 bytes of a client id, as uniqueidentifier stores them
    sid = "0x" + uuid.uuid4().bytes_le.hex().upper()
    drop = f"IF DATABASE_PRINCIPAL_ID({name}) IS NOT NULL DROP USER {user};"
    try:
        created = attempt(main, f"CREATE USER {user} WITH SID = {sid}, TYPE = E;")
        stored = rows(
            main,
            "SELECT CONVERT(char(34), [sid], 1), [type], [authentication_type_desc] "
            f"FROM sys.database_principals WHERE [name] = {name};",
        )
        grants = {
            statement: attempt(main, statement.replace("{user}", user))
            for statement in (
                "ALTER ROLE [db_ddladmin] ADD MEMBER {user};",
                "GRANT VIEW DEFINITION TO {user};",
                "GRANT VIEW DATABASE STATE TO {user};",
                "GRANT SELECT ON OBJECT::[sys].[sql_expression_dependencies] TO {user};",
            )
        }
    finally:
        main.execute(drop)
    refused_grants = sorted(statement for statement, outcome in grants.items() if outcome["raised"])
    observed = {
        "create": created,
        "principal": list(stored[0]) if stored else None,
        "sid_sent": sid,
        "sid_equal": bool(stored) and str(stored[0][0]).upper() == sid.upper(),
        "grants": grants,
        "grants_refused": refused_grants,
    }
    ok = not created["raised"] and observed["sid_equal"] and not refused_grants
    return done(
        "X4",
        PASS if ok else FAIL,
        observed,
        "pass: the script of azsqlcd setup-sql can make the users of the pipeline identities with no "
        "directory lookup, and its name and SID guard compares the right bytes. fail: setup-sql must "
        "use CREATE USER ... FROM EXTERNAL PROVIDER (the administrator then needs directory read "
        "rights), or the grant that was refused must change.",
    )


# ------------------------------------------------------------------ X5
def x5_killed_in_commit_batch(ctx: Ctx) -> ItemResult:
    fence = obj("x5_fence")
    main = ctx.main
    main.execute(
        f"CREATE TABLE {fence} ([run_id] int NOT NULL CONSTRAINT [x5_pk] PRIMARY KEY, "
        "[segments_committed] int NOT NULL);"
    )
    main.execute(f"INSERT INTO {fence} ([run_id], [segments_committed]) VALUES (1, 0);")
    victim = ctx.open()
    (spid,) = catalog.one_row(victim, SPID)
    victim.execute(f"BEGIN TRANSACTION; UPDATE {fence} SET [segments_committed] = 1 WHERE [run_id] = 1;")
    killer = ctx.background()
    killer.submit(f"KILL {spid};", delay_s=3.0)
    # the session dies while this process waits for the answer to the batch that holds COMMIT
    outcome = attempt(victim, "WAITFOR DELAY '00:00:15'; COMMIT TRANSACTION;")
    killed = killer.result(60)
    later = attempt(victim, "SELECT 1;")
    reader = ctx.open()
    value = first_value(
        attempt(
            reader,
            f"SELECT [segments_committed] FROM {fence} WITH (READCOMMITTEDLOCK, ROWLOCK) WHERE [run_id] = 1;",
        )
    )
    ctx.fixtures.error("session killed while the client waits for a batch", None, outcome)
    observed = {
        "commit_batch": outcome,
        "kill": killed,
        "execute_after_it": later,
        "closed_flag": victim.closed,
        "fence_read_by_another_session": value,
    }
    decides = (
        "Gate G-D4. pass: a connection that is lost while the client waits for COMMIT raises, so the "
        "runner ends as 'outcome unknown' and never as 'committed'. fail: the adapter reported success "
        "for a batch whose session was killed; the pyodbc adapter is needed. commit_batch.error.class "
        "other than SESSION_LOST: session.py does not know the SQLSTATE label of this loss; the runner "
        "still ends as exit 23, through the rollback that fails. A real loss of the network after the "
        "COMMIT reached the server is not automated (docs/live-testing.md, section 7)."
    )
    kill_sent = killed is not None and not killed["raised"]
    if not kill_sent:
        return done("X5", INCONCLUSIVE, observed, decides)
    ok = outcome["raised"] and outcome["seconds"] < 14 and later["raised"] and value == 0
    return done("X5", PASS if ok else FAIL, observed, decides)


# ------------------------------------------------------------------ X6
def x6_sequence_restart(ctx: Ctx) -> ItemResult:
    session = ctx.open()
    sequence = obj("x6_seq")
    read = (
        "SELECT CAST([start_value] AS bigint), CAST([current_value] AS bigint), [is_exhausted] "
        f"FROM sys.sequences WHERE [object_id] = OBJECT_ID({lit(sequence)});"
    )

    def values() -> list[Any]:
        found = rows(session, read)
        return list(found[0]) if found else [None, None, None]

    created = attempt(session, f"CREATE SEQUENCE {sequence} AS bigint START WITH 10 INCREMENT BY 1;")
    after_create = values()
    taken = first_value(attempt(session, f"SELECT NEXT VALUE FOR {sequence};"))
    after_next = values()
    restart = attempt(session, f"ALTER SEQUENCE {sequence} RESTART WITH 500;")
    after_restart = values()
    plain_restart = attempt(session, f"ALTER SEQUENCE {sequence} RESTART;")
    after_plain = values()
    clear = (
        not created["raised"]
        and not restart["raised"]
        and all(type(found[0]) is int for found in (after_create, after_restart))
    )
    observed = {
        "create": created,
        "after_create": after_create,
        "next_value": taken,
        "after_next_value": after_next,
        "restart_with_500": restart,
        "after_restart_with": after_restart,
        "restart_without_a_value": plain_restart,
        "after_restart_without_a_value": after_plain,
        "start_value_moves_with_restart_with": after_restart[0] != after_create[0] if clear else None,
        "columns": ["start_value", "current_value", "is_exhausted"],
    }
    return done(
        "X6",
        PASS if clear else INCONCLUSIVE,
        observed,
        "A fact for the capture of a sequence (RO-3); pass means that the answer is clear. "
        "start_value_moves_with_restart_with true: sys.sequences.start_value changes whenever an "
        "application reseeds, so catalog_tables.NOT_IN_DRIFT must keep start_value of a SEQUENCE, and "
        "the read-back and the baseline report of a reseeded sequence differ from START WITH of the "
        "file; write that in the design. false: start_value is the value of the file for the life of "
        "the sequence; take it out of NOT_IN_DRIFT so that drift compares it again.",
    )


# ------------------------------------------------------------------ X7
def x7_catalog_views(ctx: Ctx) -> ItemResult:
    main = ctx.main
    table, indexed, plain_view = obj("x7_t"), obj("x7_v"), obj("x7_plain")
    main.execute(f"CREATE TABLE {table} ([a] int NOT NULL, [b] int NULL);")
    main.execute(f"CREATE OR ALTER VIEW {indexed} WITH SCHEMABINDING AS SELECT [a], [b] FROM {table};")
    main.execute(f"CREATE OR ALTER VIEW {plain_view} AS SELECT [a] FROM {table};")
    index = attempt(main, f"CREATE UNIQUE CLUSTERED INDEX [x7_cx] ON {indexed} ([a]);")
    numbered = attempt(main, "SELECT COUNT(*) FROM sys.numbered_procedures;")
    ctx.fixtures.error("X7 read of sys.numbered_procedures", None, numbered)

    def index_rows(view: str) -> Any:
        found = attempt(
            main,
            "SELECT [name], [index_id], [type_desc], [is_unique] FROM sys.indexes "
            f"WHERE [object_id] = OBJECT_ID({lit(view)}) ORDER BY [index_id];",
        )
        return found["result_sets"][0] if not found["raised"] and found["result_sets"] else found.get("error")

    of_indexed, of_plain = index_rows(indexed), index_rows(plain_view)
    indexed_key, plain_key = key("VIEW", "x7_v"), key("VIEW", "x7_plain")
    # the reads of the tool itself, as export and the runner send them
    tool = {
        "catalog.has_index of the indexed view": _expect(
            lambda: catalog.has_index(main, indexed_key), lambda found: found is True
        ),
        "catalog.has_index of a plain view": _expect(
            lambda: catalog.has_index(main, plain_key), lambda found: found is False
        ),
        "catalog.indexed_views": _expect(
            lambda: [k for k in catalog.indexed_views(main) if k in (indexed_key, plain_key)],
            lambda keys: keys == [indexed_key],
        ),
        "catalog.export_facts": _expect(
            lambda: sorted(
                f"{fact.fact} {fact.name}"
                for fact in catalog.export_facts(main)
                if fact.schema == SPIKE_SCHEMA
            ),
            lambda facts: facts == [f"{catalog.INDEXED_VIEW} x7_v"],
        ),
    }
    # error 208 is the one answer that catalog.export_facts reads as "no numbered procedure"
    numbered_ok = not numbered["raised"] or numbered["error"]["number"] == catalog.NO_SUCH_OBJECT
    observed = {
        "sys_numbered_procedures": numbered,
        "create_index_on_the_view": index,
        "sys_indexes_of_the_indexed_view": of_indexed,
        "sys_indexes_of_a_plain_view": of_plain,
        "reads_of_the_tool": tool,
    }
    ok = (
        numbered_ok
        and not index["raised"]
        and isinstance(of_indexed, list)
        and [row[1] for row in of_indexed] == [1]
        and of_plain == []
        and all(found["ok"] for found in tool.values())
    )
    return done(
        "X7",
        PASS if ok else FAIL,
        observed,
        "pass: export can read its facts on this database: sys.numbered_procedures answers (or does "
        "not exist, error 208, which the tool reads as no numbered procedure), an indexed view has a "
        "row in sys.indexes with index_id 1 and a plain view has no row, so the INDEXED_VIEW fact and "
        "the has_index question of the runner (ALTER VIEW drops every index of the view) are sound. "
        "fail: reads_of_the_tool names the read; export and an unbind of a view must not be used "
        "before it is fixed. sys_numbered_procedures raised with another error than 208: export stops "
        "on every database; add the number to the cases that catalog._numbered_procedures accepts.",
    )


# ------------------------------------------------------------------ X8
def x8_implicit_transactions(ctx: Ctx) -> ItemResult:
    session = ctx.open(options=False)
    names_of_the_row = runner._OPTION_NAMES
    table_read = "SELECT TOP (1) [name] FROM sys.objects;"

    def option_row() -> dict[str, Any]:
        return dict(zip(names_of_the_row, catalog.one_row(session, runner._SESSION_OPTIONS), strict=True))

    def trancount() -> Any:
        return catalog.one_row(session, "SELECT @@TRANCOUNT;")[0]

    before = option_row()
    session.execute("SET IMPLICIT_TRANSACTIONS ON;")
    while_on = option_row()
    session.execute(table_read)
    count_on = trancount()
    session.execute(ROLLBACK)
    session.execute("SET IMPLICIT_TRANSACTIONS OFF;")
    after_off = option_row()
    session.execute(table_read)
    count_off = trancount()
    session.execute(ROLLBACK)
    try:
        # the real function of the runner: it sets the options and then asserts the row
        runner.set_session_options(session, 30000)
        asserted: dict[str, Any] = {"asserted": True}
    except ToolError as error:
        asserted = {"asserted": False, "reason_code": error.reason_code, **error.detail}
    observed = {
        "as_the_driver_gives_the_session": before,
        "with_implicit_transactions_on": while_on,
        "trancount_after_a_table_read_while_on": count_on,
        "after_set_implicit_transactions_off": after_off,
        "trancount_after_a_table_read_after_off": count_off,
        "runner_session_options": asserted,
        "value_types": [type(value).__name__ for value in after_off.values()],
    }
    ok = (
        while_on["IMPLICIT_TRANSACTIONS"] == 2
        and after_off["IMPLICIT_TRANSACTIONS"] == 0
        and count_off == 0
        and asserted["asserted"]
    )
    return done(
        "X8",
        PASS if ok else FAIL,
        observed,
        "pass: @@OPTIONS & 2 in the option row of the runner follows SET IMPLICIT_TRANSACTIONS, so "
        "runner.set_session_options proves that a state write outside a unit of work is not left in "
        "a transaction that nobody commits. fail: the row does not show the option; the assert of the "
        "runner proves nothing about it and the bit or the query must change. "
        "trancount_after_a_table_read_while_on 1 is the reason for the check: with the option ON one "
        "read opens a transaction. as_the_driver_gives_the_session shows what a session has before "
        "any SET of the tool.",
    )


# ------------------------------------------------------------------ X9
def x9_temporal_table(ctx: Ctx) -> ItemResult:
    session = ctx.open()
    table, history = obj("x9_t"), obj("x9_t_history")
    create = (
        f"CREATE TABLE {table} ([id] int NOT NULL CONSTRAINT [x9_pk] PRIMARY KEY, [v] int NULL, "
        "[valid_from] datetime2(7) GENERATED ALWAYS AS ROW START HIDDEN NOT NULL, "
        "[valid_to] datetime2(7) GENERATED ALWAYS AS ROW END HIDDEN NOT NULL, "
        "PERIOD FOR SYSTEM_TIME ([valid_from], [valid_to])) "
        f"WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = {history}));"
    )
    tables = (
        "SELECT t.[name], t.[temporal_type], t.[temporal_type_desc], h.[name] FROM sys.tables AS t "
        "LEFT JOIN sys.tables AS h ON h.[object_id] = t.[history_table_id] "
        f"WHERE t.[schema_id] = SCHEMA_ID({lit(SPIKE_SCHEMA)}) AND t.[name] LIKE N'x9[_]%' ORDER BY t.[name];"
    )
    period = (
        "SELECT p.[name], p.[period_type], sc.[name], ec.[name] FROM sys.periods AS p "
        "JOIN sys.columns AS sc ON sc.[object_id] = p.[object_id] AND sc.[column_id] = p.[start_column_id] "
        "JOIN sys.columns AS ec ON ec.[object_id] = p.[object_id] AND ec.[column_id] = p.[end_column_id] "
        f"WHERE p.[object_id] = OBJECT_ID({lit(table)});"
    )
    columns = (
        "SELECT [name], [generated_always_type], [generated_always_type_desc], [is_hidden] FROM sys.columns "
        f"WHERE [object_id] = OBJECT_ID({lit(table)}) AND [generated_always_type] <> 0 ORDER BY [column_id];"
    )

    def read(batch: str) -> Any:
        found = attempt(session, batch)
        return found["result_sets"][0] if not found["raised"] and found["result_sets"] else found.get("error")

    # Every statement runs in a transaction that is rolled back: the cleanup of this script sends a
    # plain DROP TABLE, and the engine refuses that for a table with SYSTEM_VERSIONING = ON.
    session.execute("BEGIN TRANSACTION;")
    created = attempt(session, create)
    while_versioned, period_rows, column_rows = read(tables), read(period), read(columns)
    off = attempt(session, f"ALTER TABLE {table} SET (SYSTEM_VERSIONING = OFF);")
    after_off = read(tables)
    period_after_off = read(period)
    drops = [attempt(session, f"DROP TABLE {name};") for name in (table, history)]
    after_drop = read(tables)
    guard_at_the_end = guard(session)
    session.execute(ROLLBACK)
    # a second transaction: what DROP TABLE gives while the versioning is on
    session.execute("BEGIN TRANSACTION;")
    made_again = attempt(session, create)
    drop_while_on = attempt(session, f"DROP TABLE {table};") if not made_again["raised"] else None
    guard_after_refused_drop = guard(session)
    session.execute(ROLLBACK)
    if drop_while_on is not None:
        ctx.fixtures.error("X9 DROP TABLE of a table with SYSTEM_VERSIONING = ON", None, drop_while_on)
    (left,) = catalog.one_row(
        ctx.main,
        f"SELECT COUNT(*) FROM sys.objects WHERE [schema_id] = SCHEMA_ID({lit(SPIKE_SCHEMA)}) "
        "AND [name] LIKE N'x9[_]%';",
    )
    ctx.fixtures.catalog_rows["temporal_table"] = {
        "tables_while_versioned": while_versioned,
        "period": period_rows,
        "generated_columns": column_rows,
        "tables_after_versioning_off": after_off,
    }
    observed = {
        "create": created,
        "tables_while_versioned": while_versioned,
        "period": period_rows,
        "generated_columns": column_rows,
        "versioning_off": off,
        "tables_after_versioning_off": after_off,
        "period_after_versioning_off": period_after_off,
        "drops_after_versioning_off": drops,
        "tables_after_the_drops": after_drop,
        "guard_at_the_end": guard_at_the_end,
        "drop_while_versioned": drop_while_on,
        "guard_after_the_refused_drop": guard_after_refused_drop,
        "objects_left": left,
        "columns": ["name", "temporal_type", "temporal_type_desc", "history table"],
    }

    def types(found: Any) -> list[Any]:
        return [row[1] for row in found] if isinstance(found, list) else []

    ok = (
        not created["raised"]
        and types(while_versioned) == [2, 1]  # the table, then its history table
        and isinstance(while_versioned, list)
        and while_versioned[0][3] == "x9_t_history"
        and isinstance(period_rows, list)
        and [list(row[1:]) for row in period_rows] == [[1, "valid_from", "valid_to"]]
        and isinstance(column_rows, list)
        and [row[1] for row in column_rows] == [1, 2]
        and not off["raised"]
        and types(after_off) == [0, 0]
        and not any(drop["raised"] for drop in drops)
        and after_drop == []
        and left == 0
    )
    return done(
        "X9",
        PASS if ok else FAIL,
        observed,
        "pass: the catalog shows a system-versioned table as catalog_tables.py reads it: "
        "sys.tables.temporal_type 2 with history_table_id, 1 for the history table, one row of "
        "sys.periods (period_type 1) with the two generated columns; SET (SYSTEM_VERSIONING = OFF) "
        "makes both plain tables (0) that DROP TABLE accepts, all inside one transaction. fail: the "
        "temporal reader or the order 'versioning off, then drop' of the table model does not hold "
        "on this engine; keep temporal tables quarantined. drop_while_versioned is the error text of "
        "a DROP TABLE that comes too early; guard_after_the_refused_drop tells if it ends the "
        "transaction. period_after_versioning_off not empty: the period stays after the versioning "
        "is off, and the model must drop it in a step of its own.",
    )


# ------------------------------------------------------------------ X10
# (column type, masking function): the closed list of the table model
X10_MASKS: tuple[tuple[str, str], ...] = (
    ("nvarchar(100)", "default()"),
    ("nvarchar(100)", "email()"),
    ("int", "random(1, 5)"),
    ("nvarchar(100)", 'partial(1, "xx", 0)'),
    ("datetime2(3)", 'datetime("Y")'),
)
_X10_NEWEST = 'datetime("Y")'  # not on every engine version: a refusal of it is a fact, not a failure


def x10_masked_columns(ctx: Ctx) -> ItemResult:
    main = ctx.main
    table = obj("x10_t")
    main.execute(f"CREATE TABLE {table} ([id] int NOT NULL);")
    read = (
        "SELECT c.[name], c.[is_masked], m.[masking_function] FROM sys.columns AS c "
        "LEFT JOIN sys.masked_columns AS m "
        "ON m.[object_id] = c.[object_id] AND m.[column_id] = c.[column_id] "
        f"WHERE c.[object_id] = OBJECT_ID({lit(table)}) ORDER BY c.[column_id];"
    )

    def masked() -> dict[str, Any]:
        found = rows(main, read)
        return {str(name): function for name, is_masked, function in found if is_masked}

    added: dict[str, Any] = {}
    for number, (type_name, function) in enumerate(X10_MASKS, start=1):
        outcome = attempt(
            main,
            f"ALTER TABLE {table} ADD [m{number}] {type_name} MASKED WITH (FUNCTION = '{function}') NULL;",
        )
        ctx.fixtures.error(f"X10 masking function {function}", None, outcome)
        added[function] = outcome
    first_read = attempt(main, read)
    stored = masked() if not first_read["raised"] else {}
    forms = [
        {"input": function, "engine": stored.get(f"m{number}")}
        for number, (_, function) in enumerate(X10_MASKS, start=1)
    ]
    ctx.fixtures.catalog_rows["masked_columns"] = forms
    unmask = attempt(main, f"ALTER TABLE {table} ALTER COLUMN [m1] DROP MASKED;")
    left = masked() if not first_read["raised"] else {}
    refused_functions = [function for function, outcome in added.items() if outcome["raised"]]
    observed = {
        "added": added,
        "read_of_sys_masked_columns": {"raised": first_read["raised"], "error": first_read.get("error")},
        "masking_functions": forms,
        "refused": refused_functions,
        "drop_masked": unmask,
        "masked_after_drop_masked": len(left),
    }
    accepted = [form for form in forms if form["input"] not in refused_functions]
    ok = (
        not first_read["raised"]
        and set(refused_functions) <= {_X10_NEWEST}
        and all(isinstance(form["engine"], str) and form["engine"] for form in accepted)
        and not unmask["raised"]
        and "m1" not in left
        and len(left) == len(accepted) - 1
    )
    return done(
        "X10",
        PASS if ok else FAIL,
        observed,
        "pass: sys.masked_columns gives one row with the text of the function for each masked column, "
        "and ALTER COLUMN ... DROP MASKED removes it; masking_functions (also in catalog_rows.json) is "
        "the form in which the engine writes each function: the reader of the table model must compare "
        "with that form, not with the text of the file. fail: a function of the closed list is "
        "refused, or the catalog does not show the mask; masked columns stay outside the table model. "
        f"refused may hold {_X10_NEWEST}: older engines do not have it, and the model must then refuse "
        "it for such a target.",
    )


ITEMS: dict[str, Callable[[Ctx], ItemResult]] = {
    "L1": l1_token_connect,
    "L2": l2_later_statement_error,
    "L3": l3_compile_errors,
    "L4": l4_batch_text,
    "L5": l5_killed_idle_session,
    "L5b": l5b_close_in_transaction,
    "L6": l6_reconcile_by_locking_read,
    "L7": l7_stored_module_text,
    "L8": l8_normal_forms,
    "L9": l9_catalog_in_transaction,
    "L10": l10_parse_only,
    "L11": l11_ordering_facts,
    "L12": l12_serverless_resume,
    "L13": l13_token_soak,
    "L14": l14_permissions,
    "L15": l15_applock,
    "L16": l16_fence_and_refresh,
    "L17": l17_nontx,
    "X1": x1_error_texts,
    "X2": x2_guard_values,
    "X3": x3_dependants_after_column_drop,
    "X4": x4_create_user_with_sid,
    "X5": x5_killed_in_commit_batch,
    "X6": x6_sequence_restart,
    "X7": x7_catalog_views,
    "X8": x8_implicit_transactions,
    "X9": x9_temporal_table,
    "X10": x10_masked_columns,
}


# ------------------------------------------------------------------ run
def run_item(ctx: Ctx, item_id: str) -> ItemResult:
    """One item. An error that the item did not plan for makes it inconclusive, not the whole run."""
    try:
        return ITEMS[item_id](ctx)
    except Exception as error:  # the other items and the cleanup must still run
        found = error_facts(error) if isinstance(error, SqlError) else f"{type(error).__name__}: {error}"
        return done(
            item_id,
            INCONCLUSIVE,
            {"stopped_by": found},
            "Nothing yet: the probe stopped on an error that it did not plan for. Read the text. A "
            "KILL that is refused needs the right KILL DATABASE CONNECTION for the login of the owner.",
        )
    finally:
        ctx.close_opened()


def summary(results: Mapping[str, str]) -> dict[str, Any]:
    """The driver decision and the constants that a set of item results unlocks. Pure."""
    gates = {gate: results.get(item, "not run") for gate, item in GATES.items()}
    if FAIL in gates.values():
        decision = "a gate failed: write the pyodbc adapter behind session.py, then run the spike again"
    elif all(result == PASS for result in gates.values()):
        decision = "all five gates pass on this machine: keep mssql-python"
    else:
        decision = "open: every gate needs a pass"
    return {
        "items": dict(results),
        "driver_gates": gates,
        "driver_decision": decision,
        "may_be_set_true": {name: results.get(item) == PASS for name, item in CONSTANTS.items()},
    }


def parse_args(argv: Sequence[str] | None) -> Options:
    parser = argparse.ArgumentParser(
        description="Live spike of azsqlcd on a disposable Azure SQL Database. Read docs/live-testing.md."
    )
    parser.add_argument("--list", action="store_true", help="print the items and stop; no connection")
    parser.add_argument("--server", help="for example NAME.database.windows.net")
    parser.add_argument("--database")
    parser.add_argument("--confirm-disposable-database", dest="confirm", help="the database name again")
    parser.add_argument("--items", default="", help="ids with commas, for example L1,L5; default: all")
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    parser.add_argument("--second-principal", help="L14: a database user to run statements as")
    parser.add_argument("--expect-paused", action="store_true", help="L12: the database is paused now")
    parser.add_argument("--soak-past-expiry-minutes", type=int, default=0, help="L13: 90 in the design")
    parser.add_argument("--repetitions", type=int, default=20, help="L6: repetitions")
    parser.add_argument("--rows", type=int, default=300_000, help="L17: rows of the large table")
    parser.add_argument("--compare-normal-forms", type=Path, help="L8: normal_forms.json of another database")
    args = parser.parse_args(argv)
    items = tuple(part.strip() for part in args.items.split(",") if part.strip()) or ITEM_IDS
    unknown = [item for item in items if item not in ITEM_IDS]
    if unknown:
        parser.error(f"unknown item id: {', '.join(unknown)}")
    if not args.list and not (args.server and args.database and args.confirm):
        parser.error("--server, --database and --confirm-disposable-database are required")
    return Options(
        server=args.server or "",
        database=args.database or "",
        confirm=args.confirm or "",
        out=args.out,
        items=items,
        second_principal=args.second_principal,
        expect_paused=args.expect_paused,
        soak_past_expiry_minutes=args.soak_past_expiry_minutes,
        repetitions=args.repetitions,
        rows=args.rows,
        compare_normal_forms=args.compare_normal_forms,
        list_items=args.list,
    )


def _write_fixtures(ctx: Ctx, hide: Mapping[str, str]) -> None:
    out, fixtures = ctx.options.out, ctx.fixtures
    if fixtures.errors:
        merge_json(out / "error_texts.json", plain(fixtures.errors, hide))
    if fixtures.catalog_rows:
        merge_json(out / "catalog_rows.json", plain(fixtures.catalog_rows, hide))
    if fixtures.normal_forms is not None:
        write_json(out / "normal_forms.json", plain(fixtures.normal_forms, hide))


def _usable_main(ctx: Ctx) -> None:
    """The session of the owner, outside a transaction. A session that an item lost is opened again."""
    try:
        ctx.main.execute(ROLLBACK)
    except SqlError:
        ctx.main.close()
        ctx.main = ctx.connect()
        ctx.main.execute(SESSION_OPTIONS)


def run(options: Options, connect: Connect | None = None) -> int:
    """Guards, cleanup of leftovers, the items, cleanup, files. Returns the exit code of the script."""
    refuse_unconfirmed(options.database, options.confirm)  # before any connection
    hide = private_names(options.server, options.database)
    spike_out = options.out / "spike"
    provider: TokenProvider | None = None
    log: list[dict[str, Any]] = []
    if connect is None:
        cached = provider = CachedTokenProvider(AzureCliTokenProvider())
        driver_connect = recording_connect(log)

        def connect_live() -> Session:
            return db.connect(
                options.server, options.database, cached, APP_NAME, driver_connect=driver_connect
            )

        connect = connect_live
    try:
        main_session = connect()
    except ToolError:
        if options.expect_paused and "L12" in options.items and log:
            failed = done(
                "L12",
                FAIL,
                {"attempts": log},
                "No connection inside the budget. Add the text of the attempts to sqlerrors._KNOWN_MESSAGES "
                "as 40613 if it is the message of a paused database, or make the budget longer.",
            )
            write_json(spike_out / "L12.json", item_json(failed, hide))
        raise
    first_connect = list(log)
    try:
        refuse_unless_disposable(main_session, options.database, options.confirm)
    except ToolError:
        main_session.close()
        raise
    logins = catalog.one_row(
        main_session, "/* azsqlcd:live_logins */ SELECT ORIGINAL_LOGIN(), USER_NAME(), SUSER_SNAME();"
    )
    hide = private_names(options.server, options.database, [str(name) for name in logins if name])
    main_session.execute(SESSION_OPTIONS)
    remove_spike_objects(main_session)  # leftovers of a run that was stopped
    main_session.execute(f"CREATE SCHEMA {names.quote(SPIKE_SCHEMA)};")
    ctx = Ctx(options, connect, main_session, provider, first_connect)
    results: list[ItemResult] = []
    cleaned = True
    try:
        for item_id in options.items:
            print(f"{item_id}: {TITLES[item_id]} ...", flush=True)
            _usable_main(ctx)
            result = run_item(ctx, item_id)
            results.append(result)
            write_json(spike_out / f"{item_id}.json", item_json(result, hide))
            print(f"{item_id}: {result.result}", flush=True)
    finally:
        ctx.close_opened()
        main_session.close()
        ctx.main.close()
        try:
            last = connect()
            last.execute(SESSION_OPTIONS)
            remove_spike_objects(last)
            last.close()
        except (SqlError, ToolError, RuntimeError) as error:
            cleaned = False
            print(
                f"CLEANUP FAILED: {error}. Objects of schema {SPIKE_SCHEMA} remain; run again.",
                file=sys.stderr,
            )
    _write_fixtures(ctx, hide)
    on_disk = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))["result"]
        for path in sorted(spike_out.glob("*.json"))
        if path.stem in ITEM_IDS
    }
    totals = summary({item_id: on_disk[item_id] for item_id in ITEM_IDS if item_id in on_disk})
    write_json(spike_out / "summary.json", totals)
    print()
    print_table([("item", "result", "title"), *((r.id, r.result, r.title) for r in results)])
    for result in results:
        if result.result != PASS:
            print(f"\n{result.id} {result.result}: {result.decides}")
    print(f"\ndriver: {totals['driver_decision']}")
    print(f"files: {options.out}")
    return 1 if not cleaned or any(result.result == FAIL for result in results) else 0


def main(argv: Sequence[str] | None = None, *, connect: Connect | None = None) -> int:
    options = parse_args(argv)
    if options.list_items:
        print_table([("item", "title"), *((item_id, TITLES[item_id]) for item_id in ITEM_IDS)])
        return 0
    try:
        return run(options, connect)
    except ToolError as error:
        print(f"azsqlcd live spike: {error}", file=sys.stderr)
        return int(error.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
