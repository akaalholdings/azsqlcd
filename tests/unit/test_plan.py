"""The planner. pending_work() is tested with no session; compute_plan() with FakeSession.

A release is a small in-memory bundle; the recorded state is a State value. No test runs T-SQL.
Where the intent is in the batch text (nothing can change the database), the tests read the
batches with the lexer of the tool.
"""

import dataclasses
import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from azsqlcd import catalog, chain, lex, names, plan, release, state
from azsqlcd.catalog import Difference
from azsqlcd.config import load_config
from azsqlcd.errors import Exit, ToolError, failed
from azsqlcd.modules import checksum
from azsqlcd.plan import (
    ALREADY_PAST,
    NOOP,
    RECORD,
    WORK,
    AppliedMigration,
    Destructive,
    Plan,
    TableFindings,
    Work,
    compute_plan,
    pending_work,
)
from azsqlcd.release import Bundle, Manifest
from azsqlcd.session import ResultSets
from azsqlcd.sqlerrors import SqlError, sql_error
from azsqlcd.state import Meta, ObjectRow, RunRow, State, StepRow
from support.fake_session import FakeSession

COMMIT = "c" * 40
RECORDED_COMMIT = "a" * 40
TOOL_DIGEST = "d" * 64
M1, M2, M3, M4 = "0001__a.sql", "0002__b.sql", "0003__c.sql", "0004__d.sql"
SAME = "same"
FACTS = (5, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0)
TYPE_CODES = {"VIEW": "V", "PROCEDURE": "P", "FUNCTION": "FN", "TRIGGER": "TR"}
# words that start or make a statement that can change a database
WRITES = frozenset(
    "INSERT UPDATE DELETE MERGE EXEC EXECUTE CREATE ALTER DROP TRUNCATE INTO SET GRANT BEGIN COMMIT".split()
)

TOML = """
[project]
name = "sales"
tenant_id = "11111111-1111-1111-1111-111111111111"
table_model = false
module_chunk = 2
min_token_minutes = 20

[identities]
plan = "22222222-2222-2222-2222-222222222222"
deploy = "33333333-3333-3333-3333-333333333333"

[env.dev]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "report"
lock_timeout_ms = 30000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{ id = "sales-dev", server = "sql-sales-dev.database.windows.net", database = "sales" }]

[env.prod]
plan_identity = "plan"
deploy_identity = "deploy"
drift = "block"
lock_timeout_ms = 10000
applock_wait_s = 600
job_timeout_minutes = 120
targets = [{ id = "sales-prod", server = "sql-sales-prod.database.windows.net", database = "sales" }]
"""
CONFIG = load_config(TOML)


# ------------------------------------------------------------------ a release in memory
@dataclasses.dataclass(frozen=True)
class Line:
    """One line of migrations.sum with the text of its file."""

    file: str
    text: str
    mode: str = "tx"
    added: int | None = None  # release that added the line; None = the release under test
    withdrawn: bool = False
    replaces: str | None = None


def mig(file: str, *batches: str, mode: str = "tx", **line: Any) -> Line:
    body = "\nGO\n".join(batches or ("ALTER TABLE [sales].[Order] ADD [c] int NULL;",))
    header = f"-- azsqlcd:migration {file.removesuffix('.sql')}\n-- azsqlcd:mode {mode}\n"
    return Line(file, f"{header}{body}\nGO\n", mode, **line)


def key(kind: str, name: str, schema: str = "sales") -> str:
    return names.object_key(kind, schema, name)


def proc(name: str, body: str = "SELECT 1;") -> tuple[str, str]:
    return f"schema/procedures/sales.{name}.sql", f"CREATE OR ALTER PROCEDURE [sales].[{name}] AS\n{body}\n"


def view(name: str, select: str = "SELECT 1 AS [x]", options: str = "") -> tuple[str, str]:
    return f"schema/views/sales.{name}.sql", f"CREATE OR ALTER VIEW [sales].[{name}]{options} AS\n{select};\n"


def func(name: str, expression: str = "1") -> tuple[str, str]:
    text = (
        f"CREATE OR ALTER FUNCTION [sales].[{name}]() RETURNS int AS\nBEGIN\n    RETURN {expression};\nEND\n"
    )
    return f"schema/functions/sales.{name}.sql", text


def bundle(
    *lines: Line,
    modules: Sequence[tuple[str, str]] = (),
    tombstones: Sequence[str] = (),
    seq: int = 7,
    baseline: bool = False,
    extra: dict[str, bytes] | None = None,
) -> Bundle:
    files: dict[str, bytes] = {release.CONFIG_PATH: TOML.encode()}
    entries = []
    for line in lines:
        data = line.text.encode()
        files[f"migrations/{line.file}"] = data
        entries.append(
            chain.ChainEntry(line.file, chain.file_sha256(data), line.mode, line.withdrawn, line.replaces)
        )
    if lines or baseline:
        files[chain.SUM_PATH] = chain.format_sum(chain.Chain(baseline, tuple(entries))).encode()
    for path, text in modules:
        files[path] = text.encode()
    if tombstones:
        files[chain.TOMBSTONES_PATH] = "".join(
            f'[[drop]]\nobject = "{object_key}"\nreason = "replaced"\n' for object_key in tombstones
        ).encode()
    files.update(extra or {})
    manifest = Manifest(
        commit=COMMIT,
        release_seq=seq,
        files=tuple(sorted((path, hashlib.sha256(data).hexdigest()) for path, data in files.items())),
        chain_added_in={line.file: line.added or seq for line in lines},
    )
    return Bundle(manifest, files)


# ------------------------------------------------------------------ a recorded state
def live_row(
    object_key: str, definition: str, *, ansi: bool = True, quoted: bool = True, schema_bound: bool = False
) -> tuple:
    """One row of the capture_modules result set, in its column order."""
    kind, schema, name = names.parse_object_key(object_key)
    return (
        *(object_key, schema, name, TYPE_CODES[kind], definition, ansi, quoted, schema_bound),
        *(None, 1, None, None, None, None),
    )


def capture(row: tuple) -> dict[str, Any]:
    """The capture that the tool makes of a catalog row: what a deploy records."""
    db = FakeSession()
    db.respond("azsqlcd:capture_modules", [[row]])
    return catalog.capture_modules(db, [row[0]])[row[0]]


def row(
    object_key: str, text: str, *, source: str | None = SAME, status: str = "managed", **flags: bool
) -> ObjectRow:
    """The object row of a module that was deployed with this text. source None = redeploy."""
    captured = capture(live_row(object_key, text, **flags))
    recorded_source = checksum(text.encode()) if source == SAME else source
    return ObjectRow(status, recorded_source, 1, captured, state.capture_sha256(captured))


def run(run_id: int, status: str, seq: int = 5) -> RunRow:
    return RunRow(run_id, "deploy", status, 0, seq, RECORDED_COMMIT, "2026-10-01T10:00:00.000")


def recorded(
    *,
    steps: Sequence[StepRow] = (),
    objects: dict[str, ObjectRow] | None = None,
    seq: int = 0,
    open_runs: Sequence[RunRow] = (),
    project: str = "sales",
    env: str = "dev",
) -> State:
    """seq is the recorded release: 0 = no run ended ok."""
    return State(
        Meta(1, project, env),
        tuple(open_runs),
        run(1, "ok", seq) if seq else None,
        tuple(steps),
        objects or {},
    )


def applied(b: Bundle, *files: str, status: str = "ok", first_id: int = 1) -> list[StepRow]:
    """Step rows for migrations of the bundle, in the order given."""
    entries = {entry.file: entry for entry in chain.parse_sum(b.files[chain.SUM_PATH].decode()).entries}
    return [
        StepRow(
            step_id,
            1,
            "nontx" if entries[f].mode == "nontx" else "migration",
            f,
            entries[f].sha256,
            status,
            None,
        )
        for step_id, f in enumerate(files, start=first_id)
    ]


def other_step(step_id: int, file: str = "0009__other.sql") -> StepRow:
    return StepRow(step_id, 1, "migration", file, "f" * 64, "ok", None)


def refusal(call: Callable[..., object], *args: Any, **kwargs: Any) -> ToolError:
    with pytest.raises(ToolError) as caught:
        call(*args, **kwargs)
    return caught.value


def ids(work: Work | Plan, unit: int = 0) -> list[str]:
    return [step.id for step in work.units[unit].steps]


# ------------------------------------------------------------------ pending_work: steps and runs
@pytest.mark.parametrize("status", ["started", "unknown"])
def test_a_step_whose_outcome_is_not_known_stops_every_plan(status):
    b = bundle(mig(M1, mode="nontx"))
    error = refusal(pending_work, b, recorded(steps=applied(b, M1, status=status)), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "STEP_UNRESOLVED")
    assert error.detail["steps"] == [{"step_id": 1, "migration": M1, "status": status}]


def test_a_step_that_resolve_marked_not_applied_is_pending_again():
    b = bundle(mig(M1, "CREATE INDEX [IX_a] ON [sales].[Order] ([c]) WITH (ONLINE = ON);", mode="nontx"))
    work = pending_work(b, recorded(steps=applied(b, M1, status="not_applied")), 100)
    assert [m.file for m in work.pending] == [M1]
    assert work.applied == (AppliedMigration(M1, work.units[0].steps[0].sha256, "not_applied"),)


def test_an_unknown_run_stops_every_plan_until_a_later_clear_run_step():
    b = bundle(mig(M1))
    unknown = run(2, "unknown")

    def clear(run_id: int, note: str) -> StepRow:
        return StepRow(9, run_id, "resolve", None, None, "ok", note)

    error = refusal(pending_work, b, recorded(open_runs=[unknown]), 100)
    assert (error.exit_code, error.reason_code, error.detail) == (Exit.REFUSED, "RUN_UNKNOWN", {"run_id": 2})
    # a clear of another run, and a step of an earlier run, clear nothing
    for not_a_clear in (clear(3, plan.clear_run_note(7)), clear(1, plan.clear_run_note(2))):
        still_unknown = recorded(open_runs=[unknown], steps=[not_a_clear])
        assert refusal(pending_work, b, still_unknown, 100).reason_code == "RUN_UNKNOWN"
    cleared = recorded(open_runs=[unknown], steps=[clear(3, plan.clear_run_note(2))])
    assert pending_work(b, cleared, 100).outcome == WORK


def test_a_run_that_is_only_running_does_not_stop_the_pure_plan():
    # liveness is the applock (A4); compute_plan asks it
    b = bundle(mig(M1))
    assert pending_work(b, recorded(open_runs=[run(2, "running")]), 100).outcome == WORK


def test_a_chain_that_starts_with_baseline_needs_a_baseline_step():
    b = bundle(mig(M1), baseline=True)
    error = refusal(pending_work, b, recorded(), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "BASELINE_REQUIRED")
    baselined = recorded(steps=[StepRow(1, 1, "baseline", None, None, "ok", None)], seq=3)
    assert [m.file for m in pending_work(b, baselined, 100).pending] == [M1]


# ------------------------------------------------------------------ pending_work: chain position
def test_an_applied_migration_that_the_release_does_not_hold_is_a_diverged_chain():
    b = bundle(mig(M1), seq=7)
    error = refusal(pending_work, b, recorded(steps=[*applied(b, M1), other_step(2)], seq=6), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "CHAIN_DIVERGED")
    assert error.detail == {"migration": "0009__other.sql", "recorded_release_seq": 6, "release_seq": 7}


def test_an_applied_migration_with_another_checksum_is_a_diverged_chain():
    b = bundle(mig(M1))
    changed = dataclasses.replace(applied(b, M1)[0], file_sha256="e" * 64)
    error = refusal(pending_work, b, recorded(steps=[changed], seq=6), 100)
    assert (error.reason_code, error.detail["migration"]) == ("CHAIN_DIVERGED", M1)


def test_a_later_migration_applied_without_an_earlier_one_is_a_diverged_chain():
    b = bundle(mig(M1), mig(M2), mig(M3))
    error = refusal(pending_work, b, recorded(steps=applied(b, M1, M3), seq=6), 100)
    assert (error.reason_code, error.detail["migration"]) == ("CHAIN_DIVERGED", M2)


@pytest.mark.parametrize("recorded_release", [8, 5])
def test_a_database_that_is_ahead_and_consistent_plans_nothing_and_is_not_an_error(recorded_release):
    # 8: a newer release is recorded. 5: a later run committed its migration and did not end ok
    b = bundle(mig(M1), modules=[proc("usp_a")], seq=5)
    ahead = recorded(steps=[*applied(b, M1), other_step(2, M2)], seq=recorded_release)
    work = pending_work(b, ahead, 100)
    assert (work.outcome, work.units, work.pending, work.module_changes) == (ALREADY_PAST, (), (), ())


def test_an_older_release_after_a_newer_no_op_is_refused_as_already_past_and_deploys_no_older_text():
    path, text = proc("usp_a", "SELECT 'text of release 5';")
    b = bundle(modules=[(path, text)], seq=5)
    objects = {
        key("PROCEDURE", "usp_a"): row(key("PROCEDURE", "usp_a"), "text of release 6", source="6" * 64)
    }
    # the no-op deploy of release 6 wrote a run row (A6): release 5 is past, its text is not sent
    after_no_op = pending_work(b, recorded(objects=objects, seq=6), 100)
    assert (after_no_op.outcome, after_no_op.units, after_no_op.module_changes) == (ALREADY_PAST, (), ())
    # without that record the same release would send its text
    assert (
        ids(pending_work(b, recorded(objects=objects, seq=5), 100))[0] == "module:PROCEDURE:[sales].[usp_a]"
    )


def test_a_database_that_records_a_newer_release_and_lacks_a_migration_of_this_one_is_diverged():
    b = bundle(mig(M1), mig(M2), seq=5)
    error = refusal(pending_work, b, recorded(steps=applied(b, M1), seq=8), 100)
    assert (error.reason_code, error.detail["migration"]) == ("CHAIN_DIVERGED", M2)


def test_a_database_is_past_a_release_only_when_the_chain_of_the_release_was_applied_first():
    b = bundle(mig(M1), seq=5)
    other_first = recorded(steps=[other_step(1), *applied(b, M1, first_id=2)], seq=8)
    assert refusal(pending_work, b, other_first, 100).reason_code == "CHAIN_DIVERGED"


def test_nothing_pending_at_the_recorded_release_is_a_no_op():
    b = bundle(mig(M1), seq=7)
    work = pending_work(b, recorded(steps=applied(b, M1), seq=7), 100)
    assert (work.outcome, work.units) == (NOOP, ())


def test_nothing_pending_at_a_newer_release_still_records_the_release():
    b = bundle(mig(M1), seq=7)
    work = pending_work(b, recorded(steps=applied(b, M1), seq=6), 100)
    assert (work.outcome, work.units) == (RECORD, ())


# ------------------------------------------------------------------ pending_work: withdrawn and replaces
def test_a_withdrawn_migration_that_was_never_applied_is_skipped_and_its_replacement_runs_at_its_position():
    b = bundle(mig(M1), mig(M2, withdrawn=True), mig(M3), mig(M4, replaces=M2))
    work = pending_work(b, recorded(), 100)
    assert [m.file for m in work.pending] == [M1, M4, M3]
    assert ids(work) == [f"{M1}#1", f"{M4}#1", f"{M3}#1"]


def test_the_replacement_of_an_applied_migration_is_not_pending():
    b = bundle(mig(M1), mig(M2, withdrawn=True), mig(M3), mig(M4, replaces=M2))
    work = pending_work(b, recorded(steps=applied(b, M1, M2, M3), seq=6), 100)
    assert (work.pending, work.outcome) == ((), RECORD)


def test_the_replacement_of_a_replacement_is_not_pending_when_the_first_migration_is_applied():
    b = bundle(mig(M1, withdrawn=True), mig(M2, withdrawn=True, replaces=M1), mig(M3, replaces=M2))
    assert pending_work(b, recorded(steps=applied(b, M1), seq=7), 100).outcome == NOOP
    assert [m.file for m in pending_work(b, recorded(), 100).pending] == [M3]


# ------------------------------------------------------------------ pending_work: catch-up (A7)
def test_pending_migrations_from_two_releases_require_a_catch_up():
    b = bundle(mig(M1, added=4), mig(M2, added=5), mig(M3, added=7), seq=7)
    error = refusal(pending_work, b, recorded(steps=applied(b, M1), seq=4), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "CATCHUP_REQUIRED")
    assert "promote r5 first" in error.message
    assert (error.detail["promote"], error.detail["pending"]) == (5, {M2: 5, M3: 7})


def test_the_catch_up_refusal_names_the_way_out_for_a_release_that_fails_on_a_module():
    """N2-F1: r6 holds a correct migration and fails in its transaction on a module (21). The pull
    request that only fixes the module is r7; it is refused, and r6 fails again each time. The way
    that works is in the message: withdraw the migration and replace it in that pull request."""
    b = bundle(mig(M1, added=6), modules=[proc("usp_fixed")])
    error = refusal(pending_work, b, recorded(seq=5), 50)
    assert (error.reason_code, error.detail["promote"]) == ("CATCHUP_REQUIRED", 6)
    assert "withdraw the migration" in error.message and "REPLACEMENT_EDGE" in error.message


def test_open_module_work_of_an_earlier_release_never_needs_a_catch_up():
    """The migrations of r6 are applied here (their steps are ok) and only its module work is
    open: the unit of r6 committed and its run did not end ok. r7 is planned; it adds its own
    migration and sends the modules as its files have them."""
    b = bundle(mig(M1, added=6), mig(M2, added=7), modules=[proc("usp_a", "SELECT 7;")])
    a = key("PROCEDURE", "usp_a")
    st = recorded(steps=applied(b, M1), objects={a: row(a, "old", source="1" * 64)}, seq=5)
    for at in (st, dataclasses.replace(st, committed_release_seq=6)):
        work = pending_work(b, at, 50)
        assert work.outcome == WORK and [m.file for m in work.pending] == [M2]
        assert [change.key for change in work.module_changes] == [a]


def test_a_release_without_a_migration_does_not_apply_the_migration_of_an_earlier_release():
    b = bundle(mig(M1, added=5), modules=[proc("usp_a")], seq=7)
    error = refusal(pending_work, b, recorded(seq=4), 100)
    assert (error.reason_code, error.detail["promote"]) == ("CATCHUP_REQUIRED", 5)


def test_a_release_applies_the_migrations_that_it_added_however_many():
    b = bundle(mig(M1, added=4), mig(M2, added=7), mig(M3, added=7), seq=7)
    assert [m.file for m in pending_work(b, recorded(steps=applied(b, M1), seq=6), 100).pending] == [M2, M3]


def test_a_manifest_that_does_not_name_the_release_of_a_pending_migration_is_refused():
    b = bundle(mig(M1))
    broken = Bundle(dataclasses.replace(b.manifest, chain_added_in={}), b.files)
    assert refusal(pending_work, broken, recorded(), 100).reason_code == "BUNDLE_INVALID"


@pytest.mark.parametrize("added_in", [0, -1, 8])
def test_a_manifest_that_adds_a_migration_outside_its_releases_is_refused(added_in):
    """TQ-05: release 7 cannot hold a migration of release 0 or of release 8. A7 would count with it."""
    b = bundle(mig(M1), seq=7)
    broken = Bundle(dataclasses.replace(b.manifest, chain_added_in={M1: added_in}), b.files)
    error = refusal(pending_work, broken, recorded(seq=6), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "BUNDLE_INVALID")
    assert error.detail == {"migration": M1}
    for sound in (1, 7):
        fine = Bundle(dataclasses.replace(b.manifest, chain_added_in={M1: sound}), b.files)
        assert pending_work(fine, recorded(seq=sound - 1), 100).outcome == WORK


# ------------------------------------------------------------------ A7 with withdraw-and-replace
def withdrawn_in_r4(m3_mode: str = "tx") -> Bundle:
    """r1 added M1. r2 added M2. r3 added M3. r4 withdraws M2 and adds M4, which replaces it."""
    return bundle(
        mig(M1, added=1),
        mig(M2, added=2, withdrawn=True),
        mig(M3, added=3, mode=m3_mode),
        mig(M4, added=4, replaces=M2),
        seq=4,
    )


def test_a_database_that_never_applied_a_withdrawn_migration_catches_up_in_one_release():
    """r2 failed on this database, so M2 was never applied here. r2 and r3 can no longer run on it:
    r2 holds M2, and r3 needs r2 first. r4 is the first release that this database can take."""
    b = withdrawn_in_r4()
    work = pending_work(b, recorded(steps=applied(b, M1), seq=1), 100)
    assert work.outcome == WORK and len(work.units) == 1 and work.units[0].kind == "tx"
    # effective order: the replacement runs at the position of the migration that it replaces
    assert [m.file for m in work.pending] == [M4, M3]
    assert ids(work) == [f"{M4}#1", f"{M3}#1"]
    (note,) = work.warnings
    assert "catch-up in one release" in note and M3 in note and M2 in note and "r4" in note


def test_a_release_before_the_withdrawn_migration_is_still_caught_up_first():
    """The rule opens only what cannot run one by one. r2 has no withdrawn migration: it runs first.
    The database records r1 (a release with modules only), so this is not its first deploy."""
    b = bundle(
        mig(M1, added=2),
        mig(M2, added=3, withdrawn=True),
        mig(M3, added=4),
        mig(M4, added=5, replaces=M2),
        seq=5,
    )
    error = refusal(pending_work, b, recorded(seq=1), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "CATCHUP_REQUIRED")
    assert "promote r2 first" in error.message
    assert (error.detail["promote"], error.detail["pending"]) == (2, {M1: 2, M3: 4, M4: 5})


def test_with_no_unapplied_withdrawn_migration_pending_migrations_of_two_releases_need_a_catch_up():
    # this database applied M2 before it was withdrawn: its replacement is not pending, and the
    # releases r3 and r5 can run here one by one
    m5 = "0005__e.sql"
    b = bundle(
        mig(M1, added=1),
        mig(M2, added=2, withdrawn=True),
        mig(M3, added=3),
        mig(M4, added=4, replaces=M2),
        mig(m5, added=5),
        seq=5,
    )
    error = refusal(pending_work, b, recorded(steps=applied(b, M1, M2), seq=2), 100)
    assert (error.reason_code, error.detail["promote"]) == ("CATCHUP_REQUIRED", 3)
    assert error.detail["pending"] == {M3: 3, m5: 5}
    # and the same chain on a database that never applied M2 catches up with r5
    work = pending_work(b, recorded(steps=applied(b, M1), seq=1), 100)
    assert [m.file for m in work.pending] == [M4, M3, m5]


def test_after_the_one_catch_up_the_rule_is_whole_again_although_the_withdrawn_migration_has_no_step():
    """M2 never gets a step on this database. If the missing step alone opened the rule, every
    later plan of this database could join the migrations of several releases for good."""
    m5, m6 = "0005__e.sql", "0006__f.sql"
    b = bundle(
        mig(M1, added=1),
        mig(M2, added=2, withdrawn=True),
        mig(M3, added=3),
        mig(M4, added=4, replaces=M2),
        mig(m5, added=5),
        mig(m6, added=6),
        seq=6,
    )
    caught_up = recorded(steps=applied(b, M1, M4, M3), seq=4)  # r4 took M4 and M3 in one unit
    error = refusal(pending_work, b, caught_up, 100)
    assert (error.reason_code, error.detail["promote"]) == ("CATCHUP_REQUIRED", 5)
    assert error.detail["pending"] == {m5: 5, m6: 6}
    # the record of the release of the withdrawn migration itself closes the rule too
    at_r2 = recorded(steps=applied(b, M1), seq=2)
    assert refusal(pending_work, b, at_r2, 100).detail["promote"] == 3


def test_a_withdrawn_migration_that_has_a_step_here_does_not_open_the_rule():
    """A DBA applied the change of M2 by hand and ran resolve --mark-applied: no release is recorded
    for it, and r3 can be promoted here, because this database holds what r3 takes for applied."""
    b = withdrawn_in_r4()
    error = refusal(pending_work, b, recorded(steps=applied(b, M1, M2), seq=1), 100)
    assert (error.reason_code, error.detail["promote"], error.detail["pending"]) == (
        "CATCHUP_REQUIRED",
        3,
        {M3: 3},
    )


def test_the_manifest_must_name_the_release_of_a_withdrawn_migration_that_was_never_applied():
    b = withdrawn_in_r4()
    without = {file: seq for file, seq in b.manifest.chain_added_in.items() if file != M2}
    broken = Bundle(dataclasses.replace(b.manifest, chain_added_in=without), b.files)
    error = refusal(pending_work, broken, recorded(steps=applied(b, M1), seq=1), 100)
    assert (error.reason_code, error.detail["migration"]) == ("BUNDLE_INVALID", M2)


def test_a_catch_up_over_a_withdrawn_migration_does_not_join_a_nontx_migration_with_others():
    """A8 stays: a non-transactional migration runs alone. No earlier release can be promoted here,
    so the message does not name one."""
    b = withdrawn_in_r4(m3_mode="nontx")
    error = refusal(pending_work, b, recorded(steps=applied(b, M1), seq=1), 100)
    assert (error.reason_code, error.detail["migration"]) == ("NONTX_NOT_ALONE", M3)
    assert error.detail["also_pending"] == [M4] and "promote" not in error.message


def test_a_catch_up_in_one_release_is_in_the_notes_of_the_plan_and_runs_as_one_unit():
    b = withdrawn_in_r4()
    st = recorded(steps=applied(b, M1), seq=1)
    result = planned(b, st)
    assert (result.outcome, result.pending, len(result.units)) == (WORK, True, 1)
    assert [step.id for step in result.units[0].steps] == [f"{M4}#1", f"{M3}#1"]
    assert len([note for note in result.notes if "catch-up in one release" in note]) == 1


# ------------------------------------------------------------------ pending_work: nontx (A8)
NONTX = mig(M2, "CREATE INDEX [IX_a] ON [sales].[Order] ([c]) WITH (ONLINE = ON);", mode="nontx")


def test_a_nontx_migration_together_with_a_module_change_is_refused():
    b = bundle(mig(M1), NONTX, modules=[proc("usp_a")])
    error = refusal(pending_work, b, recorded(steps=applied(b, M1), seq=6), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "NONTX_NOT_ALONE")
    assert error.detail == {"migration": M2, "also_pending": [key("PROCEDURE", "usp_a")]}
    assert "promote r6 first" in error.message


def test_a_nontx_migration_together_with_another_migration_or_a_drop_is_refused():
    with_migration = bundle(mig(M1), NONTX)
    assert refusal(pending_work, with_migration, recorded(), 100).detail["also_pending"] == [M1]
    old = key("PROCEDURE", "usp_old")
    with_drop = bundle(NONTX, tombstones=[old])
    error = refusal(pending_work, with_drop, recorded(objects={old: row(old, "x")}), 100)
    assert (error.reason_code, error.detail["also_pending"]) == ("NONTX_NOT_ALONE", [old])


def test_a_new_database_with_a_nontx_migration_in_the_chain_is_told_the_release_to_start_from():
    """N2-F4: a database after setup-sql takes the whole chain in one unit, which a non-transactional
    migration cannot join. The way that works starts at the release before the one that added it."""
    b = bundle(mig(M1, added=3), mig(M2, mode="nontx", added=5), mig(M3, added=7))
    error = refusal(pending_work, b, recorded(seq=0), 50)
    assert error.reason_code == "NONTX_NOT_ALONE"
    assert "promote r4 first" in error.message and "each later release in its turn" in error.message
    # a database that records a release keeps the message that it had
    behind = refusal(
        pending_work, bundle(mig(M1, mode="nontx"), modules=[proc("usp_a")]), recorded(seq=6), 50
    )
    assert "promote r6 first" in behind.message and "records no release" not in behind.message


def test_a_nontx_migration_alone_is_one_unit_with_one_batch():
    b = bundle(mig(M1), NONTX)
    work = pending_work(b, recorded(steps=applied(b, M1), seq=6), 100)
    assert [(unit.kind, [step.kind for step in unit.steps]) for unit in work.units] == [("nontx", ["batch"])]


# ------------------------------------------------------------------ pending_work: modules and drops
def test_a_module_is_sent_when_its_checksum_differs_is_null_or_has_no_row():
    files = [proc("usp_changed", "SELECT 2;"), proc("usp_new"), proc("usp_null"), proc("usp_same")]
    objects = {
        key("PROCEDURE", "usp_changed"): row(key("PROCEDURE", "usp_changed"), "old", source="1" * 64),
        key("PROCEDURE", "usp_null"): row(key("PROCEDURE", "usp_null"), "old", source=None),
        key("PROCEDURE", "usp_same"): row(key("PROCEDURE", "usp_same"), files[3][1]),
    }
    work = pending_work(bundle(modules=files), recorded(objects=objects, seq=7), 100)
    assert {change.key: change.action for change in work.module_changes} == {
        key("PROCEDURE", "usp_changed"): "alter",
        key("PROCEDURE", "usp_new"): "create",
        key("PROCEDURE", "usp_null"): "overwrite",
    }
    assert work.module_changes[0].checksum == checksum(files[0][1].encode())


def test_a_drop_is_planned_only_for_a_recorded_tombstoned_module():
    managed, never_here, gone = key("PROCEDURE", "usp_old"), key("VIEW", "vw_never"), key("VIEW", "vw_gone")
    b = bundle(tombstones=[managed, never_here, gone])
    objects = {managed: row(managed, "x"), gone: row(gone, "x", status="dropped")}
    work = pending_work(b, recorded(objects=objects, seq=7), 100)
    assert work.drops == (managed,)
    assert ids(work) == [f"drop:{managed}"]
    assert work.destructive == (Destructive("DROP_MODULE", managed, "replaced"),)


def test_a_managed_module_with_neither_file_nor_tombstone_is_refused_not_dropped():
    orphan = key("PROCEDURE", "usp_orphan")
    error = refusal(
        pending_work, bundle(modules=[proc("usp_a")]), recorded(objects={orphan: row(orphan, "x")}), 100
    )
    assert (error.exit_code, error.reason_code, error.detail) == (
        Exit.REFUSED,
        "MODULE_ORPHAN",
        {"objects": [orphan]},
    )


def test_a_row_with_the_status_dropped_is_not_a_managed_module():
    again = key("PROCEDURE", "usp_a")
    objects = {
        again: row(again, "x", status="dropped"),
        key("VIEW", "vw_x"): row(key("VIEW", "vw_x"), "x", status="dropped"),
    }
    work = pending_work(bundle(modules=[proc("usp_a")]), recorded(objects=objects), 100)
    assert [(change.key, change.action) for change in work.module_changes] == [(again, "create")]


def test_a_module_with_a_file_and_a_tombstone_is_refused():
    b = bundle(modules=[proc("usp_a")], tombstones=[key("PROCEDURE", "usp_a")])
    assert refusal(pending_work, b, recorded(), 100).reason_code == "TOMBSTONE_CONFLICT"


def test_two_module_files_for_one_object_name_are_refused():
    upper = ("schema/views/sales.USP_A.sql", "CREATE OR ALTER VIEW [sales].[USP_A] AS SELECT 1 AS [x];\n")
    error = refusal(pending_work, bundle(modules=[proc("usp_a"), upper]), recorded(), 100)
    assert (error.reason_code, error.detail["path"]) == ("MODULE_INVALID", "schema/views/sales.USP_A.sql")


# ------------------------------------------------------------------ pending_work: the unit of work
def test_a_release_is_one_transaction_migrations_then_modules_then_drops_then_the_read_back():
    files = [
        proc("usp_a", "SELECT [t] FROM [sales].[vw_a_top];"),
        view("vw_a_top", "SELECT [t] FROM [sales].[vw_base]"),  # sorts first, needs vw_base
        view("vw_base", "SELECT [sales].[fn_tax]() AS [t]"),
        func("fn_tax"),
    ]
    old = key("PROCEDURE", "usp_old")
    two_batches = mig(
        M1, "ALTER TABLE [sales].[Order] ADD [a] int NULL;", "CREATE TABLE [sales].[T] ([a] int);"
    )
    b = bundle(two_batches, mig(M2), modules=files, tombstones=[old])
    work = pending_work(b, recorded(objects={old: row(old, "x")}), 100)
    assert (work.outcome, work.first_converge, [unit.kind for unit in work.units]) == (WORK, False, ["tx"])
    deployed = [
        key("FUNCTION", "fn_tax"),
        key("VIEW", "vw_base"),
        key("VIEW", "vw_a_top"),
        key("PROCEDURE", "usp_a"),
    ]
    assert ids(work) == [
        f"{M1}#1",
        f"{M1}#2",
        f"{M2}#1",
        *(f"module:{object_key}" for object_key in deployed),
        f"drop:{old}",
        "readback",
    ]
    readback = work.units[0].steps[-1]
    assert (readback.keys, readback.all_managed) == (tuple(sorted(deployed)), False)
    first = work.units[0].steps[0]
    assert (first.kind, first.migration, first.batch, first.batch_kind, first.line) == (
        "batch",
        M1,
        1,
        "model",
        1,
    )
    assert (first.path, first.sha256) == (f"migrations/{M1}", chain.file_sha256(b.files[f"migrations/{M1}"]))
    assert work.units[0].steps[1].line == 5


def test_a_module_sent_by_deploy_module_is_not_sent_twice():
    files = [func("fn_tax"), proc("usp_a")]
    tax = key("FUNCTION", "fn_tax")
    b = bundle(
        mig(
            M1,
            "CREATE TABLE [sales].[T] ([a] int NULL);",
            "-- azsqlcd:deploy-module [sales].[fn_tax]\nALTER TABLE [dbo].[T] ADD [t] AS [sales].[fn_tax]();",
            "-- azsqlcd:deploy-module [Sales].[FN_TAX]\nALTER TABLE [dbo].[T] ADD [u] AS [sales].[fn_tax]();",
        ),
        modules=files,
    )
    work = pending_work(b, recorded(), 100)
    assert ids(work) == [
        f"{M1}#1",
        f"{M1}@5:deploy:{tax}",  # at its written position: above the batch that needs it
        f"{M1}#2",
        f"{M1}#3",
        f"module:{key('PROCEDURE', 'usp_a')}",
        "readback",
    ]
    sent = [step for step in work.units[0].steps if step.object_key == tax]
    assert [(step.kind, step.path, step.sha256, step.migration, step.line) for step in sent] == [
        ("deploy_module", files[0][0], checksum(files[0][1].encode()), M1, 5)
    ]
    assert [change.key for change in work.module_changes] == [tax, key("PROCEDURE", "usp_a")]


def test_deploy_module_sends_nothing_when_the_database_holds_the_text():
    path, text = func("fn_tax")
    asks = mig(M1, "-- azsqlcd:deploy-module [sales].[fn_tax]\nALTER TABLE [sales].[T] ADD [x] int NULL;")
    b = bundle(asks, modules=[(path, text)])
    tax = key("FUNCTION", "fn_tax")
    work = pending_work(b, recorded(objects={tax: row(tax, text)}, seq=6), 100)
    assert ids(work) == [f"{M1}#1"]


def test_an_unbind_stands_above_its_batch_once_and_the_file_binds_the_module_again():
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[T]", " WITH SCHEMABINDING")
    bound = key("VIEW", "vw_bound")
    b = bundle(
        mig(
            M1,
            "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [a] bigint NULL;",
            "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [b] bigint NULL;",
        ),
        modules=[(path, text)],
    )
    work = pending_work(b, recorded(objects={bound: row(bound, text, schema_bound=True)}, seq=6), 100)
    assert ids(work) == [f"{M1}@3:unbind:{bound}", f"{M1}#1", f"{M1}#2", f"module:{bound}", "readback"]
    assert work.unbinds == (bound,)
    assert [(change.key, change.action) for change in work.module_changes] == [(bound, "rebind")]
    assert work.destructive == ()


@pytest.mark.parametrize(
    "directive, problem",
    [
        ("unbind [sales].[vw_unmanaged]", "names no managed module"),
        ("deploy-module [sales].[fn_no_file]", "names no module file"),
    ],
)
def test_a_directive_that_names_no_module_is_refused(directive, problem):
    b = bundle(
        mig(M1, f"-- azsqlcd:{directive}\nALTER TABLE [sales].[T] ADD [x] int NULL;"), modules=[proc("usp_a")]
    )
    error = refusal(pending_work, b, recorded(), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "DIRECTIVE_UNRESOLVED")
    assert problem in error.message
    assert (error.detail["migration"], error.detail["line"]) == (M1, 3)


def test_an_unbind_that_names_two_managed_modules_is_refused():
    """TQ-05: one schema is one namespace for every kind of module, and the directive gives no
    kind. With two managed rows of that name it cannot say which row loses its source."""
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[T]", " WITH SCHEMABINDING")
    bound, stale = key("VIEW", "vw_bound"), key("PROCEDURE", "VW_BOUND")
    unbind = "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [a] bigint NULL;"
    b = bundle(mig(M1, unbind), modules=[(path, text)], tombstones=[stale])
    objects = {bound: row(bound, text, schema_bound=True), stale: row(stale, "x")}
    error = refusal(pending_work, b, recorded(objects=objects, seq=6), 100)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "DIRECTIVE_UNRESOLVED")
    assert "more than one managed module" in error.message
    assert (error.detail["migration"], error.detail["line"]) == (M1, 3)


def test_a_nontx_migration_with_an_unbind_is_not_alone():
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[T]", " WITH SCHEMABINDING")
    bound = key("VIEW", "vw_bound")
    build = mig(
        M1, "-- azsqlcd:unbind [sales].[vw_bound]\nCREATE INDEX [IX] ON [sales].[T] ([a]);", mode="nontx"
    )
    b = bundle(build, modules=[(path, text)])
    error = refusal(
        pending_work, b, recorded(objects={bound: row(bound, text, schema_bound=True)}, seq=6), 100
    )
    assert (error.reason_code, error.detail["also_pending"]) == ("NONTX_NOT_ALONE", [bound])


def test_a_data_or_raw_batch_makes_the_read_back_cover_every_managed_object():
    b = bundle(mig(M1, "-- azsqlcd:data\nUPDATE [sales].[Order] SET [c] = 1 WHERE [c] IS NULL;"))
    work = pending_work(b, recorded(), 100)
    assert [(step.kind, step.batch_kind, step.keys, step.all_managed) for step in work.units[0].steps] == [
        ("batch", "data", (), False),
        ("readback", None, (), True),
    ]
    assert [step.kind for step in pending_work(bundle(mig(M1)), recorded(), 100).units[0].steps] == ["batch"]


def test_the_first_converge_runs_in_dependency_ordered_chunks_each_with_its_read_back():
    files = [proc("usp_a"), view("vw_a", "SELECT [sales].[fn_z]() AS [t]"), func("fn_z")]
    keys = [key("FUNCTION", "fn_z"), key("VIEW", "vw_a"), key("PROCEDURE", "usp_a")]  # deploy order
    objects = {object_key: row(object_key, "live text", source=None) for object_key in keys}
    work = pending_work(bundle(modules=files), recorded(objects=objects, seq=3), 2)
    assert work.first_converge
    assert [(unit.kind, [step.id for step in unit.steps]) for unit in work.units] == [
        ("modules_chunk", [f"module:{keys[0]}", f"module:{keys[1]}", "readback"]),
        ("modules_chunk", [f"module:{keys[2]}", "readback"]),
    ]
    assert [unit.steps[-1].keys for unit in work.units] == [tuple(sorted(keys[:2])), (keys[2],)]
    assert [(item.code, item.object) for item in work.destructive] == [("OVERWRITE_MODULE", k) for k in keys]


def test_only_a_first_converge_is_chunked_every_other_release_is_one_transaction():
    files = [proc("usp_a"), proc("usp_b"), proc("usp_c")]
    keys = [key("PROCEDURE", name) for name in ("usp_a", "usp_b", "usp_c")]
    unknown_source = {object_key: row(object_key, "live text", source=None) for object_key in keys}
    one_known = unknown_source | {keys[0]: row(keys[0], "live text", source="1" * 64)}
    old = key("PROCEDURE", "usp_old")
    for b, objects in (
        (bundle(modules=files), one_known),  # a module with a recorded checksum
        (bundle(modules=files), dict(list(unknown_source.items())[:2])),  # a module with no row
        (bundle(mig(M1), modules=files), unknown_source),  # a pending migration
        (bundle(modules=files, tombstones=[old]), unknown_source | {old: row(old, "x")}),  # a drop
    ):
        work = pending_work(b, recorded(objects=objects, seq=3), 2)
        assert (work.first_converge, [unit.kind for unit in work.units]) == (False, ["tx"])


def test_the_destructive_list_holds_the_allow_items_the_drops_and_the_overwrites():
    old, adopted = key("PROCEDURE", "usp_old"), key("PROCEDURE", "usp_adopted")
    b = bundle(
        mig(
            M1,
            "-- azsqlcd:allow DROP_COLUMN [sales].[Order].[Stat] reason: moved to Status\n"
            "ALTER TABLE [sales].[Order] DROP COLUMN [Stat];",
        ),
        modules=[proc("usp_adopted")],
        tombstones=[old],
    )
    objects = {old: row(old, "x"), adopted: row(adopted, "live text", source=None)}
    work = pending_work(b, recorded(objects=objects, seq=6), 100)
    assert [(item.code, item.object, item.migration, item.line) for item in work.destructive] == [
        ("DROP_COLUMN", "[sales].[Order].[Stat]", M1, 3),
        ("DROP_MODULE", old, None, None),
        ("OVERWRITE_MODULE", adopted, None, None),
    ]
    assert work.destructive[0].reason == "moved to Status"


def test_a_schema_bound_dependant_is_dropped_before_the_module_it_binds():
    top, base = key("VIEW", "vw_a_top"), key("VIEW", "vw_base")
    objects = {
        top: row(
            top,
            "CREATE VIEW [sales].[vw_a_top] WITH SCHEMABINDING AS SELECT [x] FROM [sales].[vw_base];",
            schema_bound=True,
        ),
        base: row(
            base, "CREATE VIEW [sales].[vw_base] WITH SCHEMABINDING AS SELECT 1 AS [x];", schema_bound=True
        ),
    }
    work = pending_work(bundle(tombstones=[base, top]), recorded(objects=objects, seq=6), 100)
    assert work.drops == (top, base)
    # with no binding the order is the reverse of the deploy order
    unbound = {
        k: dataclasses.replace(r, capture=r.capture | {"is_schema_bound": False}) for k, r in objects.items()
    }
    assert pending_work(bundle(tombstones=[base, top]), recorded(objects=unbound, seq=6), 100).drops == (
        base,
        top,
    )


def test_a_pending_migration_must_be_the_file_that_its_chain_line_names():
    b = bundle(mig(M1))
    path = f"migrations/{M1}"
    changed = Bundle(b.manifest, b.files | {path: b.files[path] + b"-- changed after the chain line\n"})
    missing = Bundle(b.manifest, {p: data for p, data in b.files.items() if p != path})
    other_mode = bundle(dataclasses.replace(mig(M1), mode="nontx"))
    for broken in (changed, missing, other_mode):
        error = refusal(pending_work, broken, recorded(), 100)
        assert (error.exit_code, error.reason_code, error.detail) == (
            Exit.REFUSED,
            "CHAIN_INVALID",
            {"migration": M1},
        )


def test_a_cycle_of_procedures_is_planned_with_a_warning():
    files = [proc("usp_a", "EXEC [sales].[usp_b];"), proc("usp_b", "EXEC [sales].[usp_a];")]
    work = pending_work(bundle(modules=files), recorded(), 100)
    assert ids(work)[:2] == [f"module:{key('PROCEDURE', 'usp_a')}", f"module:{key('PROCEDURE', 'usp_b')}"]
    assert len(work.warnings) == 1 and "cycle" in work.warnings[0]


def test_step_texts_are_the_exact_batches_and_module_files_of_the_bundle():
    path, text = proc("usp_a", "SELECT N'?', '{d 1}';")
    second = "-- azsqlcd:data\nUPDATE [sales].[Order] SET [c] = 1 WHERE [c] IS NULL;"
    first = "ALTER TABLE [sales].[Order] ADD [c] int NULL;"
    b = bundle(mig(M1, first, second), modules=[(path, text)])
    work = pending_work(b, recorded(), 100)
    texts = plan.step_texts(b, work.units[0])
    assert texts == {
        # the header lines are comments of the first batch
        f"{M1}#1": f"-- azsqlcd:migration 0001__a\n-- azsqlcd:mode tx\n{first}",
        f"{M1}#2": second,
        f"module:{key('PROCEDURE', 'usp_a')}": text,
    }


# ------------------------------------------------------------------ compute_plan: a database
def database(
    st: State,
    *,
    live: Sequence[tuple] | None = None,
    user_objects: Sequence[tuple[str, str, str]] | None = None,
    facts: tuple = FACTS,
    indexed_views: Sequence[str] = (),
) -> FakeSession:
    """A session that answers the read-only queries of a plan. live None: as the tool recorded it.

    indexed_views: names of the views that have an index in the catalog; no other object has one.
    """
    if live is None:
        live = [
            live_row(
                object_key,
                r.capture["definition"],
                ansi=r.capture["uses_ansi_nulls"],
                quoted=r.capture["uses_quoted_identifier"],
                schema_bound=r.capture["is_schema_bound"],
            )
            for object_key, r in st.objects.items()
            if r.status == "managed" and "definition" in r.capture
        ]
    if user_objects is None:
        user_objects = [(r[1], r[2], r[3]) for r in live]
    runs: list[RunRow] = [*st.open_runs, *([st.latest_ok_run] if st.latest_ok_run else [])]
    if st.committed_release_seq:  # a deploy that committed a unit of work and did not end ok
        seq = st.committed_release_seq
        runs.append(RunRow(90, "deploy", "failed", 1, seq, COMMIT, "2026-10-02T10:00:00.000"))
    runs.sort(key=lambda r: r.run_id)
    db = FakeSession()
    db.respond("azsqlcd:fence_facts", [[facts]])
    db.respond("azsqlcd:read_state.tables", [[(table, 1) for table in state.TABLES]])
    db.respond("azsqlcd:read_state.meta", [[dataclasses.astuple(st.meta)]])
    db.respond("azsqlcd:read_state.runs", [[dataclasses.astuple(r) for r in runs]])
    db.respond("azsqlcd:read_state.steps", [[dataclasses.astuple(step) for step in st.steps]])
    db.respond(
        "azsqlcd:read_state.objects",
        [
            [
                (
                    k,
                    r.status,
                    r.source_sha256,
                    r.capture_format,
                    state.capture_json(r.capture),
                    r.catalog_sha256,
                )
                for k, r in sorted(st.objects.items())
            ]
        ],
    )
    db.respond("azsqlcd:capture_modules", [list(live)])
    db.respond("azsqlcd:list_user_objects", [list(user_objects)])
    db.respond("azsqlcd:service_objective", [[("GP_S_Gen5_2",)]])
    db.respond(
        "azsqlcd:has_index", lambda batch: [[(int(any(f"[{name}]" in batch for name in indexed_views)),)]]
    )
    return db


def planned(b: Bundle, st: State, db: FakeSession | None = None, *, env: str = "dev", **options: Any) -> Plan:
    options = {"tool_version": "0.1.0", "tool_digest": TOOL_DIGEST} | options
    config = options.pop("config", CONFIG)
    return compute_plan(b, config, env, f"sales-{env}", db or database(st), **options)


PARSEONLY_IS_OFF = "/* azsqlcd:parseonly_off */ SELECT @@SPID;"


def parse_only_session() -> FakeSession:
    """A second session that behaves as the engine does under SET PARSEONLY ON."""
    second = FakeSession()
    second.fail_on("SELECT FROM;", sql_error("Incorrect syntax near the keyword 'FROM'.", number=156))
    return second


def a_release() -> tuple[Bundle, State]:
    """One migration, one changed module, one new module, one drop; the database is one release behind."""
    changed, old = key("PROCEDURE", "usp_changed"), key("PROCEDURE", "usp_old")
    b = bundle(
        mig(M1, added=6),
        mig(
            M2,
            "ALTER TABLE [sales].[Order] ADD [d] int NULL;",
            "-- azsqlcd:data\n"
            "-- azsqlcd:allow DATA_NO_WHERE [sales].[Order] reason: backfill of a new column\n"
            "UPDATE [sales].[Order] SET [d] = 0;",
        ),
        modules=[proc("usp_changed", "SELECT 2;"), view("vw_new")],
        tombstones=[old],
    )
    objects = {
        changed: row(changed, "CREATE PROCEDURE [sales].[usp_changed] AS SELECT 1;", source="1" * 64),
        old: row(old, "CREATE PROCEDURE [sales].[usp_old] AS SELECT 0;"),
    }
    return b, recorded(steps=applied(b, M1), objects=objects, seq=6)


# ------------------------------------------------------------------ compute_plan: fence, state, liveness
@pytest.mark.parametrize(
    "facts, reason_code",
    [
        ((3, "READ_WRITE", "sales", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_ENGINE_EDITION"),
        ((5, "READ_ONLY", "sales", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_READ_ONLY"),
        ((5, "READ_WRITE", "master", "SQL_Latin1_General_CP1_CI_AS", 0), "FENCE_DB_NAME"),
        ((5, "READ_WRITE", "sales", "Latin1_General_100_CS_AS", 1), "FENCE_CASE_SENSITIVE"),
    ],
)
def test_the_fence_refuses_a_database_the_tool_is_not_made_for_before_any_other_read(facts, reason_code):
    b, st = a_release()
    db = database(st, facts=facts)
    error = refusal(planned, b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, reason_code)
    # the lock test, which waits for nothing, and the fence facts
    assert [len(db.sent(tag)) for tag in ("azsqlcd:applock_test", "azsqlcd:fence_facts")] == [1, 1]
    assert len(db.batches) == 2


def test_the_name_of_the_database_is_compared_without_case():
    b, st = a_release()
    assert planned(b, st, database(st, facts=(5, "READ_WRITE", "SALES", FACTS[3], 0))).outcome == WORK


@pytest.mark.parametrize("bound_to", [{"project": "billing"}, {"env": "prod"}])
def test_a_database_that_is_bound_to_another_project_or_environment_is_refused(bound_to):
    b = bundle(mig(M1))
    db = database(recorded(**bound_to))
    error = refusal(planned, b, recorded(), db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "FENCE_META_MISMATCH")
    assert db.sent("azsqlcd:capture_modules") == []


@pytest.mark.parametrize(
    "env, target, reason_code",
    [("test", "sales-test", "ENV_NOT_CONFIGURED"), ("dev", "sales-prod", "TARGET_NOT_CONFIGURED")],
)
def test_a_plan_is_made_only_for_a_target_of_the_environment(env, target, reason_code):
    db = FakeSession()
    error = refusal(
        compute_plan, bundle(), CONFIG, env, target, db, tool_version="0.1.0", tool_digest=TOOL_DIGEST
    )
    assert (error.exit_code, error.reason_code, db.batches) == (Exit.REFUSED, reason_code, [])


def test_a_database_without_the_state_schema_is_refused_and_nothing_is_created():
    db = FakeSession()
    db.respond("azsqlcd:fence_facts", [[FACTS]])
    error = refusal(planned, bundle(mig(M1)), recorded(), db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "STATE_MISSING")
    assert len(db.batches) == 3  # the lock test, the fence facts, the tables of the state


def test_a_live_run_locks_the_plan_out_unless_the_caller_holds_the_lock():
    b, st = a_release()
    db = database(st)
    db.applock_result = -1  # another session holds the deploy lock
    error = refusal(planned, b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.LOCKED, "RUN_LIVE")
    # Live acceptance: the catalog reads of a plan wait for the schema locks of the live run and
    # end as lock timeout 1222. So the lock is asked before every other read.
    assert len(db.sent("azsqlcd:applock_test")) == 1
    assert len(db.batches) == 1

    under_lock = database(st)
    assert planned(b, st, under_lock, lock_held=True).outcome == WORK
    assert under_lock.sent("azsqlcd:applock_test") == []


def test_a_running_row_with_a_free_lock_is_a_dead_run_that_is_reported_not_refused():
    b, st = a_release()
    stale = dataclasses.replace(st, open_runs=(run(4, "running"),))
    result = planned(b, stale)
    assert result.outcome == WORK
    assert [note for note in result.notes if note.startswith("run 4 ")] != []
    assert result.plan_sha256 == planned(b, st).plan_sha256  # the deploy reconciles it; the plan stays valid


# ------------------------------------------------------------------ compute_plan: collisions, drift, flags
@pytest.mark.parametrize(
    "live_object", [("Sales", "VW_NEW", "V"), ("sales", "vw_new", "U"), ("sales", "vw_new", "D")]
)
def test_a_create_over_an_unmanaged_name_is_refused(live_object):
    b, st = a_release()
    db = database(st, user_objects=[("sales", "usp_changed", "P"), ("sales", "usp_old", "P"), live_object])
    error = refusal(planned, b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "NAME_COLLISION")
    assert error.detail == {"objects": [key("VIEW", "vw_new")]}
    assert "resolve --adopt-module" in error.message


def test_a_touched_module_that_differs_from_its_capture_is_refused_with_property_and_hashes_only():
    b, st = a_release()
    changed, old = key("PROCEDURE", "usp_changed"), key("PROCEDURE", "usp_old")
    hand_edit = "CREATE PROCEDURE [sales].[usp_changed] AS SELECT 'edited by hand';"
    db = database(
        st, live=[live_row(changed, hand_edit), live_row(old, st.objects[old].capture["definition"])]
    )
    error = refusal(planned, b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "DRIFT_TOUCHED")
    stored = st.objects[changed].capture
    assert error.detail == {
        "objects": [
            {
                "object": changed,
                "differences": [
                    {
                        "property": "definition",
                        "stored_sha256": state.capture_sha256({"definition": stored["definition"]}),
                        "live_sha256": state.capture_sha256({"definition": hand_edit}),
                    }
                ],
            }
        ]
    }
    assert "edited by hand" not in error.message + json.dumps(error.detail)


@pytest.mark.parametrize("env", ["dev", "prod"])  # prod has drift = "block"
def test_a_tombstoned_module_that_is_gone_from_the_catalog_is_planned_and_is_not_drift(env):
    """N1-F1: a managed module that was dropped behind the tool. resolve --accept-drift refuses it
    and asks for a tombstone; the release with that tombstone must then be planned, or the state
    has no way out. The runner marks the row dropped and sends no statement (A18)."""
    b, st = a_release()
    st = dataclasses.replace(st, meta=dataclasses.replace(st.meta, environment=env))
    changed, old = key("PROCEDURE", "usp_changed"), key("PROCEDURE", "usp_old")
    db = database(st, live=[live_row(changed, st.objects[changed].capture["definition"])])
    result = planned(b, st, db, env=env)
    assert result.outcome == WORK and f"drop:{old}" in ids(result)
    assert old in [touched.key for touched in result.touched]
    assert result.drift == ()  # nothing to revert and nothing to accept: the release takes it out
    (note,) = [note for note in result.notes if old in note]
    assert "is not in the database" in note and "no statement" in note


def test_a_tombstone_in_another_letter_case_for_a_module_that_is_gone_is_planned_too():
    stored = "PROCEDURE:[Sales].[USP_Old]"
    st = recorded(objects={stored: row(stored, "CREATE PROCEDURE [Sales].[USP_Old] AS SELECT 0;")}, seq=6)
    result = planned(bundle(tombstones=[key("PROCEDURE", "usp_old")]), st, database(st, live=[]))
    assert result.outcome == WORK and result.drift == ()


def test_a_module_to_change_that_is_gone_from_the_catalog_is_refused_and_the_message_names_the_tombstone():
    """--accept-drift does not take an object that is gone, so the message must not name it."""
    b, st = a_release()
    changed, old = key("PROCEDURE", "usp_changed"), key("PROCEDURE", "usp_old")
    db = database(st, live=[live_row(old, st.objects[old].capture["definition"])])
    error = refusal(planned, b, st, db)
    assert error.reason_code == "DRIFT_TOUCHED"
    assert error.detail["objects"][0]["object"] == changed
    assert {d["live_sha256"] for d in error.detail["objects"][0]["differences"]} == {None}
    assert "is not in the database" in error.message and chain.TOMBSTONES_PATH in error.message
    # the action that resolve refuses for an object that is gone is not the advice
    assert "--accept-drift does not take" in error.message and "run azsqlcd resolve" not in error.message


def test_the_refusal_for_a_module_that_differs_and_exists_still_names_accept_drift():
    b, st = a_release()
    changed, old = key("PROCEDURE", "usp_changed"), key("PROCEDURE", "usp_old")
    live = [
        live_row(changed, st.objects[changed].capture["definition"], schema_bound=True),
        live_row(old, st.objects[old].capture["definition"]),
    ]
    error = refusal(planned, b, st, database(st, live=live))
    assert error.reason_code == "DRIFT_TOUCHED" and "--accept-drift" in error.message
    assert "tombstone" not in error.message


def untouched_drift(env: str) -> tuple[Bundle, State, FakeSession, str]:
    path, text = proc("usp_same")
    same = key("PROCEDURE", "usp_same")
    b = bundle(modules=[(path, text), proc("usp_new")])
    st = recorded(objects={same: row(same, text)}, seq=6, env=env)
    return b, st, database(st, live=[live_row(same, text, schema_bound=True)]), same


def test_drift_on_an_object_that_the_release_does_not_touch_is_listed():
    b, st, db, same = untouched_drift("dev")
    result = planned(b, st, db)
    assert [(drift.key, [d.property for d in drift.differences]) for drift in result.drift] == [
        (same, ["is_schema_bound"])
    ]
    assert result.outcome == WORK


def test_drift_on_any_managed_object_blocks_an_environment_with_drift_block():
    b, st, db, same = untouched_drift("prod")
    error = refusal(planned, b, st, db, env="prod")
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "DRIFT_BLOCK")
    assert [found["object"] for found in error.detail["objects"]] == [same]


def test_a_module_to_deploy_that_is_stored_with_legacy_flags_is_refused():
    path, text = proc("usp_a", "SELECT 2;")
    legacy = key("PROCEDURE", "usp_a")
    st = recorded(objects={legacy: row(legacy, "old text", source="1" * 64, quoted=False)}, seq=6)
    error = refusal(planned, bundle(modules=[(path, text)]), st)
    assert (error.exit_code, error.reason_code, error.detail) == (
        Exit.REFUSED,
        "LEGACY_FLAGS",
        {"objects": [legacy]},
    )


def test_an_unbind_of_a_module_whose_header_cannot_be_rewritten_is_refused_at_plan_time():
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[T]", " WITH SCHEMABINDING")
    bound = key("VIEW", "vw_bound")
    unbinds = mig(
        M1, "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [a] bigint;"
    )
    b = bundle(unbinds, modules=[(path, text)])
    not_bound = recorded(
        objects={bound: row(bound, "CREATE VIEW [sales].[vw_bound] AS SELECT 1 AS [a];")}, seq=6
    )
    error = refusal(planned, b, not_bound)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "UNBIND_HEADER")
    bound_live = recorded(objects={bound: row(bound, text, schema_bound=True)}, seq=6)
    assert [step.kind for step in planned(b, bound_live).units[0].steps][0] == "unbind"


def test_an_unbind_of_a_module_whose_definition_cannot_be_read_is_refused_in_the_plan():
    """TQ-05: the catalog gives no definition (an encrypted module). The runner could not write the
    ALTER without SCHEMABINDING, so the plan refuses; it does not stop on an error of its own."""
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[T]", " WITH SCHEMABINDING")
    bound = key("VIEW", "vw_bound")
    b = bundle(
        mig(
            M1, "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [a] bigint NULL;"
        ),
        modules=[(path, text)],
    )
    no_text = capture(live_row(bound, None, schema_bound=True))  # type: ignore[arg-type]
    assert no_text["definition"] is None
    st = recorded(
        objects={bound: ObjectRow("managed", None, 1, no_text, state.capture_sha256(no_text))}, seq=6
    )
    db = database(st, live=[live_row(bound, None, schema_bound=True)])  # type: ignore[arg-type]
    error = refusal(planned, b, st, db)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "UNBIND_HEADER")
    assert error.detail == {"object": bound} and "definition cannot be read" in error.message


def test_live_modules_without_a_managed_row_are_listed_as_unmanaged():
    b, st = a_release()
    live_objects = [
        ("sales", "usp_changed", "P"),
        ("sales", "usp_old", "P"),
        ("dbo", "usp_legacy", "P"),
        ("sales", "Order", "U"),  # tables are not modelled without table hooks
        ("sales", "PK_Order", "PK"),
    ]
    assert planned(b, st, database(st, user_objects=live_objects)).unmanaged == (
        key("PROCEDURE", "usp_legacy", "dbo"),
    )


# ------------------------------------------------------------------ compute_plan: the syntax check
def test_every_pending_batch_and_module_is_parsed_on_the_second_session_after_the_canary():
    b, st = a_release()
    db, second = database(st), parse_only_session()
    result = planned(b, st, db, open_second_session=lambda: second)
    texts = plan.step_texts(b, result.units[0])
    assert second.batches == ["SET PARSEONLY ON;", "SELECT 1/0;", "SELECT FROM;", *texts.values()]
    assert len(texts) == 4 and second.closed
    assert not set(texts.values()) & set(db.batches)
    assert [note for note in result.notes if "PARSEONLY" in note] == []


@pytest.mark.parametrize("statement_ran", ["raises", "returns a row"])
def test_a_canary_that_runs_refuses_the_plan_and_no_pending_batch_is_sent(statement_ran):
    b, st = a_release()
    second = parse_only_session()
    if statement_ran == "raises":
        second.fail_on("SELECT 1/0;", sql_error("Divide by zero error encountered.", number=8134))
    else:
        second.respond("SELECT 1/0;", [[(None,)]])
    error = refusal(planned, b, st, open_second_session=lambda: second)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "PARSEONLY_CANARY")
    assert second.batches == ["SET PARSEONLY ON;", "SELECT 1/0;"] and second.closed


def test_a_syntax_error_that_is_not_reported_refuses_the_plan_and_no_pending_batch_is_sent():
    b, st = a_release()
    second = FakeSession()  # accepts the batch with the syntax error
    error = refusal(planned, b, st, open_second_session=lambda: second)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "PARSEONLY_CANARY")
    assert second.batches == ["SET PARSEONLY ON;", "SELECT 1/0;", "SELECT FROM;"] and second.closed


def test_a_batch_that_does_not_parse_refuses_the_plan_with_file_and_line():
    b, st = a_release()
    second = parse_only_session()
    second.fail_on("ADD [d] int", sql_error("Incorrect syntax near 'secret value'.", number=102))
    second.fail_on("usp_changed", sql_error("Incorrect syntax near the keyword 'AS'.", number=156))
    error = refusal(planned, b, st, open_second_session=lambda: second)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "PARSEONLY_FAILED")
    assert [(f["step"], f["file"], f["line"], f["error_number"]) for f in error.detail["failures"]] == [
        (f"{M2}#1", f"migrations/{M2}", 1, 102),
        (f"module:{key('PROCEDURE', 'usp_changed')}", "schema/procedures/sales.usp_changed.sql", 1, 156),
    ]
    assert f"migrations/{M2} line 1" in error.message
    assert "secret value" not in error.message + json.dumps(error.detail)  # A25
    assert len(second.batches) == 3 + 4 and second.closed  # every batch is checked: one report, not four runs


def test_a_lost_second_session_is_not_a_verdict_on_the_text():
    b, st = a_release()
    second = parse_only_session()
    second.kill_on("ADD [d] int")
    with pytest.raises(Exception) as caught:
        planned(b, st, open_second_session=lambda: second)
    assert not isinstance(caught.value, ToolError) and second.closed


def test_text_that_can_switch_the_syntax_check_off_is_refused_before_the_second_session_opens():
    b = bundle(
        mig(
            M1,
            "ALTER TABLE [sales].[Order] ADD [c] int NULL;",
            "SET PARSEONLY OFF;\nDROP TABLE [sales].[Order];",
        )
    )
    opened: list[FakeSession] = []
    error = refusal(
        planned, b, recorded(), open_second_session=lambda: opened.append(FakeSession()) or opened[0]
    )
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "FORBIDDEN_TOKEN")
    assert (error.detail, opened) == ({"file": f"migrations/{M1}", "line": 5}, [])


def test_the_syntax_check_is_one_public_rung_for_plan_and_export():
    """parse_check on a session that the caller keeps: setting, canary, texts, and the setting back."""
    db = parse_only_session()
    bad = sql_error("Incorrect syntax near 'x'.", number=102)
    db.fail_on("SELECT x x x;", bad)
    texts = {"good": "SELECT 1;", "bad": "SELECT x x x;", "also good": "SELECT 2;"}
    assert plan.parse_check(db, texts, restore_setting=True) == {"bad": bad}
    assert db.batches == [
        "SET PARSEONLY ON;",
        "SELECT 1/0;",
        "SELECT FROM;",
        "SELECT 1;",
        "SELECT x x x;",
        "SELECT 2;",
        "SET PARSEONLY OFF;",
        PARSEONLY_IS_OFF,  # proves that the session runs statements again
    ]
    assert not db.closed
    closing = parse_only_session()
    assert plan.parse_check(closing, {"good": "SELECT 1;"}, restore_setting=False) == {}
    assert closing.batches[-1] == "SELECT 1;"  # the caller closes the session; nothing is given back


def test_a_canary_that_fails_gives_the_setting_back_and_sends_no_text():
    db = FakeSession()  # accepts the batch with the syntax error
    error = refusal(plan.parse_check, db, {"module": "SELECT 1;"}, restore_setting=True)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "PARSEONLY_CANARY")
    assert db.batches == [
        "SET PARSEONLY ON;",
        "SELECT 1/0;",
        "SELECT FROM;",
        "SET PARSEONLY OFF;",
        PARSEONLY_IS_OFF,
    ]


class StillParsesOnly(FakeSession):
    """The engine under SET PARSEONLY ON, on a session where SET PARSEONLY OFF has no effect: a
    SELECT is parsed and returns no result set (measured on Azure SQL Database), SELECT FROM raises."""

    def __init__(self, off_works: bool) -> None:
        super().__init__()
        self.off_works, self.parse_only = off_works, False
        self.fail_on("SELECT FROM;", sql_error("Incorrect syntax near the keyword 'FROM'.", number=156))

    def execute(self, batch: str) -> ResultSets:
        if batch == "SET PARSEONLY ON;":
            self.parse_only = True
        elif batch == "SET PARSEONLY OFF;" and self.off_works:
            self.parse_only = False
        elif self.parse_only and "FROM;" not in batch:
            self.batches.append(batch)
            return []
        return super().execute(batch)


def test_a_session_that_still_parses_only_after_the_setting_was_given_back_is_refused():
    """OM-11: export reads the catalog on the same session after the check. A session that still
    parses only would answer every read with nothing, and nothing would say so."""
    assert (
        plan.parse_check(StillParsesOnly(off_works=True), {"module": "SELECT 1;"}, restore_setting=True) == {}
    )
    db = StillParsesOnly(off_works=False)
    error = refusal(plan.parse_check, db, {"module": "SELECT 1;"}, restore_setting=True)
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "PARSEONLY_CANARY")
    assert "SET PARSEONLY OFF" in error.message
    assert db.batches[-2:] == ["SET PARSEONLY OFF;", PARSEONLY_IS_OFF]
    # a caller that closes the session asks for no proof
    closing = StillParsesOnly(off_works=False)
    assert plan.parse_check(closing, {"module": "SELECT 1;"}, restore_setting=False) == {}
    assert closing.sent("parseonly_off") == []


def test_a_lost_session_in_the_syntax_check_is_not_asked_to_give_the_setting_back():
    db = parse_only_session()
    db.kill_on("SELECT 1;")
    with pytest.raises(SqlError):
        plan.parse_check(db, {"module": "SELECT 1;"}, restore_setting=True)
    assert db.batches[-1] == "SELECT 1;" and db.closed


@pytest.mark.parametrize(
    ("text", "names_it"),
    [
        ("SET PARSEONLY OFF;", True),
        ("select 1; set\n parseonly on", True),
        ("SELECT parseonly FROM t;", True),  # the lexer cannot tell a name from the setting
        ("SELECT [PARSEONLY], 'SET PARSEONLY OFF' FROM t; -- SET PARSEONLY OFF", False),
        ("SELECT 1 /* PARSEONLY */;", False),
    ],
)
def test_a_text_with_the_unquoted_word_parseonly_is_never_sent_to_the_syntax_check(text, names_it):
    assert plan.names_parseonly(text) is names_it
    db = parse_only_session()
    if names_it:
        with pytest.raises(ValueError):
            plan.parse_check(db, {"safe": "SELECT 1;", "unsafe": text}, restore_setting=True)
        assert db.batches == []  # not the setting, not the canary, not the safe text
    else:
        assert plan.parse_check(db, {"text": text}, restore_setting=False) == {}


def test_the_module_files_of_a_release_are_read_by_key_and_other_files_are_left_out():
    path, text = proc("usp_a")
    files = {
        path: text.encode(),
        "schema/tables/sales.Order.sql": b"CREATE TABLE [sales].[Order] ([Id] int NOT NULL);\n",
        "migrations/0001__a.sql": b"x",
        "azsqlcd.toml": b"x",
    }
    found = plan.module_files(files)
    assert list(found) == [key("PROCEDURE", "usp_a")]
    assert (found[key("PROCEDURE", "usp_a")].path, found[key("PROCEDURE", "usp_a")].text) == (path, text)


def test_without_a_second_session_the_syntax_check_is_skipped_with_a_note():
    b, st = a_release()
    assert len([note for note in planned(b, st).notes if "PARSEONLY" in note and "skipped" in note]) == 1
    nothing_to_check = bundle(mig(M1), seq=7)
    done = recorded(steps=applied(nothing_to_check, M1), seq=7)
    assert [note for note in planned(nothing_to_check, done).notes if "PARSEONLY" in note] == []


# ------------------------------------------------------------------ compute_plan: read-only
def test_plan_sends_no_statement_that_can_change_the_database():
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[T]", " WITH SCHEMABINDING")
    bound, old = key("VIEW", "vw_bound"), key("PROCEDURE", "usp_old")
    b = bundle(
        mig(
            M1,
            "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [a] bigint NULL;",
            "-- azsqlcd:data\nDELETE FROM [sales].[T] WHERE [a] IS NULL;",
        ),
        modules=[(path, text), proc("usp_new")],
        tombstones=[old],
    )
    st = recorded(objects={bound: row(bound, text, schema_bound=True), old: row(old, "DROP me")}, seq=6)
    db, second = database(st), parse_only_session()
    result = planned(b, st, db, open_second_session=lambda: second, repo_url="https://github.example/o/r")
    assert [step.kind for step in result.units[0].steps] == [
        *("unbind", "batch", "batch", "deploy_module", "deploy_module", "drop_module", "readback")
    ]
    assert len(db.batches) >= 10
    for batch in db.batches:  # the session of the plan: SELECT only
        words = [t.text.upper() for t in lex.significant(lex.tokenize(batch)) if t.kind == "word"]
        assert words[0] == "SELECT" and not WRITES & set(words), batch
    # the second session: nothing before the setting and the canary; nothing but file text after
    assert second.batches[:3] == ["SET PARSEONLY ON;", "SELECT 1/0;", "SELECT FROM;"]
    assert second.batches[3:] == list(plan.step_texts(b, result.units[0]).values())
    assert db.trancount == 0 and second.trancount == 0


def test_a_database_that_is_past_the_release_is_exit_zero_with_a_note_and_no_catalog_read():
    b = bundle(mig(M1), modules=[proc("usp_a")], seq=5)
    st = recorded(steps=[*applied(b, M1), other_step(2, M2)], seq=8)
    db = database(st)
    result = planned(b, st, db, open_second_session=lambda: pytest.fail("no batch is sent"))
    assert (result.outcome, result.pending, result.units) == (ALREADY_PAST, False, ())
    assert result.notes[0].startswith("ALREADY_PAST")
    assert (result.release_seq, result.recorded_release_seq) == (5, 8)
    assert db.sent("azsqlcd:capture_modules") + db.sent("azsqlcd:list_user_objects") == []


def test_a_no_op_at_a_newer_release_counts_as_pending_work_for_the_gate():
    b = bundle(mig(M1), seq=7)
    to_record = planned(b, recorded(steps=applied(b, M1), seq=6))
    nothing = planned(b, recorded(steps=applied(b, M1), seq=7))
    assert (to_record.outcome, to_record.pending, to_record.units) == (RECORD, True, ())
    assert (nothing.outcome, nothing.pending, nothing.units) == (NOOP, False, ())
    assert to_record.plan_sha256 != nothing.plan_sha256


# ------------------------------------------------------------------ compute_plan: the plan and its hash
def test_the_plan_holds_the_facts_for_the_approver():
    b, st = a_release()
    result = planned(b, st, token_minutes_left=55)
    assert (result.service_objective, result.token_minutes_left) == ("GP_S_Gen5_2", 55)
    assert (result.environment, result.target_id) == ("dev", "sales-dev")
    assert (result.server, result.database) == ("sql-sales-dev.database.windows.net", "sales")
    assert (result.release_seq, result.recorded_release_seq) == (7, 6)
    assert result.manifest_digest == release.digest(b.manifest)
    assert [(a.id, a.status) for a in result.applied] == [(M1, "ok")]
    changed, old = key("PROCEDURE", "usp_changed"), key("PROCEDURE", "usp_old")
    assert [(t.key, t.catalog_sha256) for t in result.touched] == [
        (changed, st.objects[changed].catalog_sha256),
        (old, st.objects[old].catalog_sha256),
        (key("VIEW", "vw_new"), None),  # a create has no recorded capture
    ]
    assert [(item.code, item.object) for item in result.destructive] == [
        ("DATA_NO_WHERE", "[sales].[Order]"),
        ("DROP_MODULE", old),
    ]
    assert [note for note in result.notes if note.startswith("tables are not modelled")] != []


def other_recorded_release(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    latest = st.latest_ok_run
    assert latest is not None
    return b, dataclasses.replace(st, latest_ok_run=dataclasses.replace(latest, release_seq=5)), {}


def other_applied_list(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    # resolve marked the pending migration as not applied: the same work, another history
    return (
        b,
        dataclasses.replace(st, steps=(*st.steps, *applied(b, M2, status="not_applied", first_id=2))),
        {},
    )


def other_capture(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    # the same steps on another recorded definition of the touched module
    changed = key("PROCEDURE", "usp_changed")
    edited = row(changed, "CREATE PROCEDURE [sales].[usp_changed] AS SELECT 'another text';", source="1" * 64)
    return b, dataclasses.replace(st, objects=st.objects | {changed: edited}), {}


def other_recorded_source(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    # the same steps and the same capture; the deploy is now an OVERWRITE_MODULE
    changed = key("PROCEDURE", "usp_changed")
    unknown_source = dataclasses.replace(st.objects[changed], source_sha256=None)
    return b, dataclasses.replace(st, objects=st.objects | {changed: unknown_source}), {}


def other_artefact(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    files = b.files | {"onboarding/prod/export.md": b"one more line\n"}
    listed = tuple(sorted((path, hashlib.sha256(data).hexdigest()) for path, data in files.items()))
    return Bundle(dataclasses.replace(b.manifest, files=listed), files), st, {}


def other_server(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    return b, st, {"config": load_config(TOML.replace("sql-sales-dev.", "sql-sales-dev2."))}


def other_database(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    dev = 'sql-sales-dev.database.windows.net", database = "sales'
    config = load_config(TOML.replace(dev + '"', dev + '2"'))
    return b, st, {"config": config, "db": database(st, facts=(5, "READ_WRITE", "sales2", FACTS[3], 0))}


def other_tool_version(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    return b, st, {"tool_version": "0.1.1"}


def other_tool_digest(b: Bundle, st: State) -> tuple[Bundle, State, dict[str, Any]]:
    return b, st, {"tool_digest": "e" * 64}


@pytest.mark.parametrize(
    "change",
    [
        *(
            other_recorded_release,
            other_applied_list,
            other_capture,
            other_recorded_source,
        ),  # the recorded state
        other_artefact,
        *(other_server, other_database),  # the target
        *(other_tool_version, other_tool_digest),  # the tool
    ],
)
def test_the_plan_hash_changes_when_the_recorded_state_the_artefact_the_target_or_the_tool_changes(change):
    b, st = a_release()
    approved = planned(b, st)
    changed_b, changed_st, options = change(b, st)
    now = planned(changed_b, changed_st, options.pop("db", None), **options)
    assert len(approved.plan_sha256) == 64
    assert now.plan_sha256 != approved.plan_sha256
    assert [ids(now, 0)] == [ids(approved, 0)]  # the work is the same; only what the hash names differs


def test_the_plan_hash_is_the_same_in_the_plan_job_and_under_the_lock_in_the_deploy_job():
    b, st = a_release()
    in_plan_job = planned(
        b,
        st,
        open_second_session=parse_only_session,
        repo_url="https://github.example/akaal/db-sales",
        token_minutes_left=58,
    )
    under_the_lock = planned(b, st, lock_held=True, token_minutes_left=31)
    assert in_plan_job != under_the_lock
    assert in_plan_job.plan_sha256 == under_the_lock.plan_sha256


def test_a_plan_file_round_trips_exactly_and_holds_no_sql_text():
    b, st, db, _ = untouched_drift("dev")
    hooks = Hooks(
        refresh=[key("VIEW", "vw_dependant")],
        dependants=["VIEW:[a].[b]", "VIEW:[a].[c]"],
        before={"VIEW:[a].[b]": ["UNRESOLVED [cc]", "COLUMNS_NOT_FOUND [a].[t]"]},
    )
    for result in (
        planned(b, st, db, repo_url="https://github.example/akaal/db-sales", token_minutes_left=58),
        planned(*a_release()),
        planned(*table_release(), table_hooks=hooks),
    ):
        text = result.to_json()
        assert Plan.from_json(text) == result
        assert Plan.from_json(text).to_json() == text
        assert json.loads(text)["plan_sha256"] == result.plan_sha256
        assert "ALTER TABLE" not in text and "CREATE OR ALTER" not in text


def change_a_step(doc: dict) -> None:
    doc["units"][0]["steps"][0]["sha256"] = "0" * 64


def change_the_format(doc: dict) -> None:
    doc["format"] = 2


def drop_a_key(doc: dict) -> None:
    del doc["destructive"]


def add_a_key(doc: dict) -> None:
    doc["units"][0]["extra"] = 1


def change_a_type(doc: dict) -> None:
    doc["release_seq"] = "7"


@pytest.mark.parametrize("damage", [change_a_step, change_the_format, drop_a_key, add_a_key, change_a_type])
def test_a_plan_file_that_was_changed_or_is_of_another_shape_is_refused(damage):
    doc = json.loads(planned(*a_release()).to_json())
    damage(doc)
    error = refusal(Plan.from_json, json.dumps(doc))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "PLAN_INVALID")
    assert refusal(Plan.from_json, "not json").reason_code == "PLAN_INVALID"


def test_the_plan_links_the_two_commits_and_each_step_to_its_file_at_the_release_commit():
    spaced = ("schema/procedures/sales.usp a.sql", "CREATE OR ALTER PROCEDURE [sales].[usp a] AS SELECT 1;\n")
    old = key("PROCEDURE", "usp_old")
    two_batches = mig(
        M1, "ALTER TABLE [sales].[T] ADD [a] int NULL;", "ALTER TABLE [sales].[T] ADD [b] int NULL;"
    )
    b = bundle(two_batches, modules=[spaced], tombstones=[old])
    st = recorded(objects={old: row(old, "x")}, seq=6)
    result = planned(b, st, repo_url="https://github.example/akaal/db-sales/")
    assert (result.recorded_git_sha, result.git_sha) == (RECORDED_COMMIT, COMMIT)
    assert result.compare_url == f"https://github.example/akaal/db-sales/compare/{RECORDED_COMMIT}...{COMMIT}"
    blob = f"https://github.example/akaal/db-sales/blob/{COMMIT}"
    assert [step.url for step in result.units[0].steps] == [
        f"{blob}/migrations/{M1}#L1",  # the first batch starts with the header lines
        f"{blob}/migrations/{M1}#L5",
        f"{blob}/schema/procedures/sales.usp%20a.sql",
        f"{blob}/schema/_tombstones.toml",
        None,
    ]
    without = planned(b, st)
    assert (without.compare_url, {step.url for step in without.units[0].steps}) == (None, {None})
    first_deploy = planned(
        b, recorded(objects={old: row(old, "x")}), repo_url="https://github.example/akaal/db-sales"
    )
    assert (first_deploy.recorded_git_sha, first_deploy.compare_url) == (None, None)


# ------------------------------------------------------------------ compute_plan: table hooks
TABLE = names.object_key("TABLE", "sales", "Order")
TABLE_ROW = ObjectRow("managed", None, 1, {"columns": ["a"]}, state.capture_sha256({"columns": ["a"]}))


class Hooks:
    """Table hooks with fixed answers. It records what the planner asked."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.drift_keys: list[str] = []
        self.findings_asked: list[str] | None = None
        self.read_back_calls: list[tuple[list[str], str | None]] = []

    def touched_table_objects(self, work: Work) -> list[str]:
        assert [m.file for m in work.pending] == [M1]
        return self.answers.get("touched", [TABLE])

    def table_drift(self, session: Any, state: State, keys: Sequence[str]) -> dict[str, list[Difference]]:
        self.drift_keys = list(keys)
        return self.answers.get("drift", {})

    def blockers_and_dependants(self, session: Any, work: Work, config: Any) -> TableFindings:
        return self.answers.get("findings", TableFindings())

    def sub_object_collisions(self, session: Any, work: Work, state: State) -> list[str]:
        return self.answers.get("collisions", [])

    def refresh_set(self, session: Any, work: Work) -> list[str]:
        return self.answers.get("refresh", [])

    def unrecorded_objects(self, work: Work, state: State) -> list[str]:
        return self.answers.get("unrecorded", [])

    def dependants_to_check(self, session: Any, work: Work) -> list[str]:
        return self.answers.get("dependants", [])

    def read_back(self, session: Any, bundle: Bundle, keys: Sequence[str], through: str | None) -> dict:
        """read_back: the captures when the catalog equals the model after `through`; without the
        answer the catalog differs from that model."""
        self.read_back_calls.append((list(keys), through))
        if "read_back" not in self.answers:
            raise failed("READBACK_MISMATCH", f"{keys[0]} is not in the database as the model says")
        return self.answers["read_back"]

    def dependant_findings(self, session: Any, keys: Sequence[str]) -> dict[str, list[str]]:
        """before: key -> what the engine says of the dependant now; a key that is not there: no finding."""
        self.findings_asked = list(keys)
        return {k: list(self.answers.get("before", {}).get(k, [])) for k in keys}


def table_release() -> tuple[Bundle, State, FakeSession]:
    old, dependant = key("PROCEDURE", "usp_old"), key("VIEW", "vw_dependant")
    path, text = view("vw_dependant", "SELECT [a] FROM [sales].[Order]")
    b = bundle(mig(M1), modules=[(path, text), proc("usp_new")], tombstones=[old])
    st = recorded(objects={old: row(old, "x"), dependant: row(dependant, text), TABLE: TABLE_ROW}, seq=6)
    db = database(
        st, user_objects=[("sales", "usp_old", "P"), ("sales", "vw_dependant", "V"), ("sales", "Log", "U")]
    )
    db.respond("azsqlcd:table_facts", [[(TABLE, 120000, 900)]])
    return b, st, db


def test_table_hooks_add_the_touched_tables_the_refresh_set_and_the_row_counts():
    b, st, db = table_release()
    dependant = key("VIEW", "vw_dependant")
    broken = key("VIEW", "vw_broken")
    hooks = Hooks(refresh=[dependant], dependants=[broken], before={broken: ["UNRESOLVED [sales].[Gone]"]})
    result = planned(b, st, db, table_hooks=hooks)
    assert hooks.drift_keys == [TABLE]
    assert ids(result) == [
        f"{M1}#1",
        f"module:{key('PROCEDURE', 'usp_new')}",
        f"drop:{key('PROCEDURE', 'usp_old')}",
        f"refresh:{dependant}",
        "readback",
    ]
    assert result.units[0].steps[-1].keys == (key("PROCEDURE", "usp_new"), TABLE)
    assert (TABLE, TABLE_ROW.catalog_sha256) in [(t.key, t.catalog_sha256) for t in result.touched]
    assert [dataclasses.astuple(fact) for fact in result.table_facts] == [(TABLE, 120000, 900)]
    assert result.pre_broken == (key("VIEW", "vw_broken"),)
    assert result.unmanaged == (names.object_key("TABLE", "sales", "Log"),)
    assert [note for note in result.notes if "not modelled" in note] == []
    assert result.plan_sha256 != planned(b, st, table_release()[2], table_hooks=Hooks()).plan_sha256


# ------------------------------------------------------------------ table model: temporal tables
HISTORY = names.object_key("TABLE", "sales", "Order_History")
HISTORY_ROWS = [[("SALES", "order_history", "sales", "Order")]]  # the catalog spells names its own way
OFF = (
    "-- azsqlcd:allow TEMPORAL_OFF [sales].[Order] reason: the table goes\n"
    "ALTER TABLE [sales].[Order] SET (SYSTEM_VERSIONING = OFF);"
)
DROP = "-- azsqlcd:allow DROP_TABLE sales.[ORDER] reason: replaced in r7\nDROP TABLE [sales].[Order];"


def temporal_release(*batches: str) -> tuple[Bundle, State, FakeSession]:
    """table_release() whose database holds the history table of [sales].[Order] and one plain table."""
    st = recorded(objects={TABLE: TABLE_ROW}, seq=6)
    db = database(
        st, user_objects=[("sales", "Order", "U"), ("sales", "Order_History", "U"), ("sales", "Log", "U")]
    )
    db.respond("azsqlcd:history_tables", HISTORY_ROWS)
    db.respond("azsqlcd:table_facts", [[(TABLE, 120000, 900)]])
    return bundle(mig(M1, *batches)), st, db


def test_a_history_table_is_owned_by_the_engine_and_not_listed_as_unmanaged():
    b, st, db = temporal_release()
    result = planned(b, st, db, table_hooks=Hooks())
    # [sales].[Log] has no managed row and no owner; the history table has no managed row either
    assert result.unmanaged == (names.object_key("TABLE", "sales", "Log"),)
    assert len(db.sent("azsqlcd:history_tables")) == 1

    # a table that only has the name of a history table of another database stays unmanaged
    b, st, db = temporal_release()
    db._rules = [rule for rule in db._rules if rule.matcher != "azsqlcd:history_tables"]
    assert planned(b, st, db, table_hooks=Hooks()).unmanaged == (
        names.object_key("TABLE", "sales", "Log"),
        HISTORY,
    )


def test_without_a_table_model_the_catalog_is_not_asked_for_history_tables():
    b, st, db = temporal_release()
    result = planned(b, st, db)
    assert db.sent("azsqlcd:history_tables") == [] and result.unmanaged == ()


def test_a_plan_that_drops_a_temporal_table_says_that_its_history_table_stays_unmanaged():
    b, st, db = temporal_release(OFF, DROP)
    result = planned(b, st, db, table_hooks=Hooks())
    (note,) = [note for note in result.notes if "history table" in note]
    assert note.startswith(f"{M1} drops the system-versioned table [sales].[Order]: its history table ")
    assert "TABLE:[SALES].[order_history] stays in the database as a plain table" in note
    assert "listed as unmanaged" in note
    assert [(d.code, d.object) for d in result.destructive] == [
        ("TEMPORAL_OFF", "[sales].[Order]"),
        ("DROP_TABLE", "sales.[ORDER]"),
    ]


def test_versioning_off_for_a_table_that_stays_gets_its_own_note_and_a_plain_change_gets_none():
    b, st, db = temporal_release(OFF)
    result = planned(b, st, db, table_hooks=Hooks())
    (note,) = [note for note in result.notes if "history table" in note]
    assert note.startswith(f"{M1} switches SYSTEM_VERSIONING off for [sales].[Order], and the engine stops")
    assert "drops the system-versioned table" not in note

    # the allow line of another table's drop does not make this one a drop; no catalog row: no name
    other = "-- azsqlcd:allow DROP_TABLE [sales].[Log] reason: unused\nDROP TABLE [sales].[Log];"
    b, st, db = temporal_release(OFF, other)
    db._rules = [rule for rule in db._rules if rule.matcher != "azsqlcd:history_tables"]
    (note,) = [note for note in planned(b, st, db, table_hooks=Hooks()).notes if "history table" in note]
    assert "switches SYSTEM_VERSIONING off" in note and ": its history table stays in the database" in note

    b, st, db = temporal_release()
    assert [note for note in planned(b, st, db, table_hooks=Hooks()).notes if "history table" in note] == []


def test_a_nontx_unit_gets_no_refresh_step():
    """TQ-05: a nontx unit of work is one batch outside a transaction. A refresh step in it would
    stop every such release in the runner."""
    dependant = key("VIEW", "vw_dependant")
    path, text = view("vw_dependant", "SELECT [a] FROM [sales].[Order]")
    build = mig(M1, "CREATE INDEX [IX_a] ON [sales].[Order] ([c]) WITH (ONLINE = ON);", mode="nontx")
    b = bundle(build, modules=[(path, text)])
    st = recorded(objects={dependant: row(dependant, text), TABLE: TABLE_ROW}, seq=6)

    class Refreshes(Hooks):
        def refresh_set(self, session: Any, work: Work) -> list[str]:
            self.refresh_asked = True
            return [dependant]

    hooks = Refreshes(dependants=[dependant])
    result = planned(b, st, table_hooks=hooks)
    assert [unit.kind for unit in result.units] == ["nontx"]
    assert [step.kind for step in result.units[0].steps] == ["batch", "readback"]
    assert result.units[0].steps[-1].keys == (TABLE,)
    assert not hasattr(hooks, "refresh_asked")  # not asked: nothing could be done with the answer
    assert result.dependants == (dependant,)  # the dependants are still checked after the batch


def test_a_blocker_on_a_changed_table_refuses_the_plan():
    b, st, db = table_release()
    blockers = ("index [IX_hand] on [sales].[Order] ([c]) is not recorded",)
    error = refusal(planned, b, st, db, table_hooks=Hooks(findings=TableFindings(blockers=blockers)))
    assert (error.exit_code, error.reason_code, error.detail) == (
        Exit.REFUSED,
        "TABLE_BLOCKER",
        {"blockers": list(blockers)},
    )


def test_a_pending_create_of_an_index_or_constraint_name_that_exists_and_is_not_recorded_is_refused():
    b, st, db = table_release()
    error = refusal(planned, b, st, db, table_hooks=Hooks(collisions=["[sales].[Order].[IX_Order_c]"]))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "NAME_COLLISION")
    assert "resolve --mark-applied" in error.message


def test_drift_on_a_table_that_the_release_touches_is_refused():
    b, st, db = table_release()
    drift = {TABLE: [Difference("columns", "1" * 64, "2" * 64)]}
    error = refusal(planned, b, st, db, table_hooks=Hooks(drift=drift))
    assert (error.reason_code, error.detail["objects"][0]["object"]) == ("DRIFT_TOUCHED", TABLE)


def test_drift_on_a_table_that_equals_the_model_after_the_pending_migration_names_mark_applied():
    """N1-F2: somebody ran the statements of the pending migration by hand. --accept-drift would be
    accepted and the next deploy would send the migration again and fail (21). The action that
    works is resolve --mark-applied, which makes the same read-back."""
    b, st, db = table_release()
    drift = {TABLE: [Difference("columns", "1" * 64, "2" * 64)]}
    hooks = Hooks(drift=drift, read_back={TABLE: {"columns": ["a", "c"]}})
    error = refusal(planned, b, st, db, table_hooks=hooks)
    assert (error.reason_code, error.detail["objects"][0]["object"]) == ("DRIFT_TOUCHED", TABLE)
    assert f"azsqlcd resolve --mark-applied {M1}" in error.message
    assert "not --accept-drift" in error.message
    assert hooks.read_back_calls == [([TABLE], M1)]  # the tables of the migration, against the model after it


def test_drift_on_a_touched_table_that_is_not_the_model_after_the_migration_names_accept_drift():
    b, st, db = table_release()
    hooks = Hooks(drift={TABLE: [Difference("columns", "1" * 64, "2" * 64)]})  # the read-back differs
    error = refusal(planned, b, st, db, table_hooks=hooks)
    assert error.reason_code == "DRIFT_TOUCHED" and hooks.read_back_calls == [([TABLE], M1)]
    assert "--accept-drift" in error.message and "--mark-applied" not in error.message


def test_a_plan_with_no_drift_on_a_touched_table_makes_no_read_back():
    b, st, db = table_release()
    hooks = Hooks(read_back={})
    assert planned(b, st, db, table_hooks=hooks).outcome == WORK and hooks.read_back_calls == []


def test_without_table_hooks_the_plan_says_that_tables_are_not_modelled():
    b, st, db = table_release()
    result = planned(b, st, db)
    assert [note for note in result.notes if note.startswith("tables are not modelled")] != []
    assert (result.table_facts, result.pre_broken) == ((), ())
    assert TABLE not in [t.key for t in result.touched]
    with_model = load_config(TOML.replace("table_model = false", "table_model = true"))
    notes = planned(b, st, table_release()[2], config=with_model).notes
    assert [note for note in notes if "table_model = true" in note] != []


# ------------------------------------------------------------------ keys without case
# The catalog compares names without case (the fence refuses any other), so a key of the release
# and a key of the state that differ only in letter case name one object.
STORED = "PROCEDURE:[Sales].[USP_X]"  # as an export wrote it once
WRITTEN = key("PROCEDURE", "usp_x")  # as the file of the release spells it now


def test_a_module_file_that_differs_from_the_recorded_key_only_by_case_is_the_same_object():
    path, text = proc("usp_x")
    same = pending_work(bundle(modules=[(path, text)]), recorded(objects={STORED: row(STORED, text)}), 100)
    # not an orphan row plus a create of a second object: the database holds this text already
    assert (same.outcome, same.module_changes) == (RECORD, ())

    changed = bundle(modules=[proc("usp_x", "SELECT 2;")])
    st = recorded(objects={STORED: row(STORED, text)})
    work = pending_work(changed, st, 100)
    assert [(c.key, c.action) for c in work.module_changes] == [(WRITTEN, "alter")]

    # the name exists in the catalog and it is this module: no NAME_COLLISION, and its capture is checked
    result = planned(changed, st)
    assert [dataclasses.astuple(t) for t in result.touched] == [(WRITTEN, st.objects[STORED].catalog_sha256)]
    assert result.unmanaged == ()
    # the catalog can name the object in a third spelling: it is still the managed module
    as_the_catalog_says = database(st, user_objects=[("SALES", "usp_X", "P")])
    assert planned(changed, st, as_the_catalog_says).unmanaged == ()
    hot_fix = [live_row(STORED, "CREATE PROCEDURE [sales].[usp_x] AS SELECT 'by hand';")]
    drifted = refusal(planned, changed, st, database(st, live=hot_fix))
    assert (drifted.reason_code, drifted.detail["objects"][0]["object"]) == ("DRIFT_TOUCHED", STORED)


def test_a_module_stored_with_legacy_flags_is_found_under_a_key_of_another_case():
    path, text = proc("usp_x")
    legacy_row = row(STORED, text, source="1" * 64, ansi=False)
    st = recorded(objects={STORED: legacy_row})
    error = refusal(planned, bundle(modules=[(path, text)]), st)
    assert (error.reason_code, error.detail) == ("LEGACY_FLAGS", {"objects": [WRITTEN]})


def test_a_tombstone_that_differs_from_the_recorded_key_only_by_case_drops_that_module():
    tombstone = key("PROCEDURE", "usp_x")
    st = recorded(objects={STORED: row(STORED, "CREATE PROCEDURE [Sales].[USP_X] AS SELECT 1;")})
    work = pending_work(bundle(tombstones=[tombstone]), st, 100)
    assert work.drops == (tombstone,)  # not MODULE_ORPHAN: the tombstone names the row
    assert [(d.code, d.object) for d in work.destructive] == [("DROP_MODULE", tombstone)]
    assert [dataclasses.astuple(t) for t in planned(bundle(tombstones=[tombstone]), st).touched] == [
        (tombstone, st.objects[STORED].catalog_sha256)
    ]


def test_a_file_and_a_tombstone_that_differ_only_by_case_are_one_object_said_twice():
    b = bundle(modules=[proc("usp_x")], tombstones=["PROCEDURE:[SALES].[USP_X]"])
    error = refusal(pending_work, b, recorded(), 100)
    assert (error.reason_code, error.detail) == (
        "TOMBSTONE_CONFLICT",
        {"objects": ["PROCEDURE:[SALES].[USP_X]"]},
    )


def test_an_unbind_of_a_module_under_a_key_of_another_case_plans_the_rebind_of_its_file():
    path, text = view("vw_bound", "SELECT 1 AS [x]", " WITH SCHEMABINDING")
    stored_key = "VIEW:[SALES].[VW_BOUND]"
    st = recorded(objects={stored_key: row(stored_key, text, schema_bound=True)})
    alter = "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [a] bigint NULL;"
    b = bundle(mig(M1, alter), modules=[(path, text)])
    work = pending_work(b, st, 100)
    written = key("VIEW", "vw_bound")
    assert work.unbinds == (written,)
    assert [(c.key, c.action) for c in work.module_changes] == [(written, "rebind")]


# ------------------------------------------------------------------ table model: baseline, statement keys
def test_a_table_of_the_model_that_was_never_recorded_refuses_the_plan_before_the_drift_check():
    b, st, db = table_release()
    other = names.object_key("TABLE", "sales", "Customer")
    # the drift of a touched table would refuse too: the missing baseline is told first
    hooks = Hooks(unrecorded=[other], drift={TABLE: [Difference("columns", "1" * 64, "2" * 64)]})

    error = refusal(planned, b, st, db, table_hooks=hooks)

    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "BASELINE_REQUIRED")
    assert error.detail == {"objects": [other]} and "azsqlcd baseline" in error.message
    assert hooks.drift_keys == []  # refused before the drift step
    assert not {word for batch in db.batches for word in batch.split()} & WRITES


def test_a_statement_that_spells_a_table_in_another_letter_case_touches_the_recorded_table():
    b, st, db = table_release()
    as_written = "TABLE:[SALES].[order]"  # a dropped table keeps the spelling of its statement
    result = planned(b, st, db, table_hooks=Hooks(touched=[as_written]))
    assert (as_written, TABLE_ROW.catalog_sha256) in [(t.key, t.catalog_sha256) for t in result.touched]
    assert result.unmanaged == (names.object_key("TABLE", "sales", "Log"),)

    b, st, db = table_release()
    drift = {TABLE: [Difference("columns", "1" * 64, "2" * 64)]}  # the hooks answer under the stored key
    error = refusal(planned, b, st, db, table_hooks=Hooks(touched=[as_written], drift=drift))
    assert (error.reason_code, error.detail["objects"][0]["object"]) == ("DRIFT_TOUCHED", TABLE)


# ------------------------------------------------------------------ A6: committed, and the run not ok
def committed_not_ok(st: State, seq: int) -> State:
    """A deploy of release `seq` committed a unit of work, and its run row never became ok."""
    return dataclasses.replace(st, committed_release_seq=seq)


def test_an_older_release_is_already_past_when_a_later_release_committed_and_its_run_did_not_end_ok():
    """r7 (modules only) committed its unit; its run ended RUN_NOT_CLOSED, or the session was lost at
    COMMIT. The record still says r6. The file of r6 differs from what the database holds, and r6
    must not put its older text back (A6)."""
    path, text = proc("usp_x", "SELECT 6;")
    b = bundle(modules=[(path, text)], seq=6)
    x = key("PROCEDURE", "usp_x")
    objects = {x: row(x, "CREATE OR ALTER PROCEDURE [sales].[usp_x] AS\nSELECT 7;\n")}
    at_r6 = recorded(objects=objects, seq=6)
    assert ids(pending_work(b, at_r6, 100)) == [f"module:{x}", "readback"]  # the hole, were r7 not read
    work = pending_work(b, committed_not_ok(at_r6, 7), 100)
    assert (work.outcome, work.units, work.module_changes) == (ALREADY_PAST, (), ())
    result = planned(b, committed_not_ok(at_r6, 7))
    assert (result.outcome, result.pending, result.units) == (ALREADY_PAST, False, ())
    (note,) = [note for note in result.notes if "ALREADY_PAST" in note]
    assert "r7" in note and "did not end ok" in note


def test_the_release_that_committed_and_did_not_end_ok_is_planned_again_and_a_later_one_too():
    path, text = proc("usp_x", "SELECT 7;")
    x = key("PROCEDURE", "usp_x")
    st = committed_not_ok(recorded(objects={x: row(x, text)}, seq=6), 7)
    assert pending_work(bundle(modules=[(path, text)], seq=7), st, 100).outcome == RECORD
    newer = bundle(modules=[proc("usp_x", "SELECT 8;")], seq=8)
    assert ids(pending_work(newer, st, 100)) == [f"module:{x}", "readback"]


def test_a_release_that_committed_and_lacks_a_migration_of_this_one_is_a_diverged_chain():
    b = bundle(mig(M1), mig(M2), seq=5)
    error = refusal(pending_work, b, committed_not_ok(recorded(steps=applied(b, M1), seq=4), 8), 100)
    assert (error.reason_code, error.detail["migration"]) == ("CHAIN_DIVERGED", M2)


# ------------------------------------------------------------------ a replacement that is merged late
def late_replacement() -> Bundle:
    """r3 added M1. r4 added M2, which failed in test. r5 withdrew M2. r6 added M3. r7 adds M4,
    the replacement of M2: its place in the chain is before M3, which databases applied already."""
    return bundle(
        mig(M1, added=3),
        mig(M2, "ALTER TABLE [sales].[Order] ADD [w] int NOT NULL;", added=4, withdrawn=True),
        mig(M3, "ALTER TABLE [sales].[Order] ADD [d] int NULL;", added=6),
        mig(M4, "ALTER TABLE [sales].[Order] ADD [w] int NULL;", added=7, replaces=M2),
        seq=7,
    )


def test_a_replacement_merged_after_a_later_migration_is_planned_where_the_withdrawn_one_was_skipped():
    """Such a database took M3 with r6. It is not diverged: it never had the withdrawn migration,
    and the replacement is the one migration that it lacks. The plan says that the order differs."""
    b = late_replacement()
    skipped = recorded(steps=applied(b, M1, M3), seq=6)
    work = pending_work(b, skipped, 100)
    assert [m.file for m in work.pending] == [M4] and ids(work) == [f"{M4}#1"]
    (note,) = work.warnings
    assert M4 in note and M2 in note and M3 in note and "order" in note
    # a database that applied the withdrawn migration has its change already
    assert pending_work(b, recorded(steps=applied(b, M1, M2, M3), seq=6), 100).outcome == RECORD
    # a database that is behind the later migration takes the chain order, with no note
    behind = bundle(*(dataclasses.replace(line, added=7) for line in _lines(b)), seq=7)
    in_order = pending_work(behind, recorded(steps=applied(behind, M1), seq=6), 100)
    assert [m.file for m in in_order.pending] == [M4, M3] and in_order.warnings == ()


def _lines(b: Bundle) -> list[Line]:
    entries = chain.parse_sum(b.files[chain.SUM_PATH].decode()).entries
    return [
        Line(e.file, b.files[f"migrations/{e.file}"].decode(), e.mode, None, e.withdrawn, e.replaces)
        for e in entries
    ]


def test_a_hole_that_is_not_a_late_replacement_is_still_a_diverged_chain():
    b = late_replacement()
    m5 = "0005__e.sql"
    longer = bundle(*_lines(b), mig(m5), seq=7)
    # M3 is not applied and the later m5 is: no replacement explains that
    state_with_hole = recorded(steps=applied(longer, M1, m5), seq=6)
    error = refusal(pending_work, longer, state_with_hole, 100)
    assert (error.reason_code, error.detail["migration"]) == ("CHAIN_DIVERGED", M3)


# ------------------------------------------------------------------ A7: the first deploy to a database
def chain_of_two_releases(**options: Any) -> Bundle:
    return bundle(
        mig(M1, added=3), mig(M2, "ALTER TABLE [sales].[Order] ADD [d] int NULL;"), seq=7, **options
    )


def test_the_first_deploy_to_an_empty_database_applies_a_chain_that_came_with_several_releases():
    """A new target (a sandbox, a second region) has no release to be at, and the bundles of the
    earlier releases do not know it. With no recorded release and no migration step, on a chain
    with no baseline line, the whole chain runs with this release, in one unit of work."""
    b = chain_of_two_releases(modules=[proc("usp_a")])
    work = pending_work(b, recorded(), 100)
    assert [m.file for m in work.pending] == [M1, M2]
    assert [unit.kind for unit in work.units] == ["tx"]
    assert ids(work) == [f"{M1}#1", f"{M2}#1", f"module:{key('PROCEDURE', 'usp_a')}", "readback"]
    (note,) = work.warnings
    assert "first deploy" in note and M1 in note and "r7" in note


@pytest.mark.parametrize(
    "not_new",
    [
        {"seq": 2},  # a release is recorded
        {"committed": 2},  # a deploy committed a unit of work and did not end ok
        {"steps": [StepRow(1, 1, "nontx", "0009__other.sql", "f" * 64, "not_applied", None)]},
        {"steps": [StepRow(1, 1, "baseline", None, None, "ok", None)]},  # onboarded, not new
    ],
)
def test_a_database_with_a_record_or_a_migration_step_is_still_caught_up_release_by_release(not_new):
    b = chain_of_two_releases()
    st = recorded(seq=not_new.get("seq", 0), steps=not_new.get("steps", ()))
    st = dataclasses.replace(st, committed_release_seq=not_new.get("committed", 0))
    error = refusal(pending_work, b, st, 100)
    assert (error.reason_code, error.detail.get("promote")) == ("CATCHUP_REQUIRED", 3)


def test_a_chain_with_a_baseline_line_has_no_first_deploy_of_the_whole_chain():
    """Such a database is onboarded with azsqlcd baseline; after it the rule of A7 holds."""
    b = chain_of_two_releases(baseline=True)
    assert refusal(pending_work, b, recorded(), 100).reason_code == "BASELINE_REQUIRED"
    baselined = recorded(steps=[StepRow(1, 1, "baseline", None, None, "ok", None)])
    error = refusal(pending_work, b, baselined, 100)
    assert (error.reason_code, error.detail["promote"]) == ("CATCHUP_REQUIRED", 3)


def test_the_first_deploy_of_a_chain_with_a_nontx_migration_is_refused_and_names_the_release_before_it():
    """N2-F4. Before, the message named no release (r<n-1> is right only by chance); the release
    to start from is the one before the release that added the non-transactional migration."""
    b = bundle(mig(M1, added=3), dataclasses.replace(NONTX, added=None), seq=7)
    error = refusal(pending_work, b, recorded(), 100)
    assert (error.reason_code, error.detail["also_pending"]) == ("NONTX_NOT_ALONE", [M1])
    assert "promote r6 first" in error.message and "If the other changes" not in error.message
    # a non-transactional migration of the first release has no release before it
    first = bundle(dataclasses.replace(NONTX, added=1), modules=[proc("usp_a")], seq=1)
    assert "promote" not in refusal(pending_work, first, recorded(), 100).message


# ------------------------------------------------------------------ what the sweep checks is sealed (A12)
def test_the_findings_of_each_dependant_before_the_change_are_read_and_sealed_in_the_plan():
    """RO-1, RO-2: on a real database the engine reports findings for sound modules. The plan
    stores what it reports before the change; the runner fails a dependant only for a finding that
    is not in this list. The list decides what a deploy ignores, so it is in plan_sha256."""
    b, st, _ = table_release()
    dependant, reader = key("VIEW", "vw_dependant"), key("PROCEDURE", "usp_reader")
    old = ["UNRESOLVED [cc]", "COLUMNS_NOT_FOUND [sales].[Order]"]
    hooks = Hooks(dependants=[reader, dependant], before={reader: old})
    result = planned(b, st, table_release()[2], table_hooks=hooks)
    assert hooks.findings_asked == [reader, dependant]  # exactly the dependants to check
    assert result.dependant_findings == {reader: tuple(sorted(old)), dependant: ()}  # every key, sorted
    assert result.pre_broken == (reader,)  # for the approver: the dependants with a finding
    doc = json.loads(result.to_json())
    assert doc["dependant_findings"] == {reader: sorted(old), dependant: []}
    assert Plan.from_json(result.to_json()) == result
    # another list of findings is another plan: a finding that comes or goes after the approval
    # makes the plan stale
    for other in ({reader: old[:1]}, {reader: [*old, "ERROR_2020"]}, {dependant: old}, {}):
        changed = planned(
            b, st, table_release()[2], table_hooks=Hooks(dependants=[reader, dependant], before=other)
        )
        assert changed.plan_sha256 != result.plan_sha256
    # the order in which the hooks give the findings does not change the plan
    turned = Hooks(dependants=[reader, dependant], before={reader: old[::-1]})
    assert planned(b, st, table_release()[2], table_hooks=turned).plan_sha256 == result.plan_sha256
    # without dependants nothing is asked, and a plan without a table model has no findings
    assert planned(b, st, table_release()[2]).dependant_findings == {}


def test_a_dependant_for_which_the_hooks_give_no_findings_is_never_read_as_sound():
    class NoAnswer(Hooks):
        def dependant_findings(self, session: Any, keys: Sequence[str]) -> dict[str, list[str]]:
            return {}

    b, st, db = table_release()
    with pytest.raises(KeyError):
        planned(b, st, db, table_hooks=NoAnswer(dependants=[key("VIEW", "vw_dependant")]))


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc.update(dependant_findings=[]),
        lambda doc: doc.update(dependant_findings={"VIEW:[a].[b]": "UNRESOLVED [cc]"}),
        lambda doc: doc.update(dependant_findings={"VIEW:[a].[b]": [1]}),
        lambda doc: doc.pop("dependant_findings"),
    ],
)
def test_a_plan_file_with_dependant_findings_of_another_shape_is_refused(change):
    b, st, db = table_release()
    doc = json.loads(planned(b, st, db, table_hooks=Hooks(dependants=["VIEW:[a].[b]"])).to_json())
    change(doc)
    error = refusal(Plan.from_json, json.dumps(doc))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "PLAN_INVALID")


def test_a_plan_file_whose_dependant_findings_were_changed_is_refused():
    b, st, db = table_release()
    hooks = Hooks(dependants=["VIEW:[a].[b]"], before={"VIEW:[a].[b]": ["UNRESOLVED [cc]"]})
    doc = json.loads(planned(b, st, db, table_hooks=hooks).to_json())
    doc["dependant_findings"]["VIEW:[a].[b]"].append("COLUMNS_NOT_FOUND [sales].[Order]")
    error = refusal(Plan.from_json, json.dumps(doc))
    assert (error.reason_code, "plan_sha256" in error.message) == ("PLAN_INVALID", True)


def test_the_dependants_to_check_are_read_at_plan_time_and_sealed_in_the_plan():
    """After DROP TABLE or a rename the catalog no longer names the dependants of the table, so the
    list is taken before the change: by the plan."""
    b, st, _ = table_release()
    dependant, reader = key("VIEW", "vw_dependant"), key("PROCEDURE", "usp_reader")
    result = planned(b, st, table_release()[2], table_hooks=Hooks(dependants=[dependant, reader]))
    assert result.dependants == (reader, dependant)  # in key order
    assert json.loads(result.to_json())["dependants"] == [reader, dependant]
    assert Plan.from_json(result.to_json()) == result
    fewer = planned(b, st, table_release()[2], table_hooks=Hooks(dependants=[dependant]))
    assert fewer.plan_sha256 != result.plan_sha256
    # without a table model, and for a release with no migration, nothing is swept
    assert planned(b, st, table_release()[2]).dependants == ()
    no_migration = bundle(modules=[proc("usp_new")], seq=7)
    assert planned(no_migration, recorded(seq=6), table_hooks=Hooks(dependants=[reader])).dependants == ()


# ------------------------------------------------------------------ indexed views
@pytest.mark.parametrize("recorded_source", ["1" * 64, None])
def test_a_change_of_an_indexed_view_is_refused_at_plan_time(recorded_source):
    """ALTER VIEW drops every index of the view, and CREATE OR ALTER VIEW is that ALTER. The tool
    would report exit 0 for a view that lost its clustered index. So: no ALTER, for a changed
    file and for an overwrite after a baseline."""
    path, text = view("vw_totals", "SELECT 2 AS [x]")
    v = key("VIEW", "vw_totals")
    b = bundle(modules=[(path, text), proc("usp_a")])
    st = recorded(
        objects={v: row(v, "CREATE VIEW [sales].[vw_totals] AS SELECT 1 AS [x];", source=recorded_source)},
        seq=6,
    )
    error = refusal(planned, b, st, database(st, indexed_views=["vw_totals"]))
    assert (error.exit_code, error.reason_code) == (Exit.REFUSED, "INDEXED_VIEW")
    assert error.detail == {"objects": [v]} and "index" in error.message
    # the same release on a database where the view has no index is planned
    assert f"module:{v}" in ids(planned(b, st))


def test_an_unbind_of_an_indexed_view_is_refused_at_plan_time():
    path, text = view("vw_bound", "SELECT [a] FROM [sales].[T]", " WITH SCHEMABINDING")
    bound = key("VIEW", "vw_bound")
    unbinds = mig(
        M1, "-- azsqlcd:unbind [sales].[vw_bound]\nALTER TABLE [sales].[T] ALTER COLUMN [a] bigint;"
    )
    b = bundle(unbinds, modules=[(path, text)])
    st = recorded(objects={bound: row(bound, text, schema_bound=True)}, seq=6)
    error = refusal(planned, b, st, database(st, indexed_views=["vw_bound"]))
    assert (error.exit_code, error.reason_code, error.detail) == (
        Exit.REFUSED,
        "INDEXED_VIEW",
        {"objects": [bound]},
    )
    assert "unbind" in [step.kind for step in planned(b, st).units[0].steps]


def test_only_a_managed_view_that_the_release_alters_is_asked_for_an_index():
    """A new view has no index to lose, a procedure has none, and a drop is a tombstone that a
    reviewer approved: the read is for the views that would get an ALTER."""
    changed, old = key("VIEW", "vw_changed"), key("VIEW", "vw_old")
    b = bundle(
        modules=[view("vw_changed", "SELECT 2 AS [x]"), view("vw_new"), proc("usp_a")], tombstones=[old]
    )
    objects = {
        changed: row(changed, "CREATE VIEW [sales].[vw_changed] AS SELECT 1 AS [x];", source="1" * 64),
        old: row(old, "CREATE VIEW [sales].[vw_old] AS SELECT 1 AS [x];"),
    }
    st = recorded(objects=objects, seq=6)
    db = database(st)
    planned(b, st, db)
    (asked,) = db.sent("azsqlcd:has_index")
    assert "N'[sales].[vw_changed]'" in asked


# ------------------------------------------------------------------ RP2-7, RP2-4
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome", "noop"),
        ("environment", "prod"),
        ("target_id", "sales-prod"),
        ("git_sha", "d" * 40),
        ("release_seq", 99),
    ],
)
def test_a_plan_file_with_an_edited_outcome_environment_target_or_release_is_refused(field, value):
    """RP2-7: a reader of plan.json (the approver, the release asset) sees these fields. They are
    equal in the plan job and under the lock, so plan_sha256 covers them."""
    doc = json.loads(planned(*a_release()).to_json())
    assert doc[field] != value
    doc[field] = value
    error = refusal(Plan.from_json, json.dumps(doc))
    assert (error.reason_code, "plan_sha256" in error.message) == ("PLAN_INVALID", True)


def chain_of_three_releases() -> Bundle:
    return bundle(
        mig(M1, added=3),
        mig(M2, "ALTER TABLE [sales].[Order] ADD [d] int NULL;", added=5),
        mig("0003__c.sql", "ALTER TABLE [sales].[Order] ADD [e] int NULL;"),
        seq=7,
    )


def test_a_migration_that_resolve_marked_applied_on_a_new_database_does_not_end_the_first_deploy():
    """RP2-4: NAME_COLLISION on a first deploy says `resolve --mark-applied <migration>`. That step
    is no deploy: the database still has no release to be caught up from."""
    b = chain_of_three_releases()
    marked = StepRow(1, 1, "migration", M1, applied(b, M1)[0].file_sha256, "ok", "marked applied by resolve")
    work = pending_work(b, recorded(steps=[marked]), 100)
    assert [m.file for m in work.pending] == [M2, "0003__c.sql"]
    assert any("first deploy" in note for note in work.warnings)


def test_a_migration_that_a_deploy_applied_still_ends_the_first_deploy():
    b = chain_of_three_releases()
    step = StepRow(1, 1, "migration", M1, applied(b, M1)[0].file_sha256, "ok", None)
    error = refusal(pending_work, b, recorded(steps=[step]), 100)
    assert (error.reason_code, error.detail.get("promote")) == ("CATCHUP_REQUIRED", 5)
