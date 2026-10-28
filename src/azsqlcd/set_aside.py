"""Set-aside migrations: a pending migration that one environment does not run, for a reason.

A migration that is set aside in an environment is not applied there, is never marked applied,
and is no pending migration for the catch-up rule (A7). The releases after it deploy past it, in
chain order, when what they run is independent of it (check()). Opt-in: with no reason switched
on in azsqlcd.toml, read() reads nothing and returns NOTHING, and the planner is as before.

Three reasons are built: `parked`, `held` and `not_managed`. With [project] park_failed = true, a migration
whose batch failed in a deploy of the environment is parked there until it is applied, withdrawn, or
unparked (`azsqlcd resolve --unpark <file>`). The facts are the rows that exist; no column or status
was added. A park is a deploy run row with the status failed or unknown, a failed_step that is a
batch of the migration (`<file>#<n>`), and a note (the reason code of its end) in PARK_REASON_CODES. An
unpark is a later resolve step with the note unpark_note(file), as clear-run is (A5).

held: [products.<name>] promote_to (products.py). The hold needs the environment and the files
of the release, so plan.pending_work merges it (products.Hold.merge), not read().

not_managed: [env.<name>] not_managed of azsqlcd.toml (not_managed.py). A pending migration that
touches only objects that the environment does not manage is skipped there for good: a migration
step with the status not_applied and the note skip_note(env). It is never applied there, also after
the pattern went. Its objects are outside the scope there, so check() does not hold others to it.

A later reason plugs in at three places: a name in REASONS, its way out in WAY_OUT, and its
entries in read(). The rest (the plan notes, the catch-up past set-aside migrations, the
independence check) is the same for every reason but not_managed, which check() skips.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from azsqlcd import chain, diff, modules, names, state
from azsqlcd.chain import Migration
from azsqlcd.config import Config
from azsqlcd.errors import ToolError, refused
from azsqlcd.model import Model, Rename
from azsqlcd.modules import ModuleFile
from azsqlcd.parse import ParseError, parse_statement
from azsqlcd.session import Session
from azsqlcd.state import FailedRun, State, StepRow

PARKED = "parked"
HELD = "held"  # products.py: a product whose promote_to comes before the environment
# [env.<name>] not_managed (not_managed.py): skipped for good in the environment
NOT_MANAGED = "not_managed"
REASONS = (PARKED, HELD, NOT_MANAGED)
# what an operator does so that the migration runs, or never runs, here
WAY_OUT = {
    PARKED: "end the park with azsqlcd resolve --unpark {file}, so that it runs again at its place in the "
    "chain, or withdraw and replace it",
    HELD: "raise or remove promote_to of the product of {file} in azsqlcd.toml by pull request, so that it "
    "runs here at its place in the chain",
    NOT_MANAGED: "withdraw and replace {file}: a migration skipped as not managed never runs here",
}
# the ends of a deploy that say the engine refused the batch itself. A lost session, a lock timeout
# or a deadlock says nothing about the batch, and the batch is tried again
PARK_REASON_CODES = ("BATCH_FAILED", "GOVERNANCE_LIMIT", "NONTX_FAILED")


@dataclass(frozen=True)
class SetAside:
    file: str  # migration id
    reason: str  # one of REASONS
    why: str  # the facts behind it, for the plan note; parked: "run <id>, <reason code>"

    @property
    def note(self) -> str:
        return f"{self.reason}: {self.file} ({self.why})"


@dataclass(frozen=True)
class Aside:
    """The set-aside facts of one database."""

    active: Mapping[str, SetAside] = field(default_factory=dict)  # migration id -> why it is set aside
    # set aside before and not now: when still pending, it runs at its place among the pending ones,
    # also when later migrations of the chain are applied here already
    released: frozenset[str] = frozenset()

    @property
    def known(self) -> frozenset[str]:
        return frozenset(self.active) | self.released


NOTHING = Aside()


def unpark_note(file: str) -> str:
    """The note of the resolve step of `resolve --unpark <file>`. The note of every other action
    starts with the name of the action and a colon, so no reason text can be this note."""
    return f"unpark {file}"


def batch_migration(step_id: str) -> str | None:
    """The migration id of a batch step id (`<file>#<n>`); None for every other step id."""
    file, mark, number = step_id.rpartition("#")
    return file if mark and file and number.isdigit() else None


def parked(failed_runs: Sequence[FailedRun], steps: Sequence[StepRow]) -> Aside:
    """The parks of a database: from its failed deploy runs (by run_id) and its step rows. Pure."""
    last: dict[str, FailedRun] = {}
    for run in failed_runs:
        file = batch_migration(run.failed_step)
        if file is not None and run.reason_code in PARK_REASON_CODES:
            last[file] = run
    active: dict[str, SetAside] = {}
    released: set[str] = set()
    for file, run in last.items():
        unparked = any(
            step.kind == "resolve"
            and step.status == "ok"
            and step.run_id > run.run_id
            and step.note == unpark_note(file)
            for step in steps
        )
        if unparked:
            released.add(file)
        else:
            active[file] = SetAside(file, PARKED, f"run {run.run_id}, {run.reason_code}")
    return Aside(active, frozenset(released))


def skip_note(env: str) -> str:
    """The note of the step that records a migration skipped as not managed: a migration step with
    the status not_applied. resolve --mark-not-applied writes another note, on a nontx step."""
    return f"{NOT_MANAGED} in {env}"


def skipped(steps: Sequence[StepRow]) -> dict[str, SetAside]:
    """The migrations skipped for good as not managed: from the step rows, also after the pattern
    went from azsqlcd.toml. Pure: the steps are read by read_state already."""
    return {
        step.migration_id: SetAside(step.migration_id, NOT_MANAGED, f"skipped by run {step.run_id}")
        for step in steps
        if step.migration_id is not None
        and step.status == "not_applied"
        and (step.note or "").startswith(skip_note(""))
    }


def read(session: Session, config: Config, recorded: State) -> Aside:
    """The set-aside facts of the database of the session. No batch is sent when no reason is on."""
    skips = skipped(recorded.steps)
    if not config.project.park_failed:
        return Aside(skips) if skips else NOTHING
    found = parked(state.read_failed_runs(session), recorded.steps)
    return Aside({**found.active, **skips}, found.released) if skips else found


def in_force(aside: Aside, steps: Sequence[StepRow], files: Mapping[str, bytes]) -> tuple[SetAside, ...]:
    """The set-aside migrations that still hold, by file: not applied in the database (its step
    rows), and not withdrawn in the chain of the release (files). For drift and resolve; the
    planner reads the chain itself. Pure."""
    if not aside.active:
        return ()
    applied = {step.migration_id for step in steps if step.migration_id and step.status == "ok"}
    sums = files.get(chain.SUM_PATH)
    withdrawn = (
        {entry.file for entry in chain.parse_sum(sums.decode("utf-8")).entries if entry.withdrawn}
        if sums is not None
        else set()
    )
    return tuple(
        aside.active[file] for file in sorted(aside.active) if file not in applied and file not in withdrawn
    )


# ------------------------------------------------------------------ independence
@dataclass(frozen=True)
class Footprint:
    touched: frozenset[tuple[str, str]]  # case-folded (schema, name)
    named: frozenset[tuple[str, str]]  # every name of the text: touched, referenced, and columns
    unproven: tuple[str, ...]  # the kinds of the batches whose touched objects are their names


def _model_touched(text: str, model: Model | None) -> tuple[list[tuple[str, str]], bool] | None:
    """(the (schema, name) of what a model batch changes, proven); None when the batch does not parse.
    A rename that diff.touched_objects cannot follow (no model, or a constraint that the model does not
    hold) changes what its statement names: the names are in string literals, which no token scan
    reads. It is not proven: a table with a key to the renamed one is not in the list."""
    try:
        op = parse_statement(text)
    except ParseError:
        return None
    try:
        keys = diff.touched_objects([op], model)
    except ValueError:
        if not isinstance(op, Rename):
            return None
        schema, old = op.old[0], op.old[1]
        return [(schema, old)] + ([] if op.kind in ("column", "index") else [(schema, op.new_name)]), False
    found: list[tuple[str, str]] = []
    for key in keys:
        _, schema, name = names.parse_object_key(key)
        if schema is not None:  # a schema key names no object of a schema
            found.append((schema, name))
    return found, True


def footprint(migration: Migration, model: Model | None = None) -> Footprint:
    """What a migration touches and names. A model batch touches what its operation changes
    (diff.touched_objects; a rename with model, the head model of the release, also touches the
    tables with a key to what it renames); a data or raw batch touches every object that its text
    names."""
    touched: set[tuple[str, str]] = set()
    named: set[tuple[str, str]] = set()
    unproven: list[str] = []
    for batch in migration.batches:
        found = modules.named_objects(batch.text)
        named |= found
        changed = _model_touched(batch.text, model) if batch.kind == "model" else None
        if changed is None:
            touched |= found
            unproven.append(batch.kind)
            continue
        keys, proven = changed
        folded = {(schema.casefold(), name.casefold()) for schema, name in keys}
        touched |= folded
        named |= folded
        if not proven:
            unproven.append(batch.kind)
    return Footprint(frozenset(touched), frozenset(named), tuple(unproven))


def _dependency(blocked: str, aside: SetAside, shared: tuple[str, str]) -> ToolError:
    shown = f"{names.quote(shared[0])}.{names.quote(shared[1])}"
    return refused(
        "SET_ASIDE_DEPENDENCY",
        f"{blocked} is not independent of {aside.file}, which is {aside.reason} in this environment "
        f"({aside.why}): both name {shown}. A set-aside migration does not run here, so nothing that it "
        f"touches or names may change before it, and nothing may name what it touches. Either "
        f"{WAY_OUT[aside.reason].format(file=aside.file)}, or take {blocked} out of the release",
        blocked=blocked,
        set_aside=aside.file,
        reason=aside.reason,
        object=shown,
    )


def check(
    pending: Sequence[Migration],
    aside: Sequence[tuple[SetAside, Migration]],
    sent_modules: Collection[ModuleFile],
    model: Model | None = None,
) -> list[str]:
    """Refuse (SET_ASIDE_DEPENDENCY) a pending migration W or a module to send that is not
    independent of a set-aside migration A. W is independent when it touches nothing that A
    touches or names, and names nothing that A touches. A module is independent when its text
    names nothing that A touches, as the dependency scan of modules reads it (a one-part name is
    dbo, A17). model: the head model of the release, when tables are modelled. Returns the plan
    notes of the batches whose touched objects are not proven."""
    feet = {migration.file: footprint(migration, model) for migration in pending}
    notes: dict[str, None] = {}
    for item, migration in aside:
        if item.reason == NOT_MANAGED:
            # its objects are outside the scope here, as an object of another schema is: a release
            # that comes back to them is compared by baseline --schema first
            continue
        mine = footprint(migration, model)
        for other in pending:
            theirs = feet[other.file]
            shared = (theirs.touched & (mine.touched | mine.named)) | (theirs.named & mine.touched)
            if shared:
                raise _dependency(other.file, item, min(shared))
        for module in sorted(sent_modules, key=lambda m: m.key):
            shared = modules.referenced_objects(module.text) & mine.touched
            if shared:
                raise _dependency(module.key, item, min(shared))
        for kind in mine.unproven:
            notes[f"independence not proven for a {kind} batch: {migration.file}"] = None
    for migration in pending:
        for kind in feet[migration.file].unproven:
            notes[f"independence not proven for a {kind} batch: {migration.file}"] = None
    return list(notes)
