"""Schema azsqlcd: the administrator script, the read of the recorded state, the write statements.

Only the output of setup_sql() creates the schema, its four tables, the meta row and the grants
(A2). read_state() reads through a Session. Every write is a function that returns batch text;
the runner sends it, so the runner alone decides order and transaction.

A batch has no driver parameters. A value reaches batch text only as a checked whole number, a
checked hex digest, a word of a closed list, or an N'...' literal made by literal().
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from azsqlcd import names
from azsqlcd.config import ENVIRONMENTS, Config, resolve_target
from azsqlcd.errors import refused
from azsqlcd.session import Session
from azsqlcd.sqlerrors import redact

SCHEMA = "azsqlcd"
STATE_VERSION = 1
CAPTURE_FORMAT = 1
TABLES = ("meta", "run", "step", "object")
RUN_COMMANDS = ("deploy", "baseline", "resolve")  # A3
# The commands whose ok run records a release. A resolve run records none, whatever its row holds.
RELEASE_COMMANDS = ("deploy", "baseline")
RUN_STATUSES = ("running", "ok", "failed", "unknown")
STEP_KINDS = ("baseline", "migration", "nontx", "modules", "resolve")  # A3
STEP_STATUSES = ("ok", "started", "unknown", "not_applied")
OBJECT_STATUSES = ("managed", "dropped")

_META, _RUN, _STEP, _OBJECT = (names.qualified(SCHEMA, table) for table in TABLES)
_HEX = re.compile(r"[0-9a-f]+")
# The meta row is compared exactly, whatever the collation of the database is.
_EXACT = "COLLATE Latin1_General_100_BIN2"
# An update of state that changes no row is a lost record. The engine raises; with XACT_ABORT ON
# the open transaction is rolled back.
_ONE_ROW = " IF @@ROWCOUNT <> 1 THROW 51001, N'azsqlcd: a state write did not change one row', 1;"


# ------------------------------------------------------------------ values in batch text
def literal(text: str) -> str:
    """An N'...' literal for batch text: names.sql_literal, which holds the rules.

    ValueError for NUL and for a backslash directly before a line break.
    """
    return names.sql_literal(text)


def rows(session: Session, batch: str) -> list[tuple[Any, ...]]:
    """Rows of a batch that is one SELECT. A batch that gives no result set gives no rows."""
    result_sets = session.execute(batch)
    return result_sets[0] if result_sets else []


def _int(value: int, low: int = 0) -> str:
    if type(value) is not int or value < low:  # bool is not a number here
        raise ValueError(f"not a whole number of {low} or higher: {value!r}")
    return str(value)


def _hex(value: str, length: int) -> str:
    """A digest as the tool computes it: lower-case hex of the full length."""
    if not isinstance(value, str) or len(value) != length or not _HEX.fullmatch(value):
        raise ValueError(f"not {length} lower-case hex characters")
    return names.sql_literal(value)


def _word(value: str, allowed: Collection[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{value!r} is not one of: {', '.join(allowed)}")
    return names.sql_literal(value)


def _units(text: str) -> int:
    """Length as nvarchar counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _name(value: str, limit: int) -> str:
    """Text that identifies a row. It must fit its column: a cut value would be another identity."""
    if not isinstance(value, str) or not value or _units(value) > limit:
        raise ValueError(f"an identifying value needs 1 to {limit} characters")
    return literal(value)


def _key(key: str) -> str:
    names.parse_object_key(key)  # ValueError for text that is not an object key
    return _name(key, 300)


def _note(value: str | None, limit: int) -> str:
    """Text that describes: one line, cut to its column, so a long note cannot fail a state write."""
    line = " ".join("".join(ch for ch in value if ch.isprintable() or ch.isspace()).split()) if value else ""
    if not line:
        return "NULL"
    # a surrogate pair that the cut splits is dropped
    cut = line.encode("utf-16-le", "surrogatepass")[: 2 * limit].decode("utf-16-le", "ignore")
    return literal(cut)


def _optional[T](value: T | None, render: Callable[[T], str]) -> str:
    return "NULL" if value is None else render(value)


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a time without a time zone cannot be stored as UTC")
    text = value.astimezone(UTC).isoformat(timespec="milliseconds").removesuffix("+00:00")
    return f"CONVERT(datetime2(3), {names.sql_literal(text)}, 126)"


def _insert(table: str, values: dict[str, str]) -> str:
    columns = ", ".join(names.quote(column) for column in values)
    return f"INSERT INTO {table} ({columns}) VALUES ({', '.join(values.values())});"


def _update(table: str, values: dict[str, str], where: str) -> str:
    assignments = ", ".join(f"{names.quote(column)} = {value}" for column, value in values.items())
    return f"UPDATE {table} SET {assignments} WHERE {where};"


# ------------------------------------------------------------------ capture
def capture_json(capture: dict[str, Any]) -> str:
    """Canonical JSON of a capture: sorted keys, no spaces, ASCII only.

    ASCII only (other characters as \\uXXXX), so the text that is stored in nvarchar(max) and read
    back is byte for byte the text that was hashed, whatever the collation or the driver does.
    """
    return json.dumps(capture, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def capture_sha256(capture: dict[str, Any]) -> str:
    """sha256 (lower-case hex) of capture_json(capture). This is azsqlcd.object.catalog_sha256."""
    return hashlib.sha256(capture_json(capture).encode("ascii")).hexdigest()


# ------------------------------------------------------------------ administrator script (A2)
def _in_list(values: Collection[str]) -> str:
    return ", ".join(names.sql_literal(value) for value in values)


_HEADER = """\
-- azsqlcd setup script. An administrator of the database runs it once in the target database.
-- It is safe to run again: every CREATE is behind a check and the meta row is never changed.
-- Not in this script: rights on the schemas of the application. Where migrations hold data
-- batches, grant SELECT, INSERT, UPDATE, DELETE ON SCHEMA::[<schema>] to the deploy user."""

_CREATE_META = f"""\
IF OBJECT_ID({names.sql_literal(_META)}, N'U') IS NULL
    CREATE TABLE {_META} (
      [id]            tinyint       NOT NULL CONSTRAINT [PK_meta] PRIMARY KEY CLUSTERED
                                    CONSTRAINT [CK_meta_one] CHECK ([id] = 1),
      [state_version] int           NOT NULL,
      [project]       nvarchar(128) NOT NULL,
      [environment]   varchar(20)   NOT NULL,
      [created_utc]   datetime2(3)  NOT NULL
    );"""

_CREATE_RUN = f"""\
IF OBJECT_ID({names.sql_literal(_RUN)}, N'U') IS NULL
    CREATE TABLE {_RUN} (
      [run_id]             bigint IDENTITY(1,1) NOT NULL CONSTRAINT [PK_run] PRIMARY KEY CLUSTERED,
      [command]            varchar(10)    NOT NULL
                           CONSTRAINT [CK_run_command] CHECK ([command] IN ({_in_list(RUN_COMMANDS)})),
      [status]             varchar(10)    NOT NULL
                           CONSTRAINT [CK_run_status] CHECK ([status] IN ({_in_list(RUN_STATUSES)})),
      [segments_committed] int            NOT NULL,
      [release_seq]        int            NOT NULL,
      [git_sha]            char(40)       NOT NULL,
      [manifest_sha256]    char(64)       NOT NULL,
      [plan_sha256]        char(64)       NOT NULL,
      [tool_version]       varchar(32)    NOT NULL,
      [tool_digest]        char(64)       NOT NULL,
      [started_utc]        datetime2(3)   NOT NULL,
      [finished_utc]       datetime2(3)   NULL,
      [session_id]         int            NOT NULL,
      [principal_name]     sysname        NOT NULL,
      [previous_git_sha]   char(40)       NULL,
      [approved_by]        nvarchar(400)  NULL,
      [approved_utc]       datetime2(3)   NULL,
      [triggering_actor]   nvarchar(128)  NULL,
      [ci_actor]           nvarchar(128)  NULL,
      [ci_run_url]         nvarchar(400)  NULL,
      [failed_step]        nvarchar(200)  NULL,
      [error_number]       int            NULL,
      [error_text]         nvarchar(300)  NULL,
      [note]               nvarchar(1000) NULL
    );"""

_CREATE_STEP = f"""\
IF OBJECT_ID({names.sql_literal(_STEP)}, N'U') IS NULL
    CREATE TABLE {_STEP} (
      [step_id]      bigint IDENTITY(1,1) NOT NULL CONSTRAINT [PK_step] PRIMARY KEY CLUSTERED,
      [run_id]       bigint        NOT NULL CONSTRAINT [FK_step_run] REFERENCES {_RUN} ([run_id]),
      [kind]         varchar(10)   NOT NULL
                     CONSTRAINT [CK_step_kind] CHECK ([kind] IN ({_in_list(STEP_KINDS)})),
      [migration_id] nvarchar(200) NULL,
      [file_sha256]  char(64)      NULL,
      [status]       varchar(12)   NOT NULL
                     CONSTRAINT [CK_step_status] CHECK ([status] IN ({_in_list(STEP_STATUSES)})),
      [applied_utc]  datetime2(3)  NOT NULL,
      [note]         nvarchar(400) NULL
    );"""

_CREATE_STEP_INDEX = f"""\
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE [object_id] = OBJECT_ID({names.sql_literal(_STEP)}) AND [name] = N'UX_step_migration')
    CREATE UNIQUE NONCLUSTERED INDEX [UX_step_migration] ON {_STEP} ([migration_id])
    WHERE [migration_id] IS NOT NULL;"""

_CREATE_OBJECT = f"""\
IF OBJECT_ID({names.sql_literal(_OBJECT)}, N'U') IS NULL
    CREATE TABLE {_OBJECT} (
      [object_key]      nvarchar(300) NOT NULL CONSTRAINT [PK_object] PRIMARY KEY CLUSTERED,
      [status]          varchar(8)    NOT NULL
                        CONSTRAINT [CK_object_status] CHECK ([status] IN ({_in_list(OBJECT_STATUSES)})),
      [source_sha256]   char(64)      NULL,
      [capture_format]  int           NOT NULL,
      [catalog_capture] nvarchar(max) NOT NULL,
      [catalog_sha256]  char(64)      NOT NULL,
      [run_id]          bigint        NOT NULL CONSTRAINT [FK_object_run] REFERENCES {_RUN} ([run_id]),
      [recorded_utc]    datetime2(3)  NOT NULL
    );"""


def _stop(message: str) -> str:
    """Body of a guard of the script: raise, then execute nothing more. The message is a constant."""
    return (
        "BEGIN\n"
        f"    RAISERROR({names.sql_literal(message)}, 16, 1);\n"
        "    SET NOEXEC ON;  -- RAISERROR does not stop the batches after the next GO; NOEXEC does\n"
        "END"
    )


def setup_sql(config: Config, env: str, target_id: str) -> str:
    """The script that an administrator runs once in one target database (A2). Batches end with GO.

    Values of azsqlcd.toml appear only as N'...' literals, as bracket-quoted names and as the hex
    of a client id; never in a comment. Three guards stop the script (SET NOEXEC ON): another
    database or engine, state of another project, a principal name or SID that is taken. A
    database that is bound to another environment keeps its meta row and the script raises at
    its end; the users and grants exist by then, which resolve --rebind-environment needs (A16).
    """
    environment, target = resolve_target(config, env, target_id)
    project = config.project.name
    users = {role: f"azsqlcd-{project}-{env}-{role}" for role in ("plan", "deploy")}
    # The client tool splits the script at GO lines, and not every tool reads string literals
    # first. A value with a line break could then start a batch of its own.
    for path, value in (("project.name", project), (f"env.{env}.targets[].database", target.database)):
        if not value.isprintable():
            raise refused(
                "CONFIG_INVALID",
                f"azsqlcd.toml: {path}: a value with a line break or a control character cannot be "
                "written into the setup script",
                key=path,
            )
    if _units(users["deploy"]) > 128:  # sysname
        raise refused(
            "CONFIG_INVALID",
            "azsqlcd.toml: project.name: too long for the name of the database users",
            key="project.name",
        )
    clients = {"plan": config.identities[environment.plan_identity]}
    clients["deploy"] = config.identities[environment.deploy_identity]
    if clients["plan"].lower() == clients["deploy"].lower():
        del clients["plan"]  # one identity has one user; the rights of deploy include those of plan

    bound_to = _word(env, ENVIRONMENTS)
    batches: list[str] = []

    def add(why: str, sql: str) -> None:
        batches.append(f"-- Why: {why}\n{sql}")

    add(
        "the filtered index of the step table can be created only with these session settings.",
        "SET ANSI_NULLS, QUOTED_IDENTIFIER, ANSI_PADDING, ANSI_WARNINGS, ARITHABORT, "
        "CONCAT_NULL_YIELDS_NULL ON;",
    )
    add("the same index needs this setting to be off.", "SET NUMERIC_ROUNDABORT OFF;")
    add(
        "the script is made for one database of Azure SQL Database; anywhere else it changes nothing.",
        f"IF DB_NAME() <> {literal(target.database)} OR CAST(SERVERPROPERTY(N'EngineEdition') AS int) <> 5\n"
        + _stop("azsqlcd setup: this is not the database the script was made for. Nothing was changed.")
        + ";",
    )
    add(
        "all state of the tool lives in one schema, owned by dbo, that only this script creates.",
        f"IF SCHEMA_ID({names.sql_literal(SCHEMA)}) IS NULL\n"
        f"    EXEC (N'CREATE SCHEMA {names.quote(SCHEMA)} AUTHORIZATION [dbo];');",
    )
    add("one row binds the database to a project and an environment; every run checks it.", _CREATE_META)
    add("one row for each run: what ran, who approved it, how it ended, and the commit fence.", _CREATE_RUN)
    add("one row for each applied migration and for each other step of a run.", _CREATE_STEP)
    add("a migration is recorded once; a second row for the same migration must fail.", _CREATE_STEP_INDEX)
    add("one row for each managed object: its last deployed source and its catalog capture.", _CREATE_OBJECT)
    add(
        "state of another project, or of a state version this script does not know, is never touched.",
        f"IF EXISTS (SELECT 1 FROM {_META}\n"
        f"           WHERE [project] <> {literal(project)} {_EXACT} OR [state_version] <> {STATE_VERSION})\n"
        + _stop(
            "azsqlcd setup: this database holds the state of another project or of another state "
            "version. No user was created and no right was granted."
        )
        + ";",
    )
    for role, client_id in clients.items():
        _add_user(add, role, users[role], client_id)
    add(
        "the meta row is written once. A database that is bound to another environment is not bound "
        "again here: that is a decision for resolve --rebind-environment.",
        f"IF NOT EXISTS (SELECT 1 FROM {_META})\n    "
        + _insert(
            _META,
            {
                "id": "1",
                "state_version": str(STATE_VERSION),
                "project": literal(project),
                "environment": bound_to,
                "created_utc": "SYSUTCDATETIME()",
            },
        )
        + f"\nELSE IF EXISTS (SELECT 1 FROM {_META} WHERE [environment] <> {bound_to} {_EXACT})\n"
        "    RAISERROR(N'azsqlcd setup: this database is bound to another environment. The meta row was "
        "not changed. After a refresh from another environment use azsqlcd resolve "
        "--rebind-environment. In any other case this script ran in the wrong database.', 16, 1);",
    )
    add("gives the session back to the administrator when a guard stopped the script.", "SET NOEXEC OFF;")
    return _HEADER + "\n" + "\nGO\n".join(batches) + "\nGO\n"


def _add_user(add: Callable[[str, str], None], role: str, user: str, client_id: str) -> None:
    """The contained user of one pipeline identity and its rights (Part 2 (i), A2)."""
    quoted = names.quote(user)
    name = literal(user)
    # uniqueidentifier stores its first three groups little-endian; bytes_le is that layout
    sid = "0x" + uuid.UUID(client_id).bytes_le.hex().upper()
    role_sql = names.sql_literal(f"CREATE ROLE {quoted};")
    member_sql = names.sql_literal(f"ALTER ROLE {quoted} ADD MEMBER ")
    add(
        f"the {role} identity signs in as this user. Rights go only to the principal with this name "
        "and this SID.\n"
        "-- Needs live check: WITH SID = the 16 bytes of the client id, TYPE = E (external principal). "
        "This form needs no directory lookup.\n"
        "-- A SID is unique in a database. When another environment uses the same identity and its "
        "user is here (a\n-- copy of that environment, or one database for two environments), that "
        "user is kept: a role with the\n-- name of the user of this environment is made, the user "
        "is its member, and the grants below go to the role.\n"
        "-- Needs live check: the role path has not run on a database.",
        f"IF EXISTS (SELECT 1 FROM sys.database_principals\n"
        f"           WHERE [name] = {name} AND ([sid] IS NULL OR [sid] <> {sid})\n"
        f"             AND NOT ([type] = N'R' AND EXISTS (SELECT 1 FROM sys.database_principals AS t "
        f"WHERE t.[sid] = {sid})))\n"
        + _stop(
            f"azsqlcd setup: the name of the {role} user is taken by another database principal (it has "
            "another SID: the user of another identity, or a role). No right was granted to it. If the "
            "client id in azsqlcd.toml is right, drop that user, then run this script again."
        )
        + "\nELSE IF EXISTS (SELECT 1 FROM sys.database_principals "
        f"WHERE [sid] = {sid} AND [name] <> {name})\n"
        "BEGIN\n"
        "    DECLARE @twin nvarchar(258) = (SELECT QUOTENAME([name]) FROM sys.database_principals "
        f"WHERE [sid] = {sid});\n"
        f"    IF DATABASE_PRINCIPAL_ID({name}) IS NULL EXEC ({role_sql});\n"
        f"    EXEC ({member_sql} + @twin + N';');\n"
        f"    PRINT N'azsqlcd setup: the {role} identity has a user in this database under the name of "
        "another environment that uses the same identity. That user is kept and gets the rights of "
        f"{role} through the role ' + {names.sql_literal(quoted)} + N'; its name is ' + @twin + N'. "
        "To have a user with the name of this environment in place of the role: drop that user and "
        "the role, then run this script again.';\n"
        "END\n"
        f"ELSE IF DATABASE_PRINCIPAL_ID({name}) IS NULL\n"
        f"    CREATE USER {quoted} WITH SID = {sid}, TYPE = E;",
    )
    if role == "deploy":
        add(
            "the deploy identity runs the DDL of migrations and modules. No db_securityadmin, no db_owner.",
            f"ALTER ROLE [db_ddladmin] ADD MEMBER {quoted};",
        )
    add(
        f"{role} reads the definition and the catalog rows of every object.",
        f"GRANT VIEW DEFINITION TO {quoted};",
    )
    add(
        f"{role} reads the row count and the reserved pages of a table (sys.dm_db_partition_stats).",
        f"GRANT VIEW DATABASE STATE TO {quoted};",
    )
    if role == "deploy":
        add(
            "deploy writes run, step and object rows. No DELETE: a state row is never removed.",
            f"GRANT SELECT, INSERT, UPDATE ON SCHEMA::{names.quote(SCHEMA)} TO {quoted};",
        )
    else:
        add(
            "plan reads the recorded state and never writes it.",
            f"GRANT SELECT ON SCHEMA::{names.quote(SCHEMA)} TO {quoted};",
        )
    add(
        f"{role} finds the dependants of a changed table; VIEW DEFINITION alone does not open this view.",
        f"GRANT SELECT ON OBJECT::[sys].[sql_expression_dependencies] TO {quoted};",
    )


# ------------------------------------------------------------------ recorded state
@dataclass(frozen=True)
class Meta:
    state_version: int
    project: str
    environment: str


@dataclass(frozen=True)
class RunRow:
    run_id: int
    command: str  # deploy | baseline | resolve
    status: str  # running | ok | failed | unknown
    segments_committed: int
    release_seq: int
    git_sha: str
    started_utc: str  # server time, ISO 8601 with milliseconds: the point-in-time-restore reference


@dataclass(frozen=True)
class StepRow:
    step_id: int
    run_id: int
    kind: str  # baseline | migration | nontx | modules | resolve
    migration_id: str | None
    file_sha256: str | None
    status: str  # ok | started | unknown | not_applied
    note: str | None


@dataclass(frozen=True)
class ObjectRow:
    status: str  # managed | dropped
    source_sha256: str | None  # None = deploy the file again
    capture_format: int
    capture: dict[str, Any]
    catalog_sha256: str


@dataclass(frozen=True)
class State:
    meta: Meta
    open_runs: tuple[RunRow, ...]  # status running or unknown, by run_id
    # the ok run of a command of RELEASE_COMMANDS with the highest release_seq, then the highest run_id
    latest_ok_run: RunRow | None
    steps: tuple[StepRow, ...]  # every step, by step_id
    objects: dict[str, ObjectRow]  # object key as stored -> row; dropped rows too
    # Highest release_seq of a deploy run that committed a unit of work, whatever its status; 0
    # when none did. The fence (segments_committed) is committed with the unit, so a run that
    # committed and never became ok (RUN_NOT_CLOSED, a session lost at COMMIT) still counts: the
    # database holds what that release deployed (A6).
    committed_release_seq: int = 0

    @property
    def recorded_release_seq(self) -> int:
        """Highest release_seq of an ok deploy or baseline run; 0 when none ended ok."""
        return self.latest_ok_run.release_seq if self.latest_ok_run else 0

    @property
    def recorded_git_sha(self) -> str | None:
        # RP2-5: a baseline on a target with no release writes release 0 and forty zeros: no commit
        run = self.latest_ok_run
        if run is None or run.release_seq == 0 or not run.git_sha.strip("0"):
            return None
        return run.git_sha


_READ_TABLES = (
    "/* azsqlcd:read_state.tables */ SELECT t.[name], "
    f"HAS_PERMS_BY_NAME({names.sql_literal(names.quote(SCHEMA) + '.')} + QUOTENAME(t.[name]), N'OBJECT', "
    f"N'SELECT') FROM sys.tables AS t WHERE t.[schema_id] = SCHEMA_ID({names.sql_literal(SCHEMA)});"
)
_READ_META = (
    "/* azsqlcd:read_state.meta */ "
    f"SELECT [state_version], [project], [environment] FROM {_META} WHERE [id] = 1;"
)
_READ_RUNS = (
    "/* azsqlcd:read_state.runs */ SELECT r.[run_id], r.[command], r.[status], r.[segments_committed], "
    f"r.[release_seq], r.[git_sha], CONVERT(char(23), r.[started_utc], 126) FROM {_RUN} AS r "
    "WHERE r.[status] IN (N'running', N'unknown') OR r.[run_id] = ("
    f"SELECT TOP (1) k.[run_id] FROM {_RUN} AS k WHERE k.[status] = N'ok' "
    f"AND k.[command] IN ({_in_list(RELEASE_COMMANDS)}) "
    "ORDER BY k.[release_seq] DESC, k.[run_id] DESC) OR r.[run_id] = ("
    # the deploy run of the highest release that committed a unit of work, whatever its status
    f"SELECT TOP (1) c.[run_id] FROM {_RUN} AS c WHERE c.[command] = N'deploy' "
    "AND c.[segments_committed] > 0 "
    "ORDER BY c.[release_seq] DESC, c.[run_id] DESC) ORDER BY r.[run_id];"
)
_READ_STEPS = (
    "/* azsqlcd:read_state.steps */ SELECT [step_id], [run_id], [kind], [migration_id], [file_sha256], "
    f"[status], [note] FROM {_STEP} ORDER BY [step_id];"
)
_READ_OBJECTS = (
    "/* azsqlcd:read_state.objects */ SELECT [object_key], [status], [source_sha256], [capture_format], "
    f"[catalog_capture], [catalog_sha256] FROM {_OBJECT} ORDER BY [object_key];"
)


def read_state(session: Session) -> State:
    """Everything that plan and deploy need from schema azsqlcd. Sends five SELECT batches.

    Raises ToolError REFUSED: STATE_MISSING (a table, the right to read it, or the meta row is
    absent: something of the setup script), STATE_VERSION_UNSUPPORTED, STATE_INVALID (a capture
    that is not a JSON object).
    """
    readable = {str(name).lower(): can_select for name, can_select in rows(session, _READ_TABLES)}
    missing = [table for table in TABLES if table not in readable]
    missing += [f"SELECT right on {table}" for table in TABLES if readable.get(table, 1) != 1]
    meta_rows = [] if missing else rows(session, _READ_META)
    if not meta_rows:
        raise refused(
            "STATE_MISSING",
            "schema azsqlcd is missing or not complete in this database "
            f"(missing: {', '.join(missing) or 'the meta row'}). An administrator runs the script of "
            "`azsqlcd setup-sql` once for each target",
            missing=missing or ["meta row"],
        )
    version, project, environment = meta_rows[0]
    meta = Meta(int(version), str(project), str(environment))
    if meta.state_version != STATE_VERSION:
        raise refused(
            "STATE_VERSION_UNSUPPORTED",
            f"azsqlcd.meta holds state version {meta.state_version}; this tool reads version {STATE_VERSION}",
            state_version=meta.state_version,
            supported=STATE_VERSION,
        )
    runs = [
        RunRow(int(r[0]), str(r[1]), str(r[2]), int(r[3]), int(r[4]), str(r[5]), str(r[6]))
        for r in rows(session, _READ_RUNS)
    ]
    steps = tuple(
        StepRow(int(s[0]), int(s[1]), str(s[2]), s[3], s[4], str(s[5]), s[6])
        for s in rows(session, _READ_STEPS)
    )
    objects: dict[str, ObjectRow] = {}
    for key, status, source_sha256, capture_format, text, catalog_sha256 in rows(session, _READ_OBJECTS):
        try:
            capture = json.loads(text)
        except ValueError:
            capture = None
        if not isinstance(capture, dict):
            raise refused(
                "STATE_INVALID",
                f"azsqlcd.object: the capture of {key} is not a JSON object; the row was changed by hand",
                object=key,
            )
        objects[key] = ObjectRow(
            str(status), source_sha256, int(capture_format), capture, str(catalog_sha256)
        )
    return State(
        meta=meta,
        open_runs=tuple(run for run in runs if run.status in ("running", "unknown")),
        latest_ok_run=max(
            (run for run in runs if run.status == "ok" and run.command in RELEASE_COMMANDS),
            key=lambda run: (run.release_seq, run.run_id),
            default=None,
        ),
        steps=steps,
        objects=objects,
        committed_release_seq=max(
            (run.release_seq for run in runs if run.command == "deploy" and run.segments_committed > 0),
            default=0,
        ),
    )


# ------------------------------------------------------------------ write statements
def insert_run(
    *,
    command: str,
    release_seq: int,
    git_sha: str,
    manifest_sha256: str,
    plan_sha256: str,
    tool_version: str,
    tool_digest: str,
    previous_git_sha: str | None = None,
    approved_by: str | None = None,
    approved_utc: datetime | None = None,
    triggering_actor: str | None = None,
    ci_actor: str | None = None,
    ci_run_url: str | None = None,
    note: str | None = None,
) -> str:
    """A new run row: status running, fence 0, server time. One result set: one row (run_id)."""
    if not tool_version.isascii() or not tool_version.isprintable():
        raise ValueError("tool_version must be printable ASCII")
    values = {
        "command": _word(command, RUN_COMMANDS),
        "status": names.sql_literal("running"),
        "segments_committed": "0",
        "release_seq": _int(release_seq),
        "git_sha": _hex(git_sha, 40),
        "manifest_sha256": _hex(manifest_sha256, 64),
        "plan_sha256": _hex(plan_sha256, 64),
        "tool_version": _name(tool_version, 32),
        "tool_digest": _hex(tool_digest, 64),
        "started_utc": "SYSUTCDATETIME()",
        "session_id": "@@SPID",  # a note only; it takes part in no decision (A3)
        "principal_name": "COALESCE(ORIGINAL_LOGIN(), USER_NAME())",
        "previous_git_sha": _optional(previous_git_sha, lambda sha: _hex(sha, 40)),
        "approved_by": _note(approved_by, 400),
        "approved_utc": _optional(approved_utc, _utc),
        "triggering_actor": _note(triggering_actor, 128),
        "ci_actor": _note(ci_actor, 128),
        "ci_run_url": _note(ci_run_url, 400),
        "note": _note(note, 1000),
    }
    return _insert(_RUN, values) + " SELECT CAST(SCOPE_IDENTITY() AS bigint);"


def set_run_status(
    run_id: int,
    status: str,
    *,
    failed_step: str | None = None,
    error_number: int | None = None,
    error_text: str | None = None,
    note: str | None = None,
) -> str:
    """End a run: ok, failed or unknown, with the server time. Columns that are not given stay.

    error_text is redacted here (A25), so no caller can store the full engine message.
    """
    values = {"status": _word(status, RUN_STATUSES[1:]), "finished_utc": "SYSUTCDATETIME()"}
    if failed_step is not None:
        values["failed_step"] = _note(failed_step, 200)
    if error_number is not None:
        values["error_number"] = _int(error_number)
    if error_text is not None:
        values["error_text"] = _note(redact(error_text), 300)
    if note is not None:
        values["note"] = _note(note, 1000)
    return _update(_RUN, values, f"[run_id] = {_int(run_id, 1)}") + _ONE_ROW


def bump_fence(run_id: int) -> str:
    """Count one more unit of work as committed. It is sent inside the transaction of that unit."""
    fence = {"segments_committed": "[segments_committed] + 1"}
    return _update(_RUN, fence, f"[run_id] = {_int(run_id, 1)}") + _ONE_ROW


def read_fence(run_id: int) -> str:
    """One result set: (segments_committed) of the run; no row when the run does not exist."""
    return (
        f"/* azsqlcd:read_fence */ SELECT [segments_committed] FROM {_RUN} "
        f"WHERE [run_id] = {_int(run_id, 1)};"
    )


def read_fence_locking(run_id: int) -> str:
    """read_fence that waits for a writer of the row, also under snapshot isolation of statements.

    Used on a new connection after a lost one: the answer is the committed value or a lock timeout.
    """
    return (
        f"/* azsqlcd:read_fence_locking */ SELECT [segments_committed] FROM {_RUN} "
        f"WITH (READCOMMITTEDLOCK, ROWLOCK) WHERE [run_id] = {_int(run_id, 1)};"
    )


def insert_step(
    *,
    run_id: int,
    kind: str,
    status: str,
    migration_id: str | None = None,
    file_sha256: str | None = None,
    note: str | None = None,
) -> str:
    """A new step row with the server time. A second row for one migration_id fails in the engine."""
    values = {
        "run_id": _int(run_id, 1),
        "kind": _word(kind, STEP_KINDS),
        "migration_id": _optional(migration_id, lambda name: _name(name, 200)),
        "file_sha256": _optional(file_sha256, lambda sha: _hex(sha, 64)),
        "status": _word(status, STEP_STATUSES),
        "applied_utc": "SYSUTCDATETIME()",
        "note": _note(note, 400),
    }
    return _insert(_STEP, values)


def set_step_status(
    migration_id: str, status: str, *, run_id: int | None = None, note: str | None = None
) -> str:
    """Change the step of a migration. run_id moves the step to the run that works on it now."""
    values = {"status": _word(status, STEP_STATUSES), "applied_utc": "SYSUTCDATETIME()"}
    if run_id is not None:
        values["run_id"] = _int(run_id, 1)
    if note is not None:
        values["note"] = _note(note, 400)
    return _update(_STEP, values, f"[migration_id] = {_name(migration_id, 200)}") + _ONE_ROW


def upsert_object(key: str, *, run_id: int, capture: dict[str, Any], source_sha256: str | None) -> str:
    """Record a managed object with its capture: UPDATE, then INSERT when no row was updated."""
    text = capture_json(capture)
    values = {
        "status": names.sql_literal("managed"),
        "source_sha256": _optional(source_sha256, lambda sha: _hex(sha, 64)),
        "capture_format": str(CAPTURE_FORMAT),
        "catalog_capture": literal(text),
        "catalog_sha256": names.sql_literal(hashlib.sha256(text.encode("ascii")).hexdigest()),
        "run_id": _int(run_id, 1),
        "recorded_utc": "SYSUTCDATETIME()",
    }
    key_literal = _key(key)
    return (
        _update(_OBJECT, values, f"[object_key] = {key_literal}")
        + " IF @@ROWCOUNT = 0 "
        + _insert(_OBJECT, {"object_key": key_literal} | values)
    )


def mark_dropped(key: str, run_id: int) -> str:
    """The object is no longer managed. The row stays: state rows are never deleted."""
    values = {
        "status": names.sql_literal("dropped"),
        "run_id": _int(run_id, 1),
        "recorded_utc": "SYSUTCDATETIME()",
    }
    return _update(_OBJECT, values, f"[object_key] = {_key(key)}") + _ONE_ROW


def set_source_null(key: str) -> str:
    """Forget the deployed source of a module, so the next module step sends its file again."""
    return _update(_OBJECT, {"source_sha256": "NULL"}, f"[object_key] = {_key(key)}") + _ONE_ROW


def set_meta_environment(env: str) -> str:
    """Bind the database to another environment (resolve --rebind-environment)."""
    return _update(_META, {"environment": _word(env, ENVIRONMENTS)}, "[id] = 1") + _ONE_ROW
