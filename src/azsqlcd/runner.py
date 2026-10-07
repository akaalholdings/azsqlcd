"""The runner: `deploy` and the resolve actions. No other module changes a database.

One session, one session applock, one unit of work. A deploy computes the plan again under the
lock and runs only when its hash is the expected one. A unit of work is one transaction, or one
non-transactional batch between a committed `started` marker and a small closing transaction.

What the code holds to (design Part 2 (e), amendments A1, A4, A5):
  - every batch of a unit of work goes through _send(), which keeps a dispatch log and refuses
    to send a step twice. Nothing is retried;
  - after every batch the guard reads (@@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID()). A
    value other than (1, 1, id of the transaction) stops the dispatch: the outcome is unknown. So
    does a guard that gives no row, a row of another shape, or an error of any class;
  - after an error with the connection alive the tool rolls back, reads @@TRANCOUNT = 0 and reads
    the fence (run.segments_committed). Only then is the unit "rolled back";
  - a lost connection in a transaction is decided on a new session: it takes the deploy lock and
    reads the fence with a locking read (rolled back or committed: exit 24). When that read gives
    no answer, and for a lost connection in a non-transactional step, the outcome is "unknown";
  - an exception that the tool does not know is 22 before the first batch of the unit of work
    and 23 after it. Every end of a command is a RunError with a Report: a write that only
    records an outcome that is decided (the run row, the release of the lock, the close of the
    session) cannot raise past it, whatever the driver raises.

Batch text, definition text and tokens never reach a Report, an exception message or the run
row. Engine messages are stored and printed redacted (A25).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from azsqlcd import catalog, chain, lex, lint, modules, names, plan, release, state
from azsqlcd.catalog import Difference
from azsqlcd.config import Config, resolve_target
from azsqlcd.errors import Exit, ToolError, failed, locked, refused, retry_safe, unknown
from azsqlcd.model import fold
from azsqlcd.modules import ModuleFile
from azsqlcd.plan import ALREADY_PAST, NOOP, Plan, Step, Unit
from azsqlcd.release import Bundle
from azsqlcd.session import Session, TokenProvider, require_token_life
from azsqlcd.sqlerrors import ErrorClass, SqlError
from azsqlcd.state import State

# Read-back compares the stored definition with what the engine keeps of the file text
# (modules.stored_text: CREATE OR ALTER is stored as CREATE, every other byte as sent). Proven on
# Azure SQL Database: live spike L7, and every module deploy of live acceptance run4.
MODULE_TEXT_READBACK = True
# After a lost connection in a transaction: decide by a locking read of the fence on a new session
# (exit 24 when the read decides, exit 23 when it cannot). Proven live: spike L6, acceptance run4.
RECONCILE_BY_LOCKING_READ = True

type SessionFactory = Callable[[], Session]
type Captures = dict[str, dict[str, Any] | None]  # object key -> capture; None = the object is gone
type _Snapshot = tuple[dict[str, dict[str, Any]], dict[str, list[Difference]]]

_NO_COMMIT = "0" * 40
_NO_DIGEST = "0" * 64
_RUN = names.qualified(state.SCHEMA, "run")
_RESOURCE = names.sql_literal(catalog.APPLOCK_RESOURCE)
_RELEASE_LOCK = f"EXEC sys.sp_releaseapplock @Resource = {_RESOURCE}, @LockOwner = N'Session';"
_SPID = "/* azsqlcd:spid */ SELECT @@SPID;"
_GUARD = "/* azsqlcd:guard */ SELECT @@TRANCOUNT, XACT_STATE(), CURRENT_TRANSACTION_ID();"
_TRANCOUNT = "/* azsqlcd:trancount */ SELECT @@TRANCOUNT;"
_ROLLBACK = "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;"
_COMMIT = "COMMIT TRANSACTION;"
_SERVER_NAME = "/* azsqlcd:server_name */ SELECT CAST(SERVERPROPERTY(N'ServerName') AS nvarchar(128));"
_SET_ON = (
    "ANSI_NULLS",
    "QUOTED_IDENTIFIER",
    "ANSI_PADDING",
    "ANSI_WARNINGS",
    "ARITHABORT",
    "CONCAT_NULL_YIELDS_NULL",
)
_XACT_ABORT, _NOCOUNT, _IMPLICIT_TRANSACTIONS = 16384, 512, 2  # bits of @@OPTIONS
# sqlerrors recognises an engine error by its en-US message text, so the session must speak it
_LANGUAGE = "us_english"
_SESSION_OPTIONS = (
    "/* azsqlcd:session_options */ SELECT "
    + ", ".join(f"CAST(SESSIONPROPERTY(N'{name}') AS int)" for name in (*_SET_ON, "NUMERIC_ROUNDABORT"))
    # With IMPLICIT_TRANSACTIONS ON a statement at @@TRANCOUNT 0 opens a transaction that nobody
    # commits. In the transaction of a unit of work the option changes nothing, so no guard sees it.
    + f", @@OPTIONS & {_XACT_ABORT}, @@OPTIONS & {_NOCOUNT}, @@OPTIONS & {_IMPLICIT_TRANSACTIONS}"
    + ", @@LOCK_TIMEOUT, @@LANGUAGE;"
)
_OPTION_NAMES = (
    *_SET_ON,
    "NUMERIC_ROUNDABORT",
    "XACT_ABORT",
    "NOCOUNT",
    "IMPLICIT_TRANSACTIONS",
    "LOCK_TIMEOUT",
    "LANGUAGE",
)
_INSPECT = "Inspect the database, then run azsqlcd resolve"
# N2-F1: the release that failed on a dependant can never be deployed, and a later release that
# only changes the module is refused with CATCHUP_REQUIRED (it did not add the migration)
_FIX_DEPENDANT = (
    "This release cannot be deployed as it is. In one pull request change the module (or tombstone "
    "it), withdraw the migration and add its statements again as a replacement (allow "
    "REPLACEMENT_EDGE): a release that only changes the module is refused with CATCHUP_REQUIRED"
)


# ------------------------------------------------------------------ public data
@dataclass(frozen=True)
class Audit:
    """Who asked for the run (A28, A3). Every value is stored as a note in azsqlcd.run."""

    approved_by: str | None = None
    approved_utc: datetime | None = None  # timezone-aware
    triggering_actor: str | None = None
    ci_actor: str | None = None
    ci_run_url: str | None = None


@dataclass(frozen=True)
class Report:
    """The result of one deploy or resolve run. report.json is to_json()."""

    command: str  # deploy | resolve
    exit_code: int
    reason_code: str  # OK, ALREADY_PAST, or the reason of a non-zero exit
    message: str
    environment: str
    target_id: str
    run_id: int | None  # None: no run row was written
    started_utc: str | None  # server time of the run row: the reference for a point-in-time restore
    release_seq: int
    git_sha: str  # commit of the release
    plan_sha256: str | None
    tool_version: str
    tool_digest: str
    steps_applied: tuple[str, ...] = ()  # ids of the plan steps of every committed unit of work
    modules_deployed: tuple[str, ...] = ()
    modules_dropped: tuple[str, ...] = ()
    failed_step: str | None = None
    warnings: tuple[str, ...] = ()  # the notes of the plan
    # the plan that was computed under the lock, for plan.json of --inline-plan; not in to_json()
    plan: Plan | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> str:
        doc = {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if f.name != "plan"}
        return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


class RunError(ToolError):
    """Every non-zero end of deploy and of a resolve action: the ToolError, with the Report."""

    def __init__(self, error: ToolError, report: Report) -> None:
        super().__init__(error.exit_code, error.reason_code, error.message, detail=error.detail)
        self.report = report


class TableHooks(plan.TableHooks, Protocol):
    """The table model for the runner: the hooks of the planner, and the read-back."""

    def read_back(
        self, session: Session, bundle: Bundle, keys: Collection[str], through: str | None
    ) -> Captures:
        """Compare the catalog with the model of the release for these table-class keys.

        through None: the model of the release. through = a migration id: the model after that
        migration. Returns for each key the capture to record; None when the object is gone and
        the model does not hold it. Raises ToolError FAILED READBACK_MISMATCH (object, property
        and hashes only) when the catalog differs from the model.
        """
        ...


# ------------------------------------------------------------------ one run
@dataclass(frozen=True)
class _Job:
    command: str  # deploy | resolve
    bundle: Bundle
    config: Config
    env: str
    target_id: str
    session_factory: SessionFactory | None  # None: the caller gave the session of the run
    token_provider: TokenProvider | None
    audit: Audit
    tool_version: str
    tool_digest: str
    now: Callable[[], float]


@dataclass
class _Run:
    """The state of one run. It lives for one call of deploy or of a resolve action."""

    job: _Job
    session: Session | None = None
    token_minutes_left: int | None = None
    spid: int | None = None
    recorded: State | None = None
    plan: Plan | None = None
    run_id: int | None = None
    started_utc: str | None = None
    transaction_id: int | None = None
    committed: int = 0  # units of work whose COMMIT the tool saw: what the fence must read
    dispatched: bool = False  # a batch of a unit of work was sent (A1)
    ended: bool = False  # the end of the run row is decided; it is not written a second time
    step: str | None = None  # where the run is, for failed_step
    nontx_started: str | None = None  # migration whose step row is `started`
    dispatch_log: list[str] = field(default_factory=list)
    steps_applied: list[str] = field(default_factory=list)
    modules_deployed: list[str] = field(default_factory=list)
    modules_dropped: list[str] = field(default_factory=list)
    reason_code: str = "OK"
    message: str = ""

    @property
    def db(self) -> Session:
        return _have(self.session)

    @property
    def id(self) -> int:
        return _have(self.run_id)


@dataclass
class _Sent:
    """What one unit of work did so far."""

    migrations: dict[str, str | None] = field(default_factory=dict)  # migration id -> sha256 of its line
    modules: dict[str, ModuleFile] = field(default_factory=dict)  # key -> the file the database holds now
    dropped: list[str] = field(default_factory=list)
    unbound: set[str] = field(default_factory=set)
    steps: list[str] = field(default_factory=list)
    # touched table key -> its columns before the batches; only for a plan with a dependant that
    # the engine cannot verify (_columns_before)
    columns: dict[str, frozenset[str]] = field(default_factory=dict)
    options_asserted: bool = True  # no migration batch ran since the session options were read


def _have[T](value: T | None) -> T:
    if value is None:
        raise RuntimeError("the runner used a value before it was set")
    return value


def _is_module(key: str) -> bool:
    return names.parse_object_key(key)[0] in names.MODULE_KINDS


def _managed(recorded: State) -> dict[str, state.ObjectRow]:
    return {key: row for key, row in recorded.objects.items() if row.status == "managed"}


def _stored_keys(rows: Collection[str]) -> dict[str, str]:
    """Folded key -> the key as the state holds it. Look a key up with fold(key).

    A file, a statement or an operator can spell a key in another letter case than the row has.
    The catalog compares names without case (the fence refuses any other), so both are one object,
    and a state write goes to the row under the key that the row has.
    """
    return {fold(key): key for key in rows}


def _lost(run: _Run, error: SqlError) -> bool:
    return error.cls is ErrorClass.SESSION_LOST or run.session is None or run.session.closed


def _report(run: _Run, error: ToolError | None) -> Report:
    job = run.job
    return Report(
        command=job.command,
        exit_code=int(error.exit_code) if error else int(Exit.OK),
        reason_code=error.reason_code if error else run.reason_code,
        message=error.message if error else run.message,
        environment=job.env,
        target_id=job.target_id,
        run_id=run.run_id,
        started_utc=run.started_utc,
        release_seq=job.bundle.manifest.release_seq,
        git_sha=job.bundle.manifest.commit,
        plan_sha256=run.plan.plan_sha256 if run.plan else None,
        tool_version=job.tool_version,
        tool_digest=job.tool_digest,
        steps_applied=tuple(run.steps_applied),
        modules_deployed=tuple(run.modules_deployed),
        modules_dropped=tuple(run.modules_dropped),
        failed_step=run.step if error else None,
        warnings=run.plan.notes if run.plan else (),
        plan=run.plan,
    )


def _command(run: _Run, body: Callable[[], None]) -> Report:
    """Run the body of a command. Every error leaves as a RunError; the session is always closed."""
    try:
        try:
            body()
        except ToolError:
            raise
        except SqlError as error:
            raise _outside_unit(run, error) from error
        except Exception as error:
            raise _defect(run, error) from error
    except ToolError as error:
        cause = error.__cause__ if isinstance(error.__cause__, SqlError) else None
        if error.exit_code is Exit.UNKNOWN:
            _end_run(run, "unknown", error, cause)
        elif not run.dispatched:
            _end_run(run, "failed", error, cause)
        raise RunError(error, _report(run, error)) from error
    finally:
        if run.session is not None:
            # also gives the session applock back. The outcome is decided: an error of the close
            # must not take the place of the RunError, or of the Report of exit 0
            with contextlib.suppress(Exception):
                run.session.close()
    return _report(run, None)


def _defect(run: _Run, error: Exception) -> ToolError:
    """A1. The text of an error that the tool does not know can hold anything, so only its type is told."""
    name = type(error).__name__
    if run.dispatched:
        return unknown(
            "TOOL_DEFECT_AFTER_DISPATCH",
            f"the tool stopped on an error that it does not know ({name}) after a batch of the unit of "
            f"work was sent. What was applied is not known. {_INSPECT}",
            exception=name,
        )
    return refused(
        "TOOL_DEFECT",
        f"the tool stopped on an error that it does not know ({name}) before any batch of the unit of "
        "work was sent. Nothing was executed",
        exception=name,
    )


def _outside_unit(run: _Run, error: SqlError) -> ToolError:
    """A SqlError outside a unit of work: before the first batch, or after the last COMMIT."""
    detail = {"error_class": error.cls.name, "error_number": error.number}
    if run.dispatched:
        return retry_safe(
            "RUN_NOT_CLOSED",
            f"the unit of work is committed and the run row could not be closed: {error}. Start again: "
            "the next deploy reconciles the run row and records the release",
            **detail,
        )
    if _lost(run, error):
        return retry_safe(
            "CONNECTION_LOST",
            f"the connection was lost before any batch of the unit of work was sent: {error}. Start again",
            **detail,
        )
    if error.cls in (ErrorClass.LOCK_TIMEOUT, ErrorClass.DEADLOCK):
        return retry_safe(
            error.cls.name,
            f"a read or a state write of the tool stopped before any batch of the unit of work was "
            f"sent: {error}. Start again",
            **detail,
        )
    return refused(
        "SQL_ERROR",
        f"the database refused a read or a state write of the tool before any batch of the unit of "
        f"work was sent: {error}. Nothing of the release was executed",
        **detail,
    )


def _end_run(run: _Run, status: str, error: ToolError, sql: SqlError | None) -> None:
    """Write how the run ended. The outcome is decided before; a write that fails changes no exit code."""
    if run.run_id is None or run.ended or run.session is None or run.session.closed:
        return
    run.ended = True
    statements: list[str] = []
    if status == "unknown":
        # an open transaction would take the status with it when the session closes
        statements.append(_ROLLBACK)
        if run.nontx_started is not None:
            statements.append(state.set_step_status(run.nontx_started, "unknown"))
    statements.append(
        state.set_run_status(
            run.run_id,
            status,
            failed_step=run.step,
            error_number=sql.number if sql else None,
            error_text=sql.raw_message if sql else None,  # set_run_status stores it redacted
            note=error.reason_code,
        )
    )
    # any error, not only a SqlError: a driver can raise something else on a dead connection, and
    # an exception from here would leave the command as a raw exception with no Report (A1)
    with contextlib.suppress(Exception):
        run.session.execute(" ".join(statements))


# ------------------------------------------------------------------ connection, fence, lock
def set_session_options(session: Session, lock_timeout_ms: int) -> None:
    """Give a session the options of a run, and assert them (Part 2 (e)).

    XACT_ABORT, NOCOUNT and the ANSI options ON, IMPLICIT_TRANSACTIONS and NUMERIC_ROUNDABORT OFF,
    LOCK_TIMEOUT, and the language us_english: the tool reads engine errors by their en-US text.
    Every session that plans or runs gets them, so a plan reads what a deploy reads. With
    IMPLICIT_TRANSACTIONS ON (a login or a driver can set it) a state write outside a unit of work
    would open a transaction that nobody commits. Refused: SESSION_OPTIONS.
    """
    if type(lock_timeout_ms) is not int or lock_timeout_ms < 0:
        raise ValueError("lock_timeout_ms must be a whole number of 0 or higher")
    session.execute(
        f"SET XACT_ABORT ON; SET LOCK_TIMEOUT {lock_timeout_ms}; SET NOCOUNT ON; "
        f"SET IMPLICIT_TRANSACTIONS OFF; SET {', '.join(_SET_ON)} ON; SET NUMERIC_ROUNDABORT OFF; "
        f"SET LANGUAGE {_LANGUAGE};"
    )
    _assert_session_options(session, lock_timeout_ms)


def _assert_session_options(session: Session, lock_timeout_ms: int) -> None:
    found = tuple(catalog.one_row(session, _SESSION_OPTIONS))
    expected = (*(1,) * len(_SET_ON), 0, _XACT_ABORT, _NOCOUNT, 0, lock_timeout_ms, _LANGUAGE)
    if found != expected:
        order = ", ".join(_OPTION_NAMES)
        raise refused(
            "SESSION_OPTIONS",
            f"the session does not have the options that the tool set ({order}): found "
            f"{list(found)}, expected {list(expected)}",
            found=list(found),
            expected=list(expected),
        )


def _recheck_session_options(run: _Run) -> None:
    """After a batch of a migration, which can hold a SET statement: the options of the run still
    hold, and IMPLICIT_TRANSACTIONS is off. Refused: SESSION_OPTIONS.

    The re-check runs before the first module step after a batch (CREATE OR ALTER stores two of
    the options with the module) and after the last batch of every unit of work, before its state
    rows. So the options are proven for every state write that follows, the run row included.
    """
    environment, _ = resolve_target(run.job.config, run.job.env, run.job.target_id)
    _assert_session_options(run.db, environment.lock_timeout_ms)


def _lock_sql(wait_s: int) -> str:
    if type(wait_s) is not int or wait_s < 0:
        raise ValueError("applock_wait_s must be a whole number of 0 or higher")
    return (
        "/* azsqlcd:lock */ DECLARE @r int; "
        f"EXEC @r = sys.sp_getapplock @Resource = {_RESOURCE}, @LockMode = N'Exclusive', "
        f"@LockOwner = N'Session', @LockTimeout = {wait_s * 1000}; SELECT @r;"
    )


def _assert_owner(run: _Run) -> str:
    """Start of a batch: stop when this is not the session that took the lock, or the lock is gone."""
    spid = _have(run.spid)
    return (
        f"IF @@SPID <> {spid} OR APPLOCK_MODE(N'public', {_RESOURCE}, N'Session') <> N'Exclusive' "
        "THROW 51000, N'azsqlcd: session or lock lost', 1;"
    )


def _open(run: _Run) -> catalog.FenceFacts:
    """Token life and connect (unless the caller gave the session), session options, fence. No lock yet."""
    job = run.job
    environment, target = resolve_target(job.config, job.env, job.target_id)
    if run.session is None:
        now = job.now()
        token = _have(job.token_provider).get()
        require_token_life(token, job.config.project.min_token_minutes, now)
        run.token_minutes_left = int((token.expires_on - now) // 60)
        run.session = _have(job.session_factory)()
    session = run.db
    set_session_options(session, environment.lock_timeout_ms)
    facts = catalog.fence_facts(session)
    plan.check_fence(facts, target.database)  # the fence of the planner: one rule set for plan and deploy
    return facts


def _lock(run: _Run, wait_s: int) -> None:
    (result,) = catalog.one_row(run.db, _lock_sql(wait_s))
    if result not in (0, 1):
        raise locked(
            "LOCK_NOT_GRANTED",
            f"the deploy lock was not granted in {wait_s} s (sp_getapplock gave {result!r}): another "
            "deploy or resolve runs on this database. Nothing was sent. Start again when it ended",
            result=result,
        )
    (spid,) = catalog.one_row(run.db, _SPID)
    if type(spid) is not int:
        raise RuntimeError("@@SPID gave no whole number")
    run.spid = spid


def _unlock(run: _Run) -> None:
    # closing the session gives the lock back in any case; an error here, of any type, decides nothing
    with contextlib.suppress(Exception):
        run.db.execute(_RELEASE_LOCK)


def _read_state(run: _Run, *, check_environment: bool = True) -> State:
    """The recorded state, read under the lock, of a database that is bound to this project."""
    job = run.job
    recorded = state.read_state(run.db)
    meta = recorded.meta
    if meta.project != job.config.project.name or (check_environment and meta.environment != job.env):
        raise refused(
            "FENCE_META_MISMATCH",
            f"this database is bound to project {meta.project!r}, environment {meta.environment!r}; "
            f"the run is for {job.config.project.name!r}, {job.env!r}",
            project=meta.project,
            environment=meta.environment,
        )
    return recorded


def _reconcile(run: _Run, recorded: State) -> None:
    """A4: this session holds the lock, so every run row with the status running is a dead run."""
    unresolved: list[state.StepRow] = []
    for dead in recorded.open_runs:
        if dead.status != "running":
            continue
        started = [
            step
            for step in recorded.steps
            if step.run_id == dead.run_id and step.kind == "nontx" and step.status == "started"
        ]
        statements = [state.set_step_status(_have(step.migration_id), "unknown") for step in started]
        statements.append(
            state.set_run_status(dead.run_id, "unknown" if started else "failed", note="reconciled")
        )
        run.db.execute(f"BEGIN TRANSACTION; {' '.join(statements)} {_COMMIT}")
        unresolved += started
    if unresolved:
        first = unresolved[0]
        raise refused(
            "STEP_UNRESOLVED",
            f"run {first.run_id} died in the non-transactional migration {first.migration_id}: its "
            f"outcome is not known. The step and the run now have the status unknown. {_INSPECT} with "
            "--mark-applied or --mark-not-applied, then --clear-run",
            steps=[
                {"step_id": step.step_id, "migration": step.migration_id, "status": "unknown"}
                for step in unresolved
            ],
        )


def _insert_run(run: _Run, **fields: Any) -> None:
    job, audit = run.job, run.job.audit
    (run_id,) = catalog.one_row(
        run.db,
        state.insert_run(
            command=job.command,
            tool_version=job.tool_version,
            tool_digest=job.tool_digest,
            approved_by=audit.approved_by,
            approved_utc=audit.approved_utc,
            triggering_actor=audit.triggering_actor,
            ci_actor=audit.ci_actor,
            ci_run_url=audit.ci_run_url,
            **fields,
        ),
    )
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise RuntimeError("the insert of the run row gave no run_id")
    run.run_id = run_id
    (started,) = catalog.one_row(
        run.db,
        "/* azsqlcd:run_started */ SELECT CONVERT(char(23), [started_utc], 126) "
        f"FROM {_RUN} WHERE [run_id] = {run_id};",
    )
    run.started_utc = str(started)


def _close_run(run: _Run) -> None:
    """Set the run row to ok, and prove that the write is committed. Then exit 0 is true."""
    run.step = "close"
    run.db.execute(state.set_run_status(run.id, "ok"))
    # the write is committed when the session holds no open transaction. With an open one (a
    # session with IMPLICIT_TRANSACTIONS ON) the close of the session would roll the write back
    # behind an exit 0
    found = _one_row(run.db, _TRANCOUNT, 1)
    if found != (0,):
        raise retry_safe(
            "RUN_NOT_CLOSED",
            "the unit of work is committed, and the write that sets the run row to ok is in an open "
            f"transaction (@@TRANCOUNT is {found[0] if found else None!r}): it is lost when the session "
            "closes. Start again: the next deploy reconciles the run row and records the release",
            trancount=found[0] if found else None,
        )
    run.ended = True
    run.step = None
    _unlock(run)


# ------------------------------------------------------------------ transaction and guard
def _send(run: _Run, step_id: str, text: str) -> None:
    """Send one batch of a unit of work. The dispatch log proves that no batch goes twice."""
    if step_id in run.dispatch_log:
        raise RuntimeError(f"step {step_id} was dispatched before; no batch is sent twice")
    run.dispatch_log.append(step_id)
    run.dispatched = True
    run.db.execute(text)


def _guard_failed(run: _Run, found: object, expected: object) -> ToolError:
    return unknown(
        "GUARD_FAILED",
        f"after step {run.step} the session is not in the transaction of the unit of work: "
        f"(@@TRANCOUNT, XACT_STATE(), transaction id) is {found}, expected {expected}. A batch ended or "
        f"began a transaction. What was applied is not known. {_INSPECT} --clear-run {run.run_id}",
        step=run.step,
    )


def _whole(value: object) -> bool:
    return type(value) is int  # a bool or a NULL is not an answer of a guard


def _one_row(session: Session, batch: str, width: int) -> tuple[Any, ...] | None:
    """The row of a query that proves something. None: the answer is not one result set with one
    row of `width` whole numbers. The driver gives no result set when it cannot describe one, so
    an answer of another shape is never read as a pass."""
    result_sets = session.execute(batch)
    if len(result_sets) != 1 or len(result_sets[0]) != 1:
        return None
    row = tuple(result_sets[0][0])
    return row if len(row) == width and all(_whole(value) for value in row) else None


def _read_guard(run: _Run, batch: str, expected: tuple[object, ...]) -> tuple[Any, ...]:
    """The row of a guard query, which has the shape of `expected`. Anything else is GUARD_FAILED.

    A guard query reads session state only: it cannot fail on a sound session. So an error of any
    class means that what the session holds is not known. A killed session can first show as an
    error of the class OTHER; a lost one goes the way of a lost connection.
    """
    try:
        found = _one_row(run.db, batch, len(expected))
    except SqlError as error:
        if _lost(run, error):
            raise
        raise _guard_failed(run, f"not known, the guard query failed: {error}", expected) from error
    if found is None:
        raise _guard_failed(run, "not known, the guard query gave no row of this shape", expected)
    return found


def _begin(run: _Run) -> None:
    """One batch: assert session and lock, BEGIN TRANSACTION, count the unit in the fence. Then the id."""
    run.step = "begin"
    run.transaction_id = None
    batch = f"{_assert_owner(run)} BEGIN TRANSACTION; {state.bump_fence(run.id)}"
    _send(run, f"begin:{run.committed + 1}", batch)
    expected = (1, 1, "the id of the new transaction")
    found = _read_guard(run, _GUARD, expected)
    if found[:2] != (1, 1):
        raise _guard_failed(run, found, expected)
    run.transaction_id = found[2]


def _guard(run: _Run) -> None:
    """A5: the session is still in the transaction that _begin opened, and the transaction can commit."""
    expected = (1, 1, _have(run.transaction_id))
    found = _read_guard(run, _GUARD, expected)
    if found != expected:
        raise _guard_failed(run, found, expected)


def _no_transaction(run: _Run) -> None:
    """After COMMIT, and after a batch outside a transaction: the session holds no open transaction."""
    found = _read_guard(run, _TRANCOUNT, (0,))
    if found != (0,):
        raise _guard_failed(run, found, (0,))


def _commit(run: _Run) -> None:
    run.step = "commit"
    run.db.execute(_COMMIT)
    _no_transaction(run)
    run.committed += 1
    run.transaction_id = None
    run.step = None


def _classified(run: _Run, error: SqlError | ToolError) -> ToolError:
    """The exit of a unit of work that was rolled back, and proven so (failure matrix)."""
    rolled_back = "The unit of work was rolled back; nothing of it remains"
    if isinstance(error, ToolError):
        message = f"{error.message}. {rolled_back}"
        return ToolError(Exit.FAILED_ROLLED_BACK, error.reason_code, message, detail=error.detail)
    detail = {"step": run.step, "error_class": error.cls.name, "error_number": error.number}
    if error.cls in (ErrorClass.LOCK_TIMEOUT, ErrorClass.DEADLOCK):
        return retry_safe(
            error.cls.name, f"step {run.step} stopped: {error}. {rolled_back}. Start again", **detail
        )
    if error.cls is ErrorClass.GOVERNANCE:
        return failed(
            "GOVERNANCE_LIMIT",
            f"step {run.step} reached a limit of the service: {error}. {rolled_back}. This is never "
            "retried. Use smaller data batches, or an ONLINE, RESUMABLE build in a nontx migration",
            **detail,
        )
    return failed("BATCH_FAILED", f"step {run.step} failed: {error}. {rolled_back}", **detail)


def _connection_lost_tx(run: _Run, error: SqlError) -> ToolError:
    session_factory = run.job.session_factory
    if RECONCILE_BY_LOCKING_READ and session_factory is not None:
        environment, _ = resolve_target(run.job.config, run.job.env, run.job.target_id)
        return reconcile_by_locking_read(
            session_factory,
            run.id,
            run.committed,
            lock_timeout_ms=environment.lock_timeout_ms,
            applock_wait_s=environment.applock_wait_s,
        )
    return _lost_tx(run.id, f"at step {run.step}: {error}")


def _lost_tx(run_id: int, where: str) -> ToolError:
    return unknown(
        "CONNECTION_LOST_TX",
        f"the connection was lost in the transaction of run {run_id} ({where}). The engine rolled the "
        "unit of work back, or committed it when COMMIT reached the server; the tool cannot tell. "
        "Wait, run azsqlcd plan, then start again or run azsqlcd resolve",
        run_id=run_id,
    )


def _tx_failure(run: _Run, error: Exception) -> ToolError:
    """The end of a unit of work in a transaction that did not reach COMMIT (failure matrix)."""
    sql = error if isinstance(error, SqlError) else None
    if isinstance(error, ToolError) and error.exit_code is Exit.UNKNOWN:
        outcome = error  # the guard: nothing more is sent, only the status of the run
        sql = error.__cause__ if isinstance(error.__cause__, SqlError) else None
    elif not isinstance(error, (SqlError, ToolError)):
        outcome = _defect(run, error)
    elif sql is not None and _lost(run, sql):
        return _connection_lost_tx(run, sql)
    else:
        # the connection is alive: roll back, prove it, and read the fence (A5). An error of any
        # class on this path, and an answer of another shape, leave the rollback not proven
        try:
            run.db.execute(_ROLLBACK)
            trancount = _one_row(run.db, _TRANCOUNT, 1)
            fence = _one_row(run.db, state.read_fence(run.id), 1)
        except SqlError as second:
            if _lost(run, second):
                return _connection_lost_tx(run, second)
            trancount = fence = None
        if trancount == (0,) and fence == (run.committed,):
            outcome = _classified(run, error)
            # a ToolError that reads an engine error (a refresh that failed): the run row holds it
            cause = error.__cause__ if isinstance(error.__cause__, SqlError) else None
            _end_run(run, "failed", outcome, sql or cause)
            return outcome
        found = fence[0] if fence else None
        proven = "FENCE_MISMATCH" if trancount == (0,) else "ROLLBACK_UNVERIFIED"
        outcome = unknown(
            proven,
            f"step {run.step} failed and the rollback is not proven: @@TRANCOUNT is "
            f"{trancount[0] if trancount else None!r} and "
            f"run.segments_committed is {found!r}; the tool saw {run.committed} committed unit(s) of "
            f"work. What was applied is not known. {_INSPECT} --clear-run {run.run_id}",
            step=run.step,
            segments_committed=found,
            known=run.committed,
        )
    # an error of the tool before the first batch of the unit is a refusal (A1): nothing ran
    _end_run(run, "unknown" if outcome.exit_code is Exit.UNKNOWN else "failed", outcome, sql)
    return outcome


def _nontx_failure(run: _Run, error: Exception) -> ToolError:
    """Any error after the marker of a non-transactional step: the outcome is unknown (failure matrix)."""
    migration = run.nontx_started
    sql = error if isinstance(error, SqlError) else None
    marks = f"{_INSPECT} --mark-applied {migration} or --mark-not-applied {migration}"
    if sql is not None and _lost(run, sql):
        # no write reaches the database: the step stays `started` and the run row `running`.
        # --clear-run takes only a run with the status unknown, so the text must not name it here
        resolve = (
            f"{marks}. The run row still says running, because the session is gone: the next deploy "
            "closes it. If that deploy comes first, it sets the step and the run to unknown and names "
            "the actions to take"
        )
    else:
        resolve = f"{marks}, then --clear-run {run.run_id}"
    if isinstance(error, ToolError):
        outcome = ToolError(
            Exit.UNKNOWN, error.reason_code, f"{error.message}. {resolve}", detail=error.detail
        )
    elif sql is None:
        outcome = _defect(run, error)
    else:
        detail = {"step": run.step, "error_class": sql.cls.name, "error_number": sql.number}
        if _lost(run, sql):
            reason, what = "CONNECTION_LOST_NONTX", "the connection was lost in"
        elif sql.cls is ErrorClass.GOVERNANCE:
            reason, what = "GOVERNANCE_LIMIT", "a limit of the service stopped"
        else:
            reason, what = "NONTX_FAILED", "an error stopped"
        outcome = unknown(
            reason,
            f"{what} the non-transactional migration {migration} at step {run.step}: {sql}. A part of "
            f"it can be applied; nothing is retried. {resolve}",
            **detail,
        )
    _end_run(run, "unknown", outcome, sql)
    return outcome


def reconcile_by_locking_read(
    session_factory: SessionFactory,
    run_id: int,
    committed: int,
    *,
    lock_timeout_ms: int,
    applock_wait_s: int,
) -> ToolError:
    """After a lost connection in a transaction: read the fence of the run on a new session.

    committed = the units of work whose COMMIT the lost session saw. The new session takes the
    deploy lock (the lost session must be gone for that) and reads run.segments_committed with a
    locking read. A higher value: the unit was committed. The same value: it was rolled back.
    Then the run row is `failed` and the result is exit 24 (CONNECTION_LOST_COMMITTED or
    CONNECTION_LOST_ROLLED_BACK). Anything else is exit 23 CONNECTION_LOST_TX. Returns the error
    to raise. Used by deploy only when RECONCILE_BY_LOCKING_READ is True (live spike L6).
    """
    lost = _lost_tx(run_id, "the outcome could not be read on a new session")
    try:
        session = session_factory()
    except (SqlError, ToolError):
        return lost
    try:
        set_session_options(session, lock_timeout_ms)
        (result,) = catalog.one_row(session, _lock_sql(applock_wait_s))
        found = state.rows(session, state.read_fence_locking(run_id)) if result in (0, 1) else []
        if len(found) != 1 or type(found[0][0]) is not int or found[0][0] < committed:
            return lost
        was_committed = found[0][0] > committed
        outcome = "committed" if was_committed else "rolled back"
        note = f"connection lost; the unit of work was {outcome}"
        session.execute(state.set_run_status(run_id, "failed", note=note))
        with contextlib.suppress(SqlError):
            session.execute(_RELEASE_LOCK)
    except (SqlError, ToolError):
        return lost
    finally:
        session.close()
    return retry_safe(
        "CONNECTION_LOST_COMMITTED" if was_committed else "CONNECTION_LOST_ROLLED_BACK",
        f"the connection was lost in the transaction of run {run_id}; a new session read that the unit "
        f"of work was {outcome}. Start again: the next deploy continues from the recorded state",
        run_id=run_id,
        committed=was_committed,
    )


# ------------------------------------------------------------------ the unit of work
def _drop_sql(key: str) -> str:
    kind, schema, name = names.parse_object_key(key)
    if schema is None or kind not in names.MODULE_KINDS:
        raise ValueError(f"not a module key: {key!r}")
    return f"DROP {kind} {names.qualified(schema, name)};"


def _refresh_sql(key: str) -> str:
    kind, schema, name = names.parse_object_key(key)
    if schema is None or kind not in names.MODULE_KINDS:
        raise ValueError(f"not a module key: {key!r}")
    return f"EXEC sys.sp_refreshsqlmodule @name = {state.literal(names.qualified(schema, name))};"


def _refuse_indexed_view(run: _Run, key: str) -> None:
    """Before an ALTER of a view (a module step of a view that exists, an unbind): ALTER VIEW drops
    every index of the view, and nothing that the tool reads back would show the loss. The plan
    refuses an indexed view that the release alters; this is the same rule for an index that a
    batch of this unit of work created after the plan."""
    if names.parse_object_key(key)[0] == "VIEW" and catalog.has_index(run.db, key):
        raise failed(
            "INDEXED_VIEW",
            f"{key} has an index, and the next step alters the view. ALTER VIEW drops every index of a "
            "view, so the step was not sent",
            object=key,
        )


def _unbind(run: _Run, step: Step, sent: _Sent) -> None:
    """A15: ALTER the module with its live definition, without SCHEMABINDING; forget its source."""
    key = _have(step.object_key)
    _, schema, name = names.parse_object_key(key)
    managed = _managed(_have(run.recorded))
    stored = _stored_keys(managed).get(fold(key), key)
    row = managed.get(stored)
    live = catalog.capture_modules(run.db, [key]).get(key)
    definition = live.get("definition") if live else None
    if row is None or live is None or schema is None or not isinstance(definition, str):
        raise failed(
            "UNBIND_HEADER",
            f"{key} cannot be unbound: it has no managed row, or its definition cannot be read",
            object=key,
        )
    _refuse_indexed_view(run, key)
    # a module that this unit sent before holds the text of its file, not the recorded capture
    differences = [] if key in sent.modules else catalog.capture_differences(row.capture, live)
    if differences:
        raise failed(
            "DRIFT_TOUCHED",
            f"{key} differs from what the tool recorded "
            f"({', '.join(difference.property for difference in differences)}); it is not unbound",
            objects=[{"object": key, "differences": [dataclasses.asdict(d) for d in differences]}],
        )
    _send(run, step.id, modules.rewrite_for_unbind(definition, schema, name))
    run.db.execute(state.set_source_null(stored))
    sent.unbound.add(key)
    sent.modules.pop(key, None)


def _refresh(run: _Run, step: Step) -> None:
    """Design (d) 4: sp_refreshsqlmodule of a managed dependant that the release does not change.

    N1-F3: an engine error here says that the module does not bind after the change (207 for a
    column that is gone, 208 for an object; measured live). It is the finding of A12 for a view,
    which is refreshed before the sweep of dependants reads it: DEPENDANT_BROKEN, with the module
    named. A lock timeout, a deadlock, a limit of the service and a lost session say nothing
    about the module and keep their own ends.
    """
    key = _have(step.object_key)
    try:
        _send(run, step.id, _refresh_sql(key))
    except SqlError as error:
        if _lost(run, error) or error.cls is not ErrorClass.OTHER:
            raise
        finding = f"ERROR_{error.number}" if error.number is not None else "REFRESH_FAILED"
        raise failed(
            "DEPENDANT_BROKEN",
            f"{key} does not bind after the change of a table it uses: its refresh failed "
            f"({finding}: {error}). {_FIX_DEPENDANT}",
            object=key,
            findings=[finding],
            step=step.id,
        ) from error


def _send_steps(run: _Run, unit: Unit, sent: _Sent) -> None:
    """Design (d): the steps of the unit in the order of the plan, the guard after each one."""
    job = run.job
    texts = plan.step_texts(job.bundle, unit)
    for step in unit.steps:
        if step.kind == "readback":
            continue
        run.step = step.id
        if step.kind == "batch":
            sent.options_asserted = False  # a batch can hold a SET statement
            _send(run, step.id, texts[step.id])
            sent.migrations[_have(step.migration)] = step.sha256
        elif step.kind == "deploy_module":
            # CREATE OR ALTER stores the module with the ANSI_NULLS and QUOTED_IDENTIFIER of the
            # session. The plan proved, under this lock, that the recorded flags are ON.
            if not sent.options_asserted:
                _recheck_session_options(run)
                sent.options_asserted = True
            path = _have(step.path)
            _refuse_indexed_view(run, _have(step.object_key))
            _send(run, step.id, texts[step.id])
            sent.modules[_have(step.object_key)] = modules.read_module(path, job.bundle.files[path])
        elif step.kind == "unbind":
            _unbind(run, step, sent)
        elif step.kind == "drop_module":
            key = _have(step.object_key)
            if catalog.object_exists(run.db, key):  # A18: gone already = no statement
                _send(run, step.id, _drop_sql(key))
            sent.dropped.append(key)
        elif step.kind == "refresh":
            _refresh(run, step)
        else:
            raise RuntimeError(f"step {step.id} has the kind {step.kind!r}, which the runner does not know")
        _guard(run)
        sent.steps.append(step.id)
    if not sent.options_asserted:
        # the last batch of the unit ran with no module step after it: the state rows of the unit,
        # and after COMMIT the run row, are written with the options that are read here
        run.step = "session_options"
        _recheck_session_options(run)
        sent.options_asserted = True


# RP2-1: a finding of the runner, not of the engine. The dependant had a finding before the change
# that names no column, so the engine cannot show that a column it reads is gone now.
COLUMN_GONE = "COLUMN_GONE"
_FIX_COLUMN_GONE = (
    "COLUMN_GONE: the engine cannot verify this module (before the change it did not bind, or it "
    "uses the table together with a #temp table), and its text names a column that the change drops "
    "or renames. If the name is a column of another table, change the module file in the same "
    "release (a comment is enough) to send the module again: this text check is only for a "
    "module that the release does not send. "
)


def _sweep_dependants(run: _Run, sent: _Sent) -> None:
    """A12: no managed dependant of a changed table may bind worse than before the change.

    The dependants are the list of the plan, which was read before the change: after DROP TABLE
    or a rename of the table the catalog names no dependant of the old name. The check is
    differential. On a sound database sys.dm_sql_referenced_entities reports findings for sound
    modules (a procedure with a #temp table; measured on Azure SQL Database), so a dependant fails
    the run only for a finding that the plan did not read before the change
    (Plan.dependant_findings). Its old findings are ignored; the dependant is still checked.

    Not checked: a module that this unit of work dropped. A dependant on which the engine raised
    error 2020, 207 or 208 before the change (catalog.NOT_BOUND_FINDINGS) is read again: the same
    number is no new finding, another number is. None of the three errors ends the open
    transaction, also with XACT_ABORT ON (live spike: the guard stays (1, 1)); the rollback of a
    run that fails here is the one that the tool sends and proves.

    RP2-1: an old finding that names no column (one of the three errors, or COLUMNS_NOT_FOUND of a
    table) is the same text after a column of the table is dropped or renamed. For such a
    dependant the runner compares the columns of the touched tables before and after the batches
    and scans the text of the module for the name of a column that is gone: COLUMN_GONE [s].[t].[c].

    RP2-2: a dependant of the plan that this unit sent has a new text. The findings of its old
    text say nothing of it, so it is checked by the rule for sent modules below.

    The list of the plan cannot hold what this unit of work made: a module that it created, or a
    changed text that uses the table for the first time. So the modules that the unit sent are
    checked too, when the catalog names them as dependants of a touched table now, or when their
    file names such a table (the catalog has no row for a table that is gone). No finding of
    before exists for them, and the engine bound their text when it was sent. So only two findings
    count: an error that says the module does not bind (2020, or 207 or 208, which the engine
    raises first for a column or an object that is gone), and a touched table that does not resolve
    (a procedure is created with a name that does not resolve; the engine does not refuse it).
    """
    computed = _have(run.plan)
    before = {fold(key): set(found) for key, found in computed.dependant_findings.items()}
    planned = {fold(key) for key in computed.dependants}
    sent_now = {fold(key) for key in sent.modules}
    tables = [touched.key for touched in computed.touched if touched.key.startswith("TABLE:")]
    made: list[str] = []
    if tables and sent.modules:
        live = {fold(dependant.key) for dependant in catalog.dependants_of(run.db, tables)}
        made = sorted(
            key
            for key, module in sent.modules.items()
            if fold(key) not in planned and (fold(key) in live or modules.scan_references(module, tables))
        )
    gone = {fold(f"UNRESOLVED {key.split(':', 1)[1]}") for key in tables}
    for key in tables:  # a one-part name is an object of dbo (A17): the engine row has no schema
        _, schema, name = names.parse_object_key(key)
        if schema is not None and fold(schema) == "dbo":
            gone.add(fold(f"UNRESOLVED {names.quote(name)}"))
    dropped = {fold(key) for key in sent.dropped}
    removed: set[tuple[str, str]] | None = None
    for key in (*computed.dependants, *made):
        if fold(key) in dropped:
            continue
        found = catalog.broken_references(run.db, key)
        if fold(key) in planned and fold(key) not in sent_now:
            old = before.get(fold(key), set())  # a dependant of the plan with no list has no old finding
            if _unverifiable(old) and sent.columns:
                if removed is None:
                    removed = _removed_columns(run, sent)
                found = [*found, *_columns_gone(run, key, removed)]
            new = [finding for finding in found if finding not in old]
        else:
            new = [f for f in found if f in catalog.NOT_BOUND_FINDINGS or fold(f) in gone]
        if new:
            hint = _FIX_COLUMN_GONE if any(f.startswith(COLUMN_GONE) for f in new) else ""
            raise failed(
                "DEPENDANT_BROKEN",
                f"{key} does not bind after the change of a table it uses: {', '.join(new)}. "
                f"{hint}{_FIX_DEPENDANT}",
                object=key,
                findings=new,
            )


def _unverifiable(old: Collection[str]) -> bool:
    """The engine names no column for this dependant: it did not bind before the change, or the
    engine gave is_all_columns_found = 0 for a table it uses (a #temp table in the statement)."""
    return any(f in catalog.NOT_BOUND_FINDINGS or f.startswith("COLUMNS_NOT_FOUND ") for f in old)


def _columns_before(run: _Run) -> dict[str, frozenset[str]]:
    """The columns of the touched tables before the batches of a unit, read only when the plan
    holds a dependant that the engine cannot verify (RP2-1). One read; no read for other plans."""
    computed = _have(run.plan)
    tables = [touched.key for touched in computed.touched if touched.key.startswith("TABLE:")]
    if not tables or not any(_unverifiable(found) for found in computed.dependant_findings.values()):
        return {}
    return catalog.table_columns(run.db, tables)


def _removed_columns(run: _Run, sent: _Sent) -> set[tuple[str, str]]:
    """(table key, column) of each column that the unit dropped or renamed; all of a table that is gone."""
    after = catalog.table_columns(run.db, list(sent.columns))
    removed: set[tuple[str, str]] = set()
    for table, columns in sent.columns.items():
        left = {fold(column) for column in after.get(table, ())}
        removed |= {(table, column) for column in columns if fold(column) not in left}
    return removed


def _columns_gone(run: _Run, key: str, removed: set[tuple[str, str]]) -> list[str]:
    """COLUMN_GONE [s].[t].[c] for each removed column whose name is an identifier in the text of
    the module. A token scan of the definition in the catalog, not a parse: strings and comments
    do not count, and a column of the same name in another table does (the message says the way
    through). An encrypted module has no text: nothing is found for it."""
    if not removed:
        return []
    definition = catalog.capture_modules(run.db, [key]).get(key, {}).get("definition")
    if not definition:
        return []
    try:
        named = {
            fold(token.value)
            for token in lex.significant(lex.tokens(definition))
            if token.kind in ("word", "bident", "qident")
        }
    except lex.LexError:
        text = fold(definition)
        named = {fold(column) for _, column in removed if fold(column) in text}
    return sorted(
        f"{COLUMN_GONE} {table.split(':', 1)[1]}.{names.quote(column)}"
        for table, column in removed
        if fold(column) in named
    )


def _snapshot(run: _Run, hooks: TableHooks | None) -> _Snapshot:
    """Every managed object as the catalog holds it now: module captures, and the drift of the rest."""
    recorded = _have(run.recorded)
    managed = _managed(recorded)
    live = catalog.capture_modules(run.db, [key for key in managed if _is_module(key)])
    other = sorted(key for key in managed if not _is_module(key))
    drift = hooks.table_drift(run.db, recorded, other) if hooks is not None and other else {}
    return live, drift


def _snapshot_for(run: _Run, unit: Unit, hooks: TableHooks | None) -> _Snapshot | None:
    """The state before a unit with a data or raw batch: such a batch can change any object."""
    if any(step.kind == "readback" and step.all_managed for step in unit.steps):
        return _snapshot(run, hooks)
    return None


def _refuse_untouched_changes(
    run: _Run, hooks: TableHooks | None, before: _Snapshot, touched: Collection[str]
) -> None:
    after = _snapshot(run, hooks)
    changed: list[dict[str, Any]] = []
    touched_folded = {fold(key) for key in touched}
    for key in sorted(_managed(_have(run.recorded))):
        if fold(key) in touched_folded:
            continue
        was, now = before[0].get(key), after[0].get(key)
        if was != now:
            properties = (
                [d.property for d in catalog.capture_differences(was, now)] if was and now else ["exists"]
            )
            changed.append({"object": key, "properties": properties})
        elif before[1].get(key) != after[1].get(key):
            changed.append({"object": key, "properties": [d.property for d in after[1].get(key, [])]})
    if changed:
        raise failed(
            "UNTOUCHED_CHANGED",
            f"{changed[0]['object']} is managed and the release does not touch it, and a data or raw "
            f"batch of this unit of work changed it ({len(changed)} object(s) in all)",
            objects=changed,
        )


def _module_problems(module: ModuleFile, capture: dict[str, Any] | None) -> list[str]:
    """Design (e) 8 e. Names of properties only; never definition text (A25)."""
    if capture is None:
        return ["exists"]
    problems = [
        name
        for name, wanted in (
            ("kind", module.kind),
            ("uses_ansi_nulls", True),
            ("uses_quoted_identifier", True),
            ("is_schema_bound", module.schema_bound),
        )
        if capture.get(name) != wanted
    ]
    # the engine does not keep the verb as it was sent: CREATE OR ALTER is stored as CREATE
    definition = capture.get("definition")
    if MODULE_TEXT_READBACK and isinstance(definition, str):
        if modules.checksum(definition.encode("utf-8")) != modules.stored_checksum(module.text):
            problems.append("definition")
    return problems


def _read_back(
    run: _Run, step: Step, hooks: TableHooks | None, sent: _Sent, before: _Snapshot | None
) -> Captures:
    """Read what the unit made, in its transaction. Returns the captures to record."""
    module_keys = [key for key in step.keys if _is_module(key)]
    table_keys = [key for key in step.keys if not _is_module(key)]
    live = catalog.capture_modules(run.db, module_keys)
    problems: list[dict[str, Any]] = []
    for key in module_keys:
        if key not in sent.modules:
            raise RuntimeError(f"the plan reads back {key}, and this unit of work did not send it")
        found = _module_problems(sent.modules[key], live.get(key))
        if found:
            problems.append({"object": key, "properties": found})
    if problems:
        first = problems[0]
        raise failed(
            "READBACK_MISMATCH",
            f"{first['object']} is not in the database as its file says "
            f"({', '.join(first['properties'])}; {len(problems)} object(s) in all)",
            objects=problems,
        )
    captures: Captures = {key: live[key] for key in module_keys}
    if table_keys:
        if hooks is None:
            raise RuntimeError("the plan reads back table-class objects, and this run has no table model")
        captures |= hooks.read_back(run.db, run.job.bundle, table_keys, None)
    if before is not None:
        _refuse_untouched_changes(run, hooks, before, {*step.keys, *sent.dropped, *sent.unbound})
    return captures


def _object_writes(recorded: State, captures: Captures, sources: dict[str, str], run_id: int) -> list[str]:
    stored = _stored_keys(recorded.objects)
    writes: list[str] = []
    for key, capture in captures.items():
        row_key = stored.get(fold(key))
        if capture is not None:
            writes.append(
                state.upsert_object(
                    row_key or key, run_id=run_id, capture=capture, source_sha256=sources.get(key)
                )
            )
        elif row_key is not None:
            writes.append(state.mark_dropped(row_key, run_id))
    return writes


def _finish(
    run: _Run, unit: Unit, hooks: TableHooks | None, sent: _Sent, before: _Snapshot | None, nontx: bool
) -> None:
    """Design (e) 8 d to f after the batches: dependants, read-back, object rows, step rows, guard."""
    run_id = run.id
    run.step = "dependants"
    _sweep_dependants(run, sent)
    captures: Captures = {}
    readback = next((step for step in unit.steps if step.kind == "readback"), None)
    if readback is not None:
        run.step = readback.id
        captures = _read_back(run, readback, hooks, sent, before)
    run.step = "state"
    sources = {key: module.checksum for key, module in sent.modules.items()}
    recorded = _have(run.recorded)
    stored = _stored_keys(recorded.objects)
    writes = _object_writes(recorded, captures, sources, run_id)
    writes += [state.mark_dropped(stored.get(fold(key), key), run_id) for key in sent.dropped]
    for migration, sha256 in sent.migrations.items():
        writes.append(
            state.set_step_status(migration, "ok")
            if nontx
            else state.insert_step(
                run_id=run_id, kind="migration", status="ok", migration_id=migration, file_sha256=sha256
            )
        )
    if sent.modules or sent.dropped:
        note = f"deployed {len(sent.modules)}, dropped {len(sent.dropped)}"
        writes.append(state.insert_step(run_id=run_id, kind="modules", status="ok", note=note))
    for batch in writes:
        run.db.execute(batch)
    _guard(run)


def _applied(run: _Run, sent: _Sent) -> None:
    run.steps_applied += sent.steps
    run.modules_deployed += list(sent.modules)
    run.modules_dropped += sent.dropped


def _tx_unit(run: _Run, unit: Unit, hooks: TableHooks | None) -> None:
    """One transaction: batches, modules, drops, refresh, read-back, state rows, COMMIT (A8, A9)."""
    sent = _Sent()
    try:
        _begin(run)
        before = _snapshot_for(run, unit, hooks)
        sent.columns = _columns_before(run)
        _send_steps(run, unit, sent)
        _finish(run, unit, hooks, sent, before, nontx=False)
        _commit(run)
    except Exception as error:
        raise _tx_failure(run, error) from error
    _applied(run, sent)


def _nontx_unit(run: _Run, unit: Unit, hooks: TableHooks | None) -> None:
    """One batch outside a transaction, between a committed marker and a closing transaction."""
    step = unit.steps[0]
    if step.kind != "batch" or any(other.kind != "readback" for other in unit.steps[1:]):
        raise RuntimeError("a nontx unit of work is one batch")
    migration = _have(step.migration)
    text = plan.step_texts(run.job.bundle, unit)[step.id]
    before = _snapshot_for(run, unit, hooks)
    run.step = step.id
    # a step row exists when resolve marked the migration not applied; a new row would be a duplicate
    recorded = any(applied.id == migration for applied in _have(run.plan).applied)
    marker = (
        state.set_step_status(migration, "started", run_id=run.id)
        if recorded
        else state.insert_step(
            run_id=run.id, kind="nontx", status="started", migration_id=migration, file_sha256=step.sha256
        )
    )
    run.db.execute(f"{_assert_owner(run)} {marker}")  # autocommit: the marker is committed here
    # from here the step row says `started`: every end but the closing COMMIT is "unknown" (A1)
    run.nontx_started = migration
    run.dispatched = True
    sent = _Sent(migrations={migration: step.sha256}, steps=[step.id])
    try:
        _send(run, step.id, text)
        _no_transaction(run)
        _recheck_session_options(run)  # the batch can hold a SET statement; state writes follow
        _begin(run)
        _finish(run, unit, hooks, sent, before, nontx=True)
        _commit(run)
    except Exception as error:
        raise _nontx_failure(run, error) from error
    run.nontx_started = None
    _applied(run, sent)


# ------------------------------------------------------------------ deploy
def deploy(
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    *,
    expect_plan: Plan | None,
    inline_plan: bool,
    session_factory: SessionFactory,
    token_provider: TokenProvider,
    audit: Audit,
    tool_version: str,
    tool_digest: str,
    repo_url: str | None = None,
    table_hooks: TableHooks | None = None,
    now: Callable[[], float] = time.time,
) -> Report:
    """Apply one release to one target (design (e), deploy algorithm). Returns the Report of exit 0.

    Exactly one of expect_plan (the plan of the plan job; its plan_sha256 must be the hash of the
    plan computed under the lock) and inline_plan (A26; only where the environment is not gated).
    session_factory opens the session of the run. With inline_plan it is called a second time, for
    the session of the syntax check (SET PARSEONLY ON), which the planner closes before anything
    is executed. Every other end raises RunError, a ToolError that carries the Report:
      21 BATCH_FAILED, GOVERNANCE_LIMIT, READBACK_MISMATCH, UNTOUCHED_CHANGED, DEPENDANT_BROKEN
         (the sweep of dependants, and a refresh step that fails), DRIFT_TOUCHED, UNBIND_HEADER,
         INDEXED_VIEW, SESSION_OPTIONS (in a unit of work; rolled back and proven)
      22 INLINE_PLAN_GATED, STALE_PLAN, STEP_UNRESOLVED, SESSION_OPTIONS, FENCE_META_MISMATCH,
         SQL_ERROR, TOOL_DEFECT, and every refusal of plan.compute_plan (INDEXED_VIEW among them)
      23 GUARD_FAILED, CONNECTION_LOST_TX (the locking read on a new session gave no answer),
         CONNECTION_LOST_NONTX, NONTX_FAILED, GOVERNANCE_LIMIT (nontx), SESSION_OPTIONS (nontx),
         FENCE_MISMATCH, ROLLBACK_UNVERIFIED, TOOL_DEFECT_AFTER_DISPATCH
      24 LOCK_TIMEOUT, DEADLOCK, CONNECTION_LOST, RUN_NOT_CLOSED, CONNECTION_LOST_ROLLED_BACK and
         CONNECTION_LOST_COMMITTED (a session lost in the transaction, read on a new session), and
         TOKEN_UNAVAILABLE, TOKEN_TOO_SHORT, CONNECT_FAILED of the session module
      25 LOCK_NOT_GRANTED
    """
    if inline_plan == (expect_plan is not None):
        raise ValueError("deploy needs exactly one of expect_plan and inline_plan")
    job = _Job(
        "deploy",
        bundle,
        config,
        env,
        target_id,
        session_factory,
        token_provider,
        audit,
        tool_version,
        tool_digest,
        now,
    )
    run = _Run(job)

    def body() -> None:
        environment, _ = resolve_target(config, env, target_id)
        if inline_plan and environment.gated:
            raise refused(
                "INLINE_PLAN_GATED",
                f"[env.{env}] is gated: a deploy needs the plan that was approved (--expect-plan-file). "
                "--inline-plan is for environments without reviewers",
                environment=env,
            )
        _open(run)
        _lock(run, environment.applock_wait_s)
        run.recorded = _read_state(run)
        _reconcile(run, run.recorded)
        computed = run.plan = plan.compute_plan(
            bundle,
            config,
            env,
            target_id,
            run.db,
            tool_version=tool_version,
            tool_digest=tool_digest,
            repo_url=repo_url,
            # the plan job ran the syntax check for an expected plan; an inline plan has no plan job
            open_second_session=session_factory if inline_plan else None,
            token_minutes_left=run.token_minutes_left,
            table_hooks=table_hooks,
            lock_held=True,
        )
        if expect_plan is not None and expect_plan.plan_sha256 != computed.plan_sha256:
            raise refused(
                "STALE_PLAN",
                f"the plan that was computed under the lock has the hash {computed.plan_sha256}; the "
                f"expected plan has {expect_plan.plan_sha256}. The database, the release or the tool "
                "changed after the plan job. Nothing was executed; start again from the plan job",
                expected=expect_plan.plan_sha256,
                computed=computed.plan_sha256,
            )
        seq = computed.release_seq
        if computed.outcome == ALREADY_PAST:  # A6: no batch is sent, older text is never deployed
            run.reason_code = ALREADY_PAST.upper()
            committed = _have(run.recorded).committed_release_seq
            not_ok = (
                f", and a deploy of r{committed} committed work and did not end ok"
                if committed > computed.recorded_release_seq
                else ""
            )
            run.message = (
                f"the database is past release r{seq} (it records r{computed.recorded_release_seq}"
                f"{not_ok}); nothing was sent"
            )
            _unlock(run)
            return
        if computed.outcome == NOOP:
            run.message = f"release r{seq} is recorded and nothing is pending; nothing was sent"
            _unlock(run)
            return
        _insert_run(
            run,
            release_seq=seq,
            git_sha=computed.git_sha,
            manifest_sha256=computed.manifest_digest,
            plan_sha256=computed.plan_sha256,
            previous_git_sha=computed.recorded_git_sha,
        )
        for unit in computed.units:  # none for the outcome record (A6): the run row is the record
            if unit.kind == "nontx":
                _nontx_unit(run, unit, table_hooks)
            else:
                _tx_unit(run, unit, table_hooks)
        _close_run(run)
        run.message = (
            f"release r{seq} is recorded as run {run.run_id}: {len(run.steps_applied)} plan step(s) applied, "
            f"{len(run.modules_deployed)} module(s) deployed, {len(run.modules_dropped)} dropped"
        )

    return _command(run, body)


# ------------------------------------------------------------------ a run that changes only the state
@dataclass(frozen=True)
class StateChange:
    """What a state-only run writes. The `prepare` of state_run() decides it under the lock."""

    note: str  # the note of the run row: what the run records, and why
    step: str  # the name of the change, for failed_step
    summary: str  # the start of Report.message; the run id follows
    # run_id -> the state write statements of the one transaction, in order, the step row included
    writes: Callable[[int], list[str]]


def state_run(
    command: str,
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    prepare: Callable[[Session, State], StateChange],
    *,
    confirm_database: str,
    tool_version: str,
    tool_digest: str,
    session: Session | None = None,
    session_factory: SessionFactory | None = None,
    token_provider: TokenProvider | None = None,
    audit: Audit | None = None,
    precheck: Callable[[], None] | None = None,
    reconcile: bool = False,
    check_environment: bool = True,
    now: Callable[[], float] = time.time,
) -> Report:
    """The frame of a run that sends no object DDL and changes only schema azsqlcd.

    Every resolve action and the baseline run in it, so the lock, the run row, the commit fence
    and the guard (A1, A4, A5) have one implementation. command is the command of the run row:
    resolve or baseline. In this order:
      1. precheck(): what can be refused with no session;
      2. the session: the one given, or token life and session_factory (exactly one of the two
         ways); session options; the fence of the database;
      3. confirm_database must be the name of the database of the session (CONFIRM_MISMATCH);
      4. the deploy lock; the recorded state, of this project and, with check_environment, of
         this environment (FENCE_META_MISMATCH);
      5. with reconcile: every run row with the status running is a dead run and is closed (A4);
      6. prepare(session, recorded): reads and checks under the lock, changes nothing, and
         returns the StateChange;
      7. the run row. It copies the recorded release, so the run does not move it;
      8. one transaction: commit fence, the writes, guard, COMMIT; then the run row is closed.
    The session is closed at the end, also one that the caller gave. Returns the Report of exit 0.
    Every other end raises RunError: the refusals of the steps above (also LOCK_NOT_GRANTED, 25,
    and STEP_UNRESOLVED of reconcile), a ToolError of precheck or prepare, and the ends of a unit
    of work as deploy has them (21 rolled back and proven, 23 unknown, 24 start again).
    """
    if command not in ("resolve", "baseline"):
        raise ValueError(f"{command!r} is not the command of a state-only run")
    if session is None:
        if session_factory is None or token_provider is None:
            raise ValueError("state_run needs a session, or a session_factory with a token_provider")
    elif session_factory is not None or token_provider is not None:
        raise ValueError("state_run takes a session or a session_factory, not both")
    job = _Job(
        command,
        bundle,
        config,
        env,
        target_id,
        session_factory,
        token_provider,
        audit or Audit(),
        tool_version,
        tool_digest,
        now,
    )
    run = _Run(job, session=session)

    def body() -> None:
        environment, _ = resolve_target(config, env, target_id)
        if precheck is not None:
            precheck()
        facts = _open(run)
        if confirm_database != facts.db_name:
            raise refused(
                "CONFIRM_MISMATCH",
                f"--confirm-database names {confirm_database!r}; the session is in database "
                f"{facts.db_name!r}. Nothing was changed",
                db_name=facts.db_name,
                confirmed=confirm_database,
            )
        _lock(run, environment.applock_wait_s)
        recorded = run.recorded = _read_state(run, check_environment=check_environment)
        if reconcile:
            _reconcile(run, recorded)
        try:
            change = prepare(run.db, recorded)
        except ToolError as error:
            if error.exit_code is not Exit.FAILED_ROLLED_BACK:
                raise
            # a read-back that differs: here nothing was executed, so nothing was rolled back
            raise ToolError(Exit.REFUSED, error.reason_code, error.message, detail=error.detail) from None
        # the run must not move the recorded release: it copies the release of the last ok run
        _insert_run(
            run,
            release_seq=recorded.recorded_release_seq,
            git_sha=recorded.recorded_git_sha or _NO_COMMIT,
            manifest_sha256=release.digest(bundle.manifest),
            plan_sha256=_NO_DIGEST,
            note=change.note,
        )
        try:
            _begin(run)
            run.step = change.step
            for statement in change.writes(run.id):
                run.db.execute(statement)
            _guard(run)
            _commit(run)
        except Exception as error:
            raise _tx_failure(run, error) from error
        _close_run(run)
        run.message = f"{change.summary}: recorded as run {run.run_id}"

    return _command(run, body)


# ------------------------------------------------------------------ resolve (A16)
type _Prepare = Callable[[Session, State], Callable[[int], list[str]]]


def _not_applicable(message: str, **detail: Any) -> ToolError:
    return refused("RESOLVE_NOT_APPLICABLE", message, **detail)


def _resolve(
    job: _Job,
    action: str,
    subject: str,
    confirm_database: str,
    reason: str,
    prepare: _Prepare,
    *,
    step_note: str | None = None,
    check_environment: bool = True,
) -> Report:
    """A resolve action in the frame of state_run: its change of the state, and the resolve step.

    prepare checks the action against the recorded state and the catalog, under the lock, and
    returns the state writes for a run_id. It changes nothing.
    """

    def has_reason() -> None:
        if not reason.strip():
            raise refused("REASON_REQUIRED", "a resolve action needs a reason (--reason TEXT)")

    def change(session: Session, recorded: State) -> StateChange:
        writes = prepare(session, recorded)

        def with_step(run_id: int) -> list[str]:
            # The planner takes an unknown run for cleared when a later resolve step has exactly
            # the note plan.clear_run_note(run_id) (A5). Only clear_run gives step_note. The note
            # of every other action starts with the name of the action, so no reason text that an
            # operator types can be that note.
            note = step_note if step_note is not None else f"{action}: {reason}"
            step = state.insert_step(run_id=run_id, kind="resolve", status="ok", note=note)
            return [*writes(run_id), step]

        return StateChange(f"{action} {subject}: {reason}", action, f"resolve {action} {subject}", with_step)

    return state_run(
        job.command,
        job.bundle,
        job.config,
        job.env,
        job.target_id,
        change,
        confirm_database=confirm_database,
        tool_version=job.tool_version,
        tool_digest=job.tool_digest,
        session_factory=job.session_factory,
        token_provider=job.token_provider,
        audit=job.audit,
        precheck=has_reason,
        check_environment=check_environment,
        now=job.now,
    )


def _sub_object_rows(session: Session, target: tuple[str, str, str]) -> tuple[Any, Any]:
    """(rows of sys.indexes, rows of sys.index_resumable_operations) for the index that a nontx
    batch builds (_built_sub_object). The proof of mark-applied and of mark-not-applied."""
    schema, table, name = target
    table_id = catalog.object_id_sql(names.object_key("TABLE", schema, table))
    where = f"[object_id] = {table_id} AND [name] = {state.literal(name)}"
    present, resumable = catalog.one_row(
        session,
        "/* azsqlcd:sub_object */ SELECT "
        f"(SELECT COUNT(*) FROM sys.indexes WHERE {where}), "
        f"(SELECT COUNT(*) FROM sys.index_resumable_operations WHERE {where});",
    )
    return present, resumable


def _migration_step(recorded: State, migration: str) -> state.StepRow | None:
    return next((step for step in recorded.steps if step.migration_id == migration), None)


def _open_nontx_step(recorded: State, migration: str) -> bool:
    step = _migration_step(recorded, migration)
    return step is not None and step.kind == "nontx" and step.status in ("started", "unknown")


def mark_applied(
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    migration: str,
    *,
    confirm_database: str,
    reason: str,
    session_factory: SessionFactory,
    token_provider: TokenProvider,
    audit: Audit,
    tool_version: str,
    tool_digest: str,
    table_hooks: TableHooks | None = None,
    force_no_readback: bool = False,
    now: Callable[[], float] = time.time,
) -> Report:
    """resolve --mark-applied: record a migration as applied without sending it.

    For a nontx step with the status started or unknown: the step becomes ok. When its batch is
    the one statement of an index or key build, the catalog must show the index and no open
    resumable build of it (RESOLVE_NOT_APPLICABLE otherwise). For the next
    pending transactional migration of the bundle: a step row is written. Then table_hooks reads
    the tables of the migration back against the model after it and their captures are recorded;
    without table_hooks, or to skip the read-back, force_no_readback is required (READBACK_REQUIRED).
    """
    job = _Job(
        "resolve",
        bundle,
        config,
        env,
        target_id,
        session_factory,
        token_provider,
        audit,
        tool_version,
        tool_digest,
        now,
    )

    def prepare(session: Session, recorded: State) -> Callable[[int], list[str]]:
        if _open_nontx_step(recorded, migration):
            # N2-F2: where the catalog can prove the build, it must. The batch of an index or key
            # build names its index; an index that is not there, or a build of it that is still
            # open, is not "applied". Without this the release was recorded with no index and no
            # drift ever reported it (the capture of the table never held the index). Every other
            # nontx batch keeps the rule of A16: the operator inspected the database
            target = _built_sub_object(bundle, migration)
            if target is not None:
                present, resumable = _sub_object_rows(session, target)
                if (present, resumable) != (1, 0):
                    sub_object = f"{names.qualified(target[0], target[1])}.{names.quote(target[2])}"
                    advice = (
                        f"The build did not finish: run azsqlcd resolve --mark-not-applied {migration}; "
                        "the next deploy sends the migration again"
                        if (present, resumable) == (0, 0)
                        else "Finish or abort the build by hand, then run azsqlcd resolve again"
                    )
                    raise _not_applicable(
                        f"{migration} builds {sub_object}, and the catalog does not show it as built "
                        f"(index rows {present}, resumable rows {resumable}). {advice}",
                        migration=migration,
                        object=sub_object,
                        index_rows=present,
                        resumable_rows=resumable,
                    )
            return lambda run_id: [
                state.set_step_status(migration, "ok", note=f"marked applied by run {run_id}")
            ]
        work = plan.pending_work(bundle, recorded, config.project.module_chunk)
        first = work.pending[0] if work.pending else None
        if first is None or first.file != migration or first.mode != "tx":
            raise _not_applicable(
                f"{migration} is not a nontx step with the status started or unknown, and it is not the "
                "next pending transactional migration of this release",
                migration=migration,
                next_pending=first.file if first else None,
            )
        captures: Captures = {}
        if not force_no_readback:
            if table_hooks is None:
                raise refused(
                    "READBACK_REQUIRED",
                    f"this run has no table model, so the tool cannot prove that {migration} is applied. "
                    "Check the database by hand, then give --force-no-readback",
                    migration=migration,
                )
            only = dataclasses.replace(work, pending=(first,))
            keys = sorted(table_hooks.touched_table_objects(only))
            captures = table_hooks.read_back(session, bundle, keys, migration)
        sha256 = chain.file_sha256(bundle.files[f"migrations/{migration}"])  # pending_work checked the line
        note = plan.MARKED_APPLIED_NOTE + (", no read-back" if force_no_readback else "")
        return lambda run_id: [
            *_object_writes(recorded, captures, {}, run_id),
            state.insert_step(
                run_id=run_id,
                kind="migration",
                status="ok",
                migration_id=migration,
                file_sha256=sha256,
                note=note,
            ),
        ]

    return _resolve(job, "mark-applied", migration, confirm_database, reason, prepare)


def _built_sub_object(bundle: Bundle, migration: str) -> tuple[str, str, str] | None:
    """(schema, table, name) of the index that the one batch of a nontx migration builds, else None.

    Read with the lexer: CREATE ... INDEX name ON schema.table, and ALTER TABLE schema.table ADD
    CONSTRAINT name PRIMARY KEY | UNIQUE (the engine gives the index the name of the constraint).

    Only for a batch that is that one statement and nothing more: a model batch (not data, not
    raw) for which the classifier of lint proves one statement. A batch that builds an index and
    then drops or renames it leaves no index of that name when it ran to its end, so "the index is
    absent" would prove nothing.
    """
    data = bundle.files.get(f"migrations/{migration}")
    if data is None:
        return None
    try:
        parsed = chain.parse_migration(data.decode("utf-8"), migration)
        batch = parsed.batches[0]
        if len(parsed.batches) != 1 or batch.kind != "model":
            return None
        facts = lint.classify_batch(batch, parsed.mode)
        if any(finding.code == "MODEL_STATEMENT" for finding in facts.findings):
            return None  # not a statement of the model grammar, or a second statement follows
        tokens = lex.significant(lex.tokenize(batch.text))
    except (UnicodeDecodeError, lex.LexError, ToolError):
        return None
    words = [token.text.upper() if token.kind == "word" else "" for token in tokens]
    identifier = ("word", "bident", "qident")

    def named(*positions: int) -> tuple[str, ...] | None:
        found = [tokens[at] for at in positions if at < len(tokens)]
        if len(found) != len(positions) or any(token.kind not in identifier for token in found):
            return None
        return tuple(token.value for token in found)

    if words[:1] == ["CREATE"] and "INDEX" in words[1:5]:
        at = words.index("INDEX")
        target = named(at + 3, at + 5, at + 1)
        if target and words[at + 2 : at + 3] == ["ON"] and tokens[at + 4].text == ".":
            return target[0], target[1], target[2]
    if words[:2] == ["ALTER", "TABLE"] and words[5:7] == ["ADD", "CONSTRAINT"]:
        target = named(2, 4, 7)
        if target and tokens[3].text == "." and words[8:9] in (["PRIMARY"], ["UNIQUE"]):
            return target[0], target[1], target[2]
    return None


def mark_not_applied(
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    migration: str,
    *,
    confirm_database: str,
    reason: str,
    session_factory: SessionFactory,
    token_provider: TokenProvider,
    audit: Audit,
    tool_version: str,
    tool_digest: str,
    now: Callable[[], float] = time.time,
) -> Report:
    """resolve --mark-not-applied: a nontx step with the status started or unknown becomes not_applied.

    Only when the catalog proves it: the index that the migration builds is absent, and
    sys.index_resumable_operations holds no row for it (NOT_PROVEN_ABSENT otherwise, also for a
    batch whose target the tool cannot read, and for every batch that is not the one statement
    of an index build: a raw or data batch, or a batch with a second statement). The next deploy
    sends the migration again.
    """
    job = _Job(
        "resolve",
        bundle,
        config,
        env,
        target_id,
        session_factory,
        token_provider,
        audit,
        tool_version,
        tool_digest,
        now,
    )

    def prepare(session: Session, recorded: State) -> Callable[[int], list[str]]:
        if not _open_nontx_step(recorded, migration):
            raise _not_applicable(
                f"{migration} is not a nontx step with the status started or unknown", migration=migration
            )
        target = _built_sub_object(bundle, migration)
        if target is None:
            raise refused(
                "NOT_PROVEN_ABSENT",
                f"the tool cannot read which index {migration} builds (this release must hold the "
                "file, and its batch must be CREATE INDEX or ADD CONSTRAINT ... PRIMARY KEY | UNIQUE), "
                "so it cannot prove that nothing of it remains",
                migration=migration,
            )
        schema, table, name = target
        present, resumable = _sub_object_rows(session, target)
        if (present, resumable) != (0, 0):
            sub_object = f"{names.qualified(schema, table)}.{names.quote(name)}"
            raise refused(
                "NOT_PROVEN_ABSENT",
                f"{sub_object} exists, or sys.index_resumable_operations holds a build of it "
                f"(index rows {present}, resumable rows {resumable}). Finish or abort the build by hand; "
                "when the index exists use --mark-applied",
                object=sub_object,
                index_rows=present,
                resumable_rows=resumable,
            )
        return lambda run_id: [
            state.set_step_status(migration, "not_applied", note=f"marked not applied by run {run_id}")
        ]

    return _resolve(job, "mark-not-applied", migration, confirm_database, reason, prepare)


def accept_drift(
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    object_key: str,
    *,
    confirm_database: str,
    reason: str,
    session_factory: SessionFactory,
    token_provider: TokenProvider,
    audit: Audit,
    tool_version: str,
    tool_digest: str,
    table_hooks: TableHooks | None = None,
    now: Callable[[], float] = time.time,
) -> Report:
    """resolve --accept-drift: the capture of a managed object becomes what the catalog holds now.

    Module: source_sha256 becomes NULL, so the next deploy sends the file (OVERWRITE_MODULE).
    Table-class object: only with table_hooks, and only when the catalog equals the model of the
    release (TABLE_MODEL_REQUIRED, READBACK_MISMATCH).
    """
    job = _Job(
        "resolve",
        bundle,
        config,
        env,
        target_id,
        session_factory,
        token_provider,
        audit,
        tool_version,
        tool_digest,
        now,
    )

    def prepare(session: Session, recorded: State) -> Callable[[int], list[str]]:
        stored = _stored_keys(_managed(recorded)).get(fold(object_key))
        if stored is None:
            raise _not_applicable(f"{object_key} is not a managed object of this database", object=object_key)
        if _is_module(object_key):
            capture = catalog.capture_modules(session, [object_key]).get(object_key)
        elif table_hooks is None:
            raise refused(
                "TABLE_MODEL_REQUIRED",
                f"{object_key} is a table-class object: its drift can be accepted only with "
                "table_model = true, when the database equals the model of the release",
                object=object_key,
            )
        else:
            capture = table_hooks.read_back(session, bundle, [object_key], None).get(object_key)
        if capture is None:
            raise _not_applicable(
                f"{object_key} does not exist in the database. An object that is gone needs a tombstone "
                "or a migration, not an accepted drift",
                object=object_key,
            )
        return lambda run_id: [
            state.upsert_object(stored, run_id=run_id, capture=capture, source_sha256=None)
        ]

    return _resolve(job, "accept-drift", object_key, confirm_database, reason, prepare)


def adopt_module(
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    object_key: str,
    *,
    confirm_database: str,
    reason: str,
    session_factory: SessionFactory,
    token_provider: TokenProvider,
    audit: Audit,
    tool_version: str,
    tool_digest: str,
    now: Callable[[], float] = time.time,
) -> Report:
    """resolve --adopt-module: an unmanaged module becomes managed, with source_sha256 NULL.

    The key must name the module exactly as the catalog does. The next plan lists OVERWRITE_MODULE.
    """
    job = _Job(
        "resolve",
        bundle,
        config,
        env,
        target_id,
        session_factory,
        token_provider,
        audit,
        tool_version,
        tool_digest,
        now,
    )

    def prepare(session: Session, recorded: State) -> Callable[[int], list[str]]:
        try:
            kind, schema, name = names.parse_object_key(object_key)
        except ValueError:
            kind, schema, name = None, None, ""
        if kind not in names.MODULE_KINDS:
            raise _not_applicable(
                "--adopt-module needs a module key, for example PROCEDURE:[sales].[usp_x]", object=object_key
            )
        if fold(object_key) in _stored_keys(_managed(recorded)):
            raise _not_applicable(f"{object_key} is managed already", object=object_key)
        # the row is found by its exact key later, so the key must be the name that the catalog holds
        exact = any(
            (found.kind, found.schema, found.name) == (kind, schema, name)
            for found in catalog.list_user_objects(session)
        )
        capture = catalog.capture_modules(session, [object_key]).get(object_key) if exact else None
        if capture is None or capture.get("kind") != kind:
            raise _not_applicable(
                f"the database has no module with exactly the name and the kind of {object_key}",
                object=object_key,
            )
        return lambda run_id: [
            state.upsert_object(object_key, run_id=run_id, capture=capture, source_sha256=None)
        ]

    return _resolve(job, "adopt-module", object_key, confirm_database, reason, prepare)


def clear_run(
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    run_id: int,
    *,
    confirm_database: str,
    reason: str,
    session_factory: SessionFactory,
    token_provider: TokenProvider,
    audit: Audit,
    tool_version: str,
    tool_digest: str,
    now: Callable[[], float] = time.time,
) -> Report:
    """resolve --clear-run: a human inspected a run with the status unknown; plans can run again (A5).

    The run row keeps the status unknown. The resolve step carries plan.clear_run_note(run_id);
    the reason is in the note of the resolve run.
    """
    job = _Job(
        "resolve",
        bundle,
        config,
        env,
        target_id,
        session_factory,
        token_provider,
        audit,
        tool_version,
        tool_digest,
        now,
    )

    def prepare(session: Session, recorded: State) -> Callable[[int], list[str]]:
        if not any(found.run_id == run_id and found.status == "unknown" for found in recorded.open_runs):
            raise _not_applicable(f"run {run_id} is not a run with the status unknown", run_id=run_id)
        return lambda _: []

    return _resolve(
        job,
        "clear-run",
        str(run_id),
        confirm_database,
        reason,
        prepare,
        step_note=plan.clear_run_note(run_id),
    )


def rebind_environment(
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    *,
    confirm_database: str,
    reason: str,
    session_factory: SessionFactory,
    token_provider: TokenProvider,
    audit: Audit,
    tool_version: str,
    tool_digest: str,
    now: Callable[[], float] = time.time,
) -> Report:
    """resolve --rebind-environment: bind the database of the target to env, the environment of the target.

    For a database that was refreshed from another environment: azsqlcd.meta still names that one.
    The project must be the same. A database is bound to prod only on the server that
    azsqlcd.toml names for the prod target (REBIND_PROD_SERVER).
    """
    job = _Job(
        "resolve",
        bundle,
        config,
        env,
        target_id,
        session_factory,
        token_provider,
        audit,
        tool_version,
        tool_digest,
        now,
    )

    def prepare(session: Session, recorded: State) -> Callable[[int], list[str]]:
        if recorded.meta.environment == env:
            raise _not_applicable(f"this database is bound to {env} already", environment=env)
        if env == "prod":
            _, target = resolve_target(config, env, target_id)
            (server,) = catalog.one_row(session, _SERVER_NAME)
            if str(server).casefold() != target.server.split(".")[0].casefold():
                raise refused(
                    "REBIND_PROD_SERVER",
                    f"the session is on server {str(server)!r}; azsqlcd.toml names {target.server!r} for "
                    f"the prod target {target_id}. A database is bound to prod only on that server",
                    server=str(server),
                    configured=target.server,
                )
        return lambda _: [state.set_meta_environment(env)]

    return _resolve(
        job, "rebind-environment", env, confirm_database, reason, prepare, check_environment=False
    )
