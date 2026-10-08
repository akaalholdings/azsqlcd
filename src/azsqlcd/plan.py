"""The planner: release bundle x recorded state x live catalog -> units of work and plan_sha256.

pending_work() is pure. From the bundle and the recorded state it decides what a deploy does:
which migrations, which module files, which drops, in which order, in which units of work.

compute_plan() adds what only the database knows (fence, liveness, name collisions, drift, legacy
flags, the syntax check) and seals the result with plan_sha256. It changes nothing. On the main
session it sends SELECT batches only. On the second session it sends SET PARSEONLY ON, two canary
batches, and then the pending batches, which the engine parses and does not run.

The same function runs in the plan job and, under the lock, in the deploy job. A deploy runs only
when both give the same plan_sha256. Notes, facts for the approver and links are not in the hash.

A plan holds no SQL text. A step names a file of the bundle; step_texts() reads the text.
"""

from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from itertools import batched
from typing import Any, Protocol, Union, get_args, get_origin, get_type_hints
from urllib.parse import quote

from azsqlcd import catalog, catalog_tables, chain, lex, modules, names, release, state
from azsqlcd.catalog import Difference, FenceFacts
from azsqlcd.chain import Chain, ChainEntry, Migration
from azsqlcd.config import Config, resolve_target
from azsqlcd.errors import ToolError, locked, refused
from azsqlcd.model import fold
from azsqlcd.modules import ModuleFile
from azsqlcd.release import Bundle, Manifest
from azsqlcd.session import Session
from azsqlcd.sqlerrors import ErrorClass, SqlError
from azsqlcd.state import ObjectRow, State

PLAN_FORMAT = 1
# Plan.outcome and Work.outcome. Only WORK has units. RECORD writes a run row and nothing else (A6).
WORK, RECORD, NOOP, ALREADY_PAST = "work", "record", "noop", "already_past"
# Plan.syntax_check, for a plan with units. None: the plan has no unit, so no text to parse
SYNTAX_RAN, SYNTAX_SKIPPED = "ran", "skipped"

_MIGRATIONS_DIR = "migrations/"
_PARSEONLY_ON = "SET PARSEONLY ON;"
_PARSEONLY_OFF = "SET PARSEONLY OFF;"
_CANARY_MUST_NOT_RUN = "SELECT 1/0;"  # harmless when it does run: it then raises, or returns a row
_CANARY_MUST_NOT_PARSE = "SELECT FROM;"
# sent after SET PARSEONLY OFF: a session that still parses only gives no result set for it
_PARSEONLY_IS_OFF = "/* azsqlcd:parseonly_off */ SELECT @@SPID;"
_NOT_IN_DEPLOY = ", and the deploy of an approved plan does not run the check"


# ------------------------------------------------------------------ data
@dataclass(frozen=True)
class Step:
    """One action of a unit of work, in the order the runner takes it."""

    id: str  # unique in its unit; the runner reports a failure by this id
    kind: str  # batch | unbind | deploy_module | drop_module | refresh | readback
    migration: str | None = None  # batch, and an unbind or deploy_module that a directive asks for
    batch: int | None = None  # number of the batch in the migration file, from 1
    batch_kind: str | None = None  # batch: model | data | raw
    line: int | None = None  # line in the migration file: first line of the batch, or the directive
    object_key: str | None = None  # unbind, deploy_module, drop_module, refresh
    path: str | None = None  # file of the bundle: the migration, the module, or the tombstones
    sha256: str | None = None  # batch: sha256 of the chain line. deploy_module: checksum of the file
    keys: tuple[str, ...] = ()  # readback: the objects that this unit creates or changes
    all_managed: bool = False  # readback: every other managed object must still equal its capture
    url: str | None = None  # link to path at the release commit (A27); not in plan_sha256


@dataclass(frozen=True)
class Unit:
    """One unit of work: one transaction, or one non-transactional step."""

    kind: str  # tx | nontx | modules_chunk
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Destructive:
    code: str  # DROP_MODULE | OVERWRITE_MODULE | the code of an allow line
    object: str  # object key; for an allow line the object as written
    reason: str
    migration: str | None = None  # allow line: the migration that holds it
    line: int | None = None  # allow line: its line in the migration file


@dataclass(frozen=True)
class AppliedMigration:
    id: str
    sha256: str | None
    status: str  # ok | not_applied (started and unknown refuse the plan)


@dataclass(frozen=True)
class ModuleChange:
    key: str
    path: str
    checksum: str
    # create: no managed row. alter: the recorded checksum differs. overwrite: the recorded source
    # is NULL (baseline, adoption, accepted drift). rebind: unchanged, sent again after an unbind.
    action: str


@dataclass(frozen=True)
class Work:
    """What pending_work() decides. Table hooks read `pending` to find the tables a release touches."""

    outcome: str  # work | record | noop | already_past
    applied: tuple[AppliedMigration, ...]  # every migration step of the database, in step order
    pending: tuple[Migration, ...] = ()  # parsed, in effective chain order
    module_changes: tuple[ModuleChange, ...] = ()  # in the order they are sent
    drops: tuple[str, ...] = ()  # module keys, in drop order
    unbinds: tuple[str, ...] = ()  # module keys that an unbind directive names
    first_converge: bool = False  # A9: the units are chunks of modules
    units: tuple[Unit, ...] = ()
    destructive: tuple[Destructive, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Touched:
    key: str
    catalog_sha256: str | None  # hash of the recorded capture; None when the object has no managed row


@dataclass(frozen=True)
class Drift:
    key: str
    differences: tuple[Difference, ...]


@dataclass(frozen=True)
class TableFact:
    key: str
    rows: int
    reserved_pages: int


@dataclass(frozen=True)
class TableFindings:
    """Result of TableHooks.blockers_and_dependants (plan step 8)."""

    blockers: tuple[str, ...] = ()  # each one refuses the plan; text for the operator, names only
    # not read by the plan: Plan.pre_broken is the dependants with a finding in dependant_findings
    pre_broken: tuple[str, ...] = ()


class TableHooks(Protocol):
    """What table_model = true adds to a plan. Without hooks a plan does not model tables."""

    def touched_table_objects(self, work: Work) -> Collection[str]:
        """Keys of the table-class objects that the pending migrations create, alter or drop."""
        ...

    def table_drift(
        self, session: Session, state: State, keys: Collection[str]
    ) -> dict[str, list[Difference]]:
        """For each of the keys whose live catalog differs from its recorded capture: the differences."""
        ...

    def blockers_and_dependants(self, session: Session, work: Work, config: Config) -> TableFindings:
        """Blockers on changed tables and columns, unmanaged dependants that [ack] does not list."""
        ...

    def sub_object_collisions(self, session: Session, work: Work, state: State) -> list[str]:
        """Names of pending index and constraint creates that exist live and are not recorded (A20)."""
        ...

    def refresh_set(self, session: Session, work: Work) -> list[str]:
        """Keys of the managed, unchanged modules to refresh after the change (A12), in refresh order."""
        ...

    def unrecorded_objects(self, work: Work, state: State) -> list[str]:
        """Keys of the model of the release that have no managed row and that no pending migration
        creates (Part 2 (c) 8). The database was never compared with them."""
        ...

    def dependants_to_check(self, session: Session, work: Work) -> list[str]:
        """Keys of the managed modules that reference a table or a column that a pending operation
        alters, drops or renames, read before the change (A12). After DROP TABLE or a rename the
        catalog no longer names them, so the plan takes the list and the runner checks it."""
        ...

    def read_back(
        self, session: Session, bundle: Bundle, keys: Collection[str], through: str | None
    ) -> dict[str, dict[str, Any] | None]:
        """Compare the catalog with the model of the release for these table-class keys.

        through None: the model of the release. through = a migration id: the model after that
        migration. Returns for each key the capture to record; None when the object is gone and
        the model does not hold it. Raises ToolError FAILED READBACK_MISMATCH (object, property
        and hashes only) when the catalog differs from the model. The planner uses it only to
        name the action of a DRIFT_TOUCHED refusal; the runner records what it returns.
        """
        ...

    def dependant_findings(self, session: Session, keys: Collection[str]) -> dict[str, list[str]]:
        """For each of the keys (module keys): the sorted result of catalog.broken_references now,
        before the change (A12). On a sound database the engine reports findings for sound modules
        (a procedure with a #temp table, for one), so the runner fails a dependant only for a
        finding that is not in this list."""
        ...


@dataclass(frozen=True)
class Plan:
    """The plan of one release for one target. plan.json is to_json().

    In plan_sha256: tool_version, tool_digest, server, database, manifest_digest,
    recorded_release_seq, applied, touched, units (without the links), destructive, dependants,
    dependant_findings and pre_broken. Everything else is for the approver and can differ between
    the plan job and the deploy job.

    syntax_check is not in the hash, and cannot be. The plan job parses the texts; the deploy job
    computes the plan again under the lock and does not parse them a second time, and a deploy
    runs only when both plans have one hash. So the key is a fact of the plan job that the deploy
    job reads from plan.json. It decides a note of the report and nothing that is enforced; it is
    as good as the file, like the notes that held the warning before the key existed.
    """

    plan_sha256: str
    outcome: str  # work | record | noop | already_past
    tool_version: str
    tool_digest: str
    environment: str
    target_id: str
    server: str
    database: str
    manifest_digest: str
    release_seq: int
    git_sha: str  # commit of the release
    recorded_release_seq: int  # 0: no run ended ok
    recorded_git_sha: str | None
    compare_url: str | None  # recorded commit ... release commit (A27)
    applied: tuple[AppliedMigration, ...]
    touched: tuple[Touched, ...] = ()
    units: tuple[Unit, ...] = ()
    destructive: tuple[Destructive, ...] = ()
    drift: tuple[Drift, ...] = ()  # managed objects that the release does not touch and that differ
    unmanaged: tuple[str, ...] = ()  # live objects with no managed row
    # A12. dependants: the managed modules on a table that the release alters, drops or renames,
    # read before the change, in key order. dependant_findings: for each of them the findings of
    # catalog.broken_references before the change (codes and names only), sorted. The engine reports
    # findings for sound modules too, so the check of the runner is differential: after the change a
    # dependant fails the run only for a finding that is not in its list here. pre_broken: the
    # dependants with a list that is not empty, for the approver. All three decide what a deploy
    # enforces, so all three are in plan_sha256.
    dependants: tuple[str, ...] = ()
    dependant_findings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    pre_broken: tuple[str, ...] = ()
    table_facts: tuple[TableFact, ...] = ()
    service_objective: str | None = None
    token_minutes_left: int | None = None
    notes: tuple[str, ...] = ()
    # ran: the engine parsed every pending text of the units under SET PARSEONLY ON (in this run,
    # or in the plan job of the approved plan). skipped: it did not. None: the plan has no unit;
    # in a plan file also: the file is of a tool that did not write the key, and proves no check
    syntax_check: str | None = None

    @property
    def pending(self) -> bool:
        """A deploy of this plan writes to the database: units of work, or the run row of A6."""
        return self.outcome in (WORK, RECORD)

    def to_json(self) -> str:
        """The text of plan.json. Plan.from_json(plan.to_json()) == plan."""
        return json.dumps({"format": PLAN_FORMAT} | asdict(self), indent=2, ensure_ascii=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> Plan:
        """Read plan.json. Refused (PLAN_INVALID): another shape, or a hash that is not of the content."""
        try:
            doc = json.loads(text)
            plan_format = doc.pop("format", None) if isinstance(doc, dict) else None
            if type(plan_format) is not int or plan_format != PLAN_FORMAT:
                raise ValueError(f"not a plan of format {PLAN_FORMAT}")
            doc.setdefault("syntax_check", None)  # a file of a tool that did not write the key
            plan: Plan = _build(cls, doc)
            if plan.syntax_check not in (None, SYNTAX_RAN, SYNTAX_SKIPPED):
                raise ValueError(f"Plan.syntax_check must be {SYNTAX_RAN}, {SYNTAX_SKIPPED} or null")
        except (ValueError, RecursionError) as e:
            raise refused("PLAN_INVALID", f"the plan file cannot be read: {e}") from None
        if plan.plan_sha256 != _hash(plan):
            raise refused(
                "PLAN_INVALID", "the plan file was changed: plan_sha256 is not the hash of its content"
            )
        return plan


# ------------------------------------------------------------------ JSON and hash
def _build(cls: Any, doc: object) -> Any:
    """A dataclass from its JSON form. The keys are exactly the fields."""
    expected = {f.name for f in fields(cls)}
    if not isinstance(doc, dict) or set(doc) != expected:
        raise ValueError(f"{cls.__name__} needs exactly the keys {', '.join(sorted(expected))}")
    hints = get_type_hints(cls)
    return cls(**{name: _value(hints[name], doc[name], f"{cls.__name__}.{name}") for name in expected})


def _value(hint: Any, value: object, where: str) -> Any:
    origin = get_origin(hint)
    if origin is tuple:  # tuple[X, ...]
        if not isinstance(value, list):
            raise ValueError(f"{where} must be a list")
        return tuple(_value(get_args(hint)[0], item, where) for item in value)
    if origin is dict:  # dict[str, X]
        if not isinstance(value, dict) or not all(type(name) is str for name in value):
            raise ValueError(f"{where} must be an object")
        return {name: _value(get_args(hint)[1], item, where) for name, item in value.items()}
    if origin in (Union, types.UnionType):  # X | None
        if value is None:
            return None
        return _value(next(arg for arg in get_args(hint) if arg is not type(None)), value, where)
    if is_dataclass(hint):
        return _build(hint, value)
    if type(value) is not hint:  # str, int, bool; a bool is not an int here
        raise ValueError(f"{where} must be {hint.__name__}")
    return value


def _hash(plan: Plan) -> str:
    """plan_sha256: what must be the same in the plan job and under the lock in the deploy job."""
    identity = {
        # RP2-7: what a reader of plan.json sees of the release and of the target
        "outcome": plan.outcome,
        "environment": plan.environment,
        "target_id": plan.target_id,
        "release_seq": plan.release_seq,
        "git_sha": plan.git_sha,
        "tool_version": plan.tool_version,
        "tool_digest": plan.tool_digest,
        "server": plan.server,
        "database": plan.database,
        "manifest_digest": plan.manifest_digest,
        "recorded_release_seq": plan.recorded_release_seq,
        "applied": [asdict(migration) for migration in plan.applied],
        "touched": [asdict(touched) for touched in plan.touched],
        "units": [
            {
                "kind": unit.kind,
                "steps": [{k: v for k, v in asdict(step).items() if k != "url"} for step in unit.steps],
            }
            for unit in plan.units
        ],
        "destructive": [asdict(item) for item in plan.destructive],
        "dependants": sorted(plan.dependants),
        "dependant_findings": {key: sorted(found) for key, found in plan.dependant_findings.items()},
        "pre_broken": sorted(plan.pre_broken),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# ------------------------------------------------------------------ recorded state
MARKED_APPLIED_NOTE = "marked applied by resolve"  # start of the note of a step of resolve --mark-applied


def clear_run_note(run_id: int) -> str:
    """The note of the `resolve` step that clears an unknown run (A5).

    resolve --clear-run writes a step with kind resolve, status ok and exactly this note; the
    planner then stops refusing RUN_UNKNOWN for that run.
    """
    return f"clear-run {run_id}"


def _refuse_unresolved(recorded: State) -> None:
    open_steps = [step for step in recorded.steps if step.status in ("started", "unknown")]
    if open_steps:
        first = open_steps[0]
        raise refused(
            "STEP_UNRESOLVED",
            f"step {first.step_id} ({first.migration_id or first.kind}) has the status {first.status}: "
            "its outcome is not known. Inspect the database, then run azsqlcd resolve with "
            "--mark-applied or --mark-not-applied",
            steps=[
                {"step_id": step.step_id, "migration": step.migration_id, "status": step.status}
                for step in open_steps
            ],
        )
    for run in recorded.open_runs:
        note = clear_run_note(run.run_id)
        cleared = any(
            step.kind == "resolve" and step.status == "ok" and step.run_id > run.run_id and step.note == note
            for step in recorded.steps
        )
        if run.status == "unknown" and not cleared:
            raise refused(
                "RUN_UNKNOWN",
                f"run {run.run_id} has the status unknown and no later resolve --clear-run step. "
                f"Inspect the database, then run azsqlcd resolve --clear-run {run.run_id}",
                run_id=run.run_id,
            )


def _is_module(key: str) -> bool:
    return names.parse_object_key(key)[0] in names.MODULE_KINDS


def _managed_modules(recorded: State) -> dict[str, ObjectRow]:
    """An object is managed if and only if it has a row with the status managed (E5)."""
    return {key: row for key, row in recorded.objects.items() if row.status == "managed" and _is_module(key)}


# ------------------------------------------------------------------ chain position
def _diverged(problem: str, recorded_seq: int, seq: int, migration: str) -> ToolError:
    return refused(
        "CHAIN_DIVERGED",
        f"{problem}. The database records release r{recorded_seq}; this release is r{seq}. The "
        "database and this release do not have one history, so nothing was planned",
        migration=migration,
        recorded_release_seq=recorded_seq,
        release_seq=seq,
    )


def _pending_entries(
    ch: Chain, ok: dict[str, AppliedMigration], seq: int, recorded_seq: int, at_seq: int
) -> tuple[list[ChainEntry], list[str]] | None:
    """(the chain entries to apply, in effective order; notes). None: the database is past this
    release (A6).

    ok holds the applied migrations in the order they were applied. recorded_seq is the release
    of the last ok run. at_seq is the release that the database holds: recorded_seq, or the
    higher release of a deploy that committed a unit of work and whose run did not end ok.
    """
    by_file = {entry.file: entry for entry in ch.entries}
    for entry in ch.entries:
        step = ok.get(entry.file)
        if step is not None and step.sha256 != entry.sha256:
            problem = f"{entry.file} was applied with another checksum than this release holds"
            raise _diverged(problem, recorded_seq, seq, entry.file)

    def satisfied(entry: ChainEntry) -> bool:
        """Applied, or the replacement of a migration that is: the database has its change already."""
        while entry.file not in ok:
            if entry.replaces is None:
                return False
            entry = by_file[entry.replaces]
        return True

    order = chain.effective_order(ch)
    pending = [entry for entry in order if not entry.withdrawn and not satisfied(entry)]
    known = [file for file in ok if file in by_file]
    other = [file for file in ok if file not in by_file]
    # A6, "ahead": the database holds a newer release (recorded, or committed by a run that did
    # not end ok: its module text is in the database, and older text is never deployed over it);
    # or it holds this release and migrations that this release does not have
    if at_seq > seq or (other and at_seq == seq):
        if pending:
            problem = f"{pending[0].file} is not applied, and the database is past this release"
            raise _diverged(problem, recorded_seq, seq, pending[0].file)
        # the chain of this release is a prefix of the applied list: what it does not hold came later
        if set(list(ok)[: len(known)]) != set(known):
            problem = f"{other[0]} is not in this release and was applied before a migration that is"
            raise _diverged(problem, recorded_seq, seq, other[0])
        return None
    if other:
        raise _diverged(
            f"{other[0]} is applied and this release does not hold it", recorded_seq, seq, other[0]
        )
    position = {entry.file: index for index, entry in enumerate(order)}
    last_applied = max((position[file] for file in known), default=-1)
    # A pending entry before an applied one is a hole: the database took another way. One case is
    # not a hole. A replacement stands in the chain directly after the migration it replaces. When
    # it is merged after later migrations, a database that never applied the withdrawn migration
    # holds those later migrations already and lacks only the replacement. Such a database has one
    # history with the release; the replacement runs now, after the migrations that follow it in
    # the chain, and the plan says so. (An entry is pending only when no migration of its
    # `replaces` line is applied.)
    late = [entry for entry in pending if entry.replaces is not None and position[entry.file] < last_applied]
    hole = next(
        (entry for entry in pending if entry.replaces is None and position[entry.file] < last_applied), None
    )
    if hole is not None:
        problem = f"{hole.file} is not applied, and a later migration of the chain is"
        raise _diverged(problem, recorded_seq, seq, hole.file)
    notes = [
        f"late replacement: {entry.file} replaces the withdrawn {entry.replaces}, which this database "
        f"never applied. Its place in the chain is before "
        f"{', '.join(file for file in known if position[file] > position[entry.file])}, which this "
        "database applied already: here it runs after them, so the order differs from the chain order "
        "(for example the order of added columns)"
        for entry in late
    ]
    return pending, notes


def _refuse_catch_up(
    pending: Sequence[ChainEntry],
    withdrawn_unapplied: Sequence[ChainEntry],
    recorded_seq: int,
    manifest: Manifest,
    first_deploy: bool,
) -> list[str]:
    """A7: a release applies only the migrations that it added. Returns notes for the approver.

    first_deploy: the database has nothing to be caught up from (see pending_work). The whole
    chain then runs with this release, in one unit of work.

    withdrawn_unapplied: the withdrawn entries of the chain that have no ok step here. The
    release that added such a migration W, and each release up to its withdrawal, can no longer be
    promoted to this database: it would run W. So while the database records a release before
    W's, the pending migrations that W's release and the later releases added run with this
    release. An earlier release is still caught up first.

    The record decides, not the missing step, because W never gets a step here. A database that
    records W's release or a later one has no pending migration of those releases: it applied W,
    or it took them in the one catch-up. From then on the rule is whole again.
    """

    def added_in(entry: ChainEntry) -> int:
        release_seq = manifest.chain_added_in.get(entry.file)
        if release_seq is None or not 1 <= release_seq <= manifest.release_seq:
            raise refused(
                "BUNDLE_INVALID",
                f"the manifest does not name the release that added {entry.file} to the chain",
                migration=entry.file,
            )
        return release_seq

    added = {entry.file: added_in(entry) for entry in pending}
    carried = sorted(file for file, release_seq in added.items() if release_seq != manifest.release_seq)
    if first_deploy:
        if not carried:
            return []
        return [
            f"first deploy: this database records no release and no migration, so the whole chain runs "
            f"with r{manifest.release_seq} in one unit of work. {', '.join(carried)} came with earlier "
            f"releases (r{min(added[file] for file in carried)} and later)"
        ]
    never_applied = [entry for entry in withdrawn_unapplied if recorded_seq < added_in(entry)]
    first_withdrawn = min((added_in(entry) for entry in never_applied), default=None)
    behind = {
        file: release_seq
        for file, release_seq in added.items()
        if release_seq != manifest.release_seq and (first_withdrawn is None or release_seq < first_withdrawn)
    }
    if behind:
        first = min(behind.values())
        raise refused(
            "CATCHUP_REQUIRED",
            f"promote r{first} first: this database has pending migrations that earlier releases added, "
            f"and r{manifest.release_seq} applies only the migrations that it added. There are two "
            f"cases. (1) r{first} can still be deployed here: promote it, then each later release in "
            f"its turn. (2) r{first} cannot be deployed here, because its migration fails (21 "
            "BATCH_FAILED), or because a module fails after a correct migration (21 DEPENDANT_BROKEN, or "
            "21 BATCH_FAILED at a refresh step): withdraw the migration by pull request (the word "
            "withdrawn on its line of migrations/migrations.sum). The release that holds the withdrawal "
            "carries the pending migrations of the later releases in one catch-up, and the withdrawn "
            f"migration never runs here. A release that only changes modules cannot take the place of "
            f"r{first}. The corrected change is a replacement in the pull request of the withdrawal "
            "(allow REPLACEMENT_EDGE; with the fix of the module when a module was the cause): a "
            f"database that applied the migration of r{first} keeps what it has, and every other "
            "database runs the replacement. A withdrawal with no replacement, and the corrected change "
            "as a new migration in a later pull request, works only when no database applied the "
            f"migration of r{first} and table_model = true: a database that applied it refuses the new "
            "migration (21), and with table_model = false the withdrawal is refused (CHN006). Such a "
            'pull request puts the table files back. The steps: docs/setup.md, section "Withdraw and '
            'replace a merged migration"',
            promote=first,
            release_seq=manifest.release_seq,
            pending={file: added[file] for file in sorted(added)},
        )
    if not carried:
        return []
    withdrawn = ", ".join(entry.file for entry in never_applied)
    return [
        f"catch-up in one release: {', '.join(carried)} came with earlier releases and run with "
        f"r{manifest.release_seq}, because this database records r{recorded_seq} and never applied the "
        f"withdrawn {withdrawn}; the releases from r{first_withdrawn} to the withdrawal cannot be promoted "
        "here"
    ]


# ------------------------------------------------------------------ files of the bundle
def _utf8(files: Mapping[str, bytes], path: str, reason_code: str) -> str:
    try:
        return files[path].decode("utf-8")
    except UnicodeDecodeError:
        raise refused(reason_code, f"{path} is not UTF-8", path=path) from None


def _read_migration(files: Mapping[str, bytes], entry: ChainEntry) -> Migration:
    """The parsed file of a pending chain entry. The file must be the one that the chain line names."""
    path = _MIGRATIONS_DIR + entry.file
    data = files.get(path)
    if data is None or chain.file_sha256(data) != entry.sha256:
        problem = "is not in the release" if data is None else "does not have the sha256 of its chain line"
        raise refused("CHAIN_INVALID", f"{path} {problem}", migration=entry.file)
    migration = chain.parse_migration(_utf8(files, path, "MIGRATION_INVALID"), entry.file)
    if migration.mode != entry.mode:
        raise refused(
            "CHAIN_INVALID",
            f"{path} has the mode {migration.mode}; its chain line says {entry.mode}",
            migration=entry.file,
        )
    return migration


def _fold(schema: str, name: str) -> tuple[str, str]:
    """Names as a case-insensitive catalog compares them. The fence refuses a case-sensitive one."""
    return schema.casefold(), name.casefold()


def _fold_key(key: str) -> tuple[str, str]:
    _, schema, name = names.parse_object_key(key)
    return _fold(schema or "", name)


def module_files(files: Mapping[str, bytes]) -> dict[str, ModuleFile]:
    """key -> module file, for every module file among the files of a release (path -> bytes).

    Refused: MODULE_INVALID for a file that modules.read_module refuses, and for a second file
    that names the object of another file when case is ignored (one schema is one namespace for
    every kind of object).
    """
    found: dict[str, ModuleFile] = {}
    paths: dict[tuple[str, str], str] = {}
    for path in sorted(files):
        try:
            kind, _ = names.key_for_path(path)
        except ValueError:
            continue  # not an object file
        if kind not in names.MODULE_KINDS:
            continue
        module = modules.read_module(path, files[path])
        first = paths.setdefault(_fold(module.schema, module.name), path)
        if first != path:  # one schema is one namespace for every kind of object
            raise refused(
                "MODULE_INVALID",
                f"{path}: {first} names the same object; the database holds one object with a name",
                path=path,
            )
        found[module.key] = module
    return found


def step_texts(bundle: Bundle, unit: Unit) -> dict[str, str]:
    """step id -> the exact batch text, for every batch and deploy_module step of the unit."""
    texts: dict[str, str] = {}
    parsed: dict[str, Migration] = {}
    for step in unit.steps:
        path, migration, number = step.path, step.migration, step.batch
        if step.kind == "deploy_module" and path is not None:
            texts[step.id] = modules.read_module(path, bundle.files[path]).text
        elif step.kind == "batch" and path is not None and migration is not None and number is not None:
            if migration not in parsed:
                text = _utf8(bundle.files, path, "MIGRATION_INVALID")
                parsed[migration] = chain.parse_migration(text, migration)
            texts[step.id] = parsed[migration].batches[number - 1].text
        elif step.kind in ("batch", "deploy_module"):
            raise ValueError(f"step {step.id} does not name its text")
    return texts


# ------------------------------------------------------------------ pending work
def _module_changes(
    in_release: Mapping[str, ModuleFile], managed: Mapping[str, ObjectRow]
) -> dict[str, ModuleChange]:
    """The module files that the database does not hold: no managed row, NULL, or another checksum."""
    changes: dict[str, ModuleChange] = {}
    for key in sorted(in_release):
        module, row = in_release[key], managed.get(key)
        if row is None:
            action = "create"
        elif row.source_sha256 is None:
            action = "overwrite"
        elif row.source_sha256 != module.checksum:
            action = "alter"
        else:
            continue
        changes[key] = ModuleChange(key, module.path, module.checksum, action)
    return changes


def _directive_name(written: str) -> tuple[str, str]:
    schema, _, name = lex.significant(lex.tokenize(written))  # '[schema].[name]': parse_migration checked it
    return _fold(schema.value, name.value)


def _table_name(written: str) -> tuple[str, str] | None:
    """The folded (schema, name) of an allow object that is a two-part name; None for any other text."""
    try:
        toks = lex.significant(lex.tokenize(written))
    except lex.LexError:
        return None
    idents = ("word", "bident", "qident")
    if len(toks) != 3 or toks[1].text != "." or toks[0].kind not in idents or toks[2].kind not in idents:
        return None
    return _fold(toks[0].value, toks[2].value)


def _temporal_notes(destructive: Sequence[Destructive], history: Mapping[str, str]) -> list[str]:
    """One note for each allow TEMPORAL_OFF of the pending migrations. history: key of a history
    table -> key of its system-versioned table, as the catalog holds them before the change.

    The history table is no object of the model. After SET (SYSTEM_VERSIONING = OFF), and after
    the DROP TABLE that follows it, the engine leaves it in the database as a plain table: no
    release drops it, and the next plan lists it as unmanaged. The approver must know that.
    """
    history_of = {_fold_key(current): key for key, current in history.items()}
    dropped = {_table_name(item.object) for item in destructive if item.code == "DROP_TABLE"}
    notes: list[str] = []
    for item in destructive:
        if item.code != "TEMPORAL_OFF":
            continue
        table = _table_name(item.object)
        named = history_of.get(table) if table is not None else None
        history_table = f"its history table {named}" if named else "its history table"
        does = (
            f"drops the system-versioned table {item.object}"
            if table is not None and table in dropped
            else f"switches SYSTEM_VERSIONING off for {item.object}, and the engine stops writing history"
        )
        notes.append(
            f"{item.migration} {does}: {history_table} stays in the database as a plain table that the "
            "tool does not manage, with its rows. After the deploy it is listed as unmanaged; drop it by "
            "hand when the rows are no longer needed"
        )
    return notes


def _deploy_step(
    step_id: str,
    change: ModuleChange,
    migration: str | None = None,
    batch: int | None = None,
    line: int | None = None,
) -> Step:
    return Step(
        id=step_id,
        kind="deploy_module",
        migration=migration,
        batch=batch,
        line=line,
        object_key=change.key,
        path=change.path,
        sha256=change.checksum,
    )


def _migration_steps(
    pending: Sequence[Migration],
    sha_of: Mapping[str, str],
    need: dict[str, ModuleChange],
    in_release: Mapping[str, ModuleFile],
    managed: Mapping[str, ObjectRow],
) -> tuple[list[Step], list[ModuleChange], list[str], list[Destructive]]:
    """Batches in file order, each directive at its written position (design (d) 1).

    Returns (steps, module changes sent, keys unbound, allow items). `need` holds the module files
    still to send; it is changed here. A deploy-module directive sends its module only when the
    database does not hold that text. An unbind makes the module one to send again.
    """
    file_of = {_fold(m.schema, m.name): key for key, m in in_release.items()}
    rows_of: dict[tuple[str, str], list[str]] = {}
    for key in managed:
        rows_of.setdefault(_fold_key(key), []).append(key)
    steps: list[Step] = []
    sent: list[ModuleChange] = []
    unbinds: list[str] = []
    allows: list[Destructive] = []
    unbound: set[str] = set()  # unbound now, and not sent again since

    for migration in pending:
        for number, batch in enumerate(migration.batches, start=1):
            here = f"{migration.file}#{number}"
            for directive in batch.directives:
                if isinstance(directive, chain.Allow):
                    allows.append(
                        Destructive(
                            directive.code, directive.object, directive.reason, migration.file, directive.line
                        )
                    )
                    continue
                at = f"{migration.file} line {directive.line}"
                # the line makes the id unique when one batch names a module more than once
                directive_id = f"{migration.file}@{directive.line}"
                wanted = _directive_name(directive.object_key)
                if isinstance(directive, chain.Unbind):
                    rows = rows_of.get(wanted, [])
                    if len(rows) != 1:
                        found = "more than one managed module" if rows else "no managed module"
                        raise refused(
                            "DIRECTIVE_UNRESOLVED",
                            f"{at}: unbind {directive.object_key} names {found} of this database",
                            migration=migration.file,
                            line=directive.line,
                        )
                    key = rows[0]
                    if key in unbound:
                        continue  # A15: one unbind for each binding
                    unbound.add(key)
                    if key not in unbinds:
                        unbinds.append(key)
                    steps.append(
                        Step(
                            id=f"{directive_id}:unbind:{key}",
                            kind="unbind",
                            migration=migration.file,
                            batch=number,
                            line=directive.line,
                            object_key=key,
                            path=_MIGRATIONS_DIR + migration.file,
                        )
                    )
                    # the runner sets source_sha256 to NULL, so the file binds the module again
                    module = in_release.get(key)
                    if module is not None and key not in need:
                        need[key] = ModuleChange(key, module.path, module.checksum, "rebind")
                    continue
                key = file_of.get(wanted)
                if key is None:
                    raise refused(
                        "DIRECTIVE_UNRESOLVED",
                        f"{at}: deploy-module {directive.object_key} names no module file of this release",
                        migration=migration.file,
                        line=directive.line,
                    )
                change = need.pop(key, None)
                if change is None:
                    continue  # the database holds this text, or an earlier directive sent it
                unbound.discard(key)
                sent.append(change)
                steps.append(
                    _deploy_step(
                        f"{directive_id}:deploy:{key}", change, migration.file, number, directive.line
                    )
                )
            steps.append(
                Step(
                    id=here,
                    kind="batch",
                    migration=migration.file,
                    batch=number,
                    batch_kind=batch.kind,
                    line=batch.first_line,
                    path=_MIGRATIONS_DIR + migration.file,
                    sha256=sha_of[migration.file],
                )
            )
    return steps, sent, unbinds, allows


def _drop_order(keys: Sequence[str], managed: Mapping[str, ObjectRow]) -> list[str]:
    """Dependants first. The files are gone, so the edges come from the recorded definitions.

    Only a schema-bound module must go before the module it binds; no other drop needs an order.
    """
    edges: dict[str, set[str]] = {}
    for key in keys:
        capture = managed[key].capture
        definition = capture.get("definition")
        kind, schema, name = names.parse_object_key(key)
        if capture.get("is_schema_bound") is True and isinstance(definition, str) and schema is not None:
            recorded = ModuleFile(key, kind, schema, name, "", definition, "", True, (), ())
            edges[key] = modules.scan_references(recorded, keys)
    return modules.drop_order(keys, edges)


def _readback(keys: Collection[str], all_managed: bool = False) -> Step:
    return Step(id="readback", kind="readback", keys=tuple(sorted(keys)), all_managed=all_managed)


def pending_work(bundle: Bundle, recorded: State, module_chunk: int) -> Work:
    """What a deploy of this release does to a database with this recorded state. Pure.

    Refused (nothing is planned): STEP_UNRESOLVED, RUN_UNKNOWN (A5), BASELINE_REQUIRED,
    CHAIN_DIVERGED (A6), CATCHUP_REQUIRED (A7), NONTX_NOT_ALONE (A8), MODULE_ORPHAN,
    TOMBSTONE_CONFLICT, DIRECTIVE_UNRESOLVED, and the codes of files that cannot be read.
    Results that are not errors: outcome noop, record (A6) and already_past (A6).

    Two cases are planned with a note in Work.warnings and are not refusals: the first deploy to a
    database with no record (the whole chain in one unit, A7), and a replacement of a withdrawn
    migration that is merged after later migrations (it runs after them).
    """
    _refuse_unresolved(recorded)
    files, manifest = bundle.files, bundle.manifest
    seq, recorded_seq = manifest.release_seq, recorded.recorded_release_seq
    ch = (
        chain.parse_sum(_utf8(files, chain.SUM_PATH, "CHAIN_INVALID")) if chain.SUM_PATH in files else Chain()
    )
    if ch.baseline and not any(step.kind == "baseline" and step.status == "ok" for step in recorded.steps):
        raise refused(
            "BASELINE_REQUIRED",
            "the chain of this release starts with 'baseline', and this database has no baseline step. "
            "Onboard the target with azsqlcd baseline",
        )
    applied = tuple(
        AppliedMigration(step.migration_id, step.file_sha256, step.status)
        for step in recorded.steps
        if step.migration_id is not None
    )
    applied_ok = {a.id: a for a in applied if a.status == "ok"}
    # the release that the database holds: a deploy that committed a unit of work and whose run
    # did not end ok (RUN_NOT_CLOSED, a session lost at COMMIT) left its release here (A6)
    at_seq = max(recorded_seq, recorded.committed_release_seq)
    position = _pending_entries(ch, applied_ok, seq, recorded_seq, at_seq)
    if position is None:
        return Work(outcome=ALREADY_PAST, applied=applied)
    entries, order_notes = position
    withdrawn_unapplied = [entry for entry in ch.entries if entry.withdrawn and entry.file not in applied_ok]
    # A7, the first deploy. "Release by release" needs a release to start from. A database with no
    # recorded release, no deploy that committed, no migration step of any status and no baseline
    # step, on a chain with no baseline line, has none: it is new (after setup-sql), or its target
    # was added to azsqlcd.toml after the first releases, whose bundles do not know the target.
    # Such a database takes the whole chain with this release, in one unit of work. A database
    # that was onboarded with `azsqlcd baseline` is not new: the rule of A7 holds for it.
    # RP2-4: a step that resolve --mark-applied wrote is no deploy. NAME_COLLISION on a first deploy
    # tells the operator to write it, and the database has no release to be caught up from after it.
    deployed = [
        step
        for step in recorded.steps
        if step.migration_id is not None and not (step.note or "").startswith(MARKED_APPLIED_NOTE)
    ]
    first_deploy = (
        not ch.baseline
        and at_seq == 0
        and not deployed
        and not any(step.kind == "baseline" for step in recorded.steps)
    )
    catch_up_notes = _refuse_catch_up(entries, withdrawn_unapplied, recorded_seq, manifest, first_deploy)
    pending = tuple(_read_migration(files, entry) for entry in entries)

    in_release = module_files(files)
    tombstones = (
        {
            t.object_key: t
            for t in chain.parse_tombstones(_utf8(files, chain.TOMBSTONES_PATH, "TOMBSTONE_INVALID"))
        }
        if chain.TOMBSTONES_PATH in files
        else {}
    )
    # the key of a file or of a tombstone and the key of a row name one object when they are
    # equal without case: the row is then known under the key that the release writes
    written = {fold(key): key for key in (*tombstones, *in_release)}
    managed = {written.get(fold(key), key): row for key, row in _managed_modules(recorded).items()}
    files_folded = {fold(key) for key in in_release}
    both = sorted(key for key in tombstones if fold(key) in files_folded)
    if both:
        raise refused(
            "TOMBSTONE_CONFLICT",
            f"{both[0]} has a module file and a tombstone; the release must say one thing",
            objects=both,
        )
    orphans = sorted(key for key in managed if key not in in_release and key not in tombstones)
    if orphans:
        raise refused(
            "MODULE_ORPHAN",
            f"{orphans[0]} is managed in this database and the release has no file and no tombstone for it "
            f"({len(orphans)} in all). The tool never drops a module without a tombstone",
            objects=orphans,
        )
    need = _module_changes(in_release, managed)
    drops = [key for key in tombstones if key in managed]  # E5: recorded as managed and tombstoned

    nontx = next((migration for migration in pending if migration.mode == "nontx"), None)
    first_converge = (
        not pending and not drops and bool(need) and all(c.action == "overwrite" for c in need.values())
    )
    also = [m.file for m in pending if m is not nontx] + sorted(need) + drops
    steps, sent, unbinds, allows = _migration_steps(
        pending, {entry.file: entry.sha256 for entry in entries}, need, in_release, managed
    )
    also += unbinds  # an unbind is a change of a module, and the file must bind it again
    if nontx is not None and also:
        # A7 passed, so the migration is of this release; module changes can be of releases that
        # this database did not get (a module-only release may be skipped). Not so in a catch-up
        # over a withdrawn migration: no earlier release can be promoted there
        earlier = (
            f". If the other changes are of earlier releases, promote r{seq - 1} first"
            if seq > 1 and not catch_up_notes
            else ""
        )
        # A new database takes the whole chain in one unit of work, and a non-transactional
        # migration cannot be in it. The release before the one that added the migration holds the
        # chain up to it: from there each release is promoted in its turn
        before_nontx = manifest.chain_added_in.get(nontx.file, 0) - 1
        if first_deploy and before_nontx >= 1:
            earlier = (
                f". This database records no release: promote r{before_nontx} first (the release "
                f"before the one that added {nontx.file}), then each later release in its turn"
            )
        raise refused(
            "NONTX_NOT_ALONE",
            f"the non-transactional migration {nontx.file} must be the only change of its release, and "
            f"more is pending: {', '.join(also)}{earlier}",
            migration=nontx.file,
            also_pending=also,
        )

    rest = [in_release[key] for key in need]
    order, warnings = modules.deploy_order(rest, modules.build_edges(rest, ()))
    sent += [need[key] for key in order]
    drop_keys = _drop_order(drops, managed)
    destructive = (
        allows
        + [Destructive("DROP_MODULE", key, tombstones[key].reason) for key in drop_keys]
        + [
            Destructive(
                "OVERWRITE_MODULE",
                change.key,
                "the recorded source is not known (baseline, adoption or accepted drift): the file "
                "text replaces the text in the database",
            )
            for change in sent
            if change.action == "overwrite"
        ]
    )
    if first_converge:
        units = tuple(
            Unit(
                "modules_chunk",
                (*(_deploy_step(f"module:{key}", need[key]) for key in chunk), _readback(chunk)),
            )
            for chunk in batched(order, module_chunk)
        )
    else:
        steps += [_deploy_step(f"module:{key}", need[key]) for key in order]
        steps += [
            Step(id=f"drop:{key}", kind="drop_module", object_key=key, path=chain.TOMBSTONES_PATH)
            for key in drop_keys
        ]
        # a data or raw batch is not modelled: it can change any object, so all are read back
        unmodelled = any(step.batch_kind in ("data", "raw") for step in steps)
        if sent or unmodelled:
            steps.append(_readback([change.key for change in sent], unmodelled))
        units = (Unit("nontx" if nontx else "tx", tuple(steps)),) if steps else ()
    return Work(
        outcome=WORK if units else RECORD if seq > recorded_seq else NOOP,
        applied=applied,
        pending=pending,
        module_changes=tuple(sent),
        drops=tuple(drop_keys),
        unbinds=tuple(unbinds),
        first_converge=first_converge,
        units=units,
        destructive=tuple(destructive),
        warnings=(*catch_up_notes, *order_notes, *warnings),
    )


# ------------------------------------------------------------------ plan: checks on the database
def check_fence(facts: FenceFacts, database: str) -> None:
    """The fence of every command that reads or writes a target (Part 2 (e)). No override exists.

    database: the name that azsqlcd.toml gives the target. Refused: FENCE_ENGINE_EDITION (not Azure
    SQL Database), FENCE_READ_ONLY, FENCE_DB_NAME (the session is in another database; compared
    without case), FENCE_CASE_SENSITIVE (the tool compares object names without case).
    """
    if facts.engine_edition != 5:
        raise refused(
            "FENCE_ENGINE_EDITION",
            f"this is not Azure SQL Database (engine edition {facts.engine_edition}); the tool runs "
            "nowhere else",
            engine_edition=facts.engine_edition,
        )
    if facts.updateability != "READ_WRITE":
        raise refused(
            "FENCE_READ_ONLY",
            f"the database is {facts.updateability}: a read-only replica or a database in a read-only state",
            updateability=facts.updateability,
        )
    # the names of the databases of one server are not case-sensitive
    if facts.db_name.casefold() != database.casefold():
        raise refused(
            "FENCE_DB_NAME",
            f"the session is in database {facts.db_name!r}; azsqlcd.toml names {database!r} for this target",
            db_name=facts.db_name,
            configured=database,
        )
    if facts.case_sensitive:
        raise refused(
            "FENCE_CASE_SENSITIVE",
            f"the catalog of this database is case-sensitive ({facts.collation_name}); the tool compares "
            "object names without case",
            collation_name=facts.collation_name,
        )


def _drift(
    reason_code: str,
    why: str,
    keys: Sequence[str],
    differing: Mapping[str, list[Difference]],
    absent: Collection[str] = (),
    applied_by_hand: str | None = None,
) -> ToolError:
    """Objects, properties and hashes only: never definition text (A25).

    absent: the folded keys of the managed modules that the catalog does not hold. resolve
    --accept-drift does not take an object that is gone, so the message names the tombstone.
    applied_by_hand: the pending migration after which the model equals the catalog (N1-F2).
    """
    objects = [{"object": key, "differences": [asdict(d) for d in differing[key]]} for key in keys]
    if fold(keys[0]) in absent:
        return refused(
            reason_code,
            f"{keys[0]} is managed and is not in the database: it was dropped behind the tool, and "
            f"{why} ({len(keys)} object(s) in all). azsqlcd resolve --accept-drift does not take an "
            "object that is gone. Create it again by hand as the tool recorded it, or delete its file "
            f"and add a tombstone for it in {chain.TOMBSTONES_PATH}: the deploy of that release marks "
            "it dropped and sends no statement",
            objects=objects,
        )
    properties = ", ".join(difference.property for difference in differing[keys[0]])
    way_out = (
        "Revert the change in the database, or put it in the files and run azsqlcd resolve --accept-drift"
    )
    if applied_by_hand is not None:
        way_out = (
            f"The catalog equals the model after the pending migration {applied_by_hand}: if somebody "
            f"ran its statements by hand, run azsqlcd resolve --mark-applied {applied_by_hand} (not "
            "--accept-drift: the deploy would send the migration again and fail). Else revert the "
            "change in the database"
        )
    return refused(
        reason_code,
        f"{keys[0]} differs from what the tool recorded ({properties}), and {why} ({len(keys)} object(s) "
        f"in all). {way_out}",
        objects=objects,
    )


def _applied_by_hand(
    table_hooks: TableHooks, session: Session, bundle: Bundle, work: Work, drifted: Sequence[str]
) -> str | None:
    """N1-F2: the next pending migration, when every drifted table-class object is one that it
    changes and the catalog equals the model after it. Then somebody ran its statements by hand,
    and resolve --mark-applied (which makes this same read-back) is the action that works. Asked
    only when the plan is refused for drift: the read costs nothing on a sound database."""
    first = work.pending[0] if work.pending else None
    tables = [key for key in drifted if not _is_module(key)]
    if first is None or first.mode != "tx" or not tables:
        return None
    keys = sorted(table_hooks.touched_table_objects(replace(work, pending=(first,))))
    changed = {fold(key) for key in keys}
    if not all(fold(key) in changed for key in tables):
        return None
    try:
        table_hooks.read_back(session, bundle, keys, first.file)
    except ToolError:
        return None  # the catalog is not the model after the migration, or the model cannot be computed
    return first.file


def names_parseonly(text: str) -> bool:
    """The text holds the unquoted word PARSEONLY.

    SET PARSEONLY is read when a batch is parsed, so such a text could switch the syntax check
    off and run. It is never sent to parse_check; the caller refuses it or leaves it out.
    """
    return any(t.kind == "word" and t.text.upper() == "PARSEONLY" for t in lex.tokenize(text))


def _parse_error(session: Session, batch: str) -> SqlError | None:
    """The error of a batch that the engine only parses; None when it parses."""
    try:
        session.execute(batch)
    except SqlError as error:
        if error.cls is not ErrorClass.OTHER:
            raise  # a lost session or a limit of the service says nothing about the text
        return error
    return None


def parse_check(session: Session, texts: Mapping[str, str], *, restore_setting: bool) -> dict[str, SqlError]:
    """The syntax check of the engine: each text under SET PARSEONLY ON, which parses and does not run.

    texts: an id of the caller -> one batch. Returns id -> the error of the engine, for each text
    that does not parse, in the order of texts. The canary comes first, on the same session: a
    statement that must not run, then a batch that must not parse. If PARSEONLY had no effect the
    texts would run here, so a canary that fails is PARSEONLY_CANARY (refused) and no text is sent.

    restore_setting: send SET PARSEONLY OFF at the end, for a session that is used again, and prove
    that the session runs statements again: a SELECT that must return one row. Under PARSEONLY ON a
    SELECT returns no result set (measured on Azure SQL Database), so a session that gives none
    still parses only and every later read on it would be empty: PARSEONLY_CANARY (refused). A
    caller that closes the session passes False.

    Sends nothing and raises ValueError when a text holds the word PARSEONLY (names_parseonly). A
    SqlError that is not about the text (a lost session, a limit of the service) passes through.
    """
    unsafe = [name for name, text in texts.items() if names_parseonly(text)]
    if unsafe:
        raise ValueError(f"{unsafe[0]} holds the word PARSEONLY; it cannot be parsed without the risk to run")
    failures: dict[str, SqlError] = {}
    session.execute(_PARSEONLY_ON)
    try:
        try:
            ran = bool(session.execute(_CANARY_MUST_NOT_RUN))
        except SqlError as error:
            if error.cls is not ErrorClass.OTHER:
                raise
            ran = True
        if ran:
            raise refused(
                "PARSEONLY_CANARY",
                "SET PARSEONLY ON did not stop a statement on this database, so the syntax check cannot "
                "be trusted. No text was sent to be checked",
            )
        if _parse_error(session, _CANARY_MUST_NOT_PARSE) is None:
            raise refused(
                "PARSEONLY_CANARY",
                "a batch with a syntax error raised no error under SET PARSEONLY ON, so the syntax "
                "check cannot be trusted. No text was sent to be checked",
            )
        for name, text in texts.items():
            error = _parse_error(session, text)
            if error is not None:
                failures[name] = error
    finally:
        # a lost session has no setting to give back; on a live one a failure here must be seen
        if restore_setting and not session.closed:
            session.execute(_PARSEONLY_OFF)
            answer = session.execute(_PARSEONLY_IS_OFF)
            if len(answer) != 1 or len(answer[0]) != 1:
                raise refused(
                    "PARSEONLY_CANARY",
                    "SET PARSEONLY OFF did not give the session back: a SELECT after it returned no row, "
                    "so the session still parses only and every later read on it would be empty. Open a "
                    "new session",
                )
    return failures


def _parse_only(open_second_session: Callable[[], Session], bundle: Bundle, units: Sequence[Unit]) -> None:
    """Plan step 9: every pending batch and module through parse_check, on a session of its own."""
    pending: dict[str, tuple[Step, str, int | None]] = {}  # step id -> step, text, line of the text
    for unit in units:
        texts = step_texts(bundle, unit)
        # a module file is one batch, so its text starts at line 1
        pending |= {
            step.id: (step, texts[step.id], step.line if step.kind == "batch" else 1)
            for step in unit.steps
            if step.id in texts
        }
    for step, text, line in pending.values():
        if names_parseonly(text):
            raise refused(
                "FORBIDDEN_TOKEN",
                f"{step.path} line {line}: the text holds the word PARSEONLY; it cannot be checked "
                "without the risk that it runs",
                file=step.path,
                line=line,
            )
    second = open_second_session()
    try:
        errors = parse_check(
            second, {step_id: text for step_id, (_, text, _) in pending.items()}, restore_setting=False
        )
    finally:
        second.close()
    failures = [
        {
            "step": step_id,
            "file": pending[step_id][0].path,
            "line": pending[step_id][2],
            "error_number": error.number,
            "error": error.message,
        }
        for step_id, error in errors.items()
    ]
    if failures:
        first = failures[0]
        raise refused(
            "PARSEONLY_FAILED",
            f"{first['file']} line {first['line']}: the batch that starts here does not parse: "
            f"{first['error']} ({len(failures)} batch(es) in all)",
            failures=failures,
        )


def _with_table_steps(unit: Unit, refresh: Sequence[str], table_keys: Collection[str]) -> Unit:
    """Design (d) 4 and 5: refresh steps after the drops; the touched tables join the read-back."""
    old = next((step for step in unit.steps if step.kind == "readback"), None)
    steps = [step for step in unit.steps if step.kind != "readback"]
    steps += [Step(id=f"refresh:{key}", kind="refresh", object_key=key) for key in refresh]
    keys = set(table_keys) | set(old.keys if old else ())
    if keys or (old is not None and old.all_managed):
        steps.append(_readback(keys, old is not None and old.all_managed))
    return Unit(unit.kind, tuple(steps))


def _with_urls(units: Sequence[Unit], repo_url: str, commit: str) -> tuple[Unit, ...]:
    """A27: each step that comes from a file links to that file at the commit of the release."""
    base = f"{repo_url}/blob/{commit}/"

    def link(step: Step) -> Step:
        if step.path is None:
            return step
        anchor = f"#L{step.line}" if step.kind in ("batch", "unbind") and step.line else ""
        return replace(step, url=base + quote(step.path) + anchor)

    return tuple(Unit(unit.kind, tuple(link(step) for step in unit.steps)) for unit in units)


def compute_plan(
    bundle: Bundle,
    config: Config,
    env: str,
    target_id: str,
    session: Session,
    *,
    tool_version: str,
    tool_digest: str,
    repo_url: str | None = None,
    open_second_session: Callable[[], Session] | None = None,
    token_minutes_left: int | None = None,
    table_hooks: TableHooks | None = None,
    lock_held: bool = False,
    approved_plan: Plan | None = None,
) -> Plan:
    """The plan of one release for one target (plan steps 2 to 11). Read-only.

    lock_held: the caller holds the deploy lock on this session, so no other run can be live.
    open_second_session: opens the session of the syntax check; None skips the check with a note.
    approved_plan: the plan of the plan job, in the deploy of an approved plan. With no second
    session the check is not run again, and the note says what the plan job did: nothing when the
    approved plan records that its check ran, else a warning. The caller compares the two hashes;
    the same hash names the same texts.

    Refused: every code of pending_work(), and FENCE_ENGINE_EDITION, FENCE_READ_ONLY,
    FENCE_DB_NAME, FENCE_CASE_SENSITIVE, FENCE_META_MISMATCH, NAME_COLLISION, DRIFT_TOUCHED,
    DRIFT_BLOCK, LEGACY_FLAGS, INDEXED_VIEW, UNBIND_HEADER, TABLE_BLOCKER, FORBIDDEN_TOKEN, PARSEONLY_CANARY,
    PARSEONLY_FAILED, ENV_NOT_CONFIGURED, TARGET_NOT_CONFIGURED; STATE_MISSING and the other codes
    of state.read_state and catalog.capture_modules pass through. Locked (25): RUN_LIVE.
    """
    environment, target = resolve_target(config, env, target_id)
    # A4: the applock is the only liveness authority. It is asked first: APPLOCK_TEST waits for
    # nothing, and every catalog read after it waits for the schema locks of a live run (live
    # acceptance: read_state ended as lock timeout 1222 behind the ALTER TABLE of a deploy).
    if not lock_held and not catalog.applock_test(session):
        raise locked("RUN_LIVE", "a deploy or a resolve runs on this database now; plan again when it ended")
    check_fence(catalog.fence_facts(session), target.database)
    recorded = state.read_state(session)
    if (recorded.meta.project, recorded.meta.environment) != (config.project.name, env):
        raise refused(
            "FENCE_META_MISMATCH",
            f"this database is bound to project {recorded.meta.project!r}, environment "
            f"{recorded.meta.environment!r}; the plan is for {config.project.name!r}, {env!r}",
            project=recorded.meta.project,
            environment=recorded.meta.environment,
        )
    notes: list[str] = []
    if not lock_held:
        notes += [
            f"run {run.run_id} has the status running and no run holds the deploy lock: the run is "
            "dead. The next deploy reconciles it"
            for run in recorded.open_runs
            if run.status == "running"
        ]

    work = pending_work(bundle, recorded, config.project.module_chunk)
    manifest = bundle.manifest
    recorded_sha = recorded.recorded_git_sha
    repo = repo_url.rstrip("/") if repo_url else None
    plan = Plan(
        plan_sha256="",
        outcome=work.outcome,
        tool_version=tool_version,
        tool_digest=tool_digest,
        environment=env,
        target_id=target_id,
        server=target.server,
        database=target.database,
        manifest_digest=release.digest(manifest),
        release_seq=manifest.release_seq,
        git_sha=manifest.commit,
        recorded_release_seq=recorded.recorded_release_seq,
        recorded_git_sha=recorded_sha,
        compare_url=f"{repo}/compare/{recorded_sha}...{manifest.commit}" if repo and recorded_sha else None,
        applied=work.applied,
        token_minutes_left=token_minutes_left,
    )

    def sealed(**parts: Any) -> Plan:
        complete = replace(plan, notes=tuple(notes), **parts)
        return replace(complete, plan_sha256=_hash(complete))

    if work.outcome == ALREADY_PAST:
        committed = recorded.committed_release_seq
        not_ok = (
            f", a deploy of release r{committed} committed work and did not end ok,"
            if committed > plan.recorded_release_seq
            else ""
        )
        notes.append(
            f"ALREADY_PAST: the database is past release r{plan.release_seq}: it records release "
            f"r{plan.recorded_release_seq}{not_ok} and holds every migration of this release. Nothing is "
            "sent; older module text is never deployed again"
        )
        return sealed()
    notes += work.warnings

    # keys as the state holds them. The release can spell a key in another letter case: where a key
    # of the release meets a key of the state or of the catalog, both are folded
    managed = {key: row for key, row in recorded.objects.items() if row.status == "managed"}
    managed_modules = _managed_modules(recorded)
    rows = {fold(key): row for key, row in managed.items()}
    user_objects = catalog.list_user_objects(session)

    # step 5: a create must not take over an object that the tool did not make
    taken = {_fold(found.schema, found.name) for found in user_objects}
    collisions = [c.key for c in work.module_changes if c.action == "create" and _fold_key(c.key) in taken]
    if collisions:
        raise refused(
            "NAME_COLLISION",
            f"the release creates {collisions[0]}, and an object with this name exists in the database "
            f"that the tool did not record as this module ({len(collisions)} in all). Adopt the module "
            "with azsqlcd resolve --adopt-module <object key>, or rename one of the two",
            objects=collisions,
        )

    # step 6: drift
    captured = catalog.capture_modules(session, list(managed_modules))
    live = {fold(key): capture for key, capture in captured.items()}
    absent = {fold(key) for key in managed_modules if key not in captured}
    tombstoned = {fold(key) for key in work.drops}
    differing: dict[str, list[Difference]] = {}
    for key, row in managed_modules.items():
        if fold(key) in absent and fold(key) in tombstoned:
            # N1-F1: the module was dropped behind the tool, and this release takes it out. That is
            # not drift to revert or to accept: the runner marks the row dropped and sends no
            # statement (A18). A refusal here would leave no release that ends the state
            notes.append(
                f"{key} has a tombstone and is not in the database (it was dropped behind the tool): "
                "the deploy marks it dropped and sends no statement"
            )
            continue
        differences = catalog.capture_differences(row.capture, captured.get(key, {}))
        if differences:
            differing[key] = differences
    touched = {change.key for change in work.module_changes} | set(work.drops) | set(work.unbinds)
    table_keys: set[str] = set()
    if table_hooks is None:
        notes.append(
            "tables are not modelled: no table drift, no table read-back, no blocker or dependant scan, "
            "no refresh of dependants and no row counts"
        )
        if config.project.table_model:
            notes.append("azsqlcd.toml has table_model = true, and this run has no table model")
    else:
        unrecorded = table_hooks.unrecorded_objects(work, recorded)
        if unrecorded:  # Part 2 (c) 8
            raise refused(
                "BASELINE_REQUIRED",
                f"{unrecorded[0]} is in the table model of this release, has no managed row in this "
                f"database, and no pending migration creates it ({len(unrecorded)} object(s) in all). The "
                "database was never compared with the model: run azsqlcd baseline",
                objects=list(unrecorded),
            )
        if work.pending:
            table_keys = set(table_hooks.touched_table_objects(work))
            touched |= table_keys
        differing |= table_hooks.table_drift(session, recorded, sorted(set(managed) - set(managed_modules)))
    touched_folded = {fold(key) for key in touched}
    drifted = sorted(key for key in differing if fold(key) in touched_folded)
    if drifted:
        by_hand = (
            None if table_hooks is None else _applied_by_hand(table_hooks, session, bundle, work, drifted)
        )
        raise _drift("DRIFT_TOUCHED", "this release changes it", drifted, differing, absent, by_hand)
    if differing and environment.drift == "block":
        why = f'[env.{env}] has drift = "block"'
        raise _drift("DRIFT_BLOCK", why, sorted(differing), differing, absent)

    # step 7: CREATE OR ALTER would store the module with the flags of the session
    legacy = sorted(
        change.key
        for change in work.module_changes
        if not all(
            live.get(fold(change.key), {}).get(flag, True)
            for flag in ("uses_ansi_nulls", "uses_quoted_identifier")
        )
    )
    if legacy:
        raise refused(
            "LEGACY_FLAGS",
            f"{legacy[0]} is stored with ANSI_NULLS or QUOTED_IDENTIFIER off ({len(legacy)} in all); a "
            "deploy would change the flags. Create the module again with both on, then run azsqlcd "
            "resolve --accept-drift",
            objects=legacy,
        )
    # ALTER VIEW drops every index of the view, and CREATE OR ALTER VIEW is that ALTER when the
    # view exists. No read-back and no capture would show the loss. So a managed view that has an
    # index is never altered and never unbound: the release is refused. A view that the release
    # creates has no index to lose (step 5 refused a name that is taken); a drop is a tombstone.
    altered_views = sorted(
        key
        for key in {*(c.key for c in work.module_changes if c.action != "create"), *work.unbinds}
        if names.parse_object_key(key)[0] == "VIEW"
    )
    indexed = [key for key in altered_views if catalog.has_index(session, key)]
    if indexed:
        raise refused(
            "INDEXED_VIEW",
            f"{indexed[0]} has an index, and this release alters or unbinds it ({len(indexed)} view(s) "
            "in all). ALTER VIEW drops every index of a view, so the tool does not send it. Take the "
            "change of the view out of the release, or drop its indexes by hand first and create them "
            "again after the deploy",
            objects=indexed,
        )
    for key in work.unbinds:  # the runner must be able to rewrite the header: refuse now, not in the run
        _, schema, name = names.parse_object_key(key)
        definition = live.get(fold(key), {}).get("definition")
        if not isinstance(definition, str) or schema is None:
            raise refused(
                "UNBIND_HEADER", f"{key} cannot be unbound: its definition cannot be read", object=key
            )
        modules.rewrite_for_unbind(definition, schema, name)

    # step 8 and A20, with a table model only
    units = work.units
    pre_broken: tuple[str, ...] = ()
    dependants: tuple[str, ...] = ()
    dependant_findings: dict[str, tuple[str, ...]] = {}
    if table_hooks is not None and work.pending:
        # A12: the dependants are read now, while the old names of the tables still resolve
        dependants = tuple(sorted(table_hooks.dependants_to_check(session, work)))
        findings = table_hooks.blockers_and_dependants(session, work, config)
        if findings.blockers:
            raise refused(
                "TABLE_BLOCKER",
                f"{findings.blockers[0]} ({len(findings.blockers)} blocker(s) in all)",
                blockers=list(findings.blockers),
            )
        # A12: what the engine says of each dependant now. The runner fails one only for a finding
        # that is not here. A key with no answer is a defect of the hooks: it is never read as sound
        before = {
            fold(key): found for key, found in table_hooks.dependant_findings(session, dependants).items()
        }
        dependant_findings = {key: tuple(sorted(before[fold(key)])) for key in dependants}
        pre_broken = tuple(key for key in dependants if dependant_findings[key])
        collisions = table_hooks.sub_object_collisions(session, work, recorded)
        if collisions:
            raise refused(
                "NAME_COLLISION",
                f"a pending create names {collisions[0]}, which exists in the database and is not recorded "
                f"({len(collisions)} in all). If it is the change of the migration, run azsqlcd resolve "
                "--mark-applied <migration>; else rename one of the two",
                objects=collisions,
            )
        refresh = [] if units[0].kind == "nontx" else table_hooks.refresh_set(session, work)
        units = (_with_table_steps(units[0], refresh, table_keys),)

    unmanaged_kinds = names.MODULE_KINDS + (() if table_hooks is None else ("TABLE", "SEQUENCE", "SYNONYM"))
    # The history table of a system-versioned table is a row of sys.objects that the engine owns:
    # it has no file and no managed row, and it is not an unmanaged object. Without a table model
    # no table is listed, so the catalog is not asked.
    history = {} if table_hooks is None else catalog_tables.history_tables(session)
    owned_by_engine = {fold(key) for key in history}
    unmanaged = sorted(
        key
        for found in user_objects
        if found.kind is not None
        and found.kind in unmanaged_kinds
        and fold(key := names.object_key(found.kind, found.schema, found.name)) not in rows
        and fold(key) not in owned_by_engine
    )
    notes += _temporal_notes(work.destructive, history)

    # step 9
    syntax_check = None
    if units and open_second_session is not None:
        _parse_only(open_second_session, bundle, units)
        syntax_check = SYNTAX_RAN
    elif units and approved_plan is not None and approved_plan.syntax_check == SYNTAX_RAN:
        syntax_check = SYNTAX_RAN  # in the plan job; a warning here would be about a check that ran
    elif units:
        syntax_check = SYNTAX_SKIPPED
        if approved_plan is None:
            why = "was skipped: this run has no second session"
        elif approved_plan.syntax_check == SYNTAX_SKIPPED:
            why = "was skipped: the plan job had no second session" + _NOT_IN_DEPLOY
        else:
            why = "is not proven: the approved plan does not record that the plan job ran it" + _NOT_IN_DEPLOY
        notes.append(f"the syntax check (SET PARSEONLY ON) {why}")

    # step 10: facts for the approver
    tables = sorted(key for key in table_keys if key.startswith("TABLE:"))
    facts = catalog.table_facts(session, tables)
    return sealed(
        touched=tuple(
            Touched(key, rows[fold(key)].catalog_sha256 if fold(key) in rows else None)
            for key in sorted(touched)
        ),
        units=_with_urls(units, repo, manifest.commit) if repo else units,
        destructive=work.destructive,
        drift=tuple(Drift(key, tuple(differing[key])) for key in sorted(differing)),
        unmanaged=tuple(unmanaged),
        dependants=dependants,
        dependant_findings=dependant_findings,
        pre_broken=pre_broken,
        table_facts=tuple(
            TableFact(key, facts[key].rows, facts[key].reserved_pages) for key in sorted(facts)
        ),
        service_objective=catalog.service_objective(session),
        syntax_check=syntax_check,
    )
